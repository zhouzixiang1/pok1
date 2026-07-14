"""Versioned, Common-authoritative card/action abstraction for Route-B HUNL.

The main M4 blueprint uses the real 169 preflop classes while retaining the
Common 1,326-combination index in every descriptor and in the content-bound
asset digest.  Postflop buckets are deterministic, suit-isomorphic, and use
card-removal-correct equity samples.  No strategy logic lives in this module.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

from bots.research_native_lab.common_contracts import (
    Action,
    ActionKind,
    LegalActionSet,
    NationalGameState,
    Street,
)
from bots.research_native_lab.common_contracts.cards import (
    all_hole_combinations,
    canonical_combo,
    combo_index,
    compare_hands,
    rank_seven,
    validate_card,
)

from ..core.identity import payload_sha256


CARD_ABSTRACTION_VERSION = "route-b-hunl-card-v1"
ACTION_ABSTRACTION_VERSION = "route-b-hunl-action-v1"
INFORMATION_SCHEMA_VERSION = "route-b-hunl-infoset-v2-perfect-recall"
EQUITY_SAMPLER_VERSION = "route-b-equity-crn-v1"
RANK_CHARS = "23456789TJQKA"
RANKS_DESC = tuple(range(14, 1, -1))
MATERIAL_L1_TOLERANCE = 1e-6
_SUIT_PERMUTATIONS = tuple(itertools.permutations(range(4)))


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _rank_char(rank: int) -> str:
    if type(rank) is not int or not 2 <= rank <= 14:
        raise ValueError("rank must be in 2..14")
    return RANK_CHARS[rank - 2]


def _build_preflop_classes() -> tuple[str, ...]:
    result: list[str] = []
    for row, high in enumerate(RANKS_DESC):
        for column, low in enumerate(RANKS_DESC):
            if row == column:
                result.append(_rank_char(high) * 2)
            elif row < column:
                result.append(f"{_rank_char(high)}{_rank_char(low)}s")
            else:
                result.append(f"{_rank_char(low)}{_rank_char(high)}o")
    if len(result) != 169 or len(set(result)) != 169:
        raise AssertionError("preflop class registry must contain 169 unique rows")
    return tuple(result)


PREFLOP_CLASSES = _build_preflop_classes()
PREFLOP_CLASS_INDEX = {name: index for index, name in enumerate(PREFLOP_CLASSES)}


@dataclass(frozen=True, slots=True)
class HUNLAbstractionConfig:
    preflop_mode: str = "class169"
    equity_samples: int = 48
    equity_buckets: int = 10

    def __post_init__(self) -> None:
        if self.preflop_mode not in {"class169", "combo1326"}:
            raise ValueError("preflop_mode must be class169 or combo1326")
        if type(self.equity_samples) is not int or self.equity_samples <= 0:
            raise ValueError("equity_samples must be a positive exact integer")
        if type(self.equity_buckets) is not int or not 2 <= self.equity_buckets <= 100:
            raise ValueError("equity_buckets must be an exact integer in 2..100")

    def to_payload(self) -> dict[str, Any]:
        return {
            "preflop_mode": self.preflop_mode,
            "equity_samples": self.equity_samples,
            "equity_buckets": self.equity_buckets,
        }


def preflop_class(cards: tuple[int, int] | list[int]) -> str:
    first, second = canonical_combo(cards)
    first_rank = first // 4 + 2
    second_rank = second // 4 + 2
    high, low = sorted((first_rank, second_rank), reverse=True)
    if high == low:
        return _rank_char(high) * 2
    suffix = "s" if first % 4 == second % 4 else "o"
    return f"{_rank_char(high)}{_rank_char(low)}{suffix}"


def preflop_class_id(cards: tuple[int, int] | list[int]) -> int:
    return PREFLOP_CLASS_INDEX[preflop_class(cards)]


def canonical_suit_state(
    hole_cards: tuple[int, int] | list[int],
    board: tuple[int, ...] | list[int],
) -> tuple[tuple[int, int], tuple[int, ...]]:
    """Return the lexicographically minimal exact state under all suit maps."""

    hole = canonical_combo(hole_cards)
    public = tuple(validate_card(card) for card in board)
    if len(set(hole + public)) != len(hole) + len(public):
        raise ValueError("hole cards and board conflict")

    def remap(card: int, permutation: tuple[int, int, int, int]) -> int:
        return (card // 4) * 4 + permutation[card % 4]

    candidates = (
        (
            tuple(sorted(remap(card, permutation) for card in hole)),
            tuple(sorted(remap(card, permutation) for card in public)),
        )
        for permutation in _SUIT_PERMUTATIONS
    )
    best_hole, best_board = min(candidates)
    return (best_hole[0], best_hole[1]), best_board


def _equity_seed(hole: tuple[int, int], board: tuple[int, ...]) -> int:
    encoded = _canonical_json(
        {
            "version": EQUITY_SAMPLER_VERSION,
            "hole": list(hole),
            "board": list(board),
        }
    ).encode("ascii")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:16], "big")


def deterministic_equity(
    hole_cards: tuple[int, int] | list[int],
    board: tuple[int, ...] | list[int],
    *,
    samples: int,
) -> float:
    """Estimate showdown equity with deterministic, card-removal-correct CRN."""

    if type(samples) is not int or samples <= 0:
        raise ValueError("equity samples must be a positive exact integer")
    hole, public = canonical_suit_state(hole_cards, board)
    if len(public) not in (3, 4, 5):
        raise ValueError("postflop equity requires three to five board cards")
    return _deterministic_equity_canonical(hole, public, samples)


@lru_cache(maxsize=131_072)
def _deterministic_equity_canonical(
    hole: tuple[int, int],
    public: tuple[int, ...],
    samples: int,
) -> float:
    """Cached implementation after suit canonicalization and validation."""

    used = set(hole + public)
    remaining = tuple(card for card in range(52) if card not in used)
    runout_count = 5 - len(public)
    draw_count = 2 + runout_count
    rng = random.Random(_equity_seed(hole, public))
    score = 0.0
    for _ in range(samples):
        draw = rng.sample(remaining, draw_count)
        opponent = (draw[0], draw[1])
        runout = tuple(draw[2:])
        final_board = public + runout
        comparison = compare_hands(hole + final_board, opponent + final_board)
        score += 1.0 if comparison > 0 else 0.5 if comparison == 0 else 0.0
    return score / samples


def equity_bucket(equity: float, bucket_count: int) -> int:
    if type(equity) not in (int, float) or not math.isfinite(float(equity)):
        raise TypeError("equity must be finite numeric")
    if not 0.0 <= equity <= 1.0:
        raise ValueError("equity must lie in [0, 1]")
    if type(bucket_count) is not int or bucket_count <= 1:
        raise ValueError("bucket_count must exceed one")
    return min(bucket_count - 1, int(float(equity) * bucket_count))


def board_texture(board: tuple[int, ...] | list[int]) -> str:
    public = tuple(validate_card(card) for card in board)
    if len(public) not in (3, 4, 5) or len(set(public)) != len(public):
        raise ValueError("board texture requires three to five distinct cards")
    ranks = [card // 4 + 2 for card in public]
    suits = [card % 4 for card in public]
    rank_counts = sorted(
        ({rank: ranks.count(rank) for rank in set(ranks)}).values(),
        reverse=True,
    )
    suit_counts = sorted(
        ({suit: suits.count(suit) for suit in set(suits)}).values(),
        reverse=True,
    )
    unique_ranks = sorted(set(ranks))
    gaps = sum(
        max(0, second - first - 1)
        for first, second in zip(unique_ranks, unique_ranks[1:])
    )
    paired = "trips" if rank_counts[0] >= 3 else "paired" if rank_counts[0] == 2 else "unpaired"
    flush = f"suits{'+'.join(str(value) for value in suit_counts)}"
    connectivity = "connected" if gaps <= 1 else "gapped" if gaps <= 3 else "disconnected"
    broadway = sum(rank >= 10 for rank in ranks)
    return f"{paired}:{flush}:{connectivity}:broadway{broadway}"


def spr_bucket(state: NationalGameState) -> str:
    if type(state) is not NationalGameState:
        raise TypeError("SPR requires exact Common NationalGameState")
    pot = max(1, state.pot)
    effective = min(state.stacks)
    spr = effective / pot
    for upper in (0.5, 1.0, 2.0, 4.0, 8.0, 16.0):
        if spr <= upper:
            return f"le{upper:g}"
    return "gt16"


@dataclass(frozen=True, slots=True)
class AbstractAction:
    label: str
    common_action: Action

    def __post_init__(self) -> None:
        if type(self.label) is not str or not self.label:
            raise TypeError("abstract action label must be an exact nonempty string")
        if type(self.common_action) is not Action:
            raise TypeError("abstract action must retain exact Common Action")


def _pot_fraction_raise_to(state: NationalGameState, fraction: float) -> int:
    assert state.actor is not None
    actor = state.actor
    other = 1 - actor
    to_call = max(0, state.street_bets[other] - state.street_bets[actor])
    pot_after_call = state.pot + to_call
    increment = max(1, int(math.floor(fraction * pot_after_call + 0.5)))
    return state.street_bets[actor] + to_call + increment


def legal_action_map(state: NationalGameState) -> tuple[AbstractAction, ...]:
    """Derive all action buckets solely through Common LegalActionSet/validation."""

    if type(state) is not NationalGameState:
        raise TypeError("action abstraction requires exact Common NationalGameState")
    state.assert_invariants()
    if state.actor is None or state.is_terminal or state.chance_pending:
        return ()
    legal = state.legal_actions()
    if type(legal) is not LegalActionSet:
        raise TypeError("Common legal_actions returned an unexpected type")
    candidates: list[tuple[str, Action]] = []
    for enabled, label, kind in (
        (legal.fold, "fold", ActionKind.FOLD),
        (legal.check, "check", ActionKind.CHECK),
        (legal.call, "call", ActionKind.CALL),
    ):
        if enabled:
            candidates.append((label, Action(kind)))
    if legal.min_raise_to is not None:
        candidates.append(("raise:min", Action(ActionKind.RAISE, legal.min_raise_to)))
        assert legal.max_raise_to is not None
        for label, fraction in (
            ("raise:half_pot", 0.5),
            ("raise:pot", 1.0),
            ("raise:overbet_1_5", 1.5),
        ):
            target = _pot_fraction_raise_to(state, fraction)
            target = min(legal.max_raise_to, max(legal.min_raise_to, target))
            candidates.append((label, Action(ActionKind.RAISE, target)))
    if legal.allin:
        candidates.append(("allin", Action(ActionKind.ALLIN)))

    seen: set[Action] = set()
    result: list[AbstractAction] = []
    for label, action in candidates:
        if action in seen:
            continue
        if not legal.contains(action):
            raise ValueError(f"Common LegalActionSet rejected generated {label}")
        valid, reason = state.validate_action(action)
        if not valid:
            raise ValueError(f"Common state rejected generated {label}: {reason}")
        seen.add(action)
        result.append(AbstractAction(label, action))
    if not result:
        raise ValueError("pending Common decision produced no abstract legal action")
    return tuple(result)


def _abstract_history_key(state: NationalGameState) -> str:
    """Replay and bucket the complete public action sequence.

    Exact numeric opponent raises are mapped to the nearest legal Route-B
    bucket at the pre-action state.  This preserves ordered perfect recall at
    the abstraction level without creating a separate infoset for every chip
    amount observed on the native wire.
    """

    replay = NationalGameState.new_hand(
        state.hand_number,
        small_blind=state.small_blind,
        hole_cards=state.hole_cards,
        match_net_before=state.match_net_before,
    )

    def deal_until(target: Street) -> None:
        nonlocal replay
        while replay.street is not target:
            if not replay.chance_pending:
                raise ValueError("history changes street without a closed betting round")
            if replay.street is Street.PREFLOP:
                cards = state.board[:3]
            elif replay.street is Street.FLOP:
                cards = state.board[3:4]
            elif replay.street is Street.TURN:
                cards = state.board[4:5]
            else:
                raise ValueError("history advances beyond river")
            replay = replay.apply_chance(cards)

    labels: list[str] = []
    for record in state.hand_history:
        deal_until(record.street)
        if replay.actor != record.actor:
            raise ValueError("history actor differs during abstraction replay")
        exact = _abstract_action_label(replay, record.action)
        labels.append(f"{record.street.value[0]}{record.actor}:{exact}")
        replay = replay.apply_action(
            record.action,
            inferred_from_boundary=record.inferred_from_boundary,
        )
    return "/".join(labels) or "root"


def _history_shape(state: NationalGameState) -> str:
    raises = sum(record.action.kind is ActionKind.RAISE for record in state.hand_history)
    allins = sum(record.action.kind is ActionKind.ALLIN for record in state.hand_history)
    passive = sum(
        record.action.kind in (ActionKind.CHECK, ActionKind.CALL)
        for record in state.hand_history
    )
    return f"r{raises}:a{allins}:p{passive}:n{len(state.hand_history)}"


def _abstract_action_label(state: NationalGameState, action: Action) -> str:
    """Map one proven Common action to its deterministic Route-B action id."""

    if type(state) is not NationalGameState or type(action) is not Action:
        raise TypeError("action labeling requires exact Common state/action types")
    buckets = legal_action_map(state)
    exact = next(
        (item.label for item in buckets if item.common_action == action),
        None,
    )
    if exact is None and action.kind is ActionKind.RAISE:
        raise_candidates = tuple(
            item for item in buckets if item.common_action.kind is ActionKind.RAISE
        )
        if not raise_candidates:
            raise ValueError("legal historical raise has no action bucket")
        exact = min(
            raise_candidates,
            key=lambda item: (
                abs(int(item.common_action.amount) - int(action.amount)),
                item.label,
            ),
        ).label
    if exact is None:
        raise ValueError("historical Common action has no abstract label")
    return exact


def _observation_fields(
    state: NationalGameState,
    player: int,
    config: HUNLAbstractionConfig,
) -> dict[str, Any]:
    """Return only information observable by ``player`` before one action."""

    if type(state) is not NationalGameState:
        raise TypeError("observation abstraction requires exact Common state")
    if type(player) is not int or player not in (0, 1):
        raise ValueError("player must be exact int 0 or 1")
    if type(config) is not HUNLAbstractionConfig:
        raise TypeError("config must be exact HUNLAbstractionConfig")
    if state.actor != player or state.is_terminal or state.chance_pending:
        raise ValueError("observation requires this player's pending decision")
    if len(state.hole_cards[player]) != 2:
        raise ValueError("observation requires two private cards")
    hole = canonical_combo(state.hole_cards[player])
    class_name = preflop_class(hole)
    class_id = PREFLOP_CLASS_INDEX[class_name]
    hand_bucket = (
        f"c{class_id}:{class_name}"
        if config.preflop_mode == "class169"
        else f"x{combo_index(hole)}"
    )
    texture = "preflop"
    if state.street is not Street.PREFLOP:
        equity = deterministic_equity(
            hole,
            state.board,
            samples=config.equity_samples,
        )
        equity_id = equity_bucket(equity, config.equity_buckets)
        category = int(rank_seven(hole + state.board)[0])
        texture = board_texture(state.board)
        hand_bucket = f"eq{equity_id}:cat{category}"
    return {
        "street": state.street.value,
        "position": "sb" if player == state.small_blind else "bb",
        "hand": hand_bucket,
        "legal": [item.label for item in legal_action_map(state)],
        "spr": spr_bucket(state),
        "texture": texture,
    }


def own_observation_recall(
    state: NationalGameState,
    player: int,
    config: HUNLAbstractionConfig,
) -> tuple[dict[str, Any], ...]:
    """Replay the ordered prior own observations and own abstract action ids.

    Public action history alone is not perfect recall after private/public card
    abstractions change between streets.  This trace retains every abstraction
    the acting player previously observed, paired with the action id selected
    at that observation.  It never reads the opponent's private cards.
    """

    if type(state) is not NationalGameState:
        raise TypeError("recall requires exact Common NationalGameState")
    if type(player) is not int or player not in (0, 1):
        raise ValueError("player must be exact int 0 or 1")
    if type(config) is not HUNLAbstractionConfig:
        raise TypeError("config must be exact HUNLAbstractionConfig")
    replay = NationalGameState.new_hand(
        state.hand_number,
        small_blind=state.small_blind,
        hole_cards=state.hole_cards,
        match_net_before=state.match_net_before,
    )

    def deal_until(target: Street) -> None:
        nonlocal replay
        while replay.street is not target:
            if not replay.chance_pending:
                raise ValueError("recall history crosses an open betting round")
            if replay.street is Street.PREFLOP:
                cards = state.board[:3]
            elif replay.street is Street.FLOP:
                cards = state.board[3:4]
            elif replay.street is Street.TURN:
                cards = state.board[4:5]
            else:
                raise ValueError("recall history advances beyond river")
            replay = replay.apply_chance(cards)

    trace: list[dict[str, Any]] = []
    for record in state.hand_history:
        deal_until(record.street)
        if replay.actor != record.actor:
            raise ValueError("history actor differs during recall replay")
        if record.actor == player:
            trace.append(
                {
                    "observation": _observation_fields(replay, player, config),
                    "action": _abstract_action_label(replay, record.action),
                }
            )
        replay = replay.apply_action(
            record.action,
            inferred_from_boundary=record.inferred_from_boundary,
        )
    return tuple(trace)


@dataclass(frozen=True, slots=True)
class InformationDescriptor:
    exact_key: str
    backoff_keys: tuple[str, ...]
    action_labels: tuple[str, ...]
    preflop_class: str
    preflop_class_id: int
    combo_index: int
    equity: float | None
    equity_bucket: int | None
    strength_category: int | None
    texture: str
    spr: str


def _key(level: str, payload: Mapping[str, Any]) -> str:
    return f"{INFORMATION_SCHEMA_VERSION}:{level}:{_canonical_json(payload)}"


def _strict_key_payload(key: str, level: str) -> dict[str, Any]:
    prefix = f"{INFORMATION_SCHEMA_VERSION}:{level}:"
    if type(key) is not str or not key.startswith(prefix):
        raise ValueError(f"information key is not a {level} key")

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant {value!r}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError(f"duplicate information-key field {name!r}")
            result[name] = value
        return result

    payload = json.loads(
        key[len(prefix) :],
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )
    if type(payload) is not dict:
        raise TypeError("information key payload must be an exact object")
    return payload


def backoff_keys_from_exact_key(exact_key: str) -> tuple[str, ...]:
    """Derive the runtime backoff hierarchy from an exact stored infoset key."""

    payload = _strict_key_payload(exact_key, "exact")
    required = {
        "street",
        "position",
        "hand",
        "legal",
        "spr",
        "texture",
        "history",
        "own_recall",
    }
    if set(payload) != required:
        raise ValueError("exact information key has the wrong schema")
    history = payload["history"]
    if type(history) is not str:
        raise TypeError("exact history must be an exact string")
    records = () if history == "root" else tuple(history.split("/"))
    if any(":" not in record for record in records):
        raise ValueError("exact history record is malformed")
    actions = tuple(record.split(":", 1)[1] for record in records)
    history_shape = (
        f"r{sum(action.startswith(('raise:', 'raise ')) for action in actions)}:"
        f"a{sum(action == 'allin' for action in actions)}:"
        f"p{sum(action in ('check', 'call') for action in actions)}:"
        f"n{len(actions)}"
    )
    common = {
        "street": payload["street"],
        "position": payload["position"],
        "hand": payload["hand"],
        "legal": payload["legal"],
    }
    backoff_payloads = (
        {
            **common,
            "spr": payload["spr"],
            "texture": payload["texture"],
            "history_shape": history_shape,
        },
        {**common, "texture": payload["texture"]},
        {
            "street": payload["street"],
            "position": payload["position"],
            "legal": payload["legal"],
        },
        {"legal": payload["legal"]},
    )
    return tuple(
        _key(f"backoff{index}", value)
        for index, value in enumerate(backoff_payloads, start=1)
    )


def information_descriptor(
    state: NationalGameState,
    player: int,
    config: HUNLAbstractionConfig,
) -> InformationDescriptor:
    """Build one perfect-recall, action-mask-stable HUNL information key."""

    if type(state) is not NationalGameState:
        raise TypeError("infoset abstraction requires exact Common NationalGameState")
    if type(player) is not int or player not in (0, 1):
        raise ValueError("player must be exact int 0 or 1")
    if type(config) is not HUNLAbstractionConfig:
        raise TypeError("config must be exact HUNLAbstractionConfig")
    state.assert_invariants()
    if state.actor != player or state.is_terminal or state.chance_pending:
        raise ValueError("information descriptor requires this player's pending decision")
    if len(state.hole_cards[player]) != 2:
        raise ValueError("information descriptor requires two private cards")
    hole = canonical_combo(state.hole_cards[player])
    class_name = preflop_class(hole)
    class_id = PREFLOP_CLASS_INDEX[class_name]
    exact_combo = combo_index(hole)
    actions = legal_action_map(state)
    labels = tuple(item.label for item in actions)
    position = "sb" if player == state.small_blind else "bb"
    hand_bucket = (
        f"c{class_id}:{class_name}"
        if config.preflop_mode == "class169"
        else f"x{exact_combo}"
    )
    equity: float | None = None
    equity_id: int | None = None
    category: int | None = None
    texture = "preflop"
    if state.street is not Street.PREFLOP:
        equity = deterministic_equity(
            hole,
            state.board,
            samples=config.equity_samples,
        )
        equity_id = equity_bucket(equity, config.equity_buckets)
        category = int(rank_seven(hole + state.board)[0])
        texture = board_texture(state.board)
        hand_bucket = f"eq{equity_id}:cat{category}"
    common = {
        "street": state.street.value,
        "position": position,
        "hand": hand_bucket,
        "legal": list(labels),
    }
    exact_payload = {
        **common,
        "spr": spr_bucket(state),
        "texture": texture,
        "history": _abstract_history_key(state),
        "own_recall": list(own_observation_recall(state, player, config)),
    }
    exact_key = _key("exact", exact_payload)
    backoff_keys = backoff_keys_from_exact_key(exact_key)
    # Keep an independent live derivation assertion so a future history-key
    # change cannot silently alter artifact aggregation semantics.
    if backoff_keys[0] != _key(
        "backoff1",
        {
            **common,
            "spr": spr_bucket(state),
            "texture": texture,
            "history_shape": _history_shape(state),
        },
    ):
        raise AssertionError("exact-key backoff derivation drifted from live history")
    return InformationDescriptor(
        exact_key=exact_key,
        backoff_keys=backoff_keys,
        action_labels=labels,
        preflop_class=class_name,
        preflop_class_id=class_id,
        combo_index=exact_combo,
        equity=equity,
        equity_bucket=equity_id,
        strength_category=category,
        texture=texture,
        spr=spr_bucket(state),
    )


def abstraction_asset_payload(config: HUNLAbstractionConfig) -> dict[str, Any]:
    combo_classes = [preflop_class(combo) for combo in all_hole_combinations()]
    return {
        "card_version": CARD_ABSTRACTION_VERSION,
        "action_version": ACTION_ABSTRACTION_VERSION,
        "information_schema": INFORMATION_SCHEMA_VERSION,
        "equity_sampler": EQUITY_SAMPLER_VERSION,
        "config": config.to_payload(),
        "preflop_classes": list(PREFLOP_CLASSES),
        "combo_count": len(combo_classes),
        "combo_to_class": combo_classes,
        "action_labels": [
            "fold",
            "check",
            "call",
            "raise:min",
            "raise:half_pot",
            "raise:pot",
            "raise:overbet_1_5",
            "allin",
        ],
        "raise_semantics": "Common Action(RAISE, raise_to_total); Common dedupe",
    }


def abstraction_asset_sha256(config: HUNLAbstractionConfig) -> str:
    return payload_sha256(abstraction_asset_payload(config))
