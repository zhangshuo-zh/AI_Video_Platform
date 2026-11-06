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
   要留历史就写 `model_validation` 的 `env_check` 事件，那是另一条现成的路。
3. **超时短**。5 秒。服务没起时 TCP 连接多半立刻被拒，真连上不回话才是少数；
   探测卡在设置页里十秒钟，人只会以为页面坏了。

证据来源（2026-11-06 目标机实测，见 `文档/开发/引擎接入/00-接入前现状审计.md` §5b）：
`GET /sdapi/v1/options` 回 200、87 个扁平键，其中 **`model` 是当前选中的权重文件名**，
且这份返回体**随模型变**（切档后 width/height 自己变 1024）。也就是说
"引擎在线"和"引擎当前用的是哪一档"是两件事，本模块两样都取回来，由调用方分开判。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from repositories.engine_instances import engine_instances

# 这一族引擎的状态来自"打一次本机服务"，不是"看一次文件系统"。
# 清单与界面都用这个集合判断该走哪条探测路，避免出现第二份名单。
SERVICE_ENGINES = frozenset({"draw_things"})

# 各引擎的"自述端点"。只有实测存在的才列进来：Draw Things 实测没有
# /sdapi/v1/sd-models（404），所以这里不写一个不存在的端点凑数。
_INFO_PATHS = {"draw_things": "/sdapi/v1/options"}

_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class EngineStatus:
    """一次探测的结果。state 三档与 `runtime.Runtime`／`adapters.health()` 共用一套词。"""

    state: str            # ok / missing / unknown
    detail: str
    values: dict          # endpoint、model（当前选中的权重文件名，可能为空）

    def as_health(self) -> dict:
        return {"state": self.state, "detail": self.detail}


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


def _get_info_for(endpoint: str) -> dict:
    if not endpoint:
        return {"error": "没有登记启用中的引擎实例"}
    engine = _engine_of(endpoint)
    path = _INFO_PATHS.get(engine or "")
    if path is None:
        return {"error": f"这一档引擎（{engine or '未知'}）没有已核实的自述端点，"
                         "平台不猜地址"}
    url = endpoint.rstrip("/") + path
    try:
        request = Request(url, method="GET")
        with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except (URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"error": f"{url} 连不上：{reason}"}
    except HTTPError as exc:
        return {"error": f"{url} 返回 HTTP {exc.code}"}
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"error": f"{url} 的应答不是 JSON，不像这个服务的自述端点"}
    if not isinstance(payload, dict):
        return {"error": f"{url} 返回的不是一个对象"}
    return {"payload": payload}


def probe(engine_code: str, endpoint: str = "") -> EngineStatus:
    """这一档服务型引擎现在能不能连上、当前选的是哪个权重。"""
    if engine_code not in SERVICE_ENGINES:
        return EngineStatus("unknown", "这一档不是本机服务型引擎，平台不猜它的状态", {})
    address = endpoint or resolve_endpoint(engine_code)
    if not address:
        return EngineStatus("unknown",
                            "还没有登记启用中的引擎实例：在「引擎管理 · 引擎实例」里"
                            "加一条端点（例如 http://127.0.0.1:7860）后才会检查")
    base = {"endpoint": address}
    info = _get_info_for(address)
    if info.get("error"):
        # 连不上是 missing（配了但服务不在），不是 unknown（还没配）——
        # 人要做的事不同：前者去把应用和 API Server 打开，后者是来登记一行。
        return EngineStatus("missing", info["error"], base)
    model = info["payload"].get("model")
    values = {**base, "model": str(model) if model else "",
              "keys": len(info["payload"])}
    if not model:
        return EngineStatus("ok", "服务在线，但自述里读不到当前模型名", values)
    return EngineStatus("ok", f"服务在线，当前模型 {model}", values)


def _engine_of(endpoint: str) -> str:
    """从已登记的实例反查引擎 code。用 endpoint 精确匹配，不做子串猜测。"""
    for row in engine_instances.instances():
        if row["endpoint"].rstrip("/") == endpoint.rstrip("/"):
            return row["engine_code"]
    return ""
