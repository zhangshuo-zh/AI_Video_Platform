"""云端引擎的显式探测：人点「检查引擎连接」时，替他去供应商网关问一次「路通不通、钥匙对不对」。

为什么单开一个模块：`engines.py` 的模块头把自己锁在"本机常驻服务型引擎"这一族
（Draw Things／Ollama，探的是局域网里的自己人），`cloud_run.py` 的模块头把自己
锁在"一次性子进程执行器，不碰数据库、不认识 Job"。云端探测既不是本机服务、
也不执行任何东西，硬塞进哪一家都违反那一侧写死的边界——所以住在这里，
两边能复用的东西按名字借，不复制第二份：

* 脱敏借 `cloud_run._redact`：供应商 401 应答会回显打码后的密钥尾巴
  （实测 `****e000`），执行链已经规定"出错信息先过一遍 _redact"，探测同理。
  两处各写一份"抹密钥"，早晚会有一份漏。
* 状态码归一借 `cloud_run._error_code`：401/403 是钥匙问题、402 是欠费、
  429 是限流——同一条映射不许有第二套口径。
* 地址闸借 `safenet.check_url` ＋ 不跟随重定向的 opener：base URL 来自
  环境变量（部署时人填的），和被劫持的应答一样属于"配置输入"，不能是
  元数据地址，也不能被 302 带跑。

四条诚实纪律：

1. **只有实测存在的路径才探**（`_PROBES`，与 `engines._INFO_PATHS` 同一 doctrine）。
   未核验的供应商（volc/minimax/kling/…）连 base URL 平台都没有，如实报
   `no_endpoint`，绝不拿"没有这回事"的猜测冒充一次探测——和 grpc/cli 实例的
   `probe_unsupported` 是同一句话（`repositories/engine_instances.py`）。
2. **没配密钥不发请求**。密钥变量没值＝连都没连，如实报 `no_key`——这一次
   既不碰网络（CI 与离线机器上点按钮不会卡 5 秒），也不冒充"网关活着"这种
   用户此刻用不上的结论。下一步动作明确：填环境变量，再点一次。
3. **各档结论各说各的**：`ok` 只说"端点应答、密钥被接受"，永远不等于
   "那一档模型可用"（可用要过验证那几关，`_status_of` 不跳档，这里也不跳）；
   `auth_rejected` 说"路通、钥匙不对"——它和 `unreachable`（连不上）是两件
   不同的事，用户要做的事也不同（换钥匙 vs 查网络）。
4. **只在显式动作里出网**。本模块没有任何读路径会调用它；`probe_models`
   是人按下按钮才走到这里。缓存只存"真拿到了应答"的结论（含被拒——那是
   网关给的确定答案），连不上是瞬时状态不缓存；TTL 与
   `engines.PROBE_TTL_SECONDS` 同值共用一个常量，一屏之内不为同一台网关问好几次。

证据来源（2026-11-08 目标机实测）：
* DeepSeek `GET /models`：无密钥 401（鉴权先于路由，任何路径都先被拒），
  假密钥 401 且回显 `****e000`；带有效密钥 200 回模型清单——所以"带了密钥
  却没回 401/403"就是密钥被接受。
* DashScope `GET /api/v1/tasks/{id}`：无密钥 401 "No API-key provided"，
  假密钥 401 "Invalid API-key provided"——同样先查钥匙再查任务，所以拿一个
  必然不存在的 id 去探这条执行链轮询用的路径（`cloud_run._wan` 的 poll_url），
  钥匙对了才会走到"任务不存在"（404 也算钥匙过了闸，回执原样带进 detail）。
"""
from __future__ import annotations

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request

import adapters
import cloud_run
import engines
import safenet

# 与本机服务同一把尺：显式动作等得起 5 秒；半死的网关卡在这一档，
# 人看到的是"连不上"加一句原因，而不是页面转圈。共用 engines 的常量而不是
# 抄一个 5.0——抄来的数字会各自漂移，改了一边没人改另一边。
PROBE_TIMEOUT_SECONDS = engines.PROBE_TIMEOUT_SECONDS

# provider code → 探测目标。只有这两家登记了路径：它们的请求形状是执行链
# （`cloud_run._deepseek`／`cloud_run._wan`）真正在发的，鉴权行为实测过。
# 其余云端 adapter 是"未核验的候选名"，平台连 base URL 都没有——不猜地址。
_PROBES: dict[str, dict] = {
    "deepseek": {
        # base 与执行链同一出处：环境变量覆盖，否则官方默认。
        "base_env": "DEEPSEEK_BASE_URL",
        "base_default": cloud_run.DEFAULT_DEEPSEEK_BASE,
        "path": "/models",
    },
    "wan": {
        # 执行链明确要求 ALIYUN_WAN_BASE_URL（带 Workspace 前缀），
        # 平台不猜地址——没配就是没得探，这一档和"配了但连不上"不同。
        "base_env": "ALIYUN_WAN_BASE_URL",
        "base_default": "",
        # 探的是执行链轮询用的路径（poll_url 的形状）；id 用固定假值，
        # 鉴权先于查档是实测过的，钥匙对不对在这条路上分得开。
        "path": "/api/v1/tasks/probe-00000000-0000-0000-0000-000000000000",
    },
}

# 缓存：只存真拿到了 HTTP 应答的结论。连不上（网络层失败）是瞬时状态，
# 与 `engines._PROBE_CACHE` 不缓存超时同一口径；没出网的档（no_key／
# no_endpoint）现场算，环境变量一改下一枪就得反映新事实。
_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}


def forget_cache() -> None:
    """清空云端探测缓存。测试与改配置后用，与 `engines.forget_probes()` 对称。"""
    _CACHE.clear()


def probe_registered(rows: list[dict], *, fresh: bool = False) -> list[dict]:
    """对登记过的云端轨逐家探一次，返回结果行（同一家不同能力轨去重）。

    `rows` 是 `model_center.registrations()` 的形状；本地轨直接跳过
    （服务型引擎由 `engines.probe` 那一路负责，两条路在结果窗里各占一段）。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        if row["backend"] != "cloud":
            continue
        adapter = adapters.find(row["capability"], "cloud", row["provider"])
        if adapter is None or adapter.code in seen:
            continue
        seen.add(adapter.code)
        out.append(probe_one(adapter, fresh=fresh))
    return out


def probe_one(adapter, *, fresh: bool = False) -> dict:
    """一家云端引擎现在的可达性与密钥接受情况。绝不出其模块头的范围。"""
    spec = _PROBES.get(adapter.code)
    base = {"provider": adapter.code, "label": adapter.label,
            "capability": adapter.capability, "key_env": "",
            "url": "", "has_key": False}
    if spec is None:
        # 与 grpc/cli 实例的 probe_unsupported 同一句 doctrine：没有探测分支
        # 就说"没有"，不拿一次别的请求的失败替它编结论。
        return {**base, "state": "no_endpoint",
                "detail": "这家供应商还未核验，平台没有登记它的端点，不编造探测结论"}

    env_name = adapters.resolved_key_env(adapter.code, adapter.capability, "cloud")
    base["key_env"] = env_name or ""
    secret = os.environ.get(env_name, "") if env_name else ""
    base["has_key"] = bool(secret)

    base_url = (os.environ.get(spec["base_env"]) or spec["base_default"]).strip().rstrip("/")
    if not base_url:
        return {**base, "state": "no_endpoint",
                "detail": f"没有配置 {spec['base_env']}（平台不猜地址），没有可探的端点"}
    url = base_url + spec["path"]
    base["url"] = url

    if not secret:
        # 纪律 2：没配密钥不发请求。这一档不缓存——填上变量的下一枪就该变。
        return {**base, "state": "no_key",
                "detail": f"密钥变量 {base['key_env']} 没有值：没有发出请求，"
                          f"填上环境变量后重新点一次才能验路通不通、钥匙对不对"}

    key = (adapter.code, url)
    now = time.monotonic()
    if not fresh:
        hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < engines.PROBE_TTL_SECONDS:
            return hit[1]
    result = _probe_now(url, secret, base)
    if result["cached"]:
        _CACHE[key] = (now, result)
    return result


def _probe_now(url: str, secret: str, base: dict) -> dict:
    try:
        safenet.check_url(url)
    except safenet.UnsafeTarget as exc:
        # 被地址闸拦下＝平台根本没发这个请求，如实说"没连"，不冒充网络失败原因。
        return {**base, "state": "unreachable", "cached": False,
                "detail": cloud_run._redact(
                    f"地址未通过平台安全边界，没有发出请求：{exc}", secret)}
    try:
        request = Request(url, method="GET")
        request.add_header("Authorization", f"Bearer {secret}")
        opener = safenet.opener_without_redirects()
        with opener.open(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None) or response.getcode()
            raw = response.read(4096)
    except HTTPError as exc:
        status = exc.code
        raw = _safe_read(exc)
    except safenet.UnsafeTarget as exc:
        # `NoRedirect` 抛的就是它：上游要求 3xx。供应商接口不靠重定向工作，
        # 被重定向走通常是 base URL 配错了（或有人在指路）——不跟随、不猜去向，
        # 按"没拿到内容"处理，与地址闸本身同一档。
        return {**base, "state": "unreachable", "cached": False,
                "detail": cloud_run._redact(f"端点要求重定向，平台不跟随：{exc}", secret)}
    except (URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        return {**base, "state": "unreachable", "cached": False,
                "detail": cloud_run._redact(f"{url} 连不上：{reason}", secret)}

    verdict = {"cached": True}
    if status in (401, 403):
        # 带密钥被拒：路是通的（拿到了网关应答），钥匙不对。与"连不上"分开说，
        # 因为用户要做的事不同：这里是换密钥，不是查网络。
        return {**base, **verdict, "state": "auth_rejected",
                "detail": f"路通、钥匙被拒（HTTP {status}）：{_why(raw, secret)}"}
    if status >= 400:
        # 带密钥回其它 4xx/5xx —— 实测两家网关都是鉴权先于路由/查档，所以钥匙
        # 过了闸这件事仍然成立；具体码（如假任务 id 的 404）是路径自己的事，
        # 原样带进 detail，不替它编成"模型可用"。归一码复用执行链那一条映射。
        extra = ""
        if status == 404:
            # wan 的探路带的是必然不存在的任务 id：404 恰恰证明钥匙过了闸。
            extra = "；这条路径按假任务 id 探路，404 回执属于预期（鉴权先于查档）"
        return {**base, **verdict, "state": "ok",
                "detail": cloud_run._redact(
                    f"端点应答、密钥被接受（HTTP {status}，归一为 "
                    f"{cloud_run._error_code(status)}）{extra}", secret)}
    return {**base, **verdict, "state": "ok",
            "detail": cloud_run._redact(f"端点应答、密钥被接受（HTTP {status}）"
                                        f"{'，回执不是 JSON，但不影响这个结论' if _not_json(raw) else ''}",
                                        secret)}


def _safe_read(exc: HTTPError) -> bytes:
    try:
        return exc.read(4096)
    except Exception:
        return b""


def _not_json(raw: bytes) -> bool:
    """回执能不能按 JSON 读——只用来决定 detail 里那句实话要不要说。
    结论本身（2xx＝应答成功）不依赖它。"""
    try:
        json.loads(raw.decode("utf-8", "replace") or "{}")
        return False
    except ValueError:
        return True


def _why(raw: bytes, secret: str) -> str:
    """把 401/403 应答体里的原因摘出来（先脱敏）。没有就明说不硬编。"""
    try:
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        message = str(err.get("message") or "")
    else:
        message = str((data or {}).get("message") or "") if isinstance(data, dict) else ""
    return cloud_run._redact(message or "供应商没有说明原因", secret)
