"""Job 的日志与产物落盘：路径换算、边界校验、读日志。

三条规矩，和 workspace.py 同一套思路：
1. 库里只存**相对 JOBS_DIR 的路径**（`<job_id>/logs/run.log`），不回传绝对路径；
2. 读的时候再做一次真实路径比对——库里的路径可以被手改，不能拿它直接 open；
3. 删项目时整目录摘掉，历史成本仍由删掉的 Job 行负责（项目已删，无账可对）。
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from config import JOBS_DIR


class JobPathError(Exception):
    """路径越界或不是合法相对路径。"""


def job_dir(job_id: int) -> Path:
    return JOBS_DIR / str(int(job_id))


def volume_usage():
    """作业目录所在磁盘卷的用量（total/used/free），读不到就回 None。

    为什么单独一个函数：worker 心跳里的「盘余」、生产前检查的「磁盘偏紧」这条警告，
    问的是同一件事——产物全落 `JOBS_DIR`，那里满了生成与合成都会失败。两处原来各写
    各的 `shutil.disk_usage(JOBS_DIR)`，而**目录可能还没建**（首次启动、临时库、
    还没跑过任何作业）：那时 `disk_usage` 直接抛 OSError，worker 报 0、预检则
    一句提醒都不给——"盘要满了"这句话恰恰在最该说的时候沉默。
    往上找第一个真实存在的父级再问卷，问的就是"这块盘还剩多少"，
    与那一层目录在不在无关。
    """
    probe = JOBS_DIR
    try:
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        return shutil.disk_usage(probe)
    except OSError:
        return None


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


def stat_log(rel_path: str | None) -> dict:
    """日志文件的存在性、字节数与静默秒数。

    为什么单独要这一位：`read_log()` 只回文本，界面看不出"这条作业是正在慢慢写，
    还是早就卡住了"。模型加载权重时可以静默几分钟，所以这里只报事实，
    是不是异常由调用方拿阈值去判。
    """
    if not rel_path:
        return {"exists": False, "size": 0, "mtime": None, "silent_seconds": None}
    try:
        target = _resolve(rel_path)
    except JobPathError:
        return {"exists": False, "size": 0, "mtime": None, "silent_seconds": None}
    if not target.is_file():
        return {"exists": False, "size": 0, "mtime": None, "silent_seconds": None}
    st = target.stat()
    mtime = datetime.fromtimestamp(st.st_mtime)
    return {"exists": True, "size": st.st_size,
            "mtime": mtime.strftime("%Y-%m-%d %H:%M:%S"),
            "silent_seconds": max(0, int((datetime.now() - mtime).total_seconds()))}


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


def artifact_at(result_paths_json: str, index: int) -> Path | None:
    """产物清单里的第 index 条，换算成经过边界校验的真实路径。

    不存在或越界一律 None——调用方回 404，不把"路径长什么样"告诉外面。
    """
    try:
        paths = json.loads(result_paths_json or "[]")
    except ValueError:
        return None
    if not isinstance(paths, list) or index < 0 or index >= len(paths):
        return None
    try:
        target = _resolve(str(paths[index]))
    except JobPathError:
        return None
    return target if target.is_file() else None


def resolve_artifact(rel_path: str) -> Path | None:
    """把库里的 Job 相对路径换成真实路径；越界或不存在返回 None。

    合成要读别的作业的产物，但**不能**让调用方自己拼绝对路径：跨作业读文件
    同样走这一道边界，和读日志、列产物是同一条规则。
    """
    try:
        target = _resolve(str(rel_path))
    except JobPathError:
        return None
    return target if target.is_file() else None


def remove_job(job_id: int) -> None:
    """删作业目录。只给服务端清理用（删项目），不暴露成接口。"""
    import shutil
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


# ---------- 成片字幕（阶段 D 的补口） ----------
#
# 为什么不建表也不进 Git 工作区：字幕是"喂给一次合成的输入"，和音频素材不同，
# 它没有台账语义（没人要在界面上改它、查它被谁引用）。落在 JOBS_DIR 里，
# 备份跟着作业目录一起走，路径规则也沿用同一道边界，不必再加一套。

SUBTITLE_EXTS = (".srt", ".vtt")


def subtitle_dir(project_id: int) -> Path:
    return JOBS_DIR / "subtitles" / str(int(project_id))


def save_subtitle(project_id: int, content: bytes, suffix: str) -> str:
    """落一份字幕文件，返回库里用的相对路径（`subtitles/<项目>/<uuid>.srt`）。

    先写临时文件再 `os.replace`：半个字幕被 FFmpeg 读到，会得到一条
    "解析失败"的合成作业，而真实原因（写一半崩了）根本看不出来。
    """
    import uuid
    d = subtitle_dir(project_id)
    d.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{suffix}"
    tmp = d / f".{name}.part"
    target = d / name
    try:
        tmp.write_bytes(content)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()      # 只清这一条临时文件，不碰目录里的正式字幕
    return relative(target)


def resolve_subtitle(project_id: int, rel_path: str) -> Path | None:
    """删字幕时用：只认本项目字幕目录里真实存在的那份。

    不这么收窄的话，`path=../../<别人的项目>/x.srt` 这种写法就是一条
    跨项目删除的通道；库里的相对路径永远当不可信输入处理，这条也一样。
    """
    target = subtitle_dir(project_id)
    try:
        real = _resolve(str(rel_path))
    except JobPathError:
        return None
    if not str(real).startswith(str(target.resolve()) + os.sep):
        return None
    return real if real.is_file() else None


def list_subtitles(project_id: int) -> list[dict]:
    """本项目已有的字幕，按最近改动排在前面。目录不存在就是没传过。"""
    d = subtitle_dir(project_id)
    if not d.is_dir():
        return []
    rows = [{"path": relative(p), "bytes": p.stat().st_size,
             "name": p.name,
             "modified_at": datetime.fromtimestamp(p.stat().st_mtime)
             .strftime("%Y-%m-%d %H:%M")}
            for p in d.iterdir() if p.is_file() and not p.name.startswith(".")]
    return sorted(rows, key=lambda r: r["modified_at"], reverse=True)


def remove_project_subtitles(project_id: int) -> int:
    """删项目时清掉它的字幕。返回删掉的份数，给"清了几个"那句实话用。"""
    import shutil
    d = subtitle_dir(project_id)
    if not d.is_dir():
        return 0
    count = sum(1 for p in d.iterdir() if p.is_file())
    shutil.rmtree(d, ignore_errors=True)
    return count
