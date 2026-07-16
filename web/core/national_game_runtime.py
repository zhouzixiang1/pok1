"""Shared national TCP game runtime used by evaluation and Web Arena."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sever.engine.deck import Deck
from sever.engine.game import GameEngine
from sever.engine.validator import NATIONAL_20000_CHIP_MAX_ACTION_REQUESTS_PER_HAND
from sever.engine.thp_recorder import THPRecorder

from sever.server.transport import NationalTCPClient


class NationalHandActionLimitExceeded(RuntimeError):
    """The fixed 20k national hand exceeded its proven decision envelope."""


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
        self._hand_action_requests = 0
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
        await self._clients[player_idx].send_message(message)

    async def _recv_action(self, player_idx: int) -> str | None:
        return await self._clients[player_idx].recv_action(timeout=self.action_timeout_sec)

    async def _record_event(self, event: dict[str, Any]) -> None:
        snapshot = dict(event)
        if snapshot.get("type") == "hand_start":
            self._hand_action_requests = 0
        elif snapshot.get("type") == "action_requested":
            self._hand_action_requests += 1
            if self._hand_action_requests > NATIONAL_20000_CHIP_MAX_ACTION_REQUESTS_PER_HAND:
                limit_event = {
                    "type": "hand_action_limit_reached",
                    "hand": int(snapshot.get("hand") or self.hand_num),
                    "limit": NATIONAL_20000_CHIP_MAX_ACTION_REQUESTS_PER_HAND,
                    "actions_observed": self._hand_action_requests,
                }
                self.events.append(limit_event)
                if self.event_sink is not None:
                    result = self.event_sink(dict(limit_event))
                    if asyncio.iscoroutine(result):
                        await result
                raise NationalHandActionLimitExceeded(
                    "national_20000_chip_hand_action_limit_exceeded:"
                    f"hand={limit_event['hand']}:"
                    f"limit={NATIONAL_20000_CHIP_MAX_ACTION_REQUESTS_PER_HAND}"
                )
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
