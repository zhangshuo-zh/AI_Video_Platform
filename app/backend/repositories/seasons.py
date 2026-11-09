"""整季排产的读写（阶段 E-2）。SQL 只在这一层。

season_item 不存"进行到哪"：它只指向那条 pipeline_run，状态一律现读运行表。
唯一存下来的是"启动这一集时被拦下的原因"——那句人话没有别的地方可放。
"""
from __future__ import annotations

import sqlite3

from db import get_conn
from repositories.projects import touch

_SELECT_SEASON = """
    SELECT s.*, p.name AS project_name, t.name AS template_name,
           t.version AS template_version,
           u.display_name AS created_by_name
      FROM season_run s
      JOIN project p ON p.id = s.project_id
      JOIN pipeline_template t ON t.id = s.template_id
      LEFT JOIN app_user u ON u.id = s.created_by
"""

_SELECT_ITEM = """
    SELECT i.*, e.seq AS episode_seq, e.title AS episode_title,
           r.paused AS run_paused, r.cancelled AS run_cancelled,
           r.finished_at AS run_finished_at,
           r.cancel_reason AS run_cancel_reason
      FROM season_item i
      JOIN episode e ON e.id = i.episode_id
      LEFT JOIN pipeline_run r ON r.id = i.pipeline_run_id
"""


def _one_currency(by_currency: dict[str, int]) -> dict:
    """把"按币种分的桶"折成**只在唯一币种时**才给出的单值。

    多币种时 `cost_micros` 与 `currency` 都给 `None`：调用方要么按
    `by_currency` 自己分桶显示，要么就不显示数字。给 `None` 是刻意的——
    它逼调用方处理"这一季有两种钱"这种情况，而不是默默拿到一个假和。
    """
    if len(by_currency) == 1:
        code = next(iter(by_currency))
        return {"cost_micros": by_currency[code], "currency": code}
    return {"cost_micros": None, "currency": None}


class SeasonRepository:
    def create(self, *, project_id: int, template_id: int, name: str,
               max_active_runs: int, idempotency_key: str | None,
               created_by: int | None, episode_ids: list[int]) -> int:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO season_run
                       (project_id, template_id, name, max_active_runs,
                        idempotency_key, created_by)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (project_id, template_id, name, max_active_runs,
                 idempotency_key, created_by),
            )
            season_id = int(cur.lastrowid)
            conn.executemany(
                """INSERT INTO season_item (season_id, episode_id, position)
                   VALUES (?, ?, ?)""",
                [(season_id, eid, pos) for pos, eid in enumerate(episode_ids, 1)])
            touch(conn, "project", project_id)
        return season_id

    def get(self, season_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(_SELECT_SEASON + " WHERE s.id = ?",
                                (season_id,)).fetchone()

    def by_key(self, key: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(_SELECT_SEASON + " WHERE s.idempotency_key = ?",
                                (key,)).fetchone()

    def list_by_project(self, project_id: int, limit: int = 50) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                _SELECT_SEASON + " WHERE s.project_id = ? ORDER BY s.id DESC LIMIT ?",
                (project_id, min(max(limit, 1), 200))).fetchall()

    def items_for(self, season_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                _SELECT_ITEM + " WHERE i.season_id = ? ORDER BY i.position, i.id",
                (season_id,)).fetchall()

    def item(self, item_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(_SELECT_ITEM + " WHERE i.id = ?", (item_id,)).fetchone()

    def attach_run(self, item_id: int, run_id: int) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE season_item
                      SET pipeline_run_id = ?, error_message = '',
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""", (run_id, item_id))

    def set_error(self, item_id: int, message: str) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE season_item
                      SET error_message = ?, updated_at = datetime('now','localtime')
                    WHERE id = ?""", (message[:500], item_id))

    def clear_error(self, item_id: int) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE season_item SET error_message = '',
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND error_message <> ''""", (item_id,))

    def active_seasons(self, limit: int = 20) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                """SELECT * FROM season_run
                    WHERE cancelled = 0 AND paused = 0 AND finished_at IS NULL
                    ORDER BY id LIMIT ?""", (limit,)).fetchall()

    def set_paused(self, season_id: int, paused: bool) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE season_run SET paused = ?,
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""", (int(paused), season_id))

    def set_cancelled(self, season_id: int, reason: str) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE season_run SET cancelled = 1, paused = 0,
                          cancel_reason = ?,
                          finished_at = datetime('now','localtime'),
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND cancelled = 0""", (reason[:500], season_id))

    def finish(self, season_id: int) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE season_run
                      SET finished_at = datetime('now','localtime'),
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND finished_at IS NULL AND cancelled = 0""",
                (season_id,))

    def unfinished_runs(self, season_id: int) -> int:
        """还没收尾的运行条数——排产上限就看它。"""
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n
                     FROM season_item i
                     JOIN pipeline_run r ON r.id = i.pipeline_run_id
                    WHERE i.season_id = ? AND r.finished_at IS NULL
                      AND r.cancelled = 0""", (season_id,)).fetchone()
        return int(row["n"])

    def pending_items(self, season_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                """SELECT * FROM season_item
                    WHERE season_id = ? AND pipeline_run_id IS NULL
                    ORDER BY position, id""", (season_id,)).fetchall()

    def startable_items(self, season_id: int) -> list[sqlite3.Row]:
        """还能排的集：没有运行、也没有被启动前的校验拦下。"""
        with get_conn() as conn:
            return conn.execute(
                """SELECT * FROM season_item
                    WHERE season_id = ? AND pipeline_run_id IS NULL
                      AND error_message = ''
                    ORDER BY position, id""", (season_id,)).fetchall()

    def error_items(self, season_id: int) -> list[sqlite3.Row]:
        """没有运行、启动前就被拦下的集（等输入修好后由"继续失败集"重排）。"""
        with get_conn() as conn:
            return conn.execute(
                """SELECT * FROM season_item
                    WHERE season_id = ? AND pipeline_run_id IS NULL
                      AND error_message <> ''
                    ORDER BY position, id""", (season_id,)).fetchall()

    def reopen(self, season_id: int) -> None:
        """"继续失败集"把已收尾的一季重新打开，交给调度重新看一遍。"""
        with get_conn() as conn:
            conn.execute(
                """UPDATE season_run SET finished_at = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND cancelled = 0""", (season_id,))

    def run_counts(self, season_id: int) -> dict[int, dict[str, int]]:
        """一次取出本季所有运行按节点状态的计数，免得逐集查。"""
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT n.run_id, n.status, COUNT(*) AS n
                     FROM pipeline_node_run n
                     JOIN season_item i ON i.pipeline_run_id = n.run_id
                    WHERE i.season_id = ?
                    GROUP BY n.run_id, n.status""", (season_id,)).fetchall()
        out: dict[int, dict[str, int]] = {}
        for row in rows:
            out.setdefault(int(row["run_id"]), {})[row["status"]] = int(row["n"])
        return out

    def costs_by_item(self, season_id: int) -> dict[int, dict]:
        """按 item 汇总运行里登记过的作业成本（只算产线派出去的 Job）。

        金额只认 `job` 里已有的快照；本地作业是 0，云端未对账的仍是 pending，
        这里不替它猜"应该多少钱"。

        **按币种分桶，不跨币种相加**（2026-11-09）：原来是
        `SUM(j.cost_micros)` 一把加完，而 `currency` 根本不进聚合——两种币种的
        微单位相加出来的是一个既不是 A 也不是 B 的数。同文件 `currencies()` 早就
        能告诉调用方"这一季涉及哪几种币"，于是接口一边给出无法解释的和、
        一边给出币种列表，两半各自都对、拼起来自相矛盾。
        `schemas/costs.py:44` 的 `CostSummaryOut` 已经把口径写死了
        （"一种币种一个桶，绝不跨币种相加"），这里是同一条规则推到整季。

        返回 `by_currency` 是 `{币种: 微单位}`；顶层 `cost_micros` **只在恰好
        一种币种时**给出（那种情况下它就是真和），多币种时为 `None`——
        宁可不给数字，也不给一个假的。`currency` 同理。
        """
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT i.id AS item_id,
                          j.currency AS currency,
                          COALESCE(SUM(j.cost_micros), 0) AS cost_micros,
                          COUNT(DISTINCT j.id) AS jobs,
                          COUNT(DISTINCT CASE WHEN j.status = 'succeeded'
                                              THEN j.id END) AS succeeded
                     FROM season_item i
                     JOIN pipeline_node_run n ON n.run_id = i.pipeline_run_id
                     JOIN job j ON j.id = n.job_id
                    WHERE i.season_id = ?
                    GROUP BY i.id, j.currency""", (season_id,)).fetchall()
        # 同一个 item 现在可能有多行（每种币种一行），先在 Python 里合起来：
        # 计数要**去重相加会错**（同一个 job 只属于一个币种，所以 jobs 可以直接加），
        # 金额则按币种分桶。
        out: dict[int, dict] = {}
        for r in rows:
            item_id = int(r["item_id"])
            bucket = out.setdefault(item_id, {"by_currency": {}, "jobs": 0,
                                              "succeeded": 0})
            code = r["currency"]
            if code is not None:
                key = str(code)
                bucket["by_currency"][key] = (bucket["by_currency"].get(key, 0)
                                              + int(r["cost_micros"] or 0))
            bucket["jobs"] += int(r["jobs"] or 0)
            bucket["succeeded"] += int(r["succeeded"] or 0)
        for bucket in out.values():
            bucket.update(_one_currency(bucket["by_currency"]))
        return out

    def currencies(self, season_id: int) -> list[str]:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT j.currency
                     FROM season_item i
                     JOIN pipeline_node_run n ON n.run_id = i.pipeline_run_id
                     JOIN job j ON j.id = n.job_id
                    WHERE i.season_id = ? AND j.currency IS NOT NULL
                      AND j.cost_micros IS NOT NULL""", (season_id,)).fetchall()
        return sorted({str(r["currency"]) for r in rows})


seasons = SeasonRepository()
