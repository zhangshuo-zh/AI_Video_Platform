from __future__ import annotations

import json
import sqlite3

from config import WORKER_MAX_GLOBAL, WORKER_MAX_PER_RESOURCE
from db import get_conn

from .common import RESOURCE_CLASS_SQL, _loads


class JobsLeaseMixin:

    # ---------- 领取与执行 ----------
    def claim_next(self, worker_id: str, lease_seconds: int,
                   limits: dict | None = None) -> sqlite3.Row | None:
        """原子领取一条 queued。返回 None＝没有可领的，或并发槽位都占着。

        并发按**资源类**算，不按 worker 算：视频模型同时只跑一条（显存），
        图像／文本／音频／成片各有一条槽。默认全局 1、每类 1，与单 worker
        时代完全一致；要并行就在 `limits` 里抬上限，并且真的多开 worker
        进程——一个进程仍然只跑一条作业，这一点没有被"并发"改掉。

        `limits` = `{"global": 2, "per": {"video": 1, "image": 1, ...}}`。
        没给的类沿用配置默认值；0 表示这一类暂时不接活。
        """
        limits = limits or {}
        max_global = max(0, int(limits.get("global", WORKER_MAX_GLOBAL)))
        per = {**WORKER_MAX_PER_RESOURCE, **(limits.get("per") or {})}
        lease = f"+{int(lease_seconds)} seconds"
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 这里**不看租约是否过期**：租约过期只说明"上一个 worker 大概没了"，
            # 不等于这条作业已经收尾。先让 recover_stale 把它收成 failed(worker_lost)，
            # 再领下一条；否则会出现"过期但还挂着 running"的窗口里领进第二条同资源
            # 作业，同一时刻两个任务抢同一份显存——这条硬约束不能靠时间窗口赌。
            active_rows = conn.execute(
                f"""SELECT {RESOURCE_CLASS_SQL} AS cls, COUNT(*) AS n
                      FROM job
                     WHERE status IN ('starting','running','cancel_requested')
                     GROUP BY cls"""
            ).fetchall()
            active = {row["cls"]: int(row["n"]) for row in active_rows}
            if sum(active.values()) >= max_global:
                return None
            candidates = conn.execute(
                f"""SELECT id, {RESOURCE_CLASS_SQL} AS cls
                      FROM job
                     WHERE status = 'queued'
                     ORDER BY priority DESC, queued_at, id"""
            ).fetchall()
            row = next((r for r in candidates
                        if active.get(r["cls"], 0) < int(per.get(r["cls"], 1))),
                       None)
            if row is None:
                return None
            cur = conn.execute(
                """UPDATE job SET status = 'starting', worker_id = ?,
                          started_at = datetime('now','localtime'),
                          lease_until = datetime('now','localtime', ?),
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND status = 'queued'""",
                (worker_id, lease, row["id"]),
            )
            if cur.rowcount != 1:
                return None
            # 必须用同一个连接读回：另一个连接看不到这里还没提交的 UPDATE
            return conn.execute("SELECT * FROM job WHERE id = ?", (row["id"],)).fetchone()


    def renew(self, job_id: int, worker_id: str, lease_seconds: int) -> None:
        """续租 + 心跳。只续还在执行中的那条，结束了的不会复活。"""
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET lease_until = datetime('now','localtime', ?),
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND worker_id = ?
                      AND status IN ('starting','running','cancel_requested')""",
                (f"+{int(lease_seconds)} seconds", job_id, worker_id),
            )


    def set_running(self, job_id: int, worker_id: str) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET status = 'running', updated_at = datetime('now','localtime')
                    WHERE id = ? AND worker_id = ? AND status = 'starting'""",
                (job_id, worker_id),
            )


    def recover_stale(self) -> dict:
        """启动时收拾"上一个 worker 没来得及收尾"的作业。

        判据是租约过期，不是进程号：PID 会被复用，租约时间不会。

        云端那条多一个分支：**已经拿到供应商请求 ID 的作业不能直接算 failed**。
        供应商那边可能已经建了任务、甚至已经计了费；failed 的默认建议是重试，
        而那正好可能造成第二次调用。这里把它挂成 needs_attention，交给人核对。
        """
        with get_conn() as conn:
            attention = conn.execute(
                """UPDATE job SET status = 'needs_attention',
                          error_code = 'worker_lost',
                          error_message = 'worker 退出时这条云端作业还在执行，且供应商任务可能已建立；请先核对账单。',
                          attention_reason = 'worker 中断，云端调用结果未知',
                          finished_at = datetime('now','localtime'),
                          lease_until = NULL, worker_id = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE status IN ('starting','running')
                      AND execution_mode = 'cloud'
                      AND provider_request_id IS NOT NULL
                      AND (lease_until IS NULL
                           OR lease_until < datetime('now','localtime'))"""
            ).rowcount
            failed = conn.execute(
                """UPDATE job SET status = 'failed', error_code = 'worker_lost',
                          error_message = 'worker 退出时这条作业还在执行；可以重试。',
                          finished_at = datetime('now','localtime'),
                          lease_until = NULL, worker_id = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE status IN ('starting','running')
                      AND (lease_until IS NULL
                           OR lease_until < datetime('now','localtime'))"""
            ).rowcount
            cancelled = conn.execute(
                """UPDATE job SET status = 'cancelled', error_code = 'cancelled',
                          error_message = '取消请求时 worker 已退出',
                          finished_at = datetime('now','localtime'),
                          lease_until = NULL, worker_id = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE status = 'cancel_requested'
                      AND (lease_until IS NULL
                           OR lease_until < datetime('now','localtime'))"""
            ).rowcount
        return {"failed": failed, "cancelled": cancelled, "needs_attention": attention}


    def heartbeat(self, worker_id: str, pid: int, current_job_id: int | None,
                  version: str, *, limits: dict | None = None,
                  resources: dict | None = None) -> None:
        """写一份心跳：worker_node 记这个进程，worker_state 保留"最近一次"镜像。

        `limits`/`resources` 只在启动时或作业收尾时给；中间续租传 None 时
        保留上一份（COALESCE），别让槽位说明被空值擦掉。
        """
        limits_json = json.dumps(limits, ensure_ascii=False, sort_keys=True) if limits is not None else None
        resources_json = json.dumps(resources, ensure_ascii=False, sort_keys=True) if resources is not None else None
        host = (worker_id or "").split(":", 1)[0]
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO worker_node
                       (worker_id, pid, host, started_at, heartbeat_at,
                        current_job_id, version, limits_json, resources_json)
                   VALUES (?, ?, ?, datetime('now','localtime'),
                           datetime('now','localtime'), ?, ?, COALESCE(?, '{}'),
                           COALESCE(?, '{}'))
                   ON CONFLICT(worker_id) DO UPDATE SET
                       pid = excluded.pid,
                       heartbeat_at = excluded.heartbeat_at,
                       current_job_id = excluded.current_job_id,
                       version = excluded.version,
                       limits_json = COALESCE(excluded.limits_json,
                                              worker_node.limits_json),
                       resources_json = COALESCE(excluded.resources_json,
                                                 worker_node.resources_json)""",
                (worker_id, pid, host, current_job_id, version,
                 limits_json, resources_json),
            )
            # 面板只关心近几天的进程；删除久未心跳的行，不让表无限长
            conn.execute(
                """DELETE FROM worker_node
                    WHERE heartbeat_at < datetime('now','localtime','-7 days')"""
            )
            conn.execute(
                """INSERT INTO worker_state (id, worker_id, pid, started_at,
                                             heartbeat_at, current_job_id, version)
                   VALUES (1, ?, ?, datetime('now','localtime'),
                           datetime('now','localtime'), ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       worker_id = excluded.worker_id,
                       pid = excluded.pid,
                       started_at = CASE WHEN worker_state.worker_id = excluded.worker_id
                                         THEN worker_state.started_at
                                         ELSE excluded.started_at END,
                       heartbeat_at = excluded.heartbeat_at,
                       current_job_id = excluded.current_job_id,
                       version = excluded.version""",
                (worker_id, pid, current_job_id, version),
            )


    def workers(self, online_seconds: int) -> list[dict]:
        """所有登记过的 worker 进程（最近的在前），带"现在还活着吗"。"""
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT worker_id, pid, host, started_at, heartbeat_at,
                          current_job_id, version, limits_json, resources_json,
                          CAST(strftime('%s','now','localtime')
                               - strftime('%s', heartbeat_at) AS INTEGER)
                              AS age_seconds
                     FROM worker_node
                    ORDER BY heartbeat_at DESC, id DESC"""
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            age = item.get("age_seconds")
            item["online"] = age is not None and 0 <= age <= online_seconds
            item["limits"] = _loads(item.pop("limits_json", "") or "{}")
            item["resources"] = _loads(item.pop("resources_json", "") or "{}")
            out.append(item)
        return out


    def worker_state(self) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM worker_state WHERE id = 1").fetchone()
