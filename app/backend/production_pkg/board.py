"""production board 业务域。"""
from __future__ import annotations
from repositories.content import episodes
from repositories.production import production
from .state import DOING
from .state import DONE
from .state import FAILED
from .state import NEEDS
from .state import PENDING
from .audio import _audio_stage_note
from .audio import _audio_wired
from .state import _stage




def episode_board(project_id: int, episode_id: int, *, include_shot_tasks: bool = False) -> dict:
    """一集的生产总览：九阶段 ＋ 阻塞 ＋ 下一步。"""
    episode = episodes.get(episode_id)
    if episode is None or int(episode["project_id"]) != int(project_id):
        return {"episode_id": episode_id, "stages": [], "next_tasks": [],
                "error": "这一集不属于该项目"}

    docs = production.documents_by_kind(episode_id)
    doc_total = sum(docs.values())
    runs = production.extraction_runs(episode_id)
    gaps_assets = production.referenced_asset_gaps(episode_id)
    gaps_shots = production.shot_gaps(episode_id)
    jobs = production.job_status_by_shot(episode_id)
    no_video = production.shots_without_video(episode_id)
    unsigned = production.unsigned_succeeded_videos(episode_id)
    attention = production.attention_jobs(episode_id)
    audio = production.audio_status(episode_id)
    no_dialogue = production.shots_without_dialogue(episode_id)
    comp = production.composition_delivery(episode_id)
    audio_wired = _audio_wired()

    total_shots = gaps_shots["total"]

    # 1) 剧本创作：有文档就算起步，有剧本正文才算能往下走
    script_done = 1 if doc_total else 0
    stages = [_stage("script", status=DONE if script_done else PENDING,
                     done=script_done, total=1,
                     note=("、".join(f"{k} {v}" for k, v in sorted(docs.items()))
                           if doc_total else "这一集还没有文档"))]

    # 2) 内容解析：候选没处置完就是"需要处理"；一次都没跑过是"待处理"
    if runs["runs"] == 0:
        parse = _stage("parse", status=PENDING, total=0,
                       note="先有剧本再解析")
    elif runs["pending"]:
        parse = _stage("parse", status=NEEDS, total=runs["items"],
                       pending=runs["pending"],
                       done=runs["items"] - runs["pending"],
                       note=f"{runs['pending']} 条候选待处置")
    else:
        parse = _stage("parse", status=DONE, total=runs["items"],
                       done=runs["items"], note="候选已全部处置")
    stages.append(parse)

    # 3) 资产准备：按"这一集引用到的资产"算，不是按项目资产池算
    blocked_assets = gaps_assets["shots_with_missing_asset"]
    need_asset = gaps_assets["no_ref_image"] + gaps_assets["no_consistency"]
    stages.append(_stage(
        "assets",
        status=(FAILED if blocked_assets else NEEDS if need_asset
                else DONE if gaps_assets["total"] else PENDING),
        done=max(gaps_assets["total"] - need_asset, 0),
        total=gaps_assets["total"], pending=need_asset, blocked=blocked_assets,
        note=(f"{blocked_assets} 镜引用了不存在的资产" if blocked_assets
              else f"{gaps_assets['no_ref_image']} 个引用资产缺参考图"
                   if need_asset else "引用到的资产都齐")))

    # 4) 分镜制作
    stages.append(_stage(
        "storyboard",
        status=(PENDING if total_shots == 0 else
                NEEDS if (gaps_shots["no_prompt"] or gaps_shots["no_refs"]) else DONE),
        done=total_shots - max(gaps_shots["no_prompt"], gaps_shots["no_refs"]),
        total=total_shots,
        pending=gaps_shots["no_prompt"] + gaps_shots["no_refs"],
        note=(f"{gaps_shots['no_prompt']} 镜没提示词、"
              f"{gaps_shots['no_refs']} 镜没引用资产"
              if total_shots else "还没有镜头")))

    # 5) 视频生成：以"镜头"为分母，不是以作业为分母
    video_jobs = jobs.get("video", {})
    succeeded = video_jobs.get("succeeded", {}).get("shots", 0)
    failed = video_jobs.get("failed", {}).get("shots", 0)
    running = sum(video_jobs.get(s, {}).get("shots", 0)
                  for s in ("queued", "starting", "running", "cancel_requested"))
    video_status = (PENDING if total_shots == 0 else
                    FAILED if failed and succeeded + failed >= total_shots and succeeded < total_shots
                    else DONE if succeeded >= total_shots and total_shots else
                    DOING if running or succeeded else NEEDS)
    stages.append(_stage(
        "video", status=video_status, done=succeeded, total=total_shots,
        pending=max(total_shots - succeeded - failed, 0), failed=failed,
        note=(f"{len(no_video)} 镜还没有成功的视频作业"
              f"{'，' + str(running) + ' 条在队列里' if running else ''}"
              if total_shots else "先有分镜再生成")))

    # 6) 音频制作：这一句必须由 `_audio_wired()` 三处事实现算，不能写死。
    #
    # 曾经这里是硬编码的"执行引擎尚未接入，只能登记台账"——它在 2026-11-05 09:39
    # 写下来时是对的，但当天 10:37 配音腿就接线了（`runners._voxcpm`），音乐腿
    # 也有分支（`runners._ace_step`）。于是这句话变成了一句**对用户说的假话**：
    # 明明能提交配音，界面却在劝他"只能登记台账"。同一屏的 `audio_status_view()`
    # 早就按事实算了（配音 can_submit=True、音乐因默认轨没配而 False），
    # 只有这条 note 还停在旧世界——**一个函数里两种口径**正是最坏的那种不一致。
    audio_total = sum(sum(v.values()) for v in audio.values())
    audio_note = _audio_stage_note(audio_wired)
    stages.append(_stage(
        "audio",
        status=NEEDS if (no_dialogue or audio_total == 0) else PENDING,
        done=audio_total, total=max(total_shots, audio_total),
        pending=no_dialogue, blocked=0,
        note=(f"{no_dialogue} 镜还没有台词/旁白素材；{audio_note}"
              if total_shots else audio_note),
        entry={"module": "audio", "sub": 0, "action": ""}))

    # 7) 审核验收：签过字的镜头数 / 有成功视频的镜头数
    review_total = succeeded
    review_pending = len(unsigned)
    stages.append(_stage(
        "review",
        status=(PENDING if review_total == 0 else
                NEEDS if review_pending or attention else DONE),
        done=max(review_total - review_pending, 0), total=review_total,
        pending=review_pending, blocked=attention,
        note=(f"{review_pending} 条视频待签字"
              + (f"；{attention} 条账单待核对" if attention else "")
              if review_total else "还没有可审的产物")))

    # 8) 成片合成
    comp_status = (PENDING if comp["versions"] == 0 else
                   FAILED if comp["latest_status"] == "failed" else
                   DONE if comp["latest_status"] == "succeeded" else DOING)
    stages.append(_stage(
        "compose", status=comp_status,
        done=1 if comp["latest_status"] == "succeeded" else 0,
        total=1, failed=1 if comp["latest_status"] == "failed" else 0,
        note=(f"最新 v{comp['latest_version']}：{comp['latest_status'] or '未跑'}"
              if comp["versions"] else "还没合成过")))

    # 9) 最终交付：成片成功且那条合成作业签过字
    delivered = comp["latest_status"] == "succeeded" and comp["signed"] > 0
    stages.append(_stage(
        "deliver", status=DONE if delivered else (
            NEEDS if comp["versions"] else PENDING),
        done=1 if delivered else 0, total=1,
        note=("成片已签字" if delivered else
              "成片已合成，等产物审核签字" if comp["versions"] else "先合成再交付")))

    return {"episode_id": episode_id, "episode_seq": int(episode["seq"]),
            "episode_title": episode["title"] or "",
            "shots": total_shots, "stages": stages,
            "next_tasks": next_tasks(stages),
            "shot_tasks": _shot_tasks(project_id, episode_id) if include_shot_tasks else [],
            "counts": {"documents": doc_total, "extraction_pending": runs["pending"],
                       "assets_referenced": gaps_assets["total"],
                       "videos_succeeded": succeeded, "videos_failed": failed,
                       "unsigned": review_pending, "attention": attention,
                       "composition_versions": comp["versions"]}}



def _shot_tasks(project_id: int, episode_id: int) -> list[dict]:
    """按分镜顺序给出可连续处理的素材、视频审核、返工和掉队作业。"""
    import jobfiles
    from repositories.jobs import jobs as job_repo
    from repositories.reviews import reviews
    from services.artifact_review import artifact_version_of_manifest
    from services.content_units import asset_preparation

    prep = asset_preparation(episode_id, project_id)
    asset_images = {a["id"]: a["image_count"] for a in prep["assets"]}
    variants = {v["id"]: v for v in prep["variants"]}
    missing_purposes = {item["seq"] for item in prep["missing_purposes"]}
    latest = {sid: job for sid, job in job_repo.latest_jobs_by_shot(episode_id).items()
              if job["project_id"] == project_id}
    succeeded = [job for job in latest.values() if job["status"] == "succeeded"]
    manifests = dict(zip((job["id"] for job in succeeded),
                         jobfiles.artifacts_many([job["result_paths_json"] for job in succeeded]),
                         strict=True))
    signatures = reviews.signs_by_targets(reviews.JOB, set(manifests))
    tasks: list[dict] = []
    for shot in prep["shots"]:
        base = {"shot_id": shot["id"], "seq": shot["seq"]}
        selected = [variants[vid] for vid in shot["variant_ids"] if vid in variants]
        selected_parents = {v["asset_id"] for v in selected}
        missing_asset = (len(selected) < len(shot["variant_ids"])
                         or any(aid not in asset_images for aid in shot["asset_ids"]))
        missing_image = (any(not v["image_count"] for v in selected)
                         or any(not asset_images.get(aid) for aid in shot["asset_ids"]
                                if aid not in selected_parents))
        if missing_asset or missing_image or shot["seq"] in missing_purposes:
            note = "引用资产或形态不可用" if missing_asset else (
                "缺指定用途的参考图" if shot["seq"] in missing_purposes else "缺参考图")
            tasks.append({**base, "group": "assets", "note": note})

        job = latest.get(shot["id"])
        if job is None:
            continue
        status = job["status"]
        jid = job["id"]
        source = job["rework_of_job_id"]
        payload = {**base, "job_id": jid, "source_job_id": source}
        if status in ("failed", "cancelled", "needs_attention"):
            note = {"failed": "最新生成失败", "cancelled": "最新生成已取消",
                    "needs_attention": "最新作业待人工核对"}[status]
            tasks.append({**payload, "group": "failed", "note": note})
        elif status == "succeeded":
            manifest = manifests[jid]
            if not any(item["exists"] for item in manifest):
                tasks.append({**payload, "group": "failed", "note": "最新作业产物不可读"})
                continue
            version = artifact_version_of_manifest(manifest)
            decisions = {sig["decision"] for sig in signatures.get(jid, [])
                         if sig["base_version"] == version}
            if "approved" in decisions:
                continue
            if decisions & {"returned", "rejected"}:
                tasks.append({**payload, "group": "rework", "note": "版本待返工", "can_review": True})
            elif source is not None:
                tasks.append({**payload, "group": "rework", "note": "返工新版待审",
                              "can_review": True, "replacement_ready": True})
            else:
                tasks.append({**payload, "group": "review", "note": "最新视频待审", "can_review": True})
        elif source is not None:
            tasks.append({**payload, "group": "rework", "note": "返工作业进行中"})
    return tasks


def next_tasks(stages: list[dict]) -> list[dict]:
    """把阶段里真实的缺口翻译成"下一步做什么"。

    排序按严重度：阻塞 → 失败 → 待处理数量 → 已完成的不列。
    每条都带 `stage` 与入口，界面点一下就跳到该做事的页面；
    没有缺口时返回空清单，界面写"这一集没有待办"，不编一条假任务。
    """
    out = []
    for st in stages:
        if st["status"] in (DONE, PENDING) and not st["blocked"] and not st["failed"]:
            continue
        if st["key"] == "audio" and not st.get("entry", {}).get("action", ""):
            # 没接线的阶段不进"下一步"，只在阶段表里说明原因
            continue
        count = st["blocked"] or st["failed"] or st["pending"]
        out.append({"stage": st["key"], "label": st["label"],
                    "count": count, "status": st["status"],
                    "note": st["note"], "entry": st["entry"]})
    order = {FAILED: 0, NEEDS: 1, DOING: 2}
    out.sort(key=lambda x: (order.get(x["status"], 3), -int(x["count"] or 0)))
    return out



def project_overview(project_id: int, *, current_episode_id: int | None = None) -> dict:
    """项目层概览：几集、几镜、进度分布、当前制作集。

    每集的九阶段不在这里逐集算（一季 20 集就是 20 次聚合读）；
    项目层只给"每集走到哪一步"的一个粗粒度结论，点开某一集才算细的。
    """
    rows = episodes.list_by_project(project_id)
    progress = production.episode_progress(project_id)
    per_episode = [{
        "episode_id": int(ep["id"]), "seq": int(ep["seq"]),
        "title": ep["title"] or "", "status": ep["status"],
        **progress.get(int(ep["id"]), {"shots": 0, "videos_succeeded": 0,
                                      "composition_versions": 0, "delivered": False})}
        for ep in rows]
    total_eps = len(per_episode)
    delivered = sum(1 for e in per_episode if e["delivered"])
    current = next((e for e in per_episode
                    if current_episode_id and e["episode_id"] == int(current_episode_id)),
                   None)
    return {"project_id": int(project_id), "episodes": total_eps,
            "delivered_episodes": delivered,
            "progress": round(delivered / total_eps * 100, 1) if total_eps else 0.0,
            "current": current, "per_episode": per_episode,
            "assets": production.asset_gaps(int(project_id))}
