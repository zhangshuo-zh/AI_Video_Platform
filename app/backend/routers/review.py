"""审核中心接口（W14）。

唯一写入口就在这里：跑 gate 与按锚点签字。别的页面只读状态，
签字带着版本与身份，版本一变旧签字就不再算数。
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status as http_status

import access
import reviewgate
import services
from deps import require_permission
from models import ReviewGateOut, ReviewQueueItem, ReviewSignIn
from repositories.documents import documents as docs

router = APIRouter(prefix="/api", tags=["review"])


def _doc(doc_id: int, user, need: str = "read"):
    access.ensure_by("document", doc_id, user, need, "文档")
    row = docs.get(doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return row


@router.get("/projects/{project_id}/reviews", response_model=list[ReviewQueueItem])
def review_queue(project_id: int,
                 user=Depends(require_permission("review:read"))) -> list[ReviewQueueItem]:
    access.ensure(user, project_id, "read")
    return [ReviewQueueItem(**row) for row in services.review_queue(project_id)]


@router.get("/documents/{doc_id}/review-gate", response_model=ReviewGateOut)
def get_gate(doc_id: int, request: Request,
             user=Depends(require_permission("review:read"))) -> ReviewGateOut:
    doc = _doc(doc_id, user, "read")
    can_sign = "review:sign" in getattr(request.state, "permissions", set())
    return ReviewGateOut(**services.review_state(doc, can_sign=can_sign))


@router.post("/documents/{doc_id}/review-gate", response_model=ReviewGateOut,
             status_code=http_status.HTTP_201_CREATED)
def run_gate(doc_id: int, request: Request,
             user=Depends(require_permission("review:sign"))) -> ReviewGateOut:
    """跑一遍确定性规则。不写正文、不建 Job，规则检查不花钱。"""
    doc = _doc(doc_id, user, "write")
    try:
        state = services.review_gate(doc, user["id"])
    except reviewgate.ReviewInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    state["can_sign"] = True
    return ReviewGateOut(**state)


@router.post("/documents/{doc_id}/review-sign", response_model=ReviewGateOut)
def sign(doc_id: int, payload: ReviewSignIn, request: Request,
         user=Depends(require_permission("review:sign"))) -> ReviewGateOut:
    doc = _doc(doc_id, user, "write")
    try:
        state = services.review_sign(doc, payload, user)
    except services.ReviewMissing as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.ReviewStale as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except reviewgate.ReviewInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ReviewGateOut(**state)
