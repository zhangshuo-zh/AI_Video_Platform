"""整季生产接口（阶段 E-2）。

权限沿用单集产线：读＝`job:read`，发起／暂停／恢复／取消／继续失败集＝`job:submit`。
所有端点都先过项目级归属；看不见的项目一律 404，不透露这一季是否存在。
"""
from fastapi import APIRouter, Depends, HTTPException, status as http_status

import access
import seasons
from deps import require_permission
from models import SeasonCancelIn, SeasonIn, SeasonOut
from repositories.seasons import seasons as season_repo

router = APIRouter(prefix="/api", tags=["seasons"])


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except seasons.SeasonMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except seasons.SeasonConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except seasons.SeasonInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _load(season_id: int, user):
    row = season_repo.get(season_id)
    if row is None:
        raise HTTPException(status_code=404, detail="整季记录不存在")
    access.ensure(user, row["project_id"], "read")
    return row


@router.get("/projects/{project_id}/seasons", response_model=list[SeasonOut])
def list_seasons(project_id: int,
                 user=Depends(require_permission("job:read"))):
    access.ensure(user, project_id, "read")
    return [SeasonOut(**row) for row in seasons.list_for_project(project_id)]


@router.post("/projects/{project_id}/seasons", response_model=SeasonOut,
             status_code=http_status.HTTP_201_CREATED)
def create_season(project_id: int, payload: SeasonIn,
                  user=Depends(require_permission("job:submit"))):
    access.ensure(user, project_id, "write")
    view = _wrap(seasons.create, project_id, payload.template_id, user["id"],
                 episode_ids=payload.episode_ids, name=payload.name,
                 max_active_runs=payload.max_active_runs,
                 idempotency_key=payload.idempotency_key)
    return SeasonOut(**view)


@router.get("/seasons/{season_id}", response_model=SeasonOut)
def get_season(season_id: int, user=Depends(require_permission("job:read"))):
    _load(season_id, user)
    return SeasonOut(**_wrap(seasons.view, season_id))


@router.post("/seasons/{season_id}/pause", response_model=SeasonOut)
def pause_season(season_id: int, user=Depends(require_permission("job:submit"))):
    row = _load(season_id, user)
    access.ensure(user, row["project_id"], "write")
    return SeasonOut(**_wrap(seasons.pause, season_id))


@router.post("/seasons/{season_id}/resume", response_model=SeasonOut)
def resume_season(season_id: int, user=Depends(require_permission("job:submit"))):
    row = _load(season_id, user)
    access.ensure(user, row["project_id"], "write")
    return SeasonOut(**_wrap(seasons.resume, season_id))


@router.post("/seasons/{season_id}/cancel", response_model=SeasonOut)
def cancel_season(season_id: int, payload: SeasonCancelIn,
                  user=Depends(require_permission("job:submit"))):
    row = _load(season_id, user)
    access.ensure(user, row["project_id"], "write")
    return SeasonOut(**_wrap(seasons.cancel, season_id, payload.reason))


@router.post("/seasons/{season_id}/retry-failed", response_model=SeasonOut)
def retry_failed(season_id: int, user=Depends(require_permission("job:submit"))):
    row = _load(season_id, user)
    access.ensure(user, row["project_id"], "write")
    return SeasonOut(**_wrap(seasons.retry_failed, season_id, user["id"]))
