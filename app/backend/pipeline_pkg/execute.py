import jobfiles
import json
import services

from db import get_conn
from paths import describe as describe_path
from repositories.audio import clips
from repositories.content import episodes, shots
from repositories.jobs import jobs as job_repo
from repositories.pipeline import NODE_DONE, NODE_FAILED, NODE_REVIEW, NODE_RUNNING, NODE_SKIPPED, pipeline_repo
from repositories.projects import projects as project_repo
from types import SimpleNamespace

from .constants import JOB_NODES, REVIEW_NODES
from .graph import _idempotency_key, _shot_of_key, _snapshot_of, _upstream_image, _upstream_job
from .run_create import _pick_document, _ref_image_state




def _execute_node(run, node: dict, by_key: dict, *, pending_jobs=None,
                  pending_compose=None, probes=None) -> None:
    ntype = node["node_type"]
    if ntype == "export_compose":
        _submit_composition_node(run, node, pending=pending_compose)
    elif ntype in JOB_NODES:
        _submit_job_node(run, node, by_key, pending=pending_jobs)
    elif ntype in REVIEW_NODES:
        _arm_review_node(run, node, by_key)
    else:
        _run_internal_node(run, node, by_key, probes=probes)




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




def _submit_job_node(run, node: dict, by_key: dict, pending=None) -> None:
    """在写事务里原子占住这一格，把「建 Job」的请求交给调用方到事务外执行。

    顺序是：原子转态 →（事务外）建 Job → 回写 job_id → 记事件。
    `try_begin()` 保证连点两次重试只有一个能赢；幂等键保证即使重放也只建
    一条 Job。事务只保护「这一格归谁」，不把 `submit_job` 里的 HTTP 探测
    与参数校验关在写锁里。
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
    if pending is not None:
        pending.append((dict(node), payload, key))
        return
    _commit_submission(run, node, payload, key)




def _commit_submission(run, node: dict, payload, key: str) -> None:
    """事务外真正建 Job 并回写节点。

    先认幂等键：上一轮事务提交了、进程却没来得及建单时，重放不会排出第二条
    Job（云端那条尤其不能重复扣费）。
    """
    existing = job_repo.by_key(key)
    if existing is not None:
        pipeline_repo.attach_job(node["id"], existing["id"])
        pipeline_repo.add_event(run["id"], node["id"], "job_submitted",
                                {"job_id": existing["id"], "reused": True})
        return
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
        "job_id": job["id"], "capability": payload.capability,
        "provider": job["provider_code"], "model": job["model_code"],
        "execution_mode": job["execution_mode"]})




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




def _submit_composition_node(run, node: dict, pending=None) -> None:
    """成片合成节点：事务里只原子占住这一格，真正的建单在事务外。

    它和生成节点的区别只有"参数从哪来"：生成走模型白名单，合成走时间线。
    幂等同样用 `pl-<run>-<key>-a<n>`：同一次运行重放不会排出两个版本。
    `create_composition` 要建时间线、验素材、起 Job，不该占着写锁做。
    """
    if not pipeline_repo.try_begin(node["id"]):
        return
    key = _idempotency_key(run, node)
    if pending is not None:
        pending.append((dict(node), key))
        return
    _commit_composition(run, node, key)




def _commit_composition(run, node: dict, key: str) -> None:
    """事务外建时间线＋成片行＋Job，再把 job_id 回写到节点。"""
    from composition.service import (CompositionConflict, CompositionInvalid,
                                     CompositionMissing, create as create_composition)

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




def _run_internal_node(run, node: dict, by_key: dict, probes=None) -> None:
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
            # ffprobe 由 `advance_run` 在事务外预取；没预取到（例如直接调本函数）
            # 才退回现场探测，行为与改动前一致。
            info = probes.get(node["id"]) if probes else None
            if info is None:
                info = validators.inspect(target)
            elif isinstance(info, Exception):
                raise _NodeFail("probe_failed", f"成片探测失败：{info}")
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
