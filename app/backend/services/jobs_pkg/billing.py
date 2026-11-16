"""作业服务 billing 域。"""
from ..errors import JobInvalid
from models import CAPABILITIES, CostBucket, CostCurrencyOut, CostSummaryOut, LocalCostOut
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from repositories.jobs import jobs as job_repo
from repositories.jobs_parts.common import JobStateConflict as _RepoStateConflict
from .errors import JobMissing
from .errors import JobStateConflict





def _cost_of(payload, mode: str, created_by: int | None) -> dict:
    """把"预计成本确认单"变成 Job 的成本列。

    三条口径，任何一条不成立都不落库：
    1. 云端必须带确认单、必须勾确认。确认不是走形式——它把"按供应商计费"
       这句话和一组具体的量 × 单价绑在一起，事后能指着这一行问"当时是谁点的、按什么价"。
    2. 金额由服务端用 Decimal 算，四舍五入到整数 micros；客户端传什么总价都不采信。
    3. 本地作业不计费。带确认单说明调用方把两条轨搞混了，直接拒掉，
       比默默忽略、等到有人拿这条账去对预算更早暴露问题。
    """
    estimate = payload.estimate
    if mode == "cloud":
        if not payload.user_confirmed_cloud:
            raise JobInvalid("云端作业要先确认按供应商计费的预计成本")
        if estimate is None:
            raise JobInvalid("云端作业必须带预计成本（数量、单位、单价、币种与价格来源）")
        quantity = estimate.quantity
        cost_micros = int((quantity * Decimal(estimate.unit_price_micros))
                          .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        snapshot = {
            "quantity": str(quantity),
            "unit": estimate.unit,
            "unit_price_micros": estimate.unit_price_micros,
            "currency": estimate.currency,
            "source": estimate.source.strip(),
            "cost_micros": cost_micros,
            "formula": "quantity * unit_price_micros, half-up to integer micros",
            "confirmed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed_by": created_by,
            "billable": "pending_reconciliation",
        }
        return {"quantity": float(quantity), "unit": estimate.unit,
                "unit_price_micros": estimate.unit_price_micros,
                "cost_micros": cost_micros, "currency": estimate.currency,
                "pricing_snapshot": snapshot,
                "billable_status": "pending_reconciliation"}
    if estimate is not None:
        raise JobInvalid("本地作业不计费，不要提交预计成本；云端作业请显式指定 execution_mode")
    return {"quantity": None, "unit": None, "unit_price_micros": None,
            "cost_micros": 0, "currency": None, "pricing_snapshot": None,
            "billable_status": "not_billable"}





def reconcile_job(job_id: int, payload, user) -> dict:
    """人工核对一条待核对的云端作业。

    三条硬规矩：
    - 只有 `needs_attention` 能核；正常成功/失败不该被这条路径改写。
    - `succeeded`/`failed` 必须给实际金额，且云端作业才允许核。
    - 只动账单与核对列，**绝不重新提交**；重投是人的另一个决定。
    """
    job = job_repo.get(job_id)
    if job is None:
        raise JobMissing("作业不存在")
    if job["execution_mode"] != "cloud":
        raise JobStateConflict("只有云端作业需要账单核对；本地作业不计费")
    if job["status"] != "needs_attention":
        raise JobStateConflict("这条作业当前不是待核对状态")
    if payload.decision in ("succeeded", "failed") and payload.actual_cost_micros is None:
        raise JobStateConflict("确认结果前必须填实际金额；不确定就选「继续待核对」")
    try:
        row = job_repo.reconcile(job_id, payload.decision, payload.actual_cost_micros,
                                 payload.note.strip(), user["id"])
    except _RepoStateConflict as exc:
        # 后到者的决定没有写入。返回 409 而不是 200：界面上"我核过了"
        # 和"其实别人先核了、我的决定没生效"必须是两件事。
        raise JobStateConflict(str(exc)) from exc
    if row is None:
        raise JobMissing("作业不存在")
    return {"job": row, "message": {
        "succeeded": "已确认可用，账单记为已确认。",
        "failed": "已确认不可用，这笔账按实际金额结清。",
        "pending": "仍保持待核对。",
    }[payload.decision]}





def mark_job_attention(job_id: int, reason: str, user) -> dict:
    """人工把一条云端作业送进"待核对"。

    为什么允许从 succeeded/failed 进来：供应商回执说成功、产物却打不开，
    或用量与预计差很多，这类情况现在只能落 failed，而 failed 的默认动作是重试——
    对一笔可能已经计过费的调用，"默认重试"是最坏的建议。
    """
    job = job_repo.get(job_id)
    if job is None:
        raise JobMissing("作业不存在")
    if job["execution_mode"] != "cloud":
        raise JobStateConflict("本地作业没有供应商账单可核对")
    if job["status"] not in ("succeeded", "failed"):
        raise JobStateConflict("只有已结束的成功或失败作业能改成待核对")
    # 状态写入走仓储的状态机（2026-11-09 外部审查第 3 条）。
    # 原来这里是裸 UPDATE：不查转移、不带状态条件、不看 rowcount，
    # 是集中状态表唯一没覆盖到的旁路。现在 `transaction()` 里读-断言-写，
    # 并发下要么写成功要么明确冲突，不会把 running 的作业钉进终态。
    try:
        job_repo.mark_attention_by_user(job_id, reason)
    except _RepoStateConflict as exc:
        raise JobStateConflict(str(exc)) from exc
    return {"job": job_repo.get(job_id), "message": "已标记为待核对。"}


def job_cost_summary(project_ids: list[int] | None) -> CostSummaryOut:
    """按已鉴权项目范围聚合成本；币种分桶，保留本地资源统计。"""
    summary = job_repo.cost_summary(project_ids)
    by_currency: dict[str, dict] = {}
    for row in summary["rows"]:
        bucket = by_currency.setdefault(row["currency"], {
            "currency": row["currency"], "total_micros": 0, "jobs": 0,
            "known": 0, "pending": 0, "capabilities": {},
        })
        total = int(row["total"] or 0)
        n = int(row["n"] or 0)
        bucket["total_micros"] += total
        bucket["jobs"] += n
        bucket["known" if row["billable_status"] == "known" else "pending"] += n
        cap = bucket["capabilities"].setdefault(row["capability"], {"total": 0, "jobs": 0})
        cap["total"] += total
        cap["jobs"] += n

    currencies = []
    for code in sorted(by_currency):
        bucket = by_currency[code]
        currencies.append(CostCurrencyOut(
            currency=code, total_micros=bucket["total_micros"], jobs=bucket["jobs"],
            known=bucket["known"], pending=bucket["pending"],
            by_capability=[
                CostBucket(capability=cap, capability_label=CAPABILITIES.get(cap, cap),
                           total_micros=val["total"], jobs=val["jobs"])
                for cap, val in bucket["capabilities"].items()
            ],
        ))

    # 三个数已经由仓储层在 SQL 里聚合好（2026-11-08）：原来是把全部本地作业的
    # `resources_json` 整表拉进来再在 Python 里累加，全库 5503 行。
    agg = summary["local_agg"]
    return CostSummaryOut(
        currencies=currencies, unpriced_jobs=summary["unpriced"],
        local=LocalCostOut(jobs=agg["jobs"], elapsed_ms=agg["elapsed_ms"],
                           cpu_ms=agg["cpu_ms"], max_rss_kb=agg["max_rss_kb"]),
        total_jobs=summary["total_jobs"],
    )
