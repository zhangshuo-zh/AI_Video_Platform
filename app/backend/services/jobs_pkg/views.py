"""作业服务 views 域。"""
from datetime import datetime
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
