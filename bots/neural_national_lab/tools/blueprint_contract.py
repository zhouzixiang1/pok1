from __future__ import annotations

from typing import Any

from feature_spec import LABELS


CONTRACT_VERSION = "blueprint_policy_v1"
RAISE_RATIOS = {
    2: 0.50,
    3: 1.00,
    4: 2.00,
}


def _f(mapping: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(mapping.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def state_from_request(req: dict[str, Any], display: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the small betting-state contract shared by tools and runtime.

    The full strategy state is reconstructed inside each bot. The blueprint
    tools only need stable action legality inputs, so this function derives the
    same compact shape from either national-adapter request fields or local
    battle display fields.
    """
    display = display or {}
    my_id = int(req.get("my_id", 0) or 0)
    round_player_bet = display.get("round_player_bet") or []
    if len(round_player_bet) >= 2:
        my_round_bet = float(round_player_bet[my_id])
        opponent_round_bet = float(round_player_bet[1 - my_id])
    else:
        my_round_bet = _f(req, "my_stage_bet")
        opponent_round_bet = _f(req, "opponent_stage_bet")

    round_bet = _f(display, "round_bet", max(my_round_bet, opponent_round_bet))
    round_raise = _f(display, "round_raise", req.get("round_raise", 100))
    to_call = _f(req, "to_call", max(0.0, opponent_round_bet - my_round_bet))
    min_raise_action = _f(
        req,
        "min_raise_action",
        max(0.0, 2.0 * round_raise + 1.0 - my_round_bet),
    )
    return {
        "pot": _f(req, "pot", _f(display, "pot", 150)),
        "to_call": to_call,
        "my_round_bet": my_round_bet,
        "opponent_round_bet": opponent_round_bet,
        "round_bet": round_bet,
        "round_raise": round_raise,
        "min_raise_action": min_raise_action,
        "opponent_allin": bool(req.get("opponent_allin")),
    }


def raise_delta_for_label(label: int, state: dict[str, Any], my_chips: int) -> int:
    to_call = _f(state, "to_call")
    pot = max(1.0, _f(state, "pot", 150.0))
    min_raise = max(1.0, _f(state, "min_raise_action", _f(state, "round_raise", 100.0)))
    ratio = RAISE_RATIOS.get(label, 1.0)
    delta = max(min_raise, int(to_call + (pot + to_call) * ratio))
    if delta >= int(my_chips):
        return -2
    return 0 if delta <= to_call else int(delta)


def legal_mask(req: dict[str, Any], state: dict[str, Any] | None = None) -> list[int]:
    if state is None:
        state = state_from_request(req)
    my_chips = int(req.get("my_chips", 0) or 0)
    to_call = _f(state, "to_call")
    min_raise = _f(state, "min_raise_action", _f(state, "round_raise", 100.0))
    opponent_allin = bool(state.get("opponent_allin"))
    can_continue = my_chips > 0
    can_raise = can_continue and not opponent_allin and my_chips > max(to_call, 0.0) + max(min_raise, 1.0)
    mask = [0] * len(LABELS)
    mask[0] = 1  # fold
    mask[1] = 1 if can_continue else 0  # check/call
    for label in RAISE_RATIOS:
        mask[label] = 1 if can_raise else 0
    mask[5] = 1 if can_continue and not opponent_allin else 0
    if not any(mask):
        mask[0] = 1
    return mask


def action_from_label(label: int, req: dict[str, Any], state: dict[str, Any]) -> int:
    if label == 0:
        return -1
    if label == 1:
        return 0
    if label == 5:
        return -2
    if label in RAISE_RATIOS:
        return raise_delta_for_label(label, state, int(req.get("my_chips", 0) or 0))
    return 0


def label_from_action(action: int) -> int:
    if action == -1:
        return 0
    if action == -2:
        return 5
    if action == 0:
        return 1
    return 3
