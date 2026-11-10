"""云端 adapter 的一次性执行器（W10）。

worker 用 `subprocess.Popen([python, cloud_run.py, request.json])` 起它，
所以它只做三件事：**读请求、发 HTTP、写结果**。不碰数据库，也不认识 Job。
这样做的好处与本地 runner 一致：worker 仍然是唯一收尾的人，云端失败与本地
失败走同一条状态机；而且密钥只在子进程环境里用一下，不经过命令行、不进库。

两条硬边界：

1. **密钥只按环境变量名读**。请求 JSON 里只有 `key_env` 这个变量名；
   本文件从不打印它的值，出错信息也先过一遍 `_redact()`。
2. **请求体是 JSON，日志只写可公开的进度**。任务 ID、HTTP 状态、用量可以写；
   密钥、完整响应头、签名 URL 不写进日志。

结果 JSON 是 worker 读的唯一回执：
    {"ok": true, "request_id": "...", "usage": {...}, ...}
    {"ok": false, "error": {"code": "...", "message": "..."}, ...}
退出码：0 成功；2 供应商/配置错误；3 本地写文件失败。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import safenet

DEFAULT_DEEPSEEK_BASE = "https://api.deepseek.com"
DEFAULT_WAN_KEY_ENV = "DASHSCOPE_API_KEY"

# 供应商 task_id 的合法形状：字母数字下划线连字符，1..128。
# 收窄到这个程度是因为它会被拼进带凭据的 URL（见 run_wan/_poll 里的注释）。
_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _redact(text: str, secret: str) -> str:
    """把密钥值从任何要落盘/打印的文本里抹掉。

    供应商回显错误时偶尔会把 Authorization 片段带回来；这里再做一次
    值替换，保证日志和结果文件里都找不到它。
    """
    text = str(text or "")
    if secret:
        text = text.replace(secret, "***")
    # 常见的 sk- 前缀也一并打码，哪怕这次用的不是这个形状的密钥
    return re.sub(r"(?i)sk-[a-z0-9_-]{8,}", "sk-***", text)[:1200]


def _error_code(status: int | None, kind: str = "") -> str:
    if kind:
        return kind
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "insufficient_balance"
    if status == 429:
        return "rate_limit"
    if status is not None and 400 <= status < 500:
        return "invalid_request"
    if status is not None and status >= 500:
        return "provider_unavailable"
    return "provider_error"


def _guard_url(url: str) -> None:
    """带凭据出门之前，先过地址闸（2026-11-09 外部审查第 7 条）。

    为什么放在 `_post_json` / `_get_json` 里面而不是各调用点：
    这两个函数是**所有**带 `Authorization: Bearer <真密钥>` 请求的唯一入口
    （提交、查询、轮询都走它们）。闸放在入口上，新增调用点自动受保护；
    放在调用点上，下一个加调用点的人得先记得有这回事。

    原来 `check_url` 只被 `_download` 调过——那条路径恰恰**不带**密钥
    （签名 URL 不能带 Authorization），所以"唯一加了闸的地方是最不需要的"。
    """
    safenet.check_url(url)


def _post_json(url: str, body: dict, headers: dict, timeout: float):
    _guard_url(url)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    # 不跟随重定向：这两个函数带的是 `Authorization: Bearer <真密钥>`，
    # 而 urllib 跟随 3xx 时会把原请求头一起带到新地址——一次 302 就够把密钥
    # 交出去。`safenet.safe_urlopen` 与 urlopen 同签名、只是关掉跟随；
    # 3xx 在那里抛 UnsafeTarget（ValueError 子类），由 run() 的
    # `except ValueError` 落在 cloud_run_error 上，与"网络不通"分开。
    with safenet.safe_urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, dict(resp.headers), json.loads(raw.decode("utf-8") or "{}")


def _get_json(url: str, headers: dict, timeout: float):
    _guard_url(url)
    req = urllib.request.Request(url, method="GET")
    for key, value in headers.items():
        req.add_header(key, value)
    # 同 _post_json：轮询也带 Bearer，也不跟随重定向。
    with safenet.safe_urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, dict(resp.headers), json.loads(raw.decode("utf-8") or "{}")


def _download(url: str, target: Path, timeout: float,
              max_bytes: int | None = None) -> int:
    """视频是签名 URL，不能带 Authorization；只按 URL 取字节。

    **这条 URL 来自供应商应答，是外部输入**（2026-11-08 补两道闸）：

    1. 地址校验：不许指向云元数据/链路本地地址、不许是 `file:` 这类非 http(s)
       scheme。这条挡住"被劫持的应答让 worker 去读宿主内网"的形状。
       **loopback 与局域网仍然放行**：本机的假 HTTP 服务（测试替身）、
       内网的对象存储代理都是正常拓扑；真正危险的是链路本地的元数据接口，
       那是"不用凭据就能读到实例凭据"的一类地址。
    2. 字节上限：`MAX_DOWNLOAD_BYTES`（默认 2 GiB，可用
       `FASTVIDEO_CLOUD_MAX_DOWNLOAD_GB` 调）。原来 `while True` 读到
       流结束，一条无限流或一个 GB 级文件就能把 `data/jobs` 撑爆，
       而磁盘预检只出 WARN（见 `precheck`）。超限当场抛，
       由 worker 记成一条明确的 `download_too_large`，不留半截文件。
    """
    limit = max_bytes if max_bytes is not None else _max_download_bytes()
    try:
        safenet.check_url(url)
    except safenet.UnsafeTarget as exc:
        raise _ProviderError("unsafe_download_url", f"视频地址不可信：{exc}") from exc
    req = urllib.request.Request(url, method="GET")
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    # 不跟随重定向：一次 302 就能绕开上面那条地址校验。
    opener = safenet.opener_without_redirects()
    try:
        with opener.open(req, timeout=timeout) as resp, target.open("wb") as fh:
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise _ProviderError(
                    "download_too_large",
                    f"供应商声明视频有 {int(declared)} 字节，超过上限 {limit}")
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise _ProviderError(
                        "download_too_large",
                        f"下载超过上限 {limit} 字节，已中止（可能是无限流或异常大文件）")
                fh.write(chunk)
    except _ProviderError:
        # 半截文件必须收掉：留在 outputs/ 里会被当成"成功产物"再被 ffprobe 验一遍。
        target.unlink(missing_ok=True)
        raise
    except safenet.UnsafeTarget as exc:
        target.unlink(missing_ok=True)
        raise _ProviderError("unsafe_download_url", f"视频地址不可信：{exc}") from exc
    return size


def _max_download_bytes() -> int:
    """下载上限：默认 2 GiB，`FASTVIDEO_CLOUD_MAX_DOWNLOAD_GB` 可调（整数 GB）。"""
    raw = (os.environ.get("FASTVIDEO_CLOUD_MAX_DOWNLOAD_GB") or "").strip()
    if raw:
        try:
            gb = int(raw)
            if gb > 0:
                return gb * 1024 ** 3
        except ValueError:
            pass
    return 2 * 1024 ** 3


def _http_error(exc: urllib.error.HTTPError, secret: str) -> tuple[int, dict, str]:
    status = int(getattr(exc, "code", 0) or 0)
    body = {}
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        body = json.loads(raw or "{}")
    except (OSError, ValueError):
        body = {}
    message = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("code") or ""
        message = message or body.get("message") or body.get("code") or ""
        request_id = body.get("request_id") or body.get("id") or ""
    else:
        request_id = ""
    if not message:
        message = getattr(exc, "reason", "") or f"HTTP {status}"
    return status, {"request_id": request_id, "body": body}, _redact(message, secret)


def _usage_text(raw) -> dict:
    """只留对账用得到的 token 数，不把整份 usage 搬进库。"""
    if not isinstance(raw, dict):
        return {}
    keep = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        value = raw.get(key)
        if isinstance(value, (int, float)):
            keep[key] = int(value)
    # 个别供应商把缓存字段放在 prompt_tokens_details 里
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict):
        for key in ("cached_tokens",):
            value = details.get(key)
            if isinstance(value, (int, float)):
                keep[key] = int(value)
    return keep


def _deepseek(request: dict, result: dict) -> int:
    """文本：一次 chat/completions，把正文写进 output。"""
    params = request.get("params") or {}
    base = (os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE).rstrip("/")
    body = {
        "model": request["model"],
        "messages": [{"role": "user", "content": request["prompt"]}],
        "stream": False,
    }
    if params.get("max_tokens") is not None:
        body["max_tokens"] = int(params["max_tokens"])
    if params.get("temperature") is not None:
        body["temperature"] = float(params["temperature"])
    if "thinking" in params:
        body["thinking"] = {"type": "enabled" if params["thinking"] else "disabled"}

    secret = os.environ.get(request.get("key_env") or "", "")
    headers = {"Authorization": f"Bearer {secret}"}
    started = _now_ms()
    status, raw_headers, data = _post_json(base + "/chat/completions", body, headers,
                                           float(request.get("http_timeout") or 120))
    result["http_status"] = status
    result["latency_ms"] = _now_ms() - started
    result["request_id"] = str(data.get("id") or
                               raw_headers.get("x-request-id") or
                               raw_headers.get("X-Request-Id") or "")
    result["usage"] = _usage_text(data.get("usage"))
    choices = data.get("choices") or []
    if not choices:
        raise _ProviderError(_error_code(status), "供应商没有返回 choices")
    message = choices[0].get("message") or {}
    text = message.get("content")
    if text is None:
        raise _ProviderError("empty_response", "供应商返回了空正文")
    result["response_meta"] = {
        "finish_reason": choices[0].get("finish_reason"),
        "returned_model": data.get("model"),
        # 推理内容单独计费也常见；只记长度，不把整段思考写进结果
        "reasoning_chars": len(message.get("reasoning_content") or ""),
    }
    out = Path(request["output"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(str(text), encoding="utf-8")
    result["bytes"] = out.stat().st_size
    return 0


def _wan_usage(data: dict, params: dict) -> dict:
    """Wan 的用量在轮询响应的顶层 `usage`（不在 output 里），
    旧文档/网关也有塞进 output 的写法；两处都看，别把实际时长丢了。"""
    output = data.get("output") if isinstance(data, dict) else None
    raw = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(raw, dict) and isinstance(output, dict):
        raw = output.get("usage")
    usage = {}
    if isinstance(raw, dict):
        for key in ("duration", "video_duration", "video_ratio", "video_count",
                    "SR", "size"):
            value = raw.get(key)
            if isinstance(value, (int, float, str)):
                usage[key] = value
    # 回执没给时长时，至少把提交时的计费口径带上——对账要能回答"按几秒算的"
    usage.setdefault("duration", params.get("duration"))
    usage.setdefault("resolution", params.get("resolution"))
    return {k: v for k, v in usage.items() if v is not None}


def _wan(request: dict, result: dict) -> int:
    """视频：创建异步任务 → 轮询 → 下载 mp4。"""
    params = request.get("params") or {}
    base = (os.environ.get("ALIYUN_WAN_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise _ProviderError("missing_base_url",
                             "没有配置 ALIYUN_WAN_BASE_URL（要带 Workspace 前缀）")
    key_env = request.get("key_env") or DEFAULT_WAN_KEY_ENV
    secret = os.environ.get(key_env, "")
    headers = {"Authorization": f"Bearer {secret}",
               "X-DashScope-Async": "enable"}

    payload = {"model": request["model"],
               "input": {"prompt": request["prompt"]},
               "parameters": {}}
    for key in ("resolution", "ratio", "duration", "seed"):
        if params.get(key) is not None:
            payload["parameters"][key] = params[key]

    create_url = base + "/api/v1/services/aigc/video-generation/video-synthesis"
    started = _now_ms()
    status, _, data = _post_json(create_url, payload, headers,
                                 float(request.get("http_timeout") or 60))
    result["http_status"] = status
    output = data.get("output") or {}
    task_id = str(output.get("task_id") or "")
    if not task_id:
        raise _ProviderError(_error_code(status), "创建任务后没有拿到 task_id")
    # **task_id 是供应商给的外部数据，要当不可信输入处理**（2026-11-09 外部审查第 7 条）。
    #
    # 它会被拼进 `poll_url = base + f"/api/v1/tasks/{task_id}"`，而那条 URL
    # 是**带 Authorization 出门**的。合法 task_id 只有字母数字下划线连字符，
    # 所以白名单收得极窄；`quote(safe="")` 再兜一层，不让 `..`、`@`、`?`
    # 之类改变 URL 的结构（`..` 能改路径，`@` 能改 host）。
    if not _TASK_ID_RE.fullmatch(task_id):
        raise _ProviderError("bad_task_id",
                             f"供应商回的 task_id 形状不合法（{len(task_id)} 字符）")
    task_id = urllib.parse.quote(task_id, safe="")
    result["request_id"] = task_id
    result["task_request_id"] = str(data.get("request_id") or "")
    result["task_status"] = "CREATED"
    # 任务一建好就先落一份回执：worker 若在这时被取消/杀掉，至少能拿着
    # task_id 去供应商后台找到这次调用。最终结果会在 finally 里覆盖它。
    _write_json(Path(request["result"]), result)
    print(f"wan task {task_id} created", flush=True)

    poll_url = base + f"/api/v1/tasks/{task_id}"
    deadline = time.monotonic() + float(request.get("poll_timeout") or 1800)
    interval = float(request.get("poll_interval") or 5)
    final = {}
    while True:
        if time.monotonic() > deadline:
            raise _ProviderError("timeout", "轮询超过上限，任务仍未结束")
        time.sleep(interval)
        _, _, data = _get_json(poll_url, {"Authorization": f"Bearer {secret}"},
                               float(request.get("http_timeout") or 60))
        final = data.get("output") or {}
        state = str(final.get("task_status") or "").upper()
        print(f"wan task {task_id} status={state}", flush=True)
        if state in ("SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"):
            break
    result["task_status"] = final.get("task_status")
    result["latency_ms"] = _now_ms() - started
    result["usage"] = _wan_usage(data, params)
    if str(final.get("task_status") or "").upper() != "SUCCEEDED":
        message = final.get("message") or final.get("code") or "任务没有成功结束"
        raise _ProviderError(_error_code(None), _redact(message, secret))

    video_url = final.get("video_url") or ""
    if not video_url:
        raise _ProviderError("empty_output", "任务成功但没有 video_url")
    out = Path(request["output"])
    size = _download(video_url, out, float(request.get("download_timeout") or 300))
    result["bytes"] = size
    return 0


def cancel_task(*, provider: str, request_id: str, key_env: str = "",
                base_url: str = "", timeout: float = 15.0) -> dict:
    """向供应商请求取消一个异步任务。返回可落盘的结论 dict，不抛异常。

    **为什么这件事只能由父进程做**（2026-11-10 加）：执行中的
    `cloud_run.py` 是个子进程，它正在 `_wan()` 的轮询循环里 `time.sleep`，
    没有任何途径把它"叫停去打个电话"。worker 收到 `cancel_requested` 时
    原来只做一件事：`_terminate(proc)`——**杀掉本地进程**。供应商那边的任务
    照跑照计费，钱白花。取消动作必须由还活着的父进程（worker）发起。

    接口事实（阿里云 Model Studio，2026-10-27 版文档）：
        POST {base}/api/v1/tasks/{task_id}/cancel
        Authorization: Bearer <key>
        **只对 `PENDING`（排队未开始）的任务有效**；任务一旦 `RUNNING`，
        返回 400 `UnsupportedOperation`。所以这个函数的三态必须是可区分的：

        * `{"cancelled": True}`  —— 真取消了，不会再计费
        * `{"cancelled": False, "reason": "running"}` —— 已经在跑，取消不了
        * `{"cancelled": False, "reason": "..."}` —— 调用失败/没配置

    调用方（`worker.py`）按 reason 决定文案与是否挂「待核对」，**不能
    把"取消不了"说成"已取消"**——那正好是本轮在修的那类假话。

    安全：`request_id` 是供应商给的外部数据，会被拼进**带凭据的 URL**，
    所以先过与 `_wan` 同一个白名单（`_TASK_ID_RE`）+ quote。
    """
    out: dict = {"provider": provider, "task_id": request_id,
                 "cancelled": False, "reason": "", "http_status": None}
    if provider != "wan":
        # deepseek 是同步调用，没有异步任务可取消；别的 provider 也还没有
        # 取消接口可用。如实说"没有"比假装成功好。
        out["reason"] = "unsupported_provider"
        return out
    if not _TASK_ID_RE.fullmatch(str(request_id or "")):
        out["reason"] = "bad_task_id"
        return out
    base = (base_url or os.environ.get("ALIYUN_WAN_BASE_URL") or "").strip().rstrip("/")
    if not base:
        out["reason"] = "missing_base_url"
        return out
    secret = os.environ.get(key_env or DEFAULT_WAN_KEY_ENV, "")
    if not secret:
        out["reason"] = "missing_key"
        return out

    task_id = urllib.parse.quote(request_id, safe="")
    url = base + f"/api/v1/tasks/{task_id}/cancel"
    try:
        _guard_url(url)
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("Authorization", f"Bearer {secret}")
        with safenet.safe_urlopen(req, timeout=timeout) as resp:
            out["http_status"] = resp.status
            resp.read()
        out["cancelled"] = True
        return out
    except urllib.error.HTTPError as exc:
        status, extra, message = _http_error(exc, secret)
        out["http_status"] = status
        code = str(extra.get("code") or "")
        out["reason"] = message
        # 400 UnsupportedOperation = 任务已经不在 PENDING 了。单独标出来，
        # 因为它是**最常撞的**一种，且语义明确（已经在跑，改不了）。
        if status == 400 and ("UnsupportedOperation" in code
                              or "PENDING" in message):
            out["reason"] = "running"
        return out
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["reason"] = _redact(f"{type(exc).__name__}: {exc}", secret)
        return out


class _ProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def run(request_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    result_path = Path(request["result"])
    provider = request.get("provider") or ""
    key_env = request.get("key_env") or ""
    secret = os.environ.get(key_env, "") if key_env else ""
    result = {
        "ok": False,
        "provider": provider,
        "model": request.get("model") or "",
        "capability": request.get("capability") or "",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not secret:
        result["error"] = {
            "code": "missing_key",
            "message": f"环境变量 {key_env or '（未指定）'} 没有值",
        }
        _write_json(result_path, result)
        print("missing key: " + (key_env or "UNSET"), file=sys.stderr, flush=True)
        return 2

    try:
        if provider == "deepseek":
            code = _deepseek(request, result)
        elif provider == "wan":
            code = _wan(request, result)
        else:
            raise _ProviderError("unsupported_provider",
                                 f"cloud_run 不认识 provider「{provider}」")
        result["ok"] = True
        return code
    except _ProviderError as exc:
        result["error"] = {"code": exc.code,
                           "message": _redact(exc.message, secret)}
        return 2
    except urllib.error.HTTPError as exc:
        status, extra, message = _http_error(exc, secret)
        result["http_status"] = status
        result["request_id"] = result.get("request_id") or extra.get("request_id") or ""
        result["error"] = {"code": _error_code(status), "message": message}
        return 2
    except urllib.error.URLError as exc:
        result["error"] = {"code": "network",
                           "message": _redact(getattr(exc, "reason", str(exc)), secret)}
        return 2
    except (OSError, ValueError, KeyError) as exc:
        result["error"] = {"code": "cloud_run_error",
                           "message": _redact(f"{type(exc).__name__}: {exc}", secret)}
        return 3
    finally:
        result["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(result_path, result)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: cloud_run.py <request.json>", file=sys.stderr)
        return 3
    try:
        return run(Path(args[0]))
    except (OSError, ValueError) as exc:
        print(f"cloud_run: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
