"""Canonical public-family identity for leakage-safe neural data splits."""

from __future__ import annotations

import hashlib
import itertools
import json
from typing import Any, Sequence

from ..common_contracts.cards import validate_card
from ..common_contracts.constants import CONTRACT_VERSION
from ..common_contracts.national_state import NationalGameState


PUBLIC_FAMILY_SCHEMA = "neural-public-family-v1"
BOARD_ISOMORPHISM_SCHEMA = (
    "global-suit-permutation-min;flop-unordered;turn-river-ordered-v1"
)

_FORBIDDEN_KEYS = frozenset(
    {
        "hole_cards",
        "private_hand",
        "hand_number",
        "match_net_before",
        "match_context_id",
        "observation_id",
        "full_state_id",
        "winner",
        "terminal_reason",
        "sampled_deal",
        "future_board",
        "outcome",
        "payoff",
    }
)

_PUBLIC_STATE_FIELDS = frozenset(
    {
        "contract_version",
        "small_blind",
        "street",
        "actor",
        "stacks",
        "total_contributions",
        "street_bets",
        "action_counts",
        "street_actions",
        "hand_history",
        "board",
        "allin_occurred",
        "chance_pending",
        "runout_pending",
    }
)

_LEGAL_SUPPORT_FIELDS = frozenset(
    {"allin", "call", "check", "fold", "max_raise_to", "min_raise_to"}
)

_ACTION_RECORD_FIELDS = frozenset({"actor", "amount", "kind", "street"})
_STREETS = frozenset({"preflop", "flop", "turn", "river"})
_ACTION_KINDS = frozenset({"fold", "check", "call", "raise", "allin"})


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _assert_public_only(value: object, *, path: str = "public_family") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} contains a non-string key")
            if key in _FORBIDDEN_KEYS:
                raise ValueError(f"{path} contains forbidden key {key}")
            _assert_public_only(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_public_only(child, path=f"{path}[{index}]")
    elif value is not None and type(value) not in (str, int, bool):
        raise ValueError(f"{path} contains a non-canonical scalar")


def _exact_int(value: object, path: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int:
        raise ValueError(f"{path} must be an exact integer")
    return value


def _exact_bool(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{path} must be an exact boolean")
    return value


def _exact_integer_pair(value: object, path: str) -> list[int]:
    if type(value) is not list or len(value) != 2:
        raise ValueError(f"{path} must be an exact two-element list")
    return [
        _exact_int(value[0], f"{path}[0]"),
        _exact_int(value[1], f"{path}[1]"),
    ]


def _validate_action_record(value: object, path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != _ACTION_RECORD_FIELDS:
        raise ValueError(f"{path} fields differ")
    actor = _exact_int(value["actor"], f"{path}.actor")
    if actor not in (0, 1):
        raise ValueError(f"{path}.actor must be player 0 or 1")
    street = value["street"]
    if type(street) is not str or street not in _STREETS:
        raise ValueError(f"{path}.street is invalid")
    kind = value["kind"]
    if type(kind) is not str or kind not in _ACTION_KINDS:
        raise ValueError(f"{path}.kind is invalid")
    amount = _exact_int(value["amount"], f"{path}.amount", nullable=True)
    if (kind == "raise") != (amount is not None):
        raise ValueError(f"{path}.amount differs from action kind")
    return {
        "actor": actor,
        "amount": amount,
        "kind": kind,
        "street": street,
    }


def _validate_public_state_types(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _PUBLIC_STATE_FIELDS:
        raise ValueError("public family Common state fields differ")
    if type(value["contract_version"]) is not str:
        raise ValueError("public_state.contract_version must be an exact string")
    small_blind = _exact_int(value["small_blind"], "public_state.small_blind")
    if small_blind not in (0, 1):
        raise ValueError("public_state.small_blind must be player 0 or 1")
    street = value["street"]
    if type(street) is not str or street not in _STREETS:
        raise ValueError("public_state.street is invalid")
    actor = _exact_int(value["actor"], "public_state.actor", nullable=True)
    if actor not in (None, 0, 1):
        raise ValueError("public_state.actor must be player 0, player 1 or null")
    board = value["board"]
    if type(board) is not list:
        raise ValueError("public_state.board must be an exact list")
    normalized_board = [
        _exact_int(card, f"public_state.board[{index}]")
        for index, card in enumerate(board)
    ]
    histories: dict[str, list[dict[str, object]]] = {}
    for field in ("street_actions", "hand_history"):
        records = value[field]
        if type(records) is not list:
            raise ValueError(f"public_state.{field} must be an exact list")
        histories[field] = [
            _validate_action_record(record, f"public_state.{field}[{index}]")
            for index, record in enumerate(records)
        ]
    return {
        "contract_version": value["contract_version"],
        "small_blind": small_blind,
        "street": street,
        "actor": actor,
        "stacks": _exact_integer_pair(value["stacks"], "public_state.stacks"),
        "total_contributions": _exact_integer_pair(
            value["total_contributions"], "public_state.total_contributions"
        ),
        "street_bets": _exact_integer_pair(
            value["street_bets"], "public_state.street_bets"
        ),
        "action_counts": _exact_integer_pair(
            value["action_counts"], "public_state.action_counts"
        ),
        "street_actions": histories["street_actions"],
        "hand_history": histories["hand_history"],
        "board": normalized_board,
        "allin_occurred": _exact_bool(
            value["allin_occurred"], "public_state.allin_occurred"
        ),
        "chance_pending": _exact_bool(
            value["chance_pending"], "public_state.chance_pending"
        ),
        "runout_pending": _exact_bool(
            value["runout_pending"], "public_state.runout_pending"
        ),
    }


def _validate_legal_support_types(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != _LEGAL_SUPPORT_FIELDS:
        raise ValueError("public family legal-action support fields differ")
    return {
        "allin": _exact_bool(value["allin"], "legal_action_support.allin"),
        "call": _exact_bool(value["call"], "legal_action_support.call"),
        "check": _exact_bool(value["check"], "legal_action_support.check"),
        "fold": _exact_bool(value["fold"], "legal_action_support.fold"),
        "max_raise_to": _exact_int(
            value["max_raise_to"],
            "legal_action_support.max_raise_to",
            nullable=True,
        ),
        "min_raise_to": _exact_int(
            value["min_raise_to"],
            "legal_action_support.min_raise_to",
            nullable=True,
        ),
    }


def _map_suit(card: int, permutation: Sequence[int]) -> int:
    card = validate_card(card)
    return (card // 4) * 4 + permutation[card % 4]


def canonical_board_under_suit_isomorphism(board: Sequence[int]) -> tuple[int, ...]:
    """Return the unique board representative under all 24 suit relabelings.

    The national state publishes a three-card flop atomically.  Its internal
    ordering is not poker information, so the first three mapped cards are
    sorted.  Turn and river are appended in their observed order.
    """

    if type(board) not in (tuple, list):
        raise TypeError("board must be a tuple or list")
    normalized = tuple(validate_card(card) for card in board)
    if len(normalized) not in (0, 3, 4, 5):
        raise ValueError("public board length must be 0, 3, 4 or 5")
    if len(set(normalized)) != len(normalized):
        raise ValueError("public board contains duplicate cards")
    if not normalized:
        return ()

    candidates: list[tuple[int, ...]] = []
    for permutation in itertools.permutations(range(4)):
        mapped = tuple(_map_suit(card, permutation) for card in normalized)
        candidates.append(tuple(sorted(mapped[:3])) + mapped[3:])
    return min(candidates)


def _legal_support(state: NationalGameState) -> dict[str, object]:
    legal = state.legal_actions()
    return {
        "allin": legal.allin,
        "call": legal.call,
        "check": legal.check,
        "fold": legal.fold,
        "max_raise_to": legal.max_raise_to,
        "min_raise_to": legal.min_raise_to,
    }


def public_family_payload(state: NationalGameState) -> dict[str, Any]:
    """Build a canonical, single-hand, public-only family descriptor.

    Terminal states are rejected because their winner/reason is label outcome,
    not an input family.  Decision and public chance-boundary states are both
    representable; the latter have an empty legal-action support.
    """

    if type(state) is not NationalGameState:
        raise TypeError("state must be the exact Common NationalGameState type")
    state.assert_invariants()
    if state.is_terminal or state.terminal_reason is not None or state.winner is not None:
        raise ValueError("terminal/outcome state cannot become a public family")

    public_state = state.hand_public_dict()
    if public_state.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("Common contract version differs")
    if public_state.pop("terminal_reason", None) is not None:
        raise ValueError("terminal reason leaked into public family")
    if public_state.pop("winner", None) is not None:
        raise ValueError("winner leaked into public family")
    public_state["board"] = list(
        canonical_board_under_suit_isomorphism(public_state["board"])
    )

    payload: dict[str, Any] = {
        "board_isomorphism": BOARD_ISOMORPHISM_SCHEMA,
        "common_contract_version": CONTRACT_VERSION,
        "legal_action_support": _legal_support(state),
        "public_state": public_state,
        "schema": PUBLIC_FAMILY_SCHEMA,
    }
    return validate_public_family_payload(payload)


def validate_public_family_payload(payload: object) -> dict[str, Any]:
    """Reconstruct Common semantics and validate a serialized family entry."""

    if type(payload) is not dict or set(payload) != {
        "board_isomorphism",
        "common_contract_version",
        "legal_action_support",
        "public_state",
        "schema",
    }:
        raise ValueError("public family fields differ")
    if (
        payload["schema"] != PUBLIC_FAMILY_SCHEMA
        or payload["board_isomorphism"] != BOARD_ISOMORPHISM_SCHEMA
        or payload["common_contract_version"] != CONTRACT_VERSION
    ):
        raise ValueError("public family schema/contract differs")
    public_state = _validate_public_state_types(payload["public_state"])
    board = public_state.get("board")
    if type(board) is not list or tuple(board) != canonical_board_under_suit_isomorphism(board):
        raise ValueError("public family board is not the canonical suit representative")
    support = _validate_legal_support_types(payload["legal_action_support"])
    _assert_public_only(payload)

    replay_payload = dict(public_state)
    replay_payload.update(
        {
            "hand_number": 1,
            "hole_cards": [[], []],
            "match_net_before": [0, 0],
            "terminal_reason": None,
            "winner": None,
        }
    )
    try:
        replayed = NationalGameState.from_dict(replay_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("public family is not a replay-valid Common state") from exc
    if replayed.is_terminal:
        raise ValueError("terminal/outcome state cannot become a public family")
    roundtrip = replayed.hand_public_dict()
    roundtrip.pop("terminal_reason")
    roundtrip.pop("winner")
    if _canonical_bytes(roundtrip) != _canonical_bytes(public_state):
        raise ValueError("public family differs after Common replay round-trip")
    if _canonical_bytes(support) != _canonical_bytes(_legal_support(replayed)):
        raise ValueError("public family legal support differs from Common")

    normalized_payload = {
        "board_isomorphism": payload["board_isomorphism"],
        "common_contract_version": payload["common_contract_version"],
        "legal_action_support": support,
        "public_state": public_state,
        "schema": payload["schema"],
    }
    canonical = json.loads(_canonical_bytes(normalized_payload))
    if _canonical_bytes(canonical) != _canonical_bytes(payload):
        raise ValueError("public family contains a non-canonical container/value")
    return canonical


def public_family_payload_id(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(validate_public_family_payload(payload))).hexdigest()


def public_family_id(state: NationalGameState) -> str:
    return public_family_payload_id(public_family_payload(state))
