"""异步 TCP 竞赛服务器。

严格遵循国赛协议：
  - 平台为服务器端，引擎为客户端
  - 端口 10001
  - 行分隔文本协议
  - 60 秒超时 → fold
"""
from __future__ import annotations
import asyncio
import logging
from engine.game import GameEngine, HANDS_PER_MATCH, TIMEOUT_SECONDS
from engine.thp_recorder import THPRecorder

logger = logging.getLogger(__name__)


class ClientConnection:
    """管理单个 TCP 客户端连接。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.name = ""
        self._buffer = ""
        self._closed = False

    async def send_line(self, msg: str) -> bool:
        """发送一行消息，返回是否成功。"""
        if self._closed:
            return False
        try:
            self.writer.write((msg + "\n").encode("utf-8"))
            await self.writer.drain()
            return True
        except (ConnectionResetError, BrokenPipeError, OSError):
            self._closed = True
            return False

    async def recv_line(self, timeout: float = TIMEOUT_SECONDS) -> str | None:
        """接收一行消息，超时返回 None。"""
        if self._closed:
            return None
        try:
            # 检查缓冲区
            if "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                return line.strip()

            # 等待数据
            async with asyncio.timeout(timeout):
                while "\n" not in self._buffer:
                    data = await self.reader.read(4096)
                    if not data:
                        self._closed = True
                        return None
                    self._buffer += data.decode("utf-8")

            line, self._buffer = self._buffer.split("\n", 1)
            return line.strip()
        except (asyncio.TimeoutError, ConnectionResetError, OSError):
            return None

    async def close(self):
        self._closed = True
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except OSError:
            pass


class MatchManager:
    """管理 TCP 连接 + 比赛生命周期。"""

    def __init__(self, broadcast_func=None):
        self.clients: list[ClientConnection] = []
        self.engine: GameEngine | None = None
        self.broadcast = broadcast_func
        self._connected_event = asyncio.Event()
        self._match_task: asyncio.Task | None = None

    async def handle_new_connection(self, reader, writer):
        """处理新的客户端连接。"""
        addr = writer.get_extra_info("peername")
        logger.info(f"Client connected from {addr}")

        if len(self.clients) >= 2:
            # 已有 2 个连接，拒绝
            writer.write(b"error: match full\n")
            await writer.drain()
            writer.close()
            return

        client = ClientConnection(reader, writer)
        self.clients.append(client)

        if self.broadcast:
            await self.broadcast({
                "type": "connected",
                "client_idx": len(self.clients) - 1,
                "addr": str(addr),
            })

        if len(self.clients) == 2:
            self._connected_event.set()

    async def start_match(self):
        """开始一场比赛（2 个客户端已连接）。"""
        if len(self.clients) < 2:
            raise RuntimeError("Need 2 clients to start match")

        c0, c1 = self.clients[0], self.clients[1]

        # 交换名称
        await c0.send_line("name")
        await c1.send_line("name")

        name0 = await c0.recv_line(timeout=30)
        name1 = await c1.recv_line(timeout=30)

        if name0 is None or name1 is None:
            logger.error("Failed to get player names")
            return

        c0.name = name0
        c1.name = name1
        logger.info(f"Player 0: {name0}, Player 1: {name1}")

        # 国赛平台要求英文名称，非英文发出警告
        for i, name in enumerate([name0, name1]):
            if not name.isascii():
                logger.warning(f"Player {i} name '{name}' contains non-ASCII characters")

        if self.broadcast:
            await self.broadcast({
                "type": "names",
                "names": [name0, name1],
            })

        # 创建 THP 棋谱记录器
        recorder = THPRecorder(team_a_name=name0, team_b_name=name1)

        # 创建游戏引擎
        engine = GameEngine(
            send_func=self._send_to_client,
            broadcast_func=self.broadcast,
            recorder=recorder,
        )
        self.engine = engine

        # Monkey-patch recv_action
        engine._recv_action = self._recv_action

        try:
            await engine.run_match(name0, name1)
        except Exception as e:
            logger.error(f"Match error: {e}", exc_info=True)
            if self.broadcast:
                await self.broadcast({"type": "error", "message": str(e)})
        else:
            # 比赛正常结束，导出 THP 棋谱
            if recorder.records:
                import os
                from datetime import datetime
                os.makedirs("records", exist_ok=True)
                winner = name0 if engine.total_earnings[0] > engine.total_earnings[1] else name1
                if engine.total_earnings[0] == engine.total_earnings[1]:
                    winner = f"{name0}={name1}"
                dt = datetime.now().strftime("%Y%m%d%H%M")
                filename = f"THP-{name0} vs {name1}-{winner}胜-{dt}.txt"
                filepath = os.path.join("records", filename)
                try:
                    recorder.export_file(filepath)
                    if self.broadcast:
                        await self.broadcast({
                            "type": "thp_exported",
                            "filepath": filepath,
                            "hands": len(recorder.records),
                        })
                except Exception as e:
                    logger.error(f"THP export error: {e}")
        finally:
            await c0.close()
            await c1.close()

    async def _send_to_client(self, player_idx: int, message: str):
        """GameEngine 调用此方法发送消息给指定玩家。"""
        if player_idx < len(self.clients):
            await self.clients[player_idx].send_line(message)

    async def _recv_action(self, player_idx: int) -> str | None:
        """GameEngine 调用此方法接收指定玩家的行为。"""
        if player_idx < len(self.clients):
            return await self.clients[player_idx].recv_line(timeout=TIMEOUT_SECONDS)
        return None

    async def reset(self):
        """重置比赛状态。"""
        for c in self.clients:
            await c.close()
        self.clients.clear()
        self.engine = None
        self._connected_event.clear()
        if self._match_task and not self._match_task.done():
            self._match_task.cancel()
        logger.info("Match reset")

    def get_state(self) -> dict:
        """获取当前比赛状态（供 Web API 使用）。"""
        if self.engine is None:
            return {
                "status": "waiting",
                "clients": len(self.clients),
                "hand_num": 0,
            }
        return {
            "status": "playing",
            "clients": 2,
            "names": [c.name for c in self.clients],
            "hand_num": self.engine.hand_num,
            "total_earnings": list(self.engine.total_earnings),
            "hands_per_match": HANDS_PER_MATCH,
        }


async def run_tcp_server(host: str, port: int, manager: MatchManager):
    """启动 TCP 服务器。"""
    server = await asyncio.start_server(
        manager.handle_new_connection, host, port,
    )
    addr = server.sockets[0].getsockname()
    logger.info(f"TCP server listening on {addr[0]}:{addr[1]}")
    async with server:
        await server.serve_forever()
