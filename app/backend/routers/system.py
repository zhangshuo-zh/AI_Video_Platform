"""系统状态。现在只报真实存在的数字，没接入的一律不编。"""
from fastapi import APIRouter

from repositories.projects import projects as project_repo

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health() -> dict:
    # worker / 队列 / 内存还没接，先不返回假值，前端据此显示"未接入"
    return {"ok": True, "project_count": project_repo.count()}
