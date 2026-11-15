import adapters
import json
import re

from repositories.audio import clips
from repositories.content import assets, episodes, profiles, shots
from repositories.jobs import jobs as job_repo
from types import SimpleNamespace

from .errors import AudioInvalid, JobInvalid, NoRoute
from .jobs import shot_asset_ids, submit_job
from .providers import resolve_provider


_DIALOGUE_PREFIX_RE = re.compile(r"^\s*([^\s：:]{1,16})[：:]\s*[「“\"]?(.+?)[」”\"]?\s*$")
_QUOTE_RE = re.compile(r"[「“\"]([^」”\"]{2,200})[」”\"]")


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




def _character_index(project_id: int) -> tuple[dict[str, dict], dict[int, dict]]:
    """构造本项目可见角色的「名/别名 -> 音色档案」与「asset_id -> 音色档案」索引。"""
    by_name: dict[str, dict] = {}
    by_id: dict[int, dict] = {}
    for arow in assets.list_visible(project_id, kind="character"):
        aid = int(arow["id"])
        prof = profiles.get(aid)
        asset_name = str(arow["name"] or "").strip()
        cfg: dict = {
            "asset_id": aid,
            "asset_name": asset_name,
            "tts_voice": str(prof.get("tts_voice") or "").strip(),
            "tts_rate": float(prof.get("tts_rate") or 1.0),
            "tts_mood": str(prof.get("tts_mood") or "").strip(),
        }
        by_id[aid] = cfg
        if asset_name:
            by_name[asset_name.lower()] = cfg
        try:
            aliases = json.loads(arow["aliases"] or "[]")
        except (TypeError, ValueError):
            aliases = []
        for alias in aliases:
            name_key = str(alias or "").strip().lower()
            if name_key and name_key not in by_name:
                by_name[name_key] = cfg
    return by_name, by_id


def _resolve_character_voice(project_id: int, speaker: str, shot_id: int | None,
                             *, by_name: dict[str, dict] | None = None,
                             by_id: dict[int, dict] | None = None) -> tuple[str, dict]:
    if by_name is None or by_id is None:
        by_name, by_id = _character_index(project_id)
    spk = (speaker or "").strip()
    if spk and spk.lower() in by_name:
        return spk, by_name[spk.lower()]
    if shot_id is not None:
        shot = shots.get(int(shot_id))
        if shot is not None:
            for aid in sorted(shot_asset_ids(shot)):
                cfg = by_id.get(aid)
                if cfg is not None:
                    return (spk or cfg["asset_name"]), cfg
    return spk, {}


def _tts_params_from_voice(voice_cfg: dict) -> dict:
    if not voice_cfg:
        return {}
    tts_voice = str(voice_cfg.get("tts_voice") or "").strip()
    tts_mood = str(voice_cfg.get("tts_mood") or "").strip()
    tts_rate = float(voice_cfg.get("tts_rate") or 1.0)
    has_rate = abs(tts_rate - 1.0) > 1e-3
    params: dict = {}
    instruct_parts = [p for p in (tts_voice, tts_mood) if p]
    if instruct_parts:
        params["instruct"] = "，".join(instruct_parts)
    if instruct_parts or has_rate:
        params["speed"] = round(max(0.5, min(2.0, tts_rate)), 2)
    return params


def _extract_shot_dialogue(shot, by_id: dict[int, dict], by_name: dict[str, dict]) -> tuple[str, str] | None:
    """从分镜摘要里尝试识别「角色：台词」或带引号台词（需能定位对话文本）。"""
    summary = str(shot["summary"] or "").strip()
    if not summary:
        return None
    m = _DIALOGUE_PREFIX_RE.match(summary)
    if m:
        prefix, line = m.group(1).strip(), m.group(2).strip()
        if line and (prefix.lower() in by_name or any(ch in summary for ch in ("「", "“", "\""))):
            return prefix, line
    qm = _QUOTE_RE.search(summary)
    if qm:
        line = qm.group(1).strip()
        spk = ""
        for aid in sorted(shot_asset_ids(shot)):
            if aid in by_id:
                spk = by_id[aid]["asset_name"]
                break
        if line:
            return spk, line
    return None


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
    params: dict = {}
    if capability == "tts":
        _, voice_cfg = _resolve_character_voice(
            int(clip["project_id"]), str(clip["speaker"] or ""), clip["shot_id"])
        params.update(_tts_params_from_voice(voice_cfg))
    # 时长与字级时间戳由适配器自己写；这里只冻结文本与关联
    payload = SimpleNamespace(
        project_id=clip["project_id"], episode_id=clip["episode_id"],
        shot_id=clip["shot_id"], capability=capability,
        execution_mode="local", provider_code=route["provider"],
        model_code=route["model"], prompt=clip["text"], params=params,
        input_refs=[{"clip_id": clip["id"], "kind": kind,
                     "shot_id": clip["shot_id"]}],
        estimate=None, user_confirmed_cloud=False, priority=0,
        idempotency_key=None)
    try:
        job = submit_job(payload, user["id"])
    except JobInvalid as exc:
        raise AudioInvalid(str(exc)) from exc
    return audio_clip_view(clips.set_job(clip["id"], job["id"]))


def audio_batch_tts(project_id: int, episode_id: int, user: dict, *,
                    overwrite_existing: bool = False,
                    auto_submit: bool = False) -> dict:
    """按集批量生成/提交角色配音，并自动注入角色档案的专属音色、语速与情绪。"""
    ep = episodes.get(episode_id)
    if ep is None or int(ep["project_id"]) != int(project_id):
        raise AudioInvalid("创作单元不存在或不属于该项目")

    by_name, by_id = _character_index(project_id)
    existing = [audio_sync(c) for c in clips.list(project_id, None, episode_id)
                if c["kind"] in ("dialogue", "narration")]
    shots_with_audio = {int(c["shot_id"]) for c in existing if c["shot_id"] is not None}

    created_clip_ids: list[int] = []
    submitted_job_ids: list[int] = []
    skipped_shot_ids: list[int] = []
    voice_bindings: dict[str, dict] = {}
    warnings: list[str] = []

    # 1) 对尚未建音频条但摘要里包含明确台词的分镜，先自动建好 dialogue 草稿
    for shot in shots.list_by_episode(episode_id):
        sid = int(shot["id"])
        if sid in shots_with_audio:
            continue
        parsed = _extract_shot_dialogue(shot, by_id, by_name)
        if parsed is None:
            continue
        speaker, text = parsed
        row = clips.create(
            project_id=project_id, episode_id=episode_id, shot_id=sid,
            kind="dialogue", speaker=speaker, text=text, created_by=user["id"])
        created_clip_ids.append(int(row["id"]))
        existing.append(row)
        shots_with_audio.add(sid)

    # 2) 为本集待处理对白/旁白条目匹配角色音色，并在开启 auto_submit 时批量派发队列
    for clip in existing:
        cid = int(clip["id"])
        sid = clip["shot_id"]
        status = clip["status"]
        target_clip = clip
        if status in ("queued", "ready"):
            if not overwrite_existing or status == "queued":
                if sid is not None and int(sid) not in skipped_shot_ids:
                    skipped_shot_ids.append(int(sid))
                continue
            # overwrite_existing 且已就绪：复制一条新草稿再重生成，保留审计历史
            target_clip = clips.create(
                project_id=project_id, episode_id=episode_id, shot_id=sid,
                kind=clip["kind"], speaker=clip["speaker"], text=clip["text"],
                created_by=user["id"])
            cid = int(target_clip["id"])
            created_clip_ids.append(cid)

        matched_speaker, voice_cfg = _resolve_character_voice(
            project_id, str(target_clip["speaker"] or ""), sid,
            by_name=by_name, by_id=by_id)
        tts_params = _tts_params_from_voice(voice_cfg)
        bind_key = matched_speaker or f"clip:{cid}"
        if voice_cfg:
            voice_bindings[bind_key] = {
                "asset_id": voice_cfg["asset_id"],
                "tts_voice": voice_cfg["tts_voice"],
                "tts_rate": voice_cfg["tts_rate"],
                "tts_mood": voice_cfg["tts_mood"],
                "params": tts_params,
            }
        note_text = str(target_clip["notes"] or "").strip()
        if not note_text and tts_params:
            note_text = f"音色绑定：{tts_params.get('instruct', '默认')}（语速 {tts_params.get('speed', 1.0)}x）"
        if matched_speaker != str(target_clip["speaker"] or "") or note_text != str(target_clip["notes"] or ""):
            target_clip = clips.update(
                cid, kind=target_clip["kind"], speaker=matched_speaker,
                text=target_clip["text"], notes=note_text) or target_clip

        if auto_submit:
            try:
                submitted = audio_submit(target_clip, user)
                if submitted.get("job_id") is not None:
                    submitted_job_ids.append(int(submitted["job_id"]))
            except AudioInvalid as exc:
                warnings.append(f"clip#{cid}: {exc}")

    return {
        "project_id": project_id,
        "episode_id": episode_id,
        "created_clip_ids": created_clip_ids,
        "submitted_job_ids": submitted_job_ids,
        "skipped_shot_ids": skipped_shot_ids,
        "voice_bindings": voice_bindings,
        "warnings": warnings,
    }
