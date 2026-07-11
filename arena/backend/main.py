"""arena FastAPI 应用 + uvicorn/TCP 单进程共存。

参考 pok1 ``sever/main.py``:``uvicorn.Server(config).serve()`` +
``asyncio.gather(tcp_coro, ...)`` 让 FastAPI 与 TCP server 同 loop 共存
(**勿用阻塞** ``uvicorn.run`` — 它自起 event loop 阻塞主线程,后续 start_server
/gather 无机会调度)。SSE 桥接参考 ``sever/web/app.py``(Queue 扇出 + keepalive +
is_disconnected 清理),增强:``/api/arena/events`` 首帧发 snapshot、``/api/matches``
THP 索引、``/api/matches/{id}/thp`` 取棋谱。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .server.match_manager import MatchManager, SSE_KEEPALIVE_SEC
from .server.tcp_server import DEFAULT_HOST, DEFAULT_PORT

logger = logging.getLogger(__name__)

WEB_DEFAULT_PORT = 50180


def create_app(manager: MatchManager, *, static_dir: Path | None = None) -> FastAPI:
    """构造 FastAPI app,注入 MatchManager 单例。

    static_dir 给定时挂载前端构建产物(SPA, html=True);API 路由先于 mount 注册,
    ``/api/*`` 不会被静态捕获。
    """
    app = FastAPI(title="pok-arena", version="0.1.0",
                  description="国赛德州扑克对弈平台 web 复刻")

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        return JSONResponse(manager.get_snapshot())

    @app.get("/api/arena/events")
    async def arena_events(request: Request) -> StreamingResponse:
        queue = manager.subscribe()
        snapshot = manager.get_snapshot()

        async def gen():
            try:
                # 首帧:snapshot(前端 EventSource 重连靠它恢复全场状态)
                yield f"data: {json.dumps(snapshot, ensure_ascii=False, default=str)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SEC)
                        yield f"data: {data}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"  # SSE 注释行,过中间代理超时
            finally:
                manager.unsubscribe(queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/matches")
    async def list_matches() -> JSONResponse:
        return JSONResponse({"matches": manager.list_matches()})

    @app.get("/api/matches/{match_id}/thp")
    async def get_thp(match_id: str) -> JSONResponse:
        text = manager.read_thp(match_id)
        if text is None:
            raise HTTPException(status_code=404, detail="match thp not found")
        return JSONResponse({"match_id": match_id, "thp": text})

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
    tcp_coro = manager.serve_loop(host, tcp_port, max_matches=max_matches)
    logger.info("pok-arena starting: tcp=%s:%d web=http://%s:%d (max_matches=%s)",
                host, tcp_port, host, web_port, max_matches)
    await asyncio.gather(tcp_coro, web_server.serve())
