"""Job 队列的读写。队列只由这张表驱动，没有第二个物理队列。

并发要点：领取必须在一个 `BEGIN IMMEDIATE` 里做"查 + 改"，否则 API 刚插进去
两条、两个 worker 同时 SELECT 到同一条，就会重复执行。SQLite 的写锁很短，
这里也不在事务里等模型进程——领到就提交，执行在事务外。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime

from db import get_conn
from config import (RESOURCE_CLASSES, WORKER_MAX_GLOBAL,
                    WORKER_MAX_PER_RESOURCE)

# 一条作业占哪一类资源。只在 SQL 与 Python 各写一份表达式，口径一致：
#   * 成片合成不是模型：provider=ffmpeg 单独占 compose，不和视频模型抢显存；
#   * tts/music 都算音频腿（同一块内存与同一类子进程）；
#   * 其余按 capability 原样。
RESOURCE_CLASS_SQL = (
    "CASE WHEN provider_code = 'ffmpeg' THEN 'compose' "
    "WHEN capability IN ('tts','music','audio') THEN 'audio' "
    "ELSE capability END")


def resource_class(capability: str, provider_code: str = "") -> str:
    if (provider_code or "").lower() == "ffmpeg":
        return "compose"
    if capability in ("tts", "music", "audio"):
        return "audio"
    return capability or ""


def _loads(text: str):
    """读库里的 JSON 列；坏了就当空对象，不让一条坏行把整个面板打挂。"""
    try:
        value = json.loads(text or "null")
        return value if isinstance(value, (dict, list)) else {}
    except ValueError:
        return {}

# 字段清单集中在这里：建单、重试复制、测试断言都用同一份，免得漏一列
COPY_FIELDS = (
    "project_id", "episode_id", "shot_id", "capability", "kind", "execution_mode",
    "provider_code", "model_code", "adapter_version", "prompt_snapshot",
    "params_json", "input_refs_json", "priority", "user_confirmed_cloud",
    # 重试继承的是"这次大概是这个价"，不是"上次真的花了这么多"：
    # 原行的金额一律不动；新行的账单状态重新回到待核对。
    "quantity", "unit", "unit_price_micros", "cost_micros", "currency",
    "pricing_snapshot_json",
)


# 什么算"进了金额"。成本账（cost_summary）与项目看板（dashboard.summary）共用这一条，
# 两处各写一遍迟早会漂——看板原来就少了一个 `currency IS NOT NULL`，
# NULL 币种会被单独聚成一行，看上去像"有一笔没标币种的开销"，而成本账里根本没有它。
# 规则本身：本地作业是 not_billable，不进金额；`known` 是已核价，
# `pending_reconciliation` 是云端已出价但还没对账，两者都算。
BILLABLE_WHERE = ("cost_micros IS NOT NULL AND currency IS NOT NULL "
                  "AND billable_status IN ('known','pending_reconciliation')")


class JobRepository:
    def create(self, data: dict) -> sqlite3.Row:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO job
                   (project_id, episode_id, shot_id, capability, kind, execution_mode,
                    provider_code, model_code, adapter_version, prompt_snapshot,
                    params_json, input_refs_json, status, priority, idempotency_key,
                    user_confirmed_cloud, quantity, unit, unit_price_micros,
                    cost_micros, currency, pricing_snapshot_json, billable_status,
                    created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'queued',
                           ?,?,?,?,?,?,?,?,?,?,?)""",
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

    def by_key(self, key: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM job WHERE idempotency_key = ?",
                                (key,)).fetchone()

    def list(self, project_ids: list[int] | None, status: str | None = None,
             capability: str | None = None, limit: int = 100,
             statuses: list[str] | None = None, execution_mode: str | None = None,
             keyword: str | None = None, episode_id: int | None = None,
             shot_id: int | None = None) -> list[sqlite3.Row]:
        """project_ids=None 表示管理员看全部；空列表表示一个都看不见。

        status 与 statuses 是同一件事的两种写法：前者给旧调用方与深链，
        后者给生成中心的多状态筛选（"进行中"＝三个状态一起看）。
        episode_id/shot_id 给创作中心的分镜表用：一集的作业一次取回，
        前端再按 shot_id 分组，避免每行镜头各打一次接口。
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
        if keyword:
            # 只搜 id 与提示词：日志/产物正文不在这张表里，不能假装搜得到
            conds.append("(CAST(id AS TEXT) = ? OR prompt_snapshot LIKE ?)")
            args += [keyword, f"%{keyword}%"]
        args.append(min(max(limit, 1), 200))
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

    # ---------- 领取与执行 ----------
    def claim_next(self, worker_id: str, lease_seconds: int,
                   limits: dict | None = None) -> sqlite3.Row | None:
        """原子领取一条 queued。返回 None＝没有可领的，或并发槽位都占着。

        并发按**资源类**算，不按 worker 算：视频模型同时只跑一条（显存），
        图像／文本／音频／成片各有一条槽。默认全局 1、每类 1，与单 worker
        时代完全一致；要并行就在 `limits` 里抬上限，并且真的多开 worker
        进程——一个进程仍然只跑一条作业，这一点没有被"并发"改掉。

        `limits` = `{"global": 2, "per": {"video": 1, "image": 1, ...}}`。
        没给的类沿用配置默认值；0 表示这一类暂时不接活。
        """
        limits = limits or {}
        max_global = max(0, int(limits.get("global", WORKER_MAX_GLOBAL)))
        per = {**WORKER_MAX_PER_RESOURCE, **(limits.get("per") or {})}
        lease = f"+{int(lease_seconds)} seconds"
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 这里**不看租约是否过期**：租约过期只说明"上一个 worker 大概没了"，
            # 不等于这条作业已经收尾。先让 recover_stale 把它收成 failed(worker_lost)，
            # 再领下一条；否则会出现"过期但还挂着 running"的窗口里领进第二条同资源
            # 作业，同一时刻两个任务抢同一份显存——这条硬约束不能靠时间窗口赌。
            active_rows = conn.execute(
                f"""SELECT {RESOURCE_CLASS_SQL} AS cls, COUNT(*) AS n
                      FROM job
                     WHERE status IN ('starting','running','cancel_requested')
                     GROUP BY cls"""
            ).fetchall()
            active = {row["cls"]: int(row["n"]) for row in active_rows}
            if sum(active.values()) >= max_global:
                return None
            candidates = conn.execute(
                f"""SELECT id, {RESOURCE_CLASS_SQL} AS cls
                      FROM job
                     WHERE status = 'queued'
                     ORDER BY priority DESC, queued_at, id"""
            ).fetchall()
            row = next((r for r in candidates
                        if active.get(r["cls"], 0) < int(per.get(r["cls"], 1))),
                       None)
            if row is None:
                return None
            cur = conn.execute(
                """UPDATE job SET status = 'starting', worker_id = ?,
                          started_at = datetime('now','localtime'),
                          lease_until = datetime('now','localtime', ?),
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND status = 'queued'""",
                (worker_id, lease, row["id"]),
            )
            if cur.rowcount != 1:
                return None
            # 必须用同一个连接读回：另一个连接看不到这里还没提交的 UPDATE
            return conn.execute("SELECT * FROM job WHERE id = ?", (row["id"],)).fetchone()

    def renew(self, job_id: int, worker_id: str, lease_seconds: int) -> None:
        """续租 + 心跳。只续还在执行中的那条，结束了的不会复活。"""
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET lease_until = datetime('now','localtime', ?),
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND worker_id = ?
                      AND status IN ('starting','running','cancel_requested')""",
                (f"+{int(lease_seconds)} seconds", job_id, worker_id),
            )

    def set_running(self, job_id: int, worker_id: str) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET status = 'running', updated_at = datetime('now','localtime')
                    WHERE id = ? AND worker_id = ? AND status = 'starting'""",
                (job_id, worker_id),
            )

    @staticmethod
    def _resources(resources: dict | None) -> str:
        # 资源数据只增不减：失败与取消也要留——它们同样是"机器被占过"的证据
        return json.dumps(resources or {}, ensure_ascii=False, sort_keys=True)

    def succeed(self, job_id: int, worker_id: str, log_path: str,
                result_paths: list[str], elapsed_ms: int, return_code: int = 0,
                resources: dict | None = None,
                provider_request_id: str | None = None) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET status = 'succeeded', finished_at = datetime('now','localtime'),
                          elapsed_ms = ?, return_code = ?, log_path = ?,
                          result_paths_json = ?, error_code = NULL, error_message = NULL,
                          resources_json = ?, lease_until = NULL, worker_id = NULL,
                          provider_request_id = COALESCE(?, provider_request_id),
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND worker_id = ?""",
                (elapsed_ms, return_code, log_path,
                 json.dumps(result_paths, ensure_ascii=False),
                 self._resources(resources), provider_request_id, job_id, worker_id),
            )

    def fail(self, job_id: int, worker_id: str, error_code: str, message: str,
             log_path: str = "", return_code: int | None = None,
             elapsed_ms: int | None = None, resources: dict | None = None,
             provider_request_id: str | None = None) -> None:
        """失败**不动成本列**：一条云端作业失败时，供应商可能已经计过费；
        这里替人把金额清零，账就再也对不上了（数据字典 §5.3「禁止」）。"""
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET status = 'failed', finished_at = datetime('now','localtime'),
                          error_code = ?, error_message = ?, log_path = ?,
                          return_code = ?, elapsed_ms = ?, resources_json = ?,
                          provider_request_id = COALESCE(?, provider_request_id),
                          lease_until = NULL, worker_id = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND worker_id = ?""",
                (error_code, message[:2000], log_path, return_code, elapsed_ms,
                 self._resources(resources), provider_request_id, job_id, worker_id),
            )

    def cancelled(self, job_id: int, worker_id: str, message: str = "",
                  log_path: str = "", elapsed_ms: int | None = None,
                  resources: dict | None = None,
                  provider_request_id: str | None = None) -> None:
        """收尾成 cancelled。与 succeed／fail 同一道归属守卫。

        审计 R1：这一条原来是唯一只看 `id` 的终态写入。`recover_stale()` 收尾时
        会把 `worker_id` 置 NULL，所以"恢复之后旧 worker 回来覆盖终态"这条路在
        succeed／fail 上已经关死，只有 cancelled 还开着——危害有限（状态同为终态，
        被覆盖的是 `error_message`／`elapsed_ms`／`resources_json` 这三样收尾证据），
        但"谁收的尾"本来就该只有一个答案，补 `AND worker_id = ?` 让四条终态写入
        用同一条规则。调用方（worker.process）本来就传着自己那一份 wid。
        """
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET status = 'cancelled', finished_at = datetime('now','localtime'),
                          error_code = 'cancelled', error_message = ?, log_path = ?,
                          elapsed_ms = ?, resources_json = ?, lease_until = NULL, worker_id = NULL,
                          provider_request_id = COALESCE(?, provider_request_id),
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND worker_id = ?""",
                (message[:2000], log_path, elapsed_ms, self._resources(resources),
                 provider_request_id, job_id, worker_id),
            )

    def needs_attention(self, job_id: int, worker_id: str, error_code: str, message: str,
                        reason: str, log_path: str = "", return_code: int | None = None,
                        elapsed_ms: int | None = None, resources: dict | None = None,
                        provider_request_id: str | None = None) -> None:
        """云端结果可疑：供应商可能已经建了任务、也可能已经计了费。

        这与 `fail()` 的区别只有一件事——**不能让人以为重试是免费的**。
        所以成本列与 `billable_status` 一律不动，状态收成 `needs_attention`，
        由人在详情里核对；核对动作在 `reconcile()`，不自动重投。
        """
        with get_conn() as conn:
            conn.execute(
                """UPDATE job SET status = 'needs_attention',
                          finished_at = datetime('now','localtime'),
                          error_code = ?, error_message = ?, attention_reason = ?,
                          log_path = ?, return_code = ?, elapsed_ms = ?,
                          resources_json = ?,
                          provider_request_id = COALESCE(?, provider_request_id),
                          lease_until = NULL, worker_id = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND worker_id = ?""",
                (error_code, message[:2000], reason[:500], log_path, return_code,
                 elapsed_ms, self._resources(resources), provider_request_id,
                 job_id, worker_id),
            )

    def reconcile(self, job_id: int, decision: str, actual_cost_micros: int | None,
                  note: str, user_id: int | None) -> sqlite3.Row | None:
        """人工核对一条 `needs_attention` 的云端作业。

        三种决定：
        - `succeeded`：结果确认可用 → 状态 succeeded、账单 known。
        - `failed`：结果确认不可用 → 状态 failed，但账单仍 known（这笔钱已经算清了）。
        - `pending`：还没核清 → 只更新备注，状态与账单都不动。

        `succeeded`/`failed` 必须给实际金额：不给就等于替人猜"这次没花钱"，
        而那正是账本上最不该出现的一种猜测。
        """
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["status"] != "needs_attention":
                return row
            if decision == "pending":
                conn.execute(
                    """UPDATE job SET attention_reason = ?, reconcile_note = ?,
                              updated_at = datetime('now','localtime')
                        WHERE id = ?""",
                    (row["attention_reason"], (note or "")[:2000], job_id))
            elif decision in ("succeeded", "failed"):
                conn.execute(
                    """UPDATE job SET status = ?, actual_cost_micros = ?,
                              billable_status = 'known', reconciled_by = ?,
                              reconciled_at = datetime('now','localtime'),
                              reconcile_note = ?, updated_at = datetime('now','localtime')
                        WHERE id = ?""",
                    (decision, actual_cost_micros, user_id, (note or "")[:2000], job_id))
            else:
                return row
            return conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()

    def request_cancel(self, job_id: int) -> sqlite3.Row | None:
        """取消是"请求式"：排队中的当场取消，跑着的只记下意愿。"""
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "queued":
                # 排队中取消＝一次供应商调用都没发生，这笔钱不该进账。
                # 金额与定价快照仍留着（详情里能看见当初的预计），
                # 但账单状态改为不计费——成本账只汇总 known / pending。
                conn.execute(
                    """UPDATE job SET status = 'cancelled',
                              finished_at = datetime('now','localtime'),
                              error_code = 'cancelled', error_message = '提交后取消，未执行',
                              billable_status = CASE WHEN execution_mode = 'cloud'
                                                     THEN 'not_billable'
                                                     ELSE billable_status END,
                              updated_at = datetime('now','localtime') WHERE id = ?""",
                    (job_id,),
                )
            elif row["status"] in ("starting", "running"):
                conn.execute(
                    """UPDATE job SET status = 'cancel_requested',
                              updated_at = datetime('now','localtime') WHERE id = ?""",
                    (job_id,),
                )
            return conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()

    def retry(self, original: sqlite3.Row, created_by: int | None) -> sqlite3.Row:
        """重试＝新建一行，原行的日志／成本／错误一个字段都不动。

        两件事顺手补上（P0）：
        - `attempt` 终于写起来了。它不是执行次数计数器，而是"这一串里的第几枪"，
          界面上要能说出"这已经是第 4 次预计计费"，而不是摆 4 条互不相干的待核对。
        - 原行记下 `superseded_by_job_id`。没有这一位，重试链只能从新行往回找，
          人看成本账时看不出"哪几笔其实是同一件事"。
        """
        key = "retry-" + uuid.uuid4().hex
        cloud = original["execution_mode"] == "cloud"
        billable = "pending_reconciliation" if cloud and original["unit_price_micros"] is not None \
            else "not_billable"
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO job
                   (project_id, episode_id, shot_id, capability, kind, execution_mode,
                    provider_code, model_code, adapter_version, prompt_snapshot,
                    params_json, input_refs_json, status, priority, idempotency_key,
                    retry_of_job_id, attempt, user_confirmed_cloud, quantity, unit,
                    unit_price_micros, cost_micros, currency, pricing_snapshot_json,
                    billable_status, created_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (original["project_id"], original["episode_id"], original["shot_id"],
                 original["capability"], original["kind"], original["execution_mode"],
                 original["provider_code"], original["model_code"],
                 original["adapter_version"], original["prompt_snapshot"],
                 original["params_json"], original["input_refs_json"],
                 original["priority"], key, original["id"],
                 int(original["attempt"] or 1) + 1,
                 original["user_confirmed_cloud"], original["quantity"],
                 original["unit"], original["unit_price_micros"],
                 original["cost_micros"], original["currency"],
                 original["pricing_snapshot_json"], billable, created_by),
            )
            conn.execute(
                """UPDATE job SET superseded_by_job_id = ?,
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND superseded_by_job_id IS NULL""",
                (cur.lastrowid, original["id"]))
            return conn.execute("SELECT * FROM job WHERE id = ?",
                                (cur.lastrowid,)).fetchone()

    def retry_chain(self, job_id: int) -> list[sqlite3.Row]:
        """把一条作业所在的重试链整个取出来（含自己），按 id 升序。

        先往上游走到链头（`retry_of_job_id` 只挂在新行上，所以要一路回溯），
        再从链头往下收。链长是个位数到几十，用两次查询比递归 CTE 更好读，
        也不会因为 SQLite 版本差异在 `WITH RECURSIVE` 的列推导上出意外。
        """
        with get_conn() as conn:
            root_id = job_id
            seen: set[int] = set()
            while root_id not in seen:
                seen.add(root_id)
                row = conn.execute("SELECT retry_of_job_id FROM job WHERE id = ?",
                                   (root_id,)).fetchone()
                if row is None or row["retry_of_job_id"] is None:
                    break
                root_id = int(row["retry_of_job_id"])
            rows = conn.execute(
                """WITH RECURSIVE chain(id) AS (
                       SELECT ? UNION ALL
                       SELECT j.id FROM job j JOIN chain c ON j.retry_of_job_id = c.id
                   )
                   SELECT * FROM job WHERE id IN (SELECT id FROM chain) ORDER BY id""",
                (root_id,)).fetchall()
        return list(rows)

    def retry_count(self, job_id: int) -> int:
        """这条作业之后已经被重试过几枪（不含自己）。"""
        with get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM job WHERE retry_of_job_id = ?",
                (job_id,)).fetchone()
        return int(row["n"] or 0)

    def recover_stale(self) -> dict:
        """启动时收拾"上一个 worker 没来得及收尾"的作业。

        判据是租约过期，不是进程号：PID 会被复用，租约时间不会。

        云端那条多一个分支：**已经拿到供应商请求 ID 的作业不能直接算 failed**。
        供应商那边可能已经建了任务、甚至已经计了费；failed 的默认建议是重试，
        而那正好可能造成第二次调用。这里把它挂成 needs_attention，交给人核对。
        """
        with get_conn() as conn:
            attention = conn.execute(
                """UPDATE job SET status = 'needs_attention',
                          error_code = 'worker_lost',
                          error_message = 'worker 退出时这条云端作业还在执行，且供应商任务可能已建立；请先核对账单。',
                          attention_reason = 'worker 中断，云端调用结果未知',
                          finished_at = datetime('now','localtime'),
                          lease_until = NULL, worker_id = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE status IN ('starting','running')
                      AND execution_mode = 'cloud'
                      AND provider_request_id IS NOT NULL
                      AND (lease_until IS NULL
                           OR lease_until < datetime('now','localtime'))"""
            ).rowcount
            failed = conn.execute(
                """UPDATE job SET status = 'failed', error_code = 'worker_lost',
                          error_message = 'worker 退出时这条作业还在执行；可以重试。',
                          finished_at = datetime('now','localtime'),
                          lease_until = NULL, worker_id = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE status IN ('starting','running')
                      AND (lease_until IS NULL
                           OR lease_until < datetime('now','localtime'))"""
            ).rowcount
            cancelled = conn.execute(
                """UPDATE job SET status = 'cancelled', error_code = 'cancelled',
                          error_message = '取消请求时 worker 已退出',
                          finished_at = datetime('now','localtime'),
                          lease_until = NULL, worker_id = NULL,
                          updated_at = datetime('now','localtime')
                    WHERE status = 'cancel_requested'
                      AND (lease_until IS NULL
                           OR lease_until < datetime('now','localtime'))"""
            ).rowcount
        return {"failed": failed, "cancelled": cancelled, "needs_attention": attention}

    def ids_for_project(self, project_id: int) -> list[int]:
        with get_conn() as conn:
            return [r["id"] for r in conn.execute(
                "SELECT id FROM job WHERE project_id = ?", (project_id,))]

    def status_of(self, job_id: int) -> str | None:
        with get_conn() as conn:
            row = conn.execute("SELECT status FROM job WHERE id = ?",
                               (job_id,)).fetchone()
        return row["status"] if row else None

    # ---------- 成本账（W08） ----------
    @staticmethod
    def _scope(project_ids: list[int] | None) -> tuple[str, list]:
        if project_ids is None:
            return "1", []
        if not project_ids:
            return "0", []
        marks = ",".join("?" for _ in project_ids)
        return f"project_id IN ({marks})", list(project_ids)

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
    def heartbeat(self, worker_id: str, pid: int, current_job_id: int | None,
                  version: str, *, limits: dict | None = None,
                  resources: dict | None = None) -> None:
        """写一份心跳：worker_node 记这个进程，worker_state 保留"最近一次"镜像。

        `limits`/`resources` 只在启动时或作业收尾时给；中间续租传 None 时
        保留上一份（COALESCE），别让槽位说明被空值擦掉。
        """
        limits_json = json.dumps(limits, ensure_ascii=False, sort_keys=True) if limits is not None else None
        resources_json = json.dumps(resources, ensure_ascii=False, sort_keys=True) if resources is not None else None
        host = (worker_id or "").split(":", 1)[0]
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO worker_node
                       (worker_id, pid, host, started_at, heartbeat_at,
                        current_job_id, version, limits_json, resources_json)
                   VALUES (?, ?, ?, datetime('now','localtime'),
                           datetime('now','localtime'), ?, ?, COALESCE(?, '{}'),
                           COALESCE(?, '{}'))
                   ON CONFLICT(worker_id) DO UPDATE SET
                       pid = excluded.pid,
                       heartbeat_at = excluded.heartbeat_at,
                       current_job_id = excluded.current_job_id,
                       version = excluded.version,
                       limits_json = COALESCE(excluded.limits_json,
                                              worker_node.limits_json),
                       resources_json = COALESCE(excluded.resources_json,
                                                 worker_node.resources_json)""",
                (worker_id, pid, host, current_job_id, version,
                 limits_json, resources_json),
            )
            # 面板只关心近几天的进程；删除久未心跳的行，不让表无限长
            conn.execute(
                """DELETE FROM worker_node
                    WHERE heartbeat_at < datetime('now','localtime','-7 days')"""
            )
            conn.execute(
                """INSERT INTO worker_state (id, worker_id, pid, started_at,
                                             heartbeat_at, current_job_id, version)
                   VALUES (1, ?, ?, datetime('now','localtime'),
                           datetime('now','localtime'), ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       worker_id = excluded.worker_id,
                       pid = excluded.pid,
                       started_at = CASE WHEN worker_state.worker_id = excluded.worker_id
                                         THEN worker_state.started_at
                                         ELSE excluded.started_at END,
                       heartbeat_at = excluded.heartbeat_at,
                       current_job_id = excluded.current_job_id,
                       version = excluded.version""",
                (worker_id, pid, current_job_id, version),
            )

    def workers(self, online_seconds: int) -> list[dict]:
        """所有登记过的 worker 进程（最近的在前），带"现在还活着吗"。"""
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT worker_id, pid, host, started_at, heartbeat_at,
                          current_job_id, version, limits_json, resources_json,
                          CAST(strftime('%s','now','localtime')
                               - strftime('%s', heartbeat_at) AS INTEGER)
                              AS age_seconds
                     FROM worker_node
                    ORDER BY heartbeat_at DESC, id DESC"""
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            age = item.get("age_seconds")
            item["online"] = age is not None and 0 <= age <= online_seconds
            item["limits"] = _loads(item.pop("limits_json", "") or "{}")
            item["resources"] = _loads(item.pop("resources_json", "") or "{}")
            out.append(item)
        return out

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

    def worker_state(self) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute("SELECT * FROM worker_state WHERE id = 1").fetchone()

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

    def queue_health(self, max_runtime_seconds: int) -> dict:
        """队列健康：把"没人干活"和"刚入队 0.5 秒"分开。

        这些数字只用于提示，**不改任何 Job 状态**——状态真相仍然只在 `status` 一列。
        运行超时只报"超过上限还在跑"，杀进程是 worker 的事（见 `worker.py` 的 deadline）。
        """
        with get_conn() as conn:
            by_status = {r["status"]: int(r["n"]) for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM job GROUP BY status")}
            oldest = conn.execute(
                """SELECT MIN(queued_at) AS at FROM job WHERE status = 'queued'"""
            ).fetchone()["at"]
            running = conn.execute(
                """SELECT id, status, project_id, started_at, updated_at, lease_until,
                          capability, provider_code, model_code, execution_mode
                     FROM job
                    WHERE status IN ('starting','running','cancel_requested')
                    ORDER BY id"""
            ).fetchall()
            attention = conn.execute(
                """SELECT COUNT(*) AS n FROM job
                    WHERE status = 'needs_attention'
                       OR (execution_mode = 'cloud'
                           AND billable_status = 'pending_reconciliation'
                           AND status IN ('succeeded','failed','cancelled'))"""
            ).fetchone()["n"]
            expired = conn.execute(
                """SELECT COUNT(*) AS n FROM job
                    WHERE status IN ('starting','running','cancel_requested')
                      AND (lease_until IS NULL
                           OR lease_until < datetime('now','localtime'))"""
            ).fetchone()["n"]
        now = datetime.now()

        def age(text: str | None) -> int | None:
            if not text:
                return None
            try:
                return max(0, int((now - datetime.strptime(text, "%Y-%m-%d %H:%M:%S"))
                                  .total_seconds()))
            except ValueError:
                return None

        running_rows = []
        for r in running:
            started = age(r["started_at"])
            running_rows.append({
                # project_id 是给 worker_status 做可见性过滤用的；log_path 不再往外给：
                # 前端零引用，而它会把服务器目录结构摆进任何登录者的响应里
                "id": r["id"], "status": r["status"], "project_id": r["project_id"],
                "capability": r["capability"], "provider": r["provider_code"],
                "model": r["model_code"], "execution_mode": r["execution_mode"],
                "started_seconds": started,
                "silent_seconds": age(r["updated_at"]),
                "lease_expired": (r["lease_until"] is None
                                  or age(r["lease_until"]) is None
                                  or r["lease_until"] < now.strftime("%Y-%m-%d %H:%M:%S")),
                "over_runtime": started is not None and started > max_runtime_seconds,
            })
        return {
            "by_status": by_status,
            "queued": by_status.get("queued", 0),
            "running": sum(by_status.get(s, 0) for s in
                           ("starting", "running", "cancel_requested")),
            "oldest_queued_seconds": age(oldest),
            "running_jobs": running_rows,
            "expired_leases": int(expired or 0),
            "needs_attention": int(attention or 0),
        }


jobs = JobRepository()
