"""Job 仓储的共享常量与模块级工具（供各 mixin import）。

这一份是从 `repositories/jobs.py` 顶部原样搬来的：资源类 SQL、可复制字段、
计费口径、限额夹取、以及 `retry_reason()`／`JobOwnershipLost`／
`_require_owned`／`_check_terminal`。

为什么要单独一层：这些名字被**多个 mixin** 用（`BILLABLE_WHERE` 被 cost 用、
`_require_owned` 被 state 用、`RESOURCE_CLASS_SQL` 被 lease 与 query 用）。
放进任何一个 mixin 都会让其他 mixin 反过来依赖它，形成环。放在最底层，
谁都能依赖、它谁也不依赖。
"""

from __future__ import annotations


import job_state
import json


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




def clamp_limit(value, low: int = 1, high: int = 200) -> int:
    """仓储层统一的 LIMIT 上界：不指望每个路由都记得夹一次。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return low
    return min(max(n, low), high)




def _like_escape(text: str) -> str:
    """把 LIKE 的通配符转义成字面量。

    `%`/`_` 混进用户输入会让过滤悄悄变宽（搜 `%` 命中全部），
    而"宁可少给"正是前缀过滤存在的理由。
    """
    return str(text).replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")




# 状态集合的真相在 `job_state` 那一张表里；这里留两个别名是为了
# pipeline / routers / tests 的既有 import 不用跟着改。
JOB_ACTIVE_STATUSES = job_state.ACTIVE


RETRYABLE_JOB_STATUSES = job_state.RETRYABLE




def retry_reason(row, *, allow_cloud: bool = True,
                 allow_attention: bool = True) -> str:
    """空串＝可以重试；否则返回给人看的那句原话。

    唯一出处：`routers/jobs.py` 的单条重试、`pipeline.retry_gate()` 的产线格、
    `services.job_retry_batch()` 的批量重投都调它。批量把两档额外收紧是**有意**
    的业务规则（结果未知时自动重投可能重复扣费、云端要逐次确认计费），
    用开关表达，而不是另写一套状态集合让两边慢慢漂移。
    """
    status = row["status"]
    if status in JOB_ACTIVE_STATUSES:
        return "这条作业还在跑或排队中，等它出结果"
    if status == "succeeded":
        return "这条作业已经成功，不需要重试"
    if status not in RETRYABLE_JOB_STATUSES:
        return "只有失败、已取消或待核对的作业可以重试"
    if status == "needs_attention" and not allow_attention:
        return ("待核对的作业不自动重投：结果可能已经发生在供应商那一边，"
                "先去生成中心核对那条再说")
    keys = row.keys() if hasattr(row, "keys") else ()
    mode = row["execution_mode"] if "execution_mode" in keys else ""
    if mode == "cloud" and not allow_cloud:
        return "云端重试要逐次确认计费，请在生成中心对这一条单独重试"
    return ""




class JobOwnershipLost(RuntimeError):
    """终态写入时归属已丢。

    `recover_stale()` 会把超时作业的 `worker_id` 清成 NULL 并替它收尾。
    旧 worker 只是被长事务/机器暂停拖过租约、其实还活着时，回来写终态会
    匹配 0 行。过去四个终态方法都不看 rowcount，于是日志写「succeeded」、
    库里停在 `failed(worker_lost)`，产物与状态三处不一致且没有任何告警。
    """

    def __init__(self, job_id: int, worker_id: str, terminal: str):
        super().__init__(
            f"作业 #{job_id} 的终态 {terminal} 没有写入（worker={worker_id}）："
            "归属已被租约恢复或另一个收尾动作拿走，本次结果作废")
        self.job_id = job_id
        self.worker_id = worker_id
        self.terminal = terminal




class JobStateConflict(RuntimeError):
    """人工改状态时，读到的状态与写下去的那一刻不一致。

    与 `JobOwnershipLost` 一对：那个是**worker 收尾**时发现作业已不归自己，
    这个是**人在界面上**操作时发现状态已经变了。两者都不该静默成功——
    界面上"我点了标记待核对"和"其实没生效"必须有区别，否则用户会以为
    自己已经处置过这条作业。
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _require_owned(cur, job_id: int, worker_id: str, terminal: str) -> None:
    """终态 UPDATE 必须恰好命中一行；0 行＝这次收尾不再是它的了。"""
    if cur.rowcount != 1:
        raise JobOwnershipLost(job_id, worker_id, terminal)




def _check_terminal(conn, job_id: int, worker_id: str, new_status: str,
                    terminal: str) -> None:
    """收尾前的两道前置检查，**先归属、后状态机**，顺序不能反。

    为什么先判归属：一条作业被 `recover_stale()` 收成 failed 之后，原来那个
    worker 如果还活着并回来写终态，"不是我收的尾"才是真正的原因；
    此时若先跑状态机，会抛一句"failed 不能直接变 succeeded"——那是把一个
    归属问题说成了状态问题，排障的人会去翻状态表而不是查租约。
    `JobOwnershipLost` 是调用方（worker）明确会捕获并安静退出的一类，
    换了异常类型就会变成一条 internal_error 噪音。

    归属靠 UPDATE 的 `AND worker_id = ?` 保证（写不中就有 `_require_owned`
    抛 `JobOwnershipLost`），所以这里只需要在**写之前**先用一次轻量读
    把"明显已经不归我"的情形提前挡掉；真正的拦截仍在写那一步。
    """
    row = conn.execute(
        "SELECT status, worker_id FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return
    if row["worker_id"] != worker_id:
        raise JobOwnershipLost(job_id, worker_id, terminal)
    job_state.assert_transition(row["status"], new_status)
