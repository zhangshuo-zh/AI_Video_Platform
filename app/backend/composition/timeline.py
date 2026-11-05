"""成片时间线：把一集的镜头与音频冻结成一份可复算的快照（阶段 D）。

这一层只读库与盘上的文件，不写任何状态；写完由 `composition.service` 在同一
事务里落成片行与 Job 行，再把快照交给 worker 去跑 FFmpeg。

三条口径：

1. **镜头顺序就是分镜的 seq**，不重排、不去重；缺号说明分镜本身有问题，
   宁可拒绝也不猜顺序。
2. **成片按素材的实际时长拼接**。分镜时长是计划值，模型产出的片子未必一样长；
   时间线里两个都记（`duration_ms` 实际／`planned_duration_ms` 计划），
   音频起点按实际累计算。计划值不拿来裁切素材——静默截掉画面或冻帧比数字
   对不上更糟；对不上就让界面把两个数并排显示。
3. **只认最新一条成功的视频作业**；失败的更新作业不会顶掉更早的成功产物
   （失败没有产物可合，拿它当"缺片"会误伤已经能用的素材）。
4. **审核通过是合成的前提**。签字绑的是那条作业的产物摘要；产物被清理或
   被新一次生成顶掉，旧签字自动失配——这里读的就是同一个版本令牌。
5. **前置检查连媒体一起验**：每个镜头文件都要 ffprobe 读得出来、有视频流、
   时长有效；读不出来的片子不进时间线，原因照原话回给发起人。
"""
from __future__ import annotations

import json

import jobfiles
from config import COMPOSE_MAX_SHOTS
from composition import validators
from models import JOB_STATUSES
from repositories.audio import clips
from repositories.content import assets, episodes, shots, variants
from repositories.jobs import jobs as job_repo
from repositories.reviews import reviews


class CompositionInvalid(Exception):
    """合成前置条件不成立。`reasons` 是一次说清的全部问题。"""

    def __init__(self, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.reasons = reasons or [message]


def _shot_review(job, signs: list, version: str) -> tuple[str, str]:
    """返回 (结论, 给人看的原因)。结论：approved / rejected / missing。

    `signs` 是这次为**这一条作业**批量取回的签字行，`version` 是它当前的产物清单
    摘要。判定只看"签的是不是这一版"，**锚点不参与筛选**——整条作业（anchor=`artifact`）
    与单独一件产物（anchor=`artifact:0`）的签字都算数。这与改成批量之前那句
    `reviews.signs(JOB, job_id, version)` 一模一样；按锚点另筛会把"只签了第一件产物"
    的历史签字突然判成没签，那是行为回归，不是优化。
    """
    current = [s for s in signs if s["base_version"] == version]
    if not current:
        return "missing", f"作业 #{job['id']} 的产物还没有审核签字"
    for sign in current:
        if sign["decision"] == "approved":
            return "approved", f"作业 #{job['id']} 的产物已通过审核"
    for sign in current:
        if sign["decision"] == "rejected":
            return "rejected", f"作业 #{job['id']} 的产物已被驳回"
    return "missing", f"作业 #{job['id']} 的产物没有有效签字"


def build(project_id: int, episode_id: int, *,
          require_approved: bool = True,
          subtitle_path: str | None = None,
          shot_jobs: dict[int, int] | None = None) -> dict:
    """生成一份时间线快照。任何前置不成立都抛 `CompositionInvalid`。

    `shot_jobs` 是"这一镜到底用哪条作业"的显式钉法（键 shot_id，值 job_id）。
    产线必须传它：产线签的是**某一条**作业的产物，而成片默认取"最新成功"——
    产线跑到一半有人手抽重生成一镜，最新成功就不是产线签过的那条了。
    传了 `shot_jobs` 就只认钉住的那几条：钉的作业没签、不存在、不属于这一镜，
    一律进 reasons 点名，不退回"最新的那条"（那正是要防的静默替换）。
    手动成片不传，保持"每镜最新成功"的口径。
    """
    episode = episodes.get(episode_id)
    if episode is None or episode["project_id"] != project_id:
        raise CompositionInvalid("这一集不属于该项目")
    shot_rows = shots.list_by_episode(episode_id)
    if not shot_rows:
        raise CompositionInvalid("这一集还没有镜头；先去创作中心把分镜建起来")
    if len(shot_rows) > COMPOSE_MAX_SHOTS:
        raise CompositionInvalid(
            f"这一集有 {len(shot_rows)} 个镜头，超过单次合成上限 "
            f"{COMPOSE_MAX_SHOTS}；请先拆分成多集")

    pinned = shot_jobs if shot_jobs is not None else None
    # "这一镜用哪条视频"只有一个出处：与分镜列表那枚「源已变」共用同一条选择器，
    # 否则界面说没事、这里却把这一镜拒掉（见 jobs.latest_succeeded_videos_by_shot）。
    jobs_by_shot = None if pinned is not None else job_repo.latest_succeeded_videos_by_shot(
        episode_id)
    reasons: list[str] = []
    timeline_shots: list[dict] = []
    total_ms = 0
    # 这一集要用到的作业、签字、被引用的资产，先各一次读齐。
    # 原来循环里逐镜 `job_repo.get()` ＋逐镜问签字 ＋判过期时逐资产回库：
    # 13 镜一集就是几十趟回库，而"发起合成"是一天点几十次的动作。
    # 读法换了，判定一个字没改（见 `_shot_review` 与 `services.job_source_stale`）。
    # 延迟导入：services 会反过来调本模块（artifact_version 与签字判定在那边）。
    from services import (artifact_version, job_source_stale, shot_asset_ids,
                          shot_variant_ids)

    if pinned is not None:
        wanted = sorted({int(v) for v in pinned.values() if v})
        jobs_by_id = job_repo.get_many(wanted, with_derived=False)
    else:
        jobs_by_id = {int(job["id"]): job for job in jobs_by_shot.values()}
        wanted = sorted(jobs_by_id)
    signs_by_job: dict[int, list] = {}
    for sign in reviews.signs_for_jobs(project_id, wanted):
        signs_by_job.setdefault(int(sign["target_id"]), []).append(sign)
    asset_ids: set[int] = set()
    variant_ids: set[int] = set()
    for shot in shot_rows:
        asset_ids |= shot_asset_ids(shot)
        variant_ids |= shot_variant_ids(shot)      # 0034：这一镜用的是哪套形态
    loaded_assets = assets.get_many(sorted(asset_ids)) if asset_ids else {}
    loaded_variants = variants.get_many(sorted(variant_ids)) if variant_ids else {}

    for shot in shot_rows:
        shot_id = int(shot["id"])
        if pinned is not None:
            job_id = pinned.get(shot_id)
            video_job = jobs_by_id.get(int(job_id)) if job_id else None
            if video_job is None:
                reasons.append(f"第 {shot['seq']} 镜没有产线签过的视频作业")
                continue
            if int(video_job["shot_id"] or 0) != shot_id:
                reasons.append(
                    f"第 {shot['seq']} 镜钉的作业 #{job_id} 不属于这一镜")
                continue
            if video_job["status"] != "succeeded":
                reasons.append(
                    f"第 {shot['seq']} 镜钉的作业 #{job_id} 状态是"
                    f"「{JOB_STATUSES.get(video_job['status'], video_job['status'])}」，不是成功")
                continue
        else:
            video_job = jobs_by_shot.get(shot_id)
        if video_job is None:
            reasons.append(f"第 {shot['seq']} 镜没有成功的视频作业")
            continue
        # 镜头行与资产都在手上了：判一次过期不该再回库读同一行、再逐个资产问一遍
        if job_source_stale(video_job, shot, loaded_assets, loaded_variants):
            reasons.append(
                f"第 {shot['seq']} 镜的素材是旧版分镜／资产生成的（源内容已变），"
                f"要重新生成并重新签字")
            continue
        try:
            result_paths = json.loads(video_job["result_paths_json"] or "[]")
        except ValueError:
            result_paths = []
        rel = str(result_paths[0]) if result_paths else ""
        target = jobfiles.resolve_artifact(rel) if rel else None
        if target is None:
            reasons.append(f"第 {shot['seq']} 镜的视频产物不在了（作业 #{video_job['id']}）")
            continue
        verdict, why = _shot_review(
            video_job, signs_by_job.get(int(video_job["id"]), []),
            artifact_version(video_job))
        if require_approved and verdict != "approved":
            reasons.append(f"第 {shot['seq']} 镜{why}")
            continue
        # 实际时长从文件读，不拿分镜的计划时长顶替：合成出来的总长必须
        # 和素材对得上，否则验收那道 8% 容差永远过不去（或者要静默裁切）。
        try:
            info = validators.inspect(target)
        except validators.MediaInvalid as exc:
            reasons.append(f"第 {shot['seq']} 镜的视频读不出来：{exc.message}")
            continue
        if not info["has_video"]:
            reasons.append(f"第 {shot['seq']} 镜的文件里没有视频流")
            continue
        duration_ms = int(info["duration_ms"] or 0)
        if duration_ms < 100:
            reasons.append(f"第 {shot['seq']} 镜的视频时长无效（{duration_ms}ms）")
            continue
        planned_ms = max(0, int(round(float(shot["duration"] or 0) * 1000)))
        total_ms += duration_ms
        timeline_shots.append({
            "shot_id": shot_id,
            "seq": int(shot["seq"]),
            "summary": shot["summary"],
            "duration_ms": duration_ms,
            "planned_duration_ms": planned_ms,
            "video": {
                "job_id": int(video_job["id"]),
                "path": rel,
                "bytes": info["bytes"],
                "width": info["width"],
                "height": info["height"],
                "review": verdict,
                "review_note": why,
            },
        })

    # 这里**不提前抛**：音频素材缺失与镜头缺片是同一次发起的两半，
    # 分两次说等于让人改完一轮再撞第二轮。攒到下面一起报。
    audio_rows = clips.list(project_id, None, episode_id)
    audio: list[dict] = []
    starts = _shot_starts(timeline_shots)
    for row in audio_rows:
        if row["status"] != "ready":
            continue
        target = jobfiles.resolve_artifact(row["path"] or "")
        if target is None:
            reasons.append(f"音频素材 #{row['id']} 已就绪，但文件不在了")
            continue
        start_ms = 0 if row["kind"] == "music" else starts.get(row["shot_id"], 0)
        audio.append({
            "clip_id": int(row["id"]),
            "kind": row["kind"],
            "speaker": row["speaker"],
            "path": row["path"],
            "start_ms": start_ms,
            "duration_ms": row["duration_ms"],
            "bytes": target.stat().st_size,
        })

    if reasons:
        raise CompositionInvalid("合成前检查没过：", reasons)

    subtitles = []
    if subtitle_path:
        # 两个来源：`subtitles/` 开头是成片页上传的字幕（只认本项目那一份），
        # 其余按 Job 产物解析（历史上字幕可能跟着某条作业进来）。
        target = (jobfiles.resolve_subtitle(project_id, subtitle_path)
                  if str(subtitle_path).startswith("subtitles/")
                  else jobfiles.resolve_artifact(subtitle_path))
        if target is None:
            raise CompositionInvalid(f"字幕文件不在了或不属于这个项目：{subtitle_path}")
        # 先问这台机器能不能烧，再决定要不要排 Job：不带 libass 的 FFmpeg 连
        # `subtitles` 滤镜都没有，排下去只会得到一条失败作业和一肚子垃圾日志。
        if not validators.subtitle_filter_available():
            raise CompositionInvalid(
                "这台机器的 FFmpeg 没有 subtitles 滤镜，烧不了字幕；"
                "要装带 libass 的构建（如 `brew install ffmpeg --with-libass` "
                "或换 johnvansickle 静态包）后再发起")
        subtitles.append({"path": subtitle_path, "bytes": target.stat().st_size})

    return {
        "schema": 1,
        "project_id": int(project_id),
        "episode": {
            "id": int(episode_id),
            "seq": int(episode["seq"]),
            "title": episode["title"],
        },
        "shots": timeline_shots,
        "audio": audio,
        "subtitles": subtitles,
        "total_duration_ms": total_ms,
        # 不填分辨率时合成按它出（见 `majority_size`）；快照一起冻住，事后能复盘
        # "这一版当时是按哪一档尺寸拼的"
        "output_size": majority_size(timeline_shots),
    }


def majority_size(shot_rows: list[dict]) -> dict | None:
    """素材里最常见的那个宽高——**不填分辨率时成片就按它出**，不放大。

    为什么默认要跟着素材走：合成执行器原来写死 1280×720（`COMPOSE_WIDTH/HEIGHT`），
    而本机预览档 FastMetal 真跑出来的一镜是 640×352。2026-11-05 整集连跑量到：
    六条 640×352 的素材拼出来的是 1280×720 的成片、5,622,530 字节——
    等于每次合成都把画面拉大一倍再存一份，**画质不会多一个像素，字节与耗时倒是实打实涨**。
    所以默认改成"与素材同尺寸"；要更大就由人显式填 `width`/`height`。
    取众数而不是取第一镜：一集里混了不同尺寸的素材时，取第一镜会让整集跟着那一镜走。
    """
    counts: dict[tuple[int, int], int] = {}
    for row in shot_rows:
        video = row.get("video") or {}
        width, height = video.get("width"), video.get("height")
        if width and height:
            counts[(int(width), int(height))] = counts.get((int(width), int(height)), 0) + 1
    if not counts:
        return None
    (width, height), hits = max(counts.items(), key=lambda kv: (kv[1], -kv[0][0]))
    return {"width": width, "height": height, "clips": len(counts),
            "same_as": hits, "of": sum(counts.values())}


def _shot_starts(rows: list[dict]) -> dict[int, int]:
    """台词从所属镜头的起点开始。没有镜头关联的素材从 0 开始。"""
    starts: dict[int, int] = {}
    cursor = 0
    for row in rows:
        starts[int(row["shot_id"])] = cursor
        cursor += int(row["duration_ms"])
    return starts
