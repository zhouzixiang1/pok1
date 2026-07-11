"""arena 比赛编排 + SSE 桥接 + THP 落盘。

MatchManager 单例(单桌单场):持有 ArenaTCPServer,循环接 bot / 跑 70 局 /
re-arm 等下一对;维护 per-hand 事件缓存;THP 每手 settle 后增量 append
(崩溃不丢整场);断线连续 2 手判 forfeit(**仅 TCP 真断开累计,60s 超时只 fold**,
arena 协议决策 3);事件扇出到 SSE 订阅者(asyncio.Queue + put_nowait),
``/api/arena/events`` 首帧发 snapshot。

设计参考 pok1 ``sever/main.py``(FastAPI+uvicorn 协程共存) +
``sever/web/app.py``(SSE Queue 扇出 + keepalive + is_disconnected 清理),
增强:断线判负 + THP 增量 + snapshot 首帧 + index.json。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .game_runtime import NationalTCPGameEngine
from .tcp_server import ArenaTCPServer

logger = logging.getLogger(__name__)

HANDS_PER_MATCH = 70
ACTION_TIMEOUT_SEC = 60.0
CONNECT_TIMEOUT_SEC = 20.0
NAME_TIMEOUT_SEC = 30.0
SSE_QUEUE_MAXSIZE = 512
SSE_KEEPALIVE_SEC = 15.0
DEFAULT_RECORDS_DIR = Path("~/.local/share/pok-arena/records").expanduser()


class MatchManager:
    """单桌单场编排器。

    生命周期:外部(``main.run_server``)调 ``serve_loop`` 长驻;SSE 端点调
    ``subscribe`` / ``unsubscribe`` / ``get_snapshot``;CLI thp 命令调
    ``list_matches`` / ``read_thp``。
    """

    def __init__(
        self,
        *,
        records_dir: Path | str | None = None,
        event_name: str = "CCGC",
        location: str = "",
        hands_per_match: int = HANDS_PER_MATCH,
        action_timeout_sec: float = ACTION_TIMEOUT_SEC,
        connect_timeout_sec: float = CONNECT_TIMEOUT_SEC,
        name_timeout_sec: float = NAME_TIMEOUT_SEC,
    ) -> None:
        self.records_dir = Path(records_dir) if records_dir else DEFAULT_RECORDS_DIR
        self.event_name = event_name
        self.location = location
        self.hands_per_match = hands_per_match
        self.action_timeout_sec = action_timeout_sec
        self.connect_timeout_sec = connect_timeout_sec
        self.name_timeout_sec = name_timeout_sec

        self.server: ArenaTCPServer | None = None
        self.engine: NationalTCPGameEngine | None = None

        self._subscribers: list[asyncio.Queue[str]] = []
        self._event_log: list[dict[str, Any]] = []
        self._running = False
        self._stopping = False

        # 当前比赛状态
        self._match_id: str | None = None
        self._match_started_at: datetime | None = None
        self._thp_path: Path | None = None
        self._names: list[str] = []
        self._disconnect_streak = [0, 0]
        self._forfeit_reason: str | None = None
        self._forfeit_loser: int | None = None
        self._matches_played = 0

    # ── SSE 订阅 / 扇出 ──────────────────────────────────────

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=SSE_QUEUE_MAXSIZE)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _broadcast(self, event: dict[str, Any]) -> None:
        data = json.dumps(event, ensure_ascii=False, default=str)
        dead: list[int] = []
        for i, q in enumerate(self._subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(i)
        for i in reversed(dead):
            self._subscribers.pop(i)

    def get_snapshot(self) -> dict[str, Any]:
        """当前完整状态(SSE 首帧 + /api/state)。"""
        engine = self.engine
        return {
            "type": "snapshot",
            "status": self._status(),
            "match_id": self._match_id,
            "names": list(self._names),
            "matches_played": self._matches_played,
            "hands_per_match": self.hands_per_match,
            "hand_num": getattr(engine, "hand_num", 0) if engine else 0,
            "total_earnings": list(getattr(engine, "total_earnings", [0, 0])) if engine else [0, 0],
            "server_addr": list(self.server.actual_addr) if (self.server and self.server.actual_addr) else None,
            "connected_clients": len(self.server.clients) if self.server else 0,
            "disconnect_streak": list(self._disconnect_streak),
            "recent_events": self._event_log[-30:],
        }

    def _status(self) -> str:
        if self._stopping:
            return "stopping"
        if self.engine is not None:
            return "playing"
        if self.server is not None and len(self.server.clients) > 0:
            return "waiting_clients"
        if self.server is not None:
            return "listening"
        return "idle"

    # ── 比赛循环 ─────────────────────────────────────────────

    async def serve_loop(
        self,
        host: str,
        tcp_port: int,
        max_matches: int | None = None,
    ) -> None:
        """长驻:接 bot -> 跑一场 -> re-arm -> 下一对,直到 max_matches 或 stop()。

        max_matches=None 长驻(现场);max_matches=N/--once 跑完 N 场或 connect 超时
        无人连则退(CLI 可机械化结束)。
        """
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.server = ArenaTCPServer(host=host, port=tcp_port, event_sink=self._on_event)
        await self.server.start()
        self._running = True
        self._stopping = False
        logger.info("MatchManager serve_loop on %s:%d (max_matches=%s)", host, tcp_port, max_matches)
        try:
            while not self._stopping:
                if max_matches is not None and self._matches_played >= max_matches:
                    break
                got = await self.server.wait_for_clients(2, timeout=self.connect_timeout_sec)
                if not got:
                    if self._stopping:
                        break
                    if max_matches is not None:
                        logger.info("no clients within %.0fs, exiting (finite mode)",
                                    self.connect_timeout_sec)
                        break
                    continue
                if self._stopping:
                    break
                names = await self.server.handshake_names(timeout=self.name_timeout_sec)
                if names is None:
                    await self.server.close_clients()
                    continue
                await self._run_one_match(names)
                await self.server.close_clients()  # re-arm
        finally:
            self._running = False
            if self.server is not None:
                await self.server.stop()
            logger.info("MatchManager serve_loop ended (matches_played=%d)", self._matches_played)

    async def _run_one_match(self, names: list[str]) -> None:
        self._names = list(names)
        self._match_id = self._make_match_id(names)
        self._match_started_at = datetime.now()
        self._thp_path = self.records_dir / f"{self._match_id}.thp"
        self._disconnect_streak = [0, 0]
        self._forfeit_reason = None
        self._forfeit_loser = None
        self._event_log = []
        self._init_thp_file()

        await self._broadcast({
            "type": "match_start",
            "match_id": self._match_id,
            "names": list(names),
            "hands": self.hands_per_match,
            "started_at": self._match_started_at.isoformat(timespec="seconds"),
        })

        self.engine = NationalTCPGameEngine(
            self.server.clients,
            [],
            action_timeout_sec=self.action_timeout_sec,
            event_sink=self._on_event,
        )
        try:
            await self.engine.run_limited_match(names[0], names[1], self.hands_per_match)
        except Exception as exc:  # 长驻平台不能因单场异常退出
            logger.exception("match %s crashed", self._match_id)
            await self._broadcast({
                "type": "match_end", "reason": "error",
                "detail": f"{type(exc).__name__}: {exc}",
                "names": list(names), "match_id": self._match_id,
            })
        self._finalize_thp()
        self._update_index(names)
        self._matches_played += 1
        self.engine = None

    # ── 事件处理(断线判负 + THP 增量 + 广播)─────────────────

    async def _on_event(self, event: dict[str, Any]) -> None:
        # wire 级遥测(NationalTCPClient._notify: 含 direction 的 chunk/message)
        # 量大且前端不需要,不广播、不入 per-hand log。
        if "direction" in event:
            return
        etype = event.get("type")
        if etype == "settle":
            self._update_disconnect_streak()
            self._append_thp_hand()
        # match_end 注入 forfeit 原因(engine 发的 match_end 在此增强 reason)。
        if etype == "match_end" and self._forfeit_reason:
            event = {**event, "reason": self._forfeit_reason, "loser_idx": self._forfeit_loser}
        self._event_log.append(event)
        if len(self._event_log) > 5000:
            self._event_log = self._event_log[-3000:]
        await self._broadcast(event)

    def _update_disconnect_streak(self) -> None:
        """每手 settle 后检查 client.closed:仅真断开累计,连续 2 手 -> forfeit。

        超时(client 未 closed)不累计(只当手 fold)。arena 协议决策 3。
        """
        if self._forfeit_reason or self.engine is None or self.server is None:
            return
        if len(self.server.clients) < 2:
            return
        for i, client in enumerate(self.server.clients):
            if client.closed:
                self._disconnect_streak[i] += 1
            else:
                self._disconnect_streak[i] = 0
        for i in range(2):
            if self._disconnect_streak[i] >= 2:
                self._forfeit_reason = "disconnected"
                self._forfeit_loser = i
                self.engine.match_over = True
                name = self._names[i] if i < len(self._names) else "?"
                logger.warning("player %d (%s) forfeit: disconnected 2 hands in a row", i, name)
                break

    # ── THP 增量落盘 ─────────────────────────────────────────

    def _init_thp_file(self) -> None:
        if self._thp_path is None:
            return
        try:
            self._thp_path.write_text("", encoding="gb2312", errors="replace")
        except OSError as exc:
            logger.warning("init thp file failed: %s", exc)

    def _append_thp_hand(self) -> None:
        engine = self.engine
        if engine is None or engine.recorder is None or self._thp_path is None:
            return
        if not engine.recorder.records:
            return
        try:
            line = engine.recorder.format_hand(engine.recorder.records[-1])
            with open(self._thp_path, "a", encoding="gb2312", errors="replace") as f:
                f.write(line + "\n")
        except OSError as exc:
            logger.warning("append thp failed: %s", exc)

    def _finalize_thp(self) -> None:
        engine = self.engine
        if engine is None or engine.recorder is None or self._thp_path is None:
            return
        try:
            footer = engine.recorder.format_footer(
                event_name=self.event_name, location=self.location)
            with open(self._thp_path, "a", encoding="gb2312", errors="replace") as f:
                f.write(footer + "\n")
        except OSError as exc:
            logger.warning("finalize thp failed: %s", exc)

    # ── index.json / 查询 ────────────────────────────────────

    def _index_path(self) -> Path:
        return self.records_dir / "index.json"

    def _update_index(self, names: list[str]) -> None:
        path = self._index_path()
        index: list[dict[str, Any]] = []
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    index = data
            except (json.JSONDecodeError, OSError):
                index = []
        engine = self.engine
        total = list(getattr(engine, "total_earnings", [0, 0])) if engine else [0, 0]
        entry = {
            "match_id": self._match_id,
            "names": list(names),
            "total_earnings": total,
            "hands_played": getattr(engine, "hand_num", 0) if engine else 0,
            "thp_file": self._thp_path.name if self._thp_path else None,
            "started_at": (self._match_started_at.isoformat(timespec="seconds")
                           if self._match_started_at else None),
            "ended_at": datetime.now().isoformat(timespec="seconds"),
            "reason": self._forfeit_reason or "completed",
            "loser_idx": self._forfeit_loser,
        }
        index.append(entry)
        try:
            path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("update index failed: %s", exc)

    def list_matches(self) -> list[dict[str, Any]]:
        path = self._index_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def read_thp(self, match_id: str) -> str | None:
        path = self.records_dir / f"{match_id}.thp"
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="gb2312", errors="replace")
        except OSError:
            return None

    # ── 辅助 ─────────────────────────────────────────────────

    def _make_match_id(self, names: list[str]) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        def safe(s: str) -> str:
            return "".join(c if c.isalnum() or c in "._-" else "_" for c in s)[:32] or "anon"
        return f"{safe(names[0])}_vs_{safe(names[1])}_{ts}"

    async def stop(self) -> None:
        self._stopping = True
        if self.engine is not None:
            self.engine.match_over = True
        if self.server is not None:
            await self.server.close_clients()
