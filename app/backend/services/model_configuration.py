"""模型中心的目录、策略与引擎响应业务组装，不依赖 HTTP 框架。"""
from typing import Any
from model_center_pkg.labels import parse_required, PolicyDataInvalid

import adapters
import engine_launch
import engines
import model_center as mc
import validations
from models import CAPABILITIES, BACKENDS, REG_SOURCES, POLICY_PREFERENCES, ModelPolicyIn
from repositories.model_center import model_center
from repositories.engine_instances import (
    engine_instances, engine_probe_log, SUPPORTED_CONNECTION_TYPES, CONNECTION_TYPES)
from repositories.config_log import record as config_log_record


class ModelOperationError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ModelPolicyInvalid(ModelOperationError):
    pass

def _json_list(raw: object) -> list[str]:
    try:
        return parse_required(raw)
    except PolicyDataInvalid as exc:
        raise ModelOperationError(500, str(exc)) from exc


def _item_of(reg_id: int) -> dict:
    """改完之后回这一档的**联表视图**（而不是库里的裸行）：
    界面拿到的和列表页是同一份形状，不用自己再拼一次健康与标签。

    2026-11-07 审查 P2-6：写操作一次只改一条登记，回读却把**整张目录**重算
    一遍（每个服务型引擎一行探测、几十趟回库）——写路径不该为回读一句话
    付整页的钱。现在只算目标那一条：先查登记行，再按同一套拼行函数出单行。
    """
    reg = model_center.registration(reg_id)
    if reg is None:
        return {}
    item = mc.item_for(dict(reg))
    return item or {}


def _tier_problem(capability: str, provider: str, model_code: str) -> str:
    """这一档能不能被写进策略。空串代表没问题，否则返回一句给人看的话。

    三种毛病分开说，因为它们要改的地方不一样：
      * provider 不在清单 —— 得先写适配器（规格住代码）；
      * 清单只登记了名字、没有模型档 —— 这一档的参数谁都不许猜，存进策略等于
        给一条永远解析不出参数的轨；
      * 模型标识不是这一档清单里的 —— 手滑的拼写在这里就挡住，
        不要等到提交作业时才说"候选为空"。
    """
    adapter = next((a for backend in ("local", "cloud")
                    if (a := adapters.find(capability, backend, provider)) is not None), None)
    if adapter is None:
        return f"「{provider}」不在清单里，平台不会替它猜参数"
    if not adapter.models:
        return (f"「{provider}」在清单里只登记了名字、没有模型档："
                "参数没核过，不能写进策略")
    if model_code and not any(m.code == model_code for m in adapter.models):
        known = "、".join(m.code for m in adapter.models)
        return f"「{provider}／{model_code}」不在清单里（这一档有：{known}）"
    return ""


def _age_seconds(stamp: str) -> int | None:
    """SQLite 里的 `datetime(\'now\',\'localtime\')` 与此刻差多少秒。

    界面要的是"N 秒前"这个数，而现算它得知道库里的时间戳是本地时间还是 UTC。
    判在这一侧只有一处，界面只管把数字交给 `agoText`——与 worker 心跳那一列
    同一口径（那边也是服务端算 age）。解析不出来就回 None，界面会省略那一段，
    不拿 0 冒充"刚刚"。
    """
    try:
        from datetime import datetime

        then = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
        return max(0, int((datetime.now() - then).total_seconds()))
    except (TypeError, ValueError):
        return None


def _last_probe_fields(row) -> dict:
    """列表里"上次探测是什么时候、结果如何"那几格。

    没探过就回空串而不是"从未"这种假值：界面据空串决定要不要显示那一小块。
    历史**永远不参与判断**——它只回答"上次探过没有、当时如何"，
    "现在是好的吗"仍只由 `engines.probe()` 现查。
    """
    if row is None:
        return {"last_probe_at": "", "last_probe_state": "",
                "last_probe_detail": "", "last_probe_source": "",
                "last_probe_latency_ms": None}
    return {"last_probe_at": row["created_at"],
            # 年龄由服务端按库里的时间算，不在界面里拿"现在减一个本地时间字符串"——
            # 那个减法会踩时区与格式两个坑，而全站相对时间的定义只有 `agoText` 一处。
            "last_probe_age_seconds": _age_seconds(row["created_at"]),
            "last_probe_state": row["state"],
            "last_probe_detail": row["detail"],
            "last_probe_source": row["source"],
            "last_probe_latency_ms": row["latency_ms"]}


def _adapter_for(code: str):
    """按 code 找 adapter（实例只认 code，不认能力轴）。找到哪个算哪个。"""
    for capability in CAPABILITIES:
        for backend in ("local", "cloud"):
            adapter = adapters.find(capability, backend, code)
            if adapter is not None:
                return adapter
    return None


def catalog_payload() -> dict:
    """模型仓库与引擎管理的唯一数据源：清单 × 登记 的联表。

    清单里有但没登记的档也在结果里（status=not_registered）——
    少了这一段，"装了不等于配了"就看不见。

    2026-11-07 改：`stats` 随这一次响应一起给，且**用的就是下面这同一份 rows**。
    原来界面先 `GET /api/models` 再 `GET /api/models/stats`，两次都要整张目录
    （每行还要探一次引擎），等于把同一件事做两遍；两处各算各的，口径也迟早会漂。
    `GET /api/models/stats` 留着——命令行与浏览器验收脚本按老地址取数，不该断。
    """
    rows = mc.catalog()
    return {"manifest_version": adapters.MANIFEST_VERSION,
            "status_labels": mc.STATUS_LABEL,
            "capabilities": CAPABILITIES, "backends": BACKENDS,
            # backends 是**选型层**的取值（provider_config 只有本地／云端两轨），
            # reg_sources 是**登记层**的取值（0035 多了一档远程自建）。两把放一起给，
            # 界面就不必再自己硬写第三个 option——原来 models.js 硬写过一份，
            # 标签还和后端不一样（"远程自建服务" vs "远程自建"）。
            "reg_sources": REG_SOURCES,
            "unintegrated_engines": mc.UNINTEGRATED_ENGINES,
            "items": rows,
            "stats": mc.stats(rows)}


def policies_payload() -> dict:
    items = []
    # 每条能力轴上"清单有可能产出哪些标签"只算一次：它是清单的纯函数，
    # 与这一页有几行、谁配了什么无关。放在循环里算就是九遍同样的遍历。
    producible = {cap: mc.producible_tags(cap) for cap in CAPABILITIES}
    for row in model_center.policies():
        d = dict(row)
        required = _json_list(d["required_json"])
        # 「配不出来」与「还没配」必须在同一行里就能分开：前者要改 adapters.py，
        # 后者只是去登记、去点探测。少了这两个字段，界面只能给一句"待配置"，
        # 而其中有些场景是永远配不出来的（2026-11-07 审查里 4 个）。
        impossible = [t for t in required if t not in producible[d["capability"]]]
        items.append({
            "scene_key": d["scene_key"], "scene_label": d["scene_label"],
            "capability": d["capability"],
            "capability_label": CAPABILITIES.get(d["capability"], d["capability"]),
            "required": required,
            "impossible_required": impossible,
            "required_producible": not impossible,
            # 这一轴的标签全集：必需能力那一格的勾选项从这里出——
            # 界面不自己发明第四份标签名单，与派生标签严格同源。
            "producible": sorted(producible[d["capability"]]),
            "slot_provider": d["slot_provider"], "slot_model": d["slot_model"],
            "preference": d["preference"],
            "preference_label": POLICY_PREFERENCES.get(d["preference"], d["preference"]),
            "fallback_provider": d["fallback_provider"], "fallback_model": d["fallback_model"],
            "resource_limit": validations.json_loads(d["resource_limit_json"]),
            "cloud_confirm_required": bool(d["cloud_confirm_required"]),
            "enabled": bool(d["enabled"]), "updated_at": d["updated_at"],
        })
    return {"manifest_version": adapters.MANIFEST_VERSION,
            # 这一句从 adapters.POLICY 读，不在这里重抄字面量：清单里那条
            # `policy.no_auto_fallback` 才是权威，界面拿它决定"备用轨"那一栏
            # 要不要写成"可改选"而不是"自动切"。抄一份就等于埋第二把会漂移的口径。
            "no_auto_fallback": adapters.POLICY["no_auto_fallback"],
            "preference_labels": POLICY_PREFERENCES,
            "items": items}


def scenes_payload() -> dict[str, Any]:
    """登录可读的场景骨架；建议轨和启用状态由 resolve 返回最小提交提示。

    管理员权限保护配置写入与完整策略，普通用户可以读取预填提示。
    前端预填不改变后端选路，云端仍须提交时显式确认计费。
    """
    # 每条能力轴上"清单有可能产出哪些标签"只算一次：它是清单的纯函数，
    # 与这一页有几行、谁配了什么无关。放在循环里算就是九遍同样的遍历。
    producible = {cap: mc.producible_tags(cap) for cap in CAPABILITIES}
    items = []
    for row in model_center.policies():
        required = _json_list(row["required_json"])
        impossible = [t for t in required if t not in producible.get(row["capability"], set())]
        items.append({
            "scene_key": row["scene_key"],
            "scene_label": row["scene_label"],
            "capability": row["capability"],
            "capability_label": CAPABILITIES.get(row["capability"], row["capability"]),
            "required": required,
            # 「配不出来」与「还没配」分开说：前者要改 adapters.py，点界面没用。
            # 判据来自清单（producible_tags），界面不自己推。
            "impossible_required": impossible,
            "required_producible": not impossible,
        })
    return {"items": items,
            "manifest_version": adapters.MANIFEST_VERSION,
            "note": "只回场景骨架与'这一轴产不产得出必需能力'；"
                    "候选与挡路原因走 /model-policies/resolve/{scene_key}。"}


def engines_payload() -> dict:
    """引擎实例列表，只读取已有探测结果，缓存过期后提示重新检查。

    探测必须**带上这一行自己的端点**：`engines.probe(code)` 只认
    `engine_instance.pick()` 选出来的那一台，于是十台实例会集体显示
    第一台的体温——一台连不上就全体"连不上"，一台好好的就全体"就绪"。
    这个坑在 W-B 的单实例时代看不出来，第二台一进来就变谎话。
    """
    items = []
    # 上次探测留痕一次读齐（0042）。逐行各查一次就是 N 趟回库，
    # 与 `latest_matrix()` 同一个理由。**只读不写**：读路径不做副作用。
    last_probe = engine_probe_log.latest_by_instance()
    for row in engine_instances.instances():
        adapter = _adapter_for(row["engine_code"])
        # 连接方式不是 HTTP 的那几行**不探测**：`engines.probe()` 只会按 HTTP 打端点，
        # 打出来的结论不是这一行配的那个东西的结论。原来这里不判断，于是库里若有
        # 一行 `grpc`，界面会拿一次 HTTP 的失败说"连不上"——那是在替一个没实现的东西
        # 编结论。现在如实说"这一档连接方式还没有探测分支"。
        if row["connection_type"] not in SUPPORTED_CONNECTION_TYPES:
            items.append({
                "id": row["id"], "engine_code": row["engine_code"],
                "engine_label": adapter.label if adapter else "",
                "display_name": row["display_name"],
                "connection_type": row["connection_type"],
                "endpoint": row["endpoint"],
                "shared_secret_env": row["shared_secret_env"],
                "secret_present": bool(adapters.key_present(row["shared_secret_env"])),
                "enabled": bool(row["enabled"]), "note": row["note"],
                "health_state": "unknown",
                "health_detail": ("连接方式是「" + row["connection_type"]
                                  + "」，这一档还没有探测分支：平台不拿一次 HTTP 的结论冒充它"),
                "engine_model": "", "probe_unsupported": True,
                **_last_probe_fields(last_probe.get(row["id"])),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            })
            continue
        # 读列表只取已有结果，缺失或过期均由显式检查入口补查。
        cached = engines.peek(row["engine_code"], row["endpoint"]) if row["enabled"] else None
        if cached is None:
            status = engines.EngineStatus(
                "unknown", "尚未检查，请点击环境检查" if row["enabled"] else "已停用，未探测", {})
        else:
            previous, age = cached
            status = engines.EngineStatus(
                previous.state if age <= engines.PROBE_TTL_SECONDS else "unknown",
                f"{previous.detail}（{age:.0f} 秒前检查）"
                + ("，请重新检查" if age > engines.PROBE_TTL_SECONDS else ""),
                previous.values)
        recipe = engine_launch.recipe_for(row["engine_code"])
        items.append({
            "id": row["id"], "engine_code": row["engine_code"],
            "engine_label": adapter.label if adapter else "",
            "display_name": row["display_name"],
            "connection_type": row["connection_type"],
            "endpoint": row["endpoint"],
            "shared_secret_env": row["shared_secret_env"],
            "secret_present": bool(adapters.key_present(row["shared_secret_env"])),
            "enabled": bool(row["enabled"]), "note": row["note"],
            # 停用中的实例不探测：探测它只会让界面多一条"连不上"的噪声，
            # 而人已经告诉我们这台不用了。
            "health_state": status.state if row["enabled"] else "unknown",
            "health_detail": status.detail if row["enabled"] else "已停用，未探测",
            "engine_model": status.values.get("model", "") if row["enabled"] else "",
            **_last_probe_fields(last_probe.get(row["id"])),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            # 「启动/关闭服务」控制按钮字段（2026-11-14 升级）
            "can_control": bool(
                row["connection_type"] in SUPPORTED_CONNECTION_TYPES
                and recipe is not None),
            "is_running": bool(status.state == "ok"),
            "launch_ready": bool(
                row["connection_type"] in SUPPORTED_CONNECTION_TYPES
                and row["enabled"]
                and recipe is not None
                and status.state != "ok"),
            "launch_note": (recipe or {}).get("note", ""),
        })
    # 下发的是**能选的那一把**（SUPPORTED），不是 CHECK 认的全集：界面不该列出
    # 一个选进来看不到任何行为的选项（CR-010）。CHECK 全集另给一个键，
    # 让"库里有历史行用了别的值"这件事在界面上说得清。
    return {"items": items,
            "connection_types": list(SUPPORTED_CONNECTION_TYPES),
            "declared_connection_types": list(CONNECTION_TYPES),
            "service_engines": sorted(engines.SERVICE_ENGINES)}


def save_policies(payload: list[ModelPolicyIn], user_id: int) -> dict[str, Any]:
    """整批保存策略。只改配置，**不启动任何任务**（任务书 §七 规则 8）。

    校验一次说清所有问题，不修一条报一条：场景不认识、能力不认识、偏好不认识、
    建议轨不在清单、备用轨与建议轨同档、要启用却没选建议轨。

    建议轨与备用轨都**连模型档一起核**：原来只核 provider 在不在清单里，
    于是 `mflux/不存在的模型` 能存进去，等到提交作业时才在 `_resolve` 里
    变成一句挡路原因——那时人已经不记得是自己在策略页填的。
    备用轨原来是完全不核（只比它跟建议轨不相等），一个手滑的拼写会一直躺在库里。

    必需能力**认标签全集**（`producible_tags`）：界面从只读 chip 放开成可编辑后
    （《使用说明书》§5.4 本来就教人"把这一项从「必需」降下来"），手滑拼错或
    造一个清单里根本没有的标签，会把这个场景推到"永远 0 候选"还不报错。
    已存在于同一场景、同一能力轴的缺来源要求可以原样保存或显式移除；
    新增要求仍须来自可产出全集。
    """
    payload = [item.model_copy(update={key: getattr(item, key).strip() for key in
               ("scene_key", "slot_provider", "slot_model", "fallback_provider", "fallback_model")})
               for item in payload]
    known = {row["scene_key"]: dict(row) for row in model_center.policies()}
    producible = {cap: mc.producible_tags(cap) for cap in CAPABILITIES}
    errors: list[str] = []
    seen: set[str] = set()
    for item in payload:
        if item.scene_key in seen:
            errors.append(f"{item.scene_key}：同批任务场景重复")
        seen.add(item.scene_key)
        if item.scene_key not in known:
            errors.append(f"没有这个任务场景：{item.scene_key}"
                          "（场景名定死在 0035 的种子，界面不提供自由输入）")
        if item.capability not in CAPABILITIES:
            errors.append(f"{item.scene_key}：能力不认识")
        existing = known.get(item.scene_key)
        retained = (
            set(_retained_required(existing["required_json"]))
            if existing and existing["capability"] == item.capability else set()
        )
        allowed_tags = producible.get(item.capability, set()) | retained
        unknown_tags = [t for t in item.required if t not in allowed_tags]
        if unknown_tags:
            errors.append(f"{item.scene_key}：必需能力 "
                          + "、".join(unknown_tags)
                          + f" 不在{CAPABILITIES.get(item.capability, item.capability)}"
                            "这一轴的标签全集里（能选的与清单派生的同一份）")
        if item.preference not in POLICY_PREFERENCES:
            errors.append(f"{item.scene_key}：偏好只能是 本地／云端／远程自建／不限")
        if item.slot_provider:
            problem = _tier_problem(item.capability, item.slot_provider, item.slot_model)
            if problem:
                errors.append(f"{item.scene_key}：建议轨{problem}")
        if item.slot_provider and item.fallback_provider == item.slot_provider \
                and item.fallback_model == item.slot_model:
            errors.append(f"{item.scene_key}：备用轨和建议轨是同一档，等于没有备用")
        if item.fallback_provider:
            problem = _tier_problem(item.capability, item.fallback_provider,
                                    item.fallback_model)
            if problem:
                errors.append(f"{item.scene_key}：备用轨{problem}")
        if item.enabled and not (item.slot_provider and item.slot_model):
            errors.append(f"{item.scene_key}：要启用就得选一条建议轨，provider 和模型都不能为空")
    if errors:
        raise ModelPolicyInvalid(status_code=422, detail="；".join(errors))

    n = model_center.save_policies([
        {**item.model_dump(), "updated_by": user_id} for item in payload])
    # 保存成功后记录实际变化，包括停用、清空与仅调整能力要求。
    for item in payload:
        before = known[item.scene_key]
        after = item.model_dump()
        if not after["scene_label"]:
            after["scene_label"] = before["scene_label"]
        previous = {key: before[key] for key in after
                    if key not in {"required", "resource_limit", "enabled", "cloud_confirm_required"}}
        previous.update(required=_retained_required(before["required_json"]),
                        resource_limit=validations.json_loads(before["resource_limit_json"]),
                        enabled=bool(before["enabled"]),
                        cloud_confirm_required=bool(before["cloud_confirm_required"]))
        if previous == after:
            continue
        config_log_record(
            "model_policy", item.scene_key, "update",
            f"{'启用' if item.enabled else '停用'}｜建议轨 {item.slot_provider or '—'}/{item.slot_model or '—'}",
            {"before": previous, "after": after}, created_by=user_id)
    return {"saved": n,
            "note": "策略只改配置：默认轨的唯一来源仍是 provider_config.is_default，"
                    "备用轨不会自动顶上"}


def _retained_required(raw: object) -> list[str]:
    # 损坏的旧要求不提供保留豁免，但允许管理员用明确的新列表修复。
    try:
        return parse_required(raw)
    except PolicyDataInvalid:
        return []
