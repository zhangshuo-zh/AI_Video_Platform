"""校验报告与人工签字（W14；0025 起审核对象多态化）。SQL 只在这一层。

审核对象只有两档：
  * `document`——审的是正文的某一版，`base_version` 是正文 sha256，锚点是字符偏移；
  * `job`——审的是某条已成功作业的产物，`base_version` 是产物清单摘要，
    锚点是 `artifact`（整条作业）或 `artifact:<下标>`（某一件产物）。

两档共用一张表、一套签字语义（改判不叠历史、版本失配就作废）。产线的
「关键帧验收」「镜头审核」都落在 `job` 这一档：**审核结论只有这一个写入口**，
产线只存引用，不复制结论。
"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn

DOCUMENT = "document"
JOB = "job"


class ReviewRepository:
    # 审核对象的两档。挂在类上是为了调用方写 `reviews.DOCUMENT`——
    # `from … import reviews` 是单例导入，模块级常量那样写取不到。
    DOCUMENT = DOCUMENT
    JOB = JOB

    # ---------- gate：仍然只针对文档（规则校验器读的是正文） ----------
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

    # ---------- 签字 ----------
    def signs(self, target_kind: str, target_id: int,
              base_version: str | None = None) -> list[sqlite3.Row]:
        sql = """SELECT r.*, u.display_name AS signed_by_name
                   FROM review_item r
                   LEFT JOIN app_user u ON u.id = r.signed_by
                  WHERE r.target_kind = ? AND r.target_id = ?"""
        args: list = [target_kind, target_id]
        if base_version is not None:
            sql += " AND r.base_version = ?"
            args.append(base_version)
        sql += " ORDER BY r.id"
        with get_conn() as conn:
            return conn.execute(sql, args).fetchall()

    def upsert_sign(self, *, project_id: int, target_kind: str, target_id: int,
                    gate_id: int | None, base_version: str, anchor: str,
                    decision: str, note: str, user_id: int) -> sqlite3.Row:
        """同一对象、同一版、同一锚点改判：UNIQUE 冲突时更新，不叠第二条。"""
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO review_item
                   (project_id, target_kind, target_id, gate_id, base_version,
                    anchor, decision, note, signed_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(target_kind, target_id, base_version, anchor) DO UPDATE SET
                       gate_id = excluded.gate_id,
                       decision = excluded.decision,
                       note = excluded.note,
                       signed_by = excluded.signed_by,
                       signed_at = datetime('now','localtime')""",
                (project_id, target_kind, target_id, gate_id, base_version, anchor,
                 decision, note, user_id))
            return conn.execute(
                """SELECT r.*, u.display_name AS signed_by_name
                     FROM review_item r LEFT JOIN app_user u ON u.id = r.signed_by
                    WHERE r.target_kind = ? AND r.target_id = ?
                      AND r.base_version = ? AND r.anchor = ?""",
                (target_kind, target_id, base_version, anchor)).fetchone()

    def counts(self, target_kind: str, target_id: int, base_version: str) -> dict:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT decision, COUNT(*) AS n FROM review_item
                    WHERE target_kind = ? AND target_id = ? AND base_version = ?
                    GROUP BY decision""",
                (target_kind, target_id, base_version)).fetchall()
        out = {"approved": 0, "rejected": 0, "returned": 0}
        out.update({row["decision"]: row["n"] for row in rows})
        return out

    def signer_counts(self, project_ids: list[int] | None) -> list[dict]:
        """谁签了几次、各结论几条（按签字人与结论分组）。

        与 `decision_counts` 同一纪律：只数真存在的签字行。**不在此处合成"通过率"**——
        签得少可能只是这一轮没轮到那个人，把它读成质量高低是平台说不清的事。
        """
        if project_ids is None:
            where, args = "1", []
        elif not project_ids:
            where, args = "0", []
        else:
            marks = ",".join("?" for _ in project_ids)
            where, args = f"project_id IN ({marks})", list(project_ids)
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT r.signed_by AS user_id, u.display_name AS name,
                          r.decision, COUNT(*) AS n
                     FROM review_item r LEFT JOIN app_user u ON u.id = r.signed_by
                     WHERE {where}
                     GROUP BY r.signed_by, u.display_name, r.decision
                     ORDER BY r.signed_by, r.decision""", args).fetchall()
        return [dict(row) for row in rows]

    def decision_counts(self, project_ids: list[int] | None) -> list[dict]:
        """签字结论的分布（P2 质量分析的读数出口）：按 档别 × 结论 数条数。

        只数 `review_item` 里真存在的行——**没签过的那部分不在这里出现**，
        "还差多少没审"由作业状态那一份事实回答（`jobs.stats_for_projects`），
        两边各说各的，不在这里合成一个"通过率"骗人的数。
        """
        if project_ids is None:
            where, args = "1", []
        elif not project_ids:
            where, args = "0", []
        else:
            marks = ",".join("?" for _ in project_ids)
            where, args = f"project_id IN ({marks})", list(project_ids)
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT target_kind, decision, COUNT(*) AS n
                      FROM review_item WHERE {where}
                     GROUP BY target_kind, decision
                     ORDER BY target_kind, decision""", args).fetchall()
        return [dict(r) for r in rows]

    def delete_for_document(self, conn, doc_id: int) -> None:
        """删文档时清掉它的签字。

        为什么要在代码里删：`target_id` 是多态列，建不了外键，也就没有 CASCADE
        （0025 之前这一条是 `doc_id REFERENCES document ON DELETE CASCADE` 白送的）。
        调用方 `repositories/documents.delete()` 已经握着一条开着的连接，
        所以这里收 `conn` 而不是自己再开一条——文档行与签字必须在同一个事务里走，
        否则文档删了、签字还在，就成了指向空气的审核记录。
        """
        conn.execute(
            "DELETE FROM review_item WHERE target_kind = 'document' AND target_id = ?",
            (doc_id,))

    # ---------- 产物审核队列（关键帧／镜头视频） ----------
    def job_queue(self, project_id: int, limit: int = 200) -> list[sqlite3.Row]:
        """这个项目里"可以被人审"的作业：已成功、是图像或视频、产物清单非空。

        镜头号与集号一次带出来，界面不必为每条作业再问一次创作中心（N+1）。
        签字另查一次（`signs_for_jobs` 批量），不在这条 SQL 里做子查询——
        一条作业可能有多件产物、多个锚点的签字，摊平成行反而难读。
        """
        with get_conn() as conn:
            return conn.execute(
                """SELECT j.id AS job_id, j.project_id, j.capability, j.kind,
                          j.execution_mode, j.provider_code, j.model_code,
                          j.shot_id, j.episode_id, j.status,
                          j.result_paths_json, j.finished_at,
                          s.seq AS shot_seq, e.seq AS episode_seq, e.title AS episode_title
                     FROM job j
                     LEFT JOIN shot s ON s.id = j.shot_id
                     LEFT JOIN episode e ON e.id = j.episode_id
                    WHERE j.project_id = ?
                      AND j.status = 'succeeded'
                      AND j.capability IN ('image','video')
                      AND j.result_paths_json NOT IN ('', '[]')
                    ORDER BY j.id DESC
                    LIMIT ?""",
                (project_id, min(max(limit, 1), 200))).fetchall()

    def _signs_for(self, project_id: int, target_kind: str,
                   target_ids: list[int]) -> list[sqlite3.Row]:
        """批量取一批目标的签字。空列表直接回空，不拼出 `IN ()` 那种语法错误。

        文档与产物两档共用这一条：两边的"当前版本有没有签"是同一套判定，
        差别只在版本令牌怎么算（正文 sha256 / 产物清单摘要），那是 services 的事。
        """
        if not target_ids:
            return []
        marks = ",".join("?" for _ in target_ids)
        with get_conn() as conn:
            return conn.execute(
                f"""SELECT r.*, u.display_name AS signed_by_name
                      FROM review_item r
                      LEFT JOIN app_user u ON u.id = r.signed_by
                     WHERE r.project_id = ? AND r.target_kind = ?
                       AND r.target_id IN ({marks})
                     ORDER BY r.id""",
                [project_id, target_kind, *target_ids]).fetchall()

    def signs_for_jobs(self, project_id: int, job_ids: list[int]) -> list[sqlite3.Row]:
        return self._signs_for(project_id, self.JOB, job_ids)

    def signs_for_documents(self, project_id: int,
                            doc_ids: list[int]) -> list[sqlite3.Row]:
        """首页工作台要一次问清"这些文档里哪几份签过"，不能一份一份查。"""
        return self._signs_for(project_id, self.DOCUMENT, doc_ids)


reviews = ReviewRepository()
