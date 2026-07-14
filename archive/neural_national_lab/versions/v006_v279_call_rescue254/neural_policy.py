from __future__ import annotations

import json
import math
import os
import sys
from typing import Any

from neural_features import LABELS, encode_features


BOT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = {
    "enabled": True,
    "weights": "policy_weights.json",
    "fold_conf": 0.82,
    "call_conf": 0.88,
    "raise_conf": 0.92,
    "allin_conf": 0.985,
    "max_call_ratio": 0.30,
    "max_fold_proxy": 0.58,
    "max_call_chips": 450,
    "max_call_pot_ratio": 0.18,
    "allow_fold": True,
    "allow_call": True,
    "allow_raise": True,
    "allow_allin": True,
}
_CONFIG: dict[str, Any] | None = None
_MODEL: dict[str, Any] | None = None


def _config() -> dict[str, Any]:
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(BOT_DIR, "neural_config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception as exc:
            print(f"NEURAL_CONFIG_ERROR {exc}", file=sys.stderr)
    _CONFIG = cfg
    return cfg


def _model() -> dict[str, Any] | None:
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    cfg = _config()
    if not cfg.get("enabled", True):
        return None
    path = str(cfg.get("weights", "policy_weights.json"))
    if not os.path.isabs(path):
        path = os.path.join(BOT_DIR, path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            model = json.load(fh)
        if model.get("labels") != list(LABELS):
            raise ValueError("label mismatch")
        _MODEL = model
        return model
    except Exception as exc:
        print(f"NEURAL_MODEL_ERROR {exc}", file=sys.stderr)
        return None


def _dot(row: list[float], vec: list[float]) -> float:
    return sum(a * b for a, b in zip(row, vec))


def _predict(model: dict[str, Any], features: list[float]) -> list[float]:
    hidden = [max(0.0, _dot(row, features) + float(b)) for row, b in zip(model["w1"], model["b1"])]
    logits = [_dot(row, hidden) + float(b) for row, b in zip(model["w2"], model["b2"])]
    top = max(logits)
    exps = [math.exp(max(-30.0, min(30.0, x - top))) for x in logits]
    denom = sum(exps) or 1.0
    return [x / denom for x in exps]


def _f(state: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(state.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _rule_label(action: int) -> int:
    if action == -1:
        return 0
    if action == -2:
        return 5
    if action == 0:
        return 1
    return 3


def _raise_action(label: int, state: dict[str, Any], my_chips: int) -> int:
    to_call = _f(state, "to_call")
    pot = max(1.0, _f(state, "pot", 150.0))
    ratio = 0.50 if label == 2 else 1.00 if label == 3 else 1.75
    min_raise = int(state.get("min_raise_action", state.get("round_raise", 100)) or 100)
    amount = max(min_raise, int(to_call + (pot + to_call) * ratio))
    if amount >= my_chips:
        return -2
    return 0 if amount <= to_call else int(amount)


def apply_neural_advice(req: dict[str, Any], state: dict[str, Any], rule_action: int) -> int:
    model = _model()
    cfg = _config()
    if model is None:
        return rule_action
    feature_req = dict(req)
    for key in ("pot", "to_call", "my_stage_bet", "opponent_stage_bet", "opponent_allin"):
        if key in state:
            feature_req[key] = state[key]
    probs = _predict(model, encode_features(feature_req, None))
    label = max(range(len(probs)), key=lambda i: probs[i])
    conf = probs[label]
    if label == _rule_label(rule_action):
        return rule_action
    to_call = _f(state, "to_call")
    pot = max(1.0, _f(state, "pot", 150.0))
    my_chips = int(req.get("my_chips", 0) or 0)
    if label == 0:
        if (
            cfg.get("allow_fold", True)
            and to_call > 0
            and conf >= float(cfg["fold_conf"])
            and 1.0 - probs[0] <= float(cfg["max_fold_proxy"])
        ):
            return -1
        return rule_action
    if label == 1:
        call_ratio = to_call / max(1.0, pot + to_call)
        cheap_call = to_call <= float(cfg.get("max_call_chips", 450))
        small_pot = pot / 20000.0 <= float(cfg.get("max_call_pot_ratio", 0.18))
        if (
            cfg.get("allow_call", True)
            and rule_action == -1
            and conf >= float(cfg["call_conf"])
            and call_ratio <= float(cfg["max_call_ratio"])
            and cheap_call
            and small_pot
        ):
            return 0
        return rule_action
    if label in (2, 3, 4):
        if cfg.get("allow_raise", True) and not state.get("opponent_allin") and conf >= float(cfg["raise_conf"]):
            return _raise_action(label, state, my_chips)
        return rule_action
    if cfg.get("allow_allin", True) and label == 5 and conf >= float(cfg["allin_conf"]):
        return -2
    return rule_action
