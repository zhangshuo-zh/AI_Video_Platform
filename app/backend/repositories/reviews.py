"""校验报告与人工签字（W14）。SQL 只在这一层。"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn


class ReviewRepository:
    def create_gate(self, *, project_id: int, doc_id: int, base_version: str,
                    status: str, findings: list[dict], summary: dict,
                    created_by: int | None) -> sqlite3.Row:
        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO review_gate
                   (project_id, doc_id, base_version, status, findings_json,
                    summary_json, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (project_id, doc_id, base_version, status,
                 json.dumps(findings, ensure_ascii=False),
                 json.dumps(summary, ensure_ascii=False), created_by))
            return conn.execute("SELECT * FROM review_gate WHERE id = ?",
                                (cursor.lastrowid,)).fetchone()

    def latest_gate(self, doc_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM review_gate WHERE doc_id = ? ORDER BY id DESC LIMIT 1",
                (doc_id,)).fetchone()

    def get_gate(self, gate_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM review_gate WHERE id = ?",
                                (gate_id,)).fetchone()

    def signs(self, doc_id: int, base_version: str | None = None) -> list[sqlite3.Row]:
        sql = """SELECT r.*, u.display_name AS signed_by_name
                   FROM review_item r
                   LEFT JOIN app_user u ON u.id = r.signed_by
                  WHERE r.doc_id = ?"""
        args: list = [doc_id]
        if base_version is not None:
            sql += " AND r.base_version = ?"
            args.append(base_version)
        sql += " ORDER BY r.id"
        with get_conn() as conn:
            return conn.execute(sql, args).fetchall()

    def signed_versions(self, doc_id: int) -> list[str]:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT base_version FROM review_item WHERE doc_id = ?",
                (doc_id,)).fetchall()
        return [row["base_version"] for row in rows]

    def upsert_sign(self, *, project_id: int, doc_id: int, gate_id: int | None,
                    base_version: str, anchor: str, decision: str, note: str,
                    user_id: int) -> sqlite3.Row:
        """同一版同一锚点改判：UNIQUE 冲突时更新，不叠第二条。"""
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO review_item
                   (project_id, doc_id, gate_id, base_version, anchor, decision,
                    note, signed_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(doc_id, base_version, anchor) DO UPDATE SET
                       gate_id = excluded.gate_id,
                       decision = excluded.decision,
                       note = excluded.note,
                       signed_by = excluded.signed_by,
                       signed_at = datetime('now','localtime')""",
                (project_id, doc_id, gate_id, base_version, anchor, decision,
                 note, user_id))
            return conn.execute(
                """SELECT r.*, u.display_name AS signed_by_name
                     FROM review_item r LEFT JOIN app_user u ON u.id = r.signed_by
                    WHERE r.doc_id = ? AND r.base_version = ? AND r.anchor = ?""",
                (doc_id, base_version, anchor)).fetchone()

    def counts(self, doc_id: int, base_version: str) -> dict:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT decision, COUNT(*) AS n FROM review_item
                    WHERE doc_id = ? AND base_version = ?
                    GROUP BY decision""",
                (doc_id, base_version)).fetchall()
        out = {"approved": 0, "rejected": 0, "returned": 0}
        out.update({row["decision"]: row["n"] for row in rows})
        return out

    def project_signs(self, project_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                """SELECT r.*, u.display_name AS signed_by_name
                     FROM review_item r LEFT JOIN app_user u ON u.id = r.signed_by
                    WHERE r.project_id = ? ORDER BY r.id DESC""",
                (project_id,)).fetchall()


reviews = ReviewRepository()
