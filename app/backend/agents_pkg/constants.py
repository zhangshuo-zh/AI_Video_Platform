"""智能体编排状态常量、标签字典与领域异常。"""
from __future__ import annotations

from repositories.agents import (
    STEP_APPROVED,
    STEP_AWAITING,
    STEP_CANCELLED,
    STEP_EXPIRED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
)

# 一次计划最多排几步。上限不是给模型出难题，是给成本封顶：
# 每一步都可能是一条付费作业，"计划"没有步数上限就等于账单没有上限。
MAX_PLAN_STEPS = 12
# 一轮 tick 里一个运行最多连着走几步（读工具当场做完，付费/写步骤会停下来等审批）。
# 与上面那个上限同源，防的是"计划里全是读步骤，一轮把 CPU 吃掉"。
MAX_STEPS_PER_TICK = 6

RUN_PLANNING = "planning"
RUN_AWAITING = "awaiting_approval"
RUN_RUNNING = "running"
RUN_PAUSED = "paused"
RUN_CANCELLED = "cancelled"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"

RUN_STATUS_LABEL = {
    RUN_PLANNING: "在算计划",
    RUN_AWAITING: "等你审批",
    RUN_RUNNING: "进行中",
    RUN_PAUSED: "已暂停",
    RUN_CANCELLED: "已取消",
    RUN_COMPLETED: "已完成",
    RUN_FAILED: "失败",
}

STEP_STATUS_LABEL = {
    STEP_PENDING: "待处理",
    STEP_AWAITING: "等你审批",
    STEP_APPROVED: "已批准",
    STEP_RUNNING: "执行中",
    STEP_SUCCEEDED: "成功",
    STEP_FAILED: "失败",
    STEP_SKIPPED: "跳过",
    STEP_CANCELLED: "作废",
    STEP_EXPIRED: "审批过期",
}

EVENT_LABEL = {
    "run_created": "已发起",
    "plan_requested": "去算计划",
    "plan_ready": "计划回来了",
    "plan_failed": "计划失败",
    "run_started": "开始推进",
    "run_paused": "已暂停",
    "run_resumed": "已恢复",
    "run_cancelled": "已取消",
    "run_completed": "已完成",
    "run_failed": "运行失败",
    "step_ready": "步骤就绪",
    "step_started": "执行中",
    "step_succeeded": "步骤成功",
    "step_failed": "步骤失败",
    "step_skipped": "步骤跳过",
    "step_retried": "步骤重试",
    "step_expired": "审批过期",
    "approval_requested": "在等你批",
    "approval_granted": "已批准",
    "approval_rejected": "已驳回",
    "job_submitted": "排了一条作业",
    "preflight_warning": "提示",
    "tool_rejected": "工具被拒",
}

JOB_TERMINAL_FAILED = ("failed", "cancelled", "needs_attention")


class AgentInvalid(ValueError):
    """发起参数不成立（路由 422）。与 `services.JobInvalid` 同一分工：
    NoRoute 是"现在没有路线"，这一条是"这次请求本身不对"。"""


class AgentNotFound(LookupError):
    """对象不存在或不属于这个项目——路由层翻成 404，不泄露它是否存在。"""


class AgentConflict(RuntimeError):
    """状态不对（已取消的运行不能恢复……）。翻 409。"""
