"""生成中心的 Job 接口（W07）：入队、查看、取消、重试、日志、产物。

POST 只做校验与落库，202 立刻返回——真正的执行在独立的 `worker.py` 进程里。
这条边界是规格 02 §7.2 的原话：请求接口不得等待模型跑完。
"""
import mimetypes

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status as http_status
from fastapi.responses import FileResponse

import access
import error_catalog
import job_state
import jobfiles
import services
from db import get_conn
from deps import require_permission, require_user
from models import (CAPABILITIES, JOB_STATUSES, CostBucket,
                    JobDiagnosisOut,
                   JobRetryBatchIn,
                    CostCurrencyOut, CostSummaryOut, JobArtifactOut, JobAttentionIn,
                    JobDetailOut, JobLogOut, JobOut, JobReconcileIn, JobRetryPreviewOut,
                    JobSubmitIn, LocalCostOut, WorkerStatusOut)
from repositories.content import episodes
from repositories.jobs import clamp_limit, jobs, retry_reason
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
def list_jobs(response: Response,
              project_id: int | None = None, episode_id: int | None = None,
              shot_id: int | None = None, status: str | None = None,
              capability: str | None = None, execution_mode: str | None = None,
              q: str | None = None, limit: int = Query(100, ge=1),
              before_id: int | None = None,
              user=Depends(require_permission("job:read"))) -> list[JobOut]:
    """队列列表。episode_id／shot_id 是给创作中心分镜表用的窄筛选。

    传了集或镜就先按它们绕回项目判可见性（看不见回 404，不泄露存在），
    再与 project_id 合并成同一个范围。前端一集只打一次，按 shot_id 分组，
    不为一镜一行各发一条请求。

    翻页（`before_id`）与总量（`X-Total-Count`）是为了解决一个**安静说谎**：
    队列原来写死 limit 100，作业一多，超出的部分不是「没显示」而是「压根没取」，
    而界面上那格数字会停在 100，看着像「总共就这些」。现在：
      * `before_id` = 上一页最后一行的 id，只回比它更早的行（队列本来就是 id 倒序）；
      * 响应头 `X-Total-Count` = 与当前筛选**同口径**的总数，用作真分母。
    用游标不用 offset：队列 3 秒轮询一次、期间不断入队，offset 会让新行把整页
    往下挤，翻第二页时第一页末尾那几条会重复出现。
    """
    if capability and capability not in CAPABILITIES:
        raise HTTPException(status_code=422, detail=f"不认识的能力：{capability}")
    if execution_mode and execution_mode not in ("local", "cloud"):
        raise HTTPException(status_code=422, detail=f"不认识的执行方式：{execution_mode}")
    scope = project_id
    if episode_id is not None:
        row = access.ensure_by("episode", episode_id, user, "read", "创作单元")
        if scope is not None and row["id"] != scope:
            raise HTTPException(status_code=404, detail="创作单元不存在")
        scope = row["id"]
    if shot_id is not None:
        row = access.ensure_by("shot", shot_id, user, "read", "镜头")
        if scope is not None and row["id"] != scope:
            raise HTTPException(status_code=404, detail="镜头不存在")
        scope = row["id"]
    wanted = _statuses(status)
    ids = _visible_ids(user, scope)
    kw = dict(capability=capability, statuses=wanted,
              execution_mode=execution_mode,
              keyword=(q or "").strip()[:200] or None,
              episode_id=episode_id, shot_id=shot_id)
    # **先把 limit 夹到仓储真正会用到的那个值**（2026-11-09 外部审查第 4 条）。
    #
    # `jobs.list()` 内部把 limit 夹在 200 以内，但下面判定"这页取满了没有"
    # 用的是**原始** limit。于是 `?limit=500` 时：仓储返回 200 行，
    # `200 >= 500` 为假 → 判定没取满 → 不查总数 → `X-Total-Count` 直接
    # 报成 200。而库里其实有 300 条，界面上的"共 N 条"就少报了。
    # 夹一次之后，两个地方用的是同一个数，分母才和列表同一口径。
    limit = clamp_limit(limit)
    rows = jobs.list(ids, limit=limit, before_id=before_id, **kw)
    # 分母与列表走同一套筛选参数（kw 是同一份），不给「共 N」留第二种口径。
    # 只在这一页取满时才真算：没取满说明本来就到底了，多一次 COUNT 没有意义。
    full = len(rows) >= limit
    total = jobs.count_listed(ids, **kw) if (full or before_id) else len(rows)
    response.headers["X-Total-Count"] = str(total)
    # 跨端口取不到这个头，除非显式放行；前端同源部署时不受影响，
    # 但本地 5173 调 8000 的开发形态要靠它才能读到分母。
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
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

    # 三个数已经由仓储层在 SQL 里聚合好（2026-11-08）：原来是把全部本地作业的
    # `resources_json` 整表拉进来再在 Python 里累加，全库 5503 行。
    agg = summary["local_agg"]
    return CostSummaryOut(
        currencies=currencies, unpriced_jobs=summary["unpriced"],
        local=LocalCostOut(jobs=agg["jobs"], elapsed_ms=agg["elapsed_ms"],
                           cpu_ms=agg["cpu_ms"], max_rss_kb=agg["max_rss_kb"]),
        total_jobs=summary["total_jobs"],
    )


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
def job_detail(job_id: int, user=Depends(require_permission("job:read"))) -> JobDetailOut:
    return JobDetailOut(**services.job_detail_view(_load(job_id, user)))


@router.get("/jobs/{job_id}/diagnosis", response_model=JobDiagnosisOut)
def job_diagnosis(job_id: int, user=Depends(require_permission("job:read"))) -> dict:
    """任务书 §14 的异常恢复目录落在这一条作业上：一份形状固定的诊断。

    为什么不塞进 `JobDetailOut`：详情列表每次都要带 `error_code` 与原话，
    但那七项解释只在人点开"为什么失败、能不能重试"时才用得上。
    分开之后队列页不会被几十段文案撑大响应，而界面要点开才付这份钱。
    """
    return error_catalog.for_job(_load(job_id, user))


@router.get("/jobs/{job_id}/retry-preview", response_model=JobRetryPreviewOut)
def retry_preview(job_id: int,
                  user=Depends(require_permission("job:submit"))) -> JobRetryPreviewOut:
    """重试前那一屏。它不创建任何东西，只回答"会继承什么、可能再花多少"。"""
    _load(job_id, user)
    try:
        return JobRetryPreviewOut(**services.retry_preview(job_id))
    except services.JobMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/reconcile", response_model=JobDetailOut)
def reconcile_job(job_id: int, payload: JobReconcileIn,
                  user=Depends(require_permission("job:submit"))) -> JobDetailOut:
    """人工核对一条待核对的云端作业。不重新提交、不改提示词与参数快照。"""
    _load(job_id, user, "write")
    try:
        result = services.reconcile_job(job_id, payload, user)
    except services.JobMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.JobStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobDetailOut(**services.job_detail_view(result["job"]))


@router.post("/jobs/{job_id}/attention", response_model=JobDetailOut)
def mark_attention(job_id: int, payload: JobAttentionIn,
                   user=Depends(require_permission("job:submit"))) -> JobDetailOut:
    """把一条云端作业显式送进"待核对"。

    为什么需要这个入口：供应商回执说成功、产物却打不开时，落 failed 的默认建议是重试，
    而那笔钱可能已经花了。让人先把它挂起来核对，比让系统替人决定更安全。
    """
    _load(job_id, user, "write")
    try:
        result = services.mark_job_attention(job_id, payload.reason, user)
    except services.JobMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.JobStateConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JobDetailOut(**services.job_detail_view(result["job"]))


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: int, user=Depends(require_permission("job:cancel"))) -> JobOut:
    row = _load(job_id, user)
    # 能不能取消只看状态机那张表：原来这里另抄了一份 JOB_FINISHED。
    if not job_state.can("cancel", row["status"]):
        raise HTTPException(status_code=409, detail="这条作业已经结束，不能再取消")
    updated = jobs.request_cancel(job_id)
    return JobOut.from_row(updated)


@router.post("/jobs/{job_id}/retry", response_model=JobOut,
             status_code=http_status.HTTP_202_ACCEPTED)
def retry_job(job_id: int, user=Depends(require_permission("job:submit"))) -> JobOut:
    row = _load(job_id, user)
    reason = retry_reason(row)
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    # 新行复制参数快照，原行的日志/耗时/错误一个字段都不动
    return JobOut.from_row(jobs.retry(row, user["id"]))


@router.post("/jobs/retry-batch")
def jobs_retry_batch(payload: JobRetryBatchIn,
                     user=Depends(require_permission("job:submit"))) -> dict:
    """把这一集里卡住的镜头一次重投（任务书 §15 的 P2「批量制作」）。

    路由这一层只做两件事：**归属判断**（看不见就是 404，与单条重试同一道门）
    与把参数交给服务层。规则不在这里——`services.job_retry_batch()` 与单条
    重试走的是同一个 `jobs.retry()`，批量不是第二条写口子。
    """
    ep = episodes.get(payload.episode_id)
    if ep is None:
        raise HTTPException(status_code=404, detail="这一集不存在")
    access.ensure(user, ep["project_id"], "write")
    try:
        return services.job_retry_batch(
            int(ep["project_id"]), payload.episode_id, user_id=user["id"],
            capability=payload.capability, limit=payload.limit)
    except services.JobInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/jobs/{job_id}/logs", response_model=JobLogOut)
def job_logs(job_id: int, user=Depends(require_permission("job:read"))) -> JobLogOut:
    row = _load(job_id, user)
    stat = jobfiles.stat_log(row["log_path"])
    return JobLogOut(path=row["log_path"], text=jobfiles.read_log(row["log_path"]), **stat)


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
def worker_status(user=Depends(require_user)) -> WorkerStatusOut:
    """顶栏那个小圆点用的。任何登录者都能看在线状态与队列长度；
    作业内容仍要 job:read + 项目归属。

    P0 起这里还回 alerts：队列停滞、租约过期、运行超时、待核对——
    这些都不改任何作业状态，只是把"没人干活"和"刚入队"分开说。
    """
    return WorkerStatusOut(**services.worker_status(user))
