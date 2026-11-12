from __future__ import annotations

import sqlite3

from asset_generation_refs import parse_refs
from config import RESOURCE_CLASSES
from db import get_conn

from .common import RESOURCE_CLASS_SQL, clamp_limit


class JobsQueryMixin:

    def recent_asset_generations(self, project_id: int, operations: tuple[str, ...],
                                 *, asset_id: int | None = None,
                                 limit: int = 12) -> list[sqlite3.Row]:
        """归属条件先于 LIMIT；沿用同一个解析器兼容旧 JSON 与重复引用。

        用连接局部的确定性 SQL 函数避免再实现一份 JSON 解析语义：旧引用的
        顺序、空白、数字写法和脏项处理都与预览一致。项目条件仍走现有索引，
        按 id 倒序返回（SQLite 可使用临时排序）；不取固定候选窗口，也不加索引。
        """
        wanted = frozenset(operations)
        target = int(asset_id) if asset_id is not None else None

        def matches(raw):
            ref = parse_refs({"input_refs_json": raw})
            return (ref["operation"] in wanted
                    and (target is None or ref["asset_id"] == target))

        with get_conn() as conn:
            conn.create_function("asset_generation_matches", 1, matches, deterministic=True)
            try:
                return conn.execute(
                    """SELECT * FROM job WHERE project_id = ?
                         AND asset_generation_matches(input_refs_json)
                       ORDER BY id DESC LIMIT ?""",
                    (int(project_id), clamp_limit(limit)),
                ).fetchall()
            finally:
                conn.create_function("asset_generation_matches", 1, None)


    def _latest_by_shot(self, episode_id: int, capability: str,
                        statuses: tuple[str, ...] | None = None
                        ) -> dict[int, sqlite3.Row]:
        """每镜 id 最大的那条作业；`statuses` 给了就只在这些状态里取。

        「这一镜用哪条」与「这一镜卡在哪」只差一个状态过滤，SQL 形状必须
        一模一样——各写一份，迟早一处按 `MAX(id)`、另一处按 `finished_at`。
        """
        sql = """SELECT j.* FROM job j
                   JOIN (SELECT shot_id, MAX(id) AS mid FROM job
                          WHERE episode_id = ? AND capability = ?
                            AND shot_id IS NOT NULL{extra}
                          GROUP BY shot_id) t ON t.mid = j.id"""
        args: list = [int(episode_id), capability]
        if statuses:
            marks = ",".join("?" for _ in statuses)
            sql = sql.format(extra=f" AND status IN ({marks})")
            args += list(statuses)
        else:
            sql = sql.format(extra="")
        with get_conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return {int(row["shot_id"]): row for row in rows}


    def latest_succeeded_videos_by_shot(self, episode_id: int) -> dict[int, sqlite3.Row]:
        """这一集每一镜**最新一条成功的视频作业**。键是 shot_id。

        这是"这一镜的素材是哪一条"的**唯一答案出处**：分镜列表的「源已变」和
        成片的前置检查都读它，两边必须说同一件事。原来这里是两条各写各的 SQL
        （分镜看"最新成功的任意作业"、成片看"最新成功的视频"），于是出现过
        "改完剧本后重抽了一张关键帧 → 分镜那行不贴「源已变」，成片却把这一镜
        拒了"——界面说没事、发起时才发现，正是这一列该避免的事。
        图像／文本的作业再新也不算素材：能进成片的是视频。

        失败的尝试不进来看——它本来就没有产物可用。
        """
        return self._latest_by_shot(episode_id, "video", ("succeeded",))


    def submission_counts(self, project_ids: list[int] | None) -> list[dict]:
        """谁提交了多少条作业（按 `created_by` 分组）。

        这一条只回答"谁按了提交"，不回答"谁干了多少活"：作业数不等于产量
        （一次批量重投会算成同一个人的多条），也不等于时长。界面上必须把这句
        限制一起显示出来——平台的可查事实只有 `created_by` 这类弱引用，
        没有操作审计（设计稿 §8 明写不做），把它当绩效读数就是编。
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
                f"""SELECT j.created_by AS user_id,
                           u.display_name AS name,
                           COUNT(*) AS n,
                           SUM(CASE WHEN j.status = 'succeeded' THEN 1 ELSE 0 END) AS ok,
                           SUM(COALESCE(j.elapsed_ms, 0)) AS busy_ms
                      FROM job j LEFT JOIN app_user u ON u.id = j.created_by
                     WHERE {where}
                     GROUP BY j.created_by, u.display_name
                     ORDER BY n DESC, j.created_by""", args).fetchall()
        return [dict(r) for r in rows]


    def recent_result_paths(self, project_id: int, *, capability: str = "video",
                            shot_bound: bool | None = True, provider: str | None = None,
                            exclude_provider: str | None = None,
                            limit: int = 20) -> list[str]:
        """这个项目最近若干条**成功作业**的产物清单原文（给磁盘估算取样）。

        三种典型大小要分开取，否则数字会自己骗自己：成片作业的 `capability` 也是
        `video`，混进"每镜平均"会把平均值抬高一到两个数量级（本机实测：一镜
        640×352 约 0.17 MB，一次成片约 5.6 MB）。所以镜头样本要求挂在具体镜头上
        且排除 `ffmpeg`，成片样本正好反过来。只取 `succeeded`——失败的作业没有
        可量的文件。
        """
        sql = ["SELECT result_paths_json FROM job WHERE project_id = ?",
               "AND capability = ? AND status = 'succeeded'"]
        args = [int(project_id), capability]
        if shot_bound is True:
            sql.append("AND shot_id IS NOT NULL")
        elif shot_bound is False:
            sql.append("AND shot_id IS NULL")
        if provider:
            sql.append("AND provider_code = ?")
            args.append(provider)
        if exclude_provider:
            sql.append("AND provider_code <> ?")
            args.append(exclude_provider)
        sql.append("ORDER BY id DESC LIMIT ?")
        args.append(int(limit))
        with get_conn() as conn:
            rows = conn.execute(" ".join(sql), args).fetchall()
        return [r["result_paths_json"] or "[]" for r in rows]


    def latest_jobs_by_shot(self, episode_id: int,
                            capability: str = "video") -> dict[int, sqlite3.Row]:
        """这一集每一镜的**最后一条**该能力的作业（不分状态）。键是 shot_id。

        为什么要单独这一条：「这一镜现在卡在什么状态」必须只有一个答案出处。
        批重投、下一步任务与统计都读它——各处自己写一遍"取最新一条"，
        就会出现一处按 `MAX(id)`、另一处按 `finished_at`，
        于是同一个镜头在这两个页面上一个说"卡住"、一个说"没事"。
        """
        return self._latest_by_shot(episode_id, capability)


    def queue_by_resource(self) -> dict:
        """队列里排队／执行中各资源类几条——面板用来解释"为什么还没轮到我"。"""
        with get_conn() as conn:
            rows = conn.execute(
                f"""SELECT {RESOURCE_CLASS_SQL} AS cls, status, COUNT(*) AS n
                      FROM job
                     WHERE status IN ('queued','starting','running',
                                      'cancel_requested')
                     GROUP BY cls, status"""
            ).fetchall()
        out = {name: {"queued": 0, "running": 0} for name in RESOURCE_CLASSES}
        for row in rows:
            bucket = out.setdefault(row["cls"], {"queued": 0, "running": 0})
            if row["status"] == "queued":
                bucket["queued"] += int(row["n"])
            else:
                bucket["running"] += int(row["n"])
        return out
