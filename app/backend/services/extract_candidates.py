
from log import get_logger as _get_logger
from workspace import PathError

import access
import adapters
import extraction
import jobfiles
import json

from db import transaction
from models import JobEstimateIn
from repositories.content import assets, episodes, shots, variants
from repositories.documents import documents as docs
from repositories.extraction import runs as extraction_runs
from repositories.jobs import jobs as job_repo
from types import SimpleNamespace

from .doc_ai import text_ai_route
from .documents import read_content
from .errors import ExtractConflict, ExtractMissing
from .jobs import _cost_of

_audit_log = _get_logger(__name__)


def _extract_run_materialize(run) -> None:
    """Job 成功后把模型输出落成候选。只在第一次调用时写。

    为什么不放到 worker 里：worker 只认识"跑命令、落文件"，抽取的 JSON 形状
    与字段白名单是业务知识；把它塞进执行壳会让两个进程互相依赖。
    这里做也没关系——GET 是幂等的，materialized_at 一写就不会重复。
    """
    if run["status"] != "pending":
        return
    if run["job_status"] != "succeeded":
        # 只认**已经停下来**的终态。queued/starting/running/cancel_requested
        # 那几档要原样 return——作业还在飞，抽取页显示"抽取中"是对的。
        # 原来连 needs_attention 也没格子：作业早停下来等人核对了，这一行还是
        # 直接 return，于是运行永远挂在 pending，界面上一直"抽取中"，
        # 而错误被吞得干干净净。status 的取值只有 pending|done|failed|parse_failed
        # （0017 的 CHECK），"待核对"没有自己的格子，那就落成 failed 并把真实原因
        # 写进去——失败的原因原话比一个猜出来的状态有用。
        if run["job_status"] in ("failed", "cancelled"):
            extraction_runs.set_status(run["id"], "failed",
                                       run["error_message"] or "作业没有成功结束")
        elif run["job_status"] == "needs_attention":
            extraction_runs.set_status(
                run["id"], "failed",
                "作业停在「待核对」：产物在，但还没人签。抽取候选只在作业成功结束时落，"
                "先去那一格核对完，再重新发起一次抽取。")
        return
    job = job_repo.get(run["job_id"])
    if job is None:
        extraction_runs.set_status(run["id"], "failed", "找不到对应的作业")
        return
    try:
        path = jobfiles.artifact_at(job["result_paths_json"], 0)
        if path is None:
            raise extraction.ExtractInvalid("作业成功了，但没有可读的结果文件")
        raw = path.read_text(encoding="utf-8")
        items, note = extraction.parse_output(_extraction_content(run), raw)
    except (OSError, UnicodeError, extraction.ExtractInvalid) as exc:
        extraction_runs.set_status(run["id"], "parse_failed", str(exc)[:500])
        return
    extraction_runs.insert_items(run["id"], items)
    extraction_runs.set_status(run["id"], "done", note, materialized=True)




def _extraction_content(run) -> str:
    """物化解析要做原文证据校验，所以必须把抽的那一版正文读回来。

    文档删了就无法校验证据，按 parse_failed 处理；不拿"标题相同"的别份正文顶替。
    """
    if run["doc_id"] is None:
        raise extraction.ExtractInvalid("原文档已删除，无法校验候选证据")
    doc = docs.get(run["doc_id"])
    if doc is None:
        raise extraction.ExtractInvalid("原文档已删除，无法校验候选证据")
    data = read_content(doc)
    if data["version"] != run["base_version"]:
        raise extraction.ExtractInvalid(
            "文档在抽取之后被改过，旧证据无法逐字核对；请重新抽取")
    return data["content"]




def extraction_submit(doc, payload, created_by: int | None) -> dict:
    """把一次抽取落成 text Job，25 小时内同文同模型同提示词版本直接复用。"""
    data = read_content(doc)
    prompt, params = extraction.build_prompt(doc["title"], data["content"],
                                             payload.instruction)
    route = text_ai_route()
    if route["health_state"] != "ok":
        raise extraction.ExtractInvalid(f"文本 provider 现在不可用：{route['health_detail']}")
    price = route["price"]
    estimate = payload.estimate
    if not payload.user_confirmed_cloud:
        raise extraction.ExtractInvalid("云端文本作业要先确认按供应商计费")
    if estimate.unit != price.unit:
        raise extraction.ExtractInvalid(f"计价单位应为 {price.unit}，请刷新计价后重试")
    if estimate.currency != price.currency:
        raise extraction.ExtractInvalid(f"币种应为 {price.currency}，请刷新计价后重试")
    if estimate.unit_price_micros != price.amount_micros:
        raise extraction.ExtractInvalid("单价与清单里的官方价不一致，请刷新计价后重试")
    errors = adapters.validate_params(route["adapter"], route["model"].code, params)
    if errors:
        raise extraction.ExtractInvalid("；".join(errors))

    existing = extraction_runs.find_recent(
        doc["id"], data["version"], route["model"].code, extraction.PROMPT_VERSION)
    if existing is not None:
        return {"run": existing, "deduped": True}

    confirmed = JobEstimateIn(
        quantity=estimate.quantity, unit=price.unit,
        unit_price_micros=price.amount_micros, currency=price.currency,
        source=price.source[:200])
    cost = _cost_of(SimpleNamespace(estimate=confirmed, user_confirmed_cloud=True),
                    "cloud", created_by)
    # Job 行与抽取运行行必须在**同一个事务**里落（裁定 D2）。
    # 原来是两次独立提交：进程在中间死掉，就会留下一条"没有归属的付费 Job"——
    # 抽取页看不见它（run 没建），成本账里却有它（job 建了），而这笔钱是云端调用，
    # 供应商那边可能已经计了费。两步都是纯库内写，不碰 Git／磁盘／网络，
    # 正好落在 `transaction()` 允许的边界里。
    with transaction():
        job = job_repo.create({
            "project_id": doc["project_id"],
            "episode_id": doc["episode_id"],
            "shot_id": None,
            "capability": "text",
            "kind": "text",
            "execution_mode": "cloud",
            "provider_code": route["provider"],
            "model_code": route["model"].code,
            "adapter_version": adapters.MANIFEST_VERSION,
            "prompt_snapshot": prompt,
            "params": params,
            "input_refs": [{
                "operation": "extract",
                "doc_id": doc["id"],
                "doc_title": doc["title"],
                "base_version": data["version"],
                "prompt_version": extraction.PROMPT_VERSION,
            }],
            "priority": 0,
            "user_confirmed_cloud": True,
            "created_by": created_by,
            **cost,
        })
        run = extraction_runs.create_run(
            project_id=doc["project_id"], doc_id=doc["id"], job_id=job["id"],
            doc_title=doc["title"], base_version=data["version"],
            content_hash=data["version"], model_code=route["model"].code,
            prompt_version=extraction.PROMPT_VERSION, created_by=created_by)
    return {"run": run, "deduped": False}




_STATUS_LABELS = {
    "pending": "抽取中", "done": "候选已生成",
    "failed": "抽取失败", "parse_failed": "输出无法解析",
}


_ITEM_LABELS = {"pending": "待处理", "accepted": "已入库",
                "merged": "已合并", "rejected": "已驳回"}




def extraction_view(run, doc=None) -> dict:
    """运行摘要。`stale` 让界面一眼看出证据对的是旧正文。"""
    _extract_run_materialize(run)
    run = extraction_runs.get_run(run["id"]) or run
    current = ""
    if doc is not None:
        try:
            current = read_content(doc)["version"]
        except (OSError, UnicodeError, PathError) as exc:
            _audit_log.debug("extraction_view_fallback error=%s", type(exc).__name__)
            current = ""
    elif run["doc_id"] is not None:
        d = docs.get(run["doc_id"])
        if d is not None:
            try:
                current = read_content(d)["version"]
            except (OSError, UnicodeError, PathError) as exc:
                _audit_log.debug("extraction_view_fallback error=%s", type(exc).__name__)
                current = ""
    return {
        "id": run["id"], "project_id": run["project_id"], "doc_id": run["doc_id"],
        "doc_title": run["doc_title"], "job_id": run["job_id"],
        "status": run["status"], "status_label": _STATUS_LABELS.get(run["status"], run["status"]),
        "model_code": run["model_code"], "prompt_version": run["prompt_version"],
        "base_version": run["base_version"], "current_version": current,
        "stale": bool(current and current != run["base_version"]),
        "note": run["note"],
        "counts": extraction_runs.counts(run["id"]),
        "cost_micros": run["cost_micros"], "currency": run["currency"],
        "billable_status": run["billable_status"] or "",
        "error_code": run["error_code"], "error_message": run["error_message"],
        "created_at": run["created_at"],
    }




def extraction_item_view(row) -> dict:
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    try:
        evidence = json.loads(row["evidence"] or "{}")
    except ValueError:
        evidence = {}
    return {
        "id": row["id"], "run_id": row["run_id"], "kind": row["kind"],
        "kind_label": extraction.KIND_LABELS.get(row["kind"], row["kind"]),
        "payload": payload, "confidence": row["confidence"], "evidence": evidence,
        "status": row["status"],
        "status_label": _ITEM_LABELS.get(row["status"], row["status"]),
        "asset_id": row["asset_id"], "decided_by_name": row["decided_by_name"] or "",
        "decided_at": row["decided_at"],
    }




def extraction_items(run, kind: str | None = None, status: str | None = None) -> list[dict]:
    _extract_run_materialize(run)
    run_id = run["id"]
    return [extraction_item_view(row)
            for row in extraction_runs.items(run_id, kind, status)]




def _load_extract_item(item_id: int, user, need: str = "write"):
    row = extraction_runs.get_item(item_id)
    if row is None:
        raise ExtractMissing("候选不存在")
    run = extraction_runs.get_run(row["run_id"])
    if run is None:
        raise ExtractMissing("候选不存在")
    access.ensure(user, run["project_id"], need)
    return row, run




def _candidate_fields(item, overrides) -> tuple[str, str, str, list[str], list[str]]:
    try:
        payload = json.loads(item["payload"] or "{}")
    except ValueError:
        payload = {}
    if item["kind"] == "character":
        summary = "；".join(x for x in (payload.get("role"), payload.get("appearance"),
                                       payload.get("personality")) if x)
        prompt = payload.get("suggested_prompt", "")
    elif item["kind"] == "scene":
        summary = "；".join(x for x in (payload.get("time_of_day"), payload.get("weather"),
                                       payload.get("description")) if x)
        prompt = payload.get("suggested_prompt", "")
    else:
        summary = payload.get("description", "")
        prompt = payload.get("suggested_prompt", "")
    aliases = payload.get("aliases") or []
    name = (overrides.name or payload.get("name") or "").strip()
    if overrides.name is not None:
        name = overrides.name.strip()
    if overrides.summary is not None:
        summary = overrides.summary.strip()
    if overrides.consistency_prompt is not None:
        prompt = overrides.consistency_prompt.strip()
    if overrides.aliases is not None:
        aliases = overrides.aliases
    return name, summary, prompt, list(aliases), list(overrides.tags or [])




def extraction_accept(item_id: int, overrides, user) -> dict:
    """确认一条候选：从暂存表落成 asset。镜头候选走导出，不在这里入库。"""
    row, run = _load_extract_item(item_id, user, "write")
    if row["kind"] == "shot":
        raise extraction.ExtractInvalid("镜头候选只导出 Markdown，不入资产库")
    if row["status"] != "pending":
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    doc = docs.get(run["doc_id"]) if run["doc_id"] else None
    if doc is None:
        raise ExtractConflict("原文档已删除，不能再按这条证据入库")
    original = read_content(doc)
    if original["version"] != run["base_version"]:
        raise ExtractConflict("文档在抽取之后被改过，旧证据不能再用；请重新抽取")
    if overrides.variant_asset_id is not None or overrides.variant_name is not None:
        target_id = overrides.variant_asset_id
        variant_name = (overrides.variant_name or "").strip()
        if row["kind"] != "character" or target_id is None or not variant_name:
            raise extraction.ExtractInvalid("请为角色候选选择目标角色并填写形态名称")
        with transaction():
            target = access.ensure_asset(target_id, user, "write")
            if target["project_id"] not in (None, run["project_id"]):
                raise ExtractMissing("目标资产不存在")
            full_target = assets.get(target_id)
            if full_target is None or full_target["kind"] != "character":
                raise extraction.ExtractInvalid("请选择角色资产")
            evidence = json.loads(row["evidence"] or "{}")
            quote = evidence.get("quote")
            if not isinstance(quote, str) or not quote or quote not in original["content"]:
                raise ExtractConflict("这条候选缺少可核对的原文证据")
            if variants.exists_name(target_id, "outfit", variant_name):
                raise ExtractConflict("已存在同名服饰形态")
            payload = json.loads(row["payload"] or "{}")
            prompt = overrides.consistency_prompt
            if prompt is None:
                prompt = payload.get("suggested_prompt") or payload.get("appearance") or ""
            variants.create(asset_id=target_id, variant_type="outfit", name=variant_name, prompt=prompt.strip())
            if not extraction_runs.decide(item_id, "accepted", user["id"], target_id):
                raise ExtractConflict("这条候选已经处理过了，刷新后再看")
        return {"item": extraction_runs.get_item(item_id), "asset": full_target}

    name, summary, prompt, aliases, tags = _candidate_fields(row, overrides)
    if not name:
        raise extraction.ExtractInvalid("资产名不能为空")
    if len(name) > 60:
        raise extraction.ExtractInvalid("资产名最多 60 字")
    if assets.exists_name(run["project_id"], row["kind"], name):
        raise ExtractConflict("同一范围、同一类型里已经有这个名字；请用合并")
    asset = assets.create(run["project_id"], row["kind"], name, summary, prompt,
                          aliases, tags)
    if not extraction_runs.decide(row["id"], "accepted", user["id"], asset["id"]):
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    return {"item": extraction_runs.get_item(row["id"]), "asset": asset}




def extraction_merge(item_id: int, asset_id: int, user) -> dict:
    """把候选并进已有资产：别名、简介与提示词只补空，不覆盖人已经写的。"""
    row, run = _load_extract_item(item_id, user, "write")
    if row["kind"] == "shot":
        raise extraction.ExtractInvalid("镜头候选只导出 Markdown，不入资产库")
    if row["status"] != "pending":
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    target = assets.get(asset_id)
    if target is None or (target["project_id"] not in (None, run["project_id"])):
        raise ExtractMissing("目标资产不存在")
    if target["kind"] != row["kind"]:
        raise extraction.ExtractInvalid("候选类型与目标资产类型不一致")
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    aliases = json.loads(target["aliases"] or "[]")
    for name in [payload.get("name"), *(payload.get("aliases") or [])]:
        if name and name != target["name"] and name not in aliases:
            aliases.append(name)
    if row["kind"] == "character":
        extra = "；".join(x for x in (payload.get("appearance"),
                                     payload.get("personality")) if x)
    else:
        extra = payload.get("description", "")
    summary = target["summary"] or extra
    prompt = target["consistency_prompt"] or payload.get("suggested_prompt", "")
    tags = json.loads(target["tags"] or "[]") if "tags" in target.keys() else []
    assets.update(target["id"], kind=target["kind"], name=target["name"],
                  summary=summary, consistency_prompt=prompt, aliases=aliases,
                  tags=tags, project_id=target["project_id"])
    if not extraction_runs.decide(row["id"], "merged", user["id"], target["id"]):
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    return {"item": extraction_runs.get_item(row["id"]), "asset": assets.get(target["id"])}




def extraction_reject(item_id: int, reason: str, user) -> dict:
    row, _run = _load_extract_item(item_id, user, "write")
    if row["status"] != "pending":
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    if not extraction_runs.decide(row["id"], "rejected", user["id"]):
        raise ExtractConflict("这条候选已经处理过了，刷新后再看")
    return {"item": extraction_runs.get_item(row["id"]), "reason": reason}


def extraction_import_shots(run_id: int, payload, user: dict) -> dict:
    """将抽取出的镜头候选一键落库到 shot 表（自动匹配项目资产并保持幂等）。"""
    run = extraction_runs.get_run(run_id)
    if run is None:
        raise ExtractMissing("抽取任务不存在")
    project_id = int(run["project_id"])
    access.ensure(user, project_id, "write")
    _extract_run_materialize(run)

    req_episode_id = getattr(payload, "episode_id", None)
    doc = docs.get(run["doc_id"]) if run["doc_id"] is not None else None
    if doc is not None and doc["episode_id"] is not None:
        episode_id = int(doc["episode_id"])
    elif req_episode_id is not None:
        ep = episodes.get(int(req_episode_id))
        if ep is None or int(ep["project_id"]) != project_id:
            raise extraction.ExtractInvalid("目标创作单元不存在或不属于该项目")
        episode_id = int(ep["id"])
    else:
        ep_rows = episodes.list_by_project(project_id)
        if not ep_rows:
            raise extraction.ExtractInvalid("项目中尚无创作单元，请先新建或指定创作单元")
        episode_id = int(ep_rows[0]["id"])

    item_ids = getattr(payload, "item_ids", None)
    selected_ids = None if item_ids is None else set(item_ids)
    with transaction():
        return _import_shot_candidates(run_id, project_id, episode_id, selected_ids, int(user["id"]))


def _import_shot_candidates(run_id: int, project_id: int, episode_id: int,
                            selected_ids: set[int] | None, user_id: int) -> dict:
    visible_assets = assets.list_visible(project_id)
    exact_map: dict[str, int] = {}
    multi_names: list[tuple[str, int]] = []
    for asset_row in visible_assets:
        name = str(asset_row["name"] or "").strip()
        if not name:
            continue
        low = name.lower()
        exact_map.setdefault(low, int(asset_row["id"]))
        if len(name) >= 2:
            multi_names.append((low, int(asset_row["id"])))

    existing = shots.list_by_episode(episode_id)
    existing_pairs = {
        (str(s["summary"] or "").strip(), str(s["prompt"] or "").strip())
        for s in existing
    }
    existing_summaries = {str(s["summary"] or "").strip() for s in existing}

    raw_items = extraction_runs.items(run_id, kind="shot")
    if selected_ids is not None:
        raw_items = [row for row in raw_items if row["id"] in selected_ids]

    def _sort_key(row) -> tuple[int, int]:
        try:
            idx = int((json.loads(row["payload"] or "{}")).get("index") or 0)
        except (TypeError, ValueError):
            idx = 0
        return (idx, int(row["id"]))

    created_shot_ids: list[int] = []
    skipped_item_ids: list[int] = []
    matched_assets = 0

    for row in sorted(raw_items, key=_sort_key):
        item_id = int(row["id"])
        if row["status"] not in ("pending", "accepted"):
            skipped_item_ids.append(item_id)
            continue
        try:
            p = json.loads(row["payload"] or "{}")
        except ValueError:
            p = {}
        summary = str(p.get("summary") or "").strip()[:500]
        if not summary:
            skipped_item_ids.append(item_id)
            continue

        dialogue = [str(d).strip() for d in (p.get("dialogue") or []) if str(d).strip()]
        prompt_parts = [
            str(p.get("scene") or "").strip(),
            summary,
            (f"对白：{' / '.join(dialogue)}" if dialogue else ""),
        ]
        prompt = "，".join([part for part in prompt_parts if part])[:2000]

        if (summary, prompt) in existing_pairs or (
            row["status"] == "accepted" and summary in existing_summaries
        ):
            skipped_item_ids.append(item_id)
            continue

        matched_ids: list[int] = []
        seen_ids: set[int] = set()
        ref_tokens = [str(p.get("scene") or "").strip()] + [
            str(x).strip() for x in (p.get("asset_refs") or [])
        ]
        for token in ref_tokens:
            if not token:
                continue
            aid = exact_map.get(token.lower())
            if aid is not None and aid not in seen_ids:
                seen_ids.add(aid)
                matched_ids.append(aid)
        haystack = f"{summary} {' '.join(dialogue)}".lower()
        for low_name, aid in multi_names:
            if aid not in seen_ids and low_name in haystack:
                seen_ids.add(aid)
                matched_ids.append(aid)

        try:
            dur = float(p.get("duration_hint") or 5.0)
        except (TypeError, ValueError):
            dur = 5.0
        dur = min(60.0, max(0.5, dur))

        shot_row = shots.create(
            episode_id,
            summary,
            prompt,
            dur,
            matched_ids,
            shot_size=str(p.get("shot_size") or "").strip()[:40],
            camera_move=str(p.get("camera_move") or "").strip()[:40],
        )
        if row["status"] == "pending":
            extraction_runs.decide(item_id, "accepted", user_id, None)
        created_shot_ids.append(int(shot_row["id"]))
        existing_pairs.add((summary, prompt))
        existing_summaries.add(summary)
        matched_assets += len(matched_ids)

    return {
        "run_id": run_id,
        "episode_id": episode_id,
        "created_shot_ids": created_shot_ids,
        "skipped_item_ids": skipped_item_ids,
        "matched_assets": matched_assets,
    }
