"""智能体中心：worker 节拍推进（tick）与运行控制（pause / resume / cancel）。"""
from __future__ import annotations

from datetime import datetime, timedelta

import agent_tools
import jobfiles
from agents_pkg.constants import (
    AgentConflict,
    AgentInvalid,
    AgentNotFound,
    JOB_TERMINAL_FAILED,
    MAX_STEPS_PER_TICK,
    RUN_COMPLETED,
    RUN_FAILED,
    STEP_APPROVED,
    STEP_AWAITING,
    STEP_EXPIRED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SUCCEEDED,
)
from agents_pkg.planner import parse_plan
from agents_pkg.views import run_row_view, run_status
from repositories.agents import STEP_FINAL, agent_runs, loads
from repositories.jobs import jobs as job_repo
from repositories.reviews import reviews

from log import get_logger

_logger = get_logger(__name__)


def tick() -> dict:
    """worker 的推进节拍：把所有还没结束的运行往前推一格。"""
    stats = {"runs": 0, "advanced": 0, "awaiting": 0, "completed": 0,
             "failed": 0, "errors": 0}
    for run in agent_runs.active():
        stats["runs"] += 1
        try:
            stats.update(_tick_run(run))
        except Exception as exc:                 # noqa: BLE001
            stats["errors"] += 1
            _record_error(run, exc)
    return stats


def _tick_run(run) -> dict:
    out = {"advanced": 0, "awaiting": 0, "completed": 0, "failed": 0}
    steps = agent_runs.steps(run["id"])

    if not steps:
        job = job_repo.get(run["plan_job_id"]) if run["plan_job_id"] else None
        if job is None:
            _fail_run(run, "plan_missing", "算计划的那条作业已经不在了"
                                           "（可能随项目删除）；这一步无法继续")
            out["failed"] += 1
            return out
        if job["status"] in JOB_TERMINAL_FAILED:
            _fail_run(run, job["error_code"] or "plan_failed",
                      job["error_message"] or "算计划失败")
            out["failed"] += 1
            return out
        if job["status"] != "succeeded":
            return out
        _ingest_plan(run, job)
        steps = agent_runs.steps(run["id"])
        if not steps:
            return out

    now = datetime.now()
    for step in steps:
        if step["status"] != STEP_AWAITING or not step["approval_expires_at"]:
            continue
        if _parse_time(step["approval_expires_at"]) > now:
            continue
        agent_runs.update_step(step["id"], {
            "status": STEP_EXPIRED,
            "error_code": "approval_expired",
            "error_message": "审批有效期到了，这一步没有批出去",
        })
        agent_runs.add_event(
            "step_expired",
            run_id=run["id"],
            step_id=step["id"],
            payload={"seq": step["seq"], "expires_at": step["approval_expires_at"]},
        )
        out["advanced"] += 1

    if run["paused"] or run["cancelled"]:
        return out

    for _ in range(MAX_STEPS_PER_TICK):
        steps = agent_runs.steps(run["id"])
        current = next((s for s in steps if s["status"] not in STEP_FINAL), None)
        if current is None:
            status = run_status(run, steps)
            if status in (RUN_COMPLETED, RUN_FAILED):
                agent_runs.finish(run["id"])
                agent_runs.add_event(
                    "run_completed" if status == RUN_COMPLETED else "run_failed",
                    run_id=run["id"],
                    payload={"status": status},
                )
                out["completed" if status == RUN_COMPLETED else "failed"] += 1
            return out
        moved = _advance(run, current)
        if not moved:
            if current["status"] == STEP_AWAITING:
                out["awaiting"] += 1
            return out
        out["advanced"] += 1
    return out


def _advance(run, step) -> bool:
    """推进一步。返回 True 表示这一步的状态确实变了（可以继续往后看）。"""
    status = step["status"]

    if status == STEP_PENDING:
        if step["risk"] == "read":
            _execute(run, step)
            return True
        expires = (datetime.now() + timedelta(seconds=run["approval_valid_seconds"])
                   ).strftime("%Y-%m-%d %H:%M:%S")
        agent_runs.update_step(step["id"], {
            "status": STEP_AWAITING,
            "approval_expires_at": expires,
        })
        agent_runs.add_event(
            "approval_requested",
            run_id=run["id"],
            step_id=step["id"],
            payload={
                "seq": step["seq"],
                "tool_code": step["tool_code"],
                "args_hash": step["args_hash"],
                "risk": step["risk"],
                "expires_at": expires,
                "target_kind": reviews.AGENT_ACTION,
                "target_id": step["id"],
            },
        )
        return False

    if status == STEP_AWAITING:
        decision = _approval_decision(step)
        if decision is None:
            return False
        if decision == "approved":
            agent_runs.update_step(step["id"], {"status": STEP_APPROVED})
            agent_runs.add_event(
                "approval_granted",
                run_id=run["id"],
                step_id=step["id"],
                payload={"seq": step["seq"]},
            )
            return True
        agent_runs.update_step(step["id"], {
            "status": STEP_FAILED,
            "error_code": f"approval_{decision}",
            "error_message": (
                "这一步被驳回；参数若已改动请重新发起运行"
                "（指纹变了旧批准自然失效）"
                if decision == "rejected" else
                "这一步被退回；平台不自动改参数重提，请人工处理或重新发起"
            ),
        })
        agent_runs.add_event(
            "approval_rejected",
            run_id=run["id"],
            step_id=step["id"],
            payload={"seq": step["seq"], "decision": decision},
        )
        return True

    if status == STEP_APPROVED:
        _execute(run, step)
        return True

    if status == STEP_RUNNING:
        if not step["job_id"]:
            agent_runs.update_step(step["id"], {
                "status": STEP_FAILED,
                "error_code": "lost",
                "error_message": "步骤停在执行中但没有关联作业，平台不猜它做没做成；请重新发起",
            })
            agent_runs.add_event(
                "step_failed",
                run_id=run["id"],
                step_id=step["id"],
                payload={"seq": step["seq"], "code": "lost"},
            )
            return True
        job = job_repo.get(step["job_id"])
        if job is None:
            agent_runs.update_step(step["id"], {
                "status": STEP_FAILED,
                "error_code": "job_missing",
                "error_message": "关联的作业行已经不在了",
            })
            return True
        if job["status"] in JOB_TERMINAL_FAILED:
            agent_runs.update_step(step["id"], {
                "status": STEP_FAILED,
                "error_code": job["error_code"] or "job_failed",
                "error_message": job["error_message"] or "作业失败",
            })
            agent_runs.add_event(
                "step_failed",
                run_id=run["id"],
                step_id=step["id"],
                payload={
                    "seq": step["seq"],
                    "job_id": job["id"],
                    "code": job["error_code"],
                },
            )
            return True
        if job["status"] != "succeeded":
            return False
        _verify_and_succeed(run, step, job)
        return True
    return False


def _approval_decision(step) -> str | None:
    """读审核中心那一条：'approved' / 'rejected' / 'returned' / None。"""
    rows = reviews.signs(reviews.AGENT_ACTION, int(step["id"]), step["args_hash"])
    rows = [r for r in rows if r["anchor"] == reviews.AGENT_ACTION_ANCHOR]
    if not rows:
        return None
    return rows[-1]["decision"]


def _execute(run, step) -> None:
    """调工具，把结果落成步骤状态。"""
    authorized = step["status"] == STEP_APPROVED
    ctx = {
        "run_id": int(run["id"]),
        "seq": int(step["seq"]),
        "project_id": int(run["project_id"]),
        "episode_id": run["episode_id"],
        "actor_id": run["created_by"],
        "cloud_confirmed": authorized,
    }
    args = loads(step["args_json"], {})
    agent_runs.update_step(step["id"], {"status": STEP_RUNNING})
    agent_runs.add_event(
        "step_started",
        run_id=run["id"],
        step_id=step["id"],
        payload={"seq": step["seq"], "tool_code": step["tool_code"], "risk": step["risk"]},
    )
    try:
        result = agent_tools.execute(step["tool_code"], args, ctx)
    except (agent_tools.ToolUnknown, agent_tools.ToolArgsInvalid) as exc:
        agent_runs.update_step(step["id"], {
            "status": STEP_FAILED,
            "error_code": "tool_rejected",
            "error_message": str(exc),
        })
        agent_runs.add_event(
            "tool_rejected",
            run_id=run["id"],
            step_id=step["id"],
            payload={"seq": step["seq"], "reason": str(exc)},
        )
        return
    except agent_tools.ToolExecutionError as exc:
        agent_runs.update_step(step["id"], {
            "status": STEP_FAILED,
            "error_code": exc.code,
            "error_message": str(exc),
        })
        agent_runs.add_event(
            "step_failed",
            run_id=run["id"],
            step_id=step["id"],
            payload={"seq": step["seq"], "code": exc.code},
        )
        return
    except Exception as exc:                   # noqa: BLE001
        agent_runs.update_step(step["id"], {
            "status": STEP_FAILED,
            "error_code": "internal",
            "error_message": f"{type(exc).__name__}：内部错误，详情看 worker 日志",
        })
        agent_runs.add_event(
            "step_failed",
            run_id=run["id"],
            step_id=step["id"],
            payload={"seq": step["seq"], "code": "internal"},
        )
        return

    if result.get("job_id"):
        agent_runs.update_step(step["id"], {
            "status": STEP_RUNNING,
            "job_id": int(result["job_id"]),
            "result_json": result,
        })
        agent_runs.add_event(
            "job_submitted",
            run_id=run["id"],
            step_id=step["id"],
            payload={"seq": step["seq"], "job_id": int(result["job_id"])},
        )
        return
    agent_runs.update_step(step["id"], {
        "status": STEP_SUCCEEDED,
        "result_json": result,
    })
    agent_runs.add_event(
        "step_succeeded",
        run_id=run["id"],
        step_id=step["id"],
        payload={"seq": step["seq"], "tool_code": step["tool_code"]},
    )


def _verify_and_succeed(run, step, job) -> None:
    """以真实业务状态与产物为准，不以 Agent 自述为准。"""
    path = jobfiles.artifact_at(job["result_paths_json"], 0)
    if path is None:
        agent_runs.update_step(step["id"], {
            "status": STEP_FAILED,
            "error_code": "artifact_missing",
            "error_message": "作业显示成功，但产物清单里读不到文件；"
                             "按失败处理，不拿一个空结果往下走",
        })
        agent_runs.add_event(
            "step_failed",
            run_id=run["id"],
            step_id=step["id"],
            payload={"seq": step["seq"], "code": "artifact_missing"},
        )
        return
    result = loads(step["result_json"], {})
    result.update({
        "verified": True,
        "artifact": jobfiles.relative(str(path)),
        "bytes": path.stat().st_size,
    })
    agent_runs.update_step(step["id"], {
        "status": STEP_SUCCEEDED,
        "result_json": result,
    })
    agent_runs.add_event(
        "step_succeeded",
        run_id=run["id"],
        step_id=step["id"],
        payload={"seq": step["seq"], "job_id": job["id"], "bytes": result["bytes"]},
    )


def _ingest_plan(run, job) -> None:
    """计划作业成功 → 解析 → 落步骤。"""
    path = jobfiles.artifact_at(job["result_paths_json"], 0)
    if path is None:
        _fail_run(run, "artifact_missing", "计划作业成功但产物读不到，无法得到步骤")
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _fail_run(run, "artifact_unreadable", f"读计划失败：{type(exc).__name__}")
        return
    try:
        steps = parse_plan(text)
    except AgentInvalid as exc:
        _fail_run(run, "plan_unparsable", str(exc))
        return
    agent_runs.set_plan(run["id"], steps)
    agent_runs.set_plan_status(run["id"], "ready")
    snapshot = loads(run["model_snapshot_json"], {})
    for item in steps:
        agent_runs.add_step(
            run["id"],
            item["seq"],
            tool_code=item["tool"],
            args=item["args"],
            args_hash=item["args_hash"],
            risk=item["risk"],
            status=STEP_PENDING,
        )
    agent_runs.add_event(
        "plan_ready",
        run_id=run["id"],
        payload={
            "steps": len(steps),
            "tools": [s["tool"] for s in steps],
            "route": f"{snapshot.get('backend')}/{snapshot.get('provider')}/"
                     f"{snapshot.get('model')}",
        },
    )


def _mark_plan_failed(run) -> None:
    agent_runs.set_plan_status(run["id"], "failed")


def _fail_run(run, code: str, message: str) -> None:
    _mark_plan_failed(run)
    agent_runs.cancel_open_steps(run["id"])
    agent_runs.finish(run["id"])
    agent_runs.add_event(
        "run_failed",
        run_id=run["id"],
        payload={"code": code, "message": message[:300]},
    )


def _record_error(run, exc: Exception) -> None:
    try:
        agent_runs.add_event(
            "preflight_warning",
            run_id=run["id"],
            payload={"code": "tick_error", "message": f"{type(exc).__name__}"[:200]},
        )
    except Exception as record_exc:             # 最后一道错误记录边界
        _logger.error("agent_error_record_failed error=%s", type(record_exc).__name__)


def _parse_time(text: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return datetime.now()


def pause(run_id: int, user) -> dict:
    return _will(run_id, user, paused=True, event="run_paused")


def resume(run_id: int, user) -> dict:
    return _will(run_id, user, paused=False, event="run_resumed")


def cancel(run_id: int, user, reason: str = "") -> dict:
    return _will(run_id, user, cancelled=True, event="run_cancelled", reason=reason)


def _will(run_id: int, user, *, paused: bool | None = None,
          cancelled: bool | None = None, event: str, reason: str = "") -> dict:
    """外生意愿的三合一入口。"""
    import access

    run = agent_runs.get(run_id)
    if run is None:
        raise AgentNotFound(f"没有这条运行：#{run_id}")
    access.ensure(user, int(run["project_id"]), "read")
    if cancelled is None and run["cancelled"]:
        raise AgentConflict("这条运行已经取消，不能恢复：待审的步骤已经作废")
    if paused and run["cancelled"]:
        raise AgentConflict("这条运行已经取消，暂停没有意义")
    updated = agent_runs.set_will(run_id, paused=paused, cancelled=cancelled,
                                  reason=reason)
    if not cancelled:
        agent_runs.add_event(event, run_id=run_id, actor_id=user["id"],
                             payload={"reason": reason})
    if cancelled:
        n = agent_runs.cancel_open_steps(run_id)
        agent_runs.add_event("run_cancelled", run_id=run_id, actor_id=user["id"],
                             payload={"reason": reason, "voided_steps": n})
    return {"run": run_row_view(updated, agent_runs.steps(run_id))}
