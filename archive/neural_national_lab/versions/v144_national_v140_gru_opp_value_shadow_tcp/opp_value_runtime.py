#!/usr/bin/env python3
"""Pure-Python (stdlib-only) GRU inference for the opponent-aware value network.

This is the runtime half of the large-opponent-model line: a trained PyTorch
GRU value network (``train_opponent_value_net.py``) is exported to JSON, and this
module runs a deterministic forward pass in pure Python so a native national
TCP bot can use it at runtime with no torch, no numpy, no network access, and a
hard fallback to the rule action if anything fails.

The exported format (``opp_value_gru_v1``) stores:
  meta: {format, labels, state_dim, profile_dim, gru_hidden, hist_feat_dim,
         hidden, max_hist, best_val_mae}
  weights: {gru.weight_ih_l0, gru.weight_hh_l0, gru.bias_ih_l0, gru.bias_hh_l0,
            head.<layer>.weight, head.<layer>.bias, ...}

Forward pass = GRU over the padded action-history sequence -> opponent embedding,
concatenated with state + profile features, through the MLP head, yielding a
6-d value vector aligned to LABELS = (fold, call, raise_half, raise_pot,
raise_2pot, allin).
"""
from __future__ import annotations

import json
import math
import os
from typing import Any

LABELS = ("fold", "call", "raise_half", "raise_pot", "raise_2pot", "allin")
NUM_LABELS = len(LABELS)


def _clip01(x: float) -> float:
    if x != x:
        return 0.0
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _f(mapping: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(mapping.get(key, default) or default)
    except (TypeError, ValueError):
        return float(default)


def _sigmoid(x: float) -> float:
    if x >= 30:
        return 1.0
    if x <= -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def _matvec(weights: list[list[float]], x: list[float], bias: list[float] | None = None) -> list[float]:
    out = []
    for row in weights:
        s = 0.0
        for wi, xi in zip(row, x):
            s += wi * xi
        if bias is not None:
            s += bias[len(out)]
        out.append(s)
    return out


def _relu(v: list[float]) -> list[float]:
    return [x if x > 0.0 else 0.0 for x in v]


class OppValueGRURuntime:
    """Deterministic pure-Python inference for the exported GRU value net."""

    def __init__(self, model: dict[str, Any]) -> None:
        meta = model.get("meta") or {}
        if meta.get("format") != "opp_value_gru_v1":
            raise ValueError(f"unsupported format: {meta.get('format')!r}")
        self.meta = meta
        self.weights = model.get("weights") or {}
        self.gru_hidden = int(meta.get("gru_hidden", 64))
        self.hist_feat_dim = int(meta.get("hist_feat_dim", 15))
        self.hidden = int(meta.get("hidden", 128))
        self.max_hist = int(meta.get("max_hist", 16))
        self.labels = tuple(meta.get("labels") or LABELS)

    @classmethod
    def load(cls, path: str) -> "OppValueGRURuntime | None":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                model = json.load(fh)
            return cls(model)
        except Exception:
            return None

    def _gru_forward(self, seq: list[list[float]]) -> list[float]:
        """Run a single-layer GRU over ``seq`` and return the final hidden state.

        PyTorch GRU gates (3*hidden): reset r, update z, candidate n, in order
        r,z,n (PyTorch concatenates as [r; z; n] in the weight matrices).
        h_prev initialized to zeros.
        """
        H = self.gru_hidden
        w_ih = self.weights.get("gru.weight_ih_l0")
        w_hh = self.weights.get("gru.weight_hh_l0")
        b_ih = self.weights.get("gru.bias_ih_l0")
        b_hh = self.weights.get("gru.bias_hh_l0")
        if not w_ih or not w_hh:
            return [0.0] * H
        h = [0.0] * H
        for xt in seq:
            gi = _matvec(w_ih, xt, b_ih)
            gh = _matvec(w_hh, h, b_hh)
            for k in range(H):
                r = _sigmoid(gi[k] + gh[k])
                z = _sigmoid(gi[H + k] + gh[H + k])
                n = math.tanh(gi[2 * H + k] + r * gh[2 * H + k])
                h[k] = (1.0 - z) * n + z * h[k]
        return h

    def _head_forward(self, x: list[float]) -> list[float]:
        out = x
        layers = []
        # Collect linear layers in order: head.0, head.3, head.6 (ReLU between).
        for idx in (0, 3, 6):
            w = self.weights.get(f"head.{idx}.weight")
            b = self.weights.get(f"head.{idx}.bias")
            if w is None:
                break
            layers.append((w, b))
        for li, (w, b) in enumerate(layers):
            out = _matvec(w, out, b)
            if li < len(layers) - 1:
                out = _relu(out)
        return out

    def predict(self, state_feat: list[float], profile_feat: list[float],
                hist_seq: list[list[float]]) -> list[float]:
        """Return a NUM_LABELS value vector (chip-EV delta vs rule action).

        Always returns a length-NUM_LABELS list; never raises. On any internal
        failure the caller should fall back to the rule action.
        """
        try:
            opp_emb = self._gru_forward(hist_seq[: self.max_hist]) if hist_seq else [0.0] * self.gru_hidden
            x = list(state_feat) + list(profile_feat) + list(opp_emb)
            out = self._head_forward(x)
            if len(out) != NUM_LABELS:
                return [0.0] * NUM_LABELS
            return out
        except Exception:
            return [0.0] * NUM_LABELS


def encode_history_entry(entry: dict[str, Any], pot_ref: float) -> list[float]:
    """Runtime version of the history-entry encoder (must match the trainer)."""
    stage_bet = _f(entry, "stage_bet")
    committed = _f(entry, "committed")
    action = _f(entry, "action")
    is_raise = 1.0 if str(entry.get("action_type", "")).lower() == "raise" else 0.0
    is_allin = 1.0 if str(entry.get("action_type", "")).lower() == "allin" else 0.0
    n_pub = len(entry.get("public_cards") or [])
    r = int(entry.get("round", 0) or 0)
    street_idx = min(3, r) if n_pub == 0 else (1 if n_pub == 3 else (2 if n_pub == 4 else 3))
    street = [1.0 if street_idx == i else 0.0 for i in range(4)]
    types = ("fold", "call", "check", "raise", "allin")
    at = str(entry.get("action_type", "")).lower()
    aoh = [1.0 if at == t else 0.0 for t in types]
    return street + aoh + [
        _clip01(stage_bet / 20000.0), _clip01(action / 20000.0),
        _clip01(committed / 20000.0), _clip01(pot_ref / 20000.0),
        is_raise, is_allin,
    ]


def opponent_profile_features(req: dict[str, Any]) -> list[float]:
    p = req.get("opponent_profile") or {}
    if not isinstance(p, dict):
        p = {}
    keys = (
        "confidence", "actions_total_norm", "fold_rate", "call_rate",
        "check_rate", "raise_rate", "allin_rate", "aggression",
        "preflop_actions_norm", "preflop_raise_rate",
        "postflop_actions_norm", "postflop_raise_rate",
    )
    return [_clip01(_f(p, k)) for k in keys]
