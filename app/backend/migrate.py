"""数据库迁移：按文件名顺序执行 migrations/*.sql，版本记在 schema_version。

为什么不用 alembic：只有四张表、单进程、单机部署，引入一整套迁移工具
比它解决的问题还多。规则就三条——文件名四位数字开头、按数字升序、
跑过的记一行。

用法：
    python migrate.py          跑还没跑的
    python migrate.py status   看已应用的版本
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from config import DB_PATH
from db import get_conn

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
NAME_PATTERN = re.compile(r"^(\d{4})_.+\.sql$")
# 只留最近几份备份，免得数据目录越攒越长
MAX_BACKUPS = 5


def _has_tables(conn) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None


def _drop_backup(path: Path) -> None:
    """删一份备份，连它的 -wal/-shm 一起删。

    备份库也是 WAL 模式，close() 之后 sidecar 不一定消失（本机 data/ 里就留着
    两份主文件已被删、sidecar 还在的孤儿）。孤儿不但占地方，还会被下一轮
    清理的 glob 当成独立备份计数——于是"保留 5 份"实际只留了两三份。
    """
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        p.unlink(missing_ok=True)


def _backup(conn, *, verbose: bool = True) -> Path | None:
    """有未应用的迁移时，先把整份库备份下来。

    用 SQLite 自己的 backup() 而不是拷文件：库开着 WAL，
    直接 copy .db 会漏掉还在 -wal 里的已提交数据，备份出来是旧的。
    """
    target = DB_PATH.parent / f"{DB_PATH.name}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()

    # 只数主文件：sidecar 跟着主文件走，不算独立备份
    keep = sorted(p for p in DB_PATH.parent.glob(f"{DB_PATH.name}.bak-*")
                  if not p.name.endswith(("-wal", "-shm")))
    for old in keep[:-MAX_BACKUPS]:
        _drop_backup(old)
    if verbose:
        print(f"迁移前已备份：{target.name}")
    return target


def _discover() -> list[tuple[int, Path]]:
    """返回 [(版本号, 文件路径)]，顺序即执行顺序。"""
    found: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = NAME_PATTERN.match(path.name)
        if not match:
            raise RuntimeError(f"迁移文件名不合规（要 0001_xxx.sql）：{path.name}")
        found.append((int(match.group(1)), path))
    return found


def _split_statements(sql: str) -> list[str]:
    """按分号切语句。

    只支持普通的 DDL/DML：触发器体里那种带分号的语句会被切坏，别写。
    之所以不直接 executescript，是因为它会隐式提交，
    导致"迁移执行到一半失败"时前面的语句已经落库。
    """
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def _ensure_version_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            name       TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _connect() -> sqlite3.Connection:
    """迁移专用连接：关掉驱动的隐式事务，改由我们自己 BEGIN/COMMIT。

    必须这样做：Python 的 sqlite3 只会为 INSERT/UPDATE 这类 DML 自动开事务，
    CREATE TABLE 走的是自动提交。不手动包一层的话，一个迁移跑到一半失败，
    前面建的表已经落库了，回滚回不去。

    ## 为什么 WAL 要先读再设（2026-11-08，实测出的必现缺陷）

    `PRAGMA journal_mode=WAL` **不是幂等的无副作用操作**：它要对库文件加
    排他锁才能改写文件头。而 **PRAGMA 不受 `busy_timeout` 保护**——
    上面那个 `timeout=5.0` 对它是**不生效**的，锁着就直接抛
    `sqlite3.OperationalError: database is locked`。

    于是「两个进程同时启动」这条真实路径必炸：`main.py` 与 `worker.py`
    都调 `migrate()`，`run.sh` 拉起 API+worker 正好就是同时启动。
    基准脚本量出来是 **5 轮 5 次失败**（`tools/bench_queries.py` 里
    `_probe_migrate_race`），失败点就在这一行。

    改法：**先读回当前模式，已经就是 wal 就什么都不做**。WAL 是**库级持久
    状态**，写出去了就一直有效——没有理由每个连接都重设一遍。
    只有真的是新库（或别人把它改回 delete 模式）时才设，此时再加重试兜住
    并发首次建库那一下争锁。
    """
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None          # 手动事务
    _ensure_wal(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_wal(conn: sqlite3.Connection, *, attempts: int = 5) -> None:
    """把库切到 WAL 模式；已经是 wal 就跳过。

    读 `PRAGMA journal_mode` 返回一行一列（模式名，小写）。
    设的时候带重试：并发首次建库时两个进程都要抢那把排他锁，
    抢不到的那个**不该让整次启动失败**——退避几十毫秒再试即可，
    因为先到的那一个成功之后库就永久是 wal 了，后来者会直接走"跳过"那条路。
    """
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    except sqlite3.Error:
        mode = ""
    if str(mode or "").lower() == "wal":
        return
    delay = 0.02
    for attempt in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def migrate(*, verbose: bool = True) -> int:
    """执行所有未应用的迁移，返回本次应用的个数。重复调用是安全的。"""
    applied_now = 0
    conn = _connect()
    try:
        _ensure_version_table(conn)      # 建版本表本身不该被别的迁移回滚，放事务外
        applied = {row["version"] for row in conn.execute("SELECT version FROM schema_version")}

        # 老库认领：schema.sql 时代建的库没有 schema_version，但 project 表已经在。
        # 直接记成已应用，避免重跑 0001 撞 "table already exists"。
        if not applied and _table_exists(conn, "project"):
            conn.execute(
                "INSERT INTO schema_version (version, name) VALUES (1, '0001_init.sql')"
            )
            applied = {1}
            if verbose:
                print("采纳既有数据库，0001_init.sql 记为已应用")

        # 重建表（DROP + RENAME）在开着外键时会失败，迁移期间先关掉。
        # PRAGMA 不能在事务里改，所以放在 BEGIN 之前。
        conn.execute("PRAGMA foreign_keys=OFF")

        pending = [(v, p) for v, p in _discover() if v not in applied]
        # 只有真要动库时才备份；已经是最新版本就什么都不做。
        # 空库（首次安装）不备份——那会儿还没有任何东西值得救。
        if pending and _has_tables(conn):
            _backup(conn, verbose=verbose)

        for version, path in pending:
            # 一个迁移一个事务：这个失败，前面的迁移不受影响，它自己完全回滚
            conn.execute("BEGIN IMMEDIATE")
            try:
                # **拿到写锁之后再看一眼**（2026-11-08，实测出的必现缺陷）。
                #
                # `applied` 是在 BEGIN 之外读的，两个进程同时启动时都读到
                # 同一份集合，于是都认为这一版还没应用——后一个执行到
                # `INSERT INTO schema_version` 就撞主键
                # （`UNIQUE constraint failed: schema_version.version`），
                # 整次启动失败。基准里 8 轮撞了 2 轮。
                #
                # `BEGIN IMMEDIATE` 已经在开头拿到写锁，所以这里的读是
                # **串行化之后**的事实：如果别人刚应用完这一版，直接跳过
                # 本轮，不重复执行（重复 CREATE TABLE 也是错）。
                already = conn.execute(
                    "SELECT 1 FROM schema_version WHERE version = ?", (version,)
                ).fetchone()
                if already is not None:
                    conn.execute("ROLLBACK")
                    if verbose:
                        print(f"跳过 {path.name}（另一个进程已应用）")
                    continue
                for statement in _split_statements(path.read_text(encoding="utf-8")):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                    (version, path.name),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            applied_now += 1
            if verbose:
                print(f"应用 {path.name}")

        conn.execute("PRAGMA foreign_keys=ON")
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"迁移后外键校验不通过：{broken}")
    finally:
        conn.close()
    return applied_now


def status() -> None:
    with get_conn() as conn:
        _ensure_version_table(conn)
        rows = conn.execute(
            "SELECT version, name, applied_at FROM schema_version ORDER BY version"
        ).fetchall()
    if not rows:
        print("还没有应用任何迁移")
        return
    for row in rows:
        print(f"{row['version']:04d}  {row['name']:<24} {row['applied_at']}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        status()
    else:
        count = migrate()
        print(f"完成，本次应用 {count} 个迁移")
