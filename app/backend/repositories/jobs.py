"""Job 队列的读写。队列只由这张表驱动，没有第二个物理队列。

并发要点：领取必须在一个 `BEGIN IMMEDIATE` 里做"查 + 改"，否则 API 刚插进去
两条、两个 worker 同时 SELECT 到同一条，就会重复执行。SQLite 的写锁很短，
这里也不在事务里等模型进程——领到就提交，执行在事务外。
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from db import get_conn

# 字段清单集中在这里：建单、重试复制、测试断言都用同一份，免得漏一列
COPY_FIELDS = (
    "project_id", "episode_id", "shot_id", "capability", "kind", "execution_mode",
    "provider_code", "model_code", "adapter_version", "prompt_snapshot",
    "params_json", "input_refs_json", "priority", "user_confirmed_cloud",
)


class JobRepository:
    def create(self, data: dict) -> sqlite3.Row:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO job
                   (project_id, episode_id, shot_id, capability, kind, execution_mode,
                    provider_code, model_code, adapter_version, prompt_snapshot,
                    params_json, input_refs_json, status, priority, idempotency_key,
                    user_confirmed_cloud, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?)""",
                (data["project_id"], data.get("episode_id"), data.get("shot_id"),
                 data["capability"], data["kind"], data["execution_mode"],
                 data["provider_code"], data["model_code"],
                 data.get("adapter_version", ""), data.get("prompt_snapshot", ""),
                 json.dumps(data.get("params", {}), ensure_ascii=False, sort_keys=True),
                 json.dumps(data.get("input_refs", []), ensure_ascii=False),
                 int(data.get("priority", 0)), data.get("idempotency_key"),
                 int(bool(data.get("user_confirmed_cloud"))), data.get("created_by")),
            )
            return conn.execute("SELECT * FROM job WHERE id = ?",
                                (cur.lastrowid,)).fetchone()

    def get(self, job_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()

    def by_key(self, key: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM job WHERE idempotency_key = ?",
                                (key,)).fetchone()

    def list(self, project_ids: list[int] | None, status: str | None = None,
             capability: str | None = None, limit: int = 100) -> list[sqlite3.Row]:
        """project_ids=None 表示管理员看全部；空列表表示一个都看不见。"""
        if project_ids is not None and not project_ids:
            return []
        conds: list[str] = []
        args: list = []
        if project_ids is not None:
            marks = ",".join("?" for _ in project_ids)
            conds.append(f"project_id IN ({marks})")
            args += list(project_ids)
        if status:
            conds.append("status = ?")
            args.append(status)
        if capability:
            conds.append("capability = ?")
            args.append(capability)
        args.append(min(max(limit, 1), 200))
        with get_conn() as conn:
            return conn.execute(
                f"""SELECT * FROM job WHERE {' AND '.join(conds)}
                     ORDER BY id DESC LIMIT ?""", args).fetchall()

    # ---------- 领取与执行 ----------
    def claim_next(self, worker_id: str, lease_seconds: int) -> sqlite3.Row | None:
        """原子领取一条 queued。返回 None＝没有可领的，或已有本地作业在跑。

        "已有本地作业在跑"也在这里挡：单 worker 是常态，但多开一个 worker
        时不能让两个本地任务同时抢模型内存（规格 01-4.4 的硬要求）。
        """
        lease = f"+{int(lease_seconds)} seconds"
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                """SELECT 1 FROM job
                    WHERE status IN ('starting','running')
                      AND lease_until > datetime('now','localtime') LIMIT 1"""
            ).fetchone()
            if active is not None:
                return None
            row = conn.execute(
                """SELECT id FROM job WHERE status = 'queued'
                    ORDER BY priority DESC, queued_at, id LIMIT 1"""
            ).fetchone()
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

    def succeed(self, job_id: int, worker_id: str, log_path: str,
                result_paths: list[str], elapsed_ms: int, return_code: int = 0) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET status = 'succeeded', finished_at = datetime('now','localtime'),
                          elapsed_ms = ?, return_code = ?, log_path = ?,
                          result_paths_json = ?, error_code = NULL, error_message = NULL,
                          lease_until = NULL, worker_id = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND worker_id = ?""",
                (elapsed_ms, return_code, log_path,
                 json.dumps(result_paths, ensure_ascii=False), job_id, worker_id),
            )

    def fail(self, job_id: int, worker_id: str, error_code: str, message: str,
             log_path: str = "", return_code: int | None = None,
             elapsed_ms: int | None = None) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET status = 'failed', finished_at = datetime('now','localtime'),
                          error_code = ?, error_message = ?, log_path = ?,
                          return_code = ?, elapsed_ms = ?,
                          lease_until = NULL, worker_id = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND worker_id = ?""",
                (error_code, message[:2000], log_path, return_code, elapsed_ms,
                 job_id, worker_id),
            )

    def cancelled(self, job_id: int, worker_id: str | None, message: str = "",
                  log_path: str = "", elapsed_ms: int | None = None) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET status = 'cancelled', finished_at = datetime('now','localtime'),
                          error_code = 'cancelled', error_message = ?, log_path = ?,
                          elapsed_ms = ?, lease_until = NULL, worker_id = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""",
                (message[:2000], log_path, elapsed_ms, job_id),
            )

    def request_cancel(self, job_id: int) -> sqlite3.Row | None:
        """取消是"请求式"：排队中的当场取消，跑着的只记下意愿。"""
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "queued":
                conn.execute(
                    """UPDATE job SET status = 'cancelled',
                              finished_at = datetime('now','localtime'),
                              error_code = 'cancelled', error_message = '提交后取消，未执行',
                              updated_at = datetime('now','localtime') WHERE id = ?""",
                    (job_id,),
                )
            elif row["status"] in ("starting", "running"):
                conn.execute(
                    """UPDATE job SET status = 'cancel_requested',
                              updated_at = datetime('now','localtime') WHERE id = ?""",
                    (job_id,),
                )
            return conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()

    def retry(self, original: sqlite3.Row, created_by: int | None) -> sqlite3.Row:
        """重试＝新建一行，原行一个字段都不动（日志、成本、错误全保留）。"""
        key = "retry-" + uuid.uuid4().hex
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO job
                   (project_id, episode_id, shot_id, capability, kind, execution_mode,
                    provider_code, model_code, adapter_version, prompt_snapshot,
                    params_json, input_refs_json, status, priority, idempotency_key,
                    retry_of_job_id, user_confirmed_cloud, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?)""",
                (original["project_id"], original["episode_id"], original["shot_id"],
                 original["capability"], original["kind"], original["execution_mode"],
                 original["provider_code"], original["model_code"],
                 original["adapter_version"], original["prompt_snapshot"],
                 original["params_json"], original["input_refs_json"],
                 original["priority"], key, original["id"],
                 original["user_confirmed_cloud"], created_by),
            )
            return conn.execute("SELECT * FROM job WHERE id = ?",
                                (cur.lastrowid,)).fetchone()

    def recover_stale(self) -> dict:
        """启动时收拾"上一个 worker 没来得及收尾"的作业。

        判据是租约过期，不是进程号：PID 会被复用，租约时间不会。
        """
        with get_conn() as conn:
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
        return {"failed": failed, "cancelled": cancelled}

    def ids_for_project(self, project_id: int) -> list[int]:
        with get_conn() as conn:
            return [r["id"] for r in conn.execute(
                "SELECT id FROM job WHERE project_id = ?", (project_id,))]

    def status_of(self, job_id: int) -> str | None:
        with get_conn() as conn:
            row = conn.execute("SELECT status FROM job WHERE id = ?",
                               (job_id,)).fetchone()
        return row["status"] if row else None

    # ---------- worker 心跳 ----------
    def heartbeat(self, worker_id: str, pid: int, current_job_id: int | None,
                  version: str) -> None:
        with get_conn() as conn:
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

    def worker_state(self) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM worker_state WHERE id = 1").fetchone()

    def counts(self) -> dict:
        with get_conn() as conn:
            row = conn.execute(
                """SELECT
                     SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
                     SUM(CASE WHEN status IN ('starting','running','cancel_requested')
                              THEN 1 ELSE 0 END) AS running
                   FROM job"""
            ).fetchone()
        return {"queued": int(row["queued"] or 0), "running": int(row["running"] or 0)}


jobs = JobRepository()
