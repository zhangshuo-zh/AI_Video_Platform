"""项目生产工作台的聚合读（0037 之后新增，只读不写）。

为什么要单独一个仓储而不是把 SQL 写进 `production.py`：这一层的价值就是
"一条集一次查完"。工作台每次打开一集都要算九个阶段，如果按阶段各打各的查询，
一集 13 镜会变成几十趟库——和 v0.102 那轮把资产列表从相关子查询改派生表
是同一件事，别再犯第二次。

三条口径纪律：

1. **只数已有的事实**，不预测。`pending`／`failed`／`blocked` 全部来自
   `job.status`、`review_item.decision`、`asset.ref_images` 这些库里真有的列；
   "预计等待时间"这种算不出来的东西一律不给字段（任务书 §7.1 资源检查也写了
   "仅在能够可靠计算时展示"）。
2. **不复用别模块的算法**，只调用它们的出口：源版本判定走
   `services.shots_view()`，签字走 `repositories.reviews`，取片规则走
   `composition.timeline`。这里出现的 SQL 只负责"把行捞齐并数一遍"。
3. **集与项目不混**：资产是项目级的（可跨集复用），所以缺参考图这类计数
   按项目算，镜头／作业／成片按集算。工作台必须说清这个数字是谁的。
"""
from __future__ import annotations

import sqlite3

from db import get_conn


class ProductionRepository:
    # ---------- 引用关系（影响分析用） ----------

    def referencing_shot_rows(self, asset_id: int) -> list[sqlite3.Row]:
        """把这条资产指到分镜上的镜头，**直接引用与形态引用一次查齐**。

        为什么要 UNION 成一条：`assets.referencing_shots()` 与
        `variants.referencing_shots()` 各回一边，而后者只带 shot_id 回来——
        影响分析要的是"哪一集第几镜"，分两次查再拼就是把 N+1 换个地方写。
        两条都走 `json_each` 而不是 LIKE：LIKE 会把 12 匹配进 112／120，
        引用关系错了比没有更糟。
        """
        sql = """
            SELECT DISTINCT s.id AS shot_id, s.seq AS seq, s.episode_id AS episode_id,
                   e.project_id AS project_id
              FROM shot s
              JOIN episode e ON e.id = s.episode_id
             WHERE (json_valid(s.asset_ids)
                    AND EXISTS (SELECT 1 FROM json_each(s.asset_ids) j
                                 WHERE CAST(j.value AS INTEGER) = ?))
                OR (json_valid(s.variant_ids)
                    AND EXISTS (SELECT 1 FROM json_each(s.variant_ids) v
                                  JOIN asset_variant av ON av.id = CAST(v.value AS INTEGER)
                                 WHERE av.asset_id = ?))
             ORDER BY e.project_id, s.episode_id, s.seq"""
        with get_conn() as conn:
            return conn.execute(sql, (asset_id, asset_id)).fetchall()

    # ---------- 集内文档与解析 ----------

    def documents_by_kind(self, episode_id: int) -> dict[str, int]:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT kind, COUNT(*) AS n FROM document
                    WHERE episode_id = ? GROUP BY kind""", (episode_id,)).fetchall()
        return {r["kind"]: int(r["n"]) for r in rows}

    def pending_extraction_items(self, episode_id: int) -> dict:
        """这一集的文档里，还没被人处置过的候选有多少。

        走 `extraction_run.doc_id → document.episode_id`：候选本身不挂集，
        它挂的是"哪一次抽取"，而抽取挂的是文档。中间那一跳不能省，
        否则会把同一项目别的集的候选也算进来。
        """
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS pending,
                          SUM(CASE WHEN i.kind = 'character' THEN 1 ELSE 0 END) AS characters,
                          SUM(CASE WHEN i.kind = 'scene' THEN 1 ELSE 0 END) AS scenes,
                          SUM(CASE WHEN i.kind = 'prop' THEN 1 ELSE 0 END) AS props
                     FROM extraction_item i
                     JOIN extraction_run r ON r.id = i.run_id
                     JOIN document d ON d.id = r.doc_id
                    WHERE d.episode_id = ? AND i.status = 'pending'""",
                (episode_id,)).fetchone()
        return {"pending": int(row["pending"] or 0),
                "characters": int(row["characters"] or 0),
                "scenes": int(row["scenes"] or 0),
                "props": int(row["props"] or 0)}

    def extraction_runs(self, episode_id: int) -> dict:
        """跑过几次抽取、一共出过多少候选、其中多少已经处置完。

        工作台要能区分"没解析过"和"解析过且都处理完"——只看 pending 数做不到，
        两者都是 0。
        """
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT r.id) AS runs,
                          COUNT(i.id) AS items,
                          SUM(CASE WHEN i.status = 'pending' THEN 1 ELSE 0 END) AS pending
                     FROM extraction_run r
                     JOIN document d ON d.id = r.doc_id
                     LEFT JOIN extraction_item i ON i.run_id = r.id
                    WHERE d.episode_id = ?""", (episode_id,)).fetchone()
        return {"runs": int(row["runs"] or 0), "items": int(row["items"] or 0),
                "pending": int(row["pending"] or 0)}

    # ---------- 项目级资产 ----------

    def asset_gaps(self, project_id: int) -> dict:
        """资产准备的缺口：总数、没有参考图的、没有一致性提示词的、没有任何形态的人物。

        `ref_images` 存的是 JSON 文本，空清单是 `'[]'` 而不是空串——
        直接 `= ''` 判会永远判不出"没有参考图"（v0.96 就在生产前检查上撞过一次）。
        """
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN ref_images IN ('', '[]', 'null') THEN 1 ELSE 0 END)
                              AS no_ref_image,
                          SUM(CASE WHEN TRIM(COALESCE(consistency_prompt, '')) = ''
                              THEN 1 ELSE 0 END) AS no_consistency
                     FROM asset WHERE project_id = ?""", (project_id,)).fetchone()
            with get_conn() as conn:
                characters = conn.execute(
                    """SELECT COUNT(*) AS n FROM asset a
                        WHERE a.project_id = ? AND a.kind = 'character'
                          AND NOT EXISTS (SELECT 1 FROM asset_variant v WHERE v.asset_id = a.id)""",
                    (project_id,)).fetchone()
        return {"total": int(row["total"] or 0),
                "no_ref_image": int(row["no_ref_image"] or 0),
                "no_consistency": int(row["no_consistency"] or 0),
                "character_without_variant": int(characters["n"] or 0)}

    # ---------- 镜头作业 ----------

    def referenced_asset_gaps(self, episode_id: int) -> dict:
        """这一集的镜头引用到的资产里，缺参考图／缺一致性提示词／缺形态的各有多少。

        项目级 `asset_gaps()` 回答"这个项目的资产池健不健康"，
        这里回答"这一集要用的资产够不够"——两个数不一样，界面上必须分开标，
        不然会出现"资产缺口 3，可这一集明明一个都没引用"。
        引用关系在 `shot.asset_ids` 那个 JSON 数组里，用 `json_each` 摊平一次。
        """
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT a.id) AS total,
                          SUM(CASE WHEN a.ref_images IN ('', '[]', 'null') THEN 1 ELSE 0 END)
                              AS no_ref_image,
                          SUM(CASE WHEN TRIM(COALESCE(a.consistency_prompt, '')) = ''
                              THEN 1 ELSE 0 END) AS no_consistency
                     FROM shot s
                     JOIN asset a
                       ON a.id IN (SELECT CAST(j.value AS INTEGER)
                                     FROM json_each(s.asset_ids) j
                                    WHERE json_valid(s.asset_ids))
                    WHERE s.episode_id = ?""", (episode_id,)).fetchone()
            with get_conn() as conn:
                missing = conn.execute(
                    """SELECT COUNT(*) AS n FROM shot s
                        WHERE s.episode_id = ? AND json_valid(s.asset_ids)
                          AND EXISTS (SELECT 1 FROM json_each(s.asset_ids) j
                                WHERE CAST(j.value AS INTEGER) NOT IN (SELECT id FROM asset))""",
                    (episode_id,)).fetchone()
        return {"total": int(row["total"] or 0),
                "no_ref_image": int(row["no_ref_image"] or 0),
                "no_consistency": int(row["no_consistency"] or 0),
                "shots_with_missing_asset": int(missing["n"] or 0)}

    def shot_gaps(self, episode_id: int) -> dict:
        """分镜本身的完整度：几镜、缺提示词几镜、没引用任何资产几镜、没时长几镜。"""
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN TRIM(COALESCE(prompt, '')) = ''
                              AND TRIM(COALESCE(summary, '')) = '' THEN 1 ELSE 0 END) AS empty,
                          SUM(CASE WHEN TRIM(COALESCE(prompt, '')) = '' THEN 1 ELSE 0 END)
                              AS no_prompt,
                          SUM(CASE WHEN asset_ids IN ('', '[]', 'null') THEN 1 ELSE 0 END)
                              AS no_refs
                     FROM shot WHERE episode_id = ?""", (episode_id,)).fetchone()
        return {"total": int(row["total"] or 0), "empty": int(row["empty"] or 0),
                "no_prompt": int(row["no_prompt"] or 0),
                "no_refs": int(row["no_refs"] or 0)}

    def job_status_by_shot(self, episode_id: int) -> dict[str, dict[str, int]]:
        """这一集每条镜头的最新作业状态，按能力归堆。

        返回 `{"video": {"succeeded": 9, "failed": 1, ...}, "image": {...}, "text": {...}}`。
        一次 GROUP BY 拿全，不在 Python 里逐镜查。
        """
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT j.capability AS capability, j.status AS status,
                          COUNT(DISTINCT j.shot_id) AS shots, COUNT(*) AS n
                     FROM job j
                     JOIN shot s ON s.id = j.shot_id
                    WHERE s.episode_id = ?
                    GROUP BY j.capability, j.status""", (episode_id,)).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["capability"], {})[r["status"]] = {
                "shots": int(r["shots"]), "jobs": int(r["n"])}
        return out

    def shots_without_video(self, episode_id: int) -> list[int]:
        """一条成功视频作业都没有的镜头 id（按镜号排序）。"""
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT s.id FROM shot s
                    WHERE s.episode_id = ?
                      AND NOT EXISTS (SELECT 1 FROM job j
                            WHERE j.shot_id = s.id AND j.capability = 'video'
                              AND j.status = 'succeeded')
                    ORDER BY s.seq""", (episode_id,)).fetchall()
        return [int(r["id"]) for r in rows]

    def shots_with_stale_video(self, episode_id: int) -> list[int]:
        """有成功视频、但最新那条成功视频之外还有失败或过期风险的镜头——
        真正的"源已变"判定不在这里做（那是 `services.shots_view()` 的活），
        这里只把候选镜头捞出来给上层去比。"""
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT s.id FROM shot s
                     JOIN job j ON j.shot_id = s.id
                    WHERE s.episode_id = ? AND j.capability = 'video'
                      AND j.status = 'failed'
                    ORDER BY s.id""", (episode_id,)).fetchall()
        return [int(r["id"]) for r in rows]

    def unsigned_succeeded_videos(self, episode_id: int) -> list[dict]:
        """成功但还没有「通过」签字的视频作业——审核验收阶段的待办清单。"""
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT j.id AS job_id, j.shot_id AS shot_id, s.seq AS seq
                     FROM job j
                     JOIN shot s ON s.id = j.shot_id
                    WHERE s.episode_id = ? AND j.capability = 'video'
                      AND j.status = 'succeeded'
                      AND NOT EXISTS (SELECT 1 FROM review_item r
                            WHERE r.target_kind = 'job' AND r.target_id = j.id
                              AND r.decision = 'approved')
                    ORDER BY s.seq""", (episode_id,)).fetchall()
        return [{"job_id": int(r["job_id"]), "shot_id": int(r["shot_id"]),
                 "seq": int(r["seq"])} for r in rows]

    def attention_jobs(self, episode_id: int) -> int:
        """待人工核对的作业条数（可能重复计费，不能盲重试）。"""
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM job
                    WHERE episode_id = ?
                      AND (status = 'needs_attention'
                           OR (execution_mode = 'cloud'
                               AND billable_status = 'pending_reconciliation'
                               AND status IN ('succeeded','failed','cancelled')))""",
                (episode_id,)).fetchone()
        return int(row["n"] or 0)

    # ---------- 音频 ----------

    def audio_status(self, episode_id: int) -> dict[str, dict[str, int]]:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT kind, status, COUNT(*) AS n FROM audio_clip
                    WHERE episode_id = ? GROUP BY kind, status""",
                (episode_id,)).fetchall()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["kind"], {})[r["status"]] = int(r["n"])
        return out

    def shots_without_dialogue(self, episode_id: int) -> int:
        """有台词正文但没登记配音素材的镜头数——留给"音频制作"阶段说人话用。"""
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM shot s
                    WHERE s.episode_id = ?
                      AND TRIM(COALESCE(s.summary, '')) <> ''
                      AND NOT EXISTS (SELECT 1 FROM audio_clip c
                            WHERE c.shot_id = s.id AND c.kind IN ('dialogue','narration'))""",
                (episode_id,)).fetchone()
        return int(row["n"] or 0)

    # ---------- 成片 ----------

    def latest_composition(self, episode_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                """SELECT c.*, j.status AS job_status
                     FROM composition c
                     LEFT JOIN job j ON j.id = c.job_id
                    WHERE c.episode_id = ?
                    ORDER BY c.version DESC LIMIT 1""", (episode_id,)).fetchone()

    def composition_delivery(self, episode_id: int) -> dict:
        """成片与交付的账：几版、最新一版跑到什么状态、有没有签过字。

        "最新一版有没有签字"必须单独数：`export_review` 节点签的是那条合成作业，
        而成片行本身没有状态列（状态真相在 job 上，这是 §1 不变量）。
        """
        with get_conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS versions,
                          MAX(version) AS latest_version
                     FROM composition WHERE episode_id = ?""", (episode_id,)).fetchone()
            latest = self.latest_composition(episode_id)
            signed = 0
            if latest is not None and latest["job_id"] is not None:
                with get_conn() as conn:
                    signed = int(conn.execute(
                        """SELECT COUNT(*) AS n FROM review_item
                            WHERE target_kind = 'job' AND target_id = ?
                              AND decision = 'approved'""",
                        (latest["job_id"],)).fetchone()["n"])
        return {"versions": int(row["versions"] or 0),
                "latest_version": int(row["latest_version"] or 0),
                "latest_status": (latest["job_status"] or "") if latest else "",
                "latest_output": (latest["output_path"] or "") if latest else "",
                "signed": signed}

    def composition_count(self, project_id: int) -> int:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM composition WHERE project_id = ?",
                (project_id,)).fetchone()
        return int(row["n"] or 0)


production = ProductionRepository()
