"""产线中心接口（0026／阶段 B）。

两层授权与其余模块一模一样：`require_permission(code)` 管"这类事能不能做"，
`access.ensure(user, project_id, need)` 管"这个项目归不归他"。
看不见的项目一律 404，不泄露它存在。

权限点**一个都没新加**（附件 §7.3「不随意增加权限点」）：
  * 发起运行／重试节点／推进＝`job:submit`——因为它们就是会建 Job 的动作；
  * 暂停／恢复／取消＝`job:cancel`；
  * 读＝`job:read`；模板管理＝`system:config`（这一版只读，不给改内置模板）。

副作用要在界面上说清：`job:submit` 意味着 editor 也能起一条整集产线。
他今天已经能逐条提交作业，所以这不是新授权；而云端作业仍被 `submit_job`
逐条拦着要确认单，产线不会替人花钱。
"""
from fastapi import APIRouter, Depends, HTTPException, status as http_status

import access
import pipeline
from deps import require_permission
from models import (PipelineCancelIn, PipelineRunIn, PipelineRunOut,
                    PipelineRunSummaryOut, PipelineTemplateOut)
from repositories.pipeline import pipeline_repo

router = APIRouter(prefix="/api", tags=["pipeline"])


def _template(template_id: int):
    row = pipeline_repo.get_template(template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="流程模板不存在")
    return row


def _run(run_id: int, user, need: str = "read"):
    """取运行并过项目归属那道门。节点与事件都挂在运行下，所以只判这一层。"""
    row = pipeline_repo.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="生产运行不存在")
    access.ensure(user, row["project_id"], need)
    return row


def _node(node_id: int, user, need: str = "read"):
    row = pipeline_repo.get_node(node_id)
    if row is None:
        raise HTTPException(status_code=404, detail="节点不存在")
    run = pipeline_repo.get_run(row["run_id"])
    if run is None:
        raise HTTPException(status_code=404, detail="节点不存在")
    access.ensure(user, run["project_id"], need)
    return row, run


def _wrap(fn, *args, **kwargs):
    """把编排层的三种异常收成 HTTP 语义，四个端点共用一份，不各写一遍。"""
    try:
        return fn(*args, **kwargs)
    except pipeline.PipelineMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except pipeline.PipelineConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except pipeline.PipelineInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/pipeline/templates", response_model=list[PipelineTemplateOut])
def list_templates(user=Depends(require_permission("job:read"))
                   ) -> list[PipelineTemplateOut]:
    """内置模板清单。第一版只读——拖拽式流程编辑器不在这一阶段。"""
    return [PipelineTemplateOut(**pipeline.template_view(row))
            for row in pipeline_repo.latest_templates()]


@router.get("/pipeline/templates/{template_id}", response_model=PipelineTemplateOut)
def get_template(template_id: int,
                 user=Depends(require_permission("job:read"))) -> PipelineTemplateOut:
    return PipelineTemplateOut(**pipeline.template_view(_template(template_id)))


@router.post("/projects/{project_id}/pipeline/runs", response_model=PipelineRunOut,
             status_code=http_status.HTTP_201_CREATED)
def create_run(project_id: int, payload: PipelineRunIn,
               user=Depends(require_permission("job:submit"))) -> PipelineRunOut:
    """发起一次单集生产。同一集上只允许一条未结束的运行，连点两次不会排两套作业。"""
    access.ensure(user, project_id, "write")
    view = _wrap(pipeline.create_run, project_id, payload.episode_id,
                 payload.template_id, user["id"],
                 (payload.idempotency_key or "").strip() or None)
    return PipelineRunOut(**view)


@router.get("/projects/{project_id}/pipeline/runs",
            response_model=list[PipelineRunSummaryOut])
def list_runs(project_id: int,
              user=Depends(require_permission("job:read"))
              ) -> list[PipelineRunSummaryOut]:
    access.ensure(user, project_id, "read")
    return [PipelineRunSummaryOut(**row) for row in pipeline.runs_for_project(project_id)]


@router.get("/pipeline/runs/{run_id}", response_model=PipelineRunOut)
def get_run(run_id: int, user=Depends(require_permission("job:read"))) -> PipelineRunOut:
    _run(run_id, user)
    return PipelineRunOut(**_wrap(pipeline.run_view, run_id))


@router.post("/pipeline/runs/{run_id}/advance", response_model=PipelineRunOut)
def advance_run(run_id: int,
                user=Depends(require_permission("job:submit"))) -> PipelineRunOut:
    """手动推进一步。与 worker 循环里的 `pipeline.tick()` 共用同一个函数，
    所以"人点一下"和"节拍自己走"不会走出两种结果。"""
    _run(run_id, user, "write")
    return PipelineRunOut(**_wrap(pipeline.advance_run, run_id))


@router.post("/pipeline/runs/{run_id}/pause", response_model=PipelineRunOut)
def pause_run(run_id: int,
              user=Depends(require_permission("job:cancel"))) -> PipelineRunOut:
    """暂停＝不再派新节点。已经在跑的作业按现有规则继续，不强杀模型进程。"""
    _run(run_id, user, "write")
    return PipelineRunOut(**_wrap(pipeline.pause_run, run_id))


@router.post("/pipeline/runs/{run_id}/resume", response_model=PipelineRunOut)
def resume_run(run_id: int,
               user=Depends(require_permission("job:cancel"))) -> PipelineRunOut:
    _run(run_id, user, "write")
    return PipelineRunOut(**_wrap(pipeline.resume_run, run_id))


@router.post("/pipeline/runs/{run_id}/cancel", response_model=PipelineRunOut)
def cancel_run(run_id: int, payload: PipelineCancelIn,
               user=Depends(require_permission("job:cancel"))) -> PipelineRunOut:
    """取消：能安全取消的作业发取消请求，已完成的节点与已生成的产物一律不动。"""
    _run(run_id, user, "write")
    return PipelineRunOut(**_wrap(pipeline.cancel_run, run_id,
                                  payload.reason.strip()))


@router.post("/pipeline/nodes/{node_id}/retry", response_model=PipelineRunOut)
def retry_node(node_id: int,
               user=Depends(require_permission("job:submit"))) -> PipelineRunOut:
    """只重试这一格。旧 Job、旧产物、旧签字都留着，新作业另起一行。"""
    _node(node_id, user, "write")
    return PipelineRunOut(**_wrap(pipeline.retry_node, node_id, user["id"]))
