"""模型中心的业务层：能力标签、模型目录、顶部四数、场景候选。

这个模块只做**读与算**，不做选路。选路仍然只有 `services.resolve_provider()` 一处，
默认轨仍然只有 `provider_config.is_default` 一个来源——裁定 MC1（2026-11-04）＝
备用方案只提示不自动切，所以这里给出来的候选**不会**被任何提交路径自动采用。
清单里那条 `policy.no_auto_fallback = True`（`adapters.manifest_payload()`）
和这里必须说同一句话。

能力标签的两条合法来源（任务书 §5.1 禁"仅按模型名称推断"）：

1. **清单派生**：从已登记的参数与计价里数出来的，可复算、不需要人背书。
   `image_inputs` 本身就是 `ModelSpec.as_dict()` 从 `type=="image"` 的参数算出来的，
   所以"收不收参考图"这件事在清单里是有据的，不是名字猜的。
2. **人工声明**：登记行的 `capabilities_json`。未核验时界面必须标"人工声明 · 未核验"，
   只有这一档存在 `real_call` 或 `artifact_review` 的验证记录才允许升级为"已验证"。

模型名里带 `i2v` / `t2v` 不构成证据：`wan2.2-i2v-a14b` 被标成"图生视频"
是因为它登记了一个 `type=="image"` 的参数，不是因为名字里有 i2v。
"""
from __future__ import annotations

import json

import adapters
import engines
from repositories.model_center import model_center

# 能力轴 → 基础标签。这一层是清单里 `Adapter.capability` 的直接译名，
# 不是新造的能力：适配器声明自己服务哪类活儿，标签就跟着哪类。
BASE_TAG = {"text": "文本生成", "image": "文生图", "video": "文生视频",
            "tts": "语音合成", "music": "音乐生成"}

# 收参考图时，按能力轴追加的标签。图生图／图生视频都要求"确实有一个图片输入参数"，
# 所以 mflux（flux1-schnell 没有 image 参数）拿不到"图生图"，
# 而 wan2.2-i2v-a14b 拿得到——差别完全来自登记的参数表。
IMAGE_TAG = {"image": "图生图", "video": "图生视频"}

# 未接入引擎：本机装了应用、平台里一行适配代码都没有。
# 写死在这里而不是编进 ADAPTERS，是为了不让它意外走进 health()/runners.build()——
# 一个假的 adapter 会让"选了就能跑"变成谎言。
UNINTEGRATED_ENGINES = [
    {"code": "comfyui", "label": "ComfyUI", "kind": "本机应用",
     "note": "本机 /Applications 已装；平台无适配器、无 HTTP 客户端、无 workflow 契约 → 未接入"},
    # Draw Things 在 2026-11-06 的 W-B 里接进来了（清单 `draw_things`、0040 实例表、
    # `engine_http_run.py` 真出图跑通），所以从这张名单里摘掉——再留着就是一份
    # 会过期的第二真相：同一件事在清单里说"有这一档引擎"，这里说"未接入"。
    # 它的模型目录是否就绪仍然不可复核（TCC），那句话写在《00-接入前现状审计》里。
]


def _loads(raw, default):
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def derived_tags(capability: str, spec: dict) -> list[str]:
    """从清单已登记的事实里数出标签。只认参数表与计价表，不认模型名。

    2026-11-07 补：「结构化输出」。这一条原来在清单里**没有任何来源**，
    而 0035 的种子把它的要求写进了 4 个任务场景（character_bio / text_extraction /
    storyboard / …），于是那些场景永远 0 候选——不是人没配，是标签体系少了一档。
    现在的来源是 `ModelSpec.structured_output`：只有"运行时真的会按 schema 约束输出"
    的档才带它（今天只有本机 Ollama，`runners._ollama()` 会下发 `format`，
    云端 `cloud_run.py` 至今没有 response_format 分支，所以云端档一律不勾）。
    """
    tags = []
    base = BASE_TAG.get(capability)
    if base:
        tags.append(base)
    if spec.get("structured_output"):
        tags.append("结构化输出")
    if spec.get("image_inputs"):
        tags.append("参考图")
        extra = IMAGE_TAG.get(capability)
        if extra:
            tags.append(extra)
    return tags


def producible_tags(capability: str) -> set[str]:
    """这一条能力轴上，清单**有可能**产出的标签全集。

    用途只有一个：把"配不出来"和"还没配"分开。场景要「图生图」，而 image 轴
    上没有任何一档登记过图片输入参数时，无论人怎么点，那个场景都是 0 候选——
    这不是配置没做完，是清单少了一档能力来源。这两种在界面上必须说不同的话：
    前者要去写适配器（改代码），后者要去登记与探测（点界面）。

    口径与 `derived_tags()` 严格同源：同一批适配器、同一个函数数出来，
    不另写一份"哪些标签存在"的名单——那会立刻漂成第二真相。
    """
    out: set[str] = set()
    for adapter in adapters.ADAPTERS:
        if adapter.capability != capability:
            continue
        for spec in adapter.models:
            out.update(derived_tags(capability, spec.as_dict()))
    return out


def health_row(reg: dict) -> dict:
    """把一行登记伪装成 adapters.health() 眼里的槽位形状。

    登记行没有 key_env 列：密钥变量名归 provider_config 那一行管（它是选型层的字段），
    这里留空就按清单建议名或 FV_KEY_<PROVIDER> 兜底，和 health() 原本的三档优先序一致。

    `backend` 折回清单轴（`_manifest_axis`）：不折的话 `adapters.health()` 里
    `find(capability, "remote", provider)` 恒为空，一行正常登记的远程自建会拿到
    "这个 provider 没有登记适配器"——那不是事实，只是两套取值的对照没做。
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


def verified_kinds(capability: str, backend: str, provider: str,
                   model: str = "") -> set[str]:
    """这一槽位**这一档**已经落过的验证出处（real_call / artifact_review / …）。

    用它决定人工声明能不能从"未核验"升级成"已验证"。

    0043 起按档取：验证记录的"档"升为索引维度（`idx_validation_slot_model`），
    `latest_matrix(by_model=True)` 直接按 (capability, backend, provider, model)
    给最新——"1.3B 验过不等于 5B 验过"从此是查询的形状。原来的读点补丁
    （调用方拿槽位矩阵后再比一次记录里的 model）随 0043 撤掉：
    分区键里就是 model，查询结果已经只含这一档的记录。

    `model` 给空串时匹配"记录里 model 也是空串"的历史行——那是早期
    env_check 没记档时留下的组，只与"槽位也没填 model"的配置互相对应。
    """
    from repositories.validation import validations as vrepo

    latest = vrepo.latest_matrix(by_model=True).get(
        (capability, backend, provider, model), {})
    return {kind for kind, row in latest.items() if row["state"] == "ok"}


def _verification_index(slots: set | None = None) -> dict[tuple, dict[str, dict]]:
    """一次查询拿"这些槽位×档各自的每类最新验证"，供 `catalog()` 复用。

    与 `verified_kinds()` 同一份数据，区别只是调用次数：一次目录构建里有几十行，
    每行再各查一次就是给"打开一页"买几十趟回库。矩阵一次读完后按键取，
    口径不变、查询数从 O(行数) 降到 1。

    `slots` 由调用方按"已登记的那几组能力×后端×供应商×模型档"给
    （`catalog()` 就是这么做的）：不给过滤条件时窗口函数要把**全部历史**
    先算一遍再丢掉绝大多数，而 `model_validation` 只追加不改，
    历史越长"打开模型中心"越慢。0043 起键是四元组——分档是查询的形状。
    """
    from repositories.validation import validations as vrepo

    return vrepo.latest_matrix(slots, by_model=True)


def _kinds_of(index: dict, capability: str, backend: str, provider: str,
              model: str) -> set[str]:
    latest = index.get((capability, backend, provider, model), {})
    return {kind for kind, row in latest.items() if row["state"] == "ok"}


def catalog(verification: dict | None = None, *, probe: bool = False) -> list[dict]:
    """清单 × 登记 的联表视图：模型仓库页与引擎页的唯一数据源。

    三段拼出来，顺序固定：
    1) 清单里有、也登记了 → 正常一行，带健康与验证；
    2) 清单里有、没登记   → `manifest_only`「本机未登记」，让"装了不等于配了"看得见；
    3) 登记了、清单里没有 → `registration_only`「清单无此档」，参数与运行方式一律不猜。

    第 2、3 段是同一条边界的两种缺法，所以状态名按"缺的是哪一侧"取，不按"没登记"取——
    前者点一下「登记」就解决，后者得去改 `adapters.py`，混成一个词会让人对着后者反复点。

    `verification` 是可选的一次性验证矩阵（`_verification_index()`）。传进来就复用，
    不传就本函数自己查一次——形状对，但**同一页面里要用两次目录就请自己传**：
    重算一遍目录意味着再发一轮健康探测（`engines.probe` 有 15 秒缓存兜底，
    但回库与拼行仍要钱）。

    `probe=False`（默认）＝**这一条读路径不出网**。服务型引擎那些行只拿到
    "要连一次才知道"。要现查请走 `POST /api/models/probe` 或那一行的
    「环境检查」——它们是显式动作，人可以等 5 秒，打开页面不该等。
    """
    regs = [dict(r) for r in model_center.registrations()]
    # 登记按**折回后的轴**进索引：remote 那几行要能认领清单里同 provider 同模型的档，
    # 否则按界面正常登记的远程自建一进列表就变成"清单未登记"。
    reg_index = {(r["capability"], _manifest_axis(r["backend"]), r["provider"],
                  r["model_code"]): r
                 for r in regs}
    # 只查"已登记的那几组槽位×档"验证记录：目录里没登记的档不需要它们的验证历史，
    # 而 model_validation 只追加不改——不过滤的话窗口函数要扫全部历史再丢掉 99%。
    # 槽位按**折回后的轴**给：`reg_index` 里的键已经是折过的，两边必须同一口径。
    # 0043 起验证矩阵按档分区，键就是这里的四元组——不再需要"投影成三元组"
    # 再在 `_slot_model_matches` 里比一次 model：查询结果只含这一档的记录。
    slots = set(reg_index)
    index = (verification if verification is not None
             else _verification_index(slots))
    used: set[int] = set()
    rows: list[dict] = []

    for adapter in adapters.ADAPTERS:
        spec_by_code = {m.code: m.as_dict() for m in adapter.models}
        if not spec_by_code:
            # 只登记了名字的壳（volc/kling/…）：没有档可列，但引擎页要看得见它
            rows.append(_engine_only_row(adapter))
            continue
        for code, spec in spec_by_code.items():
            reg = reg_index.get((adapter.capability, adapter.backend,
                                 adapter.code, code))
            if reg is not None:
                used.add(reg["id"])
                rows.append(_registered_row(adapter, spec, reg, index, probe=probe))
            else:
                rows.append(_unregistered_row(adapter, spec))

    for reg in regs:
        if reg["id"] in used:
            continue
        # 没被清单那一轮认领的登记，还可能落在**已经被别的登记占走的档**上：
        # 同一 provider／同一模型档既有本地登记又有远程自建登记时，索引只能留一个。
        # 这种情况仍然是"清单认得这一档"，只是另开了第二条运行位置，得照常列出来；
        # 只有清单里真的没有这一档（`_find_adapter` 空或模型码不在参数表里）才算孤儿。
        adapter = _find_adapter(reg["capability"], reg["backend"], reg["provider"])
        spec = None
        if adapter is not None:
            spec = next((m.as_dict() for m in adapter.models
                         if m.code == reg["model_code"]), None)
        if spec is not None:
            rows.append(_registered_row(adapter, spec, reg, index, probe=probe))
        else:
            rows.append(_orphan_row(reg, probe=probe))
    return rows


def item_for(reg: dict) -> dict | None:
    """单条登记的联表视图——`catalog()` 的一行，只为这一条算。

    2026-11-07 审查 P2-6：写操作（登记/编辑）回读只要**这一档**的形状，
    原来却把整张目录重算一遍（几十趟回库 + 每行健康探测）。这里按
    `catalog()` 完全相同的判据拼出同一行（折回轴、验证矩阵、孤儿判定都一致），
    只是范围收窄到一条登记。返回 None＝这一档在当前目录里不存在（极少见：
    登记刚被并发撤掉）。
    """
    cap, provider, code = reg["capability"], reg["provider"], reg["model_code"]
    axis = _manifest_axis(reg["backend"])
    index = _verification_index({(cap, axis, provider, code)})
    adapter = _find_adapter(cap, reg["backend"], provider)
    spec = None
    if adapter is not None:
        spec = next((m.as_dict() for m in adapter.models if m.code == code), None)
    if spec is not None:
        return _registered_row(adapter, spec, dict(reg), index)
    # 清单里没有这一档：与 catalog() 同一判定——孤儿行，不是"清单未登记"
    return _orphan_row(dict(reg))


def _tags_of(capability: str, spec: dict, reg: dict | None, kinds: set[str]) -> list[dict]:
    """派生标签 + 人工标签，各自带来源与可信度。"""
    out = [{"tag": t, "source": "manifest", "verified": True}
           for t in derived_tags(capability, spec)]
    for t in _loads((reg or {}).get("capabilities_json"), []):
        name = str(t).strip()
        if not name or any(o["tag"] == name for o in out):
            continue
        # 人工声明只有在这一档真跑过（real_call）或人看过产物（artifact_review）之后
        # 才允许说"已验证"；否则就是"我说它行"。
        out.append({"tag": name, "source": "manual",
                    "verified": bool({"real_call", "artifact_review"} & kinds)})
    return out


def _engine_only_row(adapter) -> dict:
    return {
        "kind": "engine_only", "capability": adapter.capability,
        "backend": adapter.backend, "provider": adapter.code,
        "provider_label": adapter.label, "engine_code": adapter.code,
        "engine_label": adapter.label, "model_code": "", "model_label": "",
        "registration_id": None, "display_name": "",
        "source": "cloud" if adapter.backend == "cloud" else adapter.backend,
        "version": "", "artifact_path": "", "remote_model_id": "",
        "tags": [], "enabled": False, "note": adapter.note,
        "registered": False, "in_manifest": True, "params_known": bool(adapter.params_source),
        "prices": [], "home_env": adapter.home_env, "key_env_hint": adapter.key_env,
        "health": {"state": "unconfigured", "detail": "清单里只登记了名字，没有模型档"},
        "status": "name_only",
        "status_detail": "这一家只登记了名字，参数与计价未核验；平台不猜",
    }


def _base_spec_fields(adapter, spec) -> dict:
    return {
        "kind": "tier", "capability": adapter.capability, "backend": adapter.backend,
        "provider": adapter.code, "provider_label": adapter.label,
        "engine_code": adapter.code, "engine_label": adapter.label,
        "model_code": spec["code"], "model_label": spec["label"],
        "in_manifest": True, "params_known": bool(adapter.params_source),
        "prices": spec.get("prices", []), "home_env": adapter.home_env,
        "key_env_hint": adapter.key_env, "manifest_note": spec.get("note", ""),
        "image_inputs": spec.get("image_inputs", []),
        "params": spec.get("params", []),
    }


def _registered_row(adapter, spec, reg, verification: dict | None = None,
                   *, probe: bool = False) -> dict:
    remote = reg["backend"] == "remote"
    if remote:
        # 0021 的 model_validation 表 CHECK 只认 local/cloud，remote 这一档**留不下痕**。
        # 于是按 provider×backend 去取矩阵会把本地同 provider 的记录错认成它的——
        # 那等于拿另一台机器的凭据给这台远程服务背书。宁可空着。
        kinds: set[str] = set()
    elif verification is None:
        kinds = verified_kinds(adapter.capability, adapter.backend, adapter.code,
                               reg["model_code"])
    else:
        kinds = _kinds_of(verification, adapter.capability, adapter.backend,
                          adapter.code, reg["model_code"])
    # 0043：验证矩阵按档分区，`kinds` 已经只含这一档自己的记录——
    # "换档不背书"是查询的形状，不再需要 `_slot_model_matches` 在读点上补刀。
    # 这里仍要回答"这一档是不是槽位当前那一档"（status_detail 要点名解释），
    # 所以查一次槽位拿当前 model，但只用于文案，不参与 kinds 的判定。
    slot_model = _slot_current_model(adapter)
    same_model = slot_model == reg["model_code"]
    health = _health_of(reg, probe)
    return {
        **_base_spec_fields(adapter, spec),
        # 清单字段给的是**清单轴**（remote 折回了 local）；这一行真正的运行位置
        # 以登记为准，界面按 reg 的 backend 显示"远程自建"，
        # 折回只用于查清单，不许改写这一行在列表里的身份。
        "backend": reg["backend"], "source": reg["source"],
        "registration_id": reg["id"],
        "display_name": reg["display_name"] or spec["label"],
        "version": reg["version"],
        "artifact_path": reg["artifact_path"], "remote_model_id": reg["remote_model_id"],
        "enabled": bool(reg["enabled"]), "note": reg["note"],
        "registered": True,
        "tags": _tags_of(adapter.capability, spec, reg, kinds if same_model else set()),
        "health": health,
        "validation_kinds": sorted(kinds),
        "status": _status_of(reg, adapter, health, kinds, same_model),
        "status_detail": _status_detail_of(reg, adapter, health, same_model, slot_model),
        "created_at": reg["created_at"], "updated_at": reg["updated_at"],
    }


def _unregistered_row(adapter, spec) -> dict:
    return {
        **_base_spec_fields(adapter, spec),
        "registration_id": None, "display_name": "",
        "source": "cloud" if adapter.backend == "cloud" else adapter.backend,
        "version": "", "artifact_path": "", "remote_model_id": "",
        "enabled": False, "note": "", "registered": False,
        "tags": _tags_of(adapter.capability, spec, None, set()),
        "health": {"state": "unconfigured", "detail": "未登记，平台没有查过这台机器上有没有它"},
        "validation_kinds": [],
        "status": "manifest_only",
        "status_detail": "清单里有这一档，但本机还没登记；登记不等于文件在，"
                         "登记后再看健康状态",
    }


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


def _orphan_row(reg, *, probe: bool = False) -> dict:
    """登记了、清单里却没有这一档。参数与计价一律不猜，界面必须标出来。"""
    adapter = _find_adapter(reg["capability"], reg["backend"], reg["provider"])
    spec = {"image_inputs": [], "params": [], "prices": []}
    return {
        "kind": "orphan", "capability": reg["capability"], "backend": reg["backend"],
        "provider": reg["provider"], "provider_label": "",
        "engine_code": reg["engine_code"] or reg["provider"],
        "engine_label": reg["engine_code"] or reg["provider"],
        "model_code": reg["model_code"], "model_label": "",
        "registration_id": reg["id"], "display_name": reg["display_name"],
        "source": reg["source"], "version": reg["version"],
        "artifact_path": reg["artifact_path"], "remote_model_id": reg["remote_model_id"],
        "enabled": bool(reg["enabled"]), "note": reg["note"], "registered": True,
        "in_manifest": False, "params_known": False, "prices": [],
        "home_env": adapter.home_env if adapter else "",
        "key_env_hint": adapter.key_env if adapter else "",
        "image_inputs": [], "params": [],
        "tags": _tags_of(reg["capability"], spec, reg, set()),
        "health": adapters.health(health_row(reg), probe_service=probe,
                                 probe_timeout=engines.LIST_TIMEOUT_SECONDS),
        "validation_kinds": [],
        "status": "registration_only",
        "status_detail": "清单里没有这个 provider／这一档：参数不猜、运行方式不猜，"
                         "提交侧会被拒",
        "created_at": reg["created_at"], "updated_at": reg["updated_at"],
    }


def _slot_current_model(adapter) -> str:
    """这一槽位（能力×后端）当前配置的 model，没配过返回空串。

    0043 之后它只剩一个用途：回答"这一档是不是槽位当前那一档"
    （status_detail 的解释文案与 `_status_of` 的 same_model 半边）。
    验证该不该背书已经由查询本身回答了——矩阵按档分区，
    记录里不是这一档的根本不会出现在 `kinds` 里。
    """
    from repositories.providers import providers

    slot = providers.slot(adapter.capability, adapter.backend)
    if slot is None:
        return ""
    return (slot["model"] or "").strip()


def _status_of(reg, adapter, health: dict, kinds: set[str], same_model: bool) -> str:
    """把"登记状态／引擎连接状态／实际可用状态"三件事收成一个人能读的档。

    顺序是刻意的：先看启没停，再看环境，最后才谈验证。
    引擎在线不代表模型可用，模型文件在也不代表工作流依赖齐——
    所以这里任何一档都不许往上跳。
    """
    if not reg["enabled"]:
        return "disabled"
    if adapter is None:
        return "registration_only"
    if health["state"] == "missing":
        # 缺的东西分两类：密钥没值＝连不上；本地缺文件＝依赖/权重不齐
        return "connection_failed" if reg["backend"] == "cloud" else "dependency_missing"
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


def stats(rows: list[dict] | None = None) -> dict:
    """顶部四数。口径写进返回值，界面只负责念，不负责自己算。

    `rows` 是可选的一份已算好的目录：页面装载时 `GET /api/models` 已经算过一次，
    `GET /api/models/stats` 再算一次就等于把整张目录（含健康探测）做两遍。
    调用方可以把同一份 items 传进来，口径因此也天然一致——
    顶部那几个数必须与下面的表逐行对得上，两处各算各的迟早会漂。

    健康那一格在目录里是"要连一次才知道"，不当成"没配"——所以这里只按
    `status` 数，不做第二套健康判断。现查走 `POST /api/models/probe`，
    它拿同一份 items 重算一遍本函数，四数因此与表逐行对得上。
    """
    rows = rows if rows is not None else catalog()
    registered = [r for r in rows if r["registered"]]
    available = [r for r in registered if r["status"] == "available"]
    engines = {r["engine_code"] for r in rows if r["engine_code"]}
    # 队列那两个数走唯一出口：queue_health() 与生成中心、预检看的是同一份，
    # 超时口径也必须用同一个 WORKER_MAX_RUNTIME_SECONDS，不然"跑超了没"两边会给出两个答案
    from config import WORKER_MAX_RUNTIME_SECONDS
    from repositories.jobs import jobs as job_repo

    q = job_repo.queue_health(WORKER_MAX_RUNTIME_SECONDS)
    by_cap: dict[str, int] = {}
    for r in registered:
        by_cap[r["capability"]] = by_cap.get(r["capability"], 0) + 1
    return {
        "registered_total": len(registered),
        "registered_by_capability": by_cap,
        "available_total": len(available),
        "needs_attention": len(registered) - len(available),
        "engine_total": len(engines),
        "engine_params_known": len({r["engine_code"] for r in rows
                                    if r.get("params_known") and r["engine_code"]}),
        "unintegrated_engines": [dict(e) for e in UNINTEGRATED_ENGINES],
        "queued": q["queued"], "running": q["running"],
        "caliber": {
            "registered_total": "model_registration 的行数（一台机器上的实例登记），"
                                "不含清单里有但没登记的档",
            "available_total": "已登记 ∩ 已启用 ∩ 健康就绪 ∩ 这一档自己有过真实调用或产物验收",
            "needs_attention": "已登记但没到 available 的行数（含未验证、缺依赖、连接失败、已禁用）",
            "engine_total": "清单里出现过的 adapter code 去重数；"
                            "未接入的本机应用单列，不计入这个数",
            "queued": "repositories/jobs.queue_health().queued，与生成中心同一个口径",
            "running": "repositories/jobs.queue_health().running，同上",
        },
    }


def resolve_scene(scene_key: str) -> dict:
    """场景 → 候选与挡路原因。只读：不选路、不写库、不启动任务（MC1 裁甲）。"""
    row = None
    for p in model_center.policies():
        if p["scene_key"] == scene_key:
            row = dict(p)
            break
    if row is None:
        return {"scene_key": scene_key, "found": False, "candidates": [],
                "blockers": [f"没有这个任务场景：{scene_key}"]}
    required = _loads(row["required_json"], [])
    return _resolve(row, required)


def _resolve(row: dict, required: list[str]) -> dict:
    """候选＝登记且启用且健康就绪且**能力齐**；其余一律进 blockers 并说清缺哪一项。

    为什么状态不合格的也要点名（而不是悄悄从列表里消失）：任务书 §七 规则 7 要的是
    "用户显式指定模型时如果无法执行，说明具体原因"。人最不该看到的是
    "我明明登记了它，候选里却没有，也没人告诉我为什么"。
    备用轨同样只进 candidates 列表并标 role=fallback，不给它任何自动生效的通道。
    """
    out: dict = {
        "scene_key": row["scene_key"], "scene_label": row["scene_label"],
        "capability": row["capability"], "required": required, "found": True,
        "preference": row["preference"],
        "cloud_confirm_required": bool(row["cloud_confirm_required"]),
        "candidates": [], "blockers": [],
        "policy_note": "本接口只回答「谁可能行」；选路仍由 resolve_provider() 按 "
                       "provider_config.is_default 决定，备用轨不会自动顶上。",
    }
    # 场景要的标签里，有哪几档**整个清单都产不出来**。这是"配不出来"与
    # "还没配"的分界：前者要改 adapters.py（写清这一档凭哪个已登记的参数收
    # 条件图），后者只是去登记、去点探测。两种必须说不同的话——都写成
    # "没有候选"，人就会对着一个永远点不亮的场景反复配置（2026-11-07 审查里
    # character_variant / text_extraction 等 4 个场景正是这样）。
    producible = producible_tags(row["capability"])
    for r in catalog():
        if r["capability"] != row["capability"] or not r["registered"]:
            continue
        label = r["display_name"] or r["model_label"] or r["model_code"]
        reasons: list[str] = []
        if r["status"] == "disabled":
            reasons.append("已禁用")
        elif r["status"] == "registration_only":
            # 这一档在本机登记了，但清单里没有它——点多少次「登记」都没用，
            # 得去 adapters.py 补规格，或者撤掉这条登记。措辞必须让这一步显形。
            reasons.append("清单里没有这一档（参数与运行方式都不猜，要去 adapters.py 补规格）")
        elif r["status"] in ("dependency_missing", "connection_failed"):
            reasons.append(f"{STATUS_LABEL.get(r['status'], r['status'])}：{r['health']['detail']}")
        elif r["status"] == "not_installed":
            reasons.append(STATUS_LABEL["not_installed"])
        elif r["status"] == "unchecked" and not r["health"].get("probe_deferred"):
            # 真·未检查（平台自己也查不了）：照旧点名。
            reasons.append(STATUS_LABEL["unchecked"])
        # 「列表里还没现查过」（probe_deferred）**不算**挡路：目录本身不出网（见
        # catalog 的注解），服务型引擎在列表里必然是这一档——把它写成挡路原因，
        # 等于让每个用本机引擎的场景永远 0 候选，而真相只是"还没人点过一次探测"。
        # 候选里会带「未检查」徽章，人一眼看得出这一条没现查过。
        # unverified 同样允许：没验过不等于不能用，界面带着"未验证"徽章进候选。
        # 人工声明可以补齐候选，**但不能补"清单里根本没有这一档"**：
        # 那等于用一句自我声明冒充能力证明，与这一页"健康≠验证"的纪律自相矛盾。
        # 判据是 `producible_tags(capability)`——清单这一轴有可能产出的标签全集。
        # 注意：被剔掉的只是"清单这一轴根本产不出来"的那些声明；清单能产出、
        # 只是这一档没登记的，声明仍然算数（那是人的专业判断，不是凭空造能力）。
        have = {t["tag"] for t in r["tags"]
                if t["source"] == "manifest" or t["tag"] in producible}
        missing = [t for t in required if t not in have]
        if missing:
            hard = [t for t in missing if t not in producible]
            soft = [t for t in missing if t in producible]
            if hard:
                reasons.append("缺能力 " + "、".join(hard)
                               + "（清单里没有任何一档能产出它；人工声明不替代清单事实）")
            if soft:
                reasons.append("缺能力 " + "、".join(soft))
        if reasons:
            # 名字之外带上 model_code：人扫"我登记的那一档为什么不在候选里"时，
            # 只有显示名会认不出来（显示名是可以随便改的）
            out["blockers"].append(f"{label}（{r['model_code']}）：" + "；".join(reasons))
            continue
        role = "fallback" if (r["provider"] == row["fallback_provider"]
                              and r["model_code"] == row["fallback_model"]) else "primary"
        out["candidates"].append({
            "registration_id": r["registration_id"], "provider": r["provider"],
            "model_code": r["model_code"], "backend": r["backend"],
            "label": label, "role": role, "status": r["status"],
            "status_label": STATUS_LABEL.get(r["status"]),
            "needs_cloud_confirm": r["backend"] == "cloud",
        })
    impossible = [t for t in required if t not in producible]
    out["required_producible"] = not impossible
    out["impossible_tags"] = impossible
    if impossible:
        out["blockers"].append(
            "这几项必需能力在清单里**没有任何来源**：" + "、".join(impossible) + "。"
            + "「" + row["scene_label"] + "」因此在任何配置下都不可能有候选——"
            + "要让它有解，得先在 adapters.py 里给这一轴登记一档真的具备该能力的模型，"
            + "或者在策略里把这一项从「必需」降下来。平台不会替你把它当成可用")
    elif not out["candidates"]:
        out["blockers"].append(
            f"场景「{row['scene_label']}」没有可用候选：这一类能力里"
            f"没有一档同时满足 {'、'.join(required) or '（无必需能力）'}")
    return out
