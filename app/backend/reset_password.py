"""改某个用户的口令 —— 登录页立起来之后，这是被锁在门外的唯一出路。

用法（在 backend 目录）：
    .venv/bin/python reset_password.py admin

口令一律在终端里问，不接受命令行参数：参数会进 shell 历史，
也会出现在同一台机器的进程列表里。
改完会作废该用户的所有会话，并要求下次登录立刻再改一次。
"""
import getpass
import sys

from repositories.users import users
from security import hash_password

MIN_LEN = 6


def main() -> int:
    if len(sys.argv) != 2:
        print("用法：.venv/bin/python reset_password.py <用户名>", file=sys.stderr)
        return 2

    username = sys.argv[1]
    if users.get_by_username(username) is None:
        print(f"没有这个用户：{username}", file=sys.stderr)
        return 1

    password = getpass.getpass("新口令（至少 6 位）：")
    if len(password) < MIN_LEN:
        print(f"口令至少 {MIN_LEN} 位", file=sys.stderr)
        return 1
    if password != getpass.getpass("再输一次："):
        print("两次输入不一致", file=sys.stderr)
        return 1

    user = users.get_by_username(username)
    users.reset_password(user["id"], hash_password(password))
    print(f"已重置 {username} 的口令：现有会话全部作废，下次登录会被要求再改一次。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
