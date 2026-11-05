"""盘余估算：把「这一集还差多少磁盘」从一条拍出来的阈值变成能复核的算式。

为什么要新加这一层：`DISK_MIN_FREE_GB = 5` 从写下来那天起就是拍的（《发布基线》§3
一直挂着这条开口）。它有两个说不清的地方：**5 GB 是按什么算的**，以及
**这一集到底要多少**——3 镜的一集与 60 镜的一集共用同一句话，而占用差两个数量级。

这一层不新造事实，只做三件事：

1. 从**这个项目自己已经成功的那些作业**分别量出三种典型大小：一条镜头视频、
   一张关键帧、一次成片产物。取样时把「成片作业」排除在镜头样本之外——
   成片的 capability 也是 video（provider 是 ffmpeg），混进来会把每镜平均抬高一到两个数量级。
2. 按「还差几镜 × 每镜平均（＋关键帧平均）＋一次成片的平均」算出需要量，
   乘一个写明的余量系数，与 `jobfiles.volume_usage()` 那**同一个**盘余读数放在一起。
3. 一个样本都没有就不给估算，只按固定提醒线说话，并把"为什么只能这样"写在同一句里——
   不拿别的项目、别的机器或文档里的数字顶。

`DISK_MIN_FREE_GB` 保留为地板（新库、空样本、还没跑过任何作业时只能靠它）。
预检（生成前与合成前）、产线发起检查与统计面板都读这里，不各写一份算式：
两处各算一次，迟早一处说够、一处说不够。
"""
from __future__ import annotations

import jobfiles
from config import DISK_MIN_FREE_GB
from repositories.jobs import jobs as job_repo

# 样本条数上限：估算要的是"这个项目的典型大小"，取最近几条就够，
# 不必为了一次页面打开把整个项目的产物都 stat 一遍。
SAMPLE_LIMIT = 20
# 余量系数：产物之外还有日志、临时分片与一次失败的半成品；留出 1.25 倍并把系数写出来。
SAFETY_RATIO = 1.25

MB = 1024 ** 2


def _avg(sizes: list[int]) -> int:
    return round(sum(sizes) / len(sizes)) if sizes else 0


def estimate(*, shots_missing: int, shot_video: list[int], keyframe: list[int] = (),
             compose: list[int] = (), free_bytes: int | None = None,
             min_free_gb: int = DISK_MIN_FREE_GB) -> dict:
    """三种典型大小 → 这一集还需要多少磁盘。`*_sizes` 都是**每条作业的总字节**。"""
    per = {"shot_video": {"samples": len(shot_video), "avg_bytes": _avg(shot_video)},
           "keyframe": {"samples": len(keyframe), "avg_bytes": _avg(keyframe)},
           "compose": {"samples": len(compose), "avg_bytes": _avg(compose)}}
    per = {k: v for k, v in per.items() if v["samples"]}
    missing = max(0, int(shots_missing))
    need = 0
    basis = []
    for key, item in per.items():
        if key == "compose":
            need += item["avg_bytes"]
            basis.append(f"成片平均 {item['avg_bytes'] / MB:.1f} MB（{item['samples']} 条样本）")
        else:
            need += item["avg_bytes"] * missing
            label = "每镜视频" if key == "shot_video" else "每张关键帧"
            basis.append(f"{label}平均 {item['avg_bytes'] / MB:.1f} MB×{missing}"
                         f"（{item['samples']} 条样本）")
    need = round(need * SAFETY_RATIO) if per else 0
    floor_bytes = int(min_free_gb) * 1024 ** 3
    shortfall = (max(0, max(need, floor_bytes) - free_bytes)
                 if free_bytes is not None else None)
    return {"shots_missing": missing, "per": per,
            "estimated_need_bytes": need, "safety_ratio": SAFETY_RATIO,
            "free_bytes": free_bytes, "floor_bytes": floor_bytes,
            "min_free_gb": int(min_free_gb),
            "basis": "；".join(basis) if basis else
                     f"没有可参照的产物，只按固定提醒线 {min_free_gb} GB 判",
            "shortfall_bytes": shortfall,
            "has_sample": bool(per)}


def sentence(est: dict) -> str:
    """预检、发起检查与界面共用那一句人话。"""
    if est["free_bytes"] is None:
        return "读不到作业目录所在盘的余量，发起前自己确认一下空间"
    free_gb = est["free_bytes"] / 1024 ** 3
    if not est["has_sample"]:
        if est["free_bytes"] < est["floor_bytes"]:
            return (f"作业目录只剩 {free_gb:.1f} GB，低于提醒线 {est['min_free_gb']} GB"
                    f"（{est['basis']}）")
        return f"作业目录剩 {free_gb:.1f} GB（{est['basis']}）"
    need_gb = est["estimated_need_bytes"] / 1024 ** 3
    tail = ("｜已经贴到提醒线以下"
            if est["free_bytes"] < est["floor_bytes"] else "")
    # 「还差 0 镜」不是人话：合成那一步要量的只有成片产物那一档的空间，
    # 没有待生成的镜头时就不要提镜数（提了反而像"这集还差 0 镜就能出片"的判断）。
    head = (f"还差 {est['shots_missing']} 镜，" if est["shots_missing"]
            else "只算一次成片产物，")
    return (f"{head}按本项目产物实测平均预计还要约 {need_gb:.2f} GB"
            f"（含 {est['safety_ratio']} 倍余量）；作业目录剩 {free_gb:.1f} GB"
            f"{tail}｜{est['basis']}")


def disk_readout(project_id: int, *, shots_missing: int,
                 capability: str = "video") -> dict:
    """这一集还要多少磁盘。样本只取**本项目**自己的成功作业，分三种典型大小。

    为什么住在这一层：三个调用点（生成前预检、合成前预检、产线发起检查，
    以及统计面板）说的必须是同一句话。"成片作业不能混进每镜平均"这类口径
    收在仓储的 `recent_result_paths()` 里，这里只负责汇字节数与出那句人话。
    """
    sizes = {"shot_video": [], "keyframe": [], "compose": []}
    samples = (
        ("shot_video", job_repo.recent_result_paths(
            project_id, capability=capability, shot_bound=True,
            exclude_provider="ffmpeg")),
        ("keyframe", job_repo.recent_result_paths(
            project_id, capability="image", shot_bound=True)),
        ("compose", job_repo.recent_result_paths(
            project_id, capability="video", shot_bound=False, provider="ffmpeg")))
    for kind, rows in samples:
        for raw in rows:
            total = 0
            for art in jobfiles.artifacts(raw):
                if art.get("exists") and art.get("size"):
                    total += int(art["size"])
            if total:
                sizes[kind].append(total)
    usage = jobfiles.volume_usage()
    est = estimate(shots_missing=shots_missing,
                   shot_video=sizes["shot_video"], keyframe=sizes["keyframe"],
                   compose=sizes["compose"], free_bytes=usage.free if usage else None)
    est["sentence"] = sentence(est)
    return est
