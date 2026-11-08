"""登录、登出、当前用户、改密码。"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from config import COOKIE_SECURE, SESSION_HOURS
from deps import COOKIE_NAME, require_user
from repositories.users import login_throttle, sessions, users
from security import (hash_password, hash_session_token, new_session_token,
                      verify_password)

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LEN = 6

# 用户不存在时也要跑一次 scrypt（对这份固定哈希），用来抹平"用户存在/不存在"
# 两条路径的耗时差。它不是一个真口令的哈希，谁也验不过去。
_DUMMY_HASH = hash_password("dummy-password-for-constant-time-login")


class LoginIn(BaseModel):
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=MIN_PASSWORD_LEN)


def _profile(user) -> dict:
    return {
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "is_superuser": bool(user["is_superuser"]),
            "must_change_password": bool(user["must_change_password"]),
        },
        "roles": users.roles_for(user["id"]),
        "permissions": sorted(users.permissions_for(user)),
    }


@router.post("/login")
def login(payload: LoginIn, response: Response, request: Request):
    username = payload.username.strip()
    client = request.client.host if request.client else ""
    # 先看限速：连续失败到上限之后，连口令都不比对，直接 429。
    # 这一道挡的是"在线撞库"——`/api/auth/login` 是全平台唯一不用登录的入口，
    # 没有计数的话，一个 6 位口令在局域网里是可以被跑完的。
    wait = login_throttle.retry_after(username, client)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请 {wait} 秒后再试",
            headers={"Retry-After": str(wait)})

    user = users.get_by_username(username)

    # 用户不存在、账号停用、口令不对，一律同一句话：
    # 分开报错等于告诉别人哪些用户名存在。
    #
    # 用户不存在时**也走一次 scrypt**（对固定的假哈希），把两条路径的耗时抹平：
    # 原来"不存在"直接 return，短了几十毫秒，响应时间就成了一个用户名枚举信道。
    ok = False
    if user is None:
        verify_password(payload.password, _DUMMY_HASH)
    else:
        ok = bool(user["is_active"]) and verify_password(
            payload.password, user["password_hash"])
    if not ok:
        login_throttle.record_failure(username, client)
        raise HTTPException(status_code=401, detail="用户名或密码不对")
    login_throttle.reset(username, client)

    token, token_hash = new_session_token()
    expires_at = (datetime.now() + timedelta(hours=SESSION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    sessions.create(token_hash, user["id"], expires_at)
    users.touch_login(user["id"])

    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,          # JS 读不到，降低 XSS 后的损失
        # strict：跨站请求一律不带 cookie。lax 会放行"顶层 GET 导航"，
        # 对本平台没有好处（没有需要从外站点进来的登录态页面）。
        samesite="strict",
        # 由部署开关决定：局域网明文 http 时必须是 False，否则浏览器根本不会存；
        # 一旦上了 HTTPS/反代，**必须**设 FASTVIDEO_COOKIE_SECURE=1。
        secure=COOKIE_SECURE,
        path="/",
    )
    return _profile(user)


@router.post("/logout")
def logout(request: Request, response: Response, user=Depends(require_user)):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        sessions.delete(hash_session_token(token))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(user=Depends(require_user)):
    return _profile(user)


@router.post("/password")
def change_password(payload: ChangePasswordIn, response: Response,
                    user=Depends(require_user)):
    # 口令哈希从库里现取，不从会话行上拿：sessions.get 现在只带身份列，
    # 为的是让 `password_hash` 不再跟着"当前用户"这个对象到处走（缓存、日志、导出）。
    # 比对新旧口令是这一条接口的专属需要，不该由所有接口的返回值一起买单。
    account = users.get_by_id(user["id"])
    if account is None or not verify_password(payload.old_password,
                                              account["password_hash"]):
        raise HTTPException(status_code=401, detail="原密码不对")
    if payload.new_password == payload.old_password:
        raise HTTPException(status_code=422, detail="新密码不能和原密码相同")

    users.update_password(user["id"], hash_password(payload.new_password))
    # 改密后踢掉该用户所有会话（包括这一次），逼所有端重新登录
    sessions.delete_all_for_user(user["id"])
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True, "relogin": True}
