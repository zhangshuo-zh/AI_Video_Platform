"""`review_check` 的仓储（迁移 0038）。

只做两件事：按目标批量读、按目标＋项改判式写入。判命不在这儿——
"能不能签字"是 `reviewkit.checklist()` 算出来的，这里连"合格/不合格"的语义都不解释。

批量读是刚需：审核中心一屏几十条作业，逐条回库就是几十趟（v0.102 那条聚合读纪律
在 `repositories/production.py` 里写过一次，同一个道理）。
"""
from __future__ import annotations

from db import get_conn
from repositories.reviews import JOB  # 多态目标的取值与签字同一份，不另立枚举


class ReviewCheckRepository:
    """一条作业（或一份文档）的人工清单结论。"""

    TARGET_KIND = JOB

    def list_for(self, target_kind: str, target_ids: list[int]) -> dict[int, dict]:
        """一次读一批目标的结论：{target_id: {code: 行}}。

        行里带 `checked_by_name`，因为审核面要显示"这一项是谁看的"——
        复审时最先问的就是这个。
        """
        ids = [int(i) for i in target_ids if i is not None]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT c.*, u.display_name AS checked_by_name
                      FROM review_check c
                      LEFT JOIN app_user u ON u.id = c.checked_by
                     WHERE c.target_kind = ? AND c.target_id IN ({marks})""",
                [target_kind, *ids]).fetchall()
        # 回**普通 dict**，不回 sqlite3.Row：这一份要被 `reviewkit` 用 `.get()` 读
        # （项可能没勾过），Row 没有 `.get()`——同一类地雷本轮已经踩过两次，
        # 规矩是"Row 到仓储层为止"。
        out: dict[int, dict] = {}
        for row in rows:
            out.setdefault(int(row["target_id"]), {})[row["code"]] = dict(row)
        return out

    def get(self, target_kind: str, target_id: int) -> dict:
        return self.list_for(target_kind, [target_id]).get(int(target_id), {})

    def upsert(self, *, project_id: int, target_kind: str, target_id: int,
               code: str, state: str, note: str, base_version: str,
               user_id: int):
        """同一目标同一项只留一条：改判覆盖，与 `review_item` 的改判口径一致。"""
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO review_check
                   (project_id, target_kind, target_id, code, state, note,
                    checked_by, base_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(target_kind, target_id, code) DO UPDATE SET
                       state = excluded.state,
                       note = excluded.note,
                       checked_by = excluded.checked_by,
                       checked_at = datetime('now', 'localtime'),
                       base_version = excluded.base_version""",
                (project_id, target_kind, int(target_id), code, state, note,
                 user_id, base_version))
            row = conn.execute(
                """SELECT * FROM review_check
                    WHERE target_kind = ? AND target_id = ? AND code = ?""",
                (target_kind, int(target_id), code)).fetchone()
            return dict(row) if row is not None else None

    def clear_for_target(self, target_kind: str, target_id: int) -> int:
        """产物换版时清掉旧结论。

        为什么不留历史：`review_check` 的语义是"这一版你看过没有"，
        换了产物就是另一版——留着旧勾选等于让人对着新版说"我看过了"。
        要看历史请查 `review_item`（签字才是历史的那张表）。
        """
        with get_conn() as conn:
            cur = conn.execute(
                "DELETE FROM review_check WHERE target_kind = ? AND target_id = ?",
                (target_kind, int(target_id)))
            return cur.rowcount


review_checks = ReviewCheckRepository()
