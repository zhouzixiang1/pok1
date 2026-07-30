"""新平台 FastAPI 应用工厂 + uvicorn 启动。"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router as api_router
from .auth import AuthManager
from .auth.captcha import CaptchaStore
from .auth.routes import router as auth_router
from .mail import Mailer
from .mail.routes import router as mail_admin_router
from .runtime import BotManager
from .runtime.docker_runner import DockerRunner
from .runtime.orchestrator import MatchOrchestrator
from .runtime.routes import router as bots_router
from .security import (
    RateLimitMiddleware, SecurityHeadersMiddleware, security_settings,
)
from .store import DEFAULT_DB_PATH, Store, ROLE_ADMIN

logger = logging.getLogger(__name__)

WEB_DEFAULT_PORT = 50280
SYSTEM_USERNAME = "system"


def _load_dotenv(path: Path) -> None:
    """轻量加载 .env(不覆盖已有环境变量)。"""
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip("'").strip('"')
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


def _ensure_system_user(store: Store) -> int:
    user = store.get_user_by_username(SYSTEM_USERNAME)
    if user:
        if not user.get("email_verified"):
            store.update_user(user["id"], email_verified=1)
        return user["id"]
    user = store.create_user(
        SYSTEM_USERNAME, "system@arena.local", "!not_loginable",
        role=ROLE_ADMIN, display_name="系统")
    store.update_user(user["id"], email_verified=1, is_active=0)
    return user["id"]


def create_platform_app(*, db_path: str | Path = DEFAULT_DB_PATH,
                        static_dir: Path | None = None,
                        upload_root: Path | str = "bot_uploads"
                        ) -> FastAPI:
    # 尽量在 app 构造前加载项目根 .env
    root = Path(__file__).resolve().parents[3]
    _load_dotenv(root / ".env")

    app = FastAPI(title="pok-arena platform",
                  description="botzone 风格德州扑克持久化对战平台")

    store = Store(str(db_path))
    mailer = Mailer()
    auth = AuthManager(store, mailer=mailer)
    captcha = CaptchaStore()
    bot_manager = BotManager(store, upload_root=upload_root)
    docker_runner = DockerRunner()
    max_conc = int(os.environ.get("POK_PLATFORM_MAX_CONCURRENT_MATCHES", "2"))
    orchestrator = MatchOrchestrator(
        store, docker_runner, bot_manager=bot_manager,
        max_concurrent=max_conc)
    system_uid = _ensure_system_user(store)

    app.state.platform_store = store
    app.state.platform_auth = auth
    app.state.platform_mailer = mailer
    app.state.platform_captcha = captcha
    app.state.platform_bot_manager = bot_manager
    app.state.platform_orchestrator = orchestrator
    app.state.platform_docker_runner = docker_runner
    app.state.platform_system_user_id = system_uid

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)

    app.include_router(auth_router)
    app.include_router(bots_router)
    app.include_router(api_router)
    app.include_router(mail_admin_router)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse({
            "ok": True,
            "service": "pok-arena platform",
            "security": security_settings(),
            "smtp_configured": mailer.config.configured,
        })

    if static_dir is not None and static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True),
                  name="static")

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
    import uvicorn

    root = Path(__file__).resolve().parents[3]
    _load_dotenv(root / ".env")

    app = create_platform_app(db_path=db_path, static_dir=static_dir,
                              upload_root=upload_root)
    if register_builtin:
        bm: BotManager = app.state.platform_bot_manager
        system_uid: int = app.state.platform_system_user_id
        try:
            bots = bm.register_builtin_bots(system_uid)
            logger.info("registered %d builtin bots", len(bots))
        except Exception:
            logger.exception("register builtin bots failed")

    config = uvicorn.Config(app, host=host, port=web_port, log_level="warning")
    server = uvicorn.Server(config)
    logger.info("pok-arena platform starting: web=http://%s:%d (db=%s)",
                host, web_port, db_path)
    try:
        await server.serve()
    finally:
        runner: DockerRunner = app.state.platform_docker_runner
        try:
            await runner.cleanup_all()
        except Exception:
            pass
