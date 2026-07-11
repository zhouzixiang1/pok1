"""Stdlib-only v3 policy wrapper copied into an authorized native candidate."""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from cross_hand_sequence import normalize_cross_hand_sequence
from feature_spec import LABELS, encode_features, label_action
from model_input_schema import encode_model_input
from opponent_multitask_ensemble_runtime_v3 import (
    OpponentMultiTaskEnsembleRuntimeV3,
)
from opponent_profile_schema import encode_opponent_profile
from opponent_response_schema import response_context
from strategy_context_schema import (
    STRATEGY_CONTEXT_DIM,
    STRATEGY_CONTEXT_SCHEMA,
    encode_strategy_context,
)


BUNDLE_FILENAME = "v3_ensemble_bundle.json"
RAISE_RATIOS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
INITIAL_CHIPS = 20_000.0
MAX_CURRENT_HAND_HISTORY = 16


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def sanitize_stage_total(
    action: Any,
    state: dict[str, Any],
    my_chips: int,
    *,
    fallback: int = 0,
) -> int:
    """Sanitize an already raise-to-total action without adding the current bet."""
    try:
        value = int(action)
        chips = max(0, int(my_chips))
        to_call = max(0, _integer(state.get("to_call")))
        my_bet = max(0, _integer(state.get("my_round_bet")))
        round_bet = max(my_bet, _integer(state.get("round_bet")))
        min_raise = max(
            1,
            _integer(
                state.get("min_raise_action", state.get("round_raise", 100)),
                100,
            ),
        )
        if state.get("opponent_allin"):
            return -1 if value == -1 else 0
        if to_call >= chips:
            return -1 if value == -1 else -2
        if value == -1:
            return -1
        if value == -2:
            return -2
        if value <= 0:
            return 0
        needed = value - my_bet
        if needed >= chips:
            return -2
        if needed <= to_call or needed < min_raise or value <= round_bet:
            return 0
        return value
    except Exception:
        try:
            safe_fallback = int(fallback)
        except (TypeError, ValueError, OverflowError):
            safe_fallback = 0
        return safe_fallback if safe_fallback in (-2, -1, 0) else 0


def _raise_total(
    request: dict[str, Any], state: dict[str, Any], ratio: float
) -> int | None:
    my_bet = max(
        0, _integer(state.get("my_round_bet", request.get("my_stage_bet", 0)))
    )
    to_call = max(0, _integer(state.get("to_call", request.get("to_call", 0))))
    pot = max(1, _integer(state.get("pot", request.get("pot", 150)), 150))
    min_raise = max(
        1,
        _integer(
            state.get("min_raise_action", state.get("round_raise", 100)), 100
        ),
    )
    my_chips = max(0, _integer(request.get("my_chips")))
    delta = max(min_raise, int(to_call + (pot + to_call) * float(ratio)))
    if delta <= to_call or delta >= my_chips:
        return None
    return my_bet + delta


def candidate_actions(
    request: dict[str, Any], state: dict[str, Any], safe_rule_action: int
) -> list[dict[str, Any]]:
    """Reproduce the collector's one-action-per-label alternative support."""
    to_call = max(0, _integer(state.get("to_call", request.get("to_call", 0))))
    opponent_allin = bool(
        state.get("opponent_allin") or request.get("opponent_allin")
    )
    actions: list[int] = []
    if to_call > 0 and safe_rule_action != -1:
        actions.append(-1)
    if safe_rule_action != 0:
        actions.append(0)
    if not opponent_allin:
        for ratio in RAISE_RATIOS:
            action = _raise_total(request, state, ratio)
            if action is not None and action != safe_rule_action:
                actions.append(action)
        if safe_rule_action != -2:
            actions.append(-2)

    rule_label = int(label_action(safe_rule_action, request, None))
    by_label: dict[int, int] = {}
    for raw_action in actions:
        action = sanitize_stage_total(
            raw_action,
            state,
            _integer(request.get("my_chips")),
            fallback=safe_rule_action,
        )
        label_id = int(label_action(action, request, None))
        if action == safe_rule_action or label_id == rule_label:
            continue
        by_label.setdefault(label_id, action)
    return [
        {"label_id": label_id, "action": by_label[label_id]}
        for label_id in sorted(by_label)
    ]


def _model_context(
    request: dict[str, Any],
    state: dict[str, Any],
    legal_mask: list[int],
    *,
    response: bool,
    disable_cross_hand: bool,
) -> dict[str, Any]:
    row = {
        "request": request,
        "state": state,
        "legal_mask": legal_mask,
    }
    encoded = encode_model_input(
        row,
        encode_features(request, None),
        max_hist=MAX_CURRENT_HAND_HISTORY,
        response=response,
    )
    cross = [] if disable_cross_hand else normalize_cross_hand_sequence(
        request.get("cross_hand_sequence")
    )
    return {
        "state": encoded["state"],
        "profile": encode_opponent_profile(row),
        "history": encoded["history"],
        "cross_sequence": cross,
    }


def _hero_action_features(
    label_id: int, context: dict[str, Any]
) -> list[float]:
    one_hot = [1.0 if index == label_id else 0.0 for index in range(len(LABELS))]
    commit = max(0.0, _finite(context.get("hero_commit")))
    pot_before_response = max(1.0, _finite(context.get("pot_before_response"), 1.0))
    pot_before_hero = max(1.0, pot_before_response - commit)
    opponent_to_call = max(0.0, _finite(context.get("opponent_to_call")))
    hero_stack_after = max(0.0, _finite(context.get("hero_stack_after")))
    return one_hot + [
        min(1.0, commit / INITIAL_CHIPS),
        min(1.0, commit / pot_before_hero / 4.0),
        min(1.0, opponent_to_call / INITIAL_CHIPS),
        min(1.0, hero_stack_after / INITIAL_CHIPS),
    ]


def _strategy_features(context: dict[str, Any] | None) -> list[float]:
    if not isinstance(context, dict) or not context:
        return [0.0] * STRATEGY_CONTEXT_DIM
    raw_features = context.get("features")
    if raw_features is not None:
        if (
            context.get("schema") != STRATEGY_CONTEXT_SCHEMA
            or not isinstance(raw_features, list)
            or len(raw_features) != STRATEGY_CONTEXT_DIM
        ):
            raise ValueError("captured strategy context has the wrong contract")
        features = [_finite(value, math.nan) for value in raw_features]
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in features):
            raise ValueError("captured strategy context is not bounded")
        return features
    return encode_strategy_context(context)


class NativeV3Policy:
    def __init__(self, runtime: OpponentMultiTaskEnsembleRuntimeV3) -> None:
        if runtime.policy is None:
            raise ValueError("v3 native policy requires a selected policy")
        self.runtime = runtime
        self.disable_cross_hand = _env_bool("POK_V3_DISABLE_CROSS_HAND")
        self.disable_risk_match = _env_bool("POK_V3_DISABLE_RISK_MATCH")
        self.last_decision: dict[str, Any] | None = None

    @classmethod
    def load(cls, path: str | Path) -> "NativeV3Policy | None":
        if _env_bool("POK_V3_DISABLE"):
            return None
        runtime = OpponentMultiTaskEnsembleRuntimeV3.load(path)
        if runtime is None or runtime.policy is None:
            return None
        try:
            return cls(runtime)
        except Exception:
            return None

    def _response_signal(
        self,
        request: dict[str, Any],
        state: dict[str, Any],
        legal_mask: list[int],
        candidate: dict[str, Any],
    ) -> float:
        row = {
            "request": request,
            "state": state,
            "legal_mask": legal_mask,
            "stage": {0: "preflop", 1: "flop", 2: "turn", 3: "river"}.get(
                _integer(state.get("round")), "preflop"
            ),
            "hero_action": int(candidate["action"]),
            "hero_action_label_id": int(candidate["label_id"]),
        }
        context = response_context(row)
        response_mask = list(context["legal_action_mask"])
        if context.get("response_expected") is not True or not any(response_mask):
            return 0.0
        inputs = _model_context(
            request,
            state,
            legal_mask,
            response=True,
            disable_cross_hand=self.disable_cross_hand,
        )
        response = self.runtime.predict_response(
            **inputs,
            hero_action=_hero_action_features(int(candidate["label_id"]), context),
            legal_action_mask=response_mask,
        )
        return self.runtime.response_signal(
            response,
            action=int(candidate["action"]),
            pot=max(1.0, _finite(state.get("pot", request.get("pot", 150)), 150)),
            hero_stage_bet=max(
                0.0,
                _finite(state.get("my_round_bet", request.get("my_stage_bet", 0))),
            ),
            hero_stack=max(0.0, _finite(request.get("my_chips"))),
            opponent_stack=max(0.0, _finite(request.get("opponent_chips"))),
        )

    def advise(
        self,
        request: dict[str, Any],
        state: dict[str, Any],
        safe_rule_action: int,
        strategy_context: dict[str, Any] | None,
    ) -> int:
        safe_rule = sanitize_stage_total(
            safe_rule_action,
            state,
            _integer(request.get("my_chips")),
            fallback=0,
        )
        self.last_decision = {"used": False, "rule_action": safe_rule}
        try:
            alternatives = candidate_actions(request, state, safe_rule)
            if not alternatives:
                return safe_rule
            rule_label = int(label_action(safe_rule, request, None))
            legal_mask = [0] * len(LABELS)
            legal_mask[rule_label] = 1
            for candidate in alternatives:
                legal_mask[int(candidate["label_id"])] = 1
            inputs = _model_context(
                request,
                state,
                legal_mask,
                response=False,
                disable_cross_hand=self.disable_cross_hand,
            )
            values = self.runtime.predict_values(
                **inputs,
                rule_action=[
                    1.0 if index == rule_label else 0.0
                    for index in range(len(LABELS))
                ],
                strategy_context=_strategy_features(strategy_context),
            )
            if self.disable_risk_match:
                for field in ("tail_delta_vs_rule", "match_delta_vs_rule"):
                    values[field]["lower"] = [0.0] * len(LABELS)
                values["delta_vs_rule"]["lower"] = list(
                    values["delta_vs_rule"]["mean"]
                )
            for candidate in alternatives:
                candidate["response_signal"] = self._response_signal(
                    request, state, legal_mask, candidate
                )
            selected = self.runtime.select_candidate(values, alternatives)
            if selected is None:
                return safe_rule
            final = sanitize_stage_total(
                selected["action"],
                state,
                _integer(request.get("my_chips")),
                fallback=safe_rule,
            )
            self.last_decision = {
                "used": final != safe_rule,
                "rule_action": safe_rule,
                "final_action": final,
                "label": LABELS[int(selected["label_id"])],
                "prediction": selected.get("prediction"),
                "response_signal": selected.get("response_signal", 0.0),
                "disable_cross_hand": self.disable_cross_hand,
                "disable_risk_match": self.disable_risk_match,
            }
            return final
        except Exception as exc:
            self.last_decision = {
                "used": False,
                "rule_action": safe_rule,
                "error": f"{type(exc).__name__}: {exc}",
            }
            return safe_rule


def load_native_v3_policy(bot_dir: str | Path) -> NativeV3Policy | None:
    return NativeV3Policy.load(Path(bot_dir) / BUNDLE_FILENAME)
