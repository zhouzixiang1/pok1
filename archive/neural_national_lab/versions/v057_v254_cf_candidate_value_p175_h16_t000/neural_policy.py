from __future__ import annotations

import itertools
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
    "contract": "blueprint_policy_v1",
    "min_conf": 0.68,
    "fold_conf": 0.90,
    "call_conf": 0.78,
    "raise_conf": 0.86,
    "allin_conf": 1.01,
    "max_paid_call_chips": 650,
    "max_paid_call_ratio": 0.34,
    "max_call_pot_ratio": 0.10,
    "max_raise_delta": 1600,
    "max_raise_pot_ratio": 1.25,
    "allow_fold": True,
    "allow_call": True,
    "allow_raise": True,
    "allow_allin": False,
    "raise_stages": ["flop"],
    "raise_requires_free_action": True,
    "raise_rule_labels": ["call"],
}
RAISE_RATIOS = {2: 0.50, 3: 1.00, 4: 2.00}
_CONFIG: dict[str, Any] | None = None
_MODEL: dict[str, Any] | None = None
_ADVANTAGE_MODEL: dict[str, Any] | None = None
_INTERACTION_MODEL: dict[str, Any] | None = None
_ISOLATED_VETO_MODEL: dict[str, Any] | None = None
_VALUE_VETO_MODEL: dict[str, Any] | None = None
_VALUE_VETO_MODELS: list[dict[str, Any]] | None = None


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
        if int(model.get("input_dim", 0)) != len(encode_features({"my_cards": [0, 4], "public_cards": []}, None)):
            raise ValueError("feature dimension mismatch")
        _MODEL = model
        return model
    except Exception as exc:
        print(f"NEURAL_MODEL_ERROR {exc}", file=sys.stderr)
        return None


def _advantage_model() -> dict[str, Any] | None:
    global _ADVANTAGE_MODEL
    if _ADVANTAGE_MODEL is not None:
        return _ADVANTAGE_MODEL
    cfg = _config()
    if not cfg.get("advantage_enabled", False):
        return None
    path = str(cfg.get("advantage_weights", "advantage_weights.json"))
    if not os.path.isabs(path):
        path = os.path.join(BOT_DIR, path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            model = json.load(fh)
        if model.get("labels") != ["bad", "good"]:
            raise ValueError("advantage label mismatch")
        _ADVANTAGE_MODEL = model
        return model
    except Exception as exc:
        print(f"ADVANTAGE_MODEL_ERROR {exc}", file=sys.stderr)
        return None


def _interaction_model() -> dict[str, Any] | None:
    global _INTERACTION_MODEL
    if _INTERACTION_MODEL is not None:
        return _INTERACTION_MODEL
    cfg = _config()
    if not cfg.get("interaction_enabled", False):
        return None
    path = str(cfg.get("interaction_weights", "interaction_weights.json"))
    if not os.path.isabs(path):
        path = os.path.join(BOT_DIR, path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            model = json.load(fh)
        if model.get("labels") != ["bad", "good"]:
            raise ValueError("interaction label mismatch")
        _INTERACTION_MODEL = model
        return model
    except Exception as exc:
        print(f"INTERACTION_MODEL_ERROR {exc}", file=sys.stderr)
        return None


def _isolated_veto_model() -> dict[str, Any] | None:
    global _ISOLATED_VETO_MODEL
    if _ISOLATED_VETO_MODEL is not None:
        return _ISOLATED_VETO_MODEL
    cfg = _config()
    if not cfg.get("isolated_veto_enabled", False):
        return None
    path = str(cfg.get("isolated_veto_weights", "isolated_veto_weights.json"))
    if not os.path.isabs(path):
        path = os.path.join(BOT_DIR, path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            model = json.load(fh)
        if model.get("labels") != ["bad", "good"]:
            raise ValueError("isolated veto label mismatch")
        _ISOLATED_VETO_MODEL = model
        return model
    except Exception as exc:
        print(f"ISOLATED_VETO_MODEL_ERROR {exc}", file=sys.stderr)
        return None


def _value_veto_model() -> dict[str, Any] | None:
    global _VALUE_VETO_MODEL
    if _VALUE_VETO_MODEL is not None:
        return _VALUE_VETO_MODEL
    cfg = _config()
    if not cfg.get("value_veto_enabled", False):
        return None
    path = str(cfg.get("value_veto_weights", "value_veto_weights.json"))
    if not os.path.isabs(path):
        path = os.path.join(BOT_DIR, path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            model = json.load(fh)
        if model.get("format") != "tiny_mlp_value_gate_v1":
            raise ValueError("value veto format mismatch")
        _VALUE_VETO_MODEL = model
        return model
    except Exception as exc:
        print(f"VALUE_VETO_MODEL_ERROR {exc}", file=sys.stderr)
        return None


def _value_veto_models() -> list[dict[str, Any]]:
    global _VALUE_VETO_MODELS
    if _VALUE_VETO_MODELS is not None:
        return _VALUE_VETO_MODELS
    cfg = _config()
    raw_paths = cfg.get("value_veto_weights_list")
    if not cfg.get("value_veto_enabled", False):
        _VALUE_VETO_MODELS = []
        return _VALUE_VETO_MODELS
    if not raw_paths:
        model = _value_veto_model()
        _VALUE_VETO_MODELS = [model] if model is not None else []
        return _VALUE_VETO_MODELS
    models: list[dict[str, Any]] = []
    for raw_path in raw_paths:
        path = str(raw_path)
        if not os.path.isabs(path):
            path = os.path.join(BOT_DIR, path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                model = json.load(fh)
            if model.get("format") != "tiny_mlp_value_gate_v1":
                raise ValueError("value veto format mismatch")
            models.append(model)
        except Exception as exc:
            print(f"VALUE_VETO_ENSEMBLE_MODEL_ERROR {exc}", file=sys.stderr)
            _VALUE_VETO_MODELS = []
            return _VALUE_VETO_MODELS
    _VALUE_VETO_MODELS = models
    return _VALUE_VETO_MODELS


def _dot(row: list[float], vec: list[float]) -> float:
    return sum(a * b for a, b in zip(row, vec))


def _predict(model: dict[str, Any], features: list[float]) -> list[float]:
    hidden = [max(0.0, _dot(row, features) + float(b)) for row, b in zip(model["w1"], model["b1"])]
    logits = [_dot(row, hidden) + float(b) for row, b in zip(model["w2"], model["b2"])]
    top = max(logits)
    exps = [math.exp(max(-30.0, min(30.0, x - top))) for x in logits]
    denom = sum(exps) or 1.0
    return [x / denom for x in exps]


def _predict_advantage(model: dict[str, Any], features: list[float]) -> float:
    hidden = [max(0.0, _dot(row, features) + float(b)) for row, b in zip(model["w1"], model["b1"])]
    logits = [_dot(row, hidden) + float(b) for row, b in zip(model["w2"], model["b2"])]
    top = max(logits)
    exps = [math.exp(max(-30.0, min(30.0, x - top))) for x in logits]
    denom = sum(exps) or 1.0
    if len(exps) < 2:
        return 0.0
    return float(exps[1] / denom)


def _predict_value(model: dict[str, Any], features: list[float]) -> float:
    hidden = [max(0.0, _dot(row, features) + float(b)) for row, b in zip(model["w1"], model["b1"])]
    return _dot(model["w2"][0], hidden) + float(model["b2"][0])


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


def _stage_name(req: dict[str, Any]) -> str:
    n = len(req.get("public_cards") or [])
    if n >= 5:
        return "river"
    if n == 4:
        return "turn"
    if n >= 3:
        return "flop"
    return "preflop"


def _label_name(label: int) -> str:
    try:
        return str(LABELS[label])
    except IndexError:
        return "unknown"


def _rank(card: int) -> int:
    return int(card) // 4 + 2


def _suit(card: int) -> int:
    return int(card) % 4


def _score_5(cards: list[int]) -> tuple[int, ...]:
    ranks = sorted((_rank(card) for card in cards), reverse=True)
    suits = [_suit(card) for card in cards]
    counts: dict[int, int] = {}
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
    groups = sorted(((count, rank) for rank, count in counts.items()), reverse=True)
    unique = sorted(set(ranks), reverse=True)
    is_flush = len(set(suits)) == 1
    is_straight = False
    straight_high = 0
    if len(unique) == 5:
        if unique[0] - unique[4] == 4:
            is_straight = True
            straight_high = unique[0]
        elif set(unique) == {14, 2, 3, 4, 5}:
            is_straight = True
            straight_high = 5
    if is_flush and is_straight:
        return (8, straight_high)
    if groups[0][0] == 4:
        quad = groups[0][1]
        kicker = max(rank for rank in ranks if rank != quad)
        return (7, quad, kicker)
    if groups[0][0] == 3 and len(groups) > 1 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if is_flush:
        return (5, *ranks)
    if is_straight:
        return (4, straight_high)
    if groups[0][0] == 3:
        trips = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != trips), reverse=True)
        return (3, trips, *kickers)
    if groups[0][0] == 2 and len(groups) > 1 and groups[1][0] == 2:
        high_pair = max(groups[0][1], groups[1][1])
        low_pair = min(groups[0][1], groups[1][1])
        kicker = max(rank for rank in ranks if rank not in (high_pair, low_pair))
        return (2, high_pair, low_pair, kicker)
    if groups[0][0] == 2:
        pair = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != pair), reverse=True)
        return (1, pair, *kickers)
    return (0, *ranks)


def _best_score_and_hole_use(my_cards: list[int], public_cards: list[int]) -> tuple[tuple[int, ...], int]:
    cards = list(my_cards) + list(public_cards)
    if len(cards) < 5:
        return (0, 0), 0
    best_score: tuple[int, ...] | None = None
    best_hole_use = 0
    hole_set = set(my_cards)
    for combo in itertools.combinations(cards, 5):
        score = _score_5(list(combo))
        hole_use = sum(1 for card in combo if card in hole_set)
        if best_score is None or score > best_score or (score == best_score and hole_use > best_hole_use):
            best_score = score
            best_hole_use = hole_use
    return best_score or (0, 0), best_hole_use


def _straight_features(my_cards: list[int], public_cards: list[int]) -> tuple[float, float, float]:
    all_ranks = {_rank(card) for card in my_cards + public_cards}
    hole_ranks = {_rank(card) for card in my_cards}
    if 14 in all_ranks:
        all_ranks.add(1)
    if 14 in hole_ranks:
        hole_ranks.add(1)
    best_count = 0
    best_hole = 0
    for start in range(1, 11):
        window = set(range(start, start + 5))
        count = len(all_ranks & window)
        hole_count = len(hole_ranks & window)
        if count > best_count or (count == best_count and hole_count > best_hole):
            best_count = count
            best_hole = hole_count
    return min(1.0, max(0.0, best_count / 5.0)), 1.0 if best_count >= 4 else 0.0, min(1.0, max(0.0, best_hole / 2.0))


def _hand_strength_features(req: dict[str, Any]) -> list[float]:
    my_cards = list(req.get("my_cards") or [])
    public_cards = list(req.get("public_cards") or [])
    if len(my_cards) < 2:
        my_cards = (my_cards + [0, 4])[:2]
    ranks = [_rank(card) for card in my_cards]
    board_ranks = [_rank(card) for card in public_cards]
    board_suits = [_suit(card) for card in public_cards]
    made, hole_use = _best_score_and_hole_use(my_cards, public_cards)
    all_suits = [_suit(card) for card in my_cards + public_cards]
    suit_counts = {suit: all_suits.count(suit) for suit in range(4)}
    best_suit = max(suit_counts, key=lambda suit: suit_counts[suit])
    best_suit_count = suit_counts[best_suit]
    hole_best_suit = sum(1 for card in my_cards if _suit(card) == best_suit)
    rank_matches = sum(1 for rank in ranks if rank in set(board_ranks))
    board_rank_max = max((board_ranks.count(rank) for rank in set(board_ranks)), default=0)
    board_suit_max = max((board_suits.count(suit) for suit in set(board_suits)), default=0)
    max_board_rank = max(board_ranks, default=2)
    overcards = sum(1 for rank in ranks if rank > max_board_rank)
    straight_density, straight_draw, straight_hole = _straight_features(my_cards, public_cards)
    return [
        min(1.0, max(0.0, len(public_cards) / 5.0)),
        min(1.0, max(0.0, float(made[0]) / 8.0)),
        min(1.0, max(0.0, float(made[1] if len(made) > 1 else 0) / 14.0)),
        min(1.0, max(0.0, hole_use / 2.0)),
        min(1.0, max(0.0, rank_matches / 2.0)),
        min(1.0, max(0.0, overcards / 2.0)),
        1.0 if made[0] >= 1 else 0.0,
        1.0 if made[0] >= 2 else 0.0,
        1.0 if made[0] >= 3 else 0.0,
        min(1.0, max(0.0, best_suit_count / 5.0)),
        min(1.0, max(0.0, hole_best_suit / 2.0)),
        1.0 if best_suit_count == 4 and hole_best_suit > 0 else 0.0,
        1.0 if best_suit_count == 3 and hole_best_suit > 0 else 0.0,
        straight_density,
        straight_draw,
        straight_hole,
        min(1.0, max(0.0, board_rank_max / 3.0)),
        min(1.0, max(0.0, board_suit_max / 3.0)),
    ]


def _advantage_features(
    req: dict[str, Any],
    state: dict[str, Any],
    rule_action: int,
    label: int,
    conf: float,
    probs: list[float],
) -> list[float]:
    feature_req = dict(req)
    for key in ("pot", "to_call", "my_stage_bet", "opponent_stage_bet", "opponent_allin"):
        if key in state:
            feature_req[key] = state[key]
    top_onehot = [1.0 if i == label else 0.0 for i in range(len(LABELS))]
    rule = _rule_label(rule_action)
    rule_onehot = [1.0 if i == rule else 0.0 for i in range(len(LABELS))]
    to_call = _f(state, "to_call")
    pot = _f(state, "pot", 150.0)
    my_chips = float(req.get("my_chips", 20000) or 20000)
    stage_name = _stage_name(req)
    stage_onehot = [
        1.0 if stage_name == "preflop" else 0.0,
        1.0 if stage_name == "flop" else 0.0,
        1.0 if stage_name == "turn" else 0.0,
        1.0 if stage_name == "river" else 0.0,
    ]
    extras = [
        float(conf),
        float(probs[1]) if len(probs) > 1 else 0.0,
        min(1.0, max(0.0, to_call / 20000.0)),
        min(1.0, max(0.0, pot / 20000.0)),
        1.0 if to_call <= 0 else 0.0,
        min(1.0, max(0.0, my_chips / 20000.0)),
        *stage_onehot,
    ]
    return encode_features(feature_req, None) + top_onehot + rule_onehot + extras


def _interaction_features(
    req: dict[str, Any],
    state: dict[str, Any],
    rule_action: int,
    label: int,
    conf: float,
    probs: list[float],
) -> list[float]:
    features = _advantage_features(req, state, rule_action, label, conf, probs)
    if _config().get("interaction_extra_features") == "hand_strength_v1":
        features = features + _hand_strength_features(req)
    return features


def _passes_advantage_gate(
    req: dict[str, Any],
    state: dict[str, Any],
    rule_action: int,
    label: int,
    conf: float,
    probs: list[float],
) -> bool:
    cfg = _config()
    if not cfg.get("advantage_enabled", False):
        return True
    model = _advantage_model()
    if model is None:
        return False
    features = _advantage_features(req, state, rule_action, label, conf, probs)
    if len(features) != int(model.get("input_dim", 0)):
        return False
    score = _predict_advantage(model, features)
    return score >= float(cfg.get("advantage_min", 0.56))


def _passes_interaction_gate(
    req: dict[str, Any],
    state: dict[str, Any],
    rule_action: int,
    label: int,
    conf: float,
    probs: list[float],
) -> bool:
    cfg = _config()
    if not cfg.get("interaction_enabled", False):
        return True
    model = _interaction_model()
    if model is None:
        return False
    features = _interaction_features(req, state, rule_action, label, conf, probs)
    if len(features) != int(model.get("input_dim", 0)):
        return False
    score = _predict_advantage(model, features)
    if conf < float(cfg.get("interaction_apply_min_conf", 0.0)):
        return True
    return score >= float(cfg.get("interaction_min", 0.70))


def _passes_isolated_veto_gate(
    req: dict[str, Any],
    state: dict[str, Any],
    rule_action: int,
    label: int,
    conf: float,
    probs: list[float],
) -> bool:
    cfg = _config()
    if not cfg.get("isolated_veto_enabled", False):
        return True
    if conf < float(cfg.get("isolated_veto_min_conf", 1.01)):
        return True
    model = _isolated_veto_model()
    if model is None:
        return True
    features = _advantage_features(req, state, rule_action, label, conf, probs)
    if len(features) != int(model.get("input_dim", 0)):
        return True
    score = _predict_advantage(model, features)
    return score >= float(cfg.get("isolated_veto_block_below", 0.0))


def _passes_value_veto_gate(
    req: dict[str, Any],
    state: dict[str, Any],
    rule_action: int,
    label: int,
    conf: float,
    probs: list[float],
) -> bool:
    cfg = _config()
    if not cfg.get("value_veto_enabled", False):
        return True
    if conf < float(cfg.get("value_veto_min_conf", 1.01)):
        return True
    models = _value_veto_models()
    if not models:
        return True
    features = _advantage_features(req, state, rule_action, label, conf, probs)
    if any(len(features) != int(model.get("input_dim", 0)) for model in models):
        return True
    scores = [_predict_value(model, features) for model in models]
    mean_score = sum(scores) / max(1, len(scores))
    return (
        mean_score >= float(cfg.get("value_veto_mean_min", cfg.get("value_veto_block_below", -1.0)))
        and min(scores) >= float(cfg.get("value_veto_member_min", -999.0))
    )


def _legal_mask(req: dict[str, Any], state: dict[str, Any]) -> list[int]:
    my_chips = int(req.get("my_chips", 0) or 0)
    to_call = _f(state, "to_call")
    min_raise = _f(state, "min_raise_action", _f(state, "round_raise", 100.0))
    can_continue = my_chips > 0
    can_raise = (
        can_continue
        and not state.get("opponent_allin")
        and my_chips > max(to_call, 0.0) + max(min_raise, 1.0)
    )
    mask = [0] * len(LABELS)
    mask[0] = 1
    mask[1] = 1 if can_continue else 0
    for label in RAISE_RATIOS:
        mask[label] = 1 if can_raise else 0
    mask[5] = 1 if can_continue and not state.get("opponent_allin") else 0
    if not any(mask):
        mask[0] = 1
    return mask


def _masked_top(probs: list[float], mask: list[int]) -> tuple[int, float, list[float]]:
    masked = [float(p) if mask[i] else 0.0 for i, p in enumerate(probs)]
    total = sum(masked)
    if total <= 0:
        masked = [1.0 if i == 0 else 0.0 for i in range(len(probs))]
        total = 1.0
    norm = [p / total for p in masked]
    label = max(range(len(norm)), key=lambda i: norm[i])
    return label, float(norm[label]), norm


def _raise_action(label: int, state: dict[str, Any], my_chips: int) -> int:
    to_call = _f(state, "to_call")
    pot = max(1.0, _f(state, "pot", 150.0))
    ratio = RAISE_RATIOS.get(label, 1.0)
    min_raise = int(max(1.0, _f(state, "min_raise_action", _f(state, "round_raise", 100.0))))
    amount = max(min_raise, int(to_call + (pot + to_call) * ratio))
    if amount >= my_chips:
        return -2
    return 0 if amount <= to_call else int(amount)


def _candidate_action(label: int, req: dict[str, Any], state: dict[str, Any]) -> int:
    if label == 0:
        return -1
    if label == 1:
        return 0
    if label == 5:
        return -2
    if label in RAISE_RATIOS:
        return _raise_action(label, state, int(req.get("my_chips", 0) or 0))
    return 0


def _passes_runtime_gate(
    label: int,
    conf: float,
    probs: list[float],
    req: dict[str, Any],
    state: dict[str, Any],
    rule_action: int,
    candidate: int,
) -> bool:
    cfg = _config()
    if conf < float(cfg.get("min_conf", 0.68)):
        return False
    to_call = _f(state, "to_call")
    pot = max(1.0, _f(state, "pot", 150.0))
    if label == 0:
        return (
            cfg.get("allow_fold", True)
            and to_call > 0
            and conf >= float(cfg.get("fold_conf", 0.90))
            and _rule_label(rule_action) in {1, 2, 3, 4, 5}
        )
    if label == 1:
        call_ratio = to_call / max(1.0, pot + to_call)
        return (
            cfg.get("allow_call", True)
            and rule_action == -1
            and conf >= float(cfg.get("call_conf", 0.78))
            and to_call <= float(cfg.get("max_paid_call_chips", 650))
            and call_ratio <= float(cfg.get("max_paid_call_ratio", 0.34))
            and pot / 20000.0 <= float(cfg.get("max_call_pot_ratio", 0.10))
        )
    if label in RAISE_RATIOS:
        raise_delta = max(0, int(candidate))
        if _stage_name(req) not in set(cfg.get("raise_stages", ["preflop", "flop", "turn", "river"])):
            return False
        if cfg.get("raise_requires_free_action", False) and to_call > 0:
            return False
        if _label_name(_rule_label(rule_action)) not in set(cfg.get("raise_rule_labels", list(LABELS))):
            return False
        return (
            cfg.get("allow_raise", True)
            and not state.get("opponent_allin")
            and conf >= float(cfg.get("raise_conf", 0.86))
            and raise_delta > max(to_call, 0)
            and raise_delta <= float(cfg.get("max_raise_delta", 1600))
            and raise_delta / max(1.0, pot + to_call) <= float(cfg.get("max_raise_pot_ratio", 1.25))
        )
    if label == 5:
        return (
            cfg.get("allow_allin", False)
            and not state.get("opponent_allin")
            and conf >= float(cfg.get("allin_conf", 1.01))
        )
    return False


def apply_neural_advice(req: dict[str, Any], state: dict[str, Any], rule_action: int) -> int:
    model = _model()
    if model is None:
        return rule_action
    feature_req = dict(req)
    for key in ("pot", "to_call", "my_stage_bet", "opponent_stage_bet", "opponent_allin"):
        if key in state:
            feature_req[key] = state[key]
    probs = _predict(model, encode_features(feature_req, None))
    label, conf, masked_probs = _masked_top(probs, _legal_mask(req, state))
    if label == _rule_label(rule_action):
        return rule_action
    candidate = _candidate_action(label, req, state)
    if (
        _passes_runtime_gate(label, conf, masked_probs, req, state, rule_action, candidate)
        and _passes_advantage_gate(req, state, rule_action, label, conf, probs)
        and _passes_interaction_gate(req, state, rule_action, label, conf, probs)
        and _passes_isolated_veto_gate(req, state, rule_action, label, conf, probs)
        and _passes_value_veto_gate(req, state, rule_action, label, conf, probs)
    ):
        return candidate
    return rule_action
