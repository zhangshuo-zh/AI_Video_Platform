from typing import Any

from pydantic import BaseModel, ConfigDict


# ---------- 系统状态 ----------
class HealthOut(BaseModel):
    """`GET /api/health` 的响应。故意只有两个字段。

    这一条是**未登录也开放**的（唯一两个之一，另一个是 login），
    所以它只回"服务活着"这一件事；项目数是因为它对内网排障有用、
    对外不构成情报（不区分项目名、不区分状态）。

    worker / 队列 / 内存那几项还没接，**不返回假值**——
    前端据此显示"未接入"，而不是显示一个编出来的 0。
    """
    ok: bool
    project_count: int


class ProviderManifestOut(BaseModel):
    """能力清单响应（结构由 adapters.manifest_payload 决定，保留全部动态声明）。"""
    model_config = ConfigDict(extra="allow")
    manifest_version: int | str = 1
    adapters: Any = None
    capabilities: Any = None


class GcItemOut(BaseModel):
    job_id: int
    project_id: int
    capability: str
    status: str
    created_at: str
    bytes: int
    files: list[str] = []
    reason: str


class GcPreviewOut(BaseModel):
    project_id: int | None = None
    candidate_count: int
    reclaimable_bytes: int
    protected_count: int
    items: list[GcItemOut] = []


class GcExecuteIn(BaseModel):
    project_id: int | None = None
    job_ids: list[int] | None = None


class GcExecuteOut(BaseModel):
    deleted_jobs: int
    deleted_files: int
    reclaimed_bytes: int
    skipped_job_ids: list[int] = []

