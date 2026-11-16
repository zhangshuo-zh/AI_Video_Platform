"""作业服务 monitor 域。"""

from log import get_logger as _get_logger

from ..errors import JobInvalid
from config import LOG_SILENCE_SECONDS, QUEUE_STALL_SECONDS, WORKER_MAX_RUNTIME_SECONDS, WORKER_ONLINE_SECONDS
from datetime import datetime
from db import get_conn
from repositories.jobs import jobs as job_repo
import config
import sqlite3

_audit_log = _get_logger(__name__)





def _seconds_since(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return max(0, int((datetime.now()
                           - datetime.strptime(text, "%Y-%m-%d %H:%M:%S")).total_seconds()))
    except ValueError:
        return None





def concurrency_now() -> dict:
    """并发设置的现值 + 这台机器扛不扛得住。给 `/api/worker/status` 与设置接口共用。

    为什么放服务层而不是路由：两处（读状态、写设置）都要同一份评估，
    各写一遍迟早一处说危险、一处说没事——`diskplan` 模块头那句"两处各算一次，
    迟早一处说够、一处说不够"就是这条。
    """
    from repositories import worker_limit
    import concurrency_plan

    saved = worker_limit.get()
    # 库里没有值＝没人设过，报 None 而不是 1：这两件事在界面上要有区别。
    global_max = (saved or {}).get("global")
    per = (saved or {}).get("per") or {}
    if global_max is not None or per:
        source = "web"
        effective = {"global": global_max if global_max is not None else config.WORKER_MAX_GLOBAL,
                     "per": per}
    else:
        source = "default"
        effective = {"global": config.WORKER_MAX_GLOBAL, "per": {}}

    # 样本取最近成功的真作业（mock 与 ffmpeg 由 concurrency_plan 自己排除）。
    rows: list[dict] = []
    try:
        with get_conn() as conn:
            rows = [dict(r) for r in conn.execute(
                """SELECT capability, provider_code, resources_json
                     FROM job
                    WHERE status = 'succeeded'
                      AND resources_json IS NOT NULL
                      AND resources_json NOT IN ('{}', '')
                    ORDER BY id DESC
                    LIMIT ?""",
                (concurrency_plan.SAMPLE_LIMIT * len(config.RESOURCE_CLASSES),)
            ).fetchall()]
    except sqlite3.Error:
        rows = []

    # 正在跑的各算一类，让评估能顺带回答"现在已经在跑几条"。
    running_now: dict[str, int] = {}
    try:
        with get_conn() as conn:
            from repositories.jobs_parts.common import RESOURCE_CLASS_SQL
            for row in conn.execute(
                f"""SELECT {RESOURCE_CLASS_SQL} AS cls, COUNT(*) AS n
                      FROM job
                     WHERE status IN ('starting','running','cancel_requested')
                     GROUP BY cls""").fetchall():
                running_now[str(row["cls"])] = int(row["n"])
    except sqlite3.Error:
        running_now = {}

    assessment = concurrency_plan.assess(
        limits=effective, rows=rows, running_now=running_now)
    return {
        "global_max": global_max,
        "per": {str(k): int(v) for k, v in per.items()},
        "updated_at": (saved or {}).get("updated_at"),
        "updated_by": (saved or {}).get("updated_by"),
        "note": (saved or {}).get("note") or "",
        "source": source,
        "effective": effective,
        "assessment": assessment,
        "needs_acknowledge": concurrency_plan.needs_acknowledge(assessment),
    }



def set_concurrency(payload, user=None) -> dict:
    """保存网页上设的并发上限。危险档不勾 `acknowledge_risk` 直接拒。

    拒绝用 400 而不是静默存下：`claim_next` 只认数字，存进去下一轮真的会去领
    那么多条——OOM 起来是整台机器的事，不是一条作业失败。
    """
    from repositories import worker_limit
    import concurrency_plan

    per_in = payload.per if payload.per is not None else None
    if per_in is not None:
        unknown = sorted(set(per_in) - set(config.RESOURCE_CLASSES))
        if unknown:
            raise JobInvalid(
                f"不认识的资源类：{'、'.join(unknown)}；"
                f"认的只有 {'、'.join(config.RESOURCE_CLASSES)}")
        for name, value in per_in.items():
            if int(value) < 0:
                raise JobInvalid(f"{name} 的上限不能是负数")
    if payload.global_max is not None and int(payload.global_max) < 0:
        raise JobInvalid("全局上限不能是负数")

    # 先算"如果照这个存下去会怎样"，再决定要不要放行。
    trial = {"global": (int(payload.global_max) if payload.global_max is not None
                        else config.WORKER_MAX_GLOBAL),
             "per": {k: int(v) for k, v in (per_in or {}).items()}}
    rows: list[dict] = []
    try:
        with get_conn() as conn:
            rows = [dict(r) for r in conn.execute(
                """SELECT capability, provider_code, resources_json
                     FROM job
                    WHERE status = 'succeeded'
                      AND resources_json IS NOT NULL
                      AND resources_json NOT IN ('{}', '')
                    ORDER BY id DESC LIMIT ?""",
                (concurrency_plan.SAMPLE_LIMIT * len(config.RESOURCE_CLASSES),)
            ).fetchall()]
    except sqlite3.Error:
        rows = []
    assessment = concurrency_plan.assess(limits=trial, rows=rows)
    if concurrency_plan.needs_acknowledge(assessment) and not payload.acknowledge_risk:
        raise JobInvalid(
            f"{assessment['sentence']}。确认要这样设，"
            f"请勾选「我知道有内存溢出风险」再保存")

    saved = worker_limit.save(
        global_max=(int(payload.global_max) if payload.global_max is not None else None),
        per=({k: int(v) for k, v in per_in.items()} if per_in is not None else {}),
        updated_by=(user or {}).get("id") if isinstance(user, dict) else None,
        note=payload.note or "")

    # 变更痕：与选路配置同一张表、同一套纪律（0044）。失败不炸主流程。
    try:
        from repositories.config_log import record as _record
        _record("worker_limit", "singleton", "update",
                f"并发上限改为 全局={saved.get('global')} "
                f"每类={saved.get('per') or '默认'}（{assessment['verdict']}）",
                payload={"global": saved.get("global"), "per": saved.get("per"),
                         "verdict": assessment["verdict"]},
                created_by=(user or {}).get("id") if isinstance(user, dict) else None)
    except (sqlite3.Error, OSError, ValueError) as exc:
        _audit_log.debug("set_concurrency_fallback error=%s", type(exc).__name__)
        pass
    return concurrency_now()



def worker_status(user=None) -> dict:
    """worker 状态 + 队列健康。

    这里刻意**不**把"超时"写成 failed：判定与处置分开。
    界面看到告警，处置仍然是人取消或 worker 自己的 deadline 生效。

    给了 user 就把"正在跑哪条作业"的明细按可见范围裁一遍：顶栏那个小圆点是
    每人都在拉的，别人项目的 job id／供应商／模型不该顺着它漏出去。
    计数（队列长度、待核对数）不裁——那是共享机器的客观状态，不是项目内容。
    """
    state = job_repo.worker_state()
    health = job_repo.queue_health(WORKER_MAX_RUNTIME_SECONDS)
    nodes = job_repo.workers(WORKER_ONLINE_SECONDS)
    by_resource = job_repo.queue_by_resource()
    if user is not None:
        # **一次读出可见项目集，不再逐行判权限**（2026-11-09 外部审查第 10 条）。
        #
        # 原来是 `for row: ensure(user, row["project_id"], "read")`，每行一次
        # （或两次）回库。这是顶栏小圆点接口，几秒一次；实测 16 条 running
        # 时 10.67 ms，其中 8.43 ms 全花在这串逐行查询上——而判据只有
        # "自己拥有的 ∪ 作为成员加入的"一条，一次就能读完。
        #
        # 无主作业（`project_id IS NULL`，0039 之后删项目会留下）按 `ensure`
        # 的同一套纪律处理：只有管理员看得见，别人连"它存在"都不说。
        from access import visible_project_ids as _visible
        allowed = _visible(user)
        if allowed is not None:
            health = {**health, "running_jobs": [
                row for row in health["running_jobs"]
                if row["project_id"] in allowed]}
        # allowed is None ＝ 管理员，全放行，health 原样保留
    age = None
    online = False
    heartbeat = ""
    current_job_id = None
    live = [w for w in nodes if w["online"]]
    if live:
        online = True
        heartbeat = live[0]["heartbeat_at"] or ""
        age = live[0]["age_seconds"]
        current_job_id = next((w["current_job_id"] for w in live
                               if w["current_job_id"]), None)
    elif state is not None:
        # 兼容只写过单行 worker_state 的旧库／旧进程：没有节点明细时退回单行判断
        heartbeat = state["heartbeat_at"] or ""
        age = _seconds_since(heartbeat)
        online = age is not None and age <= WORKER_ONLINE_SECONDS
        current_job_id = state["current_job_id"] if online else None

    alerts: list[dict] = []
    if health["queued"] > 0 and not online:
        alerts.append({"level": "bad", "code": "worker_offline",
                       "text": f"worker 离线，队列里还有 {health['queued']} 条在等。"
                               f"启动：./run.sh worker"})
    if health["queued"] > 0 and online and (health["oldest_queued_seconds"] or 0) > QUEUE_STALL_SECONDS:
        alerts.append({"level": "warn", "code": "queue_stalled",
                       "text": f"worker 在线，但最长一条已排队 "
                               f"{health['oldest_queued_seconds']} 秒还没被领取。"
                               f"多半是上一条占着本地槽位。"})
    if health["expired_leases"] > 0:
        alerts.append({"level": "bad", "code": "lease_expired",
                       "text": f"{health['expired_leases']} 条作业的租约已过期但还挂着执行中；"
                               f"worker 下一轮恢复会把它们收掉。"})
    for running in health["running_jobs"]:
        if running["over_runtime"]:
            alerts.append({"level": "bad", "code": "run_timeout",
                           "text": f"作业 #{running['id']} 已跑 "
                                   f"{running['started_seconds']} 秒，超过上限 "
                                   f"{WORKER_MAX_RUNTIME_SECONDS} 秒。"})
        if running["silent_seconds"] is not None and running["silent_seconds"] > LOG_SILENCE_SECONDS:
            alerts.append({"level": "warn", "code": "log_silent",
                           "text": f"作业 #{running['id']} 已经 "
                                   f"{running['silent_seconds']} 秒没有新日志。"})
    if health["needs_attention"] > 0:
        alerts.append({"level": "warn", "code": "needs_attention",
                       "text": f"{health['needs_attention']} 条云端作业待人工核对；"
                               f"核对前不要重试，可能重复计费。"})

    # 并发设置与它的风险评估（0047）。读不到就报空，不影响其余状态——
    # 顶栏小圆点不该因为"这行配置读不出来"而整个 500。
    try:
        concurrency = concurrency_now()
    except (sqlite3.Error, OSError, ValueError) as exc:
        _audit_log.debug("worker_status_fallback error=%s", type(exc).__name__)
        concurrency = {}

    # **越界就进 alerts**，而不是只在设置卡里显示一句。
    # 理由：设置页平时没人打开，而内存溢出是"下一次领活就发生"的事。
    # 告警进的是每个登录者都会拉的顶栏接口，跑起来之前就能看到。
    if concurrency:
        assessment = concurrency.get("assessment") or {}
        if assessment.get("verdict") == "over":
            alerts.append({"level": "bad", "code": "concurrency_over_memory",
                           "text": f"并发上限可能撑破本机内存：{assessment.get('sentence')}。"
                                   f"调低：模型中心 · 运行监控 · 并发设置"})
        elif assessment.get("verdict") == "tight":
            alerts.append({"level": "warn", "code": "concurrency_tight_memory",
                           "text": f"并发上限余量偏薄：{assessment.get('sentence')}"})

    return {
        "online": online, "heartbeat_at": heartbeat, "age_seconds": age,
        "current_job_id": current_job_id,
        "workers": nodes,
        # 两个计数由这里数，不让界面自己 filter 一遍再算：
        # "在线几台、历史几台"是同一份 worker_node 上的事实，两处各算迟早分家。
        # `workers` 仍原样给全部行——面板默认只画在线的，历史靠这两个数说话，
        # 点开时用的还是这一份数据，不另开一条查询。
        "live_count": len(live),
        "history_count": len(nodes) - len(live),
        "queue": {"queued": health["queued"], "running": health["running"],
                  "by_resource": by_resource},
        "alerts": alerts, "by_status": health["by_status"],
        "oldest_queued_seconds": health["oldest_queued_seconds"],
        "running_jobs": health["running_jobs"],
        "expired_leases": health["expired_leases"],
        "needs_attention": health["needs_attention"],
        "concurrency": concurrency,
    }
