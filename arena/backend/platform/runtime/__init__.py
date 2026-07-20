"""新平台运行时:bot 上传/Docker 构建/对战执行(里程碑 3-4)。"""
from .bot_manager import BotManager
from .builder import BotBuildError, build_bot_image, make_dockerfile
from .docker_runner import DockerRunner
from .match_runner import MatchRunner

__all__ = [
    "BotManager", "BotBuildError",
    "build_bot_image", "make_dockerfile",
    "DockerRunner", "MatchRunner",
]
