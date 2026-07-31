#!/usr/bin/env python3
"""端到端用户流程功能测试:模拟真实用户走遍全部功能。

架构:``httpx.AsyncClient`` + ``ASGITransport`` + 持久事件循环。
  - 同进程访问 app,图形验证码 answer 可从 ``app.state.platform_captcha`` 取
    (模拟"肉眼看图识别");邮箱验证码从 DB ``email_codes`` 表读(模拟"查收邮件")。
  - 持久 loop 让 orchestrator 的 ``create_task`` 后台对战胜任务能正常推进
    (TestClient 的临时 loop 会丢弃后台 task,故不用)。
  - 走真实 ASGI 协议栈 + 中间件 + 路由,与 HTTP 等价。

覆盖功能:
  1. 认证:图形码 / 注册 / 邮箱验证 / 登录 / me / 改密 / 登出
  2. 权限:未登录拒绝 / 越权拒绝 / admin
  3. 密码重置:申请重置码 / 重置 / admin token 重置 / 防枚举
  4. Bot:上传 JSON/TCP / 列表 / 详情 / 版本 / 改名 / 上下架 / 删除
  5. 对战:发起(JSON vs 内置) / 完成 / 状态正确 / argv_style 回归
  6. 数据:对局列表 / 详情 / 回放(逐手/逐步) / 排行榜 / 战绩
  7. 管理:邮件模板 / outbox

用法:.venv/bin/python scripts/e2e_user_flow_test.py
"""
from __future__ import annotations

import asyncio
import io
import os
import sqlite3
import sys
import tempfile
import time
import zipfile

os.environ.setdefault("POK_PLATFORM_RATE_LIMIT", "0")
os.environ.setdefault("POK_PLATFORM_MAX_CONCURRENT_MATCHES", "2")

import httpx  # noqa: E402

from arena.backend.platform.main import create_platform_app  # noqa: E402

# ──────────────────────────────────────────────────────────────
# 辅助
# ──────────────────────────────────────────────────────────────

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  ✅ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ❌ {name}  {detail}")


async def get_captcha(c: httpx.AsyncClient, app) -> tuple[str, str]:
    r = await c.get("/api/auth/captcha")
    cid = r.json()["captcha_id"]
    # 同进程取 answer(模拟"识别图形码")
    await asyncio.sleep(0)  # 让事件循环推进,确保 create 完成
    answer = app.state.platform_captcha._items[cid].answer
    return cid, answer


def latest_code(db_path: str, purpose: str) -> str | None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT code FROM email_codes WHERE purpose=? ORDER BY id DESC LIMIT 1",
        (purpose,)).fetchone()
    con.close()
    return row["code"] if row else None


async def register_verify(c: httpx.AsyncClient, app, db_path: str,
                          username: str, email: str,
                          password: str = "Pass1234") -> bool:
    cid, ans = await get_captcha(c, app)
    r = await c.post("/api/auth/register", json={
        "username": username, "email": email, "password": password,
        "captcha_id": cid, "captcha_answer": ans})
    if r.status_code != 200:
        return False
    code = latest_code(db_path, "verify")
    if not code:
        return False
    r = await c.post("/api/auth/verify-email", json={
        "email_or_username": username, "code": code})
    return r.status_code == 200


async def login(c: httpx.AsyncClient, app, username: str,
                password: str = "Pass1234") -> str | None:
    cid, ans = await get_captcha(c, app)
    r = await c.post("/api/auth/login", json={
        "username": username, "password": password,
        "captcha_id": cid, "captcha_answer": ans})
    return r.json().get("token") if r.status_code == 200 else None


# ──────────────────────────────────────────────────────────────
# Bot fixture
# ──────────────────────────────────────────────────────────────

def json_bot_zip() -> bytes:
    src = ("import json, sys\n"
           "for line in sys.stdin:\n"
           "    json.loads(line.strip())\n"
           "    print(json.dumps({'response': 0}))\n"
           "    sys.stdout.flush()\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.py", src)
    return buf.getvalue()


def tcp_bot_zip() -> bytes:
    """极简 TCP bot(位置参数解析),收消息回 check。验证连通性。"""
    src = (
        "import socket, sys\n"
        "host = sys.argv[1] if len(sys.argv) > 1 else '127.0.0.1'\n"
        "port = int(sys.argv[2]) if len(sys.argv) > 2 else 50101\n"
        "name = sys.argv[3] if len(sys.argv) > 3 else 'Bot'\n"
        "s = socket.create_connection((host, port), timeout=30)\n"
        "buf = b''\n"
        "while True:\n"
        "    data = s.recv(4096)\n"
        "    if not data: break\n"
        "    buf += data\n"
        "    if buf.startswith(b'name'):\n"
        "        s.sendall(name.encode()); buf = buf[4:]; break\n"
        "while True:\n"
        "    data = s.recv(4096)\n"
        "    if not data: break\n"
        "    buf += data\n"
        "    s.sendall(b'check')\n"
        "    buf = b''\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("national_bot.py", src)
    return buf.getvalue()


async def wait_match(c: httpx.AsyncClient, H: dict, mid: str,
                     timeout: int = 120) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = await c.get(f"/api/matches/{mid}", headers=H)
        if r.status_code == 200:
            m = r.json().get("match", {})
            if m.get("status") in ("completed", "aborted"):
                return m
        await asyncio.sleep(1)
    # 超时返回最后一次
    r = await c.get(f"/api/matches/{mid}", headers=H)
    return r.json().get("match", {}) if r.status_code == 200 else {}


# ══════════════════════════════════════════════════════════════
async def run_tests() -> int:
    print("=" * 64)
    print("pok-arena 端到端用户流程功能测试")
    print("=" * 64)

    tmpdir = tempfile.mkdtemp(prefix="e2e_")
    db_path = os.path.join(tmpdir, "test.db")
    upload_root = os.path.join(tmpdir, "uploads")
    os.makedirs(upload_root, exist_ok=True)

    app = create_platform_app(db_path=db_path, upload_root=upload_root)
    app.state.platform_bot_manager.register_builtin_bots(
        app.state.platform_system_user_id)
    transport = httpx.ASGITransport(app=app)
    c = httpx.AsyncClient(transport=transport, base_url="http://test")

    # ══════════════════════════════════════════════════════════
    print("\n【1】认证流程:图形码 / 注册 / 邮箱验证 / 登录 / me")
    # ══════════════════════════════════════════════════════════
    r = await c.get("/api/auth/captcha")
    check("图形验证码生成", r.status_code == 200
          and "captcha_id" in r.json() and "image_base64" in r.json(),
          f"status={r.status_code}")

    cid, ans = await get_captcha(c, app)
    r = await c.post("/api/auth/register", json={
        "username": "alice", "email": "alice@x.com", "password": "Pass1234",
        "display_name": "Alice", "captcha_id": cid, "captcha_answer": ans})
    check("注册(含图形码)", r.status_code == 200 and r.json().get("need_verify"),
          f"status={r.status_code} {r.text[:80]}")

    r = await c.post("/api/auth/verify-email", json={
        "email_or_username": "alice", "code": "000000"})
    check("错误邮箱码被拒", r.status_code == 400, f"status={r.status_code}")

    cid, ans = await get_captcha(c, app)
    r = await c.post("/api/auth/login", json={
        "username": "alice", "password": "Pass1234",
        "captcha_id": cid, "captcha_answer": ans})
    check("未验证邮箱禁止登录", r.status_code == 403, f"status={r.status_code}")

    code = latest_code(db_path, "verify")
    r = await c.post("/api/auth/verify-email", json={
        "email_or_username": "alice", "code": code})
    check("邮箱验证成功", r.status_code == 200, f"status={r.status_code}")

    tok_alice = await login(c, app, "alice")
    check("登录成功(获 token)", tok_alice is not None, "无 token")
    H = {"Authorization": f"Bearer {tok_alice}"} if tok_alice else {}

    r = await c.get("/api/auth/me", headers=H)
    check("/me 返回当前用户", r.status_code == 200
          and r.json()["user"]["username"] == "alice", f"{r.status_code}")

    cid, ans = await get_captcha(c, app)
    r = await c.post("/api/auth/register", json={
        "username": "alice", "email": "o@x.com", "password": "Pass1234",
        "captcha_id": cid, "captcha_answer": ans})
    check("重复用户名注册被拒(409)", r.status_code == 409, f"status={r.status_code}")

    cid, ans = await get_captcha(c, app)
    r = await c.post("/api/auth/register", json={
        "username": "newu", "email": "n@x.com", "password": "Pass1234",
        "captcha_id": cid, "captcha_answer": "wrong"})
    check("错误图形码注册被拒(400)", r.status_code == 400, f"status={r.status_code}")

    # ══════════════════════════════════════════════════════════
    print("\n【2】权限控制:未登录拒绝 / 弱密码拒绝")
    # ══════════════════════════════════════════════════════════
    # 用独立的、无任何登录态的 client 测未登录(主 client c 会复用登录 cookie)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
        r = await anon.get("/api/auth/me")
        check("未登录访问 /me 被拒", r.status_code in (401, 403), f"status={r.status_code}")
        r = await anon.post("/api/matches/challenge", json={"opponent_bot_id": 1})
        check("未登录发起对战被拒", r.status_code in (401, 403), f"status={r.status_code}")
        r = await anon.get("/api/bots?scope=mine")
        check("未登录列 bot 被拒", r.status_code in (401, 403), f"status={r.status_code}")

    cid, ans = await get_captcha(c, app)
    r = await c.post("/api/auth/register", json={
        "username": "weakpw", "email": "w@x.com", "password": "123",
        "captcha_id": cid, "captcha_answer": ans})
    check("弱密码注册被拒(422)", r.status_code == 422, f"status={r.status_code}")

    # ══════════════════════════════════════════════════════════
    print("\n【3】Bot 上传与管理")
    # ══════════════════════════════════════════════════════════
    r = await c.post("/api/bots", headers=H,
                     files={"file": ("jb.zip", json_bot_zip(), "application/zip")},
                     data={"name": "json_alice", "protocol": "json",
                           "entry_file": "main.py", "display_name": "Alice JSON"})
    check("上传 JSON bot", r.status_code == 200, f"status={r.status_code} {r.text[:100]}")
    json_id = r.json().get("bot", {}).get("id") if r.status_code == 200 else None

    r = await c.post("/api/bots", headers=H,
                     files={"file": ("tb.zip", tcp_bot_zip(), "application/zip")},
                     data={"name": "tcp_alice", "protocol": "tcp",
                           "entry_file": "national_bot.py",
                           "argv_style": "positional"})
    check("上传 TCP bot", r.status_code == 200, f"status={r.status_code} {r.text[:100]}")
    tcp_id = r.json().get("bot", {}).get("id") if r.status_code == 200 else None

    if tcp_id:
        r = await c.get(f"/api/bots/{tcp_id}", headers=H)
        argv = r.json().get("bot", {}).get("argv_style")
        check("TCP bot argv_style=positional 已存(回归)", argv == "positional",
              f"argv_style={argv}")

    r = await c.get("/api/bots?scope=mine", headers=H)
    mine = r.json().get("bots", []) if r.status_code == 200 else []
    check("列出我的 bot(≥2)", r.status_code == 200 and len(mine) >= 2, f"count={len(mine)}")

    r = await c.get("/api/bots?scope=public", headers=H)
    pub = r.json().get("bots", []) if r.status_code == 200 else []
    check("列出公开 bot(含内置 ≥10)", r.status_code == 200 and len(pub) >= 10,
          f"count={len(pub)}")

    if json_id:
        r = await c.get(f"/api/bots/{json_id}", headers=H)
        check("bot 详情", r.status_code == 200
              and r.json()["bot"]["name"] == "json_alice", f"{r.status_code}")

        r = await c.post(f"/api/bots/{json_id}/versions", headers=H,
                         files={"file": ("v2.zip", json_bot_zip(), "application/zip")},
                         data={"upload_note": "v2"})
        check("上传新版本", r.status_code == 200, f"status={r.status_code}")
        r = await c.get(f"/api/bots/{json_id}/versions", headers=H)
        vers = r.json().get("versions", []) if r.status_code == 200 else []
        check("版本历史(≥2)", len(vers) >= 2, f"count={len(vers)}")

        r = await c.patch(f"/api/bots/{json_id}", headers=H,
                          json={"display_name": "Alice JSON v2"})
        check("改 bot 信息", r.status_code == 200
              and r.json()["bot"]["display_name"] == "Alice JSON v2", f"{r.status_code}")

    # ══════════════════════════════════════════════════════════
    print("\n【4】对战发起(JSON bot vs 内置 bot)+ 完成")
    # ══════════════════════════════════════════════════════════
    if json_id:
        r = await c.post("/api/matches/challenge", headers=H,
                         json={"my_bot_id": json_id, "opponent_bot_id": 1})
        check("发起对战", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")
        mid1 = r.json().get("match_id") if r.status_code == 200 else None
        if mid1:
            print(f"    ⏳ 等待对战完成(最多 120s)...")
            m = await wait_match(c, H, mid1, timeout=120)
            check("对战结束(completed/aborted)",
                  m.get("status") in ("completed", "aborted"),
                  f"status={m.get('status')}")
            check("对战 hands_played > 0", (m.get("hands_played") or 0) > 0,
                  f"hands={m.get('hands_played')}")

    # ══════════════════════════════════════════════════════════
    print("\n【5】数据查询:列表 / 详情 / 回放 / 排行榜 / 战绩")
    # ══════════════════════════════════════════════════════════
    r = await c.get("/api/matches?limit=10", headers=H)
    matches = r.json().get("matches", []) if r.status_code == 200 else []
    total = r.json().get("total", 0) if r.status_code == 200 else 0
    check("对局列表", r.status_code == 200, f"status={r.status_code}")
    check("对局列表有数据", total >= 1, f"total={total}")

    if matches:
        dm = matches[0]["id"]
        r = await c.get(f"/api/matches/{dm}", headers=H)
        check("对局详情", r.status_code == 200
              and "match" in r.json() and "events" in r.json(), f"{r.status_code}")
        r = await c.get(f"/api/matches/{dm}/replay", headers=H)
        check("对局回放(snapshots)", r.status_code == 200
              and "snapshots" in r.json(), f"status={r.status_code}")
        r = await c.get(f"/api/matches/{dm}/replay/hands", headers=H)
        check("逐手快照", r.status_code == 200 and "hand_count" in r.json(),
              f"status={r.status_code}")
        r = await c.get(f"/api/matches/{dm}/replay/step?step=0", headers=H)
        check("逐步回放", r.status_code == 200, f"status={r.status_code}")

    r = await c.get("/api/leaderboard", headers=H)
    check("排行榜(Glicko-2)", r.status_code == 200
          and isinstance(r.json().get("leaderboard"), list), f"status={r.status_code}")
    r = await c.get("/api/leaderboard/by-chips", headers=H)
    check("按筹码排行榜", r.status_code == 200, f"status={r.status_code}")
    if json_id:
        r = await c.get(f"/api/bots/{json_id}/record", headers=H)
        check("bot 战绩", r.status_code == 200, f"status={r.status_code} {r.text[:80]}")
    r = await c.get("/api/state")
    check("全局状态 /api/state", r.status_code == 200, f"status={r.status_code}")

    # ══════════════════════════════════════════════════════════
    print("\n【6】密码修改与重置")
    # ══════════════════════════════════════════════════════════
    r = await c.post("/api/auth/change-password", headers=H, json={
        "old_password": "Pass1234", "new_password": "NewPass5678"})
    check("改密码", r.status_code == 200, f"status={r.status_code}")
    old = await login(c, app, "alice", "Pass1234")
    check("改密后旧密码登录失败", old is None, "旧密码仍可登录!")
    new = await login(c, app, "alice", "NewPass5678")
    check("改密后新密码登录成功", new is not None, "新密码登录失败")
    if new:
        tok_alice = new
        H = {"Authorization": f"Bearer {tok_alice}"}

    cid, ans = await get_captcha(c, app)
    r = await c.post("/api/auth/request-reset", json={
        "email_or_username": "alice", "captcha_id": cid, "captcha_answer": ans})
    check("申请密码重置", r.status_code == 200, f"status={r.status_code}")
    rcode = latest_code(db_path, "reset")
    r = await c.post("/api/auth/reset-password", json={
        "email_or_username": "alice", "code": rcode, "new_password": "ResetPass90"})
    check("用邮箱码重置密码", r.status_code == 200, f"status={r.status_code}")
    rt = await login(c, app, "alice", "ResetPass90")
    check("重置后新密码登录", rt is not None, "重置后无法登录")
    if rt:
        tok_alice = rt
        H = {"Authorization": f"Bearer {tok_alice}"}

    cid, ans = await get_captcha(c, app)
    r = await c.post("/api/auth/request-reset", json={
        "email_or_username": "ghost_user", "captcha_id": cid, "captcha_answer": ans})
    check("防枚举(不存在账号也成功)", r.status_code == 200, f"status={r.status_code}")

    # ══════════════════════════════════════════════════════════
    print("\n【7】登出与会话失效")
    # ══════════════════════════════════════════════════════════
    r = await c.post("/api/auth/logout", headers=H)
    check("登出", r.status_code == 200, f"status={r.status_code}")
    r = await c.get("/api/auth/me", headers=H)
    check("登出后 token 失效", r.status_code in (401, 403), f"status={r.status_code}")

    # ══════════════════════════════════════════════════════════
    print("\n【8】管理功能(admin)")
    # ══════════════════════════════════════════════════════════
    await register_verify(c, app, db_path, "bob", "bob@x.com")
    app.state.platform_store.update_user(
        app.state.platform_store.get_user_by_username("bob")["id"], role="admin")
    tok_bob = await login(c, app, "bob")
    Hb = {"Authorization": f"Bearer {tok_bob}"} if tok_bob else {}

    r = await c.get("/api/admin/email-templates", headers=Hb)
    check("admin 列邮件模板", r.status_code == 200, f"status={r.status_code}")
    r = await c.get("/api/admin/email-outbox?limit=5", headers=Hb)
    check("admin 查发件箱", r.status_code == 200, f"status={r.status_code}")

    await register_verify(c, app, db_path, "carol", "carol@x.com")
    tok_carol = await login(c, app, "carol")
    Hc = {"Authorization": f"Bearer {tok_carol}"} if tok_carol else {}
    r = await c.get("/api/admin/email-templates", headers=Hc)
    check("非 admin 访问管理被拒(403)", r.status_code == 403, f"status={r.status_code}")

    r = await c.post("/api/auth/admin/create-reset-token", headers=Hb,
                     json={"username_or_email": "carol"})
    check("admin 创建重置 token", r.status_code == 200 and r.json().get("token"),
          f"status={r.status_code}")
    atok = r.json().get("token")
    if atok:
        r = await c.post("/api/auth/reset-password-token", json={
            "token": atok, "new_password": "AdminReset1"})
        check("用 admin token 重置密码", r.status_code == 200, f"status={r.status_code}")

    # ══════════════════════════════════════════════════════════
    print("\n【9】bot 上下架与删除")
    # ══════════════════════════════════════════════════════════
    r = await c.post("/api/bots", headers=Hb,
                     files={"file": ("del.zip", json_bot_zip(), "application/zip")},
                     data={"name": "bob_del", "protocol": "json", "entry_file": "main.py"})
    did = r.json().get("bot", {}).get("id") if r.status_code == 200 else None
    if did:
        r = await c.post(f"/api/bots/{did}/deactivate", headers=Hb)
        check("bot 下架", r.status_code == 200, f"status={r.status_code}")
        r = await c.delete(f"/api/bots/{did}", headers=Hb)
        check("bot 删除", r.status_code == 200, f"status={r.status_code} {r.text[:80]}")

    await c.aclose()
    # 清理 docker(测试起的容器)
    try:
        await app.state.platform_docker_runner.cleanup_all()
    except Exception:
        pass

    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 64)
    print(f"结果:✅ {len(PASSED)} 通过   ❌ {len(FAILED)} 失败")
    if FAILED:
        print("-" * 64)
        for name, detail in FAILED:
            print(f"  ❌ {name}  {detail}")
        print("=" * 64)
        return 1
    print("=" * 64)
    print("全部功能测试通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run_tests()))
