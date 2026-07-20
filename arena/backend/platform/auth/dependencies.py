"""认证 FastAPI 依赖:require_user / require_admin。

所有需要登录的平台端点注入这两个依赖。token 来源:cookie ``arena_session``
或 ``Authorization: Bearer <token>`` header(前端 fetch 用)。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from .auth_manager import AuthManager, COOKIE_NAME


def get_auth_manager(request: Request) -> AuthManager:
    """从 app.state 取 AuthManager(main.py 启动时注入)。"""
    auth = getattr(request.app.state, "platform_auth", None)
    if auth is None:
        raise HTTPException(status_code=503, detail="平台认证未启用(DB 未初始化)")
    return auth


def _extract_token(request: Request) -> str | None:
    """token 优先级:Authorization Bearer header > cookie。

    直接从 request 读,不用 FastAPI Cookie 参数注入(避免参数解析耦合)。
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    return request.cookies.get(COOKIE_NAME)


def require_user(request: Request,
                 auth: AuthManager = Depends(get_auth_manager)) -> dict:
    """要求已登录。返回 safe_user(无 password_hash)。"""
    token = _extract_token(request)
    user = auth.verify_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话过期")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    """要求管理员。"""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
