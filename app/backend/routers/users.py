"""用户与角色管理。

守卫分三个粒度：看（user:read）、改人（user:write）、给角色（user:assign）。
两条自我保护写在这一层而不是界面上：不能停用自己、不能改自己的角色——
这个工具只有一个超级用户兜底，一次误操作就能把人锁在门外。
第三条更狠：内置的 admin 角色不许改权限，它是急救通道。
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status as http_status

from db import transaction
from deps import actor_name, require_permission
from models import (PasswordResetIn, PermissionOut, RoleOut, RolePermIn,
                    UserCreateIn, UserDeleteIn, UserOut, UserRolesIn, UserUpdateIn)
from ranks import row_rank, user_rank
from repositories.projects import projects as project_repo
from repositories.users import users
from services import delete_project_cascade
from security import hash_password

router = APIRouter(prefix="/api", tags=["users"])

# admin 角色是急救通道：超级用户靠 is_superuser 绕过判定，
# 而这个角色是留给"人"的最后一把钥匙，改坏了没有第二个用户能救
PROTECTED_ROLE = "admin"


def _need(request: Request, code: str) -> None:
    """在一个已过守卫的端点里追加要求另一个权限点。

    不写成第二个 Depends，是因为要不要它取决于请求内容
    （建用户时不带角色就不需要 user:assign）。
    """
    if code not in getattr(request.state, "permissions", set()):
        raise HTTPException(status_code=403, detail=f"缺少权限：{code}")


def _guard_target(user_id: int, action: str, actor=None):
    """取目标用户，顺带挡住不该动的账号。

    两种拦截：超级用户账号一律只读（停用它等于全员锁死）；
    比调用者层级高的账号也不许动——看不见的东西更不该能改。
    要动超管只能走 reset_password.py 那条命令行出路。
    """
    row = users.user_row(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if row["is_superuser"]:
        raise HTTPException(status_code=422, detail=f"超级用户账号不可{action}")
    if actor is not None and row_rank(row) > user_rank(actor):
        raise HTTPException(status_code=404, detail="用户不存在")
    return row


def _role_out(row) -> RoleOut:
    return RoleOut(id=row["id"], code=row["code"], name=row["name"],
                   description=row["description"] or "",
                   permissions=json.loads(row["permissions"] or "[]"))


def _user_out(user_id: int) -> UserOut:
    return UserOut.from_row(users.user_row(user_id))


@router.get("/users", response_model=list[UserOut])
def list_users(user=Depends(require_permission("user:read"))) -> list[UserOut]:
    # 过滤在服务端做：界面上藏起来而接口照样返回，等于没藏
    my_rank = user_rank(user)
    return [UserOut.from_row(row) for row in users.list_all()
            if row_rank(row) <= my_rank]


@router.post("/users", response_model=UserOut,
             status_code=http_status.HTTP_201_CREATED)
def create_user(payload: UserCreateIn, request: Request,
                _=Depends(require_permission("user:write"))) -> UserOut:
    if users.get_by_username(payload.username) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    if payload.roles:
        _need(request, "user:assign")

    row = users.create(
        username=payload.username,
        display_name=payload.display_name.strip() or payload.username,
        password_hash=hash_password(payload.password),
        # 管理员给的口令是临时的，第一次登录必须自己改掉
        must_change_password=True,
        role_codes=payload.roles,
    )
    return UserOut.from_row(users.user_row(row["id"]))


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdateIn,
                user=Depends(require_permission("user:write"))) -> UserOut:
    _guard_target(user_id, "修改", user)
    if payload.is_active is False and user_id == user["id"]:
        raise HTTPException(status_code=422, detail="不能停用自己")

    if payload.display_name is not None:
        users.set_display_name(user_id, payload.display_name.strip() or "")
    if payload.is_active is not None:
        users.set_active(user_id, payload.is_active)
    return _user_out(user_id)


@router.put("/users/{user_id}/roles", response_model=UserOut)
def set_user_roles(user_id: int, payload: UserRolesIn,
                   user=Depends(require_permission("user:assign"))) -> UserOut:
    _guard_target(user_id, "改角色", user)
    if user_id == user["id"]:
        # 自己给自己降权就是当场把自己锁在外面，除非还留着别的管理员
        raise HTTPException(status_code=422, detail="不能修改自己的角色")

    users.set_roles(user_id, payload.roles)
    return _user_out(user_id)


@router.post("/users/{user_id}/password", response_model=UserOut)
def reset_password(user_id: int, payload: PasswordResetIn,
                   user=Depends(require_permission("user:write"))) -> UserOut:
    _guard_target(user_id, "重置口令", user)
    users.reset_password(user_id, hash_password(payload.new_password))
    return _user_out(user_id)


@router.delete("/users/{user_id}", status_code=http_status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, payload: UserDeleteIn,
                user=Depends(require_permission("user:write"))) -> None:
    """删账号。名下项目必须二选一处理掉；他作为协作人的关系由外键随之清掉。

    不允许"只删人、项目悬空"：库里的 owner_id 是 RESTRICT，那条路本来就走不通，
    这里提前给人一句能看懂的话。
    """
    _guard_target(user_id, "删除", user)
    if user_id == user["id"]:
        raise HTTPException(status_code=422, detail="不能删除自己")
    if payload.transfer_to is not None and payload.delete_projects:
        raise HTTPException(status_code=422, detail="转移与连带删除只能选一样")

    owned = project_repo.owned_by(user_id)
    if owned and payload.transfer_to is None and not payload.delete_projects:
        names = "、".join(row["name"] for row in owned)
        raise HTTPException(
            status_code=422,
            detail=f"该账号名下还有 {len(owned)} 个项目（{names}）；"
                   f"先指定转移给谁，或勾选连同项目一起删")

    if payload.transfer_to is not None:
        target = users.get_by_id(payload.transfer_to)
        if target is None or not target["is_active"]:
            raise HTTPException(status_code=404, detail="接手人不存在")
        if payload.transfer_to == user_id:
            raise HTTPException(status_code=422, detail="不能转移给即将删除的账号")
        # 转移 N 个项目 ＋ 删账号 ＝ 一个事务。原来它们是十几个独立事务，
        # 第 3 个失败时前 2 个已经改了东家、账号还挂着——库里就是半套状态，
        # 而且没人会立刻发现（删账号这件事本来就一年做不了几次）。
        # 这一条包得起事务：全程只有库内写，不碰 Git 与磁盘。
        # 失败要自己转成 500：让异常裸出去的话，界面只剩一句"Internal Server Error"，
        # 而这里最该说的是"什么都没动、账号还在"——人据此才知道可以直接重来。
        try:
            with transaction():
                for row in owned:
                    project_repo.set_owner(row["id"], payload.transfer_to)
                users.delete(user_id)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"转移与删账号已整体回滚：{len(owned)} 个项目的东家都没变、"
                       f"账号也还在，修好原因后可以直接重来。卡住的那一步说：{exc}") from exc
    elif owned:
        # 连带删项目**不能**包进事务：那一步要动工作区文件与 Git 提交，
        # 回滚不会把它们带回去，"看起来原子"比不原子更危险。
        # 所以这里做的是另一件事——把已经做到的部分如实报出来，
        # 而不是让人对着一个 500 猜删到第几个了。
        gone = []
        for row in owned:
            try:
                delete_project_cascade(row["id"], actor=actor_name(user))
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"名下 {len(owned)} 个项目已删掉 {len(gone)} 个，"
                           f"卡在《{row['name']}》：{exc}。账号还在，"
                           f"剩下的项目修好后可以再来一次；"
                           f"已经删掉的那些不会回来。") from exc
            gone.append(row["name"])
        users.delete(user_id)
    else:
        # 名下没有项目：没什么要"二选一"的，直接删账号。
        # 这一支原来是缺的——`elif owned:` 为假时函数一路走到结尾什么都没做，
        # 却照样回 204：界面说"已删除"，库里账号还在（阶段 B 验收时撞到的）。
        users.delete(user_id)


@router.get("/roles", response_model=list[RoleOut])
def list_roles(_=Depends(require_permission("user:read"))) -> list[RoleOut]:
    return [_role_out(row) for row in users.all_roles()]


@router.get("/permissions", response_model=list[PermissionOut])
def list_permissions(_=Depends(require_permission("user:read"))) -> list[PermissionOut]:
    return [PermissionOut(code=row["code"], name=row["name"],
                          group_name=row["group_name"])
            for row in users.all_permissions()]


@router.put("/roles/{role_id}/permissions", response_model=RoleOut)
def set_role_permissions(role_id: int, payload: RolePermIn,
                         _=Depends(require_permission("user:write"))) -> RoleOut:
    target = next((row for row in users.all_roles() if row["id"] == role_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    if target["code"] == PROTECTED_ROLE:
        raise HTTPException(status_code=422, detail="内置的 admin 角色不许改权限")

    users.set_role_permissions(role_id, payload.permissions)
    return _role_out(next(r for r in users.all_roles() if r["id"] == role_id))
