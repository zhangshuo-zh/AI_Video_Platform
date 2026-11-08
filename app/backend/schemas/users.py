import json

from pydantic import BaseModel, Field


# ---------- 用户与角色（系统设置页） ----------

class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    is_superuser: bool
    is_active: bool
    must_change_password: bool
    created_at: str | None = None
    last_login_at: str | None = None
    roles: list[str] = []

    @classmethod
    def from_row(cls, row) -> "UserOut":
        return cls(id=row["id"], username=row["username"],
                   display_name=row["display_name"] or row["username"],
                   is_superuser=bool(row["is_superuser"]),
                   is_active=bool(row["is_active"]),
                   must_change_password=bool(row["must_change_password"]),
                   created_at=row["created_at"], last_login_at=row["last_login_at"],
                   roles=json.loads(row["roles"] or "[]"))




class UserCreateIn(BaseModel):
    # 用户名进 URL 与日志，限成 ASCII，省掉一半的转义问题
    username: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(default="", max_length=40)
    password: str = Field(min_length=6, max_length=64)
    roles: list[str] = []




class UserUpdateIn(BaseModel):
    """只更新传了的那几项。没传 = 不动，和项目那个"整张表单"的取舍不同：
    这里停用和改名是两个不同的动作，混在一次提交里更容易误触。
    角色不在这个包里——改角色是另一个权限点（user:assign），走单独的端点。
    """
    display_name: str | None = Field(default=None, max_length=40)
    is_active: bool | None = None




class PasswordResetIn(BaseModel):
    new_password: str = Field(min_length=6, max_length=64)




class UserRolesIn(BaseModel):
    roles: list[str] = []




class RoleOut(BaseModel):
    id: int
    code: str
    name: str
    description: str = ""
    permissions: list[str] = []




class PermissionOut(BaseModel):
    code: str
    name: str
    group_name: str




class RolePermIn(BaseModel):
    permissions: list[str] = []
