"""作业仓储——**门面**，实体已拆到 `repositories/jobs_parts/` 包。

历史：这里原本是 1130 行、单个 `JobRepository` 类（35 个方法）＋ 一批模块级
常量与工具函数。2026-11-08 按职责拆成 7 个 mixin ＋ 一个 `common` 模块，
本文件只做 re-export，保证所有调用方一行不用改：

    from repositories.jobs import jobs, retry_reason, clamp_limit, ...

`jobs` 仍是同一个 `JobRepository()` 实例，API 完全不变。

**不要往这个文件里加新方法**：加到 `repositories/jobs_parts/<职责>.py`
对应的 mixin 里。
"""
from repositories.jobs_parts import JobRepository
from repositories.jobs_parts.common import (        # noqa: F401
    BILLABLE_WHERE,
    COPY_FIELDS,
    JOB_ACTIVE_STATUSES,
    RESOURCE_CLASS_SQL,
    RETRYABLE_JOB_STATUSES,
    JobOwnershipLost,
    _check_terminal,
    _like_escape,
    _loads,
    _require_owned,
    clamp_limit,
    resource_class,
    retry_reason,
)

jobs = JobRepository()

__all__ = [
    "JobRepository", "jobs", "retry_reason", "resource_class", "clamp_limit",
    "JobOwnershipLost", "RESOURCE_CLASS_SQL", "COPY_FIELDS", "BILLABLE_WHERE",
    "JOB_ACTIVE_STATUSES", "RETRYABLE_JOB_STATUSES",
]
