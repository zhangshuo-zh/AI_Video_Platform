import error_catalog
import json

from db import get_conn
from models import JOB_STATUSES
from repositories.jobs import jobs as job_repo
from repositories.pipeline import NODE_ATTENTION, NODE_DONE, NODE_FAILED, NODE_PENDING, NODE_REVIEW, NODE_RUNNING, NODE_SKIPPED, pipeline_repo

from .constants import EVENT_LABELS, NODE_STATUS_LABELS, NODE_TYPE_LABELS, PipelineMissing, RUN_STATUS_LABELS
from .gates import retry_gate
from .graph import _node_map, _snapshot_of
from .templates import _definition_of


# ---------- 视图 ----------
def run_status(run, counts: dict, *, retried: bool | None = None) -> str:
    """运行状态**全部现算**，库里只存 paused／cancelled 两个外生意愿位。

    为什么不全存：状态存两份就要互相同步，而"忘记同步"正是 v0.86 修掉的
    那两个真 bug 的形状。现算慢一点，但永远不会自相矛盾。

    `retried` 是"这条运行里有没有节点返过工"。单条详情自己问（一次查询无所谓），
    列表页一次把整组问完再传进来——否则 N 条运行就是 N 趟额外查询。
    """
    if run["cancelled"]:
        return "cancelled"
    if run["finished_at"]:
        if counts[NODE_FAILED]:
            return "failed"
        if counts[NODE_SKIPPED] or (retried if retried is not None
                                    else _has_retry(run["id"])):
            return "completed_with_errors"
        return "completed"
    if run["paused"]:
        return "paused"
    if counts[NODE_ATTENTION]:
        return "attention"
    if counts[NODE_FAILED]:
        return "failed"
    if counts[NODE_REVIEW]:
        return "waiting_review"
    if counts[NODE_DONE] or counts[NODE_RUNNING]:
        return "running"
    return "running"




def _has_retry(run_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM pipeline_node_run
                WHERE run_id = ? AND retry_count > 0 LIMIT 1""", (run_id,)).fetchone()
    return row is not None




def _display_status(node: dict, by_key: dict) -> str:
    """pending 的节点在界面上分两种：前置没完成＝blocked，前置都完成＝ready。

    这两位不入库（入了就要跟着依赖变化去维护，又是两套事实），只在读的时候算。
    """
    if node["status"] != NODE_PENDING:
        return node["status"]
    deps = json.loads(node.get("depends_on_json") or "[]")
    if all(by_key.get(d, {}).get("status") == NODE_DONE for d in deps):
        return "ready"
    return "blocked"




def node_view(row, by_key: dict, run=None, job=None) -> dict:
    """一格的对外视图。`job` 由 `run_view` 批量取好后传进来，不在这里逐格回库。

    `retryable`／`retry_hint` 不是"给前端的便利字段"：能不能重试是业务规则，
    规则写在 `retry_gate()` 一处，界面按它摆钮、接口按它拒 409——
    两边各判一次，就会出现"按钮在、点了报错"。
    """
    node = dict(row)
    display = _display_status(node, by_key)
    if job is None:
        job = job_repo.get(node["job_id"]) if node["job_id"] else None
    try:
        output = json.loads(node["output_json"] or "{}")
    except ValueError:
        output = {}
    # 没有 run 就没有判定依据（取消位挂在 run 上）——这时宁可不摆钮，
    # 也不给界面一个"看着能点、点了被接口拒"的字段。
    hint = retry_gate(node, run, job) if run is not None else "读不到这条运行，先刷新页面"
    return {
        "id": node["id"], "run_id": node["run_id"], "node_key": node["node_key"],
        "label": node["label"], "node_type": node["node_type"],
        "type_label": NODE_TYPE_LABELS.get(node["node_type"], node["node_type"]),
        "status": display,
        "status_label": NODE_STATUS_LABELS.get(display, display),
        "stored_status": node["status"],
        "depends_on": json.loads(node["depends_on_json"] or "[]"),
        "job_id": node["job_id"],
        "job_status": job["status"] if job else None,
        "job_status_label": (JOB_STATUSES.get(job["status"], job["status"])
                             if job else ""),
        "review_id": node["review_id"],
        "retry_count": int(node["retry_count"] or 0),
        "retryable": not hint,
        "retry_hint": hint,
        "error_code": node["error_code"], "error_message": node["error_message"],
        # 七字段诊断与生成中心那条作业抽屉同一个出处（`error_catalog`）。
        # 放在这里而不是让前端再打一次接口：这一格能不能重试已经算过了（`hint`），
        # 目录要的正是这个口试，前端再问一次只会拿到两份不一致的答复。
        "diagnosis": error_catalog.for_node(node, retry_hint=hint)["diagnosis"],
        "output": output,
        "started_at": node["started_at"] or "", "finished_at": node["finished_at"] or "",
        "created_at": node["created_at"],
    }




def run_view(run_id: int, *, with_events: bool = True) -> dict:
    run = pipeline_repo.get_run(run_id)
    if run is None:
        raise PipelineMissing("生产运行不存在")
    rows = pipeline_repo.nodes_for_run(run_id)
    by_key = _node_map(rows)
    counts = pipeline_repo.counts_for_run(run_id)
    status = run_status(run, counts)
    total = len(rows)
    done = counts[NODE_DONE] + counts[NODE_SKIPPED]
    snapshot = _snapshot_of(run)
    wanted = [row["job_id"] for row in rows if row["job_id"]]
    jobs_by_id = job_repo.get_many(wanted) if wanted else {}
    return {
        "id": run["id"], "project_id": run["project_id"],
        "project_name": run["project_name"],
        "episode_id": run["episode_id"], "episode_title": run["episode_title"],
        "episode_seq": run["episode_seq"],
        "template_id": run["template_id"], "template_name": run["template_name"],
        "template_version": run["template_version"],
        "status": status, "status_label": RUN_STATUS_LABELS.get(status, status),
        "paused": bool(run["paused"]), "cancelled": bool(run["cancelled"]),
        "cancel_reason": run["cancel_reason"],
        "counts": counts,
        "progress": {"total": total, "done": done, "failed": counts[NODE_FAILED],
                     "waiting_review": counts[NODE_REVIEW],
                     "attention": counts[NODE_ATTENTION],
                     "percent": round(done * 100.0 / total, 1) if total else 0.0},
        "shots": len(snapshot.get("shots") or []),
        "document": snapshot.get("document"),
        # 一格一条 Job 是这条详情页最主要的浪费：16 节就是 16 趟回库，
        # 而这一页是人盯着推进、会反复刷的那一页。一次按 id 批量取完再分发。
        "nodes": [node_view(row, by_key, run,
                            jobs_by_id.get(int(row["job_id"])) if row["job_id"] else None)
                  for row in rows],
        "events": ([_event_view(e) for e in pipeline_repo.events_for_run(run_id)]
                   if with_events else []),
        # 事件中文名随详情下发：界面不再自己维护一份 17 档的字典
        # （那份与 0031 的 CHECK 之间没有任何守卫）。
        "event_labels": EVENT_LABELS,
        "started_at": run["started_at"] or "", "finished_at": run["finished_at"] or "",
        "created_at": run["created_at"], "updated_at": run["updated_at"],
    }




def _event_view(row) -> dict:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except ValueError:
        payload = {}
    return {"id": row["id"], "event_type": row["event_type"],
            "node_id": row["node_id"], "node_key": row["node_key"],
            "node_label": row["node_label"] or "", "payload": payload,
            "created_at": row["created_at"]}




def runs_for_project(project_id: int) -> list[dict]:
    """项目里的运行记录。列表不带节点与事件——那是详情页的事。

    一共三趟查询，与运行条数无关：运行列表（`list_by_project` 带回的就是整行
    `r.*` ＋ 模板名／集名／集号）、这组运行的节点状态计数、这组运行里哪些返过工。
    原来每条运行再 `get_run()` 一遍是把**已经在手的同一行**又读一次，
    counts 与 retry 也各问一遍——20 条运行就是六十趟。
    """
    rows = pipeline_repo.list_by_project(project_id)
    if not rows:
        return []
    run_ids = [int(row["id"]) for row in rows]
    counts_by_run = pipeline_repo.counts_for_runs(run_ids)
    retried = pipeline_repo.retried_run_ids(run_ids)
    out = []
    for row in rows:
        counts = counts_by_run[int(row["id"])]
        status = run_status(row, counts, retried=int(row["id"]) in retried)
        total = sum(counts.values())
        done = counts[NODE_DONE] + counts[NODE_SKIPPED]
        out.append({
            "id": row["id"], "project_id": row["project_id"],
            "episode_id": row["episode_id"], "episode_title": row["episode_title"],
            "episode_seq": row["episode_seq"],
            "template_id": row["template_id"], "template_name": row["template_name"],
            "template_version": row["template_version"],
            "status": status, "status_label": RUN_STATUS_LABELS.get(status, status),
            "paused": bool(row["paused"]), "cancelled": bool(row["cancelled"]),
            "counts": counts,
            "progress": {"total": total, "done": done, "failed": counts[NODE_FAILED],
                         "waiting_review": counts[NODE_REVIEW],
                         "attention": counts[NODE_ATTENTION],
                         "percent": round(done * 100.0 / total, 1) if total else 0.0},
            "started_at": row["started_at"] or "",
            "finished_at": row["finished_at"] or "",
            "created_at": row["created_at"],
        })
    return out




def template_view(row) -> dict:
    definition = _definition_of(row)
    return {
        "id": row["id"], "name": row["name"], "version": row["version"],
        "description": row["description"], "enabled": bool(row["enabled"]),
        "builtin": bool(row["builtin"]),
        "nodes": [{"key": n.get("key"), "type": n.get("type"),
                   "label": n.get("label") or NODE_TYPE_LABELS.get(n.get("type"),
                                                                  n.get("type")),
                   "depends_on": n.get("depends_on") or [],
                   "fan_out": n.get("fan_out")}
                  for n in definition.get("nodes", []) if isinstance(n, dict)],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
