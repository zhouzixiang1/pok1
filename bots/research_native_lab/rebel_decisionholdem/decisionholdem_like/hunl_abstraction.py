"""Versioned HUNL card, betting and action abstractions for route A2.

The Common package is the only rules authority.  This module never recreates
betting legality: it turns a trusted :class:`NationalGameState` and its
``LegalActionSet`` into a finite training/action abstraction.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ...common_contracts.actions import Action, ActionKind, LegalActionSet
from ...common_contracts.cards import (
    canonical_combo,
    combo_index,
    legal_combo_mask,
    rank_seven,
)
from ...common_contracts.constants import BIG_BLIND, CONTRACT_VERSION
from ...common_contracts.national_state import NationalGameState, Street


HUNL_RULES_VERSION = "common-national-hunl-20000-50-100-v1"
HUNL_ACTION_ABSTRACTION_VERSION = "route-a2-hunl-actions-v1"
HUNL_CARD_ABSTRACTION_VERSION = "route-a2-hunl-cards-v1"
HUNL_INFOSET_VERSION = "route-a2-hunl-infoset-perfect-recall-v3"
HUNL_ACTION_IDS = (
    "fold",
    "check_call",
    "exact_min",
    "half_pot",
    "pot",
    "one_half_pot",
    "allin",
)
POT_FRACTIONS = (
    ("half_pot", 0.5),
    ("pot", 1.0),
    ("one_half_pot", 1.5),
)
HAND_CATEGORY_NAMES = (
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


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _trusted_state(state: NationalGameState) -> NationalGameState:
    if type(state) is not NationalGameState:
        raise TypeError("state must be the exact Common NationalGameState type")
    state.assert_invariants()
    return state


def _rank_symbol(rank: int) -> str:
    return "23456789TJQKA"[rank - 2]


def preflop_class(cards: Sequence[int]) -> str:
    """Map one exact Common combo onto one of the canonical 169 classes."""

    first, second = canonical_combo(cards)
    first_rank, second_rank = first // 4 + 2, second // 4 + 2
    high, low = sorted((first_rank, second_rank), reverse=True)
    if high == low:
        return _rank_symbol(high) * 2
    suited = first % 4 == second % 4
    return f"{_rank_symbol(high)}{_rank_symbol(low)}{'s' if suited else 'o'}"


def all_preflop_classes() -> tuple[str, ...]:
    classes: list[str] = []
    for high in range(14, 1, -1):
        for low in range(14, 1, -1):
            if high == low:
                value = _rank_symbol(high) * 2
            elif high > low:
                value = f"{_rank_symbol(high)}{_rank_symbol(low)}s"
            else:
                value = f"{_rank_symbol(low)}{_rank_symbol(high)}o"
            if value not in classes:
                classes.append(value)
    return tuple(classes)


def _board_pairing(board: Sequence[int]) -> str:
    counts: dict[int, int] = {}
    for card in board:
        rank = card // 4
        counts[rank] = counts.get(rank, 0) + 1
    multiplicities = sorted(counts.values(), reverse=True)
    if multiplicities[0] == 4:
        return "quads"
    if multiplicities[0] == 3:
        return "trips" if 2 not in multiplicities[1:] else "full_house"
    pair_count = multiplicities.count(2)
    if pair_count >= 2:
        return "two_pair"
    if pair_count == 1:
        return "paired"
    return "unpaired"


def _board_suit_texture(board: Sequence[int]) -> tuple[str, int]:
    counts = [sum(card % 4 == suit for card in board) for suit in range(4)]
    maximum = max(counts)
    if maximum == 1:
        name = "rainbow"
    elif maximum == 2:
        name = "two_tone"
    elif maximum == 3:
        name = "three_flush"
    elif maximum == 4:
        name = "four_flush"
    else:
        name = "five_flush"
    return name, maximum


def _board_connectivity(board: Sequence[int]) -> int:
    ranks = {card // 4 + 2 for card in board}
    if 14 in ranks:
        ranks.add(1)
    return max(
        sum(rank in ranks for rank in range(start, start + 5))
        for start in range(1, 11)
    )


def _high_rank_band(rank: int) -> str:
    if rank <= 7:
        return "low"
    if rank <= 10:
        return "middle"
    if rank <= 12:
        return "broadway"
    return "king_ace"


@dataclass(frozen=True, slots=True)
class HUNLHandAbstraction:
    exact_combo_index: int
    preflop_class: str
    bucket: str
    made_category: str
    strength_band: str
    board_pairing: str
    board_suit_texture: str
    board_connectivity: int
    rank_blockers: int
    suit_blockers: int
    legal_opponent_combos: int


def hand_abstraction(
    hole_cards: Sequence[int],
    board: Sequence[int],
) -> HUNLHandAbstraction:
    """Return an explainable deterministic card bucket and exact combo path."""

    hole = canonical_combo(hole_cards)
    board_tuple = tuple(board)
    if len(board_tuple) not in (0, 3, 4, 5):
        raise ValueError("board must contain 0, 3, 4 or 5 cards")
    known = hole + board_tuple
    if len(set(known)) != len(known):
        raise ValueError("hole cards and board conflict")
    exact_index = combo_index(hole)
    class_169 = preflop_class(hole)
    opponent_combos = sum(legal_combo_mask(known))
    if not board_tuple:
        return HUNLHandAbstraction(
            exact_combo_index=exact_index,
            preflop_class=class_169,
            bucket=f"preflop:{class_169}",
            made_category="preflop",
            strength_band="preflop",
            board_pairing="none",
            board_suit_texture="none",
            board_connectivity=0,
            rank_blockers=0,
            suit_blockers=0,
            legal_opponent_combos=opponent_combos,
        )

    rank = rank_seven(hole + board_tuple)
    category = HAND_CATEGORY_NAMES[rank[0]]
    strength_band = _high_rank_band(rank[1])
    pairing = _board_pairing(board_tuple)
    suit_texture, maximum_suit_count = _board_suit_texture(board_tuple)
    connectivity = _board_connectivity(board_tuple)
    board_ranks = {card // 4 for card in board_tuple}
    rank_blockers = sum(card // 4 in board_ranks for card in hole)
    board_suit_counts = {
        suit: sum(card % 4 == suit for card in board_tuple) for suit in range(4)
    }
    dominant_suits = {
        suit
        for suit, count in board_suit_counts.items()
        if count == maximum_suit_count and maximum_suit_count >= 2
    }
    suit_blockers = sum(card % 4 in dominant_suits for card in hole)
    bucket = ":".join(
        (
            category,
            strength_band,
            pairing,
            suit_texture,
            f"conn{connectivity}",
            f"rb{rank_blockers}",
            f"sb{suit_blockers}",
        )
    )
    return HUNLHandAbstraction(
        exact_combo_index=exact_index,
        preflop_class=class_169,
        bucket=bucket,
        made_category=category,
        strength_band=strength_band,
        board_pairing=pairing,
        board_suit_texture=suit_texture,
        board_connectivity=connectivity,
        rank_blockers=rank_blockers,
        suit_blockers=suit_blockers,
        legal_opponent_combos=opponent_combos,
    )


@dataclass(frozen=True, slots=True)
class HUNLActionSpec:
    action_id: str
    action: Action

    @property
    def wire_action(self) -> str:
        return self.action.to_wire()


def _append_unique(
    output: list[HUNLActionSpec],
    seen_wire: set[str],
    action_id: str,
    action: Action,
    legal: LegalActionSet,
) -> None:
    if action_id not in HUNL_ACTION_IDS:
        raise ValueError(f"unknown HUNL action id {action_id}")
    wire = action.to_wire()
    if wire in seen_wire:
        return
    if not legal.contains(action):
        raise AssertionError(f"abstract action escaped Common legality: {wire}")
    output.append(HUNLActionSpec(action_id, action))
    seen_wire.add(wire)


def abstract_actions(state: NationalGameState) -> tuple[HUNLActionSpec, ...]:
    """Build legal, deduplicated raise-to actions from Common bounds only."""

    state = _trusted_state(state)
    if state.actor is None or state.is_terminal or state.chance_pending:
        raise ValueError("abstract actions require a Common decision state")
    legal = state.legal_actions()
    output: list[HUNLActionSpec] = []
    seen_wire: set[str] = set()
    if legal.fold:
        _append_unique(output, seen_wire, "fold", Action(ActionKind.FOLD), legal)
    if legal.check:
        _append_unique(output, seen_wire, "check_call", Action(ActionKind.CHECK), legal)
    elif legal.call:
        _append_unique(output, seen_wire, "check_call", Action(ActionKind.CALL), legal)

    if legal.min_raise_to is not None and legal.max_raise_to is not None:
        _append_unique(
            output,
            seen_wire,
            "exact_min",
            Action(ActionKind.RAISE, legal.min_raise_to),
            legal,
        )
        actor = state.actor
        opponent = 1 - actor
        to_call = max(0, state.street_bets[opponent] - state.street_bets[actor])
        pot_after_call = state.pot + to_call
        matched_bet = max(state.street_bets)
        for action_id, fraction in POT_FRACTIONS:
            target = matched_bet + int(math.floor(fraction * pot_after_call + 0.5))
            if legal.min_raise_to <= target <= legal.max_raise_to:
                _append_unique(
                    output,
                    seen_wire,
                    action_id,
                    Action(ActionKind.RAISE, target),
                    legal,
                )
    if legal.allin:
        _append_unique(output, seen_wire, "allin", Action(ActionKind.ALLIN), legal)
    if not output:
        raise RuntimeError("Common exposed a decision without an abstract legal action")
    if len({item.action_id for item in output}) != len(output):
        raise AssertionError("HUNL action ids must be unique after legal deduplication")
    return tuple(output)


def _ratio_bucket(value: float, boundaries: Iterable[tuple[float, str]], last: str) -> str:
    for boundary, name in boundaries:
        if value <= boundary:
            return name
    return last


def _action_line(state: NationalGameState) -> str:
    tokens = {
        ActionKind.FOLD: "f",
        ActionKind.CHECK: "k",
        ActionKind.CALL: "c",
        ActionKind.RAISE: "r",
        ActionKind.ALLIN: "a",
    }
    return "".join(tokens[record.action.kind] for record in state.hand_history) or "root"


@dataclass(frozen=True, slots=True)
class HUNLInformationAbstraction:
    key: str
    street: str
    position: str
    hand: HUNLHandAbstraction
    pot_bucket: str
    spr_bucket: str
    to_call_bucket: str
    betting_line: str
    raise_count: str
    legal_signature: tuple[str, ...]
    observation_recall: tuple[tuple[str, str], ...]
    action_recall: tuple[tuple[str, str, str, str], ...]


_INFO_FIELDS = {
    "action_recall",
    "betting_line",
    "card_bucket",
    "legal",
    "observation_recall",
    "position",
    "pot",
    "raises",
    "spr",
    "street",
    "to_call",
    "version",
}
_INFO_STRING_DOMAINS = {
    "position": {"bb", "sb"},
    "pot": {"p2", "p4", "p8", "p16", "p32", "p64", "p128plus"},
    "raises": {"0", "1", "2", "3plus"},
    "spr": {"spr0.5", "spr1", "spr2", "spr4", "spr8", "spr16", "spr16plus"},
    "street": {"preflop", "flop", "turn", "river"},
    "to_call": {"none", "c0.25", "c0.5", "c1", "c1plus"},
}
_STREET_SEQUENCE = ("preflop", "flop", "turn", "river")


def _validate_observation_recall(
    payload: object,
    *,
    current_street: str,
) -> tuple[tuple[str, str], ...]:
    expected_streets = _STREET_SEQUENCE[: _STREET_SEQUENCE.index(current_street) + 1]
    if not isinstance(payload, list) or len(payload) != len(expected_streets):
        raise ValueError("HUNL observation recall has the wrong street coverage")
    result: list[tuple[str, str]] = []
    for raw, expected_street in zip(payload, expected_streets, strict=True):
        if (
            not isinstance(raw, dict)
            or set(raw) != {"card_bucket", "street"}
            or raw["street"] != expected_street
            or type(raw["card_bucket"]) is not str
            or not raw["card_bucket"]
        ):
            raise ValueError("HUNL observation recall entry is invalid")
        result.append((expected_street, raw["card_bucket"]))
    return tuple(result)


def _validate_action_recall(
    payload: object,
    *,
    current_street: str,
) -> tuple[tuple[str, str, str, str], ...]:
    if not isinstance(payload, list):
        raise ValueError("HUNL action recall must be a list")
    result: list[tuple[str, str, str, str]] = []
    previous_street_index = 0
    current_street_index = _STREET_SEQUENCE.index(current_street)
    for raw in payload:
        if not isinstance(raw, dict) or set(raw) != {
            "abstract_action",
            "actor",
            "street",
            "wire_action",
        }:
            raise ValueError("HUNL action recall entry fields are invalid")
        street = raw["street"]
        actor = raw["actor"]
        action_id = raw["abstract_action"]
        wire = raw["wire_action"]
        if type(street) is not str or street not in _STREET_SEQUENCE:
            raise ValueError("HUNL action recall street is invalid")
        street_index = _STREET_SEQUENCE.index(street)
        if street_index < previous_street_index or street_index > current_street_index:
            raise ValueError("HUNL action recall street order is invalid")
        previous_street_index = street_index
        if actor not in ("self", "other"):
            raise ValueError("HUNL action recall actor is invalid")
        if action_id not in (*HUNL_ACTION_IDS, "off_grid"):
            raise ValueError("HUNL action recall abstraction label is invalid")
        if type(wire) is not str:
            raise ValueError("HUNL action recall wire action is invalid")
        try:
            action = Action.from_wire(wire)
        except ValueError as exc:
            raise ValueError("HUNL action recall wire action is invalid") from exc
        if action_id == "fold" and action.kind is not ActionKind.FOLD:
            raise ValueError("HUNL fold recall label disagrees with wire action")
        if action_id == "check_call" and action.kind not in (
            ActionKind.CHECK,
            ActionKind.CALL,
        ):
            raise ValueError("HUNL check/call recall label disagrees with wire action")
        if action_id == "allin" and action.kind is not ActionKind.ALLIN:
            raise ValueError("HUNL all-in recall label disagrees with wire action")
        if action_id in {"exact_min", "half_pot", "pot", "one_half_pot"} and (
            action.kind is not ActionKind.RAISE
        ):
            raise ValueError("HUNL raise recall label disagrees with wire action")
        result.append((street, actor, action_id, wire))
    return tuple(result)


def parse_infoset_key(key: str) -> dict[str, Any]:
    prefix = HUNL_INFOSET_VERSION + "|"
    if not isinstance(key, str) or not key.startswith(prefix):
        raise ValueError("HUNL infoset key has the wrong version prefix")
    try:
        payload = json.loads(key[len(prefix) :], object_pairs_hook=_strict_object)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("HUNL infoset key is not strict JSON") from exc
    if not isinstance(payload, dict) or set(payload) != _INFO_FIELDS:
        raise ValueError("HUNL infoset key fields are not canonical")
    if payload["version"] != HUNL_INFOSET_VERSION:
        raise ValueError("HUNL infoset payload version mismatch")
    if (
        type(payload["card_bucket"]) is not str
        or not payload["card_bucket"]
        or type(payload["betting_line"]) is not str
        or not payload["betting_line"]
        or (
            payload["betting_line"] != "root"
            and any(token not in "fkrca" for token in payload["betting_line"])
        )
    ):
        raise ValueError("HUNL infoset card/action fields are invalid")
    if any(
        type(payload[field]) is not str or payload[field] not in allowed
        for field, allowed in _INFO_STRING_DOMAINS.items()
    ):
        raise ValueError("HUNL infoset scalar bucket is invalid")
    _validate_observation_recall(
        payload["observation_recall"], current_street=payload["street"]
    )
    _validate_action_recall(
        payload["action_recall"], current_street=payload["street"]
    )
    legal = payload["legal"]
    if (
        not isinstance(legal, list)
        or not legal
        or any(type(value) is not str or value not in HUNL_ACTION_IDS for value in legal)
        or len(set(legal)) != len(legal)
    ):
        raise ValueError("HUNL infoset legal signature is invalid")
    if key != prefix + _canonical_json(payload):
        raise ValueError("HUNL infoset key is not canonically encoded")
    return payload


def _board_prefix_for_street(board: Sequence[int], street: str) -> tuple[int, ...]:
    lengths = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
    return tuple(board[: lengths[street]])


def _observation_recall(
    state: NationalGameState,
    player: int,
) -> tuple[tuple[str, str], ...]:
    current_index = _STREET_SEQUENCE.index(state.street.value)
    hole = state.hole_cards[player]
    return tuple(
        (
            street,
            hand_abstraction(hole, _board_prefix_for_street(state.board, street)).bucket,
        )
        for street in _STREET_SEQUENCE[: current_index + 1]
    )


def _next_replay_chance_cards(
    replay: NationalGameState,
    board: Sequence[int],
) -> tuple[int, ...]:
    if replay.street is Street.PREFLOP:
        return tuple(board[:3])
    if replay.street is Street.FLOP:
        return tuple(board[3:4])
    if replay.street is Street.TURN:
        return tuple(board[4:5])
    raise ValueError("action recall cannot advance chance beyond the river")


def _action_recall(
    state: NationalGameState,
    player: int,
) -> tuple[tuple[str, str, str, str], ...]:
    replay = NationalGameState.new_hand(
        state.hand_number,
        small_blind=state.small_blind,
        hole_cards=state.hole_cards,
        match_net_before=state.match_net_before,
    )
    result: list[tuple[str, str, str, str]] = []
    for record in state.hand_history:
        while replay.street is not record.street:
            if not replay.chance_pending:
                raise ValueError("history changes street before a replayable chance node")
            replay = replay.apply_chance(
                _next_replay_chance_cards(replay, state.board)
            )
        if replay.actor != record.actor:
            raise ValueError("history actor differs during action-recall replay")
        matching_ids = [
            spec.action_id
            for spec in abstract_actions(replay)
            if spec.action == record.action
        ]
        if len(matching_ids) > 1:
            raise AssertionError("deduplicated abstract actions produced two recall labels")
        action_id = matching_ids[0] if matching_ids else "off_grid"
        result.append(
            (
                record.street.value,
                "self" if record.actor == player else "other",
                action_id,
                record.action.to_wire(),
            )
        )
        replay = replay.apply_action(
            record.action,
            inferred_from_boundary=record.inferred_from_boundary,
        )
    return tuple(result)


def perfect_recall_signature(
    state: NationalGameState,
    player: int,
) -> dict[str, object]:
    """Return the exact abstract-observation and public-action recall contract."""

    state = _trusted_state(state)
    if type(player) is not int or player not in (0, 1):
        raise ValueError("perfect recall requires an exact player index")
    observations = _observation_recall(state, player)
    actions = _action_recall(state, player)
    return {
        "observation_recall": [
            {"card_bucket": bucket, "street": street}
            for street, bucket in observations
        ],
        "action_recall": [
            {
                "abstract_action": action_id,
                "actor": actor,
                "street": street,
                "wire_action": wire,
            }
            for street, actor, action_id, wire in actions
        ],
    }


def information_abstraction(
    state: NationalGameState,
    player: int,
) -> HUNLInformationAbstraction:
    state = _trusted_state(state)
    if type(player) is not int or player not in (0, 1) or state.actor != player:
        raise ValueError("information abstraction requires the acting player")
    if len(state.hole_cards[player]) != 2:
        raise ValueError("acting player must have exactly two known private cards")
    actions = abstract_actions(state)
    hand = hand_abstraction(state.hole_cards[player], state.board)
    opponent = 1 - player
    to_call = max(0, state.street_bets[opponent] - state.street_bets[player])
    pot_bb = state.pot / BIG_BLIND
    pot_bucket = _ratio_bucket(
        pot_bb,
        ((2, "p2"), (4, "p4"), (8, "p8"), (16, "p16"), (32, "p32"), (64, "p64")),
        "p128plus",
    )
    effective_stack = min(state.stacks)
    spr = effective_stack / max(1, state.pot)
    spr_bucket = _ratio_bucket(
        spr,
        ((0.5, "spr0.5"), (1, "spr1"), (2, "spr2"), (4, "spr4"), (8, "spr8"), (16, "spr16")),
        "spr16plus",
    )
    call_ratio = to_call / max(1, state.pot)
    to_call_bucket = (
        "none"
        if to_call == 0
        else _ratio_bucket(
            call_ratio,
            ((0.25, "c0.25"), (0.5, "c0.5"), (1.0, "c1")),
            "c1plus",
        )
    )
    raise_total = sum(
        record.action.kind is ActionKind.RAISE for record in state.street_actions
    )
    raise_count = str(raise_total) if raise_total < 3 else "3plus"
    position = "sb" if state.small_blind == player else "bb"
    legal_signature = tuple(action.action_id for action in actions)
    recall = perfect_recall_signature(state, player)
    payload = {
        "action_recall": recall["action_recall"],
        "betting_line": _action_line(state),
        "card_bucket": hand.bucket,
        "legal": list(legal_signature),
        "observation_recall": recall["observation_recall"],
        "position": position,
        "pot": pot_bucket,
        "raises": raise_count,
        "spr": spr_bucket,
        "street": state.street.value,
        "to_call": to_call_bucket,
        "version": HUNL_INFOSET_VERSION,
    }
    key = HUNL_INFOSET_VERSION + "|" + _canonical_json(payload)
    parse_infoset_key(key)
    return HUNLInformationAbstraction(
        key=key,
        street=state.street.value,
        position=position,
        hand=hand,
        pot_bucket=pot_bucket,
        spr_bucket=spr_bucket,
        to_call_bucket=to_call_bucket,
        betting_line=payload["betting_line"],
        raise_count=raise_count,
        legal_signature=legal_signature,
        observation_recall=_validate_observation_recall(
            payload["observation_recall"], current_street=state.street.value
        ),
        action_recall=_validate_action_recall(
            payload["action_recall"], current_street=state.street.value
        ),
    )


def abstraction_contract() -> dict[str, object]:
    return {
        "common_contract_version": CONTRACT_VERSION,
        "rules_version": HUNL_RULES_VERSION,
        "card_abstraction_version": HUNL_CARD_ABSTRACTION_VERSION,
        "action_abstraction_version": HUNL_ACTION_ABSTRACTION_VERSION,
        "infoset_version": HUNL_INFOSET_VERSION,
        "preflop_classes": 169,
        "exact_hole_combinations": 1326,
        "diagnostic_metadata_not_policy_key": [
            "exact_combo_index",
            "legal_opponent_combo_count",
        ],
        "action_ids": list(HUNL_ACTION_IDS),
        "raise_fractions": {name: fraction for name, fraction in POT_FRACTIONS},
        "postflop_features": [
            "exact_common_hand_category",
            "strength_high-rank_band",
            "board_pairing",
            "board_suit_texture",
            "board_connectivity",
            "hole_rank_blockers",
            "dominant_suit_blockers",
        ],
    }
