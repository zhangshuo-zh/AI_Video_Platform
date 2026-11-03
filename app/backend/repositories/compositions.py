"""成片记录与"哪条视频作业算这一镜的成片"的查询（阶段 D）。

SQL 只在这一层。成片状态**不存库**：由关联 Job 现算（见 composition/service.py），
所以这里没有 status 列，也没有"同步状态"的方法。

`latest_video_job()` 的选取口径要说清楚：
  * 只看 `capability='video'` 且 `status='succeeded'` 的作业；
  * 同一镜有多条成功作业时取 id 最大的一条（最新一次成功）；
  * 被判失败的更新作业不会把一个更早的成功产物顶掉——失败没有产物可合，
    拿它当"这一镜没有成片"会把已经能用的素材误判成缺片。
"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn


class CompositionRepository:
    def create(self, *, project_id: int, episode_id: int,
               pipeline_run_id: int | None, version: int,
               timeline: dict, params: dict, job_id: int | None,
               created_by: int | None) -> sqlite3.Row:
        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO composition
                   (project_id, episode_id, pipeline_run_id, version,
                    timeline_json, params_json, job_id, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, episode_id, pipeline_run_id, version,
                 json.dumps(timeline, ensure_ascii=False, sort_keys=True),
                 json.dumps(params, ensure_ascii=False, sort_keys=True),
                 job_id, created_by))
            return conn.execute(
                "SELECT * FROM composition WHERE id = ?",
                (cursor.lastrowid,)).fetchone()

    def get(self, composition_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                """SELECT c.*, e.seq AS episode_seq, e.title AS episode_title
                     FROM composition c JOIN episode e ON e.id = c.episode_id
                    WHERE c.id = ?""", (composition_id,)).fetchone()

    def by_job(self, job_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                "SELECT * FROM composition WHERE job_id = ?",
                (job_id,)).fetchone()

    def list_for_project(self, project_id: int,
                         episode_id: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT c.*, e.seq AS episode_seq, e.title AS episode_title "
               "FROM composition c "
               "JOIN episode e ON e.id = c.episode_id "
               "WHERE c.project_id = ?")
        args: list = [project_id]
        if episode_id is not None:
            sql += " AND c.episode_id = ?"
            args.append(episode_id)
        sql += " ORDER BY c.id DESC"
        with get_conn() as conn:
            return conn.execute(sql, args).fetchall()

    def next_version(self, episode_id: int) -> int:
        """同一集的下一个成片版本。并发插入由 UNIQUE(episode_id,version) 兜底。"""
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS v "
                "FROM composition WHERE episode_id = ?",
                (episode_id,)).fetchone()
            return int(row["v"])

    def attach_job(self, composition_id: int, job_id: int) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE composition SET job_id = ?,
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND job_id IS NULL""",
                (job_id, composition_id))

    def record_output(self, composition_id: int, *, output_path: str,
                      sha256: str, bytes_: int, duration_ms: int | None,
                      width: int | None, height: int | None) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE composition
                      SET output_path = ?, sha256 = ?, bytes = ?,
                          duration_ms = ?, width = ?, height = ?,
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""",
                (output_path, sha256, bytes_, duration_ms, width, height,
                 composition_id))

    def count_for_project(self, project_id: int) -> int:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM composition WHERE project_id = ?",
                (project_id,)).fetchone()
            return int(row["c"] or 0)


compositions = CompositionRepository()
