"""新平台认证(注册/登录/重置密码/role)。"""
from .auth_manager import AuthError, AuthManager, COOKIE_NAME
from .dependencies import get_auth_manager, require_admin, require_user
from .routes import router

__all__ = [
    "AuthManager", "AuthError",
    "require_user", "require_admin", "get_auth_manager",
    "router", "COOKIE_NAME",
]
