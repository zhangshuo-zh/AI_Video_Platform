from pydantic import BaseModel, Field

from .jobs import JobArtifactOut


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
    # 「这一格现在能不能重试」由服务端判（`pipeline.retry_gate`），界面按它摆钮。
    # 让前端自己按 status==='failed' 猜，就会出现被驳回的审核节点挂着一颗
    # 点了必吃 409 的按钮——按钮的有无是业务规则，不是样式。
    retryable: bool = False
    retry_hint: str = ""
    error_code: str = ""
    error_message: str = ""
    # 七字段诊断（`error_catalog.for_node`）。它必须写在模型里：
    # `response_model` 会丢掉模型外的字段，以前一处后端算好了、HTTP 层却什么也没带出来，
    # 界面只能显示"没有原话"——pytest 直接调 `run_view()` 还看不见这一层丢失。
    diagnosis: dict = {}
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
    # 事件中文名由服务端下发（`pipeline.EVENT_LABELS`）。原来只住在界面，
    # 与 0031 的 CHECK 各写一份、中间没有守卫——清单加一档界面就少说一句。
    event_labels: dict = {}
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
