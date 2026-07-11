"""Shared national TCP game runtime used by evaluation and Web Arena."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

# arena 布局:engine 在 sibling 包(.engine),transport 同包(.transport)。见 engine/PROVENANCE.md。
from ..engine.deck import Deck
from ..engine.game import GameEngine
from ..engine.thp_recorder import THPRecorder

from .transport import NationalTCPClient


class NationalTCPGameEngine(GameEngine):
    def __init__(
        self,
        clients: list[NationalTCPClient],
        events: list[dict[str, Any]],
        deck_seed_base: int | None = None,
        action_timeout_sec: float = 60.0,
        event_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> None:
        self._clients = clients
        self.events = events
        self.event_sink = event_sink
        self.action_timeout_sec = float(action_timeout_sec)
        deck_factory = None
        if deck_seed_base is not None:
            deck_factory = lambda hand_num: Deck(seed=deck_seed_base + hand_num)
        super().__init__(
            send_func=self._send_to_client,
            broadcast_func=self._record_event,
            recorder=THPRecorder(clients[0].name or "A", clients[1].name or "B"),
            deck_factory=deck_factory,
        )

    async def _send_to_client(self, player_idx: int, message: str) -> None:
        await self._clients[player_idx].send_line(message)

    async def _recv_action(self, player_idx: int) -> str | None:
        return await self._clients[player_idx].recv_line(timeout=self.action_timeout_sec)

    async def _record_event(self, event: dict[str, Any]) -> None:
        snapshot = dict(event)
        self.events.append(snapshot)
        if self.event_sink is not None:
            result = self.event_sink(snapshot)
            if asyncio.iscoroutine(result):
                await result

    async def run_limited_match(self, name1: str, name2: str, hands: int) -> None:
        self.players[0].name = name1
        self.players[1].name = name2
        self.total_earnings = [0, 0]
        self.match_over = False
        for hand_num in range(1, hands + 1):
            self.hand_num = hand_num
            result = await self._run_hand(hand_num)
            if result is None:
                break
            self.total_earnings[0] += result.earnings[0]
            self.total_earnings[1] += result.earnings[1]
            if self.match_over:
                break
        await self._emit("match_end", {
            "total_earnings": list(self.total_earnings),
            "names": [player.name for player in self.players],
            "hands_played": self.hand_num,
        })
