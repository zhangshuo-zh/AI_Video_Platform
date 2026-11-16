"""图片导入的持久化意图与恢复；不扫描或删除无意图记录的历史文件。

文件锁协调导入与恢复，文件准备与清理位于数据库事务外。
数据库事务仅检查或写入引用；兼容旧调用方持有的数据库事务。
"""
from __future__ import annotations

import fcntl
from contextlib import contextmanager

import json
import os
import re
from pathlib import Path

from config import FILES_DIR
from db import transaction
from log import get_logger
from repositories.content import assets

logger = get_logger(__name__)

_PATTERN = re.compile(r"([1-9][0-9]*)-([0-9a-f]{32})\.(png|jpg|webp)\.json")


class ImportRecoveryPending(Exception):
    """文件补偿暂时失败，持久化意图保留，供下次启动或恢复工具重试。"""



@contextmanager
def import_lock():
    """跨线程、进程串行化导入意图的准备、提交与恢复。"""
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    path = FILES_DIR / ".asset-import.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _directory() -> Path:
    path = FILES_DIR / ".asset-imports"
    if path.is_symlink():
        raise ValueError("导入恢复目录不能是符号链接")
    return path


def _target(token: str) -> Path:
    match = _PATTERN.fullmatch(token)
    if match is None:
        raise ValueError("导入恢复记录名不合法")
    target = FILES_DIR.resolve() / match[1] / f"{match[2]}.{match[3]}"
    if target.resolve() != target:
        raise ValueError("导入恢复目标不能包含符号链接")
    return target


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def begin(relative_path: str, job_id: int) -> str:
    token = relative_path.replace("/", "-") + ".json"
    _target(token)
    directory = _directory()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / token).open("x", encoding="utf-8") as fh:
        json.dump({"version": 1, "path": relative_path, "job_id": int(job_id)}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    _sync_directory(directory)
    # 新建目录本身也先落盘，之后才能开始复制。
    _sync_directory(directory.parent)
    return token


def sync_image(path: Path) -> None:
    with path.open("rb") as fh:
        os.fsync(fh.fileno())
    _sync_directory(path.parent)


def discard(token: str) -> None:
    """先清理本次文件，再移除意图；失败时保留意图且返回可操作错误。"""
    try:
        target = _target(token)
        target.unlink(missing_ok=True)
        if target.parent.exists():
            _sync_directory(target.parent)
        (_directory() / token).unlink(missing_ok=True)
        _sync_directory(_directory())
    except (OSError, ValueError) as exc:
        raise ImportRecoveryPending(
            "图片导入未完成，文件清理暂时失败；已保留恢复记录，请恢复目录写权限后重启服务或运行导入恢复工具") from exc


def committed(token: str) -> None:
    """数据库已提交；意图清理失败不能把成功导入伪报为失败。"""
    try:
        _target(token)
        (_directory() / token).unlink(missing_ok=True)
        _sync_directory(_directory())
    except (OSError, ValueError):
        logger.warning("图片已登记，恢复记录稍后清理")


def recover(*, apply: bool = True) -> dict:
    result = {"removed": 0, "kept": 0, "pending": 0, "invalid": 0}
    directory = _directory()
    if not directory.exists():
        return result
    with import_lock():
        return _recover_locked(directory, apply=apply, result=result)


def _recover_locked(directory: Path, *, apply: bool, result: dict) -> dict:
    for marker in sorted(directory.glob("*.json")):
        try:
            # 文件锁已排除正常导入；数据库短事务兼容旧版导入持有的写锁。
            if marker.is_symlink():
                result["invalid"] += 1
                continue
            if not marker.exists():
                continue
            target = _target(marker.name)
            record = json.loads(marker.read_text(encoding="utf-8"))
            relative = str(target.relative_to(FILES_DIR.resolve()))
            if (not isinstance(record, dict) or record.get("version") != 1
                    or record.get("path") != relative):
                result["invalid"] += 1
                continue
            with transaction():
                referenced = assets.image_reference_state(relative)
            if referenced is None:
                result["pending"] += 1
                continue
            if referenced:
                if not target.is_file():
                    result["pending"] += 1
                    continue
                if apply:
                    marker.unlink()
                    _sync_directory(directory)
                result["kept"] += 1
            else:
                if apply:
                    discard(marker.name)
                result["removed"] += 1
        except FileNotFoundError:
            continue
        except (OSError, ImportRecoveryPending):
            result["pending"] += 1
        except (ValueError, TypeError):
            result["invalid"] += 1
    return result
