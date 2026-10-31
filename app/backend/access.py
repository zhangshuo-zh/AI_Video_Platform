"""项目级访问控制：全局权限点 × 项目归属。

权限点回答"这个人能不能做这类事"，这里回答"这条项目归不归他"。
两者都满足才放行——所以端点上是两层：`require_permission("...")` 管前者，
本模块的 `ensure()` 管后者。层级规则本身在 `ranks.py`。

看不见的项目一律回 404，不泄露它存在；看得见但级别不够的事（协作人想删项目）
才回 403。
"""
from fastapi import HTTPException

from db import get_conn
from ranks import user_rank

# 间接挂靠的实体要先绕回项目：分镜经分集，资产直接挂项目
_PROJECT_OF = {
    "episode": "SELECT project_id FROM episode WHERE id = ?",
    "shot": """SELECT e.project_id FROM shot s
                 JOIN episode e ON e.id = s.episode_id WHERE s.id = ?""",
    "asset": "SELECT project_id FROM asset WHERE id = ?",
}


def is_manager(user) -> bool:
    """管理员及以上对全部项目放行——删号时"由管理员转移项目"全靠它。"""
    return user_rank(user) >= 1


def project_of(kind: str, target_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute(_PROJECT_OF[kind], (target_id,)).fetchone()
    return row["project_id"] if row else None


def ensure(user, project_id: int, need: str = "read"):
    """need：read 看 / write 改 / delete 删项目 / manage 管协作人。

    返回项目行（含 owner_id），调用方常要继续用。
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="项目不存在")
        if is_manager(user) or row["owner_id"] == user["id"]:
            return row
        member = conn.execute(
            "SELECT 1 FROM project_member WHERE project_id = ? AND user_id = ?",
            (project_id, user["id"]),
        ).fetchone()

    if member is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if need in ("delete", "manage"):
        raise HTTPException(status_code=403, detail="只有项目所有人能删除项目或管理协作人")
    return row


def ensure_by(kind: str, target_id: int, user, need: str = "read", label: str = "对象"):
    """给用 episode／shot／asset 的 id 定位的端点用：先绕回项目再判。"""
    project_id = project_of(kind, target_id)
    if project_id is None:
        raise HTTPException(status_code=404, detail=f"{label}不存在")
    return ensure(user, project_id, need)
