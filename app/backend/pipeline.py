"""产线编排（阶段 B／裁定 D3）。

它只做四件事：**展开流程、按依赖调度、把节点状态与真相对齐、给人看清走到哪了**。
模型不由它执行——生成的活一律通过 `services.submit_job` 交给现有的 Job 队列与
那一个 worker（不变量 2）。审核结论也不由它写——它只读审核中心的签字（不变量 3）。

三条贯穿全文件的规矩：

1. **节点行不抄 Job 的事实**。状态、成本、日志、产物一律现读 `job` 表；
   节点只存"关联哪条 Job、流程走到哪一步、失败时给人看的那句话"。
2. **运行状态只存外生意愿**（paused／cancelled），`running`／`waiting_review`／
   `completed`／`failed` 全部从节点现算。两套状态各自维护，是 v0.86 那两个真 bug
   的成因，这里不重犯。
3. **推进只有一个函数**（`advance_run`）。worker 循环调它，接口 `/advance` 也调它，
   两处共用；重复触发靠"节点原子转态 ＋ 确定性幂等键"挡住，不靠"只有一个进程在跑"。

调度节拍住在 worker 进程（裁定 D3 方案 A）：它已经是常驻循环、已经有
`recover_stale()` 的恢复节拍，也是唯一会写 Job 状态的进程。API 进程不起后台线程——
uvicorn 的进程数不在我们手里，多进程各起一个调度器只能靠锁才正确。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from types import SimpleNamespace

import diskplan
import error_catalog
import jobfiles
import services
from config import COMPOSE_MAX_SHOTS, DISK_MIN_FREE_GB, FFMPEG_BIN, FILES_DIR
from db import get_conn, transaction
from models import JOB_FINISHED, JOB_STATUSES
from paths import describe as describe_path
from paths import resolve_within
from repositories.audio import clips
from repositories.content import assets, episodes, shots
from repositories.documents import documents as docs
from repositories.jobs import jobs as job_repo
from repositories.pipeline import (NODE_ATTENTION, NODE_CANCELLED, NODE_DONE,
                                   NODE_FAILED, NODE_FINAL, NODE_PENDING,
                                   NODE_REVIEW, NODE_RUNNING, NODE_SKIPPED,
                                   pipeline_repo)
from repositories.projects import projects as project_repo
from repositories.reviews import reviews

# 一轮 tick 最多派发多少个节点。正常一集也就几十个，这一位是防呆：
# 模板写错成"每个节点都依赖自己之外的所有节点"时，别把队列一次灌爆。
MAX_DISPATCH_PER_TICK = 60

# 一次推进最多走多少"层"。层数上限只是防呆：模板最长也就十几层，
# 真跑到这个数说明依赖图有问题（比如每层都在放新节点），停下来比空转好。
MAX_LAYERS = 40

NODE_TYPE_LABELS = {
    "project_input": "读取项目",
    "episode_input": "读取集",
    "document_input": "读取剧本",
    "shot_validate": "分镜检查",
    "asset_validate": "资产检查",
    "human_gate": "人工确认",
    "image_generate": "关键帧生成",
    "image_review": "关键帧验收",
    "video_generate": "镜头视频生成",
    "artifact_validate": "产物检查",
    "shot_review": "镜头审核",
    "audio_prepare": "音频准备检查",
    "export_prepare": "成片导出准备",
    "export_compose": "成片合成",
    "export_validate": "成片验收",
    "export_review": "成片审核",
}

# 哪些节点要提交 Job（其余是内部检查或人工审核）
JOB_NODES = {"image_generate": "image", "video_generate": "video",
             # 成片合成也走 Job，但它由 composition 服务建单，不经过
             # `submit_job` 的模型白名单；`_execute_node` 里有专门分支。
             "export_compose": "video"}
# 哪些节点要等人签字
REVIEW_NODES = {"human_gate", "image_review", "shot_review", "export_review"}
# 哪些节点按镜头展开（一镜一个节点，互不影响：镜头 2 失败不该重跑镜头 1 和 3）
FANOUT_TYPES = {"image_generate", "image_review", "video_generate",
                "artifact_validate", "shot_review"}
# 内部检查节点：调度器当场做完，不排队
INTERNAL_NODES = {"project_input", "episode_input", "document_input",
                  "shot_validate", "asset_validate", "artifact_validate",
                  "audio_prepare", "export_prepare", "export_validate"}

NODE_STATUS_LABELS = {
    "blocked": "等待前置", "pending": "待执行", "ready": "可执行",
    "running": "执行中", "waiting_review": "等待审核", "attention": "待人工核对",
    "completed": "已完成", "failed": "失败", "skipped": "已跳过",
    "cancelled": "已取消",
}

RUN_STATUS_LABELS = {
    "running": "生产中", "waiting_review": "待审核", "attention": "待人工核对",
    "paused": "已暂停", "cancelled": "已取消", "completed": "已完成",
    "completed_with_errors": "完成（有跳过或重试）", "failed": "失败",
}


class PipelineInvalid(Exception):
    """模板或请求本身不成立（路由回 422）。"""


class PipelineMissing(Exception):
    """运行或节点不存在、或者不属于可见项目（路由回 404）。"""


class PipelineConflict(Exception):
    """当前状态不允许这个动作（路由回 409）。"""


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


# ---------- 创建运行 ----------
def _pick_document(project_id: int, episode_id: int):
    """这一集要读的那份正文：优先本集剧本，其次本集小说，最后项目级共用稿。"""
    rows = docs.list_by_project(project_id)
    for kind in ("script", "novel"):
        for row in rows:
            if row["kind"] == kind and row["episode_id"] == episode_id:
                return row
    for kind in ("script", "novel"):
        for row in rows:
            if row["kind"] == kind and row["episode_id"] is None:
                return row
    return None


def _ref_image_state(project_id: int, asset_ids: list[int]) -> dict:
    """资产可用性 ＋ 定妆图是否真的还在盘上。

    参考图的存在性以前只在上传／删除时校验过；产线要在开跑前说清
    "这两个资产没有可用参考图"，所以这里补一次读时检查。
    只认相对路径：库里躺着绝对路径或 `..` 的一律当作不可用，不跟着它走出 FILES_DIR。
    """
    usable = assets.usable_ids(project_id, asset_ids)
    missing = [aid for aid in asset_ids if aid not in usable]
    without_ref: list[int] = []
    for aid in sorted(usable):
        row = assets.get(aid)
        if row is None:
            missing.append(aid)
            continue
        try:
            refs = json.loads(row["ref_images"] or "[]")
        except ValueError:
            refs = []
        alive = any(resolve_within(FILES_DIR, str(rel)) is not None for rel in refs)
        if not alive:
            without_ref.append(aid)
    return {"requested": asset_ids, "usable": sorted(usable),
            "missing": missing, "without_ref_image": without_ref}


def build_input_snapshot(project_id: int, episode_id: int) -> dict:
    """运行开始前把输入冻结下来：剧本版本、镜头四要素、资产与参考图状态。

    运行开始后不再改这一份——"当时跑的是哪一版剧本、哪几条镜头"必须回答得出来。
    """
    project = project_repo.get(project_id)
    episode = episodes.get(episode_id)
    shot_rows = shots.list_by_episode(episode_id)
    doc = _pick_document(project_id, episode_id)
    doc_part = None
    if doc is not None:
        try:
            data = services.read_content(doc)
            doc_part = {"id": doc["id"], "title": doc["title"], "kind": doc["kind"],
                        "base_version": data["version"],
                        "git_commit": doc["git_commit"] or ""}
        except Exception as exc:            # 正文读不出来不该挡住建运行，节点会再说一次
            doc_part = {"id": doc["id"], "title": doc["title"], "kind": doc["kind"],
                        "base_version": "", "git_commit": "",
                        "read_error": describe_path(str(exc))[:200]}
    asset_ids: list[int] = []
    for row in shot_rows:
        try:
            asset_ids += [int(a) for a in json.loads(row["asset_ids"] or "[]")]
        except (ValueError, TypeError):
            continue
    return {
        "project": {"id": project_id, "name": project["name"] if project else "",
                    "status": project["status"] if project else ""},
        "episode": {"id": episode_id,
                    "seq": episode["seq"] if episode else None,
                    "title": episode["title"] if episode else "",
                    "status": episode["status"] if episode else ""},
        "document": doc_part,
        "shots": [{"id": r["id"], "seq": r["seq"], "summary": r["summary"],
                   "prompt": r["prompt"], "duration": r["duration"],
                   "asset_ids": json.loads(r["asset_ids"] or "[]")} for r in shot_rows],
        "assets": _ref_image_state(project_id, sorted(set(asset_ids))),
    }


def preflight(project_id: int, episode_id: int,
              template_id: int | None = None, rows: dict | None = None) -> dict:
    """生产前检查：把"跑到一半才发现"的问题提前到按下开始之前。

    只做**确定性**判断——不跑模型、不发请求、不写任何状态，所以它可以被反复调用。
    返回三档：

    * `blocking`：必须修，否则不建运行（缺镜头、引用了不存在的资产、超合成上限、
      同一集已有未结束的运行）；
    * `warnings`：可以建，但要在运行里明确告诉人（提示词为空、资产缺参考图、
      默认轨解析不出来、worker 离线、找不到 ffmpeg、磁盘偏紧）；
    * `info`：这次检查依据的数字与时间，界面用它证明"检查过"。

    为什么 worker 与 ffmpeg 只算 warning：它们不改变"这次生产对不对"，只决定
    "跑到哪一步会停"。把它们做成阻断会让"先在配置齐全的机器上排队、稍后启动 worker"
    这种正常操作变得做不到。

    `rows` 是给"同一次请求里已经把这些行读过一遍"的调用方留的口子（就是 `create_run`）：
    它自己要做幂等与 409 那两道判断，已经把 template／episode／镜头／未结束运行
    各读了一次，再让这里各读一次就是每按一次"开始生产"多打四趟库。
    判断规则只有一份——传进来的就是同一批行，不另开一套口径。
    """
    rows = rows or {}
    blocking: list[str] = []
    warnings: list[str] = []
    info: dict = {"checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    episode = rows["episode"] if "episode" in rows else episodes.get(episode_id)
    if episode is None or episode["project_id"] != project_id:
        return {"blocking": ["这一集不属于该项目"], "warnings": [], "info": info}

    shot_rows = rows["shot_rows"] if "shot_rows" in rows else shots.list_by_episode(episode_id)
    info["shots"] = len(shot_rows)
    if not shot_rows:
        blocking.append("这一集还没有镜头；先去创作中心把分镜建起来")
    if len(shot_rows) > COMPOSE_MAX_SHOTS:
        blocking.append(
            f"这一集有 {len(shot_rows)} 个镜头，超过单次合成上限 {COMPOSE_MAX_SHOTS}；先拆集")

    # 先把这一集的引用关系摊平：资产 id → 引用它的镜号。
    # 原来这里是"逐镜 × 逐资产"各回一次库（13 镜一集、每镜两三个引用＝四十次上下），
    # 而这条接口每次打开发起弹窗都要跑一遍；现在读一次、判一次。
    referenced: dict[int, list[str]] = {}
    for shot in shot_rows:
        if not (shot["prompt"] or "").strip():
            warnings.append(f"第 {shot['seq']} 镜没有提示词，生成时会拿内容那一栏代替")
        for asset_id in services.shot_asset_ids(shot):
            referenced.setdefault(asset_id, []).append(str(shot["seq"]))
    loaded = assets.get_many(referenced)
    for asset_id in sorted(referenced):
        seqs = "／".join(referenced[asset_id])
        row = loaded.get(asset_id)
        if row is None:
            # 说清"哪些镜还挂着这条引用"，比只点第一镜更有用：删了资产的人
            # 下一步就是要去那几镜里改引用。
            blocking.append(f"资产 #{asset_id} 不存在，第 {seqs} 镜还在引用它")
            continue
        # `ref_images` 存的是 JSON 文本：空清单是 "[]"，非空字符串——
        # 直接 `if not row["ref_images"]` 永远为真，「没有参考图」这条警告
        # 就从来没说过话（v0.96 真浏览器验收时抓到）。先解析再判空。
        try:
            ref_images = json.loads(row["ref_images"] or "[]")
        except ValueError:
            ref_images = []
        if not ref_images:
            warnings.append(f"资产「{row['name']}」没有参考图，定妆一致性没有依据")
        if not (row["consistency_prompt"] or "").strip():
            warnings.append(f"资产「{row['name']}」没有一致性提示词")
    info["assets"] = len(referenced)

    if template_id:
        template = (rows["template"] if "template" in rows
                    else pipeline_repo.get_template(template_id))
        if template is None:
            blocking.append("流程模板不存在")
        else:
            definition = _definition_of(template)
            needs = {JOB_NODES[n["type"]] for n in definition["nodes"]
                     if isinstance(n, dict) and n.get("type") in JOB_NODES
                     and n["type"] != "export_compose"}
            for capability in sorted(needs):
                try:
                    services.resolve_provider(capability)
                except services.NoRoute as exc:
                    warnings.append(f"{capability} 没有可用的默认轨：{exc}")
            has_compose = any(isinstance(n, dict) and n.get("type") == "export_compose"
                              for n in definition["nodes"])
            if has_compose and shutil.which(FFMPEG_BIN) is None:
                warnings.append(f"模板含成片节点，但这台机器找不到 {FFMPEG_BIN}")

    busy = (rows["busy"] if "busy" in rows
            else pipeline_repo.unfinished_for_episode(project_id, episode_id))
    if busy is not None:
        blocking.append(
            f"这一集已有一条未结束的运行（#{busy['id']}）；先取消或等它跑完，别排两套镜头作业")

    try:
        worker = services.worker_status()
        info["worker_online"] = bool(worker.get("online"))
        if not worker.get("online"):
            warnings.append("生成 worker 不在线；运行建好了也不会自己往前走")
    except Exception:                      # 读不到状态不该拖垮预检
        warnings.append("读不到 worker 状态")

    # 磁盘余量问的是同一个出处（jobfiles.volume_usage）：作业目录可能还没建，
    # 那时直接 disk_usage 会抛 OSError——而"盘要满了"这句话恰恰在最该说的时候
    # 最容易沉默。这里不再 catch 掉：读不到就当读不到，明说。
    # 磁盘这一条与两个预检入口共用 `diskplan` 那一处算式：产线发起前说的
    # "预计还要多少"必须和工作台说的一样，否则一处放行一处提醒就是界面在骗人。
    disk = diskplan.disk_readout(project_id, shots_missing=len(shot_rows))
    info["free_bytes"] = disk["free_bytes"]
    info["min_free_gb"] = disk["min_free_gb"]
    info["estimated_need_bytes"] = disk["estimated_need_bytes"]
    low = (disk["free_bytes"] is None
           or disk["free_bytes"] < max(disk["estimated_need_bytes"], disk["floor_bytes"]))
    if low or not disk["has_sample"]:
        warnings.append(disk["sentence"])

    return {"blocking": blocking, "warnings": list(dict.fromkeys(warnings)), "info": info}


def create_run(project_id: int, episode_id: int, template_id: int,
               created_by: int | None, idempotency_key: str | None = None) -> dict:
    """创建并立刻开始一次生产运行。

    幂等：同一个 `idempotency_key` 再来一次，返回已有那条运行，不建第二条。
    另外还有一道业务闸：同一集上只允许一条未结束的运行——连点两次"开始生产"
    不该排出两套镜头作业。
    """
    template = pipeline_repo.get_template(template_id)
    if template is None:
        raise PipelineMissing("流程模板不存在")
    if not template["enabled"]:
        raise PipelineInvalid(f"模板「{template['name']}」已停用")
    definition = _definition_of(template)
    errors = validate_definition(definition)
    if errors:
        raise PipelineInvalid(f"模板定义不成立：{'；'.join(errors)}")

    project = project_repo.get(project_id)
    if project is None:
        raise PipelineMissing("项目不存在")
    episode = episodes.get(episode_id)
    if episode is None or episode["project_id"] != project_id:
        raise PipelineInvalid("这一集不属于该项目")
    shot_rows = shots.list_by_episode(episode_id)
    if not shot_rows:
        raise PipelineInvalid("这一集还没有镜头；先去创作中心把分镜建起来")

    # 云端节点这一版不代发：云端作业必须逐条确认预计费用（`submit_job` 强制），
    # 产线替人按下去＝在人没看过价格的情况下花钱。要说清是哪些节点，别只回一句"不行"。
    cloud_nodes = [n.get("key") for n in definition["nodes"]
                   if isinstance(n, dict) and n.get("execution_mode") == "cloud"]
    if cloud_nodes:
        raise PipelineInvalid(
            f"这一版产线不代发云端作业（要逐条确认费用）：{', '.join(cloud_nodes)}。"
            f"把模板里这些节点改成本地档，或到生成中心逐条提交。")

    if idempotency_key:
        existing = pipeline_repo.by_key(idempotency_key)
        if existing is not None:
            if existing["project_id"] != project_id:
                raise PipelineConflict("这个幂等键已被别的项目占用")
            return run_view(existing["id"])

    busy = pipeline_repo.unfinished_for_episode(project_id, episode_id)
    if busy is not None:
        raise PipelineConflict(
            f"这一集已经有一条还没结束的生产运行（#{busy['id']}）；"
            f"先把它取消或等它跑完，别排两套镜头作业")

    # 预检的阻断项在这里一次性说完，别让人跑到第 3 镜才发现资产被删了。
    # 幂等重放已经在上面返回了，所以这一步只对"真的要新建"的运行生效。
    # 上面为了幂等与 409 那两道判断已经读过的那几行，这里直接交给预检复用——
    # 同一次"开始生产"不该把模板、集、镜头、未结束运行各问两遍（规则仍只有一份）。
    check = preflight(project_id, episode_id, template_id, rows={
        "episode": episode, "shot_rows": shot_rows, "busy": busy, "template": template})
    if check["blocking"]:
        raise PipelineInvalid("生产前检查没过：" + "；".join(check["blocking"]))

    snapshot = build_input_snapshot(project_id, episode_id)
    nodes = expand_nodes(definition, snapshot)
    now = _now()
    with transaction():
        run = pipeline_repo.create_run(
            project_id=project_id, episode_id=episode_id,
            template_id=template["id"], template_version=template["version"],
            input_snapshot=snapshot, idempotency_key=idempotency_key,
            created_by=created_by, started_at=now)
        pipeline_repo.create_nodes(run["id"], nodes)
        pipeline_repo.add_event(run["id"], None, "run_created", {
            "template": template["name"], "version": template["version"],
            "nodes": len(nodes), "shots": len(snapshot["shots"])})
        if check["warnings"]:
            pipeline_repo.add_event(run["id"], None, "preflight_warning",
                                    {"warnings": check["warnings"][:8]})
        pipeline_repo.add_event(run["id"], None, "run_started", {})
    # 建完立刻推进一轮：第一个节点（读项目）不该等人来点
    advance_run(run["id"])
    return run_view(run["id"])


def expand_nodes(definition: dict, snapshot: dict) -> list[dict]:
    """把模板展开成这次运行的节点行。

    按镜头展开的节点，一镜一个，key 是 `<模板key>#s<镜头id>`；
    它的依赖也跟着换成同一镜的那一个。汇聚节点（不按镜头展开、却依赖展开节点）
    依赖的是**全部**镜头实例——所以"镜头 2 失败"会挡住音频检查，
    但不会让镜头 1 和镜头 3 重跑。
    """
    shot_ids = [s["id"] for s in snapshot["shots"]]
    shots_by_id = {s["id"]: s for s in snapshot["shots"]}
    specs = [n for n in definition["nodes"] if isinstance(n, dict)]
    fanout_keys = {str(n["key"]) for n in specs if n.get("fan_out") == "shot"}
    out: list[dict] = []

    def deps_for(spec: dict, shot_id: int | None) -> list[str]:
        resolved = []
        for dep in (spec.get("depends_on") or []):
            if dep in fanout_keys:
                if shot_id is None:
                    resolved += [f"{dep}#s{sid}" for sid in shot_ids]
                else:
                    resolved.append(f"{dep}#s{shot_id}")
            else:
                resolved.append(dep)
        return resolved

    for spec in specs:
        key = str(spec["key"])
        ntype = str(spec["type"])
        label = str(spec.get("label") or NODE_TYPE_LABELS.get(ntype, ntype))
        params = spec.get("params") or {}
        if spec.get("fan_out") == "shot":
            for sid in shot_ids:
                shot_row = shots_by_id[sid]
                out.append({
                    "node_key": f"{key}#s{sid}", "node_type": ntype,
                    "label": f"{label} · 镜 {int(shot_row['seq']):02d}",
                    "depends_on": deps_for(spec, sid),
                    "input": {"shot_id": sid, "shot_seq": shot_row["seq"],
                              "prompt": shot_row["prompt"],
                              "summary": shot_row["summary"],
                              "duration": shot_row["duration"],
                              "asset_ids": shot_row["asset_ids"],
                              "params": params},
                })
        else:
            out.append({"node_key": key, "node_type": ntype, "label": label,
                        "depends_on": deps_for(spec, None), "input": {"params": params}})
    return out


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 推进（唯一的调度入口） ----------
def tick(limit: int = 20) -> dict:
    """worker 每轮调一次：把所有还没结束的运行各推进一轮。

    放在 worker 而不是 API 进程：worker 已经是常驻循环、已经在每轮做
    `recover_stale()`，也是唯一会写 Job 状态的进程。一个进程内只有一处推进，
    天然不会出现两个调度器互相抢。
    """
    advanced = 0
    for run in pipeline_repo.tickable_runs(limit):
        advance_run(run["id"])
        advanced += 1
    return {"runs": advanced}


def advance_run(run_id: int) -> dict:
    """把一条运行推进一步。重复调用是安全的：每一步都按当前真状态现算。

    整段包在一个事务里（`BEGIN IMMEDIATE`）：两个进程同时推进同一条运行时，
    后到的那个会等前一个提交，读到的一定是最新状态；而"同一个节点被派两次"
    由 `try_begin()` 的原子转态挡住，不依赖这把锁。

    派发是**分层**的：`_dispatch_ready` 拿的依赖快照是这一层开始时的状态，
    所以一层只放出"前置在本层之前就已经完成"的节点。内部检查节点是当场做完的，
    于是要循环到"这一层没有新东西可放"为止——否则读项目→读集→查分镜这种
    纯检查链，要点三次"推进"才走完一格，界面上看就是卡住。
    """
    with transaction():
        run = pipeline_repo.get_run(run_id)
        if run is None:
            raise PipelineMissing("生产运行不存在")
        nodes = pipeline_repo.nodes_for_run(run_id)
        if run["cancelled"] or run["finished_at"] or run["paused"]:
            # 不再派新节点，但**仍然对齐真相**：已经在跑的作业会继续跑完／被取消，
            # 不同步的话取消之后那一格会永远显示"执行中"，人看不出它到底怎么样了。
            _sync_job_nodes(run, nodes)
            _sync_review_nodes(run, nodes)
            return run_view(run_id)

        _sync_job_nodes(run, nodes)
        _sync_review_nodes(run, nodes)
        for _ in range(MAX_LAYERS):
            nodes = pipeline_repo.nodes_for_run(run_id)
            if not _dispatch_ready(run, nodes):
                break
        nodes = pipeline_repo.nodes_for_run(run_id)
        _finalize(run, nodes)
    return run_view(run_id)


def _node_map(nodes) -> dict[str, dict]:
    return {row["node_key"]: dict(row) for row in nodes}


def _sync_job_nodes(run, nodes) -> None:
    """按 Job 的**真实状态**修节点。这就是重启恢复：不猜、不重投，只对齐。"""
    for row in nodes:
        node = dict(row)
        if node["status"] not in (NODE_RUNNING, NODE_ATTENTION):
            continue
        if node["job_id"] is None:
            # 作业行被删掉时，外键把节点上的 job_id 置空（SET NULL）。
            # 不处理的话这一格会永远停在"执行中"——那是个卡死态，
            # 既不会被调度、也不给人一个可以重试的入口。
            pipeline_repo.finish_node(
                node["id"], NODE_FAILED, expect=node["status"],
                error_code="job_missing",
                error_message="关联的作业已经不在了（可能随项目或集一起被清理）；"
                              "这一格需要重试。")
            pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                    {"reason": "job_missing"})
            continue
        job = job_repo.get(node["job_id"])
        if job is None:
            pipeline_repo.finish_node(
                node["id"], NODE_FAILED, expect=node["status"],
                error_code="job_missing",
                error_message="关联的作业已经不在了（可能随项目一起被删）；"
                              "这一格需要重试。")
            pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                    {"reason": "job_missing"})
            continue
        status = job["status"]
        if status in ("queued", "starting", "running", "cancel_requested"):
            continue                      # 还在跑，什么都不改
        if status == "succeeded":
            artifacts = jobfiles.artifacts(job["result_paths_json"])
            pipeline_repo.finish_node(
                node["id"], NODE_DONE, expect=node["status"],
                output={"job_id": job["id"], "artifacts": len(artifacts),
                        "provider": job["provider_code"], "model": job["model_code"]})
            pipeline_repo.add_event(run["id"], node["id"], "node_completed",
                                    {"job_id": job["id"]})
        elif status == "needs_attention":
            # 云端结果未知／账单待核对。**不自动重试**：那可能造成第二次付费调用。
            if node["status"] != NODE_ATTENTION:
                pipeline_repo.finish_node(
                    node["id"], NODE_ATTENTION, expect=node["status"],
                    error_code="job_needs_attention",
                    error_message=(job["attention_reason"] or "作业结果待人工核对")
                    + "；去生成中心核对账单之后再决定要不要重试。")
                pipeline_repo.add_event(run["id"], node["id"], "node_attention",
                                        {"job_id": job["id"]})
        else:                              # failed / cancelled
            if run["cancelled"] and status == "cancelled":
                # 是"这条运行被取消"顺手取消的作业，不是它自己跑坏了。
                # 记成 cancelled：界面上不该把人的取消决定显示成一次失败。
                pipeline_repo.finish_node(
                    node["id"], NODE_CANCELLED, expect=node["status"],
                    error_code="run_cancelled",
                    error_message="运行取消时这一格跟着一起取消了。")
                pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                        {"job_id": job["id"],
                                         "reason": "run_cancelled"})
                continue
            pipeline_repo.finish_node(
                node["id"], NODE_FAILED, expect=node["status"],
                error_code=job["error_code"] or status,
                error_message=(job["error_message"] or "")[:500]
                or f"作业 #{job['id']} 的状态是 {status}")
            pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                    {"job_id": job["id"], "job_status": status})


def _sync_review_nodes(run, nodes) -> None:
    """按审核中心的签字修节点。产线只读结论，绝不写结论（不变量 3）。"""
    for row in nodes:
        node = dict(row)
        if node["status"] != NODE_REVIEW:
            continue
        try:
            node_input = json.loads(node["input_json"] or "{}")
        except ValueError:
            node_input = {}
        sign = _find_approval(node, node_input)
        if sign is None:
            continue
        decision = sign["decision"]
        if decision == "approved":
            pipeline_repo.finish_node(
                node["id"], NODE_DONE, expect=NODE_REVIEW, review_id=sign["id"],
                output={"review_item_id": sign["id"], "anchor": sign["anchor"],
                        "note": sign["note"], "signed_by": sign["signed_by_name"] or ""})
            pipeline_repo.add_event(run["id"], node["id"], "node_completed",
                                    {"review_item_id": sign["id"]})
        elif decision == "rejected":
            pipeline_repo.finish_node(
                node["id"], NODE_FAILED, expect=NODE_REVIEW, review_id=sign["id"],
                error_code="review_rejected",
                error_message=(f"{sign['signed_by_name'] or '审核人'}驳回"
                               f"{('：' + sign['note']) if sign['note'] else ''}"
                               "；重做上游生成节点之后这一格才会重新等签字。"))
            pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                    {"reason": "review_rejected",
                                     "review_item_id": sign["id"]})
        # returned（退回）停在 waiting_review：退回是"再改改"，不是终局


def _find_approval(node: dict, node_input: dict):
    """这一格等的是谁的签字。文档确认看文档那一档，产物审核看作业那一档。"""
    if node["node_type"] == "human_gate":
        doc_id = node_input.get("doc_id")
        if not doc_id:
            return None
        doc = docs.get(doc_id)
        if doc is None:
            return None
        try:
            current = services.read_content(doc)["version"]
        except Exception:
            return None
        signs = reviews.signs(reviews.DOCUMENT, doc_id, current)
    else:
        job_id = node_input.get("job_id") or node["job_id"]
        if not job_id:
            return None
        job = job_repo.get(job_id)
        if job is None:
            return None
        version = services.artifact_version(job)
        signs = reviews.signs(reviews.JOB, job_id, version)
    for sign in signs:
        if sign["decision"] in ("approved", "rejected"):
            return dict(sign)
    return None


def _dispatch_ready(run, nodes) -> int:
    """把"依赖都完成了"的 pending 节点派出去。返回这一层派了几个。

    依赖状态取自传进来的那份快照，所以一层之内刚完成的节点不会立刻放出下游；
    调用方按层循环（见 `advance_run`），每层重读一次。
    """
    by_key = _node_map(nodes)
    dispatched = 0
    for row in nodes:
        if dispatched >= MAX_DISPATCH_PER_TICK:
            break
        node = dict(row)
        if node["status"] != NODE_PENDING:
            continue
        deps = _deps_of(node)
        if not all(by_key.get(d, {}).get("status") == NODE_DONE for d in deps):
            continue                       # 前置没完成：blocked（现算，不入库）
        dispatched += 1
        _execute_node(run, node, by_key)
    return dispatched


def _execute_node(run, node: dict, by_key: dict) -> None:
    ntype = node["node_type"]
    if ntype == "export_compose":
        _submit_composition_node(run, node)
    elif ntype in JOB_NODES:
        _submit_job_node(run, node, by_key)
    elif ntype in REVIEW_NODES:
        _arm_review_node(run, node, by_key)
    else:
        _run_internal_node(run, node, by_key)


def _idempotency_key(run, node: dict) -> str:
    """确定性幂等键：同一条运行的同一个节点、同一次重试，键永远一样。

    这样即使有人绕过原子转态重放提交，`job.idempotency_key` 的 UNIQUE 也只会有
    一条作业——附件 §6.3 要的"不重复提交"由三层各自兜住，不靠其中任何一层单独成立。
    """
    return f"pl-{run['id']}-{node['node_key']}-a{int(node['retry_count'] or 0) + 1}"


def _missing_image_params(capability: str, params: dict) -> list[str]:
    """默认轨的白名单里，哪些"条件图"参数还没给。

    只有真解析出路线才判：解析不出来时不猜——那种情况交给 `submit_job`
    用它自己那句话拒掉，产线不复述一套路线规则（那会变成第二处口径）。
    """
    try:
        got = services.resolve_provider(capability)
    except services.NoRoute:
        return []
    model = next((m for m in got["adapter"].models if m.code == got["model"]), None)
    if model is None:
        return []
    # 空字符串也算没给：模板里可能写着 `"image": ""` 占位，
    # 那不是路径，交给白名单去拒只会得到一句看不懂的话。
    return [p.name for p in model.params
            if p.type == "image" and not str(params.get(p.name) or "").strip()]


def _upstream_image(node: dict, by_key: dict) -> str | None:
    """顺着依赖往上找一张关键帧，返回经过边界校验的绝对路径。

    直接依赖通常是审核节点（它自己不挂 Job），所以要往上多走几层；
    只认 `image_generate` 的产物——把音频或视频文件当参考图喂进去是另一种事故。
    """
    seen: set[str] = set()
    stack = _deps_of(node)
    while stack:
        key = stack.pop()
        if key in seen:
            continue
        seen.add(key)
        up = by_key.get(key)
        if not up:
            continue
        if up.get("node_type") == "image_generate" and up.get("job_id"):
            job = job_repo.get(up["job_id"])
            if job is not None and job["status"] == "succeeded":
                target = jobfiles.artifact_at(job["result_paths_json"], 0)
                if target is not None:
                    return str(target)
        stack += _deps_of(up)
    return None


def _deps_of(node) -> list[str]:
    try:
        return json.loads(node["depends_on_json"] or "[]")
    except ValueError:
        return []


def _submit_job_node(run, node: dict, by_key: dict) -> None:
    """提交一条生成作业，并把 Job ID 记到节点上。**两步在同一个事务里**（调用方开着）。

    顺序是：原子转态 → 建 Job → 回写 job_id → 记事件。
    事务保证不存在"Job 建好了、节点不知道"这个中间态；`try_begin()` 的 rowcount
    保证连点两次重试只有一个能赢。
    """
    if not pipeline_repo.try_begin(node["id"]):
        return                             # 别人已经抢走了这一格
    capability = JOB_NODES[node["node_type"]]
    try:
        node_input = json.loads(node["input_json"] or "{}")
    except ValueError:
        node_input = {}
    params = dict(node_input.get("params") or {})
    # I2V 这类档位要一张条件图，而模板里不该写死路径（路径是运行期才知道的）。
    # 所以：白名单确实缺图时，从上游关键帧产物补一张；补不出来就明确失败，
    # 不拿"没有参考图"去跑一条注定失败的作业。
    missing = _missing_image_params(capability, params)
    if missing:
        found = _upstream_image(node, by_key)
        if found is None:
            pipeline_repo.finish_node(
                node["id"], NODE_FAILED, error_code="condition_image_missing",
                error_message=f"当前默认轨要一张条件图（{', '.join(missing)}），"
                              f"但上游没有可用的关键帧产物；先重试关键帧那一格，"
                              f"或把默认轨换成不需要参考图的档。")
            pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                    {"reason": "condition_image_missing",
                                     "params": missing})
            return
        params[missing[0]] = found
    key = _idempotency_key(run, node)
    existing = job_repo.by_key(key)
    if existing is not None:
        pipeline_repo.attach_job(node["id"], existing["id"])
        pipeline_repo.add_event(run["id"], node["id"], "job_submitted",
                                {"job_id": existing["id"], "reused": True})
        return
    payload = SimpleNamespace(
        project_id=run["project_id"], episode_id=run["episode_id"],
        shot_id=node_input.get("shot_id"), capability=capability,
        # 三件都不传＝走平台配的默认轨。绝不替人切云端：云端要逐条确认费用，
        # 而 `submit_job` 会在没有确认单时直接拒掉（不兜底）。
        execution_mode=None, provider_code=None, model_code=None,
        prompt=node_input.get("prompt") or node.get("label") or "",
        params=params,
        input_refs=[{"pipeline_run_id": run["id"], "node_key": node["node_key"],
                     "node_type": node["node_type"]}],
        estimate=None, user_confirmed_cloud=False, priority=0, idempotency_key=key)
    try:
        job = services.submit_job(payload, run["created_by"])
    except services.JobInvalid as exc:
        pipeline_repo.finish_node(
            node["id"], NODE_FAILED, error_code="submit_rejected",
            error_message=str(exc))
        pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                {"reason": "submit_rejected", "detail": str(exc)[:200]})
        return
    pipeline_repo.attach_job(node["id"], job["id"])
    pipeline_repo.add_event(run["id"], node["id"], "job_submitted", {
        "job_id": job["id"], "capability": capability,
        "provider": job["provider_code"], "model": job["model_code"],
        "execution_mode": job["execution_mode"]})


def _shot_of_key(node_key: str) -> int | None:
    """`<模板key>#s<镜头id>` 里取出镜头 id；不是按镜头展开的节点返回 None。"""
    marker = "#s"
    if marker not in node_key:
        return None
    try:
        return int(node_key.rsplit(marker, 1)[1])
    except ValueError:
        return None


def compose_shot_jobs(run) -> dict[int, int] | None:
    """这条运行里每一镜用哪条作业：取产线自己签过的那条，不取"最新成功"。

    为什么必须有这一层：产线的「镜头审核」签的是**某一条**作业的产物，而成片
    默认按"这一镜最新一条成功视频"取。产线跑到一半有人手抽重生成一镜并签了字，
    最新成功就换人了——产线签过的那条被静默顶掉，成片用的是另一条素材。
    所以产线成片把节点里冻结的 `job_id` 钉进时间线：签的哪条，拼的就哪条。

    优先 `shot_review` 节点 `input_json.job_id`（那是"等谁签字"冻下来的），
    没有再退到 `video_generate` 节点自己的 `job_id`（同一支线里就是它）。
    两者都没有的镜头不会被补成"最新的那条"，留给时间线点名拒绝。
    """
    nodes = pipeline_repo.nodes_for_run(run["id"])
    by_shot: dict[int, int] = {}
    for node in nodes:
        if node["node_type"] != "shot_review":
            continue
        shot_id = _shot_of_key(node["node_key"])
        if shot_id is None:
            continue
        try:
            payload = json.loads(node["input_json"] or "{}")
        except ValueError:
            payload = {}
        job_id = payload.get("job_id") or node["job_id"]
        if job_id:
            by_shot[shot_id] = int(job_id)
    if by_shot:
        return by_shot
    # 没有镜头审核节点（自定义模板只到生成）：仍用产线派出的作业，
    # 不回到"最新成功"——产线派出哪条就是哪条
    for node in nodes:
        if node["node_type"] != "video_generate" or not node["job_id"]:
            continue
        shot_id = _shot_of_key(node["node_key"])
        if shot_id is not None:
            by_shot[shot_id] = int(node["job_id"])
    return by_shot or None


def _submit_composition_node(run, node: dict) -> None:
    """成片合成节点：建时间线＋成片行＋Job，三步一个事务（在 composition 里）。

    它和生成节点的区别只有"参数从哪来"：生成走模型白名单，合成走时间线。
    幂等同样用 `pl-<run>-<key>-a<n>`：同一次运行重放不会排出两个版本。
    """
    if not pipeline_repo.try_begin(node["id"]):
        return
    from composition.service import (CompositionConflict, CompositionInvalid,
                                     CompositionMissing, create as create_composition)

    key = _idempotency_key(run, node)
    existing = job_repo.by_key(key)
    if existing is not None:
        from repositories.compositions import compositions
        row = compositions.by_job(existing["id"])
        pipeline_repo.attach_job(node["id"], existing["id"])
        pipeline_repo.add_event(run["id"], node["id"], "job_submitted", {
            "job_id": existing["id"], "reused": True,
            "composition_id": row["id"] if row is not None else None})
        return
    try:
        view = create_composition(
            run["project_id"], run["episode_id"], run["created_by"],
            idempotency_key=key, pipeline_run_id=run["id"],
            # 产线签的哪条，就拼哪条；不传这个参数就会退回"每镜最新成功"，
            # 那正是"产线签 A、成片用 B"的来路
            shot_jobs=compose_shot_jobs(run))
    except CompositionInvalid as exc:
        detail = "；".join(exc.reasons[:6]) if exc.reasons else exc.message
        pipeline_repo.finish_node(
            node["id"], NODE_FAILED, error_code="composition_precheck",
            error_message=f"{exc.message} {detail}".strip()[:800])
        pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                {"reason": "composition_precheck",
                                 "detail": detail[:200]})
        return
    except CompositionConflict as exc:
        pipeline_repo.finish_node(
            node["id"], NODE_FAILED, error_code="composition_conflict",
            error_message=str(exc))
        pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                {"reason": "composition_conflict"})
        return
    except CompositionMissing as exc:
        pipeline_repo.finish_node(
            node["id"], NODE_FAILED, error_code="composition_missing",
            error_message=str(exc))
        pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                {"reason": "composition_missing"})
        return
    job_id = view.get("job_id")
    pipeline_repo.attach_job(node["id"], job_id)
    pipeline_repo.add_event(run["id"], node["id"], "job_submitted", {
        "job_id": job_id, "composition_id": view["id"], "version": view["version"],
        "capability": "video", "provider": "ffmpeg", "model": "ffmpeg-compose-1",
        "execution_mode": "local"})


def _arm_review_node(run, node: dict, by_key: dict) -> None:
    """审核节点：转成 waiting_review，并把"等谁的签字"冻结进输入。

    产物审核等的是上游那条作业的产物；人工确认等的是剧本那一版的整份签字。
    这里只记引用（job_id／doc_id），结论由审核中心写。
    """
    if not pipeline_repo.try_begin(node["id"]):
        return
    try:
        node_input = json.loads(node["input_json"] or "{}")
    except ValueError:
        node_input = {}
    if node["node_type"] == "human_gate":
        snapshot = _snapshot_of(run)
        doc_part = (snapshot.get("document") or {})
        if not doc_part.get("id"):
            pipeline_repo.finish_node(
                node["id"], NODE_FAILED, error_code="document_missing",
                error_message="这一集没有可确认的剧本或小说正文；先去创作中心建一份。")
            pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                    {"reason": "document_missing"})
            return
        node_input.update({"doc_id": doc_part["id"],
                           "base_version": doc_part.get("base_version", "")})
    else:
        upstream = _upstream_job(node, by_key)
        if upstream is None:
            pipeline_repo.finish_node(
                node["id"], NODE_FAILED, error_code="upstream_job_missing",
                error_message="找不到要审的那条作业（上游节点没有关联 Job）。")
            pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                    {"reason": "upstream_job_missing"})
            return
        node_input["job_id"] = upstream
    # 先把"等谁的签字"冻结进输入，再把状态转成等待审核
    _update_node_input(node["id"], node_input)
    pipeline_repo.finish_node(node["id"], NODE_REVIEW, expect=NODE_RUNNING,
                              output={"awaiting": node["node_type"]})
    pipeline_repo.add_event(run["id"], node["id"], "node_waiting_review",
                            {"target": node_input.get("job_id") or node_input.get("doc_id")})


def _update_node_input(node_id: int, node_input: dict) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE pipeline_node_run SET input_json = ? WHERE id = ?",
                     (json.dumps(node_input, ensure_ascii=False, sort_keys=True),
                      node_id))


def _snapshot_of(run) -> dict:
    try:
        return json.loads(run["input_snapshot_json"] or "{}")
    except ValueError:
        return {}


def _upstream_job(node: dict, by_key: dict) -> int | None:
    """这个节点要认哪条作业：顺着依赖**逐层**往上找最近的一条。

    直接依赖常常是内部检查节点（`artifact_validate` 自己不挂 Job），
    只看一层就会找不到要审的作业。按层找（先看完这一层再上一层）保证拿到的是
    "离这一格最近的那条"；而按镜头展开的支线互不相连，所以不会串到别的镜头去。
    """
    seen: set[str] = set()
    frontier = _deps_of(node)
    while frontier:
        nxt: list[str] = []
        for key in frontier:
            if key in seen:
                continue
            seen.add(key)
            up = by_key.get(key)
            if not up:
                continue
            if up.get("job_id"):
                return int(up["job_id"])
            nxt += _deps_of(up)
        frontier = nxt
    return None


def _run_internal_node(run, node: dict, by_key: dict) -> None:
    """内部检查节点：调度器当场做完，不排队、不花钱。

    读文件与读库都在事务里——这些都是**只读**，回滚不需要撤销任何东西；
    真正不能进事务的是 Git 提交与删文件，产线这一步一个都不做。
    """
    if not pipeline_repo.try_begin(node["id"]):
        return
    ntype = node["node_type"]
    snapshot = _snapshot_of(run)
    try:
        if ntype == "project_input":
            project = project_repo.get(run["project_id"])
            if project is None:
                raise _NodeFail("project_missing", "项目已经不在了")
            status, output = NODE_DONE, {"project_id": project["id"],
                                         "name": project["name"],
                                         "status": project["status"]}
        elif ntype == "episode_input":
            episode = episodes.get(run["episode_id"])
            if episode is None:
                raise _NodeFail("episode_missing", "这一集已经不在了")
            status, output = NODE_DONE, {
                "episode_id": episode["id"], "seq": episode["seq"],
                "title": episode["title"], "status": episode["status"],
                "shots": len(shots.list_by_episode(episode["id"]))}
        elif ntype == "document_input":
            doc = _pick_document(run["project_id"], run["episode_id"])
            if doc is None:
                raise _NodeFail("document_missing",
                                "这一集没有剧本或小说正文；先去创作中心建一份")
            data = services.read_content(doc)
            status, output = NODE_DONE, {
                "doc_id": doc["id"], "title": doc["title"], "kind": doc["kind"],
                "base_version": data["version"], "git_commit": doc["git_commit"] or "",
                "bytes": len(data["content"].encode("utf-8"))}
        elif ntype == "shot_validate":
            rows = shots.list_by_episode(run["episode_id"])
            problems = []
            for row in rows:
                if not (row["prompt"] or "").strip():
                    problems.append(f"镜 {int(row['seq']):02d} 没有提示词")
                if not row["duration"] or float(row["duration"]) <= 0:
                    problems.append(f"镜 {int(row['seq']):02d} 的时长不成立")
            if not rows:
                raise _NodeFail("no_shots", "这一集一条镜头都没有")
            if problems:
                raise _NodeFail("shots_incomplete", "；".join(problems[:8]))
            status, output = NODE_DONE, {
                "shots": len(rows),
                "total_duration": round(sum(float(r["duration"] or 0) for r in rows), 2)}
        elif ntype == "asset_validate":
            state = snapshot.get("assets") or _ref_image_state(
                run["project_id"], episode_asset_ids(run["episode_id"]))
            if state.get("missing"):
                names = ", ".join(str(a) for a in state["missing"][:8])
                raise _NodeFail("assets_missing",
                                f"镜头引用的资产不可用（不属于本项目、也不在全局池）：{names}")
            status, output = NODE_DONE, {
                "assets": len(state.get("usable") or []),
                "without_ref_image": state.get("without_ref_image") or [],
                "note": ("有资产没有可用定妆图；生成会少一层一致性约束"
                         if state.get("without_ref_image") else "")}
        elif ntype == "artifact_validate":
            job_id = _upstream_job(node, by_key)
            if job_id is None:
                raise _NodeFail("upstream_job_missing", "找不到要验的那条作业")
            job = job_repo.get(job_id)
            if job is None:
                raise _NodeFail("job_missing", "关联的作业已经不在了")
            artifacts = jobfiles.artifacts(job["result_paths_json"])
            gone = [a["path"] for a in artifacts if not a["exists"] or a["size"] <= 0]
            if not artifacts:
                raise _NodeFail("no_artifacts", f"作业 #{job_id} 没有登记任何产物")
            if gone:
                raise _NodeFail("artifact_missing",
                                f"产物不在或为空：{', '.join(gone[:4])}")
            status, output = NODE_DONE, {
                "job_id": job_id, "artifacts": len(artifacts),
                "bytes": sum(int(a["size"] or 0) for a in artifacts)}
        elif ntype == "audio_prepare":
            rows = clips.list(run["project_id"], None, run["episode_id"])
            ready = [r for r in rows if r["status"] == "ready"]
            # 这一格只报事实、不判失败：本机没有 TTS／音乐运行时时，
            # "音频还没就绪"是环境状态，不是这一集的流程错误（需求 §8 第 1 条）。
            status, output = NODE_DONE, {
                "clips": len(rows), "ready": len(ready),
                "kinds": sorted({r["kind"] for r in rows}),
                "note": ("本机音频路线还没配好；配音与音乐要到音频中心逐条提交"
                         if len(ready) < len(rows) else "")}
        elif ntype == "export_validate":
            # 合成后验收：worker 已经用 ffprobe 复核过一遍，这里再按**成片记录**
            # 对一次（文件还在、大小与摘要对得上、分辨率与时长与记录一致）。
            # 两道都过才算完成；任何一道不成立，节点明确失败，不记 skipped。
            from composition import service as composition_service
            from composition import validators
            from repositories.compositions import compositions

            job_id = _upstream_job(node, by_key)
            if job_id is None:
                raise _NodeFail("upstream_job_missing", "找不到要验收的合成作业")
            row = compositions.by_job(job_id)
            if row is None:
                raise _NodeFail("composition_missing",
                                f"作业 #{job_id} 没有对应的成片记录")
            view = composition_service.view(row["id"])
            if view["status"] != "succeeded":
                raise _NodeFail("composition_not_ready",
                                f"合成作业现在是「{view['status_label']}」，还不能验收")
            if not view["output_path"]:
                raise _NodeFail("output_missing", "成片记录里没有最终产物路径")
            target = jobfiles.resolve_artifact(view["output_path"])
            if target is None:
                raise _NodeFail("output_missing", "最终成片文件不在了")
            info = validators.inspect(target)
            if info["bytes"] != view["bytes"]:
                raise _NodeFail("output_changed", "最终成片的大小与记录不一致")
            status, output = NODE_DONE, {
                "composition_id": view["id"], "job_id": job_id,
                "version": view["version"], "sha256": view["sha256"],
                "duration_ms": info["duration_ms"], "width": info["width"],
                "height": info["height"], "bytes": info["bytes"]}
        elif ntype == "export_prepare":
            # 成片中心是阶段 D。这里明确 skipped，不写 completed——
            # 没做的事不能记成做了（附件：不得把未验证能力标记为成功）。
            status, output = NODE_SKIPPED, {
                "reason": "成片中心还没建（阶段 D）；这一格只占位，不假装合成了成片"}
        else:
            raise _NodeFail("unknown_node_type", f"不认识的节点类型：{ntype}")
    except _NodeFail as exc:
        pipeline_repo.finish_node(node["id"], NODE_FAILED, error_code=exc.code,
                                  error_message=exc.message)
        pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                {"reason": exc.code, "detail": exc.message[:200]})
        return
    except Exception as exc:               # 内部检查炸了也要落成失败，不能让节点卡在 running
        message = describe_path(" ".join(str(exc).split())[:200])
        pipeline_repo.finish_node(node["id"], NODE_FAILED, error_code="internal_error",
                                  error_message=f"{type(exc).__name__}: {message}")
        pipeline_repo.add_event(run["id"], node["id"], "node_failed",
                                {"reason": "internal_error"})
        return
    pipeline_repo.finish_node(node["id"], status, output=output)
    pipeline_repo.add_event(
        run["id"], node["id"],
        "node_skipped" if status == NODE_SKIPPED else "node_completed", output)


class _NodeFail(Exception):
    """内部节点的"检查没过"。带一个稳定错误码，界面上能分类。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def episode_asset_ids(episode_id: int) -> list[int]:
    ids: list[int] = []
    for row in shots.list_by_episode(episode_id):
        try:
            ids += [int(a) for a in json.loads(row["asset_ids"] or "[]")]
        except (ValueError, TypeError):
            continue
    return sorted(set(ids))


def _unreachable(nodes) -> set[str]:
    """永远跑不到的待执行节点：前置里有失败／取消的，以及它们向下传的。

    阶段 C 验收撞出来的：一格失败之后，下游那些"等前置"的节点永远不会进终态，
    于是 `_finalize` 的"全部终态"条件永远不成立——运行没有 `finished_at`，
    同一集就被自己挡住（再发起回 409），worker 还会每轮白推一次。
    收尾的条件改成"全部终态，或只剩跑不到的"，运行才算结束；
    节点本身仍留在 pending（读时显示"等待前置"），那是事实：它确实没跑。
    """
    status = {n["node_key"]: n["status"] for n in nodes}
    deps = {n["node_key"]: _deps_of(n) for n in nodes}
    dead = {k for k, v in status.items() if v in (NODE_FAILED, NODE_CANCELLED)}
    changed = True
    while changed:
        changed = False
        for key, value in status.items():
            if value == NODE_PENDING and key not in dead \
                    and any(d in dead for d in deps.get(key, [])):
                dead.add(key)
                changed = True
    return dead


def _finalize(run, nodes) -> None:
    """全部节点进终态（或只剩跑不到的）时给运行收尾。

    状态本身不落库——它是现算的（见 run_status）。
    """
    dead = _unreachable(nodes)
    if any(n["status"] not in NODE_FINAL and n["node_key"] not in dead for n in nodes):
        return
    failed = [n for n in nodes if n["status"] == NODE_FAILED]
    pipeline_repo.finish(run["id"])
    pipeline_repo.add_event(run["id"], None,
                            "run_failed" if failed else "run_completed",
                            {"nodes": len(nodes), "failed": len(failed)})


# ---------- 暂停／恢复／取消／重试 ----------
def pause_run(run_id: int) -> dict:
    with transaction():
        run = _need_run(run_id)
        if run["cancelled"]:
            raise PipelineConflict("这条运行已经取消了")
        if run["finished_at"]:
            raise PipelineConflict("这条运行已经结束，不用暂停")
        if not run["paused"]:
            pipeline_repo.set_paused(run_id, True)
            pipeline_repo.add_event(run_id, None, "run_paused", {})
    # 已经在跑的作业不动：暂停的意思是不再派新的，不是把模型进程杀掉
    return run_view(run_id)


def resume_run(run_id: int) -> dict:
    with transaction():
        run = _need_run(run_id)
        if run["cancelled"]:
            raise PipelineConflict("这条运行已经取消了，不能恢复；重新起一条")
        if run["finished_at"]:
            raise PipelineConflict("这条运行已经结束")
        if run["paused"]:
            pipeline_repo.set_paused(run_id, False)
            pipeline_repo.add_event(run_id, None, "run_resumed", {})
    advance_run(run_id)
    return run_view(run_id)


def cancel_run(run_id: int, reason: str = "") -> dict:
    """取消：标记运行、对能安全取消的作业发取消请求、把还没开始的节点收成 cancelled。

    三条不做：不删已生成的产物、不改已完成节点、不强杀不能安全取消的模型任务。
    正在跑的作业只发 `cancel_requested`，它真的停下来之后由 `_sync_job_nodes`
    按真实状态收节点——所以取消之后节点可能还显示"执行中"一小会儿，那是事实。
    """
    with transaction():
        run = _need_run(run_id)
        if run["cancelled"]:
            return run_view(run_id)
        pipeline_repo.set_cancelled(run_id, reason or "人工取消")
        for row in pipeline_repo.nodes_for_run(run_id):
            node = dict(row)
            if node["status"] == NODE_PENDING:
                pipeline_repo.finish_node(node["id"], NODE_CANCELLED,
                                          expect=NODE_PENDING,
                                          error_message="运行取消时这一格还没开始")
            elif node["status"] in (NODE_REVIEW, NODE_ATTENTION) \
                    and not node["job_id"]:
                # 人工确认这类节点没有作业可取消（它等的是签字）。
                # 不在这里收掉的话，运行已经取消了、这一格却还挂着"等待审核"，
                # 之后谁再来签一个字，`_sync_review_nodes` 会把它推成已完成——
                # 取消的运行不该被一次迟到的签字复活。
                pipeline_repo.finish_node(node["id"], NODE_CANCELLED,
                                          expect=node["status"],
                                          error_message="运行取消时这一格还在等人工处理")
            elif node["status"] in (NODE_RUNNING, NODE_REVIEW, NODE_ATTENTION) \
                    and node["job_id"]:
                job = job_repo.get(node["job_id"])
                if job is not None and job["status"] not in JOB_FINISHED:
                    job_repo.request_cancel(node["job_id"])
        pipeline_repo.add_event(run_id, None, "run_cancelled",
                                {"reason": (reason or "人工取消")[:200]})
    return run_view(run_id)


RETRYABLE_JOB_STATUSES = ("failed", "cancelled", "needs_attention")


def retry_gate(node, run, job=None) -> str:
    """这一格现在能不能重试。空串＝可以；否则返回给人看的那句原话。

    **一处判定，两处用**：`retry_node` 拿它决定抛不抛 409，`node_view` 拿它决定
    界面上那枚「重试这一格」摆不摆。原来界面自己按 `status === 'failed'` 判，
    而被驳回的审核节点（`error_code == 'review_rejected'`，服务端一律拒）在页面上
    就永远挂着一颗点了必然报错的钮——按钮该不该在，是业务规则，不是前端样式。

    `job` 传进来是为了让详情页一次批量取回作业后不再逐格回库；不传就按 `node["job_id"]`
    自己读一次（单格重试那条路只有一条作业，不值得为它多写一层）。
    """
    if run["cancelled"]:
        return "这条运行已经取消；重新起一条运行"
    if node["status"] != NODE_FAILED:
        return ("只有失败的节点能重试；这一格现在是"
                f"「{NODE_STATUS_LABELS.get(node['status'], node['status'])}」")
    if node["error_code"] == "review_rejected":
        upstream = _deps_of(node)
        return ("这一格是被驳回的，重试它没有意义：先重试上游生成节点"
                f"{('（' + '、'.join(upstream) + '）') if upstream else ''}，"
                "产出新产物之后这里会重新等签字。")
    if node["node_type"] in JOB_NODES and node["job_id"]:
        if job is None:
            job = job_repo.get(node["job_id"])
        if job is None:
            return "关联的作业已经不在了，没法重试"
        if job["status"] not in RETRYABLE_JOB_STATUSES:
            return (f"作业 #{job['id']} 现在是「{JOB_STATUSES.get(job['status'], job['status'])}」，"
                    f"只有失败、已取消或待核对的作业能重试")
    return ""


def retry_node(node_id: int, user_id: int | None) -> dict:
    """单独重试一个失败节点。旧 Job、旧产物、旧签字一个都不动。

    作业节点走现有 `jobs.retry()`：新建一行、`attempt+1`、原行记
    `superseded_by_job_id`（附件 §6.4 要的语义已经在 Job 层实现，不另写一套）。
    内部检查节点直接回到 pending，下一轮重算。
    被驳回的审核节点不给重试——驳回意味着产物要重做，得先重试上游生成节点。

    返回整条运行的视图，与暂停／恢复／取消一致：重试一格会牵动下游一整片，
    界面要刷的是整屏，不是那一格。
    """
    node = pipeline_repo.get_node(node_id)
    if node is None:
        raise PipelineMissing("节点不存在")
    run = _need_run(node["run_id"])
    job = job_repo.get(node["job_id"]) if node["job_id"] else None
    reason = retry_gate(node, run, job)
    if reason:
        raise PipelineConflict(reason)

    new_job_id = None
    new_status = NODE_PENDING
    if node["node_type"] in JOB_NODES and node["job_id"]:
        new_job_id = job_repo.retry(job, user_id)["id"]
        # 新作业已经排进队列了，节点直接跟着它走；回 pending 会让调度器再提交一条
        new_status = NODE_RUNNING

    with transaction():
        pipeline_repo.reset_for_retry(node_id, job_id=new_job_id, status=new_status)
        # 已经收尾的运行要重新打开，否则节拍只会对齐真相、不再往下派活
        pipeline_repo.reopen(run["id"])
        pipeline_repo.add_event(run["id"], node_id, "node_retried", {
            "old_job_id": node["job_id"], "new_job_id": new_job_id,
            "retry_count": int(node["retry_count"] or 0) + 1})
    advance_run(run["id"])
    return run_view(run["id"])


def _need_run(run_id: int):
    run = pipeline_repo.get_run(run_id)
    if run is None:
        raise PipelineMissing("生产运行不存在")
    return run


# ---------- 视图 ----------
def run_status(run, counts: dict, *, retried: bool | None = None) -> str:
    """运行状态**全部现算**，库里只存 paused／cancelled 两个外生意愿位。

    为什么不全存：状态存两份就要互相同步，而"忘记同步"正是 v0.86 修掉的
    那两个真 bug 的形状。现算慢一点，但永远不会自相矛盾。

    `retried` 是"这条运行里有没有节点返过工"。单条详情自己问（一次查询无所谓），
    列表页一次把整组问完再传进来——否则 N 条运行就是 N 趟额外查询。
    """
    if run["cancelled"]:
        return "cancelled"
    if run["finished_at"]:
        if counts[NODE_FAILED]:
            return "failed"
        if counts[NODE_SKIPPED] or (retried if retried is not None
                                    else _has_retry(run["id"])):
            return "completed_with_errors"
        return "completed"
    if run["paused"]:
        return "paused"
    if counts[NODE_ATTENTION]:
        return "attention"
    if counts[NODE_FAILED]:
        return "failed"
    if counts[NODE_REVIEW]:
        return "waiting_review"
    if counts[NODE_DONE] or counts[NODE_RUNNING]:
        return "running"
    return "running"


def _has_retry(run_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM pipeline_node_run
                WHERE run_id = ? AND retry_count > 0 LIMIT 1""", (run_id,)).fetchone()
    return row is not None


def _display_status(node: dict, by_key: dict) -> str:
    """pending 的节点在界面上分两种：前置没完成＝blocked，前置都完成＝ready。

    这两位不入库（入了就要跟着依赖变化去维护，又是两套事实），只在读的时候算。
    """
    if node["status"] != NODE_PENDING:
        return node["status"]
    deps = json.loads(node.get("depends_on_json") or "[]")
    if all(by_key.get(d, {}).get("status") == NODE_DONE for d in deps):
        return "ready"
    return "blocked"


def node_view(row, by_key: dict, run=None, job=None) -> dict:
    """一格的对外视图。`job` 由 `run_view` 批量取好后传进来，不在这里逐格回库。

    `retryable`／`retry_hint` 不是"给前端的便利字段"：能不能重试是业务规则，
    规则写在 `retry_gate()` 一处，界面按它摆钮、接口按它拒 409——
    两边各判一次，就会出现"按钮在、点了报错"。
    """
    node = dict(row)
    display = _display_status(node, by_key)
    if job is None:
        job = job_repo.get(node["job_id"]) if node["job_id"] else None
    try:
        output = json.loads(node["output_json"] or "{}")
    except ValueError:
        output = {}
    # 没有 run 就没有判定依据（取消位挂在 run 上）——这时宁可不摆钮，
    # 也不给界面一个"看着能点、点了被接口拒"的字段。
    hint = retry_gate(node, run, job) if run is not None else "读不到这条运行，先刷新页面"
    return {
        "id": node["id"], "run_id": node["run_id"], "node_key": node["node_key"],
        "label": node["label"], "node_type": node["node_type"],
        "type_label": NODE_TYPE_LABELS.get(node["node_type"], node["node_type"]),
        "status": display,
        "status_label": NODE_STATUS_LABELS.get(display, display),
        "stored_status": node["status"],
        "depends_on": json.loads(node["depends_on_json"] or "[]"),
        "job_id": node["job_id"],
        "job_status": job["status"] if job else None,
        "job_status_label": (JOB_STATUSES.get(job["status"], job["status"])
                             if job else ""),
        "review_id": node["review_id"],
        "retry_count": int(node["retry_count"] or 0),
        "retryable": not hint,
        "retry_hint": hint,
        "error_code": node["error_code"], "error_message": node["error_message"],
        # 七字段诊断与生成中心那条作业抽屉同一个出处（`error_catalog`）。
        # 放在这里而不是让前端再打一次接口：这一格能不能重试已经算过了（`hint`），
        # 目录要的正是这个口试，前端再问一次只会拿到两份不一致的答复。
        "diagnosis": error_catalog.for_node(node, retry_hint=hint)["diagnosis"],
        "output": output,
        "started_at": node["started_at"] or "", "finished_at": node["finished_at"] or "",
        "created_at": node["created_at"],
    }


def run_view(run_id: int, *, with_events: bool = True) -> dict:
    run = pipeline_repo.get_run(run_id)
    if run is None:
        raise PipelineMissing("生产运行不存在")
    rows = pipeline_repo.nodes_for_run(run_id)
    by_key = _node_map(rows)
    counts = pipeline_repo.counts_for_run(run_id)
    status = run_status(run, counts)
    total = len(rows)
    done = counts[NODE_DONE] + counts[NODE_SKIPPED]
    snapshot = _snapshot_of(run)
    wanted = [row["job_id"] for row in rows if row["job_id"]]
    jobs_by_id = job_repo.get_many(wanted) if wanted else {}
    return {
        "id": run["id"], "project_id": run["project_id"],
        "project_name": run["project_name"],
        "episode_id": run["episode_id"], "episode_title": run["episode_title"],
        "episode_seq": run["episode_seq"],
        "template_id": run["template_id"], "template_name": run["template_name"],
        "template_version": run["template_version"],
        "status": status, "status_label": RUN_STATUS_LABELS.get(status, status),
        "paused": bool(run["paused"]), "cancelled": bool(run["cancelled"]),
        "cancel_reason": run["cancel_reason"],
        "counts": counts,
        "progress": {"total": total, "done": done, "failed": counts[NODE_FAILED],
                     "waiting_review": counts[NODE_REVIEW],
                     "attention": counts[NODE_ATTENTION],
                     "percent": round(done * 100.0 / total, 1) if total else 0.0},
        "shots": len(snapshot.get("shots") or []),
        "document": snapshot.get("document"),
        # 一格一条 Job 是这条详情页最主要的浪费：16 节就是 16 趟回库，
        # 而这一页是人盯着推进、会反复刷的那一页。一次按 id 批量取完再分发。
        "nodes": [node_view(row, by_key, run,
                            jobs_by_id.get(int(row["job_id"])) if row["job_id"] else None)
                  for row in rows],
        "events": ([_event_view(e) for e in pipeline_repo.events_for_run(run_id)]
                   if with_events else []),
        "started_at": run["started_at"] or "", "finished_at": run["finished_at"] or "",
        "created_at": run["created_at"], "updated_at": run["updated_at"],
    }


def _event_view(row) -> dict:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except ValueError:
        payload = {}
    return {"id": row["id"], "event_type": row["event_type"],
            "node_id": row["node_id"], "node_key": row["node_key"],
            "node_label": row["node_label"] or "", "payload": payload,
            "created_at": row["created_at"]}


def runs_for_project(project_id: int) -> list[dict]:
    """项目里的运行记录。列表不带节点与事件——那是详情页的事。

    一共三趟查询，与运行条数无关：运行列表（`list_by_project` 带回的就是整行
    `r.*` ＋ 模板名／集名／集号）、这组运行的节点状态计数、这组运行里哪些返过工。
    原来每条运行再 `get_run()` 一遍是把**已经在手的同一行**又读一次，
    counts 与 retry 也各问一遍——20 条运行就是六十趟。
    """
    rows = pipeline_repo.list_by_project(project_id)
    if not rows:
        return []
    run_ids = [int(row["id"]) for row in rows]
    counts_by_run = pipeline_repo.counts_for_runs(run_ids)
    retried = pipeline_repo.retried_run_ids(run_ids)
    out = []
    for row in rows:
        counts = counts_by_run[int(row["id"])]
        status = run_status(row, counts, retried=int(row["id"]) in retried)
        total = sum(counts.values())
        done = counts[NODE_DONE] + counts[NODE_SKIPPED]
        out.append({
            "id": row["id"], "project_id": row["project_id"],
            "episode_id": row["episode_id"], "episode_title": row["episode_title"],
            "episode_seq": row["episode_seq"],
            "template_id": row["template_id"], "template_name": row["template_name"],
            "template_version": row["template_version"],
            "status": status, "status_label": RUN_STATUS_LABELS.get(status, status),
            "paused": bool(row["paused"]), "cancelled": bool(row["cancelled"]),
            "counts": counts,
            "progress": {"total": total, "done": done, "failed": counts[NODE_FAILED],
                         "waiting_review": counts[NODE_REVIEW],
                         "attention": counts[NODE_ATTENTION],
                         "percent": round(done * 100.0 / total, 1) if total else 0.0},
            "started_at": row["started_at"] or "",
            "finished_at": row["finished_at"] or "",
            "created_at": row["created_at"],
        })
    return out


def template_view(row) -> dict:
    definition = _definition_of(row)
    return {
        "id": row["id"], "name": row["name"], "version": row["version"],
        "description": row["description"], "enabled": bool(row["enabled"]),
        "builtin": bool(row["builtin"]),
        "nodes": [{"key": n.get("key"), "type": n.get("type"),
                   "label": n.get("label") or NODE_TYPE_LABELS.get(n.get("type"),
                                                                  n.get("type")),
                   "depends_on": n.get("depends_on") or [],
                   "fan_out": n.get("fan_out")}
                  for n in definition.get("nodes", []) if isinstance(n, dict)],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
