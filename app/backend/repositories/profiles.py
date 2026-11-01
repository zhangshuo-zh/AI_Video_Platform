"""参数模板的读写（W08）。

模板是"提交前预填什么"，不是执行真相：Job 落库时已经冻结了自己的参数快照，
之后改模板、删模板都不会影响历史作业。这条边界要在仓储层就守住——
这里只读写 generation_profile，绝不回写 job。
"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn

_SELECT = """
    SELECT g.*, COALESCE(p.name, '') AS project_name
      FROM generation_profile g
      LEFT JOIN project p ON p.id = g.project_id
"""


class ProfileRepository:
    @staticmethod
    def _scope(project_ids: list[int] | None) -> tuple[str, list]:
        """None＝管理员看全部（含全局模板）；列表＝只看这些项目 + 全局。"""
        if project_ids is None:
            return "1", []
        if not project_ids:
            return "g.project_id IS NULL", []
        marks = ",".join("?" for _ in project_ids)
        return f"(g.project_id IS NULL OR g.project_id IN ({marks}))", list(project_ids)

    def list(self, project_ids: list[int] | None,
             capability: str | None = None) -> list[sqlite3.Row]:
        where, args = self._scope(project_ids)
        conds = [where]
        if capability:
            conds.append("g.capability = ?")
            args.append(capability)
        with get_conn() as conn:
            return conn.execute(
                _SELECT + " WHERE " + " AND ".join(conds)
                + " ORDER BY g.project_id IS NOT NULL, g.capability, g.name", args
            ).fetchall()

    def get(self, profile_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(_SELECT + " WHERE g.id = ?", (profile_id,)).fetchone()

    def name_taken(self, project_id: int | None, capability: str, name: str,
                   exclude_id: int | None = None) -> bool:
        # SQLite 的 UNIQUE 索引把 NULL 当互不相等，全局模板这条只能在这里拦
        scope = "project_id IS NULL" if project_id is None else "project_id = ?"
        args: list = [] if project_id is None else [project_id]
        sql = f"SELECT 1 FROM generation_profile WHERE {scope} AND capability = ? AND name = ?"
        args += [capability, name]
        if exclude_id is not None:
            sql += " AND id <> ?"
            args.append(exclude_id)
        with get_conn() as conn:
            return conn.execute(sql, args).fetchone() is not None

    @staticmethod
    def _clear_default(conn, project_id: int | None, capability: str,
                       exclude_id: int | None = None) -> None:
        scope = "project_id IS NULL" if project_id is None else "project_id = ?"
        args: list = [] if project_id is None else [project_id]
        sql = f"UPDATE generation_profile SET is_default = 0 WHERE {scope} AND capability = ?"
        args.append(capability)
        if exclude_id is not None:
            sql += " AND id <> ?"
            args.append(exclude_id)
        conn.execute(sql, args)

    def create(self, data: dict) -> sqlite3.Row:
        with get_conn() as conn:
            if data.get("is_default"):
                self._clear_default(conn, data.get("project_id"), data["capability"])
            cur = conn.execute(
                """INSERT INTO generation_profile
                   (project_id, name, capability, provider_code, model_code,
                    params_json, is_default, created_by)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (data.get("project_id"), data["name"], data["capability"],
                 data.get("provider_code", ""), data.get("model_code", ""),
                 json.dumps(data.get("params", {}), ensure_ascii=False, sort_keys=True),
                 int(bool(data.get("is_default"))), data.get("created_by")),
            )
            return conn.execute(_SELECT + " WHERE g.id = ?",
                                (cur.lastrowid,)).fetchone()

    def update(self, profile_id: int, name: str, params: dict,
               is_default: bool) -> sqlite3.Row | None:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM generation_profile WHERE id = ?",
                               (profile_id,)).fetchone()
            if row is None:
                return None
            if is_default:
                self._clear_default(conn, row["project_id"], row["capability"],
                                    exclude_id=profile_id)
            conn.execute(
                """UPDATE generation_profile
                      SET name = ?, params_json = ?, is_default = ?,
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""",
                (name, json.dumps(params, ensure_ascii=False, sort_keys=True),
                 int(bool(is_default)), profile_id),
            )
            return conn.execute(_SELECT + " WHERE g.id = ?", (profile_id,)).fetchone()

    def delete(self, profile_id: int) -> bool:
        with get_conn() as conn:
            cur = conn.execute("DELETE FROM generation_profile WHERE id = ?", (profile_id,))
            return cur.rowcount > 0


profiles = ProfileRepository()
