"""production stats 业务域。"""
from __future__ import annotations

from log import get_logger as _get_logger

from pathlib import Path
from repositories.jobs import jobs as job_repo
from repositories.reviews import reviews
import diskplan
import error_catalog
import subprocess

_audit_log = _get_logger(__name__)



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
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _audit_log.debug("_machine_total_gb_fallback error=%s", type(exc).__name__)
        total = 0.0
    if not total:
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024 / 1024 ** 3
                    break
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            _audit_log.debug("_machine_total_gb_fallback error=%s", type(exc).__name__)
            total = 0.0
    _MACHINE_TOTAL_GB = round(total, 1)
    return _MACHINE_TOTAL_GB



_MACHINE_TOTAL_GB: float | None = None



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
