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
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

import imagetypes
import safenet

POLL_HINT = 600     # 单次本机出图的上限（秒）。远小于 worker 的 7200 作业上限


def _comfyui_headers(secret_env: str) -> dict[str, str]:
    headers = {"ngrok-skip-browser-warning": "1"}
    if secret_env:
        secret = (os.environ.get(secret_env) or "").strip()
        if not secret:
            raise ValueError(f"密钥环境变量 {secret_env} 没配或值为空")
        headers["Authorization"] = f"Bearer {secret}"
        headers["x-shared-secret"] = secret
    return headers


def _comfyui_pick_checkpoint(endpoint: str, headers: dict[str, str], timeout: int) -> str | None:
    url = f"{endpoint}/object_info/CheckpointLoaderSimple"
    safenet.check_url(url)
    with safenet.safe_urlopen(Request(url, headers=headers), timeout=min(timeout, 20)) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    options = data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    if not isinstance(options, list):
        raise ValueError("ComfyUI ckpt_name 选项应为列表")
    names = [name for name in options if isinstance(name, str) and name.strip()]
    anime_keywords = ("animagine", "illustrious", "noobai", "pony", "anime")
    for name in names:
        if any(keyword in name.lower() for keyword in anime_keywords):
            return name
    return next((name for name in names if name.lower().endswith(".safetensors")),
                names[0] if names else None)


def _comfyui_upload_image(endpoint: str, init_path: Path, headers: dict[str, str], timeout: int) -> str:
    boundary = f"----FastVideoComfy{uuid.uuid4().hex}"
    content = init_path.read_bytes()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{init_path.name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = head + content + tail
    upload_url = endpoint + "/upload/image"
    safenet.check_url(upload_url)
    req = Request(
        upload_url,
        data=body,
        method="POST",
        headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with safenet.safe_urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return str(data.get("name") or init_path.name)


def _comfyui_first_image(history_entry: dict) -> dict | None:
    outputs = history_entry.get("outputs") or {}
    if not isinstance(outputs, dict):
        return None
    for node_out in outputs.values():
        if not isinstance(node_out, dict):
            continue
        imgs = node_out.get("images") or []
        if imgs and isinstance(imgs[0], dict) and imgs[0].get("filename"):
            return imgs[0]
    return None


def _run_comfyui(request: dict, endpoint: str, out_path: Path, receipt_path: Path) -> int:
    started = time.monotonic()
    secret_env = (request.get("secret_env") or "").strip()
    timeout = int(request.get("timeout") or 120)
    poll_timeout = float(request.get("poll_timeout") or 300)
    poll_interval = max(0.02, float(request.get("poll_interval") or 0.8))
    if not endpoint:
        return _finish(receipt_path, ok=False, code="endpoint_missing",
                       message="没有可用的 ComfyUI 端点（实例没登记或已停用）")
    workflow = request.get("workflow")
    if not isinstance(workflow, dict) or not workflow:
        return _finish(receipt_path, ok=False, code="workflow_missing",
                       message="ComfyUI workflow 定义为空")
    try:
        headers = _comfyui_headers(secret_env)
    except ValueError as exc:
        return _finish(receipt_path, ok=False, code="secret_env_missing", message=str(exc))

    checkpoint = None
    for node in workflow.values():
        if not isinstance(node, dict) or node.get("class_type") != "CheckpointLoaderSimple":
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict) or inputs.get("ckpt_name") not in (None, "", "__AUTO_CHECKPOINT__"):
            continue
        if checkpoint is None:
            try:
                checkpoint = _comfyui_pick_checkpoint(endpoint, headers, timeout)
            except safenet.UnsafeTarget as exc:
                return _finish(receipt_path, ok=False, code="unsafe_target", message=str(exc), endpoint=endpoint)
            except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                return _finish(receipt_path, ok=False, code="comfyui_checkpoint_lookup_failed",
                               message=f"查询 ComfyUI 已安装模型失败：{exc}", endpoint=endpoint)
            if checkpoint is None:
                return _finish(
                    receipt_path, ok=False, code="comfyui_no_checkpoint",
                    message="ComfyUI 节点尚未安装生图大模型（models/checkpoints 为空），"
                            "请先在 Colab 里勾选下载 Animagine XL 3.1 或放入 safetensors 模型",
                    endpoint=endpoint,
                )
        inputs["ckpt_name"] = checkpoint

    init_image = str(request.get("init_image") or "").strip()
    if init_image:
        init_path = Path(init_image)
        if not init_path.is_file():
            return _finish(receipt_path, ok=False, code="init_image_missing",
                           message="参考底图不存在", endpoint=endpoint)
        try:
            uploaded_name = _comfyui_upload_image(endpoint, init_path, headers, timeout)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            return _finish(receipt_path, ok=False, code="comfyui_upload_failed",
                           message=f"上传参考底图到 ComfyUI 失败：{exc}", endpoint=endpoint)
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if isinstance(inputs, dict) and node.get("class_type") == "LoadImage":
                if inputs.get("image") in (None, "", "__UPLOADED_IMAGE__"):
                    inputs["image"] = uploaded_name
                elif "{{image}}" in str(inputs.get("image")):
                    inputs["image"] = uploaded_name

    prompt_url = endpoint + "/prompt"
    try:
        safenet.check_url(prompt_url)
    except safenet.UnsafeTarget as exc:
        return _finish(receipt_path, ok=False, code="unsafe_target", message=str(exc), endpoint=endpoint)
    payload_bytes = json.dumps(
        {"prompt": workflow, "client_id": request.get("client_id") or "fastvideo"},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        req = Request(
            prompt_url,
            data=payload_bytes,
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
        )
        with safenet.safe_urlopen(req, timeout=timeout) as resp:
            submitted = json.loads(resp.read().decode("utf-8", "replace"))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        return _finish(receipt_path, ok=False, code="comfyui_submit_failed",
                       message=f"提交 ComfyUI /prompt 失败：{exc}", endpoint=endpoint)

    node_errors = submitted.get("node_errors")
    if node_errors:
        return _finish(receipt_path, ok=False, code="comfyui_node_error",
                       message=f"ComfyUI 工作流节点出错：{node_errors}", endpoint=endpoint)
    prompt_id = str(submitted.get("prompt_id") or "").strip()
    if not prompt_id:
        return _finish(receipt_path, ok=False, code="comfyui_no_prompt_id",
                       message="ComfyUI /prompt 未返回 prompt_id", endpoint=endpoint)

    history_url = f"{endpoint}/history/{prompt_id}"
    try:
        safenet.check_url(history_url)
    except safenet.UnsafeTarget as exc:
        return _finish(receipt_path, ok=False, code="unsafe_target", message=str(exc), endpoint=endpoint)

    deadline = time.monotonic() + poll_timeout
    image_meta: dict | None = None
    while time.monotonic() <= deadline:
        try:
            with safenet.safe_urlopen(Request(history_url, headers=headers), timeout=timeout) as resp:
                history_raw = json.loads(resp.read().decode("utf-8", "replace"))
        except (HTTPError, URLError, OSError, ValueError) as exc:
            return _finish(receipt_path, ok=False, code="comfyui_history_failed",
                           message=f"查询 ComfyUI /history 失败：{exc}", prompt_id=prompt_id)
        entry = history_raw.get(prompt_id) if isinstance(history_raw, dict) else None
        if isinstance(entry, dict):
            status_obj = entry.get("status") or {}
            if status_obj.get("status_str") == "error":
                return _finish(receipt_path, ok=False, code="comfyui_exec_error",
                               message="ComfyUI 工作流执行失败", prompt_id=prompt_id)
            image_meta = _comfyui_first_image(entry)
            if image_meta is not None:
                break
        time.sleep(poll_interval)

    if image_meta is None:
        return _finish(receipt_path, ok=False, code="comfyui_timeout",
                       message="等待 ComfyUI 出图超时", prompt_id=prompt_id)

    view_qs = urlencode({
        "filename": str(image_meta.get("filename") or ""),
        "subfolder": str(image_meta.get("subfolder") or ""),
        "type": str(image_meta.get("type") or "output"),
    })
    view_url = f"{endpoint}/view?{view_qs}"
    blob = bytearray()
    limit = imagetypes.MAX_IMAGE_BYTES
    try:
        safenet.check_url(view_url)
        with safenet.safe_urlopen(Request(view_url, headers=headers), timeout=timeout) as resp:
            while chunk := resp.read(min(65536, limit + 1 - len(blob))):
                blob.extend(chunk)
                if len(blob) > limit:
                    break
    except (HTTPError, URLError, OSError, ValueError) as exc:
        return _finish(receipt_path, ok=False, code="comfyui_view_failed",
                       message=f"回收 ComfyUI 图像失败：{exc}", prompt_id=prompt_id)

    if len(blob) > limit:
        return _finish(receipt_path, ok=False, code="comfyui_image_too_large",
                       message=f"ComfyUI 回收图像超过 {limit} 字节上限", prompt_id=prompt_id)
    if len(blob) < 64:
        return _finish(receipt_path, ok=False, code="empty_image",
                       message=f"ComfyUI 回收图像只有 {len(blob)} 字节", prompt_id=prompt_id)
    if imagetypes.sniff(bytes(blob[:32])) is None:
        return _finish(receipt_path, ok=False, code="comfyui_view_failed",
                       message="ComfyUI /view 返回的内容不是有效图片", prompt_id=prompt_id)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    tmp.write_bytes(blob)
    tmp.replace(out_path)
    latency_ms = int((time.monotonic() - started) * 1000)
    return _finish(
        receipt_path,
        ok=True,
        code="",
        message="",
        engine="comfyui",
        prompt_id=prompt_id,
        latency_ms=latency_ms,
        bytes=len(blob),
        filename=str(image_meta.get("filename") or ""),
    )


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
    out_path = Path(request["output"])
    receipt_path = Path(request["receipt"])
    if request.get("engine") == "comfyui":
        return _run_comfyui(request, endpoint, out_path, receipt_path)

    path = request.get("path") or "/sdapi/v1/txt2img"
    body = dict(request.get("body") or {})
    secret_env = (request.get("secret_env") or "").strip()

    if not endpoint:
        return _finish(receipt_path, ok=False, code="endpoint_missing",
                       message="没有可用的引擎端点（实例没登记或已停用）")

    init_image = str(request.get("init_image") or "").strip()
    if init_image:
        init_path = Path(init_image)
        if not init_path.is_file():
            return _finish(receipt_path, ok=False, code="init_image_missing",
                           message="参考底图不存在", endpoint=endpoint, path=path)
        body["init_images"] = [base64.b64encode(init_path.read_bytes()).decode("ascii")]
        if path == "/sdapi/v1/txt2img":
            path = "/sdapi/v1/img2img"

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
    # **拼出来的 URL 也要过地址闸**（2026-11-09 外部审查第 7 条）。
    #
    # 原来只有 `cloud_run._download` 调 `check_url`，这条路径一次都没有：
    # `endpoint` 来自实例配置、`path` 来自代码，单看都可信，但**拼起来的字符串**
    # 才是真正要发出去的地址。`safenet.safe_urlopen` 只关重定向跟随、
    # **不校验目标**，所以"用了 safe_urlopen"不等于"目标可信"。
    # 这条请求会带 `Authorization: Bearer <sharedSecret>`，落错地方就是交密钥。
    try:
        safenet.check_url(url)
    except safenet.UnsafeTarget as exc:
        return _finish(receipt_path, ok=False, code="unsafe_engine_url",
                       message=f"{url} 不可信：{exc}",
                       latency_ms=int((time.perf_counter() - started) * 1000))
    try:
        req = Request(url, data=payload, headers=headers, method="POST")
        # 不跟随重定向：这条请求可能带 `Authorization: Bearer <真实 sharedSecret>`，
        # 而 urllib 跟随 3xx 会把请求头一起带到新地址。UnsafeTarget 是 ValueError
        # 子类，必须排在最前面接住，否则会落到下面的网络分支被说成"连不上"。
        with safenet.safe_urlopen(
                req, timeout=float(request.get("timeout") or POLL_HINT)) as resp:
            raw = resp.read()
    except safenet.UnsafeTarget as exc:
        return _finish(receipt_path, ok=False, code="unsafe_redirect",
                       message=f"{url} 没有发出请求：{exc}",
                       latency_ms=int((time.perf_counter() - started) * 1000))
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
        with safenet.safe_urlopen(Request(endpoint + "/sdapi/v1/options"),
                                  timeout=10) as resp:
            engine_model = str(json.loads(resp.read().decode("utf-8", "replace"))
                               .get("model") or "")
    except (URLError, OSError, ValueError, HTTPError):
        # 含 safenet.UnsafeTarget（ValueError 子类）：这一步是账不是闸门，
        # 被重定向拦下就留空 engine_model，不让整个作业失败。
        pass

    return _finish(receipt_path, ok=True, code="", message="",
                   latency_ms=latency_ms, bytes=len(blob),
                   mode=("img2img" if init_image else "txt2img"),
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
