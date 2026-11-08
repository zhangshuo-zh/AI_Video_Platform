"""抽取运行与候选（W13）。SQL 只出现在这一层。"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn

_RUN_SELECT = """
    SELECT r.*, j.status AS job_status, j.error_code, j.error_message,
           j.cost_micros, j.currency, j.billable_status
      FROM extraction_run r
      JOIN job j ON j.id = r.job_id
"""


class ExtractionRepository:
    def create_run(self, *, project_id: int, doc_id: int | None, job_id: int,
                   doc_title: str, base_version: str, content_hash: str,
                   model_code: str, prompt_version: str,
                   created_by: int | None) -> sqlite3.Row:
        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO extraction_run
                   (project_id, doc_id, job_id, doc_title, base_version, content_hash,
                    model_code, prompt_version, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, doc_id, job_id, doc_title, base_version, content_hash,
                 model_code, prompt_version, created_by),
            )
            return conn.execute(f"{_RUN_SELECT} WHERE r.id = ?",
                                (cursor.lastrowid,)).fetchone()

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(f"{_RUN_SELECT} WHERE r.id = ?", (run_id,)).fetchone()

    def list_runs(self, project_id: int, limit: int = 30) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                f"{_RUN_SELECT} WHERE r.project_id = ? ORDER BY r.id DESC LIMIT ?",
                (project_id, limit)).fetchall()

    def find_recent(self, doc_id: int, content_hash: str, model_code: str,
                    prompt_version: str) -> sqlite3.Row | None:
        """24 小时内同一份正文 + 同一模型 + 同一提示词版本＝同一次抽取。"""
        with get_conn() as conn:
            return conn.execute(
                f"""{_RUN_SELECT}
                     WHERE r.doc_id = ? AND r.content_hash = ? AND r.model_code = ?
                       AND r.prompt_version = ?
                       AND r.created_at > datetime('now','localtime','-1 day')
                     ORDER BY r.id DESC LIMIT 1""",
                (doc_id, content_hash, model_code, prompt_version)).fetchone()

    def set_status(self, run_id: int, status: str, note: str = "",
                   materialized: bool = False) -> None:
        with get_conn() as conn:
            if materialized:
                conn.execute(
                    """UPDATE extraction_run
                          SET status = ?, note = ?,
                              materialized_at = datetime('now','localtime')
                        WHERE id = ?""", (status, note, run_id))
            else:
                conn.execute("UPDATE extraction_run SET status = ?, note = ? WHERE id = ?",
                             (status, note, run_id))

    def counts(self, run_id: int) -> dict:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT status, COUNT(*) AS n FROM extraction_item
                    WHERE run_id = ? GROUP BY status""", (run_id,)).fetchall()
        out = {"pending": 0, "accepted": 0, "merged": 0, "rejected": 0}
        out.update({row["status"]: row["n"] for row in rows})
        return out

    def items(self, run_id: int, kind: str | None = None,
              status: str | None = None) -> list[sqlite3.Row]:
        clauses = ["i.run_id = ?"]
        args: list = [run_id]
        if kind:
            clauses.append("i.kind = ?")
            args.append(kind)
        if status:
            clauses.append("i.status = ?")
            args.append(status)
        with get_conn() as conn:
            return conn.execute(
                """SELECT i.*, u.display_name AS decided_by_name
                     FROM extraction_item i
                     LEFT JOIN app_user u ON u.id = i.decided_by
                    WHERE """ + " AND ".join(clauses) +
                " ORDER BY CASE i.kind WHEN 'character' THEN 1 WHEN 'scene' THEN 2 "
                "WHEN 'prop' THEN 3 ELSE 4 END, i.id", args).fetchall()

    def get_item(self, item_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                """SELECT i.*, u.display_name AS decided_by_name
                     FROM extraction_item i
                     LEFT JOIN app_user u ON u.id = i.decided_by
                    WHERE i.id = ?""", (item_id,)).fetchone()

    def insert_items(self, run_id: int, items: list[dict]) -> int:
        """一次写入；重复物化时先清掉 pending，避免同一批候选出现两遍。"""
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM extraction_item WHERE run_id = ? AND status = 'pending'",
                (run_id,))
            conn.executemany(
                """INSERT INTO extraction_item
                   (run_id, kind, payload, confidence, evidence)
                   VALUES (?, ?, ?, ?, ?)""",
                [(run_id, item["kind"],
                  json.dumps(item["payload"], ensure_ascii=False),
                  item.get("confidence"),
                  json.dumps(item["evidence"], ensure_ascii=False))
                 for item in items])
            return len(items)

    def decide(self, item_id: int, status: str, user_id: int,
               asset_id: int | None = None) -> bool:
        """把一条候选推到终态。只允许从 pending 出发——两个人同时点，
        第二个返回 False，路由回 409。"""
        with get_conn() as conn:
            changed = conn.execute(
                """UPDATE extraction_item
                      SET status = ?, asset_id = ?, decided_by = ?,
                          decided_at = datetime('now','localtime')
                    WHERE id = ? AND status = 'pending'""",
                (status, asset_id, user_id, item_id)).rowcount
            return changed > 0

    def shots_for_doc(self, doc_id: int) -> list[sqlite3.Row]:
        """某份文档历次抽取里的镜头候选，按运行倒序。导出用。"""
        with get_conn() as conn:
            return conn.execute(
                """SELECT i.* FROM extraction_item i
                     JOIN extraction_run r ON r.id = i.run_id
                    WHERE r.doc_id = ? AND i.kind = 'shot'
                    ORDER BY r.id DESC, i.id""", (doc_id,)).fetchall()


runs = ExtractionRepository()
