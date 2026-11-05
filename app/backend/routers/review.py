"""审核中心接口（W14；0025 起也审产物）。

唯一写入口就在这里：跑 gate、按锚点签字，文档与产物两档都在这一个模块里落。
别的页面只读状态——签字带着版本与身份，版本一变旧签字就不再算数。
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status as http_status

import access
import reviewgate
import services
from deps import require_permission
from models import (JobReviewOut, ReviewBatchIn, ReviewCheckIn, ReviewGateOut,
                    ReviewQueueItem, ReviewSignIn)
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


@router.get("/jobs/{job_id}/review-checklist")
def job_checklist_view(job_id: int,
                       user=Depends(require_permission("review:read"))) -> dict:
    """一条作业的审核清单：自动项现算 ＋ 人工项已存的结论 ＋ 能不能签字的结论。

    为什么单独一个接口而不塞进 `JobReviewOut`：审核队列一屏几十条作业，
    每条都要 ffprobe 一次才能填这栏，那是把队列页变成一次媒体扫描。
    清单只在人点开某一条时才算。
    """
    return services.job_checklist(_job(job_id, user))


@router.post("/jobs/{job_id}/review-checklist")
def job_checklist_save_view(job_id: int, payload: ReviewCheckIn,
                            user=Depends(require_permission("review:sign"))) -> dict:
    """保存人工清单结论。项不属于这一类能力的清单整批拒（422），不往里掺野 code。"""
    try:
        return services.job_checklist_save(_job(job_id, user, "write"), payload, user)
    except services.ReviewNotReviewable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except reviewgate.ReviewInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/jobs/review-batch")
def job_review_batch_view(payload: ReviewBatchIn,
                          user=Depends(require_permission("review:sign"))) -> dict:
    """批量签字：整批同一个结论，逐条落地、逐条报原因。

    两条口径写死在这里：
    ① **通过（approved）要求那一条的人工清单看完**（§8.2「审核不仅检查文件，
       也检查内容质量」——没看够就不能盖章）；**驳回与退回不要求**，
       一眼看出不行就该能立刻驳回，不能被表单卡住。
    ② 每条都走与单条接口**完全相同**的归属判断与签字路径（下面的 `sign_one`
       就是那条路），批量不是第二个写口子。
    """
    def sign_one(one):
        job = _job(int(one.job_id), user, "write")
        if payload.decision == "approved":
            checklist = services.job_checklist(job)
            if not checklist["can_approve"]:
                raise services.ReviewNotReviewable(
                    f"作业 #{job['id']} 还不能签通过：{checklist['hint']}")
        return services.job_review_sign(
            job, ReviewSignIn(anchor=one.anchor, decision=payload.decision,
                              note=payload.note), user)

    return services.job_review_batch(payload.items, payload.decision,
                                     payload.note, user, sign_one)


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
