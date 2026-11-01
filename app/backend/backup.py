"""备份、校验与恢复（W18）。

    python backup.py create [输出目录]
    python backup.py verify <备份目录>
    python backup.py restore <备份目录> --yes

为什么不是 cp -r data：
- 库开着 WAL，直接拷 .db 会漏掉还在 -wal 里的已提交数据；
  这里用 SQLite 自己的 backup() 取一份一致的快照。
- 工作区是一个 Git 仓库、jobs 下是日志与产物、files 下是定妆图；
  四样必须一起备份，只拿库会得到"库里说文件存在、盘上没有"的半截状态。

恢复是破坏性动作：必须显式 `--yes`，并且会先把当前 data/ 再备份一份到
`data/restore-safety-<时间>/`，出问题还能回来。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from config import DB_PATH, FILES_DIR, JOBS_DIR, WORKSPACE_DIR

MANIFEST = "manifest.json"
def _parts():
    """每次现取路径，不在 import 时冻住：测试与换目录都以当前配置为准。"""
    return (("files", FILES_DIR), ("workspace", WORKSPACE_DIR), ("jobs", JOBS_DIR))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.rglob("*") if p.is_file())


def _copy_tree(src: Path, dst: Path) -> int:
    if not src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        return 0
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return _count_files(dst)


def _schema_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def create(out_dir: str | Path | None = None) -> Path:
    """做一份完整备份，返回备份目录。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(out_dir) if out_dir else DB_PATH.parent / "backups" / stamp
    target.mkdir(parents=True, exist_ok=True)
    db_copy = target / "platform.db"
    source = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(db_copy)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    counts = {}
    for name, src in _parts():
        counts[name] = _copy_tree(src, target / name)

    manifest = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "schema_version": _schema_version(db_copy),
        "db_sha256": _sha256(db_copy),
        "db_bytes": db_copy.stat().st_size,
        "file_counts": counts,
        "source": {"db": str(DB_PATH), "files": str(FILES_DIR),
                   "workspace": str(WORKSPACE_DIR), "jobs": str(JOBS_DIR)},
    }
    (target / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return target


def verify(target: str | Path) -> dict:
    """校验备份完整性。返回一份可打印的报告；任何一项不过就抛 RuntimeError。"""
    root = Path(target)
    if not (root / MANIFEST).is_file():
        raise RuntimeError(f"不是备份目录（缺 {MANIFEST}）：{root}")
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    db = root / "platform.db"
    if not db.is_file():
        raise RuntimeError("备份里没有 platform.db")
    if _sha256(db) != manifest.get("db_sha256"):
        raise RuntimeError("platform.db 的 sha256 与清单不符（文件被改过或拷坏了）")

    conn = sqlite3.connect(db)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("PRAGMA integrity_check 没有返回 ok")
        foreign = conn.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            raise RuntimeError(f"外键检查有 {len(foreign)} 条不一致")
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    finally:
        conn.close()
    if int(version or 0) != int(manifest.get("schema_version") or 0):
        raise RuntimeError("schema_version 与清单不符")

    counts = {name: _count_files(root / name) for name, _src in _parts()}
    if counts != manifest.get("file_counts"):
        raise RuntimeError(f"文件数量与清单不符：清单 {manifest.get('file_counts')}，实际 {counts}")
    return {"manifest": manifest, "counts": counts, "schema_version": version,
            "foreign_keys": "ok", "integrity": "ok"}


def restore(target: str | Path, yes: bool = False) -> Path:
    """把备份恢复回运行目录。必须先 verify，且必须显式 yes。"""
    if not yes:
        raise RuntimeError("恢复会覆盖当前 data/，确认后加 --yes")
    root = Path(target)
    verify(root)                     # 坏备份绝不往运行目录里倒

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safety = DB_PATH.parent / f"restore-safety-{stamp}"
    safety.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(safety / "platform.db")
    try:
        conn.backup(dest)
    finally:
        dest.close()
        conn.close()
    for name, src in _parts():
        if src.exists():
            shutil.copytree(src, safety / name, dirs_exist_ok=True)

    # 库：先删 sidecar 再换主文件，避免旧 -wal 盖到新库上
    for suffix in ("-wal", "-shm"):
        Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    shutil.copy2(root / "platform.db", DB_PATH)
    for name, dst in _parts():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(root / name, dst)
    return safety


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="平台数据备份与恢复")
    sub = parser.add_subparsers(dest="command", required=True)
    p_create = sub.add_parser("create", help="做一份完整备份")
    p_create.add_argument("out", nargs="?", help="输出目录；默认 data/backups/<时间>")
    p_verify = sub.add_parser("verify", help="校验一份备份")
    p_verify.add_argument("path")
    p_restore = sub.add_parser("restore", help="恢复一份备份（覆盖 data/）")
    p_restore.add_argument("path")
    p_restore.add_argument("--yes", action="store_true", help="确认覆盖当前数据")
    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            target = create(args.out)
            print(f"备份完成：{target}")
            print(f"  库 {target / 'platform.db'}")
            return 0
        if args.command == "verify":
            report = verify(args.path)
            print(f"备份可用：schema {report['schema_version']}，"
                  f"integrity {report['integrity']}，外键 {report['foreign_keys']}")
            print(f"  文件：{report['counts']}")
            return 0
        safety = restore(args.path, args.yes)
        print(f"已恢复：{args.path}")
        print(f"恢复前的数据另存：{safety}")
        return 0
    except RuntimeError as exc:
        print(f"失败：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
