"""模型配置：五类能力 × 本地/云端 两轨。

PUT 是整批覆盖而不是逐行改：配置页一次保存十行，
整批提交能在一个事务里校验"每类能力恰好一个默认"，不会出现中间态。
"""
from fastapi import APIRouter, Depends, HTTPException

from deps import require_permission
from models import CAPABILITIES, ProviderOut, ProviderUpdate
from repositories.providers import has_key, providers

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/providers", response_model=list[ProviderOut])
def list_providers(_=Depends(require_permission("system:config"))) -> list[ProviderOut]:
    return [ProviderOut.from_row(row, has_key(row["provider"]))
            for row in providers.all()]


@router.put("/providers", response_model=list[ProviderOut])
def save_providers(updates: list[ProviderUpdate],
                   _=Depends(require_permission("system:config"))) -> list[ProviderOut]:
    current = {row["id"]: row for row in providers.all()}

    # 只允许改已有的行：不认识的 id 直接拒，避免半截写入
    unknown = [u.id for u in updates if u.id not in current]
    if unknown:
        raise HTTPException(status_code=422, detail=f"未知的配置行：{unknown}")

    # 每类能力必须恰好一个默认。两个默认等于"提交作业时不知道该走哪条"
    defaults: dict[str, int] = {}
    for item in updates:
        if item.is_default:
            cap = current[item.id]["capability"]
            defaults[cap] = defaults.get(cap, 0) + 1
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
