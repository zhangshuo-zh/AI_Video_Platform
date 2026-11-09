"""worker 掉线的告警：找出被影响的项目、通知对的人、恢复后结清（0049）。

这一层要解决的问题是需求原话那句：「如果 worker 停止、掉线影响任务运行，
被影响的项目所属的用户和协作用户可以收到消息提醒，点击跳转至相关页面处理」。

拆开是三件事，每件都有一个不能想当然的地方：

**1. 「被影响」怎么判**
不是"所有项目"，也不是"所有有作业的项目"——是**还有活没干完**的项目。
判据只看排队里那批：`status = 'queued'` 的作业所属项目。
跑完的、失败的都不算（那些用户已经知道了，再提醒就是噪音）。

**2. 通知谁**
`audience='project:<id>'`，由 `notifications.inbox()` 在查询期解析成
"当前项目所有者 + 协作者"。**不在发的时候展开成员名单**——成员会变，
而且退出的成员不该留着副本。

**3. 恢复后要结清**
worker 回来之后，那条"掉线"通知就过时了。不结清的话：
`dedupe_key` 会把下次真的掉线挡住（见 `notifications.emit` 的去重），
于是真出事时反而没提醒——比重复提醒糟得多。
"""
from __future__ import annotations

from db import get_conn

# 与 `worker_supervisor` 的判活同一套口径，但这里只看"有没有人在跑"，
# 不重复探 pid——调用方已经探过了。
DEDUPE_PREFIX = "worker_offline"


def queued_by_project() -> dict[int, int]:
    """还排着的作业按项目分组：`{project_id: 条数}`。

    只数 `queued`：`starting/running` 说明 worker 正好在跑它，
    这时候掉线确实是"被打断"，但那条由 `recover_stale` 收成 failed，
    走的是"作业失败"那条通知，不是"队列停了"这条。
    """
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT project_id, COUNT(*) AS n
                     FROM job
                    WHERE status = 'queued' AND project_id IS NOT NULL
                    GROUP BY project_id""").fetchall()
        return {int(r["project_id"]): int(r["n"]) for r in rows}
    except Exception:
        return {}


def notify_offline(*, online: int, queued_total: int) -> list[int]:
    """worker 掉线时通知受影响的项目。返回真的发出去了的几个 project_id。

    `online == 0 and queued_total > 0` 才发——有 worker 在跑就不算"队列停了"，
    队列空则没人被影响（不必打扰）。这两条前置判断是"不制造噪音"的关键。
    """
    if online > 0 or queued_total <= 0:
        return []
    import notifications

    sent: list[int] = []
    for project_id, count in queued_by_project().items():
        nid = notifications.emit(
            kind="worker_offline",
            level="bad",
            title=f"worker 掉线：{count} 条作业在排队等它",
            body=(f"这个项目还有 {count} 条作业没被领取。"
                  f"队列不会自己前进，需要有权限的人把 worker 起来。"),
            audience=f"project:{project_id}",
            # 同项目同一轮掉线只留一条；`resolve_offline` 在恢复时清掉，
            # 这样"再掉一次"还能发出来。
            dedupe_key=f"{DEDUPE_PREFIX}:{project_id}",
            link_page="models",
            link_params={"sub": "运行监控"},
        )
        if nid:
            sent.append(project_id)
    return sent


def resolve_offline() -> int:
    """worker 回来了：把还没结的掉线通知全清掉。返回清了几个项目。

    **这一步必须做**（本模块头第 3 条）：不清的话 `dedupe_key` 会把
    下一次真掉线挡在门外，那是"该响的时候不响"。
    """
    import notifications

    # **先看有没有，再决定删不删**（2026-11-10 自查）。
    # 原来这段无条件跑一条 `DELETE FROM notification WHERE kind='worker_offline'`，
    # 而它挂在守护线程的 `_notify_state` 里、每 15 秒执行一次——worker 正常在跑时
    # 这条 DELETE 永远是 0 行，等于每 15 秒拿一次写锁空转。
    # `heartbeat` 里那条 prune 也是为同样的事被优化过（见 lease.py 的注释），
    # 这里犯的是同一个错。先 SELECT 一次（读不挡写），没有就直接返回。
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM notification WHERE kind = 'worker_offline'"
            ).fetchone()
        if not row or not row["n"]:
            return 0
    except Exception:
        return 0

    cleared = 0
    for project_id in list(queued_by_project()):
        cleared += notifications.resolve(
            kind="worker_offline",
            audience=f"project:{project_id}",
            dedupe_key=f"{DEDUPE_PREFIX}:{project_id}")
    # 项目后来没活了也别留着旧通知：把库里所有未结的掉线条目一并清掉。
    # 走到这里说明确实有条目（上面 COUNT 过），所以这条 DELETE 不是空转。
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM notification WHERE kind = 'worker_offline'")
            cleared = max(cleared, cur.rowcount)
    except Exception:
        pass
    return cleared
