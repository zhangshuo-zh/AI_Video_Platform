"""项目看板的只读聚合（W16）。所有数字都从权威表现算，不存第二份状态。"""
from __future__ import annotations

import sqlite3

from db import get_conn
from repositories.jobs import BILLABLE_WHERE


class DashboardRepository:
    def summary(self, project_id: int) -> dict:
        with get_conn() as conn:
            episodes = {row["status"]: row["n"] for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM episode WHERE project_id = ? GROUP BY status",
                (project_id,))}
            shots = conn.execute(
                """SELECT COUNT(*) AS n FROM shot s JOIN episode e ON e.id = s.episode_id
                    WHERE e.project_id = ?""", (project_id,)).fetchone()["n"]
            assets = conn.execute(
                "SELECT COUNT(*) AS n FROM asset WHERE project_id = ?",
                (project_id,)).fetchone()["n"]
            jobs = {row["status"]: row["n"] for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM job WHERE project_id = ? GROUP BY status",
                (project_id,))}
            costs = [dict(row) for row in conn.execute(
                # 金额口径不在这里第二遍写：`BILLABLE_WHERE` 是成本账那一条，
                # 看板少了 `currency IS NOT NULL` 时会多出一行 NULL 币种的"开销"，
                # 两处数字对不上，人就分不清哪个是假的。
                f"""SELECT currency, SUM(cost_micros) AS micros, COUNT(*) AS jobs
                     FROM job
                    WHERE project_id = ? AND {BILLABLE_WHERE}
                    GROUP BY currency ORDER BY currency""", (project_id,))]
            gates = {row["status"]: row["n"] for row in conn.execute(
                """SELECT status, COUNT(*) AS n FROM review_gate g
                    WHERE project_id = ? AND g.id = (
                        SELECT MAX(id) FROM review_gate WHERE doc_id = g.doc_id)
                    GROUP BY status""", (project_id,))}
            signs = {row["decision"]: row["n"] for row in conn.execute(
                "SELECT decision, COUNT(*) AS n FROM review_item WHERE project_id = ? "
                "GROUP BY decision", (project_id,))}
            audio = {row["status"]: row["n"] for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM audio_clip WHERE project_id = ? "
                "GROUP BY status", (project_id,))}
            docs = conn.execute(
                "SELECT COUNT(*) AS n FROM document WHERE project_id = ?",
                (project_id,)).fetchone()["n"]
        return {
            "episodes": episodes, "episode_total": sum(episodes.values()),
            "shots": shots, "assets": assets, "documents": docs,
            "jobs": jobs, "job_total": sum(jobs.values()),
            "costs": costs, "gates": gates, "signs": signs, "audio": audio,
        }

    def shot_export(self, project_id: int) -> list[sqlite3.Row]:
        """镜头清单 + 每镜最近一条作业的状态/产物。导出不写任何状态。"""
        with get_conn() as conn:
            return conn.execute(
                """SELECT e.seq AS episode_seq, e.title AS episode_title,
                          s.seq, s.summary, s.prompt, s.duration, s.asset_ids,
                          (SELECT GROUP_CONCAT(a.name, '、')
                             FROM json_each(s.asset_ids) j
                             JOIN asset a ON a.id = j.value) AS asset_names,
                          (SELECT j2.id FROM job j2 WHERE j2.shot_id = s.id
                            ORDER BY j2.id DESC LIMIT 1) AS job_id,
                          (SELECT j2.status FROM job j2 WHERE j2.shot_id = s.id
                            ORDER BY j2.id DESC LIMIT 1) AS job_status,
                          (SELECT j2.result_paths_json FROM job j2 WHERE j2.shot_id = s.id
                            ORDER BY j2.id DESC LIMIT 1) AS job_paths
                     FROM shot s JOIN episode e ON e.id = s.episode_id
                    WHERE e.project_id = ?
                    ORDER BY e.seq, s.seq""", (project_id,)).fetchall()


dashboard = DashboardRepository()
