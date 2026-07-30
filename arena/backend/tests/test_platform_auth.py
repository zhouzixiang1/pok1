"""新平台认证系统测试。

测两层:
1. AuthManager 业务逻辑
2. FastAPI 路由(/api/auth/*)
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena.backend.platform.auth import AuthError, AuthManager, COOKIE_NAME, router
from arena.backend.platform.auth.captcha import CaptchaStore
from arena.backend.platform.mail import Mailer
from arena.backend.platform.store import Store


class FakeMailer(Mailer):
    """测试用:不真正 SMTP,只记录发送。"""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, str]] = []
        # 伪造已配置
        self.config.user = "test@example.com"
        self.config.password = "x"
        self.config.from_addr = "test@example.com"

    def send(self, to_addr: str, subject: str, *,
             body_text: str = "", body_html: str = "") -> None:
        self.sent.append((to_addr, subject))


def _make_app(tmp_path) -> tuple[FastAPI, Store, AuthManager, CaptchaStore, FakeMailer]:
    store = Store(str(tmp_path / "test.db"))
    mailer = FakeMailer()
    auth = AuthManager(store, mailer=mailer)
    captcha = CaptchaStore()
    app = FastAPI()
    app.state.platform_store = store
    app.state.platform_auth = auth
    app.state.platform_captcha = captcha
    app.state.platform_mailer = mailer
    app.include_router(router)
    return app, store, auth, captcha, mailer


def _captcha_pair(captcha: CaptchaStore) -> dict:
    cid, answer, _ = captcha.create()
    return {"captcha_id": cid, "captcha_answer": answer}


def _verify_user(store: Store, username: str) -> None:
    u = store.get_user_by_username(username)
    store.update_user(u["id"], email_verified=1)


# ══════════════════════════════════════════════════════════
# 1. AuthManager 业务逻辑
# ══════════════════════════════════════════════════════════

def test_register_success(tmp_path):
    _, _, auth, _, _ = _make_app(tmp_path)
    user = auth.register("alice", "alice@example.com", "securepass1")
    assert user["username"] == "alice"
    assert user["role"] == "user"
    assert "password_hash" not in user
    assert user["is_active"] == 1
    assert user.get("email_verified") in (0, False)


def test_register_validation(tmp_path):
    _, _, auth, _, _ = _make_app(tmp_path)
    with pytest.raises(AuthError) as e:
        auth.register("ab", "a@b.com", "securepass1")
    assert e.value.code == "invalid_username"
    with pytest.raises(AuthError):
        auth.register("1abc", "a@b.com", "securepass1")
    with pytest.raises(AuthError) as e:
        auth.register("bob", "not-an-email", "securepass1")
    assert e.value.code == "invalid_email"
    with pytest.raises(AuthError) as e:
        auth.register("bob", "b@c.com", "short")
    assert e.value.code == "weak_password"


def test_register_duplicate(tmp_path):
    _, _, auth, _, _ = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    with pytest.raises(AuthError) as e:
        auth.register("alice", "other@example.com", "securepass1")
    assert e.value.code == "username_taken"
    with pytest.raises(AuthError) as e:
        auth.register("bob", "alice@example.com", "securepass1")
    assert e.value.code == "email_taken"


def test_authenticate_requires_email_verified(tmp_path):
    _, store, auth, _, _ = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    with pytest.raises(AuthError) as e:
        auth.authenticate("alice", "securepass1")
    assert e.value.code == "email_unverified"
    _verify_user(store, "alice")
    user, token = auth.authenticate("alice", "securepass1")
    assert user["username"] == "alice"
    assert len(token) > 20
    assert auth.verify_session(token)["username"] == "alice"


def test_authenticate_wrong_password(tmp_path):
    _, store, auth, _, _ = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    _verify_user(store, "alice")
    with pytest.raises(AuthError) as e:
        auth.authenticate("alice", "wrongpass")
    assert e.value.code == "invalid_credentials"
    with pytest.raises(AuthError) as e:
        auth.authenticate("ghost", "whatever")
    assert e.value.code == "invalid_credentials"


def test_authenticate_inactive_user(tmp_path):
    _, store, auth, _, _ = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    _verify_user(store, "alice")
    store.update_user(store.get_user_by_username("alice")["id"], is_active=0)
    with pytest.raises(AuthError) as e:
        auth.authenticate("alice", "securepass1")
    assert e.value.code == "inactive"


def test_logout_and_session_expiry(tmp_path):
    _, store, auth, _, _ = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    _verify_user(store, "alice")
    _, token = auth.authenticate("alice", "securepass1")
    assert auth.verify_session(token) is not None
    auth.logout(token)
    assert auth.verify_session(token) is None


def test_change_password(tmp_path):
    _, store, auth, _, _ = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    _verify_user(store, "alice")
    _, token = auth.authenticate("alice", "securepass1")
    auth.change_password(
        auth.verify_session(token)["id"], "securepass1", "newpass123")
    assert auth.verify_session(token) is None
    _, token2 = auth.authenticate("alice", "newpass123")
    with pytest.raises(AuthError) as e:
        auth.change_password(
            auth.verify_session(token2)["id"], "wrongold", "another9")
    assert e.value.code == "wrong_old_password"


def test_email_verify_and_reset_code_flow(tmp_path):
    _, store, auth, _, mailer = _make_app(tmp_path)
    user = auth.register("alice", "alice@example.com", "securepass1")
    auth.send_email_code(user, "verify")
    assert mailer.sent
    row = store.get_latest_email_code(user["id"], "verify")
    assert row
    auth.verify_email_code("alice", row["code"])
    assert store.get_user_by_username("alice")["email_verified"] == 1

    auth.request_password_reset("alice")
    row = store.get_latest_email_code(user["id"], "reset")
    auth.reset_password_with_code("alice", row["code"], "resetpass1")
    auth.authenticate("alice", "resetpass1")


def test_request_reset_nonexistent_user_no_leak(tmp_path):
    _, _, auth, _, _ = _make_app(tmp_path)
    sent, user = auth.request_password_reset("ghost@example.com")
    assert sent is False
    assert user == {}


def test_admin_create_reset_token(tmp_path):
    _, store, auth, _, _ = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    store.update_user(store.get_user_by_username("alice")["id"], role="admin")
    token, user = auth.admin_create_reset_token("alice")
    assert token and user["username"] == "alice"
    with pytest.raises(AuthError) as e:
        auth.admin_create_reset_token("ghost")
    assert e.value.code == "no_user"


def test_captcha_one_shot(tmp_path):
    _, _, _, captcha, _ = _make_app(tmp_path)
    cid, answer, png = captcha.create()
    assert png.startswith(b"\x89PNG")
    assert captcha.verify(cid, answer)
    assert not captcha.verify(cid, answer)  # 一次性


# ══════════════════════════════════════════════════════════
# 2. FastAPI 路由(TestClient)
# ══════════════════════════════════════════════════════════

def test_api_register_and_login(tmp_path):
    app, store, _, captcha, _ = _make_app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/auth/register", json={
        "username": "alice", "email": "alice@example.com",
        "password": "securepass1", **_captcha_pair(captcha)})
    assert r.status_code == 200, r.text
    assert r.json()["need_verify"] is True
    _verify_user(store, "alice")
    r = client.post("/api/auth/login", json={
        "username": "alice", "password": "securepass1",
        **_captcha_pair(captcha)})
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "alice"
    assert COOKIE_NAME in r.cookies or "token" in r.json()


def test_api_login_sets_cookie_and_me(tmp_path):
    app, store, _, captcha, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@example.com", "password": "securepass1",
        **_captcha_pair(captcha)})
    _verify_user(store, "bob")
    r = client.post("/api/auth/login", json={
        "username": "bob", "password": "securepass1", **_captcha_pair(captcha)})
    assert r.status_code == 200
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "bob"


def test_api_me_unauthorized(tmp_path):
    app, _, _, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_api_login_wrong_password(tmp_path):
    app, store, _, captcha, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@example.com", "password": "securepass1",
        **_captcha_pair(captcha)})
    _verify_user(store, "bob")
    r = client.post("/api/auth/login", json={
        "username": "bob", "password": "wrong", **_captcha_pair(captcha)})
    assert r.status_code == 401


def test_api_logout(tmp_path):
    app, store, _, captcha, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@example.com", "password": "securepass1",
        **_captcha_pair(captcha)})
    _verify_user(store, "bob")
    client.post("/api/auth/login", json={
        "username": "bob", "password": "securepass1", **_captcha_pair(captcha)})
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_api_register_validation_error(tmp_path):
    app, _, _, captcha, _ = _make_app(tmp_path)
    client = TestClient(app)
    r = client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@example.com", "password": "short",
        **_captcha_pair(captcha)})
    assert r.status_code == 422
    r = client.post("/api/auth/register", json={
        "username": "ab", "email": "b@c.com", "password": "securepass1",
        **_captcha_pair(captcha)})
    assert r.status_code == 422


def test_api_register_duplicate(tmp_path):
    app, _, _, captcha, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "alice", "email": "alice@example.com", "password": "securepass1",
        **_captcha_pair(captcha)})
    r = client.post("/api/auth/register", json={
        "username": "alice", "email": "other@example.com", "password": "securepass1",
        **_captcha_pair(captcha)})
    assert r.status_code == 409


def test_api_password_reset_full_flow(tmp_path):
    app, store, _, captcha, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "alice", "email": "alice@example.com", "password": "securepass1",
        **_captcha_pair(captcha)})
    _verify_user(store, "alice")
    r = client.post("/api/auth/request-reset", json={
        "email_or_username": "alice", **_captcha_pair(captcha)})
    assert r.status_code == 200
    assert r.json().get("token") is None
    row = store.get_latest_email_code(
        store.get_user_by_username("alice")["id"], "reset")
    r = client.post("/api/auth/reset-password", json={
        "email_or_username": "alice", "code": row["code"],
        "new_password": "newpass123"})
    assert r.status_code == 200
    r = client.post("/api/auth/login", json={
        "username": "alice", "password": "newpass123",
        **_captcha_pair(captcha)})
    assert r.status_code == 200


def test_api_admin_endpoint_requires_admin(tmp_path):
    app, store, _, captcha, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "normaluser", "email": "u@example.com", "password": "securepass1",
        **_captcha_pair(captcha)})
    _verify_user(store, "normaluser")
    client.post("/api/auth/login", json={
        "username": "normaluser", "password": "securepass1",
        **_captcha_pair(captcha)})
    r = client.post("/api/auth/admin/create-reset-token",
                    json={"username_or_email": "normaluser"})
    assert r.status_code == 403


def test_api_admin_can_create_reset_token(tmp_path):
    app, store, _, captcha, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "adminuser", "email": "a@example.com", "password": "securepass1",
        **_captcha_pair(captcha)})
    client.post("/api/auth/register", json={
        "username": "target", "email": "t@example.com", "password": "securepass1",
        **_captcha_pair(captcha)})
    store.update_user(store.get_user_by_username("adminuser")["id"],
                      role="admin", email_verified=1)
    client.post("/api/auth/login", json={
        "username": "adminuser", "password": "securepass1",
        **_captcha_pair(captcha)})
    r = client.post("/api/auth/admin/create-reset-token",
                    json={"username_or_email": "target"})
    assert r.status_code == 200
    assert r.json()["token"]
