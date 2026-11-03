"""产线中心的读写。SQL 只在这一层（与其余 12 个仓储同一规矩）。

一条贯穿全文件的约束：**节点行里不存任何能从 Job 现读的东西**。
状态、成本、日志、耗时、产物一律去 `job` 表读；这里只存"这个节点关联哪条 Job、
流程走到哪一步、失败时人看到的那句话"。所以产线与 Job 不可能出现两套执行事实。

写方法都不自己开事务边界：调用方（`pipeline.py`）用 `db.transaction()` 把
"转态 → 建 Job → 回写 → 记事件"包成一个原子步骤，`get_conn()` 在事务里
借用同一条连接、不各自提交。
"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn

# 节点状态。`blocked`／`ready` 不在这里——它们是"pending ＋ 依赖满不满足"的现算结果
NODE_PENDING = "pending"
NODE_RUNNING = "running"
NODE_REVIEW = "waiting_review"
NODE_ATTENTION = "attention"
NODE_DONE = "completed"
NODE_FAILED = "failed"
NODE_SKIPPED = "skipped"
NODE_CANCELLED = "cancelled"

# 终态：进了这几个状态就不再被调度
NODE_FINAL = (NODE_DONE, NODE_FAILED, NODE_SKIPPED, NODE_CANCELLED)

# 一份"每个状态都数过、当前都是零"的计数。列顺序固定，
# 因为 `run_status`／进度条都按下标取值，缺一个键就是 KeyError。
_NODE_STATUS_KEYS = (NODE_PENDING, NODE_RUNNING, NODE_REVIEW, NODE_ATTENTION,
                     NODE_DONE, NODE_FAILED, NODE_SKIPPED, NODE_CANCELLED)


def _zero_counts() -> dict:
    return {key: 0 for key in _NODE_STATUS_KEYS}


class PipelineRepository:
    # ---------- 模板 ----------
    def ensure_template(self, *, name: str, version: int, description: str,
                        definition: dict, builtin: bool = True) -> sqlite3.Row:
        """内置模板落库。已存在就原样返回——启动多少次都不会改历史版本。"""
        with get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO pipeline_template
                   (name, version, description, definition_json, enabled, builtin)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                (name, version, description,
                 json.dumps(definition, ensure_ascii=False, sort_keys=True),
                 int(builtin)))
            return conn.execute(
                "SELECT * FROM pipeline_template WHERE name = ? AND version = ?",
                (name, version)).fetchone()

    def get_template(self, template_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM pipeline_template WHERE id = ?",
                                (template_id,)).fetchone()

    def list_templates(self, enabled_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM pipeline_template"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY name, version DESC"
        with get_conn() as conn:
            return conn.execute(sql).fetchall()

    def latest_templates(self) -> list[sqlite3.Row]:
        """每个名字只出最新一版：界面默认选当前口径，旧版本仍可按 id 取到。

        旧版本不删——已经跑过的运行冻结在那一版；但"新发起"不该在四张卡里
        猜哪个是现在的标准流程。
        """
        with get_conn() as conn:
            return conn.execute(
                """SELECT t.* FROM pipeline_template t
                    JOIN (SELECT name, MAX(version) AS v
                            FROM pipeline_template WHERE enabled = 1
                           GROUP BY name) m
                      ON m.name = t.name AND m.v = t.version
                    ORDER BY t.name""").fetchall()

    # ---------- 运行 ----------
    def create_run(self, *, project_id: int, episode_id: int, template_id: int,
                   template_version: int, input_snapshot: dict,
                   idempotency_key: str | None, created_by: int | None,
                   started_at: str) -> sqlite3.Row:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO pipeline_run
                   (project_id, episode_id, template_id, template_version,
                    input_snapshot_json, idempotency_key, started_at, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, episode_id, template_id, template_version,
                 json.dumps(input_snapshot, ensure_ascii=False, sort_keys=True),
                 idempotency_key, started_at, created_by))
            return conn.execute("SELECT * FROM pipeline_run WHERE id = ?",
                                (cur.lastrowid,)).fetchone()

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                """SELECT r.*, t.name AS template_name, t.builtin AS template_builtin,
                          p.name AS project_name, e.title AS episode_title, e.seq AS episode_seq
                     FROM pipeline_run r
                     JOIN pipeline_template t ON t.id = r.template_id
                     JOIN project p ON p.id = r.project_id
                     JOIN episode e ON e.id = r.episode_id
                    WHERE r.id = ?""", (run_id,)).fetchone()

    def by_key(self, key: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM pipeline_run WHERE idempotency_key = ?", (key,)).fetchone()

    def unfinished_for_episode(self, project_id: int,
                               episode_id: int) -> sqlite3.Row | None:
        """同一集上还没结束的运行。连点两次"开始生产"要靠它挡住第二条。"""
        with get_conn() as conn:
            return conn.execute(
                """SELECT * FROM pipeline_run
                    WHERE project_id = ? AND episode_id = ?
                      AND cancelled = 0 AND finished_at IS NULL
                    ORDER BY id DESC LIMIT 1""", (project_id, episode_id)).fetchone()

    def list_by_project(self, project_id: int, limit: int = 100) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                """SELECT r.*, t.name AS template_name,
                          e.title AS episode_title, e.seq AS episode_seq
                     FROM pipeline_run r
                     JOIN pipeline_template t ON t.id = r.template_id
                     JOIN episode e ON e.id = r.episode_id
                    WHERE r.project_id = ?
                    ORDER BY r.id DESC LIMIT ?""",
                (project_id, min(max(limit, 1), 200))).fetchall()

    def tickable_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        """worker 每轮要推进的运行：没取消、没暂停、还没结束。"""
        with get_conn() as conn:
            return conn.execute(
                """SELECT * FROM pipeline_run
                    WHERE cancelled = 0 AND paused = 0 AND finished_at IS NULL
                    ORDER BY id LIMIT ?""", (limit,)).fetchall()

    def set_paused(self, run_id: int, paused: bool) -> sqlite3.Row | None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE pipeline_run SET paused = ?,
                          updated_at = datetime('now','localtime') WHERE id = ?""",
                (int(paused), run_id))
        return self.get_run(run_id)

    def set_cancelled(self, run_id: int, reason: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE pipeline_run SET cancelled = 1, cancel_reason = ?,
                          paused = 0, finished_at = datetime('now','localtime'),
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND cancelled = 0""", (reason[:500], run_id))
        return self.get_run(run_id)

    def finish(self, run_id: int) -> sqlite3.Row | None:
        """全部节点进终态时收口。只在还没收尾时写，重复调用是空操作。"""
        with get_conn() as conn:
            conn.execute(
                """UPDATE pipeline_run SET finished_at = datetime('now','localtime'),
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND finished_at IS NULL""", (run_id,))
        return self.get_run(run_id)

    def reopen(self, run_id: int) -> None:
        """重试一格时把已收尾的运行重新打开。

        运行一旦写了 `finished_at`，`advance_run` 就只对齐真相、不再派活；
        而重试的意义正是"再往下走一格"。不重开的话，重试成功之后下游永远停在
        "等待前置"——阶段 C 验收里重试一条已失败的运行正好撞在这上面。
        """
        with get_conn() as conn:
            conn.execute(
                """UPDATE pipeline_run SET finished_at = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""", (run_id,))

    def touch(self, run_id: int) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE pipeline_run SET updated_at = datetime('now','localtime')
                    WHERE id = ?""", (run_id,))

    # ---------- 节点 ----------
    def create_nodes(self, run_id: int, nodes: list[dict]) -> None:
        """一次运行的节点整批建。`(run_id, node_key)` 唯一，重复展开会直接报错。"""
        with get_conn() as conn:
            conn.executemany(
                """INSERT INTO pipeline_node_run
                   (run_id, node_key, node_type, status, depends_on_json, label,
                    input_json)
                   VALUES (?, ?, ?, 'pending', ?, ?, ?)""",
                [(run_id, n["node_key"], n["node_type"],
                  json.dumps(n["depends_on"], ensure_ascii=False),
                  n.get("label", ""),
                  json.dumps(n.get("input", {}), ensure_ascii=False, sort_keys=True))
                 for n in nodes])

    def nodes_for_run(self, run_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                """SELECT * FROM pipeline_node_run WHERE run_id = ? ORDER BY id""",
                (run_id,)).fetchall()

    def get_node(self, node_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM pipeline_node_run WHERE id = ?", (node_id,)).fetchone()

    def node_by_key(self, run_id: int, node_key: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                """SELECT * FROM pipeline_node_run WHERE run_id = ? AND node_key = ?""",
                (run_id, node_key)).fetchone()

    def try_begin(self, node_id: int) -> bool:
        """把 pending 原子地转成 running。返回 False＝别人已经抢走了。

        这一句是防重复调度的最后一道：两个进程同时推进同一条运行时，
        `rowcount` 只会给其中一个 1。**调度正确性靠它，不靠"只有一个 worker"这个假设**。
        """
        with get_conn() as conn:
            cur = conn.execute(
                """UPDATE pipeline_node_run
                      SET status = 'running', started_at = datetime('now','localtime')
                    WHERE id = ? AND status = 'pending'""", (node_id,))
            return cur.rowcount == 1

    def finish_node(self, node_id: int, status: str, *, expect: str = NODE_RUNNING,
                    output: dict | None = None, error_code: str = "",
                    error_message: str = "", job_id: int | None = None,
                    review_id: int | None = None) -> bool:
        """按期望状态收尾一个节点。期望不匹配就不动（返回 False），绝不覆盖别人的结论。"""
        sets = ["status = ?", "finished_at = datetime('now','localtime')"]
        args: list = [status]
        if output is not None:
            sets.append("output_json = ?")
            args.append(json.dumps(output, ensure_ascii=False, sort_keys=True))
        if error_code or error_message:
            sets += ["error_code = ?", "error_message = ?"]
            args += [error_code, error_message[:1000]]
        else:
            sets += ["error_code = ''", "error_message = ''"]
        if job_id is not None:
            sets.append("job_id = ?")
            args.append(job_id)
        if review_id is not None:
            sets.append("review_id = ?")
            args.append(review_id)
        args += [node_id, expect]
        with get_conn() as conn:
            cur = conn.execute(
                f"UPDATE pipeline_node_run SET {', '.join(sets)} WHERE id = ? AND status = ?",
                args)
            return cur.rowcount == 1

    def attach_job(self, node_id: int, job_id: int) -> None:
        """节点与 Job 的关联。与 `try_begin` 在同一个事务里，
        所以不存在"Job 建好了、节点不知道"的中间态。"""
        with get_conn() as conn:
            conn.execute("UPDATE pipeline_node_run SET job_id = ? WHERE id = ?",
                         (job_id, node_id))

    def reset_for_retry(self, node_id: int, *, job_id: int | None = None,
                        expect: str = NODE_FAILED,
                        status: str = NODE_PENDING) -> bool:
        """重试：清掉失败原因、次数加一。旧 Job 与旧产物都不动。

        `status` 分两种，这是"一格只对应一条作业"的关键：
          * 内部检查节点回 `pending`——下一轮按当前事实重算；
          * 作业节点直接跟着 `jobs.retry()` 新建的那条作业进 `running`。
            那条作业已经排进队列了；要是这里也回 pending，调度器会再提交一条，
            同一格就有了两条作业（其中一条谁也不认）。
        """
        with get_conn() as conn:
            cur = conn.execute(
                """UPDATE pipeline_node_run
                      SET status = ?, retry_count = retry_count + 1,
                          error_code = '', error_message = '',
                          started_at = CASE WHEN ? = 'running'
                                            THEN datetime('now','localtime')
                                            ELSE NULL END,
                          finished_at = NULL,
                          job_id = COALESCE(?, job_id)
                    WHERE id = ? AND status = ?""",
                (status, status, job_id, node_id, expect))
            return cur.rowcount == 1

    def counts_for_run(self, run_id: int) -> dict:
        return self.counts_for_runs([run_id]).get(int(run_id), _zero_counts())

    def counts_for_runs(self, run_ids: list[int] | set[int]) -> dict[int, dict]:
        """一次把一组运行的节点状态计数全带回来，键是 run_id。

        列表页原来逐条 `counts_for_run()`：N 条运行就是 N 趟 GROUP BY。
        这里一条 `run_id IN (...) GROUP BY run_id, status` 打完，
        没出现的运行补一份全零——调用方要的是"每条都有 counts"，
        而不是"有节点才有"。
        """
        ids = sorted({int(i) for i in run_ids})
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT run_id, status, COUNT(*) AS n FROM pipeline_node_run
                     WHERE run_id IN ({marks}) GROUP BY run_id, status""", ids).fetchall()
        out = {run_id: _zero_counts() for run_id in ids}
        for row in rows:
            if row["status"] in out[row["run_id"]]:
                out[int(row["run_id"])][row["status"]] = int(row["n"])
        return out

    def retried_run_ids(self, run_ids: list[int] | set[int]) -> set[int]:
        """这一组运行里，哪些**至少有一个节点被重试过**。

        给 `run_status` 判 `completed_with_errors` 用：它是"跑完了但路上返过工"
        的第二个依据（另一个是 skipped 节点数，counts 里已经带了）。
        逐条问就是每条运行多一趟查询，列表页不划算。
        """
        ids = sorted({int(i) for i in run_ids})
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT DISTINCT run_id FROM pipeline_node_run
                     WHERE run_id IN ({marks}) AND retry_count > 0""", ids).fetchall()
        return {int(row["run_id"]) for row in rows}

    # ---------- 事件 ----------
    def add_event(self, run_id: int, node_id: int | None, event_type: str,
                  payload: dict | None = None) -> None:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO pipeline_event (run_id, node_id, event_type, payload_json)
                   VALUES (?, ?, ?, ?)""",
                (run_id, node_id, event_type,
                 json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)))

    def events_for_run(self, run_id: int, limit: int = 300) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                """SELECT e.*, n.node_key, n.label AS node_label
                     FROM pipeline_event e
                     LEFT JOIN pipeline_node_run n ON n.id = e.node_id
                    WHERE e.run_id = ?
                    ORDER BY e.id DESC LIMIT ?""",
                (run_id, min(max(limit, 1), 500))).fetchall()


pipeline_repo = PipelineRepository()
