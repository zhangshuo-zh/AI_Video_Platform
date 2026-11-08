"""登录态相关的依赖。其他路由要守卫时，这样用：

    from deps import require_permission

    @router.post("/projects")
    def create_project(..., _=Depends(require_permission("project:write"))):
        ...

守卫挂在每个端点上（不在路由层统一挂），因为 18 个权限点要一个端点一个样，
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


# 初始口令没改之前只放行这三条：改密、看自己、登出。
# 原来这条规则只写在前端（app.js 打开强制改密闸门），后端不拦＝拿着旧会话的人
# 照样能调所有业务接口，"第一次登录必须改密"就只是个提示框
_FORCE_PWD_EXEMPT = ("/api/auth/me", "/api/auth/password", "/api/auth/logout")


def _normalized_path(request: Request) -> str:
    """把请求路径规范化成可比较的形式：解码 → 折叠重复斜杠 → 去尾斜杠。

    为什么不能直接比 `request.url.path`（2026-11-08，P1）：
    Starlette **按解码后的路径路由**，但 `request.url.path` 保留的是原始写法。
    于是 `/api/auth/%6de` 会正常路由到 `me`，却在白名单比较时对不上
    `/api/auth/me` ——它被当成"不豁免"，而真正的风险方向正好相反：
    凡是能编码成"看起来不在白名单、实际路由到白名单端点"的写法，
    都会让这道闸门按错误的那个答案判。反过来也成立：将来若有人把白名单写成
    前缀匹配，`/api/auth/me/../../projects` 这类写法就会从豁免侧溜进业务端点。

    做法是"先归一，再精确比较"，与 `paths.py`／`workspace.py` 里那条
    「字符串层面的拒收 + 规范化后的边界复核」同一套思路。
    """
    raw = request.scope.get("path") or request.url.path or ""
    try:
        from urllib.parse import unquote
        decoded = unquote(raw)
    except Exception:
        decoded = raw
    # 折叠多余斜杠、去掉尾斜杠（根路径除外），避免 `//api/auth/me` / `/api/auth/me/`
    # 这类写法在两侧产生不同字符串。
    parts = [p for p in decoded.split("/") if p]
    return "/" + "/".join(parts)


def _force_pwd_exempt(request: Request) -> bool:
    return _normalized_path(request) in _FORCE_PWD_EXEMPT


def require_user(request: Request):
    user = optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    if user["must_change_password"] and not _force_pwd_exempt(request):
        raise HTTPException(status_code=403, detail="请先修改初始口令")
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
