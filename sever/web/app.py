"""FastAPI Web 仪表板。

端口 :18080，提供实时比赛展示界面。
通过 SSE 推送事件：connected, names, hand_start, stage, action, settle, match_end, error
"""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(match_manager) -> FastAPI:
    """创建 FastAPI 应用，注入 MatchManager 实例。"""
    app = FastAPI(title="德州扑克对弈平台")

    # SSE 客户端管理
    _clients: list[asyncio.Queue] = []

    async def broadcast(event: dict):
        """广播事件给所有 SSE 客户端。"""
        data = json.dumps(event, ensure_ascii=False)
        dead = []
        for i, q in enumerate(_clients):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(i)
        for i in reversed(dead):
            _clients.pop(i)

    # 将 broadcast 注入 MatchManager
    match_manager.broadcast = broadcast

    # ── 静态文件 ──
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── 路由 ──

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/state")
    async def get_state():
        return JSONResponse(match_manager.get_state())

    @app.get("/api/events")
    async def sse_stream(request: Request):
        """SSE 实时事件流。"""
        queue = asyncio.Queue(maxsize=200)
        _clients.append(queue)

        async def generate():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {data}\n\n"
                    except asyncio.TimeoutError:
                        yield f": keepalive\n\n"
            finally:
                if queue in _clients:
                    _clients.remove(queue)

        from starlette.responses import StreamingResponse
        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.post("/api/start")
    async def start_match():
        """开始比赛（需已连接 2 个客户端）。"""
        if len(match_manager.clients) < 2:
            return JSONResponse({"error": "需要 2 个客户端连接"}, status_code=400)
        if match_manager.engine is not None:
            return JSONResponse({"error": "比赛进行中"}, status_code=400)

        # 在后台启动比赛
        asyncio.create_task(match_manager.start_match())
        return JSONResponse({"status": "started"})

    @app.post("/api/reset")
    async def reset_match():
        await match_manager.reset()
        return JSONResponse({"status": "reset"})

    return app
