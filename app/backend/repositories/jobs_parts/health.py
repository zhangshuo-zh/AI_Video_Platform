from __future__ import annotations

import jobfiles

from datetime import datetime
from db import get_conn


class JobsHealthMixin:

    def queue_health(self, max_runtime_seconds: int) -> dict:
        """队列健康：把"没人干活"和"刚入队 0.5 秒"分开。

        这些数字只用于提示，**不改任何 Job 状态**——状态真相仍然只在 `status` 一列。
        运行超时只报"超过上限还在跑"，杀进程是 worker 的事（见 `worker.py` 的 deadline）。
        """
        with get_conn() as conn:
            by_status = {r["status"]: int(r["n"]) for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM job GROUP BY status")}
            oldest = conn.execute(
                """SELECT MIN(queued_at) AS at FROM job WHERE status = 'queued'"""
            ).fetchone()["at"]
            # 把 `log_path` 也取出来：`silent_seconds` 必须按**日志文件的 mtime**
            # 算，不能按 `updated_at`（见下面 running_rows 里的注释）。
            running = conn.execute(
                """SELECT id, status, project_id, started_at, updated_at, lease_until,
                          capability, provider_code, model_code, execution_mode, log_path
                     FROM job
                    WHERE status IN ('starting','running','cancel_requested')
                    ORDER BY id"""
            ).fetchall()
            # 「待核对作业」只认 status 一列。云端作业建单就是
            # pending_reconciliation、终态也不改它，旧口径把这一档 OR 进来，
            # 结果是每条云端成功作业都让面板 +1，人点进去又被 reconcile 的
            # status 闸挡回 409——数字和能做的事对不上。
            attention = conn.execute(
                """SELECT COUNT(*) AS n FROM job
                    WHERE status = 'needs_attention'"""
            ).fetchone()["n"]
            # 账单没核清是另一件事，单独给一个读数，界面文案要说明是账单。
            unreconciled = conn.execute(
                """SELECT COUNT(*) AS n FROM job
                    WHERE execution_mode = 'cloud'
                      AND billable_status = 'pending_reconciliation'
                      AND status IN ('succeeded','failed','cancelled')"""
            ).fetchone()["n"]
            expired = conn.execute(
                """SELECT COUNT(*) AS n FROM job
                    WHERE status IN ('starting','running','cancel_requested')
                      AND (lease_until IS NULL
                           OR lease_until < datetime('now','localtime'))"""
            ).fetchone()["n"]
        now = datetime.now()

        def age(text: str | None) -> int | None:
            if not text:
                return None
            try:
                return max(0, int((now - datetime.strptime(text, "%Y-%m-%d %H:%M:%S"))
                                  .total_seconds()))
            except ValueError:
                return None

        running_rows = []
        for r in running:
            started = age(r["started_at"])
            running_rows.append({
                # project_id 是给 worker_status 做可见性过滤用的；log_path 不再往外给：
                # 前端零引用，而它会把服务器目录结构摆进任何登录者的响应里
                "id": r["id"], "status": r["status"], "project_id": r["project_id"],
                "capability": r["capability"], "provider": r["provider_code"],
                "model": r["model_code"], "execution_mode": r["execution_mode"],
                "started_seconds": started,
                # **静默秒数按日志文件的 mtime 算，不是 `updated_at`**（2026-11-08）。
                # `renew()` 每 0.2 秒就把 `updated_at` 刷成 now，所以只要 worker
                # 活着、`_pump` 还在转，`updated_at` 永远是"刚刚"——拿它判静默，
                # `log_silent` 告警永远不会触发，而 config 里那句话承诺的正是
                # "日志多久没有字节增长就提示可能卡住"（模型加载权重卡死不写日志
                # 是这台机器上真实发生过的形状）。
                # 日志还没建出来（领取后极早期）时退回 `updated_at`：
                # 那一刻"多久没动静"确实只能看行更新时间。
                "silent_seconds": (jobfiles.stat_log(r["log_path"])["silent_seconds"]
                                   if r["log_path"] else age(r["updated_at"])),
                "lease_expired": (r["lease_until"] is None
                                  or age(r["lease_until"]) is None
                                  or r["lease_until"] < now.strftime("%Y-%m-%d %H:%M:%S")),
                "over_runtime": started is not None and started > max_runtime_seconds,
            })
        return {
            "by_status": by_status,
            "queued": by_status.get("queued", 0),
            "running": sum(by_status.get(s, 0) for s in
                           ("starting", "running", "cancel_requested")),
            "oldest_queued_seconds": age(oldest),
            "running_jobs": running_rows,
            "expired_leases": int(expired or 0),
            "needs_attention": int(attention or 0),
            "unreconciled_cloud": int(unreconciled or 0),
        }
