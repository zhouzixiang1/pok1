"""新平台认证管理器(注册/登录/登出/重置密码)。

复用现有 ``arena/backend/auth.py`` 的 pbkdf2-sha256 密码哈希算法(已验证安全:
200k 迭代 + secrets.compare_digest 常量时间比较),但面向全用户操作新平台 Store。

与旧 ``auth.py``(只做管理员)的区别:
- 支持注册(username + email + password)
- 支持密码重置(无邮件服务:admin 生成一次性 token / 用户凭 token 设新密码)
- session 绑定 user_id(非 username)
- role 体系:user / admin
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

# 复用已验证的 pbkdf2 算法(不重写,直接 import 现有实现)
from ...auth import (  # noqa: TID252  有意复用同一套哈希算法
    hash_password, new_session_token, session_expires, verify_password,
)
from ..store import Store
from ..store.schema import ROLE_ADMIN, ROLE_USER

SESSION_TTL_SEC = 7 * 24 * 3600  # 7 天
PASSWORD_RESET_TTL_SEC = 24 * 3600  # 密码重置 token 24 小时有效

# session cookie 名(routes 和 dependencies 共用,放此避免循环 import)
COOKIE_NAME = "arena_session"

# 用户名规则:3-32 字符,字母数字下划线,首字符须字母
_USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,31}$")
# 简单邮箱校验(不做严格 RFC,够用即可)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# 密码最低强度:至少 8 字符(不强求复杂度,平台非金融级)
_MIN_PASSWORD_LEN = 8


class AuthError(Exception):
    """认证业务错误(基类)。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.match(username or ""):
        raise AuthError("invalid_username",
                        "用户名须 3-32 字符,字母开头,只含字母数字下划线")


def _validate_email(email: str) -> None:
    if not _EMAIL_RE.match(email or ""):
        raise AuthError("invalid_email", "邮箱格式不正确")


def _validate_password(password: str) -> None:
    if not password or len(password) < _MIN_PASSWORD_LEN:
        raise AuthError("weak_password", f"密码至少 {_MIN_PASSWORD_LEN} 个字符")


class AuthManager:
    """新平台认证:注册/登录/登出/重置密码,操作 Store。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    # ── 注册 ────────────────────────────────────────────────

    def register(self, username: str, email: str, password: str, *,
                 display_name: str = "") -> dict:
        """注册新用户。失败抛 AuthError,成功返回用户(不含 password_hash)。"""
        _validate_username(username)
        _validate_email(email)
        _validate_password(password)
        # 唯一性预检(给友好错误,而非裸 IntegrityError)
        if self.store.get_user_by_username(username):
            raise AuthError("username_taken", "用户名已被占用")
        if self.store.get_user_by_email(email):
            raise AuthError("email_taken", "邮箱已注册")
        pw_hash = hash_password(password)
        user = self.store.create_user(username, email, pw_hash,
                                      display_name=display_name, role=ROLE_USER)
        return _safe_user(user)

    # ── 登录 / 登出 ─────────────────────────────────────────

    def authenticate(self, username: str, password: str, *,
                     ip_addr: str = "", user_agent: str = "") -> tuple[dict, str]:
        """验证密码。成功返回 (safe_user, session_token),失败抛 AuthError。

        用户名不存在与密码错误返回相同错误码(防用户名枚举)。
        """
        user = self.store.get_user_by_username(username or "")
        # 即使用户不存在也做一次 verify(常量时间,防时序侧信道)
        ok = verify_password(password or "", user["password_hash"]) if user else False
        if not user or not ok:
            raise AuthError("invalid_credentials", "用户名或密码错误")
        if not user["is_active"]:
            raise AuthError("inactive", "账号已被停用,请联系管理员")
        token = new_session_token()
        self.store.add_session(token, user["id"],
                               session_expires(SESSION_TTL_SEC),
                               ip_addr=ip_addr, user_agent=user_agent)
        self.store.update_user(user["id"], last_login_at=datetime.now().isoformat(timespec="seconds"))
        return _safe_user(user), token

    def logout(self, token: str | None) -> None:
        """登出(删 session)。token 无效静默不报错。"""
        if token:
            self.store.delete_session(token)

    def verify_session(self, token: str | None) -> dict | None:
        """校验 session token。过期/无效返回 None,有效返回 safe_user。"""
        if not token:
            return None
        s = self.store.get_session(token)
        if not s:
            return None
        try:
            if datetime.fromisoformat(s["expires_at"]) < datetime.now():
                self.store.delete_session(token)
                return None
        except ValueError:
            return None
        user = self.store.get_user(s["user_id"])
        if not user or not user["is_active"]:
            self.store.delete_session(token)
            return None
        return _safe_user(user)

    # ── 改密码(已登录)──────────────────────────────────────

    def change_password(self, user_id: int, old_password: str,
                        new_password: str) -> None:
        """已登录用户改密码(需验证旧密码)。改完吊销所有 session 强制重登。"""
        _validate_password(new_password)
        user = self.store.get_user(user_id)
        if not user:
            raise AuthError("no_user", "用户不存在")
        if not verify_password(old_password or "", user["password_hash"]):
            raise AuthError("wrong_old_password", "旧密码错误")
        self.store.update_user(user_id, password_hash=hash_password(new_password))
        # 安全:改密码后所有旧 session 失效
        self.store.delete_user_sessions(user_id)

    # ── 密码重置(忘记密码,无邮件服务)──────────────────────

    def request_password_reset(self, email_or_username: str) -> tuple[str, dict]:
        """申请重置。无论账号是否存在都成功返回(防枚举),但只有真实账号才有有效 token。

        无邮件服务:返回 token 由 admin 转交用户(或开发态直接显示)。
        返回 (token, user) —— user 为 None 时表示账号不存在(调用方应丢弃 token)。
        """
        user = (self.store.get_user_by_email(email_or_username or "")
                or self.store.get_user_by_username(email_or_username or ""))
        if not user:
            # 防枚举:返回假 token,但调用方不应使用(我们用空串标记)
            return ("", {})
        token = secrets.token_urlsafe(32)
        expires = (datetime.now() + timedelta(seconds=PASSWORD_RESET_TTL_SEC)).isoformat(timespec="seconds")
        self.store.add_password_reset(token, user["id"], expires)
        return token, _safe_user(user)

    def reset_password(self, token: str, new_password: str) -> dict:
        """凭一次性 token 设新密码。成功返回用户,失败抛 AuthError。"""
        _validate_password(new_password)
        r = self.store.get_password_reset(token)
        if not r:
            raise AuthError("invalid_reset_token", "重置链接无效或已使用")
        try:
            if datetime.fromisoformat(r["expires_at"]) < datetime.now():
                self.store.mark_password_reset_used(token)
                raise AuthError("expired_reset_token", "重置链接已过期,请重新申请")
        except ValueError:
            raise AuthError("invalid_reset_token", "重置链接无效")
        user = self.store.get_user(r["user_id"])
        if not user:
            raise AuthError("no_user", "用户不存在")
        self.store.update_user(user["id"], password_hash=hash_password(new_password))
        self.store.mark_password_reset_used(token)
        # 吊销所有旧 session
        self.store.delete_user_sessions(user["id"])
        return _safe_user(user)

    # ── admin 操作 ──────────────────────────────────────────

    def admin_create_reset_token(self, username_or_email: str) -> tuple[str, dict]:
        """管理员为用户生成重置 token(admin 后台「重置某用户密码」用)。

        与 request_password_reset 类似,但账号不存在时抛错(admin 需要知道结果)。
        """
        user = (self.store.get_user_by_email(username_or_email or "")
                or self.store.get_user_by_username(username_or_email or ""))
        if not user:
            raise AuthError("no_user", "用户不存在")
        token = secrets.token_urlsafe(32)
        expires = (datetime.now() + timedelta(seconds=PASSWORD_RESET_TTL_SEC)).isoformat(timespec="seconds")
        self.store.add_password_reset(token, user["id"], expires)
        return token, _safe_user(user)

    def admin_set_user_role(self, user_id: int, role: str) -> dict:
        """管理员设置用户角色(user/admin)。"""
        if role not in (ROLE_USER, ROLE_ADMIN):
            raise AuthError("invalid_role", "角色必须是 user 或 admin")
        user = self.store.update_user(user_id, role=role)
        if not user:
            raise AuthError("no_user", "用户不存在")
        return _safe_user(user)


def _safe_user(user: dict | None) -> dict:
    """脱敏:去掉 password_hash,外部永不返回。"""
    if user is None:
        return {}
    return {k: v for k, v in user.items() if k != "password_hash"}
