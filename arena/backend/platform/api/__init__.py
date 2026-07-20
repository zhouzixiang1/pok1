"""对战 + 排行榜 API(里程碑 5 + 6)。

提供 FastAPI router,挂载到主 app 的 ``/api`` 前缀(里程碑 8 main.py 集成)::

    from arena.backend.platform.api import router
    app.include_router(router)

需要 ``app.state.platform_orchestrator``(MatchOrchestrator 实例)注入。
"""
from .routes import router

__all__ = ["router"]
