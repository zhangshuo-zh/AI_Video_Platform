"""模型配置：五类能力 × 本地/云端 两轨。

PUT 是整批覆盖而不是逐行改：配置页一次保存十行，
整批提交能在一个事务里校验"每类能力恰好一个默认"，不会出现中间态。

W06 起这一页还负责两件规格里点名的事：密钥变量名（只存名字）与备注；
允许参数和健康状态不落库，前者来自 adapter 清单、后者是打开页面时现查的。

P0 又加了一件事：**把"环境齐"和"真跑过"分开显示**。健康状态每次现查，
但"这一槽位最近一次真实调用是什么时候、结果如何"是历史事实，落在
`model_validation` 表里——它不能跟着配置一起被整批覆盖掉。
"""
import re

from fastapi import APIRouter, Depends, HTTPException

import adapters
import validations
from deps import require_permission, require_user
from models import (BACKENDS, CAPABILITIES, CurrentProviderOut, ProviderOptionOut,
                    ProviderOut, ProviderUpdate, ValidationIn, ValidationOut)
from repositories.providers import has_key, providers
from repositories.validation import validations as validation_repo

router = APIRouter(prefix="/api", tags=["settings"])

# 变量名只允许环境变量的合法写法；它不是值，但拼错会让"已配置"永远亮不起来，
# 所以宁可在保存时挡掉，也不让人对着一个不存在的名字猜。
KEY_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROVIDER_RE = re.compile(r"^[A-Za-z0-9._-]*$")


def _row_out(row) -> ProviderOut:
    health = adapters.health(row)
    env = health["key_env"] if row["backend"] == "cloud" else ""
    summary = validations.summary_for(row)
    return ProviderOut.from_row(row, has_key(row, env), env, health, summary)


@router.get("/providers", response_model=list[ProviderOut])
def list_providers(_=Depends(require_permission("system:config"))) -> list[ProviderOut]:
    return [_row_out(row) for row in providers.all()]


@router.get("/providers/manifest")
def provider_manifest(_=Depends(require_permission("system:config"))) -> dict:
    """能力清单：每个 adapter 支持哪些档、每档允许哪些参数、出处是哪。

    界面只按这份清单渲染；清单里没有的 provider 一律不显示参数，
    免得"模型名听着像"就成了一个假承诺。
    """
    return adapters.manifest_payload()


@router.get("/providers/available", response_model=list[ProviderOptionOut])
def available_providers(_=Depends(require_user)) -> list[ProviderOptionOut]:
    """提交作业时可选的槽位：只列已启用且填了 provider/模型的。

    参数白名单跟着槽位一起回：界面按它渲染输入框，提交时后端再验一遍。
    这里不含 key_env、notes 与本地运行时目录——那些是系统配置那一页的事。
    """
    out: list[ProviderOptionOut] = []
    for row in providers.all():
        if not row["enabled"] or not row["provider"].strip() or not row["model"].strip():
            continue
        adapter = adapters.find(row["capability"], row["backend"], row["provider"])
        health = adapters.health(row)
        badge, at = validations.badge(validations.summary_for(row))
        out.append(ProviderOptionOut(
            capability=row["capability"],
            capability_label=CAPABILITIES.get(row["capability"], row["capability"]),
            backend=row["backend"],
            backend_label=BACKENDS.get(row["backend"], row["backend"]),
            provider=row["provider"], model=row["model"],
            adapter_label=adapter.label if adapter else "",
            health_state=health["state"], health_detail=health["detail"],
            validation_state=badge, validation_at=at,
            models=[m.as_dict() for m in adapter.models] if adapter else [],
        ))
    return out


@router.get("/providers/current", response_model=list[CurrentProviderOut])
def current_providers(_=Depends(require_user)) -> list[CurrentProviderOut]:
    """给其他模块只读的"当前默认"。五类能力固定五行，没配的也返回。

    普通模块要知道"这一类现在走本地还是云端、配没配好"，但不需要 notes
    与密钥变量名，所以这里只回状态，不复用管理页那份带备注的对象。
    """
    defaults = {row["capability"]: row for row in providers.defaults()}
    out: list[CurrentProviderOut] = []
    for capability, label in CAPABILITIES.items():
        row = defaults.get(capability)
        if row is None:
            out.append(CurrentProviderOut(
                capability=capability, capability_label=label,
                backend="", backend_label="", provider="", model="",
                adapter="", health_state="unconfigured",
                health_detail="没有默认后端"))
            continue
        health = adapters.health(row)
        badge, at = validations.badge(validations.summary_for(row))
        out.append(CurrentProviderOut(
            capability=capability, capability_label=label,
            backend=row["backend"], backend_label=BACKENDS.get(row["backend"], ""),
            provider=row["provider"], model=row["model"],
            adapter=(health["adapter"] or {}).get("label", ""),
            health_state=health["state"], health_detail=health["detail"],
            validation_state=badge, validation_at=at))
    return out


@router.get("/providers/validations", response_model=list[ValidationOut])
def list_validations(limit: int = 50,
                     _=Depends(require_permission("system:config"))) -> list[ValidationOut]:
    """验证历史。只有管理员能看整份——detail 里会有本机路径与缺失项。"""
    return [ValidationOut.from_row(r) for r in validation_repo.list_recent(limit)]


@router.post("/providers/validate", response_model=ValidationOut)
def run_env_validation(payload: ValidationIn,
                       user=Depends(require_permission("system:config"))) -> ValidationOut:
    """跑一次环境检查并留痕。

    这一枪**不发网络请求、不加载权重**，所以它永远不能替代真实调用验证；
    `kind` 也被限死成 env_check——想从这里写出一条 real_call，接口直接拒。
    """
    if payload.kind != "env_check":
        raise HTTPException(status_code=422,
                            detail="这个入口只做环境检查；真实调用要从一条已成功的作业登记")
    row = providers.slot(payload.capability, payload.backend)
    if row is None:
        raise HTTPException(status_code=422, detail="这一轨不存在")
    if payload.provider and row["provider"] != payload.provider:
        raise HTTPException(status_code=422,
                            detail=f"这一轨现在配的是 {row['provider']}，不是 {payload.provider}")
    result = validations.env_check(row, user["id"])
    return ValidationOut.from_row(result["validation"])


@router.post("/providers/validate-job/{job_id}", response_model=ValidationOut)
def record_job_validation(job_id: int, payload: ValidationIn,
                          user=Depends(require_permission("system:config"))) -> ValidationOut:
    """把一条已经跑完的作业登记成该槽位的验证凭据。

    为什么只让管理员做：这条记录会让界面上那一格从"环境检查通过"升级成
    "真实调用已验证"，是个有后果的判断，不该由提交作业的人顺手点。
    """
    if payload.kind == "env_check":
        raise HTTPException(status_code=422, detail="环境检查不走作业，请用「环境检查」按钮")
    try:
        result = validations.from_job(job_id, payload.kind, user["id"], payload.detail,
                                      passed=payload.passed)
    except validations.ValidationInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ValidationOut.from_row(result["validation"])


@router.put("/providers", response_model=list[ProviderOut])
def save_providers(updates: list[ProviderUpdate],
                   _=Depends(require_permission("system:config"))) -> list[ProviderOut]:
    current = {row["id"]: row for row in providers.all()}

    # 只允许改已有的行：不认识的 id 直接拒，避免半截写入
    unknown = [u.id for u in updates if u.id not in current]
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知的配置行：{unknown}")

    for item in updates:
        provider = item.provider.strip()
        if not PROVIDER_RE.match(provider):
            raise HTTPException(
                status_code=422,
                detail=f"provider 只能写字母、数字、点、横线、下划线：{provider}")
        env = item.key_env.strip()
        if env and not KEY_ENV_RE.match(env):
            raise HTTPException(
                status_code=422,
                detail=f"密钥变量名不合法（只能字母/数字/下划线，且不以数字开头）：{env}")

    # 每类能力必须恰好一个默认。两个默认等于"提交作业时不知道该走哪条"
    defaults: dict[str, int] = {}
    for item in updates:
        if item.is_default:
            cap = current[item.id]["capability"]
            defaults[cap] = defaults.get(cap, 0) + 1
            # 视频的云端档只能被镜头显式选中；设成默认等于"配了云端就默认花钱"，
            # 与规格 01-4.8.2「云端视频必须逐镜头显式选择」正面冲突。
            if cap == "video" and current[item.id]["backend"] == "cloud":
                raise HTTPException(
                    status_code=422,
                    detail="云端视频不能设为默认；只能逐镜头显式选择")
    duplicated = [CAPABILITIES.get(cap, cap) for cap, n in defaults.items() if n > 1]
    if duplicated:
        raise HTTPException(status_code=422,
                            detail="每类能力只能有一个默认后端：" + "、".join(duplicated))
    missing = [CAPABILITIES[cap] for cap in CAPABILITIES if defaults.get(cap, 0) == 0]
    if missing:
        raise HTTPException(status_code=422,
                            detail="每类能力都要选一个默认后端：" + "、".join(missing))

    providers.replace_many([item.model_dump() for item in updates])
    return list_providers()
