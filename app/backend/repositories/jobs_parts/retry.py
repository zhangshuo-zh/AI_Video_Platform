from __future__ import annotations

import sqlite3
import uuid

from db import get_conn, transaction


class JobsRetryMixin:

    def retry(self, original: sqlite3.Row, created_by: int | None) -> sqlite3.Row:
        """重试＝新建一行，原行的日志／成本／错误一个字段都不动。

        三件事顺手补上（P0）：
        - `attempt` 终于写起来了。它不是执行次数计数器，而是"这一串里的第几枪"，
          界面上要能说出"这已经是第 4 次预计计费"，而不是摆 4 条互不相干的待核对。
        - 原行记下 `superseded_by_job_id`。没有这一位，重试链只能从新行往回找，
          人看成本账时看不出"哪几笔其实是同一件事"。
        - **`source_version` 必须跟着继承**。这一列记的是"这条产物是按哪一版
          分镜／资产渲染的"，而重试复制的正是原来那份提示词快照——渲染依据没变，
          指纹却留成空串的话，`job_source_stale()` 按口径把空串当"旧作业，不判过期"，
          改过剧本再重试就会把旧素材洗成新素材：分镜不贴「源已变」，成片前置检查
          也放行。继承而不是按当前分镜重算，是因为重算出来的那个"新"根本没被用过。
        """
        key = "retry-" + uuid.uuid4().hex
        cloud = original["execution_mode"] == "cloud"
        billable = "pending_reconciliation" if cloud and original["unit_price_micros"] is not None \
            else "not_billable"
        # 整段放进 `transaction()`（`BEGIN IMMEDIATE`，写锁一开始就拿到）：
        # "有没有已存在的重试行" 与 "插一条新行" 之间不能有窗口，否则两个
        # 并发重试各看各的、各插一条。`transaction()` 里所有 `get_conn()`
        # 借用同一条连接，所以下面不需要再开第二个。
        with transaction() as conn:
            # **先确认"这一条还没被重试过"，再建新行**（2026-11-08，P1 幂等）。
            #
            # 原来是"先 INSERT 新作业、再 UPDATE 原行"，而那条 UPDATE 的条件是
            # `superseded_by_job_id IS NULL`——它只能阻止**第二次回写**，
            # 拦不住第二次 INSERT。于是用户对同一条 failed 作业快速双击、或者
            # 「批量重试」与单条重试并发时，库里会真真切切多出两条 attempt=2
            # 的排队作业，worker 把两条都领走、跑两遍模型（云端就是两笔钱）。
            #
            # `BEGIN IMMEDIATE` 让"查 + 插 + 回写"落在同一个写事务里，
            # 两个并发的重试因此会串行：后来那一个在 `existing` 这一步就看见
            # 前一个建好的新行，直接把它返回。用户看到的是"同一条重试"，
            # 而不是又排了一件活。
            #
            # 为什么不用 `superseded_by_job_id = -1` 占位：那一列有
            # `REFERENCES job(id)` 外键（0020），写一个不存在的 id 会直接
            # FOREIGN KEY constraint failed。
            existing = conn.execute(
                """SELECT * FROM job WHERE retry_of_job_id = ?
                    ORDER BY id DESC LIMIT 1""",
                (original["id"],)).fetchone()
            if existing is not None:
                return existing
            cur = conn.execute(
                """INSERT INTO job
                       (project_id, episode_id, shot_id, capability, kind, execution_mode,
                        provider_code, model_code, adapter_version, prompt_snapshot,
                        params_json, input_refs_json, status, priority, idempotency_key,
                        retry_of_job_id, attempt, user_confirmed_cloud, quantity, unit,
                        unit_price_micros, cost_micros, currency, pricing_snapshot_json,
                        billable_status, source_version, created_by)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (original["project_id"], original["episode_id"], original["shot_id"],
                     original["capability"], original["kind"], original["execution_mode"],
                     original["provider_code"], original["model_code"],
                     original["adapter_version"], original["prompt_snapshot"],
                     original["params_json"], original["input_refs_json"],
                     original["priority"], key, original["id"],
                     int(original["attempt"] or 1) + 1,
                     original["user_confirmed_cloud"], original["quantity"],
                     original["unit"], original["unit_price_micros"],
                     original["cost_micros"], original["currency"],
                     original["pricing_snapshot_json"], billable,
                     original["source_version"] if "source_version" in original.keys() else "",
                     created_by),
                )
            conn.execute(
                """UPDATE job SET superseded_by_job_id = ?,
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND superseded_by_job_id IS NULL""",
                (cur.lastrowid, original["id"]))
            return conn.execute("SELECT * FROM job WHERE id = ?",
                                (cur.lastrowid,)).fetchone()


    def retry_chain(self, job_id: int) -> list[sqlite3.Row]:
        """把一条作业所在的重试链整个取出来（含自己），按 id 升序。

        先往上游走到链头（`retry_of_job_id` 只挂在新行上，所以要一路回溯），
        再从链头往下收。链长是个位数到几十，用两次查询比递归 CTE 更好读，
        也不会因为 SQLite 版本差异在 `WITH RECURSIVE` 的列推导上出意外。
        """
        with get_conn() as conn:
            root_id = job_id
            seen: set[int] = set()
            while root_id not in seen:
                seen.add(root_id)
                row = conn.execute("SELECT retry_of_job_id FROM job WHERE id = ?",
                                   (root_id,)).fetchone()
                if row is None or row["retry_of_job_id"] is None:
                    break
                root_id = int(row["retry_of_job_id"])
            rows = conn.execute(
                """WITH RECURSIVE chain(id) AS (
                       SELECT ? UNION ALL
                       SELECT j.id FROM job j JOIN chain c ON j.retry_of_job_id = c.id
                   )
                   SELECT * FROM job WHERE id IN (SELECT id FROM chain) ORDER BY id""",
                (root_id,)).fetchall()
        return list(rows)


    def retry_count(self, job_id: int) -> int:
        """这条作业之后已经被重试过几枪（不含自己）。"""
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM job WHERE retry_of_job_id = ?",
                (job_id,)).fetchone()
        return int(row["n"] or 0)
