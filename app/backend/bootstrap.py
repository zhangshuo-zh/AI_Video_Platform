"""首次启动的兜底：库里一个用户都没有时，建超级用户 admin。

为什么放在 Python 而不是迁移 SQL 里：口令要现算 scrypt 串，
把固定的哈希写进迁移文件等于固定了盐，而且不可读。

口令来源优先级：FASTVIDEO_ADMIN_PASSWORD 环境变量 > 默认值。
默认值只是为了让第一次能登录，登录后必须改（must_change_password=1）。
"""
import os
import sys

from repositories.users import users as user_repo
from security import hash_password

DEFAULT_USERNAME = os.environ.get("FASTVIDEO_ADMIN_USER", "superadmin")
DEFAULT_DISPLAY = os.environ.get("FASTVIDEO_ADMIN_DISPLAY", "超级管理员")
# 注意：这是首次登录用的初始口令，不是长期口令。
# 生产环境请用 FASTVIDEO_ADMIN_PASSWORD 覆盖，并在登录后立刻修改。
DEFAULT_PASSWORD = os.environ.get("FASTVIDEO_ADMIN_PASSWORD", "808088")


def ensure_admin(*, verbose: bool = True) -> bool:
    """没有用户时建 admin；已经有人就什么都不做。返回是否新建。"""
    if user_repo.count() > 0:
        return False

    admin = user_repo.create(
        username=DEFAULT_USERNAME,
        display_name=DEFAULT_DISPLAY,
        password_hash=hash_password(DEFAULT_PASSWORD),
        is_superuser=True,
        must_change_password=True,
        role_codes=["admin"],       # 同时挂上 admin 角色，角色列表里能看到
    )
    if verbose:
        print(f"已创建超级用户 {admin['username']}（超级用户绕过权限判定，拥有全部权限）",
              file=sys.stderr)
        if DEFAULT_PASSWORD == "808088":
            print("⚠️  正在使用初始口令，登录后请立刻修改；"
                  "生产环境请用 FASTVIDEO_ADMIN_PASSWORD 指定", file=sys.stderr)
    return True
