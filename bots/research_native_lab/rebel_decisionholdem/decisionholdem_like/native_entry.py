#!/usr/bin/env python3
"""Direct national TCP entry for the provisional A2 projection artifact."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import select
import socket
import sys
import time
import traceback

# Keep the content-bound deployment directory immutable during execution.
sys.dont_write_bytecode = True

if __package__:
    from .a2_runtime import (
        ActionContext,
        SparseBlueprint,
        choose_blueprint_action,
        legal_action_specs,
        map_observed_raise_to,
        tcp_card_id,
    )
    from .realtime_resolver import (
        ResolveConfig,
        resolve_public_state,
        should_resolve,
    )
else:
    from a2_runtime import (
        ActionContext,
        SparseBlueprint,
        choose_blueprint_action,
        legal_action_specs,
        map_observed_raise_to,
        tcp_card_id,
    )
    # Resolver not available in standalone mode — disable
    ResolveConfig = None
    resolve_public_state = None
    should_resolve = None


NATIONAL_STREAM_DECODER_VERSION = 3
DEFAULT_OFFICIAL_ACTION_DELAY_SEC = 0.30
DEFAULT_DECISION_HARD_DEADLINE_SEC = 54.0
DEFAULT_NUMERIC_IDLE_GRACE_SEC = 0.05
CARD_RE = re.compile(r"<([0-3]),([0-9]|1[0-2])>")
NUMERIC_RE = re.compile(r"^(raise) ([0-9]+)")
EARN_RE = re.compile(r"^(earnChips) (-?[0-9]+)")
WORDS = ("allin", "check", "call", "fold", "name")
STAGE_CARDS = {"flop|": 3, "turn|": 1, "river|": 1, "oppo_hands|": 2}


class NationalStreamDecoder:
    """Delimiter-free national stream decoder with idle numeric finalization."""

    def __init__(self) -> None:
        self.buffer = ""

    @property
    def has_pending_numeric(self) -> bool:
        return bool(
            re.fullmatch(r"(?:raise [0-9]+|earnChips -?[0-9]+)", self.buffer)
        )

    @staticmethod
    def _card_message(buffer: str, prefix: str, count: int):
        if not buffer.startswith(prefix):
            return None
        position = len(prefix)
        for _ in range(count):
            match = CARD_RE.match(buffer, position)
            if match is None:
                return None
            position = match.end()
        return buffer[:position], buffer[position:]

    def _take(self, allow_terminal_numeric: bool):
        self.buffer = self.buffer.lstrip(" \t\r\n")
        if not self.buffer:
            return None
        for blind in ("SMALLBLIND", "BIGBLIND"):
            item = self._card_message(
                self.buffer,
                f"preflop|{blind}|",
                2,
            )
            if item is not None:
                return item
        for prefix, count in STAGE_CARDS.items():
            item = self._card_message(self.buffer, prefix, count)
            if item is not None:
                return item
        for pattern in (NUMERIC_RE, EARN_RE):
            match = pattern.match(self.buffer)
            if match is not None:
                end = match.end()
                if end == len(self.buffer) and not allow_terminal_numeric:
                    return None
                return self.buffer[:end], self.buffer[end:]
        for word in WORDS:
            if self.buffer.startswith(word):
                return word, self.buffer[len(word):]
        return None

    def feed(self, chunk: str) -> list[str]:
        self.buffer += chunk
        if len(self.buffer) > 65_536:
            raise ValueError("national TCP undecoded buffer exceeded 64 KiB")
        emitted: list[str] = []
        while True:
            item = self._take(False)
            if item is None:
                break
            message, self.buffer = item
            emitted.append(message)
        return emitted

    def flush_idle(self) -> list[str]:
        emitted: list[str] = []
        while True:
            item = self._take(True)
            if item is None:
                break
            message, self.buffer = item
            emitted.append(message)
        return emitted


def _split_messages(buffer: str) -> tuple[list[str], str]:
    decoder = NationalStreamDecoder()
    messages = decoder.feed(buffer)
    messages.extend(decoder.flush_idle())
    return messages, decoder.buffer


def _cards(message: str) -> list[int]:
    return [tcp_card_id(int(suit), int(rank)) for suit, rank in CARD_RE.findall(message)]


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


class A2BlueprintClient:
    def __init__(self, name: str, blueprint_path: str, seed: int, log_path: str) -> None:
        self.name = name
        self.blueprint = SparseBlueprint.load(blueprint_path)
        self.seed = int(seed)
        self.log = open(log_path, "a", encoding="utf-8", buffering=1) if log_path else None
        self.action_delay = _env_float(
            "POK_OFFICIAL_ACTION_DELAY",
            DEFAULT_OFFICIAL_ACTION_DELAY_SEC,
            0.0,
            2.0,
        )
        self.decision_deadline = _env_float(
            "POK_DECISION_HARD_DEADLINE_SEC",
            DEFAULT_DECISION_HARD_DEADLINE_SEC,
            0.05,
            54.0,
        )
        self.last_platform_message_at = 0.0
        self.hand_number = 0
        self.decision_number = 0
        self.private_cards: list[int] = []
        self.board: list[int] = []
        self.street = "preflop"
        self.is_small_blind = False
        self.hero_chips = 20_000
        self.opponent_chips = 20_000
        self.hero_bet = 0
        self.opponent_bet = 0
        self.pot = 0
        self.hero_action_count = 0
        self.stage_actions: list[tuple[str, int | None]] = []
        self.opponent_allin = False
        self.in_allin_runout = False
        self.fold_seen = False
        self.responding_to_check = False
        self.last_off_tree = None
        self.resolve_enabled = os.environ.get("A2_RESOLVE", "1") == "1"
        self.resolve_iterations = int(os.environ.get("A2_RESOLVE_ITERS", "10"))
        self.resolve_depth = int(os.environ.get("A2_RESOLVE_DEPTH", "2"))

    def _log(self, text: str) -> None:
        if self.log is not None:
            self.log.write(text + "\n")

    def close(self) -> None:
        if self.log is not None:
            self.log.close()
            self.log = None

    def _new_hand(self, blind: str, cards: list[int]) -> None:
        self.hand_number += 1
        self.private_cards = cards
        self.board = []
        self.street = "preflop"
        self.is_small_blind = blind == "SMALLBLIND"
        self.hero_chips = 20_000 - (50 if self.is_small_blind else 100)
        self.opponent_chips = 20_000 - (100 if self.is_small_blind else 50)
        self.hero_bet = 50 if self.is_small_blind else 100
        self.opponent_bet = 100 if self.is_small_blind else 50
        self.pot = 150
        self.hero_action_count = 0
        self.stage_actions = []
        self.opponent_allin = False
        self.in_allin_runout = False
        self.fold_seen = False
        self.responding_to_check = False
        self.last_off_tree = None

    def _infer_peer_closure_at_boundary(self) -> None:
        """Apply only the peer contribution proved by a new-street boundary."""

        if self.fold_seen or self.hero_bet <= self.opponent_bet:
            return
        required = self.hero_bet - self.opponent_bet
        committed = min(required, self.opponent_chips)
        if committed <= 0:
            return
        self.opponent_chips -= committed
        self.opponent_bet += committed
        self.pot += committed
        inferred = "call" if committed == required else "allin"
        self.stage_actions.append((inferred, None))
        if self.opponent_chips == 0:
            self.in_allin_runout = True
        self._log(
            f"boundary_inferred_peer={inferred} committed={committed} "
            f"previous_street={self.street}"
        )

    def _new_street(self, street: str, cards: list[int]) -> None:
        self._infer_peer_closure_at_boundary()
        self.street = street
        self.board.extend(cards)
        self.hero_bet = 0
        self.opponent_bet = 0
        self.hero_action_count = 0
        self.stage_actions = []
        self.opponent_allin = self.opponent_chips == 0
        self.responding_to_check = False
        self.last_off_tree = None

    def _context(self) -> ActionContext:
        return ActionContext(
            street=self.street,
            pot=self.pot,
            hero_bet=self.hero_bet,
            opponent_bet=self.opponent_bet,
            hero_chips=self.hero_chips,
            is_small_blind=self.is_small_blind,
            hero_action_count=self.hero_action_count,
            stage_actions=tuple(self.stage_actions),
            responding_to_check=self.responding_to_check,
            opponent_allin=self.opponent_allin,
        )

    def _random_unit(self) -> float:
        material = (
            f"{self.seed}|{self.hand_number}|{self.decision_number}|"
            f"{self.blueprint.digest}"
        ).encode("ascii")
        return int.from_bytes(hashlib.sha256(material).digest(), "big") / (1 << 256)

    def _fallback(self, context: ActionContext) -> str:
        legal = {spec.action_id: spec for spec in legal_action_specs(context)}
        action_id = "fold" if context.to_call > 0 else "check_call"
        return legal[action_id].wire_action

    def decide(self) -> str:
        started = time.monotonic()
        context = self._context()
        fallback = self._fallback(context)
        self.decision_number += 1

        # Try real-time resolving if enabled and beneficial
        if self.resolve_enabled and self.street != "preflop":
            remaining_ms = (self.decision_deadline - (time.monotonic() - started)) * 1000
            if remaining_ms > 500:
                try:
                    from .hunl_abstraction import HUNLInformationAbstraction
                    from .a2_runtime import hand_bucket, normalize_action_specs, ActionSpec
                    from .a2_runtime import information_key

                    # Build blueprint strategy for current infoset
                    bucket = hand_bucket(self.private_cards, self.board)
                    infoset_key = information_key(self.street, bucket, tuple(self.stage_actions))
                    bp_lookup = self.blueprint.lookup(
                        street=self.street,
                        bucket=bucket,
                        stage_actions=tuple(self.stage_actions),
                    )
                    bp_strategy = {}
                    if bp_lookup and bp_lookup.matched_strategy:
                        for spec, prob in zip(
                            normalize_action_specs(context),
                            bp_lookup.matched_strategy,
                        ):
                            bp_strategy[spec.action_id] = prob

                    if bp_strategy:
                        resolve_cfg = ResolveConfig(
                            iterations=self.resolve_iterations,
                            depth=self.resolve_depth,
                            seed=self.seed + self.decision_number,
                            method="plain",
                        )
                        # Resolve is computationally expensive — only run if time permits
                        resolve_result = resolve_public_state(
                            state=None,  # resolver uses abstraction-based evaluation
                            config=resolve_cfg,
                            blueprint_strategy=bp_strategy,
                            abstraction=None,
                        )
                        # Pick action from resolved strategy
                        import random as _rng
                        rng = _rng.Random(self._random_unit())
                        cumulative = 0.0
                        roll = rng.random()
                        for action_id, prob in zip(resolve_result.actions, resolve_result.resolved_strategy):
                            cumulative += prob
                            if roll <= cumulative:
                                # Map action_id to wire action
                                specs = {s.action_id: s for s in legal_action_specs(context)}
                                if action_id in specs:
                                    action = specs[action_id].wire_action
                                    self._log(f"resolved_action={action} method=resolve")
                                    return action
                except Exception as exc:
                    self._log(f"resolve_error={type(exc).__name__}:{str(exc)[:200]}")

        # Blueprint fallback
        try:
            chosen = choose_blueprint_action(
                self.blueprint,
                context=context,
                private_cards=self.private_cards,
                board=self.board,
                random_unit=self._random_unit(),
            )
            action = chosen.action.wire_action
        except Exception as exc:
            self._log(f"lookup_error={type(exc).__name__}:{str(exc)[:200]}")
            return fallback
        if time.monotonic() - started >= self.decision_deadline:
            self._log("deadline_fallback=1")
            return fallback
        self._log(
            f"hand={self.hand_number} decision={self.decision_number} "
            f"action={action} lookup={chosen.lookup.matched_key} "
            f"offtree={self.last_off_tree}"
        )
        return action

    def _apply_hero_action(self, action: str) -> None:
        if action.startswith("raise "):
            target = int(action.split(" ", 1)[1])
            committed = max(0, target - self.hero_bet)
            self.hero_chips -= committed
            self.hero_bet = target
            self.pot += committed
            self.stage_actions.append(("raise", target))
        elif action == "call":
            committed = min(self.hero_chips, max(0, self.opponent_bet - self.hero_bet))
            self.hero_chips -= committed
            self.hero_bet += committed
            self.pot += committed
            self.stage_actions.append(("call", None))
            if self.hero_chips == 0 or self.opponent_chips == 0 or self.opponent_allin:
                self.in_allin_runout = True
        elif action == "check":
            self.stage_actions.append(("check", None))
        elif action == "fold":
            self.stage_actions.append(("fold", None))
            self.fold_seen = True
        elif action == "allin":
            committed = self.hero_chips
            self.hero_chips = 0
            self.hero_bet += committed
            self.pot += committed
            self.stage_actions.append(("allin", None))
        self.hero_action_count += 1
        self.responding_to_check = False

    def _apply_opponent_action(self, message: str) -> str:
        if message.startswith("raise "):
            target = int(message.split(" ", 1)[1])
            context_before = self._context()
            self.last_off_tree = map_observed_raise_to(target, context_before)
            committed = min(
                self.opponent_chips,
                max(0, target - self.opponent_bet),
            )
            self.opponent_chips -= committed
            self.opponent_bet += committed
            self.pot += committed
            self.stage_actions.append(("raise", target))
            self.responding_to_check = False
            return "act"
        if message == "allin":
            committed = self.opponent_chips
            self.opponent_chips = 0
            self.opponent_bet += committed
            self.pot += committed
            self.opponent_allin = True
            self.stage_actions.append(("allin", None))
            self.responding_to_check = False
            return "act"
        if message == "check":
            self.stage_actions.append(("check", None))
            self.responding_to_check = True
            return (
                "act"
                if self.street != "preflop" and self.hero_action_count == 0
                else "wait"
            )
        if message == "call":
            committed = min(
                self.opponent_chips,
                max(0, self.hero_bet - self.opponent_bet),
            )
            self.opponent_chips -= committed
            self.opponent_bet += committed
            self.pot += committed
            self.stage_actions.append(("call", None))
            self.responding_to_check = False
            if self.hero_chips == 0 or self.opponent_chips == 0:
                self.in_allin_runout = True
            if self.street == "preflop" and not self.is_small_blind and self.hero_action_count == 0:
                return "act"
            return "wait"
        if message == "fold":
            self.stage_actions.append(("fold", None))
            self.fold_seen = True
            self.responding_to_check = False
            return "wait"
        self._log(f"ignored_unknown_platform_action={message}")
        return "wait"

    def on_message(self, message: str) -> str | None:
        self.last_platform_message_at = time.monotonic()
        if message == "name":
            return self.name
        if message.startswith("preflop|"):
            blind = "SMALLBLIND" if "|SMALLBLIND|" in message else "BIGBLIND"
            self._new_hand(blind, _cards(message))
            return self.decide() if self.is_small_blind else None
        for street in ("flop", "turn", "river"):
            if message.startswith(street + "|"):
                self._new_street(street, _cards(message))
                return (
                    self.decide()
                    if not self.is_small_blind and not self.in_allin_runout
                    else None
                )
        if message.startswith("earnChips "):
            self._infer_peer_closure_at_boundary()
            self.in_allin_runout = False
            return None
        if message.startswith("oppo_hands|"):
            self._infer_peer_closure_at_boundary()
            return None
        if self.in_allin_runout:
            self._log(f"ignored_action_during_allin_runout={message}")
            return None
        status = self._apply_opponent_action(message)
        return self.decide() if status == "act" else None


def _send_wire_action(
    sock: socket.socket,
    client: A2BlueprintClient,
    action: str,
    *,
    is_handshake: bool = False,
) -> None:
    if not is_handshake:
        remaining = (
            client.last_platform_message_at + client.action_delay - time.monotonic()
        )
        if remaining > 0.0:
            time.sleep(remaining)
    sock.sendall(action.encode("ascii"))
    if not is_handshake:
        client._apply_hero_action(action)


def run_client(args: argparse.Namespace) -> int:
    client = A2BlueprintClient(args.name, args.blueprint, args.seed, args.log)
    decoder = NationalStreamDecoder()
    numeric_pending_since: float | None = None
    sock = socket.create_connection((args.host, args.port), timeout=15.0)
    sock.setblocking(False)
    try:
        while True:
            readable, _, _ = select.select([sock], [], [], 0.01)
            if readable:
                chunk = sock.recv(65_536)
                if not chunk:
                    break
                messages = decoder.feed(chunk.decode("utf-8", errors="strict"))
                numeric_pending_since = (
                    time.monotonic() if decoder.has_pending_numeric else None
                )
            else:
                ready = (
                    decoder.has_pending_numeric
                    and numeric_pending_since is not None
                    and time.monotonic() - numeric_pending_since
                    >= DEFAULT_NUMERIC_IDLE_GRACE_SEC
                )
                messages = decoder.flush_idle() if ready else []
                if ready:
                    numeric_pending_since = None
            for message in messages:
                action = client.on_message(message)
                if action is not None:
                    _send_wire_action(
                        sock,
                        client,
                        action,
                        is_handshake=message == "name",
                    )
        return 0
    finally:
        try:
            sock.close()
        finally:
            client.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", default="A2Blueprint")
    parser.add_argument(
        "--blueprint",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "blueprint.json"),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=int(os.environ.get("POK_NATIVE_BOT_SEED", "0")),
    )
    parser.add_argument("--log", default="")
    return parser


def main() -> int:
    try:
        return run_client(_parser().parse_args())
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
