"""Job 的日志与产物落盘：路径换算、边界校验、读日志。

三条规矩，和 workspace.py 同一套思路：
1. 库里只存**相对 JOBS_DIR 的路径**（`<job_id>/logs/run.log`），不回传绝对路径；
2. 读的时候再做一次真实路径比对——库里的路径可以被手改，不能拿它直接 open；
3. 删项目时整目录摘掉，历史成本仍由删掉的 Job 行负责（项目已删，无账可对）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from config import JOBS_DIR


class JobPathError(Exception):
    """路径越界或不是合法相对路径。"""


def job_dir(job_id: int) -> Path:
    return JOBS_DIR / str(int(job_id))


def ensure_dirs(job_id: int) -> tuple[Path, Path]:
    logs = job_dir(job_id) / "logs"
    outputs = job_dir(job_id) / "outputs"
    logs.mkdir(parents=True, exist_ok=True)
    outputs.mkdir(parents=True, exist_ok=True)
    return logs, outputs


def relative(path: Path) -> str:
    """把 JOBS_DIR 下的绝对路径换成库里的相对写法。"""
    return path.resolve().relative_to(JOBS_DIR.resolve()).as_posix()


def _resolve(rel_path: str) -> Path:
    # 反斜杠在 POSIX 里不是分隔符，但库里若混进 Windows 写法的 "..\\x"，
    # 先换成 "/" 再按段检查，边界就只有一条规则
    rel_path = (rel_path or "").strip().replace("\\", "/")
    if not rel_path or rel_path.startswith("/") or ".." in Path(rel_path).parts:
        raise JobPathError(f"不是合法的 Job 相对路径：{rel_path}")
    root = JOBS_DIR.resolve()
    target = (root / rel_path).resolve()
    # 必须写成 root + os.sep：/data/jobs 不能把自己匹配给 /data/jobs-evil
    if not str(target).startswith(str(root) + os.sep):
        raise JobPathError(f"路径越出 Job 目录：{rel_path}")
    return target


def read_log(rel_path: str | None, limit: int = 32768) -> str:
    """读日志尾部。日志是给人排查用的，不需要整份回传。"""
    if not rel_path:
        return ""
    try:
        target = _resolve(rel_path)
    except JobPathError:
        return ""
    if not target.is_file():
        return ""
    size = target.stat().st_size
    with target.open("rb") as fh:
        if size > limit:
            fh.seek(size - limit)
            body = fh.read().decode("utf-8", errors="replace")
            return f"…（只显示最后 {limit // 1024}KB，共 {size} 字节）\n" + body
        return fh.read().decode("utf-8", errors="replace")


def artifacts(result_paths_json: str) -> list[dict]:
    """产物清单：路径、是否还在、字节数。绝不返回绝对路径。"""
    try:
        paths = json.loads(result_paths_json or "[]")
    except ValueError:
        return []
    out = []
    for rel in paths if isinstance(paths, list) else []:
        try:
            target = _resolve(str(rel))
        except JobPathError:
            out.append({"path": str(rel), "exists": False, "size": 0})
            continue
        exists = target.is_file()
        out.append({"path": str(rel), "exists": exists,
                    "size": target.stat().st_size if exists else 0})
    return out


def remove_job(job_id: int) -> None:
    """删作业目录。只给服务端清理用（删项目），不暴露成接口。"""
    import shutil
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
