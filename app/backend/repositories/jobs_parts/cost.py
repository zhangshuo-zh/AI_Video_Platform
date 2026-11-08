from __future__ import annotations

from db import get_conn

from .common import BILLABLE_WHERE


class JobsCostMixin:

    def cost_summary(self, project_ids: list[int] | None) -> dict:
        """按币种 × 能力汇总。金额口径见 `BILLABLE_WHERE`，项目看板共用那一条。"""
        where, args = self._scope(project_ids)
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT currency, capability, billable_status,
                           SUM(cost_micros) AS total, COUNT(*) AS n
                      FROM job
                     WHERE {where}
                       AND {BILLABLE_WHERE}
                     GROUP BY currency, capability, billable_status
                     ORDER BY currency, capability""", args).fetchall()
            unpriced = conn.execute(
                f"""SELECT COUNT(*) AS n FROM job
                     WHERE {where} AND execution_mode = 'cloud'
                       AND (cost_micros IS NULL OR currency IS NULL)""", args).fetchone()["n"]
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM job WHERE {where}", args).fetchone()["n"]
            locals_ = conn.execute(
                f"""SELECT elapsed_ms, resources_json FROM job
                     WHERE {where} AND execution_mode = 'local'""", args).fetchall()
        return {"rows": [dict(r) for r in rows], "unpriced": int(unpriced or 0),
                "total_jobs": int(total or 0), "locals": [dict(r) for r in locals_]}


    # ---------- worker 心跳 ----------
    def stats_for_projects(self, project_ids: list[int] | None) -> dict:
        """项目范围的作业事实（P2 生产统计的读数出口）。

        为什么另开一个方法而不是让界面去拼 `cost_summary`：`cost_summary` 的口径是
        **成本**（`BILLABLE_WHERE` 只收计得上价的那几条），拿它当"跑了多少条、
  成功几条"会悄悄漏掉本地作业——本地不计费。这里按状态×能力如实数，
        金额仍然只从 `cost_summary` 那一条口径出（一个出处）。

        只读、单条聚合，不 JOIN 产物表：这条接口挂在工作台上，每次打开都跑。
        """
        where, args = self._scope(project_ids)
        with get_conn() as conn:
            by_group = conn.execute(
                f"""SELECT status, capability,
                           COUNT(*) AS n,
                           SUM(COALESCE(elapsed_ms, 0)) AS total_ms,
                           SUM(CASE WHEN elapsed_ms IS NOT NULL THEN 1 ELSE 0 END) AS timed,
                           MAX(elapsed_ms) AS max_ms
                      FROM job WHERE {where}
                     GROUP BY status, capability""", args).fetchall()
            codes = conn.execute(
                f"""SELECT error_code, COUNT(*) AS n FROM job
                     WHERE {where} AND status IN ('failed', 'needs_attention')
                       AND error_code IS NOT NULL AND error_code <> ''
                     GROUP BY error_code
                     ORDER BY n DESC, error_code LIMIT 12""", args).fetchall()
            # 重试的两端各数一次：`retry_of_job_id` 非空＝这一条是重投出来的，
            # `superseded_by_job_id` 非空＝这一条被后来的顶掉了。job 表没有
            # `retry_count` 那一列（那是产线节点上的），别拿节点口径来数作业。
            retried = conn.execute(
                f"""SELECT SUM(CASE WHEN retry_of_job_id IS NOT NULL THEN 1 ELSE 0 END) AS n,
                           SUM(CASE WHEN superseded_by_job_id IS NOT NULL THEN 1 ELSE 0 END) AS m
                      FROM job WHERE {where}""", args).fetchone()
            # 每条能力各自的**采样**峰值。`child_peak_rss_sampled_kb` 是 worker
            # 里 `_RssWatch` 按秒采的这棵子进程树峰值；老的行没有这一栏，`json_extract`
            # 给 NULL，于是那一档显示「没读数」而不是拿 `child_max_rss_kb`（生命周期值）冒充。
            peaks = conn.execute(
                f"""SELECT capability,
                           MAX(json_extract(resources_json,
                                '$.child_peak_rss_sampled_kb')) AS peak_kb,
                           SUM(CASE WHEN json_extract(resources_json,
                                '$.child_peak_rss_sampled_kb') IS NOT NULL
                                THEN 1 ELSE 0 END) AS n
                      FROM job
                     WHERE {where} AND status = 'succeeded'
                       AND execution_mode = 'local' AND json_valid(resources_json)
                     GROUP BY capability""", args).fetchall()
        return {"by_group": [dict(r) for r in by_group],
                "peaks": [dict(r) for r in peaks],
                "by_error_code": [dict(r) for r in codes],
                "retried": int(retried["n"] or 0),
                "superseded": int(retried["m"] or 0)}
