"""Prepare reference images without persisting derived files or metadata."""
from __future__ import annotations

import base64
import hashlib
import io
import os
import stat
from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path
from threading import Lock

from PIL import Image, ImageOps, UnidentifiedImageError

from config import FILES_DIR
from paths import resolve_within

from .content_operations import ContentOperationError

_MAX_SOURCE = 10 * 1024 * 1024
_MAX_PIXELS = 20_000_000
_MAX_ENCODED = 768 * 1024
_CACHE_ENTRIES = 24
_cache: OrderedDict[tuple[bytes, int], str] = OrderedDict()
_cache_lock = Lock()


def _read_reference(rel: str) -> bytes:
    try:
        path = resolve_within(FILES_DIR, rel)
        if path is None:
            raise ContentOperationError(400, "参考图路径无效")
        base = FILES_DIR.resolve()
        parts = Path(rel.strip().replace("\\", "/")).parts
        if base.joinpath(*parts) != path:
            raise ContentOperationError(400, "参考图路径不能使用符号链接")
        # Anchor each lookup to its open parent directory to prevent symlink swaps.
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        with ExitStack() as opened:
            parent = os.open(base, directory_flags)
            opened.callback(os.close, parent)
            for component in parts[:-1]:
                parent = os.open(component, directory_flags, dir_fd=parent)
                opened.callback(os.close, parent)
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            source = opened.enter_context(os.fdopen(fd, "rb"))
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ContentOperationError(400, "参考图不是普通文件")
            if info.st_size > _MAX_SOURCE:
                raise ContentOperationError(413, "单张参考图不得超过 10 MiB")
            raw = source.read(_MAX_SOURCE + 1)
    except FileNotFoundError:
        raise ContentOperationError(404, "参考图文件不存在") from None
    except (ValueError, RuntimeError):
        raise ContentOperationError(400, "参考图路径无效") from None
    except OSError:
        raise ContentOperationError(422, "参考图无法读取") from None
    if len(raw) > _MAX_SOURCE:
        raise ContentOperationError(413, "单张参考图不得超过 10 MiB")
    return raw


def _encode(raw: bytes, side: int) -> str:
    key = (hashlib.sha256(raw).digest(), side)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    try:
        with Image.open(io.BytesIO(raw), formats=("PNG", "JPEG", "WEBP")) as source:
            if source.width * source.height > _MAX_PIXELS:
                raise ContentOperationError(413, "单张参考图不得超过 2000 万像素")
            if getattr(source, "is_animated", False):
                raise ContentOperationError(422, "请选择静态 PNG、JPEG 或 WebP 参考图")
            image = ImageOps.exif_transpose(source)
            image.thumbnail((side, side), Image.Resampling.LANCZOS)
            if "A" in image.getbands() or "transparency" in image.info:
                rgba = image.convert("RGBA")
                image = Image.new("RGB", rgba.size, "white")
                image.paste(rgba, mask=rgba.getchannel("A"))
            else:
                image = image.convert("RGB")
            image.info.clear()
            for quality in (90, 82, 76, 76, 76):
                with io.BytesIO() as out:
                    image.save(out, "JPEG", quality=quality, subsampling=0 if quality == 90 else 2)
                    if out.tell() <= _MAX_ENCODED:
                        encoded = base64.b64encode(out.getvalue()).decode("ascii")
                        with _cache_lock:
                            _cache[key] = encoded
                            _cache.move_to_end(key)
                            if len(_cache) > _CACHE_ENTRIES:
                                _cache.popitem(last=False)
                        return encoded
                if quality == 76:
                    image.thumbnail((max(1, int(image.width * .8)), max(1, int(image.height * .8))),
                                    Image.Resampling.LANCZOS)
    except Image.DecompressionBombError:
        raise ContentOperationError(413, "单张参考图不得超过 2000 万像素") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, OverflowError):
        raise ContentOperationError(422, "参考图损坏或格式不受支持，请重新上传静态图片") from None
    raise ContentOperationError(413, "参考图压缩后仍过大，请缩小图片后重试")


def _prepare_reference(rel: str, *, detailed: bool) -> str:
    # Read and validate on every request. Only equal content can share an encoding.
    return _encode(_read_reference(rel), 2048 if detailed else 1536)
