"""音频中心接口（W15）。

音频中心不建第二条队列：提交素材时只落一条 job，执行仍由同一个 worker。
这一页负责"这条台词/音乐属于哪一集哪一镜"，以及作业成功后把产物路径抄回来。
"""
from fastapi import APIRouter, Depends, HTTPException, status as http_status

import access
import services
from deps import require_permission
from models import AudioClipIn, AudioClipOut, AudioClipUpdateIn
from repositories.audio import clips
from repositories.content import episodes, shots

router = APIRouter(prefix="/api", tags=["audio"])

KINDS = ("dialogue", "narration", "music", "sfx")


def _check_links(project_id: int, episode_id: int | None, shot_id: int | None) -> None:
    if episode_id is not None:
        ep = episodes.get(episode_id)
        if ep is None or ep["project_id"] != project_id:
            raise HTTPException(status_code=422, detail="这一集不属于该项目")
    if shot_id is not None:
        shot = shots.get(shot_id)
        if shot is None or access.project_of("shot", shot_id) != project_id:
            raise HTTPException(status_code=422, detail="这个镜头不属于该项目")
        if episode_id is not None and shot["episode_id"] != episode_id:
            raise HTTPException(status_code=422, detail="这个镜头不在指定的那一集里")


def _clip(clip_id: int, user, need: str = "read"):
    row = clips.get(clip_id)
    if row is None:
        raise HTTPException(status_code=404, detail="音频素材不存在")
    access.ensure(user, row["project_id"], need)
    return row


@router.get("/audio/capabilities")
def audio_capabilities(user=Depends(require_permission("audio:read"))) -> dict:
    """音频四类能力的真实状态：能不能提交、缺哪一件、为什么。

    项目无关，所以不带走可见性判断——它只读清单与运行时，不含任何项目内容。
    """
    import production
    return production.audio_status_view()


@router.get("/projects/{project_id}/audio-clips", response_model=list[AudioClipOut])
def list_clips(project_id: int, kind: str | None = None, episode_id: int | None = None,
               user=Depends(require_permission("audio:read"))) -> list[AudioClipOut]:
    access.ensure(user, project_id, "read")
    if kind and kind not in KINDS:
        raise HTTPException(status_code=422, detail="音频类型不认识")
    rows = clips.list(project_id, kind, episode_id)
    return [AudioClipOut(**services.audio_clip_view(services.audio_sync(row)))
            for row in rows]


@router.post("/projects/{project_id}/audio-clips", response_model=AudioClipOut,
             status_code=http_status.HTTP_201_CREATED)
def create_clip(project_id: int, payload: AudioClipIn,
                user=Depends(require_permission("audio:write"))) -> AudioClipOut:
    access.ensure(user, project_id, "write")
    _check_links(project_id, payload.episode_id, payload.shot_id)
    row = clips.create(project_id=project_id, episode_id=payload.episode_id,
                       shot_id=payload.shot_id, kind=payload.kind,
                       speaker=payload.speaker.strip(), text=payload.text.strip(),
                       created_by=user["id"])
    return AudioClipOut(**services.audio_clip_view(row))


@router.patch("/audio-clips/{clip_id}", response_model=AudioClipOut)
def update_clip(clip_id: int, payload: AudioClipUpdateIn,
                user=Depends(require_permission("audio:write"))) -> AudioClipOut:
    row = _clip(clip_id, user, "write")
    if row["status"] in ("queued", "ready"):
        raise HTTPException(status_code=409, detail="作业进行中或已就绪；要改就新建一条")
    updated = clips.update(clip_id, kind=payload.kind, speaker=payload.speaker.strip(),
                           text=payload.text.strip(), notes=payload.notes.strip())
    return AudioClipOut(**services.audio_clip_view(updated))


@router.post("/audio-clips/{clip_id}/submit", response_model=AudioClipOut,
             status_code=http_status.HTTP_202_ACCEPTED)
def submit_clip(clip_id: int,
                user=Depends(require_permission("audio:write")),
                _job=Depends(require_permission("job:submit"))) -> AudioClipOut:
    row = _clip(clip_id, user, "write")
    if row["status"] in ("queued", "ready"):
        raise HTTPException(status_code=409, detail="这条素材已经在队列里或已就绪")
    try:
        return AudioClipOut(**services.audio_submit(row, user))
    except services.AudioInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/audio-clips/{clip_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_clip(clip_id: int,
                user=Depends(require_permission("audio:write"))) -> None:
    _clip(clip_id, user, "write")
    clips.delete(clip_id)
