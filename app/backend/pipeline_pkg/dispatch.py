import jobfiles
import json

from db import transaction
from repositories.jobs import jobs as job_repo
from repositories.pipeline import NODE_ATTENTION, NODE_CANCELLED, NODE_DONE, NODE_FAILED, NODE_FINAL, NODE_PENDING, NODE_REVIEW, NODE_RUNNING, pipeline_repo

from .constants import MAX_DISPATCH_PER_TICK, MAX_LAYERS, PipelineMissing
from .execute import _commit_composition, _commit_submission, _execute_node
from .graph import _deps_of, _find_approval, _node_map, _upstream_job
from .views import run_view


# ---------- 推进（唯一的调度入口） ----------
def tick(limit: int = 20) -> dict:
    """worker 每轮调一次：把所有还没结束的运行各推进一轮。

    放在 worker 而不是 API 进程：worker 已经是常驻循环、已经在每轮做
    `recover_stale()`，也是唯一会写 Job 状态的进程。一个进程内只有一处推进，
    天然不会出现两个调度器互相抢。
    """
    advanced = 0
    for run in pipeline_repo.tickable_runs(limit):
        advance_run(run["id"])
        advanced += 1
    return {"runs": advanced}




def advance_run(run_id: int) -> dict:
    """把一条运行推进一步。重复调用是安全的：每一步都按当前真状态现算。

    **写事务只包「库内状态推进」**：读 run/节点、同步真状态、分层算出该派
    哪些节点、给运行收尾。建 Job、建成片、跑 ffprobe 都不在事务里——它们
    要么发 HTTP 探测、要么起子进程，最长能占住写锁几十秒；而 `db.get_conn`
    只等 5 秒，另一条连接（包括 worker 每 0.2 秒的续租心跳）就会撞
    `database is locked`。

    派发改成两阶段：事务内用 `try_begin()` 原子转态占住这一格并把请求记下来
    （所以两个进程同时推进时仍只有一个能派），提交后立刻把请求发出去再回写
    job_id。中途崩了，这一格停在 running 且 job_id 为空，下一轮
    `_sync_job_nodes` 会明说成「需要重试」——这是可见、可恢复的失败，好过
    「Job 建好了、节点却不知道」那样重复扣费。

    派发是**分层**的：`_dispatch_ready` 拿的依赖快照是这一层开始时的状态，
    所以一层只放出"前置在本层之前就已经完成"的节点。内部检查节点是当场做完的，
    于是要循环到"这一层没有新东西可放"为止——否则读项目→读集→查分镜这种
    纯检查链，要点三次"推进"才走完一格，界面上看就是卡住。
    """
    pending_jobs: list[tuple[dict, object, str]] = []
    pending_compose: list[tuple[dict, str]] = []
    probes = _prefetch_export_probes(run_id)
    with transaction():
        run = pipeline_repo.get_run(run_id)
        if run is None:
            raise PipelineMissing("生产运行不存在")
        nodes = pipeline_repo.nodes_for_run(run_id)
        if run["cancelled"] or run["finished_at"] or run["paused"]:
            # 不再派新节点，但**仍然对齐真相**：已经在跑的作业会继续跑完／被取消，
            # 不同步的话取消之后那一格会永远显示"执行中"，人看不出它到底怎么样了。
            _sync_job_nodes(run, nodes)
            _sync_review_nodes(run, nodes)
            return run_view(run_id)

        _sync_job_nodes(run, nodes)
        _sync_review_nodes(run, nodes)
        for _ in range(MAX_LAYERS):
            nodes = pipeline_repo.nodes_for_run(run_id)
            if not _dispatch_ready(run, nodes, pending_jobs=pending_jobs,
                                   pending_compose=pending_compose, probes=probes):
                break
        nodes = pipeline_repo.nodes_for_run(run_id)
        _finalize(run, nodes)
    # 写锁已经放开：下面这些动作可能发 HTTP、起 ffprobe，但不再挡别人的写。
    # 仍在同一次调用里同步做完，所以 advance_run 返回时节点与 Job 已经对齐。
    for node, key in pending_compose:
        _commit_composition(run, node, key)
    for node, payload, key in pending_jobs:
        _commit_submission(run, node, payload, key)
    return run_view(run_id)




def _prefetch_export_probes(run_id: int) -> dict[int, object]:
    """事务外把 `export_validate` 要用的 ffprobe 先跑掉。

    `validators.inspect` 起子进程（最长 30 秒），放在 `BEGIN IMMEDIATE` 里
    等于让所有写连接排队。这里全程只读，失败也只把异常带回去让节点明确失败；
    没覆盖到的节点仍走事务内那条现场探测（兼容直接调 `_dispatch_ready` 的调用方）。
    """
    try:
        nodes = pipeline_repo.nodes_for_run(run_id)
        by_key = _node_map(nodes)
        from composition import validators
        from composition.service import view as composition_view
        from repositories.compositions import compositions

        probes: dict[int, object] = {}
        for row in nodes:
            node = dict(row)
            if node["node_type"] != "export_validate" or node["status"] != NODE_PENDING:
                continue
            if not all(by_key.get(d, {}).get("status") == NODE_DONE
                       for d in _deps_of(node)):
                continue
            job_id = _upstream_job(node, by_key)
            if job_id is None:
                continue
            comp = compositions.by_job(job_id)
            if comp is None:
                continue
            try:
                view = composition_view(comp["id"])
                target = jobfiles.resolve_artifact(view.get("output_path") or "")
                if target is None:
                    continue
                probes[node["id"]] = validators.inspect(target)
            except Exception as exc:
                probes[node["id"]] = exc
        return probes
    except Exception:
        return {}




def _sync_job_nodes(run, nodes) -> None:
    """按 Job 的**真实状态**修节点。这就是重启恢复：不猜、不重投，只对齐。"""
    for row in nodes:
        node = dict(row)
        if node["status"] not in (NODE_RUNNING, NODE_ATTENTION):
            continue
        if node["job_id"] is None:
            # 作业行被删掉时，外键把节点上的 job_id 置空（SET NULL）。
            # 不处理的话这一格会永远停在"执行中"——那是个卡死态，
            # 既不会被调度、也不给人一个可以重试的入口。
            pipeline_repo.finish_node(
                node["id"], NODE_FAILED, expect=node["status"],
                error_code="job_missing",
                error_message="关联的作业已经不在了（可能随项目或集一起被清理）；"
                              "这一格需要重试。")
            pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                    {"reason": "job_missing"})
            continue
        job = job_repo.get(node["job_id"])
        if job is None:
            pipeline_repo.finish_node(
                node["id"], NODE_FAILED, expect=node["status"],
                error_code="job_missing",
                error_message="关联的作业已经不在了（可能随项目一起被删）；"
                              "这一格需要重试。")
            pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                    {"reason": "job_missing"})
            continue
        status = job["status"]
        if status in ("queued", "starting", "running", "cancel_requested"):
            continue                      # 还在跑，什么都不改
        if status == "succeeded":
            artifacts = jobfiles.artifacts(job["result_paths_json"])
            pipeline_repo.finish_node(
                node["id"], NODE_DONE, expect=node["status"],
                output={"job_id": job["id"], "artifacts": len(artifacts),
                        "provider": job["provider_code"], "model": job["model_code"]})
            pipeline_repo.add_event(run["id"], node["id"], "node_completed",
                                    {"job_id": job["id"]})
        elif status == "needs_attention":
            # 云端结果未知／账单待核对。**不自动重试**：那可能造成第二次付费调用。
            if node["status"] != NODE_ATTENTION:
                pipeline_repo.finish_node(
                    node["id"], NODE_ATTENTION, expect=node["status"],
                    error_code="job_needs_attention",
                    error_message=(job["attention_reason"] or "作业结果待人工核对")
                    + "；去生成中心核对账单之后再决定要不要重试。")
                pipeline_repo.add_event(run["id"], node["id"], "node_attention",
                                        {"job_id": job["id"]})
        else:                              # failed / cancelled
            if run["cancelled"] and status == "cancelled":
                # 是"这条运行被取消"顺手取消的作业，不是它自己跑坏了。
                # 记成 cancelled：界面上不该把人的取消决定显示成一次失败。
                pipeline_repo.finish_node(
                    node["id"], NODE_CANCELLED, expect=node["status"],
                    error_code="run_cancelled",
                    error_message="运行取消时这一格跟着一起取消了。")
                pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                        {"job_id": job["id"],
                                         "reason": "run_cancelled"})
                continue
            pipeline_repo.finish_node(
                node["id"], NODE_FAILED, expect=node["status"],
                error_code=job["error_code"] or status,
                error_message=(job["error_message"] or "")[:500]
                or f"作业 #{job['id']} 的状态是 {status}")
            pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                    {"job_id": job["id"], "job_status": status})




def _sync_review_nodes(run, nodes) -> None:
    """按审核中心的签字修节点。产线只读结论，绝不写结论（不变量 3）。"""
    for row in nodes:
        node = dict(row)
        if node["status"] != NODE_REVIEW:
            continue
        try:
            node_input = json.loads(node["input_json"] or "{}")
        except ValueError:
            node_input = {}
        sign = _find_approval(node, node_input)
        if sign is None:
            continue
        decision = sign["decision"]
        if decision == "approved":
            pipeline_repo.finish_node(
                node["id"], NODE_DONE, expect=NODE_REVIEW, review_id=sign["id"],
                output={"review_item_id": sign["id"], "anchor": sign["anchor"],
                        "note": sign["note"], "signed_by": sign["signed_by_name"] or ""})
            pipeline_repo.add_event(run["id"], node["id"], "node_completed",
                                    {"review_item_id": sign["id"]})
        elif decision == "rejected":
            pipeline_repo.finish_node(
                node["id"], NODE_FAILED, expect=NODE_REVIEW, review_id=sign["id"],
                error_code="review_rejected",
                error_message=(f"{sign['signed_by_name'] or '审核人'}驳回"
                               f"{('：' + sign['note']) if sign['note'] else ''}"
                               "；重做上游生成节点之后这一格才会重新等签字。"))
            pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                    {"reason": "review_rejected",
                                     "review_item_id": sign["id"]})
        # returned（退回）停在 waiting_review：退回是"再改改"，不是终局


def _dispatch_ready(run, nodes, *, pending_jobs=None, pending_compose=None,
                    probes=None) -> int:
    """把"依赖都完成了"的 pending 节点派出去。返回这一层派了几个。

    依赖状态取自传进来的那份快照，所以一层之内刚完成的节点不会立刻放出下游；
    调用方按层循环（见 `advance_run`），每层重读一次。
    `pending_jobs`／`pending_compose` 由 `advance_run` 传入：真正建 Job／成片的
    动作要等写事务提交后再做，本函数只负责在事务里原子占住这一格。
    """
    by_key = _node_map(nodes)
    dispatched = 0
    for row in nodes:
        if dispatched >= MAX_DISPATCH_PER_TICK:
            break
        node = dict(row)
        if node["status"] != NODE_PENDING:
            continue
        deps = _deps_of(node)
        if not all(by_key.get(d, {}).get("status") == NODE_DONE for d in deps):
            continue                       # 前置没完成：blocked（现算，不入库）
        dispatched += 1
        _execute_node(run, node, by_key, pending_jobs=pending_jobs,
                      pending_compose=pending_compose, probes=probes)
    return dispatched




def _unreachable(nodes) -> set[str]:
    """永远跑不到的待执行节点：前置里有失败／取消的，以及它们向下传的。

    阶段 C 验收撞出来的：一格失败之后，下游那些"等前置"的节点永远不会进终态，
    于是 `_finalize` 的"全部终态"条件永远不成立——运行没有 `finished_at`，
    同一集就被自己挡住（再发起回 409），worker 还会每轮白推一次。
    收尾的条件改成"全部终态，或只剩跑不到的"，运行才算结束；
    节点本身仍留在 pending（读时显示"等待前置"），那是事实：它确实没跑。
    """
    status = {n["node_key"]: n["status"] for n in nodes}
    deps = {n["node_key"]: _deps_of(n) for n in nodes}
    dead = {k for k, v in status.items() if v in (NODE_FAILED, NODE_CANCELLED)}
    changed = True
    while changed:
        changed = False
        for key, value in status.items():
            if value == NODE_PENDING and key not in dead \
                    and any(d in dead for d in deps.get(key, [])):
                dead.add(key)
                changed = True
    return dead




def _finalize(run, nodes) -> None:
    """全部节点进终态（或只剩跑不到的）时给运行收尾。

    状态本身不落库——它是现算的（见 run_status）。
    """
    dead = _unreachable(nodes)
    if any(n["status"] not in NODE_FINAL and n["node_key"] not in dead for n in nodes):
        return
    failed = [n for n in nodes if n["status"] == NODE_FAILED]
    pipeline_repo.finish(run["id"])
    pipeline_repo.add_event(run["id"], None,
                            "run_failed" if failed else "run_completed",
                            {"nodes": len(nodes), "failed": len(failed)})
