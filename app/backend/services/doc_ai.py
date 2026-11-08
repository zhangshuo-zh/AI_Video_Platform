import adapters
import docai
import jobfiles
import json

from config import DOC_MAX_BYTES, LOCK_MINUTES
from models import JOB_STATUSES, JobEstimateIn
from repositories.jobs import jobs as job_repo
from types import SimpleNamespace

from .documents import read_content, save_content
from .errors import Conflict, DocAiJobMissing, NoRoute
from .jobs import _cost_of
from .providers import resolve_provider


def text_ai_route() -> dict:
    """文本 AI 的唯一路由出口：默认轨 + 清单里的模型/价格 + 健康。

    文本没有合格的本地路线（规格 02 §8.2），所以这里只接受 cloud；
    默认轨没配好就报错，不偷偷换到另一家。
    """
    try:
        got = resolve_provider("text")
    except NoRoute as exc:
        raise docai.DocAiInvalid(f"文本 AI 路由不可用：{exc}") from exc
    if got["backend"] != "cloud":
        raise docai.DocAiInvalid("文本 AI 操作需要云端文本 provider；本地没有合格的文本路线")
    adapter = got["adapter"]
    model = next((m for m in adapter.models if m.code == got["model"]), None)
    if model is None:
        raise docai.DocAiInvalid(
            f"模型 {got['model']} 不在清单里；先到系统设置核对文本槽位")
    price = next((p for p in model.prices if p.unit == "million_token"), None)
    if price is None:
        raise docai.DocAiInvalid(
            f"文本模型 {model.code} 没有登记百万 token 价格，不能提交计费作业")
    health = adapters.health(got["row"])
    return {"row": got["row"], "adapter": adapter, "model": model,
            "price": price, "provider": got["provider"],
            "health_state": health["state"], "health_detail": health["detail"]}




def _ai_input(doc, operation: str, scope: str, start: int,
              end: int, selection_text: str) -> tuple[dict, dict]:
    """读盘上正文并收敛范围。所有 AI 入口都先过这一条，口径只有一份。"""
    data = read_content(doc)
    part = docai.resolve_scope(data["content"], operation, scope, start, end,
                               selection_text)
    return data, part




def doc_ai_options(doc, operation: str, scope: str, start: int, end: int,
                   selection_text: str = "") -> dict:
    """给弹窗的计价与健康。数量按“输入字数 + 输出上限”算，服务端不给猜。"""
    info = docai.operation_info(operation)
    _data, part = _ai_input(doc, operation, scope, start, end, selection_text)
    route = text_ai_route()
    quantity = docai.estimate_quantity(len(part["text"]), info["max_tokens"])
    return {
        "operation": operation,
        "operation_label": info["label"],
        "provider": route["provider"],
        "model": route["model"].code,
        "unit": route["price"].unit,
        "unit_price_micros": route["price"].amount_micros,
        "currency": route["price"].currency,
        "source": route["price"].source,
        "max_tokens": info["max_tokens"],
        "chars": len(part["text"]),
        "estimated_quantity": quantity,
        "health_state": route["health_state"],
        "health_detail": route["health_detail"],
        "hint": info["hint"],
    }




def submit_doc_ai_job(doc, payload, created_by: int | None, actor: str) -> dict:
    """把一次文档 AI 操作落成一条 text Job。

    Job 里冻结三样东西：拼好的提示词（含选中的原文）、base_version（正文哈希）、
    选区偏移与 apply_mode。应用时只认这份快照，不拿客户端后来传来的新正文。
    """
    data, part = _ai_input(doc, payload.operation, payload.scope,
                           payload.start, payload.end, payload.selection_text)
    route = text_ai_route()
    # 健康不过就不建作业：作业一旦落行，成本表里就有一条账单，
    # 而缺密钥这种失败一次请求都不会发出去——那笔钱不该出现在账上。
    if route["health_state"] != "ok":
        raise docai.DocAiInvalid(
            f"文本 provider 现在不可用：{route['health_detail']}")
    price = route["price"]
    estimate = payload.estimate
    if not payload.user_confirmed_cloud:
        raise docai.DocAiInvalid("云端文本作业要先确认按供应商计费")
    if estimate.unit != price.unit:
        raise docai.DocAiInvalid(f"计价单位应为 {price.unit}，请刷新计价后重试")
    if estimate.currency != price.currency:
        raise docai.DocAiInvalid(f"币种应为 {price.currency}，请刷新计价后重试")
    if estimate.unit_price_micros != price.amount_micros:
        raise docai.DocAiInvalid("单价与清单里的官方价不一致，请刷新计价后重试")

    prompt, params = docai.build_prompt(
        payload.operation, doc["title"], data["content"],
        part["text"], payload.instruction)
    errors = adapters.validate_params(route["adapter"], route["model"].code, params)
    if errors:
        raise docai.DocAiInvalid("；".join(errors))

    # 价格来源以清单为准，不采信客户端传上来的 source。
    # 数量仍用用户确认过的那个数（界面按同一公式预填）。
    confirmed = JobEstimateIn(
        quantity=estimate.quantity, unit=price.unit,
        unit_price_micros=price.amount_micros, currency=price.currency,
        source=price.source[:200])
    cost = _cost_of(SimpleNamespace(estimate=confirmed, user_confirmed_cloud=True),
                    "cloud", created_by)
    refs = [{
        "doc_id": doc["id"],
        "doc_title": doc["title"],
        "operation": payload.operation,
        "operation_label": docai.operation_label(payload.operation),
        "base_version": data["version"],
        "scope": part["scope"],
        "selection_start": part["start"],
        "selection_end": part["end"],
        "apply_mode": part["apply_mode"],
    }]
    return job_repo.create({
        "project_id": doc["project_id"],
        "episode_id": doc["episode_id"],
        "shot_id": None,
        "capability": "text",
        # kind 仍是 text：job.kind 有 CHECK 白名单，不为 W11 重建整张表。
        # “这条 text 作业属于哪份文档”由 input_refs 里的 doc_id/operation 回答。
        "kind": "text",
        "execution_mode": "cloud",
        "provider_code": route["provider"],
        "model_code": route["model"].code,
        "adapter_version": adapters.MANIFEST_VERSION,
        "prompt_snapshot": prompt,
        "params": params,
        "input_refs": refs,
        "priority": 0,
        "user_confirmed_cloud": True,
        "created_by": created_by,
        **cost,
    })




def _ai_ref(job) -> dict:
    try:
        refs = json.loads(job["input_refs_json"] or "[]")
    except (ValueError, TypeError):
        refs = []
    ref = refs[0] if isinstance(refs, list) and refs and isinstance(refs[0], dict) else None
    if not ref or not ref.get("doc_id") or not ref.get("operation") or not ref.get("base_version"):
        raise DocAiJobMissing("这条作业不是文档 AI 操作")
    return ref




def _ai_job_for_doc(doc, job_id: int):
    job = job_repo.get(job_id)
    if job is None:
        raise DocAiJobMissing("AI 作业不存在")
    ref = _ai_ref(job)
    if job["project_id"] != doc["project_id"] or int(ref["doc_id"]) != int(doc["id"]):
        raise DocAiJobMissing("AI 作业不存在")
    return job, ref




def _ai_result_text(job) -> str:
    path = jobfiles.artifact_at(job["result_paths_json"], 0)
    if path is None:
        raise docai.DocAiInvalid("作业成功了，但找不到可读的正文产物")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise docai.DocAiInvalid(f"读不到 AI 结果：{exc}") from exc
    if len(text.encode("utf-8")) > DOC_MAX_BYTES:
        raise docai.DocAiInvalid(
            f"AI 结果超过单份正文上限 {DOC_MAX_BYTES // 1024} KB，不能应用")
    return text




def doc_ai_job_view(job) -> dict:
    """提交后回给界面的那份摘要。"""
    ref = _ai_ref(job)
    return {
        "job_id": job["id"], "status": job["status"],
        "status_label": JOB_STATUSES.get(job["status"], job["status"]),
        "operation": ref["operation"],
        "operation_label": ref.get("operation_label")
        or docai.operation_label(ref["operation"]),
        "apply_mode": ref.get("apply_mode", "replace_document"),
        "base_version": ref["base_version"],
        "selection_start": int(ref.get("selection_start") or 0),
        "selection_end": int(ref.get("selection_end") or 0),
        "provider_code": job["provider_code"], "model_code": job["model_code"],
        "error_code": job["error_code"], "error_message": job["error_message"],
    }




def doc_ai_preview(doc, job_id: int) -> dict:
    """作业状态 + “盘上正文 → 应用后正文”的统一 diff。

    文档在 AI 跑的过程中被改过时**不计算 diff、不允许应用**：
    这时按旧偏移拼回去会改错位置，宁可让人重新跑一次。
    """
    job, ref = _ai_job_for_doc(doc, job_id)
    base = read_content(doc)
    out = {
        "job_id": job["id"], "status": job["status"],
        "status_label": JOB_STATUSES.get(job["status"], job["status"]),
        "operation": ref["operation"],
        "operation_label": ref.get("operation_label")
        or docai.operation_label(ref["operation"]),
        "apply_mode": ref.get("apply_mode", "replace_document"),
        "base_version": ref["base_version"], "current_version": base["version"],
        "can_apply": False, "conflict": False,
        "diff": "", "content": "",
        "base_chars": len(base["content"]), "result_chars": 0, "applied_chars": 0,
        "note": "作业还没有成功结束；原文不会变化。",
        "error_code": job["error_code"], "error_message": job["error_message"],
    }
    if job["status"] != "succeeded":
        return out
    if base["version"] != ref["base_version"]:
        out["conflict"] = True
        out["note"] = ("文档在 AI 运行期间被改过（版本已变），这次结果不能直接应用；"
                       "原文保持不动，需要的话重新跑一次。")
        return out
    try:
        result = _ai_result_text(job)
        applied = docai.apply_text(base["content"], result,
                                   ref.get("apply_mode", "replace_document"),
                                   int(ref.get("selection_start") or 0),
                                   int(ref.get("selection_end") or 0))
    except docai.DocAiInvalid as exc:
        out["note"] = str(exc)
        return out
    out.update({
        "can_apply": True, "diff": docai.diff_text(base["content"], applied),
        "content": result, "result_chars": len(result),
        "applied_chars": len(applied),
        "note": "版本一致，可以应用；写回时仍然过锁与冲突检查。",
    })
    return out




def apply_doc_ai(doc, job_id: int, actor: str, user_id: int) -> dict:
    """把成功的 AI 结果写回正文。写盘、锁、冲突、Git 全部复用 save_content。"""
    job, ref = _ai_job_for_doc(doc, job_id)
    if job["status"] != "succeeded":
        raise docai.DocAiInvalid("AI 作业没有成功结束，不能应用")
    base = read_content(doc)
    if base["version"] != ref["base_version"]:
        raise Conflict(
            "文档在 AI 运行期间被改过（版本已变），这次结果不能应用。"
            "原文保持不动；需要的话重新跑一次。", base["version"])
    result = _ai_result_text(job)
    applied = docai.apply_text(base["content"], result,
                               ref.get("apply_mode", "replace_document"),
                               int(ref.get("selection_start") or 0),
                               int(ref.get("selection_end") or 0))
    label = ref.get("operation_label") or docai.operation_label(ref["operation"])
    saved = save_content(
        doc, applied, base["version"], actor, user_id, LOCK_MINUTES,
        commit_message=f"AI {label}：{doc['title']}（作业 #{job['id']}）")
    return {"job": job, "ref": ref, "saved": saved}
