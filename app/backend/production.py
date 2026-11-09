"""项目生产工作台的业务层：九个生产阶段、进度、阻塞与下一步任务。

一句话职责边界：**这里只算数，不改状态**。所有数字都从库里现读，
所有"下一步"都是从真实缺口算出来的；不写任何表、不建作业、不碰签字。
所以它可以被顶栏、工作台、脚本反复调用，成本只有一次聚合读。

三条设计决定，都是为了让这一层不至于变成第二个事实来源：

1. **阶段不是状态机**。任务书 §4.2 明说"阶段之间不能简单采用固定顺序强制锁定"，
   所以 `STAGES` 只是一张展示顺序表，`stage.status` 由数据算出，
   上游没做完也不拦下游（先建资产再写剧本同样合法）。真正会拦人的只有
   `precheck`（提交与合成那两道），那是另一回事。
2. **数字只有一个出处**。镜头数走 `shot_gaps()`，视频数走 `job_status_by_shot()`，
   签字数走 `unsigned_succeeded_videos()`，成片走 `composition_delivery()`；
   这里不重抄任何一条 SQL 口径，也不自己数 `len(list)` 之外的东西。
3. **没接线的东西不许给动作**。配音／音乐的执行器在 `runners.build()` 里没有分支，
   口型的 Rhubarb 也没接。所以"音频制作"这一阶段的状态可以是"需要处理"，
   但 `entry.action` 一律为空，并且 `note` 直接写"执行引擎尚未接入"——
   给一个点了必失败的按钮，比不写这一阶段更糟。
"""
from __future__ import annotations

import subprocess
from pathlib import Path


import adapters
import diskplan
import error_catalog
from repositories.content import episodes, shots
from repositories.jobs import jobs as job_repo
from repositories.production import production
from repositories.reviews import reviews

# 对外五档状态（任务书 §12.3）。深层细节仍由作业状态原码给出，这里只做展示层。
PENDING = "待处理"
DOING = "进行中"
NEEDS = "需要处理"
DONE = "已完成"
FAILED = "失败"

# 对外五档的清单，接口随返回值带一份：界面拿它做筛选下拉，
# 而不是在 JS 里再抄一遍——抄一份就会有两套词（任务书 §12.3）。
PUBLIC_STATUSES = [PENDING, DOING, NEEDS, DONE, FAILED]

# 展示顺序＝推荐顺序，不是强制顺序。entry 是"点这一阶段去哪个页面"，
# 由 modules.js 的模块 id ＋二级页下标组成，前端直接用它拼 #module/sub。
STAGES = [
    {"key": "script", "label": "剧本创作", "entry": {"module": "writing", "sub": 0}},
    {"key": "parse", "label": "内容解析", "entry": {"module": "writing", "sub": 0}},
    {"key": "assets", "label": "资产准备", "entry": {"module": "assets", "sub": 0}},
    {"key": "storyboard", "label": "分镜制作", "entry": {"module": "writing", "sub": 2}},
    {"key": "video", "label": "视频生成", "entry": {"module": "generate", "sub": 0}},
    {"key": "audio", "label": "音频制作", "entry": {"module": "audio", "sub": 0}},
    {"key": "review", "label": "审核验收", "entry": {"module": "review", "sub": 1}},
    {"key": "compose", "label": "成片合成", "entry": {"module": "composition", "sub": 0}},
    {"key": "deliver", "label": "最终交付", "entry": {"module": "composition", "sub": 0}},
]


def _stage(key: str, **fields) -> dict:
    """把 STAGES 里的展示信息与算出来的数字合成一行阶段。"""
    meta = next(s for s in STAGES if s["key"] == key)
    out = {"key": key, "label": meta["label"], "entry": dict(meta["entry"]),
           "status": PENDING, "done": 0, "total": 0, "pending": 0, "failed": 0,
           "blocked": 0, "note": ""}
    out.update(fields)
    return out


def _audio_wired() -> dict:
    """配音／音乐到底能不能真跑。只用两个既有事实：

    1. `services.resolve_provider()` 能不能解析出默认轨，以及那一轨的健康状态；
    2. 该 provider 在 `runners.build()` 里有没有分支——问 `runners.has_branch()`，
       它与 `build()` 读同一串条件，所以这张表不会在接线之后还写着"没接线"。
    """
    import services

    out = {}
    for capability, label in (("tts", "配音"), ("music", "音乐")):
        provider, health = "", {"state": "unconfigured", "detail": "默认轨没配"}
        try:
            resolved = services.resolve_provider(capability)
            provider = resolved["provider"]
            # 项目概览是**读**接口（工作台一次读取要给「跑到哪＋花了多少」），
            # 不能因为某台服务半死就把整页拖住。要现查走模型中心的显式入口。
            health = adapters.health(resolved["row"], probe_service=False)
        except services.NoRoute as exc:
            health = {"state": "unconfigured", "detail": str(exc)}
        # `runner_wired` 问 `runners.has_branch()`：这里曾经写死 False，
        # 而写死的那个值在接线当天就会变成假话——能力表必须跟着代码走。
        import runners
        out[capability] = {"label": label, "provider": provider,
                           "health": health,
                           "runner_wired": runners.has_branch(capability, provider)}
    return out


def audio_status_view() -> dict:
    """音频这一腿的真实能力表（任务书 §6.1 第五阶段、§15 P1「音频能力状态透明化」）。

    为什么要服务端算这张表而不是前端写死四行：这一页上"能不能提交"以前是**看按钮存不存在**
    推断出来的，而真相分散在三处——清单里有没有这个 capability、默认轨解析不解析得出、
    `runners.build()` 有没有这一条分支。三处任意一处变了，前端那份写死的话就成了假话。
    所以这里只读那三处已有的事实，一条都不新造。

    四类（配音／旁白走 tts，音乐走 music）之外还回答**口型与音效**：
    它们今天连 capability 都没有，透明化的意思是把"没有"这件事也摆到同一张表上，
    而不是让人在界面上找了半天才发现这功能不存在。
    """

    wired = _audio_wired()

    def row(code, label, capability, engine, *, can_register=True):
        entry = wired.get(capability) or {}
        health = entry.get("health") or {}
        runner_wired = bool(entry.get("runner_wired"))
        missing = []
        if not entry:
            missing.append("清单里没有这个能力的适配器")
        else:
            if not entry.get("provider"):
                missing.append("这一类能力的默认轨没配")
            if health.get("state") not in ("ok",):
                missing.append(health.get("detail") or "运行时或权重不齐")
            if not runner_wired:
                missing.append("`runners.build()` 没有这条分支（执行未接线）")
        return {
            "code": code, "label": label, "capability": capability, "engine": engine,
            "provider": entry.get("provider") or "",
            "health_state": health.get("state") or "absent",
            "health_detail": health.get("detail") or "",
            "runner_wired": runner_wired,
            "can_submit": runner_wired and bool(entry.get("provider"))
                          and health.get("state") == "ok",
            # 登记台账是纯库内动作，与执行接没接线无关——台词该先记下来就记下来
            "can_register": can_register,
            "missing": missing,
            "why": "可以提交执行" if runner_wired and health.get("state") == "ok" else
                   ("；".join(missing) if missing else "现在还不能提交"),
        }

    rows = [
        row("dialogue", "配音（台词）", "tts", "VoxCPM2"),
        row("narration", "旁白", "tts", "VoxCPM2"),
        row("music", "背景音乐", "music", "ACE-Step 1.5"),
        row("sfx", "音效", "", "（无）"),
        row("lipsync", "口型同步", "", "（设计里是 Rhubarb viseme 分层）",
            can_register=False),
    ]
    ready = [r for r in rows if r["can_submit"]]
    return {
        "rows": rows,
        "any_wired": bool(ready),
        "note": ("四类里有 " + str(len(ready)) + " 类已经能提交执行" if ready else
                 "今天音频这一腿没有一类能真正执行：能登记台词与素材归属，"
                 "但配音／音乐／音效／口型的执行都还没接线。"
                 "所以这几页不给「提交」按钮——给了就是让人点一次失败一次。"),
    }



def episode_board(project_id: int, episode_id: int) -> dict:
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

    # 6) 音频制作：台账能记，执行没接线——状态照实说，动作一律不给
    audio_total = sum(sum(v.values()) for v in audio.values())
    audio_note = "执行引擎尚未接入，只能登记台账"
    if not audio_wired["tts"]["provider"]:
        audio_note = "配音默认轨还没配；且执行器未接线"
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
            "counts": {"documents": doc_total, "extraction_pending": runs["pending"],
                       "assets_referenced": gaps_assets["total"],
                       "videos_succeeded": succeeded, "videos_failed": failed,
                       "unsigned": review_pending, "attention": attention,
                       "composition_versions": comp["versions"]}}


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
    per_episode = []
    for ep in rows:
        counts = production.shot_gaps(int(ep["id"]))
        succeeded = production.job_status_by_shot(int(ep["id"])).get(
            "video", {}).get("succeeded", {}).get("shots", 0)
        comp = production.composition_delivery(int(ep["id"]))
        per_episode.append({
            "episode_id": int(ep["id"]), "seq": int(ep["seq"]),
            "title": ep["title"] or "", "status": ep["status"],
            "shots": counts["total"], "videos_succeeded": succeeded,
            "composition_versions": comp["versions"],
            "delivered": comp["latest_status"] == "succeeded" and comp["signed"] > 0})
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


# ---------------------------------------------------------------- 单镜头工作台


def _machine_total_gb() -> float:
    """物理内存总量（GB）。macOS 读 `sysctl hw.memsize`，Linux 回落到 /proc/meminfo。
    读不到就返回 0，让界面写「问不到这台机器有多少内存」，而不是猜一个 16。"""
    global _MACHINE_TOTAL_GB
    if _MACHINE_TOTAL_GB is not None:
        return _MACHINE_TOTAL_GB
    total = 0.0
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        if out.isdigit():
            total = int(out) / 1024 ** 3
    except Exception:
        total = 0.0
    if not total:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024 / 1024 ** 3
                    break
        except Exception:
            total = 0.0
    _MACHINE_TOTAL_GB = round(total, 1)
    return _MACHINE_TOTAL_GB


_MACHINE_TOTAL_GB = None


def _memory_readout(peak_rows: list[dict]) -> dict:
    """每条能力各自的内存峰值，以及「再并行一条扛不扛得住」的参考值。

    参考值的算法写在字段里（`formula`），因为它是一个**可以复核的除法**，
    不是一句结论：`(物理内存 × 0.8) ÷ 该能力单条采样峰值` 取整。
    0.8 是给系统与缓存留的余量——与合成前检查里磁盘阈值同一个思路
    （给余量、把余量写出来，而不是算到刚好满）。没有读数时一律给 None，
    不写「1 条应该没问题」这种没有依据的安慰。
    """
    by_capability = {}
    for row in peak_rows:
        peak_kb = row.get("peak_kb")
        if not peak_kb:
            continue
        by_capability[row["capability"]] = {
            "peak_gb": round(int(peak_kb) / 1024 ** 2, 2),
            "sampled_jobs": int(row["n"] or 0)}
    total = _machine_total_gb()
    hints = {}
    for capability, item in by_capability.items():
        if total and item["peak_gb"]:
            hints[capability] = {
                "parallel_reference": max(1, int(total * 0.8 // item["peak_gb"])),
                "formula": "(" + str(total) + " × 0.8) ÷ "
                          + str(item["peak_gb"]) + " 取整"}
    return {"machine_total_gb": total, "by_capability": by_capability,
            "parallel_reference": hints,
            "note": "峰值＝worker 按秒采的**这一条作业**的子进程树；"
                    "可能低于真实峰值，但不会把上一条的峰值算到这一条头上"}


def _team_readout(project_id: int) -> dict:
    """协作读数：谁提交了多少、谁签了几次。两个限制必须跟着数字一起显示。

    一、作业条数不等于产量：一次批量重投会算成同一个人的多条，一次产线运行会一次
    多出 N 条；这里只把 `created_by` 如实数出来，不排"谁贡献大"。
    二、签得少不等于质量差：谁能审到哪一条由可见性与分工决定，平台没有操作审计
    （设计稿 §8 明写不做），所以这里也不合成通过率、不做排名。
    """
    submissions = {int(r["user_id"]) if r["user_id"] is not None else 0: r
                   for r in job_repo.submission_counts([project_id])}
    by_user: dict[int, dict] = {}
    for user_id, row in submissions.items():
        by_user[user_id] = {"user_id": user_id or None,
                            "name": row["name"] or f"账号 #{user_id}",
                            "submitted": int(row["n"]),
                            "succeeded": int(row["ok"] or 0),
                            "busy_ms": int(row["busy_ms"] or 0),
                            "signs": {}}
    for row in reviews.signer_counts([project_id]):
        user_id = int(row["user_id"]) if row["user_id"] is not None else 0
        entry = by_user.setdefault(user_id, {
            "user_id": user_id or None,
            "name": row["name"] or f"账号 #{user_id}",
            "submitted": 0, "succeeded": 0, "busy_ms": 0, "signs": {}})
        entry["signs"][row["decision"]] = entry["signs"].get(row["decision"], 0) + int(row["n"])
    members = sorted(by_user.values(),
                     key=lambda m: (-m["submitted"], -sum(m["signs"].values()), str(m["name"])))
    return {"members": members,
            "caveats": ["作业条数不等于产量：一次批量重投会算成同一个人的多条",
                        "不排通过率也不排名：谁能审到哪一条由可见性与分工决定，平台没有操作审计"]}


def project_stats(project_id: int, overview: dict | None = None) -> dict:
    """项目层的生产统计与质量读数（任务书 §15 的 P2「生产统计」「质量分析」里**能算的那半**）。

    三个读数出口，一个都不新造判据：
      * 作业事实＝`jobs.stats_for_projects`（状态×能力如实数）；
      * 金额＝`jobs.cost_summary`——它与成本账、项目看板共用同一条 `BILLABLE_WHERE`，
        在这里重抄一遍 WHERE 就是第二本账；
      * 签字结论＝`reviews.decision_counts`。

    「这一集还要跑多久」只在**本项目自己真有已完成的视频作业**时才给数字，
    并写明按哪几条的均值推算；没有样本就不给（`estimate.shots` 为 0 时 `minutes` 是 None），
    也不拿别的机器、别的模型、别的项目的耗时顶——那种数看着像结论，实际是编的。
    同样不做的是"推荐重做哪一镜"：那需要质量标准，库里只有人的点击结果。
    """
    stats = job_repo.stats_for_projects([int(project_id)])
    by_status: dict[str, int] = {}
    by_capability: dict[str, dict] = {}
    for row in stats["by_group"]:
        status, capability, n = row["status"], row["capability"], int(row["n"])
        by_status[status] = by_status.get(status, 0) + n
        cap = by_capability.setdefault(capability, {
            "total": 0, "succeeded": 0, "failed": 0, "needs_attention": 0,
            "other": 0, "total_ms": 0, "timed": 0, "max_ms": 0})
        cap["total"] += n
        if status == "succeeded":
            cap["succeeded"] += n
        elif status == "failed":
            cap["failed"] += n
        elif status == "needs_attention":
            cap["needs_attention"] += n
        else:
            cap["other"] += n
        cap["total_ms"] += int(row["total_ms"] or 0)
        cap["timed"] += int(row["timed"] or 0)
        cap["max_ms"] = max(cap["max_ms"], int(row["max_ms"] or 0))
    for cap in by_capability.values():
        # 平均只按**量得出耗时的那些条**算：把没跑完的也算进分母，
        # 得到的不是"平均要跑多久"，而是一条被排队中拉低的假数。
        cap["avg_ms"] = round(cap["total_ms"] / cap["timed"]) if cap["timed"] else 0

    cost = job_repo.cost_summary([int(project_id)])
    by_currency: dict[str, int] = {}
    for row in cost["rows"]:
        key = row["currency"] or "—"
        by_currency[key] = by_currency.get(key, 0) + int(row["total"] or 0)

    signs: dict[str, dict[str, int]] = {}
    for row in reviews.decision_counts([int(project_id)]):
        bucket = signs.setdefault(row["target_kind"], {})
        bucket[row["decision"]] = bucket.get(row["decision"], 0) + int(row["n"])

    remaining = 0
    if overview is not None:
        for ep in overview.get("per_episode", []):
            remaining += max(0, int(ep["shots"]) - int(ep["videos_succeeded"]))
    video = by_capability.get("video", {})
    sample_ms = video.get("avg_ms", 0)

    # 冷／热提示词分开算：FastMetal 那一条腿把 UMT5 的提示词向量缓存在磁盘上
    # （`~/.cache/fastvideo/prompt_embeds`），**换过一条没跑过的提示词就要多跑一次编码**。
    # 本机实测：同一条提示词第二次 `prompt_encode_s` 是 0.007s、子进程峰值 1.99GB；
    # 一条全新的提示词是 82.0s、峰值 8.75GB——差出去 80 秒和 6.7GB，
    # 一集里每条提示词都新的话，这就是排产的真实成本，不能拿均值一句话糊过去。
    cold_s: list[float] = []
    warm_s: list[float] = []
    # 仓储层已经把 `prompt_encode_s` 单独取出来了（2026-11-08）：
    # 原来这里要解析每条作业的整块 `resources_json` 才拿到那一个数。
    for encode in cost["local_encodes"]:
        (cold_s if float(encode) > 1.0 else warm_s).append(float(encode))
    cold_extra_ms = int((sum(cold_s) / len(cold_s) * 1000) -
                        (sum(warm_s) / len(warm_s) * 1000)) if cold_s and warm_s else 0
    return {
        "jobs_total": sum(by_status.values()),
        "by_status": by_status,
        "by_capability": by_capability,
        "top_error_codes": [
            {"error_code": row["error_code"], "n": int(row["n"]),
             # 原因分布直接借异常目录的中文名：界面不必再抄一份码表（§14 那一条纪律）
             "label": error_catalog.lookup(row["error_code"])["label"]}
            for row in stats["by_error_code"]],
        "retries": {"resubmitted": stats["retried"],
                    "superseded": stats["superseded"]},
        "memory": _memory_readout(stats["peaks"]),
        "disk": diskplan.disk_readout(project_id, shots_missing=remaining),
        "team": _team_readout(int(project_id)),
        "money": {"by_currency": {k: round(v / 1e6, 4) for k, v in by_currency.items()},
                  "unpriced_cloud": cost["unpriced"],
                  "cost_scope": "与成本账同一条 BILLABLE_WHERE（只算计得上价的）"},
        "signs": signs,
        "estimate": {"remaining_shots": remaining,
                     "sample_jobs": video.get("timed", 0),
                     "avg_ms_per_shot": sample_ms,
                     "minutes": round(remaining * sample_ms / 60000, 1)
                              if remaining and sample_ms else None,
                     # 上界＝每条提示词都要重跑一次编码器；下界＝提示词都命中缓存。
                     # 真实值取决于这一集里有多少条**没用过的**提示词，
                     # 所以两个都给、并把依据写出来，比给一个看似精确的数诚实。
                     "minutes_if_all_new_prompts": (
                         round(remaining * (sample_ms + cold_extra_ms) / 60000, 1)
                         if remaining and sample_ms and cold_extra_ms else None),
                     "cold_prompt_count": len(cold_s),
                     "warm_prompt_count": len(warm_s),
                     "cold_extra_ms": cold_extra_ms,
                     "basis": (f"按本项目已完成的 {video.get('timed')} 条视频作业的本机均值"
                               f" {round(sample_ms / 1000)}s/镜推算"
                               + (f"；其中 {len(cold_s)} 条是新提示词（多花 "
                                  f"{round(cold_extra_ms / 1000)}s 跑文本编码）"
                                  if cold_extra_ms else "")
                               if remaining and sample_ms else
                               "本项目还没有已完成的视频作业，不给推算值")},
        "not_computed": ["哪个镜头该重做（需要质量标准，库里只有人的点击结果）",
                         "推荐换哪一档模型（配置不补位，见裁定 MC1）"],
    }


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
        except Exception:
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
            "params": job["params_json"], "artifacts": artifacts,
            "sign": signs.get(int(job["id"])),
        })
    return out


def shot_workbench(shot_id: int) -> dict:
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
    job_ids = [int(j["id"]) for j in list(image_jobs) + list(video_jobs)]
    sign_rows = reviews.signs_for_jobs(project_id, job_ids) if job_ids else []
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
    stale_video = bool(latest_video and services.job_source_stale(
        latest_video, shot=shot))

    asset_ids = sorted(services.shot_asset_ids(shot))
    variant_ids = sorted(services.shot_variant_ids(shot))
    loaded_assets = asset_repo.get_many(asset_ids) if asset_ids else {}
    loaded_variants = variant_repo.get_many(variant_ids) if variant_ids else {}
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
        "engine_note": ("配音/音乐执行引擎尚未接入，音频段只能登记台账"
                        if not any(v["runner_wired"] for v in audio_wired.values()) else ""),
        "blockers": blockers,
        "can_compose": (latest_video is not None and signed_video
                        and not stale_video and not blockers),
    }

    return {
        "shot_id": shot_id, "project_id": project_id,
        "episode_id": int(shot["episode_id"]),
        "setting": {
            "seq": int(shot["seq"]), "summary": shot["summary"],
            "prompt": shot["prompt"], "duration": float(shot["duration"]),
            "shot_size": shot["shot_size"] if "shot_size" in shot.keys() else "",
            "camera_move": shot["camera_move"] if "camera_move" in shot.keys() else "",
            "aspect": shot["aspect"] if "aspect" in shot.keys() else "",
            "style": shot["style"] if "style" in shot.keys() else "",
            "negative_prompt": (shot["negative_prompt"]
                                if "negative_prompt" in shot.keys() else ""),
            "revision": int(shot["revision"])},
        "refs": {"assets": refs, "variants": ref_variants,
                 "missing_assets": [a for a in asset_ids if a not in loaded_assets],
                 "missing_variants": [v for v in variant_ids if v not in loaded_variants]},
        "keyframes": {"jobs": _job_rows(image_jobs, signs),
                      "formal_job_id": (int(formal_image["id"]) if formal_image else None)},
        "videos": {"jobs": _job_rows(video_jobs, signs),
                   "formal_job_id": (int(formal_video["id"]) if formal_video else None),
                   "source_stale": stale_video},
        "audio": {"clips": [{"id": int(c["id"]), "kind": c["kind"],
                             "speaker": c["speaker"] or "", "text": c["text"],
                             "status": c["status"], "duration_ms": c["duration_ms"],
                             "job_status": c["job_status"] if "job_status" in c.keys() else ""}
                            for c in clips],
                  "engines": audio_wired},
        "preview": {
            "formal_video": _job_rows([formal_video], signs)[0] if formal_video else None,
            "formal_keyframe": _job_rows([formal_image], signs)[0] if formal_image else None},
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
    from repositories.content import assets as asset_repo

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

    # 每集一次"最新成功视频"批量读，而不是逐镜回库——一集 13 镜时那是 13 趟。
    latest: dict[int, dict] = {}
    for eid in {h["episode_id"] for h in hits.values()}:
        for sid, job in job_repo.latest_succeeded_videos_by_shot(eid).items():
            latest[int(sid)] = job

    # 循环外两次批量读完：镜头行一次 IN、签字一次 IN。
    # 原来这两句在循环里逐条 `shots.get()`／`reviews.signs()`——600 资产／1 500 镜头
    # 那一档量到的是**回库次数随引用镜头数增长**（18→54），正是规模基准要抓的那种。
    shot_rows = shots.get_many(list(hits))
    sign_rows = reviews.signs_by_targets("job", [int(j["id"]) for j in latest.values()])
    items = []
    for hit in sorted(hits.values(), key=lambda h: (h["episode_id"], h["seq"])):
        job = latest.get(hit["shot_id"])
        stale = False
        signed = False
        if job is not None:
            stale = services.job_source_stale(job, shot=shot_rows.get(hit["shot_id"]))
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
