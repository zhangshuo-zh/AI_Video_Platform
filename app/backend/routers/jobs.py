"""生成中心的 Job 接口（W07）：入队、查看、取消、重试、日志、产物。

POST 只做校验与落库，202 立刻返回——真正的执行在独立的 `worker.py` 进程里。
这条边界是规格 02 §7.2 的原话：请求接口不得等待模型跑完。
"""
import json
import mimetypes
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status as http_status
from fastapi.responses import FileResponse

import access
import jobfiles
import services
from config import WORKER_ONLINE_SECONDS
from db import get_conn
from deps import require_permission, require_user
from models import (CAPABILITIES, JOB_FINISHED, JOB_STATUSES, CostBucket,
                    CostCurrencyOut, CostSummaryOut, JobArtifactOut, JobDetailOut,
                    JobLogOut, JobOut, JobSubmitIn, LocalCostOut, WorkerStatusOut)
from repositories.jobs import jobs
from repositories.projects import projects as project_repo

router = APIRouter(prefix="/api", tags=["jobs"])


def _load(job_id: int, user, need: str = "read"):
    """取作业并做项目归属那一道门；看不见就是 404，不泄露存在。"""
    row = jobs.get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="作业不存在")
    access.ensure(user, row["project_id"], need)
    return row


def _check_links(payload: JobSubmitIn) -> None:
    """集与镜头必须真的属于这个项目；两者同时给时必须互相对得上。

    这不是权限问题（项目那一道已经过了），是输入自相矛盾，所以回 422。
    """
    with get_conn() as conn:
        if payload.episode_id is not None:
            ep = conn.execute("SELECT project_id FROM episode WHERE id = ?",
                              (payload.episode_id,)).fetchone()
            if ep is None or ep["project_id"] != payload.project_id:
                raise HTTPException(status_code=422, detail="这一集不属于该项目")
        if payload.shot_id is not None:
            shot = conn.execute(
                """SELECT e.project_id, e.id AS episode_id FROM shot s
                     JOIN episode e ON e.id = s.episode_id WHERE s.id = ?""",
                (payload.shot_id,)).fetchone()
            if shot is None or shot["project_id"] != payload.project_id:
                raise HTTPException(status_code=422, detail="这个镜头不属于该项目")
            if payload.episode_id is not None and shot["episode_id"] != payload.episode_id:
                raise HTTPException(status_code=422, detail="这个镜头不在指定的那一集里")


@router.post("/jobs", response_model=JobOut, status_code=http_status.HTTP_202_ACCEPTED)
def create_job(payload: JobSubmitIn, response: Response,
               user=Depends(require_permission("job:submit"))) -> JobOut:
    access.ensure(user, payload.project_id, "read")
    _check_links(payload)

    # 幂等：同一个 key 再来一次，返回已有那条，不重复执行、也不重复扣费。
    # 已经被别的项目占用时只回一句"被占用"，不把那条作业的内容带出来。
    if payload.idempotency_key:
        existing = jobs.by_key(payload.idempotency_key)
        if existing is not None:
            try:
                access.ensure(user, existing["project_id"], "read")
            except HTTPException as exc:
                raise HTTPException(status_code=409,
                                    detail="这个幂等键已被占用") from exc
            response.status_code = http_status.HTTP_200_OK
            return JobOut.from_row(existing)

    try:
        row = services.submit_job(payload, user["id"])
    except services.JobInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JobOut.from_row(row)


def _visible_ids(user, project_id: int | None) -> list[int] | None:
    """None＝管理员看全部；列表＝只看这些项目。看不见的项目回 404。"""
    if project_id is not None:
        access.ensure(user, project_id, "read")
        return [project_id]
    if access.is_manager(user):
        return None
    return [r["id"] for r in project_repo.list_visible(user["id"], False, "all")]


def _statuses(raw: str | None) -> list[str]:
    """status 允许逗号分隔：生成中心的"进行中"就是三个状态一起看。
    单值照旧；不认识的值直接 422，不悄悄忽略。"""
    wanted = [s.strip() for s in (raw or "").split(",") if s.strip()]
    for value in wanted:
        if value not in JOB_STATUSES:
            raise HTTPException(status_code=422, detail=f"不认识的状态：{value}")
    return wanted


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(project_id: int | None = None, status: str | None = None,
              capability: str | None = None, execution_mode: str | None = None,
              q: str | None = None, limit: int = 100,
              user=Depends(require_permission("job:read"))) -> list[JobOut]:
    if capability and capability not in CAPABILITIES:
        raise HTTPException(status_code=422, detail=f"不认识的能力：{capability}")
    if execution_mode and execution_mode not in ("local", "cloud"):
        raise HTTPException(status_code=422, detail=f"不认识的执行方式：{execution_mode}")
    wanted = _statuses(status)
    ids = _visible_ids(user, project_id)
    rows = jobs.list(ids, capability=capability, limit=limit,
                     statuses=wanted, execution_mode=execution_mode,
                     keyword=(q or "").strip()[:200] or None)
    return [JobOut.from_row(r) for r in rows]


@router.get("/jobs/costs", response_model=CostSummaryOut)
def job_costs(project_id: int | None = None,
              user=Depends(require_permission("job:read"))) -> CostSummaryOut:
    """成本账：Job 的聚合，唯一的读法。多币种分桶，绝不跨币种相加。

    只汇总 known / pending_reconciliation；not_billable 是本地作业与
    "排队中取消、一次都没调用供应商"的作业。本地机时另算一块。
    """
    summary = jobs.cost_summary(_visible_ids(user, project_id))
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

    local_jobs = len(summary["locals"])
    elapsed = cpu = peak = 0
    for row in summary["locals"]:
        try:
            res = json.loads(row["resources_json"] or "{}")
        except ValueError:
            res = {}
        if not isinstance(res, dict):
            res = {}
        elapsed += int(row["elapsed_ms"] or res.get("wall_ms") or 0)
        cpu += int(res.get("cpu_ms") or 0)
        peak = max(peak, int(res.get("max_rss_kb") or 0))

    return CostSummaryOut(
        currencies=currencies, unpriced_jobs=summary["unpriced"],
        local=LocalCostOut(jobs=local_jobs, elapsed_ms=elapsed, cpu_ms=cpu,
                           max_rss_kb=peak),
        total_jobs=summary["total_jobs"],
    )


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
def job_detail(job_id: int, user=Depends(require_permission("job:read"))) -> JobDetailOut:
    return JobDetailOut.from_row(_load(job_id, user))


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: int, user=Depends(require_permission("job:cancel"))) -> JobOut:
    row = _load(job_id, user)
    if row["status"] in JOB_FINISHED:
        raise HTTPException(status_code=409, detail="这条作业已经结束，不能再取消")
    updated = jobs.request_cancel(job_id)
    return JobOut.from_row(updated)


@router.post("/jobs/{job_id}/retry", response_model=JobOut,
             status_code=http_status.HTTP_202_ACCEPTED)
def retry_job(job_id: int, user=Depends(require_permission("job:submit"))) -> JobOut:
    row = _load(job_id, user)
    if row["status"] not in ("failed", "cancelled", "needs_attention"):
        raise HTTPException(status_code=409,
                            detail="只有失败、已取消或待核对的作业可以重试")
    # 新行复制参数快照，原行的日志/耗时/错误一个字段都不动
    return JobOut.from_row(jobs.retry(row, user["id"]))


@router.get("/jobs/{job_id}/logs", response_model=JobLogOut)
def job_logs(job_id: int, user=Depends(require_permission("job:read"))) -> JobLogOut:
    row = _load(job_id, user)
    return JobLogOut(path=row["log_path"], text=jobfiles.read_log(row["log_path"]))


@router.get("/jobs/{job_id}/artifacts", response_model=list[JobArtifactOut])
def job_artifacts(job_id: int,
                  user=Depends(require_permission("job:read"))) -> list[JobArtifactOut]:
    row = _load(job_id, user)
    return [JobArtifactOut(**item) for item in jobfiles.artifacts(row["result_paths_json"])]


@router.get("/jobs/{job_id}/artifacts/{index}")
def job_artifact_file(job_id: int, index: int,
                      user=Depends(require_permission("job:read"))):
    """取一件产物。路径在库里只是相对写法，读之前再比对一次真实边界；
    越界或已不在都回 404，不泄露文件系统结构。"""
    row = _load(job_id, user)
    target = jobfiles.artifact_at(row["result_paths_json"], index)
    if target is None:
        raise HTTPException(status_code=404, detail="产物不存在或已清理")
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media, filename=target.name)


@router.get("/worker/status", response_model=WorkerStatusOut)
def worker_status(_=Depends(require_user)) -> WorkerStatusOut:
    """顶栏那个小圆点用的。任何登录者都能看在线状态与队列长度；
    作业内容仍要 job:read + 项目归属。"""
    state = jobs.worker_state()
    heartbeat = (state["heartbeat_at"] if state else "") or ""
    age = None
    online = False
    if heartbeat:
        try:
            then = datetime.strptime(heartbeat, "%Y-%m-%d %H:%M:%S")
            age = max(0, int((datetime.now() - then).total_seconds()))
            online = age <= WORKER_ONLINE_SECONDS
        except ValueError:
            age = None
    return WorkerStatusOut(
        online=online, heartbeat_at=heartbeat, age_seconds=age,
        current_job_id=state["current_job_id"] if (state and online) else None,
        queue=jobs.counts(),
    )
