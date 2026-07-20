"""里程碑 3 测试:bot 上传/构建/版本管理。

分两层:
1. builder 纯逻辑(Dockerfile 生成、zip 安全解压、checksum)—— 不实际 docker build
2. BotManager 业务(用 build=False 跳过 Docker)—— 测元数据/版本/权限/内置注册

Docker 实际构建在里程碑 4 的端到端测试覆盖。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arena.backend.platform.runtime import BotBuildError, make_dockerfile
from arena.backend.platform.runtime.bot_manager import BotManager
from arena.backend.platform.runtime.builder import (
    _find_entry, _safe_extract, checksum, save_upload,
)
from arena.backend.platform.runtime.routes import router as bots_router
from arena.backend.platform.auth import AuthManager
from arena.backend.platform.auth.routes import router as auth_router
from arena.backend.platform.store import Store


# ── fixture ───────────────────────────────────────────────

def _make_zip(files: dict[str, str]) -> bytes:
    """构造 zip:files={相对路径: 内容}。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def _make_app(tmp_path) -> tuple[FastAPI, Store, AuthManager, BotManager]:
    store = Store(str(tmp_path / "test.db"))
    auth = AuthManager(store)
    bot_mgr = BotManager(store, upload_root=tmp_path / "uploads")
    # 建系统用户(内置 bot 的 owner)+ 普通用户
    system = store.create_user("system", "system@arena.local", "!",
                               role="admin", display_name="系统")
    app = FastAPI()
    app.state.platform_store = store
    app.state.platform_auth = auth
    app.state.platform_bot_manager = bot_mgr
    app.state.platform_system_user_id = system["id"]
    app.include_router(auth_router)
    app.include_router(bots_router)
    return app, store, auth, bot_mgr


# ══════════════════════════════════════════════════════════
# 1. builder 纯逻辑
# ══════════════════════════════════════════════════════════

def test_dockerfile_json():
    df = make_dockerfile(protocol="json", entry_file="main.py", runtime_lang="python")
    assert "FROM python:3.12-slim" in df
    assert 'ENTRYPOINT ["python", "main.py"]' in df
    assert "USER botuser" in df  # 非 root
    assert "tcp_bridge" not in df  # JSON 不含桥


def test_dockerfile_tcp():
    df = make_dockerfile(protocol="tcp", entry_file="national_bot.py", runtime_lang="python")
    assert "FROM python:3.12-slim" in df
    assert "COPY tcp_bridge.py /app/_bridge/tcp_bridge.py" in df  # 含桥
    assert "tcp_bridge.py" in df  # entrypoint 用桥
    assert "USER botuser" in df


def test_dockerfile_bad_protocol():
    with pytest.raises(BotBuildError) as e:
        make_dockerfile(protocol="xxx", entry_file="main.py")
    assert e.value.code == "bad_protocol"


def test_safe_extract_normal(tmp_path):
    raw = _make_zip({"main.py": "print('hi')", "utils.py": "x=1",
                     "sub/deep.py": "y=2"})
    zip_path = tmp_path / "src.zip"
    zip_path.write_bytes(raw)
    files = _safe_extract(zip_path, tmp_path / "out")
    assert set(files) == {"main.py", "utils.py", "sub/deep.py"}


def test_safe_extract_path_traversal(tmp_path):
    # 经典 zip slip:../../etc/passwd
    raw = _make_zip({"../../evil.py": "hacked"})
    zip_path = tmp_path / "x.zip"
    zip_path.write_bytes(raw)
    with pytest.raises(BotBuildError) as e:
        _safe_extract(zip_path, tmp_path / "out")
    assert e.value.code == "path_traversal"


def test_safe_extract_bad_zip(tmp_path):
    (tmp_path / "bad.zip").write_bytes(b"not a zip")
    with pytest.raises(BotBuildError) as e:
        _safe_extract(tmp_path / "bad.zip", tmp_path / "out")
    assert e.value.code == "bad_zip"


def test_find_entry():
    assert _find_entry(["main.py", "x.py"], "main.py") == "main.py"
    # 子目录下的入口
    assert _find_entry(["pkg/main.py", "readme.md"], "main.py") == "pkg/main.py"
    with pytest.raises(BotBuildError) as e:
        _find_entry(["other.py"], "main.py")
    assert e.value.code == "entry_not_found"


def test_save_upload_too_large(tmp_path):
    big = b"x" * (60 * 1024 * 1024)
    with pytest.raises(BotBuildError) as e:
        save_upload(big, tmp_path / "big.zip")
    assert e.value.code == "too_large"


def test_checksum_deterministic():
    raw = b"hello world"
    assert checksum(raw) == checksum(raw)
    assert len(checksum(raw)) == 64  # sha256 hex


# ══════════════════════════════════════════════════════════
# 2. BotManager 业务(build=False 跳过 Docker)
# ══════════════════════════════════════════════════════════

def test_create_bot_from_upload(tmp_path):
    _, store, _, mgr = _make_app(tmp_path)
    user = store.create_user("alice", "a@b.com", "!")
    raw = _make_zip({"main.py": "print(1)", "strategy.py": "x=1"})
    bot = mgr.create_bot_from_upload(
        user["id"], "MyBot", raw, protocol="json", entry_file="main.py",
        build=False)
    assert bot["name"] == "MyBot"
    assert bot["protocol"] == "json"
    assert bot["current_version"] == 1
    assert bot["has_image"] is False  # build=False 无镜像
    # 文件落盘
    assert (tmp_path / "uploads" / str(bot["id"]) / "v1" / "source.zip").exists()
    assert (tmp_path / "uploads" / str(bot["id"]) / "v1" / "src" / "main.py").exists()


def test_upload_new_version(tmp_path):
    _, store, _, mgr = _make_app(tmp_path)
    user = store.create_user("alice", "a@b.com", "!")
    raw1 = _make_zip({"main.py": "v1"})
    bot = mgr.create_bot_from_upload(user["id"], "Bot", raw1, build=False)
    raw2 = _make_zip({"main.py": "v2"})
    bot = mgr.upload_new_version(bot["id"], raw2, build=False)
    assert bot["current_version"] == 2
    versions = mgr.list_bot_versions(bot["id"])
    assert len(versions) == 2
    assert versions[0]["version"] == 2  # 最新在前


def test_upload_missing_entry_rolls_back(tmp_path):
    """入口文件缺失应回滚(删 bot 记录 + 文件)。"""
    _, store, _, mgr = _make_app(tmp_path)
    user = store.create_user("alice", "a@b.com", "!")
    raw = _make_zip({"other.py": "x"})  # 缺 main.py
    with pytest.raises(BotBuildError) as e:
        mgr.create_bot_from_upload(user["id"], "Bot", raw,
                                   entry_file="main.py", build=False)
    assert e.value.code == "entry_not_found"
    # 回滚:bot 记录不存在
    assert store.list_bots(owner_id=user["id"]) == []
    # 回滚:文件清理
    upload_dirs = list((tmp_path / "uploads").iterdir()) if (tmp_path / "uploads").exists() else []
    # 可能留空 uploads 目录,但不应有 bot 的子目录
    assert all(not d.name.isdigit() for d in upload_dirs)


def test_list_and_filter(tmp_path):
    _, store, _, mgr = _make_app(tmp_path)
    u1 = store.create_user("alice", "a@b.com", "!")
    u2 = store.create_user("bob", "b@c.com", "!")
    raw = _make_zip({"main.py": "x"})
    b1 = mgr.create_bot_from_upload(u1["id"], "BotA", raw, build=False)
    b2 = mgr.create_bot_from_upload(u2["id"], "BotB", raw, build=False)
    # 我的 bot
    mine = mgr.list_bots(owner_id=u1["id"], include_builtin=False)
    assert [b["name"] for b in mine] == ["BotA"]
    # 公开 bot(含内置)
    pub = mgr.list_bots(public_only=True, include_builtin=False)
    assert {b["name"] for b in pub} == {"BotA", "BotB"}


def test_set_active_and_delete(tmp_path):
    _, store, _, mgr = _make_app(tmp_path)
    user = store.create_user("alice", "a@b.com", "!")
    raw = _make_zip({"main.py": "x"})
    bot = mgr.create_bot_from_upload(user["id"], "Bot", raw, build=False)
    # 下架
    mgr.set_active(bot["id"], False)
    assert mgr.get_bot(bot["id"])["is_active"] is False
    # 无对局可删
    assert mgr.delete_bot(bot["id"]) is True
    assert mgr.get_bot(bot["id"]) is None


def test_register_builtin_bots(tmp_path):
    """注册内置 bot 库(现有 national_v*)。"""
    _, store, _, mgr = _make_app(tmp_path)
    system = store.get_user_by_username("system")
    bots = mgr.register_builtin_bots(system["id"])
    assert len(bots) > 0
    names = [b["name"] for b in bots]
    assert any("national_v" in n for n in names)
    # 都标为内置 + TCP 协议 + national_bot.py 入口
    for b in bots:
        assert b["is_builtin"] is True
        assert b["protocol"] == "tcp"
        assert b["entry_file"] == "national_bot.py"
        assert b["is_public"] is True
    # 幂等:再注册不重复
    bots2 = mgr.register_builtin_bots(system["id"])
    assert len(bots2) == len(bots)


def test_register_builtin_specific_versions(tmp_path):
    _, store, _, mgr = _make_app(tmp_path)
    system = store.get_user_by_username("system")
    bots = mgr.register_builtin_bots(system["id"], versions=["national_v141"])
    assert len(bots) == 1
    assert bots[0]["name"] == "national_v141"


# ══════════════════════════════════════════════════════════
# 3. API 路由(TestClient)
# ══════════════════════════════════════════════════════════

def _login(client, username, password="securepass1"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200


def test_api_upload_bot(tmp_path):
    app, store, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    store.create_user("alice", "a@b.com", "!")  # 预建账号省去注册
    # 直接改密码登录(测试便利)
    from arena.backend.auth import hash_password
    store.update_user(store.get_user_by_username("alice")["id"],
                      password_hash=hash_password("securepass1"))
    _login(client, "alice")
    zip_bytes = _make_zip({"main.py": "print(1)"})
    r = client.post("/api/bots", files={"file": ("bot.zip", zip_bytes, "application/zip")},
                    data={"name": "MyBot", "protocol": "json", "entry_file": "main.py"})
    assert r.status_code == 200, r.text
    assert r.json()["bot"]["name"] == "MyBot"


def test_api_upload_requires_login(tmp_path):
    app, _, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    zip_bytes = _make_zip({"main.py": "x"})
    r = client.post("/api/bots", files={"file": ("b.zip", zip_bytes)},
                    data={"name": "Bot"})
    assert r.status_code == 401


def test_api_list_bots(tmp_path):
    app, store, _, mgr = _make_app(tmp_path)
    client = TestClient(app)
    # 注册内置 bot
    mgr.register_builtin_bots(store.get_user_by_username("system")["id"])
    store.create_user("alice", "a@b.com", "!")
    from arena.backend.auth import hash_password
    store.update_user(store.get_user_by_username("alice")["id"],
                      password_hash=hash_password("securepass1"))
    _login(client, "alice")
    r = client.get("/api/bots?scope=public")
    assert r.status_code == 200
    # 含内置 bot
    assert len(r.json()["bots"]) > 0


def test_api_permission_other_user_version(tmp_path):
    """不能给别人的 bot 上传版本。"""
    app, store, _, mgr = _make_app(tmp_path)
    client = TestClient(app)
    # alice 建 bot
    alice = store.create_user("alice", "a@b.com", "!")
    raw = _make_zip({"main.py": "x"})
    bot = mgr.create_bot_from_upload(alice["id"], "Bot", raw, build=False)
    # bob 登录想给 alice 的 bot 传版本 → 403
    store.create_user("bob", "b@c.com", "!")
    from arena.backend.auth import hash_password
    store.update_user(store.get_user_by_username("bob")["id"],
                      password_hash=hash_password("securepass1"))
    _login(client, "bob")
    zip2 = _make_zip({"main.py": "hack"})
    r = client.post(f"/api/bots/{bot['id']}/versions",
                    files={"file": ("v.zip", zip2)}, data={"upload_note": "hack"})
    assert r.status_code == 403


def test_api_register_builtin_requires_admin(tmp_path):
    app, store, _, _ = _make_app(tmp_path)
    client = TestClient(app)
    store.create_user("normaluser", "u@b.com", "!")
    from arena.backend.auth import hash_password
    store.update_user(store.get_user_by_username("normaluser")["id"],
                      password_hash=hash_password("securepass1"))
    _login(client, "normaluser")
    r = client.post("/api/bots/register-builtin")
    assert r.status_code == 403
