"""智能体中心的服务层：计划、推进、验证、视图（0041 三张表的唯一使用者）。

一句话职责边界：**这里编排，不执行模型，也不写业务事实**。
算计划＝提交一条普通文本 Job（走 `services.submit_job` 那唯一一条路）；
干活＝调 `agent_tools` 里的白名单工具，而工具本身又是既有服务层的一层薄壳。
所以"智能体"不是一个新引擎，是**把已有动作按顺序串起来、并在花钱与改事实之前
停下来等人点头**的那一层。

四条纪律，每条都对着本项目的一条既有红线：

1. **不开第二条队列**（不变量 2）。计划作业与步骤作业全在 `job` 表里，
   worker 那一个队列串行跑；本文件一次都不 `subprocess`、一次都不发 HTTP。
2. **执行事实不复制**。步骤只存"关联哪条 Job"；状态、成本、日志、产物
   每次现读 `job` 与 `jobfiles`。判据同 0026 对产线说的话。
3. **调度器住在 worker 进程**（裁定 D3）。`tick()` 由 worker 的循环调，
   API 进程里没有任何后台线程——所以页面看到的进度是"上一次节拍推到的地方"，
   刷新或稍后再看为准，与产线页同一口径。
4. **审批只有落库一处**。本文件不写 `review_item`，只读它来判断"人点没点头"；
   写那一行的是审核中心（`routers/review.py`，不变量 3）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import adapters
import agent_tools
import engines  # noqa: F811  —— 只为 SERVICE_ENGINES 这一族判断
import jobfiles
from db import transaction
from repositories.agents import (STEP_APPROVED, STEP_AWAITING, STEP_CANCELLED,
                                 STEP_EXPIRED, STEP_FAILED, STEP_FINAL,
                                 STEP_PENDING, STEP_RUNNING, STEP_SKIPPED,
                                 STEP_SUCCEEDED,
                                 agent_runs, loads)
from repositories.content import episodes as episodes_repo
from repositories.engine_instances import engine_instances
from repositories.jobs import jobs as job_repo
from repositories.model_center import model_center
from repositories.providers import providers as provider_repo
from repositories.reviews import reviews

# 一次计划最多排几步。上限不是给模型出难题，是给成本封顶：
# 每一步都可能是一条付费作业，"计划"没有步数上限就等于账单没有上限。
MAX_PLAN_STEPS = 12
# 一轮 tick 里一个运行最多连着走几步（读工具当场做完，付费/写步骤会停下来等审批）。
# 与上面那个上限同源，防的是"计划里全是读步骤，一轮把 CPU 吃掉"。
MAX_STEPS_PER_TICK = 6

RUN_PLANNING = "planning"
RUN_AWAITING = "awaiting_approval"
RUN_RUNNING = "running"
RUN_PAUSED = "paused"
RUN_CANCELLED = "cancelled"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"

RUN_STATUS_LABEL = {
    RUN_PLANNING: "在算计划",
    RUN_AWAITING: "等你审批",
    RUN_RUNNING: "进行中",
    RUN_PAUSED: "已暂停",
    RUN_CANCELLED: "已取消",
    RUN_COMPLETED: "已完成",
    RUN_FAILED: "失败",
}

STEP_STATUS_LABEL = {
    STEP_PENDING: "待处理", STEP_AWAITING: "等你审批", STEP_APPROVED: "已批准",
    STEP_RUNNING: "执行中", STEP_SUCCEEDED: "成功", STEP_FAILED: "失败",
    STEP_SKIPPED: "跳过", STEP_CANCELLED: "作废", STEP_EXPIRED: "审批过期",
}



# 事件类型的中文名。**这是唯一一份**：原来它只住在界面（`views/agents.js` 的
# `EVENT_LABEL`），服务端不下发，于是库里的 CHECK 与界面标签之间没有任何守卫——
# 清单改了，界面会安静地少说一档，而那句话正好是「这一条发生了什么」。
# 这正是《写作与引用规范》§2 说的"第二份定义"那一类（此前登记在《系统设计说明书》§16 第 27 条）。
# 现在中文名收在服务端，随 `GET /agent/runs`（列表）与详情一起下发；
# `tests/test_agents.py::test_事件的每一档都有中文名` 比 EVENT_TYPES 与这本字典的键
# **且只比它们**——少一档就红，多写一档不存在的也红。
EVENT_LABEL = {
    "run_created": "已发起", "plan_requested": "去算计划",
    "plan_ready": "计划回来了", "plan_failed": "计划失败",
    "run_started": "开始推进", "run_paused": "已暂停",
    "run_resumed": "已恢复", "run_cancelled": "已取消",
    "run_completed": "已完成", "run_failed": "运行失败",
    "step_ready": "步骤就绪", "step_started": "执行中",
    "step_succeeded": "步骤成功", "step_failed": "步骤失败",
    "step_skipped": "步骤跳过", "step_retried": "步骤重试",
    "step_expired": "审批过期",
    "approval_requested": "在等你批", "approval_granted": "已批准",
    "approval_rejected": "已驳回", "job_submitted": "排了一条作业",
    "preflight_warning": "提示", "tool_rejected": "工具被拒",
}
JOB_TERMINAL_FAILED = ("failed", "cancelled", "needs_attention")


class AgentInvalid(ValueError):
    """发起参数不成立（路由 422）。与 `services.JobInvalid` 同一分工：
    NoRoute 是"现在没有路线"，这一条是"这次请求本身不对"。"""


class AgentNotFound(LookupError):
    """对象不存在或不属于这个项目——路由层翻成 404，不泄露它是否存在。"""


class AgentConflict(RuntimeError):
    """状态不对（已取消的运行不能恢复……）。翻 409。"""


# ---------- 计划提示词 ----------
def _tool_menu() -> str:
    """把白名单摊给模型看。

    只摊**声明里已有的东西**（码、参数名与类型、枚举取值、风险档）。
    不写"你可以执行 shell"这种句子——模型不会照单全收，但它一定会试着越界，
    而越界那一步会被 `agent_tools.validate()` 拒掉。这里的作用是让它在
    能做的范围内少试错，不是给它一个权限的错觉。
    """
    lines = []
    for code, tool in agent_tools.TOOLS.items():
        args = []
        for name, spec in tool["args"].items():
            kind = spec["type"]
            if kind == "enum":
                kind = "、".join(f"「{c}」" for c in spec.get("choices") or ())
            args.append(f"{name}({kind}{'' if spec.get('required') else '，可省略'})")
        lines.append(f"- {code}：{tool['label']}；风险档 {tool['risk']}；"
                     f"参数 {{{', '.join(args)}}}")
    return "\n".join(lines)


def plan_prompt(goal: str, episode, numbers: dict) -> str:
    """算计划那次调用的提示词。

    送出去的内容只有：目标原话、集号、镜头数、场景名（说明书 §2「内容最小化」）。
    **正文一个字都不进来**——计划阶段要的是"分几步、用哪些工具"，
    正文对这个问题没有增量，而它是最贵也最敏感的那部分。
    """
    seq = episode["seq"] if episode is not None else None
    scope = f"第 {seq} 集（共 {numbers.get('shots', 0)} 个镜头）" if episode \
        else "整个项目（未指定集）"
    return (
        "你是这个 AI 动漫生产平台的编排助手。请用【可用的工具】把目标拆成 1—"
        f"{MAX_PLAN_STEPS} 个有序步骤，只输出一个 JSON 数组，不要任何解释文字、"
        "不要 markdown 代码块标记。\n\n"
        "数组每一项形如 {\"tool\": \"工具码\", \"args\": {参数对象}, "
        "\"why\": \"一句话理由\"}。工具码必须逐字来自下面这份清单，"
        "参数只能用该工具声明过的那几个键；写错码或多塞键，平台会直接拒绝执行。\n\n"
        f"【可用的工具】\n{_tool_menu()}\n\n"
        f"【目标】{goal}\n"
        f"【范围】{scope}\n"
        "【规则】先读后写：需要事实先安排只读工具；"
        "花钱的（risk=paid）与改正式业务事实的（risk=formal）步骤各不超过 2 个；"
        "不要为了凑步数安排空步骤。\n"
    )


def parse_plan(text: str) -> list[dict]:
    """把模型输出收成规范化步骤。

    两处宽容＋一处绝不宽容：
    * 宽容一：允许外面裹一段说明或 ```json 代码块——真实的模型输出经常带（本机
      qwen3:8b 实测就是裸 JSON，但云端那一档不一定）。取第一个 `[` 到最后一个 `]`。
    * 宽容二：`why` 可以缺，理由不是执行所需的。
    * 绝不宽容：工具码不在白名单、参数多了少了、步数超上限——一律整体失败，
      不做"跳过坏步骤保留好步骤"。半套计划比没有计划更危险：
      人审批时看到的是"第 2 步"，而它原本是第 3 步。
    """
    raw = (text or "").strip()
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        raise AgentInvalid("计划里没有解析出 JSON 数组；这次不自动重跑，"
                           "请看运行详情里的原文再决定")
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError as exc:
        raise AgentInvalid(f"计划不是合法 JSON：{exc}") from exc
    if not isinstance(data, list) or not data:
        raise AgentInvalid("计划是一个空数组：没有任何一步要做，"
                           "这通常说明目标写得太宽，请重说一句要什么")
    if len(data) > MAX_PLAN_STEPS:
        raise AgentInvalid(f"计划有 {len(data)} 步，超过上限 {MAX_PLAN_STEPS}；"
                           "请拆成两次运行（步数上限就是成本上限）")
    steps: list[dict] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise AgentInvalid(f"计划第 {index} 项不是对象")
        code = str(item.get("tool") or "").strip()
        try:
            args = agent_tools.validate(code, item.get("args") or {})
        except (agent_tools.ToolUnknown, agent_tools.ToolArgsInvalid) as exc:
            raise AgentInvalid(f"计划第 {index} 项被拒：{exc}") from exc
        steps.append({"seq": index, "tool": code,
                      "args": args,
                      "args_hash": agent_tools.args_hash(code, args),
                      "risk": agent_tools.risk_of(code),
                      "why": str(item.get("why") or "").strip()[:300]})
    return steps


# ---------- 发起 ----------
def plan_route(scene_key: str = "") -> dict:
    """这一步会走哪条轨、要花多少钱、往外送什么。

    默认轨的唯一来源仍然是 `services.resolve_provider()`——本函数只转述它，
    不另算一份。策略表（`model_policy`）在这里只用于**告诉人**这一类任务
    的建议轨是什么（裁定 MC1：只提示不自动切），
    所以字段分开：`route` 是真会走的，`suggestion` 是建议。
    """
    from services import NoRoute, resolve_provider

    hint: dict = {}
    if scene_key:
        for row in model_center.policies():
            if row["scene_key"] == scene_key:
                hint = {"scene_label": row["scene_label"],
                        "preference": row["preference"],
                        "suggestion": f"{row['slot_provider']}/{row['slot_model']}"
                                      if row["slot_provider"] else "",
                        "policy_enabled": bool(row["enabled"])}
                break
    try:
        route = resolve_provider("text")
    except NoRoute as exc:
        return {"ok": False, "reason": str(exc), "backend": "", "provider": "",
                "model": "", "cost_note": "", **hint}
    out = {"ok": True, "backend": route["backend"], "provider": route["provider"],
           "model": route["model"],
           "cost_note": agent_tools.cost_note("text", route["provider"],
                                              route["model"], route["backend"]),
           **hint}
    # 授权要求分两档说清楚：云端＝要花钱＋内容出本机；本地＝不出本机，无需确认
    out["needs_cloud_confirm"] = out["backend"] == "cloud"
    out["data_out"] = (f"计划提示词（目标原话＋集号＋镜头数，不含正文）会发给 "
                       f"{out['provider']}" if out["backend"] == "cloud"
                       else "计划提示词只在本机处理，不外发")
    return out


def plan_estimate(route: dict):
    """给"算计划"那条云端作业凑一张预计成本单——数字全部来自适配器清单。

    为什么必须有这一张：`services._cost_of()` 对云端作业的三条口径里，
    「必须带预计成本（数量、单位、单价、币种与价格来源）」这一条没有商量余地，
    而它要的正是"人点头时看见的那张单子"（说明书 §3.2）。
    自己编一个数字比不提交更糟：那张单子事后要进成本账。
    清单里没有计价条目 ⇒ 明确拒绝、说清下一步能做什么，不猜价。
    """
    from models import JobEstimateIn

    if route.get("backend") != "cloud":
        return None
    adapter = adapters.find("text", "cloud", route.get("provider") or "")
    spec = next((m for m in (adapter.models if adapter else ())
                 if m.code == route.get("model")), None)
    if spec is None or not spec.prices:
        raise AgentInvalid(
            f"云端这一档 {route.get('provider')}/{route.get('model')} "
            "在清单里没有计价条目：平台不猜价格，所以这次计划不能提交到云端。"
            "要么先在模型中心把价格核进清单，要么把文本的默认轨改回本地")
    price = spec.prices[0]
    return JobEstimateIn(
        quantity=price.default_quantity, unit=price.unit,
        unit_price_micros=price.amount_micros, currency=price.currency,
        source=price.source or f"适配器清单 {adapters.MANIFEST_VERSION}")


def create_run(*, project_id: int, episode_id: int | None, goal: str,
               scene_key: str, user, idempotency_key: str = "",
               confirmed_cloud: bool = False,
               approval_valid_seconds: int = 86400) -> dict:
    """发起一次运行：建 run ＋ 提交"算计划"那条 Job，一个事务。

    为什么两条腿必须同事务：分开写就可能出现"run 有了、没人算计划"的孤儿——
    它会一直挂在"在算计划"那一档，而 worker 永远不会理它。
    D2 抽提交折进一个事务是同一个道理，这里不重犯。
    """
    import access
    import services
    from models import JobSubmitIn

    goal = (goal or "").strip()
    if len(goal) < 4:
        raise AgentInvalid("目标太短说不清要什么：至少写一句话（4 字以上）")
    access.ensure(user, project_id, "read")
    episode = None
    if episode_id:
        episode = episodes_repo.get(int(episode_id))
        if episode is None or int(episode["project_id"]) != int(project_id):
            raise AgentInvalid(f"第 {episode_id} 集不属于这个项目：跨项目的运行不接")

    route = plan_route(scene_key)
    if not route["ok"]:
        # 没有路线就当场说清楚，而不是留一条注定失败的 Job：
        # "提交后必然失败的事提前到提交前说"——precheck 的第一条纪律
        raise AgentInvalid(f"现在算不出计划：{route['reason']}")
    if route["needs_cloud_confirm"] and not confirmed_cloud:
        raise AgentInvalid(
            f"计划要走云端 {route['provider']}/{route['model']}："
            f"{route['data_out']}；{route['cost_note']}。"
            "要发起请勾选「确认用云端算计划」——平台不替人点这个勾")

    numbers = {"shots": 0}
    if episode is not None:
        from repositories.content import shots as shots_repo
        numbers["shots"] = len(shots_repo.list_by_episode(int(episode["id"])))

    key = (idempotency_key or "").strip()
    if key:
        existing = agent_runs.by_idempotency(key)
        if existing is not None and int(existing["project_id"]) == int(project_id):
            return {"run": existing, "reused": True, "route": route}

    snapshot = {"capability": "text", "backend": route["backend"],
                "provider": route["provider"], "model": route["model"],
                "scene_key": scene_key, "route_source": "provider_config.is_default",
                "needs_cloud_confirm": route["needs_cloud_confirm"]}
    with transaction() as conn:
        run = agent_runs.create({
            "project_id": project_id, "episode_id": episode["id"] if episode else None,
            "goal": goal, "scene_key": scene_key, "model_snapshot": snapshot,
            "approval_valid_seconds": approval_valid_seconds,
            "idempotency_key": key or None, "created_by": user["id"]})
        payload = JobSubmitIn(
            project_id=int(project_id),
            episode_id=int(episode["id"]) if episode else None,
            shot_id=None, capability="text",
            prompt=plan_prompt(goal, episode, numbers), params={}, input_refs=[],
            idempotency_key=f"ag-{run['id']}-plan",
            # 云端确认原样传下去：上面已经要过人点头，这里不重复要、也不自己补一个 True。
            # 预计成本单是从清单上读来的（见 `plan_estimate()`），不是前端传来的数字
            estimate=plan_estimate(route),
            user_confirmed_cloud=bool(confirmed_cloud))
        job = services.submit_job(payload, user["id"])
        agent_runs.set_plan_job(run["id"], job["id"])
        agent_runs.add_event("run_created", run_id=run["id"], actor_id=user["id"],
                             payload={"goal": goal, "scene_key": scene_key,
                                      "route": f"{route['backend']}/"
                                               f"{route['provider']}/"
                                               f"{route['model']}"},
                             conn=conn)
        agent_runs.add_event("plan_requested", run_id=run["id"], actor_id=user["id"],
                             payload={"job_id": job["id"],
                                      "chars": len(payload.prompt)}, conn=conn)
        fresh = agent_runs.get(run["id"])
    return {"run": fresh, "reused": False, "route": route,
            "plan_job_id": job["id"],
            "note": "作业已排进生成中心那一个队列；计划回来后由 worker 的节拍落步骤"}


# ---------- 状态现算 ----------
def run_status(run, steps: list) -> str:
    """运行状态**一律现算**（库里只有 paused／cancelled 两位）。

    这条函数是"两套状态"最容易长出来的地方，所以判定顺序写死并解释：
    先人后机——取消与暂停是人的意愿，任何步骤状态都不能盖过它。
    """
    if run["cancelled"]:
        return RUN_CANCELLED
    if not steps:
        # 计划阶段的三种结局：失败（含"产物不是计划"）、还在算、
        # 以及"算完了但还没来得及落步骤"（同一拍里的极窄窗口，仍算在算）
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


# ---------- 视图 ----------
def step_view(step, job=None) -> dict:
    """步骤 → 界面那一行。

    第三份信息（关联作业的状态、成本）从 `job` 现读进来，**不落在步骤行上**；
    调用方（`detail()`）负责把 job 查好传进来，这里不各查各的。
    """
    args = loads(step["args_json"], {})
    preview = args.get("prompt") or args.get("name") or ""
    return {
        "id": step["id"], "seq": step["seq"],
        "tool_code": step["tool_code"],
        "tool_label": (agent_tools.TOOLS.get(step["tool_code"]) or {}).get(
            "label", "清单外的工具码（说明白名单改过）"),
        "risk": step["risk"],
        "risk_label": agent_tools.RISK_LABEL.get(step["risk"], step["risk"]),
        "status": step["status"],
        "status_label": STEP_STATUS_LABEL.get(step["status"], step["status"]),
        "args": args,
        # 参数摘要给列表页用；完整参数在详情里展开，因为审批要看得见全文
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
        "error_code": step["error_code"], "error_message": step["error_message"],
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
        "id": run["id"], "project_id": run["project_id"],
        "episode_id": run["episode_id"], "goal": run["goal"],
        "scene_key": run["scene_key"], "status": status,
        "status_label": RUN_STATUS_LABEL[status],
        "paused": bool(run["paused"]), "cancelled": bool(run["cancelled"]),
        "cancel_reason": run["cancel_reason"],
        "steps": views, "step_counts": counts,
        "model_snapshot": loads(run["model_snapshot_json"], {}),
        "plan_job_id": run["plan_job_id"],
        "approval_valid_seconds": run["approval_valid_seconds"],
        "idempotency_key": run["idempotency_key"] or "",
        "started_at": run["started_at"], "finished_at": run["finished_at"],
        "created_at": run["created_at"], "created_by": run["created_by"],
    }


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
    # 事件中文名随详情一起下发（见 `EVENT_LABEL` 上面那段）：界面不再自己维护一份，
    # "清单改了、界面少说一档"这件事因此没有发生的余地。
    out["event_labels"] = EVENT_LABEL
    if plan_job is not None and plan_job["status"] in JOB_TERMINAL_FAILED:
        out["failure"] = {"where": "算计划", "code": plan_job["error_code"],
                          "message": plan_job["error_message"]}
    else:
        blocked = next((s for s in steps if s["status"] == STEP_FAILED), None)
        out["failure"] = ({"where": f"第 {blocked['seq']} 步",
                           "code": blocked["error_code"],
                           "message": blocked["error_message"]} if blocked else None)
        if out["failure"] is None and run["plan_status"] == "failed":
            # 计划阶段崩在解析或读产物上：那一步没有步骤行，原因只在事件里。
            # 这里读一条事件是**给人看原因**，不是拿来派生状态——
            # 状态仍由 `plan_status` 那一列决定，两件事分开才不会互相拖。
            failed = next((e for e in out["events"]
                           if e["event_type"] == "run_failed"), None)
            payload = (failed or {}).get("payload") or {}
            out["failure"] = {"where": "算计划", "code": payload.get("code") or "",
                              "message": payload.get("message") or ""}
    return out


def _job_map(jobs: dict, plan_job) -> dict:
    merged = dict(jobs)
    if plan_job is not None:
        merged[plan_job["id"]] = plan_job
    return merged


def _job_brief(job) -> dict | None:
    if job is None:
        return None
    return {"id": job["id"], "status": job["status"],
            "status_label": _job_label(job["status"]),
            "execution_mode": job["execution_mode"],
            "provider_code": job["provider_code"], "model_code": job["model_code"],
            "cost_micros": job["cost_micros"] or 0, "currency": job["currency"] or "",
            "error_code": job["error_code"], "error_message": job["error_message"],
            "locate": {"module": "generate", "sub": 0}}


def _event_view(row) -> dict:
    return {"id": row["id"], "run_id": row["run_id"], "step_id": row["step_id"],
            "event_type": row["event_type"], "payload": loads(row["payload_json"], {}),
            "created_at": row["created_at"]}


def _job_label(status: str) -> str:
    from models import JOB_STATUSES
    return JOB_STATUSES.get(status, status)


# ---------- 配置视图（本地／云端两张卡） ----------
def config_cards() -> dict:
    """本地智能体／云端智能体两页的数据，**全部来自已有的三处**：
    `provider_config`（这一类走哪条轨）、`model_registration`（这一档在哪）、
    `engine_instance`（服务在哪个端点），健康每次现查。

    这里刻意**没有**"agent 配置表"：说明书 §7 建议的 `agent_provider_config`
    与上面三张重叠，取证与裁定写在《文档/开发/智能体管理/00-接入前现状审计.md》§3。

    四个概念分开返回、分开标注（说明书 §2「状态概念不互相混淆」）：
    `configured`（配了没有）／`health`（现在连不连得上）／
    `resident`（驻留策略，一句话说明，不是结论）／`runs`（有没有在跑）。
    """
    cards = []
    for backend in ("local", "cloud"):
        row = provider_repo.slot("text", backend)
        card = {"backend": backend,
               "backend_label": "本地智能体" if backend == "local" else "云端智能体",
               "configured": False, "provider": "", "model": "",
               "health_state": "unconfigured", "health_detail": "",
               "endpoint": "", "key_env": "", "secret_present": False,
               "resident": "", "cost_note": "", "enabled": False,
               "registration": None, "engines": []}
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
        # 这是**读**路径：智能体中心的配置卡片一次拼十几行，每行都去连一次
        # 服务型引擎等于「打开一页顺带扫一遍网」（默认值 2026-11-09 已翻成 False，
        # 这里显式写出来，让「不出网」是这一行的意思而不是默认值的副作用）。
        # 要现查请走模型中心的「检查引擎连接」。
        health = adapters.health(dict(row), probe_service=False)
        card["health_state"] = health["state"]
        card["health_detail"] = health["detail"]
        card["cost_note"] = agent_tools.cost_note("text", row["provider"],
                                                 row["model"], backend)
        reg = model_center.find_registration("text", backend, row["provider"],
                                             row["model"])
        if reg is not None:
            card["registration"] = {"id": reg["id"], "display_name": reg["display_name"],
                                    "artifact_path": reg["artifact_path"],
                                    "remote_model_id": reg["remote_model_id"],
                                    "enabled": bool(reg["enabled"])}
        # 驻留这句话不是探测结论，是把两种执行形状各自的事实念一遍：
        # 子进程型每次起、每次退；服务型由那个服务自己管（平台不替它卸载）
        adapter = adapters.find("text", backend, row["provider"])
        if adapter is None:
            card["resident"] = "清单里没有这一档适配器：参数不猜、驻留也不猜"
        elif adapter.code in engines.SERVICE_ENGINES:
            card["resident"] = ("常驻服务型：平台只打它的端点，"
                                "模型加载与卸载由该服务自己决定，空闲后是否驻留不归平台管")
        else:
            card["resident"] = ("子进程型：每次调用起一个进程、跑完就退，"
                               "所以「驻留策略」对这一档没有意义（不存在常驻）")
        for instance in engine_instances.instances():
            if instance["engine_code"] == (adapter.code if adapter else ""):
                card["engines"].append({"id": instance["id"],
                                        "endpoint": instance["endpoint"],
                                        "enabled": bool(instance["enabled"]),
                                        "connection_type": instance["connection_type"]})
                if instance["enabled"] and not card["endpoint"]:
                    card["endpoint"] = instance["endpoint"]
        cards.append(card)
    return {"cards": cards,
            "caliber": ("配置状态＝provider_config 那行填没填；健康＝每次现查（不落库）；"
                        "驻留＝执行形状的一句说明；运行＝agent_run 的数。"
                        "这四件事互不等价，界面分开显示"),
            "manifest_version": adapters.MANIFEST_VERSION}


def _approval_route(run) -> dict:
    snapshot = loads(run["model_snapshot_json"], {})
    if snapshot.get("route_source") == "chatgpt_oauth":
        # ChatGPT 只产出计划草案，付费步骤仍使用平台当前文本默认轨。
        route = plan_route()
        return {**route, "cost_note": route.get("cost_note") or route.get("reason", "")}
    return snapshot


# ---------- 待审批 ----------
def pending_approvals(project_ids: list[int], limit: int = 50) -> list[dict]:
    """正在等人选的步骤。审核中心那一页从这里取，**签字仍写进 `review_item`**。"""
    out = []
    for step in agent_runs.awaiting_steps(project_ids, limit):
        run = agent_runs.get(step["run_id"])
        if run is None:
            continue
        args = loads(step["args_json"], {})
        snapshot = _approval_route(run)
        view = step_view(step)
        view.update({
            "goal": run["goal"], "project_id": run["project_id"],
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
    """审批页那一张卡：把人要点头的那一步**完整**摊开（参数全文＋送出去什么＋价）。"""
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
        "goal": run["goal"], "project_id": run["project_id"],
        "episode_id": run["episode_id"],
        "args": loads(step["args_json"], {}),
        "args_hash": step["args_hash"],
        "data_out": tool.get("data_out", ""),
        "verify": tool.get("verify", ""),
        "cost_note": snapshot.get("cost_note") or agent_tools.cost_note("text", snapshot.get("provider", ""),
                                          snapshot.get("model", ""),
                                          snapshot.get("backend", "")),
        "model_snapshot": snapshot,
        # 这一步批下去会不会花钱、要不要人明确点头。审核中心据此**专门**写一句话——
        # 因为"批准"在这个平台上同时是"我同意花这笔钱"，那句话不能靠人自己猜。
        "paid": step["risk"] == "paid",
        "cloud_confirm_note": (
            "批准这一步同时等于确认按供应商计费：签完之后才可能真的发生一笔费用。"
            "参数一改（指纹变了）旧批准失效，这笔确认也随之失效，平台会重新问一次。"
            if step["risk"] == "paid" else ""),
        "expires_at": step["approval_expires_at"] or "",
        "signed": [{"decision": s["decision"], "note": s["note"],
                    "signed_by": s["signed_by"], "signed_at": s["signed_at"]}
                   for s in signs],
        # 这句话是这一整套设计的目的本身：审批绑的是参数哈希，
        # 人签完之后参数若有改动，旧签字立刻失配
        "binding": "批准绑定的是上面那组参数的指纹；参数一改指纹就变，旧批准自动作废",
    }


# ---------- 推进 ----------
def tick() -> dict:
    """worker 的推进节拍：把所有还没结束的运行往前推一格。

    调用点是 `worker.py` 里 `pipeline.tick()` 之后同一处（裁定 D3：
    调度器住 worker，API 进程不起后台线程）。这里每条运行独立 try——
    一条崩了不能拖死队列，与产线那一轮同一处理。
    """
    stats = {"runs": 0, "advanced": 0, "awaiting": 0, "completed": 0,
             "failed": 0, "errors": 0}
    for run in agent_runs.active():
        stats["runs"] += 1
        try:
            stats.update(_tick_run(run))
        except Exception as exc:                 # noqa: BLE001 —— 见上面那句
            stats["errors"] += 1
            _record_error(run, exc)
    return stats


def _tick_run(run) -> dict:
    out = {"advanced": 0, "awaiting": 0, "completed": 0, "failed": 0}
    steps = agent_runs.steps(run["id"])

    # ① 还没有步骤＝计划阶段。看那条算计划的作业到哪了
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
            return out                      # 排队／运行中：计划还没回来，这轮什么都不做
        _ingest_plan(run, job)
        steps = agent_runs.steps(run["id"])
        if not steps:
            return out

    # ② 审批到期收成 expired（暂停中也要收：不然恢复之后会出现
    #    "批过一个早过期的动作"这种最难查的状态）
    now = datetime.now()
    for step in steps:
        if step["status"] != STEP_AWAITING or not step["approval_expires_at"]:
            continue
        if _parse_time(step["approval_expires_at"]) > now:
            continue
        agent_runs.update_step(step["id"], {"status": STEP_EXPIRED,
                                            "error_code": "approval_expired",
                                            "error_message":
                                                "审批有效期到了，这一步没有批出去"})
        agent_runs.add_event("step_expired", run_id=run["id"], step_id=step["id"],
                             payload={"seq": step["seq"],
                                      "expires_at": step["approval_expires_at"]})
        out["advanced"] += 1

    if run["paused"] or run["cancelled"]:
        return out

    # ③ 往前推：一轮最多推 MAX_STEPS_PER_TICK 步，剩下的下一轮再说
    for _ in range(MAX_STEPS_PER_TICK):
        steps = agent_runs.steps(run["id"])
        current = next((s for s in steps if s["status"] not in STEP_FINAL), None)
        if current is None:
            status = run_status(run, steps)
            if status in (RUN_COMPLETED, RUN_FAILED):
                agent_runs.finish(run["id"])
                agent_runs.add_event(
                    "run_completed" if status == RUN_COMPLETED else "run_failed",
                    run_id=run["id"], payload={"status": status})
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
            # 只读工具当场做完：它不排队、不花钱、不改业务事实，
            # 为它开一次审批只会让人多点三次"确认"而没有任何增量保护
            _execute(run, step)
            return True
        expires = (datetime.now() + timedelta(seconds=run["approval_valid_seconds"])
                   ).strftime("%Y-%m-%d %H:%M:%S")
        agent_runs.update_step(step["id"], {"status": STEP_AWAITING,
                                            "approval_expires_at": expires})
        agent_runs.add_event("approval_requested", run_id=run["id"],
                             step_id=step["id"],
                             payload={"seq": step["seq"], "tool_code": step["tool_code"],
                                      "args_hash": step["args_hash"],
                                      "risk": step["risk"], "expires_at": expires,
                                      # 审核中心靠这三样定位那一行签字
                                      "target_kind": reviews.AGENT_ACTION,
                                      "target_id": step["id"]})
        return False

    if status == STEP_AWAITING:
        decision = _approval_decision(step)
        if decision is None:
            return False                       # 还没人签：保持等人
        if decision == "approved":
            agent_runs.update_step(step["id"], {"status": STEP_APPROVED})
            agent_runs.add_event("approval_granted", run_id=run["id"],
                                 step_id=step["id"],
                                 payload={"seq": step["seq"]})
            return True
        agent_runs.update_step(step["id"], {
            "status": STEP_FAILED, "error_code": f"approval_{decision}",
            "error_message": ("这一步被驳回；参数若已改动请重新发起运行"
                              "（指纹变了旧批准自然失效）"
                              if decision == "rejected" else
                              "这一步被退回；平台不自动改参数重提，"
                              "请人工处理或重新发起")})
        agent_runs.add_event("approval_rejected", run_id=run["id"],
                             step_id=step["id"],
                             payload={"seq": step["seq"], "decision": decision})
        return True

    if status == STEP_APPROVED:
        _execute(run, step)
        return True

    if status == STEP_RUNNING:
        if not step["job_id"]:
            # running 而指针是空的＝这一步的执行体在写指针之前崩了。
            # 按失败处理，不猜它到底做没做——猜错就是"重复执行一次付费动作"
            agent_runs.update_step(step["id"], {
                "status": STEP_FAILED, "error_code": "lost",
                "error_message": "步骤停在执行中但没有关联作业，"
                                 "平台不猜它做没做成；请重新发起"})
            agent_runs.add_event("step_failed", run_id=run["id"], step_id=step["id"],
                                 payload={"seq": step["seq"], "code": "lost"})
            return True
        job = job_repo.get(step["job_id"])
        if job is None:
            agent_runs.update_step(step["id"], {
                "status": STEP_FAILED, "error_code": "job_missing",
                "error_message": "关联的作业行已经不在了"})
            return True
        if job["status"] in JOB_TERMINAL_FAILED:
            agent_runs.update_step(step["id"], {
                "status": STEP_FAILED, "error_code": job["error_code"] or "job_failed",
                "error_message": job["error_message"] or "作业失败"})
            agent_runs.add_event("step_failed", run_id=run["id"], step_id=step["id"],
                                 payload={"seq": step["seq"], "job_id": job["id"],
                                          "code": job["error_code"]})
            return True
        if job["status"] != "succeeded":
            return False                       # 排队／运行中，等下一轮
        _verify_and_succeed(run, step, job)
        return True
    return False


def _approval_decision(step) -> str | None:
    """读审核中心那一条：'approved' / 'rejected' / 'returned' / None（还没人看）。

    只认 **base_version 等于本步 args_hash** 的那条签字——这就是"审批与参数绑定"
    （说明书 §3.4）。参数被改过（指纹不同）则视同没人签过：宁可停在等人，
    也不能拿旧批准放行新参数。
    """
    rows = reviews.signs(reviews.AGENT_ACTION, int(step["id"]), step["args_hash"])
    # 再按锚点筛一遍：今天 agent_action 只有 `execute` 一个锚点，
    # 但"只有一个"是靠约定维持的——筛一遍之后，将来多一个锚点也不会让
    # 一条不相干的签字被当成"这一步批过了"
    rows = [r for r in rows if r["anchor"] == reviews.AGENT_ACTION_ANCHOR]
    if not rows:
        return None
    # 同一步同一指纹只可能有一条（`uq_review_item_target`），改判是覆盖不是叠行；
    # 取最后一条只是为了万一人手工插过第二行，行为仍然稳定可解释
    return rows[-1]["decision"]


def _execute(run, step) -> None:
    """调工具，把结果落成步骤状态。

    注意这里**不包 try 把所有异常收成一个 failed**：
    白名单层的拒绝（ToolUnknown／ToolArgsInvalid）与执行层的业务失败分开记，
    因为前者通常说明"平台改了白名单而旧运行还带着旧计划"，
    那是运维要单独看见的一类，混进"作业失败"里就查不动了。
    """
    # 这一步是不是**人签过字**的。只有 `STEP_APPROVED` 会走到这里还带这个状态：
    # 待批的步骤走的是 `_advance` 的 STEP_AWAITING 那一支（不执行），
    # 只读步骤从 STEP_PENDING 直接进场（没签过）。
    #
    # 为什么要把它传给工具：花钱的那一步（比如排一条云端文本作业）在
    # `services.submit_job()` 那里有一道"人已确认计费"的闸。原来 agent_tools
    # 恒传 `user_confirmed_cloud=False`，于是**默认轨一旦指向云端，这类步骤
    # 就注定失败**——而失败原因是"没有人能替人做那次确认"。
    #
    # 现在这个确认由**审核中心那一次签字**提供：批准本身就是"我知道这一步要花钱，
    # 我同意"。签字落库、绑参数指纹（`review_item.base_version = args_hash`），
    # 参数一改旧批准自动失效——所以这个"确认"不是一句口头答应，
    # 而是一条读得回来、对得上参数的记录。
    authorized = step["status"] == STEP_APPROVED
    ctx = {"run_id": int(run["id"]), "seq": int(step["seq"]),
           "project_id": int(run["project_id"]),
           "episode_id": run["episode_id"], "actor_id": run["created_by"],
           "cloud_confirmed": authorized}
    args = loads(step["args_json"], {})
    agent_runs.update_step(step["id"], {"status": STEP_RUNNING})
    agent_runs.add_event("step_started", run_id=run["id"], step_id=step["id"],
                         payload={"seq": step["seq"], "tool_code": step["tool_code"],
                                  "risk": step["risk"]})
    try:
        result = agent_tools.execute(step["tool_code"], args, ctx)
    except (agent_tools.ToolUnknown, agent_tools.ToolArgsInvalid) as exc:
        agent_runs.update_step(step["id"], {
            "status": STEP_FAILED, "error_code": "tool_rejected",
            "error_message": str(exc)})
        agent_runs.add_event("tool_rejected", run_id=run["id"], step_id=step["id"],
                             payload={"seq": step["seq"], "reason": str(exc)})
        return
    except agent_tools.ToolExecutionError as exc:
        agent_runs.update_step(step["id"], {
            "status": STEP_FAILED, "error_code": exc.code,
            "error_message": str(exc)})
        agent_runs.add_event("step_failed", run_id=run["id"], step_id=step["id"],
                             payload={"seq": step["seq"], "code": exc.code})
        return
    except Exception as exc:                   # noqa: BLE001
        # 未预期异常：记类型与消息，但不带栈、不带绝对路径——
        # 这一步的 message 会进接口响应，也可能进导出（同 v0.85 那批脱敏口径）
        agent_runs.update_step(step["id"], {
            "status": STEP_FAILED, "error_code": "internal",
            "error_message": f"{type(exc).__name__}：内部错误，详情看 worker 日志"})
        agent_runs.add_event("step_failed", run_id=run["id"], step_id=step["id"],
                             payload={"seq": step["seq"], "code": "internal"})
        return

    if result.get("job_id"):
        agent_runs.update_step(step["id"], {
            "status": STEP_RUNNING, "job_id": int(result["job_id"]),
            "result_json": result})
        agent_runs.add_event("job_submitted", run_id=run["id"], step_id=step["id"],
                             payload={"seq": step["seq"],
                                      "job_id": int(result["job_id"])})
        return
    # 没有作业＝这一步是同步做完的（读工具、登记资产）。
    # 工具自己负责验收（`asset_register` 会回读那一行），所以这里直接记成功
    agent_runs.update_step(step["id"], {"status": STEP_SUCCEEDED,
                                        "result_json": result})
    agent_runs.add_event("step_succeeded", run_id=run["id"], step_id=step["id"],
                         payload={"seq": step["seq"], "tool_code": step["tool_code"]})


def _verify_and_succeed(run, step, job) -> None:
    """说明书 §5 步 7：以**真实业务状态与产物**为准，不以 Agent 自述为准。

    作业成功只是必要条件；还要产物清单里真有一个能读的文件。
    少这一层就会留下"作业绿了、产物是空的"那种最难查的账——
    worker 那侧本来就有 `needs_attention`，但那是给媒体的，文本作业没有。
    """
    path = jobfiles.artifact_at(job["result_paths_json"], 0)
    if path is None:
        agent_runs.update_step(step["id"], {
            "status": STEP_FAILED, "error_code": "artifact_missing",
            "error_message": "作业显示成功，但产物清单里读不到文件；"
                             "按失败处理，不拿一个空结果往下走"})
        agent_runs.add_event("step_failed", run_id=run["id"], step_id=step["id"],
                             payload={"seq": step["seq"], "code": "artifact_missing"})
        return
    result = loads(step["result_json"], {})
    result.update({"verified": True, "artifact": jobfiles.relative(str(path)),
                   "bytes": path.stat().st_size})
    agent_runs.update_step(step["id"], {"status": STEP_SUCCEEDED,
                                        "result_json": result})
    agent_runs.add_event("step_succeeded", run_id=run["id"], step_id=step["id"],
                         payload={"seq": step["seq"], "job_id": job["id"],
                                  "bytes": result["bytes"]})


def _ingest_plan(run, job) -> None:
    """计划作业成功 → 解析 → 落步骤。解析失败按运行失败处理，不猜计划。"""
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
        # `approval_expires_at` 留空是有意：有效期从"这一步真的要人批了"起算
        # （见 `_advance`），而不是从计划落库起算——第一步若是只读、当场做完，
        # 不该白白消耗后面写步骤的审批窗口
        agent_runs.add_step(
            run["id"], item["seq"], tool_code=item["tool"], args=item["args"],
            args_hash=item["args_hash"], risk=item["risk"], status=STEP_PENDING)
    agent_runs.add_event("plan_ready", run_id=run["id"],
                         payload={"steps": len(steps),
                                  "tools": [s["tool"] for s in steps],
                                  "route": f"{snapshot.get('backend')}/"
                                           f"{snapshot.get('provider')}/"
                                           f"{snapshot.get('model')}"})


def _mark_plan_failed(run) -> None:
    """计划这一腿的失败要落进 `plan_status`，派生状态才看得见。

    只写事件不够：`run_status()` 不该为了还原一个失败去扫事件表。
    """
    agent_runs.set_plan_status(run["id"], "failed")


def _fail_run(run, code: str, message: str) -> None:
    """运行级失败：把还没做完的步骤一并作废，然后收口。

    作废而不是留着：留着会出现"运行已失败、第三步仍在等人审批"——
    那种待办出现在审核队列里，人会签一个永远不会被执行的授权。
    """
    _mark_plan_failed(run)
    agent_runs.cancel_open_steps(run["id"])
    agent_runs.finish(run["id"])
    agent_runs.add_event("run_failed", run_id=run["id"],
                         payload={"code": code, "message": message[:300]})


def _record_error(run, exc: Exception) -> None:
    """`tick()` 里一条运行崩了：记一行事件，不吞掉、也不拖死队列。

    不把 run 直接判失败——崩的位置可能在读库之前的循环里，
    此时步骤状态还是自洽的，下一轮大概率能接着推。真的推不动时会走到
    `_fail_run`，那才是有意的失败。
    """
    try:
        agent_runs.add_event("preflight_warning", run_id=run["id"],
                             payload={"code": "tick_error",
                                      "message": f"{type(exc).__name__}"[:200]})
    except Exception:                          # noqa: BLE001 —— 记不上也不能再崩一次
        pass


def _parse_time(text: str) -> datetime:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        # 库里读到不可解析的时间就当作"刚到期"：宁可早一步收成 expired
        # （人可以重新发起），也不要因为它而永久挂着待办
        return datetime.now()


def pause(run_id: int, user) -> dict:
    return _will(run_id, user, paused=True, event="run_paused")


def resume(run_id: int, user) -> dict:
    return _will(run_id, user, paused=False, event="run_resumed")


def cancel(run_id: int, user, reason: str = "") -> dict:
    return _will(run_id, user, cancelled=True, event="run_cancelled",
                 reason=reason)


def _will(run_id: int, user, *, paused: bool | None = None,
          cancelled: bool | None = None, event: str, reason: str = "") -> dict:
    """外生意愿的三合一入口。

    取消之后**不能**再恢复：`cancelled` 一置，`finished_at` 就落了，
    运行已经不在"活跃"集合里。这个不对称是有意的——
    取消意味着那批待审步骤同时作废，恢复一条"参数都没了"的运行只会骗人。
    """
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
        # 待审步骤同时作废。作废条数记在同一条事件里，
        # 不另发一条"step_cancelled"：那会让一次取消在时间线上留下两行，
        # 而它们说的是同一件人刚做的事
        n = agent_runs.cancel_open_steps(run_id)
        agent_runs.add_event("run_cancelled", run_id=run_id, actor_id=user["id"],
                             payload={"reason": reason, "voided_steps": n})
    return {"run": run_row_view(updated, agent_runs.steps(run_id))}


# ---------- 概览 ----------
def overview(project_ids: list[int]) -> dict:
    """顶部四数＋工具表＋最近事件。

    四个数各自口径写在 `caliber` 里，界面上原样念。这四个东西很容易被界面
    自己"顺手算一下"，而其中两个（可用配置、驻留）平台根本没有可数的落库列。
    """
    runs = agent_runs.recent(project_ids, limit=200)
    # 一次取齐：步骤与行内 Job 都按 run 批量读，列表与详情不再是两份字段。
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
        "counts": {"runs": len(runs), "active": counts[RUN_RUNNING] + counts[RUN_PLANNING],
                   "awaiting_approval": pending, "completed": counts[RUN_COMPLETED],
                   "failed": counts[RUN_FAILED], "usable_configs": usable,
                   "total_configs": len(cards["cards"])},
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
