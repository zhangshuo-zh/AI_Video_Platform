"""统一预检（任务书 §7）。一句话职责：**把"提交后必然失败"提前到提交前说清楚**。

为什么要单独一层，而不是让每个页面各写各的检查：
生成中心、镜头工作台、产线发起弹窗、成片中心四个入口都在问同一批问题
（内容全不全、资产在不在、模型能不能跑、这台机器行不行、要不要花钱）。
分四处写，迟早出现"弹窗说能提交、点下去吃 422"——那是界面在骗人。
所以这里只有一份规则，四个入口读的是同一个结果。

三条纪律：

1. **只读、确定性**。不发请求、不加载模型、不写任何状态；同一份数据反复调用
   给出同一个结论，所以它可以被界面在每次打开弹窗时跑一遍。
2. **三档，不含糊**。`阻塞`＝现在提交一定失败（服务端同一条规则会拒）；
   `警告`＝能提交但要有心理准备（缺参考图、worker 离线、云端要花钱）；
   `通过`＝这一项没问题。任务书 §7.2 要的就是这三档。
3. **不新造判据**。模型能不能跑走 `adapters.health()`；有没有默认轨走
   `services.resolve_provider()`；参数对不对走 `adapters.validate_params()`；
   成片能不能拼走 `composition.timeline.build()`。**判定规则只有一份**，
   这一层只是把它们翻译成"人能在界面上点过去"的清单。

`locate` 是给界面的跳转目标（模块 id ＋二级页下标），
"点击问题直接跳到对应配置位置"是任务书 §7.2 的要求，不是装饰。
"""
from __future__ import annotations

import json
import re
import shutil

import adapters
import diskplan
import jobfiles
import pipeline
import services
from config import FFMPEG_BIN
from composition import subtitles as subtitles_mod
from composition import timeline as timeline_mod
from repositories.content import assets, episodes, shots, variants
from repositories.production import production

PASS = "通过"
WARN = "警告"
BLOCK = "阻塞"

# 跳转目标：模块 id 与二级页下标要和 modules.js 一致，否则点了跳不到地方。
LOCATE = {
    "storyboard": {"module": "writing", "sub": 2},
    "assets": {"module": "assets", "sub": 0},
    "asset_studio": {"module": "assets", "sub": 1},
    "generate": {"module": "generate", "sub": 0},
    "models": {"module": "models", "sub": 1},
    "review": {"module": "review", "sub": 1},
    "composition": {"module": "composition", "sub": 0},
    "audio": {"module": "audio", "sub": 0},
    "pipeline": {"module": "pipeline", "sub": 0},
}


def _first_seq(text: str) -> int | None:
    """从句子里取出**第一个镜号**，取不到返回 None。

    为什么要认「第 1／2 镜」这种并列写法：`pipeline.preflight()` 把引用同一个坏资产的
    镜号用／连成一句（"资产 #9 不存在，第 1／2 镜还在引用它"），只认「第 N 镜」就会漏——
    而那条阻塞恰恰最该跳到镜头上去改引用，丢人去资产列表翻是帮不上忙的。
    """
    m = re.search(r"第\s*((?:\d+\s*[／、，,]?\s*)+)\s*镜", text or "")
    if not m:
        return None
    nums = re.findall(r"\d+", m.group(1))
    return int(nums[0]) if nums else None


def _shot_entry(row, seq: int) -> dict:
    """跳到那一镜的工作台，并**把集号一起带上**。

    为什么要把 episode_id 写进定位表：`goto.js` 会按它对齐顶栏上下文。少了它，
    页面装着第 4 集的这一镜、顶栏却还写着第 1 集——"跳过去看"最容易出现的自相矛盾。
    老数据行没有这一列就不写（界面照样能跳，只是顶栏不动）。
    """
    locate = {"module": "shot", "sub": 0, "shot_id": int(row["id"]),
              "label": f"第 {seq} 镜"}
    if "episode_id" in row:
        locate["episode_id"] = int(row["episode_id"])
    return locate


def _shot_locate(text: str, shot_rows) -> dict:
    """把"第 N 镜……"这类原话落到**那一镜的工作台**上（任务书 §7.2「点击问题定位到具体镜头」）。

    为什么在这里按文案解析，而不是让 `timeline.build()` 抛结构化对象：
    那句话本身就是它的用户界面（`test_composition.py` 与合成接口的 422 文案都在钉这些句子），
    再拿一份结构化字段并行维护就是两套真相。这里只做一次"文案 → 镜头"的翻译，
    认不出来就退回审核中心——**认不出不等于假装知道**。
    """
    seq = _first_seq(text)
    if seq is not None:
        for row in shot_rows:
            if int(row["seq"]) == seq:
                return _shot_entry(row, seq)
    m = re.search(r"作业\s*#(\d+)", text or "")
    if m:
        return {"module": "generate", "sub": 0, "job_id": int(m.group(1)),
                "label": f"作业 #{m.group(1)}"}
    return dict(LOCATE["review"])


# 整集清单（产线发起弹窗）的跳转规则：**按文案关键词认，认不出就不摆按钮**。
# 顺序是从具体到一般——「资产「X」没有参考图」同时命中「资产」与「参考图」，
# 但该跳的是资产创作（补图的地方）而不是资产库列表，所以那条规则排在前面。
_EPISODE_LOCATE_RULES = (
    (r"ffmpeg|worker|不在线|磁盘|作业目录|只剩", LOCATE["generate"]),
    (r"参考图|一致性提示词|定妆", LOCATE["asset_studio"]),
    (r"默认轨|清单|模型", LOCATE["models"]),
    (r"还没有镜头|先拆集|分镜|剧本|正文", LOCATE["storyboard"]),
    (r"未结束的运行", LOCATE["pipeline"]),
    (r"模板", {**LOCATE["pipeline"], "sub": 1}),
    (r"资产|形态", LOCATE["assets"]),
    (r"成片|合成|字幕|字体", LOCATE["composition"]),
)


def _episode_locate(text: str, shot_rows) -> dict:
    """整集清单里每一句话该跳到哪儿。

    与 `_shot_locate()` 同一思路：**认得出才给跳转**，认不出返回空字典（界面上就不摆按钮），
    而不是把人丢回某个默认页——"去审核中心"对一条"worker 不在线"的提醒毫无意义。

    为什么不改 `pipeline.preflight()` 让它直接返回结构化条目：那些句子同时是
    运行事件（`preflight_warning`）与接口 422 的正文，动它们要改三处下游；
    这一层的职责本来就是"把既有判据翻译成人能点过去的清单"，翻译放在这里正合适。

    规则按**先具体后一般**排：先认「第 N 镜」，再认资产／模型／成片／环境这些类别词。
    """
    seq = _first_seq(text)
    if seq is not None:
        for row in shot_rows:
            if int(row["seq"]) == seq:
                return _shot_entry(row, seq)
    for pattern, locate in _EPISODE_LOCATE_RULES:
        if re.search(pattern, text or ""):
            return dict(locate)
    return {}


def _item(code: str, level: str, text: str, locate: dict | None = None) -> dict:
    # **拷一份 locate**：调用方传进来的常是 `LOCATE[...]` 那个模块级字典本身，
    # 直接引用会让所有条目共用同一个对象——补 `shot_id` 时就等于把全局定位表改了，
    return {"code": code, "level": level, "text": text, "locate": dict(locate or {})}


def _free_gb() -> float | None:
    """作业目录所在盘的剩余 GB。`volume_usage()` 是 worker 心跳与预检共用的那一个出处，
    它已经处理过"目录还没建"的情况——在这里重抄一遍 disk_usage 就会有两套盘余说法。"""
    usage = jobfiles.volume_usage()
    return round(usage.free / 1024 ** 3, 1) if usage else None


def _disk_item(disk: dict, locate: str) -> dict:
    """磁盘这一项：**空间不够就是阻塞，不只是提醒**（2026-11-08，P1）。

    原来这里是 `WARN if disk_low else PASS`，而 `disk_low` 在
    "预计还要 12 GB、只剩 3 GB" 的时候也是 True——它只换来一句黄色提醒，
    `check["blocking"]` 仍为空，发起照走。于是一条作业跑到一半把盘写满，
    留下的半截产物还要靠 `runners.verify` 的魔数与非空校验事后判 failed。
    这台机器上磁盘是共享瓶颈（`release_blockers.json` 那条也挂着），
    所以"明知放不下还放行"不是提醒不够，是没挡住。

    判据分三种，只有第一种是阻塞：
      * `shortfall_bytes > 0`：按本项目自己的产物样本算，**确实放不下** → BLOCK；
      * 读不到盘余（`None`）：无法判断 → WARN，不替人拍板说"放得下"；
      * 只是低于固定提醒线（没有样本可比）：WARN——那是保守的地板，不是结论。
    """
    shortfall = disk.get("shortfall_bytes")
    if shortfall is not None and shortfall > 0:
        return _item("env.disk", BLOCK,
                     f"{disk['sentence']} 放不下：按本项目的产物尺寸还差"
                     f" {shortfall / 1024 ** 3:.1f} GB。先清理 or 换更大的盘再发起",
                     locate)
    if disk.get("free_bytes") is None:
        return _item("env.disk", WARN, disk["sentence"], locate)
    if disk["free_bytes"] < disk["floor_bytes"]:
        return _item("env.disk", WARN, disk["sentence"], locate)
    return _item("env.disk", PASS, disk["sentence"], locate)


def _rollup(items: list[dict]) -> dict:
    """三档汇总。`status` 是界面顶部那一枚徽章，不是每项的状态拼字符串。"""
    blocking = sum(1 for i in items if i["level"] == BLOCK)
    warnings = sum(1 for i in items if i["level"] == WARN)
    return {
        "status": BLOCK if blocking else WARN if warnings else PASS,
        "total": len(items), "blocking": blocking, "warnings": warnings,
        "passed": sum(1 for i in items if i["level"] == PASS),
        "items": items}


def _model_items(capability: str, execution_mode: str | None,
                 provider: str = "", model: str = "",
                 params: dict | None = None,
                 confirmed_cloud: bool = False) -> tuple[list[dict], dict]:
    """模型与环境那一组：默认轨、清单、健康、参数白名单、云端计费。

    返回 (清单, 解析出来的路线)。`resolve_provider()` 失败不抛出去——
    预检的意义就是把异常变成一行可读的阻塞项。
    """
    import services as _s

    items: list[dict] = []
    route: dict = {}
    try:
        route = _s.resolve_provider(capability, execution_mode,
                                    confirmed_cloud=confirmed_cloud)
    except _s.NoRoute as exc:
        items.append(_item("model.route", BLOCK, str(exc), LOCATE["models"]))
        return items, route

    provider = provider or route["provider"]
    model = model or route["model"]
    adapter = adapters.find(capability, route["backend"], provider)
    if adapter is None:
        items.append(_item("model.manifest", BLOCK,
                           f"provider「{provider}」不在清单里，平台不会猜它的参数",
                           LOCATE["models"]))
    else:
        health = adapters.health(route["row"])
        if health["state"] != "ok":
            items.append(_item("model.health", BLOCK,
                              f"{adapter.label} 现在不可用：{health['detail']}",
                              LOCATE["models"]))
        else:
            items.append(_item("model.health", PASS,
                              f"{adapter.label}：{health['detail']}"))
        errors = adapters.validate_params(adapter, model, params or {})
        for err in errors:
            items.append(_item("model.params", BLOCK, f"参数不成立：{err}",
                               LOCATE["generate"]))
        if not errors:
            items.append(_item("model.params", PASS, "参数在清单白名单内"))
    if route.get("backend") == "cloud":
        if not confirmed_cloud:
            items.append(_item("cost.cloud", BLOCK,
                               "云端任务要逐次确认计费后才能提交",
                               LOCATE["generate"]))
        else:
            items.append(_item("cost.cloud", WARN,
                               "云端任务按供应商计费，提交后会进成本账待核对",
                               LOCATE["generate"]))
    return items, route


def shot_precheck(shot_id: int, *, capability: str = "video",
                  execution_mode: str | None = None,
                  params: dict | None = None,
                  confirmed_cloud: bool = False) -> dict:
    """一镜的生成前预检：内容 → 资产 → 模型 → 环境 → 费用 → 资源。"""
    items: list[dict] = []
    shot = shots.get(shot_id)
    if shot is None:
        return _rollup([_item("content.shot", BLOCK, "镜头不存在")])

    # ① 内容
    if not (shot["prompt"] or "").strip() and not (shot["summary"] or "").strip():
        items.append(_item("content.empty", BLOCK,
                           f"第 {shot['seq']} 镜既没有提示词也没有画面描述，"
                           "生成时没有任何可交给模型的文字", LOCATE["storyboard"]))
    elif not (shot["prompt"] or "").strip():
        items.append(_item("content.prompt", WARN,
                          f"第 {shot['seq']} 镜没有提示词，会用画面描述代替",
                          LOCATE["storyboard"]))
    else:
        items.append(_item("content.prompt", PASS, "提示词已填"))

    # ② 资产
    asset_ids = sorted(services.shot_asset_ids(shot))
    variant_ids = sorted(services.shot_variant_ids(shot))
    loaded = assets.get_many(asset_ids) if asset_ids else {}
    missing = [a for a in asset_ids if a not in loaded]
    if missing:
        items.append(_item("asset.missing", BLOCK,
                           "引用了不存在的资产：" + "、".join(f"#{a}" for a in missing),
                           LOCATE["assets"]))
    no_image = [row["name"] for row in loaded.values()
                if json.loads(row["ref_images"] or "[]") == []]
    if no_image:
        items.append(_item("asset.ref_image", WARN,
                          "这些引用资产没有参考图，一致性只能靠文字："
                          + "、".join(no_image), LOCATE["asset_studio"]))
    if variant_ids:
        loaded_variants = variants.get_many(variant_ids)
        lost = [v for v in variant_ids if v not in loaded_variants]
        if lost:
            items.append(_item("variant.missing", BLOCK,
                               "引用了不存在的形态：" + "、".join(f"#{v}" for v in lost),
                               LOCATE["assets"]))
    if not asset_ids and not variant_ids:
        items.append(_item("asset.none", WARN,
                          "这一镜没有引用任何资产，人物与场景一致性没有依据",
                          LOCATE["storyboard"]))

    # ③④⑤ 模型 / 环境 / 费用
    model_items, route = _model_items(capability, execution_mode,
                                      params=params,
                                      confirmed_cloud=confirmed_cloud)
    items += model_items

    # ⑥ 资源：worker 与队列。这两个不阻断——排队是正常的，离线才要提醒
    worker = services.worker_status()
    if not worker["online"]:
        items.append(_item("env.worker", WARN,
                          "worker 不在线；提交后作业会一直排队，"
                          "启动命令：./run.sh worker", LOCATE["generate"]))
    else:
        items.append(_item("env.worker", PASS, "worker 在线"))
    # 磁盘这一条从"拍出来的阈值"改成"这一镜还要多少"（`diskplan` 一处算式）。
    # 阈值仍然保留为地板：新项目没有样本时只能说"低于提醒线"，有样本时说的是
    # "预计还要约 X GB、还剩 Y GB"——后者才答得出人真正想问的问题。
    _ep = episodes.get(int(shot["episode_id"])) if shot["episode_id"] else None
    disk = diskplan.disk_readout(int(_ep["project_id"]) if _ep else 0, shots_missing=1)
    items.append(_disk_item(disk, LOCATE["generate"]))
    queued = worker["queue"]["queued"]
    items.append(_item("resource.queue",
                       WARN if queued > 5 else PASS,
                       f"队列里已排 {queued} 条"
                       + ("；这一条要等前面跑完（默认全局并发 1）" if queued else "")))

    # ⑦ 已有产物的源版本。为什么不阻断：重新生成本身就是"源变了"的解法，
    #   把它判成阻塞会让人连重做都点不动。但必须说在前面——
    #   这一镜如果已经有一条签过字的正式视频，而分镜或资产后来改过，
    #   那条视频在成片口径里已经算过期；不提这句，人就会以为它能直接进成片。
    #   判定不另写一份：用 `services.job_source_stale`，与时间线取片同一个出处。
    from repositories.jobs import jobs as job_repo
    latest = job_repo.latest_succeeded_videos_by_shot(
        int(shot["episode_id"])).get(int(shot_id))
    if latest is not None and services.job_source_stale(latest, shot=shot):
        items.append(_item("artifact.source_stale", WARN,
                           f"第 {shot['seq']} 镜最新的正式视频（作业 #{latest['id']}）"
                           "是旧版分镜／资产生成的，源内容已变；"
                           "要重新生成并重新签字才能进成片",
                           LOCATE["review"]))

    # 这一份清单整张都是**这一镜**的：给每条都补上镜头定位，
    # 界面于是可以把"去处理"直接做成"去这一镜的工作台"，而不是丢回列表页让人再找
    for item in items:
        locate = item.get("locate") or {}
        locate.setdefault("shot_id", int(shot_id))
        locate.setdefault("module", "shot")
        locate.setdefault("sub", 0)
        item["locate"] = locate

    out = _rollup(items)
    out["shot_id"] = int(shot_id)
    out["route"] = {k: route.get(k) for k in ("backend", "provider", "model")} \
        if route else {}
    return out


def episode_precheck(project_id: int, episode_id: int,
                     template_id: int | None = None) -> dict:
    """整集预检：直接复用 `pipeline.preflight()`，不重抄一遍它的判断。

    产线发起用的就是那一个函数；这里把它的人话清单翻成三档结构，
    于是"产线弹窗里的三档"与"工作台里的预检"永远是同一份结论。
    任务书 §10 要的「产线与人工两种模式共用一套」正落在这里：
    同一个函数、同一份判据、同一个跳转，只是入口不同。
    """
    check = pipeline.preflight(project_id, episode_id, template_id)
    shot_rows = shots.list_by_episode(episode_id)
    items = [_item("pipeline.blocking", BLOCK, text, _episode_locate(text, shot_rows))
             for text in check.get("blocking", [])]
    items += [_item("pipeline.warning", WARN, text, _episode_locate(text, shot_rows))
              for text in check.get("warnings", [])]
    if not items:
        items.append(_item("pipeline.pass", PASS,
                      f"这一集 {check.get('info', {}).get('shots', 0)} 镜，"
                      "生产前检查没有异议"))
    out = _rollup(items)
    out["info"] = check.get("info", {})
    # `can_start` 是给发起弹窗那颗按钮的：它原来自己数 `blocking`，
    # 数法与这里不同（一处按字符串、一处按条目）——按钮禁不禁该只有一处说了算。
    out["can_start"] = not check.get("blocking")
    out["blocking_texts"] = list(check.get("blocking", []))
    out["warning_texts"] = list(check.get("warnings", []))
    return out


def composition_precheck(project_id: int, episode_id: int,
                         subtitle_path: str | None = None) -> dict:
    """合成前预检（任务书 §9.1 的 11 项）。

    第 9 项"时间线有效性"直接调 `timeline.build(require_approved=True)`——
    那就是真合成时走的那条路，所以这里说"能拼"，点下去就真能拼。
    """
    items: list[dict] = []
    output_plan: dict | None = None
    shot_rows = shots.list_by_episode(episode_id)

    # 1 镜头完整性
    if not shot_rows:
        items.append(_item("shots.none", BLOCK, "这一集还没有镜头，无从合成",
                          LOCATE["storyboard"]))
    else:
        items.append(_item("shots.count", PASS, f"这一集 {len(shot_rows)} 镜"))

    # 2 镜头顺序
    seqs = [int(s["seq"]) for s in shot_rows]
    if len(set(seqs)) != len(seqs):
        items.append(_item("shots.duplicate", BLOCK, "有重复镜号", LOCATE["storyboard"]))
    elif seqs and sorted(seqs) != list(range(min(seqs), min(seqs) + len(seqs))):
        items.append(_item("shots.gap", WARN, "镜号不连续，成片顺序以镜号为准",
                          LOCATE["storyboard"]))
    else:
        items.append(_item("shots.order", PASS, "镜号连续"))

    # 3 视频文件有效性 ＋ 4 审核状态 ＋ 5 源版本（都由 timeline.build 一次判完）
    try:
        timeline = timeline_mod.build(project_id, episode_id,
                                      require_approved=True,
                                      subtitle_path=subtitle_path)
        # 顺手把"不填分辨率会按哪一档出"带出去：发起弹窗要显示这一句，
        # 而它不能自己再算一遍众数——那是 `timeline.majority_size()` 的活。
        output_plan = timeline.get("output_size")
        items.append(_item("timeline.ok", PASS,
                      f"时间线可用：{len(timeline['shots'])} 镜、"
                      f"总长 {round(timeline.get('duration_ms', 0) / 1000, 1)} 秒"))
    except timeline_mod.CompositionInvalid as exc:
        for reason in (exc.reasons or [exc.message]):
            items.append(_item("timeline.blocking", BLOCK, reason,
                               _shot_locate(reason, shot_rows)))

    # 6 音频完整性：有台词素材但还没出片的，合成时是"没声音"而不是失败
    audio = production.audio_status(episode_id)
    not_ready = sum(n for st, n in audio.get("dialogue", {}).items()
                    if st != "ready") + sum(n for st, n in audio.get("narration", {}).items()
                                            if st != "ready")
    if not_ready:
        items.append(_item("audio.pending", WARN,
                          f"{not_ready} 条台词/旁白素材还没就绪，成片会缺配音",
                          LOCATE["audio"]))
    else:
        items.append(_item("audio.ok", PASS, "台词素材没有欠账"))

    # 7 字幕文件 ＋ 8 字体依赖
    if subtitle_path:
        # 字幕是在上传时就验过格式与大小的（`subtitles.check`）；这里只确认
        # "这一版时间线引用的那份还在、还在本项目目录里"，不重跑一遍解析。
        if jobfiles.resolve_subtitle(project_id, subtitle_path) is None:
            items.append(_item("subtitle.bad", BLOCK,
                               "字幕文件不在本项目字幕目录里，或已被清理",
                               LOCATE["composition"]))
        else:
            items.append(_item("subtitle.ok", PASS, "字幕文件在，且属于本项目"))
        available = subtitles_mod.availability()
        if not available["supported"]:
            items.append(_item("subtitle.filter", BLOCK, available["hint"],
                              LOCATE["composition"]))
        else:
            items.append(_item("subtitle.filter", PASS, "字幕滤镜可用"))
    else:
        items.append(_item("subtitle.none", PASS, "这次不带字幕"))

    # 10 FFmpeg / ffprobe 环境
    if shutil.which(FFMPEG_BIN) is None:
        items.append(_item("env.ffmpeg", BLOCK,
                           f"找不到 {FFMPEG_BIN}，合成跑不起来", LOCATE["composition"]))
    else:
        items.append(_item("env.ffmpeg", PASS, f"{FFMPEG_BIN} 在"))

    # 11 输出目录与存储空间：合成这一腿要的是**一次成片产物**的空间，
    # 所以按 `shots_missing=0` 问估算（镜头重生成不在这一步的账里）。
    # 没有样本时退回固定提醒线那句话——两处用同一个 `diskplan.sentence()`。
    disk = diskplan.disk_readout(project_id, shots_missing=0)
    items.append(_disk_item(disk, LOCATE["composition"]))

    out = _rollup(items)
    out["output_plan"] = output_plan
    return out
