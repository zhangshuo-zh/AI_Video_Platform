"""系统状态、实时进度 SSE 流与历史废片安全 GC 接口。"""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import StreamingResponse

import access
import diskplan
from deps import require_permission
from models import GcExecuteIn, GcExecuteOut, GcPreviewOut, HealthOut
from repositories.jobs import jobs as job_repo
from repositories.pipeline import pipeline_repo
from repositories.projects import projects as project_repo

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    # worker / 队列 / 内存还没接，先不返回假值，前端据此显示"未接入"
    return HealthOut(ok=True, project_count=project_repo.count())


def _sse_snapshot(project_ids: list[int] | None) -> dict:
    """纯只读数据库快照：绝对不出网，供 SSE 增量推送使用。"""
    job_rows = job_repo.list(project_ids, limit=24)
    jobs_brief = [
        {
            "id": int(r["id"]),
            "project_id": int(r["project_id"]),
            "capability": str(r["capability"]),
            "status": str(r["status"]),
            "updated_at": str(r["updated_at"]),
        }
        for r in job_rows
    ]
    active_jobs = sum(
        1 for j in jobs_brief
        if j["status"] in ("queued", "starting", "running", "cancel_requested")
    )
    run_rows = []
    if project_ids is None:
        vis = project_repo.list_visible(0, True, "active")
        target_pids = [int(p["id"]) for p in vis[:20]]
    else:
        target_pids = project_ids[:20]
    for pid in target_pids:
        for pr in pipeline_repo.list_by_project(pid, limit=5):
            r_status = (
                "cancelled" if pr["cancelled"]
                else "completed" if pr["finished_at"]
                else "paused" if pr["paused"]
                else "running"
            )
            run_rows.append({
                "id": int(pr["id"]),
                "project_id": int(pr["project_id"]),
                "episode_id": int(pr["episode_id"]),
                "status": r_status,
                "updated_at": str(pr["updated_at"]),
            })
    return {
        "ts": int(time.time()),
        "active_jobs": active_jobs,
        "jobs": jobs_brief,
        "pipeline_runs": run_rows,
    }


@router.get("/events/stream")
async def events_stream(
    request: Request,
    project_id: int | None = Query(default=None),
    once: int = Query(default=0, ge=0, le=1),
    user=Depends(require_permission("job:read")),
):
    """轻量 SSE 实时状态流：推送作业与产线运行进度变化（纯只读库，绝不出网）。"""
    if project_id is not None:
        access.ensure(user, project_id, "read")
        scope_ids: list[int] | None = [project_id]
    else:
        vis = access.visible_project_ids(user)
        scope_ids = None if vis is None else sorted(vis)

    async def _stream():
        last_sig = ""
        ticks = 0
        while True:
            snap = _sse_snapshot(scope_ids)
            sig = json.dumps(
                {"jobs": snap["jobs"], "runs": snap["pipeline_runs"]},
                sort_keys=True,
                ensure_ascii=False,
            )
            if sig != last_sig:
                last_sig = sig
                yield f"event: snapshot\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
            elif ticks % 5 == 4:
                yield ": keepalive\n\n"
            if once:
                return
            if await request.is_disconnected() or ticks >= 15:
                return
            ticks += 1
            await asyncio.sleep(2.0)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/system/gc-preview", response_model=GcPreviewOut)
def system_gc_preview(
    project_id: int | None = Query(default=None),
    user=Depends(require_permission("system:config")),
) -> GcPreviewOut:
    """只读预览可清理的历史废片与预期释放空间。"""
    if project_id is not None:
        access.ensure(user, project_id, "read")
    return GcPreviewOut(**diskplan.gc_preview(project_id=project_id))


@router.post("/system/gc-execute", response_model=GcExecuteOut)
def system_gc_execute(
    payload: GcExecuteIn = Body(default_factory=GcExecuteIn),
    user=Depends(require_permission("system:config")),
) -> GcExecuteOut:
    """安全清理未钉住、非最新成功且未签字的历史废片产物。"""
    if payload.project_id is not None:
        access.ensure(user, payload.project_id, "write")
    return GcExecuteOut(
        **diskplan.gc_execute(project_id=payload.project_id, job_ids=payload.job_ids)
    )
