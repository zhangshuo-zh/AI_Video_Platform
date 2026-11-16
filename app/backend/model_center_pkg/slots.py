"""模型中心：slots 业务域。"""
from __future__ import annotations
import adapters
from contextvars import ContextVar
import engines

_SLOT_MODELS: ContextVar[dict[tuple[str, str], str] | None] = ContextVar("model_slot_snapshot", default=None)



def health_row(reg: dict) -> dict:
    """把一行登记伪装成 adapters.health() 眼里的槽位形状。

    登记行没有 key_env 列：密钥变量名归 provider_config 那一行管（它是选型层的字段），
    这里留空就按清单建议名或 FV_KEY_<PROVIDER> 兜底，和 health() 原本的三档优先序一致。

    `backend` 折回清单轴（`_manifest_axis`）：不折的话 `adapters.health()` 里
    `find(capability, "remote", provider)` 恒为空，一行正常登记的远程自建会拿到
    "这个 provider 没有登记适配器"——那不是事实，只是两套取值的对照没做。

    **remote 的 `health` 本身不可信**：平台查不了别人的机器（`_health_of()` 就是
    因此对 remote 提前返回 unknown）。这个函数的产出只用于两件事：给
    `env_check` 落一条记录、让清单里的参数与计价按折回后的轴找到。谁要拿它回答
    "这一档现在能不能用"，请走 `_health_of()`。
    """
    return {"capability": reg["capability"], "backend": _manifest_axis(reg["backend"]),
            "provider": reg["provider"], "model": reg.get("model_code", ""),
            "key_env": ""}



def _health_of(reg: dict, probe: bool = False) -> dict:
    """这一档的健康。remote 特事特办：本机文件系统查不到别人的机器。

    remote 的"在线与否"归 `engine_instance` 那张表（0040），不该让
    `runtime.probe()` 拿本机目录去回答一台远端服务有没有权重——那正是
    "unknown＝平台检查不了，需要人确认"这一档存在的理由。

    `probe=False`（目录的默认）**不出网**：服务型引擎那一族只回一句
    "要连一次才知道，去哪儿连"。这条边界的理由写在 `adapters.health()` 的
    注解里——列表接口一次拼几十行，每行发一次 HTTP，等于打开一页扫一遍网。
    """
    if reg["backend"] == "remote":
        return {"state": "unknown",
                "detail": "远程自建：参数与计价按清单同一档认，端点是否在线归「引擎实例」那张表；"
                          "本机不查别人的文件系统",
                "adapter": None, "key_env": ""}
    return adapters.health(health_row(reg), probe_service=probe,
                           probe_timeout=engines.LIST_TIMEOUT_SECONDS)



def _manifest_axis(backend: str) -> str:
    """登记层的运行位置 → 清单层的轴。判据住在 `adapters.manifest_axis()`。

    这里只留一个转发，不自己写 `"local" if backend == "remote" else backend`：
    那句话此前在路由、本模块、前端各有一份，三份一起漂的后果是 remote 登记
    在列表里显示"清单未登记"。判据只许有一处（`adapters.MANIFEST_AXIS`），
    本函数与路由都从这里读。
    """
    return adapters.manifest_axis(backend)



def find_adapter(capability: str, backend: str, provider: str):
    """按登记层取值查适配器，remote 折回 local。所有读路径都走这里。

    公开名字是给路由层用的（`POST /api/models/probe` 要按 engine_code 去重探测）：
    折回这件事只许有一个实现，路由里再写一遍 `"local" if backend == "remote" else backend`
    就是第二份口径，而它迟早会和这里分家。
    """
    return adapters.find(capability, _manifest_axis(backend), provider)



# 老名字：模块内还有几处按 `_find_adapter` 调用，改成一次全量替换不如留一个别名，
# 免得下一次改动漏掉一处。两个名字指向同一个函数，不是两份实现。
_find_adapter = find_adapter



def _slot_current_model(adapter: adapters.Adapter) -> str:
    """这一槽位（能力×后端）当前配置的 model，没配过返回空串。

    0043 之后它只剩一个用途：回答"这一档是不是槽位当前那一档"
    （status_detail 的解释文案与 `_status_of` 的 same_model 半边）。
    验证该不该背书已经由查询本身回答了——矩阵按档分区，
    记录里不是这一档的根本不会出现在 `kinds` 里。
    """
    from repositories.providers import providers

    snapshot = _SLOT_MODELS.get()
    if snapshot is not None:
        return snapshot.get((adapter.capability, adapter.backend), "")
    slot = providers.slot(adapter.capability, adapter.backend)
    if slot is None:
        return ""
    return (slot["model"] or "").strip()



def _status_of(reg, adapter, health: dict, kinds: set[str], same_model: bool) -> str:
    """把"登记状态／引擎连接状态／实际可用状态"三件事收成一个人能读的档。

    顺序是刻意的：先看启没停，再看环境，最后才谈验证。
    引擎在线不代表模型可用，模型文件在也不代表工作流依赖齐——
    所以这里任何一档都不许往上跳。

    **remote 这一档要单独说**（0045）：它是"局域网另一台机器上的服务"，
    平台**天生查不了**它的健康（`_health_of()` 恒回 unknown，理由是本机不查
    别人的文件系统）。所以不能把它塞进下面那条 `unknown → unchecked` 的链——
    那会让它永远显示"未检查"，而"未检查"的意思是"你去点一下就知道了"，
    点多少次都还是 unknown，等于把一个结构事实说成一个待办。
    对 remote，健康这一环**不再是前置条件**，能证明它行不行的是验证凭据本身。
    """
    if not reg["enabled"]:
        return "disabled"
    if adapter is None:
        return "registration_only"
    if health["state"] == "missing":
        # 缺的东西分两类：密钥没值＝连不上；本地缺文件＝依赖/权重不齐
        return "connection_failed" if reg["backend"] == "cloud" else "dependency_missing"
    if reg["backend"] == "remote":
        # 健康恒 unknown，不作为判定条件：只看证据齐不齐。
        # `same_model` 对 remote 恒真（见 `_registered_row`），因为选型层的十行
        # 里没有它这一档——要求"它是槽位当前那一档"等于要求它永不为真。
        if not ({"real_call", "artifact_review"} & kinds):
            return "unverified"
        return "available"
    if health["state"] == "unconfigured":
        return "not_installed"
    if health["state"] == "unknown":
        return "unchecked"
    if not (same_model and ({"real_call", "artifact_review"} & kinds)):
        return "unverified"
    return "available"



# 状态译名。**三个"缺东西"的档名里必须写出缺的是哪一侧**——
# 原来它们叫 `not_registered`／`unregistered`／`unregistered_tier`，中文是
# 「未登记」／「清单未登记」／「只登记了名字」：三个词都含"登记"，前两个还互为前缀。
# 后果是同一屏上出现两枚长得很像的灰徽章，一个是"清单有、本机没登记"（去点登记就行），
# 另一个是"本机登记了、清单里没有这一档"（得去改 adapters.py，点多少次都没用）——
# 处置完全相反，读起来却像同一件事。名字改了两侧：
#   manifest_only    清单里有、本机没登记       → 本机未登记
#   registration_only 本机登记了、清单里没有     → 清单无此档
#   name_only         清单只登记了名字、没有档   → 清单只有名字
# 键也一起改成左右对称的一对（`manifest_only`／`registration_only`），
# 看键名就知道缺的是哪一侧，不必回头查这张表。
STATUS_LABEL = {
    "available": "可用", "unverified": "未验证", "not_installed": "未安装或未下载",
    "dependency_missing": "依赖缺失", "connection_failed": "连接失败",
    "unchecked": "未检查", "disabled": "已禁用",
    "registration_only": "清单无此档",
    "manifest_only": "本机未登记",
    "name_only": "清单只有名字",
}



def _status_detail_of(reg, adapter, health: dict, same_model: bool,
                      slot_model: str = "") -> str:
    if adapter is None:
        return "清单里没有这个 provider：参数不猜、运行方式不猜，提交侧会被拒"
    if not same_model and health["state"] == "ok":
        # 「未验证」里最费解释的一类：环境全齐、甚至真跑过作业，但这一档不是
        # 能力轨当前选中的那一档——查多少次环境、跑多少作业都不会变「可用」，
        # 因为换档不背书的另一半是"记录只认槽位当前那一档"。
        # 不点名这句话，人就会在这一行上反复点「环境检查」；解法在引擎管理那列。
        cur = f"（当前是 {slot_model}）" if slot_model else ""
        return ("环境就绪，但这一档不是能力轨当前选中的那一档" + cur
                + "：验证记录只认当前那一档，要它变「可用」请到「引擎管理」"
                  "把这一类能力的轨指到这一档；或撤掉这条登记")
    return health["detail"]
