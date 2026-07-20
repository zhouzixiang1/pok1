"""认证 API 路由:注册/登录/登出/改密/重置密码。

挂载到主 app 的 ``/api/auth`` 前缀(里程碑 5 的 main.py 集成)。

端点:
- POST /api/auth/register     注册
- POST /api/auth/login        登录(设 cookie)
- POST /api/auth/logout       登出
- GET  /api/auth/me           当前登录用户
- POST /api/auth/change-password  改密码(需登录 + 旧密码)
- POST /api/auth/request-reset    申请密码重置(返回 token,无邮件服务)
- POST /api/auth/reset-password   凭 token 设新密码
- POST /api/auth/admin/create-reset-token  admin 为用户生成重置 token
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .auth_manager import AuthError, AuthManager, COOKIE_NAME
from .dependencies import require_admin, require_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_MAX_AGE = 7 * 24 * 3600  # 与 SESSION_TTL_SEC 一致


# ── 请求/响应模型 ─────────────────────────────────────────

class RegisterReq(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    email: str  # 严格校验在 AuthManager.register 的 _validate_email
    password: str = Field(..., min_length=8)
    display_name: str = Field("", max_length=64)


class LoginReq(BaseModel):
    username: str
    password: str


class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8)


class RequestResetReq(BaseModel):
    email_or_username: str


class ResetPasswordReq(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)


class AdminResetReq(BaseModel):
    username_or_email: str


# ── 路由 ──────────────────────────────────────────────────

def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(COOKIE_NAME, token,
                    httponly=True, max_age=COOKIE_MAX_AGE,
                    samesite="lax", path="/")


def _err(exc: AuthError) -> HTTPException:
    """AuthError → HTTPException(409 用于已存在,400 用于格式,401 用于凭证)。"""
    code_to_status = {
        "username_taken": 409, "email_taken": 409,
        "invalid_credentials": 401, "inactive": 403,
        "wrong_old_password": 401, "invalid_reset_token": 400,
        "expired_reset_token": 400,
    }
    status = code_to_status.get(exc.code, 400)
    return HTTPException(status_code=status, detail=exc.message)


@router.post("/register")
async def register(req: RegisterReq, request: Request,
                  response: Response) -> dict:
    auth: AuthManager = request.app.state.platform_auth
    try:
        user = auth.register(req.username, req.email, req.password,
                             display_name=req.display_name)
    except AuthError as exc:
        raise _err(exc)
    return {"user": user, "message": "注册成功,请登录"}


@router.post("/login")
async def login(req: LoginReq, request: Request, response: Response) -> dict:
    auth: AuthManager = request.app.state.platform_auth
    try:
        user, token = auth.authenticate(
            req.username, req.password,
            ip_addr=request.client.host if request.client else "",
            user_agent=request.headers.get("user-agent", ""))
    except AuthError as exc:
        raise _err(exc)
    _set_session_cookie(response, token)
    return {"user": user, "token": token}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    auth: AuthManager = request.app.state.platform_auth
    token = (request.headers.get("authorization", "")[7:].strip()
             if request.headers.get("authorization", "").lower().startswith("bearer ")
             else request.cookies.get(COOKIE_NAME))
    auth.logout(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(require_user)) -> dict:
    return {"user": user}


@router.post("/change-password")
async def change_password(req: ChangePasswordReq,
                          user: dict = Depends(require_user),
                          auth: AuthManager = Depends(lambda r=...: r.app.state.platform_auth)) -> dict:  # type: ignore
    try:
        auth.change_password(user["id"], req.old_password, req.new_password)
    except AuthError as exc:
        raise _err(exc)
    return {"ok": True, "message": "密码已修改,请重新登录"}


@router.post("/request-reset")
async def request_reset(req: RequestResetReq, request: Request) -> dict:
    """申请密码重置。无邮件服务:返回 token(开发态/admin 转交)。

    注意:为防用户名枚举,无论账号是否存在都返回 200。token 为空串表示账号不存在
    (调用方应丢弃);非空才有效。生产环境若有邮件服务,这里改为发邮件不返回 token。
    """
    auth: AuthManager = request.app.state.platform_auth
    token, user = auth.request_password_reset(req.email_or_username)
    if not token:
        # 账号不存在,但仍返回成功(防枚举),不暴露 token
        return {"ok": True, "message": "若账号存在,重置链接已生成",
                "token": None}
    return {"ok": True, "message": "重置 token 已生成(无邮件服务,请记录)",
            "token": token, "username": user.get("username")}


@router.post("/reset-password")
async def reset_password(req: ResetPasswordReq, request: Request) -> dict:
    auth: AuthManager = request.app.state.platform_auth
    try:
        user = auth.reset_password(req.token, req.new_password)
    except AuthError as exc:
        raise _err(exc)
    return {"ok": True, "message": "密码已重置,请用新密码登录", "username": user.get("username")}


@router.post("/admin/create-reset-token")
async def admin_create_reset_token(req: AdminResetReq, request: Request,
                                   _: dict = Depends(require_admin)) -> dict:
    """管理员为某用户生成密码重置 token(admin 后台用)。"""
    auth: AuthManager = request.app.state.platform_auth
    try:
        token, user = auth.admin_create_reset_token(req.username_or_email)
    except AuthError as exc:
        raise _err(exc)
    return {"ok": True, "token": token, "user": user}
