import json

from repositories.pipeline import pipeline_repo

from .constants import FANOUT_TYPES, NODE_TYPE_LABELS, PipelineInvalid


# ---------- 内置模板 ----------
# 定义写在代码里，但**运行时的事实来源是 `pipeline_template` 表**：
# `ensure_builtin_templates()` 只做 INSERT OR IGNORE，启动多少次都不会改已发布的版本。
# 要改内置流程＝version+1 再加一份，已经在跑的运行仍认它创建时那一版。
STANDARD_EPISODE = {
    "nodes": [
        {"key": "read_project", "type": "project_input", "depends_on": []},
        {"key": "read_episode", "type": "episode_input", "depends_on": ["read_project"]},
        {"key": "read_script", "type": "document_input", "depends_on": ["read_episode"]},
        {"key": "check_shots", "type": "shot_validate", "depends_on": ["read_script"]},
        {"key": "check_assets", "type": "asset_validate", "depends_on": ["check_shots"]},
        {"key": "confirm_script", "type": "human_gate", "depends_on": ["check_assets"]},
        {"key": "keyframe", "type": "image_generate", "depends_on": ["confirm_script"],
         "fan_out": "shot"},
        {"key": "review_keyframe", "type": "image_review", "depends_on": ["keyframe"],
         "fan_out": "shot"},
        {"key": "shot_video", "type": "video_generate", "depends_on": ["review_keyframe"],
         "fan_out": "shot"},
        {"key": "check_video", "type": "artifact_validate", "depends_on": ["shot_video"],
         "fan_out": "shot"},
        {"key": "review_shot", "type": "shot_review", "depends_on": ["check_video"],
         "fan_out": "shot"},
        {"key": "check_audio", "type": "audio_prepare", "depends_on": ["review_shot"]},
        {"key": "prepare_export", "type": "export_prepare", "depends_on": ["check_audio"]},
    ],
}



# v2（阶段 D）：在 v1 的"音频检查"之后接上真正的成片流程——合成走 Job，
# 合成后自动验收，最后等一次成片审核。v1 保留：已经在跑的运行冻结在 v1，
# 旧版本的行为（export_prepare 只占位 skipped）不能被新模板悄悄改写。
STANDARD_EPISODE_V2 = {
    "nodes": STANDARD_EPISODE["nodes"][:-1] + [
        {"key": "compose", "type": "export_compose",
         "depends_on": ["check_audio"]},
        {"key": "check_composition", "type": "export_validate",
         "depends_on": ["compose"]},
        {"key": "review_composition", "type": "export_review",
         "depends_on": ["check_composition"]},
    ],
}



IMAGE_PREVIEW = {
    "nodes": [
        {"key": "read_project", "type": "project_input", "depends_on": []},
        {"key": "read_episode", "type": "episode_input", "depends_on": ["read_project"]},
        {"key": "check_shots", "type": "shot_validate", "depends_on": ["read_episode"]},
        {"key": "check_assets", "type": "asset_validate", "depends_on": ["check_shots"]},
        {"key": "keyframe", "type": "image_generate", "depends_on": ["check_assets"],
         "fan_out": "shot"},
        {"key": "review_keyframe", "type": "image_review", "depends_on": ["keyframe"],
         "fan_out": "shot"},
    ],
}



VIDEO_ONLY = {
    "nodes": [
        {"key": "read_project", "type": "project_input", "depends_on": []},
        {"key": "read_episode", "type": "episode_input", "depends_on": ["read_project"]},
        {"key": "check_shots", "type": "shot_validate", "depends_on": ["read_episode"]},
        {"key": "shot_video", "type": "video_generate", "depends_on": ["check_shots"],
         "fan_out": "shot"},
        {"key": "check_video", "type": "artifact_validate", "depends_on": ["shot_video"],
         "fan_out": "shot"},
        {"key": "review_shot", "type": "shot_review", "depends_on": ["check_video"],
         "fan_out": "shot"},
    ],
}



BUILTIN_TEMPLATES = [
    ("标准漫剧单集生产", 1,
     "项目 → 剧本 → 分镜 → 资产 → 人工确认 → 关键帧 → 关键帧验收 → 镜头视频 → "
     "产物检查 → 镜头审核 → 音频检查 → 成片准备。一镜一条支线，失败只重试那一镜。",
     STANDARD_EPISODE),
    ("标准漫剧单集生产", 2,
     "在 v1 的全流程之后接成片：音频检查 → FFmpeg 合成（走 Job）→ 成片验收 → "
     "成片审核。合成前要求每镜视频已通过审核，缺片或未审会明确拒绝。",
     STANDARD_EPISODE_V2),
    ("图片预览生产", 1, "只做关键帧：读项目与集、查分镜与资产，逐镜生成关键帧并等验收。",
     IMAGE_PREVIEW),
    ("视频生产", 1, "跳过关乎本帧：查完分镜直接逐镜生成视频、查产物、等镜头审核。",
     VIDEO_ONLY),
]




def ensure_builtin_templates() -> int:
    """把内置模板补进库里（只补缺的）。返回本次新插入的数量。"""
    added = 0
    for name, version, description, definition in BUILTIN_TEMPLATES:
        errors = validate_definition(definition)
        if errors:
            # 内置模板写错是开发事故，不是运行时该默默吞掉的事
            raise PipelineInvalid(f"内置模板「{name}」v{version} 定义不成立：{'；'.join(errors)}")
        before = pipeline_repo.list_templates(enabled_only=False)
        pipeline_repo.ensure_template(name=name, version=version,
                                      description=description, definition=definition)
        after = pipeline_repo.list_templates(enabled_only=False)
        added += len(after) - len(before)
    return added


# ---------- 模板校验 ----------
def validate_definition(definition: dict) -> list[str]:
    """模板定义合不合法。返回错误清单（空＝合法），不抛异常——
    调用方要把所有问题一次说清，而不是修一条报一条。"""
    errors: list[str] = []
    if not isinstance(definition, dict):
        return ["模板定义必须是一个对象"]
    nodes = definition.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return ["模板至少要有一个节点"]

    keys: set[str] = set()
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append(f"第 {i + 1} 个节点不是对象")
            continue
        key = str(node.get("key") or "").strip()
        ntype = str(node.get("type") or "").strip()
        if not key:
            errors.append(f"第 {i + 1} 个节点没有 key")
        elif key in keys:
            errors.append(f"节点 key 重复：{key}")
        elif not key.replace("_", "").replace("-", "").isalnum():
            errors.append(f"节点 key 只允许字母数字与下划线：{key}")
        keys.add(key)
        if ntype not in NODE_TYPE_LABELS:
            errors.append(f"节点「{key or i}」的类型不认识：{ntype or '（空）'}")
        deps = node.get("depends_on") or []
        if not isinstance(deps, list):
            errors.append(f"节点「{key}」的 depends_on 必须是数组")
        fan = node.get("fan_out")
        if fan is not None:
            if fan != "shot":
                errors.append(f"节点「{key}」的 fan_out 只支持 shot")
            elif ntype not in FANOUT_TYPES:
                errors.append(f"节点「{key}」的类型 {ntype} 不能按镜头展开")

    for node in nodes:
        if not isinstance(node, dict):
            continue
        key = str(node.get("key") or "")
        for dep in (node.get("depends_on") or []):
            if dep == key:
                errors.append(f"节点「{key}」依赖自己")
            elif dep not in keys:
                errors.append(f"节点「{key}」依赖一个不存在的节点：{dep}")

    cycle = _find_cycle(nodes)
    if cycle:
        errors.append(f"依赖成环：{' → '.join(cycle)}")
    return errors




def _find_cycle(nodes: list[dict]) -> list[str]:
    """Kahn 拓扑排序；排不完就是有环，并把环上的节点名报出来。"""
    graph = {str(n.get("key")): set(n.get("depends_on") or []) for n in nodes
             if isinstance(n, dict)}
    indegree = {k: len(v) for k, v in graph.items()}
    queue = [k for k, d in indegree.items() if d == 0]
    seen = 0
    while queue:
        key = queue.pop()
        seen += 1
        for other, deps in graph.items():
            if key in deps:
                indegree[other] -= 1
                if indegree[other] == 0:
                    queue.append(other)
    if seen == len(graph):
        return []
    return sorted(k for k, d in indegree.items() if d > 0)




def _definition_of(template) -> dict:
    try:
        definition = json.loads(template["definition_json"] or "{}")
    except ValueError:
        raise PipelineInvalid("模板定义不是合法 JSON")
    return definition if isinstance(definition, dict) else {}
