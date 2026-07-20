"""新平台:botzone 风格持久化对战平台。

与 TCP 通道(arena/backend/server/,serve 命令)隔离。入口 serve-web(端口 50280)。

里程碑构成:
- store(里程碑1):SQLite users/bots/matches/replays/ratings
- auth(里程碑2):注册/登录/重置密码/role
- runtime(里程碑3-4):bot 上传/Docker 构建/双协议适配/对战引擎
- api + orchestrator(里程碑5-6):对战编排/SSE 观赛/排行榜 Glicko-2
- runtime/replay(里程碑7):事件流→逐手快照回放
"""
from .main import WEB_DEFAULT_PORT, create_platform_app, run_platform_server

__all__ = ["create_platform_app", "run_platform_server", "WEB_DEFAULT_PORT"]
