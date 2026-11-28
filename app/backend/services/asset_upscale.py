"""ComfyUI 参考图超分及事务化资产更新。"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request

import safenet
from config import FILES_DIR
from db import transaction
from imagetypes import MAX_IMAGE_BYTES, sniff
from paths import resolve_within
from repositories.content import _images_of, assets
from repositories.engine_instances import engine_instances
from schemas.assets import AssetImageUpscaleIn, AssetImageUpscaleOut
from schemas.content import AssetOut
from .content_operations import ContentOperationError

_SEMAPHORE = threading.BoundedSemaphore(2)
_MODEL = re.compile(r"^[A-Za-z0-9._-]{1,128}\.(pth|pt|safetensors)$")


def _timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ContentOperationError(504, "ComfyUI 超分任务超时")
    return min(10.0, remaining)


def _request(url: str, deadline: float, data: bytes | None = None,
             content_type: str | None = None, limit: int = 1024 * 1024, *,
             headers: dict[str, str] | None = None) -> bytes:
    request_headers = {"ngrok-skip-browser-warning": "1", **(headers or {})}
    if content_type:
        request_headers["Content-Type"] = content_type
    req = Request(url, data=data, headers=request_headers)
    for attempt in range(3):
        try:
            with safenet.safe_urlopen(req, timeout=_timeout(deadline)) as response:
                chunks = []
                size = 0
                while True:
                    _timeout(deadline)
                    chunk = response.read(min(65536, limit + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ContentOperationError(502, "ComfyUI 返回的数据超过大小上限")
                    chunks.append(chunk)
                return b"".join(chunks)
        except HTTPError as exc:
            raise ContentOperationError(502, f"ComfyUI 请求失败（HTTP {exc.code}），请检查模型及节点配置") from exc
        except (URLError, TimeoutError, OSError, HTTPException, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (ConnectionRefusedError, TimeoutError)) or attempt >= 2 or time.monotonic() + 0.5 >= deadline:
                raise ContentOperationError(502, f"ComfyUI 连接失败：{exc}") from exc
            time.sleep(0.5)
    raise ContentOperationError(502, "ComfyUI 连接失败")


def _json_request(url: str, deadline: float, data: bytes | None = None,
                  content_type: str | None = None, *,
                  headers: dict[str, str] | None = None) -> dict:
    try:
        raw = (_request(url, deadline, data, content_type) if headers is None
               else _request(url, deadline, data, content_type, headers=headers))
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ContentOperationError(502, "ComfyUI 返回了无效的 JSON") from exc
    if not isinstance(result, dict):
        raise ContentOperationError(502, "ComfyUI 返回的数据格式不正确")
    return result


def _upload(base: str, source: Path, deadline: float, headers: dict[str, str] | None = None) -> str:
    boundary = uuid.uuid4().hex
    data = source.read_bytes()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{uuid.uuid4().hex}{source.suffix}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode() + data
    body += (f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n--{boundary}--\r\n').encode()
    result = _json_request(base + "/upload/image", deadline, body,
                           f"multipart/form-data; boundary={boundary}", headers=headers)
    name = result.get("name")
    if not isinstance(name, str) or not name:
        raise ContentOperationError(502, "ComfyUI 未返回上传图片名称")
    folder = result.get("subfolder")
    return f"{folder}/{name}" if isinstance(folder, str) and folder else name


def _submit(base: str, name: str, model: str, deadline: float, headers: dict[str, str] | None = None) -> str:
    workflow = {
        "1": {"class_type": "LoadImage", "inputs": {"image": name}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model}},
        "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "SaveImage", "inputs": {"filename_prefix": "aivp_upscale", "images": ["3", 0]}},
    }
    result = _json_request(base + "/prompt", deadline, json.dumps({"prompt": workflow}).encode(),
                           "application/json", headers=headers)
    if result.get("node_errors"):
        raise ContentOperationError(502, f"ComfyUI 工作流节点出错，请检查模型：{result['node_errors']}")
    prompt_id = result.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id:
        raise ContentOperationError(502, "ComfyUI 未返回任务编号，请检查模型及节点配置")
    return prompt_id


def _wait(base: str, prompt_id: str, deadline: float, headers: dict[str, str] | None = None) -> dict:
    while True:
        result = _json_request(base + "/history/" + quote(prompt_id, safe=""), deadline, headers=headers)
        item = result.get(prompt_id)
        if isinstance(item, dict):
            status = item.get("status", {})
            if isinstance(status, dict) and status.get("status_str") == "error":
                raise ContentOperationError(502, "ComfyUI 超分任务执行失败，请检查模型及节点日志")
            outputs = item.get("outputs", {})
            if isinstance(outputs, dict):
                nodes = [outputs.get("4")] + list(outputs.values())
                for node in nodes:
                    if isinstance(node, dict) and isinstance(node.get("images"), list) and node["images"]:
                        image = node["images"][0]
                        if isinstance(image, dict) and isinstance(image.get("filename"), str) and image["filename"]:
                            return image
        time.sleep(min(0.8, _timeout(deadline)))


def _dimensions(data: bytes, ext: str) -> tuple[int, int]:
    try:
        if ext == "png" and len(data) >= 24 and data[12:16] == b"IHDR":
            width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        elif ext == "webp" and len(data) >= 30:
            kind = data[12:16]
            if kind == b"VP8X":
                width, height = 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            elif kind == b"VP8L" and data[20] == 0x2f:
                bits = int.from_bytes(data[21:25], "little")
                width, height = (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
            elif kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
                width, height = int.from_bytes(data[26:28], "little") & 0x3fff, int.from_bytes(data[28:30], "little") & 0x3fff
            else:
                raise ValueError()
        elif ext == "jpg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xff:
                    raise ValueError()
                marker = data[i + 1]
                if marker in (0xd8, 0xd9, 0x01) or 0xd0 <= marker <= 0xd7:
                    i += 2
                    continue
                length = int.from_bytes(data[i + 2:i + 4], "big")
                if length < 2 or i + 2 + length > len(data):
                    raise ValueError()
                if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf) and length >= 7:
                    height, width = int.from_bytes(data[i + 5:i + 7], "big"), int.from_bytes(data[i + 7:i + 9], "big")
                    break
                i += 2 + length
            else:
                raise ValueError()
        else:
            raise ValueError()
        if not width or not height:
            raise ValueError()
        return width, height
    except (ValueError, IndexError) as exc:
        raise ContentOperationError(502, "ComfyUI 返回的图片格式或尺寸无效") from exc


def upscale_asset_image(asset_id: int, payload: AssetImageUpscaleIn) -> AssetImageUpscaleOut:
    row = assets.get(asset_id)
    if row is None:
        raise ContentOperationError(404, "资产不存在")
    old_path = payload.path
    source = resolve_within(FILES_DIR, old_path)
    if old_path not in _images_of(row) or source is None or not source.is_file():
        raise ContentOperationError(404, "这张图不在该资产下或文件不存在")
    if not _MODEL.fullmatch(payload.model_name):
        raise ContentOperationError(422, "超分模型名称不合法")
    instance = engine_instances.pick("comfyui")
    if instance is None:
        raise ContentOperationError(503, "未配置或未启用 ComfyUI 节点，请先在「系统设置 → 引擎实例」中添加并启用 ComfyUI 地址")
    try:
        base = safenet.check_url(instance["endpoint"]).rstrip("/")
    except ValueError as exc:
        raise ContentOperationError(422, f"ComfyUI 地址不合法：{exc}") from exc
    secret_env = (dict(instance).get("shared_secret_env") or "").strip()
    headers = None
    if secret_env:
        secret = (os.environ.get(secret_env) or "").strip()
        if not secret:
            raise ContentOperationError(422, f"密钥环境变量 {secret_env} 没配或值为空")
        headers = {"Authorization": f"Bearer {secret}"}
    if not _SEMAPHORE.acquire(timeout=2.0):
        raise ContentOperationError(429, "已有 2 个超分任务正在运行，请稍后再试")
    target = None
    temp = None
    committed = False
    try:
        deadline = time.monotonic() + 90.0
        name = _upload(base, source, deadline, headers)
        prompt_id = _submit(base, name, payload.model_name, deadline, headers)
        image = _wait(base, prompt_id, deadline, headers)
        query = urlencode({key: image.get(key, default) for key, default in (("filename", ""), ("subfolder", ""), ("type", "output"))})
        data = _request(base + "/view?" + query, deadline, limit=MAX_IMAGE_BYTES, headers=headers)
        ext = sniff(data[:32])
        if ext is None:
            raise ContentOperationError(502, "ComfyUI 返回的内容不是有效图片")
        width, height = _dimensions(data, ext)
        folder = FILES_DIR / str(asset_id)
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}.{ext}"
        target = folder / filename
        temp = folder / (filename + ".tmp")
        temp.write_bytes(data)
        os.replace(temp, target)
        new_path = f"{asset_id}/{filename}"
        with transaction() as conn:
            row = assets.get(asset_id)
            if row is None or old_path not in _images_of(row):
                raise ContentOperationError(404, "资产或原图片已不存在")
            images = _images_of(row)
            purposes = json.loads(row["image_purposes"] or "{}")
            if payload.replace:
                images[images.index(old_path)] = new_path
                if old_path in purposes:
                    purposes[new_path] = purposes.pop(old_path)
            else:
                images.append(new_path)
            conn.execute("UPDATE asset SET ref_images = ?, image_purposes = ?, updated_at = datetime('now','localtime') WHERE id = ?", (json.dumps(images, ensure_ascii=False), json.dumps(purposes, ensure_ascii=False), asset_id))
            updated = assets.get(asset_id)
            assert updated is not None
        committed = True
        if payload.replace:
            source = resolve_within(FILES_DIR, old_path)
            if source is not None:
                source.unlink(missing_ok=True)
        return AssetImageUpscaleOut(asset=AssetOut.from_row(updated), old_path=old_path, new_path=new_path,
                                    width=width, height=height, model_name=payload.model_name, replaced=payload.replace)
    finally:
        try:
            if temp is not None:
                temp.unlink(missing_ok=True)
            if not committed and target is not None:
                target.unlink(missing_ok=True)
        finally:
            _SEMAPHORE.release()
