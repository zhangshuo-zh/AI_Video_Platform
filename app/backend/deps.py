"""登录态相关的依赖。其他路由要守卫时，这样用：

    from deps import require_permission

    @router.post("/projects")
    def create_project(..., _=Depends(require_permission("project:write"))):
        ...

守卫挂在每个端点上（不在路由层统一挂），因为 20 个权限点要一个端点一个样，
集中映射表会在"新加端点忘了登记"时静默放过。
前端另有一层 store.can() 只负责别让人白点，真正的拦截在这里。
"""
from fastapi import Depends, HTTPException, Request

from repositories.users import users, sessions
from security import hash_session_token

COOKIE_NAME = "fv_session"


def _load_user(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return sessions.get(hash_session_token(token))


def optional_user(request: Request):
    """有登录态就返回用户，没有返回 None。"""
    user = getattr(request.state, "user", None)
    if user is None:
        user = _load_user(request)
        if user is not None:
            request.state.user = user
            request.state.permissions = users.permissions_for(user)
    return request.state.user if user is not None else None


def require_user(request: Request):
    user = optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def require_permission(code: str):
    """按权限点守卫。超级用户的 permissions 集合本来就是全集，天然通过。"""

    def dependency(request: Request, user=Depends(require_user)):
        granted = getattr(request.state, "permissions", set())
        if code not in granted:
            raise HTTPException(status_code=403, detail=f"缺少权限：{code}")
        return user

    return dependency


def actor_name(user) -> str:
    """这次动作的署名——用在文档正文的 Git 提交作者上。

    单独一个函数是为了"显示名优先、没有才退到用户名"这条规则只有一个出处：
    提交历史是要回头查"这版谁改的"，四处拼字符串会出现两种署名混在一条历史里。
    """
    return (user["display_name"] or user["username"]) if user is not None else "平台"
