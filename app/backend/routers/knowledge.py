"""设定中心路由（M4）：项目画风词缀库与设定矛盾审阅面板。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status

import access
import services
from deps import require_permission
from models import (
    ConsistencyIssueOut,
    ConsistencyResolveIn,
    ConsistencyScanOut,
    StyleAffixIn,
    StyleAffixOut,
    StyleAffixUpdateIn,
)
from repositories.knowledge import knowledge

router = APIRouter(prefix="/api", tags=["knowledge"])


def _affix_or_404(project_id: int, affix_id: int):
    row = knowledge.get_affix(affix_id)
    if row is None or int(row["project_id"]) != project_id:
        raise HTTPException(status_code=404, detail="画风词缀不存在")
    return row


def _issue_or_404(project_id: int, issue_id: int):
    row = knowledge.get_issue(issue_id)
    if row is None or int(row["project_id"]) != project_id:
        raise HTTPException(status_code=404, detail="设定矛盾条目不存在")
    return row


@router.get(
    "/projects/{project_id}/knowledge/affixes",
    response_model=list[StyleAffixOut],
)
def list_affixes(
    project_id: int,
    user=Depends(require_permission("project:read")),
) -> list[StyleAffixOut]:
    access.ensure(user, project_id, "read")
    return [StyleAffixOut(**services.affix_view(row)) for row in knowledge.list_affixes(project_id)]


@router.post(
    "/projects/{project_id}/knowledge/affixes",
    response_model=StyleAffixOut,
    status_code=http_status.HTTP_201_CREATED,
)
def create_affix(
    project_id: int,
    payload: StyleAffixIn,
    user=Depends(require_permission("asset:write")),
) -> StyleAffixOut:
    access.ensure(user, project_id, "write")
    row = knowledge.create_affix(
        project_id=project_id,
        name=payload.name,
        category=payload.category,
        positive=payload.positive,
        negative=payload.negative,
        is_default=payload.is_default,
        sort_order=payload.sort_order,
        created_by=user["id"],
    )
    return StyleAffixOut(**services.affix_view(row))


@router.put(
    "/projects/{project_id}/knowledge/affixes/{affix_id}",
    response_model=StyleAffixOut,
)
def update_affix(
    project_id: int,
    affix_id: int,
    payload: StyleAffixUpdateIn,
    user=Depends(require_permission("asset:write")),
) -> StyleAffixOut:
    access.ensure(user, project_id, "write")
    _affix_or_404(project_id, affix_id)
    row = knowledge.update_affix(
        affix_id,
        name=payload.name,
        category=payload.category,
        positive=payload.positive,
        negative=payload.negative,
        is_default=payload.is_default,
        sort_order=payload.sort_order,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="画风词缀不存在")
    return StyleAffixOut(**services.affix_view(row))


@router.delete(
    "/projects/{project_id}/knowledge/affixes/{affix_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
def delete_affix(
    project_id: int,
    affix_id: int,
    user=Depends(require_permission("asset:write")),
) -> None:
    access.ensure(user, project_id, "write")
    _affix_or_404(project_id, affix_id)
    knowledge.delete_affix(affix_id)


@router.get(
    "/projects/{project_id}/knowledge/consistency",
    response_model=list[ConsistencyIssueOut],
)
def list_consistency_issues(
    project_id: int,
    status: str | None = Query(default=None),
    user=Depends(require_permission("project:read")),
) -> list[ConsistencyIssueOut]:
    access.ensure(user, project_id, "read")
    if status is not None and status not in ("open", "adopted", "ignored"):
        raise HTTPException(status_code=422, detail="不支持的审阅状态筛选")
    rows = knowledge.list_issues(project_id, status=status)
    return [ConsistencyIssueOut(**services.issue_view(row)) for row in rows]


@router.post(
    "/projects/{project_id}/knowledge/consistency/scan",
    response_model=ConsistencyScanOut,
)
def scan_consistency(
    project_id: int,
    user=Depends(require_permission("doc:write")),
) -> ConsistencyScanOut:
    access.ensure(user, project_id, "write")
    result = services.scan_consistency(project_id)
    return ConsistencyScanOut(**result)


@router.post(
    "/projects/{project_id}/knowledge/consistency/{issue_id}/resolve",
    response_model=ConsistencyIssueOut,
)
def resolve_consistency_issue(
    project_id: int,
    issue_id: int,
    payload: ConsistencyResolveIn,
    user=Depends(require_permission("doc:write")),
) -> ConsistencyIssueOut:
    access.ensure(user, project_id, "write")
    issue = _issue_or_404(project_id, issue_id)
    result = services.resolve_consistency_issue(
        project_id,
        issue,
        action=payload.action,
        user_id=user["id"],
    )
    return ConsistencyIssueOut(**result)
