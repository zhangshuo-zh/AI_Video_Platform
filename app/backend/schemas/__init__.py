"""请求/响应的数据结构（按域拆包后的门面）。

原来这里是一个 2513 行的 `models.py`，把 172 个定义挤在一个文件里：项目、作业、
成本、资产、文档、审核、产线、音频、成片、模型中心……改一处要滚很远，
两个人同时改不同域必冲突。

现在按域拆到 `schemas/` 下的 21 个模块，**本文件只做 re-export**，
保证 `from models import XxxIn` 这 25 个调用方一行都不用改。

## 拆分是纯搬迁

每个类、函数、常量的**正文逐字未动**（连注释一起搬），只是换了文件。
唯一的改动是 import：原来靠"同一文件内随便引"，现在显式写成 `from .xxx import`。

## 分层顺序（照依赖图拓扑排的，不要随便改）

    enums → jobs → costs → content → review → audio → composition → model_center
    → users → members → system → diagnosis → workbench → dashboard → production
    → assets → documents → extraction → pipeline → tokens → projects

这条链是无环的（用 `graphlib.TopologicalSorter` 验过）。加新模块时先想清楚它依赖谁。
`enums` 是最底层：纯常量（`CAPABILITIES`／`UNITS`／`ASSET_KINDS`／各种 `Literal`
别名）与 `job_state` 的状态组，`repositories/ordering.py` 也直接依赖它。

## 两处遗留（有意保留）

1. `schemas/jobs.py` 的 `JobOut.from_row()` 里有一句
   `from repositories.jobs import retry_reason` 的函数内延迟导入（避开
   models ↔ repositories 环）。它跟着搬过去了，**没删**——删除需要改成
   "由调用方传入 retry_hint"，那会动所有调用 `JobOut.from_row` 的地方。

2. `models.py` 现在只是个 `from schemas import *` 的门面。**不要往里加新东西**：
   新模型加到 `schemas/<域>.py`，再在 `schemas/__init__.py` 里 re-export 一次。
   否则这里会重新长成第二个大文件。
"""


from .assets import (
    AssetProfileIn, AssetProfileOut, VariantIn, VariantUpdateIn, VariantOut, AssetGenIn,
    AssetGenOut, AssetGenPreviewOut, AssetImageImportIn, AssetImageImportOut
)

from .audio import (
    AudioClipIn, AudioClipUpdateIn, AudioClipOut
)

from .composition import (
    CompositionIn, CompositionOut, SubtitleFileOut, SeasonIn, SeasonCancelIn, SeasonOut
)

from .content import (
    EpisodeIn, EpisodeStatusIn, EpisodeOut, ShotIn, ContextIn, ShotOut, AssetIn,
    AssetOut, AssetCoverIn, AssetShotOut
)

from .costs import (
    CostBucket, CostCurrencyOut, LocalCostOut, CostSummaryOut, GenerationProfileIn,
    GenerationProfileUpdateIn, GenerationProfileOut, ValidationIn, ValidationOut
)

from .dashboard import (
    DashboardBucket, DashboardSignBucket, DashboardCostRow, ProjectDashboardOut
)

from .diagnosis import (
    JobDiagnosisBody, JobDiagnosisOut
)

from .documents import (
    DocumentIn, DocumentUpdateIn, DocStatusIn, DocContentIn, DocumentOut, DocContentOut,
    DocAiOptionsOut, DocAiSubmitIn, DocAiJobOut, DocAiPreviewOut, DocSaveOut,
    DocAiApplyOut, DocVersionOut, DocDiffOut, DocLockOut
)

from .enums import (
    CAPABILITIES, BACKENDS, BACKEND_ORDER, UNITS, BILLABLE_LABELS, VALIDATION_KINDS,
    ERROR_LABELS, ProjectStatus, EpisodeStatus, JOB_STATUSES, JOB_RUNNING, JOB_FINISHED,
    ASSET_KINDS, AssetKind, VARIANT_KINDS, VariantType, ASSET_GEN_OPERATIONS,
    AssetGenOperation, DOC_KINDS, DocKind, DocStatus, DocAiOperation, ExtractItemKind,
    AudioKind
)

from .extraction import (
    ExtractSubmitIn, ExtractRunOut, ExtractItemOut, ExtractAcceptIn, ExtractMergeIn,
    ExtractRejectIn
)

from .jobs import (
    JobEstimateIn, JobSubmitIn, JobOut, JobDetailOut, JobLogOut, JobArtifactOut,
    WorkerStatusOut, JobReconcileIn, JobAttentionIn, JobRetryBatchIn, JobRetryPreviewOut
)

from .members import (
    ProjectMemberOut, ProjectMemberIn, ProjectTransferIn, ProjectMembersOut,
    UserDeleteIn
)

from .model_center import (
    REG_SOURCES, POLICY_PREFERENCES, ModelRegistrationIn, ModelRegistrationPatch,
    EngineInstanceIn, EngineInstancePatch, EngineInstanceOut, ModelPolicyIn, AgentRunIn
)

from .pipeline import (
    PipelineRunIn, PipelineCancelIn, PipelineNodeOut, PipelineEventOut,
    PipelineProgressOut, PipelineRunOut, PipelineRunSummaryOut, PipelineTemplateNodeOut,
    PipelineTemplateOut, JobReviewOut
)

from .production import (
    ProductionEntry, ProductionEpisodeBrief, ProductionAssets, ProductionOverview,
    ProductionStage, ProductionNextTask, ProductionCounts, ProductionBoard,
    ProductionMemory, ProductionDisk, ProductionTeam, ProductionMoney,
    ProductionEstimate, ProductionStats, ProjectProductionOut
)

from .projects import (
    ProjectIn, ProjectStatusIn, ProjectBatchIn, ProjectBatchDeleteIn, ProjectSkip,
    ProjectNote, ProjectBatchOut, ProjectTagsIn, ProjectUpdateIn, ProjectCostOut,
    ProjectOut, ProjectSummary, ProviderOut, ProviderUpdate, ProviderOptionOut,
    CurrentProviderOut
)

from .review import (
    ReviewSignIn, ReviewCheckItemIn, ReviewCheckIn, ReviewBatchItemIn, ReviewBatchIn,
    ReviewGateOut, ReviewQueueItem
)

from .system import (
    HealthOut
)

from .tokens import (
    provider_slot_version, _digest, asset_version, profile_version
)

from .users import (
    UserOut, UserCreateIn, UserUpdateIn, PasswordResetIn, UserRolesIn, RoleOut,
    PermissionOut, RolePermIn
)

from .workbench import (
    WorkbenchCounts, WorkbenchProject, WorkbenchThroughput, WorkbenchOut
)

__all__ = [
    "ASSET_GEN_OPERATIONS",
    "ASSET_KINDS",
    "AgentRunIn",
    "AssetCoverIn",
    "AssetGenIn",
    "AssetGenOperation",
    "AssetGenOut",
    "AssetGenPreviewOut",
    "AssetImageImportIn",
    "AssetImageImportOut",
    "AssetIn",
    "AssetKind",
    "AssetOut",
    "AssetProfileIn",
    "AssetProfileOut",
    "AssetShotOut",
    "AudioClipIn",
    "AudioClipOut",
    "AudioClipUpdateIn",
    "AudioKind",
    "BACKENDS",
    "BACKEND_ORDER",
    "BILLABLE_LABELS",
    "CAPABILITIES",
    "CompositionIn",
    "CompositionOut",
    "ContextIn",
    "CostBucket",
    "CostCurrencyOut",
    "CostSummaryOut",
    "CurrentProviderOut",
    "DOC_KINDS",
    "DashboardBucket",
    "DashboardCostRow",
    "DashboardSignBucket",
    "DocAiApplyOut",
    "DocAiJobOut",
    "DocAiOperation",
    "DocAiOptionsOut",
    "DocAiPreviewOut",
    "DocAiSubmitIn",
    "DocContentIn",
    "DocContentOut",
    "DocDiffOut",
    "DocKind",
    "DocLockOut",
    "DocSaveOut",
    "DocStatus",
    "DocStatusIn",
    "DocVersionOut",
    "DocumentIn",
    "DocumentOut",
    "DocumentUpdateIn",
    "ERROR_LABELS",
    "EngineInstanceIn",
    "EngineInstanceOut",
    "EngineInstancePatch",
    "EpisodeIn",
    "EpisodeOut",
    "EpisodeStatus",
    "EpisodeStatusIn",
    "ExtractAcceptIn",
    "ExtractItemKind",
    "ExtractItemOut",
    "ExtractMergeIn",
    "ExtractRejectIn",
    "ExtractRunOut",
    "ExtractSubmitIn",
    "GenerationProfileIn",
    "GenerationProfileOut",
    "GenerationProfileUpdateIn",
    "HealthOut",
    "JOB_FINISHED",
    "JOB_RUNNING",
    "JOB_STATUSES",
    "JobArtifactOut",
    "JobAttentionIn",
    "JobDetailOut",
    "JobDiagnosisBody",
    "JobDiagnosisOut",
    "JobEstimateIn",
    "JobLogOut",
    "JobOut",
    "JobReconcileIn",
    "JobRetryBatchIn",
    "JobRetryPreviewOut",
    "JobReviewOut",
    "JobSubmitIn",
    "LocalCostOut",
    "ModelPolicyIn",
    "ModelRegistrationIn",
    "ModelRegistrationPatch",
    "POLICY_PREFERENCES",
    "PasswordResetIn",
    "PermissionOut",
    "PipelineCancelIn",
    "PipelineEventOut",
    "PipelineNodeOut",
    "PipelineProgressOut",
    "PipelineRunIn",
    "PipelineRunOut",
    "PipelineRunSummaryOut",
    "PipelineTemplateNodeOut",
    "PipelineTemplateOut",
    "ProductionAssets",
    "ProductionBoard",
    "ProductionCounts",
    "ProductionDisk",
    "ProductionEntry",
    "ProductionEpisodeBrief",
    "ProductionEstimate",
    "ProductionMemory",
    "ProductionMoney",
    "ProductionNextTask",
    "ProductionOverview",
    "ProductionStage",
    "ProductionStats",
    "ProductionTeam",
    "ProjectBatchDeleteIn",
    "ProjectBatchIn",
    "ProjectBatchOut",
    "ProjectCostOut",
    "ProjectDashboardOut",
    "ProjectIn",
    "ProjectMemberIn",
    "ProjectMemberOut",
    "ProjectMembersOut",
    "ProjectNote",
    "ProjectOut",
    "ProjectProductionOut",
    "ProjectSkip",
    "ProjectStatus",
    "ProjectStatusIn",
    "ProjectSummary",
    "ProjectTagsIn",
    "ProjectTransferIn",
    "ProjectUpdateIn",
    "ProviderOptionOut",
    "ProviderOut",
    "ProviderUpdate",
    "REG_SOURCES",
    "ReviewBatchIn",
    "ReviewBatchItemIn",
    "ReviewCheckIn",
    "ReviewCheckItemIn",
    "ReviewGateOut",
    "ReviewQueueItem",
    "ReviewSignIn",
    "RoleOut",
    "RolePermIn",
    "SeasonCancelIn",
    "SeasonIn",
    "SeasonOut",
    "ShotIn",
    "ShotOut",
    "SubtitleFileOut",
    "UNITS",
    "UserCreateIn",
    "UserDeleteIn",
    "UserOut",
    "UserRolesIn",
    "UserUpdateIn",
    "VALIDATION_KINDS",
    "VARIANT_KINDS",
    "ValidationIn",
    "ValidationOut",
    "VariantIn",
    "VariantOut",
    "VariantType",
    "VariantUpdateIn",
    "WorkbenchCounts",
    "WorkbenchOut",
    "WorkbenchProject",
    "WorkbenchThroughput",
    "WorkerStatusOut",
    "_digest",
    "asset_version",
    "profile_version",
    "provider_slot_version",
]
