"""production workbench 业务域。"""
from __future__ import annotations

from log import get_logger as _get_logger

from repositories.content import episodes, shots
from .audio import _audio_wired

_audit_log = _get_logger(__name__)



def _job_rows(job_rows, signs: dict[int, dict]) -> list[dict]:
    """把作业行摊成界面要的档案条目：签字与产物一起给。

    为什么在服务端拼：这些字段（谁签的、什么时候、产物叫什么、花了多少钱）
    分属 `job`／`review_item`／`result_paths_json` 三处，让浏览器各调一次再拼
    就是三次往返加一份前端口径——而前端那份口径迟早和后端对不上。
    """
    import jobfiles

    out = []
    for job in job_rows:
        try:
            artifacts = jobfiles.artifacts(job["result_paths_json"])
        except (OSError, TypeError, ValueError) as exc:
            _audit_log.debug("_job_rows_fallback error=%s", type(exc).__name__)
            artifacts = []          # 脏 JSON 不该把整个工作台打挂，产物读不到就说读不到
        out.append({
            "job_id": int(job["id"]), "status": job["status"],
            "capability": job["capability"], "provider": job["provider_code"],
            "model": job["model_code"], "execution_mode": job["execution_mode"],
            "created_at": job["created_at"], "finished_at": job["finished_at"],
            "elapsed_ms": job["elapsed_ms"], "cost_micros": job["cost_micros"],
            "currency": job["currency"], "billable_status": job["billable_status"],
            "error_code": job["error_code"], "error_message": job["error_message"],
            "attempt": job["attempt"], "retry_of_job_id": job["retry_of_job_id"],
            "rework_of_job_id": job["rework_of_job_id"],
            "params": job["params_json"], "artifacts": artifacts,
            "sign": signs.get(int(job["id"])),
        })
    return out



def shot_workbench(shot_id: int, *, job_id: int | None = None) -> dict:
    """单镜头生产工作台（任务书 §6.1 八段）。

    一屏要回答的是"这一镜走到哪、还差什么、能不能进合成"。八段的数据
    分别来自 `job`／`review_item`／`asset`／`asset_variant`／`audio_clip`，
    这里只做拼装，**不新建任何事实**：正式关键帧与正式视频仍然是
    "最新那条已签字的成功作业"，判据与 `composition/timeline.build()` 同源。

    "能不能合成"那一栏不自己列规则——它直接引用 `precheck.shot_precheck()`，
    于是镜头工作台、生成中心弹窗、产线发起三处看到的是同一份阻塞清单。
    """
    import precheck
    import services
    from repositories.audio import clips as audio_repo
    from repositories.content import assets as asset_repo, variants as variant_repo
    from repositories.jobs import jobs as job_repo
    from repositories.reviews import reviews

    shot = shots.get(shot_id)
    if shot is None:
        return {"shot_id": int(shot_id), "error": "镜头不存在"}
    episode = episodes.get(int(shot["episode_id"]))
    project_id = int(episode["project_id"]) if episode else None

    def _jobs(capability: str):
        return job_repo.list([project_id] if project_id else None,
                             capability=capability, shot_id=shot_id, limit=50)

    image_jobs, video_jobs = _jobs("image"), _jobs("video")
    by_capability = {"image": image_jobs, "video": video_jobs}

    def include_job(row):
        if row is None or row["project_id"] != project_id or row["shot_id"] != shot_id:
            return
        group = by_capability.get(row["capability"])
        if group is not None and all(item["id"] != row["id"] for item in group):
            group.append(row)

    target = job_repo.get(job_id) if job_id is not None else None
    include_job(target)
    visible_ids = {int(job["id"]) for jobs in by_capability.values() for job in jobs}
    sources = {int(job["rework_of_job_id"]) for jobs in by_capability.values() for job in jobs
               if job["rework_of_job_id"] is not None}
    for source_id in sorted(sources - visible_ids, reverse=True):
        include_job(job_repo.get(source_id))
    job_ids = [int(j["id"]) for j in list(image_jobs) + list(video_jobs)]
    sign_rows = reviews.signs_for_jobs(project_id, job_ids) if job_ids and project_id is not None else []
    # 同一作业可能有多条改判记录：`upsert_sign` 保证同锚点只有一行，
    # 但历史锚点（换过产物）会留下多行——界面要的是最新那一次的结论。
    signs: dict[int, dict] = {}
    for row in sorted(sign_rows, key=lambda r: str(r["signed_at"])):
        signs[int(row["target_id"])] = {
            "decision": row["decision"], "note": row["note"],
            "signed_by": row["signed_by_name"] or "", "signed_at": row["signed_at"],
            "anchor": row["anchor"]}

    def _formal(rows):
        """图像/视频的「正式产物」展示字段＝最新一条"成功且签过通过"的作业。

        注意：**合成判定不读它**。成片只认"每镜最新一条成功视频"（`MAX(id)`），
        签字是独立条件；把"签过 approved"混进合成选择器，会出现
        "最新那条还没签、更早签过的顶上来"→ 工作台说能合成、成片却拒收。
        """
        for job in sorted(rows, key=lambda r: (str(r["finished_at"] or ""), int(r["id"])),
                          reverse=True):
            if job["status"] == "succeeded" and (signs.get(int(job["id"])) or {}).get(
                    "decision") == "approved":
                return job
        return None

    formal_image, formal_video = _formal(image_jobs), _formal(video_jobs)
    # 与 `composition/timeline.build()` 同一个出处：最新一条成功视频作业。
    # `can_compose` 与「源已变」都读它，签字单独算。
    video_by_shot = job_repo.latest_succeeded_videos_by_shot(int(shot["episode_id"]))
    latest_video = video_by_shot.get(int(shot_id))
    signed_video = bool(latest_video is not None and (
        signs.get(int(latest_video["id"])) or {}).get("decision") == "approved")

    asset_ids = sorted(services.shot_asset_ids(shot))
    variant_ids = sorted(services.shot_variant_ids(shot))
    loaded_assets = asset_repo.get_many(asset_ids) if asset_ids else {}
    loaded_variants = variant_repo.get_many(variant_ids) if variant_ids else {}
    stale_video = bool(latest_video and services.job_source_stale(
        latest_video, shot=shot, loaded_assets=loaded_assets, loaded_variants=loaded_variants))
    import json as _json
    refs = [{
        "id": int(row["id"]), "name": row["name"], "kind": row["kind"],
        "images": len(_json.loads(row["ref_images"] or "[]")),
        "consistency_prompt": row["consistency_prompt"] or "",
        "updated_at": row["updated_at"]} for row in loaded_assets.values()]
    ref_variants = [{
        "id": int(v["id"]), "asset_id": int(v["asset_id"]),
        "name": v["name"], "variant_type": v["variant_type"],
        "images": len(_json.loads(v["ref_images"] or "[]"))
        if "ref_images" in v.keys() else 0,
        # `get_many` 给的是 sqlite3.Row，没有 `.get()`：这里必须按键取，
        # 并且要容得下"父资产已被删/不在这个项目里"。上一版写成 `.get("name", "")`，
        # 于是**任何引用了形态的镜头一打开工作台就 500**——六场景真跑时才炸出来。
        "parent_name": (loaded_assets[int(v["asset_id"])]["name"]
                        if int(v["asset_id"]) in loaded_assets else "")
        if v["asset_id"] else ""} for v in loaded_variants.values()]

    clips = [c for c in (audio_repo.list(project_id, episode_id=int(shot["episode_id"]))
                         if project_id else []) if int(c["shot_id"] or 0) == shot_id]
    audio_wired = _audio_wired()

    check = precheck.shot_precheck(shot_id)
    blockers = [i for i in check["items"] if i["level"] == precheck.BLOCK]
    compose_ready = {
        # 展示口径：有没有一条签过通过的正式视频。
        "video_ok": formal_video is not None,
        # 判定口径：最新那条成功视频签过没有（与成片取片同一条）。
        "signed_ok": signed_video,
        "source_ok": not stale_video,
        "audio_ok": all(c["status"] == "ready" for c in clips) if clips else True,
        "audio_note": ("" if not clips else
                       f"{sum(1 for c in clips if c['status'] != 'ready')} 条素材未就绪"),
        # 这一句只在**两条腿都没有分支**时才该出现；有分支就不该再说"未接入"，
        # 剩下的缺口（比如音乐默认轨没配）由 `audio_note` 那一格分开说。
        "engine_note": ("配音/音乐执行引擎尚未接入，音频段只能登记台账"
                        if not any(v["runner_wired"] for v in audio_wired.values()) else ""),
        "blockers": blockers,
        "can_compose": (latest_video is not None and signed_video
                        and not stale_video and not blockers),
    }

    next_shot = shots.next_in_episode(int(shot["episode_id"]), int(shot["seq"]))
    image_items, video_items = _job_rows(image_jobs, signs), _job_rows(video_jobs, signs)
    items_by_id = {item["job_id"]: item for item in image_items + video_items}
    return {
        "shot_id": shot_id, "project_id": project_id,
        "episode_id": int(shot["episode_id"]),
        "next_shot_id": int(next_shot["id"]) if next_shot is not None else None,
        "setting": {
            "seq": int(shot["seq"]), "summary": shot["summary"],
            "prompt": shot["prompt"], "duration": float(shot["duration"]),
            "shot_size": shot["shot_size"] if "shot_size" in shot.keys() else "",
            "camera_move": shot["camera_move"] if "camera_move" in shot.keys() else "",
            "aspect": shot["aspect"] if "aspect" in shot.keys() else "",
            "style": shot["style"] if "style" in shot.keys() else "",
            "negative_prompt": (shot["negative_prompt"]
                                if "negative_prompt" in shot.keys() else ""),
            "trim_in_ms": int(shot["trim_in_ms"]) if "trim_in_ms" in shot.keys() else 0,
            "trim_out_ms": int(shot["trim_out_ms"]) if "trim_out_ms" in shot.keys() else 0,
            "transition": (shot["transition"] if "transition" in shot.keys() else "cut") or "cut",
            "first_frame_image": (shot["first_frame_image"]
                                  if "first_frame_image" in shot.keys() else ""),
            "last_frame_image": (shot["last_frame_image"]
                                 if "last_frame_image" in shot.keys() else ""),
            "revision": int(shot["revision"])},
        "refs": {"assets": refs, "variants": ref_variants,
                 "missing_assets": [a for a in asset_ids if a not in loaded_assets],
                 "missing_variants": [v for v in variant_ids if v not in loaded_variants]},
        "keyframes": {"jobs": image_items,
                      "formal_job_id": (int(formal_image["id"]) if formal_image else None)},
        "videos": {"jobs": video_items,
                   "formal_job_id": (int(formal_video["id"]) if formal_video else None),
                   "source_stale": stale_video},
        "audio": {"clips": [{"id": int(c["id"]), "kind": c["kind"],
                             "speaker": c["speaker"] or "", "text": c["text"],
                             "status": c["status"], "duration_ms": c["duration_ms"],
                             "job_status": c["job_status"] if "job_status" in c.keys() else ""}
                            for c in clips],
                  "engines": audio_wired},
        "preview": {
            "formal_video": items_by_id[int(formal_video["id"])] if formal_video else None,
            "formal_keyframe": items_by_id[int(formal_image["id"])] if formal_image else None},
        "review": {"signs": [{"job_id": int(r["target_id"]), "decision": r["decision"],
                              "note": r["note"], "signed_at": r["signed_at"],
                              "signed_by": r["signed_by_name"] or ""}
                             for r in sign_rows],
                   # 待签列表必须是**普通 dict**：直接把 sqlite3.Row 塞进响应，
                   # FastAPI 的 JSON 序列化会在 `type_adapter.dump_json` 里炸
                   # （"Unable to serialize unknown type: sqlite3.Row"），
                   # 界面看到的就是一句 500。六场景真跑时这条是被签前那一屏撞出来的。
                   "pending": [{"job_id": int(j["id"]),
                                "prompt": j["prompt_snapshot"],
                                "finished_at": j["finished_at"]}
                               for j in video_jobs if j["status"] == "succeeded"
                               and (signs.get(int(j["id"])) or {}).get("decision")
                               != "approved"]},
        "compose_ready": compose_ready,
        "precheck": check,
    }
