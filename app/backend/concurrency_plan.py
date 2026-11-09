"""并发上限该设几：把「网页上填个数」从拍脑袋变成有据可查、且危险时会拦一下。

为什么要有这一层：并发上限原本只能从命令行给（`./run.sh worker --max-global N`），
那是刻意的——`worker.py` 的模块头写明「并发是 operator 明确抬上限并多开进程才
发生的事，不会因为升级代码就悄悄在 16GB 机器上同时跑两条视频」。但只在命令行
给带来另一个问题：**人看不到自己设的东西有多危险**。设 8 的时候没人告诉他这台
机器上 mflux 一条就要 1.4 GB、视频峰值到过 2 GB。

这一层做三件事，都不新造事实：

1. **从这台机器自己的历史作业里量**每类资源各占多少内存（`job.resources_json`
   里已有 `child_peak_rss_sampled_kb` 等读数，worker 一直在写，只是从没人拿它
   算过并发）。样本不足就明说「没样本」，不拿别的机器或文档里的数字顶
   ——与 `diskplan.py` 同一套纪律。
2. 用**这台机器**的总内存（`os.sysconf`，跨平台、不引 psutil）减去系统与
   worker 自身的常驻，算出「还剩多少能给模型用」，再看声明的上限会不会撑破它。
3. 给出**三档**结论：safe／tight／over，`tight` 与 `over` 各有一句人话。
   界面拿这句话直接显示，不各写一遍判定。

这里**不拦**任何事：判定归判定，要不要照做归人。真要拦，那是 `claim_next`
的活，而它只认数字不认风险评估——两件事分开，改评估口径不会改到领取逻辑。
"""
from __future__ import annotations

import json
import os

from config import RESOURCE_CLASSES

# 每类资源取最近多少条成功作业当样本。与 diskplan 的 SAMPLE_LIMIT 同量级：
# 要的是"这台机器上这类的典型占用"，取最近一批就够，不为一次页面打开扫全表。
SAMPLE_LIMIT = 20

# 四个可能装内存读数的字段名，按可靠性排序。历史作业不同年代写的不一样：
# `child_peak_rss_sampled_kb` 是采样峰值（W09 起），`child_max_rss_kb` 是
# getrusage 的生命周期峰值，`max_rss_kb` 是更早的单进程写法。
# 三个都认，是因为"老样本也是这台机器上的真实读数"，丢掉它们等于假装没跑过。
_RSS_KEYS = ("child_peak_rss_sampled_kb", "child_max_rss_kb", "max_rss_kb")

# 系统与 worker 自身要占的部分。macOS/Linux 的页缓存、内核、其他应用加起来
# 通常吃掉 3—5 GB；取 4 GB 作为保守基线，可由参数覆盖。
RESERVED_OS_MB = 4096

# 余量系数：估出来的模型占用之外还要留一口气（权重加载峰值、临时张量、
# 磁盘缓存）。1.15 是保守的，并把系数写进返回体，不藏在代码里。
SAFETY_RATIO = 1.15

MB = 1024 ** 2


def total_memory_mb() -> int:
    """这台机器的物理内存（MB）。读不到返回 0——调用方据此说"量不出"而不是猜。"""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return int(pages * page_size / MB)
    except (ValueError, OSError, AttributeError):
        return 0
    return 0


def _rss_kb(resources: dict) -> int:
    """从一条作业的 resources_json 里取它的内存读数（KB）。取不到返回 0。"""
    for key in _RSS_KEYS:
        value = resources.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return 0


def samples_by_class(rows: list[dict] | None = None,
                     limit: int = SAMPLE_LIMIT) -> dict[str, list[int]]:
    """按资源类收集内存样本（字节）。

    `rows` 每项形如 `{"capability": ..., "provider_code": ..., "resources_json": ...}`，
    与 `job` 表那几列对应。给 None 时由调用方决定——这里不做查询，
    与 `diskplan` 一样"取数"和"算"分开，测试才好喂固定样本。

    **`mock` 与 `ffmpeg` 一律不进样本**（2026-11-09 实测）：
      * mock 的读数是假适配器自己烧的 22 MB，真正的视频模型峰值到过 2038 MB。
        混进来会把 video 的峰值抹到几百 MB，估出来的"安全上限"直接错一个数量级。
      * ffmpeg（compose）是纯转码，占用与模型不是一回事，且它几乎不占显存；
        混进 image/video 的样本里同样会把它们拉低。
    样本宁可少也不能脏——脏样本比没样本更危险，因为它看起来有依据。
    """
    out: dict[str, list[int]] = {}
    for row in (rows or [])[: limit * len(RESOURCE_CLASSES)]:
        provider = row.get("provider_code") or ""
        if provider in ("mock", "ffmpeg"):
            continue
        raw = row.get("resources_json") or "{}"
        try:
            resources = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except ValueError:
            continue
        if not isinstance(resources, dict):
            continue
        kb = _rss_kb(resources)
        if kb <= 0:
            continue
        cls = _class_of(row)
        bucket = out.setdefault(cls, [])
        if len(bucket) < limit:
            bucket.append(kb * 1024)
    return out


def _class_of(row: dict) -> str:
    """与 `repositories/jobs_parts/common.RESOURCE_CLASS_SQL` 同一套映射。

    为什么在这里再写一份：SQL 那份只能在库边用，这里是纯 Python 的评估，
    不该为了一个 `CASE WHEN` 回库。两处口径一致性由测试守着
    （`test_concurrency_plan.py` 用同一批行分别走两条路比结果），
    不是靠"记得同时改"。
    """
    provider = row.get("provider_code") or ""
    if provider == "ffmpeg":
        return "compose"
    capability = row.get("capability") or ""
    if capability in ("tts", "music", "audio"):
        return "audio"
    return capability


def assess(*, limits: dict | None, rows: list[dict] | None = None,
           total_mb: int | None = None,
           reserved_mb: int = RESERVED_OS_MB,
           running_now: dict[str, int] | None = None) -> dict:
    """声明的并发上限在这台机器上扛不扛得住。

    `limits` 与 `claim_next` 收的是同一个形状：`{"global": 2, "per": {...}}`。
    返回体里的 `verdict` 是 safe／tight／over，`sentence` 给人看的那一句。
    """
    limits = limits or {}
    total = total_memory_mb() if total_mb is None else int(total_mb)
    per = {k: int(v) for k, v in (limits.get("per") or {}).items()}
    global_max = int(limits.get("global") or 0)

    samples = samples_by_class(rows)
    per_class_mb = {k: round(sum(v) / len(v) / MB) for k, v in samples.items()}

    # 没给某一类的显式上限时，按"全局上限条都落在最贵那一类"来估最坏情况——
    # 这是保守方向：真实情况多半分散在不同类。反过来按最便宜估，就会在
    # "8 条全是视频"时给出"安全"的错误结论。
    worst_class = max(per_class_mb, key=per_class_mb.get) if per_class_mb else ""
    worst_mb = per_class_mb.get(worst_class, 0) if per_class_mb else 0

    # 每类各自的峰值占用（同一类内部取最大值而不是平均：并发时同时跑的两条
    # 都在峰值附近最坏，平均会把尖峰抹平）
    peak_by_class = {k: round(max(v) / MB) for k, v in samples.items()}
    need_mb = 0
    detail: list[dict] = []
    for cls in RESOURCE_CLASSES:
        slot_cap = per.get(cls, global_max)     # 没显式给该类，就按全局上限估
        if slot_cap <= 0:
            continue
        unit = peak_by_class.get(cls)
        if unit is None:
            continue
        slots_mb = unit * slot_cap
        need_mb += slots_mb
        detail.append({"class": cls, "slots": slot_cap,
                       "unit_peak_mb": unit, "sum_mb": slots_mb,
                       "samples": len(samples.get(cls, []))})

    need_mb = round(need_mb * SAFETY_RATIO) if detail else 0
    available_mb = max(0, total - reserved_mb) if total else 0
    headroom_mb = available_mb - need_mb if total and detail else None

    if not detail:
        verdict = "unknown"
        sentence = ("这台机器还没跑过任何带内存读数的作业，估不出并发要多少；"
                    "先按默认上限跑一条，再回来看这里")
    elif not total:
        verdict = "unknown"
        sentence = "读不到本机物理内存，估不出上限合不合适"
    elif headroom_mb is not None and headroom_mb < 0:
        verdict = "over"
        sentence = (f"这套上限在最坏情况下要约 {need_mb / 1024:.1f} GB，"
                    f"而本机留给模型的只有约 {available_mb / 1024:.1f} GB（总 "
                    f"{total / 1024:.0f} GB 减 {reserved_mb / 1024:.0f} GB 系统常驻）；"
                    f"同时跑起来有内存溢出风险")
    elif headroom_mb is not None and headroom_mb < available_mb * 0.25:
        verdict = "tight"
        sentence = (f"这套上限要约 {need_mb / 1024:.1f} GB，"
                    f"余下约 {headroom_mb / 1024:.1f} GB 余量偏薄；能跑但别再叠一条")
    else:
        verdict = "safe"
        sentence = (f"按本机实测的每类峰值，这套上限要约 {need_mb / 1024:.1f} GB，"
                    f"还剩约 {(headroom_mb or 0) / 1024:.1f} GB 余量")

    return {
        "verdict": verdict,
        "sentence": sentence,
        "global": global_max,
        "per": per,
        "total_mb": total,
        "reserved_mb": reserved_mb,
        "available_mb": available_mb,
        "estimated_need_mb": need_mb,
        "headroom_mb": headroom_mb,
        "safety_ratio": SAFETY_RATIO,
        "peak_by_class_mb": peak_by_class,
        "avg_by_class_mb": per_class_mb,
        "worst_class": worst_class,
        "worst_class_mb": worst_mb,
        "detail": detail,
        "running_now": running_now or {},
        "basis": ("按这台机器最近成功作业的实测内存峰值推算"
                  if detail else "没有样本，只能按默认值说话"),
    }


def needs_acknowledge(result: dict) -> bool:
    """这一档是不是必须先勾"我知道有内存溢出风险"才让保存。

    只有 `over` 算危险档。`tight` 只是提醒——把提醒也做成拦截，
    人会一路点"我知道"学到"这个框永远要点"，真危险时就没人看了。
    """
    return result.get("verdict") == "over"
