"""新平台认证系统测试(里程碑 2)。

测两层:
1. AuthManager 业务逻辑(注册/登录/改密/重置,直接调方法)
2. FastAPI 路由(/api/auth/*,用 TestClient)
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena.backend.platform.auth import AuthError, AuthManager, COOKIE_NAME, router
from arena.backend.platform.store import Store


# ── fixture:每测试独立 app + db ──────────────────────────

def _make_app(tmp_path) -> tuple[FastAPI, Store, AuthManager]:
    """构造带 /api/auth 路由的 app + 注入 Store/AuthManager。"""
    store = Store(str(tmp_path / "test.db"))
    auth = AuthManager(store)
    app = FastAPI()
    app.state.platform_store = store
    app.state.platform_auth = auth
    app.include_router(router)
    return app, store, auth


# ══════════════════════════════════════════════════════════
# 1. AuthManager 业务逻辑
# ══════════════════════════════════════════════════════════

def test_register_success(tmp_path):
    _, _, auth = _make_app(tmp_path)
    user = auth.register("alice", "alice@example.com", "securepass1")
    assert user["username"] == "alice"
    assert user["role"] == "user"
    assert "password_hash" not in user
    assert user["is_active"] == 1


def test_register_validation(tmp_path):
    _, _, auth = _make_app(tmp_path)
    # 用户名太短
    with pytest.raises(AuthError) as e:
        auth.register("ab", "a@b.com", "securepass1")
    assert e.value.code == "invalid_username"
    # 用户名首字符非字母
    with pytest.raises(AuthError):
        auth.register("1abc", "a@b.com", "securepass1")
    # 邮箱格式
    with pytest.raises(AuthError) as e:
        auth.register("bob", "not-an-email", "securepass1")
    assert e.value.code == "invalid_email"
    # 密码太短
    with pytest.raises(AuthError) as e:
        auth.register("bob", "b@c.com", "short")
    assert e.value.code == "weak_password"


def test_register_duplicate(tmp_path):
    _, _, auth = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    with pytest.raises(AuthError) as e:
        auth.register("alice", "other@example.com", "securepass1")
    assert e.value.code == "username_taken"
    with pytest.raises(AuthError) as e:
        auth.register("bob", "alice@example.com", "securepass1")
    assert e.value.code == "email_taken"


def test_authenticate_success(tmp_path):
    _, _, auth = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    user, token = auth.authenticate("alice", "securepass1")
    assert user["username"] == "alice"
    assert len(token) > 20
    # session 可校验
    assert auth.verify_session(token)["username"] == "alice"


def test_authenticate_wrong_password(tmp_path):
    _, _, auth = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    # 错误密码:错误码与用户不存在相同(防枚举)
    with pytest.raises(AuthError) as e:
        auth.authenticate("alice", "wrongpass")
    assert e.value.code == "invalid_credentials"
    # 不存在的用户:同错误码
    with pytest.raises(AuthError) as e:
        auth.authenticate("ghost", "whatever")
    assert e.value.code == "invalid_credentials"


def test_authenticate_inactive_user(tmp_path):
    _, store, auth = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    store.update_user(store.get_user_by_username("alice")["id"], is_active=0)
    with pytest.raises(AuthError) as e:
        auth.authenticate("alice", "securepass1")
    assert e.value.code == "inactive"


def test_verify_session_expiry_and_logout(tmp_path):
    _, store, auth = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    _, token = auth.authenticate("alice", "securepass1")
    assert auth.verify_session(token) is not None
    # 手动让 session 过期
    from datetime import datetime, timedelta
    store.add_session(token, store.get_user_by_username("alice")["id"],
                      (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"))
    assert auth.verify_session(token) is None  # 过期后无效且被删
    # 重新登录拿新 token,登出后失效
    _, token2 = auth.authenticate("alice", "securepass1")
    auth.logout(token2)
    assert auth.verify_session(token2) is None


def test_change_password(tmp_path):
    _, _, auth = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    _, token = auth.authenticate("alice", "securepass1")
    # 改密码
    auth.change_password(
        auth.verify_session(token)["id"], "securepass1", "newpass123")
    # 旧 session 被吊销(改密强制重登)
    assert auth.verify_session(token) is None
    # 新密码能登录,旧的不行
    with pytest.raises(AuthError):
        auth.authenticate("alice", "securepass1")
    _, _ = auth.authenticate("alice", "newpass123")
    # 旧密码错误
    _, token2 = auth.authenticate("alice", "newpass123")
    with pytest.raises(AuthError) as e:
        auth.change_password(
            auth.verify_session(token2)["id"], "wrongold", "another9")
    assert e.value.code == "wrong_old_password"


def test_password_reset_flow(tmp_path):
    _, _, auth = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    # 申请重置
    token, user = auth.request_password_reset("alice")
    assert token and user["username"] == "alice"
    # 凭 token 设新密码
    auth.reset_password(token, "resetpass1")
    # 旧密码失效,新密码可登录
    with pytest.raises(AuthError):
        auth.authenticate("alice", "securepass1")
    _, _ = auth.authenticate("alice", "resetpass1")
    # token 一次性,再用失败
    with pytest.raises(AuthError) as e:
        auth.reset_password(token, "another1")
    assert e.value.code == "invalid_reset_token"


def test_request_reset_nonexistent_user_no_leak(tmp_path):
    """申请重置不存在的账号:不抛错(防枚举),但 token 为空。"""
    _, _, auth = _make_app(tmp_path)
    token, user = auth.request_password_reset("ghost@example.com")
    assert token == ""
    assert user == {}


def test_admin_create_reset_token(tmp_path):
    _, store, auth = _make_app(tmp_path)
    auth.register("alice", "alice@example.com", "securepass1")
    # admin 先把自己提为 admin
    store.update_user(store.get_user_by_username("alice")["id"], role="admin")
    token, user = auth.admin_create_reset_token("alice")
    assert token and user["username"] == "alice"
    # admin 操作不存在的用户:抛错(与 request_reset 不同,admin 需知道结果)
    with pytest.raises(AuthError) as e:
        auth.admin_create_reset_token("ghost")
    assert e.value.code == "no_user"


# ══════════════════════════════════════════════════════════
# 2. FastAPI 路由(TestClient)
# ══════════════════════════════════════════════════════════

def test_api_register_and_login(tmp_path):
    app, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    # 注册
    r = client.post("/api/auth/register", json={
        "username": "alice", "email": "alice@example.com",
        "password": "securepass1"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["username"] == "alice"
    # 登录
    r = client.post("/api/auth/login", json={
        "username": "alice", "password": "securepass1"})
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "alice"
    assert COOKIE_NAME in r.cookies or "token" in r.json()


def test_api_login_sets_cookie_and_me(tmp_path):
    app, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@example.com", "password": "securepass1"})
    r = client.post("/api/auth/login", json={
        "username": "bob", "password": "securepass1"})
    assert r.status_code == 200
    # cookie 自动带(TestClient 维持会话),/me 应返回用户
    r = client.get("/api/auth/me")
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "bob"


def test_api_me_unauthorized(tmp_path):
    app, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_api_login_wrong_password(tmp_path):
    app, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@example.com", "password": "securepass1"})
    r = client.post("/api/auth/login", json={
        "username": "bob", "password": "wrong"})
    assert r.status_code == 401


def test_api_logout(tmp_path):
    app, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@example.com", "password": "securepass1"})
    client.post("/api/auth/login", json={
        "username": "bob", "password": "securepass1"})
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    # 登出后 cookie 失效(TestClient 会带旧 cookie,但服务端已删 session)
    # 注意:登出删除了 cookie,/me 应 401
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_api_register_validation_error(tmp_path):
    app, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    # 密码太短(pydantic Field min_length=8 → 422)
    r = client.post("/api/auth/register", json={
        "username": "bob", "email": "bob@example.com", "password": "short"})
    assert r.status_code == 422
    # 用户名太短
    r = client.post("/api/auth/register", json={
        "username": "ab", "email": "b@c.com", "password": "securepass1"})
    assert r.status_code == 422


def test_api_register_duplicate(tmp_path):
    app, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "alice", "email": "alice@example.com", "password": "securepass1"})
    r = client.post("/api/auth/register", json={
        "username": "alice", "email": "other@example.com", "password": "securepass1"})
    assert r.status_code == 409


def test_api_password_reset_full_flow(tmp_path):
    app, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "alice", "email": "alice@example.com", "password": "securepass1"})
    # 申请重置
    r = client.post("/api/auth/request-reset", json={"email_or_username": "alice"})
    assert r.status_code == 200
    token = r.json()["token"]
    assert token
    # 凭 token 重置
    r = client.post("/api/auth/reset-password", json={
        "token": token, "new_password": "newpass123"})
    assert r.status_code == 200
    # 新密码登录
    r = client.post("/api/auth/login", json={
        "username": "alice", "password": "newpass123"})
    assert r.status_code == 200


def test_api_admin_endpoint_requires_admin(tmp_path):
    app, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    client.post("/api/auth/register", json={
        "username": "normaluser", "email": "u@example.com", "password": "securepass1"})
    client.post("/api/auth/login", json={
        "username": "normaluser", "password": "securepass1"})
    # 普通用户调 admin 端点 → 403
    r = client.post("/api/auth/admin/create-reset-token",
                    json={"username_or_email": "normaluser"})
    assert r.status_code == 403


def test_api_admin_can_create_reset_token(tmp_path):
    app, store, _ = _make_app(tmp_path)
    client = TestClient(app)
    # 注册两个用户,把 admin 提权
    client.post("/api/auth/register", json={
        "username": "adminuser", "email": "a@example.com", "password": "securepass1"})
    client.post("/api/auth/register", json={
        "username": "target", "email": "t@example.com", "password": "securepass1"})
    store.update_user(store.get_user_by_username("adminuser")["id"], role="admin")
    client.post("/api/auth/login", json={
        "username": "adminuser", "password": "securepass1"})
    r = client.post("/api/auth/admin/create-reset-token",
                    json={"username_or_email": "target"})
    assert r.status_code == 200
    assert r.json()["token"]
