"""账号层级的唯一出处：2 超级用户 > 1 管理员角色 > 0 其余。

这个规则同时被两处用到——Python 侧判断"能不能管这个账号"，
SQL 侧"挑协作人"时必须先筛掉比自己高的。两头各写一遍迟早分叉，
所以集中在这里：Python 用 user_rank／row_rank，SQL 拼 RANK_SQL。

注意它和权限点是两件事：权限点管"能做什么"，层级管"够得着谁"。
"""
import json

from repositories.users import users

# 给 SQL 用的形态。别名固定为 u（查询里账号表都叫 u）。
RANK_SQL = """(CASE WHEN u.is_superuser = 1 THEN 2
                     WHEN EXISTS (SELECT 1 FROM user_role ur JOIN role r ON r.id = ur.role_id
                                   WHERE ur.user_id = u.id AND r.code = 'admin') THEN 1
                     ELSE 0 END)"""


def user_rank(user) -> int:
    """输入是 app_user 的一行（只需要 id 与 is_superuser）。"""
    if user["is_superuser"]:
        return 2
    return 1 if "admin" in users.roles_for(user["id"]) else 0


def row_rank(row) -> int:
    """输入是带 roles JSON 聚合列的整行（列表场景，避免 N+1）。"""
    if row["is_superuser"]:
        return 2
    return 1 if "admin" in json.loads(row["roles"] or "[]") else 0
