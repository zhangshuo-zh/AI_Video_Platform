from pydantic import BaseModel


# ---------- 项目协作人 ----------

class ProjectMemberOut(BaseModel):
    user_id: int
    username: str
    display_name: str
    created_at: str




class ProjectMemberIn(BaseModel):
    user_id: int




class ProjectTransferIn(BaseModel):
    """转移所有权。只收一个人 id——交出去之后自己降为协作人，没有第二种选项。"""
    user_id: int




class ProjectMembersOut(BaseModel):
    """一页给齐：已受邀的人和可供挑选的人。

    候选也在这里出，是因为挑选名单要按"当前调用者能看见谁"过滤——
    让前端去调 /api/users 就等于绕开了这层过滤，而且只有 user:read
    的人才调得动，项目所有人（可能只有一个制作角色）反而调不动。
    """
    owner_id: int
    members: list[ProjectMemberOut] = []
    candidates: list[ProjectMemberOut] = []




class UserDeleteIn(BaseModel):
    """删用户必须表态：名下项目是转移还是跟着删。

    两个都不给就拒——静默把项目带进坟墓是这类操作最危险的方式。
    """
    transfer_to: int | None = None
    delete_projects: bool = False
