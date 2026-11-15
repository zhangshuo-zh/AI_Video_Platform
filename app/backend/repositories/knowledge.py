"""设定中心数据访问层（M4）：画风词缀与设定矛盾审阅 SQL 只出现在此层。"""
from __future__ import annotations

import sqlite3
from typing import Any

from db import get_conn
from repositories.projects import touch

_ISSUE_SELECT = """
    SELECT c.*, COALESCE(u.display_name, '') AS resolved_by_name
      FROM consistency_issue c
      LEFT JOIN app_user u ON u.id = c.resolved_by
"""


class KnowledgeRepository:
    def list_affixes(self, project_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                """SELECT * FROM style_affix
                    WHERE project_id = ?
                    ORDER BY is_default DESC, sort_order ASC, id ASC""",
                (project_id,),
            ).fetchall()

    def get_affix(self, affix_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM style_affix WHERE id = ?",
                (affix_id,),
            ).fetchone()

    def create_affix(
        self,
        *,
        project_id: int,
        name: str,
        category: str,
        positive: str,
        negative: str,
        is_default: bool,
        sort_order: int,
        created_by: int | None,
    ) -> sqlite3.Row:
        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO style_affix
                   (project_id, name, category, positive, negative,
                    is_default, sort_order, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    name,
                    category,
                    positive,
                    negative,
                    1 if is_default else 0,
                    int(sort_order),
                    created_by,
                ),
            )
            touch(conn, "project", project_id)
            row = conn.execute(
                "SELECT * FROM style_affix WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            assert row is not None
            return row

    def update_affix(
        self,
        affix_id: int,
        *,
        name: str,
        category: str,
        positive: str,
        negative: str,
        is_default: bool,
        sort_order: int,
    ) -> sqlite3.Row | None:
        with get_conn() as conn:
            cur = conn.execute(
                "SELECT project_id FROM style_affix WHERE id = ?",
                (affix_id,),
            ).fetchone()
            if cur is None:
                return None
            conn.execute(
                """UPDATE style_affix
                      SET name = ?, category = ?, positive = ?, negative = ?,
                          is_default = ?, sort_order = ?,
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""",
                (
                    name,
                    category,
                    positive,
                    negative,
                    1 if is_default else 0,
                    int(sort_order),
                    affix_id,
                ),
            )
            touch(conn, "project", int(cur["project_id"]))
            return conn.execute(
                "SELECT * FROM style_affix WHERE id = ?",
                (affix_id,),
            ).fetchone()

    def delete_affix(self, affix_id: int) -> bool:
        with get_conn() as conn:
            cur = conn.execute(
                "SELECT project_id FROM style_affix WHERE id = ?",
                (affix_id,),
            ).fetchone()
            if cur is None:
                return False
            touch(conn, "project", int(cur["project_id"]))
            return (
                conn.execute(
                    "DELETE FROM style_affix WHERE id = ?",
                    (affix_id,),
                ).rowcount
                > 0
            )

    def list_shots_by_project(self, project_id: int) -> list[sqlite3.Row]:
        with get_conn() as conn:
            return conn.execute(
                """SELECT s.*, e.seq AS episode_seq, e.title AS episode_title
                     FROM shot s
                     JOIN episode e ON e.id = s.episode_id
                    WHERE e.project_id = ?
                    ORDER BY e.seq ASC, s.seq ASC""",
                (project_id,),
            ).fetchall()

    def list_issues(
        self,
        project_id: int,
        status: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses = ["c.project_id = ?"]
        args: list[Any] = [project_id]
        if status:
            clauses.append("c.status = ?")
            args.append(status)
        with get_conn() as conn:
            return conn.execute(
                f"{_ISSUE_SELECT} WHERE {' AND '.join(clauses)}"
                " ORDER BY CASE c.status WHEN 'open' THEN 1 WHEN 'adopted' THEN 2 ELSE 3 END,"
                " CASE c.severity WHEN 'error' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,"
                " c.id DESC",
                args,
            ).fetchall()

    def get_issue(self, issue_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                f"{_ISSUE_SELECT} WHERE c.id = ?",
                (issue_id,),
            ).fetchone()

    def upsert_scanned_issues(
        self,
        project_id: int,
        items: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """写入扫描发现的问题并清掉已修复的待处理项；保留已采纳/已忽略决定。"""
        keys = [str(item["issue_key"]) for item in items]
        new_count = 0
        reopened_count = 0
        with get_conn() as conn:
            existing_rows = conn.execute(
                "SELECT id, issue_key, status FROM consistency_issue WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            existing = {str(r["issue_key"]): r for r in existing_rows}

            if keys:
                marks = ",".join("?" for _ in keys)
                conn.execute(
                    f"""DELETE FROM consistency_issue
                         WHERE project_id = ? AND status = 'open'
                           AND issue_key NOT IN ({marks})""",
                    [project_id, *keys],
                )
            else:
                conn.execute(
                    "DELETE FROM consistency_issue WHERE project_id = ? AND status = 'open'",
                    (project_id,),
                )

            for item in items:
                key = str(item["issue_key"])
                row = existing.get(key)
                if row is None:
                    conn.execute(
                        """INSERT INTO consistency_issue
                           (project_id, issue_key, category, severity, title,
                            description, source_type, source_id, suggestion, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
                        (
                            project_id,
                            key,
                            str(item["category"]),
                            str(item["severity"]),
                            str(item["title"]),
                            str(item.get("description", "")),
                            str(item.get("source_type", "")),
                            item.get("source_id"),
                            str(item.get("suggestion", "")),
                        ),
                    )
                    new_count += 1
                elif row["status"] == "open":
                    conn.execute(
                        """UPDATE consistency_issue
                              SET category = ?, severity = ?, title = ?,
                                  description = ?, source_type = ?, source_id = ?,
                                  suggestion = ?, updated_at = datetime('now','localtime')
                            WHERE id = ?""",
                        (
                            str(item["category"]),
                            str(item["severity"]),
                            str(item["title"]),
                            str(item.get("description", "")),
                            str(item.get("source_type", "")),
                            item.get("source_id"),
                            str(item.get("suggestion", "")),
                            int(row["id"]),
                        ),
                    )
            touch(conn, "project", project_id)
        return new_count, reopened_count

    def resolve_issue(
        self,
        issue_id: int,
        *,
        status: str,
        resolved_by: int | None,
    ) -> sqlite3.Row | None:
        with get_conn() as conn:
            cur = conn.execute(
                "SELECT project_id FROM consistency_issue WHERE id = ?",
                (issue_id,),
            ).fetchone()
            if cur is None:
                return None
            if status == "open":
                conn.execute(
                    """UPDATE consistency_issue
                          SET status = 'open', resolved_by = NULL, resolved_at = NULL,
                              updated_at = datetime('now','localtime')
                        WHERE id = ?""",
                    (issue_id,),
                )
            else:
                conn.execute(
                    """UPDATE consistency_issue
                          SET status = ?, resolved_by = ?,
                              resolved_at = datetime('now','localtime'),
                              updated_at = datetime('now','localtime')
                        WHERE id = ?""",
                    (status, resolved_by, issue_id),
                )
            touch(conn, "project", int(cur["project_id"]))
            return conn.execute(
                f"{_ISSUE_SELECT} WHERE c.id = ?",
                (issue_id,),
            ).fetchone()


knowledge = KnowledgeRepository()
