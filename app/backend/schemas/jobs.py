import json
from decimal import Decimal
from typing import Literal

import job_state
from pydantic import BaseModel, Field, field_validator

from .enums import BACKENDS, BILLABLE_LABELS, CAPABILITIES, ERROR_LABELS, JOB_STATUSES


class JobEstimateIn(BaseModel):
    """云端作业提交前那张"要花多少钱"的确认单。

    数量 × 单价由**服务端**算，客户端只给观测量与价格快照：
    让人填一个总价再信它，等于把账本交给前端。单价以 micros 存整数，
    避免 0.1 + 0.2 这类浮点误差进了账；`source` 必填，是为了以后对账时
    能回答"这个价是哪来的"——没有出处的数字不算数。
    """
    quantity: Decimal = Field(gt=0, le=Decimal("1000000"), max_digits=12, decimal_places=3)
    unit: Literal["second", "minute", "request", "token", "million_token",
                  "image", "character"]
    unit_price_micros: int = Field(ge=0, le=1_000_000_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    source: str = Field(min_length=1, max_length=200)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()




class JobSubmitIn(BaseModel):
    project_id: int
    episode_id: int | None = None
    shot_id: int | None = None
    capability: Literal["text", "image", "video", "tts", "music"]
    # 不传执行方式就走这一类的默认轨；传了就必须三件一起给（方式/供应商/模型），
    # 不允许"给了 provider 却让平台自己猜后端"。
    execution_mode: Literal["local", "cloud"] | None = None
    provider_code: str = Field(default="", max_length=80)
    model_code: str = Field(default="", max_length=120)
    prompt: str = Field(min_length=1, max_length=8000)
    params: dict = {}
    input_refs: list[str] = Field(default_factory=list, max_length=20)
    idempotency_key: str | None = Field(default=None, max_length=120,
                                         pattern=r"^[A-Za-z0-9._:-]+$")
    # 云端作业必须带确认单并勾确认；本地作业带了会被拒——
    # 那说明调用方把"本地免费"和"云端按量"搞混了，账上不能含糊。
    estimate: JobEstimateIn | None = None
    user_confirmed_cloud: bool = False
    priority: int = Field(default=0, ge=0, le=100)




class JobOut(BaseModel):
    id: int
    # 0039：删项目后作业行留着、project_id 置空。界面把这种行显示成「已删除项目」。
    project_id: int | None
    episode_id: int | None
    shot_id: int | None
    capability: str
    capability_label: str
    kind: str
    execution_mode: str
    execution_mode_label: str
    provider_code: str
    model_code: str
    status: str
    status_label: str
    prompt_preview: str
    # 提交那一刻的源内容指纹（0031）。空串＝0031 之前落的作业或非分镜作业，
    # 界面据此区分「没有指纹」与「指纹对不上」，不把两者混成"过期"。
    source_version: str = ""
    queued_at: str
    started_at: str | None
    finished_at: str | None
    elapsed_ms: int | None
    retry_of_job_id: int | None
    error_code: str | None
    error_message: str | None
    error_label: str = ""
    # 云端供应商回执里的请求/任务 ID；本地作业恒为 None。
    # 只回 ID，不回供应商原始响应——原始响应在 resources 的是白名单摘录。
    provider_request_id: str | None = None
    has_log: bool
    has_artifacts: bool
    # 成本只回整数 micros 与币种，格式化交给界面；
    # billable_status 是给人看的口径：known 已确认 / pending 待核对 / not_billable 不计费
    quantity: float | None = None
    unit: str | None = None
    cost_micros: int | None = None
    currency: str | None = None
    billable_status: str = "not_billable"
    billable_status_label: str = "不计费"
    # P0：重试链与账单核对要能在列表里看出来，不然一串重试就是几笔互不相干的待核对。
    # actual/delta 只有人核对过才有值；没核对一律 None，不拿 0 冒充"没花钱"。
    attempt: int = 1
    superseded_by_job_id: int | None = None
    retry_count: int = 0
    # 服务端下发的「这一行现在能做什么」：前端不再自己按状态集合猜，
    # 否则会出现「钮在、点下去必吃 409」。用 Optional 是为了不经过 from_row
    # 的老构造点不被误标成 False——前端只在拿到布尔值时才采信。
    cancelable: bool | None = None
    retryable: bool | None = None
    retry_hint: str = ""
    actual_cost_micros: int | None = None
    delta_micros: int | None = None
    attention_reason: str = ""
    reconciled_at: str | None = None
    reconciled_by_name: str = ""
    reconcile_note: str = ""

    @classmethod
    def from_row(cls, row) -> "JobOut":
        from repositories.jobs import retry_reason   # 延迟导入，避开 models ↔ repositories 环

        prompt = row["prompt_snapshot"] or ""
        keys = row.keys()
        cost = row["cost_micros"]
        hint = retry_reason(row)
        actual = row["actual_cost_micros"] if "actual_cost_micros" in keys else None
        return cls(
            id=row["id"], project_id=row["project_id"],
            episode_id=row["episode_id"], shot_id=row["shot_id"],
            capability=row["capability"],
            capability_label=CAPABILITIES.get(row["capability"], row["capability"]),
            kind=row["kind"],
            execution_mode=row["execution_mode"],
            execution_mode_label=BACKENDS.get(row["execution_mode"], row["execution_mode"]),
            provider_code=row["provider_code"], model_code=row["model_code"],
            status=row["status"],
            status_label=JOB_STATUSES.get(row["status"], row["status"]),
            prompt_preview=prompt[:120],
            source_version=(row["source_version"] if "source_version" in keys else "") or "",
            queued_at=row["queued_at"], started_at=row["started_at"],
            finished_at=row["finished_at"], elapsed_ms=row["elapsed_ms"],
            retry_of_job_id=row["retry_of_job_id"],
            error_code=row["error_code"], error_message=row["error_message"],
            error_label=ERROR_LABELS.get(row["error_code"] or "", row["error_code"] or ""),
            provider_request_id=row["provider_request_id"],
            has_log=bool(row["log_path"]),
            has_artifacts=row["result_paths_json"] not in ("", "[]"),
            quantity=row["quantity"], unit=row["unit"],
            cost_micros=cost, currency=row["currency"],
            billable_status=row["billable_status"],
            billable_status_label=BILLABLE_LABELS.get(row["billable_status"],
                                                      row["billable_status"]),
            attempt=int(row["attempt"] or 1) if "attempt" in keys else 1,
            superseded_by_job_id=(row["superseded_by_job_id"]
                                  if "superseded_by_job_id" in keys else None),
            # 仓储给的是 sqlite3.Row，没有 .get()：一律按 keys() 判断，
            # 否则「重试过几次」会静默恒为 0（与 production.py 里那条注释同理）。
            retry_count=int(row["retry_count"] or 0) if "retry_count" in keys else 0,
            cancelable=job_state.can("cancel", row["status"]),
            retryable=not hint,
            retry_hint=hint,
            actual_cost_micros=actual,
            delta_micros=(None if actual is None or cost is None else int(actual) - int(cost)),
            attention_reason=(row["attention_reason"] or "") if "attention_reason" in keys else "",
            reconciled_at=row["reconciled_at"] if "reconciled_at" in keys else None,
            reconciled_by_name=(row["reconciled_by_name"] or "")
            if "reconciled_by_name" in keys else "",
            reconcile_note=(row["reconcile_note"] or "") if "reconcile_note" in keys else "",
        )




class JobDetailOut(JobOut):
    adapter_version: str
    prompt_snapshot: str
    params: dict
    input_refs: list
    log_path: str | None
    result_paths: list
    return_code: int | None
    unit_price_micros: int | None
    pricing_snapshot: dict
    resources: dict
    # P0 可观测性：租约与日志静默。它们只回答"这条现在到底动没动"，
    # 不改状态——把"卡住"直接写成 failed 会替 worker 做它还没做的判断。
    lease_until: str | None = None
    lease_expired: bool = False
    log_silent_seconds: int | None = None
    log_size: int = 0
    running_seconds: int | None = None

    @classmethod
    def from_row(cls, row) -> "JobDetailOut":
        base = JobOut.from_row(row).model_dump()

        def load(text, fallback):
            try:
                value = json.loads(text or "")
            except ValueError:
                return fallback
            return value if isinstance(value, type(fallback)) else fallback

        return cls(**base,
                   adapter_version=row["adapter_version"],
                   prompt_snapshot=row["prompt_snapshot"],
                   params=load(row["params_json"], {}),
                   input_refs=load(row["input_refs_json"], []),
                   log_path=row["log_path"],
                   result_paths=load(row["result_paths_json"], []),
                   return_code=row["return_code"],
                   unit_price_micros=row["unit_price_micros"],
                   pricing_snapshot=load(row["pricing_snapshot_json"], {}),
                   resources=load(row["resources_json"], {}))




class JobLogOut(BaseModel):
    """日志尾部。`path` 是相对 Job 目录的路径，不是绝对磁盘路径。

    size/silent_seconds 是给"这条是不是卡住了"用的：只有文本，界面分不清
    慢慢写和早就没动。
    """

    path: str | None
    text: str
    size: int = 0
    mtime: str | None = None
    silent_seconds: int | None = None




class JobArtifactOut(BaseModel):
    path: str
    exists: bool
    size: int




class WorkerStatusOut(BaseModel):
    online: bool
    heartbeat_at: str
    age_seconds: int | None
    current_job_id: int | None
    queue: dict
    # 阶段 E：多 worker 时逐个列出（worker_id／pid／心跳／上限／当前作业），
    # 顶栏仍只看 online，面板看这一列解释"为什么两个进程只动了一条"。
    workers: list[dict] = []
    # 面板默认只列在线进程，"另有 N 个已离线"这句话由这两个数说话。
    # 为什么单独声明：`response_model` 会静默丢掉没在这里写的字段——
    # 服务层算了、pytest 直调 `services.worker_status()` 也看得见，
    # 但 HTTP 响应里没有，界面就永远拿不到（这一笔就是这么撞上的）。
    live_count: int = 0
    history_count: int = 0
    # P0：光有"在线/离线"两态不够。队列里有活却没人领、跑着的作业早该超时、
    # 租约过期但还挂着 running——这三种都要在界面上说清，而不是让人猜。
    alerts: list[dict] = []
    by_status: dict = {}
    oldest_queued_seconds: int | None = None
    running_jobs: list[dict] = []
    expired_leases: int = 0
    needs_attention: int = 0




class JobReconcileIn(BaseModel):
    """人工核对一条 `needs_attention` 的云端作业。

    `succeeded`/`failed` 必须带实际金额：不给金额就等于替人断定"这次没花钱"，
    而那正是账本上最不该出现的一种猜测。`pending` 只留备注，状态与账单都不动。
    """
    decision: Literal["succeeded", "failed", "pending"]
    actual_cost_micros: int | None = Field(default=None, ge=0, le=1_000_000_000_000)
    note: str = Field(default="", max_length=500)




class JobAttentionIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)




class JobRetryBatchIn(BaseModel):
    """批量重投的入参。`capability` 只认清单里已有的能力，`limit` 上限 50。"""
    episode_id: int
    capability: str = "video"
    limit: int = Field(default=50, ge=1, le=200)




class JobRetryPreviewOut(BaseModel):
    """重试前给人看清"这一枪会继承什么、可能再花多少"。

    不弹这一屏，重试按钮就只是一个"再点一次"；而云端重试是真会多一笔预计计费的。
    """
    job_id: int
    execution_mode: str
    provider_code: str
    model_code: str
    prompt_preview: str
    params: dict
    quantity: float | None
    unit: str | None
    currency: str | None
    cost_micros: int | None
    chain: list[dict] = []
    chain_total_micros: int = 0
    chain_pending: int = 0
    may_charge_again: bool = False
    warning: str = ""
