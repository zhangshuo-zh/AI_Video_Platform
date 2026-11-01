"""项目接口。

删除是需要确认的：单个项目走 `DELETE ?confirm=<名字>`，名字对不上就 400；
一次删多个没法逐个输名字，改成 `POST /projects/batch-delete` 要求填固定确认词。
前端的弹窗只是把这件事做得显眼，真正拦住误删的是服务端这一道。

批量接口一律"逐项过权限、失败的报原因不整批回滚"：勾十个项目里有一个是
别人的，不该让那九个也做不完。
"""
import json

from fastapi import APIRouter, Depends, HTTPException, status as http_status

from access import ensure, is_manager
from deps import require_permission
from models import (ProjectBatchDeleteIn, ProjectBatchIn, ProjectBatchOut, ProjectIn,
                    ProjectMemberIn, ProjectMemberOut, ProjectMembersOut, ProjectOut,
                    ProjectSkip, ProjectStatusIn, ProjectSummary, ProjectTagsIn,
                    ProjectUpdateIn)
from ranks import row_rank, user_rank
from repositories.projects import projects as project_repo
from repositories.users import users as user_repo
from services import MAX_TAGS, delete_project_cascade
from services import clean_tags as _clean_tags

router = APIRouter(prefix="/api", tags=["projects"])

# 标签的归一化规则与上限挪到 services 了：资产标签从 v0.60 起共用同一条，
# 两边各写一份迟早出现"项目最多 10 个、资产最多 8 个"这种没人能解释的差别。

# 批量删除的确认词。写成一行的理由：前端弹窗里要原样显示它，两处必须同一个值
BATCH_DELETE_CONFIRM = "批量删除"

# 批量动作 → 要写进 status 的值
_BATCH_STATUS = {"archive": "archived", "restore": "active"}


def _clean_members(raw: list[int], actor, owner_id: int) -> list[int]:
    """建项目时勾选的协作人：去重、去掉所有人自己，并逐个校验。

    校验放在这里而不是只靠界面：候选名单是按层级过滤的，但接口不能假设
    调用方走的是那个名单——不校验就能把比自己高的账号拉进项目。
    """
    picked: list[int] = []
    for uid in raw:
        if uid == owner_id or uid in picked:
            continue
        row = user_repo.user_row(uid)
        if row is None or not row["is_active"]:
            raise HTTPException(status_code=422, detail=f"协作人 #{uid} 不存在或已停用")
        if row_rank(row) > user_rank(actor):
            raise HTTPException(status_code=422, detail=f"协作人 #{uid} 的层级高于你，不能邀请")
        picked.append(uid)
    return picked


@router.get("/member-candidates", response_model=list[ProjectMemberOut])
def member_candidates(user=Depends(require_permission("project:write"))) -> list[ProjectMemberOut]:
    """建项目时弹窗里挑人用：还没项目，所以只按启用状态与层级过滤。"""
    return [ProjectMemberOut(user_id=r["user_id"], username=r["username"],
                             display_name=r["display_name"], created_at="")
            for r in project_repo.candidate_users(user_rank(user), user["id"])]


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(status: str = "all",
                  user=Depends(require_permission("project:read"))) -> list[ProjectOut]:
    """默认给全部状态（含已归档），排序按最后打开。

    为什么默认 all 而不是只看在产：项目中心那页要能在"全部／在产／已归档"
    之间切，一次捞回来前端筛；而顶栏的项目下拉只认在产，走 store.activeProjects。
    """
    rows = project_repo.list_visible(user["id"], is_manager(user), status)
    return [ProjectOut.from_row(row) for row in rows]


@router.post("/projects/{project_id}/open", status_code=http_status.HTTP_204_NO_CONTENT)
def mark_project_opened(project_id: int,
                        user=Depends(require_permission("project:read"))) -> None:
    """记一次"我打开了这个项目"，供列表按最近使用排序。

    只读权限就够：打开不是修改。它也不推 updated_at——那个字段要留给内容改动。
    """
    ensure(user, project_id, "read")
    project_repo.mark_opened(project_id)


@router.patch("/projects/{project_id}/status", response_model=ProjectOut)
def set_project_status(project_id: int, payload: ProjectStatusIn,
                       user=Depends(require_permission("project:write"))) -> ProjectOut:
    """归档或恢复。值由 ProjectStatusIn 的 Literal 把关，写别的直接 422。"""
    ensure(user, project_id, "write")
    row = project_repo.set_status(project_id, payload.status)
    if row is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ProjectOut.from_row(row)


@router.post("/projects/batch", response_model=ProjectBatchOut)
def batch_projects(payload: ProjectBatchIn,
                   user=Depends(require_permission("project:write"))) -> ProjectBatchOut:
    """批量归档／恢复／加标签。逐项过权限，被拒的进 skipped 并报原因。"""
    done: list[int] = []
    skipped: list[ProjectSkip] = []
    add_tags = _clean_tags(payload.tags) if payload.action == "tag" else []
    if payload.action == "tag" and not add_tags:
        raise HTTPException(status_code=422, detail="没有可添加的标签")

    for project_id in payload.ids:
        try:
            # 看不见的项目在这里就是 404，不因为"是批量"而降级成 403
            ensure(user, project_id, "write")
        except HTTPException as err:
            skipped.append(ProjectSkip(id=project_id, reason=str(err.detail)))
            continue
        if payload.action == "tag":
            project_repo.add_tags(project_id, add_tags, MAX_TAGS)
        else:
            project_repo.set_status(project_id, _BATCH_STATUS[payload.action])
        done.append(project_id)
    return ProjectBatchOut(done=done, skipped=skipped)


@router.post("/projects/batch-delete", response_model=ProjectBatchOut)
def batch_delete_projects(payload: ProjectBatchDeleteIn,
                          user=Depends(require_permission("project:delete"))) -> ProjectBatchOut:
    """批量删除。确认词不对就整批不动——这一步在循环之前，不存在删了一半。"""
    if payload.confirm.strip() != BATCH_DELETE_CONFIRM:
        raise HTTPException(
            status_code=400,
            detail=f"确认词不对，未删除。要一次删多个项目，请输入「{BATCH_DELETE_CONFIRM}」")

    done: list[int] = []
    skipped: list[ProjectSkip] = []
    for project_id in payload.ids:
        try:
            ensure(user, project_id, "delete")
        except HTTPException as err:
            skipped.append(ProjectSkip(id=project_id, reason=str(err.detail)))
            continue
        delete_project_cascade(project_id)
        done.append(project_id)
    return ProjectBatchOut(done=done, skipped=skipped)


@router.post("/projects", response_model=ProjectOut,
             status_code=http_status.HTTP_201_CREATED)
def create_project(payload: ProjectIn,
                   user=Depends(require_permission("project:write"))) -> ProjectOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="项目名不能为空")
    # 建的人就是所有人，这一步定死归属，之后只有他能删、能管协作人
    members = _clean_members(payload.member_ids, user, user["id"])
    return ProjectOut.from_row(
        project_repo.create(name, payload.description.strip(),
                            _clean_tags(payload.tags), user["id"], members))


@router.get("/tags", response_model=list[dict])
def list_tags(user=Depends(require_permission("project:read"))) -> list[dict]:
    rows = project_repo.all_tags(user["id"], is_manager(user))
    return [{"tag": row["tag"], "count": row["n"]} for row in rows]


@router.put("/projects/{project_id}/tags", response_model=ProjectOut)
def set_project_tags(project_id: int, payload: ProjectTagsIn,
                     user=Depends(require_permission("project:write"))) -> ProjectOut:
    ensure(user, project_id, "write")
    row = project_repo.set_tags(project_id, _clean_tags(payload.tags))
    if row is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ProjectOut.from_row(row)


@router.put("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: int, payload: ProjectUpdateIn,
                   user=Depends(require_permission("project:write"))) -> ProjectOut:
    ensure(user, project_id, "write")
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="项目名不能为空")
    row = project_repo.update(project_id, name, payload.description.strip(),
                              _clean_tags(payload.tags))
    if row is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ProjectOut.from_row(row)


@router.get("/projects/{project_id}/summary", response_model=ProjectSummary)
def project_summary(project_id: int,
                    user=Depends(require_permission("project:read"))) -> ProjectSummary:
    ensure(user, project_id, "read")
    row = project_repo.summary(project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    images = project_repo.asset_images(project_id)
    return ProjectSummary(name=row["name"], episodes=row["episodes"],
                          shots=row["shots"], assets=row["assets"],
                          image_files=len(images))


@router.delete("/projects/{project_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_project(project_id: int, confirm: str = "",
                   user=Depends(require_permission("project:delete"))) -> None:
    row = ensure(user, project_id, "delete")
    summary = project_repo.summary(project_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="项目不存在")

    # 必须原样输入项目名。少一个字都不行，防止手滑点掉
    if confirm.strip() != row["name"]:
        raise HTTPException(status_code=400, detail="项目名不匹配，未删除")

    delete_project_cascade(project_id)


# ---------- 协作人 ----------
# 只有所有人和管理员能看这份名单、能加能移；协作人只能看项目，不在这里。
# 三个端点都声明 project:write——能看名单的一定是能管的，声明的权限要和
# 实际要求一致，否则读代码的人会以为有个 project:read 就能看名单。

def _members_payload(project_id: int, user) -> ProjectMembersOut:
    """组返回体。不含权限判断：调用方先 ensure，这里只负责把数据摆齐。"""
    project = project_repo.get(project_id)
    members = [ProjectMemberOut(user_id=r["user_id"], username=r["username"],
                                display_name=r["display_name"], created_at=r["created_at"])
               for r in project_repo.members(project_id)]
    candidates = [ProjectMemberOut(user_id=r["user_id"], username=r["username"],
                                   display_name=r["display_name"], created_at="")
                  for r in project_repo.member_candidates(project_id, user_rank(user))]
    return ProjectMembersOut(owner_id=project["owner_id"], members=members,
                             candidates=candidates)


@router.get("/projects/{project_id}/members", response_model=ProjectMembersOut)
def list_members(project_id: int,
                 user=Depends(require_permission("project:write"))) -> ProjectMembersOut:
    ensure(user, project_id, "manage")
    return _members_payload(project_id, user)


@router.post("/projects/{project_id}/members", response_model=ProjectMembersOut)
def add_member(project_id: int, payload: ProjectMemberIn,
               user=Depends(require_permission("project:write"))) -> ProjectMembersOut:
    project = ensure(user, project_id, "manage")
    target = user_repo.get_by_id(payload.user_id)
    if target is None or not target["is_active"]:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target["id"] == project["owner_id"]:
        raise HTTPException(status_code=422, detail="所有人本来就有全部权限")
    project_repo.add_member(project_id, payload.user_id, user["id"])
    return _members_payload(project_id, user)


@router.delete("/projects/{project_id}/members/{member_id}",
               response_model=ProjectMembersOut)
def remove_member(project_id: int, member_id: int,
                  user=Depends(require_permission("project:write"))) -> ProjectMembersOut:
    ensure(user, project_id, "manage")
    project_repo.remove_member(project_id, member_id)
    return _members_payload(project_id, user)
