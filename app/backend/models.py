"""请求/响应用的数据结构。

ASSET_KINDS 是"资产类型 → 中文名"的唯一出处，前端不再自己判断。
"""
import json
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 五类能力：固定，前端按这个顺序渲染，不靠库里查出来的顺序
CAPABILITIES: dict[str, str] = {
    "text": "文本",
    "image": "图像",
    "video": "视频",
    "tts": "配音",
    "music": "音乐",
}

BACKENDS: dict[str, str] = {"local": "本地", "cloud": "云端"}

# 计费单位白名单。不做自由文本：单位是给人对账的，
# "sec"／"秒"／"seconds" 混着存，汇总时就成了三个口径。
UNITS: dict[str, str] = {
    "second": "秒",
    "minute": "分钟",
    "request": "次",
    "token": "Token",
    # 文本供应商按"百万 token"报价；单 token 单价会写成 0.000002 这种小数，
    # 既难核对也容易在前端被四舍五入吃掉。单独留一档，预填就是官方口径。
    "million_token": "百万 Token",
    "image": "张",
    "character": "字符",
}

BILLABLE_LABELS: dict[str, str] = {
    "known": "已确认",
    "pending_reconciliation": "待核对",
    "not_billable": "不计费",
}

# 一条"能用"的结论凭什么成立。五类互不升级——外部评审点名的就是这一刀：
# mock 跑通不等于真实供应商可用，环境齐备不等于真产出过文件，
# 真实调用成功也只证明"链路产出过"，文件好不好要人看过（产物验收）才算数。
VALIDATION_KINDS: dict[str, str] = {
    "env_check": "环境检查",
    "mock": "假适配器",
    "real_call": "真实调用",
    "target_machine": "目标机基准",
    "artifact_review": "产物验收",
}

# 错误码 → 人话分类。不追求穷尽：没登记的原样显示，至少不会把未知伪装成已知。
# 这一位的用途很窄：让非开发用户知道"下一步该找配置、找参数、找机器，还是去核对账单"。
ERROR_LABELS: dict[str, str] = {
    "bad_params": "参数不合法",
    "adapter_not_ready": "适配器尚未接线",
    "adapter_missing": "缺少运行时",
    "adapter_unavailable": "运行时条件不足",
    "adapter_start_failed": "模型进程起不来",
    "adapter_failed": "模型进程异常退出",
    "empty_output": "返回成功但没有有效产物",
    "run_timeout": "执行超过时间上限",
    "worker_lost": "执行进程中断",
    "worker_stopped": "worker 被停止",
    "cancelled": "已取消",
    "cancelled_unconfirmed": "取消时供应商任务可能已建立",
    "internal_error": "平台内部错误",
    "mock_failed": "假适配器模拟失败",
    "provider_missing_key": "缺少密钥",
    "provider_missing_base_url": "缺少供应商地址",
    "provider_unsupported_provider": "供应商未登记",
    "provider_auth": "供应商拒绝鉴权（密钥不对或过期）",
    "provider_insufficient_balance": "供应商余额不足",
    "provider_rate_limit": "供应商限流",
    "provider_invalid_request": "供应商拒绝请求参数",
    "provider_unavailable": "供应商暂时不可用",
    "provider_error": "供应商返回错误",
    "provider_empty_response": "供应商返回空响应",
    "provider_timeout": "供应商轮询超时",
    "provider_empty_output": "供应商任务成功但没有产物",
    "provider_network": "与供应商的网络中断",
    "provider_cloud_run_error": "云端调用进程异常",
}


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=500)
    tags: list[str] = []
    # 建项目时一并指定协作人：弹窗里一次填完，免去"建完再去加人"两步
    member_ids: list[int] = []


# 库里这两列都没有 CHECK 约束（加约束要重建表，而且取值到三期还会扩），
# 所以白名单写在这里：Literal 会让 FastAPI 直接挡掉表外的值，回 422。
ProjectStatus = Literal["active", "archived"]
EpisodeStatus = Literal["draft", "active", "done"]


class ProjectStatusIn(BaseModel):
    status: ProjectStatus


class ProjectBatchIn(BaseModel):
    """批量归档／恢复／加标签。ids 上限 200：一屏能勾出来的量级，
    再多就是拿批量接口当同步用，那不是这一页的用途。"""
    ids: list[int] = Field(min_length=1, max_length=200)
    action: Literal["archive", "restore", "tag"]
    tags: list[str] = []


class ProjectBatchDeleteIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)
    # 必须原样填 BATCH_DELETE_CONFIRM 那句话。单个删除靠输入项目名防手滑，
    # 一次删十几个没法逐个输入名字，就换成一句明确的批量确认词
    confirm: str = ""


class ProjectSkip(BaseModel):
    """批量操作里被跳过的那一项。不报错、但要报原因。"""
    id: int
    name: str = ""
    reason: str = ""


class ProjectNote(BaseModel):
    """批量操作里"做成了，但有话要说"的那一项。

    和 ProjectSkip 分开：skipped 是没做（权限不够），note 是做了但没全做到——
    比如项目标签已经 10 个，新标签一个也加不进去。混在一起报，
    人就会以为"已加标签 3 个项目"里那 3 个都加上了。
    """
    id: int
    name: str = ""
    note: str = ""


class ProjectBatchOut(BaseModel):
    done: list[int] = []
    skipped: list[ProjectSkip] = []
    notes: list[ProjectNote] = []


class ProjectTagsIn(BaseModel):
    tags: list[str] = []


class ProjectUpdateIn(BaseModel):
    """「修改」弹窗一次提交整张表单。

    不做部分更新（缺哪个字段就保留原值）：弹窗里三项都摆出来了，
    三样一起提交才是所见即所得；拆成"只传改了的"反而会出现
    界面上显示 A、库里存 B 的错位。
    """
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=500)
    tags: list[str] = []


class ProjectCostOut(BaseModel):
    """项目中心那一格只读累计。pending 是"其中待核对"的条数，不是金额。"""
    currency: str
    total_micros: int
    jobs: int = 0
    pending: int = 0


class ProjectOut(BaseModel):
    id: int
    name: str
    status: str
    description: str = ""
    owner_id: int
    owner_name: str = ""
    created_at: str
    updated_at: str
    # 空串＝从没打开过。列表按它排序（见 repositories.projects._ORDER），
    # 归档状态走 status，两者都不影响"内容最后改动时间"
    last_opened_at: str = ""
    tags: list[str] = []
    # 只读摘要。集/镜/资产的东家是创作中心，项目中心只显示、不修改
    episode_count: int = 0
    episode_done: int = 0
    shot_count: int = 0
    asset_count: int = 0
    # 这两个计数只服务于"批量删除前清点影响范围"：列表顺手带出来，
    # 那里就不必对每个项目各打一次 /summary
    document_count: int = 0
    image_count: int = 0
    # 成本账的东家是生成中心，项目中心只读这一份累计（按币种分开，不加总）。
    # 不在这里放明细：明细只属于生成中心那一页（设计稿 §3.2）。
    costs: list["ProjectCostOut"] = []

    @classmethod
    def from_row(cls, row) -> "ProjectOut":
        keys = row.keys()

        def costs():
            try:
                raw = json.loads(row["costs"] or "[]")
            except (ValueError, KeyError, TypeError):
                return []
            return [ProjectCostOut(**item) for item in raw if isinstance(item, dict)]

        return cls(id=row["id"], name=row["name"], status=row["status"],
                   description=row["description"],
                   owner_id=row["owner_id"],
                   owner_name=row["owner_name"] if "owner_name" in keys else "",
                   created_at=row["created_at"], updated_at=row["updated_at"],
                   last_opened_at=row["last_opened_at"] or "",
                   tags=json.loads(row["tags"] or "[]"),
                   episode_count=row["episode_count"] if "episode_count" in keys else 0,
                   episode_done=row["episode_done"] if "episode_done" in keys else 0,
                   shot_count=row["shot_count"] if "shot_count" in keys else 0,
                   asset_count=row["asset_count"] if "asset_count" in keys else 0,
                   document_count=row["document_count"] if "document_count" in keys else 0,
                   image_count=row["image_count"] if "image_count" in keys else 0,
                   costs=costs())


class ProjectSummary(BaseModel):
    """删除前要给人看清楚会连带删掉什么。"""
    name: str
    episodes: int
    shots: int
    assets: int
    image_files: int
    # 文档元数据会被删、正文会从工作区摘掉，但那一稿在 Git 历史里还在——
    # 数量还是要报，人要知道的是"这个项目带走了什么"
    documents: int = 0


class ProviderOut(BaseModel):
    id: int
    capability: str
    capability_label: str
    backend: str
    backend_label: str
    provider: str
    model: str
    enabled: bool
    is_default: bool
    has_key: bool          # 只看环境变量里有没有，永远不回传密钥本身
    # key_env 是"实际去哪个变量找"（用户填的名字 > 清单建议 > FV_KEY_<PROVIDER>）；
    # key_env_custom 是库里那一列的原值，界面要能把两者分开显示。
    key_env: str
    key_env_custom: str
    notes: str
    # 健康是每次现查的：state 四档 + 一句人话 + 命中的 adapter（没登记就是 null）
    health: dict
    # P0：把"凭什么认为能用"单独摆出来。health 只回答环境齐不齐，
    # validation 回答"最近一次真调用／假适配器／目标机基准是什么时候、结果如何"。
    validation: dict = {}

    @classmethod
    def from_row(cls, row, has_key: bool, key_env: str, health: dict,
                 validation: dict | None = None) -> "ProviderOut":
        return cls(
            id=row["id"],
            capability=row["capability"],
            capability_label=CAPABILITIES.get(row["capability"], row["capability"]),
            backend=row["backend"],
            backend_label=BACKENDS.get(row["backend"], row["backend"]),
            provider=row["provider"],
            model=row["model"],
            enabled=bool(row["enabled"]),
            is_default=bool(row["is_default"]),
            has_key=has_key,
            key_env=key_env,
            key_env_custom=row["key_env"],
            notes=row["notes"],
            health=health,
            validation=validation or {},
        )


class ProviderUpdate(BaseModel):
    id: int
    provider: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=120)
    enabled: bool = False
    is_default: bool = False
    key_env: str = Field(default="", max_length=64)
    notes: str = Field(default="", max_length=500)


class ProviderOptionOut(BaseModel):
    """给提交表单看的可用槽位：只有 provider/模型与参数白名单，没有密钥名与备注。

    生成中心的"本地／云端"选择靠它——普通编辑者没有 system:config，
    不该为了选一条云端视频去读整张配置页。
    """
    capability: str
    capability_label: str
    backend: str
    backend_label: str
    provider: str
    model: str
    adapter_label: str
    health_state: str
    health_detail: str
    # 提交表单也要能看出"这一槽位从没真跑过"。不带 detail，避免把内部路径泄露给普通编辑者。
    validation_state: str = "never"
    validation_at: str = ""
    models: list[dict] = []


class CurrentProviderOut(BaseModel):
    """给其他模块只读的"当前默认"。

    不带 notes、不带密钥名——别的页面只需要知道"这一类现在走谁、状态如何"。
    """

    capability: str
    capability_label: str
    backend: str
    backend_label: str
    provider: str
    model: str
    adapter: str
    health_state: str
    health_detail: str
    validation_state: str = "never"
    validation_at: str = ""


# ---------- 生成作业（W07） ----------

JOB_STATUSES: dict[str, str] = {
    "queued": "排队中",
    "starting": "启动中",
    "running": "运行中",
    "succeeded": "成功",
    "failed": "失败",
    "cancel_requested": "取消中",
    "cancelled": "已取消",
    "needs_attention": "待核对",
}

JOB_RUNNING = ("starting", "running", "cancel_requested")
JOB_FINISHED = ("succeeded", "failed", "cancelled", "needs_attention")


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
    project_id: int
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
    actual_cost_micros: int | None = None
    delta_micros: int | None = None
    attention_reason: str = ""
    reconciled_at: str | None = None
    reconciled_by_name: str = ""
    reconcile_note: str = ""

    @classmethod
    def from_row(cls, row) -> "JobOut":
        prompt = row["prompt_snapshot"] or ""
        keys = row.keys()
        cost = row["cost_micros"]
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
            retry_count=int(row.get("retry_count") or 0) if hasattr(row, "get") else 0,
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


# ---------- 成本账与参数模板（W08） ----------

class CostBucket(BaseModel):
    capability: str
    capability_label: str
    total_micros: int
    jobs: int


class CostCurrencyOut(BaseModel):
    currency: str
    total_micros: int
    jobs: int
    known: int
    pending: int
    by_capability: list[CostBucket] = []


class LocalCostOut(BaseModel):
    """本地作业没有金额，但有"用了多少机器"——
    它是对账的另一半：判断该不该把这批活挪到云端，看的就是这一块。"""
    jobs: int = 0
    elapsed_ms: int = 0
    cpu_ms: int = 0
    max_rss_kb: int = 0


class CostSummaryOut(BaseModel):
    # 一种币种一个桶，绝不跨币种相加；unpriced 是"云端但没记价"的条数，
    # 它必须显眼——有作业没进账比金额小更值得查
    currencies: list[CostCurrencyOut] = []
    unpriced_jobs: int = 0
    local: LocalCostOut = LocalCostOut()
    total_jobs: int = 0


class GenerationProfileIn(BaseModel):
    """参数模板。project_id 为空＝全局模板，只有管理员能写。"""
    project_id: int | None = None
    name: str = Field(min_length=1, max_length=60)
    capability: Literal["text", "image", "video", "tts", "music"]
    provider_code: str = Field(default="", max_length=80)
    model_code: str = Field(default="", max_length=120)
    params: dict = {}
    is_default: bool = False


class GenerationProfileUpdateIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    params: dict = {}
    is_default: bool = False


class GenerationProfileOut(BaseModel):
    id: int
    project_id: int | None
    project_name: str = ""
    name: str
    capability: str
    capability_label: str
    provider_code: str
    model_code: str
    params: dict
    is_default: bool
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "GenerationProfileOut":
        try:
            params = json.loads(row["params_json"] or "{}")
        except ValueError:
            params = {}
        keys = row.keys()
        return cls(
            id=row["id"], project_id=row["project_id"],
            project_name=row["project_name"] if "project_name" in keys else "",
            name=row["name"], capability=row["capability"],
            capability_label=CAPABILITIES.get(row["capability"], row["capability"]),
            provider_code=row["provider_code"], model_code=row["model_code"],
            params=params if isinstance(params, dict) else {},
            is_default=bool(row["is_default"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


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


class ValidationIn(BaseModel):
    """主动记一次验证。`real_call` 必须带 job_id——真调用的凭据就是那条作业。

    `artifact_review` 额外要求 `passed`：这一档记的是人看过产物之后的结论，
    通过与否必须明说，服务端不猜、界面不默认。
    """
    capability: Literal["text", "image", "video", "tts", "music"]
    backend: Literal["local", "cloud"]
    provider: str = Field(default="", max_length=40)
    model: str = Field(default="", max_length=120)
    kind: Literal["env_check", "mock", "real_call", "target_machine", "artifact_review"]
    job_id: int | None = None
    passed: bool | None = None          # 只有 artifact_review 用：True=通过，False=不通过
    paid_confirmed: bool = False
    detail: str = Field(default="", max_length=500)


class ValidationOut(BaseModel):
    id: int
    capability: str
    capability_label: str
    backend: str
    provider: str
    model: str
    adapter_code: str
    manifest_version: str
    kind: str
    kind_label: str
    state: str
    detail: str
    checks: dict
    params: dict
    job_id: int | None
    provider_request_id: str | None
    http_status: int | None
    latency_ms: int | None
    artifact_path: str | None
    artifact_sha256: str | None
    cost_micros: int | None
    currency: str | None
    paid_confirmed: bool
    created_by_name: str = ""
    created_at: str

    @classmethod
    def from_row(cls, row) -> "ValidationOut":
        keys = row.keys()

        def load(text, fallback):
            try:
                value = json.loads(text or "")
            except ValueError:
                return fallback
            return value if isinstance(value, type(fallback)) else fallback

        return cls(
            id=row["id"], capability=row["capability"],
            capability_label=CAPABILITIES.get(row["capability"], row["capability"]),
            backend=row["backend"], provider=row["provider"], model=row["model"],
            adapter_code=row["adapter_code"], manifest_version=row["manifest_version"],
            kind=row["kind"], kind_label=VALIDATION_KINDS.get(row["kind"], row["kind"]),
            state=row["state"], detail=row["detail"],
            checks=load(row["checks_json"], {}), params=load(row["params_json"], {}),
            job_id=row["job_id"], provider_request_id=row["provider_request_id"],
            http_status=row["http_status"], latency_ms=row["latency_ms"],
            artifact_path=row["artifact_path"], artifact_sha256=row["artifact_sha256"],
            cost_micros=row["cost_micros"], currency=row["currency"],
            paid_confirmed=bool(row["paid_confirmed"]),
            created_by_name=(row["created_by_name"] or "") if "created_by_name" in keys else "",
            created_at=row["created_at"],
        )


# ---------- 内容三件套 ----------

ASSET_KINDS: dict[str, str] = {"character": "角色", "scene": "场景", "prop": "道具"}
AssetKind = Literal["character", "scene", "prop"]


class EpisodeIn(BaseModel):
    title: str = Field(min_length=1, max_length=60)


class EpisodeStatusIn(BaseModel):
    status: EpisodeStatus


class EpisodeOut(BaseModel):
    id: int
    project_id: int
    seq: int
    title: str
    status: str
    shot_count: int = 0

    @classmethod
    def from_row(cls, row) -> "EpisodeOut":
        return cls(id=row["id"], project_id=row["project_id"], seq=row["seq"],
                   title=row["title"], status=row["status"],
                   shot_count=row["shot_count"] if "shot_count" in row.keys() else 0)


class ShotIn(BaseModel):
    summary: str = Field(min_length=1, max_length=500)
    prompt: str = Field(default="", max_length=2000)
    duration: float = Field(default=5, gt=0, le=60)
    asset_ids: list[int] = []


class ShotOut(BaseModel):
    id: int
    episode_id: int
    seq: int
    summary: str
    prompt: str
    duration: float
    asset_ids: list[int]

    @classmethod
    def from_row(cls, row) -> "ShotOut":
        return cls(id=row["id"], episode_id=row["episode_id"], seq=row["seq"],
                   summary=row["summary"], prompt=row["prompt"],
                   duration=row["duration"],
                   asset_ids=json.loads(row["asset_ids"] or "[]"))


class AssetIn(BaseModel):
    kind: AssetKind
    name: str = Field(min_length=1, max_length=60)
    summary: str = Field(default="", max_length=500)
    consistency_prompt: str = Field(default="", max_length=1000)
    aliases: list[str] = []
    tags: list[str] = []
    # 归属范围：project = 当前项目，global = 全局资产池。
    # 写全局要管理员（routers/content.py 里判），读不要——指令 §5.3「能查看不等于能修改」。
    scope: Literal["project", "global"] = "project"


class AssetOut(BaseModel):
    id: int
    project_id: int | None
    kind: str
    kind_label: str
    name: str
    summary: str
    consistency_prompt: str
    aliases: list[str] = []
    tags: list[str] = []
    ref_images: list[str]
    # 这里没有 status：资产那一列（0004 带来的 `draft|ready`）从来没有写入方，
    # 0023 把它从表里摘掉了。"这个资产算不算齐了"在界面上由引用数与有没有定妆图现算，
    # 留一列没人写的状态只会让人以为它有含义。
    # 详情页要摆"创建／最后修改"两个时间。第一次截图里它们是 undefined——
    # 模板用了字段而 schema 没带出来，这类错只有真渲染才会露出来
    created_at: str
    updated_at: str
    # 卡片那一格"关联 N 镜"与详情页的引用清单都靠它，列表 SQL 里一次带出，
    # 不给每个资产再跑一趟反查（N+1）
    ref_shot_count: int = 0

    @classmethod
    def from_row(cls, row) -> "AssetOut":
        keys = row.keys()
        return cls(id=row["id"], project_id=row["project_id"], kind=row["kind"],
                   kind_label=ASSET_KINDS.get(row["kind"], row["kind"]),
                   name=row["name"], summary=row["summary"],
                   consistency_prompt=row["consistency_prompt"],
                   aliases=json.loads(row["aliases"] or "[]") if "aliases" in keys else [],
                   tags=json.loads(row["tags"] or "[]") if "tags" in keys else [],
                   ref_images=json.loads(row["ref_images"] or "[]"),
                   created_at=row["created_at"], updated_at=row["updated_at"],
                   ref_shot_count=(row["ref_shot_count"] or 0)
                   if "ref_shot_count" in keys else 0)


class AssetCoverIn(BaseModel):
    """设封面：把这条路径挪到 ref_images 的第一位。

    只收已登记在该资产下的路径，收不到别的——所以它不能用来把任意文件指成图片。
    """
    path: str = Field(min_length=1, max_length=300)


class AssetShotOut(BaseModel):
    """引用这个资产的镜头。详情页与删除保护共用这一份清单。"""
    shot_id: int
    seq: int
    summary: str
    episode_id: int
    episode_seq: int
    episode_title: str
    project_id: int
    project_name: str


# ---------- 文档（正文在文件系统，库里只有元数据） ----------

DOC_KINDS: dict[str, str] = {"novel": "小说", "script": "剧本", "storyboard": "分镜稿"}
# storyboard 在枚举里但界面上不给建：分镜的真相是 shot 表（一镜一行、要挂生成作业），
# 再存一份 Markdown 就是第二个事实来源。留这一档是为了将来真要出"人读的分镜稿"时不改表。
DocKind = Literal["novel", "script", "storyboard"]
DocStatus = Literal["draft", "polishing", "finalized"]


class DocumentIn(BaseModel):
    kind: DocKind
    title: str = Field(min_length=1, max_length=80)
    # 可空：全项目共用的一份（总纲、人物小传）不属于任何一集（数据字典 §6.1）
    episode_id: int | None = None
    # 首版正文可以为空——"先立个标题占位，晚上再写"是真实用法。
    # 上限不在这里按字符数卡，统一由 config.DOC_MAX_BYTES 按字节判：
    # 两处各写一个数，迟早分叉。
    content: str = ""


class DocumentUpdateIn(BaseModel):
    """改元数据：标题与挂靠的分集。**不含路径**（改名不挪文件，见仓储层注释）。"""
    title: str = Field(min_length=1, max_length=80)
    episode_id: int | None = None


class DocStatusIn(BaseModel):
    status: DocStatus


class DocContentIn(BaseModel):
    """保存正文。`expected_version` 必填——

    没有它就没法判断"你看到的和现在盘上的是不是同一份"，
    而"第二个编辑者不会静默覆盖"是 W05 的验收项。
    所以不提供"不传就当没冲突"这条便道。
    """
    content: str
    expected_version: str = Field(min_length=1, max_length=120)


class DocumentOut(BaseModel):
    id: int
    project_id: int
    episode_id: int | None
    episode_seq: int = 0
    episode_title: str = ""
    kind: str
    kind_label: str
    title: str
    # 相对路径回给前端只为"看得懂"（编辑器标题栏、备份说明），
    # 所有读写都按 id 走，前端传路径进来的接口一个都没有。
    rel_path: str
    status: str
    git_commit: str | None
    created_at: str
    updated_at: str
    # 锁的三列合起来回答界面上那一句"现在谁在改"。NULL＝没人在改（或已过期）
    lock_holder_id: int | None = None
    lock_holder_name: str = ""
    lock_expires_at: str = ""

    @classmethod
    def from_row(cls, row) -> "DocumentOut":
        keys = row.keys()
        return cls(
            id=row["id"], project_id=row["project_id"], episode_id=row["episode_id"],
            episode_seq=row["episode_seq"] if "episode_seq" in keys else 0,
            episode_title=row["episode_title"] if "episode_title" in keys else "",
            kind=row["kind"], kind_label=DOC_KINDS.get(row["kind"], row["kind"]),
            title=row["title"], rel_path=row["rel_path"], status=row["status"],
            git_commit=row["git_commit"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            lock_holder_id=row["holder_user_id"] if "holder_user_id" in keys else None,
            lock_holder_name=(row["holder_name"] or "") if "holder_name" in keys else "",
            lock_expires_at=(row["expires_at"] or "") if "expires_at" in keys else "",
        )


class DocContentOut(BaseModel):
    """一次正文读取。`version` 就是下一次保存要带回去的 expected_version。"""
    id: int
    title: str
    content: str
    version: str
    git_commit: str | None
    lock_holder_id: int | None = None
    lock_holder_name: str = ""
    lock_expires_at: str = ""


DocAiOperation = Literal["draft", "expand", "continue", "polish", "scriptify", "prompt"]


class DocAiOptionsOut(BaseModel):
    """AI 操作弹窗的计价与路由：provider/模型/计价单位/单价/预计量/健康。

    单价来自 adapter 清单的官方价，不由前端决定；数量按选中字数 + 输出上限
    估算，前端只负责显示与让用户勾确认。
    """
    operation: str
    operation_label: str
    provider: str
    model: str
    unit: str
    unit_price_micros: int
    currency: str
    source: str
    max_tokens: int
    chars: int
    estimated_quantity: float
    health_state: str
    health_detail: str
    hint: str


class DocAiSubmitIn(BaseModel):
    """提交一次文档 AI 操作。

    `start/end` 是前端选区偏移，`selection_text` 用来做第二重校验；
    服务端仍以盘上正文为准重新切片。金额必须带确认单与勾选——文本也是云端。
    """
    operation: DocAiOperation
    instruction: str = Field(default="", max_length=2000)
    scope: Literal["document", "selection"] = "document"
    start: int = Field(default=0, ge=0, le=2_000_000)
    end: int = Field(default=0, ge=0, le=2_000_000)
    selection_text: str = Field(default="", max_length=40000)
    estimate: JobEstimateIn
    user_confirmed_cloud: bool = False


class DocAiJobOut(BaseModel):
    job_id: int
    status: str
    status_label: str
    operation: str
    operation_label: str
    apply_mode: str
    base_version: str
    selection_start: int
    selection_end: int
    provider_code: str
    model_code: str
    error_code: str | None = None
    error_message: str | None = None


class DocAiPreviewOut(BaseModel):
    """AI 作业的预览。diff 是“盘上正文 → 应用后正文”的统一 diff。"""
    job_id: int
    status: str
    status_label: str
    operation: str
    operation_label: str
    apply_mode: str
    base_version: str
    current_version: str
    can_apply: bool
    conflict: bool
    diff: str = ""
    content: str = ""
    base_chars: int = 0
    result_chars: int = 0
    applied_chars: int = 0
    note: str = ""
    error_code: str | None = None
    error_message: str | None = None


class DocSaveOut(BaseModel):
    """保存结果。`changed=false` 表示写的和盘上那份一模一样，没有产生新提交。

    `updated_at` 一起回：保存成功后界面上那行"当前提交 / 改动"要当场换掉。
    只改提交号不改时间的话，刚存完一屏还写着旧的改动时间，看着像没存上。
    """
    id: int
    version: str
    git_commit: str | None
    changed: bool
    updated_at: str


class DocAiApplyOut(DocSaveOut):
    """应用 AI 结果后回的和保存同一份字段，另带来源作业与操作。"""
    job_id: int
    operation: str
    operation_label: str


class DocVersionOut(BaseModel):
    commit: str
    author: str
    at: str
    subject: str


class DocDiffOut(BaseModel):
    commit: str
    diff: str


class DocLockOut(BaseModel):
    """拿锁/看锁的结果。`state`：acquired 抢到 / renewed 续期 / held 别人拿着。"""
    doc_id: int
    state: str
    holder_user_id: int | None = None
    holder_name: str = ""
    expires_at: str = ""


# ---------- 用户与角色（系统设置页） ----------

class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    is_superuser: bool
    is_active: bool
    must_change_password: bool
    created_at: str | None = None
    last_login_at: str | None = None
    roles: list[str] = []

    @classmethod
    def from_row(cls, row) -> "UserOut":
        return cls(id=row["id"], username=row["username"],
                   display_name=row["display_name"] or row["username"],
                   is_superuser=bool(row["is_superuser"]),
                   is_active=bool(row["is_active"]),
                   must_change_password=bool(row["must_change_password"]),
                   created_at=row["created_at"], last_login_at=row["last_login_at"],
                   roles=json.loads(row["roles"] or "[]"))


class UserCreateIn(BaseModel):
    # 用户名进 URL 与日志，限成 ASCII，省掉一半的转义问题
    username: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(default="", max_length=40)
    password: str = Field(min_length=6, max_length=64)
    roles: list[str] = []


class UserUpdateIn(BaseModel):
    """只更新传了的那几项。没传 = 不动，和项目那个"整张表单"的取舍不同：
    这里停用和改名是两个不同的动作，混在一次提交里更容易误触。
    角色不在这个包里——改角色是另一个权限点（user:assign），走单独的端点。
    """
    display_name: str | None = Field(default=None, max_length=40)
    is_active: bool | None = None


class PasswordResetIn(BaseModel):
    new_password: str = Field(min_length=6, max_length=64)


class UserRolesIn(BaseModel):
    roles: list[str] = []


class RoleOut(BaseModel):
    id: int
    code: str
    name: str
    description: str = ""
    permissions: list[str] = []


class PermissionOut(BaseModel):
    code: str
    name: str
    group_name: str


class RolePermIn(BaseModel):
    permissions: list[str] = []


# ---------- 项目协作人 ----------

class ProjectMemberOut(BaseModel):
    user_id: int
    username: str
    display_name: str
    created_at: str


class ProjectMemberIn(BaseModel):
    user_id: int


class ProjectTransferIn(BaseModel):
    """转移所有权。只收一个人 id——交出去之后自己降为协作人，没有第二种选项。"""
    user_id: int


class ProjectMembersOut(BaseModel):
    """一页给齐：已受邀的人和可供挑选的人。

    候选也在这里出，是因为挑选名单要按"当前调用者能看见谁"过滤——
    让前端去调 /api/users 就等于绕开了这层过滤，而且只有 user:read
    的人才调得动，项目所有人（可能只有一个制作角色）反而调不动。
    """
    owner_id: int
    members: list[ProjectMemberOut] = []
    candidates: list[ProjectMemberOut] = []


class UserDeleteIn(BaseModel):
    """删用户必须表态：名下项目是转移还是跟着删。

    两个都不给就拒——静默把项目带进坟墓是这类操作最危险的方式。
    """
    transfer_to: int | None = None
    delete_projects: bool = False


# ---------- 文本抽取候选（W13） ----------

ExtractItemKind = Literal["character", "scene", "prop", "shot"]


class ExtractSubmitIn(BaseModel):
    """提交一次抽取。正文永远从文档 id 读，前端不传路径、也不传全文。

    和 W11 的文档 AI 同一条纪律：单价来自清单，数量按“正文 + 输出上限”估算；
    必须带确认单并勾云端确认，否则不落 Job、不花钱。
    """
    project_id: int
    doc_id: int
    instruction: str = Field(default="", max_length=2000)
    estimate: JobEstimateIn
    user_confirmed_cloud: bool = False


class ExtractRunOut(BaseModel):
    id: int
    project_id: int
    doc_id: int | None
    doc_title: str
    job_id: int
    status: str
    status_label: str
    model_code: str
    prompt_version: str
    base_version: str
    current_version: str = ""
    stale: bool = False
    note: str = ""
    counts: dict = {}
    cost_micros: int | None = None
    currency: str | None = None
    billable_status: str = ""
    error_code: str | None = None
    error_message: str | None = None
    created_at: str
    deduped: bool = False


class ExtractItemOut(BaseModel):
    id: int
    run_id: int
    kind: str
    kind_label: str
    payload: dict
    confidence: float | None
    evidence: dict
    status: str
    status_label: str
    asset_id: int | None = None
    decided_by_name: str = ""
    decided_at: str | None = None


class ExtractAcceptIn(BaseModel):
    """确认入库时的覆盖值。只开放资产编辑面上真有意义的几项。"""
    name: str | None = Field(default=None, max_length=60)
    summary: str | None = Field(default=None, max_length=500)
    consistency_prompt: str | None = Field(default=None, max_length=1000)
    aliases: list[str] | None = None
    tags: list[str] | None = None


class ExtractMergeIn(BaseModel):
    asset_id: int


class ExtractRejectIn(BaseModel):
    reason: str = Field(default="", max_length=500)


# ---------- 审核中心（W14） ----------

class ReviewSignIn(BaseModel):
    """按锚点签字。`anchor` 来自 gate 报告，同一版同一锚点改判会替换。"""
    anchor: str = Field(min_length=1, max_length=40)
    decision: Literal["approved", "rejected", "returned"]
    note: str = Field(default="", max_length=500)


class ReviewGateOut(BaseModel):
    id: int | None = None
    project_id: int
    doc_id: int
    doc_title: str
    base_version: str
    current_version: str = ""
    stale: bool = False
    status: str
    status_label: str
    findings: list[dict] = []
    summary: dict = {}
    counts: dict = {}
    signatures: list[dict] = []
    can_sign: bool = False
    created_by_name: str = ""
    created_at: str = ""


class ReviewQueueItem(BaseModel):
    doc_id: int
    doc_title: str
    doc_kind: str
    kind_label: str
    episode_id: int | None = None
    episode_title: str = ""
    doc_status: str
    current_version: str
    gate_status: str = "none"
    gate_status_label: str = "未校验"
    gate_at: str = ""
    findings: int = 0
    errors: int = 0
    warnings: int = 0
    signed: int = 0
    approved: int = 0
    rejected: int = 0
    returned: int = 0
    stale: bool = False


# ---------- 产线中心（0026／阶段 B） ----------

class PipelineRunIn(BaseModel):
    """发起一次生产运行。

    只收三样：哪一集、用哪个模板、以及防连点的幂等键。
    提示词、参数与模型档都不在这里传——它们由模板与项目现有配置决定，
    运行开始时冻结进快照；让人在"开始生产"这一屏改参数，等于每次都要重新审一遍流程。
    """
    episode_id: int
    template_id: int
    idempotency_key: str | None = Field(default=None, max_length=120)


class PipelineCancelIn(BaseModel):
    reason: str = Field(default="", max_length=200)


class PipelineNodeOut(BaseModel):
    """一个节点的流程状态。

    `status` 是界面要显示的那一位（pending 会细分成 blocked／ready，现算不入库）；
    `stored_status` 是库里真正存的那个。Job 的状态只给引用与标签，
    成本、日志、产物一律去生成中心读——节点行不抄 Job 的事实。
    """
    id: int
    run_id: int
    node_key: str
    label: str
    node_type: str
    type_label: str
    status: str
    status_label: str
    stored_status: str
    depends_on: list[str] = []
    job_id: int | None = None
    job_status: str | None = None
    job_status_label: str = ""
    review_id: int | None = None
    retry_count: int = 0
    error_code: str = ""
    error_message: str = ""
    output: dict = {}
    started_at: str = ""
    finished_at: str = ""
    created_at: str


class PipelineEventOut(BaseModel):
    id: int
    event_type: str
    node_id: int | None = None
    node_key: str | None = None
    node_label: str = ""
    payload: dict = {}
    created_at: str


class PipelineProgressOut(BaseModel):
    total: int
    done: int
    failed: int
    waiting_review: int
    attention: int
    percent: float


class PipelineRunOut(BaseModel):
    id: int
    project_id: int
    project_name: str = ""
    episode_id: int
    episode_title: str = ""
    episode_seq: int | None = None
    template_id: int
    template_name: str
    template_version: int
    status: str
    status_label: str
    paused: bool = False
    cancelled: bool = False
    cancel_reason: str = ""
    counts: dict = {}
    progress: PipelineProgressOut
    shots: int = 0
    document: dict | None = None
    nodes: list[PipelineNodeOut] = []
    events: list[PipelineEventOut] = []
    started_at: str = ""
    finished_at: str = ""
    created_at: str
    updated_at: str = ""


class PipelineRunSummaryOut(BaseModel):
    """运行记录列表的一行：不带节点与事件，那是详情页的事。"""
    id: int
    project_id: int
    episode_id: int
    episode_title: str = ""
    episode_seq: int | None = None
    template_id: int
    template_name: str
    template_version: int
    status: str
    status_label: str
    paused: bool = False
    cancelled: bool = False
    counts: dict = {}
    progress: PipelineProgressOut
    started_at: str = ""
    finished_at: str = ""
    created_at: str


class PipelineTemplateNodeOut(BaseModel):
    key: str
    type: str
    label: str
    depends_on: list[str] = []
    fan_out: str | None = None


class PipelineTemplateOut(BaseModel):
    id: int
    name: str
    version: int
    description: str = ""
    enabled: bool = True
    builtin: bool = False
    nodes: list[PipelineTemplateNodeOut] = []
    created_at: str
    updated_at: str


class JobReviewOut(BaseModel):
    """一条作业的产物审核面（0025／裁定 D1）。

    与 `ReviewGateOut` 的区别：这里没有 gate——规则校验器读的是正文，产物没有正文。
    "版本"是产物清单摘要 `base_version`（相对路径＋是否还在＋字节数的 sha256）：
    产物被清理或被新一次生成顶掉，旧签字立刻失配，不会挂着"通过"骗人。
    签字复用 `ReviewSignIn`，锚点是 `artifact`（整条）或 `artifact:<下标>`（某一件）。
    """
    job_id: int
    project_id: int
    capability: str
    capability_label: str
    execution_mode: str
    provider_code: str
    model_code: str
    episode_id: int | None = None
    episode_title: str = ""
    shot_id: int | None = None
    shot_seq: int | None = None
    status: str
    status_label: str
    finished_at: str = ""
    artifacts: list[JobArtifactOut] = []
    base_version: str = ""
    reviewable: bool = False
    reason: str = ""
    stale: bool = False
    counts: dict = {}
    signatures: list[dict] = []
    anchors: list[str] = []
    can_sign: bool = False


# ---------- 音频中心（W15） ----------

AudioKind = Literal["dialogue", "narration", "music", "sfx"]


class AudioClipIn(BaseModel):
    episode_id: int | None = None
    shot_id: int | None = None
    kind: AudioKind = "dialogue"
    speaker: str = Field(default="", max_length=80)
    text: str = Field(min_length=1, max_length=4000)
    notes: str = Field(default="", max_length=500)


class AudioClipUpdateIn(BaseModel):
    kind: AudioKind
    speaker: str = Field(default="", max_length=80)
    text: str = Field(min_length=1, max_length=4000)
    notes: str = Field(default="", max_length=500)


class AudioClipOut(BaseModel):
    id: int
    project_id: int
    episode_id: int | None
    episode_seq: int = 0
    episode_title: str = ""
    shot_id: int | None
    shot_seq: int = 0
    kind: str
    kind_label: str
    speaker: str
    text: str
    status: str
    status_label: str
    path: str | None = None
    duration_ms: int | None = None
    timestamps: list = []
    visemes: list = []
    job_id: int | None = None
    job_status: str | None = None
    job_error: str | None = None
    notes: str = ""
    created_at: str
    updated_at: str
