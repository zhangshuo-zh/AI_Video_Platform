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


class ProjectBatchOut(BaseModel):
    done: list[int] = []
    skipped: list[ProjectSkip] = []


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

    @classmethod
    def from_row(cls, row, has_key: bool, key_env: str, health: dict) -> "ProviderOut":
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

    @classmethod
    def from_row(cls, row) -> "JobOut":
        prompt = row["prompt_snapshot"] or ""
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
            provider_request_id=row["provider_request_id"],
            has_log=bool(row["log_path"]),
            has_artifacts=row["result_paths_json"] not in ("", "[]"),
            quantity=row["quantity"], unit=row["unit"],
            cost_micros=row["cost_micros"], currency=row["currency"],
            billable_status=row["billable_status"],
            billable_status_label=BILLABLE_LABELS.get(row["billable_status"],
                                                      row["billable_status"]),
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
    """日志尾部。`path` 是相对 Job 目录的路径，不是绝对磁盘路径。"""

    path: str | None
    text: str


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


# ---------- 内容三件套 ----------

ASSET_KINDS: dict[str, str] = {"character": "角色", "scene": "场景", "prop": "物品"}
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
    status: str
    asset_ids: list[int]

    @classmethod
    def from_row(cls, row) -> "ShotOut":
        return cls(id=row["id"], episode_id=row["episode_id"], seq=row["seq"],
                   summary=row["summary"], prompt=row["prompt"],
                   duration=row["duration"], status=row["status"],
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
    status: str
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
                   status=row["status"],
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
