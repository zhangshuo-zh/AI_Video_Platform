from __future__ import annotations

import job_state
import json
import sqlite3

from db import get_conn, transaction

from .common import JobStateConflict, _check_terminal, _require_owned


class JobsStateMixin:

    def succeed(self, job_id: int, worker_id: str, log_path: str,
                result_paths: list[str], elapsed_ms: int, return_code: int = 0,
                resources: dict | None = None,
                provider_request_id: str | None = None) -> None:
        with get_conn() as conn:
            _check_terminal(conn, job_id, worker_id, job_state.SUCCEEDED, "succeeded")
            cur = conn.execute(
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
            _require_owned(cur, job_id, worker_id, "succeeded")


    def fail(self, job_id: int, worker_id: str, error_code: str, message: str,
             log_path: str = "", return_code: int | None = None,
             elapsed_ms: int | None = None, resources: dict | None = None,
             provider_request_id: str | None = None) -> None:
        """失败**不动成本列**：一条云端作业失败时，供应商可能已经计过费；
        这里替人把金额清零，账就再也对不上了（数据字典 §5.3「禁止」）。"""
        with get_conn() as conn:
            _check_terminal(conn, job_id, worker_id, job_state.FAILED, "failed")
            cur = conn.execute(
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
            _require_owned(cur, job_id, worker_id, "failed")


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
            _check_terminal(conn, job_id, worker_id, job_state.CANCELLED, "cancelled")
            cur = conn.execute(
                """UPDATE job SET status = 'cancelled', finished_at = datetime('now','localtime'),
                          error_code = 'cancelled', error_message = ?, log_path = ?,
                          elapsed_ms = ?, resources_json = ?, lease_until = NULL, worker_id = NULL,
                          provider_request_id = COALESCE(?, provider_request_id),
                          updated_at = datetime('now','localtime')
                    WHERE id = ? AND worker_id = ?""",
                (message[:2000], log_path, elapsed_ms, self._resources(resources),
                 provider_request_id, job_id, worker_id),
            )
            _require_owned(cur, job_id, worker_id, "cancelled")


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
            _check_terminal(conn, job_id, worker_id, job_state.NEEDS_ATTENTION, "needs_attention")
            cur = conn.execute(
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
            _require_owned(cur, job_id, worker_id, "needs_attention")


    def mark_attention_by_user(self, job_id: int, reason: str) -> None:
        """人工把一条云端作业送进"待核对"。2026-11-09 外部审查第 3 条。

        原来这段逻辑在 `services/jobs.py:mark_job_attention` 里，是**集中状态表
        唯一没覆盖到的旁路**：`job_repo.get()` 在事务外读状态、然后裸
        `UPDATE job SET status='needs_attention' WHERE id=?`——

        - 没过 `assert_transition`，状态机只在这一个入口失效；
        - UPDATE 没带 `AND status IN (...)`，读与写之间别人把它推到
          `running` 也照样被改写成 `needs_attention`；
        - 不看 rowcount，写中了 0 行也当成功返回。

        并发下这是能真的出错的：A 看到 `failed` 正要标记，B 同时把作业重投
        成 `running`，A 的 UPDATE 把一个正在跑的作业钉进终态。

        **为什么用 transaction() 而不是一句带条件的 UPDATE**：
        后者也能挡住并发，但就绕开了状态机——而"状态怎么变"应当只有
        一个出处。放进事务里，`BEGIN IMMEDIATE` 先拿写锁，读到的是串行化
        之后的事实，`assert_transition` 的判据才是可靠的。
        """
        with transaction():
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT status FROM job WHERE id = ?", (job_id,)).fetchone()
                if row is None:
                    raise LookupError(f"作业 {job_id} 不存在")
                # **只接受已结束的作业**，这一条不能只靠服务层的前置检查：
                # 那个检查在事务外读，读完到这里之间状态可能已经变了。
                # 而 `assert_transition` 拦不住 `running → needs_attention`
                # （worker 收尾路径需要这条转移），所以显式收在这里。
                if row["status"] not in (job_state.SUCCEEDED, job_state.FAILED):
                    raise JobStateConflict(
                        f"作业 #{job_id} 当前是 {row['status']}，"
                        "只有已结束的成功/失败作业能标记待核对")
                job_state.assert_transition(row["status"], job_state.NEEDS_ATTENTION)
                cur = conn.execute(
                    """UPDATE job SET status = 'needs_attention',
                              attention_reason = ?,
                              error_message = COALESCE(error_message, ?),
                              updated_at = datetime('now','localtime')
                        WHERE id = ? AND status IN (?, ?)""",
                    (reason[:500], f"人工标记待核对：{reason[:500]}",
                     job_id, job_state.SUCCEEDED, job_state.FAILED),
                )
                if cur.rowcount != 1:
                    raise JobStateConflict(
                        f"作业 {job_id} 的状态在标记过程中被改成了别的值，请刷新后重试")

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
            if row is None:
                return None
            if row["status"] != "needs_attention":
                # **已经不在待核对状态**：可能被另一个人核过了，也可能
                # 被别的路径改走了。这不是"幂等成功"——本次决定确实没写入。
                # 服务层会把它翻成 409（2026-11-09 外部审查第 5 条）。
                raise JobStateConflict(
                    f"这条作业当前是 {row['status']}，不是待核对状态；"
                    "可能已被其他人核对，本次决定未写入")
            if decision == "pending":
                conn.execute(
                    """UPDATE job SET attention_reason = ?, reconcile_note = ?,
                              updated_at = datetime('now','localtime')
                        WHERE id = ? AND status = 'needs_attention'""",
                    (row["attention_reason"], (note or "")[:2000], job_id))
            elif decision in ("succeeded", "failed"):
                # `AND status = 'needs_attention'`（2026-11-08，P0）：两个人几乎
                # 同时核对同一条时，后写者原来会静默覆盖前者的结论与实际金额，
                # 账本上"谁核的、核成什么"就不再有唯一答案。带上状态条件之后，
                # 第二个人这次 UPDATE 命中 0 行，返回的是重读后的行——
                # 界面能看出"已经被别人核过了"，而不是以为自己的决定生效了。
                cur = conn.execute(
                    """UPDATE job SET status = ?, actual_cost_micros = ?,
                              billable_status = 'known', reconciled_by = ?,
                              reconciled_at = datetime('now','localtime'),
                              reconcile_note = ?, updated_at = datetime('now','localtime')
                        WHERE id = ? AND status = 'needs_attention'""",
                    (decision, actual_cost_micros, user_id, (note or "")[:2000], job_id))
                # **写中 0 行要说出来**（2026-11-09 外部审查第 5 条）。
                #
                # 2026-11-08 给这条 UPDATE 加了 `AND status='needs_attention'`，
                # 挡住"后到者覆盖前者结论"——但只挡了一半：命中 0 行时这里
                # 什么都不说，路由照样 200，而服务层拼的 message 是
                # **按"我要做什么"生成的**（"已确认可用…"），不是按
                # "实际发生了什么"。于是后到者看到的是一句成功提示，
                # 而库里保留的仍是前一个人的结论——注释里承诺的
                # "能看出已被别人核过"从来没落地。
                #
                # 这里返回一个显式标记，由服务层翻成 409。
                if cur.rowcount != 1:
                    raise JobStateConflict(
                        "这条作业已经被人核对过了；本次决定未写入，"
                        "请刷新查看最新结论")
            else:
                return row
            return conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()


    def request_cancel(self, job_id: int) -> sqlite3.Row | None:
        """取消是"请求式"：排队中的当场取消，跑着的只记下意愿。

        **两条 UPDATE 都带 `AND status = ...`（2026-11-08，P0）**。
        原来它们只带 `WHERE id = ?`：取消与 worker 领取交错时，
        取消方在读到 `queued` 之后、写 `cancelled` 之前，worker 可能已经
        在 `BEGIN IMMEDIATE` 里把同一条领成 `starting` 并起了子进程；
        那条无条件 UPDATE 会把状态覆盖回 `cancelled`——DB 说没跑，
        子进程却在跑模型、占显存，产物还照写 outputs/。
        带上状态条件之后，这种竞争会让 rowcount=0，本次取消**不生效**、
        返回的是重读后的真实状态（此时通常已是 starting/running，
        调用方再点一次取消就会走"记意愿"那一支）。
        """
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
                              updated_at = datetime('now','localtime')
                        WHERE id = ? AND status = 'queued'""",
                    (job_id,),
                )
            elif row["status"] in ("starting", "running"):
                conn.execute(
                    """UPDATE job SET status = 'cancel_requested',
                              updated_at = datetime('now','localtime')
                        WHERE id = ? AND status IN ('starting','running')""",
                    (job_id,),
                )
            # 状态仍以库里为准：竞争失败时这里读到的是别人写进去的那个，
            # 调用方据此判断"取消请求有没有被记下"。
            return conn.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()

    @staticmethod
    def _resources(resources: dict | None) -> str:
        # 资源数据只增不减：失败与取消也要留——它们同样是"机器被占过"的证据
        return json.dumps(resources or {}, ensure_ascii=False, sort_keys=True)
