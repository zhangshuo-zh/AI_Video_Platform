import diskplan
import json
import services
import shutil

from config import COMPOSE_MAX_SHOTS, FFMPEG_BIN, FILES_DIR
from datetime import datetime
from db import transaction
from paths import describe as describe_path, resolve_within
from repositories.content import assets, episodes, shots
from repositories.documents import documents as docs
from repositories.pipeline import pipeline_repo
from repositories.projects import projects as project_repo

from .constants import JOB_NODES, NODE_TYPE_LABELS, PipelineConflict, PipelineInvalid, PipelineMissing
from .templates import _definition_of, validate_definition
from .views import run_view


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
    # 延迟导入：run_create/run_control 在模块级不依赖 dispatch（否则成环）
    from .dispatch import advance_run
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
