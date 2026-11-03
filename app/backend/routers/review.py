"""审核中心接口（W14；0025 起也审产物）。

唯一写入口就在这里：跑 gate、按锚点签字，文档与产物两档都在这一个模块里落。
别的页面只读状态——签字带着版本与身份，版本一变旧签字就不再算数。
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status as http_status

import access
import reviewgate
import services
from deps import require_permission
from models import JobReviewOut, ReviewGateOut, ReviewQueueItem, ReviewSignIn
from repositories.documents import documents as docs
from repositories.jobs import jobs as job_repo

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


# ---------- 产物审核（0025／裁定 D1）：关键帧与镜头视频 ----------
# 为什么挂在审核中心而不是生成中心：不变量 3 说 Review 只有一个写入口。
# 产线的 image_review／shot_review 节点只存签字的引用，不自己记结论——
# 否则"这一镜过没过"就会有两个地方能改，谁也说不清哪个算数。

def _job(job_id: int, user, need: str = "read"):
    """取作业并过项目归属那道门；看不见就是 404，不泄露存在。"""
    row = job_repo.get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="作业不存在")
    access.ensure(user, row["project_id"], need)
    return row


@router.get("/projects/{project_id}/job-reviews", response_model=list[JobReviewOut])
def job_review_queue(project_id: int,
                     user=Depends(require_permission("review:read"))
                     ) -> list[JobReviewOut]:
    """这个项目里可以被人审的产物：已成功的图像／视频作业，带最新签字。"""
    access.ensure(user, project_id, "read")
    return [JobReviewOut(**row) for row in services.job_review_queue(project_id)]


@router.get("/jobs/{job_id}/review", response_model=JobReviewOut)
def job_review(job_id: int, request: Request,
               user=Depends(require_permission("review:read"))) -> JobReviewOut:
    row = _job(job_id, user)
    can_sign = "review:sign" in getattr(request.state, "permissions", set())
    return JobReviewOut(**services.job_review_state(row, can_sign=can_sign))


@router.post("/jobs/{job_id}/review-sign", response_model=JobReviewOut)
def job_review_sign(job_id: int, payload: ReviewSignIn,
                    user=Depends(require_permission("review:sign"))) -> JobReviewOut:
    """给一条作业的产物签字。改判不叠历史；产物换过版本，旧签字自动失配。"""
    row = _job(job_id, user, "write")
    try:
        state = services.job_review_sign(row, payload, user)
    except services.ReviewNotReviewable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except reviewgate.ReviewInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JobReviewOut(**state)
