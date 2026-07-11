"""arena 平台 TCP 接入层。

长驻 TCP server,按连接顺序接两个外部 bot 引擎(p0=桌面下方/先连,
p1=桌面上方/后连),做 name 握手后交给 MatchManager 编排一场 70 局。

基于 pok1 origin/main (a334b9ff) web/core/national_native.py 的
``_run_tcp_server_with_processes`` (L2187-2483) 改造:
- 删 bot 子进程 spawn(arena 不拉 bot,外部引擎自连)
- host/port 从硬编码 127.0.0.1:0 改参数注入(CLI serve 默认 50101/127.0.0.1)
- 删 9 个 pok 专用 import / 决策遥测 / 合规统计(bot_namespace / eval_stats /
  national_runtime_telemetry / national_bot_launcher / pipeline_schema /
  runtime_capacity / strength_order)
- 加 event_sink(实时事件 -> SSE)、re-arm(一场结束 close_clients 清列表
  回 listening,等下一对 bot)
- 第 3 连接发 ``error: match full`` 关闭(HANDOFF §118)
- name 握手失败/重名不抛(长驻平台不因单场失败退出),返回 None 由
  MatchManager 决定 re-arm 或退出

仅接入层;比赛循环 / 断线判负 / THP 落盘 / SSE 桥接在 MatchManager。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .transport import NationalProtocolError, NationalTCPClient

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 50101
DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_NAME_TIMEOUT = 30.0
DEFAULT_IDLE_FLUSH_SEC = 0.003

EventSink = Callable[[dict[str, Any]], Awaitable[None] | None]


async def emit_event(event_sink: EventSink | None, event_type: str, data: dict[str, Any]) -> None:
    """向 SSE / MatchManager 推一个事件(sink 可同步也可异步)。"""
    if event_sink is None:
        return
    event = {"type": event_type, **data}
    result = event_sink(event)
    if asyncio.iscoroutine(result):
        await result


class ArenaTCPServer:
    """平台 TCP 接入层:长驻 server + clients 列表 + name 握手。

    生命周期由 MatchManager 编排::

        srv = ArenaTCPServer(host=, port=, event_sink=)
        await srv.start()                       # 绑端口, 回 listening
        ok = await srv.wait_for_clients(2)      # 等两个 bot 连入
        names = await srv.handshake_names()     # 发 name, 收队名
        # ... MatchManager 用 srv.clients 跑 NationalTCPGameEngine ...
        await srv.close_clients()               # 一场结束: 清 clients, re-arm
        # ... 下一场 wait_for_clients ...
        await srv.stop()                        # 进程退出

    座位:``clients[0]``=桌面下方(先连), ``clients[1]``=桌面上方(后连),不重排
    (HANDOFF §118)。三件套之二 transport/game_runtime 见同包。
    """

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        event_sink: EventSink | None = None,
        idle_flush_sec: float = DEFAULT_IDLE_FLUSH_SEC,
    ) -> None:
        self.host = host
        self.port = port
        self.event_sink = event_sink
        self.idle_flush_sec = idle_flush_sec
        self.clients: list[NationalTCPClient] = []
        self._server: asyncio.base_events.Server | None = None
        self._connected = asyncio.Event()

    @property
    def actual_addr(self) -> tuple[str, int] | None:
        if self._server is None or not self._server.sockets:
            return None
        return self._server.sockets[0].getsockname()[:2]

    async def start(self) -> tuple[str, int]:
        """绑定 host:port,开始 listening。返回实际绑定地址。"""
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        host, port = self.actual_addr or (self.host, self.port)
        logger.info("ArenaTCPServer listening on %s:%d", host, port)
        await emit_event(self.event_sink, "server_started", {"host": host, "port": port})
        return host, port

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # 第 3+ 连接: 发 'error: match full' 并关闭(HANDOFF §118)。
        if len(self.clients) >= 2:
            try:
                writer.write(b"error: match full\n")
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            logger.info("rejected extra connection (match full)")
            await emit_event(self.event_sink, "connection_rejected", {"reason": "match_full"})
            return
        client = NationalTCPClient(
            reader,
            writer,
            idle_flush_sec=self.idle_flush_sec,
            wire_sink=self.event_sink,
        )
        idx = len(self.clients)
        self.clients.append(client)
        seat = "lower" if idx == 0 else "upper"
        await emit_event(self.event_sink, "connected", {
            "client_idx": idx,
            "addr": writer.get_extra_info("peername"),
            "seat": seat,
        })
        logger.info("client %d connected from %s (seat=%s)",
                    idx, writer.get_extra_info("peername"), seat)
        if len(self.clients) == 2:
            self._connected.set()

    async def wait_for_clients(self, count: int = 2, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> bool:
        """等 count 个 bot 连入。超时返回 False。"""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def handshake_names(self, timeout: float = DEFAULT_NAME_TIMEOUT) -> list[str] | None:
        """向双方发 ``name``,收队名。成功返回 ``[name0, name1]``,失败返回 None。

        失败情形:超时 / 断开 / 空名 / 超长 / 控制字符(NationalProtocolError) / 重名。
        长驻平台不因单场握手失败退出;MatchManager 据 None 决定 re-arm 或退出。
        """
        if len(self.clients) < 2:
            return None
        try:
            await self.clients[0].send_line("name")
            await self.clients[1].send_line("name")
            name0 = await self.clients[0].recv_name(timeout=timeout)
            name1 = await self.clients[1].recv_name(timeout=timeout)
        except NationalProtocolError as exc:
            logger.warning("name handshake protocol error: %s", exc)
            return None
        except (ConnectionError, OSError) as exc:
            logger.warning("name handshake connection error: %s", exc)
            return None
        if not name0 or not name1:
            logger.warning("name handshake timeout/empty: %r / %r", name0, name1)
            return None
        if name0 == name1:
            logger.warning("duplicate team name rejected: %s", name0)
            return None
        self.clients[0].name = name0
        self.clients[1].name = name1
        await emit_event(self.event_sink, "names", {"names": [name0, name1]})
        return [name0, name1]

    async def close_clients(self) -> None:
        """一场结束:关所有 client 并清空列表、复位连接事件(re-arm 下一场)。"""
        for client in self.clients:
            try:
                await client.close()
            except (ConnectionError, OSError):
                pass
        self.clients.clear()
        self._connected.clear()

    async def stop(self) -> None:
        """关闭 server 与所有连接(进程退出用)。"""
        await self.close_clients()
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, OSError):
                pass
            self._server = None
        logger.info("ArenaTCPServer stopped")
        await emit_event(self.event_sink, "server_stopped", {})
