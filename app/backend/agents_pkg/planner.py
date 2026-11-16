"""智能体中心：计划提示词、计划解析、路由预算与运行发起。"""
from __future__ import annotations

import json

import adapters
import agent_tools
from agents_pkg.constants import AgentInvalid, MAX_PLAN_STEPS
from db import transaction
from repositories.agents import agent_runs
from repositories.content import episodes as episodes_repo
from repositories.model_center import model_center


def _tool_menu() -> str:
    """把白名单摊给模型看。

    只摊**声明里已有的东西**（码、参数名与类型、枚举取值、风险档）。
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
    """算计划那次调用的提示词（内容最小化，正文一个字都不进来）。"""
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
    """把模型输出收成规范化步骤。"""
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
        steps.append({
            "seq": index,
            "tool": code,
            "args": args,
            "args_hash": agent_tools.args_hash(code, args),
            "risk": agent_tools.risk_of(code),
            "why": str(item.get("why") or "").strip()[:300],
        })
    return steps


def plan_route(scene_key: str = "") -> dict:
    """这一步会走哪条轨、要花多少钱、往外送什么。"""
    from services import NoRoute, resolve_provider

    hint: dict = {}
    if scene_key:
        for row in model_center.policies():
            if row["scene_key"] == scene_key:
                hint = {
                    "scene_label": row["scene_label"],
                    "preference": row["preference"],
                    "suggestion": (f"{row['slot_provider']}/{row['slot_model']}"
                                   if row["slot_provider"] else ""),
                    "policy_enabled": bool(row["enabled"]),
                }
                break
    try:
        route = resolve_provider("text")
    except NoRoute as exc:
        return {"ok": False, "reason": str(exc), "backend": "", "provider": "",
                "model": "", "cost_note": "", **hint}
    out = {
        "ok": True,
        "backend": route["backend"],
        "provider": route["provider"],
        "model": route["model"],
        "cost_note": agent_tools.cost_note("text", route["provider"],
                                           route["model"], route["backend"]),
        **hint,
    }
    out["needs_cloud_confirm"] = out["backend"] == "cloud"
    out["data_out"] = (
        f"计划提示词（目标原话＋集号＋镜头数，不含正文）会发给 {out['provider']}"
        if out["backend"] == "cloud"
        else "计划提示词只在本机处理，不外发"
    )
    return out


def plan_estimate(route: dict):
    """给"算计划"那条云端作业凑一张预计成本单——数字全部来自适配器清单。"""
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
        quantity=price.default_quantity,
        unit=price.unit,
        unit_price_micros=price.amount_micros,
        currency=price.currency,
        source=price.source or f"适配器清单 {adapters.MANIFEST_VERSION}",
    )


def create_run(*, project_id: int, episode_id: int | None, goal: str,
               scene_key: str, user, idempotency_key: str = "",
               confirmed_cloud: bool = False,
               approval_valid_seconds: int = 86400) -> dict:
    """发起一次运行：建 run ＋ 提交"算计划"那条 Job，一个事务。"""
    import access
    import agents as _ag
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

    route = _ag.plan_route(scene_key)
    if not route["ok"]:
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

    snapshot = {
        "capability": "text",
        "backend": route["backend"],
        "provider": route["provider"],
        "model": route["model"],
        "scene_key": scene_key,
        "route_source": "provider_config.is_default",
        "needs_cloud_confirm": route["needs_cloud_confirm"],
    }
    payload = JobSubmitIn(
        project_id=int(project_id),
        episode_id=int(episode["id"]) if episode else None,
        shot_id=None,
        capability="text",
        prompt=plan_prompt(goal, episode, numbers),
        params={},
        input_refs=[],
        idempotency_key=None,
        estimate=plan_estimate(route),
        user_confirmed_cloud=bool(confirmed_cloud),
    )
    prepared = services.prepare_job(payload, user["id"])
    with transaction() as conn:
        run = agent_runs.create({
            "project_id": project_id,
            "episode_id": episode["id"] if episode else None,
            "goal": goal,
            "scene_key": scene_key,
            "model_snapshot": snapshot,
            "approval_valid_seconds": approval_valid_seconds,
            "idempotency_key": key or None,
            "created_by": user["id"],
        })
        payload.idempotency_key = f"ag-{run['id']}-plan"
        job = services.submit_job(payload, user["id"], prepared=prepared)
        agent_runs.set_plan_job(run["id"], job["id"])
        agent_runs.add_event(
            "run_created",
            run_id=run["id"],
            actor_id=user["id"],
            payload={
                "goal": goal,
                "scene_key": scene_key,
                "route": f"{route['backend']}/{route['provider']}/{route['model']}",
            },
            conn=conn,
        )
        agent_runs.add_event(
            "plan_requested",
            run_id=run["id"],
            actor_id=user["id"],
            payload={"job_id": job["id"], "chars": len(payload.prompt)},
            conn=conn,
        )
        fresh = agent_runs.get(run["id"])
    return {
        "run": fresh,
        "reused": False,
        "route": route,
        "plan_job_id": job["id"],
        "note": "作业已排进生成中心那一个队列；计划回来后由 worker 的节拍落步骤",
    }
