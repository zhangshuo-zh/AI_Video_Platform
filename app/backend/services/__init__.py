"""跨模块复用的业务动作——**门面**，实体已按域拆到 `services/` 包。

原来这是一个 2392 行的单文件，93 个顶层定义。2026-11-08 按域拆成 12 个模块，
本文件只做 re-export，保证 `import services; services.xxx()` 与
`from services import Xxx` 这 13 个调用方一行都不用改。

## 拆分是纯搬迁

每个函数、类的**正文逐字未动**（连注释一起搬），只换了文件与 import。
已用 `inspect.getsource` 逐函数比对过，确认函数体一字未变。

## 模块划分

    errors          异常类、共享常量（ARTIFACT_ANCHOR 等）、MAX_TAGS
    cascade         删项目／删集的级联清理（动文件与 Git，不进事务）
    documents       文档正文这条链：读—写—Git 提交、编辑锁
    doc_ai          文档 AI 操作（选项、提交、预览、应用）
    extraction      文本抽取候选
    review          文档审核中心
    artifact_review 产物审核（关键帧与镜头视频的人工验收）
    workbench       首页工作台跨模块待办聚合
    audio           音频中心
    dashboard       项目看板与导出
    providers       Provider 路由（resolve_provider）
    jobs            Job 提交、队列健康、成本估算、人工核对

## 分层（依赖方向，无环）

    errors → documents → providers → jobs → doc_ai → artifact_review
    → review → extraction → audio → workbench
    （cascade / dashboard 只依赖 errors 或什么都不依赖）

`errors` 必须是最底层：它装的是异常与共享常量，任何模块都可以依赖它，
但它不依赖任何同包模块。这一条有测试守着。

## 两处刻意的例外

1. `artifact_review` 与 `review` 曾经互相引用（前者用
   `review._review_signature_view`，后者用前者的 `WHOLE_DOC_ANCHOR`）。
   解法不是"允许环"，而是把两个 anchor 常量下沉到 `errors`——
   它们是纯字符串常量，放最底层天经地义。

2. `job_checklist()` 里保留了 `from repositories.review_checks import review_checks`
   这类函数内 import，原样搬过去了（原本就是函数内的）。

## 不要往这个文件里加新东西

新函数加到对应域的模块，再在 `services/__init__.py` 里 re-export。
否则这里会重新长成第二个 2392 行。
"""


from .artifact_review import (
    artifact_version, _artifact_anchors, _job_id_of, _job_reviewable, job_review_state,
    job_review_queue, job_checklist, job_checklist_save, job_retry_batch,
    job_review_batch, job_review_sign
)

from .audio import (
    AUDIO_KINDS, AUDIO_STATUS, audio_clip_view, audio_sync, audio_submit
)

from .cascade import (
    clean_tags, delete_project_cascade, delete_episode_cascade
)

from .doc_ai import (
    text_ai_route, _ai_input, doc_ai_options, submit_doc_ai_job, _ai_ref,
    _ai_job_for_doc, _ai_result_text, doc_ai_job_view, doc_ai_preview, apply_doc_ai
)

from .documents import (
    version_token, read_content, _tend, create_document, save_content,
    preserve_unapplied
)

from .errors import (
    MAX_TAGS, MAX_TAG_LEN, Conflict, LockHeld, DocTooLarge, GitSaveFailed,
    DocAiJobMissing, ExtractMissing, ExtractConflict, ReviewMissing, ReviewStale,
    ReviewNotReviewable, ARTIFACT_ANCHOR, WHOLE_DOC_ANCHOR, AudioInvalid, NoRoute,
    JobInvalid
)

from .extract_candidates import (
    _extract_run_materialize, _extraction_content, extraction_submit, _STATUS_LABELS,
    _ITEM_LABELS, extraction_view, extraction_item_view, extraction_items,
    _load_extract_item, _candidate_fields, extraction_accept, extraction_merge,
    extraction_reject
)

from .jobs import (
    job_kind, shot_source_version, shots_view, shot_asset_ids, shot_variant_ids,
    job_source_stale, _service_engine_gate, submit_job, _cost_of, JobMissing,
    JobStateConflict, _seconds_since, worker_status, job_detail_view, retry_preview,
    json_loads_dict, reconcile_job, mark_job_attention
)

from .project_stats import (
    _DASH_STATUS, project_dashboard, _export_rows, shots_csv, shots_markdown,
    ensure_workspace
)

from .providers import (
    resolve_provider
)

from .review import (
    _REVIEW_STATUS, _DECISION_LABELS, _review_signature_view, review_gate, review_state,
    review_sign, review_queue
)

from .workbench import (
    WORKBENCH_ARTIFACT_WINDOW, _pending_documents_for, _pending_artifacts_for,
    _signed_ready_for, _throughput_for, _pending_documents, _pending_artifacts,
    workbench
)

__all__ = [
    "ARTIFACT_ANCHOR",
    "AUDIO_KINDS",
    "AUDIO_STATUS",
    "AudioInvalid",
    "Conflict",
    "DocAiJobMissing",
    "DocTooLarge",
    "ExtractConflict",
    "ExtractMissing",
    "GitSaveFailed",
    "JobInvalid",
    "JobMissing",
    "JobStateConflict",
    "LockHeld",
    "MAX_TAGS",
    "MAX_TAG_LEN",
    "NoRoute",
    "ReviewMissing",
    "ReviewNotReviewable",
    "ReviewStale",
    "WHOLE_DOC_ANCHOR",
    "WORKBENCH_ARTIFACT_WINDOW",
    "_DASH_STATUS",
    "_DECISION_LABELS",
    "_ITEM_LABELS",
    "_REVIEW_STATUS",
    "_STATUS_LABELS",
    "_ai_input",
    "_ai_job_for_doc",
    "_ai_ref",
    "_ai_result_text",
    "_artifact_anchors",
    "_candidate_fields",
    "_cost_of",
    "_export_rows",
    "_extract_run_materialize",
    "_extraction_content",
    "_job_id_of",
    "_job_reviewable",
    "_load_extract_item",
    "_pending_artifacts",
    "_pending_artifacts_for",
    "_pending_documents",
    "_pending_documents_for",
    "_review_signature_view",
    "_seconds_since",
    "_service_engine_gate",
    "_signed_ready_for",
    "_tend",
    "_throughput_for",
    "apply_doc_ai",
    "artifact_version",
    "audio_clip_view",
    "audio_submit",
    "audio_sync",
    "clean_tags",
    "create_document",
    "delete_episode_cascade",
    "delete_project_cascade",
    "doc_ai_job_view",
    "doc_ai_options",
    "doc_ai_preview",
    "ensure_workspace",
    "extraction_accept",
    "extraction_item_view",
    "extraction_items",
    "extraction_merge",
    "extraction_reject",
    "extraction_submit",
    "extraction_view",
    "job_checklist",
    "job_checklist_save",
    "job_detail_view",
    "job_kind",
    "job_retry_batch",
    "job_review_batch",
    "job_review_queue",
    "job_review_sign",
    "job_review_state",
    "job_source_stale",
    "json_loads_dict",
    "mark_job_attention",
    "preserve_unapplied",
    "project_dashboard",
    "read_content",
    "reconcile_job",
    "resolve_provider",
    "retry_preview",
    "review_gate",
    "review_queue",
    "review_sign",
    "review_state",
    "save_content",
    "shot_asset_ids",
    "shot_source_version",
    "shot_variant_ids",
    "shots_csv",
    "shots_markdown",
    "shots_view",
    "submit_doc_ai_job",
    "submit_job",
    "text_ai_route",
    "version_token",
    "workbench",
    "worker_status",
]

# ---------- 兼容：原单文件暴露的间接引用 ----------
#
# 拆分前 `services.py` 里 `import adapters` / `import extraction_runs` 这些
# 模块级 import 会让它们成为 `services.adapters` 这类属性。有 4 个测试
# 用这个形式做 monkeypatch（`monkeypatch.setattr(services.adapters, "health", ...)`），
# 拆包后这些属性就不再存在了。
#
# 这里把它们补回来，**只为兼容这些用法**。新代码请直接
# `import adapters`，不要写 `services.adapters`——那会让"services 依赖谁"
# 变得看不出来。
import adapters as adapters                    # noqa: E402,F401
from config import ALLOW_MOCK                  # noqa: E402,F401  —— 测试用 monkeypatch.setattr("services.ALLOW_MOCK", ...)
from models import JOB_STATUSES                # noqa: E402,F401  —— `reviewkit.py` 通过 services 取它（既有的间接依赖）
from repositories.extraction import runs as extraction_runs   # noqa: E402,F401
from repositories.jobs import jobs as job_repo                # noqa: E402,F401
from repositories.model_center import model_center as registration_repo  # noqa: E402,F401
