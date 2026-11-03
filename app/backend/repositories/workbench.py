"""首页工作台的跨模块只读聚合。

这个模块只回答两件 SQL 就能回答的事：作业按状态还剩几条、产线还有几条没收尾。
**审核待签的两个数字不在这里算**——文档要读盘取版本、产物要按清单算摘要，
这两件事在 `services` 里已经各有一个定义（`read_content` / `artifact_version`），
放到 SQL 里另写一遍就是第二个真相源，迟早和审核中心对不上。

`project_ids` 由调用方按可见性筛好再传进来：本模块不做权限判断，
免得"哪一步该挡人"散在两个地方。
"""
from __future__ import annotations

from db import get_conn


class WorkbenchRepository:
    def job_status_counts(self, project_ids: list[int]) -> dict[int, dict[str, int]]:
        """失败与待核对各几条。

        两个状态是两回事，所以要分开数、不能合并：`failed` 是队列侧的失败，
        `needs_attention` 是等人结清账单的那一档（见跨模块规则「失败不伪装」）。
        """
        if not project_ids:
            return {}
        marks = ",".join("?" for _ in project_ids)
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT project_id, status, COUNT(*) AS n
                      FROM job
                     WHERE project_id IN ({marks})
                       AND status IN ('failed', 'needs_attention')
                     GROUP BY project_id, status""", project_ids).fetchall()
        out: dict[int, dict[str, int]] = {}
        for row in rows:
            out.setdefault(int(row["project_id"]), {})[row["status"]] = int(row["n"])
        return out

    def active_run_counts(self, project_ids: list[int]) -> dict[int, int]:
        """还没收尾、也没被取消的产线运行。

        取消的运行即使没写 `finished_at` 也不算"进行中"——它的节点都已经收了，
        再把它算进待办只会让人去点一条已经没有下一步的运行。
        """
        if not project_ids:
            return {}
        marks = ",".join("?" for _ in project_ids)
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT project_id, COUNT(*) AS n
                      FROM pipeline_run
                     WHERE project_id IN ({marks})
                       AND finished_at IS NULL AND cancelled = 0
                     GROUP BY project_id""", project_ids).fetchall()
        return {int(row["project_id"]): int(row["n"]) for row in rows}


workbench = WorkbenchRepository()
