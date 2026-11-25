"""智能体中心：状态现算、详情视图、配置卡片与审批视图。"""
from __future__ import annotations

import adapters
import agent_tools
import engines
from agents_pkg.constants import (
    AgentNotFound,
    EVENT_LABEL,
    JOB_TERMINAL_FAILED,
    RUN_AWAITING,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PAUSED,
    RUN_PLANNING,
    RUN_RUNNING,
    RUN_STATUS_LABEL,
    STEP_APPROVED,
    STEP_AWAITING,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_STATUS_LABEL,
)
from repositories.agents import STEP_FINAL, agent_runs, loads
from repositories.engine_instances import engine_instances
from repositories.jobs import jobs as job_repo
from repositories.model_center import model_center
from repositories.providers import providers as provider_repo
from repositories.reviews import reviews


def run_status(run, steps: list) -> str:
    """运行状态一律现算（库里只有 paused／cancelled 两位）。"""
    if run["cancelled"]:
        return RUN_CANCELLED
    if not steps:
        if run["plan_status"] == "failed":
            return RUN_FAILED
        if run["plan_status"] == "ready":
            return RUN_COMPLETED if run["finished_at"] else RUN_RUNNING
        return RUN_PLANNING
    if any(s["status"] == STEP_AWAITING for s in steps):
        return RUN_AWAITING
    if run["paused"]:
        return RUN_PAUSED
    if any(s["status"] in (STEP_RUNNING, STEP_APPROVED) for s in steps):
        return RUN_RUNNING
    pending = [s for s in steps if s["status"] == STEP_PENDING]
    if pending:
        return RUN_RUNNING
    if any(s["status"] == STEP_FAILED for s in steps):
        return RUN_FAILED
    if all(s["status"] in STEP_FINAL for s in steps):
        return RUN_COMPLETED
    return RUN_RUNNING


def step_view(step, job=None) -> dict:
    """步骤 → 界面那一行。"""
    args = loads(step["args_json"], {})
    preview = args.get("prompt") or args.get("name") or ""
    return {
        "id": step["id"],
        "seq": step["seq"],
        "tool_code": step["tool_code"],
        "tool_label": (agent_tools.TOOLS.get(step["tool_code"]) or {}).get(
            "label", "清单外的工具码（说明白名单改过）"),
        "risk": step["risk"],
        "risk_label": agent_tools.RISK_LABEL.get(step["risk"], step["risk"]),
        "status": step["status"],
        "status_label": STEP_STATUS_LABEL.get(step["status"], step["status"]),
        "args": args,
        "args_preview": (str(preview)[:80] if preview else
                         "、".join(f"{k}={v}" for k, v in list(args.items())[:3])),
        "args_hash": step["args_hash"],
        "approval_expires_at": step["approval_expires_at"] or "",
        "job_id": step["job_id"],
        "job_status": job["status"] if job is not None else "",
        "job_status_label": _job_label(job["status"]) if job is not None else "",
        "job_cost_micros": (job["cost_micros"] or 0) if job is not None else 0,
        "job_currency": (job["currency"] or "") if job is not None else "",
        "result": loads(step["result_json"], {}),
        "error_code": step["error_code"],
        "error_message": step["error_message"],
        "retry_count": step["retry_count"],
        "updated_at": step["updated_at"],
    }


def run_row_view(run, steps: list, jobs: dict | None = None) -> dict:
    jobs = jobs or {}
    views = [step_view(s, jobs.get(s["job_id"])) for s in steps]
    status = run_status(run, steps)
    counts: dict[str, int] = {}
    for s in views:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {
        "id": run["id"],
        "project_id": run["project_id"],
        "episode_id": run["episode_id"],
        "goal": run["goal"],
        "scene_key": run["scene_key"],
        "status": status,
        "status_label": RUN_STATUS_LABEL[status],
        "paused": bool(run["paused"]),
        "cancelled": bool(run["cancelled"]),
        "cancel_reason": run["cancel_reason"],
        "steps": views,
        "step_counts": counts,
        "model_snapshot": loads(run["model_snapshot_json"], {}),
        "plan_job_id": run["plan_job_id"],
        "approval_valid_seconds": run["approval_valid_seconds"],
        "idempotency_key": run["idempotency_key"] or "",
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "created_at": run["created_at"],
        "created_by": run["created_by"],
    }


def _job_map(jobs: dict, plan_job) -> dict:
    merged = dict(jobs)
    if plan_job is not None:
        merged[plan_job["id"]] = plan_job
    return merged


def _job_brief(job) -> dict | None:
    if job is None:
        return None
    return {
        "id": job["id"],
        "status": job["status"],
        "status_label": _job_label(job["status"]),
        "execution_mode": job["execution_mode"],
        "provider_code": job["provider_code"],
        "model_code": job["model_code"],
        "cost_micros": job["cost_micros"] or 0,
        "currency": job["currency"] or "",
        "error_code": job["error_code"],
        "error_message": job["error_message"],
        "locate": {"module": "generate", "sub": 0},
    }


def _event_view(row) -> dict:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "step_id": row["step_id"],
        "event_type": row["event_type"],
        "payload": loads(row["payload_json"], {}),
        "created_at": row["created_at"],
    }


def _job_label(status: str) -> str:
    from models import JOB_STATUSES
    return JOB_STATUSES.get(status, status)


def detail(run_id: int, user) -> dict:
    import access

    run = agent_runs.get(run_id)
    if run is None:
        raise AgentNotFound(f"没有这条运行：#{run_id}")
    access.ensure(user, int(run["project_id"]), "read")
    steps = agent_runs.steps(run_id)
    jobs = job_repo.get_many([s["job_id"] for s in steps if s["job_id"]]) \
        if any(s["job_id"] for s in steps) else {}
    plan_job = job_repo.get(run["plan_job_id"]) if run["plan_job_id"] else None
    out = run_row_view(run, steps, _job_map(jobs, plan_job))
    out["plan_job"] = _job_brief(plan_job)
    out["events"] = [_event_view(e) for e in agent_runs.events(run_id)]
    out["event_labels"] = EVENT_LABEL
    if plan_job is not None and plan_job["status"] in JOB_TERMINAL_FAILED:
        out["failure"] = {
            "where": "算计划",
            "code": plan_job["error_code"],
            "message": plan_job["error_message"],
        }
    else:
        blocked = next((s for s in steps if s["status"] == STEP_FAILED), None)
        out["failure"] = ({
            "where": f"第 {blocked['seq']} 步",
            "code": blocked["error_code"],
            "message": blocked["error_message"],
        } if blocked else None)
        if out["failure"] is None and run["plan_status"] == "failed":
            failed = next((e for e in out["events"]
                           if e["event_type"] == "run_failed"), None)
            payload = (failed or {}).get("payload") or {}
            out["failure"] = {
                "where": "算计划",
                "code": payload.get("code") or "",
                "message": payload.get("message") or "",
            }
    return out


def config_cards() -> dict:
    """本地智能体／云端智能体两页的数据，读路径不出网。"""
    cards = []
    for backend in ("local", "cloud"):
        row = provider_repo.slot("text", backend)
        card: dict = {
            "backend": backend,
            "backend_label": "本地智能体" if backend == "local" else "云端智能体",
            "configured": False,
            "provider": "",
            "model": "",
            "health_state": "unconfigured",
            "health_detail": "",
            "endpoint": "",
            "key_env": "",
            "secret_present": False,
            "resident": "",
            "cost_note": "",
            "enabled": False,
            "registration": None,
            "engines": [],
        }
        if row is None:
            card["health_detail"] = "能力×后端的这一行还没填 provider 与模型"
            cards.append(card)
            continue
        card["configured"] = bool(row["provider"].strip() and row["model"].strip())
        card["provider"], card["model"] = row["provider"], row["model"]
        card["enabled"] = bool(row["enabled"])
        card["is_default"] = bool(row["is_default"])
        card["key_env"] = row["key_env"] or ""
        if card["key_env"]:
            card["secret_present"] = bool(adapters.key_present(row["key_env"]))
        health = adapters.health(dict(row), probe_service=False)
        card["health_state"] = health["state"]
        card["health_detail"] = health["detail"]
        card["cost_note"] = agent_tools.cost_note("text", row["provider"],
                                                  row["model"], backend)
        reg = model_center.find_registration("text", backend, row["provider"],
                                             row["model"])
        if reg is not None:
            card["registration"] = {
                "id": reg["id"],
                "display_name": reg["display_name"],
                "artifact_path": reg["artifact_path"],
                "remote_model_id": reg["remote_model_id"],
                "enabled": bool(reg["enabled"]),
            }
        adapter = adapters.find("text", backend, row["provider"])
        if adapter is None:
            card["resident"] = "未登记适配器"
        elif adapter.code in engines.SERVICE_ENGINES:
            card["resident"] = "常驻服务型（模型加载由外部服务托管）"
        else:
            card["resident"] = "按次调用型（无常驻内存状态）"
        for instance in engine_instances.instances():
            if instance["engine_code"] == (adapter.code if adapter else ""):
                card["engines"].append({
                    "id": instance["id"],
                    "endpoint": instance["endpoint"],
                    "enabled": bool(instance["enabled"]),
                    "connection_type": instance["connection_type"],
                })
                if instance["enabled"] and not card["endpoint"]:
                    card["endpoint"] = instance["endpoint"]
        cards.append(card)
    return {
        "cards": cards,
        "caliber": ("配置状态反映模型槽位是否填报；健康状态为进入页面时的实时探活结果（不落库）；"
                    "驻留状态说明执行形态；运行统计来自实际编排任务记录，四项指标互不等价。"),
        "manifest_version": adapters.MANIFEST_VERSION,
    }


def _approval_route(run) -> dict:
    import agents as _ag

    snapshot = loads(run["model_snapshot_json"], {})
    if snapshot.get("route_source") == "chatgpt_oauth":
        route = _ag.plan_route()
        return {**route, "cost_note": route.get("cost_note") or route.get("reason", "")}
    return snapshot


def pending_approvals(project_ids: list[int], limit: int = 50) -> list[dict]:
    """正在等人选的步骤。审核中心那一页从这里取，签字仍写进 `review_item`。"""
    out = []
    for step in agent_runs.awaiting_steps(project_ids, limit):
        run = agent_runs.get(step["run_id"])
        if run is None:
            continue
        args = loads(step["args_json"], {})
        snapshot = _approval_route(run)
        view = step_view(step)
        view.update({
            "goal": run["goal"],
            "project_id": run["project_id"],
            "episode_id": run["episode_id"],
            "backend": snapshot.get("backend", ""),
            "provider": snapshot.get("provider", ""),
            "model": snapshot.get("model", ""),
            "data_out": (agent_tools.TOOLS.get(step["tool_code"]) or {}).get(
                "data_out", ""),
            "cost_note": snapshot.get("cost_note") or agent_tools.cost_note(
                "text",
                snapshot.get("provider", ""),
                snapshot.get("model", ""),
                snapshot.get("backend", "")),
            "full_args": args,
        })
        out.append(view)
    return out


def approval_view(step_id: int, user) -> dict:
    """审批页那一张卡：把人要点头的那一步完整摊开（参数全文＋送出去什么＋价）。"""
    import access

    step = agent_runs.step(step_id)
    if step is None:
        raise AgentNotFound(f"没有这一步：#{step_id}")
    run = agent_runs.get(step["run_id"])
    if run is None:
        raise AgentNotFound(f"这一步所属的运行已经不在了：#{step['run_id']}")
    access.ensure(user, int(run["project_id"]), "read")
    snapshot = _approval_route(run)
    tool = agent_tools.TOOLS.get(step["tool_code"]) or {}
    signs = reviews.signs(reviews.AGENT_ACTION, int(step["id"]), step["args_hash"])
    return {
        "step": step_view(step),
        "goal": run["goal"],
        "project_id": run["project_id"],
        "episode_id": run["episode_id"],
        "args": loads(step["args_json"], {}),
        "args_hash": step["args_hash"],
        "data_out": tool.get("data_out", ""),
        "verify": tool.get("verify", ""),
        "cost_note": snapshot.get("cost_note") or agent_tools.cost_note(
            "text",
            snapshot.get("provider", ""),
            snapshot.get("model", ""),
            snapshot.get("backend", "")),
        "model_snapshot": snapshot,
        "paid": step["risk"] == "paid",
        "cloud_confirm_note": (
            "批准这一步同时等于确认按供应商计费：签完之后才可能真的发生一笔费用。"
            "参数一改（指纹变了）旧批准失效，这笔确认也随之失效，平台会重新问一次。"
            if step["risk"] == "paid" else ""),
        "expires_at": step["approval_expires_at"] or "",
        "signed": [{"decision": s["decision"], "note": s["note"],
                    "signed_by": s["signed_by"], "signed_at": s["signed_at"]}
                   for s in signs],
        "binding": "批准绑定的是上面那组参数的指纹；参数一改指纹就变，旧批准自动作废",
    }


def overview(project_ids: list[int]) -> dict:
    """顶部四数＋工具表＋最近事件。"""
    runs = agent_runs.recent(project_ids, limit=200)
    steps_by_run = agent_runs.steps_for_runs([r["id"] for r in runs])
    job_ids = [s["job_id"] for steps in steps_by_run.values()
               for s in steps if s["job_id"]]
    jobs_by_id = job_repo.get_many(job_ids, with_derived=False) if job_ids else {}
    counts = {key: 0 for key in RUN_STATUS_LABEL}
    for run in runs:
        counts[run_status(run, steps_by_run.get(run["id"], []))] += 1
    cards = config_cards()
    usable = sum(1 for c in cards["cards"] if c["health_state"] == "ok")
    pending = len(agent_runs.awaiting_steps(project_ids, limit=200))
    return {
        "counts": {
            "runs": len(runs),
            "active": counts[RUN_RUNNING] + counts[RUN_PLANNING],
            "awaiting_approval": pending,
            "completed": counts[RUN_COMPLETED],
            "failed": counts[RUN_FAILED],
            "usable_configs": usable,
            "total_configs": len(cards["cards"]),
        },
        "status_labels": RUN_STATUS_LABEL,
        "caliber": ("运行数＝看得见的项目里最近 200 条；进行中＝在算计划＋执行中；"
                    "待审批＝正在等人签的步骤条数（不是运行条数）；"
                    "可用配置＝健康现查为 ok 的文本轨条数（本地＋云端两档）"),
        "runs": [run_row_view(r, steps_by_run.get(r["id"], []), jobs_by_id)
                 for r in runs[:20]],
        "tools": agent_tools.labels(),
        "events": [_event_view(e) for e in agent_runs.recent_events(20)],
        "configs": cards,
    }
