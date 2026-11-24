"""Bounded HTTP and response decoding for reference-image inference."""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from http.client import HTTPException
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request

import safenet

from .content_operations import ContentOperationError

_MAX_RESPONSE = 2_000_000
_MAX_EVENT = 256_000
_MAX_TEXT = 32_000


class _ReadableResponse(Protocol):
    def read1(self, size: int) -> bytes: ...


class _VisionError(ContentOperationError):
    def __init__(self, code: int, detail: str, *, unavailable: bool = False):
        super().__init__(code, detail)
        self.unavailable = unavailable


def _remaining(deadline: float, ceiling: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _VisionError(504, "看图推理超时，请减少参考图或更换通道")
    return min(left, ceiling)


def _json_object(data: bytes) -> dict:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return value


def _chunks(response: _ReadableResponse, deadline: float) -> Iterator[bytes]:
    total = 0
    while True:
        _remaining(deadline, 1)
        block = response.read1(8192)
        if not block:
            return
        _remaining(deadline, 1)
        total += len(block)
        if total > _MAX_RESPONSE:
            raise _VisionError(502, "看图通道返回内容过大")
        yield block


def _events(response: _ReadableResponse, deadline: float) -> Iterator[dict]:
    buffer = b""
    fields: list[bytes] = []
    size = 0
    event_type = ""
    for block in _chunks(response, deadline):
        buffer += block
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            if not line:
                if fields:
                    payload = b"\n".join(fields)
                    if payload == b"[DONE]":
                        return
                    data = _json_object(payload)
                    if event_type:
                        data.setdefault("type", event_type)
                    yield data
                fields, size, event_type = [], 0, ""
            elif line.startswith(b"data:"):
                value = line[5:].removeprefix(b" ")
                fields.append(value)
                size += len(value) + 1
            elif line.startswith(b"event:"):
                event_type = line[6:].strip().decode("ascii")
            if size > _MAX_EVENT or len(line) > _MAX_EVENT:
                raise _VisionError(502, "看图通道的单条流式事件过大")
        if len(buffer) > _MAX_EVENT:
            raise _VisionError(502, "看图通道的单条流式事件过大")


def _responses_text(result: dict) -> str:
    if result.get("status") not in (None, "completed"):
        raise _VisionError(502, "ChatGPT 推理未完成，请缩短输入或更换模型")
    text: list[str] = []
    for item in result.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "refusal":
                raise _VisionError(422, "ChatGPT 未接受此图片或提示")
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                text.append(part["text"])
    direct = result.get("output_text")
    return "\n".join(text) or (direct if isinstance(direct, str) else "")


def _read_responses(response: _ReadableResponse, deadline: float) -> str:
    deltas: dict[tuple[int, int], list[str]] = {}
    done: dict[tuple[int, int], str] = {}
    size = 0
    for event in _events(response, deadline):
        kind = event.get("type")
        if kind in ("response.failed", "response.incomplete", "error"):
            raise _VisionError(502, "ChatGPT 推理未完成，请检查通道状态、额度或输入")
        if kind in ("response.refusal.delta", "response.refusal.done"):
            raise _VisionError(422, "ChatGPT 未接受此图片或提示")
        if kind == "response.completed":
            result = event.get("response")
            final = _responses_text(result) if isinstance(result, dict) else ""
            return final or "\n".join(done[key] if key in done else "".join(deltas[key])
                                      for key in sorted(done.keys() | deltas.keys()))
        if kind not in ("response.output_text.delta", "response.output_text.done"):
            continue
        output = event.get("output_index", 0)
        part = event.get("content_index", 0)
        value = event.get("text" if kind.endswith(".done") else "delta")
        if not isinstance(output, int) or not isinstance(part, int) or not isinstance(value, str):
            raise ValueError("invalid text event")
        size += len(value)
        if size > _MAX_TEXT:
            raise _VisionError(502, "ChatGPT 返回文本过长")
        if kind.endswith(".done"):
            done[(output, part)] = value
        else:
            deltas.setdefault((output, part), []).append(value)
    raise _VisionError(502, "ChatGPT 连接提前结束，未收到完整提示词")


def _http_error(exc: HTTPError, label: str) -> _VisionError:
    code = exc.code
    if code == 429:
        return _VisionError(429, f"{label}额度不足或请求过于频繁，请稍后重试")
    if code in (401, 403):
        return _VisionError(424, f"{label}授权失效或无权访问该模型")
    if code in (408, 504):
        return _VisionError(504, f"{label}推理超时，请减少参考图或更换通道")
    if code == 404:
        return _VisionError(422, f"{label}所选模型不存在或已停用，请检查或更换模型")
    if code in (400, 413, 415, 422):
        return _VisionError(422, f"{label}不接受所选模型或图片，请检查模型及参考图")
    if code >= 500:
        return _VisionError(503, f"{label}暂时不可用，请稍后重试", unavailable=True)
    return _VisionError(502, f"{label}请求失败")


def _request(url: str, *, label: str, deadline: float, idle: float,
            body: dict | None = None, token: str | None = None,
            extra_headers: dict[str, str] | None = None,
            responses: bool = False) -> dict | str:
    headers = {"Accept": "text/event-stream, application/json" if responses else "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    try:
        # safe_urlopen blocks redirects; explicit validation protects configured engine targets.
        safenet.check_url(url)
        req = Request(url, data=data, headers=headers)
        with safenet.safe_urlopen(req, timeout=_remaining(deadline, idle)) as response:
            ctype = response.headers.get_content_type()
            is_stream = bool(isinstance(body, dict) and body.get("stream"))
            if responses and (ctype == "text/event-stream" or (is_stream and ctype != "application/json")):
                return _read_responses(response, deadline)
            result = _json_object(b"".join(_chunks(response, deadline)))
            return _responses_text(result) if responses else result
    except HTTPError as exc:
        try:
            raise _http_error(exc, label) from None
        finally:
            exc.close()
    except safenet.UnsafeTarget:
        raise _VisionError(424, f"{label}服务地址未通过安全检查") from None
    except TimeoutError:
        raise _VisionError(504, f"{label}推理超时，请减少参考图或更换通道") from None
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise _VisionError(504, f"{label}连接超时") from None
        raise _VisionError(503, f"{label}无法连接，请检查服务或网络", unavailable=True) from None
    except (OSError, HTTPException):
        raise _VisionError(502, f"{label}连接中断") from None
    except (ValueError, TypeError, RecursionError):
        raise _VisionError(502, f"{label}响应格式无效") from None
