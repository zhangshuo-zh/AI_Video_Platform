"""智能体中心的读写（0041）。SQL 只在这一层（与其余仓储同一规矩）。

一条贯穿全文件的约束：**步骤行里不存任何能从 Job 现读的东西**。
状态、成本、日志、耗时、产物一律去 `job` 表读；这里只存"这一步关联哪条 Job、
卡在哪一步、失败时人看到的那句话"。所以智能体与 Job 不可能长出两套执行事实
（与 `repositories/pipeline.py` 的头一句同法，0026 已经验证过这条约束可行）。

三条纪律，写下来免得下一个改代码的人顺手破坏：

1. **运行状态不落库**。库里只有 `paused`／`cancelled` 两个外生意愿位，
   其余全部由 `agents.run_status()` 从步骤现算。v0.86 那两个真 bug
   都是"两套状态各自维护"造成的。
2. **审批结果不落库**。谁批的、批没批只在 `review_item`
   （`target_kind='agent_action'`）那一条，这里只有 `approval_expires_at`
   这样一个"有效期"位——它不是结论，是结论的到期日，写在 0041 的注释里。
3. **写方法不自己开事务**。调用方（`agents.py`）用 `db.transaction()`
   把"转态 → 建作业/写业务行 → 回写 → 记事件"包成一步；
   `get_conn()` 在事务里借用同一条连接、不各自提交（同 D2 的收口做法）。
"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn

# 步骤状态。`ready` 不在这里——它是"pending ＋ 前面都到了终态"的现算结果，
# 存下来就等于把流程位置写成了两份。
STEP_PENDING = "pending"
STEP_AWAITING = "awaiting_approval"
STEP_APPROVED = "approved"
STEP_RUNNING = "running"
STEP_SUCCEEDED = "succeeded"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_CANCELLED = "cancelled"
STEP_EXPIRED = "expired"

# 终态：进了这几个状态就不再被推进节拍动
STEP_FINAL = (STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED, STEP_CANCELLED, STEP_EXPIRED)
# 还没做完、但不需要人看的中间态
STEP_WORKING = (STEP_PENDING, STEP_APPROVED, STEP_RUNNING)

_STATUS_KEYS = (STEP_PENDING, STEP_AWAITING, STEP_APPROVED, STEP_RUNNING,
                STEP_SUCCEEDED, STEP_FAILED, STEP_SKIPPED, STEP_CANCELLED,
                STEP_EXPIRED)

# 事件类型定死在这一份，与 0041 的 CHECK 一一对应。
# 存在的理由：界面要按类型上色、`agents_browser_check.py` 要数条数，
# 两处各写一遍字符串就迟早对不上。
EVENT_TYPES = (
    "run_created", "plan_requested", "plan_ready", "plan_failed",
    "run_started", "run_paused", "run_resumed", "run_cancelled",
    "run_completed", "run_failed",
    "step_ready", "step_started", "step_succeeded", "step_failed",
    "step_skipped", "step_retried", "step_expired",
    "approval_requested", "approval_granted", "approval_rejected",
    "job_submitted", "preflight_warning", "tool_rejected",
)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class AgentError(ValueError):
    """这一层的业务拒绝。路由层统一转 422，把原话给人看。"""


class AgentRepository:
    # ---------- 运行：读 ----------
    def get(self, run_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM agent_run WHERE id = ?",
                                (run_id,)).fetchone()

    def list_by_project(self, project_id: int, limit: int = 50) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM agent_run WHERE project_id = ? "
                "ORDER BY id DESC LIMIT ?", (project_id, int(limit))).fetchall()

    def recent(self, project_ids: list[int], limit: int = 30) -> list[sqlite3.Row]:
        """首页/概览用的"我看得见的项目"最近运行。

        `project_ids` 由调用方按可见范围给（None 一律当空列表处理——
        看不见任何项目的人不该拿到全库列表，与 `access.visible_ids` 同一条口径）。
        """
        ids = [int(p) for p in (project_ids or [])]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with get_conn() as conn:
            return conn.execute(
                f"SELECT * FROM agent_run WHERE project_id IN ({marks}) "
                "ORDER BY id DESC LIMIT ?", (*ids, int(limit))).fetchall()

    def by_idempotency(self, key: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM agent_run WHERE idempotency_key = ?",
                                (key,)).fetchone()

    def active(self) -> list[sqlite3.Row]:
        """推进节拍每轮捞的"还没结束"的运行：没取消、没 finished_at。

        暂停的**仍然捞**——`tick()` 自己会跳过它并只把到期审批收成 `expired`，
        在 SQL 里过滤掉反而看不见审批过期这件事。
        """
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM agent_run WHERE cancelled = 0 AND finished_at IS NULL "
                "ORDER BY id").fetchall()

    # ---------- 运行：写 ----------
    def create(self, data: dict) -> sqlite3.Row:
        goal = (data.get("goal") or "").strip()
        if not goal:
            raise AgentError("目标不能空——一句话都不写，运行就只是"
                             "「让平台自己找点事做」")
        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO agent_run
                        (project_id, episode_id, goal, scene_key, plan_json,
                         model_snapshot_json, approval_valid_seconds,
                         paused, cancelled, idempotency_key, started_at, created_by)
                   VALUES (?, ?, ?, ?, '[]', ?, ?, 0, 0, ?, NULL, ?)""",
                (int(data["project_id"]), data.get("episode_id"), goal,
                 (data.get("scene_key") or "").strip(),
                 _dumps(data.get("model_snapshot") or {}),
                 int(data.get("approval_valid_seconds") or 86400),
                 data.get("idempotency_key"), data.get("created_by")),
            )
            return self._conn_get(conn, cursor.lastrowid)

    def set_plan_job(self, run_id: int, job_id: int) -> None:
        with get_conn() as conn:
            conn.execute("UPDATE agent_run SET plan_job_id = ?, "
                         "started_at = COALESCE(started_at, datetime('now','localtime')) "
                         "WHERE id = ?", (int(job_id), run_id))

    def set_plan(self, run_id: int, plan: list[dict],
                 model_snapshot: dict | None = None) -> None:
        """把算好的计划冻进运行。`model_snapshot` 为 None 时不动快照——
        快照在发起时就定了，计划回来只补步骤，不允许中途换轨（换了就等于
        人批的是另一档模型在跑）。

        参数顺序是 `(run_id, plan)`：调用方每次都按这个顺序写，
        反过来会把 id 当成计划、把 int(None) 抛成 TypeError（2026-11-06 真踩过）。
        """
        fields = {"plan_json": _dumps(plan)}
        if model_snapshot is not None:
            fields["model_snapshot_json"] = _dumps(model_snapshot)
        sets = ", ".join(f"{k} = ?" for k in fields)
        with get_conn() as conn:
            conn.execute(f"UPDATE agent_run SET {sets}, "
                         "updated_at = datetime('now','localtime') WHERE id = ?",
                         (*fields.values(), int(run_id)))

    def set_will(self, run_id: int, *, paused: bool | None = None,
                 cancelled: bool | None = None, reason: str = "") -> sqlite3.Row:
        """只动外生意愿两位。取消时顺手写 `finished_at`：
        人按了取消就该从"活跃"里退出去，不等 worker 下一轮。
        """
        row = self.get(run_id)
        if row is None:
            raise AgentError(f"没有这条运行：#{run_id}")
        fields: dict = {}
        if paused is not None:
            fields["paused"] = 1 if paused else 0
        if cancelled is not None:
            if cancelled and row["cancelled"]:
                return row                     # 重复取消：不覆盖第一次的原因
            fields["cancelled"] = 1 if cancelled else 0
            if cancelled:
                fields["cancel_reason"] = reason or "人已取消"
                fields["finished_at"] = _now()
        if not fields:
            return row
        sets = ", ".join(f"{k} = ?" for k in fields)
        with get_conn() as conn:
            conn.execute(f"UPDATE agent_run SET {sets}, "
                         "updated_at = datetime('now','localtime') WHERE id = ?",
                         (*fields.values(), int(run_id)))
            return self._conn_get(conn, run_id)

    def set_plan_status(self, run_id: int, status: str) -> None:
        """把计划的落定结果写成一列：'pending' / 'ready' / 'failed'。

        只由 `agents._ingest_plan()` 与它的两个失败分支调用——界面不写这一列。
        """
        if status not in ("pending", "ready", "failed"):
            raise AgentError(f"内部：不认识的计划状态 {status}")
        with get_conn() as conn:
            conn.execute("UPDATE agent_run SET plan_status = ?, "
                         "updated_at = datetime('now','localtime') WHERE id = ?",
                         (status, int(run_id)))

    def finish(self, run_id: int) -> None:
        """收口：只有真的到了终态才写 `finished_at`（调用方 `tick()` 负责判）。
        重复调用是空操作，所以 worker 重启后多收一次口不会留下第二个时间。
        """
        with get_conn() as conn:
            conn.execute("UPDATE agent_run SET finished_at = "
                         "COALESCE(finished_at, datetime('now','localtime')), "
                         "updated_at = datetime('now','localtime') WHERE id = ?",
                         (int(run_id),))

    # ---------- 步骤 ----------
    def add_step(self, run_id: int, seq: int, *, tool_code: str, args: dict,
                 args_hash: str, risk: str, status: str = STEP_PENDING,
                 approval_expires_at: str | None = None) -> sqlite3.Row:
        with get_conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM agent_step WHERE run_id = ? AND seq = ?",
                (int(run_id), int(seq))).fetchone()
            if exists is not None:
                # 幂等收在这里而不是收在调用方：worker 重启、计划重放、
                # 两条 worker 同时 tick 都会走到这一句。抛出去比插进第二行好——
                # 同一个运行的同一步有两行，"哪一行是真的"就没人答得出了
                return conn.execute("SELECT * FROM agent_step "
                                    "WHERE run_id = ? AND seq = ?",
                                    (int(run_id), int(seq))).fetchone()
            cursor = conn.execute(
                """INSERT INTO agent_step
                        (run_id, seq, tool_code, args_json, args_hash, risk, status,
                         approval_expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(run_id), int(seq), tool_code, _dumps(args), args_hash, risk,
                 status, approval_expires_at))
            return conn.execute("SELECT * FROM agent_step WHERE id = ?",
                                (cursor.lastrowid,)).fetchone()

    def step(self, step_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM agent_step WHERE id = ?",
                                (int(step_id),)).fetchone()

    def steps(self, run_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM agent_step WHERE run_id = ? "
                                "ORDER BY seq", (int(run_id),)).fetchall()

    def update_step(self, step_id: int, patch: dict) -> sqlite3.Row:
        """逐字段更新一步。列名白名单写死在这里：
        把外部传来的键直接拼进 SET 就是 SQL 注入的门，哪怕来源是 worker。
        """
        allowed = {"status", "job_id", "result_json", "error_code",
                   "error_message", "retry_count", "approval_expires_at"}
        unknown = set(patch) - allowed
        if unknown:
            raise AgentError(f"这些步骤字段不能改：{'、'.join(sorted(unknown))}")
        row = self.step(step_id)
        if row is None:
            raise AgentError(f"没有这一步：#{step_id}")
        fields: dict = {}
        for key in ("status", "error_code", "error_message", "approval_expires_at"):
            if key in patch:
                fields[key] = patch[key]
        if "job_id" in patch:
            fields["job_id"] = patch["job_id"]
        if "result_json" in patch:
            fields["result_json"] = patch["result_json"] if isinstance(
                patch["result_json"], str) else _dumps(patch["result_json"])
        if "retry_count" in patch:
            fields["retry_count"] = int(patch["retry_count"])
        if not fields:
            return row
        sets = ", ".join(f"{key} = ?" for key in fields)
        with get_conn() as conn:
            conn.execute(f"UPDATE agent_step SET {sets}, "
                         "updated_at = datetime('now','localtime') WHERE id = ?",
                         (*fields.values(), int(step_id)))
            return self.step(step_id)

    def cancel_open_steps(self, run_id: int) -> int:
        """取消运行时代动作废。
        还在等人看的那些（pending／awaiting_approval／approved）收成 cancelled；
        `running` 的**不动**——它关联的作业仍在队列里，由 worker 那侧的
        取消语义处理，这里替它写终态就是撒谎。
        """
        with get_conn() as conn:
            cursor = conn.execute(
                "UPDATE agent_step SET status = ?, "
                "updated_at = datetime('now','localtime') "
                "WHERE run_id = ? AND status IN (?,?,?)",
                (STEP_CANCELLED, int(run_id),
                 STEP_PENDING, STEP_AWAITING, STEP_APPROVED))
            return cursor.rowcount

    def awaiting_steps(self, project_ids: list[int],
                       limit: int = 50) -> list[sqlite3.Row]:
        """审批页那一列：看得见的项目里、正在等人的步骤。"""
        ids = [int(p) for p in (project_ids or [])]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with get_conn() as conn:
            return conn.execute(
                f"""SELECT s.* FROM agent_step s
                      JOIN agent_run r ON r.id = s.run_id
                     WHERE s.status = ? AND r.project_id IN ({marks}) AND r.cancelled = 0
                     ORDER BY s.id DESC LIMIT ?""",
                (STEP_AWAITING, *ids, int(limit))).fetchall()

    # ---------- 事件 ----------
    def add_event(self, event_type: str, *, run_id: int | None = None,
                  step_id: int | None = None, payload: dict | None = None,
                  actor_id: int | None = None, conn=None) -> None:
        if event_type not in EVENT_TYPES:
            # 白名单在仓储层也拦一遍：CHECK 会拦，但那时报的是
            # "constraint failed"，人看不出是自己写了个新事件名忘了登记
            raise AgentError(f"事件类型「{event_type}」没有登记在 EVENT_TYPES 里")
        if run_id is None and step_id is None:
            raise AgentError("事件必须至少属于一条运行或一个步骤，"
                             "否则它就成了没人能查到的孤行")
        sql = ("INSERT INTO agent_event (run_id, step_id, event_type, payload_json, "
               "actor_id) VALUES (?, ?, ?, ?, ?)")
        args = (run_id, step_id, event_type, _dumps(payload or {}), actor_id)
        if conn is not None:
            conn.execute(sql, args)      # 借用调用方的事务连接，不自己提交
            return
        with get_conn() as owned:
            owned.execute(sql, args)

    def events(self, run_id: int, limit: int = 200) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM agent_event WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (int(run_id), int(limit))).fetchall()

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        """概览页那一张"最近事件"。倒序取，最新在上——与产线事件时间线同一口径。

        可见性不在这里筛：这一张表只出现在「智能体中心」的概览里，
        而那一页要 `system:config`；将来若要放进首页，
        得先按项目裁（与 `/api/worker/status` 那一刀同法），别在这一句里偷懒。
        """
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM agent_event ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()

    # ---------- 内部 ----------
    def _conn_get(self, conn, run_id: int) -> sqlite3.Row:
        return conn.execute("SELECT * FROM agent_run WHERE id = ?",
                            (run_id,)).fetchone()


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def loads(raw, default):
    """JSON 列的读法与其余模块同一把（`model_center._loads` 那个思路）。
    坏 JSON 回默认值而不是抛——历史行里可能存过被截断的字符串，
    列表页不该因为一行读不出来就整页 500。
    """
    try:
        value = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


agent_runs = AgentRepository()
