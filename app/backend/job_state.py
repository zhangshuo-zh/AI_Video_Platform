"""Job 状态机：状态、动作、合法转移与权限，**唯一一张表**。

为什么要单独一份：状态集合原来散在四个地方——`models.py` 的
`JOB_RUNNING/JOB_FINISHED`、`repositories/jobs.py` 的
`JOB_ACTIVE_STATUSES/RETRYABLE_JOB_STATUSES`、路由里的 `status not in (...)`、
以及前端自己抄的一份。它们是同一件事，却各自漂：本轮就修过
「工作台说能合成、成片拒收」这种两处口径不一致。

这里只回答三个问题：
  * 这个状态还能不能做某个动作（`can`）；
  * 做了之后落到哪个状态（`target`）；
  * 这个动作要什么权限（`permission`）。

**不掺业务**：能不能重试还要看「云端作业有没有核对账单」这类规则，
那仍在 `retry_reason()` 里；本表只给"状态这一层"的答案。
"""

QUEUED = "queued"
STARTING = "starting"
RUNNING = "running"
CANCEL_REQUESTED = "cancel_requested"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
NEEDS_ATTENTION = "needs_attention"

STATUSES = (QUEUED, STARTING, RUNNING, CANCEL_REQUESTED,
            SUCCEEDED, FAILED, CANCELLED, NEEDS_ATTENTION)
# 还在队列里或正在跑：取消、看板"进行中"、心跳归属都看这一组。
ACTIVE = (QUEUED, STARTING, RUNNING, CANCEL_REQUESTED)
# 已经开工、还没出结果（不含排队）。界面上的「进行中」是 ACTIVE，
# 而 worker 收尾/续租关心的是这一组——两者差一个 queued，别混。
# 名字不叫 RUNNING：那已经是单数状态常量，同名会把它覆盖掉。
IN_FLIGHT = (STARTING, RUNNING, CANCEL_REQUESTED)
# 已经出结果（成功/失败/取消/待核对）：重试的候选池。
FINISHED = (SUCCEEDED, FAILED, CANCELLED, NEEDS_ATTENTION)
RETRYABLE = (FAILED, CANCELLED, NEEDS_ATTENTION)
# 终态写入要校验归属的那四个（worker 收尾用）。
TERMINAL = (SUCCEEDED, FAILED, CANCELLED)

# 状态的合法去处。写终态时用它挡住"从已取消跳到成功"这类脏写。
TRANSITIONS = {
    QUEUED: (STARTING, CANCELLED),
    # 已领取但模型还没跑起来：取消请求直接落到 cancel_requested，
    # 由 worker 在真正开跑前把这一格收掉。
    #
    # 2026-11-08 补 `SUCCEEDED`：`starting` 也能直接收成成功。这不是放宽纪律，
    # 是承认一个真实窗口——`set_running()` 的 UPDATE 只带
    # `AND status = 'starting'` 却**不校验 rowcount**，一旦那一步没命中
    # （状态在这一瞬被别的写动过），作业会停在 starting，随后照常收尾；
    # mock 这类"领取后几毫秒就结束"的作业同样可能落在这一档。
    # 挡掉它并不会让数据更干净，只会让一条本来成功的作业在收尾时抛异常、
    # 变成 internal_error——那比允许这一步要糟得多。
    STARTING: (RUNNING, CANCEL_REQUESTED, SUCCEEDED, FAILED, CANCELLED,
               NEEDS_ATTENTION),
    RUNNING: (SUCCEEDED, FAILED, CANCELLED, NEEDS_ATTENTION, CANCEL_REQUESTED),
    # 取消请求后仍可能先跑完（模型不响应中断），所以成功/失败也是合法去处。
    CANCEL_REQUESTED: (CANCELLED, SUCCEEDED, FAILED),
    SUCCEEDED: (NEEDS_ATTENTION,),
    FAILED: (NEEDS_ATTENTION,),
    CANCELLED: (NEEDS_ATTENTION,),
    # 待核对是"结了账但人要看一眼"，核对完退回它本来该在的终态。
    NEEDS_ATTENTION: (SUCCEEDED, FAILED, CANCELLED),
}

# 人对一条作业能做的动作。`to` 是"落到哪个状态"，取消是分状态的：
# 排队中的可以直接取消，已经在跑的只能先记一个取消请求，由 worker 收尾。
ACTIONS = {
    "cancel": {
        "from": {QUEUED: CANCELLED, STARTING: CANCEL_REQUESTED,
                 RUNNING: CANCEL_REQUESTED,
                 # 已经请求过取消：再点一次是幂等的，不报 409——
                 # 旧代码只挡"已结束"，重复取消本来也返回原状态。
                 CANCEL_REQUESTED: CANCEL_REQUESTED},
        "permission": "job:cancel",
    },
    "retry": {
        # 重试不是原地改状态，而是新建一行；这里只声明"哪些状态可以重试"。
        "from": {s: QUEUED for s in RETRYABLE},
        "permission": "job:submit",
        "new_row": True,
    },
    "reconcile": {
        "from": {NEEDS_ATTENTION: None},
        "permission": "job:submit",
    },
}


def can(action: str, status: str) -> bool:
    """这个状态能不能做这个动作。未知动作一律 False，不抛。"""
    spec = ACTIONS.get(action)
    return bool(spec) and status in spec["from"]


def target(action: str, status: str) -> str | None:
    """做了动作之后落到哪个状态；不能做时返回 None（`reconcile` 本就是 None）。"""
    spec = ACTIONS.get(action)
    if not spec or status not in spec["from"]:
        return None
    return spec["from"][status]


def permission(action: str) -> str:
    return (ACTIONS.get(action) or {}).get("permission", "")


def assert_transition(old: str, new: str) -> None:
    """写入前校验一次转移。只在服务层/收尾处调用，不在每个 UPDATE 里加读。"""
    if old == new:
        return
    if new not in TRANSITIONS.get(old, ()):
        raise ValueError(f"作业状态不能从 {old} 直接变成 {new}")
