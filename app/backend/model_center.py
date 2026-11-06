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
    """从清单已登记的事实里数出标签。只认参数表与计价表，不认模型名。"""
    tags = []
    base = BASE_TAG.get(capability)
    if base:
        tags.append(base)
    if spec.get("image_inputs"):
        tags.append("参考图")
        extra = IMAGE_TAG.get(capability)
        if extra:
            tags.append(extra)
    return tags


def health_row(reg: dict) -> dict:
    """把一行登记伪装成 adapters.health() 眼里的槽位形状。

    登记行没有 key_env 列：密钥变量名归 provider_config 那一行管（它是选型层的字段），
    这里留空就按清单建议名或 FV_KEY_<PROVIDER> 兜底，和 health() 原本的三档优先序一致。
    """
    return {"capability": reg["capability"], "backend": reg["backend"],
            "provider": reg["provider"], "model": reg.get("model_code", ""),
            "key_env": ""}


def verified_kinds(capability: str, backend: str, provider: str) -> set[str]:
    """这一轨上已经落过的验证出处（real_call / artifact_review / …）。

    用它决定人工声明能不能从"未核验"升级成"已验证"。
    注意口径：验证记录是按**槽位**（capability×backend×provider）落的，
    不是按模型档——同一 provider 换了一档模型，旧记录不该替新档背书，
    所以调用方必须再比一次记录里的 model 是不是同一档。
    """
    from repositories.validation import validations as vrepo

    out: set[str] = set()
    for row in vrepo.list_recent(200):
        if (row["capability"], row["backend"], row["provider"]) != (capability, backend, provider):
            continue
        if row["state"] == "ok":
            out.add(row["kind"])
    return out


def catalog() -> list[dict]:
    """清单 × 登记 的联表视图：模型仓库页与引擎页的唯一数据源。

    三段拼出来，顺序固定：
    1) 清单里有、也登记了 → 正常一行，带健康与验证；
    2) 清单里有、没登记   → 一行"未登记"，让"装了不等于配了"看得见；
    3) 登记了、清单里没有 → 一行"清单未登记"，参数与运行方式一律不猜。
    """
    regs = [dict(r) for r in model_center.registrations()]
    reg_index = {(r["capability"], r["backend"], r["provider"], r["model_code"]): r
                 for r in regs}
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
                rows.append(_registered_row(adapter, spec, reg))
            else:
                rows.append(_unregistered_row(adapter, spec))

    for reg in regs:
        if reg["id"] not in used:
            rows.append(_orphan_row(reg))
    return rows


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
        "status": "unregistered_tier",
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


def _registered_row(adapter, spec, reg) -> dict:
    kinds = verified_kinds(adapter.capability, adapter.backend, adapter.code)
    # 验证记录按槽位落，换档不背书：只有记录里 model 就是这一档时才算它验过
    same_model = _slot_model_matches(adapter, reg)
    health = adapters.health(health_row(reg))
    return {
        **_base_spec_fields(adapter, spec),
        "registration_id": reg["id"],
        "display_name": reg["display_name"] or spec["label"],
        "source": reg["source"], "version": reg["version"],
        "artifact_path": reg["artifact_path"], "remote_model_id": reg["remote_model_id"],
        "enabled": bool(reg["enabled"]), "note": reg["note"],
        "registered": True,
        "tags": _tags_of(adapter.capability, spec, reg, kinds if same_model else set()),
        "health": health,
        "validation_kinds": sorted(kinds),
        "status": _status_of(reg, adapter, health, kinds, same_model),
        "status_detail": _status_detail_of(reg, adapter, health),
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
        "status": "not_registered",
        "status_detail": "清单里有这一档，但本机还没登记；登记不等于文件在，"
                         "登记后再看健康状态",
    }


def _orphan_row(reg) -> dict:
    """登记了、清单里却没有这一档。参数与计价一律不猜，界面必须标出来。"""
    adapter = adapters.find(reg["capability"], reg["backend"], reg["provider"])
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
        "health": adapters.health(health_row(reg)),
        "validation_kinds": [],
        "status": "unregistered",
        "status_detail": "清单里没有这个 provider／这一档：参数不猜、运行方式不猜，"
                         "提交侧会被拒",
        "created_at": reg["created_at"], "updated_at": reg["updated_at"],
    }


def _slot_model_matches(adapter, reg) -> bool:
    """这一档是不是这个槽位当前真正在用的那一档。

    验证记录只到 provider 粒度（`model_validation` 按 capability×backend×provider 索引），
    所以换档之后旧记录不能替新档说话。这里比的是登记行的 model_code 与
    provider_config 里那一行的 model；槽位没配时返回 False（没配过就等于没验过）。
    """
    from repositories.providers import providers

    slot = providers.slot(adapter.capability, adapter.backend)
    if slot is None:
        return False
    return (slot["provider"] or "").strip() == reg["provider"] \
        and (slot["model"] or "").strip() == reg["model_code"]


def _status_of(reg, adapter, health: dict, kinds: set[str], same_model: bool) -> str:
    """把"登记状态／引擎连接状态／实际可用状态"三件事收成一个人能读的档。

    顺序是刻意的：先看启没停，再看环境，最后才谈验证。
    引擎在线不代表模型可用，模型文件在也不代表工作流依赖齐——
    所以这里任何一档都不许往上跳。
    """
    if not reg["enabled"]:
        return "disabled"
    if adapter is None:
        return "unregistered"
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


STATUS_LABEL = {
    "available": "可用", "unverified": "未验证", "not_installed": "未安装或未下载",
    "dependency_missing": "依赖缺失", "connection_failed": "连接失败",
    "unchecked": "未检查", "disabled": "已禁用", "unregistered": "清单未登记",
    "not_registered": "未登记", "unregistered_tier": "只登记了名字",
}


def _status_detail_of(reg, adapter, health: dict) -> str:
    if adapter is None:
        return "清单里没有这个 provider：参数不猜、运行方式不猜，提交侧会被拒"
    return health["detail"]


def stats() -> dict:
    """顶部四数。口径写进返回值，界面只负责念，不负责自己算。"""
    rows = catalog()
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
    for r in catalog():
        if r["capability"] != row["capability"] or not r["registered"]:
            continue
        label = r["display_name"] or r["model_label"] or r["model_code"]
        reasons: list[str] = []
        if r["status"] == "disabled":
            reasons.append("已禁用")
        elif r["status"] == "unregistered":
            reasons.append("清单未登记这一档，参数不猜")
        elif r["status"] in ("dependency_missing", "connection_failed"):
            reasons.append(f"{STATUS_LABEL.get(r['status'], r['status'])}：{r['health']['detail']}")
        elif r["status"] in ("not_installed", "unchecked"):
            reasons.append(STATUS_LABEL.get(r["status"], r["status"]))
        # unverified 是允许的：没验过不等于不能用，界面会带着"未验证"这枚徽章进候选
        have = {t["tag"] for t in r["tags"]}
        missing = [t for t in required if t not in have]
        if missing:
            reasons.append("缺能力 " + "、".join(missing))
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
    if not out["candidates"]:
        out["blockers"].append(
            f"场景「{row['scene_label']}」没有可用候选：这一类能力里"
            f"没有一档同时满足 {'、'.join(required) or '（无必需能力）'}")
    return out
