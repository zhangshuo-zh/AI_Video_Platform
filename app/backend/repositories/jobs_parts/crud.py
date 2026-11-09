from __future__ import annotations

import json
import sqlite3

from db import get_conn

from .common import _like_escape, clamp_limit


class JobsCrudMixin:
    def create(self, data: dict) -> sqlite3.Row:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO job
                   (project_id, episode_id, shot_id, capability, kind, execution_mode,
                    provider_code, model_code, adapter_version, prompt_snapshot,
                    params_json, input_refs_json, status, priority, idempotency_key,
                    user_confirmed_cloud, quantity, unit, unit_price_micros,
                    cost_micros, currency, pricing_snapshot_json, billable_status,
                    source_version, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'queued',
                           ?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data["project_id"], data.get("episode_id"), data.get("shot_id"),
                 data["capability"], data["kind"], data["execution_mode"],
                 data["provider_code"], data["model_code"],
                 data.get("adapter_version", ""), data.get("prompt_snapshot", ""),
                 json.dumps(data.get("params", {}), ensure_ascii=False, sort_keys=True),
                 json.dumps(data.get("input_refs", []), ensure_ascii=False),
                 int(data.get("priority", 0)), data.get("idempotency_key"),
                 int(bool(data.get("user_confirmed_cloud"))),
                 data.get("quantity"), data.get("unit"),
                 data.get("unit_price_micros"), data.get("cost_micros"),
                 data.get("currency"),
                 json.dumps(data["pricing_snapshot"], ensure_ascii=False, sort_keys=True)
                 if data.get("pricing_snapshot") else None,
                 data.get("billable_status", "not_billable"),
                 data.get("source_version", ""),
                 data.get("created_by")),
            )
            return conn.execute("SELECT * FROM job WHERE id = ?",
                                (cur.lastrowid,)).fetchone()


    def get(self, job_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(
                """SELECT j.*,
                          (SELECT COUNT(*) FROM job r
                            WHERE r.retry_of_job_id = j.id) AS retry_count,
                          (SELECT u.display_name FROM app_user u
                            WHERE u.id = j.reconciled_by) AS reconciled_by_name
                     FROM job j WHERE j.id = ?""", (job_id,)).fetchone()


    def get_many(self, job_ids: list[int] | set[int], *, with_derived: bool = True
                 ) -> dict[int, sqlite3.Row]:
        """一次读回一组作业，键是 id。缺的 id 不出现在结果里。

        列表类接口本来就攒好了一串 id（成片列表要每条的行内 Job、审核队列要
        每条的作业行），逐条 `get()` 就是把一次列表打成 N 次回库。
        `with_derived` 决定要不要带 `retry_count`／`reconciled_by_name` 那两个
        相关子查询——它们是**给队列页显示的**，别的调用方（成片列表）用不上，
        那就别为此多扫两趟。
        """
        ids = sorted({int(i) for i in job_ids if i is not None})
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        derived = """(SELECT COUNT(*) FROM job r WHERE r.retry_of_job_id = j.id)
                        AS retry_count,
                     (SELECT u.display_name FROM app_user u
                       WHERE u.id = j.reconciled_by) AS reconciled_by_name"""
        select = (f"SELECT j.*, {derived} FROM job j" if with_derived
                  else "SELECT j.* FROM job j")
        with get_conn() as conn:
            rows = conn.execute(f"{select} WHERE j.id IN ({marks})", ids).fetchall()
        return {int(row["id"]): row for row in rows}


    def by_key(self, key: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM job WHERE idempotency_key = ?",
                                (key,)).fetchone()


    def list(self, project_ids: list[int] | None, status: str | None = None,
             capability: str | None = None, limit: int = 100,
             statuses: list[str] | None = None, execution_mode: str | None = None,
             keyword: str | None = None, episode_id: int | None = None,
             shot_id: int | None = None,
             refs_prefix: str | None = None,
             before_id: int | None = None) -> list[sqlite3.Row]:
        """project_ids=None 表示管理员看全部；空列表表示一个都看不见。

        status 与 statuses 是同一件事的两种写法：前者给旧调用方与深链，
        后者给生成中心的多状态筛选（"进行中"＝三个状态一起看）。
        episode_id/shot_id 给创作中心的分镜表用：一集的作业一次取回，
        前端再按 shot_id 分组，避免每行镜头各打一次接口。

        `refs_prefix` 是给"归属写在 input_refs 里"的那类作业用的下推过滤
        （资产生成用 `["op:..."` 开头，见 `asset_studio.build_refs`）。
        为什么值得开这一个参数而不是让调用方自己多取几行再筛：
        同一项目里视频／文档 AI 作业一多，"取最近 N 条再筛"会把要的几条整批挤出窗口，
        界面于是显示"还没有生成记录"——那是**假空态**，不是没数据。
        别的写家不会误命中：docai 存的是对象数组（`[{"doc_id"...`）、
        成片与产线存的是路径数组（`["data/jobs/...`），都不以 `["op:` 开头。

        `before_id` 是队列翻页的游标：只回 id 严格小于它的行（列表本来就是
        `ORDER BY id DESC`）。用游标而不是 offset，是因为 queue 每 3 秒轮询一次、
        期间还在不断入队——offset 翻页会让新入队的行把整页往下挤，人翻第二页时
        会看到第一页末尾那几条重复出现。游标钉在 id 上，新行只会出现在更上面。
        """
        if project_ids is not None and not project_ids:
            return []
        conds: list[str] = []
        args: list = []
        if project_ids is not None:
            marks = ",".join("?" for _ in project_ids)
            conds.append(f"project_id IN ({marks})")
            args += list(project_ids)
        wanted = list(statuses or ([] if not status else [status]))
        if wanted:
            marks = ",".join("?" for _ in wanted)
            conds.append(f"status IN ({marks})")
            args += wanted
        if capability:
            conds.append("capability = ?")
            args.append(capability)
        if execution_mode:
            conds.append("execution_mode = ?")
            args.append(execution_mode)
        if episode_id is not None:
            conds.append("episode_id = ?")
            args.append(episode_id)
        if shot_id is not None:
            conds.append("shot_id = ?")
            args.append(shot_id)
        if refs_prefix:
            # 前缀里出现 LIKE 的通配符要转义掉：`%` 混进来会让过滤悄悄变宽，
            # 而那正好是这条存在的理由（宁可少给）的反面
            conds.append("""input_refs_json LIKE ? ESCAPE '\\'""")
            args.append(_like_escape(refs_prefix) + "%")
        if keyword:
            # 只搜 id 与提示词：日志/产物正文不在这张表里，不能假装搜得到。
            # keyword 与 refs_prefix 守同一条纪律：通配符一律转义后再进 LIKE。
            conds.append("""(CAST(id AS TEXT) = ? OR prompt_snapshot LIKE ? ESCAPE '\\')""")
            args += [keyword, f"%{_like_escape(keyword)}%"]
        if before_id is not None:
            # 严格小于：游标本身就是上一页最后一行，不能再回它
            conds.append("id < ?")
            args.append(int(before_id))
        args.append(clamp_limit(limit))
        # 管理员看全部、又没加任何筛选时 conds 是空的；直接拼会得到
        # "WHERE  ORDER BY" 这种语法错误（W08 真浏览器第一轮就打出来了）
        where = " AND ".join(conds) or "1"
        with get_conn() as conn:
            # retry_count / reconciled_by_name 是列表要直接显示的派生值。
            # 用相关子查询一次带出，而不是前端拿到 200 行后再逐行问"你被重试过几次"。
            return conn.execute(
                f"""SELECT j.*,
                          (SELECT COUNT(*) FROM job r
                            WHERE r.retry_of_job_id = j.id) AS retry_count,
                          (SELECT u.display_name FROM app_user u
                            WHERE u.id = j.reconciled_by) AS reconciled_by_name
                     FROM job j WHERE {where}
                     ORDER BY j.id DESC LIMIT ?""", args).fetchall()


    def count_listed(self, project_ids: list[int] | None, status: str | None = None,
                     capability: str | None = None,
                     statuses: list[str] | None = None, execution_mode: str | None = None,
                     keyword: str | None = None, episode_id: int | None = None,
                     shot_id: int | None = None,
                     refs_prefix: str | None = None) -> int:
        """`list` 的**同口径**总数，给队列翻页当分母。

        为什么不复用 `cost_summary` 的 `total_jobs`：那个数只按项目范围过滤，
        不知道 q／status／capability／mode。加了筛选之后两者就不是一回事——
        界面上会出现「筛选出 3 条／共 412」这种把两套口径并列给人看的假分母。
        （`generate.js` 早先那段注释已经点到这件事，当时的处理是「拿不准就不显示」，
        现在有了同口径计数，可以真的显示。）

        条件拼装与 `list` 保持逐条对应：这一段重复是刻意的——把 where 抽成共享
        函数会让 `list` 的语义与计数耦合，而 `list` 有 12 个调用方。这里只读地
        复制同一套判据，任何一边加筛选时另一边的测试会立刻红（见 test_jobs_count.py）。
        """
        if project_ids is not None and not project_ids:
            return 0
        conds: list[str] = []
        args: list = []
        if project_ids is not None:
            marks = ",".join("?" for _ in project_ids)
            conds.append(f"project_id IN ({marks})")
            args += list(project_ids)
        wanted = list(statuses or ([] if not status else [status]))
        if wanted:
            marks = ",".join("?" for _ in wanted)
            conds.append(f"status IN ({marks})")
            args += wanted
        if capability:
            conds.append("capability = ?")
            args.append(capability)
        if execution_mode:
            conds.append("execution_mode = ?")
            args.append(execution_mode)
        if episode_id is not None:
            conds.append("episode_id = ?")
            args.append(episode_id)
        if shot_id is not None:
            conds.append("shot_id = ?")
            args.append(shot_id)
        if refs_prefix:
            conds.append("""input_refs_json LIKE ? ESCAPE '\\'""")
            args.append(_like_escape(refs_prefix) + "%")
        if keyword:
            conds.append("""(CAST(id AS TEXT) = ? OR prompt_snapshot LIKE ? ESCAPE '\\')""")
            args += [keyword, f"%{_like_escape(keyword)}%"]
        where = " AND ".join(conds) or "1"
        with get_conn() as conn:
            return int(conn.execute(
                f"SELECT COUNT(*) FROM job WHERE {where}", args).fetchone()[0])


    def ids_for_project(self, project_id: int) -> list[int]:
        with get_conn() as conn:
            return [r["id"] for r in conn.execute(
                "SELECT id FROM job WHERE project_id = ?", (project_id,))]


    def status_of(self, job_id: int) -> str | None:
        with get_conn() as conn:
            row = conn.execute("SELECT status FROM job WHERE id = ?",
                               (job_id,)).fetchone()
        return row["status"] if row else None


    def counts(self) -> dict:
        with get_conn() as conn:
            row = conn.execute(
                """SELECT
                     SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
                     SUM(CASE WHEN status IN ('starting','running','cancel_requested')
                              THEN 1 ELSE 0 END) AS running
                   FROM job"""
            ).fetchone()
        return {"queued": int(row["queued"] or 0), "running": int(row["running"] or 0)}

    @staticmethod
    def _scope(project_ids: list[int] | None) -> tuple[str, list]:
        if project_ids is None:
            return "1", []
        if not project_ids:
            return "0", []
        marks = ",".join("?" for _ in project_ids)
        return f"project_id IN ({marks})", list(project_ids)
