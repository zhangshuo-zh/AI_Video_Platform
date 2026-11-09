import adapters
import json

from repositories.audio import clips
from repositories.jobs import jobs as job_repo
from types import SimpleNamespace

from .errors import AudioInvalid, JobInvalid, NoRoute
from .jobs import submit_job
from .providers import resolve_provider


AUDIO_KINDS = {"dialogue": "台词", "narration": "旁白",
               "music": "音乐", "sfx": "音效"}


AUDIO_STATUS = {"draft": "草稿", "queued": "排队中",
                "ready": "已就绪", "failed": "失败"}




def audio_clip_view(row) -> dict:
    try:
        timestamps = json.loads(row["timestamps_json"] or "[]")
    except ValueError:
        timestamps = []
    try:
        visemes = json.loads(row["viseme_json"] or "[]")
    except ValueError:
        visemes = []
    return {
        "id": row["id"], "project_id": row["project_id"],
        "episode_id": row["episode_id"], "episode_seq": row["episode_seq"],
        "episode_title": row["episode_title"], "shot_id": row["shot_id"],
        "shot_seq": row["shot_seq"], "kind": row["kind"],
        "kind_label": AUDIO_KINDS.get(row["kind"], row["kind"]),
        "speaker": row["speaker"], "text": row["text"],
        "status": row["status"],
        "status_label": AUDIO_STATUS.get(row["status"], row["status"]),
        "path": row["path"], "duration_ms": row["duration_ms"],
        "timestamps": timestamps, "visemes": visemes,
        "job_id": row["job_id"], "job_status": row["job_status"],
        "job_error": row["job_error"], "notes": row["notes"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }




def audio_sync(clip):
    """作业终态后把产物路径抄回素材。

    只在 queued 时同步一次；失败保留原因，成功就 ready。
    时长与字级时间戳只有真适配器能写，这一包不伪造。
    """
    if clip["status"] != "queued" or clip["job_id"] is None:
        return clip
    if clip["job_status"] == "succeeded":
        job = job_repo.get(clip["job_id"])
        path = None
        if job is not None:
            try:
                paths = json.loads(job["result_paths_json"] or "[]")
            except ValueError:
                paths = []
            path = paths[0] if paths else None
        if path is None:
            # 作业报成功但产物路径抄不到（job 行已被收掉／result_paths_json 是空的）：
            # 原来这一格照样写 ready，于是界面上出现一条"状态=已就绪、path=null"的
            # 素材——点它什么也不会发生。"没拿到的东西不假装拿到"是这套平台的红线，
            # 所以这里落 failed 并把原因说清，宁可让人重跑一次。
            clips.finish(clip["id"], status="failed", path=None, duration_ms=None)
            return clips.get(clip["id"]) or clip
        clips.finish(clip["id"], status="ready", path=path,
                     duration_ms=clip["duration_ms"])
    elif clip["job_status"] in ("failed", "cancelled"):
        clips.finish(clip["id"], status="failed", path=None,
                     duration_ms=None)
    # needs_attention 不落到上面任何一档：素材的 status 只有
    # draft|queued|ready|failed 四个取值（0019 的 CHECK），而"待核对"既不是失败
    # 也不是就绪。硬塞成 failed 是替人下结论，塞成 ready 是撒谎，所以留 queued，
    # 由音频中心那一列直接把作业状态念出来（audio_clip_view 带 job_status）。
    return clips.get(clip["id"]) or clip




def audio_submit(clip, user) -> dict:
    """把一条素材送进唯一队列。音频中心不另建队列，这里只建 Job。"""
    kind = clip["kind"]
    if kind == "sfx":
        raise AudioInvalid("环境音还没有可核对的自托管适配器，这一版标为实验态，不能提交")
    capability = "music" if kind == "music" else "tts"
    try:
        route = resolve_provider(capability)
    except NoRoute as exc:
        raise AudioInvalid(str(exc)) from exc
    if route["backend"] != "local":
        raise AudioInvalid("音频这一期只走本地轨；云端配音/音乐还没有接确认单")
    # 读路径：能力表是"打开一页"就要的东西，不代用户连服务。
    health = adapters.health(route["row"], probe_service=False)
    if health["state"] != "ok":
        raise AudioInvalid(f"{route['adapter'].label} 现在不可用：{health['detail']}")
    # 时长与字级时间戳由适配器自己写；这里只冻结文本与关联
    payload = SimpleNamespace(
        project_id=clip["project_id"], episode_id=clip["episode_id"],
        shot_id=clip["shot_id"], capability=capability,
        execution_mode="local", provider_code=route["provider"],
        model_code=route["model"], prompt=clip["text"], params={},
        input_refs=[{"clip_id": clip["id"], "kind": kind,
                     "shot_id": clip["shot_id"]}],
        estimate=None, user_confirmed_cloud=False, priority=0,
        idempotency_key=None)
    try:
        job = submit_job(payload, user["id"])
    except JobInvalid as exc:
        raise AudioInvalid(str(exc)) from exc
    return audio_clip_view(clips.set_job(clip["id"], job["id"]))
