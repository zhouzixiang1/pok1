"""NATIVE_BOT_TEMPLATE companion module.

Holds the generated ``national_bot.py`` source template and the two
post-processing ``.replace()`` steps that bake in the runtime version and
collapse triple newlines.  The post-processed value is the byte-pinned one
asserted by test_national_runtime_probe.py; the raw literal and both
``.replace()`` lines were moved here byte-for-byte from
national_native_templates.py during the line-count refactor.
"""

# Mirrors the canonical constant in national_native_templates.py.  Defined
# locally (rather than imported) to avoid a circular import between this
# companion and the re-export hub.  The value is baked into the template via
# the ``.replace()`` below, so the post-processed bytes stay identical.
NATIONAL_DECISION_RUNTIME_VERSION = 10


NATIVE_BOT_TEMPLATE = r'''#!/usr/bin/env python3
"""Native national TCP entrypoint for this bot.

This file is the formal national-platform submission entry. It connects to the
TCP server, maintains authoritative raw-stream state, calls a typed local
``policy.py`` in an isolated worker, and sends only canonical national wire
actions: raise <amount>, fold, call, check, allin.
No candidate policy code owns sockets or writes protocol output.
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
try:
    import resource as _resource
except ImportError:  # pragma: no cover - non-POSIX submission host
    _resource = None
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
ACTION_PREFIX_RE = re.compile(r"^raise [0-9]+")
EARN_PREFIX_RE = re.compile(r"^earnChips -?[0-9]+")
DEFAULT_OFFICIAL_ACTION_DELAY_SEC = 0.30
OFFICIAL_ACTION_DELAY_ENV = "POK_OFFICIAL_ACTION_DELAY"
NATIONAL_STREAM_DECODER_VERSION = 2
NATIONAL_DECISION_RUNTIME_VERSION = __POK_DECISION_RUNTIME_VERSION__
DEFAULT_STREAM_IDLE_FLUSH_SEC = 0.10
STREAM_IDLE_FLUSH_ENV = "POK_NATIONAL_STREAM_IDLE_FLUSH"
MAX_STREAM_BUFFER_CHARS = 16_384
OPPONENT_TRACKER_SCHEMA_VERSION = 4
LINE_CONTEXT_SCHEMA_VERSION = 1
DECISION_CONTEXT_SCHEMA_VERSION = 1
POLICY_DECISION_SCHEMA_VERSION = 1
NATIONAL_CARD_ENCODING = "national_tcp_suit_rank_v1"
MAX_DECISION_HISTORY_ACTIONS = 64
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


def _parse_cards(text: str) -> list[tuple[int, int]]:
    """Keep cards in the national protocol's native ``(suit, rank)`` space."""
    cards = []
    for raw_suit, raw_rank in CARD_RE.findall(text):
        suit = int(raw_suit)
        rank = int(raw_rank)
        if not (0 <= suit <= 3 and 0 <= rank <= 12):
            raise ValueError(f"invalid national card <{suit},{rank}>")
        cards.append((suit, rank))
    return cards


def _context_card(card: tuple[int, int]) -> dict:
    suit, rank = card
    return {"suit": int(suit), "rank": int(rank)}


def _parse_action(raw: str) -> tuple[str, int | None]:
    if re.fullmatch(r"raise [0-9]+", raw):
        return "raise", int(raw.split(" ", 1)[1])
    if raw in {"call", "check", "fold", "allin"}:
        return raw, None
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
    candidate = buffer
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
    def _showdown_class(cards: list[tuple[int, int]]) -> tuple[str, str]:
        rank_a, rank_b = cards[0][1] + 2, cards[1][1] + 2
        high, low = max(rank_a, rank_b), min(rank_a, rank_b)
        suited = cards[0][0] == cards[1][0]
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
        opponent_cards: list[tuple[int, int]] | None = None,
        public_cards: list[tuple[int, int]] | None = None,
    ) -> None:
        hand = int(hand)
        if hand in self._showdown_hands:
            return
        self._showdown_hands.add(hand)
        self.showdowns += 1
        cards = list(opponent_cards or [])
        if len(cards) == 2:
            ranks = [card[1] for card in cards]
            suits = [card[0] for card in cards]
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


def _safe_policy_payload(raw_value) -> dict:
    """Copy an untrusted policy result into a primitive, pickle-safe shape.

    This is serialization hygiene only.  The socket owner remains the sole
    authority that validates a decision against the live betting state.
    """
    if type(raw_value) is not dict:
        return {"kind": "__invalid_non_mapping__"}
    if not set(raw_value).issubset({"kind", "raise_to"}):
        return {"kind": "__invalid_mapping_shape__"}
    kind = raw_value.get("kind")
    if type(kind) is not str:
        return {"kind": "__invalid_kind__"}
    payload = {"kind": kind}
    if "raise_to" in raw_value:
        raise_to = raw_value.get("raise_to")
        if type(raise_to) is not int:
            return {"kind": "__invalid_raise_to__"}
        payload["raise_to"] = raise_to
    return payload


def _policy_worker_candidate(raw_value):
    """Normalize one incremental policy result without legalizing it."""
    metadata = {}
    decision = raw_value
    if type(raw_value) is dict and "decision" in raw_value:
        if not set(raw_value).issubset({
            "decision", "sample_count", "confidence", "reason", "complete"
        }):
            decision = {"kind": "__invalid_envelope_shape__"}
        else:
            decision = raw_value.get("decision")
            metadata = {
                str(key): raw_value[key]
                for key in ("sample_count", "confidence", "reason", "complete")
                if key in raw_value
                and type(raw_value[key]) in (str, int, float, bool, type(None))
            }
    return _safe_policy_payload(decision), metadata


def _apply_policy_worker_resource_limits() -> None:
    if _resource is None:
        return
    limits = [
        (_resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024)),
        (_resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024)),
        (_resource.RLIMIT_NOFILE, (64, 64)),
        (_resource.RLIMIT_CORE, (0, 0)),
    ]
    for resource_id, value in limits:
        try:
            _resource.setrlimit(resource_id, value)
        except (ValueError, OSError):
            pass
    if hasattr(_resource, "RLIMIT_NPROC"):
        try:
            _resource.setrlimit(_resource.RLIMIT_NPROC, (32, 32))
        except (ValueError, OSError):
            pass


def _policy_worker_main(connection, bot_dir: str, random_seed: int | None) -> None:
    """Persistent, killable candidate policy runtime with no socket authority."""
    started = time.monotonic()
    try:
        if hasattr(os, "setsid"):
            # Own a process group so any candidate-created CPU workers inherit a
            # tree the socket process can terminate atomically at the deadline.
            try:
                os.setsid()
            except OSError:
                pass
        _apply_policy_worker_resource_limits()
        if random_seed is not None:
            random.seed(int(random_seed))
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)
        # Standardized pure-fact precompute is loaded once per worker lifetime.
        # Policy modules may import and consume it without rebuilding tables per turn.
        if os.path.isfile(os.path.join(bot_dir, "precompute.py")):
            importlib.import_module("precompute")
        policy_module = importlib.import_module("policy")
        get_baseline_decision = getattr(policy_module, "get_baseline_decision")
        iter_decisions = getattr(policy_module, "iter_decisions")
        if not callable(get_baseline_decision) or not callable(iter_decisions):
            raise TypeError(
                "policy.py must define callable get_baseline_decision(context) "
                "and iter_decisions(context, baseline, deadline)"
            )
        connection.send({
            "kind": "ready",
            "import_elapsed_ms": round((time.monotonic() - started) * 1000.0, 3),
            "pid": os.getpid(),
            "has_baseline": True,
            "has_iterator": True,
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
        context = job.get("decision_context") or {}
        deadline = float(job.get("deadline_monotonic") or time.monotonic())
        fallback = _safe_policy_payload(job.get("fallback_decision") or {})
        raw_candidate_limit = job.get("max_refinement_candidates")
        candidate_limit = (
            None
            if raw_candidate_limit is None
            else max(0, min(MAX_REFINEMENT_MESSAGES, int(raw_candidate_limit)))
        )
        sequence = 0
        try:
            def publish(phase: str, raw_decision, metadata=None, trusted=None) -> dict:
                nonlocal sequence
                decision = _safe_policy_payload(raw_decision)
                sequence += 1
                connection.send({
                    "kind": "candidate",
                    "decision_id": decision_id,
                    "phase": phase,
                    "sequence": sequence,
                    "decision": decision,
                    "published_monotonic": time.monotonic(),
                    "metadata": dict(metadata or {}),
                    "trusted": dict(trusted or {}),
                })
                return decision

            baseline = fallback
            if time.monotonic() < deadline:
                baseline = publish(
                    "baseline",
                    get_baseline_decision(context),
                    {"source": "get_baseline_decision"},
                )

            refinement_started_cpu = time.process_time_ns()
            refinement_started_wall = time.monotonic_ns()
            iterator_steps = 0
            iterator_exhausted = False
            termination_reason = "not_available"
            if (
                candidate_limit != 0
                and time.monotonic() < deadline
            ):
                iterator = iter_decisions(context, baseline, deadline)
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
                    candidate, metadata = _policy_worker_candidate(raw_candidate)
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
            elif candidate_limit == 0:
                termination_reason = "probe_candidate_limit"
            connection.send({
                "kind": "done",
                "decision_id": decision_id,
                "sequence": sequence,
                "decision": baseline,
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
        self._strategy_process = self._strategy_connection = None
        self._strategy_worker_generation = 0
        # Raw name starts the persistent worker but never waits for readiness.
        self._name_handshake_count = 0
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
        self._my_cards: list[tuple[int, int]] = []
        self._public_cards: list[tuple[int, int]] = []
        self._is_sb = False
        self._hand_num = 0
        self._history: list[dict] = []
        self._stage = "preflop"
        self._my_id = 0
        self._opponent_id = 1
        self._my_action_count = 0
        self._my_chips = INITIAL_CHIPS
        self._my_stage_bet = 0
        self._opponent_chips = INITIAL_CHIPS
        self._opponent_stage_bet = 0
        self._pot = 0
        self._in_allin_runout = False
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

    def _line_state(self) -> dict:
        """Return socket-authoritative cross-street line semantics."""
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
            "schema_version": LINE_CONTEXT_SCHEMA_VERSION,
            "street": self._stage,
            "street_index": round_num,
            "position": "small_blind" if self._is_sb else "big_blind",
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
        }

    def _match_control_state(self, remaining_including_current: int) -> dict:
        """Publish the exact worst-case fold-to-finish match bound.

        Each national hand resets to ``INITIAL_CHIPS`` and seat/blind roles
        alternate.  Folding now loses only this hand's committed exposure;
        folding every later hand loses that seat's forced blind.  Strictly
        exceeding the sum guarantees a match win, while equality guarantees
        only a draw and is deliberately not marked locked.
        """

        remaining = max(1, min(TOTAL_HANDS, int(remaining_including_current)))
        future_hands = remaining - 1
        complete_pairs, odd = divmod(future_hands, 2)
        future_forced_blinds = complete_pairs * (SMALL_BLIND + BIG_BLIND)
        if odd:
            future_forced_blinds += BIG_BLIND if self._is_sb else SMALL_BLIND
        current_exposure = max(0, INITIAL_CHIPS - int(self._my_chips))
        forced_fold_loss_bound = current_exposure + future_forced_blinds
        hero_net_earned = int(self._opponent_tracker.net_earned)
        return {
            "schema_version": 1,
            "initial_chips": INITIAL_CHIPS,
            "small_blind": SMALL_BLIND,
            "big_blind": BIG_BLIND,
            "current_position": "small_blind" if self._is_sb else "big_blind",
            "current_exposure": current_exposure,
            "future_forced_blinds": future_forced_blinds,
            "forced_fold_loss_bound": forced_fold_loss_bound,
            "hero_net_earned": hero_net_earned,
            "fold_locks_win": hero_net_earned > forced_fold_loss_bound,
        }

    def _semantic_hand_history(self) -> dict:
        actions = []
        last_by_round = {}
        for record in self._history:
            round_num = int(record.get("round", 0))
            wire_kind = str(record.get("action_type") or "unknown")
            committed = int(record.get("committed", 0) or 0)
            previous = last_by_round.get(round_num)
            if wire_kind == "check":
                semantic_kind = "pass"
            elif wire_kind == "call" and (
                committed == 0
                or (previous and previous.get("wire_kind") == "check")
            ):
                semantic_kind = "pass"
            elif wire_kind == "call":
                semantic_kind = "match"
            else:
                semantic_kind = wire_kind
            item = {
                "street": record.get("street", self._stage),
                "street_index": round_num,
                "actor": self._actor_label(record.get("player_id")),
                "wire_kind": wire_kind,
                "semantic_kind": semantic_kind,
                "committed": committed,
                "stage_bet_after": int(record.get("stage_bet", 0) or 0),
                "inferred": bool(record.get("inferred")),
            }
            if wire_kind == "raise":
                item["raise_to"] = int(record.get("stage_bet", 0) or 0)
            if record.get("inferred"):
                item["inference_boundary"] = str(
                    record.get("inference_boundary") or ""
                )
            actions.append(item)
            last_by_round[round_num] = item
        truncated = max(0, len(actions) - MAX_DECISION_HISTORY_ACTIONS)
        return {
            "schema_version": 1,
            "actions": actions[-MAX_DECISION_HISTORY_ACTIONS:],
            "truncated_count": truncated,
        }

    def _pass_wire_kind(self) -> str:
        """Map candidate ``pass`` to the only legal official pass token."""
        if self._opponent_stage_bet > self._my_stage_bet:
            return "call"
        if self._responding_to_check():
            return "call"
        return "check"

    def _legal_policy_state(self) -> dict:
        policy_kinds = ["fold", "pass"]
        allin_occurred = self._current_round_has_allin()
        if not allin_occurred and self._my_chips > 0:
            policy_kinds.append("allin")
        minimum = self._minimum_raise_total()
        # An exact stack commitment must use the official ``allin`` token.
        maximum = self._my_stage_bet + max(0, self._my_chips - 1)
        if not allin_occurred and minimum <= maximum:
            policy_kinds.append("raise")
            min_raise_to = minimum
            max_raise_to = maximum
        else:
            min_raise_to = None
            max_raise_to = None
        return {
            "schema_version": POLICY_DECISION_SCHEMA_VERSION,
            "policy_kinds": policy_kinds,
            "pass_wire_kind": self._pass_wire_kind(),
            "min_raise_to": min_raise_to,
            "max_raise_to": max_raise_to,
            "raise_boundary": "inclusive_exact_2x_raise_to",
        }

    def _build_decision_context(
        self,
        *,
        decision_id: int,
        hard_deadline: float,
        refinement_deadline: float,
    ) -> dict:
        """Build the sole bounded, versioned candidate policy input."""
        to_call = max(0, self._opponent_stage_bet - self._my_stage_bet)
        effective_stack = min(self._my_chips, self._opponent_chips)
        remaining = max(1, TOTAL_HANDS - max(0, self._hand_num - 1))
        return {
            "schema_version": DECISION_CONTEXT_SCHEMA_VERSION,
            "runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
            "decision_id": int(decision_id),
            "cards": {
                "encoding": NATIONAL_CARD_ENCODING,
                "hole": [_context_card(card) for card in self._my_cards],
                "board": [_context_card(card) for card in self._public_cards],
            },
            "hand": {
                "number": int(self._hand_num),
                "total_hands": TOTAL_HANDS,
                "remaining_including_current": remaining,
                "street": self._stage,
                "street_index": self._round_num(),
                "position": "small_blind" if self._is_sb else "big_blind",
                "acts_first_postflop": self._acts_first_postflop(),
                "match_control": self._match_control_state(remaining),
            },
            "betting": {
                "pot": int(self._pot),
                "hero_stack": int(self._my_chips),
                "opponent_stack": int(self._opponent_chips),
                "effective_stack": int(effective_stack),
                "hero_street_bet": int(self._my_stage_bet),
                "opponent_street_bet": int(self._opponent_stage_bet),
                "to_call": int(to_call),
                "spr": round(effective_stack / max(1.0, float(self._pot)), 6),
                "pot_odds": round(
                    to_call / max(1.0, float(self._pot + to_call)), 6
                ),
                "call_closes_allin_runout": bool(
                    to_call > 0
                    and (
                        self._opponent_chips == 0
                        or self._my_chips <= to_call
                    )
                ),
            },
            "history": self._semantic_hand_history(),
            "line": self._line_state(),
            "legal": self._legal_policy_state(),
            "opponent": self._opponent_tracker.snapshot(),
            "deadline": {
                "clock": "time.monotonic",
                "hard_monotonic": float(hard_deadline),
                "refinement_monotonic": float(refinement_deadline),
                "hard_budget_ms": int(self._decision_hard_deadline_sec * 1000),
                "baseline_target_ms": int(
                    self._decision_baseline_target_sec * 1000
                ),
                "refinement_budget_ms": int(
                    self._decision_refinement_budget_sec * 1000
                ),
            },
        }

    def _socket_safe_fallback_decision(self) -> dict:
        """Return a typed risk-safe decision before candidate code runs."""
        if self._opponent_stage_bet > self._my_stage_bet:
            return {"kind": "fold"}
        return {"kind": "pass"}

    def _legalize_policy_decision(self, raw_decision, fallback: dict) -> dict:
        """Validate untrusted policy output against socket-owned live state."""
        safe_fallback = dict(fallback)
        if type(raw_decision) is not dict:
            return safe_fallback
        if not set(raw_decision).issubset({"kind", "raise_to"}):
            return safe_fallback
        kind = raw_decision.get("kind")
        if type(kind) is not str:
            return safe_fallback
        legal = self._legal_policy_state()
        if kind not in legal["policy_kinds"]:
            return safe_fallback
        if kind != "raise":
            if "raise_to" in raw_decision:
                return safe_fallback
            return {"kind": kind}
        if set(raw_decision) != {"kind", "raise_to"}:
            return safe_fallback
        raise_to = raw_decision.get("raise_to")
        if type(raise_to) is not int:
            return safe_fallback
        minimum = legal.get("min_raise_to")
        maximum = legal.get("max_raise_to")
        if minimum is None or maximum is None or not minimum <= raise_to <= maximum:
            return safe_fallback
        return {"kind": "raise", "raise_to": raise_to}

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
            target=_policy_worker_main,
            args=(child_connection, BOT_DIR, worker_seed),
            name=f"national-policy-{next_generation}",
            # Non-daemon is intentional: policy may own a bounded fixed CPU
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

    def _policy_decision(self) -> dict:
        started = time.monotonic()
        hard_deadline = started + self._decision_hard_deadline_sec
        baseline_target = started + self._decision_baseline_target_sec
        refinement_deadline = started + self._decision_refinement_budget_sec
        baseline = self._socket_safe_fallback_decision()
        self._last_decision_source = "socket_baseline"
        self._decision_serial += 1
        decision_id = self._decision_serial
        try:
            context = self._build_decision_context(
                decision_id=decision_id,
                hard_deadline=hard_deadline,
                refinement_deadline=refinement_deadline,
            )
        except BaseException as exc:
            _log(
                f"DECIDE context_error={type(exc).__name__}:{str(exc)[:160]!r} "
                f"fallback={baseline}"
            )
            self._last_decision_source = "context_error_baseline"
            return baseline
        decision_metrics = {
            "runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
            "decision_id": decision_id,
            "socket_fallback_decision": dict(baseline),
            "socket_fallback_ready_ms": round((time.monotonic() - started) * 1000.0, 3),
            "baseline_published_ms": None,
            "baseline_target_ms": round(
                self._decision_baseline_target_sec * 1000.0,
                3,
            ),
            "baseline_target_met": False,
            "policy_baseline_decision": None,
            "refinement_messages": 0,
            "refinement_decision_changes": 0,
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
                "decision_context": context,
                "fallback_decision": baseline,
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
                    f"target={self._decision_baseline_target_sec:.3f}s safe_decision={baseline}"
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
                previous_decision = baseline
                baseline = self._legalize_policy_decision(
                    message.get("decision"),
                    baseline,
                )
                phase = str(message.get("phase") or "refinement")
                elapsed = result_at - started
                metadata = message.get("metadata") or {}
                trusted = message.get("trusted") or {}
                if phase == "baseline":
                    decision_metrics["policy_baseline_decision"] = dict(baseline)
                    decision_metrics["baseline_published_ms"] = round(elapsed * 1000.0, 3)
                    decision_metrics["baseline_target_met"] = (
                        elapsed <= self._decision_baseline_target_sec
                    )
                    self._last_decision_source = "policy_baseline"
                    _log(
                        f"DECIDE baseline decision_id={decision_id} decision={baseline} "
                        f"elapsed={elapsed:.3f}s "
                        f"target_met={elapsed <= self._decision_baseline_target_sec}"
                    )
                else:
                    decision_metrics["refinement_messages"] += 1
                    if baseline != previous_decision:
                        decision_metrics["refinement_decision_changes"] += 1
                    if len(decision_metrics["refinement_progress"]) < 64:
                        decision_metrics["refinement_progress"].append({
                            "sequence": sequence,
                            "decision": dict(baseline),
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
                        f"decision={baseline} elapsed={elapsed:.3f}s "
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
                    f"DECIDE policy_error decision_id={decision_id} "
                    f"error={message.get('error')!r} latest_safe={baseline}"
                )
                self._last_decision_source = "policy_error_latest_safe"
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
        if action_type not in {"call", "check", "fold", "allin", "raise"}:
            return
        if action_type == "raise" and amount is None:
            return
        entry = {
            "street": self._stage,
            "round": self._round_num(),
            "player_id": player_id,
            "action_type": action_type,
            "committed": int(committed),
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

        The local server mirrors the official EXE and may advance directly
        after a terminal call/check without relaying it.  A
        street/settlement/showdown boundary proves the peer response only when
        our last action still required one:
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
            value = record.get("stage_bet")
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
            # The controlled official oracle proves exact inclusive 2x legal.
            minimum = last_raise * 2
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

    def _decision_to_tcp(self, raw_decision) -> tuple[str, str, int | None]:
        """Final socket-owner legalization and canonical wire translation."""
        fallback = self._socket_safe_fallback_decision()
        decision = self._legalize_policy_decision(raw_decision, fallback)
        kind = decision["kind"]
        if kind == "fold":
            return "fold", "fold", None
        if kind == "pass":
            wire_kind = self._pass_wire_kind()
            return wire_kind, wire_kind, None
        if kind == "allin":
            return "allin", "allin", None
        target = int(decision["raise_to"])
        return f"raise {target}", "raise", target

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
            decision = self._policy_decision()
        except Exception:
            traceback.print_exc(file=sys.stderr)
            _log("DECIDE exception -> fold")
            decision = {"kind": "fold"}
        elapsed = time.perf_counter() - t0
        _log(
            f"DECIDE done decision={decision!r} source={self._last_decision_source} "
            f"elapsed={elapsed:.3f}s"
        )
        msg, action_type, amount = self._decision_to_tcp(decision)
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
            self._name_handshake_count += 1
            handshake_count, generation_before = self._name_handshake_count, self._strategy_worker_generation
            launch_ok = self._ensure_strategy_worker(); generation = self._strategy_worker_generation
            launch_started = launch_ok and generation > generation_before
            _log(f"NAME_HANDSHAKE count={handshake_count} worker_launch_started={launch_started} worker_generation={generation} launch_ok={launch_ok}")
            if handshake_count != 1 or not launch_started: return
            sock.sendall(self.name.encode("utf-8"))
            _log(f"SEND name_handshake name={self.name!r} count={handshake_count} worker_generation={generation}")
            return
        if line.startswith("preflop"):
            # Infer only a boundary-proven omitted closer before clearing state.
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
            self._earned_by_hand[self._hand_num] = earned
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
            self._opponent_tracker.observe_showdown(
                self._hand_num,
                record["opponent_cards"],
                record["public_cards"],
            )
            # Official/local ordering is earnChips followed by oppo_hands. The
            # settlement snapshot above therefore precedes showdown learning;
            # emit again so the final hand's revealed range is not lost.
            _log(
                "OPPONENT_TRACKER "
                + json.dumps(
                    self._opponent_tracker.snapshot(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
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
                    # A delimiter-free numeric settlement can be followed
                    # immediately by EOF. EOF is a proven token boundary; do
                    # not discard the final complete token merely because no
                    # idle window elapsed first.
                    for line in decoder.flush_idle():
                        _log(f"DISPATCH eof_flush line={line!r}")
                        bot.handle(line, sock)
                    _log("RECV empty -> server closed")
                    return 0
                chunk = data.decode("ascii")
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
# Keep the system-owned runtime below the same fail-closed file-size ceiling
# enforced for every published candidate.  The raw template uses triple
# newlines for source readability; generated artifacts retain one blank line
# between definitions without carrying the redundant separator line.
NATIVE_BOT_TEMPLATE = NATIVE_BOT_TEMPLATE.replace("\n\n\n", "\n\n")
