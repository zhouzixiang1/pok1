"""德州扑克对弈平台 — 统一入口。

并发启动：
  - TCP 服务器 (:10001) — 接受引擎客户端连接
  - Web 仪表板 (:18080) — 实时比赛展示

用法:
  python main.py                          # 默认 TCP :10001, Web :18080
  python main.py --tcp-port 20001         # 自定义 TCP 端口
  python main.py --web-port 28080         # 自定义 Web 端口
  python main.py --host 0.0.0.0          # 监听地址
"""
import argparse
import asyncio
import logging
import sys
import os

# 确保可以 import 本目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server.tcp_server import MatchManager, run_tcp_server
from web.app import create_app

import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="德州扑克对弈平台")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    parser.add_argument("--tcp-port", type=int, default=10001, help="TCP 端口 (默认 10001)")
    parser.add_argument("--web-port", type=int, default=18080, help="Web 端口 (默认 18080)")
    return parser.parse_args()


async def main():
    args = parse_args()
    manager = MatchManager()

    # 创建 FastAPI app（注入 MatchManager）
    app = create_app(manager)

    # 并发启动 TCP + Web
    tcp_coro = run_tcp_server(args.host, args.tcp_port, manager)
    web_config = uvicorn.Config(app, host=args.host, port=args.web_port,
                                log_level="warning")
    web_server = uvicorn.Server(web_config)

    logger.info(f"启动 TCP 服务器 {args.host}:{args.tcp_port}")
    logger.info(f"启动 Web 仪表板 http://{args.host}:{args.web_port}")

    await asyncio.gather(
        tcp_coro,
        web_server.serve(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("服务器关闭")
