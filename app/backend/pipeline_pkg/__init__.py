"""产线编排——**门面**，实体已按域拆到 `pipeline_pkg/` 包。

原来是一个 1860 行、71 个顶层定义的单文件。2026-11-08 拆成 9 个模块，
本文件只做 re-export，保证 `import pipeline; pipeline.xxx()` 这 6 个调用方
（`worker.py`／`seasons.py`／`agent_tools.py`／`routers/pipeline.py` 等）与
测试里对 `pipeline._submit_job_node` 这类**内部函数**的直调一行都不用改。

## 拆分是纯搬迁

每个函数、类的**正文逐字未动**（连注释一起搬）。已用 `inspect.getsource`
逐函数比对过。

## 模块划分（按依赖方向，无环）

    constants    节点类型/状态/事件常量表、三个异常
    graph        纯图工具：_deps_of / _node_map / _snapshot_of / _upstream_job
    gates        判定闸门：retry_gate（"这一格能不能重试"的唯一出处）
    templates    内置模板、模板校验、补齐模板表
    views        视图：run_status / node_view / run_view / template_view
    run_create   建运行：预检、输入快照、展开节点
    run_control  暂停/恢复/取消/重试
    execute      节点执行：建 Job、建成片、等签字、内部检查
    dispatch     调度骨架：tick / advance_run / 派发 / 对账

## 两处刻意的延迟导入

`create_run()` / `retry_node()` 末尾要"建完立刻推进一轮"，即调
`dispatch.advance_run()`；而 `dispatch` 经 `execute` 又依赖 `run_create`
（建 Job 要用输入快照）。模块级互相 import 会成环，所以那两处调用改成
**函数内延迟导入**——与项目里 `reviewkit ↔ services` 的处理方式一致。

**不要往这个文件里加新东西**：新函数加到 `pipeline_pkg/<域>.py`，再在这里
re-export 一次。
"""


from .constants import (
    MAX_DISPATCH_PER_TICK, MAX_LAYERS, NODE_TYPE_LABELS, JOB_NODES, REVIEW_NODES,
    FANOUT_TYPES, INTERNAL_NODES, NODE_STATUS_LABELS, EVENT_LABELS, RUN_STATUS_LABELS,
    PipelineInvalid, PipelineMissing, PipelineConflict
)

from .dispatch import (
    tick, advance_run, _prefetch_export_probes, _sync_job_nodes, _sync_review_nodes,
    _dispatch_ready, _unreachable, _finalize
)

from .execute import (
    _execute_node, _missing_image_params, _submit_job_node, _commit_submission,
    compose_shot_jobs, _submit_composition_node, _commit_composition, _arm_review_node,
    _update_node_input, _run_internal_node, _NodeFail, episode_asset_ids
)

from .gates import (
    retry_gate
)

from .graph import (
    _node_map, _find_approval, _idempotency_key, _upstream_image, _deps_of, _shot_of_key,
    _snapshot_of, _upstream_job
)

from .run_control import (
    pause_run, resume_run, cancel_run, retry_node, _need_run
)

from .run_create import (
    _pick_document, _ref_image_state, build_input_snapshot, preflight, create_run,
    expand_nodes, _now
)

from .templates import (
    STANDARD_EPISODE, STANDARD_EPISODE_V2, IMAGE_PREVIEW, VIDEO_ONLY, BUILTIN_TEMPLATES,
    ensure_builtin_templates, validate_definition, _find_cycle, _definition_of
)

from .views import (
    run_status, _has_retry, _display_status, node_view, run_view, _event_view,
    runs_for_project, template_view
)

__all__ = [
    "BUILTIN_TEMPLATES",
    "EVENT_LABELS",
    "FANOUT_TYPES",
    "IMAGE_PREVIEW",
    "INTERNAL_NODES",
    "JOB_NODES",
    "MAX_DISPATCH_PER_TICK",
    "MAX_LAYERS",
    "NODE_STATUS_LABELS",
    "NODE_TYPE_LABELS",
    "PipelineConflict",
    "PipelineInvalid",
    "PipelineMissing",
    "REVIEW_NODES",
    "RUN_STATUS_LABELS",
    "STANDARD_EPISODE",
    "STANDARD_EPISODE_V2",
    "VIDEO_ONLY",
    "_NodeFail",
    "_arm_review_node",
    "_commit_composition",
    "_commit_submission",
    "_definition_of",
    "_deps_of",
    "_dispatch_ready",
    "_display_status",
    "_event_view",
    "_execute_node",
    "_finalize",
    "_find_approval",
    "_find_cycle",
    "_has_retry",
    "_idempotency_key",
    "_missing_image_params",
    "_need_run",
    "_node_map",
    "_now",
    "_pick_document",
    "_prefetch_export_probes",
    "_ref_image_state",
    "_run_internal_node",
    "_shot_of_key",
    "_snapshot_of",
    "_submit_composition_node",
    "_submit_job_node",
    "_sync_job_nodes",
    "_sync_review_nodes",
    "_unreachable",
    "_update_node_input",
    "_upstream_image",
    "_upstream_job",
    "advance_run",
    "build_input_snapshot",
    "cancel_run",
    "compose_shot_jobs",
    "create_run",
    "ensure_builtin_templates",
    "episode_asset_ids",
    "expand_nodes",
    "node_view",
    "pause_run",
    "preflight",
    "resume_run",
    "retry_gate",
    "retry_node",
    "run_status",
    "run_view",
    "runs_for_project",
    "template_view",
    "tick",
    "validate_definition",
    "NODE_ATTENTION",
    "NODE_CANCELLED",
    "NODE_DONE",
    "NODE_FAILED",
    "NODE_FINAL",
    "NODE_PENDING",
    "NODE_REVIEW",
    "NODE_RUNNING",
    "NODE_SKIPPED",
    "pipeline_repo",
]

# ---------- 兼容：原单文件暴露的间接引用 ----------
#
# 拆分前 `pipeline.py` 里 `from repositories.pipeline import (NODE_PENDING, ...)`
# 让这些常量成为 `pipeline.NODE_PENDING` 这类属性。`seasons.py` 直接用这个形式
# （`pipeline.NODE_ATTENTION` 等 8 个）。拆包后这些属性不再存在，这里补回来。
#
# 新代码请直接 `from repositories.pipeline import NODE_PENDING`，
# 不要写 `pipeline.NODE_PENDING`——那会让"pipeline 依赖谁"变得看不出来。
from repositories.pipeline import (  # noqa: E402,F401
    NODE_ATTENTION,
    NODE_CANCELLED,
    NODE_DONE,
    NODE_FAILED,
    NODE_FINAL,
    NODE_PENDING,
    NODE_REVIEW,
    NODE_RUNNING,
    NODE_SKIPPED,
    pipeline_repo,
)
