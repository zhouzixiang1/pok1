"""Versioned supervision contract for immediate opponent responses."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import importlib.util
import math
from pathlib import Path
from typing import Any


OPPONENT_RESPONSE_SCHEMA = "national_opponent_response_v2"
OPPONENT_ACTION_LABELS = ("fold", "check", "call", "raise", "allin")
AGGRESSIVE_SIZE_SCHEMA = "increment_over_pre_response_pot_log200_v1"
INITIAL_CHIPS = 20_000.0
MAX_AGGRESSIVE_POT_RATIO = 200.0
_STAGE_ROUND = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}


def _load_validator():
    root = Path(__file__).resolve().parents[3]
    path = root / "sever" / "engine" / "validator.py"
    spec = importlib.util.spec_from_file_location("national_response_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load national validator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_VALIDATOR = _load_validator()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _integer(value: Any, default: int = 0) -> int:
    return int(round(_number(value, float(default))))


def _indexed_number(value: Any, index: int, default: float = 0.0) -> float:
    if isinstance(value, Mapping):
        if index in value:
            return _number(value[index], default)
        return _number(value.get(str(index)), default)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if 0 <= index < len(value):
            return _number(value[index], default)
    return float(default)


def _stage(row: Mapping[str, Any], request: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    value = str(row.get("stage", ""))
    if value in _STAGE_ROUND:
        return value
    round_index = _integer(state.get("round"), -1)
    for name, index in _STAGE_ROUND.items():
        if index == round_index:
            return name
    board = request.get("public_cards")
    size = len(board) if isinstance(board, Sequence) else 0
    return "river" if size >= 5 else "turn" if size == 4 else "flop" if size >= 3 else "preflop"


def _current_actions(
    request: Mapping[str, Any], round_index: int
) -> tuple[list[tuple[str, int | None]], list[Mapping[str, Any]]]:
    actions: list[tuple[str, int | None]] = []
    records: list[Mapping[str, Any]] = []
    history = request.get("history")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        return actions, records
    for raw in history:
        if not isinstance(raw, Mapping) or _integer(raw.get("round"), -1) != round_index:
            continue
        action = str(raw.get("action_type", ""))
        if action not in OPPONENT_ACTION_LABELS:
            continue
        amount = None
        if action == "raise":
            amount = _integer(raw.get("stage_bet", raw.get("action")), 0)
            if amount <= 0:
                continue
        actions.append((action, amount))
        records.append(raw)
    return actions, records


def _hero_action_type(
    row: Mapping[str, Any],
    request: Mapping[str, Any],
    state: Mapping[str, Any],
    actions_before: Sequence[tuple[str, int | None]],
    stage: str,
) -> str:
    explicit = str(row.get("hero_action_type", ""))
    if explicit in OPPONENT_ACTION_LABELS:
        return explicit
    action = _integer(row.get("hero_action", row.get("final_action")), 0)
    if action == -1:
        return "fold"
    if action == -2:
        return "allin"
    if action > 0:
        return "raise"
    to_call = max(0.0, _number(state.get("to_call", request.get("to_call")), 0.0))
    if to_call > 0.0:
        return "call"
    if stage != "preflop" and actions_before and actions_before[-1][0] == "check":
        return "call"
    return "check"


def _minimum_raise_total(game_state: Mapping[str, Any]) -> int:
    last_raise = None
    for action, amount in reversed(list(game_state["actions"])):
        if action == "raise" and amount is not None:
            last_raise = int(amount)
            break
    if last_raise is not None:
        minimum = last_raise * int(_VALIDATOR.RAISE_MULTIPLIER) + 1
    elif game_state["stage"] == "preflop":
        minimum = int(_VALIDATOR.MIN_RAISE_PREFLOP)
    else:
        minimum = int(_VALIDATOR.MIN_RAISE_POSTFLOP)
    return max(
        minimum,
        int(game_state["player_bet"]) + 1,
        int(game_state["opponent_bet"]) + 1,
    )


def response_context(row: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct the public state immediately before the opponent response."""
    request = _mapping(row.get("request"))
    state = _mapping(row.get("state"))
    stage = _stage(row, request, state)
    round_index = _STAGE_ROUND[stage]
    actions_before, records = _current_actions(request, round_index)

    hero_id = _integer(request.get("my_id"), 0)
    if hero_id not in (0, 1):
        hero_id = 0
    opponent_id = 1 - hero_id
    dealer_id = _integer(request.get("dealer_id"), 0)
    if dealer_id not in (0, 1):
        dealer_id = 0

    hero_stack = max(
        0.0,
        _number(
            request.get("my_chips"),
            _indexed_number(state.get("stacks"), hero_id, INITIAL_CHIPS),
        ),
    )
    opponent_stack = max(
        0.0,
        _number(
            request.get("opponent_chips"),
            _indexed_number(state.get("stacks"), opponent_id, INITIAL_CHIPS),
        ),
    )
    hero_bet = max(
        0.0,
        _number(
            request.get("my_stage_bet"),
            state.get("my_round_bet", _indexed_number(state.get("round_contrib"), hero_id)),
        ),
    )
    opponent_bet = max(
        0.0,
        _number(
            request.get("opponent_stage_bet"),
            _indexed_number(state.get("round_contrib"), opponent_id),
        ),
    )
    pot_before_hero = max(1.0, _number(state.get("pot", request.get("pot")), 150.0))
    hero_action = _integer(row.get("hero_action", row.get("final_action")), 0)
    hero_action_type = _hero_action_type(
        row, request, state, actions_before, stage
    )

    if hero_action_type == "call":
        hero_commit = min(max(0.0, opponent_bet - hero_bet), hero_stack)
        hero_bet_after = hero_bet + hero_commit
    elif hero_action_type == "raise":
        hero_bet_after = max(hero_bet, float(hero_action))
        hero_commit = min(max(0.0, hero_bet_after - hero_bet), hero_stack)
        hero_bet_after = hero_bet + hero_commit
    elif hero_action_type == "allin":
        hero_commit = hero_stack
        hero_bet_after = hero_bet + hero_commit
    else:
        hero_commit = 0.0
        hero_bet_after = hero_bet
    hero_stack_after = max(0.0, hero_stack - hero_commit)

    action_amount = int(hero_bet_after) if hero_action_type == "raise" else None
    if hero_action_type == "allin":
        action_amount = int(hero_commit)
    actions_after = [*actions_before, (hero_action_type, action_amount)]
    opponent_action_count = sum(
        1 for record in records if _integer(record.get("player_id"), -1) == opponent_id
    )
    hero_action_count = sum(
        1 for record in records if _integer(record.get("player_id"), -1) == hero_id
    )
    allin_before = any(action == "allin" for action, _ in actions_before)
    allin_after = allin_before or hero_action_type == "allin" or (
        hero_action_type == "call" and hero_stack_after <= 0.0
    )

    response_expected = True
    response_reason = "awaiting_opponent_action"
    if hero_action_type == "fold":
        response_expected = False
        response_reason = "hero_fold_settled"
    elif hero_action_type == "call":
        if allin_after:
            response_expected = False
            response_reason = "allin_call_runout"
        elif opponent_action_count > 0:
            response_expected = False
            response_reason = "call_closed_street"
    elif hero_action_type == "check":
        hero_is_big_blind = hero_id != dealer_id
        if (
            stage == "preflop"
            and hero_is_big_blind
            and hero_action_count == 0
            and actions_before
            and actions_before[-1][0] == "call"
        ):
            response_expected = False
            response_reason = "big_blind_check_closed_preflop"

    game_state = {
        "stage": stage,
        "actions": actions_after,
        "player_chips": int(opponent_stack),
        "player_bet": int(opponent_bet),
        "opponent_bet": int(hero_bet_after),
        "is_small_blind": opponent_id == dealer_id,
        "is_big_blind": opponent_id != dealer_id,
        "allin_occurred": bool(allin_after),
        "player_action_count": int(opponent_action_count),
    }
    minimum_raise = _minimum_raise_total(game_state)
    legal_mask = []
    for action in OPPONENT_ACTION_LABELS:
        amount = minimum_raise if action == "raise" else None
        legal, _ = _VALIDATOR.validate_action(action, amount, game_state)
        if action in {"check", "call", "raise", "allin"} and opponent_stack <= 0.0:
            legal = False
        legal_mask.append(1 if response_expected and legal else 0)

    return {
        "stage": stage,
        "round": round_index,
        "hero_id": hero_id,
        "opponent_id": opponent_id,
        "hero_action_type": hero_action_type,
        "hero_commit": int(hero_commit),
        "hero_stage_bet_after": int(hero_bet_after),
        "hero_stack_after": int(hero_stack_after),
        "opponent_stage_bet_before": int(opponent_bet),
        "opponent_stack_before": int(opponent_stack),
        "pot_before_response": int(pot_before_hero + hero_commit),
        "opponent_to_call": max(0, int(hero_bet_after - opponent_bet)),
        "minimum_raise_to_total": int(minimum_raise),
        "response_expected": bool(response_expected),
        "response_reason": response_reason,
        "legal_action_mask": legal_mask,
        "game_state": game_state,
    }


def annotate_response_row(
    row: Mapping[str, Any], *, strict: bool = True
) -> dict[str, Any]:
    """Attach legality, eligibility, and unambiguous sizing supervision."""
    result = dict(row)
    context = response_context(row)
    action = str(row.get("opponent_action", ""))
    observed = action in OPPONENT_ACTION_LABELS
    expected = bool(context["response_expected"])
    if observed and not expected:
        if strict:
            raise ValueError(
                f"observed opponent action {action!r} after {context['response_reason']}"
            )
    elif expected and not observed and strict:
        raise ValueError("missing opponent action while a response was required")

    target_id = OPPONENT_ACTION_LABELS.index(action) if observed else None
    if observed:
        raw_amount = row.get("opponent_action_amount")
        action_amount = _integer(raw_amount, 0) if action == "raise" else None
        legal, reason = _VALIDATOR.validate_action(
            action, action_amount, context["game_state"]
        )
        if not legal or not context["legal_action_mask"][target_id]:
            if strict:
                raise ValueError(f"observed opponent action is illegal: {reason or action}")
            observed = False

    aggressive = observed and action in {"raise", "allin"}
    wire_amount = max(0.0, _number(row.get("opponent_action_amount"), 0.0))
    if action == "raise":
        raise_to_total = wire_amount
        increment = max(0.0, raise_to_total - context["opponent_stage_bet_before"])
    elif action == "allin":
        increment = wire_amount or float(context["opponent_stack_before"])
        raise_to_total = context["opponent_stage_bet_before"] + increment
    else:
        raise_to_total = 0.0
        increment = 0.0
    pot_ratio = increment / max(1.0, float(context["pot_before_response"]))
    stack_fraction = increment / max(1.0, float(context["opponent_stack_before"]))
    amount_target = min(
        1.0,
        math.log1p(max(0.0, pot_ratio)) / math.log1p(MAX_AGGRESSIVE_POT_RATIO),
    )

    if "opponent_action_amount_norm" in result:
        result["legacy_opponent_action_amount_norm"] = result["opponent_action_amount_norm"]
    if "opponent_action_pot_ratio" in result:
        result["legacy_opponent_action_pot_ratio"] = result["opponent_action_pot_ratio"]
    result.update({
        "response_schema": OPPONENT_RESPONSE_SCHEMA,
        "response_action_labels": list(OPPONENT_ACTION_LABELS),
        "response_eligible": expected,
        "response_observed": bool(observed),
        "response_target_mask": 1 if expected and observed else 0,
        "response_outcome": (
            "observed_action" if observed else context["response_reason"]
            if not expected else "missing_response"
        ),
        "response_legal_action_mask": list(context["legal_action_mask"]),
        "response_legal_actions": [
            label
            for label, legal in zip(OPPONENT_ACTION_LABELS, context["legal_action_mask"])
            if legal
        ],
        "response_context": {
            key: value for key, value in context.items() if key != "game_state"
        },
        "response_amount_schema": AGGRESSIVE_SIZE_SCHEMA,
        "response_amount_target_mask": 1 if aggressive else 0,
        "response_amount_target": amount_target if aggressive else 0.0,
        "response_raise_to_total": int(raise_to_total) if aggressive else 0,
        "response_aggressive_increment": int(increment) if aggressive else 0,
        "response_aggressive_increment_pot_ratio": pot_ratio if aggressive else 0.0,
        "response_aggressive_stack_fraction": min(1.0, stack_fraction) if aggressive else 0.0,
        "opponent_action_amount_norm": min(1.0, increment / INITIAL_CHIPS) if aggressive else 0.0,
        "opponent_action_pot_ratio": pot_ratio if aggressive else 0.0,
    })
    if target_id is not None:
        result["opponent_action_label_id"] = target_id
    return result


def annotate_response_rows(
    rows: Sequence[Mapping[str, Any]], *, strict: bool = True
) -> list[dict[str, Any]]:
    return [annotate_response_row(row, strict=strict) for row in rows]


def _response_key(row: Mapping[str, Any]) -> tuple[int, str, int, int]:
    request = _mapping(row.get("request"))
    state = _mapping(row.get("state"))
    return (
        _integer(row.get("hand"), 0),
        _stage(row, request, state),
        _integer(row.get("hand_decision_index"), 0),
        _integer(row.get("decision_serial"), 0),
    )


def summarize_response_population(
    decisions: Sequence[Mapping[str, Any]],
    observed_rows: Sequence[Mapping[str, Any]],
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Account for observed responses and decisions that close action."""
    observed_by_key: dict[tuple[int, str, int, int], Mapping[str, Any]] = {}
    for row in observed_rows:
        key = _response_key(row)
        if key in observed_by_key:
            raise ValueError(f"duplicate observed response key: {key}")
        observed_by_key[key] = row

    expected = observed = not_expected = missing = 0
    reasons: Counter[str] = Counter()
    consumed: set[tuple[int, str, int, int]] = set()
    for decision in decisions:
        decision_row = {
            "hand": decision.get("hand"),
            "stage": decision.get("stage"),
            "hand_decision_index": decision.get("hand_decision_index"),
            "decision_serial": decision.get("decision_serial"),
            "hero_action": decision.get("final_action"),
            "request": decision.get("request"),
            "state": decision.get("state"),
        }
        key = _response_key(decision_row)
        context = response_context(decision_row)
        if context["response_expected"]:
            expected += 1
            row = observed_by_key.get(key)
            if row is None:
                missing += 1
                reasons["missing_required_response"] += 1
                continue
            if row.get("response_schema") != OPPONENT_RESPONSE_SCHEMA:
                row = annotate_response_row(row, strict=strict)
            if not row.get("response_observed"):
                missing += 1
                reasons["unobserved_required_response"] += 1
                continue
            observed += 1
            consumed.add(key)
        else:
            not_expected += 1
            reasons[str(context["response_reason"])] += 1
            if key in observed_by_key:
                reasons["unexpected_observed_response"] += 1

    unused = sorted(set(observed_by_key) - consumed)
    if strict and (missing or unused or reasons["unexpected_observed_response"]):
        raise ValueError(
            "opponent response population mismatch: "
            f"missing={missing} unused={len(unused)} "
            f"unexpected={reasons['unexpected_observed_response']}"
        )
    return {
        "schema": OPPONENT_RESPONSE_SCHEMA,
        "decisions": len(decisions),
        "response_expected": expected,
        "response_observed": observed,
        "response_not_expected": not_expected,
        "missing_required_response": missing,
        "unused_observed_rows": len(unused),
        "not_expected_by_reason": dict(sorted(reasons.items())),
    }


def response_schema_metadata() -> dict[str, Any]:
    return {
        "schema": OPPONENT_RESPONSE_SCHEMA,
        "action_labels": list(OPPONENT_ACTION_LABELS),
        "action_dim": len(OPPONENT_ACTION_LABELS),
        "legal_mask_field": "response_legal_action_mask",
        "target_mask_field": "response_target_mask",
        "amount_schema": AGGRESSIVE_SIZE_SCHEMA,
        "amount_target_field": "response_amount_target",
        "amount_target_mask_field": "response_amount_target_mask",
        "raise_wire_semantics": "raise_to_stage_total",
        "allin_wire_semantics": "remaining_stack_increment",
        "public_only": True,
    }
