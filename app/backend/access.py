"""项目级访问控制：全局权限点 × 项目归属。

权限点回答"这个人能不能做这类事"，这里回答"这条项目归不归他"。
两者都满足才放行——所以端点上是两层：`require_permission("...")` 管前者，
本模块的 `ensure()` 管后者。层级规则本身在 `ranks.py`。

看不见的项目一律回 404，不泄露它存在；看得见但级别不够的事（协作人想删项目）
才回 403。
"""
import sqlite3

from fastapi import HTTPException

from db import get_conn
from ranks import user_rank

# 间接挂靠的实体要先绕回项目：分镜经分集，资产与文档直接挂项目
_PROJECT_OF = {
    "episode": "SELECT project_id FROM episode WHERE id = ?",
    "shot": """SELECT e.project_id FROM shot s
                 JOIN episode e ON e.id = s.episode_id WHERE s.id = ?""",
    "asset": "SELECT project_id FROM asset WHERE id = ?",
    # 文档一定挂项目（不像资产有全局池），所以它能走 ensure_by，不需要 ensure_asset 那种特判
    "document": "SELECT project_id FROM document WHERE id = ?",
    # 作业一定挂项目；删项目时 Job 行随外键 CASCADE 走
    "job": "SELECT project_id FROM job WHERE id = ?",
}


def is_manager(user) -> bool:
    """管理员及以上对全部项目放行——删号时"由管理员转移项目"全靠它。"""
    return user_rank(user) >= 1


def visible_project_ids(user) -> set[int] | None:
    """这个人看得见的项目 id 集合；管理员返回 `None` 表示"全部"。

    **一次查询取代逐行判权限**（2026-11-09 外部审查第 10 条）。
    `worker_status()` 里有 `for row in health["running_jobs"]: ensure(...)`，
    而 `ensure` 至少回库一次（管理员短路前也要先读 project 行），
    于是这个"顶栏小圆点"接口的成本随正在跑的作业数线性涨——实测 16 条
    running 时每次 10.67 ms，其中 8.43 ms 是这串逐行查询白花的。

    判据与 `ensure(need="read")` 完全一致，改成一次读出来：
    管理员全放行；否则"自己拥有的" ∪ "作为成员加入的"。
    无主作业（`project_id IS NULL`）不在这里判——调用方按 `ensure` 的同一套
    纪律自己处理（只有管理员看得见）。
    """
    if is_manager(user):
        return None
    with get_conn() as conn:
        owned = {r["id"] for r in conn.execute(
            "SELECT id FROM project WHERE owner_id = ?", (user["id"],))}
        member = {r["project_id"] for r in conn.execute(
            "SELECT project_id FROM project_member WHERE user_id = ?", (user["id"],))}
    return owned | member


def project_of(kind: str, target_id: int) -> int | None:
    with get_conn() as conn:
        row = conn.execute(_PROJECT_OF[kind], (target_id,)).fetchone()
    return row["project_id"] if row else None


# "这件事只有挂名的人能做"——三种情况分开写原因，别共用一句含糊的话。
# own 是 v0.84 新加的：归档／恢复会改变别人眼前看到的是哪部剧，
# 原来跟着 project:write 走，协作人能把所有人的项目收起来。
_OWNER_ONLY = {
    "delete": "只有项目所有人能删除项目",
    "manage": "只有项目所有人能管理协作人",
    "own": "只有项目所有人能归档或恢复这个项目",
}


# 认得的级别就这几个。写这个集合是为了下面那句 fail-closed：
# 以前 need 拼错时，成员会一路走到函数末尾被**放行**——
# 权限判定里最坏的一种失败方式，错一个字母就少一道门
NEEDS = ("read", "write", "own", "delete", "manage")

# 协作人（项目成员）能做到哪一档：**白名单**，不是"除了 own/delete/manage 都行"。
# 2026-11-04 修一件事：上一轮把"未知级别不默认放行"改成 `need != "read"` 就拒，
# 顺手把 write 一起关在门外——协作人从此建不了集、登不了资产、改不动项目名，
# 而 §1.3、§4 与 `projects.js` 的注释都还写着"成员能读能改"。三处口径对不上，
# 以这一行为准：**读与写归成员，own／delete／manage 归所有人**。
# 将来新加一档，既要登记进 NEEDS，也要在这里或 _OWNER_ONLY 里表态；
# 两处都没表态的一律按拒处理（宁可少给一个动作，不少一道门）。
_MEMBER_OK = ("read", "write")


def ensure(user, project_id: int, need: str = "read"):
    """need：read 看 / write 改 / own 归档与恢复 / delete 删项目 / manage 管协作人。

    返回项目行（含 owner_id），调用方常要继续用。
    未知级别一律按最严的处理（拒绝），不默认放行。
    """
    if need not in NEEDS:
        raise HTTPException(status_code=500, detail="内部：未知的权限级别")
    if project_id is None:
        # 0039：删项目不再带走作业行，`job.project_id` 会被外键置成 NULL。
        # 这类"无主作业"只有管理员能读——别人一律 404，与「看不见就是不存在」
        # 同一条纪律：不因为行还在就告诉调用方"这行存在，只是你不配看"。
        if not is_manager(user):
            raise HTTPException(status_code=404, detail="项目不存在")
        return None
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
    if need not in _MEMBER_OK:
        # 成员能读能改；own／delete／manage 三档都归所有人。
        # 归档会改变别人眼前看到的是哪部剧，删与管人更是——所以这三档不放行，
        # 而拦下来时用的是 _OWNER_ONLY 里那三句各说各的话，不是含糊的一句"没权限"
        raise HTTPException(status_code=403,
                            detail=_OWNER_ONLY.get(need, "只有项目所有人能做这件事"))
    return row


def ensure_by(kind: str, target_id: int, user, need: str = "read", label: str = "对象"):
    """给用 episode／shot 的 id 定位的端点用：先绕回项目再判。

    资产不走这里——全局资产的 project_id 是 NULL，绕不回任何项目，
    用这个函数会把"改一条全局资产"错判成 404。资产用 `ensure_asset`。
    """
    project_id = project_of(kind, target_id)
    if project_id is None:
        raise HTTPException(status_code=404, detail=f"{label}不存在")
    return ensure(user, project_id, need)


def ensure_asset(asset_id: int, user, need: str = "read") -> sqlite3.Row:
    """资产这一层单独判，因为资产现在可以不属于任何项目（全局资产池）。

    规则照开发指令 §5.3：全局资产人人可读，**能看绝不等于能改**——
    写与删只放管理员；项目资产仍走"权限点 × 项目归属"那一套。
    全局资产回 403 而不是 404 是有意的：它本来就对所有人可见，
    藏它反而会让界面上"看得见却报不存在"，说不清。
    """
    with get_conn() as conn:
        row = conn.execute("SELECT id, project_id, name FROM asset WHERE id = ?",
                           (asset_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="资产不存在")
    if row["project_id"] is None:
        if need == "read":
            return row
        if not is_manager(user):
            raise HTTPException(status_code=403, detail="全局资产只有管理员能修改或删除")
        return row
    ensure(user, row["project_id"], need)
    return row
