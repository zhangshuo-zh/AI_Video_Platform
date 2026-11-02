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
from deps import actor_name, require_permission
from models import (ProjectBatchDeleteIn, ProjectBatchIn, ProjectBatchOut, ProjectIn,
                    ProjectMemberIn, ProjectMemberOut, ProjectMembersOut, ProjectNote,
                    ProjectOut, ProjectSkip, ProjectStatusIn, ProjectSummary,
                    ProjectTagsIn, ProjectTransferIn,
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
    """归档或恢复。值由 ProjectStatusIn 的 Literal 把关，写别的直接 422。

    v0.84 起这一件事要 own（所有人或管理员），不再跟着 write 走：
    归档会改变别人眼前看到的是哪部剧，协作人不该能把名下的项目收起来。
    """
    ensure(user, project_id, "own")
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
    notes: list[ProjectNote] = []
    add_tags = _clean_tags(payload.tags) if payload.action == "tag" else []
    if payload.action == "tag" and not add_tags:
        raise HTTPException(status_code=422, detail="没有可添加的标签")
    # 归档／恢复是"own"级（只有所有人或管理员），加标签仍是"write"级：
    # 给项目打标签是日常协作，把一部剧从别人眼前收走不是
    need = "write" if payload.action == "tag" else "own"

    for project_id in payload.ids:
        try:
            # 看不见的项目在这里就是 404，不因为"是批量"而降级成 403
            ensure(user, project_id, need)
        except HTTPException as err:
            skipped.append(ProjectSkip(id=project_id, reason=str(err.detail)))
            continue
        if payload.action == "tag":
            res = project_repo.add_tags(project_id, add_tags, MAX_TAGS)
            if res and res["over_cap"]:
                # 一个都没加上和加上一半，都要说出来——只回 done 等于界面在报喜
                names = "、".join(res["over_cap"])
                notes.append(ProjectNote(
                    id=project_id,
                    note=(f"标签已满 {MAX_TAGS} 个，未追加：{names}" if not res["added"]
                          else f"标签装不下，只加上 {'、'.join(res['added'])}；未追加：{names}")))
        else:
            project_repo.set_status(project_id, _BATCH_STATUS[payload.action])
        done.append(project_id)
    return ProjectBatchOut(done=done, skipped=skipped, notes=notes)


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
        delete_project_cascade(project_id, actor=actor_name(user))
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
                          image_files=len(images), documents=row["documents"])


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

    delete_project_cascade(project_id, actor=actor_name(user))


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


@router.post("/projects/{project_id}/transfer", response_model=ProjectMembersOut)
def transfer_ownership(project_id: int, payload: ProjectTransferIn,
                       user=Depends(require_permission("project:write"))) -> ProjectMembersOut:
    """把项目交给别人。四条口径，都是"交出去之后还会发生什么"决定的：

    1. 谁能发起：`ensure(..., "manage")` —— 只有所有人与管理员，和删项目、管成员同一把锁。
    2. 交给谁（v0.81 改口）：**任何你够得着的启用账号**，不再要求先加为协作人。
       层级这一条仍然卡着——`user_rank(接手人) <= user_rank(发起人)`，与"建项目时不能邀请
       比自己高的人"是同一条规则：制作把项目交给超级管理员之后自己降成协作人，
       就再也拿不回来了，这种单向门不能开。
       为什么去掉"先加协作人"：交接这一步本身就把对方变成所有人，多那道门槛只会
       让「成员」弹窗在没有协作人时显示不出一行可点的按钮（就是这么被当成"没有这个功能"）。
    3. 原所有人：**自动降为协作人**，不是消失。交完就看不见这个项目的话，
       没人敢点这个按钮，历史进度也等于自己给自己锁在门外。
       反过来，接手人原本在协作人名单里，交完就把他那行删掉——
       不然名单上会出现"既是所有人又是协作人"，移除按钮还对他开放。
    4. **不推 `updated_at`**：与 `set_status` 同一个理由——换个人挂名没动任何内容，
       推了"最后修改"就不再表示"这部剧在推进"。两步写还要求原子，
       见 `repositories/projects.py:transfer_owner`。
    """
    project = ensure(user, project_id, "manage")
    old_owner = project["owner_id"]
    target_id = payload.user_id
    if target_id == old_owner:
        raise HTTPException(status_code=422, detail="对方已经是这个项目的所有人")
    target = user_repo.get_by_id(target_id)
    if target is None or not target["is_active"]:
        raise HTTPException(status_code=404, detail="接手人不存在或已停用")
    if user_rank(target) > user_rank(user):
        raise HTTPException(status_code=422, detail="对方层级高于你，不能把项目交给他")

    project_repo.transfer_owner(project_id, target_id, old_owner)
    return _members_payload(project_id, user)
