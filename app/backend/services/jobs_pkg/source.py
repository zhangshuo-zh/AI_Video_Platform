"""作业服务 source 域。"""
from repositories.content import assets, shots, variants
from repositories.jobs import jobs as job_repo
import hashlib
import json





def shot_source_version(shot, loaded_assets: dict | None = None,
                        loaded_variants: dict | None = None) -> str:
    """这一镜"源内容"的指纹：内容、提示词、时长、引用资产及资产的版本信息。

    为什么不覆盖模型与参数：换模型不改变"这一镜讲什么、引用谁"，那是 Job 参数
    快照与成本账要回答的问题；把模型塞进指纹会让"换条便宜的轨"变成"素材过期"，
    反而逼人重跑一遍已经能用的片子。

    资产那边用 `updated_at`＋一致性提示词＋参考图清单：参考图被换掉、定妆描述被改，
    都会让这一镜的画面依据变掉，旧视频就不该还挂着"已签"。

    0034 起再叠一层**形态**（这一镜用的是哪套衣服）：形态的描述与图同样是画面依据，
    改了形态而不让旧产物过期，等于承认"改衣服不用重跑"——那正是任务书 §6.3
    要防的"仅修改服装却意外改变人物身份"里的前半段。

    `loaded_assets`／`loaded_variants` 是给"一集几十镜一起算"的调用方留的口子
    （分镜列表、生产前检查）：它们本来就要把这一集的资产读一遍，逐镜再 `get()`
    会把一次列表打成几十次回库。传了就只查这一份字典，没传就自己回库。
    """
    try:
        ids = [int(x) for x in json.loads(shot["asset_ids"] or "[]")]
    except (TypeError, ValueError):
        ids = []
    try:
        # 老库读出来的行可能还没有这一列（0034 未跑到的测试夹具）：按空清单处理，
        # 不抛——指纹要能算，"没有形态引用"本来就是空清单的意思。
        vids = ([int(x) for x in json.loads(shot["variant_ids"] or "[]")]
                if "variant_ids" in shot.keys() else [])
    except (TypeError, ValueError):
        vids = []
    parts = {
        "summary": shot["summary"],
        "prompt": shot["prompt"],
        "duration": round(float(shot["duration"] or 0), 3),
        "assets": [],
        "variants": [],
    }
    if ids and loaded_assets is None:
        loaded_assets = assets.get_many(ids)
    for asset_id in ids:
        row = (loaded_assets or {}).get(asset_id)
        parts["assets"].append({
            "id": asset_id,
            "updated_at": row["updated_at"] if row else "",
            "consistency_prompt": row["consistency_prompt"] if row else "",
            "ref_images": row["ref_images"] if row else "",
        })
    if vids and loaded_variants is None:
        loaded_variants = variants.get_many(vids)
    for variant_id in vids:
        row = (loaded_variants or {}).get(variant_id)
        parts["variants"].append({
            "id": variant_id,
            "updated_at": row["updated_at"] if row else "",
            "prompt": row["prompt"] if row else "",
            "ref_images": row["ref_images"] if row else "",
        })
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]





def shots_view(episode_id: int) -> list[dict]:
    """分镜列表 + 每镜「成片会用到的那条视频作业是否基于旧源内容」。

    标记放在服务端算：前端拿不到分镜源指纹（它需要读引用资产的版本信息），
    让浏览器自己判断等于把口径复制一份到 JS 里。

    回库次数是算过的：作业一条、分镜一条、这一集引用到的资产一条，共三次。
    逐镜 `shots.get()` ＋逐资产 `assets.get()` 的老写法在 13 镜一集上是 20+ 次，
    而创作中心每次打开一集都要走这条路。
    """
    rows = shots.list_by_episode(episode_id)
    latest = job_repo.latest_succeeded_videos_by_shot(episode_id)
    asset_ids: set[int] = set()
    variant_ids: set[int] = set()
    for row in rows:
        if int(row["id"]) not in latest:
            continue                      # 没有可用视频作业的镜头不判过期，省掉一批资产读
        asset_ids |= shot_asset_ids(row)
        variant_ids |= shot_variant_ids(row)
    loaded = assets.get_many(sorted(asset_ids)) if asset_ids else {}
    loaded_variants = variants.get_many(sorted(variant_ids)) if variant_ids else {}
    out = []
    for row in rows:
        job = latest.get(int(row["id"]))
        stale = bool(job and job_source_stale(
            job, shot=row, loaded_assets=loaded, loaded_variants=loaded_variants))
        out.append({"shot": row, "source_stale": stale})
    return out





def shot_asset_ids(shot) -> set[int]:
    """镜头行里的 `asset_ids`（JSON 文本）→ 整数集合。坏数据当空清单。"""
    try:
        return {int(x) for x in json.loads(shot["asset_ids"] or "[]")}
    except (TypeError, ValueError):
        return set()





def shot_variant_ids(shot) -> set[int]:
    """同上，读 `variant_ids`（0034）。列还不存在时当空清单，不抛。"""
    try:
        if "variant_ids" not in shot.keys():
            return set()
        return {int(x) for x in json.loads(shot["variant_ids"] or "[]")}
    except (TypeError, ValueError):
        return set()





def job_source_stale(job, shot=None, loaded_assets: dict | None = None,
                     loaded_variants: dict | None = None) -> bool:
    """这条作业的产物是不是基于**旧版**分镜／资产生的。

    没有 `source_version` 的作业（0031 之前落的）一律不判过期：那时候平台确实
    没记过源内容，硬判成过期会把历史作业全打成废纸，也会让已经签过字的片子凭空失效。

    **0034 加形态列之后，旧作业的指纹仍然对不上**——那不是 bug，是事实：
    那些产物确实是在"镜头没有形态引用"的世界里生的，而这一镜现在指到了某个形态上，
    画面依据变了。要保住旧产物就得保住它当时的形态引用，而当时没记（记在
    `source_version` 那个哈希里，反推不出来）。所以这里照实标过期，
    由人决定重跑还是接受，而不是替平台编一个"应该还是可用的"。

    `shot`／`loaded_assets`／`loaded_variants` 是同一次列表里已经在手的行；
    不传就自己回库（单条检查那条路）。
    """
    keys = job.keys()
    version = job["source_version"] if "source_version" in keys else ""
    if not version:
        return False
    shot_id = job["shot_id"] if "shot_id" in keys else None
    if not shot_id:
        return False
    if shot is None:
        shot = shots.get(int(shot_id))
    if shot is None:
        return False
    return shot_source_version(shot, loaded_assets, loaded_variants) != version
