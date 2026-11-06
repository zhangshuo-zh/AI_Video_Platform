#!/usr/bin/env python3
"""本机常驻服务型引擎的一次 HTTP 调用（W-B，2026-11-06）。

为什么不直接在 worker 里 `urllib` 一把梭：那样这条调用就活在 worker 主进程里——
取消要另外搭一套、超时和 `WORKER_MAX_RUNTIME_SECONDS` 各算各的、日志要接进
`run.log` 也得再写一遍。`cloud_run.py` 已经把这形状跑通了一年（子进程读请求
JSON、写产物、写回执，worker 只用 `_pump` 盯着它），本机服务没有理由长第二套。

与云端那条的**两点不同**，都来自 2026-11-06 的目标机实测：

1. **产物在响应体的 base64 里**，不是一个要再去下载的 URL。Draw Things 的
   `txt2img` 应答顶层只有 `images` 一个键，`info`／`parameters` 都是空对象，
   所以外部读数（耗时、步数）只能自己算，别指望引擎告诉你。
2. **没有计费确认这一档**，但多了一道"当前模型对不对"的检查。这件事不能在这里做
   （这里看不到登记行），住在 `services.submit_job` 的闸门里；本脚本只负责
   把引擎此刻自述的 `model` 抄进回执，好让事后能对上账。

密钥一律不进这个文件：如果哪天真需要 `sharedSecret`，它从环境变量按**变量名**
取（`engine_instance.shared_secret_env`），值不落库、不进请求文件、不进日志。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

POLL_HINT = 600     # 单次本机出图的上限（秒）。远小于 worker 的 7200 作业上限


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: engine_http_run.py <request.json>", flush=True)
        return 2
    request_path = Path(sys.argv[1])
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"receipt_error 请求文件读不出来：{exc}", flush=True)
        return 2

    endpoint = (request.get("endpoint") or "").rstrip("/")
    path = request.get("path") or "/sdapi/v1/txt2img"
    out_path = Path(request["output"])
    receipt_path = Path(request["receipt"])
    body = dict(request.get("body") or {})
    secret_env = (request.get("secret_env") or "").strip()

    if not endpoint:
        return _finish(receipt_path, ok=False, code="endpoint_missing",
                       message="没有可用的引擎端点（实例没登记或已停用）")

    payload = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret_env:
        # 只按变量名取；名字本身可以进日志，值永远不进。
        secret = os.environ.get(secret_env, "")
        if not secret:
            return _finish(receipt_path, ok=False, code="secret_env_missing",
                           message=f"环境变量 {secret_env} 没有值；"
                                   "该填变量名而不是值，且值只放进程环境")
        headers["Authorization"] = f"Bearer {secret}"

    url = endpoint + path
    started = time.perf_counter()
    try:
        req = Request(url, data=payload, headers=headers, method="POST")
        with urlopen(req, timeout=float(request.get("timeout") or POLL_HINT)) as resp:
            raw = resp.read()
            status_code = resp.status
    except HTTPError as exc:
        # 422 是这个服务最有信息量的一种失败：它会把不认识的键原样点名。
        detail = exc.read()[:600].decode("utf-8", "replace") if exc.fp else ""
        return _finish(receipt_path, ok=False, code=f"http_{exc.code}",
                       message=f"{url} 返回 {exc.code}", detail=detail,
                       latency_ms=int((time.perf_counter() - started) * 1000))
    except (URLError, OSError, TimeoutError) as exc:
        return _finish(receipt_path, ok=False, code="engine_unreachable",
                       message=f"{url} 连不上：{getattr(exc, 'reason', exc)}")

    latency_ms = int((time.perf_counter() - started) * 1000)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return _finish(receipt_path, ok=False, code="bad_json",
                       message=f"{url} 的应答不是 JSON", latency_ms=latency_ms)

    images = data.get("images") or []
    if not images:
        # 没有图就是失败，不写空文件冒充产物——worker 那条"退出码 0 但没有合法
        # 产物"的红线在这里同样成立（runners.verify 只会认魔数与大小）。
        return _finish(receipt_path, ok=False, code="no_image",
                       message="服务返回成功但 images 是空的", latency_ms=latency_ms)
    try:
        blob = base64.b64decode(images[0])
    except (ValueError, TypeError) as exc:
        return _finish(receipt_path, ok=False, code="bad_base64",
                       message=f"images[0] 解不出 base64：{exc}", latency_ms=latency_ms)
    if len(blob) < 64:
        return _finish(receipt_path, ok=False, code="empty_image",
                       message=f"解出来的图只有 {len(blob)} 字节", latency_ms=latency_ms)

    tmp = out_path.with_suffix(out_path.suffix + ".part")
    tmp.write_bytes(blob)
    tmp.replace(out_path)              # 原子落盘：不留半截产物

    # 顺手回读一次自述，把"当时引擎选的是哪一档权重"钉在回执里。
    # 这一步失败不影响作业成败——它是账，不是闸门（闸门在 submit_job）。
    engine_model = ""
    try:
        with urlopen(Request(endpoint + "/sdapi/v1/options"), timeout=10) as resp:
            engine_model = str(json.loads(resp.read().decode("utf-8", "replace"))
                               .get("model") or "")
    except (URLError, OSError, ValueError, HTTPError):
        pass

    return _finish(receipt_path, ok=True, code="", message="",
                   latency_ms=latency_ms, bytes=len(blob),
                   engine_model=engine_model, requested_model=body.get("model") or "",
                   response_keys=sorted(data.keys()))


def _finish(receipt_path: Path, *, ok: bool, code: str, message: str,
            **extra) -> int:
    receipt = {"ok": ok, "error": ("" if ok else code),
               "message": message, **extra}
    tmp = receipt_path.with_suffix(".json.part")
    tmp.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    tmp.replace(receipt_path)
    print(("ok" if ok else f"error {code}") + f" {message}"[:400], flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
