"""智能体中心接口（0041 ＋《智能体管理中心》说明书）。

一句话职责边界：**这一层管"谁替人做决定、做到哪一步了"，不管模型是什么、能不能跑**。
后一半归模型中心——本文件一次都不查 `model_registration` 之外的模型字段，
配置视图 (`/agent/configs`) 只是把模型中心那三处现成的东西**换个角度念一遍**，
一个字段都不复制、也不给第二个写入口（取证见
《文档/开发/智能体管理/00-接入前现状审计.md》§7）。

权限沿用现有点，**一个都不新增**（与产线那几轮同口径）：

| 动作 | 权限点 | 为什么是它 |
|---|---|---|
| 看配置、看工具表、看概览 | `system:config` | 与模型中心同一档：这些是"平台怎么装的"，不是业务内容 |
| 发起运行 | `job:submit` | 发起＝排一条作业进那一个队列，不是一种新权力 |
| 暂停／恢复／取消 | `job:cancel` | 与产线运行的暂停／恢复／取消完全同一档语义 |
| 看运行、看待审批列表 | `project:read`／`review:read` | 看得见项目才看得见它的运行 |
| 签字（批准这一步） | `review:sign`，**且落在审核中心** | 不变量 3：签字只有审核中心一个写入口。本文件不签 |

`/agent/configs` 与 `/agent/tools` 里**不含密钥值**：只回"变量名＋有没有值"，
与 `routers/model_center.py` 那三行同一条纪律。
"""
from fastapi import APIRouter, Depends, HTTPException, status as http_status

import access
import agents
import agent_tools
from deps import require_permission
from models import AgentRunIn
from repositories.projects import projects as project_repo

router = APIRouter(prefix="/api", tags=["agents"])

# 审批有效期的人话档位。写在这里而不是界面里，是为了"最长多久"这句话只有一处：
# 说明书 §3.4 要"有效期"，而 0041 的 CHECK 已把 60—604800 秒钉死
VALID_CHOICES = ((3600, "1 小时"), (21600, "6 小时"), (86400, "24 小时"),
                 (604800, "7 天"))


def _visible_ids(user) -> list[int]:
    return [row["id"] for row in
            project_repo.list_visible(user["id"], access.is_manager(user), "all")]


def _bad(exc: Exception, code: int = 422) -> HTTPException:
    return HTTPException(status_code=code, detail=str(exc))


@router.get("/agent/overview")
def overview(user=Depends(require_permission("system:config"))) -> dict:
    """顶部四数＋最近事件＋运行列表。

    四个数的口径由 `agents.overview()` 的 `caliber` 字段一起给——界面只念不算。
    这一条必须带可见范围：概览里的运行数如果按全库算，
    协作人就会看见"有 12 条运行"却一条都点不开。
    """
    return agents.overview(_visible_ids(user))


@router.get("/agent/configs")
def configs(user=Depends(require_permission("system:config"))) -> dict:
    """本地／云端两张配置卡。配置、健康、驻留、运行四个概念分开返回。"""
    return agents.config_cards()


@router.get("/agent/tools")
def tools(_=Depends(require_permission("system:config"))) -> dict:
    """白名单工具表（只读）。

    这一页存在的意义不是"能配置工具"——工具是代码，改它要走代码评审，
    不给界面一个加按钮的口子（说明书 §3.4「工具白名单」）。
    它回答的是"平台到底允许智能体做哪几件事"，让人不必读代码才知道边界。
    """
    return {"items": agent_tools.labels(),
            "max_plan_steps": agents.MAX_PLAN_STEPS,
            "note": "工具清单住代码（agent_tools.py）：加一个工具必须有真实执行分支，"
                    "界面上不会出现点了没反应的选项（CR-010）",
            "forbidden": "平台不执行任何不在上表里的动作：没有任意 SQL、"
                         "没有 Shell、没有 Python"}


@router.get("/agent/plan-options")
def plan_options(project_id: int, goal: str = "", scene_key: str = "",
                 episode_id: int | None = None,
                 user=Depends(require_permission("project:read"))) -> dict:
    """发起前那一张单子：走哪条轨、往外送什么、按什么计价、要不要确认。

    读权限就够：这一步不建作业、不花钱、不发送任何内容（同
    `/documents/{id}/ai-options` 那一条的口径）。
    说明书 §3.2 要"调用前展示模型、数据范围、费用估算并取授权"，落点就在这里——
    界面先调这一条，把三样念给人看，人勾了才 POST。
    """
    access.ensure(user, project_id, "read")
    route = agents.plan_route(scene_key)
    return {"route": route, "approval_choices": [
        {"seconds": s, "label": label} for s, label in VALID_CHOICES],
        "default_valid_seconds": dict(VALID_CHOICES).get(86400, "24 小时"),
        "needs_cloud_confirm": bool(route.get("needs_cloud_confirm")),
        "blockers": [] if route.get("ok") else [route.get("reason")]}


@router.post("/agent/runs", status_code=http_status.HTTP_201_CREATED)
def create_run(payload: AgentRunIn,
               user=Depends(require_permission("job:submit"))) -> dict:
    """发起一次运行。201 返回的是 run 的视图，此刻只多了一条排队中的作业。

    云端却没确认 ⇒ 422，原话是"要发起请勾选确认"——
    这不是刁难：说明书 §2「云端发送内容需最小化并经明确授权」，
    而 `agents.create_run()` 里那一句判定与 `services.submit_job` 同一条纪律，
    路由层不替人补一个 True。
    """
    try:
        out = agents.create_run(
            project_id=payload.project_id, episode_id=payload.episode_id,
            goal=payload.goal, scene_key=payload.scene_key, user=user,
            idempotency_key=payload.idempotency_key,
            confirmed_cloud=payload.user_confirmed_cloud,
            approval_valid_seconds=payload.approval_valid_seconds)
    except agents.AgentInvalid as exc:
        raise _bad(exc) from exc
    run = out["run"]
    return {"run": agents.run_row_view(run, agents.agent_runs.steps(run["id"])),
            "reused": out["reused"], "route": out["route"],
            "plan_job_id": out.get("plan_job_id"),
            "note": out["note"] if "note" in out else
            "这条运行的意图和已发起的某条完全一样，平台没有再排一条"}


@router.get("/agent/runs")
def list_runs(project_id: int, limit: int = 50,
              user=Depends(require_permission("project:read"))) -> dict:
    access.ensure(user, project_id, "read")
    rows = agents.agent_runs.list_by_project(project_id, limit=max(1, min(limit, 200)))
    # 一条 IN 查询取齐所有步骤，再一次读回行内 Job：列表与详情给出同一份字段，
    # 不再出现「列表里 job_status 是空串、详情里却有值」。
    steps_by_run = agents.agent_runs.steps_for_runs([r["id"] for r in rows])
    job_ids = [s["job_id"] for steps in steps_by_run.values()
               for s in steps if s["job_id"]]
    jobs_by_id = agents.job_repo.get_many(job_ids, with_derived=False) if job_ids else {}
    return {"items": [agents.run_row_view(r, steps_by_run.get(r["id"], []), jobs_by_id)
                      for r in rows],
            "status_labels": agents.RUN_STATUS_LABEL}


@router.get("/agent/runs/{run_id}")
def run_detail(run_id: int,
               user=Depends(require_permission("project:read"))) -> dict:
    """运行详情：步骤（带关联作业的现读状态）＋事件时间线（倒序）。"""
    try:
        return agents.detail(run_id, user)
    except agents.AgentNotFound as exc:
        raise _bad(exc, 404) from exc


@router.post("/agent/runs/{run_id}/pause")
def pause(run_id: int, user=Depends(require_permission("job:cancel"))) -> dict:
    try:
        return agents.pause(run_id, user)
    except agents.AgentNotFound as exc:
        raise _bad(exc, 404) from exc
    except agents.AgentConflict as exc:
        raise _bad(exc, 409) from exc


@router.post("/agent/runs/{run_id}/resume")
def resume(run_id: int, user=Depends(require_permission("job:cancel"))) -> dict:
    try:
        return agents.resume(run_id, user)
    except agents.AgentNotFound as exc:
        raise _bad(exc, 404) from exc
    except agents.AgentConflict as exc:
        raise _bad(exc, 409) from exc


@router.post("/agent/runs/{run_id}/cancel")
def cancel(run_id: int, reason: str = "",
           user=Depends(require_permission("job:cancel"))) -> dict:
    """取消。要人自己确认那句话由前端弹窗负责，这里只收原因。

    取消会同时作废该运行所有还没做完的步骤（`agents._will`），
    否则审核队列里会留下一批"签了也永远不会执行"的授权——
    那种待办比没有待办更消耗人。
    """
    try:
        return agents.cancel(run_id, user, reason)
    except agents.AgentNotFound as exc:
        raise _bad(exc, 404) from exc
    except agents.AgentConflict as exc:
        raise _bad(exc, 409) from exc


@router.get("/agent/pending")
def pending(user=Depends(require_permission("review:read"))) -> dict:
    """待审批那几行。**这里只能看，不能签**——签字在审核中心（不变量 3）。

    界面上这一列必须把"去审"做成跳转，而不是就地给两个按钮。
    否则一个月后会有第二处审批入口，而两处互不知道对方签过什么，
    那正是说明书 §7 想建的 `agent_approval` 会带来的局面。
    """
    items = agents.pending_approvals(_visible_ids(user))
    return {"items": items, "count": len(items),
            "sign_module": "review", "sign_sub": 2,
            "note": "批准与驳回只在审核中心落笔；这一页显示状态并跳过去"}
