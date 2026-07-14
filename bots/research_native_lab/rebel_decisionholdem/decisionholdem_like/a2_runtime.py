"""Versioned A2 abstraction, sparse blueprint lookup and action translation.

The action/hand projection in this module is a functional national adaptation.
It is not claimed to match DecisionHoldem's unavailable cluster assets, and
nearest-action off-tree translation is explicitly not a safe resolver.
"""

from __future__ import annotations

import hashlib
import json
import math
import stat
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


HAND_ABSTRACTION_VERSION = "route-a2-hand-abstraction-v1"
ACTION_ABSTRACTION_VERSION = "route-a2-action-abstraction-v1"
BLUEPRINT_SCHEMA = "route-a2-sparse-blueprint-v1"
ACTION_IDS = (
    "fold",
    "check_call",
    "exact_min",
    "0.5p",
    "1p",
    "1.5p",
    "allin",
)
STREETS = ("preflop", "flop", "turn", "river")
RANK_SYMBOLS = "23456789TJQKA"
MADE_NAMES = (
    "high_card",
    "pair",
    "two_pair",
    "trips",
    "straight",
    "flush",
    "full_house",
    "quads",
    "straight_flush",
)
MAX_BLUEPRINT_BYTES = 128 * 1024 * 1024
BLUEPRINT_ALGORITHM = "alternating-linear-cfr-leduc-seed-projection-v1"
BLUEPRINT_SOURCE_GAME = "limit-leduc-clean-room-v1"
BLUEPRINT_FIDELITY = {
    "lcfr_kernel": "paper-faithful-clean-room-small-game",
    "leduc_tree": "independent-exact-correctness-gate",
    "national_projection": "functional-adaptation-not-decisionholdem-blueprint",
    "off_tree": "nearest-action-translation-only-not-safe-resolve",
}


def _strict_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def tcp_card_id(suit: int, rank: int) -> int:
    return _strict_int(suit, "suit", 0, 3) * 13 + _strict_int(rank, "rank", 0, 12)


def card_components(card: int) -> tuple[int, int]:
    card = _strict_int(card, "card", 0, 51)
    return divmod(card, 13)


def preflop_class(cards: Sequence[int]) -> str:
    if len(cards) != 2:
        raise ValueError("preflop abstraction requires exactly two cards")
    normalized = tuple(_strict_int(card, "card", 0, 51) for card in cards)
    if normalized[0] == normalized[1]:
        raise ValueError("private cards must be distinct")
    first_suit, first_rank = card_components(normalized[0])
    second_suit, second_rank = card_components(normalized[1])
    high, low = sorted((first_rank, second_rank), reverse=True)
    if high == low:
        return RANK_SYMBOLS[high] * 2
    suffix = "s" if first_suit == second_suit else "o"
    return f"{RANK_SYMBOLS[high]}{RANK_SYMBOLS[low]}{suffix}"


def preflop_tier(cards: Sequence[int]) -> str:
    first = card_components(cards[0])[1]
    second = card_components(cards[1])[1]
    high, low = sorted((first, second), reverse=True)
    pair = high == low
    suited = card_components(cards[0])[0] == card_components(cards[1])[0]
    if (pair and high >= 9) or (high == 12 and low >= 10):
        return "premium"
    if pair or high >= 10 or (suited and high >= 9 and low >= 7):
        return "strong"
    if high >= 7 or suited or high - low <= 2:
        return "medium"
    return "weak"


def _rank_five(cards: Sequence[int]) -> tuple[int, ...]:
    ranks = sorted((card_components(card)[1] + 2 for card in cards), reverse=True)
    suits = [card_components(card)[0] for card in cards]
    counts: dict[int, int] = {}
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
    groups = sorted(((count, rank) for rank, count in counts.items()), reverse=True)
    unique = sorted(set(ranks), reverse=True)
    straight_high = 0
    if len(unique) == 5:
        if unique[0] - unique[-1] == 4:
            straight_high = unique[0]
        elif unique == [14, 5, 4, 3, 2]:
            straight_high = 5
    flush = len(set(suits)) == 1
    if flush and straight_high:
        return (8, straight_high)
    if groups[0][0] == 4:
        return (7, groups[0][1], groups[1][1])
    if [count for count, _ in groups] == [3, 2]:
        return (6, groups[0][1], groups[1][1])
    if flush:
        return (5, *ranks)
    if straight_high:
        return (4, straight_high)
    if groups[0][0] == 3:
        kickers = sorted((rank for count, rank in groups[1:] if count == 1), reverse=True)
        return (3, groups[0][1], *kickers)
    pairs = sorted((rank for count, rank in groups if count == 2), reverse=True)
    singles = sorted((rank for count, rank in groups if count == 1), reverse=True)
    if len(pairs) == 2:
        return (2, pairs[0], pairs[1], singles[0])
    if len(pairs) == 1:
        return (1, pairs[0], *singles)
    return (0, *ranks)


def postflop_bucket(private_cards: Sequence[int], board: Sequence[int]) -> str:
    cards = tuple(private_cards) + tuple(board)
    expected = {3: 5, 4: 6, 5: 7}
    if len(board) not in expected or len(cards) != expected[len(board)]:
        raise ValueError("postflop abstraction requires 2 private and 3-5 board cards")
    normalized = tuple(_strict_int(card, "card", 0, 51) for card in cards)
    if len(set(normalized)) != len(normalized):
        raise ValueError("known cards must be unique")
    rank = max(_rank_five(combo) for combo in combinations(normalized, 5))
    return f"made:{MADE_NAMES[rank[0]]}"


def hand_bucket(
    street: str,
    private_cards: Sequence[int],
    board: Sequence[int],
) -> tuple[str, str]:
    if street not in STREETS:
        raise ValueError(f"unknown street: {street}")
    if street == "preflop":
        if board:
            raise ValueError("preflop abstraction requires an empty board")
        return f"class:{preflop_class(private_cards)}", f"tier:{preflop_tier(private_cards)}"
    made = postflop_bucket(private_cards, board)
    return made, made


@dataclass(frozen=True, slots=True)
class ActionContext:
    street: str
    pot: int
    hero_bet: int
    opponent_bet: int
    hero_chips: int
    is_small_blind: bool
    hero_action_count: int
    stage_actions: tuple[tuple[str, int | None], ...] = ()
    responding_to_check: bool = False
    opponent_allin: bool = False

    def __post_init__(self) -> None:
        if self.street not in STREETS:
            raise ValueError(f"unknown street: {self.street}")
        for name in ("pot", "hero_bet", "opponent_bet", "hero_chips", "hero_action_count"):
            _strict_int(getattr(self, name), name, 0, 1_400_000)
        if type(self.is_small_blind) is not bool:
            raise ValueError("is_small_blind must be boolean")
        if type(self.responding_to_check) is not bool or type(self.opponent_allin) is not bool:
            raise ValueError("action context markers must be boolean")

    @property
    def to_call(self) -> int:
        return max(0, self.opponent_bet - self.hero_bet)


@dataclass(frozen=True, slots=True)
class ActionSpec:
    action_id: str
    wire_action: str
    raise_to: int | None = None


def _last_raise_to(actions: Iterable[tuple[str, int | None]]) -> int | None:
    for kind, amount in reversed(tuple(actions)):
        if kind == "raise" and type(amount) is int and amount > 0:
            return amount
    return None


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def normalize_action_specs(
    specs: Iterable[ActionSpec],
    context: ActionContext,
) -> tuple[ActionSpec, ...]:
    normalized: list[ActionSpec] = []
    seen_ids: set[str] = set()
    seen_wire: set[str] = set()
    stack_total = context.hero_bet + context.hero_chips
    pass_wire = (
        "call"
        if context.to_call > 0 or context.responding_to_check
        else "check"
    )
    last_raise = _last_raise_to(context.stage_actions)
    minimum = (
        last_raise * 2
        if last_raise is not None
        else 200 if context.street == "preflop" else 100
    )
    minimum = max(minimum, context.opponent_bet + 1, context.hero_bet + 1)
    for spec in specs:
        if spec.action_id not in ACTION_IDS or spec.action_id in seen_ids:
            continue
        wire = spec.wire_action
        if spec.action_id == "fold":
            valid = wire == "fold" and spec.raise_to is None
        elif spec.action_id == "check_call":
            valid = wire == pass_wire and spec.raise_to is None
        elif spec.action_id == "allin":
            valid = (
                wire == "allin"
                and spec.raise_to is None
                and context.hero_chips > 0
                and not context.opponent_allin
            )
        else:
            valid = wire.startswith("raise ")
        if valid and wire.startswith("raise "):
            parts = wire.split(" ")
            valid = (
                len(parts) == 2
                and parts[1].isdigit()
                and spec.raise_to == int(parts[1])
                and spec.raise_to is not None
                and minimum <= spec.raise_to < stack_total
                and not context.opponent_allin
            )
        elif spec.raise_to is not None:
            valid = False
        if not valid or wire in seen_wire:
            continue
        seen_ids.add(spec.action_id)
        seen_wire.add(wire)
        normalized.append(spec)
    if not any(spec.action_id == "fold" for spec in normalized):
        normalized.insert(0, ActionSpec("fold", "fold"))
    return tuple(normalized)


def legal_action_specs(context: ActionContext) -> tuple[ActionSpec, ...]:
    pass_wire = (
        "call"
        if context.to_call > 0 or context.responding_to_check
        else "check"
    )
    raw = [
        ActionSpec("fold", "fold"),
        ActionSpec("check_call", pass_wire),
    ]
    if not context.opponent_allin and context.hero_chips > 0:
        last_raise = _last_raise_to(context.stage_actions)
        minimum = (
            last_raise * 2
            if last_raise is not None
            else 200 if context.street == "preflop" else 100
        )
        minimum = max(minimum, context.opponent_bet + 1, context.hero_bet + 1)
        raw.append(ActionSpec("exact_min", f"raise {minimum}", minimum))
        pot_after_call = context.pot + context.to_call
        base = context.hero_bet + context.to_call
        for action_id, fraction in (("0.5p", 0.5), ("1p", 1.0), ("1.5p", 1.5)):
            target = max(minimum, base + _round_half_up(fraction * pot_after_call))
            raw.append(ActionSpec(action_id, f"raise {target}", target))
        raw.append(ActionSpec("allin", "allin"))
    return normalize_action_specs(raw, context)


@dataclass(frozen=True, slots=True)
class OffTreeMapping:
    observed_raise_to: int
    mapped_action_id: str
    mapped_raise_to: int | None
    exact: bool
    fidelity: str = "nearest-action-translation-only-not-safe-resolve"


def map_observed_raise_to(
    observed_raise_to: int,
    context: ActionContext,
) -> OffTreeMapping:
    observed = _strict_int(observed_raise_to, "observed raise-to", 1, 1_400_000)
    candidates = [
        spec
        for spec in legal_action_specs(context)
        if spec.raise_to is not None or spec.action_id == "allin"
    ]
    if not candidates:
        return OffTreeMapping(observed, "check_call", None, False)
    stack_total = context.hero_bet + context.hero_chips
    ranked = sorted(
        candidates,
        key=lambda spec: (
            abs((spec.raise_to if spec.raise_to is not None else stack_total) - observed),
            ACTION_IDS.index(spec.action_id),
        ),
    )
    chosen = ranked[0]
    target = chosen.raise_to if chosen.raise_to is not None else stack_total
    return OffTreeMapping(observed, chosen.action_id, chosen.raise_to, target == observed)


def information_key(
    *,
    street: str,
    hand: str,
    position: str,
    facing: str,
    raises: str,
) -> str:
    return (
        f"{HAND_ABSTRACTION_VERSION}|street={street}|hand={hand}|"
        f"position={position}|facing={facing}|raises={raises}"
    )


@dataclass(frozen=True, slots=True)
class BlueprintLookup:
    requested_key: str
    matched_key: str
    probabilities: Mapping[str, float]
    fidelity: str = "sparse-blueprint-lookup-no-online-resolve"


class SparseBlueprint:
    def __init__(self, payload: Mapping[str, Any]) -> None:
        if payload.get("schema") not in (BLUEPRINT_SCHEMA, "route-a2-hunl-sparse-blueprint-v6"):
            raise ValueError("unsupported sparse blueprint schema")
        if payload.get("hand_abstraction") != HAND_ABSTRACTION_VERSION:
            raise ValueError("blueprint hand abstraction version mismatch")
        if payload.get("action_abstraction") != ACTION_ABSTRACTION_VERSION:
            raise ValueError("blueprint action abstraction version mismatch")
        iterations = payload.get("iterations_completed")
        _strict_int(iterations, "iterations_completed", 1, 2_000_000_000)
        if payload.get("algorithm") != BLUEPRINT_ALGORITHM:
            raise ValueError("blueprint algorithm identity mismatch")
        if payload.get("source_game") != BLUEPRINT_SOURCE_GAME:
            raise ValueError("blueprint source-game identity mismatch")
        checkpoint_digest = payload.get("training_checkpoint_digest")
        if (
            not isinstance(checkpoint_digest, str)
            or len(checkpoint_digest) != 64
            or any(character not in "0123456789abcdef" for character in checkpoint_digest)
        ):
            raise ValueError("blueprint training checkpoint digest is invalid")
        fidelity = payload.get("fidelity")
        if fidelity != BLUEPRINT_FIDELITY:
            raise ValueError("blueprint fidelity boundary is missing")
        raw_policies = payload.get("policies")
        if not isinstance(raw_policies, dict) or not raw_policies:
            raise ValueError("blueprint policies must be a non-empty object")
        policies: dict[str, dict[str, float]] = {}
        for key, row in raw_policies.items():
            if not isinstance(key, str) or not key.startswith(HAND_ABSTRACTION_VERSION + "|"):
                raise ValueError("blueprint policy key is not versioned")
            if not isinstance(row, dict) or not row:
                raise ValueError(f"blueprint policy is empty at {key}")
            if any(action not in ACTION_IDS for action in row):
                raise ValueError(f"blueprint policy has unknown action at {key}")
            if any(type(value) not in (int, float) for value in row.values()):
                raise ValueError(f"blueprint policy has non-numeric probability at {key}")
            values = {action: float(value) for action, value in row.items()}
            if any(not math.isfinite(value) or value < 0.0 for value in values.values()):
                raise ValueError(f"blueprint policy has invalid probability at {key}")
            total = sum(values.values())
            if total <= 0.0 or abs(total - 1.0) > 1e-9:
                raise ValueError(f"blueprint policy does not sum to one at {key}")
            policies[key] = values
        canonical = _canonical_bytes(payload)
        # Detach all nested objects from the caller so the digest, exported
        # metadata and validated policy cannot drift after construction.
        self.payload = json.loads(canonical)
        self.iterations_completed = iterations
        self.policies = policies
        self.digest = hashlib.sha256(canonical).hexdigest()

    @classmethod
    def load(cls, path: str | Path) -> "SparseBlueprint":
        target = Path(path)
        metadata = target.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("blueprint artifact must be a real regular file")
        if metadata.st_size > MAX_BLUEPRINT_BYTES:
            raise ValueError("blueprint artifact exceeds the frozen size limit")
        return cls(json.loads(target.read_text(encoding="utf-8")))

    def lookup(
        self,
        *,
        street: str,
        hand: str,
        fallback_hand: str,
        position: str,
        facing: str,
        raises: str,
    ) -> BlueprintLookup:
        requested = information_key(
            street=street,
            hand=hand,
            position=position,
            facing=facing,
            raises=raises,
        )
        candidates = (
            requested,
            information_key(
                street=street,
                hand=hand,
                position=position,
                facing=facing,
                raises="*",
            ),
            information_key(
                street=street,
                hand=fallback_hand,
                position="*",
                facing=facing,
                raises="*",
            ),
            information_key(
                street=street,
                hand="*",
                position="*",
                facing=facing,
                raises="*",
            ),
            information_key(
                street=street,
                hand="*",
                position="*",
                facing="*",
                raises="*",
            ),
        )
        for key in candidates:
            if key in self.policies:
                return BlueprintLookup(requested, key, self.policies[key])
        raise ValueError(f"blueprint has no fallback policy for {requested}")


@dataclass(frozen=True, slots=True)
class BlueprintDecision:
    action: ActionSpec
    lookup: BlueprintLookup
    used_legality_fallback: bool = False
    available_policy_mass: float = 1.0
    dropped_policy_mass: float = 0.0


def choose_blueprint_action(
    blueprint: SparseBlueprint,
    *,
    context: ActionContext,
    private_cards: Sequence[int],
    board: Sequence[int],
    random_unit: float,
    legal_specs_override: Sequence[ActionSpec] | None = None,
) -> BlueprintDecision:
    if type(random_unit) not in (int, float) or not 0.0 <= random_unit < 1.0:
        raise ValueError("random_unit must lie in [0, 1)")
    hand, fallback_hand = hand_bucket(context.street, private_cards, board)
    facing = (
        "allin"
        if context.opponent_allin
        else "raise" if context.to_call > 0 else "check" if context.responding_to_check else "none"
    )
    raise_count = sum(1 for kind, _ in context.stage_actions if kind == "raise")
    lookup = blueprint.lookup(
        street=context.street,
        hand=hand,
        fallback_hand=fallback_hand,
        position="sb" if context.is_small_blind else "bb",
        facing=facing,
        raises="2plus" if raise_count >= 2 else str(raise_count),
    )
    generated_specs = legal_action_specs(context)
    if legal_specs_override is None:
        selected_specs = generated_specs
    else:
        selected_specs = tuple(legal_specs_override)
        generated = {
            (spec.action_id, spec.wire_action, spec.raise_to) for spec in generated_specs
        }
        if (
            not selected_specs
            or len({spec.action_id for spec in selected_specs}) != len(selected_specs)
            or any(
                (spec.action_id, spec.wire_action, spec.raise_to) not in generated
                for spec in selected_specs
            )
        ):
            raise ValueError("legal action override is not a non-empty canonical subset")
    legal = {spec.action_id: spec for spec in selected_specs}
    available = [
        (action, probability)
        for action, probability in lookup.probabilities.items()
        if action in legal and probability > 0.0
    ]
    total = sum(probability for _, probability in available)
    dropped = sum(
        probability
        for action, probability in lookup.probabilities.items()
        if action not in legal and probability > 0.0
    )
    if total <= 0.0:
        preferred = "fold" if context.to_call > 0 else "check_call"
        fallback = legal.get(preferred, next(iter(legal.values())))
        return BlueprintDecision(fallback, lookup, True, total, dropped)
    threshold = float(random_unit) * total
    cumulative = 0.0
    for action_id in ACTION_IDS:
        probability = next(
            (value for action, value in available if action == action_id),
            0.0,
        )
        cumulative += probability
        if threshold < cumulative:
            return BlueprintDecision(legal[action_id], lookup, False, total, dropped)
    action_id = available[-1][0]
    return BlueprintDecision(legal[action_id], lookup, False, total, dropped)
