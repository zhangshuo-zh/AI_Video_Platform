"""成片中心接口（阶段 D）。

权限仍用现有点：读＝`job:read`，发起合成＝`job:submit`（它确实会建一条 Job）。
合成本身由 worker 的子进程跑，接口不在这里等 FFmpeg；这里的 201 只代表
"成片记录与 Job 都已落库"。
"""
from fastapi import (APIRouter, Depends, File, HTTPException, UploadFile,
                     status as http_status)

import access
import jobfiles
from composition import service as composition_service
from composition import subtitles
from composition.subtitles import MAX_SUBTITLE_BYTES
from deps import require_permission
from models import CompositionIn, CompositionOut, SubtitleFileOut
from repositories.compositions import compositions

router = APIRouter(prefix="/api", tags=["compositions"])


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except composition_service.CompositionMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except composition_service.CompositionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except composition_service.CompositionInvalid as exc:
        detail = exc.message
        if exc.reasons and exc.reasons != [exc.message]:
            detail = exc.message + " " + "；".join(exc.reasons)
        raise HTTPException(status_code=422, detail=detail) from exc


@router.get("/projects/{project_id}/compositions",
            response_model=list[CompositionOut])
def list_compositions(project_id: int,
                      user=Depends(require_permission("job:read"))):
    access.ensure(user, project_id, "read")
    return [CompositionOut(**row)
            for row in composition_service.list_for_project(project_id)]


@router.post("/projects/{project_id}/compositions",
             response_model=CompositionOut,
             status_code=http_status.HTTP_201_CREATED)
def create_composition(project_id: int, payload: CompositionIn,
                       user=Depends(require_permission("job:submit"))):
    access.ensure(user, project_id, "write")
    view = _wrap(composition_service.create, project_id, payload.episode_id,
                 user["id"], params={
                     "width": payload.width, "height": payload.height,
                     "fps": payload.fps},
                 idempotency_key=payload.idempotency_key,
                 subtitle_path=payload.subtitle_path)
    return CompositionOut(**view)


@router.get("/compositions/{composition_id}", response_model=CompositionOut)
def get_composition(composition_id: int,
                    user=Depends(require_permission("job:read"))):
    row = compositions.get(composition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="成片记录不存在")
    access.ensure(user, row["project_id"], "read")
    return CompositionOut(**_wrap(composition_service.view, composition_id))


# ---------- 字幕（烧进画面的那一路输入） ----------
#
# 三条接口都不建库表：字幕是"喂给一次合成的输入文件"，台账语义在时间线快照里
# （发起合成时把路径与字节数冻进 timeline_json），这里只管网上的文件本身。

@router.get("/projects/{project_id}/composition-subtitles",
            response_model=SubtitleFileOut)
def list_subtitles(project_id: int, user=Depends(require_permission("job:read"))):
    access.ensure(user, project_id, "read")
    return _subtitle_out(project_id)


@router.post("/projects/{project_id}/composition-subtitles",
             response_model=SubtitleFileOut,
             status_code=http_status.HTTP_201_CREATED)
async def upload_subtitle(project_id: int, file: UploadFile = File(...),
                          user=Depends(require_permission("job:submit"))):
    access.ensure(user, project_id, "write")
    content = await file.read(MAX_SUBTITLE_BYTES + 1)
    try:
        suffix = subtitles.check(file.filename or "", content)
    except subtitles.SubtitleInvalid as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc
    rel = jobfiles.save_subtitle(project_id, content[:MAX_SUBTITLE_BYTES], suffix)
    return _subtitle_out(project_id)


@router.delete("/projects/{project_id}/composition-subtitles",
               response_model=SubtitleFileOut)
def delete_subtitle(project_id: int, path: str,
                    user=Depends(require_permission("job:submit"))):
    access.ensure(user, project_id, "write")
    target = jobfiles.resolve_subtitle(project_id, path)
    if target is None:
        raise HTTPException(status_code=404, detail="这份字幕不在了")
    target.unlink()
    return _subtitle_out(project_id)


def _subtitle_out(project_id: int) -> SubtitleFileOut:
    """把"有哪些字幕"和"这台机器烧不烧得动"一起给界面。

    两件事必须同时到：只看文件会把输入框点亮，等人选好再吃一句 422；
    只看能力又让人不知道该传什么。
    """
    avail = subtitles.availability()
    return SubtitleFileOut(items=jobfiles.list_subtitles(project_id),
                           subtitle_supported=avail["supported"],
                           ffmpeg_hint=avail["hint"])
