"""登录、登出、当前用户、改密码。"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from config import SESSION_HOURS
from deps import COOKIE_NAME, require_user
from repositories.users import users, sessions
from security import hash_password, hash_session_token, new_session_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD_LEN = 6


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
def login(payload: LoginIn, response: Response):
    user = users.get_by_username(payload.username.strip())

    # 用户不存在、账号停用、口令不对，一律同一句话：
    # 分开报错等于告诉别人哪些用户名存在
    if (user is None or not user["is_active"]
            or not verify_password(payload.password, user["password_hash"])):
        raise HTTPException(status_code=401, detail="用户名或密码不对")

    token, token_hash = new_session_token()
    expires_at = (datetime.now() + timedelta(hours=SESSION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    sessions.create(token_hash, user["id"], expires_at)
    users.touch_login(user["id"])

    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,          # JS 读不到，降低 XSS 后的损失
        samesite="lax",
        secure=False,           # 局域网走 http；上 HTTPS 后改 True
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
