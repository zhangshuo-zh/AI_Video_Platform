"""SQLite 连接管理。建表不在这里——那是 migrate.py 的事。

为什么用 contextmanager 而不是连接池：2—5 人、单进程、写操作低频，
一个请求一条连接最省心；WAL 让读不挡写。

`transaction()` 是这一轮补的例外：少数跨多表的写（删账号＝转移 N 个项目 ＋ 删一行）
必须是一个原子动作，不能让仓储层"一个方法一条连接一次提交"把它切成十几段。
"""
import contextvars
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

# 当前正在使用的事务连接。放在 contextvar 而不是函数参数上：
# 仓储层的签名不必为了事务改一遍，也不用把 conn 一路透传到每个方法。
# FastAPI 每个请求一个上下文、worker 单线程顺序跑，两边都不会串线。
_HELD = contextvars.ContextVar("fv_held_conn", default=None)


# 本进程是否已确认过"库确实是 WAL 模式"。见 `_connect()` 里的说明。
_WAL_VERIFIED = False


def _connect() -> sqlite3.Connection:
    """开一条连接。**不在这里设 journal_mode**（2026-11-09 外部审查第 1 条）。

    原来这里每条连接都执行一次 `PRAGMA journal_mode=WAL`，有两个问题：

    1. **它是白花的**。WAL 是**数据库级的持久状态**（写进库文件头），
       设一次就一直有效，不是连接级设置。所以第 2 条之后的连接再执行它，
       作用只是把"读一下当前模式"这一步做一遍——实测在已经是 WAL 的库上
       每条连接多花 206.9 µs。这是热路径（每个请求、每次仓储调用都开连接）。
       （顺带纠正一个常见说法：并不是"运行时并发会因此失败"——库能起来
       就说明它已经是 WAL 了，我按 6 轮 × 3 进程 × 每进程 40 次连接压过，
       0 次失败。真正的风险是下面第 2 点。）
    2. **首次建库时它会真的抢锁**。`PRAGMA journal_mode=WAL` 需要排他锁，
       而 `busy_timeout`／`timeout=5.0` **对 PRAGMA 不生效**。多进程同时
       首次连一个非 WAL 的库时，抢不到的那几个直接吃
       `OperationalError: database is locked`（实测 3 轮里有 1 轮复现）。
       这件事的正确归属是 `migrate._ensure_wal()`——它带指数退避重试，
       而且 `main.py` 与 `worker.py` 都在任何 `get_conn()` 之前调 `migrate()`。

    所以这里只保留 `foreign_keys`（**这个**是连接级设置，每条连接都要设），
    再补一个一次性兜底：万一有人绕过 migrate 直接用 db（脚本、测试夹具），
    第一次连接时看一眼模式，不是 wal 才去补——只查一次，不回到每条连接都查。
    """
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # 这两条 PRAGMA 必须在 BEGIN 之前执行：外键约束是连接级的
    conn.execute("PRAGMA foreign_keys=ON")
    global _WAL_VERIFIED
    if not _WAL_VERIFIED:
        _ensure_wal_once(conn)
        _WAL_VERIFIED = True
    return conn


def _ensure_wal_once(conn: sqlite3.Connection) -> None:
    """首次连接时确认一次库是 WAL；不是就补上。只在进程内跑一次。

    给"绕过 `migrate()` 直接用 db"的路径兜底（脚本、测试、故障演练工具）。
    正常请求路径上 `main.py`／`worker.py` 都已经在更早的 `migrate()` 里
    设好了，所以这里通常只读到 `wal` 就直接返回。
    """
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    except sqlite3.Error:
        return                      # 库还没建出来：交给 migrate 去建
    if str(mode or "").lower() == "wal":
        return
    # 需要补设：复用 migrate 的带重试实现，避免自己再写一遍退避。
    # 延迟导入是因为 migrate 在模块级 import 了 db，顶层互相 import 会成环。
    import migrate
    migrate._ensure_wal(conn)


@contextmanager
def get_conn():
    held = _HELD.get()
    if held is not None:
        # 已经在 transaction() 里：这条连接由 transaction() 收尾，
        # 这里既不能 commit 也不能 close——提前提交就等于把半个事务写出去了
        yield held
        return
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def transaction():
    """把一段跨表写收进**同一个**事务；里面的每次 `get_conn()` 借用同一条连接。

    为什么需要：仓储层每个方法各开一条连接、各提交一次，于是"删账号"实际上是
    十几个独立事务——第 3 个项目转移失败时前 2 个已经落库、账号还在，
    库里留下半套状态，而且没有任何人会立刻发现。
    `BEGIN IMMEDIATE` 与 `jobs.claim_next` 同一写法：写锁一开始就拿到，
    中途不会有别人的写插进来。

    一条边界：**事务里只做库内写**。要动 Git 提交或删文件的那些步骤不包进来——
    文件删了、提交做了，事务回滚不会把它们带回去，"看起来原子"比不原子更危险。
    那种流程改成"做完再收口，中途失败就把已经做到的部分讲清楚"。
    """
    held = _HELD.get()
    if held is not None:
        yield held          # 嵌套：沿用外层那一个事务，收尾由外层负责
        return
    conn = _connect()
    token = _HELD.set(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _HELD.reset(token)
        conn.close()
