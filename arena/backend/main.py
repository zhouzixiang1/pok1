"""arena FastAPI 应用 + uvicorn/TCP 单进程共存。

参考 pok1 ``sever/main.py``:``uvicorn.Server(config).serve()`` +
``asyncio.gather(tcp_coro, ...)`` 让 FastAPI 与 TCP server 同 loop 共存
(**勿用阻塞** ``uvicorn.run``)。SSE 桥接参考 ``sever/web/app.py``(Queue 扇出 +
keepalive + is_disconnected 清理)。

端点:
- 公开只读:/api/state /api/arena/events(SSE) /api/leaderboard /api/users/{name}
  /api/matches(筛选分页) /api/matches/{id}(详情+thp+events) /api/matches/{id}/thp
- admin(/api/admin/*):login/logout + users CRUD(需 session token)
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .auth import AuthManager
from .server.match_manager import MatchManager, SSE_KEEPALIVE_SEC
from .server.tcp_server import DEFAULT_HOST, DEFAULT_PORT

logger = logging.getLogger(__name__)

WEB_DEFAULT_PORT = 50180


def _read_events_jsonl(match_id: str) -> list:
    """读 logs/<match_id>/events.jsonl(每行 JSON)。文件不存在返回 []。"""
    path = Path("logs") / match_id / "events.jsonl"
    if not path.exists():
        return []
    out: list = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return out


def create_app(manager: MatchManager, *, static_dir: Path | None = None) -> FastAPI:
    """构造 FastAPI app,注入 MatchManager 单例。

    static_dir 给定时挂载前端构建产物(SPA, html=True);API 路由先于 mount 注册,
    ``/api/*`` 不会被静态捕获。
    """
    app = FastAPI(title="pok-arena", version="0.1.0",
                  description="国赛德州扑克对弈平台 web 复刻")
    store = manager.store
    auth = AuthManager(store) if store is not None else None
    app.state.store = store
    app.state.auth = auth

    def require_admin(request: Request) -> str:
        if auth is None:
            raise HTTPException(status_code=503, detail="DB 未启用,无管理功能")
        token = request.cookies.get("arena_admin") or request.headers.get("x-admin-token")
        s = auth.verify_session(token)
        if not s:
            raise HTTPException(status_code=401, detail="未登录或会话过期")
        return s["username"]

    # ── 公开只读 ──────────────────────────────────────────────

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        return JSONResponse(manager.get_snapshot())

    @app.get("/api/arena/events")
    async def arena_events(request: Request) -> StreamingResponse:
        queue = manager.subscribe()
        snapshot = manager.get_snapshot()

        async def gen():
            try:
                yield f"data: {json.dumps(snapshot, ensure_ascii=False, default=str)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SEC)
                        yield f"data: {data}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                manager.unsubscribe(queue)

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/leaderboard")
    async def leaderboard(limit: int = 50) -> JSONResponse:
        if store is None:
            return JSONResponse({"leaderboard": []})
        return JSONResponse({"leaderboard": store.leaderboard(limit)})

    @app.get("/api/users/{name}")
    async def user_profile(name: str) -> JSONResponse:
        if store is None:
            raise HTTPException(status_code=503, detail="DB 未启用")
        user = store.get_user(name)
        if user is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        return JSONResponse({
            "user": user,
            "rating": store.get_rating(name),
            "pair_stats": store.pair_stats_for(name),
            "recent_matches": store.list_matches(user=name, limit=20),
        })

    @app.get("/api/matches")
    async def list_matches(user: str | None = None,
                           limit: int = 50, offset: int = 0) -> JSONResponse:
        if store is not None:
            return JSONResponse({
                "matches": store.list_matches(user, limit, offset),
                "total": store.count_matches(user),
            })
        # 无 DB 回退:从 index.json
        idx = manager.list_matches()
        return JSONResponse({"matches": idx, "total": len(idx)})

    @app.get("/api/matches/{match_id}")
    async def match_detail(match_id: str) -> JSONResponse:
        m = store.get_match(match_id) if store is not None else None
        if m is None and not (manager.records_dir / f"{match_id}.thp").exists():
            raise HTTPException(status_code=404, detail="对局不存在")
        return JSONResponse({
            "match": m,
            "thp": manager.read_thp(match_id),
            "events": _read_events_jsonl(match_id),
        })

    @app.get("/api/matches/{match_id}/thp")
    async def get_thp(match_id: str) -> JSONResponse:
        text = manager.read_thp(match_id)
        if text is None:
            raise HTTPException(status_code=404, detail="match thp not found")
        return JSONResponse({"match_id": match_id, "thp": text})

    # ── admin(需 session token)──────────────────────────────

    @app.post("/api/admin/login")
    async def admin_login(request: Request) -> JSONResponse:
        if auth is None:
            raise HTTPException(status_code=503, detail="DB 未启用,无管理功能")
        try:
            body = await request.json()
        except Exception:
            body = {}
        token = auth.authenticate(body.get("username", ""), body.get("password", ""))
        if not token:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        resp = JSONResponse({"username": body.get("username"), "token": token})
        resp.set_cookie("arena_admin", token, httponly=True,
                        max_age=7 * 24 * 3600, samesite="lax")
        return resp

    @app.post("/api/admin/logout")
    async def admin_logout(request: Request) -> JSONResponse:
        token = request.cookies.get("arena_admin") or request.headers.get("x-admin-token")
        if auth is not None:
            auth.logout(token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("arena_admin")
        return resp

    @app.get("/api/admin/users")
    async def admin_list_users(_: str = Depends(require_admin)) -> JSONResponse:
        return JSONResponse({"users": store.list_users()})

    @app.post("/api/admin/users")
    async def admin_create_user(request: Request,
                                _: str = Depends(require_admin)) -> JSONResponse:
        body = await request.json()
        name = body.get("name")
        if not name:
            raise HTTPException(status_code=400, detail="name 必填")
        store.ensure_user(name, body.get("display_name"),
                          body.get("team", ""), body.get("note", ""))
        if body.get("active") is not None:
            store.update_user(name, active=int(bool(body["active"])))
        return JSONResponse({"ok": True, "user": store.get_user(name)})

    @app.put("/api/admin/users/{name}")
    async def admin_update_user(request: Request, name: str,
                                _: str = Depends(require_admin)) -> JSONResponse:
        body = await request.json()
        ok = store.update_user(name, **{k: v for k, v in body.items()
                                        if k in {"display_name", "team", "note",
                                                 "secret", "active"}})
        if not ok:
            raise HTTPException(status_code=404, detail="用户不存在")
        return JSONResponse({"ok": True, "user": store.get_user(name)})

    @app.delete("/api/admin/users/{name}")
    async def admin_delete_user(name: str,
                                _: str = Depends(require_admin)) -> JSONResponse:
        store.delete_user(name)
        return JSONResponse({"ok": True})

    if static_dir is not None and static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


async def run_server(
    *,
    host: str = DEFAULT_HOST,
    tcp_port: int = DEFAULT_PORT,
    web_port: int = WEB_DEFAULT_PORT,
    manager: MatchManager,
    static_dir: Path | None = None,
    max_matches: int | None = None,
) -> None:
    """启动 TCP 平台 + FastAPI web,同进程 asyncio.gather 共存。"""
    import uvicorn  # 延迟 import,CLI 子命令(connect/thp/status)不需要

    app = create_app(manager, static_dir=static_dir)
    config = uvicorn.Config(app, host=host, port=web_port, log_level="warning")
    web_server = uvicorn.Server(config)
    tcp_task = asyncio.create_task(manager.serve_loop(host, tcp_port, max_matches=max_matches))
    web_task = asyncio.create_task(web_server.serve())
    logger.info("pok-arena starting: tcp=%s:%d web=http://%s:%d (max_matches=%s)",
                host, tcp_port, host, web_port, max_matches)
    # 任一完成即停:serve_loop 在 --once/max_matches 跑完后退出 -> 关 web;
    # web 异常也停。长驻(max_matches=None)两者都不退,正常服务。
    done, pending = await asyncio.wait({tcp_task, web_task}, return_when=asyncio.FIRST_COMPLETED)
    web_server.should_exit = True  # 优雅停,避免 cancel 触发 uvicorn lifespan CancelledError
    for t in pending:
        try:
            await asyncio.wait_for(t, timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    for t in done:
        exc = t.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            raise exc
