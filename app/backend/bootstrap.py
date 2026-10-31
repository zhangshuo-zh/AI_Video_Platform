"""首次启动的兜底：库里一个用户都没有时，建超级用户 superadmin。

为什么放在 Python 而不是迁移 SQL 里：口令要现算 scrypt 串，
把固定的哈希写进迁移文件等于固定了盐，而且不可读。

口令来源只有一条：FASTVIDEO_ADMIN_PASSWORD。没给就随机生成一个、
只在启动那一次打到 stderr，然后 must_change_password=1 逼着登录时换掉。

这里原来写着一个硬编码的回退口令——它躺在 git 跟踪的源文件里，
任何拿到仓库的人都能登进"没改默认口令"的部署。规格 01-7.2-7
「口令不得写入提交」直接违反，W00 盘点把它列成冲突 C7。
随机生成同样能让人第一次登进去，而且没有"忘了改就永久敞开"这条路径。
"""
import os
import secrets
import sys

from repositories.users import users as user_repo
from security import hash_password

DEFAULT_USERNAME = os.environ.get("FASTVIDEO_ADMIN_USER", "superadmin")
DEFAULT_DISPLAY = os.environ.get("FASTVIDEO_ADMIN_DISPLAY", "超级管理员")


def _initial_password() -> tuple[str, bool]:
    """返回（口令，是不是环境变量给的）。随机串避开易混字符，够长即可——它只活一次登录。"""
    given = os.environ.get("FASTVIDEO_ADMIN_PASSWORD", "").strip()
    if given:
        return given, True
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16)), False


def ensure_admin(*, verbose: bool = True) -> bool:
    """没有用户时建超级用户；已经有人就什么都不做。返回是否新建。"""
    if user_repo.count() > 0:
        return False

    password, from_env = _initial_password()
    admin = user_repo.create(
        username=DEFAULT_USERNAME,
        display_name=DEFAULT_DISPLAY,
        password_hash=hash_password(password),
        is_superuser=True,
        must_change_password=True,
        role_codes=["admin"],       # 同时挂上 admin 角色，角色列表里能看到
    )
    if verbose:
        print(f"已创建超级用户 {admin['username']}（超级用户绕过权限判定，拥有全部权限）",
              file=sys.stderr)
        if from_env:
            print("初始口令来自 FASTVIDEO_ADMIN_PASSWORD，登录后会要求改密", file=sys.stderr)
        else:
            print(f"未设 FASTVIDEO_ADMIN_PASSWORD，本次随机生成的初始口令是：{password}\n"
                  "它只会打印这一次；登录后必须改密。不想在终端里看到口令就设环境变量。",
                  file=sys.stderr)
    return True
