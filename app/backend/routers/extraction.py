"""文本抽取候选的接口（W13）。

一次抽取仍然是一条 Job：这里只负责把"哪份文档、哪一版"交给 services，
再把候选按人读的形状回给前端。所有候选都必须带原文逐字证据；
确认之前一个字节都不进 asset。
"""
from fastapi import APIRouter, Depends, HTTPException, Response, status as http_status

import access
import docai
import extraction
import services
from deps import require_permission
from models import (ExtractAcceptIn, ExtractItemOut, ExtractMergeIn, ExtractRejectIn,
                    ExtractRunOut, ExtractSubmitIn)
from repositories.documents import documents as docs
from repositories.extraction import runs as extraction_runs
from workspace import PathError

router = APIRouter(prefix="/api", tags=["extraction"])

ITEM_KINDS = ("character", "scene", "prop", "shot")
ITEM_STATUS = ("pending", "accepted", "merged", "rejected")


def _doc(project_id: int, doc_id: int, user, need: str):
    access.ensure(user, project_id, need)
    row = docs.get(doc_id)
    if row is None or row["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    return row


def _run(run_id: int, user, need: str = "read"):
    row = extraction_runs.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="抽取记录不存在")
    access.ensure(user, row["project_id"], need)
    return row


@router.get("/projects/{project_id}/extract/options")
def extraction_options(project_id: int, doc_id: int,
                       user=Depends(require_permission("doc:read"))) -> dict:
    """弹窗里的计价与健康。只读，不建作业、不花钱。"""
    doc = _doc(project_id, doc_id, user, "read")
    try:
        data = services.read_content(doc)
        route = services.text_ai_route()
    except (docai.DocAiInvalid, PathError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    price = route["price"]
    return {
        "doc_id": doc["id"], "doc_title": doc["title"],
        "version": data["version"], "chars": len(data["content"]),
        "provider": route["provider"], "model": route["model"].code,
        "unit": price.unit, "unit_price_micros": price.amount_micros,
        "currency": price.currency, "source": price.source,
        "estimated_quantity": extraction.estimate_quantity(len(data["content"])),
        "prompt_version": extraction.PROMPT_VERSION,
        "health_state": route["health_state"], "health_detail": route["health_detail"],
        "kinds": [{"code": k, "label": extraction.KIND_LABELS[k]} for k in ITEM_KINDS],
    }


@router.post("/projects/{project_id}/extract/runs", response_model=ExtractRunOut,
             status_code=http_status.HTTP_202_ACCEPTED)
def submit_extraction(project_id: int, payload: ExtractSubmitIn,
                      user=Depends(require_permission("doc:write"))) -> ExtractRunOut:
    if payload.project_id != project_id:
        raise HTTPException(status_code=422, detail="项目 id 与路径不一致")
    doc = _doc(project_id, payload.doc_id, user, "write")
    try:
        result = services.extraction_submit(doc, payload, user["id"])
    except extraction.ExtractInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except docai.DocAiInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except services.JobInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    out = services.extraction_view(result["run"], doc)
    out["deduped"] = result["deduped"]
    return ExtractRunOut(**out)


@router.get("/projects/{project_id}/extract/runs", response_model=list[ExtractRunOut])
def list_extractions(project_id: int, limit: int = 30,
                     user=Depends(require_permission("doc:read"))) -> list[ExtractRunOut]:
    access.ensure(user, project_id, "read")
    limit = max(1, min(int(limit), 100))
    return [ExtractRunOut(**services.extraction_view(row))
            for row in extraction_runs.list_runs(project_id, limit)]


@router.get("/extract/runs/{run_id}", response_model=ExtractRunOut)
def get_extraction(run_id: int,
                   user=Depends(require_permission("doc:read"))) -> ExtractRunOut:
    run = _run(run_id, user, "read")
    return ExtractRunOut(**services.extraction_view(run))


@router.get("/extract/runs/{run_id}/items", response_model=list[ExtractItemOut])
def list_items(run_id: int, kind: str | None = None, status: str | None = None,
               user=Depends(require_permission("doc:read"))) -> list[ExtractItemOut]:
    run = _run(run_id, user, "read")
    if kind and kind not in ITEM_KINDS:
        raise HTTPException(status_code=422, detail="候选类型只认角色／场景／道具／镜头")
    if status and status not in ITEM_STATUS:
        raise HTTPException(status_code=422, detail="候选状态不认识")
    return [ExtractItemOut(**row)
            for row in services.extraction_items(run, kind, status)]


@router.post("/extract/items/{item_id}/accept")
def accept_item(item_id: int, payload: ExtractAcceptIn,
                user=Depends(require_permission("asset:write"))) -> dict:
    return _decide(item_id, user, lambda: services.extraction_accept(item_id, payload, user))


@router.post("/extract/items/{item_id}/merge")
def merge_item(item_id: int, payload: ExtractMergeIn,
               user=Depends(require_permission("asset:write"))) -> dict:
    # 全局资产只有管理员能改；先把这一道门过掉，再进事务
    try:
        access.ensure_asset(payload.asset_id, user, "write")
    except HTTPException:
        raise
    return _decide(item_id, user,
                   lambda: services.extraction_merge(item_id, payload.asset_id, user))


@router.post("/extract/items/{item_id}/reject")
def reject_item(item_id: int, payload: ExtractRejectIn,
                user=Depends(require_permission("doc:write"))) -> dict:
    return _decide(item_id, user,
                   lambda: services.extraction_reject(item_id, payload.reason, user))


def _decide(item_id: int, user, action) -> dict:
    try:
        result = action()
    except services.ExtractMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.ExtractConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except extraction.ExtractInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    out = {"item": services.extraction_item_view(
        extraction_runs.get_item(item_id))}
    if result.get("asset") is not None:
        from models import AssetOut
        out["asset"] = AssetOut.from_row(result["asset"]).model_dump()
    if "reason" in result:
        out["reason"] = result["reason"]
    return out


@router.get("/extract/runs/{run_id}/shots.md")
def export_shots(run_id: int,
                 user=Depends(require_permission("doc:read"))) -> Response:
    """镜头候选只导出 Markdown，不写 shot 表（接口草案的 v1 边界）。"""
    run = _run(run_id, user, "read")
    items = services.extraction_items(run)
    text = extraction.shots_markdown(run["doc_title"], items)
    name = f"shots-run{run_id}.md"
    return Response(text, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
