#!/usr/bin/env python3
"""Train an opponent-aware neural value network on multi-action counterfactual data.

This is the large-opponent-model trainer requested by the neural-line objective.
Unlike the earlier small-MLP profile gates (h8/h16/h32 single-layer), this model:

1. Encodes the decision state with the existing ``encode_features`` contract plus
   opponent-profile and action-history features.
2. Optionally encodes the *observable opponent action history* of the current
   hand into a fixed-size embedding via a small GRU, producing an opponent
   behavioural embedding (not just hand-crafted VPIP/PFR).
3. Concatenates state + opponent-embedding + candidate-action features and
   predicts a per-legal-action value vector (chip-EV delta vs the rule action).

Targets are the counterfactual chip deltas produced by
``native_tcp_counterfactual_probe.py`` / ``multi_action_shard_runner.py``.

The trained model is exported to a small JSON weight file so a native national
TCP runtime bot can run a deterministic pure-Python forward pass (no torch at
runtime, no network access).

Usage:
    python train_opponent_value_net.py \\
        --data data/cf_train.jsonl --val-data data/cf_val.jsonl \\
        --hidden 128 --gru-hidden 64 --epochs 60 --lr 1e-3 \\
        --device cuda --out weights/opp_value_gru_h128_seed<N>.json
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import numpy as np  # noqa: E402

try:
    import torch
    import torch.nn as nn
    HAS_TORCH = True
except Exception:  # pragma: no cover
    HAS_TORCH = False

from feature_spec import LABELS, encode_features  # noqa: E402

LABEL_TO_ID = {name: i for i, name in enumerate(LABELS)}
NUM_LABELS = len(LABELS)

# Tunable feature sizes for the opponent action-history GRU input.
# Each observed opponent action in the current hand is encoded into a small
# vector: [street one-hot (4), action-type one-hot (5), normalized amount,
# normalized stage bet, normalized pot, is_raise, is_allin].
HIST_FEAT_DIM = 4 + 5 + 6


def _f(mapping: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(mapping.get(key, default) or default)
    except (TypeError, ValueError):
        return float(default)


def _clip01(x: float) -> float:
    if x != x:
        return 0.0
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _opponent_profile_features(req: dict[str, Any]) -> list[float]:
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


# Number of cross-hand opponent features (must match runtime).
CROSS_HAND_DIM = 20


def _f_list(lst: Any, idx: int, default: float = 0.0) -> float:
    try:
        return float(lst[idx]) if lst is not None and len(lst) > idx else default
    except (TypeError, ValueError, IndexError):
        return default


def _cross_hand_opp_features(req: dict[str, Any]) -> list[float]:
    """Cross-hand opponent encoding: aggregated behavioural stats + showdown
    summaries + match-progress context (CROSS_HAND_DIM = 20 features).

    [0:12]  opponent_profile rates (confidence, fold/call/check/raise/allin,
            aggression, preflop/postflop norms + raise rates, actions_total_norm)
    [12]    hand progress (hand/max_hand)
    [13]    net chip position
    [14:18] showdown: n_showdowns_norm, opp_win_rate, opp_aggression, avg_pot
    [18]    is_small_blind
    [19]    confidence * actions_total_norm (reliability)
    """
    p = req.get("opponent_profile") or {}
    if not isinstance(p, dict):
        p = {}
    rates = [
        _clip01(_f(p, "confidence")), _clip01(_f(p, "fold_rate")),
        _clip01(_f(p, "call_rate")), _clip01(_f(p, "check_rate")),
        _clip01(_f(p, "raise_rate")), _clip01(_f(p, "allin_rate")),
        _clip01(_f(p, "aggression")), _clip01(_f(p, "preflop_raise_rate")),
        _clip01(_f(p, "postflop_raise_rate")), _clip01(_f(p, "preflop_actions_norm")),
        _clip01(_f(p, "postflop_actions_norm")), _clip01(_f(p, "actions_total_norm")),
    ]
    hand = _f(req, "hand", 0.0)
    max_hand = _f(req, "max_hand", 70.0) or 70.0
    hand_prog = _clip01(hand / max(max_hand, 1.0))
    my_chips = _f(req, "my_chips", 20000.0)
    twc = req.get("total_win_chips") or [0.0, 0.0]
    net_pos = _clip01((my_chips + _f_list(twc, 0)) / 40000.0)
    sds = req.get("opponent_showdowns") or []
    n_sd = min(len(sds), 10)
    opp_won = opp_agg = 0.0
    pots = []
    for sd in sds[:10]:
        if not isinstance(sd, dict):
            continue
        earned = _f(sd, "earned", 0.0)
        if earned < 0:
            opp_won += 1
        hist = sd.get("history") or []
        n_raise = sum(1 for h in hist if isinstance(h, dict)
                      and str(h.get("action_type", "")).lower() == "raise")
        opp_agg += _clip01(n_raise / max(1, len(hist)))
        pots.append(abs(earned))
    n_sd_norm = _clip01(n_sd / 10.0)
    opp_win_sd = _clip01(opp_won / max(1, n_sd))
    opp_agg_sd = _clip01(opp_agg / max(1, n_sd))
    avg_pot = _clip01((sum(pots) / max(1, len(pots))) / 20000.0) if pots else 0.0
    is_sb = 1.0 if _f(req, "dealer_id", -1) == _f(req, "my_id", -2) else 0.0
    reliability = rates[0] * rates[11]
    return rates + [hand_prog, net_pos, n_sd_norm, opp_win_sd, opp_agg_sd,
                    avg_pot, is_sb, reliability]


def _street_onehot(history_entry: dict[str, Any]) -> list[float]:
    stage = history_entry.get("stage_bet", 0)
    # Derive street from public-card count if available; fall back to round.
    n_pub = len(history_entry.get("public_cards") or [])
    if "round" in history_entry:
        r = int(history_entry.get("round", 0) or 0)
    else:
        r = 0
    street_idx = min(3, r) if n_pub == 0 else (1 if n_pub == 3 else (2 if n_pub == 4 else 3))
    return [1.0 if street_idx == i else 0.0 for i in range(4)]


def _action_onehot(action_type: str) -> list[float]:
    types = ("fold", "call", "check", "raise", "allin")
    at = str(action_type or "").lower()
    return [1.0 if at == t else 0.0 for t in types]


def _encode_history_entry(entry: dict[str, Any], pot_ref: float) -> list[float]:
    """Encode one observed action (mine or opponent) for the GRU."""
    stage_bet = _f(entry, "stage_bet")
    committed = _f(entry, "committed")
    action = _f(entry, "action")
    is_raise = 1.0 if str(entry.get("action_type", "")).lower() == "raise" else 0.0
    is_allin = 1.0 if str(entry.get("action_type", "")).lower() == "allin" else 0.0
    return (
        _street_onehot(entry)
        + _action_onehot(str(entry.get("action_type", "")))
        + [
            _clip01(stage_bet / 20000.0),
            _clip01(action / 20000.0),
            _clip01(committed / 20000.0),
            _clip01(pot_ref / 20000.0),
            is_raise,
            is_allin,
        ]
    )


def build_sample(row: dict[str, Any], *, max_hist: int = 16) -> dict[str, Any]:
    """Turn one JSONL counterfactual row into tensors-ready features + targets.

    A row produced by the probe contains: request, state, rule_label_id,
    label values (per-legal-action chip deltas), and the observable action
    history of the hand. We emit the state feature vector, the opponent
    profile vector, the padded action-history sequence, and the 6-d value
    target vector (NaN where a label is illegal / unobserved).
    """
    req = row.get("request") or {}
    if not isinstance(req, dict):
        req = {}
    state = row.get("state") or {}
    if not isinstance(state, dict):
        state = {}
    feat_req = dict(req)
    for key in ("pot", "to_call", "my_stage_bet", "opponent_stage_bet", "opponent_allin"):
        if key in state:
            feat_req[key] = state[key]
    state_feat = encode_features(feat_req, None)
    profile_feat = _opponent_profile_features(req)
    history = list(req.get("history") or [])
    pot_ref = _f(state, "pot", _f(req, "pot", 150.0))
    hist_feat = [_encode_history_entry(h, pot_ref) for h in history if isinstance(h, dict)]
    hist_feat = hist_feat[-max_hist:] if hist_feat else []
    # Per-label value targets (chip delta vs rule). The native probe emits a
    # length-NUM_LABELS ``targets`` array aligned to LABELS plus a
    # ``target_mask`` (1=observed/legal, 0=unprobed). Unobserved entries become
    # NaN so the loss masks them out. Fall back to legacy field names.
    raw_targets = row.get("targets") or row.get("raw_targets") or row.get("label_values")
    raw_mask = row.get("target_mask") or row.get("legal_mask")
    if isinstance(raw_targets, dict):
        raw_targets = [raw_targets.get(name) for name in LABELS]
    if isinstance(raw_targets, list) and len(raw_targets) == NUM_LABELS:
        target = []
        for i, v in enumerate(raw_targets):
            mask_i = 1
            if isinstance(raw_mask, list) and i < len(raw_mask):
                mask_i = int(raw_mask[i])
            if v is None or v != v or mask_i == 0:
                target.append(float("nan"))
            else:
                target.append(float(v))
    else:
        target = [float("nan")] * NUM_LABELS
    # Candidate action id this row is focused on (if single-decision probe).
    candidate_id = row.get("label_id")
    if candidate_id is None:
        candidate_id = row.get("candidate_label_id")
    rule_id = row.get("rule_label_id")
    return {
        "state": state_feat,
        "profile": profile_feat,
        "cross_hand": _cross_hand_opp_features(req),
        "history": hist_feat,
        "target": target,
        "rule_id": rule_id,
        "candidate_id": candidate_id,
        "opponent": row.get("opponent", "?"),
    }


class OpponentAwareValueNet(nn.Module):
    """State MLP + intra-hand GRU + cross-hand opponent encoder -> value vector."""

    def __init__(self, state_dim: int, profile_dim: int, *, gru_hidden: int = 64,
                 hidden: int = 128, dropout: float = 0.1,
                 cross_hand_dim: int = CROSS_HAND_DIM) -> None:
        super().__init__()
        self.gru = nn.GRU(HIST_FEAT_DIM, gru_hidden, batch_first=True)
        self.cross_hand_dim = cross_hand_dim
        self.opp_encoder = nn.Sequential(
            nn.Linear(cross_hand_dim, 32),
            nn.ReLU(),
            nn.Linear(32, 16),
        )
        head_in = state_dim + profile_dim + gru_hidden + 16
        self.head = nn.Sequential(
            nn.Linear(head_in, hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, NUM_LABELS),
        )

    def forward(self, state: torch.Tensor, profile: torch.Tensor,
                hist: torch.Tensor, hist_mask: torch.Tensor,
                cross_hand: torch.Tensor | None = None) -> torch.Tensor:
        if hist.size(1) > 0:
            _, h = self.gru(hist)
            opp_emb = h.squeeze(0) * hist_mask.squeeze(-1)
        else:
            opp_emb = torch.zeros(state.size(0), self.gru.hidden_size, device=state.device)
        parts = [state, profile, opp_emb]
        if cross_hand is not None and self.cross_hand_dim > 0:
            parts.append(self.opp_encoder(cross_hand))
        x = torch.cat(parts, dim=1)
        return self.head(x)


def load_jsonl(path: str) -> list[dict[str, Any]]:
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def collate(samples: list[dict[str, Any]], *, max_hist: int, device: str):
    B = len(samples)
    state_dim = len(samples[0]["state"]) if samples else 0
    profile_dim = len(samples[0]["profile"]) if samples else 0
    cross_dim = len(samples[0].get("cross_hand") or []) if samples else 0
    state_t = torch.zeros(B, state_dim, device=device)
    profile_t = torch.zeros(B, profile_dim, device=device)
    cross_t = torch.zeros(B, cross_dim, device=device) if cross_dim else None
    hist_t = torch.zeros(B, max_hist, HIST_FEAT_DIM, device=device)
    hist_mask = torch.zeros(B, 1, 1, device=device)
    target_t = torch.full((B, NUM_LABELS), float("nan"), device=device)
    rule_ids = []
    cand_ids = []
    for i, s in enumerate(samples):
        state_t[i] = torch.tensor(s["state"], dtype=torch.float32)
        profile_t[i] = torch.tensor(s["profile"], dtype=torch.float32)
        if cross_t is not None and s.get("cross_hand"):
            cross_t[i] = torch.tensor(s["cross_hand"], dtype=torch.float32)
        h = s["history"]
        for t, vec in enumerate(h):
            hist_t[i, t] = torch.tensor(vec, dtype=torch.float32)
        if h:
            hist_mask[i] = 1.0
        target_t[i] = torch.tensor(s["target"], dtype=torch.float32)
        rule_ids.append(s["rule_id"])
        cand_ids.append(s["candidate_id"])
    return state_t, profile_t, cross_t, hist_t, hist_mask, target_t, rule_ids, cand_ids


def main(argv: list[str] | None = None) -> int:
    if not HAS_TORCH:
        print("ERROR: torch is required for this trainer", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, help="Training JSONL.")
    ap.add_argument("--val-data", help="Validation JSONL.")
    ap.add_argument("--out", required=True, help="Output JSON weights path.")
    ap.add_argument("--hidden", type=int, default=128)
    ap.add_argument("--gru-hidden", type=int, default=64)
    ap.add_argument("--max-hist", type=int, default=16)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch-size", type=int, default=128)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--clip-target", type=float, default=5000.0,
                    help="Clip absolute chip-delta targets to this value.")
    ap.add_argument("--task", choices=("regression", "classification"), default="regression",
                    help="regression: predict chip-delta; classification: predict candidate>rule.")
    ap.add_argument("--pos-margin", type=float, default=100.0,
                    help="classification positive threshold: delta > pos_margin => label 1.")
    ap.add_argument("--neg-margin", type=float, default=-100.0,
                    help="classification: deltas in (neg_margin, pos_margin) are ignored (ambiguous).")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    device = args.device if torch.cuda.is_available() else "cpu"
    print(f"[opp_value] device={device} torch={torch.__version__}", flush=True)

    train_rows = load_jsonl(args.data)
    val_rows = load_jsonl(args.val_data) if args.val_data else []
    print(f"[opp_value] train rows={len(train_rows)} val rows={len(val_rows)}", flush=True)
    if not train_rows:
        print("ERROR: no training rows", file=sys.stderr)
        return 1

    train_samples = [build_sample(r, max_hist=args.max_hist) for r in train_rows]
    val_samples = [build_sample(r, max_hist=args.max_hist) for r in val_rows]
    state_dim = len(train_samples[0]["state"])
    profile_dim = len(train_samples[0]["profile"])

    model = OpponentAwareValueNet(
        state_dim, profile_dim, gru_hidden=args.gru_hidden,
        hidden=args.hidden, dropout=args.dropout,
    ).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    # MSE on valid (non-NaN) per-legal-action targets, clipped.
    print(f"[opp_value] params={sum(p.numel() for p in model.parameters())} "
          f"state_dim={state_dim} profile_dim={profile_dim} gru_hidden={args.gru_hidden}", flush=True)

    def run_epoch(samples, *, train: bool):
        model.train(train)
        idx = list(range(len(samples)))
        if train:
            random.shuffle(idx)
        total_loss = 0.0
        total_n = 0
        all_err = []
        all_prob = []  # for classification metrics
        all_lbl = []
        for start in range(0, len(idx), args.batch_size):
            batch = [samples[i] for i in idx[start:start + args.batch_size]]
            state_t, profile_t, cross_t, hist_t, hist_mask, target_t, _, _ = collate(
                batch, max_hist=args.max_hist, device=device)
            target_t = torch.clamp(target_t, -args.clip_target, args.clip_target)
            valid = ~torch.isnan(target_t)
            if valid.sum() == 0:
                continue
            scale = float(args.clip_target)
            pred = model(state_t, profile_t, hist_t, hist_mask, cross_t)
            if args.task == "regression":
                target_norm = target_t / scale
                target_clean = torch.where(valid, target_norm, torch.zeros_like(target_norm))
                err = (pred - target_clean) ** 2
                loss = (err * valid).sum() / valid.sum().clamp(min=1)
                metric_val = ((pred[valid] - target_norm[valid]).abs() * scale).cpu().numpy()
                with torch.no_grad():
                    all_err.append(metric_val)
            else:  # classification: label = 1 if delta > pos_margin, 0 if < neg_margin, skip otherwise
                pos = float(args.pos_margin)
                neg = float(args.neg_margin)
                cls_mask = valid & (target_t > pos) | (valid & (target_t < neg))
                if cls_mask.sum() == 0:
                    continue
                labels = (target_t > pos).float()
                labels = torch.where(cls_mask, labels, torch.zeros_like(labels))
                probs = torch.sigmoid(pred)
                bce = torch.nn.functional.binary_cross_entropy(probs, labels, reduction="none")
                loss = (bce * cls_mask).sum() / cls_mask.sum().clamp(min=1)
                with torch.no_grad():
                    all_prob.append(probs[cls_mask].cpu().numpy())
                    all_lbl.append(labels[cls_mask].cpu().numpy())
            if torch.isnan(loss) or torch.isinf(loss):
                continue
            if train:
                opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
            total_loss += float(loss.item()) * int((valid if args.task == "regression" else cls_mask).sum().item())
            total_n += int((valid if args.task == "regression" else cls_mask).sum().item())
        if args.task == "regression":
            mae = float(np.concatenate(all_err).mean()) if all_err else float("nan")
            return total_loss / max(1, total_n), mae
        # classification metrics: return (logloss, accuracy)
        if all_prob:
            probs = np.concatenate(all_prob)
            lbls = np.concatenate(all_lbl)
            eps = 1e-7
            ll = float(-(lbls * np.log(probs.clip(eps, 1 - eps)) +
                         (1 - lbls) * np.log((1 - probs).clip(eps, 1 - eps))).mean())
            acc = float(((probs >= 0.5).astype(float) == lbls).mean())
            return ll, acc
        return float("nan"), float("nan")

    metric_name = "mae" if args.task == "regression" else "acc"
    best_val_metric = float("inf") if args.task == "regression" else -1.0
    better = (lambda v: v < best_val_metric) if args.task == "regression" else (lambda v: v > best_val_metric)
    for ep in range(1, args.epochs + 1):
        tr_m1, tr_m2 = run_epoch(train_samples, train=True)
        va_m1, va_m2 = run_epoch(val_samples, train=False) if val_samples else (float("nan"), float("nan"))
        flag = ""
        if val_samples and better(va_m2):
            best_val_metric = va_m2
            flag = " *best"
        if args.task == "regression":
            print(f"[opp_value] ep {ep:3d} train_mse={tr_m1:.1f} train_mae={tr_m2:.1f} "
                  f"val_mse={va_m1:.1f} val_mae={va_m2:.1f}{flag}", flush=True)
        else:
            print(f"[opp_value] ep {ep:3d} train_logloss={tr_m1:.4f} train_acc={tr_m2:.3f} "
                  f"val_logloss={va_m1:.4f} val_acc={va_m2:.3f}{flag}", flush=True)

    # Export to a deterministic JSON weight file for pure-Python runtime.
    _export_json(model, args.out, state_dim, profile_dim, args.gru_hidden,
                 args.hidden, args.max_hist, best_val_metric, val_samples, task=args.task)
    print(f"[opp_value] wrote {args.out} best_val_{metric_name}={best_val_metric}", flush=True)
    return 0


def _export_json(model, out_path: str, state_dim: int, profile_dim: int,
                 gru_hidden: int, hidden: int, max_hist: int,
                 best_val_mae: float, val_samples, *, task: str = "regression") -> None:
    model.eval()
    sd = model.state_dict()
    weights = {k: v.cpu().tolist() for k, v in sd.items()}
    metric_key = "best_val_mae" if task == "regression" else "best_val_acc"
    meta = {
        "format": "opp_value_gru_v1",
        "task": task,
        "labels": list(LABELS),
        "state_dim": state_dim,
        "profile_dim": profile_dim,
        "gru_hidden": gru_hidden,
        "hist_feat_dim": HIST_FEAT_DIM,
        "hidden": hidden,
        "max_hist": max_hist,
        "cross_hand_dim": CROSS_HAND_DIM,
        metric_key: float(best_val_mae) if best_val_mae == best_val_mae else None,
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "weights": weights}, fh)


if __name__ == "__main__":
    raise SystemExit(main())
