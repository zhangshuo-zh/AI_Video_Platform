"""模型中心：登记、引擎视图、任务调用策略、场景候选。

一句话职责边界：**这一层只管"模型与引擎的配置与真相"，不管选路。**
选路仍然只有 `services.resolve_provider()` 一处，默认轨仍然只有
`provider_config.is_default` 一个来源（裁定 MC1＝备用方案只提示不自动切）。
所以本文件里没有任何接口会被提交路径自动采用，`/model-policies/resolve`
返回的候选只是给人看的，返回值里也明写 `auto_switch: false`。

权限全部沿用 `system:config`（18 个权限点之一，不新增）。只有
`/model-policies/resolve/{scene}` 是登录即可——它和 `/providers/available` 一样是
"业务页面上的候选池"，不含密钥变量名、不含备注、不含本机路径之外的敏感信息。
"""
import json

from fastapi import APIRouter, Depends, HTTPException

import adapters
import model_center as mc
import validations
from deps import require_permission, require_user
from models import (BACKENDS, CAPABILITIES, ModelPolicyIn, ModelRegistrationIn,
                    ModelRegistrationPatch, POLICY_PREFERENCES, ValidationOut)
from repositories.model_center import RegistrationError, model_center

router = APIRouter(prefix="/api", tags=["model-center"])

# provider_config（0003）与 model_validation（0021）的 CHECK 都只认 local/cloud。
# remote 是登记层新加的运行位置，本轮不接验证：想留痕得先扩验证表，那是独立改动。
_VERIFIABLE_BACKENDS = ("local", "cloud")


def _json_list(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _row_or_404(reg_id: int):
    row = model_center.registration(reg_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"没有这条登记：#{reg_id}")
    return row


def _item_of(reg_id: int) -> dict:
    """改完之后回这一档的**联表视图**（而不是库里的裸行）：
    界面拿到的和列表页是同一份形状，不用自己再拼一次健康与标签。"""
    for item in mc.catalog():
        if item.get("registration_id") == reg_id:
            return item
    return {}


@router.get("/models")
def list_models(_=Depends(require_permission("system:config"))) -> dict:
    """模型仓库与引擎管理的唯一数据源：清单 × 登记 的联表。

    清单里有但没登记的档也在结果里（status=not_registered）——
    少了这一段，"装了不等于配了"就看不见。
    """
    return {"manifest_version": adapters.MANIFEST_VERSION,
            "status_labels": mc.STATUS_LABEL,
            "capabilities": CAPABILITIES, "backends": BACKENDS,
            "unintegrated_engines": mc.UNINTEGRATED_ENGINES,
            "items": mc.catalog()}


@router.get("/models/stats")
def model_stats(_=Depends(require_permission("system:config"))) -> dict:
    """顶部四数。口径由后端一处给（caliber 字段），界面只念不算。"""
    return mc.stats()


@router.post("/models", status_code=201)
def create_model(payload: ModelRegistrationIn,
                 user=Depends(require_permission("system:config"))) -> dict:
    capability = payload.capability.strip()
    backend = payload.backend.strip()
    provider = payload.provider.strip()
    model_code = payload.model_code.strip()
    if capability not in CAPABILITIES:
        raise HTTPException(status_code=422,
                            detail=f"能力只能是 {'、'.join(CAPABILITIES)}，不是「{capability}」")
    if backend not in ("local", "cloud", "remote"):
        raise HTTPException(status_code=422, detail="运行位置只能是 本地／云端／远程自建")
    if not provider or not model_code:
        raise HTTPException(status_code=422, detail="provider 与模型标识都不能空")

    # remote 在清单里没有对应轴（清单按 local/cloud 登记），比的时候折回 local，
    # 这样"远程自建但用的是同一套 wan 参数"能认出来；认不出来就走下面的警告。
    adapter = adapters.find(capability, "local" if backend == "remote" else backend, provider)

    try:
        row = model_center.create({
            "capability": capability, "backend": backend, "provider": provider,
            "model_code": model_code, "display_name": payload.display_name,
            "source": payload.source if payload.source in ("local", "cloud", "remote") else backend,
            "engine_code": payload.engine_code or (adapter.code if adapter else provider),
            "version": payload.version, "artifact_path": payload.artifact_path,
            "remote_model_id": payload.remote_model_id,
            "capabilities": payload.capabilities, "enabled": payload.enabled,
            "note": payload.note, "created_by": user["id"],
        })
    except RegistrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"id": row["id"], "item": _item_of(row["id"]),
            "in_manifest": adapter is not None,
            "warning": "" if adapter is not None else
            "清单里没有这个 provider：登记留下了，但参数不猜、运行方式不猜，提交侧会被拒"}


@router.patch("/models/{reg_id}")
def patch_model(reg_id: int, payload: ModelRegistrationPatch,
                _=Depends(require_permission("system:config"))) -> dict:
    _row_or_404(reg_id)
    patch = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "source" in patch and patch["source"] not in ("local", "cloud", "remote"):
        raise HTTPException(status_code=422, detail="运行位置只能是 本地／云端／远程自建")
    try:
        model_center.update(reg_id, patch)
    except RegistrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"item": _item_of(reg_id)}


@router.delete("/models/{reg_id}")
def remove_model(reg_id: int, confirm: str = "",
                 _=Depends(require_permission("system:config"))) -> dict:
    """撤登记。**不删权重文件、不动历史作业、不清验证记录**。

    要 confirm 是因为人常把"移除登记"理解成"删掉模型"；
    提示语里必须把"文件还在盘上"说明白，否则下次找权重就找不着了。
    """
    row = _row_or_404(reg_id)
    if confirm != "撤登记":
        raise HTTPException(
            status_code=422,
            detail="要撤登记请带 confirm=撤登记。这一步只删除这条登记："
                   "权重文件仍在盘上，历史作业与验证记录一律不动。")
    model_center.delete(row["id"])
    return {"removed": row["id"],
            "note": "只撤了登记；权重文件、job 历史与 model_validation 记录都还在"}


@router.post("/models/{reg_id}/check", response_model=ValidationOut)
def check_model(reg_id: int, user=Depends(require_permission("system:config"))):
    """对这一档跑一次环境检查并留痕（复用 `validations.env_check`）。

    这一枪不发请求、不加载权重，所以它永远不能把状态推到"可用"——
    可用要求 real_call 或 artifact_review，那是另一回事。
    """
    row = _row_or_404(reg_id)
    if row["backend"] not in _VERIFIABLE_BACKENDS:
        raise HTTPException(
            status_code=422,
            detail="远程自建服务本轮不接验证：0021 的 model_validation 只认 local/cloud。"
                   "要接它得先扩验证表，那是一个独立改动，不在模型中心这一轮。")
    try:
        result = validations.env_check(mc.health_row(dict(row)), user["id"])
    except validations.ValidationInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ValidationOut.from_row(result["validation"])


@router.get("/model-policies")
def list_policies(_=Depends(require_permission("system:config"))) -> dict:
    items = []
    for row in model_center.policies():
        d = dict(row)
        items.append({
            "scene_key": d["scene_key"], "scene_label": d["scene_label"],
            "capability": d["capability"],
            "capability_label": CAPABILITIES.get(d["capability"], d["capability"]),
            "required": _json_list(d["required_json"]),
            "slot_provider": d["slot_provider"], "slot_model": d["slot_model"],
            "preference": d["preference"],
            "preference_label": POLICY_PREFERENCES.get(d["preference"], d["preference"]),
            "fallback_provider": d["fallback_provider"], "fallback_model": d["fallback_model"],
            "resource_limit": validations.json_loads(d["resource_limit_json"]),
            "cloud_confirm_required": bool(d["cloud_confirm_required"]),
            "enabled": bool(d["enabled"]), "updated_at": d["updated_at"],
        })
    return {"manifest_version": adapters.MANIFEST_VERSION,
            # 这一句和 adapters.manifest_payload().policy.no_auto_fallback 同一口径，
            # 界面拿它决定"备用轨"那一栏要不要写成"可改选"而不是"自动切"
            "no_auto_fallback": True,
            "preference_labels": POLICY_PREFERENCES,
            "items": items}


@router.put("/model-policies")
def save_policies(payload: list[ModelPolicyIn],
                  user=Depends(require_permission("system:config"))) -> dict:
    """整批保存策略。只改配置，**不启动任何任务**（任务书 §七 规则 8）。

    校验一次说清所有问题，不修一条报一条：场景不认识、能力不认识、偏好不认识、
    建议轨不在清单、备用轨与建议轨同档、要启用却没选建议轨。
    """
    known = {row["scene_key"] for row in model_center.policies()}
    errors: list[str] = []
    for item in payload:
        if item.scene_key not in known:
            errors.append(f"没有这个任务场景：{item.scene_key}"
                          "（场景名定死在 0035 的种子，界面不提供自由输入）")
        if item.capability not in CAPABILITIES:
            errors.append(f"{item.scene_key}：能力不认识")
        if item.preference not in POLICY_PREFERENCES:
            errors.append(f"{item.scene_key}：偏好只能是 本地／云端／远程自建／不限")
        if item.slot_provider and not _in_manifest(item.capability, item.slot_provider):
            errors.append(f"{item.scene_key}：建议轨「{item.slot_provider}」不在清单里，"
                          "平台不会替它猜参数")
        if item.slot_provider and item.fallback_provider == item.slot_provider \
                and item.fallback_model == item.slot_model:
            errors.append(f"{item.scene_key}：备用轨和建议轨是同一档，等于没有备用")
        if item.enabled and not item.slot_provider:
            errors.append(f"{item.scene_key}：要启用就得选一条建议轨，不能空着启用")
    if errors:
        raise HTTPException(status_code=422, detail="；".join(errors))

    n = model_center.save_policies([
        {**item.model_dump(), "updated_by": user["id"]} for item in payload])
    return {"saved": n,
            "note": "策略只改配置：默认轨的唯一来源仍是 provider_config.is_default，"
                    "备用轨不会自动顶上"}


@router.get("/model-policies/resolve/{scene_key}")
def resolve(scene_key: str, _=Depends(require_user)) -> dict:
    """场景 → 候选与挡路原因。只读，登录即可（与 /providers/available 同一档）。"""
    out = mc.resolve_scene(scene_key)
    out["auto_switch"] = False
    return out


def _in_manifest(capability: str, provider: str) -> bool:
    """建议轨必须能在清单里找到（local 或 cloud 任一轴）。"""
    return any(adapters.find(capability, backend, provider) is not None
               for backend in ("local", "cloud"))
