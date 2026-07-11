"""Native national TCP execution backend for evolved bots.

The legacy national backend runs Botzone JSON bots through ``sever/bot_adapter.py``.
This module is the native path: a candidate must contain ``national_bot.py`` that
connects to the national TCP server directly and sends wire actions itself.
"""

from __future__ import annotations

import asyncio
import ast
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
from typing import Any

from eval_stats import paired_bootstrap_ci
from bot_namespace import ACTIVE_BOT_PREFIX, bot_name, parse_bot_version, version_sort_key
from national_runtime_telemetry import (
    empty_bot_log_summary as _empty_bot_log_summary,
    empty_runtime_telemetry as _empty_runtime_telemetry,
    merge_runtime_telemetry as _merge_runtime_telemetry,
    parse_native_bot_log as _parse_native_bot_log,
    server_action_latency as _server_action_latency,
)
from national_bot_launcher import build_native_bot_launch, native_entry_supports_log_arg
from national_game_runtime import NationalTCPGameEngine
from national_transport import NationalTCPClient
from pipeline_schema import NationalAcceptanceResult
from runtime_capacity import acquire_match_slots_async
from strength_order import is_strength_matchup, precommit_outcome_blockers


ROOT = Path(__file__).resolve().parents[2]
NATIVE_ENTRY = "national_bot.py"
PRECOMPUTE_ENTRY = "precompute.py"
TRACE_PREFIX = "POK_TRACE_DECISION "
NATIONAL_DECISION_RUNTIME_VERSION = 7
LOCAL_NATIVE_STRENGTH_HARD_DEADLINE_SEC = 2.0
LOCAL_NATIVE_STRENGTH_REFINEMENT_BUDGET_SEC = 1.8
LOCAL_NATIVE_STRENGTH_BASELINE_TARGET_SEC = 0.20
LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC = 420.0


NATIVE_BOT_TEMPLATE = r'''#!/usr/bin/env python3
"""Native national TCP entrypoint for this bot.

This file is the formal national-platform submission entry. It connects to the
TCP server, maintains raw-stream state, calls the local strategy in process,
and sends only national wire actions: raise <amount>, fold, call, check, allin.
It deliberately uses no legacy bridge module and prints no JSON responses to
stdout.
"""

from __future__ import annotations

import argparse
from collections import deque
import importlib
import json
import multiprocessing as mp
import os
import random
import re
import select
import signal
import socket
import subprocess
import sys
import time
import traceback


BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

SMALL_BLIND = 50
BIG_BLIND = 100
INITIAL_CHIPS = 20000
TOTAL_HANDS = 70
CARD_RE = re.compile(r"<(\d+),(\d+)>")
TCP_TO_JUDGE_SUIT = {0: 2, 1: 0, 2: 1, 3: 3}
ACTION_PREFIX_RE = re.compile(r"^(raise|bet)\s+(\d+)")
EARN_PREFIX_RE = re.compile(r"^earnChips\s+-?\d+")
DEFAULT_OFFICIAL_ACTION_DELAY_SEC = 0.30
OFFICIAL_ACTION_DELAY_ENV = "POK_OFFICIAL_ACTION_DELAY"
NATIONAL_STREAM_DECODER_VERSION = 2
NATIONAL_DECISION_RUNTIME_VERSION = __POK_DECISION_RUNTIME_VERSION__
DEFAULT_STREAM_IDLE_FLUSH_SEC = 0.10
STREAM_IDLE_FLUSH_ENV = "POK_NATIONAL_STREAM_IDLE_FLUSH"
MAX_STREAM_BUFFER_CHARS = 16_384
OPPONENT_TRACKER_SCHEMA_VERSION = 4
HAND_RUNTIME_SCHEMA_VERSION = 1
SHOWDOWN_RANGE_SCHEMA_VERSION = 1
OPPONENT_PRIOR_WEIGHT = 8.0
OPPONENT_ADAPTATION_CAP = 0.65
SHOWDOWN_RANGE_PRIOR_SOURCE = "uniform_1326_hole_combinations_v1"
SHOWDOWN_RANGE_BUCKET_COMBOS = {
    "premium_pair": 30,
    "small_pair": 48,
    "ace_broadway": 64,
    "broadway": 96,
    "suited_connector": 64,
    "suited_ace": 32,
    "offsuit_ace": 96,
    "suited_other": 176,
    "offsuit_other": 720,
}
SHOWDOWN_RANGE_BUCKET_PRIORS = {
    bucket: combinations / 1326.0
    for bucket, combinations in SHOWDOWN_RANGE_BUCKET_COMBOS.items()
}
RANK_SYMBOLS = "23456789TJQKA"
DEFAULT_DECISION_HARD_DEADLINE_SEC = 55.0
DEFAULT_DECISION_BASELINE_TARGET_SEC = 0.25
DEFAULT_DECISION_REFINEMENT_BUDGET_SEC = 54.0
DECISION_HARD_DEADLINE_ENV = "POK_DECISION_HARD_DEADLINE_SEC"
DECISION_BASELINE_TARGET_ENV = "POK_DECISION_BASELINE_TARGET_SEC"
DECISION_REFINEMENT_BUDGET_ENV = "POK_DECISION_REFINEMENT_BUDGET_SEC"
STRATEGY_WORKER_KILL_GRACE_SEC = 0.05
MAX_REFINEMENT_MESSAGES = 4096

_LOG_FP = None


def _log_open(path: str) -> None:
    global _LOG_FP
    if not path:
        return
    try:
        _LOG_FP = open(path, "a", encoding="utf-8", buffering=1)
    except Exception:
        _LOG_FP = None


def _log(msg: str) -> None:
    if _LOG_FP is None:
        return
    try:
        import time as _time
        _LOG_FP.write(f"[{_time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _official_action_delay_sec() -> float:
    raw = os.environ.get(OFFICIAL_ACTION_DELAY_ENV, str(DEFAULT_OFFICIAL_ACTION_DELAY_SEC))
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = DEFAULT_OFFICIAL_ACTION_DELAY_SEC
    return max(0.0, min(delay, 2.0))


def _bounded_runtime_seconds(env_name: str, default: float, upper: float) -> float:
    raw = os.environ.get(env_name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return max(0.001, min(value, upper))


def _decision_runtime_limits() -> tuple[float, float, float]:
    hard_deadline = _bounded_runtime_seconds(
        DECISION_HARD_DEADLINE_ENV,
        DEFAULT_DECISION_HARD_DEADLINE_SEC,
        DEFAULT_DECISION_HARD_DEADLINE_SEC,
    )
    refinement_ceiling = max(0.002, hard_deadline - min(0.5, hard_deadline * 0.1))
    refinement_budget = _bounded_runtime_seconds(
        DECISION_REFINEMENT_BUDGET_ENV,
        min(DEFAULT_DECISION_REFINEMENT_BUDGET_SEC, refinement_ceiling),
        refinement_ceiling,
    )
    baseline_ceiling = max(0.001, refinement_budget - min(0.05, refinement_budget * 0.1))
    baseline_target = _bounded_runtime_seconds(
        DECISION_BASELINE_TARGET_ENV,
        min(DEFAULT_DECISION_BASELINE_TARGET_SEC, baseline_ceiling),
        baseline_ceiling,
    )
    return hard_deadline, baseline_target, refinement_budget


def _decision_process_context():
    # Normal submissions run as __main__ and use portable spawn. Contract probes
    # import the entry under a synthetic module name on POSIX; fork keeps that
    # isolated test path executable while preserving killable process semantics.
    if os.name != "nt" and __name__ not in {"__main__", "__mp_main__"}:
        return mp.get_context("fork")
    return mp.get_context("spawn")


def _stream_idle_flush_sec() -> float:
    raw = os.environ.get(STREAM_IDLE_FLUSH_ENV, str(DEFAULT_STREAM_IDLE_FLUSH_SEC))
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = DEFAULT_STREAM_IDLE_FLUSH_SEC
    return max(0.01, min(delay, 0.50))


def _tcp_card_to_int(suit: int, rank: int) -> int:
    return rank * 4 + TCP_TO_JUDGE_SUIT[suit]


def _parse_cards(text: str) -> list[int]:
    return [_tcp_card_to_int(int(s), int(r)) for s, r in CARD_RE.findall(text)]


def _parse_action(raw: str) -> tuple[str, int | None]:
    parts = raw.strip().split()
    if not parts:
        return "unknown", None
    head = parts[0]
    if head in {"raise", "bet"} and len(parts) >= 2 and parts[1].isdigit():
        return "raise", int(parts[1])
    if head in {"call", "check", "fold", "allin"}:
        return head, None
    return "unknown", None


def _take_card_message(buffer: str, prefix: str, count: int) -> tuple[str | None, str]:
    if not buffer.startswith(prefix):
        return None, buffer
    pos = len(prefix)
    for _ in range(count):
        match = CARD_RE.match(buffer, pos)
        if not match:
            return None, buffer
        pos = match.end()
    return buffer[:pos], buffer[pos:]


def _take_message(buffer: str, *, flush_numeric: bool = False) -> tuple[str | None, str]:
    buffer = buffer.lstrip("\r\n\t ")
    if not buffer:
        return None, ""
    if buffer.startswith("name"):
        return "name", buffer[4:]
    for blind in ("SMALLBLIND", "BIGBLIND"):
        msg, rest = _take_card_message(buffer, f"preflop|{blind}|", 2)
        if msg is not None:
            return msg, rest
    for prefix, count in (("flop|", 3), ("turn|", 1), ("river|", 1), ("oppo_hands|", 2)):
        msg, rest = _take_card_message(buffer, prefix, count)
        if msg is not None:
            return msg, rest
    match = EARN_PREFIX_RE.match(buffer)
    if match:
        if match.end() == len(buffer) and not flush_numeric:
            return None, buffer
        return buffer[:match.end()], buffer[match.end():]
    match = ACTION_PREFIX_RE.match(buffer)
    if match:
        if match.end() == len(buffer) and not flush_numeric:
            return None, buffer
        return buffer[:match.end()], buffer[match.end():]
    for word in ("allin", "check", "call", "fold"):
        if buffer.startswith(word):
            return word, buffer[len(word):]
    return None, buffer


def _split_messages(buffer: str, *, flush_numeric: bool = False) -> tuple[list[str], str]:
    messages: list[str] = []
    while buffer:
        msg, rest = _take_message(buffer, flush_numeric=flush_numeric)
        if msg is None:
            return messages, rest
        messages.append(msg)
        buffer = rest
    return messages, ""


def _has_ambiguous_numeric_tail(buffer: str) -> bool:
    candidate = buffer.lstrip("\r\n\t ")
    if not candidate:
        return False
    return any(
        match is not None and match.end() == len(candidate)
        for match in (EARN_PREFIX_RE.match(candidate), ACTION_PREFIX_RE.match(candidate))
    )


class NationalStreamDecoder:
    """Decode the delimiter-free official stream without truncating numbers.

    A terminal ``raise 2`` may be the first fragment of ``raise 200``. Numeric
    messages are therefore committed only after a following protocol token or a
    short socket-quiescence window. Fixed-width/card and keyword messages remain
    immediately dispatchable.
    """

    def __init__(self, *, max_buffer_chars: int = MAX_STREAM_BUFFER_CHARS):
        self.buffer = ""
        self.max_buffer_chars = max_buffer_chars

    def feed(self, chunk: str) -> list[str]:
        self.buffer += chunk
        if len(self.buffer) > self.max_buffer_chars:
            raise ValueError(
                f"national protocol buffer exceeded {self.max_buffer_chars} characters"
            )
        messages, self.buffer = _split_messages(self.buffer)
        return messages

    @property
    def has_pending_numeric(self) -> bool:
        return _has_ambiguous_numeric_tail(self.buffer)

    def flush_idle(self) -> list[str]:
        messages, self.buffer = _split_messages(self.buffer, flush_numeric=True)
        return messages


class OpponentTracker:
    """Bounded connection-level facts for confidence-scaled opponent adaptation."""

    _STREETS = ("preflop", "flop", "turn", "river")
    _ACTIONS = ("fold", "check", "call", "raise", "allin")
    _SEMANTIC_ACTIONS = (*_ACTIONS, "pass")

    def __init__(self) -> None:
        self.reset_match()

    def reset_match(self) -> None:
        self.hands_started = 0
        self.hands_completed = 0
        self.showdowns = 0
        self.total_actions = 0
        self.opponent_wins = 0
        self.hero_wins = 0
        self.net_earned = 0
        self.preflop_vpip_hands = 0
        self.preflop_raise_hands = 0
        self.raise_total_sum = 0
        self.raise_total_count = 0
        self.street_actions = {
            street: {action: 0 for action in self._ACTIONS}
            for street in self._STREETS
        }
        self.semantic_street_actions = {
            street: {action: 0 for action in self._SEMANTIC_ACTIONS}
            for street in self._STREETS
        }
        self.facing_check = self._response_counter(include_pass=True)
        self.facing_check_by_street = {
            street: self._response_counter(include_pass=True)
            for street in self._STREETS
        }
        self.street_raise_sum = {street: 0.0 for street in self._STREETS}
        self.street_raise_sumsq = {street: 0.0 for street in self._STREETS}
        self.street_raise_count = {street: 0 for street in self._STREETS}
        self.facing_raise = self._response_counter()
        self.facing_allin = self._response_counter()
        self.facing_raise_by_street = {
            street: self._response_counter() for street in self._STREETS
        }
        self.facing_allin_by_street = {
            street: self._response_counter() for street in self._STREETS
        }
        self.context_actions: dict[str, dict] = {}
        self.showdown_hole_classes = {"pair": 0, "suited": 0, "offsuit": 0}
        self.showdown_range_samples = 0
        self.showdown_range_buckets = {
            bucket: 0 for bucket in SHOWDOWN_RANGE_BUCKET_PRIORS
        }
        self.showdown_hand_classes: dict[str, int] = {}
        self.showdown_contexts: dict[str, dict] = {}
        self._active_hand: dict | None = None
        self._pending_hero_pressure: dict[str, object] | None = None
        self._last_hero_action: str | None = None
        self._settled_hands: set[int] = set()
        self._showdown_hands: set[int] = set()
        self._recent_hands = deque(maxlen=8)

    def _response_counter(self, *, include_pass: bool = False) -> dict[str, int]:
        counter = {
            "opportunities": 0,
            "fold": 0,
            "call": 0,
            "raise": 0,
            "allin": 0,
        }
        if include_pass:
            counter["check"] = 0
            counter["pass"] = 0
        return counter

    def begin_hand(self, hand: int, *, opponent_is_sb: bool) -> None:
        self.hands_started += 1
        self._active_hand = {
            "hand": int(hand),
            "opponent_is_sb": bool(opponent_is_sb),
            "opponent_actions": 0,
            "opponent_vpip": False,
            "opponent_pfr": False,
            "opponent_preflop_line": "none",
            "showdown": False,
            "last_opponent_action": None,
        }
        self._pending_hero_pressure = None
        self._last_hero_action = None

    def begin_street(self) -> None:
        self._pending_hero_pressure = None
        self._last_hero_action = None

    @staticmethod
    def _size_bucket(amount: int | None) -> str:
        if not amount:
            return "none"
        bb = max(0.0, float(amount) / float(BIG_BLIND))
        return "small" if bb <= 4.0 else "medium" if bb <= 10.0 else "large"

    def _context_key(self, street: str, amount: int | None) -> str:
        position = "sb" if (self._active_hand or {}).get("opponent_is_sb") else "bb"
        pressure = self._pending_hero_pressure
        facing = str(pressure["action"]) if pressure else (
            "check" if self._last_hero_action == "check" else "unopened"
        )
        # A response context is conditioned on the hero's pressure size, never
        # on the observed outcome.  In particular, an omitted terminal fold has
        # no amount while a terminal call carries the final stage bet; using the
        # response amount would split the same decision into outcome-dependent
        # buckets and introduce selection bias.
        context_amount = pressure.get("amount") if pressure else amount
        return "|".join((street, position, facing, self._size_bucket(context_amount)))

    def observe_action(
        self,
        actor: str,
        street: str,
        action: str,
        *,
        amount: int | None = None,
        committed: int | None = None,
    ) -> None:
        if actor == "hero":
            self._pending_hero_pressure = (
                {"action": action, "amount": amount}
                if action in {"raise", "allin"}
                else None
            )
            self._last_hero_action = action
            return
        if actor != "opponent" or action not in self._ACTIONS:
            return

        street = street if street in self._STREETS else "preflop"
        semantic_action = action
        if (
            street != "preflop"
            and action == "call"
            and committed == 0
            and self._last_hero_action == "check"
        ):
            # The official protocol encodes the second player's zero-chip pass
            # after a check as ``call``.  Preserve the raw wire action above but
            # expose its poker meaning to every persistent statistic.
            semantic_action = "pass"
        self.total_actions += 1
        self.street_actions[street][action] += 1
        self.semantic_street_actions[street][semantic_action] += 1
        context_key = self._context_key(street, amount)
        context = self.context_actions.setdefault(
            context_key,
            {
                "samples": 0,
                "raw_actions": {name: 0 for name in self._ACTIONS},
                "semantic_actions": {
                    name: 0 for name in self._SEMANTIC_ACTIONS
                },
            },
        )
        context["samples"] += 1
        context["raw_actions"][action] += 1
        context["semantic_actions"][semantic_action] += 1
        if action in {"raise", "allin"} and amount is not None:
            try:
                raise_total = max(0, int(amount))
            except (TypeError, ValueError):
                raise_total = 0
            if raise_total:
                self.raise_total_sum += raise_total
                self.raise_total_count += 1
                raise_bb = raise_total / float(BIG_BLIND)
                self.street_raise_sum[street] += raise_bb
                self.street_raise_sumsq[street] += raise_bb * raise_bb
                self.street_raise_count[street] += 1
        if self._active_hand is not None:
            self._active_hand["opponent_actions"] += 1
            self._active_hand["last_opponent_action"] = action
            if street == "preflop" and action in {"call", "raise", "allin"}:
                if not self._active_hand["opponent_vpip"]:
                    self.preflop_vpip_hands += 1
                self._active_hand["opponent_vpip"] = True
            if street == "preflop" and action in {"raise", "allin"}:
                if not self._active_hand["opponent_pfr"]:
                    self.preflop_raise_hands += 1
                self._active_hand["opponent_pfr"] = True
                self._active_hand["opponent_preflop_line"] = "raise"
            elif street == "preflop" and action == "call":
                self._active_hand["opponent_preflop_line"] = (
                    "called_pressure" if self._pending_hero_pressure else "limp"
                )
            elif street == "preflop" and action == "check":
                self._active_hand["opponent_preflop_line"] = "checked_option"

        if self._last_hero_action == "check" and street != "preflop":
            self.facing_check["opportunities"] += 1
            self.facing_check[semantic_action] += 1
            street_check = self.facing_check_by_street[street]
            street_check["opportunities"] += 1
            street_check[semantic_action] += 1

        if self._pending_hero_pressure is not None:
            pressure_action = str(self._pending_hero_pressure["action"])
            counter = (
                self.facing_allin
                if pressure_action == "allin"
                else self.facing_raise
            )
            counter["opportunities"] += 1
            if action in counter:
                counter[action] += 1
            street_counter = (
                self.facing_allin_by_street[street]
                if pressure_action == "allin"
                else self.facing_raise_by_street[street]
            )
            street_counter["opportunities"] += 1
            if action in street_counter:
                street_counter[action] += 1
            self._pending_hero_pressure = None

    @staticmethod
    def _showdown_class(cards: list[int]) -> tuple[str, str]:
        rank_a, rank_b = cards[0] // 4 + 2, cards[1] // 4 + 2
        high, low = max(rank_a, rank_b), min(rank_a, rank_b)
        suited = cards[0] % 4 == cards[1] % 4
        if high == low:
            hand_class = RANK_SYMBOLS[high - 2] * 2
            bucket = "premium_pair" if high >= 10 else "small_pair"
        else:
            hand_class = (
                RANK_SYMBOLS[high - 2]
                + RANK_SYMBOLS[low - 2]
                + ("s" if suited else "o")
            )
            if high == 14 and low >= 10:
                bucket = "ace_broadway"
            elif low >= 10:
                bucket = "broadway"
            elif suited and high - low <= 2:
                bucket = "suited_connector"
            elif suited and high == 14:
                bucket = "suited_ace"
            elif not suited and high == 14:
                bucket = "offsuit_ace"
            elif suited:
                bucket = "suited_other"
            else:
                bucket = "offsuit_other"
        return hand_class, bucket

    def _showdown_context_key(self) -> str:
        active = self._active_hand or {}
        position = "sb" if active.get("opponent_is_sb") else "bb"
        if active.get("opponent_pfr"):
            line = "pfr"
        elif active.get("opponent_vpip"):
            line = "passive_vpip"
        else:
            line = str(active.get("opponent_preflop_line") or "unobserved")
        return f"{position}|{line}"

    def observe_settlement(self, hand: int, *, hero_earned: int) -> None:
        hand = int(hand)
        if hand in self._settled_hands:
            return
        self._settled_hands.add(hand)
        self.hands_completed += 1
        self.net_earned += int(hero_earned)
        if hero_earned > 0:
            self.hero_wins += 1
        elif hero_earned < 0:
            self.opponent_wins += 1
        summary = dict(self._active_hand or {"hand": hand})
        summary["hand"] = hand
        summary["hero_earned"] = int(hero_earned)
        summary["showdown"] = hand in self._showdown_hands or bool(summary.get("showdown"))
        self._recent_hands.append(summary)
        self._pending_hero_pressure = None

    def observe_showdown(
        self,
        hand: int,
        opponent_cards: list[int] | None = None,
        public_cards: list[int] | None = None,
    ) -> None:
        hand = int(hand)
        if hand in self._showdown_hands:
            return
        self._showdown_hands.add(hand)
        self.showdowns += 1
        cards = list(opponent_cards or [])
        if len(cards) == 2:
            ranks = [card // 4 for card in cards]
            suits = [card % 4 for card in cards]
            hole_class = "pair" if ranks[0] == ranks[1] else "suited" if suits[0] == suits[1] else "offsuit"
            self.showdown_hole_classes[hole_class] += 1
            hand_class, bucket = self._showdown_class(cards)
            self.showdown_range_samples += 1
            self.showdown_range_buckets[bucket] += 1
            self.showdown_hand_classes[hand_class] = (
                self.showdown_hand_classes.get(hand_class, 0) + 1
            )
            context_key = self._showdown_context_key()
            context = self.showdown_contexts.setdefault(
                context_key,
                {
                    "samples": 0,
                    "buckets": {
                        name: 0 for name in SHOWDOWN_RANGE_BUCKET_PRIORS
                    },
                },
            )
            context["samples"] += 1
            context["buckets"][bucket] += 1
        if self._active_hand is not None and self._active_hand.get("hand") == hand:
            self._active_hand["showdown"] = True
        for summary in reversed(self._recent_hands):
            if summary.get("hand") == hand:
                summary["showdown"] = True
                break

    @staticmethod
    def _smoothed_rate(successes: int, total: int, prior: float) -> float:
        return (float(successes) + prior * OPPONENT_PRIOR_WEIGHT) / (
            float(total) + OPPONENT_PRIOR_WEIGHT
        )

    def snapshot(self) -> dict:
        aggressive = sum(
            counts["raise"] + counts["allin"]
            for counts in self.street_actions.values()
        )
        fold_to_raise = self._smoothed_rate(
            self.facing_raise["fold"],
            self.facing_raise["opportunities"],
            0.35,
        )
        fold_to_allin = self._smoothed_rate(
            self.facing_allin["fold"],
            self.facing_allin["opportunities"],
            0.45,
        )
        confidence = self.total_actions / (self.total_actions + 24.0)
        adaptation_weight = min(OPPONENT_ADAPTATION_CAP, confidence * OPPONENT_ADAPTATION_CAP)
        postflop_actions = sum(
            sum(self.street_actions[street].values())
            for street in ("flop", "turn", "river")
        )
        postflop_aggressive = sum(
            self.street_actions[street]["raise"] + self.street_actions[street]["allin"]
            for street in ("flop", "turn", "river")
        )
        postflop_checks = sum(
            self.semantic_street_actions[street]["check"]
            + self.semantic_street_actions[street]["pass"]
            for street in ("flop", "turn", "river")
        )
        allins = sum(counts["allin"] for counts in self.street_actions.values())

        def street_aggression(street: str, prior: float = 0.36) -> float:
            counts = self.street_actions[street]
            aggressive_count = counts["raise"] + counts["allin"]
            return self._smoothed_rate(aggressive_count, sum(counts.values()), prior)

        def average_raise(street: str, default: float) -> float:
            count = self.street_raise_count[street]
            return self.street_raise_sum[street] / count if count else default

        def street_polarity(street: str) -> float:
            count = self.street_raise_count[street]
            if count < 3:
                return 0.0
            mean = self.street_raise_sum[street] / count
            variance = max(0.0, self.street_raise_sumsq[street] / count - mean * mean)
            cv = variance ** 0.5 / max(mean, 0.1)
            return max(-1.0, min(1.0, (cv - 0.30) / 0.30)) * min(1.0, count / 8.0)

        flop_aggr = street_aggression("flop")
        turn_aggr = street_aggression("turn")
        river_aggr = street_aggression("river")
        avg_flop_raise = average_raise("flop", 5.0)
        avg_turn_raise = average_raise("turn", 5.5)
        avg_river_raise = average_raise("river", 5.5)
        postflop_raise_samples = sum(
            self.street_raise_count[street] for street in ("flop", "turn", "river")
        )
        postflop_shoves = sum(
            self.street_actions[street]["allin"] for street in ("flop", "turn", "river")
        )
        context_profiles = {}
        for key, counts in sorted(self.context_actions.items()):
            samples = int(counts["samples"])
            context_confidence = samples / (samples + OPPONENT_PRIOR_WEIGHT)
            context_profiles[key] = {
                "samples": samples,
                "confidence": round(context_confidence, 6),
                "adaptation_weight": round(
                    min(OPPONENT_ADAPTATION_CAP, context_confidence * OPPONENT_ADAPTATION_CAP),
                    6,
                ),
                # ``actions`` remains the schema-3 raw-wire compatibility view.
                "actions": {
                    action: int(counts["raw_actions"][action])
                    for action in self._ACTIONS
                },
                "raw_actions": {
                    action: int(counts["raw_actions"][action])
                    for action in self._ACTIONS
                },
                "semantic_actions": {
                    action: int(counts["semantic_actions"][action])
                    for action in self._SEMANTIC_ACTIONS
                },
            }
        showdown_denominator = self.showdown_range_samples + OPPONENT_PRIOR_WEIGHT
        showdown_confidence = self.showdown_range_samples / showdown_denominator
        showdown_reach_rate = min(
            1.0,
            self.showdown_range_samples / max(1.0, float(self.hands_started)),
        )
        # Revealed hands are selected by reaching showdown, so even a large
        # posterior must not become an uncapped unconditional range estimate.
        # Reach coverage discounts sparse/selected observations and the shared
        # adaptation cap bounds their maximum effect on live decisions.
        showdown_adaptation_weight = min(
            OPPONENT_ADAPTATION_CAP,
            OPPONENT_ADAPTATION_CAP * showdown_confidence * showdown_reach_rate,
        )
        showdown_bucket_rates = {
            bucket: round(
                (self.showdown_range_buckets[bucket] + prior * OPPONENT_PRIOR_WEIGHT)
                / showdown_denominator,
                6,
            )
            for bucket, prior in SHOWDOWN_RANGE_BUCKET_PRIORS.items()
        }
        showdown_tightness = sum(
            showdown_bucket_rates[bucket]
            for bucket in ("premium_pair", "ace_broadway", "broadway")
        )
        showdown_context_profiles = {}
        for key, context in sorted(self.showdown_contexts.items()):
            samples = int(context["samples"])
            denominator = samples + OPPONENT_PRIOR_WEIGHT
            showdown_context_profiles[key] = {
                "samples": samples,
                "confidence": round(samples / denominator, 6),
                "adaptation_weight": round(
                    min(
                        OPPONENT_ADAPTATION_CAP,
                        OPPONENT_ADAPTATION_CAP
                        * (samples / denominator)
                        * showdown_reach_rate,
                    ),
                    6,
                ),
                "bucket_rates": {
                    bucket: round(
                        (context["buckets"][bucket] + prior * OPPONENT_PRIOR_WEIGHT)
                        / denominator,
                        6,
                    )
                    for bucket, prior in SHOWDOWN_RANGE_BUCKET_PRIORS.items()
                },
            }
        river_raise_responses = self.facing_raise_by_street["river"]
        river_allin_responses = self.facing_allin_by_street["river"]
        river_overcall_opportunities = (
            river_raise_responses["opportunities"]
            + river_allin_responses["opportunities"]
        )
        river_overcalls = (
            river_raise_responses["call"] + river_allin_responses["call"]
        )
        river_overcall_freq = self._smoothed_rate(
            river_overcalls,
            river_overcall_opportunities,
            0.55,
        )
        terminal_response_samples = (
            self.facing_raise["opportunities"] + self.facing_allin["opportunities"]
        )
        terminal_response_confidence = terminal_response_samples / (
            terminal_response_samples + OPPONENT_PRIOR_WEIGHT
        )
        return {
            "schema_version": OPPONENT_TRACKER_SCHEMA_VERSION,
            "hands_started": self.hands_started,
            "hands_completed": self.hands_completed,
            "showdowns": self.showdowns,
            "total_actions": self.total_actions,
            "confidence": round(confidence, 6),
            "adaptation_weight": round(adaptation_weight, 6),
            "rates": {
                "aggression": round(self._smoothed_rate(aggressive, self.total_actions, 0.30), 6),
                "preflop_vpip": round(
                    self._smoothed_rate(self.preflop_vpip_hands, self.hands_started, 0.55),
                    6,
                ),
                "fold_to_raise": round(fold_to_raise, 6),
                "fold_to_allin": round(fold_to_allin, 6),
            },
            "samples": {
                "preflop_vpip": self.hands_started,
                "fold_to_raise": self.facing_raise["opportunities"],
                "fold_to_allin": self.facing_allin["opportunities"],
                "raises": self.raise_total_count,
            },
            "contexts": context_profiles,
            "raw_street_actions": {
                street: dict(counts) for street, counts in self.street_actions.items()
            },
            "semantic_street_actions": {
                street: dict(counts)
                for street, counts in self.semantic_street_actions.items()
            },
            "terminal_response": {
                "samples": terminal_response_samples,
                "confidence": round(terminal_response_confidence, 6),
                "adaptation_weight": round(
                    min(
                        OPPONENT_ADAPTATION_CAP,
                        terminal_response_confidence * OPPONENT_ADAPTATION_CAP,
                    ),
                    6,
                ),
                "fold_to_raise": round(fold_to_raise, 6),
                "fold_to_jam": round(fold_to_allin, 6),
                "river_overcall": round(river_overcall_freq, 6),
                "facing_raise": dict(self.facing_raise),
                "facing_allin": dict(self.facing_allin),
                "facing_raise_by_street": {
                    street: dict(counts)
                    for street, counts in self.facing_raise_by_street.items()
                },
                "facing_allin_by_street": {
                    street: dict(counts)
                    for street, counts in self.facing_allin_by_street.items()
                },
                "contexts": {
                    key: profile
                    for key, profile in context_profiles.items()
                    if "|raise|" in key or "|allin|" in key
                },
            },
            "showdown_hole_classes": dict(self.showdown_hole_classes),
            "showdown_range": {
                "schema_version": SHOWDOWN_RANGE_SCHEMA_VERSION,
                "samples": self.showdown_range_samples,
                "confidence": round(showdown_confidence, 6),
                "adaptation_weight": round(showdown_adaptation_weight, 6),
                "showdown_reach_rate": round(showdown_reach_rate, 6),
                "selection_scope": "reached_showdown_only",
                "selection_bias_guard": "reach_rate_discount_and_capped_influence",
                "prior_source": SHOWDOWN_RANGE_PRIOR_SOURCE,
                "bucket_combo_counts": dict(SHOWDOWN_RANGE_BUCKET_COMBOS),
                "bucket_priors": dict(SHOWDOWN_RANGE_BUCKET_PRIORS),
                "bucket_counts": dict(self.showdown_range_buckets),
                "bucket_rates": showdown_bucket_rates,
                "tightness": round(showdown_tightness, 6),
                "class_counts": dict(sorted(self.showdown_hand_classes.items())),
                "contexts": showdown_context_profiles,
            },
            "average_raise_total": round(
                self.raise_total_sum / self.raise_total_count,
                3,
            ) if self.raise_total_count else 0.0,
            # Flat compatibility projection consumed by existing strategy modules.
            # Values are updated incrementally; no decision-time history scan is needed.
            "vpip": round(
                self._smoothed_rate(self.preflop_vpip_hands, self.hands_started, 0.55), 6
            ),
            "pfr": round(
                self._smoothed_rate(self.preflop_raise_hands, self.hands_started, 0.28), 6
            ),
            "allin_rate": round(self._smoothed_rate(allins, self.total_actions, 0.08), 6),
            "postflop_aggr": round(
                self._smoothed_rate(postflop_aggressive, postflop_actions, 0.36), 6
            ),
            "postflop_check_rate": round(
                self._smoothed_rate(postflop_checks, postflop_actions, 0.42), 6
            ),
            "postflop_checkback_rate": round(
                self._smoothed_rate(
                    self.facing_check["pass"],
                    self.facing_check["opportunities"],
                    0.50,
                ),
                6,
            ),
            "postflop_checkback_samples": self.facing_check["opportunities"],
            "fold_to_raise": round(fold_to_raise, 6),
            "aggression": round(
                self._smoothed_rate(aggressive, self.total_actions, 0.32), 6
            ),
            "avg_raise_bb": round(
                self.raise_total_sum / float(BIG_BLIND * self.raise_total_count), 3
            ) if self.raise_total_count else 3.0,
            "raise_samples": self.raise_total_count,
            "flop_aggr": round(flop_aggr, 6),
            "turn_aggr": round(turn_aggr, 6),
            "river_aggr": round(river_aggr, 6),
            "avg_flop_raise_bb": round(avg_flop_raise, 3),
            "avg_turn_raise_bb": round(avg_turn_raise, 3),
            "avg_river_raise_bb": round(avg_river_raise, 3),
            "barrel_freq": 0.45,
            "river_overcall_freq": round(river_overcall_freq, 6),
            "river_overcall_samples": river_overcall_opportunities,
            "fold_to_jam_rate": round(fold_to_allin, 6),
            "fold_to_jam_samples": self.facing_allin["opportunities"],
            "betsize_polarity": round(
                (street_polarity("flop") + street_polarity("turn") + street_polarity("river")) / 3.0,
                6,
            ),
            "shove_rate": round(
                self._smoothed_rate(postflop_shoves, postflop_aggressive, 0.08), 6
            ),
            "postflop_raise_samples": postflop_raise_samples,
            "postflop_shove_samples": postflop_shoves,
            "flop_shove_rate": round(
                self._smoothed_rate(
                    self.street_actions["flop"]["allin"],
                    self.street_actions["flop"]["raise"] + self.street_actions["flop"]["allin"],
                    0.08,
                ),
                6,
            ),
            "turn_shove_rate": round(
                self._smoothed_rate(
                    self.street_actions["turn"]["allin"],
                    self.street_actions["turn"]["raise"] + self.street_actions["turn"]["allin"],
                    0.08,
                ),
                6,
            ),
            "river_shove_rate": round(
                self._smoothed_rate(
                    self.street_actions["river"]["allin"],
                    self.street_actions["river"]["raise"] + self.street_actions["river"]["allin"],
                    0.08,
                ),
                6,
            ),
            "flop_shove_samples": self.street_actions["flop"]["allin"],
            "turn_shove_samples": self.street_actions["turn"]["allin"],
            "river_shove_samples": self.street_actions["river"]["allin"],
            "flop_polarity": round(street_polarity("flop"), 6),
            "turn_polarity": round(street_polarity("turn"), 6),
            "river_polarity": round(street_polarity("river"), 6),
            "street_actions": {
                street: dict(counts) for street, counts in self.street_actions.items()
            },
            "facing_raise_by_street": {
                street: dict(counts)
                for street, counts in self.facing_raise_by_street.items()
            },
            "facing_allin_by_street": {
                street: dict(counts)
                for street, counts in self.facing_allin_by_street.items()
            },
            "facing_check_by_street": {
                street: dict(counts)
                for street, counts in self.facing_check_by_street.items()
            },
            "match_result": {
                "hero_wins": self.hero_wins,
                "opponent_wins": self.opponent_wins,
                "hero_net_earned": self.net_earned,
            },
            "recent_hands": [dict(item) for item in self._recent_hands],
        }


def _resolve_seat(name: str, seat: str) -> str:
    value = seat.lower()
    if value in {"upper", "lower"}:
        return value
    lowered = name.strip().lower()
    if lowered.endswith(("b", "2", "_lower", "-lower", "lower", "bottom")):
        return "lower"
    if lowered.endswith(("a", "1", "_upper", "-upper", "upper", "top")):
        return "upper"
    return "unknown"


def _strategy_worker_candidate(raw_value):
    """Normalize one refinement yield without imposing a strategy framework."""
    metadata = {}
    value = raw_value
    if isinstance(raw_value, dict):
        value = raw_value.get("action")
        metadata = {
            str(key): raw_value[key]
            for key in ("sample_count", "confidence", "reason", "complete")
            if key in raw_value
            and isinstance(raw_value[key], (str, int, float, bool, type(None)))
        }
    elif isinstance(raw_value, (tuple, list)) and raw_value:
        value = raw_value[0]
        if len(raw_value) > 1 and isinstance(raw_value[1], dict):
            metadata = {
                str(key): item
                for key, item in raw_value[1].items()
                if isinstance(item, (str, int, float, bool, type(None)))
            }
    return value, metadata


def _strategy_worker_main(connection, bot_dir: str, random_seed: int | None) -> None:
    """Persistent, killable strategy runtime. It never owns the TCP socket."""
    started = time.monotonic()
    try:
        if hasattr(os, "setsid"):
            # Own a process group so any candidate-created CPU workers inherit a
            # tree the socket process can terminate atomically at the deadline.
            try:
                os.setsid()
            except OSError:
                pass
        if random_seed is not None:
            random.seed(int(random_seed))
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        # Standardized pure-fact precompute is loaded once per worker lifetime.
        # Strategy modules may import and consume it without rebuilding tables per turn.
        if os.path.isfile(os.path.join(bot_dir, "precompute.py")):
            importlib.import_module("precompute")
        strategy_module = importlib.import_module("strategy")
        main_module = importlib.import_module("main")
        state_module = importlib.import_module("state")
        get_action = getattr(strategy_module, "get_action")
        get_baseline_action = getattr(strategy_module, "get_baseline_action", None)
        iter_refinements = getattr(strategy_module, "iter_refinements", None)
        refine_action = getattr(strategy_module, "refine_action", None)
        sanitize_action = getattr(main_module, "sanitize_action")
        reconstruct_state = getattr(state_module, "reconstruct_state")
        connection.send({
            "kind": "ready",
            "import_elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            "pid": os.getpid(),
            "has_baseline": callable(get_baseline_action),
            "has_iterator": callable(iter_refinements),
            "has_refine_action": callable(refine_action),
            "random_seed": random_seed,
            "process_group": os.getpgrp() if hasattr(os, "getpgrp") else None,
        })
    except BaseException as exc:
        try:
            connection.send({
                "kind": "startup_error",
                "error": f"{type(exc).__name__}:{str(exc)[:400]}",
                "pid": os.getpid(),
            })
        except BaseException:
            pass
        return

    while True:
        try:
            job = connection.recv()
        except (EOFError, OSError):
            return
        if not isinstance(job, dict):
            continue
        if job.get("kind") == "stop":
            return
        if job.get("kind") != "decide":
            continue
        decision_id = int(job.get("decision_id") or 0)
        req = job.get("request") or {}
        current_view = tuple(job.get("current_request_view") or (req,))
        deadline = float(job.get("deadline_monotonic") or time.monotonic())
        fallback = int(job.get("fallback_action") or 0)
        raw_candidate_limit = job.get("max_refinement_candidates")
        candidate_limit = (
            None
            if raw_candidate_limit is None
            else max(0, min(MAX_REFINEMENT_MESSAGES, int(raw_candidate_limit)))
        )
        sequence = 0
        try:
            state = reconstruct_state(req)

            def publish(phase: str, raw_action, metadata=None, trusted=None) -> int:
                nonlocal sequence
                sanitized = int(sanitize_action(raw_action, state, int(req.get("my_chips", 0))))
                sequence += 1
                connection.send({
                    "kind": "candidate",
                    "decision_id": decision_id,
                    "phase": phase,
                    "sequence": sequence,
                    "action": sanitized,
                    "published_monotonic": time.monotonic(),
                    "metadata": dict(metadata or {}),
                    "trusted": dict(trusted or {}),
                })
                return sanitized

            baseline = fallback
            if callable(get_baseline_action):
                baseline = publish(
                    "baseline",
                    get_baseline_action(req, current_view),
                    {"source": "get_baseline_action"},
                )
            elif time.monotonic() < deadline:
                baseline = publish(
                    "baseline",
                    get_action(req, current_view),
                    {"source": "legacy_get_action"},
                )

            refinement_started_cpu = time.process_time_ns()
            refinement_started_wall = time.monotonic_ns()
            iterator_steps = 0
            iterator_exhausted = False
            termination_reason = "not_available"
            if (
                callable(iter_refinements)
                and candidate_limit != 0
                and time.monotonic() < deadline
            ):
                iterator = iter_refinements(req, current_view, baseline, deadline)
                termination_reason = "deadline"
                while True:
                    if iterator_steps >= MAX_REFINEMENT_MESSAGES:
                        termination_reason = "message_limit"
                        break
                    if candidate_limit is not None and iterator_steps >= candidate_limit:
                        termination_reason = "probe_candidate_limit"
                        break
                    if time.monotonic() >= deadline:
                        termination_reason = "deadline"
                        break
                    try:
                        raw_candidate = next(iterator)
                    except StopIteration:
                        iterator_exhausted = True
                        termination_reason = "iterator_exhausted"
                        break
                    iterator_steps += 1
                    candidate, metadata = _strategy_worker_candidate(raw_candidate)
                    baseline = publish(
                        "refinement",
                        candidate,
                        metadata,
                        {
                            "iterator_step": iterator_steps,
                            "process_cpu_ms": round(
                                (time.process_time_ns() - refinement_started_cpu) / 1_000_000.0,
                                6,
                            ),
                            "elapsed_ms": round(
                                (time.monotonic_ns() - refinement_started_wall) / 1_000_000.0,
                                6,
                            ),
                        },
                    )
            elif callable(refine_action) and candidate_limit != 0 and time.monotonic() < deadline:
                iterator_steps = 1
                termination_reason = "one_shot_refine_action"
                baseline = publish(
                    "refinement",
                    refine_action(req, current_view, baseline, deadline),
                    {"source": "refine_action"},
                    {
                        "iterator_step": iterator_steps,
                        "process_cpu_ms": round(
                            (time.process_time_ns() - refinement_started_cpu) / 1_000_000.0,
                            6,
                        ),
                        "elapsed_ms": round(
                            (time.monotonic_ns() - refinement_started_wall) / 1_000_000.0,
                            6,
                        ),
                    },
                )
            elif candidate_limit == 0:
                termination_reason = "probe_candidate_limit"
            connection.send({
                "kind": "done",
                "decision_id": decision_id,
                "sequence": sequence,
                "action": baseline,
                "completed_monotonic": time.monotonic(),
                "refinement_stats": {
                    "iterator_steps": iterator_steps,
                    "iterator_exhausted": iterator_exhausted,
                    "termination_reason": termination_reason,
                    "process_cpu_ms": round(
                        (time.process_time_ns() - refinement_started_cpu) / 1_000_000.0,
                        6,
                    ),
                    "elapsed_ms": round(
                        (time.monotonic_ns() - refinement_started_wall) / 1_000_000.0,
                        6,
                    ),
                },
            })
        except BaseException as exc:
            try:
                connection.send({
                    "kind": "decision_error",
                    "decision_id": decision_id,
                    "sequence": sequence,
                    "error": f"{type(exc).__name__}:{str(exc)[:400]}",
                })
            except BaseException:
                return


class NativeNationalBot:
    def __init__(self, name: str, seat: str = "auto"):
        self.name = name
        self.seat = _resolve_seat(name, seat)
        self._official_action_delay_sec = _official_action_delay_sec()
        (
            self._decision_hard_deadline_sec,
            self._decision_baseline_target_sec,
            self._decision_refinement_budget_sec,
        ) = _decision_runtime_limits()
        self._last_platform_message_at = 0.0
        self._reset_match()
        self._mp_context = _decision_process_context()
        self._strategy_process = None
        self._strategy_connection = None
        self._strategy_worker_generation = 0
        try:
            self._strategy_base_seed = int(os.environ["POK_NATIVE_BOT_SEED"])
        except (KeyError, TypeError, ValueError):
            self._strategy_base_seed = None
        self._strategy_worker_seed = None
        self._strategy_max_refinement_candidates = None
        self._decision_serial = 0
        self._retired_strategy_processes = []
        self._unconfirmed_strategy_tree_pids = set()
        self._last_stopped_worker_pid = None
        self._last_stopped_worker_exitcode = None
        self._last_stopped_worker_tree_termination_requested = False
        self._last_stopped_worker_terminated = False

    def _reset_match(self) -> None:
        self._buf = ""
        self._my_cards: list[int] = []
        self._public_cards: list[int] = []
        self._is_sb = False
        self._hand_num = 0
        self._history: list[dict] = []
        self._stage = "preflop"
        self._my_id = 0
        self._opponent_id = 1
        self._dealer_id = 0
        self._my_action_count = 0
        self._my_chips = INITIAL_CHIPS
        self._my_stage_bet = 0
        self._opponent_chips = INITIAL_CHIPS
        self._opponent_stage_bet = 0
        self._pot = 0
        self._in_allin_runout = False
        self._requests: list[dict] = []
        self._responses: list[int] = []
        self._total_win_chips = [0, 0]
        self._total_win_games = [0, 0]
        self._last_earned = 0
        self._showdowns: list[dict] = []
        self._showdown_by_hand: dict[int, dict] = {}
        self._earned_by_hand: dict[int, int] = {}
        self._opponent_tracker = OpponentTracker()
        self._last_decision_source = "uninitialized"
        self._last_decision_metrics = {}

    def _acts_first_postflop(self) -> bool:
        return not self._is_sb

    def _responding_to_check(self) -> bool:
        round_num = self._round_num()
        return bool(
            self._my_action_count == 0
            and self._history
            and self._history[-1].get("round") == round_num
            and self._history[-1].get("player_id") == self._opponent_id
            and self._history[-1].get("action_type") == "check"
        )

    def _round_num(self) -> int:
        return {"preflop": 0, "flop": 1, "turn": 2, "river": 3}.get(self._stage, 0)

    def _betting_snapshot(self) -> dict:
        to_call = max(0, self._opponent_stage_bet - self._my_stage_bet)
        return {
            "opponent_chips": self._opponent_chips,
            "my_stage_bet": self._my_stage_bet,
            "opponent_stage_bet": self._opponent_stage_bet,
            "pot": self._pot,
            "to_call": to_call,
            "opponent_allin": self._opponent_chips == 0 and self._opponent_stage_bet > 0,
        }

    def _actor_label(self, player_id: int | None) -> str:
        if player_id == self._my_id:
            return "hero"
        if player_id == self._opponent_id:
            return "opponent"
        return "unknown"

    def _street_records(self, round_num: int) -> list[dict]:
        return [
            record for record in self._history
            if record.get("round") == round_num
        ]

    def _semantic_street_summary(self, round_num: int) -> dict:
        records = self._street_records(round_num)
        actions = []
        saw_opening_check = False
        for record in records:
            action_type = str(record.get("action_type") or "unknown")
            committed = int(record.get("committed", 0) or 0)
            semantic_action = action_type
            if action_type == "check":
                saw_opening_check = True
                semantic_action = "check"
            elif action_type == "call" and committed == 0 and saw_opening_check:
                semantic_action = "pass"
            elif action_type == "call":
                semantic_action = "match"
            actions.append({
                "actor": self._actor_label(record.get("player_id")),
                "action": action_type,
                "semantic_action": semantic_action,
                "committed": committed,
                "inferred": bool(record.get("inferred")),
            })
        checked_through = bool(
            len(actions) >= 2
            and actions[0]["semantic_action"] == "check"
            and actions[-1]["semantic_action"] == "pass"
            and not any(
                item["action"] in {"raise", "allin"} for item in actions
            )
        )
        last = actions[-1] if actions else None
        return {
            "round": round_num,
            "actions": actions,
            "checked_through": checked_through,
            "closed_by": last["actor"] if last else None,
            "opponent_checked_back": bool(
                checked_through
                and last
                and last["actor"] == "opponent"
            ),
            "hero_checked_back": bool(
                checked_through
                and last
                and last["actor"] == "hero"
            ),
        }

    def _preflop_line(self) -> tuple[str, str]:
        records = self._street_records(0)
        raises = [
            record for record in records
            if record.get("action_type") in {"raise", "allin"}
        ]
        aggressor = (
            self._actor_label(raises[-1].get("player_id"))
            if raises else "none"
        )
        opponent_raised = any(
            record.get("player_id") == self._opponent_id
            and record.get("action_type") in {"raise", "allin"}
            for record in records
        )
        hero_raised = any(
            record.get("player_id") == self._my_id
            and record.get("action_type") in {"raise", "allin"}
            for record in records
        )
        first_raise_index = next(
            (
                index for index, record in enumerate(records)
                if record.get("action_type") in {"raise", "allin"}
            ),
            len(records),
        )
        opponent_limped = any(
            record.get("player_id") == self._opponent_id
            and record.get("action_type") == "call"
            for record in records[:first_raise_index]
        )
        hero_limped = any(
            record.get("player_id") == self._my_id
            and record.get("action_type") == "call"
            for record in records[:first_raise_index]
        )
        if not self._is_sb:
            if opponent_raised:
                spot = "bb_vs_raise"
            elif opponent_limped:
                spot = "bb_vs_limp"
            else:
                spot = "bb_option"
        elif opponent_raised:
            spot = "sb_vs_reraise"
        elif hero_raised:
            spot = "sb_open"
        elif hero_limped:
            spot = "sb_limp"
        else:
            spot = "sb_open"
        return aggressor, spot

    def _hand_runtime(self) -> dict:
        """Return wrapper-owned, cross-street semantics for strategy consumers.

        The socket state machine is the only authority for these fields.  In
        particular, strategy modules must not rediscover the preflop aggressor
        or checked-through streets from a current-street-only request view.
        """
        round_num = self._round_num()
        current = self._semantic_street_summary(round_num)
        previous = (
            self._semantic_street_summary(round_num - 1)
            if round_num > 0 else None
        )
        preflop_aggressor, preflop_spot = self._preflop_line()
        street_open = not current["actions"]
        hero_position = "sb" if self._is_sb else "bb"
        can_donk = bool(
            round_num == 1
            and street_open
            and hero_position == "bb"
            and preflop_aggressor == "opponent"
        )
        can_delayed_probe = bool(
            round_num in {2, 3}
            and street_open
            and hero_position == "bb"
            and preflop_aggressor == "opponent"
            and previous
            and previous["opponent_checked_back"]
        )
        to_call = max(0, self._opponent_stage_bet - self._my_stage_bet)
        effective_stack = min(self._my_chips, self._opponent_chips)
        line_tags = []
        if can_donk:
            line_tags.append("donk_opportunity")
        if can_delayed_probe:
            line_tags.append("delayed_probe_opportunity")
        if previous and previous["checked_through"]:
            line_tags.append("previous_street_checked_through")
        if self._responding_to_check():
            line_tags.append("responding_to_check")
        return {
            "schema_version": HAND_RUNTIME_SCHEMA_VERSION,
            "street": self._stage,
            "round": round_num,
            "hero_position": hero_position,
            "hero_in_position_postflop": self._is_sb,
            "preflop_aggressor": preflop_aggressor,
            "preflop_spot": preflop_spot,
            "street_open": street_open,
            "responding_to_check": self._responding_to_check(),
            "can_donk": can_donk,
            "can_delayed_probe": can_delayed_probe,
            "line_tags": line_tags,
            "current_street": current,
            "previous_street": previous,
            "pot": self._pot,
            "my_chips": self._my_chips,
            "opponent_chips": self._opponent_chips,
            "effective_stack": effective_stack,
            "my_stage_bet": self._my_stage_bet,
            "opponent_stage_bet": self._opponent_stage_bet,
            "to_call": to_call,
            "spr": round(effective_stack / max(1.0, float(self._pot)), 6),
            "pot_odds": round(to_call / max(1.0, float(self._pot + to_call)), 6),
        }

    def _request(self) -> dict:
        req = {
            "num_players": 2,
            "dealer_id": self._dealer_id,
            "my_id": self._my_id,
            "my_chips": self._my_chips,
            "my_cards": list(self._my_cards),
            "public_cards": list(self._public_cards),
            "history": list(self._history),
            "hand": self._hand_num - 1,
            "max_hand": TOTAL_HANDS,
            "total_win_chips": list(self._total_win_chips),
            "total_win_games": list(self._total_win_games),
            # Cross-hand opponent evidence is available through the bounded
            # incremental opponent_runtime snapshot. Do not expose the full
            # showdown list to decision code and reintroduce batch rescans.
            "opponent_showdowns": [],
            "opponent_runtime": self._opponent_tracker.snapshot(),
            "hand_runtime": self._hand_runtime(),
            **self._betting_snapshot(),
        }
        req["remaining_hands"] = max(1, TOTAL_HANDS - int(req["hand"]))
        return req

    def _socket_safe_fallback_action(self) -> int:
        """Choose a zero-strategy-risk action from socket-owned betting state."""
        return -1 if self._opponent_stage_bet > self._my_stage_bet else 0

    def _sanitize_worker_action(self, raw_action, fallback: int) -> int:
        """Keep untrusted strategy output inside the integer action domain."""
        try:
            action = int(raw_action)
        except (TypeError, ValueError, OverflowError):
            return int(fallback)
        if action in {-2, -1, 0} or action > 0:
            return action
        return int(fallback)

    def _strategy_worker_alive(self) -> bool:
        return bool(self._strategy_process is not None and self._strategy_process.is_alive())

    @staticmethod
    def _terminate_worker_tree(process, *, force: bool) -> bool:
        """Request whole-tree termination, returning whether that request landed.

        A parent-only ``Process.kill`` fallback is deliberately not reported as
        whole-tree success: candidate-created children could otherwise survive
        while the runtime advertises a clean kill.  Callers separately verify
        that the multiprocessing worker itself has actually exited.
        """
        pid = getattr(process, "pid", None)
        tree_requested = False
        if pid and os.name == "posix" and hasattr(os, "killpg"):
            try:
                os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
                tree_requested = True
            except ProcessLookupError:
                # No process remains in that process group: cleanup is already
                # complete and replacement is safe.
                return True
            except (PermissionError, OSError):
                pass
        elif pid and os.name == "nt":
            try:
                killer = subprocess.Popen(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
                try:
                    tree_requested = (
                        killer.wait(timeout=STRATEGY_WORKER_KILL_GRACE_SEC) == 0
                    )
                except subprocess.TimeoutExpired:
                    try:
                        killer.kill()
                        killer.wait(timeout=STRATEGY_WORKER_KILL_GRACE_SEC)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            except OSError:
                pass
        if not tree_requested:
            try:
                if force and hasattr(process, "kill"):
                    process.kill()
                else:
                    process.terminate()
            except (AssertionError, OSError, ValueError):
                pass
        return tree_requested

    def _reap_retired_strategy_workers(self, *, wait: bool) -> None:
        """Reap workers terminated on a decision deadline at a safe point."""
        remaining = []
        for process in self._retired_strategy_processes:
            pid = getattr(process, "pid", None)
            try:
                process.join(timeout=0)
                if wait:
                    tree_confirmed = self._terminate_worker_tree(process, force=True)
                    if tree_confirmed and pid is not None:
                        self._unconfirmed_strategy_tree_pids.discard(pid)
                    elif pid is not None:
                        self._unconfirmed_strategy_tree_pids.add(pid)
                    process.join(timeout=STRATEGY_WORKER_KILL_GRACE_SEC)
                if (
                    process.is_alive()
                    or pid in self._unconfirmed_strategy_tree_pids
                ):
                    remaining.append(process)
            except (AssertionError, OSError, ValueError):
                if pid is not None:
                    self._unconfirmed_strategy_tree_pids.add(pid)
                if process.is_alive() or pid in self._unconfirmed_strategy_tree_pids:
                    remaining.append(process)
        self._retired_strategy_processes = remaining

    def _stop_strategy_worker(self, reason: str, *, wait: bool = True) -> None:
        process = self._strategy_process
        connection = self._strategy_connection
        tree_termination_requested = False
        terminated = process is None
        if connection is not None and wait:
            try:
                connection.send({"kind": "stop", "reason": reason})
            except (BrokenPipeError, EOFError, OSError):
                pass
        if process is not None:
            stopped_pid = process.pid
            if wait:
                # A polite worker exit is insufficient when candidate code may
                # have spawned a fixed CPU pool or subprocess. Always target the
                # process group/tree while the parent PID is still available;
                # otherwise a clean parent return could orphan live compute.
                tree_termination_requested = self._terminate_worker_tree(
                    process,
                    force=True,
                )
                process.join(timeout=STRATEGY_WORKER_KILL_GRACE_SEC)
            else:
                # The socket deadline owns this path. Request termination but
                # use the reserved hard-deadline margin for one bounded join.
                # SIGKILL/taskkill cannot be ignored, unlike the old SIGTERM-only
                # path that could accumulate one live CPU worker per decision.
                try:
                    if process.is_alive():
                        tree_termination_requested = self._terminate_worker_tree(
                            process,
                            force=True,
                        )
                    process.join(timeout=STRATEGY_WORKER_KILL_GRACE_SEC)
                except (AssertionError, OSError, ValueError):
                    tree_termination_requested = False
                if process.is_alive():
                    self._retired_strategy_processes.append(process)
            terminated = not process.is_alive()
            if tree_termination_requested:
                self._unconfirmed_strategy_tree_pids.discard(stopped_pid)
            else:
                self._unconfirmed_strategy_tree_pids.add(stopped_pid)
            if (
                process.is_alive()
                or stopped_pid in self._unconfirmed_strategy_tree_pids
            ) and process not in self._retired_strategy_processes:
                self._retired_strategy_processes.append(process)
            _log(
                f"DECISION_WORKER stop reason={reason} pid={process.pid} "
                f"alive={process.is_alive()} exitcode={process.exitcode} wait={wait} "
                f"tree_request={tree_termination_requested} terminated={terminated}"
            )
            self._last_stopped_worker_pid = stopped_pid
            self._last_stopped_worker_exitcode = process.exitcode
            self._last_stopped_worker_tree_termination_requested = (
                tree_termination_requested
            )
            self._last_stopped_worker_terminated = terminated
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        self._strategy_process = None
        self._strategy_connection = None

    def _ensure_strategy_worker(self) -> bool:
        # Never replace a timed-out worker until the old process is confirmed
        # dead.  This is fail-closed on platforms where whole-tree termination
        # cannot be confirmed and prevents silent CPU oversubscription.
        self._reap_retired_strategy_workers(wait=True)
        if self._retired_strategy_processes or self._unconfirmed_strategy_tree_pids:
            _log(
                "DECISION_WORKER replacement_blocked "
                f"retired={len(self._retired_strategy_processes)} "
                f"unconfirmed_trees={len(self._unconfirmed_strategy_tree_pids)}"
            )
            return False
        if self._strategy_worker_alive() and self._strategy_connection is not None:
            return True
        self._stop_strategy_worker("replace_dead_worker")
        if self._retired_strategy_processes or self._unconfirmed_strategy_tree_pids:
            _log("DECISION_WORKER replacement_blocked_after_dead_worker_cleanup")
            return False
        parent_connection, child_connection = self._mp_context.Pipe(duplex=True)
        next_generation = self._strategy_worker_generation + 1
        worker_seed = (
            None
            if self._strategy_base_seed is None
            else self._strategy_base_seed + next_generation - 1
        )
        process = self._mp_context.Process(
            target=_strategy_worker_main,
            args=(child_connection, BOT_DIR, worker_seed),
            name=f"national-strategy-{next_generation}",
            # Non-daemon is intentional: strategy may own a bounded fixed CPU
            # pool. The socket owner enforces the deadline on its whole process
            # group/tree rather than allowing descendants to escape.
            daemon=False,
        )
        try:
            process.start()
        except BaseException as exc:
            parent_connection.close()
            child_connection.close()
            _log(f"DECISION_WORKER start_error={type(exc).__name__}:{str(exc)[:240]}")
            return False
        child_connection.close()
        self._strategy_worker_generation += 1
        self._strategy_worker_seed = worker_seed
        self._strategy_process = process
        self._strategy_connection = parent_connection
        _log(
            f"DECISION_WORKER started pid={process.pid} "
            f"generation={self._strategy_worker_generation} seed={worker_seed}"
        )
        return True

    def close(self) -> None:
        self._stop_strategy_worker("client_close")
        self._reap_retired_strategy_workers(wait=True)

    def _strategy_action(self) -> int:
        started = time.monotonic()
        hard_deadline = started + self._decision_hard_deadline_sec
        baseline_target = started + self._decision_baseline_target_sec
        refinement_deadline = started + self._decision_refinement_budget_sec
        # Facing a bet, action 0 would become a call. The socket-owned fallback
        # therefore folds to positive to_call and passes only at zero to_call.
        baseline = self._socket_safe_fallback_action()
        self._last_decision_source = "socket_baseline"
        try:
            req = self._request()
        except BaseException as exc:
            _log(
                f"DECIDE request_error={type(exc).__name__}:{str(exc)[:160]!r} "
                f"fallback={baseline}"
            )
            self._last_decision_source = "request_error_baseline"
            return baseline
        self._decision_serial += 1
        decision_id = self._decision_serial
        decision_metrics = {
            "runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
            "decision_id": decision_id,
            "socket_fallback_action": baseline,
            "socket_fallback_ready_ms": round((time.monotonic() - started) * 1000.0, 3),
            "baseline_published_ms": None,
            "baseline_target_met": False,
            "strategy_baseline_action": None,
            "refinement_messages": 0,
            "refinement_action_changes": 0,
            "refinement_progress": [],
            "trusted_refinement_steps": 0,
            "trusted_refinement_cpu_ms": 0.0,
            "trusted_refinement_elapsed_ms": 0.0,
            "refinement_iterator_exhausted": False,
            "refinement_termination_reason": None,
            "latest_sequence": 0,
            "timed_out": False,
            "worker_terminated": False,
            "worker_generation": None,
            "worker_pid": None,
            "completed": False,
        }
        self._last_decision_metrics = decision_metrics
        req["decision_runtime_version"] = NATIONAL_DECISION_RUNTIME_VERSION
        req["decision_id"] = decision_id
        req["decision_hard_deadline_ms"] = int(self._decision_hard_deadline_sec * 1000)
        req["decision_baseline_target_ms"] = int(self._decision_baseline_target_sec * 1000)
        req["decision_refinement_budget_ms"] = int(self._decision_refinement_budget_sec * 1000)
        req["decision_hard_deadline_monotonic"] = hard_deadline
        req["decision_refinement_deadline_monotonic"] = refinement_deadline
        req["decision_baseline_action"] = baseline
        self._requests.append(req)
        if not self._ensure_strategy_worker():
            self._last_decision_source = "worker_start_error_baseline"
            return baseline
        decision_metrics["worker_generation"] = self._strategy_worker_generation
        decision_metrics["worker_pid"] = self._strategy_process.pid
        decision_metrics["worker_seed"] = self._strategy_worker_seed
        connection = self._strategy_connection
        try:
            connection.send({
                "kind": "decide",
                "decision_id": decision_id,
                "request": req,
                # Bounded current-hand compatibility view; never full match history.
                "current_request_view": (req,),
                "fallback_action": baseline,
                "deadline_monotonic": refinement_deadline,
                "max_refinement_candidates": self._strategy_max_refinement_candidates,
            })
        except (BrokenPipeError, EOFError, OSError) as exc:
            _log(f"DECIDE worker_send_error={type(exc).__name__}:{str(exc)[:160]}")
            self._stop_strategy_worker("send_error")
            self._last_decision_source = "worker_send_error_baseline"
            return baseline

        baseline_target_logged = False
        latest_sequence = 0
        while True:
            now = time.monotonic()
            if not baseline_target_logged and now >= baseline_target:
                baseline_target_logged = True
                _log(
                    f"DECIDE baseline_target_missed decision_id={decision_id} "
                    f"target={self._decision_baseline_target_sec:.3f}s safe_action={baseline}"
                )
            remaining = min(refinement_deadline, hard_deadline) - now
            if remaining <= 0:
                timed_out_pid = self._strategy_process.pid if self._strategy_process else None
                self._stop_strategy_worker("decision_deadline", wait=False)
                decision_metrics["timed_out"] = True
                decision_metrics["worker_terminated"] = (
                    timed_out_pid is not None
                    and self._last_stopped_worker_pid == timed_out_pid
                    and self._last_stopped_worker_terminated
                    and self._last_stopped_worker_tree_termination_requested
                )
                self._last_decision_source = "refinement_deadline_latest_safe"
                _log(
                    f"DECIDE refinement_deadline decision_id={decision_id} "
                    f"latest_safe={baseline} sequence={latest_sequence} "
                    f"refinement_budget={self._decision_refinement_budget_sec:.3f}s "
                    f"hard_deadline={self._decision_hard_deadline_sec:.3f}s"
                )
                return baseline
            if not self._strategy_worker_alive():
                self._stop_strategy_worker("worker_exited")
                self._last_decision_source = "worker_exited_latest_safe"
                return baseline
            wait_for = min(remaining, 0.25)
            if not baseline_target_logged:
                wait_for = min(wait_for, max(0.001, baseline_target - now))
            try:
                if not connection.poll(wait_for):
                    continue
                message = connection.recv()
            except (BrokenPipeError, EOFError, OSError) as exc:
                _log(f"DECIDE worker_recv_error={type(exc).__name__}:{str(exc)[:160]}")
                self._stop_strategy_worker("recv_error")
                self._last_decision_source = "worker_recv_error_latest_safe"
                return baseline
            if not isinstance(message, dict):
                continue
            kind = message.get("kind")
            if kind == "ready":
                _log(
                    f"DECISION_WORKER ready pid={message.get('pid')} "
                    f"import_ms={message.get('import_elapsed_ms')} "
                    f"baseline={message.get('has_baseline')} iterator={message.get('has_iterator')}"
                )
                continue
            if kind == "startup_error":
                _log(f"DECIDE worker_startup_error={message.get('error')!r}")
                self._stop_strategy_worker("startup_error")
                self._last_decision_source = "worker_startup_error_baseline"
                return baseline
            if int(message.get("decision_id") or -1) != decision_id:
                _log(
                    f"DECIDE stale_worker_message expected={decision_id} "
                    f"actual={message.get('decision_id')} kind={kind}"
                )
                continue
            result_at = time.monotonic()
            if result_at >= hard_deadline:
                timed_out_pid = self._strategy_process.pid if self._strategy_process else None
                self._stop_strategy_worker("hard_deadline", wait=False)
                decision_metrics["timed_out"] = True
                decision_metrics["worker_terminated"] = (
                    timed_out_pid is not None
                    and self._last_stopped_worker_pid == timed_out_pid
                    and self._last_stopped_worker_terminated
                    and self._last_stopped_worker_tree_termination_requested
                )
                self._last_decision_source = "hard_deadline_latest_safe"
                _log(
                    f"DECIDE hard_deadline decision_id={decision_id} "
                    f"latest_safe={baseline} sequence={latest_sequence} "
                    f"hard_deadline={self._decision_hard_deadline_sec:.3f}s"
                )
                return baseline
            if kind == "candidate":
                sequence = int(message.get("sequence") or 0)
                if sequence <= latest_sequence:
                    continue
                latest_sequence = sequence
                decision_metrics["latest_sequence"] = sequence
                previous_action = baseline
                baseline = self._sanitize_worker_action(message.get("action"), baseline)
                phase = str(message.get("phase") or "refinement")
                req["decision_baseline_action"] = baseline
                elapsed = result_at - started
                metadata = message.get("metadata") or {}
                trusted = message.get("trusted") or {}
                if phase == "baseline":
                    decision_metrics["strategy_baseline_action"] = baseline
                    decision_metrics["baseline_published_ms"] = round(elapsed * 1000.0, 3)
                    decision_metrics["baseline_target_met"] = (
                        elapsed <= self._decision_baseline_target_sec
                    )
                    self._last_decision_source = "strategy_baseline"
                    _log(
                        f"DECIDE baseline decision_id={decision_id} action={baseline} "
                        f"elapsed={elapsed:.3f}s "
                        f"target_met={elapsed <= self._decision_baseline_target_sec}"
                    )
                else:
                    decision_metrics["refinement_messages"] += 1
                    if baseline != previous_action:
                        decision_metrics["refinement_action_changes"] += 1
                    if len(decision_metrics["refinement_progress"]) < 64:
                        decision_metrics["refinement_progress"].append({
                            "sequence": sequence,
                            "action": baseline,
                            "reported_sample_count": metadata.get("sample_count"),
                            "reported_confidence": metadata.get("confidence"),
                            "reported_complete": metadata.get("complete"),
                            "trusted_iterator_step": trusted.get("iterator_step"),
                            "trusted_process_cpu_ms": trusted.get("process_cpu_ms"),
                            "trusted_elapsed_ms": trusted.get("elapsed_ms"),
                        })
                    decision_metrics["trusted_refinement_steps"] = max(
                        int(decision_metrics["trusted_refinement_steps"]),
                        int(trusted.get("iterator_step") or 0),
                    )
                    decision_metrics["trusted_refinement_cpu_ms"] = max(
                        float(decision_metrics["trusted_refinement_cpu_ms"]),
                        float(trusted.get("process_cpu_ms") or 0.0),
                    )
                    decision_metrics["trusted_refinement_elapsed_ms"] = max(
                        float(decision_metrics["trusted_refinement_elapsed_ms"]),
                        float(trusted.get("elapsed_ms") or 0.0),
                    )
                    self._last_decision_source = "incremental_refinement"
                    _log(
                        f"DECIDE refinement decision_id={decision_id} sequence={sequence} "
                        f"action={baseline} elapsed={elapsed:.3f}s "
                        f"trusted_step={trusted.get('iterator_step')} "
                        f"trusted_cpu_ms={trusted.get('process_cpu_ms')} "
                        f"reported_samples={metadata.get('sample_count')} "
                        f"reported_confidence={metadata.get('confidence')}"
                    )
                continue
            if kind == "done":
                decision_metrics["completed"] = True
                refinement_stats = message.get("refinement_stats") or {}
                decision_metrics["trusted_refinement_steps"] = max(
                    int(decision_metrics["trusted_refinement_steps"]),
                    int(refinement_stats.get("iterator_steps") or 0),
                )
                decision_metrics["trusted_refinement_cpu_ms"] = max(
                    float(decision_metrics["trusted_refinement_cpu_ms"]),
                    float(refinement_stats.get("process_cpu_ms") or 0.0),
                )
                decision_metrics["trusted_refinement_elapsed_ms"] = max(
                    float(decision_metrics["trusted_refinement_elapsed_ms"]),
                    float(refinement_stats.get("elapsed_ms") or 0.0),
                )
                decision_metrics["refinement_iterator_exhausted"] = bool(
                    refinement_stats.get("iterator_exhausted")
                )
                decision_metrics["refinement_termination_reason"] = str(
                    refinement_stats.get("termination_reason") or ""
                )
                _log(
                    f"DECIDE worker_done decision_id={decision_id} sequence={latest_sequence} "
                    f"latest_safe={baseline} elapsed={result_at - started:.3f}s "
                    f"trusted_steps={decision_metrics['trusted_refinement_steps']} "
                    f"trusted_cpu_ms={decision_metrics['trusted_refinement_cpu_ms']} "
                    f"iterator_exhausted={decision_metrics['refinement_iterator_exhausted']} "
                    f"termination={decision_metrics['refinement_termination_reason']}"
                )
                return baseline
            if kind == "decision_error":
                _log(
                    f"DECIDE strategy_error decision_id={decision_id} "
                    f"error={message.get('error')!r} latest_safe={baseline}"
                )
                self._last_decision_source = "strategy_error_latest_safe"
                return baseline

    def _current_round_has_allin(self) -> bool:
        round_num = self._round_num()
        return any(h.get("round") == round_num and h.get("action_type") == "allin" for h in self._history)

    def _record_action(
        self,
        player_id: int,
        action_type: str,
        amount: int | None,
        committed: int = 0,
        *,
        inferred_boundary: str | None = None,
    ) -> None:
        if action_type == "call":
            action_val = 0
        elif action_type == "check":
            action_val = 0
        elif action_type == "fold":
            action_val = -1
        elif action_type == "allin":
            action_val = -2
        elif action_type == "raise" and amount is not None:
            if player_id == self._my_id:
                action_val = self._my_stage_bet
            else:
                action_val = self._opponent_stage_bet
        else:
            return
        entry = {
            "round": self._round_num(),
            "player_id": player_id,
            "action": action_val,
            "action_type": action_type,
        }
        if inferred_boundary is not None:
            entry["inferred"] = True
            entry["inference_reason"] = "official_suppressed_terminal_action"
            entry["inference_boundary"] = inferred_boundary
        if action_type in {"call", "raise", "allin"}:
            if player_id == self._my_id:
                entry["stage_bet"] = self._my_stage_bet
                entry["chips_after"] = self._my_chips
            else:
                entry["stage_bet"] = self._opponent_stage_bet
                entry["chips_after"] = self._opponent_chips
            entry["committed"] = committed
        self._history.append(entry)
        self._opponent_tracker.observe_action(
            "hero" if player_id == self._my_id else "opponent",
            self._stage,
            action_type,
            amount=entry.get("stage_bet", amount),
            committed=committed,
        )

    def _infer_suppressed_terminal_opponent_action(self, boundary: str) -> str | None:
        """Repair the official EXE's omitted peer pass before a proven boundary.

        The local server relays terminal calls/checks, while the official EXE can
        advance directly after our action.  A street/settlement/showdown boundary
        proves the peer response only when our last action still required one:
        calls close our raise/allin, postflop calls close our first check, and the
        big blind checks behind our opening small-blind limp.  Recording through
        the normal action path keeps chips, pot, current-hand history, and the
        persistent opponent tracker consistent.  If a server relayed the token,
        the last actor is already the opponent and this method is a no-op.
        """
        if self._hand_num <= 0 or self._in_allin_runout:
            return None
        round_num = self._round_num()
        round_history = [
            record for record in self._history
            if record.get("round") == round_num
        ]
        if not round_history:
            return None
        last = round_history[-1]
        if last.get("player_id") != self._my_id:
            return None

        hero_action = last.get("action_type")
        inferred_action = None
        if hero_action in {"raise", "allin"}:
            inferred_action = "call"
        elif self._stage != "preflop" and hero_action == "check":
            inferred_action = "call"
        elif (
            self._stage == "preflop"
            and self._is_sb
            and hero_action == "call"
            and len(round_history) == 1
        ):
            inferred_action = "check"
        if inferred_action is None:
            return None

        committed = self._apply_opponent_action(inferred_action, None)
        self._record_action(
            self._opponent_id,
            inferred_action,
            None,
            committed,
            inferred_boundary=boundary,
        )
        if inferred_action == "call" and (
            self._current_round_has_allin()
            or self._my_chips == 0
            or self._opponent_chips == 0
        ):
            self._in_allin_runout = True
        _log(
            f"INFER_SUPPRESSED_TERMINAL hand={self._hand_num} stage={self._stage} "
            f"action={inferred_action} committed={committed} boundary={boundary}"
        )
        return inferred_action

    def _last_raise_total(self) -> int | None:
        round_num = self._round_num()
        for record in reversed(self._history):
            if record.get("round") != round_num:
                continue
            if record.get("action_type") != "raise":
                continue
            value = record.get("stage_bet", record.get("action"))
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def _minimum_raise_total(self) -> int:
        last_raise = self._last_raise_total()
        if last_raise is not None:
            # Official legality is inclusive 2x.  Keep +1 as conservative
            # sizing headroom, not because exact 2x is illegal.
            minimum = last_raise * 2 + 1
        elif self._stage == "preflop":
            minimum = 2 * BIG_BLIND
        else:
            minimum = BIG_BLIND
        return max(minimum, self._my_stage_bet + 1, self._opponent_stage_bet + 1)

    def _apply_opponent_action(self, action_type: str, amount: int | None) -> int:
        committed = 0
        if action_type == "call":
            committed = min(max(0, self._my_stage_bet - self._opponent_stage_bet), self._opponent_chips)
        elif action_type == "raise" and amount is not None:
            committed = min(max(0, amount - self._opponent_stage_bet), self._opponent_chips)
        elif action_type == "allin":
            committed = self._opponent_chips
        if committed > 0:
            self._opponent_chips -= committed
            self._opponent_stage_bet += committed
            self._pot += committed
        return committed

    def _apply_my_action(self, action_type: str, amount: int | None) -> int:
        committed = 0
        if action_type == "call":
            committed = min(max(0, self._opponent_stage_bet - self._my_stage_bet), self._my_chips)
        elif action_type == "raise" and amount is not None:
            committed = min(max(0, amount - self._my_stage_bet), self._my_chips)
        elif action_type == "allin":
            committed = self._my_chips
        if committed > 0:
            self._my_chips -= committed
            self._my_stage_bet += committed
            self._pot += committed
        return committed

    def _raise_action(self, requested_total: int) -> tuple[str, str, int | None]:
        if self._current_round_has_allin():
            return self._zero_action()
        target = max(int(requested_total), self._minimum_raise_total())
        needed = target - self._my_stage_bet
        if needed <= 0:
            return self._zero_action()
        if needed >= self._my_chips:
            return "allin", "allin", None
        return f"raise {target}", "raise", target

    def _zero_action(self) -> tuple[str, str, int | None]:
        if self._opponent_stage_bet > self._my_stage_bet:
            return "call", "call", None
        if self._responding_to_check():
            return "call", "call", None
        return "check", "check", None

    def _action_to_tcp(self, action: int) -> tuple[str, str, int | None]:
        if action == -1:
            return "fold", "fold", None
        if action == -2:
            if self._current_round_has_allin():
                return self._zero_action()
            if self._opponent_chips == 0 and self._opponent_stage_bet > self._my_stage_bet:
                return "call", "call", None
            return "allin", "allin", None
        if action > 0:
            if self._current_round_has_allin():
                return self._zero_action()
            if action <= self._my_stage_bet:
                return self._zero_action()
            if self._opponent_stage_bet > self._my_stage_bet and action <= self._opponent_stage_bet:
                return "call", "call", None
            return self._raise_action(action)
        return self._zero_action()

    def _should_respond(self, action_type: str) -> bool:
        if action_type == "fold":
            return False
        if action_type in {"raise", "allin"}:
            return True
        if action_type == "call":
            return self._stage == "preflop" and not self._is_sb and self._my_action_count == 0
        if action_type == "check":
            return self._stage != "preflop" and self._my_action_count == 0
        return False

    def _send_wire_action(self, sock: socket.socket, msg: str) -> None:
        if self._official_action_delay_sec > 0 and self._last_platform_message_at > 0:
            elapsed = time.perf_counter() - self._last_platform_message_at
            wait_sec = self._official_action_delay_sec - elapsed
            if wait_sec > 0:
                _log(f"OFFICIAL_ACTION_DELAY wait={wait_sec:.3f}s target={self._official_action_delay_sec:.3f}s")
                time.sleep(wait_sec)
        sock.sendall(msg.encode("utf-8"))

    def _send_decision(self, sock: socket.socket) -> None:
        t0 = time.perf_counter()
        _log(
            f"DECIDE start name={self.name} hand={self._hand_num} stage={self._stage} "
            f"act_cnt={self._my_action_count} my_sb={self._my_stage_bet} "
            f"opp_sb={self._opponent_stage_bet} my_chips={self._my_chips} "
            f"opp_chips={self._opponent_chips} is_sb={self._is_sb}"
        )
        try:
            action = self._strategy_action()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            _log("DECIDE exception -> fold")
            action = -1
        elapsed = time.perf_counter() - t0
        _log(
            f"DECIDE done action={action!r} source={self._last_decision_source} "
            f"elapsed={elapsed:.3f}s"
        )
        self._responses.append(int(action))
        msg, action_type, amount = self._action_to_tcp(int(action))
        self._send_wire_action(sock, msg)
        _log(
            f"SEND name={self.name} hand={self._hand_num} stage={self._stage} "
            f"act_cnt={self._my_action_count} my_sb={self._my_stage_bet} "
            f"opp_sb={self._opponent_stage_bet} my_chips={self._my_chips} "
            f"opp_chips={self._opponent_chips} is_sb={self._is_sb} msg={msg!r}"
        )
        committed = self._apply_my_action(action_type, amount)
        self._record_action(self._my_id, action_type, amount, committed)
        if action_type == "call" and (self._current_round_has_allin() or self._my_chips == 0 or self._opponent_chips == 0):
            self._in_allin_runout = True
        self._my_action_count += 1

    def handle(self, line: str, sock: socket.socket) -> None:
        self._last_platform_message_at = time.perf_counter()
        if line.startswith("name"):
            sock.sendall(self.name.encode("utf-8"))
            _log(f"SEND name_handshake name={self.name!r}")
            self._ensure_strategy_worker()
            return
        if line.startswith("preflop"):
            # Normal hands settle before the next preflop token.  Keep this
            # boundary defensive and idempotent so an already relayed/repaired
            # closer is never duplicated, while no prior-hand state is cleared
            # before a boundary-proven omitted call/check has been recorded.
            self._infer_suppressed_terminal_opponent_action("hand_start")
            parts = line.split("|", 2)
            blind = parts[1]
            self._is_sb = blind == "SMALLBLIND"
            self._my_cards = _parse_cards(parts[2])
            self._public_cards = []
            self._stage = "preflop"
            self._hand_num += 1
            self._history = []
            self._my_action_count = 0
            self._my_chips = INITIAL_CHIPS
            self._opponent_chips = INITIAL_CHIPS
            self._pot = SMALL_BLIND + BIG_BLIND
            self._in_allin_runout = False
            if self._is_sb:
                self._my_chips -= SMALL_BLIND
                self._my_stage_bet = SMALL_BLIND
                self._opponent_chips -= BIG_BLIND
                self._opponent_stage_bet = BIG_BLIND
            else:
                self._my_chips -= BIG_BLIND
                self._my_stage_bet = BIG_BLIND
                self._opponent_chips -= SMALL_BLIND
                self._opponent_stage_bet = SMALL_BLIND
            self._dealer_id = 0 if self._is_sb else 1
            self._opponent_tracker.begin_hand(
                self._hand_num,
                opponent_is_sb=not self._is_sb,
            )
            if self._is_sb:
                self._send_decision(sock)
            return
        if line.startswith(("flop", "turn", "river")):
            stage, cards = line.split("|", 1)
            self._infer_suppressed_terminal_opponent_action(f"street:{stage}")
            self._stage = stage
            self._public_cards.extend(_parse_cards(cards))
            self._my_action_count = 0
            self._my_stage_bet = 0
            self._opponent_stage_bet = 0
            self._opponent_tracker.begin_street()
            if not self._in_allin_runout and self._acts_first_postflop():
                self._send_decision(sock)
            return
        if line.startswith("earnChips"):
            earned = int(line.split()[1])
            self._infer_suppressed_terminal_opponent_action("settlement")
            self._last_earned = earned
            self._earned_by_hand[self._hand_num] = earned
            self._total_win_chips[self._my_id] += earned
            self._total_win_chips[self._opponent_id] -= earned
            if earned > 0:
                self._total_win_games[self._my_id] += 1
            elif earned < 0:
                self._total_win_games[self._opponent_id] += 1
            showdown = self._showdown_by_hand.get(self._hand_num)
            if showdown is not None:
                showdown["earned"] = earned
            self._opponent_tracker.observe_settlement(
                self._hand_num,
                hero_earned=earned,
            )
            _log(
                "OPPONENT_TRACKER "
                + json.dumps(
                    self._opponent_tracker.snapshot(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            self._in_allin_runout = False
            return
        if line.startswith("oppo_hands|"):
            self._infer_suppressed_terminal_opponent_action("showdown")
            record = self._showdown_by_hand.get(self._hand_num)
            if record is None:
                record = {
                    "hand": self._hand_num,
                    "opponent_cards": _parse_cards(line.split("|", 1)[1]),
                    "my_cards": list(self._my_cards),
                    "public_cards": list(self._public_cards),
                    "history": list(self._history),
                    "earned": self._earned_by_hand.get(self._hand_num),
                }
                self._showdown_by_hand[self._hand_num] = record
                self._showdowns.append(record)
            self._opponent_tracker.observe_showdown(
                self._hand_num,
                record["opponent_cards"],
                record["public_cards"],
            )
            return

        action_type, amount = _parse_action(line)
        if action_type == "unknown":
            _log(f"UNKNOWN message={line!r}")
            return
        committed = self._apply_opponent_action(action_type, amount)
        self._record_action(self._opponent_id, action_type, amount, committed)
        if action_type == "call" and (self._current_round_has_allin() or self._my_chips == 0 or self._opponent_chips == 0):
            self._in_allin_runout = True
        if self._should_respond(action_type):
            self._send_decision(sock)


def run_client(host: str, port: int, name: str, log_path: str = "", seat: str = "auto") -> int:
    _log_open(log_path)
    bot = NativeNationalBot(name, seat)
    _log(f"START name={name} seat={bot.seat} host={host} port={port} log={log_path or '-'}")
    try:
        with socket.create_connection((host, port), timeout=30) as sock:
            sock.settimeout(180)
            decoder = NationalStreamDecoder()
            while True:
                try:
                    data = sock.recv(4096)
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
                    _log(f"RECV closed_by_server exception={type(exc).__name__}: {exc}")
                    return 0
                if not data:
                    _log("RECV empty -> server closed")
                    return 0
                chunk = data.decode("utf-8", "replace")
                messages = decoder.feed(chunk)
                _log(f"RECV raw={chunk!r} buffer={decoder.buffer!r}")
                for line in messages:
                    _log(f"DISPATCH line={line!r}")
                    bot.handle(line, sock)
                if decoder.has_pending_numeric:
                    readable, _, _ = select.select([sock], [], [], _stream_idle_flush_sec())
                    if not readable:
                        messages = decoder.flush_idle()
                        _log(f"RECV idle_flush buffer={decoder.buffer!r}")
                        for line in messages:
                            _log(f"DISPATCH line={line!r}")
                            bot.handle(line, sock)
    finally:
        bot.close()


def main() -> int:
    mp.freeze_support()
    parser = argparse.ArgumentParser(description="Native national TCP bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", default="Bot")
    parser.add_argument("--seat", choices=("auto", "upper", "lower"), default="auto",
                        help="Desktop seat hint; action order is still inferred from blind state.")
    parser.add_argument("--log", default="", help="Log file path. Empty disables file logging.")
    args = parser.parse_args()
    try:
        return run_client(args.host, args.port, args.name, args.log, args.seat)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
'''
NATIVE_BOT_TEMPLATE = NATIVE_BOT_TEMPLATE.replace(
    "__POK_DECISION_RUNTIME_VERSION__",
    str(NATIONAL_DECISION_RUNTIME_VERSION),
)


NATIVE_PRECOMPUTE_TEMPLATE = r'''"""Bounded pure poker facts built once before live decisions."""

from __future__ import annotations

import hashlib
import itertools
import json


PRECOMPUTE_SCHEMA_VERSION = 1
CARD_ENCODING = "judge_int:rank=card//4+2,suit=card%4"
GENERATOR_VERSION = "national-precompute-v1"
FIVE_OF_SEVEN_INDICES = tuple(itertools.combinations(range(7), 5))


def _hole_fact(card_a: int, card_b: int) -> tuple[int, int, bool, bool, int]:
    rank_a, rank_b = card_a // 4 + 2, card_b // 4 + 2
    high, low = max(rank_a, rank_b), min(rank_a, rank_b)
    return high, low, card_a % 4 == card_b % 4, high == low, high - low


def _straight_high(rank_mask: int) -> int:
    mask = int(rank_mask) & 0x1FFF
    for high_index in range(12, 3, -1):
        window = 0b11111 << (high_index - 4)
        if mask & window == window:
            return high_index + 2
    wheel = (1 << 12) | 0b1111
    return 5 if mask & wheel == wheel else 0


HOLE_COMBO_FACTS = {
    (card_a, card_b): _hole_fact(card_a, card_b)
    for card_a in range(52)
    for card_b in range(card_a + 1, 52)
}
STRAIGHT_HIGH_BY_MASK = {
    rank_mask: _straight_high(rank_mask)
    for rank_mask in range(1 << 13)
}


def _content_digest() -> str:
    payload = {
        "five_of_seven": FIVE_OF_SEVEN_INDICES,
        "hole_combo_facts": sorted((list(key), list(value)) for key, value in HOLE_COMBO_FACTS.items()),
        "straight_high": [STRAIGHT_HIGH_BY_MASK[index] for index in range(1 << 13)],
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii")
    ).hexdigest()


PRECOMPUTE_MANIFEST = {
    "schema_version": PRECOMPUTE_SCHEMA_VERSION,
    "generator_version": GENERATOR_VERSION,
    "card_encoding": CARD_ENCODING,
    "hole_combo_entries": len(HOLE_COMBO_FACTS),
    "straight_mask_entries": len(STRAIGHT_HIGH_BY_MASK),
    "five_of_seven_entries": len(FIVE_OF_SEVEN_INDICES),
    "content_digest": _content_digest(),
}


def hole_combo_fact(card_a: int, card_b: int):
    key = (card_a, card_b) if card_a < card_b else (card_b, card_a)
    return HOLE_COMBO_FACTS.get(key)


def straight_high(rank_mask: int) -> int:
    return STRAIGHT_HIGH_BY_MASK.get(int(rank_mask) & 0x1FFF, 0)
'''


@dataclass(frozen=True)
class NativeBotSpec:
    label: str
    path: Path
    entry: Path
    temp_root: Path | None = None
    wrapper_used: bool = False


def ensure_native_entry(bot_dir: str | Path, *, overwrite: bool = False) -> Path:
    bot_dir = Path(bot_dir)
    entry = bot_dir / NATIVE_ENTRY
    if overwrite or not entry.exists():
        entry.write_text(NATIVE_BOT_TEMPLATE, encoding="utf-8")
    precompute = bot_dir / PRECOMPUTE_ENTRY
    if not precompute.exists():
        precompute.write_text(NATIVE_PRECOMPUTE_TEMPLATE, encoding="utf-8")
    return entry


_NATIVE_STREAM_PROBE_SCRIPT = r'''
import contextlib
import io
import json
import runpy
import sys

entry = sys.argv[1]
captured = io.StringIO()
errors = []
try:
    with contextlib.redirect_stdout(captured):
        namespace = runpy.run_path(entry, run_name="_national_stream_contract_probe")
except BaseException as exc:
    errors.append(f"entry_load:{type(exc).__name__}:{exc}")
    namespace = {}
if captured.getvalue():
    errors.append(f"stdout_pollution:{captured.getvalue()[:160]!r}")

decoder_class = namespace.get("NationalStreamDecoder")
cases = (
    ("raise 200", ["raise 200"]),
    ("earnChips -100", ["earnChips -100"]),
    ("raise 200call", ["raise 200", "call"]),
    ("raise 200earnChips -100", ["raise 200", "earnChips -100"]),
    (
        "earnChips -100preflop|SMALLBLIND|<0,3><1,3>",
        ["earnChips -100", "preflop|SMALLBLIND|<0,3><1,3>"],
    ),
    ("allinriver|<3,12>", ["allin", "river|<3,12>"]),
)

def decode(chunks):
    decoder = decoder_class()
    emitted = []
    for chunk in chunks:
        emitted.extend(decoder.feed(chunk))
    emitted.extend(decoder.flush_idle())
    return emitted, decoder.buffer

if decoder_class is None:
    errors.append("missing_decoder_class")
else:
    for raw, expected in cases:
        chunkings = [(raw,)]
        chunkings.extend((raw[:split], raw[split:]) for split in range(1, len(raw)))
        chunkings.append(tuple(raw))
        for chunks in chunkings:
            try:
                actual, remainder = decode(chunks)
            except BaseException as exc:
                errors.append(
                    f"decode_exception:{raw!r}:{type(exc).__name__}:{exc}"
                )
                break
            if actual != expected or remainder:
                errors.append(
                    f"decode_mismatch:{raw!r}:chunks={chunks!r}:"
                    f"actual={actual!r}:remainder={remainder!r}"
                )
                break

print(json.dumps({"errors": errors[:20]}, ensure_ascii=True))
'''


def check_native_stream_decoder(bot_dir: str | Path) -> list[str]:
    """Behaviorally verify the current delimiter-free stream decoder contract."""

    bot_dir = Path(bot_dir)
    entry = bot_dir / NATIVE_ENTRY
    if not entry.exists():
        return [f"{NATIVE_ENTRY} missing; cannot verify stream decoder"]
    try:
        text = entry.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{NATIVE_ENTRY} unreadable: {exc}"]

    required_tokens = (
        "NATIONAL_STREAM_DECODER_VERSION = 2",
        "class NationalStreamDecoder",
        "has_pending_numeric",
        "flush_idle",
        "select.select",
    )
    missing = [token for token in required_tokens if token not in text]
    if missing:
        return [
            f"{NATIVE_ENTRY}: missing stream decoder v2 token {token!r}; "
            "new candidates must defer terminal numeric messages until a following token or idle flush"
            for token in missing
        ]

    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _NATIVE_STREAM_PROBE_SCRIPT, str(entry.resolve())],
            cwd=str(bot_dir),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"{NATIVE_ENTRY}: stream decoder behavior probe failed: {type(exc).__name__}: {exc}"]
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "no output")[-500:].strip()
        return [
            f"{NATIVE_ENTRY}: stream decoder behavior probe exited {proc.returncode}: {detail}"
        ]
    try:
        payload = json.loads(proc.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return [
            f"{NATIVE_ENTRY}: stream decoder behavior probe returned invalid JSON: "
            f"{type(exc).__name__}: {proc.stdout[-300:]!r}"
        ]
    return [
        f"{NATIVE_ENTRY}: stream decoder behavior violation: {item}"
        for item in (payload.get("errors") or [])[:20]
    ]


def check_native_contract(
    bot_dir: str | Path,
    *,
    require_current_stream_decoder: bool = False,
    require_current_decision_runtime: bool = False,
) -> list[str]:
    bot_dir = Path(bot_dir)
    entry = bot_dir / NATIVE_ENTRY
    errors: list[str] = []
    if not entry.exists():
        return [f"{NATIVE_ENTRY} missing; national_native bots must have a direct TCP entrypoint"]
    try:
        text = entry.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{NATIVE_ENTRY} unreadable: {exc}"]
    forbidden = ("bot_adapter", "BotAdapter", '"response"', "'response'")
    for token in forbidden:
        if token in text:
            errors.append(f"{NATIVE_ENTRY}: forbidden legacy adapter/JSON response token {token!r}")
    legacy_wire_tokens = (
        "makefile(",
        ".readline(",
        "readline()",
        "newline=\"\\n\"",
        "newline='\\n'",
        "msg + \"\\n\"",
        "msg + '\\n'",
        "self.name + \"\\n\"",
        "self.name + '\\n'",
    )
    for token in legacy_wire_tokens:
        if token in text:
            errors.append(f"{NATIVE_ENTRY}: forbidden legacy newline TCP token {token!r}")
    required = ("socket", "raise ", "fold", "call", "check", "allin")
    for token in required:
        if token not in text:
            errors.append(f"{NATIVE_ENTRY}: missing native TCP token {token!r}")
    for token in ("sock.recv", "_split_messages"):
        if token not in text:
            errors.append(f"{NATIVE_ENTRY}: missing official raw TCP splitter token {token!r}")
    official_delay_tokens = ("POK_OFFICIAL_ACTION_DELAY", "_send_wire_action", "DEFAULT_OFFICIAL_ACTION_DELAY_SEC")
    for token in official_delay_tokens:
        if token not in text:
            errors.append(
                f"{NATIVE_ENTRY}: missing official EXE action throttle token {token!r}; "
                "native bots must delay action sends for the official Windows platform"
            )
    if "wire value is the extra chips added" in text:
        errors.append(f"{NATIVE_ENTRY}: TCP raise amount is documented as an increment; it must be raise-to-total")
    if "committed = min(max(0, amount), self._opponent_chips)" in text:
        errors.append(f"{NATIVE_ENTRY}: opponent raise amount is treated as an increment; it must be raise-to-total")
    if "return f\"raise {needed}\", \"raise\", action" in text:
        errors.append(f"{NATIVE_ENTRY}: outgoing raise uses delta-style wire amount; it must send raise-to-total")
    formal_wrapper = "class NativeNationalBot" in text or "def _action_to_tcp" in text or "def _zero_action" in text
    if formal_wrapper:
        action_to_tcp = _function_source(text, "_action_to_tcp")
        if action_to_tcp is None:
            errors.append(f"{NATIVE_ENTRY}: missing _action_to_tcp protocol translator")
        elif "self._current_round_has_allin()" not in action_to_tcp:
            errors.append(
                f"{NATIVE_ENTRY}: _action_to_tcp missing current-round allin guard; "
                "after any allin it must map strategy raises/allins to call/fold/check-safe actions"
            )
        zero_action = _function_source(text, "_zero_action")
        if zero_action is None:
            errors.append(f"{NATIVE_ENTRY}: missing _zero_action call/check mapper")
        elif "_responding_to_check()" not in zero_action:
            errors.append(
                f"{NATIVE_ENTRY}: _zero_action missing postflop check-response guard; "
                "second pass after an opponent check must be call, not check"
            )
    if _strategy_action_has_exception_pass(text):
        errors.append(
            f"{NATIVE_ENTRY}: _strategy_action must not continue with raw action after sanitizer failure"
        )
    if require_current_stream_decoder:
        errors.extend(check_native_stream_decoder(bot_dir))
    if require_current_decision_runtime:
        runtime_tokens = (
            f"NATIONAL_DECISION_RUNTIME_VERSION = {NATIONAL_DECISION_RUNTIME_VERSION}",
            "decision_hard_deadline_monotonic",
            "decision_refinement_deadline_monotonic",
            "decision_baseline_target_ms",
            "mp.get_context(\"spawn\")",
            "def _strategy_worker_main",
            'os.environ["POK_NATIVE_BOT_SEED"]',
            "random.seed(int(random_seed))",
            "iter_refinements",
            "decision_id",
            "process.terminate()",
            "process.kill()",
            "daemon=False",
            "os.killpg",
            '"taskkill"',
            "trusted_refinement_steps",
            "reported_sample_count",
            "def _reap_retired_strategy_workers",
            '_stop_strategy_worker("decision_deadline", wait=False)',
            "def _socket_safe_fallback_action",
            "return -1 if self._opponent_stage_bet > self._my_stage_bet else 0",
            "def _infer_suppressed_terminal_opponent_action",
            "official_suppressed_terminal_action",
        )
        for token in runtime_tokens:
            if token not in text:
                errors.append(
                    f"{NATIVE_ENTRY}: missing current bounded decision runtime token {token!r}"
                )
    return errors


def _function_source(text: str, name: str) -> str | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    return None


def _strategy_action_has_exception_pass(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_strategy_action":
            for child in ast.walk(node):
                if isinstance(child, ast.Try):
                    for handler in child.handlers:
                        if (
                            _handler_catches_broad_exception(handler)
                            and len(handler.body) == 1
                            and isinstance(handler.body[0], ast.Pass)
                        ):
                            return True
    return False


def _handler_catches_broad_exception(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in {"Exception", "BaseException"}
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(item, ast.Name) and item.id in {"Exception", "BaseException"}
            for item in handler.type.elts
        )
    return False


def _bot_version(label: str) -> int:
    return parse_bot_version(label) or -1


def resolve_bot(token: str | Path) -> tuple[str, Path]:
    token_str = str(token)
    raw = Path(token_str)
    candidates: list[Path] = []
    if raw.exists():
        candidates.append(raw)
    if token_str.startswith("v") and token_str[1:].isdigit():
        candidates.append(ROOT / "bots" / bot_name(token_str[1:]))
    if token_str.isdigit():
        candidates.append(ROOT / "bots" / bot_name(token_str))
        candidates.append(ROOT / "bots" / f"bot{token_str}")
    if token_str.startswith(ACTIVE_BOT_PREFIX) or token_str.startswith("claude_v") or token_str.startswith("bot"):
        candidates.append(ROOT / "bots" / token_str)
    for path in candidates:
        if path.is_dir() and (
            (path / NATIVE_ENTRY).is_file() or (path / "main.py").is_file()
        ):
            return path.name, path.resolve()
        if path.is_file() and path.name in {NATIVE_ENTRY, "main.py"}:
            return path.parent.name, path.resolve().parent
    raise ValueError(
        f"bot not found or missing {NATIVE_ENTRY}/main.py entry: {token_str}"
    )


def _completed_active_bots() -> list[tuple[str, Path]]:
    from evolution_infra import get_active_bots

    specs: list[tuple[str, Path]] = []
    for name in get_active_bots():
        path = ROOT / "bots" / name
        if path.is_dir() and (path / NATIVE_ENTRY).is_file():
            specs.append((name, path.resolve()))
    return sorted(specs, key=lambda item: version_sort_key(item[0]), reverse=True)


def select_acceptance_opponents(candidate_label: str, source_v: int | None, limit: int = 2) -> list[tuple[str, Path]]:
    chosen: list[tuple[str, Path]] = []
    seen = {candidate_label}

    def add(spec: tuple[str, Path]):
        if spec[0] not in seen and spec[1].exists():
            chosen.append(spec)
            seen.add(spec[0])

    if source_v is not None:
        try:
            add(resolve_bot(bot_name(source_v)))
        except ValueError:
            pass
    for spec in _completed_active_bots():
        add(spec)
        if len(chosen) >= limit:
            break
    return chosen[:limit]


def _prepare_native_spec(
    label: str,
    bot_dir: Path,
    *,
    allow_legacy_wrapper: bool = False,
) -> NativeBotSpec:
    """Resolve an existing native entry, optionally wrapping a copied legacy bot.

    The strict path never writes to ``bot_dir``. Wrapper generation is reserved
    for the explicitly named legacy/debug runner and only touches a temporary
    copy of the source bot.
    """
    entry = bot_dir / NATIVE_ENTRY
    if entry.exists():
        contract_errors = check_native_contract(bot_dir)
        if not contract_errors:
            return NativeBotSpec(label=label, path=bot_dir, entry=entry)
        if not allow_legacy_wrapper:
            raise ValueError(f"{label}: invalid {NATIVE_ENTRY}: {'; '.join(contract_errors[:3])}")
    if not allow_legacy_wrapper:
        raise ValueError(f"{label}: missing required {NATIVE_ENTRY}")
    tmp = Path(tempfile.mkdtemp(prefix=f"pok_native_{label}_"))
    dst = tmp / bot_dir.name
    shutil.copytree(bot_dir, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return NativeBotSpec(
        label=label,
        path=dst,
        entry=ensure_native_entry(dst, overwrite=True),
        temp_root=tmp,
        wrapper_used=True,
    )


def _cleanup_specs(specs: list[NativeBotSpec]) -> None:
    for spec in specs:
        if spec.temp_root is not None:
            shutil.rmtree(spec.temp_root, ignore_errors=True)


def _native_bot_seed(bot_seed_base: int | None, player_idx: int) -> int | None:
    if bot_seed_base is None:
        return None
    return int(bot_seed_base) + int(player_idx)


def _parse_decision_trace(stderr_text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_line in stderr_text.splitlines():
        if not raw_line.startswith(TRACE_PREFIX):
            continue
        payload = raw_line[len(TRACE_PREFIX):]
        try:
            row = json.loads(payload)
        except Exception:
            rows.append({"type": "parse_error", "raw": payload[:1000]})
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _safe_label_fragment(label: str) -> str:
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in "_.-") else "_"
        for char in label
    )
    return safe[:80] or "bot"


async def _run_tcp_server_with_processes(
    bot_a: NativeBotSpec,
    bot_b: NativeBotSpec,
    *,
    hands: int,
    timeout_sec: float,
    deck_seed_base: int | None,
    bot_seed_base: int | None = None,
    bot_a_env_overrides: dict[str, str | int | None] | None = None,
    bot_b_env_overrides: dict[str, str | int | None] | None = None,
    capture_events: bool = False,
) -> dict[str, Any]:
    clients: list[NationalTCPClient] = []
    connected = asyncio.Event()
    events: list[dict[str, Any]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if len(clients) >= 2:
            writer.close()
            await writer.wait_closed()
            return
        clients.append(NationalTCPClient(reader, writer, idle_flush_sec=0.003))
        if len(clients) == 2:
            connected.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    run_labels = [bot_a.label, bot_b.label]
    if run_labels[0] == run_labels[1]:
        run_labels = [f"{run_labels[0]}_A", f"{run_labels[1]}_B"]
    procs: list[subprocess.Popen] = []
    proc_streams = []
    stdout_stderr: dict[str, dict[str, Any]] = {}
    log_temp_root = Path(tempfile.mkdtemp(prefix="pok_native_logs_"))
    bot_log_paths: dict[str, Path] = {}
    engine = None
    run_error = ""
    connect_timeout = max(1.0, min(20.0, float(timeout_sec) / 3.0))
    name_timeout = max(1.0, min(30.0, float(timeout_sec) / 3.0))
    action_timeout = max(1.0, min(60.0, float(timeout_sec)))
    process_drain_timeout = max(1.0, min(5.0, float(timeout_sec) / 6.0))
    bot_seeds: dict[str, int | None] = {}
    try:
        env_overrides = (bot_a_env_overrides or {}, bot_b_env_overrides or {})
        for idx, (spec, label) in enumerate(zip((bot_a, bot_b), run_labels)):
            base_environment = os.environ.copy()
            for key, value in env_overrides[idx].items():
                if value is None:
                    base_environment.pop(str(key), None)
                else:
                    base_environment[str(key)] = str(value)
            action_delay = base_environment.get("POK_NATIVE_LOCAL_ACTION_DELAY", "0")
            default_local_hard_deadline = max(
                0.05,
                min(
                    LOCAL_NATIVE_STRENGTH_HARD_DEADLINE_SEC,
                    action_timeout - 0.25,
                ),
            )
            local_hard_deadline_raw = base_environment.get(
                "POK_NATIVE_DECISION_HARD_DEADLINE_SEC",
                str(default_local_hard_deadline),
            )
            try:
                local_hard_deadline_value = max(
                    0.05,
                    min(55.0, float(local_hard_deadline_raw)),
                )
            except (TypeError, ValueError):
                local_hard_deadline_value = default_local_hard_deadline
            default_refinement_budget = max(
                0.04,
                min(
                    LOCAL_NATIVE_STRENGTH_REFINEMENT_BUDGET_SEC,
                    local_hard_deadline_value - 0.10,
                ),
            )
            refinement_budget_raw = base_environment.get(
                "POK_NATIVE_DECISION_REFINEMENT_BUDGET_SEC",
                str(default_refinement_budget),
            )
            refinement_ceiling = max(
                0.04,
                local_hard_deadline_value
                - min(0.10, local_hard_deadline_value * 0.10),
            )
            try:
                refinement_budget = max(
                    0.04,
                    min(float(refinement_budget_raw), refinement_ceiling),
                )
            except (TypeError, ValueError):
                refinement_budget = min(
                    default_refinement_budget,
                    refinement_ceiling,
                )
            default_baseline_target = min(
                LOCAL_NATIVE_STRENGTH_BASELINE_TARGET_SEC,
                max(0.01, local_hard_deadline_value * 0.25),
            )
            baseline_target_raw = base_environment.get(
                "POK_NATIVE_DECISION_BASELINE_TARGET_SEC",
                str(default_baseline_target),
            )
            baseline_ceiling = max(
                0.01,
                refinement_budget - min(0.05, refinement_budget * 0.10),
            )
            try:
                baseline_target = max(
                    0.01,
                    min(float(baseline_target_raw), baseline_ceiling),
                )
            except (TypeError, ValueError):
                baseline_target = min(default_baseline_target, baseline_ceiling)
            seed = _native_bot_seed(bot_seed_base, idx)
            bot_seeds[label] = seed
            log_path = None
            if native_entry_supports_log_arg(spec.entry):
                log_path = log_temp_root / f"{idx}_{_safe_label_fragment(label)}.log"
                bot_log_paths[label] = log_path
            launch = build_native_bot_launch(
                bot_dir=spec.path,
                entry=spec.entry,
                label=label,
                host=str(host),
                port=int(port),
                action_delay=float(action_delay),
                hard_deadline=local_hard_deadline_value,
                refinement_budget=refinement_budget,
                baseline_target=baseline_target,
                decision_log=log_path,
                seed=seed,
                base_environment=base_environment,
                inherit_all_environment=True,
            )
            stdout_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            stderr_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    list(launch.command),
                    cwd=str(launch.cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    env=launch.environment,
                )
            except Exception:
                stdout_file.close()
                stderr_file.close()
                raise
            proc_streams.append((stdout_file, stderr_file))
            procs.append(proc)
        await asyncio.wait_for(connected.wait(), timeout=connect_timeout)
        await clients[0].send_line("name")
        await clients[1].send_line("name")
        name0 = await clients[0].recv_name(timeout=name_timeout)
        name1 = await clients[1].recv_name(timeout=name_timeout)
        if not name0 or not name1:
            raise RuntimeError("native TCP bot name handshake failed")
        clients[0].name = name0
        clients[1].name = name1
        ordered_clients = clients
        clients_by_name = {client.name: client for client in clients}
        if run_labels[0] in clients_by_name and run_labels[1] in clients_by_name:
            ordered_clients = [clients_by_name[run_labels[0]], clients_by_name[run_labels[1]]]
            if ordered_clients != clients:
                events.append({
                    "type": "client_order",
                    "order": list(run_labels),
                    "connection_order": [name0, name1],
                })
        engine = NationalTCPGameEngine(
            ordered_clients,
            events,
            deck_seed_base=deck_seed_base,
            action_timeout_sec=action_timeout,
        )
        await asyncio.wait_for(engine.run_limited_match(name0, name1, hands), timeout=timeout_sec)
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        try:
            server.close()
            for client in clients:
                await client.close(timeout=process_drain_timeout)
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=process_drain_timeout)
            except asyncio.TimeoutError:
                pass
            for label, proc, streams in zip(run_labels, procs, proc_streams):
                stdout_file, stderr_file = streams
                stderr_note = ""
                try:
                    proc.wait(timeout=process_drain_timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=process_drain_timeout)
                    except subprocess.TimeoutExpired:
                        stderr_note = "process did not exit after kill"
                stdout_file.seek(0)
                stderr_file.seek(0)
                out = stdout_file.read() or ""
                err = stderr_file.read() or ""
                if stderr_note:
                    err = (err + "\n" + stderr_note).strip()
                bot_log_text = ""
                bot_log_path = bot_log_paths.get(label)
                if bot_log_path is not None:
                    try:
                        bot_log_text = bot_log_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except OSError as exc:
                        err = (err + f"\nfailed to read bot log: {exc}").strip()
                stdout_file.close()
                stderr_file.close()
                stdout_stderr[label] = {
                    "returncode": proc.returncode,
                    "stdout": out or "",
                    "stderr": err or "",
                    "bot_log": bot_log_text,
                    "bot_log_supported": label in bot_log_paths,
                }
        finally:
            shutil.rmtree(log_temp_root, ignore_errors=True)

    illegal = {
        0: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 0 and str(e.get("action", "")).startswith("illegal:")),
        1: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 1 and str(e.get("action", "")).startswith("illegal:")),
    }
    timeouts = {
        0: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 0 and e.get("action") == "timeout"),
        1: sum(1 for e in events if e.get("type") == "action" and e.get("player_idx") == 1 and e.get("action") == "timeout"),
    }
    earnings = getattr(engine, "total_earnings", [0, 0]) if engine is not None else [0, 0]
    hands_played = int(getattr(engine, "hand_num", 0) or 0) if engine is not None else 0
    settlements = [
        {
            "hand": int(event.get("hand", 0) or 0),
            "earnings": [int(value) for value in event.get("earnings", [0, 0])],
            "pot": int(event.get("pot", 0) or 0),
            "is_showdown": bool(event.get("is_showdown", False)),
            "winner_idx": event.get("winner_idx"),
            "reason": event.get("reason", ""),
        }
        for event in events
        if event.get("type") == "settle"
    ]
    per_player = {}
    issues: list[str] = []
    if run_error:
        issues.append(f"native_tcp_match_error={run_error}")
    for idx, label in enumerate(run_labels):
        spec = (bot_a, bot_b)[idx]
        proc_info = stdout_stderr.get(label, {})
        proc_failed = bool(proc_info.get("returncode") not in (0, None))
        stdout_text = str(proc_info.get("stdout") or "")
        stderr_text = str(proc_info.get("stderr") or "")
        bot_log_text = str(proc_info.get("bot_log") or "")
        decision_trace = _parse_decision_trace(stderr_text)
        bot_log_summary = (
            _parse_native_bot_log(bot_log_text)
            if bot_log_text
            else _empty_bot_log_summary()
        )
        runtime_telemetry = {
            "schema_version": 1,
            "server_action_latency": _server_action_latency(events, idx),
            "bot_log_supported": bool(proc_info.get("bot_log_supported")),
            "bot_log": bot_log_summary,
            "trace_decision_count": len(decision_trace),
        }
        per_player[label] = {
            "earnings": int(earnings[idx]),
            "illegal_actions": illegal[idx],
            "timeouts": timeouts[idx],
            "wrapper_used": spec.wrapper_used,
            "runtime_telemetry": runtime_telemetry,
            "native": {
                "returncode": proc_info.get("returncode"),
                "bot_seed": bot_seeds.get(label),
                "stdout_tail": stdout_text[-2000:] if stdout_text else "",
                "stderr_tail": stderr_text[-2000:] if stderr_text else "",
                "bot_log_supported": bool(proc_info.get("bot_log_supported")),
                "decision_trace": decision_trace,
                "process_failures": 1 if proc_failed else 0,
                "json_response_stdout": 1 if '"response"' in stdout_text or "'response'" in stdout_text else 0,
            },
            "adapter": {
                "bot_failures": 0,
                "invalid_actions": 0,
                "actions_sent": 0,
                "clamped_raises": 0,
                "allin_conversions": 0,
                "would_be_illegal_raise": 0,
                "postflop_pass_conversions": 0,
            },
        }
        player_issues = []
        if illegal[idx]:
            player_issues.append(f"{label}: illegal_actions={illegal[idx]}")
        if timeouts[idx]:
            player_issues.append(f"{label}: timeouts={timeouts[idx]}")
        if proc_failed:
            player_issues.append(f"{label}: native_process_returncode={proc_info.get('returncode')}")
        if per_player[label]["native"]["json_response_stdout"]:
            player_issues.append(f"{label}: json_response_stdout")
        per_player[label]["compliance_issues"] = player_issues
        per_player[label]["passed_compliance"] = not player_issues
        issues.extend(player_issues)
    if hands_played != hands:
        issues.append(f"hands_played={hands_played}, expected={hands}")
    return {
        "bot_a": run_labels[0],
        "bot_b": run_labels[1],
        "hands_requested": hands,
        "hands_played": hands_played,
        "per_player": per_player,
        "net_chips_a": int(earnings[0]),
        "net_chips_b": int(earnings[1]),
        "net_chips_a_per_hand": round(int(earnings[0]) / max(1, hands_played), 3),
        "execution_mode": "native_tcp",
        "wrapper_used": bot_a.wrapper_used or bot_b.wrapper_used,
        "wrapper_used_by_player": {
            run_labels[0]: bot_a.wrapper_used,
            run_labels[1]: bot_b.wrapper_used,
        },
        "deck_seed_base": deck_seed_base,
        "bot_seed_base": bot_seed_base,
        "settlements": settlements,
        "passed_compliance": not issues,
        "issues": issues,
        "events_tail": events[-20:],
        **({"events": list(events)} if capture_events else {}),
    }


async def run_native_tcp_pair(
    bot_a_token: str | Path,
    bot_b_token: str | Path,
    hands: int,
    *,
    require_native_a: bool = True,
    require_native_b: bool = True,
    deck_seed_base: int | None = None,
    bot_seed_base: int | None = None,
    timeout_sec: float | None = None,
    bot_a_env_overrides: dict[str, str | int | None] | None = None,
    bot_b_env_overrides: dict[str, str | int | None] | None = None,
    capture_events: bool = False,
) -> dict[str, Any]:
    """Run a formal native TCP match using both bots' existing entries.

    The ``require_native_*`` arguments are retained so formal callers can state
    the contract explicitly. They cannot be disabled; legacy wrapper generation
    is available only through ``run_legacy_debug_tcp_pair_with_wrappers``.
    """
    if require_native_a is not True or require_native_b is not True:
        raise ValueError(
            "run_native_tcp_pair requires existing valid national_bot.py entries "
            "for both players; use run_legacy_debug_tcp_pair_with_wrappers only "
            "for legacy/debug regression"
        )
    return await _run_native_tcp_pair(
        bot_a_token,
        bot_b_token,
        hands,
        allow_legacy_wrappers=False,
        deck_seed_base=deck_seed_base,
        bot_seed_base=bot_seed_base,
        timeout_sec=timeout_sec,
        bot_a_env_overrides=bot_a_env_overrides,
        bot_b_env_overrides=bot_b_env_overrides,
        capture_events=capture_events,
    )


async def run_legacy_debug_tcp_pair_with_wrappers(
    bot_a_token: str | Path,
    bot_b_token: str | Path,
    hands: int,
    *,
    deck_seed_base: int | None = None,
    bot_seed_base: int | None = None,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    """Run an old regression match, wrapping missing/invalid native entries.

    This API is intentionally named for legacy/debug use. Any generated entry is
    written only to a temporary copy and reported through ``wrapper_used``.
    """
    return await _run_native_tcp_pair(
        bot_a_token,
        bot_b_token,
        hands,
        allow_legacy_wrappers=True,
        deck_seed_base=deck_seed_base,
        bot_seed_base=bot_seed_base,
        timeout_sec=timeout_sec,
    )


async def _run_native_tcp_pair(
    bot_a_token: str | Path,
    bot_b_token: str | Path,
    hands: int,
    *,
    allow_legacy_wrappers: bool,
    deck_seed_base: int | None,
    bot_seed_base: int | None,
    timeout_sec: float | None,
    bot_a_env_overrides: dict[str, str | int | None] | None = None,
    bot_b_env_overrides: dict[str, str | int | None] | None = None,
    capture_events: bool = False,
) -> dict[str, Any]:
    label_a, dir_a = resolve_bot(bot_a_token)
    label_b, dir_b = resolve_bot(bot_b_token)
    capacity_owner = (
        f"native_tcp:{label_a}:{label_b}:{os.getpid()}:{time.monotonic_ns()}"
    )
    capacity_lease = await acquire_match_slots_async(capacity_owner, count=1)
    specs: list[NativeBotSpec] = []
    try:
        specs.append(_prepare_native_spec(
            label_a,
            dir_a,
            allow_legacy_wrapper=allow_legacy_wrappers,
        ))
        specs.append(_prepare_native_spec(
            label_b,
            dir_b,
            allow_legacy_wrapper=allow_legacy_wrappers,
        ))
        hands = max(1, min(70, int(hands)))
        if timeout_sec is None:
            timeout_sec = max(90.0, hands * 4.0)
        return await _run_tcp_server_with_processes(
            specs[0],
            specs[1],
            hands=hands,
            timeout_sec=float(timeout_sec),
            deck_seed_base=deck_seed_base,
            bot_seed_base=bot_seed_base,
            bot_a_env_overrides=bot_a_env_overrides,
            bot_b_env_overrides=bot_b_env_overrides,
            capture_events=capture_events,
        )
    finally:
        _cleanup_specs(specs)
        capacity_lease.release()


async def run_native_tcp_smoke(
    candidate_token: str | Path,
    *,
    source_v: int | None = None,
    opponent_token: str | Path | None = None,
    hands: int = 1,
    timeout_sec: float | None = 90.0,
) -> dict[str, Any]:
    """Run a minimal direct-TCP national smoke match for a candidate bot."""
    hands = max(1, min(70, int(hands)))
    try:
        candidate_label, candidate_dir = resolve_bot(candidate_token)
    except Exception as exc:
        return {
            "passed": False,
            "execution_mode": "native_tcp",
            "wrapper_used": False,
            "hands": hands,
            "issues": [f"native_smoke_candidate_error={type(exc).__name__}: {str(exc)[:300]}"],
            "outcome": "candidate_failure",
            "failure_side": "candidate",
        }

    if opponent_token is not None:
        try:
            opponents = [resolve_bot(opponent_token)]
        except Exception as exc:
            return {
                "candidate": candidate_label,
                "passed": False,
                "execution_mode": "native_tcp",
                "wrapper_used": False,
                "hands": hands,
                "issues": [f"native_smoke_opponent_error={type(exc).__name__}: {str(exc)[:300]}"],
                "outcome": "infrastructure_failure",
                "failure_side": "opponent",
            }
    else:
        opponents = select_acceptance_opponents(candidate_label, source_v, limit=1)

    if not opponents:
        return {
            "candidate": candidate_label,
            "passed": False,
            "execution_mode": "native_tcp",
            "wrapper_used": False,
            "hands": hands,
            "issues": ["native_smoke_no_opponent"],
            "outcome": "infrastructure_failure",
            "failure_side": "opponent",
        }

    opponent_label, opponent_dir = opponents[0]
    try:
        result = await run_native_tcp_pair(
            candidate_dir,
            opponent_dir,
            hands,
            require_native_a=True,
            require_native_b=True,
            timeout_sec=timeout_sec,
        )
    except Exception as exc:
        return {
            "candidate": candidate_label,
            "opponent": opponent_label,
            "passed": False,
            "execution_mode": "native_tcp",
            "wrapper_used": False,
            "hands": hands,
            "issues": [f"native_smoke_exception={type(exc).__name__}: {str(exc)[:500]}"],
            "outcome": "infrastructure_failure",
            "failure_side": "harness",
        }

    candidate_row = (result.get("per_player") or {}).get(candidate_label) or {}
    opponent_row = (result.get("per_player") or {}).get(opponent_label) or {}
    candidate_issues = list(candidate_row.get("compliance_issues") or [])
    opponent_issues = list(opponent_row.get("compliance_issues") or [])
    attributed = set(candidate_issues + opponent_issues)
    unscoped_issues = [
        str(item) for item in result.get("issues") or []
        if str(item) not in attributed
    ]
    if candidate_issues:
        outcome, failure_side, issues = "candidate_failure", "candidate", candidate_issues
    elif opponent_issues or unscoped_issues:
        outcome, failure_side = "infrastructure_failure", (
            "opponent" if opponent_issues and not unscoped_issues else "harness"
        )
        issues = opponent_issues + unscoped_issues
    else:
        outcome, failure_side, issues = "passed", "", []
    passed = outcome == "passed"
    return {
        "candidate": candidate_label,
        "opponent": opponent_label,
        "passed": passed,
        "execution_mode": "native_tcp",
        "wrapper_used": bool(result.get("wrapper_used")),
        "hands": hands,
        "issues": issues,
        "outcome": outcome,
        "failure_side": failure_side,
        "result": result,
    }


def _summary_from_results(bots: list[tuple[str, Path]], results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    runtime_rows: dict[str, list[dict[str, Any]]] = {label: [] for label, _ in bots}
    summary = {
        label: {
            "matches": 0,
            "net_chips": 0,
            "illegal_actions": 0,
            "timeouts": 0,
            "bot_failures": 0,
            "invalid_actions": 0,
            "clamped_raises": 0,
            "allin_conversions": 0,
            "would_be_illegal_raise": 0,
            "postflop_pass_conversions": 0,
            "native_process_failures": 0,
            "json_response_stdout": 0,
            "wrapper_used": False,
            "passed_compliance": True,
            "runtime_telemetry": _empty_runtime_telemetry(),
        }
        for label, _ in bots
    }
    for result in results:
        for label, pdata in result["per_player"].items():
            row = summary[label]
            row["matches"] += 1
            row["net_chips"] += int(pdata.get("earnings", 0) or 0)
            row["illegal_actions"] += int(pdata.get("illegal_actions", 0) or 0)
            row["timeouts"] += int(pdata.get("timeouts", 0) or 0)
            runtime_rows.setdefault(label, []).append(pdata.get("runtime_telemetry", {}) or {})
            native = pdata.get("native", {}) or {}
            row["native_process_failures"] += int(native.get("process_failures", 0) or 0)
            row["json_response_stdout"] += int(native.get("json_response_stdout", 0) or 0)
            row["wrapper_used"] = row["wrapper_used"] or bool(pdata.get("wrapper_used"))
            row["passed_compliance"] = (
                row["passed_compliance"]
                and bool(pdata.get("passed_compliance", result.get("passed_compliance", False)))
            )
    for label, rows in runtime_rows.items():
        if label in summary:
            summary[label]["runtime_telemetry"] = _merge_runtime_telemetry(rows)
    return summary


async def run_native_acceptance_for_candidate(
    candidate_token: str | Path,
    *,
    source_v: int | None = None,
    opponent_tokens: list[str | Path] | None = None,
    hands: int = 70,
    max_opponents: int = 2,
    timeout_sec: float | None = None,
) -> NationalAcceptanceResult:
    candidate = resolve_bot(candidate_token)
    if opponent_tokens:
        opponents = [resolve_bot(token) for token in opponent_tokens]
    else:
        opponents = select_acceptance_opponents(candidate[0], source_v, limit=max_opponents)
    bots = [candidate] + [opp for opp in opponents if opp[0] != candidate[0]]
    if len(bots) < 2:
        return NationalAcceptanceResult(
            candidate=candidate[0],
            opponents=[],
            hands_per_pair=hands,
            passed=False,
            outcome="infrastructure_failure",
            failure_side="opponent",
            issues=["need at least one opponent for native national acceptance"],
            summary={"wrapper_used": False, "passed_compliance": False},
            report={"execution_mode": "native_tcp", "wrapper_used": False},
        )
    pair_indices = [(0, idx) for idx in range(1, len(bots))]
    if timeout_sec is None:
        timeout_sec = max(180.0, float(hands * len(pair_indices) * 5))

    results: list[dict[str, Any]] = []
    try:
        for pair_index, (i, j) in enumerate(pair_indices):
            pair_seed = 71_000 + pair_index * 1_000
            bot_seed = 171_000 + pair_index * 1_000
            results.append(await run_native_tcp_pair(
                bots[i][1],
                bots[j][1],
                hands,
                require_native_a=True,
                require_native_b=True,
                deck_seed_base=pair_seed,
                bot_seed_base=bot_seed,
                timeout_sec=timeout_sec,
            ))
    except TimeoutError:
        issue = f"native_national_acceptance_timeout: exceeded {timeout_sec:g}s"
        return NationalAcceptanceResult(
            candidate=candidate[0],
            opponents=[opp[0] for opp in bots[1:]],
            hands_per_pair=hands,
            passed=False,
            outcome="infrastructure_failure",
            failure_side="harness",
            issues=[issue],
            summary={
                "matches": 0,
                "net_chips": 0,
                "wrapper_used": False,
                "passed_compliance": False,
            },
            report={
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "hands_per_pair": hands,
                "execution_mode": "native_tcp",
                "wrapper_used": False,
                "candidate_only": True,
                "timeout_sec": timeout_sec,
                "timed_out": True,
                "issues": [issue],
            },
        )

    summary = _summary_from_results(bots, results)
    matrix: dict[str, dict[str, Any]] = {label: {} for label, _ in bots}
    for result in results:
        a = result["bot_a"]
        b = result["bot_b"]
        matrix[a][b] = {
            "net_chips": result["net_chips_a"],
            "per_hand": result["net_chips_a_per_hand"],
            "passed_compliance": result["passed_compliance"],
            "wrapper_used": bool(result.get("wrapper_used")),
            "issues": result["issues"],
        }
        matrix[b][a] = {
            "net_chips": result["net_chips_b"],
            "per_hand": round(result["net_chips_b"] / max(1, result["hands_played"]), 3),
            "passed_compliance": result["passed_compliance"],
            "wrapper_used": bool(result.get("wrapper_used")),
            "issues": result["issues"],
        }
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hands_per_pair": hands,
        "execution_mode": "native_tcp",
        "wrapper_used": any(bool(result.get("wrapper_used")) for result in results),
        "pair_count": len(pair_indices),
        "bots": [{"label": label, "path": str(path)} for label, path in bots],
        "results": results,
        "summary": summary,
        "matrix": matrix,
        "candidate_only": True,
        "timeout_sec": timeout_sec,
    }
    candidate_summary = summary.get(candidate[0], {})
    candidate_issues: list[str] = []
    opponent_issues: list[str] = []
    unscoped_issues: list[str] = []
    for result in results:
        rows = result.get("per_player") or {}
        candidate_issues.extend((rows.get(candidate[0]) or {}).get("compliance_issues") or [])
        for opponent in bots[1:]:
            opponent_issues.extend((rows.get(opponent[0]) or {}).get("compliance_issues") or [])
        attributed = set(candidate_issues + opponent_issues)
        unscoped_issues.extend(
            str(item) for item in result.get("issues") or []
            if str(item) not in attributed
        )
    if candidate_issues:
        outcome, failure_side, issues = "candidate_failure", "candidate", candidate_issues
    elif opponent_issues or unscoped_issues:
        outcome = "infrastructure_failure"
        failure_side = "opponent" if opponent_issues and not unscoped_issues else "harness"
        issues = opponent_issues + unscoped_issues
    else:
        outcome, failure_side, issues = "passed", "", []
    return NationalAcceptanceResult(
        candidate=candidate[0],
        opponents=[opp[0] for opp in bots[1:]],
        hands_per_pair=hands,
        passed=outcome == "passed" and bool(candidate_summary.get("passed_compliance")),
        outcome=outcome,
        failure_side=failure_side,
        issues=issues,
        summary=candidate_summary,
        matrix=matrix.get(candidate[0], {}),
        report=report,
    )


def _mean(values: list[int]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _ci(values: list[int]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    return paired_bootstrap_ci(values)


async def run_native_precommit(
    candidate_token: str | Path,
    opponents: list[dict[str, Any]],
    *,
    hands: int = 70,
    matches_per_opponent: int = 1,
    parent_label: str = "",
    deck_seed_base: int | None = 91_000,
    sample_plan: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    candidate = resolve_bot(candidate_token)
    hands = int(hands)
    if hands != 70:
        raise ValueError(
            f"native precommit strength samples must contain exactly 70 hands; got {hands}"
        )
    matches_per_opponent = max(1, int(matches_per_opponent))
    matchups: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    aggregate_net_chips: list[int] = []
    total_wins = total_losses = total_draws = 0
    resolved_opponents: list[dict[str, Any]] = []
    frozen_samples: dict[tuple[str, int], dict[str, Any]] = {}
    if sample_plan is not None:
        for row in sample_plan:
            if not isinstance(row, dict):
                raise ValueError("native precommit sample plan contains a non-object row")
            key = (str(row.get("opponent") or ""), int(row.get("repeat") or 0))
            if not key[0] or key[1] < 1 or key in frozen_samples:
                raise ValueError("native precommit sample plan has an invalid or duplicate key")
            frozen_samples[key] = dict(row)
        expected_rows = len(opponents) * matches_per_opponent
        if len(frozen_samples) != expected_rows:
            raise ValueError(
                f"native precommit sample plan has {len(frozen_samples)} rows; "
                f"expected {expected_rows}"
            )
    if not opponents:
        blockers.append({"reason": "native_no_opponents", "details": "Native precommit requires at least one opponent."})
    for opp_index, item in enumerate(opponents):
        reason = str(item.get("reason") or "precommit")
        strength_authoritative = is_strength_matchup(item)
        token = item.get("path") or item.get("token") or item.get("name")
        opponent = resolve_bot(token)
        resolved_opponents.append({"name": item.get("name") or opponent[0], "reason": reason, "path": str(opponent[1])})
        samples: list[int] = []
        repeats: list[dict[str, Any]] = []
        candidate_issues: list[str] = []
        opponent_issues: list[str] = []
        hands_played_total = 0
        for repeat in range(matches_per_opponent):
            sample_key = (str(item.get("name") or opponent[0]), repeat + 1)
            frozen = frozen_samples.get(sample_key) if sample_plan is not None else None
            if sample_plan is not None and frozen is None:
                raise ValueError(
                    f"native precommit sample plan is missing {sample_key[0]} repeat {sample_key[1]}"
                )
            seed = (
                frozen.get("deck_seed_base")
                if frozen is not None
                else (
                    None
                    if deck_seed_base is None
                    else int(deck_seed_base) + (opp_index * 100_000) + (repeat * 1_000)
                )
            )
            bot_seed = (
                frozen.get("bot_seed_base")
                if frozen is not None
                else (None if seed is None else int(seed) + 1_000_000_000)
            )
            result = await run_native_tcp_pair(
                candidate[1],
                opponent[1],
                hands,
                require_native_a=True,
                require_native_b=True,
                deck_seed_base=seed,
                bot_seed_base=bot_seed,
                timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
                bot_a_env_overrides={
                    "POK_NATIVE_DECISION_HARD_DEADLINE_SEC": LOCAL_NATIVE_STRENGTH_HARD_DEADLINE_SEC,
                    "POK_NATIVE_DECISION_REFINEMENT_BUDGET_SEC": LOCAL_NATIVE_STRENGTH_REFINEMENT_BUDGET_SEC,
                    "POK_NATIVE_DECISION_BASELINE_TARGET_SEC": LOCAL_NATIVE_STRENGTH_BASELINE_TARGET_SEC,
                },
                bot_b_env_overrides={
                    "POK_NATIVE_DECISION_HARD_DEADLINE_SEC": LOCAL_NATIVE_STRENGTH_HARD_DEADLINE_SEC,
                    "POK_NATIVE_DECISION_REFINEMENT_BUDGET_SEC": LOCAL_NATIVE_STRENGTH_REFINEMENT_BUDGET_SEC,
                    "POK_NATIVE_DECISION_BASELINE_TARGET_SEC": LOCAL_NATIVE_STRENGTH_BASELINE_TARGET_SEC,
                },
            )
            net = int(result.get("net_chips_a", 0) or 0)
            hands_played = int(result.get("hands_played", 0) or 0)
            hands_played_total += hands_played
            c_issues = [
                str(issue)
                for issue in result.get("issues", [])
                if str(issue).startswith(candidate[0] + ":") or str(issue).startswith("hands_played=")
            ]
            o_issues = [
                str(issue)
                for issue in result.get("issues", [])
                if not (str(issue).startswith(candidate[0] + ":") or str(issue).startswith("hands_played="))
            ]
            complete = hands_played == hands
            compliance_passed = bool(result.get("passed_compliance", False))
            sample_valid = complete and compliance_passed and not c_issues and not o_issues
            strength_admitted = strength_authoritative and sample_valid
            if sample_valid:
                samples.append(net)
            if strength_admitted:
                aggregate_net_chips.append(net)
            candidate_issues.extend(c_issues)
            opponent_issues.extend(o_issues)
            repeats.append({
                "repeat": repeat + 1,
                "deck_seed_base": seed,
                "bot_seed_base": bot_seed,
                "hands_played": hands_played,
                "net_chips": net,
                "candidate_issues": c_issues,
                "opponent_issues": o_issues,
                "complete": complete,
                "passed_compliance": compliance_passed,
                "sample_valid": sample_valid,
                "strength_admitted": strength_admitted,
                "local_runtime_budget": {
                    "hard_deadline_sec": LOCAL_NATIVE_STRENGTH_HARD_DEADLINE_SEC,
                    "refinement_budget_sec": LOCAL_NATIVE_STRENGTH_REFINEMENT_BUDGET_SEC,
                    "baseline_target_sec": LOCAL_NATIVE_STRENGTH_BASELINE_TARGET_SEC,
                    "match_timeout_sec": LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
                    "scope": "local_strength_only",
                },
                "raw": result,
            })
        wins = sum(1 for value in samples if value > 0)
        losses = sum(1 for value in samples if value < 0)
        draws = sum(1 for value in samples if value == 0)
        if strength_authoritative:
            total_wins += wins
            total_losses += losses
            total_draws += draws
        mean = _mean(samples)
        ci_lo, ci_hi = _ci(samples)
        matchup = {
            "opponent": item.get("name") or opponent[0],
            "reason": reason,
            "strength_authoritative": strength_authoritative,
            "protocol": "national_native_tcp",
            "hands_per_match": hands,
            "matches": matches_per_opponent,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "n_played": len(samples),
            "samples_expected": matches_per_opponent,
            "hands_played_total": hands_played_total,
            "net_chips": samples,
            "net_chips_mean": _rounded(mean),
            "net_chip_ci": [_rounded(ci_lo), _rounded(ci_hi)],
            "candidate_compliance_issues": candidate_issues,
            "opponent_compliance_issues": opponent_issues,
            "wrapper_used": any(bool(row["raw"].get("wrapper_used")) for row in repeats),
            "repeats": repeats,
        }
        matchups.append(matchup)
        if strength_authoritative and candidate_issues:
            blockers.append({"reason": "native_candidate_compliance", "opponent": matchup["opponent"], "details": "; ".join(candidate_issues[:5])})
        if strength_authoritative and opponent_issues:
            blockers.append({"reason": "native_opponent_compliance", "opponent": matchup["opponent"], "details": "; ".join(opponent_issues[:5])})
        if strength_authoritative and any(not row["complete"] for row in repeats):
            blockers.append({"reason": "native_incomplete_match", "opponent": matchup["opponent"], "details": f"{hands_played_total}/{hands * matches_per_opponent} hands completed"})
        if strength_authoritative and len(samples) != matches_per_opponent:
            blockers.append({
                "reason": "native_strength_sample_shortfall",
                "opponent": matchup["opponent"],
                "details": f"{len(samples)}/{matches_per_opponent} complete compliant 70-hand samples admitted",
            })
    agg_mean = _mean(aggregate_net_chips)
    agg_ci_lower, agg_ci_upper = _ci(aggregate_net_chips)
    if not aggregate_net_chips:
        blockers.append({"reason": "native_no_samples", "details": "Native precommit produced zero completed match samples."})
    outcome_blockers, outcome_gate = precommit_outcome_blockers(
        matchups,
        parent_label=parent_label,
        aggregate_reason="aggregate_native_regression",
    )
    blockers.extend(outcome_blockers)
    paired_payload = {
        "protocol": "national_native_tcp",
        "hands_per_match": hands,
        "matches_per_opponent": matches_per_opponent,
        "aggregate_ci_lower": _rounded(agg_ci_lower),
        "aggregate_ci_upper": _rounded(agg_ci_upper),
        "aggregate_threshold": None,
        "aggregate_gate_bound": outcome_gate.get("primary_match_score"),
        "aggregate_gate_rule": "complete_70_hand_wld_loss_margin",
        "outcome_gate": outcome_gate,
        "net_chips_samples": len(aggregate_net_chips),
        "gate_degraded": len(aggregate_net_chips) < 2,
        "net_chips_mean": _rounded(agg_mean),
        "net_chips_std": round(statistics.pstdev(aggregate_net_chips), 1) if len(aggregate_net_chips) > 1 else None,
        "net_chips_min": min(aggregate_net_chips) if aggregate_net_chips else None,
        "net_chips_max": max(aggregate_net_chips) if aggregate_net_chips else None,
        "secondary_net_chip_ci": [_rounded(agg_ci_lower), _rounded(agg_ci_upper)],
    }
    return {
        "evaluation_protocol": "national_native_tcp",
        "candidate": candidate[0],
        "candidate_path": str(candidate[1]),
        "opponents": resolved_opponents,
        "matchups": matchups,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_draws": total_draws,
        "aggregate_net_chips": aggregate_net_chips,
        "sample_plan": list(sample_plan or []),
        "paired_bootstrap": paired_payload,
        "wrapper_used": any(bool(matchup.get("wrapper_used")) for matchup in matchups),
        "blockers": blockers,
        "passed": not blockers,
    }
