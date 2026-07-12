"""arena 认证:管理员密码(pbkdf2_hmac-sha256 哈希)+ session token。

stdlib 实现(hashlib + secrets),无新依赖。
- 管理员(web)用密码登录 -> session token(cookie/header)
- bot 连接身份用 users.active 白名单(可选 --require-registration,serve 阶段实现),
  不改 TCP 协议(官方裸 name)

密码格式:``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``,常量时间比较。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

SESSION_TTL_SEC = 7 * 24 * 3600  # 7 天


def hash_password(password: str, salt: str | None = None,
                  iterations: int = 200_000) -> str:
    """pbkdf2-hmac-sha256 哈希。返回 ``pbkdf2_sha256$iterations$salt_hex$hash_hex``。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间比较(secrets.compare_digest,抗时序)。"""
    try:
        algo, iters, salt, hash_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), int(iters))
        return secrets.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_expires(seconds: int = SESSION_TTL_SEC) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class AuthManager:
    """管理员认证 + session(经 Store admins/sessions 表)。"""

    def __init__(self, store) -> None:
        self.store = store

    def set_password(self, username: str, password: str) -> None:
        self.store.upsert_admin(username, hash_password(password))

    def has_admin(self, username: str) -> bool:
        return self.store.get_admin(username) is not None

    def authenticate(self, username: str, password: str) -> str | None:
        """验证密码;成功返回新 session token,失败 None。"""
        admin = self.store.get_admin(username)
        if not admin or not verify_password(password, admin["password_hash"]):
            return None
        token = new_session_token()
        self.store.add_session(token, username, session_expires())
        return token

    def verify_session(self, token: str | None) -> dict | None:
        """校验 session token(cookie/header);过期/无效返回 None。"""
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
        return s

    def logout(self, token: str | None) -> None:
        if token:
            self.store.delete_session(token)
