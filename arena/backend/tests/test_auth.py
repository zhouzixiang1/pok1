"""认证测试:pbkdf2 哈希/校验(常量时间)+ session + AuthManager。"""
from __future__ import annotations

from arena.backend.auth import (
    AuthManager,
    hash_password,
    new_session_token,
    verify_password,
)
from arena.backend.store import Store


def test_hash_and_verify_password():
    h = hash_password("secret123")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("secret123", h)
    assert not verify_password("wrong", h)
    assert not verify_password("secret123", "garbage")
    assert not verify_password("secret123", "pbkdf2_sha256$bad$xx$yy")


def test_password_unique_salt():
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2                      # 不同 salt -> 不同哈希
    assert verify_password("same", h1)
    assert verify_password("same", h2)


def test_new_session_token_unique():
    a, b = new_session_token(), new_session_token()
    assert a != b and len(a) > 20


def test_auth_manager_lifecycle(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    auth = AuthManager(s)
    assert not auth.has_admin("admin")
    auth.set_password("admin", "pass")
    assert auth.has_admin("admin")
    assert auth.authenticate("admin", "wrong") is None      # 错密码
    tok = auth.authenticate("admin", "pass")
    assert tok and len(tok) > 20
    assert auth.verify_session(tok)["username"] == "admin"  # session 有效
    assert auth.verify_session("bogus") is None
    assert auth.verify_session(None) is None
    auth.logout(tok)
    assert auth.verify_session(tok) is None                  # logout 后失效
