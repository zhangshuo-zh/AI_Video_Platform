"""项目生产工作台与当前上下文接口（0037）。

三条纪律跟着任务书走：

1. **只读**。这一族接口不建作业、不改状态、不签字；写操作仍归各自模块
   （提交在生成中心、签字在审核中心、合成在成片中心）。工作台只是把它们
   的数字摊在一屏上。
2. **上下文不是权限**。`/api/context` 存的是"这个人上次站在哪一集"，
   读写都要过一次项目可见性；看不见的项目一律 404，不泄露它存在
   （与 `access.ensure` 同一口径）。
3. **数字现算**。阶段进度、下一步任务全部来自 `production.py` 的聚合读，
   界面上不许再算第二遍——否则"工作台说 3 镜待生成、生成中心说 4 条排队"
   这种事故迟早会来。
"""
from fastapi import APIRouter, Depends, HTTPException

import access
import precheck
import production
from deps import require_permission, require_user
from models import ContextIn, ProjectProductionOut
from repositories.content import episodes
from repositories.context import context

router = APIRouter(prefix="/api", tags=["production"])


def _view(row) -> dict:
    if row is None:
        return {"project_id": None, "episode_id": None}
    return {"project_id": row["project_id"], "episode_id": row["episode_id"]}


@router.get("/context")
def get_context(user=Depends(require_user)) -> dict:
    """读当前上下文。项目已经看不见时**当场抹掉**并返回空，不拿旧行骗人。"""
    row = context.get(user["id"])
    if row is None or row["project_id"] is None:
        return _view(row)
    try:
        access.ensure(user, row["project_id"], "read")
    except HTTPException:
        # 项目被删、被转移、或他已被移出成员：上下文作废，但账号别的东西不受影响
        context.set(user["id"], project_id=None, episode_id=None)
        return {"project_id": None, "episode_id": None, "cleared": "项目已不可见"}
    if row["episode_id"] is not None:
        ep = episodes.get(row["episode_id"])
        if ep is None or int(ep["project_id"]) != int(row["project_id"]):
            context.set(user["id"], project_id=row["project_id"], episode_id=None)
            return {"project_id": row["project_id"], "episode_id": None,
                    "cleared": "这一集已不属于该项目"}
    return _view(row)


@router.put("/context")
def put_context(payload: ContextIn, user=Depends(require_user)) -> dict:
    """写当前上下文。只接受"看得见的项目 × 属于该项目的集"。"""
    project_id = payload.project_id
    episode_id = payload.episode_id
    if project_id is None:
        context.set(user["id"], project_id=None, episode_id=None)
        return {"project_id": None, "episode_id": None}
    access.ensure(user, project_id, "read")
    if episode_id is not None:
        ep = episodes.get(episode_id)
        if ep is None or int(ep["project_id"]) != int(project_id):
            raise HTTPException(status_code=422,
                                detail="这一集不属于该项目；换项目后请先选集")
    row = context.set(user["id"], project_id=int(project_id),
                      episode_id=int(episode_id) if episode_id is not None else None)
    return _view(row)


@router.get("/projects/{project_id}/production", response_model=ProjectProductionOut)
def project_production(project_id: int, episode_id: int | None = None,
                       user=Depends(require_permission("project:read"))
                       ) -> ProjectProductionOut:
    """项目生产总览。`episode_id` 不传时按上下文那一集算，没有上下文就取第一集。"""
    access.ensure(user, project_id, "read")
    eps = episodes.list_by_project(project_id)
    target = episode_id
    if target is None:
        row = context.get(user["id"])
        if row is not None and row["project_id"] == project_id and row["episode_id"]:
            target = row["episode_id"]
    if target is None and eps:
        target = int(eps[0]["id"])
    overview = production.project_overview(project_id, current_episode_id=target)
    board = (production.episode_board(project_id, int(target))
             if target is not None else {"stages": [], "next_tasks": []})
    # `stats` 挂在这同一条接口上而不是新开一条：项目工作台一次读取就该同时给出
    # 「走到哪」与「跑了多少、花了多少、还要多久」；开两条迟早一处新一处旧。
    # 空板时 `episode_board` 只给 `{"stages": [], "next_tasks": []}`，
    # 而模型会给 episode_id/shots/counts 等补上默认值（None/0）。这**不是丢数据**，
    # 是补默认值：前端本来就写着 `board.shots || 0`、`board.stages || []`。
    return ProjectProductionOut(
        overview=overview, board=board,
        stats=production.project_stats(project_id, overview),
        stages=production.STAGES, statuses=production.PUBLIC_STATUSES)


# ---------------------------------------------------------------- 统一预检


@router.get("/precheck/shot/{shot_id}")
def shot_precheck_api(shot_id: int, capability: str = "video",
                      execution_mode: str | None = None,
                      confirmed_cloud: bool = False,
                      user=Depends(require_permission("job:read"))) -> dict:
    """一镜的生成前预检。参数不在这里收：界面选的那套参数由提交时的白名单兜。

    `params` 不接是刻意的：预检要回答的是"这一镜现在能不能开工"，
    而参数对不对在提交那一刻由 `adapters.validate_params()` 判——
    把同一份参数在两个地方各校验一遍，迟早出现"预检过了但提交 422"。
    """
    access.ensure_by("shot", shot_id, user, "read", "镜头")
    return precheck.shot_precheck(shot_id, capability=capability,
                                  execution_mode=execution_mode,
                                  confirmed_cloud=confirmed_cloud)


@router.get("/projects/{project_id}/precheck")
def episode_precheck_api(project_id: int, episode_id: int,
                         template_id: int | None = None,
                         user=Depends(require_permission("job:read"))) -> dict:
    """整集预检。产线发起弹窗用的就是这一份，不再自己拼一遍。"""
    access.ensure(user, project_id, "read")
    # `episode_id` 必须真属于这个项目（2026-11-10 审计修的真 bug）。
    #
    # 原来只判了 `project_id` 可见就把 `episode_id` 原样传给下层，于是
    # 「自己可见的 p + 别人的 e」能拿到 200：下层 `precheck.episode_precheck`
    # 第一句就 `shots.list_by_episode(episode_id)` 按别人的集算完了 1/2/6/7/8…
    # 各项，「这一集不属于该项目」只是最后一条 blocking 文案——数据早就出去了。
    # 拿自己的 p 配上遍历 e，能扫出全平台每一集的镜头数等结构信息。
    #
    # 走 `ensure_by` 让"别人的集"回 404，与"别人的项目回 404"同一条纪律。
    access.ensure_by("episode", episode_id, user, "read", "集")
    return precheck.episode_precheck(project_id, episode_id, template_id)


@router.get("/projects/{project_id}/composition-precheck")
def composition_precheck_api(project_id: int, episode_id: int,
                             subtitle_path: str | None = None,
                             user=Depends(require_permission("job:read"))) -> dict:
    """合成前预检（11 项）。点「新建成片」之前先跑它，阻塞项直接定位到镜。"""
    access.ensure(user, project_id, "read")
    # 同上：这一条更宽——下层 `composition_precheck` 会先按别人的集算出
    # 11 项里的 10 项才轮到归属检查。
    access.ensure_by("episode", episode_id, user, "read", "集")
    return precheck.composition_precheck(project_id, episode_id,
                                         subtitle_path=subtitle_path)


@router.get("/shots/{shot_id}/workbench")
def shot_workbench_api(shot_id: int,
                       user=Depends(require_permission("project:read"))) -> dict:
    """单镜头生产工作台：八段一屏（设定／引用／关键帧／视频／音频／预览／审核／可合成）。

    权限用 `project:read` 而不是 `job:read`：这一屏里有一半是资产引用与签字历史，
    它属于"看这一镜的生产档案"，不是看队列。真正的写动作仍在各自模块里
    （提交要 `job:submit`、签字要 `review:sign`），这里一个写口子都不开。
    """
    access.ensure_by("shot", shot_id, user, "read", "镜头")
    out = production.shot_workbench(shot_id)
    if out.get("error"):
        raise HTTPException(status_code=404, detail=out["error"])
    return out
