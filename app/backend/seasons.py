"""整季生产（阶段 E-2）：把一季的若干集排成批量，逐集起单集运行。

三条边界：

1. **一季不是第二个队列**。每一集仍是一条普通 `pipeline_run`（及其 Job），
   这一层只负责"什么时候起下一集、失败怎么恢复、整季停在哪个状态"。
2. **失败恢复不重跑已成功的镜头**。继续失败集时，只对运行里状态为 failed 的
   节点调 `pipeline.retry_node`；已完成节点、已用产物、已签字一律不动。
   被驳回的审核节点不自动重试——那要人先决定重做哪一镜。
3. **并发是上限，不是承诺**。同时推进几集由 `max_active_runs` 冻结，真正能不能
   并行还看 worker 的资源类槽位（E-1）；两个闸门都过才会同时跑。
"""
from __future__ import annotations

import json

import pipeline
from config import (SEASON_MAX_ACTIVE_LIMIT, SEASON_MAX_ACTIVE_RUNS,
                    SEASON_MAX_EPISODES)
from repositories.content import episodes
from repositories.pipeline import pipeline_repo
from repositories.projects import projects as project_repo
from repositories.seasons import seasons

PENDING = "pending"
FAILED = "failed"
RUNNING = "running"
PAUSED = "paused"
CANCELLED = "cancelled"
COMPLETED = "completed"
COMPLETED_WITH_ERRORS = "completed_with_errors"

SEASON_LABELS = {
    RUNNING: "生产中", PAUSED: "已暂停", CANCELLED: "已取消",
    COMPLETED: "已完成",
    COMPLETED_WITH_ERRORS: pipeline.RUN_STATUS_LABELS[COMPLETED_WITH_ERRORS],
}
ITEM_LABELS = {
    PENDING: "待开始", FAILED: "启动被拒",
    **{key: label for key, label in pipeline.RUN_STATUS_LABELS.items()},
}
TERMINAL_ITEM = {COMPLETED, COMPLETED_WITH_ERRORS, "failed", CANCELLED}


class SeasonInvalid(Exception):
    """请求或输入不成立（路由回 422）。"""


class SeasonMissing(Exception):
    """季／项目／模板看不见或不存在（路由回 404）。"""


class SeasonConflict(Exception):
    """当前状态不允许这个动作（路由回 409）。"""


def _clamp_active(value: int | None) -> int:
    default = SEASON_MAX_ACTIVE_RUNS
    if value is None:
        value = default
    try:
        value = int(value)
    except (TypeError, ValueError) as exc:
        raise SeasonInvalid("max_active_runs 必须是整数") from exc
    return max(1, min(SEASON_MAX_ACTIVE_LIMIT, value))


def create(project_id: int, template_id: int, created_by: int | None, *,
           episode_ids: list[int] | None = None, name: str = "",
           max_active_runs: int | None = None,
           idempotency_key: str | None = None) -> dict:
    """建一季并立刻排第一集。幂等键重复时返回同一季，不重复排产。"""
    project = project_repo.get(project_id)
    if project is None:
        raise SeasonMissing("项目不存在")
    template = pipeline_repo.get_template(template_id)
    if template is None:
        raise SeasonMissing("流程模板不存在")
    if not template["enabled"]:
        raise SeasonInvalid(f"模板「{template['name']}」已停用")
    try:
        definition = json.loads(template["definition_json"] or "{}")
    except ValueError as exc:
        raise SeasonInvalid("模板定义读不出来") from exc
    errors = pipeline.validate_definition(definition)
    if errors:
        raise SeasonInvalid(f"模板定义不成立：{'；'.join(errors)}")
    cloud_nodes = [n.get("key") for n in definition.get("nodes") or []
                   if isinstance(n, dict) and n.get("execution_mode") == "cloud"]
    if cloud_nodes:
        raise SeasonInvalid(
            f"这一版不代发云端作业（要逐条确认费用）：{', '.join(cloud_nodes)}。")

    if idempotency_key:
        existing = seasons.by_key(idempotency_key)
        if existing is not None:
            if int(existing["project_id"]) != int(project_id):
                raise SeasonConflict("这个幂等键已被别的项目占用")
            return view(int(existing["id"]))

    all_eps = episodes.list_by_project(project_id)
    if not all_eps:
        raise SeasonInvalid("这个项目还没有集；先去创作中心建集和分镜")
    by_id = {int(e["id"]): e for e in all_eps}
    if episode_ids:
        wanted = []
        seen = set()
        for raw in episode_ids:
            try:
                eid = int(raw)
            except (TypeError, ValueError) as exc:
                raise SeasonInvalid("集 id 必须是整数") from exc
            if eid in seen:
                continue
            if eid not in by_id:
                raise SeasonInvalid(f"集 #{eid} 不属于该项目")
            seen.add(eid)
            wanted.append(eid)
    else:
        wanted = [int(e["id"]) for e in all_eps]
    if not wanted:
        raise SeasonInvalid("至少要选一集")
    if len(wanted) > SEASON_MAX_EPISODES:
        raise SeasonInvalid(
            f"一次最多排 {SEASON_MAX_EPISODES} 集；请分批发起")

    label = (name or "").strip()[:80] or f"{template['name']} · 整季 {len(wanted)} 集"
    season_id = seasons.create(
        project_id=project_id, template_id=template_id, name=label,
        max_active_runs=_clamp_active(max_active_runs),
        idempotency_key=idempotency_key, created_by=created_by,
        episode_ids=wanted)
    _schedule(season_id)
    return view(season_id)


def tick(limit: int = 20) -> dict:
    """worker 每轮调一次：给还没结束的季补下一集，并同步失败原因。"""
    count = 0
    started = 0
    for row in seasons.active_seasons(limit):
        started += _schedule(int(row["id"]))
        count += 1
    return {"seasons": count, "started": started}


def _schedule(season_id: int) -> int:
    """按上限补排下一集。返回本次新起的运行条数。重复调用是安全的。"""
    season = seasons.get(season_id)
    if season is None or season["cancelled"] or season["paused"] \
            or season["finished_at"]:
        return 0
    limit = int(season["max_active_runs"] or 1)
    active = seasons.unfinished_runs(season_id)
    started = 0
    for item in seasons.startable_items(season_id):
        if active >= limit:
            break
        eid = int(item["episode_id"])
        key = f"season-{season_id}-ep{eid}"
        try:
            run = pipeline.create_run(
                int(season["project_id"]), eid, int(season["template_id"]),
                season["created_by"], idempotency_key=key)
            seasons.attach_run(int(item["id"]), int(run["id"]))
            active += 1
            started += 1
        except pipeline.PipelineConflict as exc:
            # 这一集已经有一条人在跑的运行：挂到那一集上，不另起一条。
            # 找不到就说明是别的业务闸（比如幂等键被占），照原话记下来。
            busy = pipeline_repo.unfinished_for_episode(
                int(season["project_id"]), eid)
            if busy is not None:
                seasons.attach_run(int(item["id"]), int(busy["id"]))
                active += 1
            else:
                seasons.set_error(int(item["id"]), str(exc))
        except pipeline.PipelineInvalid as exc:
            seasons.set_error(int(item["id"]), str(exc))
        except pipeline.PipelineMissing as exc:
            seasons.set_error(int(item["id"]), str(exc))

    _sync_errors(season_id)
    # 收口：没有可排的（包括被拦下的），也没有未结束的运行
    if not seasons.startable_items(season_id) \
            and seasons.unfinished_runs(season_id) == 0:
        seasons.finish(season_id)
    return started


def _zero_counts() -> dict:
    return {key: 0 for key in (pipeline.NODE_PENDING, pipeline.NODE_RUNNING,
                               pipeline.NODE_REVIEW, pipeline.NODE_ATTENTION,
                               pipeline.NODE_DONE, pipeline.NODE_FAILED,
                               pipeline.NODE_SKIPPED, pipeline.NODE_CANCELLED)}


def _sync_errors(season_id: int) -> None:
    """把运行里的失败原因抄到 item 上，成功的把旧原因清掉。

    item 只存一句显示用的人话；判定仍以节点状态为准（这里不写运行状态）。
    """
    counts_by_run = seasons.run_counts(season_id)
    for row in seasons.items_for(season_id):
        run_id = row["pipeline_run_id"]
        if not run_id:
            continue
        counts = _zero_counts()
        counts.update(counts_by_run.get(int(run_id), {}))
        status = pipeline.run_status({
            "id": int(run_id),
            "cancelled": row["run_cancelled"],
            "paused": row["run_paused"],
            "finished_at": row["run_finished_at"],
        }, counts)
        if status == "failed":
            message = ""
            for node in pipeline_repo.nodes_for_run(int(run_id)):
                if node["status"] == pipeline.NODE_FAILED:
                    message = node["error_message"] or node["error_code"]
                    break
            message = message or "运行失败；去产线中心看节点原因"
            if row["error_message"] != message:
                seasons.set_error(int(row["id"]), message)
        elif status in (COMPLETED, COMPLETED_WITH_ERRORS, CANCELLED) \
                and row["error_message"]:
            seasons.clear_error(int(row["id"]))


def pause(season_id: int) -> dict:
    season = _need(season_id)
    if season["cancelled"]:
        raise SeasonConflict("这一季已经取消了")
    if season["finished_at"]:
        raise SeasonConflict("这一季已经结束，不用暂停")
    seasons.set_paused(season_id, True)
    return view(season_id)


def resume(season_id: int) -> dict:
    season = _need(season_id)
    if season["cancelled"]:
        raise SeasonConflict("这一季已经取消；重新发起一季")
    if season["finished_at"]:
        raise SeasonConflict("这一季已经结束；失败集请用「继续失败集」")
    seasons.set_paused(season_id, False)
    _schedule(season_id)
    return view(season_id)


def cancel(season_id: int, reason: str = "") -> dict:
    season = _need(season_id)
    if season["cancelled"]:
        return view(season_id)
    note = (reason or "整季人工取消")[:500]
    seasons.set_cancelled(season_id, note)
    for row in seasons.items_for(season_id):
        if row["pipeline_run_id"] and not row["run_finished_at"] \
                and not row["run_cancelled"]:
            try:
                pipeline.cancel_run(int(row["pipeline_run_id"]), note)
            except pipeline.PipelineConflict:
                pass
    return view(season_id)


def retry_failed(season_id: int, user_id: int | None) -> dict:
    """继续失败集：重试运行里失败的节点／重排启动被拒的集，不重跑已成功的镜头。"""
    season = _need(season_id)
    if season["cancelled"]:
        raise SeasonConflict("这一季已经取消；重新发起一季")
    retried = 0
    skipped_rejected = 0
    cleared = 0
    problems: list[str] = []
    for row in seasons.items_for(season_id):
        run_id = row["pipeline_run_id"]
        if not run_id:
            if row["error_message"]:
                seasons.clear_error(int(row["id"]))
                cleared += 1
            continue
        for node in pipeline_repo.nodes_for_run(int(run_id)):
            if node["status"] != pipeline.NODE_FAILED:
                continue
            if node["error_code"] == "review_rejected":
                skipped_rejected += 1
                continue
            try:
                pipeline.retry_node(int(node["id"]), user_id)
                retried += 1
            except pipeline.PipelineConflict as exc:
                problems.append(str(exc))
            except pipeline.PipelineMissing as exc:
                problems.append(str(exc))
    # 收尾过的一季要重新打开，否则调度器不会再看它
    seasons.reopen(season_id)
    started = _schedule(season_id)
    body = view(season_id)
    body["retry"] = {"nodes": retried, "cleared_items": cleared,
                     "skipped_rejected": skipped_rejected,
                     "started_runs": started,
                     "problems": problems[:10]}
    return body


def view(season_id: int) -> dict:
    season = _need(season_id)
    rows = seasons.items_for(season_id)
    costs = seasons.costs_by_item(season_id)
    counts_by_run = seasons.run_counts(season_id)
    items = []
    for row in rows:
        item = _item_view(row, counts_by_run.get(int(row["pipeline_run_id"] or 0), {}),
                          costs.get(int(row["id"]), {}))
        items.append(item)
    done = sum(1 for i in items if i["status"] in (COMPLETED, COMPLETED_WITH_ERRORS))
    failed = sum(1 for i in items if i["status"] in ("failed", CANCELLED))
    problem = failed + sum(1 for i in items
                           if i["status"] == COMPLETED_WITH_ERRORS)
    pending = sum(1 for i in items if i["status"] == PENDING)
    running = len(items) - done - failed - pending
    finished = bool(season["finished_at"]) or bool(season["cancelled"])
    if season["cancelled"]:
        status = CANCELLED
    elif season["finished_at"]:
        status = COMPLETED_WITH_ERRORS if problem else COMPLETED
    elif season["paused"]:
        status = PAUSED
    else:
        status = RUNNING
    total = len(items)
    return {
        "id": int(season["id"]), "project_id": int(season["project_id"]),
        "project_name": season["project_name"],
        "template_id": int(season["template_id"]),
        "template_name": season["template_name"],
        "template_version": int(season["template_version"]),
        "name": season["name"],
        "max_active_runs": int(season["max_active_runs"]),
        "status": status, "status_label": SEASON_LABELS.get(status, status),
        "paused": bool(season["paused"]), "cancelled": bool(season["cancelled"]),
        "cancel_reason": season["cancel_reason"],
        "finished": finished,
        "counts": {"total": total, "completed": done, "failed": failed,
                   "pending": pending, "running": max(0, running)},
        "progress": {"done": done, "total": total,
                     "percent": round(done * 100.0 / total, 1) if total else 0.0},
        "cost": {"cost_micros": sum(int(i["cost"]["cost_micros"]) for i in items),
                 "jobs": sum(int(i["cost"]["jobs"]) for i in items),
                 "currencies": seasons.currencies(season_id)},
        "items": items,
        "created_by": season["created_by"],
        "created_by_name": season["created_by_name"] or "",
        "started_at": season["started_at"] or "",
        "finished_at": season["finished_at"] or "",
        "created_at": season["created_at"],
        "updated_at": season["updated_at"],
    }


def _item_view(row, counts: dict, cost: dict) -> dict:
    run_id = row["pipeline_run_id"]
    if not run_id:
        status = FAILED if row["error_message"] else PENDING
        return {
            "id": int(row["id"]), "episode_id": int(row["episode_id"]),
            "episode_seq": int(row["episode_seq"] or 0),
            "episode_title": row["episode_title"] or "",
            "position": int(row["position"]), "run_id": None,
            "status": status, "status_label": ITEM_LABELS.get(status, status),
            "error_message": row["error_message"] or "",
            "progress": {"done": 0, "total": 0, "percent": 0.0},
            "cost": {"cost_micros": int(cost.get("cost_micros", 0)),
                     "jobs": int(cost.get("jobs", 0))},
        }
    full = _zero_counts()
    full.update(counts)
    status = pipeline.run_status({
        "id": int(run_id),
        "cancelled": row["run_cancelled"],
        "paused": row["run_paused"],
        "finished_at": row["run_finished_at"],
    }, full)
    total = sum(full.values())
    done = full[pipeline.NODE_DONE] + full[pipeline.NODE_SKIPPED]
    return {
        "id": int(row["id"]), "episode_id": int(row["episode_id"]),
        "episode_seq": int(row["episode_seq"] or 0),
        "episode_title": row["episode_title"] or "",
        "position": int(row["position"]), "run_id": int(run_id),
        "status": status, "status_label": ITEM_LABELS.get(status, status),
        "error_message": row["error_message"] or "",
        "progress": {"done": done, "total": total,
                     "percent": round(done * 100.0 / total, 1) if total else 0.0},
        "counts": full,
        "cost": {"cost_micros": int(cost.get("cost_micros", 0)),
                 "jobs": int(cost.get("jobs", 0))},
    }


def list_for_project(project_id: int) -> list[dict]:
    return [view(int(row["id"])) for row in seasons.list_by_project(project_id)]


def _need(season_id: int):
    season = seasons.get(season_id)
    if season is None:
        raise SeasonMissing("整季记录不存在")
    return season
