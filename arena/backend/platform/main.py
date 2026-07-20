"""新平台 FastAPI 应用工厂 + uvicorn 启动(里程碑 8a)。

挂载所有里程碑 2-7 的 router(auth/bots/matches/leaderboard/replay),
与现有 TCP 通道的 ``arena/backend/main.py``(serve 命令)**隔离**:
- 旧 ``serve``:TCP 平台 50101 + 旧 web 50180(冻结只读)
- 新 ``serve-web``:新平台 web(同进程,web + Docker runner pool)

端口策略(不损坏现有服务):
- 默认 web 端口 **50280**(新平台独立端口,与旧 50180 区分)
- 可 --web-port 覆盖
- 绑定 127.0.0.1(安全基线,--host 0.0.0.0 显式告警)

app.state 注入:
- platform_store / platform_auth / platform_bot_manager / platform_orchestrator
- platform_system_user_id(内置 bot 的 owner)
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router as api_router
from .auth import AuthManager
from .auth.routes import router as auth_router
from .runtime import BotManager
from .runtime.docker_runner import DockerRunner
from .runtime.bot_manager import BUILTIN_SOURCE_ROOT
from .runtime.routes import router as bots_router
from .runtime.orchestrator import MatchOrchestrator
from .store import DEFAULT_DB_PATH, Store, ROLE_ADMIN

logger = logging.getLogger(__name__)

# 新平台默认端口(与旧 web 50180 区分,避免冲突)
WEB_DEFAULT_PORT = 50280
SYSTEM_USERNAME = "system"


def _ensure_system_user(store: Store) -> int:
    """确保 system 用户存在(内置 bot 的 owner + admin 操作主体)。返回其 id。"""
    user = store.get_user_by_username(SYSTEM_USERNAME)
    if user:
        return user["id"]
    # system 用户不可登录(password_hash 占位,is_active=0 但 role=admin)
    user = store.create_user(SYSTEM_USERNAME, "system@arena.local", "!not_loginable",
                             role=ROLE_ADMIN, display_name="系统")
    return user["id"]


def create_platform_app(*, db_path: str | Path = DEFAULT_DB_PATH,
                        static_dir: Path | None = None,
                        upload_root: Path | str = "bot_uploads"
                        ) -> FastAPI:
    """构造新平台 FastAPI app。注入 Store/Auth/BotManager/Orchestrator。"""
    app = FastAPI(title="pok-arena platform",
                  description="botzone 风格德州扑克持久化对战平台")

    store = Store(str(db_path))
    auth = AuthManager(store)
    bot_manager = BotManager(store, upload_root=upload_root)
    docker_runner = DockerRunner()
    orchestrator = MatchOrchestrator(store, docker_runner, bot_manager=bot_manager)
    system_uid = _ensure_system_user(store)

    app.state.platform_store = store
    app.state.platform_auth = auth
    app.state.platform_bot_manager = bot_manager
    app.state.platform_orchestrator = orchestrator
    app.state.platform_docker_runner = docker_runner
    app.state.platform_system_user_id = system_uid

    # 挂载路由
    app.include_router(auth_router)
    app.include_router(bots_router)
    app.include_router(api_router)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True, "service": "pok-arena platform"})

    # 前端静态文件(SPA,html=True);API 路由已先注册,不被静态捕获
    if static_dir is not None and static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


async def run_platform_server(
    *,
    host: str = "127.0.0.1",
    web_port: int = WEB_DEFAULT_PORT,
    db_path: str | Path = DEFAULT_DB_PATH,
    static_dir: Path | None = None,
    upload_root: Path | str = "bot_uploads",
    register_builtin: bool = True,
) -> None:
    """启动新平台 web(单进程 web + Docker runner pool)。"""
    import uvicorn  # 延迟 import

    app = create_platform_app(db_path=db_path, static_dir=static_dir,
                              upload_root=upload_root)
    # 启动时注册内置 bot 库(幂等)
    if register_builtin:
        bm: BotManager = app.state.platform_bot_manager
        system_uid: int = app.state.platform_system_user_id
        try:
            bots = bm.register_builtin_bots(system_uid)
            logger.info("registered %d builtin bots", len(bots))
        except Exception:  # 不阻塞启动
            logger.exception("register builtin bots failed")

    config = uvicorn.Config(app, host=host, port=web_port, log_level="warning")
    server = uvicorn.Server(config)
    logger.info("pok-arena platform starting: web=http://%s:%d (db=%s)",
                host, web_port, db_path)
    # 优雅停:关所有 docker session
    try:
        await server.serve()
    finally:
        runner: DockerRunner = app.state.platform_docker_runner
        try:
            await runner.cleanup_all()
        except Exception:
            pass
