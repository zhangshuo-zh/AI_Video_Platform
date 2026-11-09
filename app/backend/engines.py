"""本机常驻服务型引擎（Draw Things 这类）的状态探测。

为什么单开一个模块而不是塞进 `runtime.py`：`runtime.py` 的模块头把边界写死了
——"只做文件系统检查，不 import MLX、不加载权重、不发网络请求"，它之所以能
被设置页随手调用，正是因为这句话。服务型引擎的探测必须发一次 HTTP，
把它塞进去等于让"点开一页设置会不会联网"变成要看代码才知道的事。
所以这里专管"打一次本机服务、读它的自述"，`runtime.py` 继续只管文件。

三条纪律：

1. **只读，不提交任何东西**。探测只做 `GET /sdapi/v1/options`。这条不是形式主义：
   实测过 Draw Things 的 `POST /sdapi/v1/options` 返回 404，也就是说这个服务
   压根不接受写配置，探测里若带写请求只会得到 404 并把"服务在不在"和
   "我们请求姿势对不对"混成一件事。
2. **不把结论落库**（CR-ENGINE-001 v1.1 边界②）。返回值给界面现读现显示；
   要留历史就写 `engine_probe_log`（0042）——实例探测的事件表，形状对得上
   （instance_id ＋ state ＋ detail），`POST /api/engines/{id}/probe` 与建实例
   都会顺手记一条。`model_validation` 别再接这个活：它的键是
   (capability, backend, provider) 的验证凭据，不是"某条端点在某次探了"。
3. **超时短**。5 秒。服务没起时 TCP 连接多半立刻被拒，真连上不回话才是少数；
   探测卡在设置页里十秒钟，人只会以为页面坏了。

证据来源（2026-11-06 目标机实测，见 `文档/开发/引擎接入/00-接入前现状审计.md` §5b）：
`GET /sdapi/v1/options` 回 200、87 个扁平键，其中 **`model` 是当前选中的权重文件名**，
且这份返回体**随模型变**（切档后 width/height 自己变 1024）。也就是说
"引擎在线"和"引擎当前用的是哪一档"是两件事，本模块两样都取回来，由调用方分开判。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request

import safenet
from repositories.engine_instances import engine_instances

# 这一族引擎的状态来自"打一次本机服务"，不是"看一次文件系统"。
# 清单与界面都用这个集合判断该走哪条探测路，避免出现第二份名单。
# 2026-11-06 加入 ollama：本机常驻 HTTP 服务，与 Draw Things 同一族探测形状
# （目标机实测 GET /api/tags 回 200 与 models[]）。
SERVICE_ENGINES = frozenset({"draw_things", "ollama"})

# 其中**只有一个当前模型**的那一小族，才需要提交前核对"引擎现在选的是哪一档"。
# Ollama 不在这一族里：它的模型名是每次请求里带的，不存在"引擎自己选着别的档"
# 这件事——去核对一个不存在的概念，只会得到一句假话或一个假阻断。
# 分开两个集合，而不是给 draw_things 写特判：下一个服务型引擎进来时
# 必须在这里表态"有没有单一当前模型"，这是 W-B 那条边界的自然延伸。
MODEL_GATED_ENGINES = frozenset({"draw_things"})

# 各引擎的"自述端点"。只有实测存在的才列进来：Draw Things 实测没有
# /sdapi/v1/sd-models（404），所以这里不写一个不存在的端点凑数。
_INFO_PATHS = {"draw_things": "/sdapi/v1/options", "ollama": "/api/tags"}

_TIMEOUT_SECONDS = 5.0
# 公开别名：显式探测档的超时只有这一把尺。cloud_probe 原来自己抄了一份 5.0，
# 两份数字会各自漂移——改了一边没人改另一边，"同一把尺"就成了注释里的谎话。
PROBE_TIMEOUT_SECONDS = _TIMEOUT_SECONDS

# 列表接口用的超时。为什么与手动探测分开：
# 「打开一页」不该被一台卡住的服务拖住，而「点一下检查连接」是人明确要求
# 把这一台问到底。0.8 秒够本机服务回一次自述（实测 /api/tags 与
# /sdapi/v1/options 都在几十毫秒级），连不上时 TCP 立刻被拒更是零等待；
# 真超过这个时间说明这台服务在半死状态，列表该如实说"回话太慢"而不是陪着等。
LIST_TIMEOUT_SECONDS = 0.8


@dataclass(frozen=True)
class EngineStatus:
    """一次探测的结果。state 三档与 `runtime.Runtime`／`adapters.health()` 共用一套词。"""

    state: str            # ok / missing / unknown
    detail: str
    values: dict          # endpoint、model（当前选中的权重文件名，可能为空）
    # 这一次是不是"用短超时探到的"。列表接口用短超时是为了不拖住页面，
    # 但"太慢"不等于"这台服务坏了"——所以这种结论可以给人看，不该被当成
    # 下次手动探测的缓存答案。默认 False＝权威结论。
    soft: bool = False

    def as_health(self) -> dict:
        return {"state": self.state, "detail": self.detail}


# 探测结果能留多久。一次页面装载会把同一个端点问上好几遍——模型仓库每建一行问一次、
# 顶部四数又要重算一遍目录——它们问的是同一台机器同一个瞬间。
# 15 秒是刻意的短：人刚把应用打开、回页面点一下刷新就该看见变化；
# 而一屏之内的重复探测（几十毫秒里发生）全部落在同一个窗口里。
# 手动「检查连接」与「环境检查」一律 fresh=True 绕过缓存，那是人明确要求的现查。
PROBE_TTL_SECONDS = 15.0
_PROBE_CACHE: dict[tuple[str, str], tuple[float, EngineStatus]] = {}


def resolve_endpoint(engine_code: str) -> str:
    """这一档引擎当前该打哪个地址。没有启用中的实例就返回空串（＝unknown 档）。

    只取 `enabled=1` 的第一条，**不轮询、不合并**：第一阶段一个引擎一台服务，
    真要多实例时"选哪台"得变成作业上的显式选择，不能在这里偷偷决定。
    """
    row = engine_instances.pick(engine_code)
    return (row["endpoint"] if row is not None else "")


def current_model(endpoint: str) -> tuple[str | None, str | None]:
    """读引擎自述里的当前模型名。返回 (model, 错误说明)，两者最多一个非空。"""
    info = fetch_info(endpoint)
    if info.get("error"):
        return None, info["error"]
    model = info["payload"].get("model")
    return (str(model) if model else None), None


def fetch_info(endpoint: str) -> dict:
    """GET 引擎的自述端点。返回 {"payload": dict} 或 {"error": 一句人话}。

    公开给 `runners` 用（执行前也要读一次当前模型），所以这里只包一层，
    不复制解析逻辑——同一件事有两个实现，早晚会有一份是错的。
    """
    return _get_info_for(endpoint)


def _get_info_for(endpoint: str, engine_hint: str = "",
                  timeout: float | None = None) -> dict:
    if not endpoint:
        return {"error": "没有登记启用中的引擎实例"}
    # engine_hint 是给 probe() 用的：它已经知道自己在探哪一档引擎。
    # 没有 hint 时仍按端点反查（`fetch_info()` 这条公开路径就是这么进来的）。
    engine = engine_hint or _engine_of(endpoint)
    path = _INFO_PATHS.get(engine or "")
    if path is None:
        return {"error": f"这一档引擎（{engine or '未知'}）没有已核实的自述端点，"
                         "平台不猜地址"}
    url = endpoint.rstrip("/") + path
    try:
        request = Request(url, method="GET")
        # 不跟随重定向：端点是人填的，一次 302 就能把它指去任何地方
        # （包括云元数据接口）。这里抛的 UnsafeTarget 是 ValueError 子类，
        # 必须单独接在下面——否则会被当成"连不上"，丢掉"有人在指路"这个信息。
        with safenet.safe_urlopen(request,
                                  timeout=timeout or _TIMEOUT_SECONDS) as response:
            raw = response.read()
    except safenet.UnsafeTarget as exc:
        return {"error": f"{url} 没有发出请求：{exc}", "timed_out": False}
    except (URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
        return {"error": f"{url} 连不上：{reason}", "timed_out": timed_out}
    except HTTPError as exc:
        return {"error": f"{url} 返回 HTTP {exc.code}"}
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"error": f"{url} 的应答不是 JSON，不像这个服务的自述端点"}
    if not isinstance(payload, dict):
        return {"error": f"{url} 返回的不是一个对象"}
    return {"payload": payload}


def probe(engine_code: str, endpoint: str = "", *, fresh: bool = False,
          timeout: float | None = None) -> EngineStatus:
    """这一档服务型引擎现在能不能连上、当前选的是哪个权重。

    `fresh=True` 表示"人明确要求现查"（手动检查连接、环境检查），绕过短期缓存。
    默认走缓存：列表接口会对同一台机器连着问好几次，不缓存就是在同一屏里
    把同一个 5 秒超时的请求发四遍，页面打开时间随引擎台数线性增长。
    """
    if engine_code not in SERVICE_ENGINES:
        return EngineStatus("unknown", "这一档不是本机服务型引擎，平台不猜它的状态", {})
    address = endpoint or resolve_endpoint(engine_code)
    if not address:
        # 第三个参数不能省：`EngineStatus` 是 frozen dataclass，`values` 没有默认值。
        # 这一句在 W-B 那天起就没被走到过（那时表里还没有任何引擎会被判成服务型
        # 而同时没有实例行），2026-11-06 接 Ollama 时第一次走到就 TypeError——
        # "还没配"这一档恰恰是最常遇到的那一档，值得有一条自己的测试。
        return EngineStatus("unknown",
                            "还没有登记启用中的引擎实例：在「引擎管理 · 引擎实例」里"
                            "加一条端点（例如 http://127.0.0.1:7860）后才会检查",
                            {})
    key = (engine_code, address.rstrip("/"))
    now = time.monotonic()
    if not fresh:
        hit = _PROBE_CACHE.get(key)
        # 缓存的是**那一刻**的结论，所以命中也原样返回旧 detail：
        # 界面看到的时间点与"刚刚真的打过一次"不同这件事，由 TTL 的短（15 秒）兜住，
        # 而不是靠在这里改写文案——改写会让"最后检查时间"无处可对。
        if hit is not None and now - hit[0] < PROBE_TTL_SECONDS:
            return hit[1]
    status = _probe_now(engine_code, address, timeout)
    # 短超时探出来的结论不写进缓存：它是"这一屏等不起"的产物，不是"这台服务
    # 现在怎么样"的结论。手动探测（fresh=True）与下一次列表都该重新问一遍。
    if not status.soft:
        _PROBE_CACHE[key] = (now, status)
    return status


def peek(engine_code: str, endpoint: str = "") -> tuple["EngineStatus", float] | None:
    """读上次探测的结论（含年龄），**不发任何请求**。

    为什么需要这一档：列表接口不出网（见 `probe()` 的注解），但"我三十秒前
    刚点过探测"这件事不该被当成没查过——那一行显示"未检查"会让人以为白点了。
    所以读路径改读这一份缓存，把年龄一起带出去（"12 秒前查的"），
    过期了就如实说"要重查"，绝不拿旧结论冒充现在。
    """
    address = endpoint or resolve_endpoint(engine_code)
    if not address:
        return None
    hit = _PROBE_CACHE.get((engine_code, address.rstrip("/")))
    if hit is None:
        return None
    return hit[1], max(0.0, time.monotonic() - hit[0])


def forget_probes() -> None:
    """清空探测缓存。改配置、换端点、跑测试时用：那些时刻"旧结论"已经不作数。"""
    _PROBE_CACHE.clear()


def _probe_now(engine_code: str, address: str,
               timeout: float | None = None) -> EngineStatus:
    base = {"endpoint": address}
    info = _get_info_for(address, engine_code, timeout)
    if info.get("error"):
        # 连不上是 missing（配了但服务不在），不是 unknown（还没配）——
        # 人要做的事不同：前者去把应用和 API Server 打开，后者是来登记一行。
        return EngineStatus("missing", info["error"], base,
                            soft=bool(timeout and info.get("timed_out")))
    described = _describe(engine_code, info["payload"])
    values = {**base, "model": described["model"],
              "models": described["models"], "keys": len(info["payload"])}
    return EngineStatus("ok", described["detail"], values)


def _describe(engine_code: str, payload: dict) -> dict:
    """把一坨自述翻成"在线／当前哪一档／还有哪几档"。

    每个服务型引擎各写一条，而不是猜一个通用形状：Draw Things 回的是一个扁平
    options 表里的 `model` 键（实测），Ollama 回的是 `models[]` 数组
    （实测），两者没有任何共同结构。写成"通用解析"就会变成对其中一家说谎。
    """
    if engine_code == "ollama":
        rows = payload.get("models")
        names = [str(m.get("name")) for m in rows
                 if isinstance(m, dict) and m.get("name")] \
            if isinstance(rows, list) else []
        if not names:
            return {"model": "", "models": [],
                    "detail": "服务在线，但 /api/tags 里没有已下载的模型"}
        return {"model": "", "models": names,
                "detail": f"服务在线，本机在册 {len(names)} 档："
                          + "、".join(names[:4])
                          + ("…" if len(names) > 4 else "")}
    model = payload.get("model")
    if not model:
        return {"model": "", "models": [],
                "detail": "服务在线，但自述里读不到当前模型名"}
    return {"model": str(model), "models": [str(model)],
            "detail": f"服务在线，当前模型 {model}"}


def _engine_of(endpoint: str) -> str:
    """从已登记的实例反查引擎 code。用 endpoint 精确匹配，不做子串猜测。"""
    for row in engine_instances.instances():
        if row["endpoint"].rstrip("/") == endpoint.rstrip("/"):
            return row["engine_code"]
    return ""
