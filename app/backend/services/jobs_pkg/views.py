"""作业服务 views 域。"""
from datetime import datetime
from typing import TypedDict
from sqlite3 import Row
from repositories.jobs import clamp_limit
from models import JOB_STATUSES
from repositories.jobs import jobs as job_repo
import jobfiles
import json
from .errors import JobMissing
from .monitor import _seconds_since





def job_detail_view(job) -> dict:
    """详情视图：在 JobDetailOut 之外补日志静默与租约状态。

    这些是"现在动没动"的事实，不是新状态；所以不改 job 行，只随响应算一次。
    """
    from models import JobDetailOut

    log = jobfiles.stat_log(job["log_path"])
    out = JobDetailOut.from_row(job).model_dump()
    now = datetime.now()
    out.update({
        "lease_until": job["lease_until"],
        "lease_expired": bool(job["lease_until"]) and job["lease_until"] < now.strftime(
            "%Y-%m-%d %H:%M:%S"),
        "log_silent_seconds": log["silent_seconds"],
        "log_size": log["size"],
        "running_seconds": _seconds_since(job["started_at"])
        if job["status"] in ("starting", "running", "cancel_requested") else None,
    })
    return out





def retry_preview(job_id: int) -> dict:
    """重试前那一屏：继承什么、这一串已经计了多少、会不会再花一次钱。"""
    job = job_repo.get(job_id)
    if job is None:
        raise JobMissing("作业不存在")
    chain = job_repo.retry_chain(job_id)
    cloud = job["execution_mode"] == "cloud"
    rows = []
    total = 0
    pending = 0
    for row in chain:
        cost = row["cost_micros"] or 0
        total += int(cost)
        if row["billable_status"] == "pending_reconciliation":
            pending += 1
        rows.append({"id": row["id"], "status": row["status"],
                     "status_label": JOB_STATUSES.get(row["status"], row["status"]),
                     "attempt": int(row["attempt"] or 1), "cost_micros": int(cost),
                     "billable_status": row["billable_status"],
                     "created_at": row["created_at"]})
    may_charge = cloud and job["unit_price_micros"] is not None
    warning = ""
    if may_charge:
        warning = ("云端重试会新建一条作业并重新走一次供应商调用；"
                   f"这一串目前已有 {pending} 笔待核对、合计 "
                   f"{total / 1_000_000:.2f} {job['currency'] or ''}。")
    elif cloud:
        warning = "这条云端作业没有计价快照；重试仍不会自动补价，请先核对。"
    else:
        warning = "本地重试不计费，但会再占一次本机队列。"
    return {
        "job_id": job["id"], "execution_mode": job["execution_mode"],
        "provider_code": job["provider_code"], "model_code": job["model_code"],
        "prompt_preview": (job["prompt_snapshot"] or "")[:120],
        "params": json_loads_dict(job["params_json"]),
        "quantity": job["quantity"], "unit": job["unit"], "currency": job["currency"],
        "cost_micros": job["cost_micros"], "chain": rows,
        "chain_total_micros": total, "chain_pending": pending,
        "may_charge_again": bool(may_charge), "warning": warning,
    }





def json_loads_dict(text: str) -> dict:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class JobFilters(TypedDict):
    capability: str | None
    statuses: list[str] | None
    execution_mode: str | None
    keyword: str | None
    episode_id: int | None
    shot_id: int | None

def job_page(project_ids: list[int] | None, filters: JobFilters,
             limit: int, before_id: int | None) -> tuple[list[Row], int]:
    """列表和总数共享夹取后的页长与同一份过滤条件。"""
    # **先把 limit 夹到仓储真正会用到的那个值**（2026-11-09 外部审查第 4 条）。
    #
    # `jobs.list()` 内部把 limit 夹在 200 以内，但下面判定"这页取满了没有"
    # 用的是**原始** limit。于是 `?limit=500` 时：仓储返回 200 行，
    # `200 >= 500` 为假 → 判定没取满 → 不查总数 → `X-Total-Count` 直接
    # 报成 200。而库里其实有 300 条，界面上的"共 N 条"就少报了。
    # 夹一次之后，两个地方用的是同一个数，分母才和列表同一口径。
    limit = clamp_limit(limit)
    rows = job_repo.list(project_ids, limit=limit, before_id=before_id, **filters)
    # 分母与列表走同一套筛选参数（kw 是同一份），不给「共 N」留第二种口径。
    # 只在这一页取满时才真算：没取满说明本来就到底了，多一次 COUNT 没有意义。
    full = len(rows) >= limit
    total = job_repo.count_listed(project_ids, **filters) if (full or before_id) else len(rows)
    return rows, total
