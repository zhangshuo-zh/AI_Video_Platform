from pydantic import BaseModel, Field


# ---------- 生产工作台（跨模块聚合，键最多的一组） ----------
#
# 这一组要注意两类键：
#   1. **动态键字典**：`by_status`（状态名→数）、`by_capability`（能力→数）、
#      `by_currency`（币种→金额）、`signs`（决策→数）、`disk.per`（产物类别→样本）。
#      这些**不能用固定字段接**，否则库里冒出第六类能力时字段会被静默剔掉。
#      一律用 `dict[str, ...]`：Pydantic 会原样保留任意键。
#   2. **可为 None 的数**：样本为 0 时不给 0，给 None（界面据此显示"暂无样本"）。
#      写成 `int = 0` 会把"没有数据"伪装成"数值是零"，那是两回事。


class ProductionEntry(BaseModel):
    """「去哪个模块、哪个二级页」。看板每一格都带一个。"""
    module: str = ""
    sub: int = 0




class ProductionEpisodeBrief(BaseModel):
    """一集在生产总览里的进度。"""
    episode_id: int
    seq: int = 0
    title: str = ""
    status: str = ""
    shots: int = 0
    videos_succeeded: int = 0
    composition_versions: int = 0
    delivered: bool = False




class ProductionAssets(BaseModel):
    total: int = 0
    no_ref_image: int = 0
    no_consistency: int = 0
    character_without_variant: int = 0




class ProductionOverview(BaseModel):
    project_id: int
    episodes: int = 0
    delivered_episodes: int = 0
    progress: int | None = None
    current: ProductionEpisodeBrief | None = None
    per_episode: list[ProductionEpisodeBrief] = Field(default_factory=list)
    assets: ProductionAssets




class ProductionStage(BaseModel):
    key: str
    label: str = ""
    entry: ProductionEntry | None = None
    status: str = ""
    done: int = 0
    total: int = 0
    pending: int = 0
    failed: int = 0
    blocked: int = 0
    note: str = ""




class ProductionNextTask(BaseModel):
    stage: str = ""
    label: str = ""
    count: int = 0
    status: str = ""
    note: str = ""
    entry: ProductionEntry | None = None




class ProductionCounts(BaseModel):
    documents: int = 0
    extraction_pending: int = 0
    assets_referenced: int = 0
    videos_succeeded: int = 0
    videos_failed: int = 0
    unsigned: int = 0
    attention: int = 0
    composition_versions: int = 0




class ProductionBoard(BaseModel):
    """一集的产线看板。`episode_id` 为 None 时是"还没有集"的空板。"""
    episode_id: int | None = None
    episode_seq: int | None = None
    episode_title: str = ""
    shots: int = 0
    stages: list[ProductionStage] = Field(default_factory=list)
    next_tasks: list[ProductionNextTask] = Field(default_factory=list)
    counts: ProductionCounts | None = None




class ProductionMemory(BaseModel):
    machine_total_gb: float | None = None
    # 动态键：能力 → 内存读数
    by_capability: dict[str, dict] = Field(default_factory=dict)
    # **dict 不是 str**：这里装的是"每类能力最多能并行几条"的分档依据，
    # 不是一个句子。初版按名字猜成 str，逐键比对时当场被 Pydantic 拒掉
    # （Input should be a valid string, input_value={}）——正好说明
    # 给聚合视图建模**必须测绘、不能凭印象**。
    parallel_reference: dict = Field(default_factory=dict)
    note: str = ""




class ProductionDisk(BaseModel):
    """磁盘读数。`diskplan.disk_readout()` 的形状，字段多是因为它要回答
    "还要多少、只剩多少、够不够"，每一项界面上都有位置。"""
    shots_missing: int = 0
    per: dict[str, dict] = Field(default_factory=dict)     # 动态键：产物类别
    estimated_need_bytes: int | None = None
    safety_ratio: float | None = None
    free_bytes: int | None = None
    floor_bytes: int | None = None
    min_free_gb: int | None = None
    basis: str = ""
    # **可为 None**：读不到盘余时不是 0，是"不知道"。
    shortfall_bytes: int | None = None
    has_sample: bool = False
    sentence: str = ""




class ProductionTeam(BaseModel):
    members: list[dict] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)




class ProductionMoney(BaseModel):
    by_currency: dict[str, dict] = Field(default_factory=dict)   # 动态键：币种
    unpriced_cloud: int = 0
    cost_scope: str = ""




class ProductionEstimate(BaseModel):
    """工期估算。样本不足时整块 `not_computed` 会说为什么给不出。"""
    remaining_shots: int = 0
    sample_jobs: int = 0
    avg_ms_per_shot: float | None = None
    minutes: float | None = None
    minutes_if_all_new_prompts: float | None = None
    cold_prompt_count: int = 0
    warm_prompt_count: int = 0
    cold_extra_ms: int | None = None
    basis: str = ""




class ProductionStats(BaseModel):
    jobs_total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)      # 动态键：状态名
    # 动态键：能力名 → **一包计数**（total/succeeded/failed/needs_attention/
    # other/total_ms/timed/max_ms/avg_ms）。初版按名字猜成 `dict[str, int]`，
    # 被 Pydantic 当场拒掉——这就是"必须测绘"的第二个例子。
    by_capability: dict[str, dict] = Field(default_factory=dict)
    top_error_codes: list[dict] = Field(default_factory=list)
    retries: dict = Field(default_factory=dict)
    memory: ProductionMemory | None = None
    disk: ProductionDisk | None = None
    team: ProductionTeam | None = None
    money: ProductionMoney | None = None
    # **两层**：外层键是签字对象种类（`target_kind`，如 document/artifact），
    # 内层是决策（approved/rejected/returned）→ 数。
    # 初版只写了一层 `dict[str, int]`，被 Pydantic 拒掉（input_value={'approved': 1}）。
    signs: dict[str, dict[str, int]] = Field(default_factory=dict)
    estimate: ProductionEstimate | None = None
    not_computed: list[str] = Field(default_factory=list)




class ProjectProductionOut(BaseModel):
    overview: ProductionOverview
    board: ProductionBoard
    stats: ProductionStats
    # 这两项是**常量清单**（产线阶段表与对外状态词），随响应一起下发是为了
    # 让界面不用再抄一份；它们不由用户数据驱动。
    stages: list[dict] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
