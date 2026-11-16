"""模型中心：catalog_view 业务域。"""
from __future__ import annotations
import adapters
import engines
from repositories.model_center import model_center
from .labels import _tags_of
from .slots import _find_adapter, _health_of, _manifest_axis, _slot_current_model, _status_detail_of, _status_of, health_row
from .verification import _kinds_of, _verification_index, verified_kinds


# 未接入引擎：本机装了应用、平台里一行适配代码都没有。
# 写死在这里而不是编进 ADAPTERS，是为了不让它意外走进 health()/runners.build()——
# 一个假的 adapter 会让"选了就能跑"变成谎言。
UNINTEGRATED_ENGINES: list[dict] = [
    # ComfyUI 于 2026-11-13 从这张名单里摘出去了（清单 `comfyui`）：
    # 它现在有适配器壳与自述端点，引擎页能看见它通不通。
    # **但"未接入"这个词对它仍然半真**——接的是探测，不是执行。
    # 所以它出现在清单里（`_engine_only_row`，models 为空那一档），
    # 由 `adapters._COMFYUI` 的 note 与 `engines._describe()` 的结语
    # 把"只接探测、没有提交入口"说两遍（列表页与探测结论各一次）。
    # 这里留空数组而不是删掉常量：`stats()` 与前端都读这个键，
    # 删了会变成"少一个字段"而不是"没有未接入引擎"。
    # Draw Things 在 2026-11-06 的 W-B 里接进来了（清单 `draw_things`、0040 实例表、
    # `engine_http_run.py` 真出图跑通），所以从这张名单里摘掉——再留着就是一份
    # 会过期的第二真相：同一件事在清单里说"有这一档引擎"，这里说"未接入"。
    # 它的模型目录是否就绪仍然不可复核（TCC），那句话写在《00-接入前现状审计》里。
]



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
    # 0043 起验证矩阵按档分区，键就是四元组——不再需要"投影成三元组"
    # 再在 `_slot_model_matches` 里比一次 model：查询结果只含这一档的记录。
    # 0045 起**槽位的 backend 用登记行自己的值**（remote 也照常取），
    # 不用 `reg_index` 那把折回后的键：折回只用于查清单的参数与计价，
    # 验证记录记在登记行自己名下。两条轴混在一起就会拿本地记录替远端背书。
    slots = {(r["capability"], r["backend"], r["provider"], r["model_code"])
             for r in regs}
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
        orphan_spec: dict | None = None
        if adapter is not None:
            orphan_spec = next((m.as_dict() for m in adapter.models
                                if m.code == reg["model_code"]), None)
        if orphan_spec is not None:
            rows.append(_registered_row(adapter, orphan_spec, reg, index, probe=probe))
        else:
            rows.append(_orphan_row(reg, probe=probe, verification=index))
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
    # 0045：验证矩阵的槽位用**登记行自己的 backend**（与 catalog() 同一口径）。
    # `_manifest_axis()` 那把折回只用于查 adapter 与它的参数、计价。
    index = _verification_index({(cap, reg["backend"], provider, code)})
    adapter = _find_adapter(cap, reg["backend"], provider)
    spec = None
    if adapter is not None:
        spec = next((m.as_dict() for m in adapter.models if m.code == code), None)
    if spec is not None:
        return _registered_row(adapter, spec, dict(reg), index)
    # 清单里没有这一档：与 catalog() 同一判定——孤儿行，不是"清单未登记"
    return _orphan_row(dict(reg), verification=index)



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
    # 0045：验证记录按**登记行的 backend** 取，remote 也照常取。
    # 0045 之前这里对 remote 硬置空集，理由是"0021 的 CHECK 只认 local/cloud，
    # 按 provider×backend 取会拿本地同 provider 的记录错认成它的"。
    # 现在表认 remote 了，那个错认不存在——按 (provider, backend='remote') 取的
    # 就是它自己的记录。`adapter.backend` 是**折回后**的清单轴（remote→local），
    # 拿它去查会把两条轴混起来，所以这里显式用 `reg["backend"]`。
    backend = reg["backend"]
    if verification is None:
        kinds = verified_kinds(adapter.capability, backend, adapter.code,
                               reg["model_code"])
    else:
        kinds = _kinds_of(verification, adapter.capability, backend,
                          adapter.code, reg["model_code"])
    # 0043：验证矩阵按档分区，`kinds` 已经只含这一档自己的记录——
    # "换档不背书"是查询的形状，不再需要 `_slot_model_matches` 在读点上补刀。
    # 这里仍要回答"这一档是不是槽位当前那一档"（status_detail 要点名解释），
    # 所以查一次槽位拿当前 model，但只用于文案，不参与 kinds 的判定。
    slot_model = _slot_current_model(adapter)
    # 「这一档是不是槽位当前那一档」只对**选型层管得着的两档**成立（local／cloud）——
    # `provider_config` 是十行固定（能力×本地／云端），里面没有 remote 这一档。
    # 对 remote 要求"它是槽位当前那一档"等于要求它永不为真：槽位里根本不会有它。
    # 0045 起 remote 走"有真调用／产物验收凭据就算数"，与其它两档同一把尺子，
    # 只是不参与槽位比对。
    same_model = (reg["backend"] == "remote") or (slot_model == reg["model_code"])
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



def _orphan_row(reg, *, probe: bool = False, verification: dict | None = None) -> dict:
    """登记了、清单里却没有这一档。参数与计价一律不猜，界面必须标出来。

    **验证痕迹照认**（0045）：这一档在清单里没有规格，不等于它的验证记录不存在——
    `model_validation` 记录的是"这一档自己跑过真作业"，与清单里有没有它的参数表
    是两件事。原来这里恒回空集（`kind` 也是 `orphan`），于是 remote 自建这类
    清单外登记永远显示"一条验证记录都没有"，与 §4.5 的说法自相矛盾。
    清单仍然管参数与计价：`params`／`prices` 一律空，这条不变。
    """
    adapter = _find_adapter(reg["capability"], reg["backend"], reg["provider"])
    if verification is None:
        kinds = verified_kinds(reg["capability"], reg["backend"], reg["provider"],
                               reg["model_code"])
    else:
        kinds = _kinds_of(verification, reg["capability"], reg["backend"],
                          reg["provider"], reg["model_code"])
    spec: dict = {"image_inputs": [], "params": [], "prices": []}
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
        "tags": _tags_of(reg["capability"], spec, reg, kinds),
        "health": adapters.health(health_row(reg), probe_service=probe,
                                 probe_timeout=engines.LIST_TIMEOUT_SECONDS),
        "validation_kinds": sorted(kinds),
        "status": "registration_only",
        "status_detail": "清单里没有这个 provider／这一档：参数不猜、运行方式不猜，"
                         "提交侧会被拒"
                         + ("（已有验证记录，但清单缺规格这件事不变）" if kinds else ""),
        "created_at": reg["created_at"], "updated_at": reg["updated_at"],
    }



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
