#!/usr/bin/env python3
"""本机 LLM 服务型引擎的一次 chat 调用（A4，2026-11-06）。

为什么不直接在 worker 里发 HTTP：与 `engine_http_run.py` 文件头那句话完全一样——
取消要另搭一套、超时与 `WORKER_MAX_RUNTIME_SECONDS` 各算各的、日志要接进
`run.log` 还得再写一遍。子进程读请求 JSON、写产物、写回执，worker 只用 `_pump`
盯着，这套形状已经在云端（`cloud_run.py`）与本机图像服务（`engine_http_run.py`）
两边跑通了一年，本机 LLM 没有理由长第三套。

为什么单开一个脚本而不是复用 `engine_http_run.py`：它的响应处理是
"base64 图片数组"（Draw Things 的 `txt2img` 就是这个形状，实测），
而 chat 的响应是 `message.content` 一个字符串。把两种形状塞进一个脚本
会长出一堆 `if kind ==`，而这两种形状的差异恰好是各自最本质的部分。

一条来自实测的默认值，写在这里是因为它会骗人（2026-11-06 本机 qwen3:8b）：
带 `format`（JSON schema）而不同时关掉思考，模型会把整个 token 预算花在
推理上、`content` 是空的——看起来"调用成功、产物是个空文件"。
所以这里在请求带 `format` 时**强制** `think=false`，
并把这件事写进回执，让事后能看出来是按哪一档跑的。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

import safenet

DEFAULT_TIMEOUT = 180.0


def _finish(receipt_path: Path, *, ok: bool, code: str = "", message: str = "",
            **extra) -> int:
    receipt = {"ok": ok, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not ok:
        receipt["error"] = {"code": code, "message": message[:400]}
    for key, value in extra.items():
        if value is not None:
            receipt[key] = value
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False)[:600], flush=True)
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: llm_http_run.py <request.json>", flush=True)
        return 2
    request_path = Path(sys.argv[1])
    receipt_path = request_path.with_name(request_path.stem + "-receipt.json")
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"receipt_error 请求文件读不出来：{exc}", flush=True)
        return 2
    receipt_path = Path(request.get("receipt") or receipt_path)
    out_path = Path(request["output"])

    endpoint = (request.get("endpoint") or "").rstrip("/")
    model = request.get("model") or ""
    if not endpoint:
        return _finish(receipt_path, ok=False, code="endpoint_missing",
                       message="没有可用的引擎端点（实例没登记或已停用）")
    if not model:
        return _finish(receipt_path, ok=False, code="model_missing",
                       message="请求里没有模型标识：本机服务认的是它自己的模型名，"
                               "平台不替这个请求猜一个")

    body: dict = {"model": model,
                  "messages": request.get("messages")
                  or [{"role": "user", "content": request.get("prompt") or ""}],
                  "stream": False}
    options: dict = {}
    params = request.get("params") or {}
    if params.get("max_tokens"):
        options["num_predict"] = int(params["max_tokens"])
    if params.get("temperature") is not None:
        options["temperature"] = float(params["temperature"])
    # 见文件头那句实测结论：要 JSON 输出就必须关思考，否则 content 会是空的
    think = params.get("think")
    if request.get("format") is not None and think is not False:
        think = False
    if think is not None:
        body["think"] = bool(think)
    if request.get("format") is not None:
        body["format"] = request["format"]
    if request.get("tools"):
        body["tools"] = request["tools"]
    if options:
        body["options"] = options

    payload = json.dumps(body).encode("utf-8")
    secret_env = (request.get("secret_env") or "").strip()
    headers = {"Content-Type": "application/json"}
    if secret_env:
        # 本机服务通常没有凭据；真有就照平台那条唯一纪律来：
        # 表里存变量名，值只从进程环境读，永不进文件、日志与回执
        secret = os.environ.get(secret_env, "")
        if not secret:
            return _finish(receipt_path, ok=False, code="secret_env_missing",
                           message=f"环境变量 {secret_env} 没有值")
        headers["Authorization"] = f"Bearer {secret}"

    url = endpoint + (request.get("path") or "/api/chat")
    timeout = float(request.get("timeout") or DEFAULT_TIMEOUT)
    started = time.perf_counter()
    try:
        # 不跟随重定向：可带 `Authorization: Bearer <sharedSecret>`，一次 307
        # 就够把密钥带到别的域。UnsafeTarget 是 ValueError 子类，单独接在最前面。
        with safenet.safe_urlopen(
                Request(url, data=payload, headers=headers, method="POST"),
                timeout=timeout) as resp:
            raw = resp.read()
            http_status = resp.status
    except safenet.UnsafeTarget as exc:
        return _finish(receipt_path, ok=False, code="unsafe_redirect",
                       message=f"{url} 没有发出请求：{exc}",
                       model=model,
                       latency_ms=int((time.perf_counter() - started) * 1000))
    except HTTPError as exc:
        detail = exc.read()[:400].decode("utf-8", "replace") if exc.fp else ""
        return _finish(receipt_path, ok=False, code=f"http_{exc.code}",
                       message=f"{url} 返回 {exc.code}", detail=detail,
                       http_status=exc.code, model=model,
                       latency_ms=int((time.perf_counter() - started) * 1000))
    except (URLError, OSError, TimeoutError) as exc:
        return _finish(receipt_path, ok=False, code="engine_unreachable",
                       message=f"{url} 连不上：{getattr(exc, 'reason', exc)}",
                       model=model)
    latency_ms = int((time.perf_counter() - started) * 1000)

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return _finish(receipt_path, ok=False, code="bad_json",
                       message=f"{url} 的应答不是 JSON", http_status=http_status,
                       model=model, latency_ms=latency_ms)

    message = data.get("message") or {}
    content = message.get("content")
    tool_calls = message.get("tool_calls") or []
    if not content and not tool_calls:
        # 没有内容也没有工具调用＝失败。**不写一个空文件冒充产物**——
        # 那是"作业绿了、结果是空的"这一类最难查的账的起点
        return _finish(receipt_path, ok=False, code="empty_content",
                       message="服务返回成功但 content 与 tool_calls 都是空的"
                               "（如果请求带了 format，请确认 think 已关）",
                       http_status=http_status, model=model, latency_ms=latency_ms,
                       response_meta={"done": data.get("done"),
                                      "eval_count": data.get("eval_count")})

    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    if tool_calls:
        # 工具调用与文本同时给界面看：把调用意图追加在产物末尾，
        # 因为智能体那一层解析的是 JSON 数组，多出来的说明文字不影响解析
        # （`agents.parse_plan` 取第一个 [ 到最后一个 ]）
        text += "\n\n<!-- tool_calls -->\n" + json.dumps(
            tool_calls, ensure_ascii=False, indent=2)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out_path)                # 原子落盘：不留半截产物

    return _finish(receipt_path, ok=True, provider=request.get("provider") or "ollama",
                   model=data.get("model") or model, capability="text",
                   request_id=None, http_status=http_status,
                   latency_ms=latency_ms, bytes=len(text.encode("utf-8")),
                   usage={"eval_count": data.get("eval_count"),
                          "prompt_eval_count": data.get("prompt_eval_count"),
                          "total_tokens": (data.get("eval_count") or 0)
                          + (data.get("prompt_eval_count") or 0),
                          "load_duration_ns": data.get("load_duration")},
                   response_meta={"done": data.get("done"),
                                  "think": body.get("think"),
                                  "has_format": request.get("format") is not None,
                                  "tool_calls": len(tool_calls),
                                  "returned_model": data.get("model")})


if __name__ == "__main__":
    sys.exit(main())
