import csv
import gitrepo
import io
import json

from repositories.dashboard import dashboard
from repositories.documents import documents as docs


# ---------- 项目看板与导出（W16） ----------
_DASH_STATUS = {
    "draft": "草稿", "active": "在产", "done": "定稿",
    "queued": "排队中", "starting": "启动中", "running": "运行中",
    "succeeded": "成功", "failed": "失败", "cancel_requested": "取消中",
    "cancelled": "已取消", "needs_attention": "待核对",
}




def project_dashboard(project_id: int) -> dict:
    data = dashboard.summary(project_id)
    return {
        "episodes": [{"status": k, "label": _DASH_STATUS.get(k, k), "count": v}
                     for k, v in sorted(data["episodes"].items())],
        "episode_total": data["episode_total"],
        "shots": data["shots"], "assets": data["assets"], "documents": data["documents"],
        "jobs": [{"status": k, "label": _DASH_STATUS.get(k, k), "count": v}
                 for k, v in sorted(data["jobs"].items())],
        "job_total": data["job_total"],
        "costs": data["costs"],
        "gates": [{"status": k, "label": {"passed": "通过", "failed": "未通过"}.get(k, k),
                   "count": v} for k, v in sorted(data["gates"].items())],
        "signs": [{"decision": k, "label": {"approved": "通过", "rejected": "驳回",
                                            "returned": "退回"}.get(k, k), "count": v}
                  for k, v in sorted(data["signs"].items())],
        "audio": [{"status": k, "label": {"draft": "草稿", "queued": "排队中",
                                          "ready": "已就绪", "failed": "失败"}.get(k, k),
                   "count": v} for k, v in sorted(data["audio"].items())],
    }




def _export_rows(project_id: int) -> list[dict]:
    rows = []
    for row in dashboard.shot_export(project_id):
        try:
            paths = json.loads(row["job_paths"] or "[]")
        except ValueError:
            paths = []
        rows.append({
            "episode_seq": row["episode_seq"], "episode_title": row["episode_title"],
            "seq": row["seq"], "summary": row["summary"], "prompt": row["prompt"],
            "duration": row["duration"], "assets": row["asset_names"] or "",
            "job_id": row["job_id"] or "", "job_status": row["job_status"] or "",
            "paths": "；".join(str(p) for p in paths),
        })
    return rows




def shots_csv(project_id: int) -> str:
    """镜头清单 CSV。带 BOM，Excel 打开中文才不是乱码。"""
    out = io.StringIO()
    out.write("\ufeff")
    writer = csv.writer(out)
    writer.writerow(["集序", "集标题", "镜号", "内容", "提示词", "时长(秒)",
                     "引用资产", "最新作业", "作业状态", "产物路径"])
    for r in _export_rows(project_id):
        writer.writerow([r["episode_seq"], r["episode_title"], r["seq"],
                         r["summary"], r["prompt"], r["duration"], r["assets"],
                         r["job_id"], r["job_status"], r["paths"]])
    return out.getvalue()




def shots_markdown(project_id: int, project_name: str) -> str:
    lines = [f"# {project_name} · 镜头清单", "",
             "| 集 | 镜 | 内容 | 提示词 | 时长 | 引用资产 | 最新作业 | 产物 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in _export_rows(project_id):
        cell = lambda v: str(v or "").replace("|", "\\|").replace("\n", " ")
        lines.append("| {e} | {s} | {m} | {p} | {d}s | {a} | {j} {st} | {path} |".format(
            e=cell(r["episode_title"]), s=r["seq"], m=cell(r["summary"]),
            p=cell(r["prompt"]), d=r["duration"], a=cell(r["assets"]),
            j=r["job_id"] and f"#{r['job_id']}" or "—", st=cell(r["job_status"]),
            path=cell(r["paths"])))
    return "\n".join(lines) + "\n"




def ensure_workspace() -> str:
    """启动时把工作区准备成可用状态，返回一句状态给健康检查显示。

    幂等：仓库已经在了就什么都不做。顺带清一次过期的锁行
    （`drop_expired` 只影响"几个人在编辑"这种计数，正确性不靠它）。
    """
    docs.drop_expired()
    gitrepo.ensure_repo()
    return gitrepo.status_line()
