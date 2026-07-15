"""异步 TCP 竞赛服务器。

严格遵循国赛协议：
  - 平台为服务器端，引擎为客户端
  - 端口 10001
  - 原始短消息，无换行/长度分隔，不依赖 TCP 包边界
  - 60 秒超时 → fold
"""
from __future__ import annotations
import asyncio
import logging
import re
import os
try:
    from ..engine.game import GameEngine, HANDS_PER_MATCH, TIMEOUT_SECONDS
    from ..engine.thp_recorder import THPRecorder
    from .transport import NationalProtocolError, NationalTCPClient
except ImportError:  # Standalone ``cd sever`` compatibility.
    from engine.game import GameEngine, HANDS_PER_MATCH, TIMEOUT_SECONDS
    from engine.thp_recorder import THPRecorder
    from server.transport import NationalProtocolError, NationalTCPClient

_DECK_SEED_RAW = os.environ.get("POK_DECK_SEED_BASE", "")
_deck_seed_base = int(_DECK_SEED_RAW) if _DECK_SEED_RAW.strip().isdigit() else None


def _seeded_deck_factory(hand_num: int):
    """Create a deterministic deck for paired seed block evaluation."""
    from engine.deck import Deck
    return Deck(seed=_deck_seed_base + hand_num if _deck_seed_base is not None else None)

logger = logging.getLogger(__name__)

ClientConnection = NationalTCPClient


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
            # 官方消息集合没有错误文本 token；第三个连接直接关闭，避免
            # 本地平台制造正式 EXE 永远不会发送的线格式。
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
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
            if self._match_task is None or self._match_task.done():
                self._match_task = asyncio.create_task(self.start_match())

    async def start_match(self):
        """开始一场比赛（2 个客户端已连接）。"""
        if len(self.clients) < 2:
            raise RuntimeError("Need 2 clients to start match")

        c0, c1 = self.clients[0], self.clients[1]

        # 交换名称
        await c0.send_message("name")
        await c1.send_message("name")

        try:
            name0 = await c0.recv_name(timeout=30)
            name1 = await c1.recv_name(timeout=30)
        except NationalProtocolError as exc:
            logger.error("Invalid national name handshake: %s", exc)
            return

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
            deck_factory=_seeded_deck_factory if _deck_seed_base is not None else None,
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
                    winner = "平局"
                dt = datetime.now().strftime("%Y%m%d%H%M")
                filename = _safe_record_filename(
                    f"THP-{name0} vs {name1}-{winner}胜-{dt}-CCGC.txt"
                )
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
            await self.clients[player_idx].send_message(message)

    async def _recv_action(self, player_idx: int) -> str | None:
        """GameEngine 调用此方法接收指定玩家的行为。"""
        if player_idx < len(self.clients):
            return await self.clients[player_idx].recv_action(timeout=TIMEOUT_SECONDS)
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


def _safe_record_filename(filename: str) -> str:
    """Return a filesystem-safe THP filename while keeping readable names."""
    return re.sub(r'[\\/:*?"<>|]+', "_", filename)


async def run_tcp_server(host: str, port: int, manager: MatchManager):
    """启动 TCP 服务器。"""
    server = await asyncio.start_server(
        manager.handle_new_connection, host, port,
    )
    addr = server.sockets[0].getsockname()
    logger.info(f"TCP server listening on {addr[0]}:{addr[1]}")
    async with server:
        await server.serve_forever()
