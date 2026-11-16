"""production impact 业务域。"""
from __future__ import annotations
from repositories.content import shots
from repositories.production import production



def referencing_shots(asset_id: int) -> list:
    """引用这条资产的镜头行（含形态引用）。给路由做可见性过滤用，别自己拼 SQL。"""
    return production.referencing_shot_rows(asset_id)



def asset_impact(asset_id: int, *, only_shot_ids: set[int] | None = None) -> dict:
    """资产改动会影响哪些镜头与哪些下游产物（任务书 §6.3、场景三）。

    已有的 `GET /api/assets/{id}/shots` 只回答"谁引用了它"；这里再往前走一步，
    把每条引用镜头的**最新正式视频是否因此判过期**一起给出——
    改一次形态却不知道要重跑哪几镜，是这平台最容易让人白花钱的地方。

    `only_shot_ids` 是给调用方的可见性闸：跨项目引用查得到，但"看不见就是不存在"
    这条规格不能因为多了一个聚合接口就被绕过。传 None＝不过滤（内部调用）。
    """
    import services
    from repositories.jobs import jobs as job_repo
    from repositories.reviews import reviews
    from repositories.content import assets as asset_repo, variants as variant_repo

    row = asset_repo.get(asset_id)
    if row is None:
        return {"asset_id": int(asset_id), "error": "资产不存在"}

    # 直接引用与形态引用一次查齐（改一套衣服也是改动，漏了它就等于没做这件事）
    hits = {int(r["shot_id"]): {"shot_id": int(r["shot_id"]), "seq": int(r["seq"]),
                                "episode_id": int(r["episode_id"]),
                                "project_id": int(r["project_id"])}
            for r in production.referencing_shot_rows(asset_id)}
    if only_shot_ids is not None:
        hits = {k: v for k, v in hits.items() if k in only_shot_ids}

    latest_rows = job_repo.latest_succeeded_videos_by_episodes(
        sorted({h["episode_id"] for h in hits.values()}))
    latest = {int(sid): dict(job) for sid, job in latest_rows.items() if sid in hits}

    # 循环外两次批量读完：镜头行一次 IN、签字一次 IN。
    # 原来这两句在循环里逐条 `shots.get()`／`reviews.signs()`——600 资产／1 500 镜头
    # 那一档量到的是**回库次数随引用镜头数增长**（18→54），正是规模基准要抓的那种。
    shot_rows = shots.get_many(list(hits))
    sign_rows = reviews.signs_by_targets("job", [int(j["id"]) for j in latest.values()])
    fingerprinted = [shot for sid, shot in shot_rows.items()
                     if sid in latest and latest[sid]["source_version"]]
    asset_ids = set().union(*(services.shot_asset_ids(s) for s in fingerprinted))
    variant_ids = set().union(*(services.shot_variant_ids(s) for s in fingerprinted))
    loaded_assets = asset_repo.get_many(sorted(asset_ids)) if asset_ids else {}
    loaded_variants = variant_repo.get_many(sorted(variant_ids)) if variant_ids else {}
    items = []
    for hit in sorted(hits.values(), key=lambda h: (h["episode_id"], h["seq"])):
        job = latest.get(hit["shot_id"])
        stale = False
        signed = False
        if job is not None:
            shot = shot_rows.get(hit["shot_id"])
            stale = bool(shot is not None and services.job_source_stale(
                job, shot=shot, loaded_assets=loaded_assets, loaded_variants=loaded_variants))
            signed = any(s["decision"] == "approved"
                         for s in sign_rows.get(int(job["id"]), []))
        items.append({**hit, "job_id": int(job["id"]) if job is not None else None,
                      "stale": stale, "signed": signed,
                      "recommend": ("源已变，建议重新生成这一镜" if stale else
                                    "产物仍可用" if job is not None else "还没生成过")})
    return {"asset_id": asset_id, "name": row["name"], "kind": row["kind"],
            "updated_at": row["updated_at"], "shots": items,
            "counts": {"referencing": len(items),
                       "stale": sum(1 for i in items if i["stale"]),
                       "never_generated": sum(1 for i in items
                                              if i["job_id"] is None)}}
