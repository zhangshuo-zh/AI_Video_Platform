from db import transaction
from models import JOB_FINISHED
from repositories.jobs import jobs as job_repo
from repositories.pipeline import NODE_ATTENTION, NODE_CANCELLED, NODE_PENDING, NODE_REVIEW, NODE_RUNNING, pipeline_repo

from .constants import JOB_NODES, PipelineConflict, PipelineMissing
from .gates import retry_gate
from .views import run_view


# ---------- 暂停／恢复／取消／重试 ----------
def pause_run(run_id: int) -> dict:
    with transaction():
        run = _need_run(run_id)
        if run["cancelled"]:
            raise PipelineConflict("这条运行已经取消了")
        if run["finished_at"]:
            raise PipelineConflict("这条运行已经结束，不用暂停")
        if not run["paused"]:
            pipeline_repo.set_paused(run_id, True)
            pipeline_repo.add_event(run_id, None, "run_paused", {})
    # 已经在跑的作业不动：暂停的意思是不再派新的，不是把模型进程杀掉
    return run_view(run_id)




def resume_run(run_id: int) -> dict:
    with transaction():
        run = _need_run(run_id)
        if run["cancelled"]:
            raise PipelineConflict("这条运行已经取消了，不能恢复；重新起一条")
        if run["finished_at"]:
            raise PipelineConflict("这条运行已经结束")
        if run["paused"]:
            pipeline_repo.set_paused(run_id, False)
            pipeline_repo.add_event(run_id, None, "run_resumed", {})
    from .dispatch import advance_run   # 同上
    advance_run(run_id)
    return run_view(run_id)




def cancel_run(run_id: int, reason: str = "") -> dict:
    """取消：标记运行、对能安全取消的作业发取消请求、把还没开始的节点收成 cancelled。

    三条不做：不删已生成的产物、不改已完成节点、不强杀不能安全取消的模型任务。
    正在跑的作业只发 `cancel_requested`，它真的停下来之后由 `_sync_job_nodes`
    按真实状态收节点——所以取消之后节点可能还显示"执行中"一小会儿，那是事实。
    """
    with transaction():
        run = _need_run(run_id)
        if run["cancelled"]:
            return run_view(run_id)
        pipeline_repo.set_cancelled(run_id, reason or "人工取消")
        for row in pipeline_repo.nodes_for_run(run_id):
            node = dict(row)
            if node["status"] == NODE_PENDING:
                pipeline_repo.finish_node(node["id"], NODE_CANCELLED,
                                          expect=NODE_PENDING,
                                          error_message="运行取消时这一格还没开始")
            elif node["status"] in (NODE_REVIEW, NODE_ATTENTION) \
                    and not node["job_id"]:
                # 人工确认这类节点没有作业可取消（它等的是签字）。
                # 不在这里收掉的话，运行已经取消了、这一格却还挂着"等待审核"，
                # 之后谁再来签一个字，`_sync_review_nodes` 会把它推成已完成——
                # 取消的运行不该被一次迟到的签字复活。
                pipeline_repo.finish_node(node["id"], NODE_CANCELLED,
                                          expect=node["status"],
                                          error_message="运行取消时这一格还在等人工处理")
            elif node["status"] in (NODE_RUNNING, NODE_REVIEW, NODE_ATTENTION) \
                    and node["job_id"]:
                job = job_repo.get(node["job_id"])
                if job is not None and job["status"] not in JOB_FINISHED:
                    job_repo.request_cancel(node["job_id"])
        pipeline_repo.add_event(run_id, None, "run_cancelled",
                                {"reason": (reason or "人工取消")[:200]})
    return run_view(run_id)




def retry_node(node_id: int, user_id: int | None) -> dict:
    """单独重试一个失败节点。旧 Job、旧产物、旧签字一个都不动。

    作业节点走现有 `jobs.retry()`：新建一行、`attempt+1`、原行记
    `superseded_by_job_id`（附件 §6.4 要的语义已经在 Job 层实现，不另写一套）。
    内部检查节点直接回到 pending，下一轮重算。
    被驳回的审核节点不给重试——驳回意味着产物要重做，得先重试上游生成节点。

    返回整条运行的视图，与暂停／恢复／取消一致：重试一格会牵动下游一整片，
    界面要刷的是整屏，不是那一格。
    """
    node = pipeline_repo.get_node(node_id)
    if node is None:
        raise PipelineMissing("节点不存在")
    run = _need_run(node["run_id"])
    job = job_repo.get(node["job_id"]) if node["job_id"] else None
    reason = retry_gate(node, run, job)
    if reason:
        raise PipelineConflict(reason)

    new_job_id = None
    new_status = NODE_PENDING
    # `job_repo.retry()` 必须与节点回写在**同一个事务**里（2026-11-08，P0）。
    #
    # 原来是两段：`retry()` 自己开 `get_conn()` 提交（一条 INSERT 新 queued 作业
    # ＋ 一条 UPDATE 旧行的 superseded_by_job_id），然后才进 `with transaction()`
    # 回写节点。进程若死在这两行之间，库里就留下一条**已排队、worker 会照常领走、
    # 可能真的去调云端并按秒计费、但没有任何节点指向它**的作业——界面上查不到，
    # 这正是《设计稿》D2 裁定要避免的"没有归属的付费作业"。
    #
    # 放进事务里安全，是因为这两步都只是库内写（`retry()` 与 `reset_for_retry()`
    # 都走 `get_conn()`，会自动借用外层那条连接），没有文件、Git 或网络操作——
    # 符合 `db.transaction()` 文档写的那条边界。
    with transaction():
        if node["node_type"] in JOB_NODES and node["job_id"]:
            new_job_id = job_repo.retry(job, user_id)["id"]
            # 新作业已经排进队列了，节点直接跟着它走；回 pending 会让调度器再提交一条
            new_status = NODE_RUNNING
        pipeline_repo.reset_for_retry(node_id, job_id=new_job_id, status=new_status)
        # 已经收尾的运行要重新打开，否则节拍只会对齐真相、不再往下派活
        pipeline_repo.reopen(run["id"])
        pipeline_repo.add_event(run["id"], node_id, "node_retried", {
            "old_job_id": node["job_id"], "new_job_id": new_job_id,
            "retry_count": int(node["retry_count"] or 0) + 1})
    # 延迟导入：run_create/run_control 在模块级不依赖 dispatch（否则成环）
    from .dispatch import advance_run
    advance_run(run["id"])
    return run_view(run["id"])




def _need_run(run_id: int):
    run = pipeline_repo.get_run(run_id)
    if run is None:
        raise PipelineMissing("生产运行不存在")
    return run
