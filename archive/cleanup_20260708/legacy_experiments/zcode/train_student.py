"""Train a behaviour-cloning student MLP on v279 decisions.

Input features (per scenario):
  - hero hole cards (rank/suit encoded, one-hot-ish)
  - public cards (up to 5)
  - betting-round one-hot (0..3)
  - pot size (log-scaled), to-call (log-scaled), my-round-bet
  - position (is_sb), actions-this-round
  - preflop opponent action summary (limp / raise / 3bet) one-hot
  - postflop opponent aggression flags

Output: action class among
    0 fold, 1 check, 2 call-small, 3 bet-small, 4 bet-medium,
    5 bet-large, 6 allin

The model is a small MLP (3 hidden layers) trained with cross-entropy.
We also support a "bucket average" fallback (per-feature-bucket majority
vote) for environments without torch, but torch is available here.

The trained model is saved as ``zcode/student_policy.pt`` plus a JSON
encoding of weights for stdlib-only inference (so the bot itself stays
numpy-free at runtime if we choose).
"""

from __future__ import annotations

import json
import os
import random
import sys

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from zcode.cards import card_rank, card_suit
from zcode.state import BIG_BLIND, SMALL_BLIND, reconstruct_state

DATASET = os.path.join(_HERE, "nv18_v10.jsonl")  # v18+v10 merged: best generalization
MODEL_PT = os.path.join(_HERE, "student_policy.pt")
MODEL_JSON = os.path.join(_HERE, "student_policy.json")
FEATURE_DIM_FILE = os.path.join(_HERE, "student_feature_dim.txt")

# ---------------------------------------------------------------------------
# Action discretisation
# ---------------------------------------------------------------------------

# Classes:
FOLD = 0
CHECK = 1
CALL = 2        # call facing a bet (to_call > 0)
BET_S = 3       # bet/raise ~0.33-0.5x pot
BET_M = 4       # bet/raise ~0.6-1.0x pot
BET_L = 5       # bet/raise > 1.0x pot (but not allin)
ALLIN = 6
N_CLASSES = 7


def action_to_class(action: int, to_call: int, pot: int, my_chips: int,
                     min_raise_to: int, my_round_bet: int) -> int:
    """Map a raw v279 action integer to a discrete class."""
    if action == -1:
        return FOLD
    if action == -2:
        return ALLIN
    if action == 0:
        return CHECK if to_call == 0 else CALL
    # raise to ``action`` (raise-to-total).
    target = int(action)
    need = target - my_round_bet
    if pot <= 0:
        pot = 1
    ratio = need / pot
    if ratio < 0.55:
        return BET_S
    if ratio < 1.05:
        return BET_M
    return BET_L


def class_to_action(cls: int, st) -> int:
    """Convert a predicted class back to a legal action integer.

    ``st`` is a zcode.state.GameState used to compute legal sizes.
    """
    if cls == FOLD:
        return -1
    if cls == ALLIN:
        return -2
    if cls == CHECK:
        return 0
    if cls == CALL:
        return 0  # call/check both encode as 0
    # Bet sizing.
    pot = max(1, st.pot)
    if cls == BET_S:
        frac = 0.40
    elif cls == BET_M:
        frac = 0.75
    else:  # BET_L
        frac = 1.20
    desired_delta = max(BIG_BLIND, int(round(pot * frac)))
    target = max(st.min_raise_to, st.round_bet + desired_delta)
    # Clamp by stack.
    need = target - st.my_round_bet
    if need >= st.my_chips:
        return -2
    if target < st.min_raise_to:
        return 0
    return int(target)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def _card_rank_oh(card):
    """One-hot 13-dim rank encoding."""
    v = np.zeros(13, dtype=np.float32)
    v[card_rank(card) - 2] = 1.0
    return v


def _card_suit_oh(card):
    v = np.zeros(4, dtype=np.float32)
    v[card_suit(card)] = 1.0
    return v


def _cards_block(cards):
    """rank(13) + suit(4) per card, padded for up to 7 cards (2 hole + 5 board)."""
    feats = []
    for c in cards[:7]:
        feats.append(_card_rank_oh(c))
        feats.append(_card_suit_oh(c))
    while len(feats) < 7 * 2:
        feats.append(np.zeros(13, dtype=np.float32))
        feats.append(np.zeros(4, dtype=np.float32))
    return np.concatenate(feats)


def _opp_preflop_summary(history, my_id):
    """One-hot: opp limp / opp raise / opp 3bet / no-pf-action."""
    opp = 1 - my_id
    pf = [r for r in history if r.get("round", 0) == 0
          and r.get("player_id") == opp]
    oh = np.zeros(4, dtype=np.float32)
    raises = sum(1 for r in pf if r.get("action_type") == "raise")
    calls = sum(1 for r in pf if r.get("action_type") == "call")
    if not pf:
        oh[3] = 1.0
    elif raises >= 2:
        oh[2] = 1.0  # 3bet+
    elif raises == 1:
        oh[1] = 1.0
    elif calls >= 1:
        oh[0] = 1.0  # limp
    else:
        oh[3] = 1.0
    return oh


def _opp_postflop_summary(history, my_id, cur_round):
    opp = 1 - my_id
    pf_bets = 0
    pf_raises = 0
    pf_calls = 0
    for r in history:
        if r.get("round", 0) == 0:
            continue
        if r.get("player_id") != opp:
            continue
        at = r.get("action_type")
        if at == "raise":
            pf_bets += 1
            pf_raises += 1
        elif at == "call":
            pf_calls += 1
    return np.array([pf_bets, pf_raises, pf_calls], dtype=np.float32)


def extract_features(record):
    """Return (feature_vector, label) for one dataset record."""
    request = {
        "num_players": 2,
        "dealer_id": record["dealer_id"],
        "my_id": record["my_id"],
        "my_chips": record["my_chips"],
        "my_cards": record["my_cards"],
        "public_cards": record["public_cards"],
        "history": record["history"],
        "hand": 0,
        "max_hand": 70,
        "total_win_chips": [0, 0],
    }
    st = reconstruct_state(request)
    cards = list(record["my_cards"]) + list(record["public_cards"])
    cards_feat = _cards_block(cards)

    betting_round = st.betting_round
    round_oh = np.zeros(4, dtype=np.float32)
    round_oh[min(betting_round, 3)] = 1.0

    pot = max(1, st.pot)
    to_call = st.to_call
    log_pot = np.array([np.log1p(pot)], dtype=np.float32)
    log_to_call = np.array([np.log1p(to_call)], dtype=np.float32)
    is_sb = np.array([1.0 if st.is_sb else 0.0], dtype=np.float32)
    my_round_bet = np.array([st.my_round_bet / 1000.0], dtype=np.float32)
    actions_round = np.array([st.actions_this_round / 5.0], dtype=np.float32)
    min_raise = np.array([np.log1p(st.min_raise_to)], dtype=np.float32)

    opp_pf = _opp_preflop_summary(record["history"], record["my_id"])
    opp_post = _opp_postflop_summary(record["history"], record["my_id"],
                                      betting_round)
    to_call_ratio = np.array([(to_call / pot) if pot else 0.0],
                              dtype=np.float32)

    feat = np.concatenate([
        cards_feat,
        round_oh,
        log_pot, log_to_call, to_call_ratio, is_sb, my_round_bet,
        actions_round, min_raise,
        opp_pf, opp_post,
    ])

    label = action_to_class(record["action"], to_call, pot,
                             record["my_chips"], st.min_raise_to,
                             st.my_round_bet)
    return feat, label


FEATURE_DIM = 7 * (13 + 4) + 4 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 4 + 3
# = 119 + 4 + 9 = 132


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class StudentPolicy(nn.Module):
    def __init__(self, in_dim=FEATURE_DIM, hidden=64, n_classes=N_CLASSES):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(in_dim, hidden), nn.ReLU(), nn.Dropout(0.4),
            nn.Linear(hidden, hidden), nn.ReLU(), nn.Dropout(0.4),
            nn.Linear(hidden, n_classes),
        )

    def forward(self, x):
        return self.net(x)


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def load_dataset():
    recs = []
    with open(DATASET) as f:
        for line in f:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    return recs


def featurise_all(recs):
    X, y = [], []
    for r in recs:
        f, lbl = extract_features(r)
        X.append(f)
        y.append(lbl)
    X = np.stack(X)
    y = np.array(y, dtype=np.int64)
    return X, y


def train(epochs=25, lr=3e-4, batch_size=64, seed=0, val_frac=0.15):
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    recs = load_dataset()
    print(f"loaded {len(recs)} records")
    X, y = featurise_all(recs)
    print(f"feature dim: {X.shape[1]} (expected {FEATURE_DIM})")
    assert X.shape[1] == FEATURE_DIM, "feature dim mismatch"

    # Class balance.
    from collections import Counter
    counts = Counter(y.tolist())
    print("class counts:", dict(counts))
    # Class weights (inverse frequency, capped).
    total = len(y)
    weights = np.array([min(5.0, total / max(1, counts.get(i, 1)))
                        for i in range(N_CLASSES)], dtype=np.float32)
    weights = weights / weights.mean()
    print("class weights:", weights.round(3).tolist())

    # Shuffle + split.
    idx = np.random.permutation(total)
    n_val = max(1, int(total * val_frac))
    val_idx, train_idx = idx[:n_val], idx[n_val:]
    Xtr, ytr = X[train_idx], y[train_idx]
    Xva, yva = X[val_idx], y[val_idx]

    Xtr_t = torch.from_numpy(Xtr)
    ytr_t = torch.from_numpy(ytr)
    Xva_t = torch.from_numpy(Xva)
    yva_t = torch.from_numpy(yva)
    w_t = torch.from_numpy(weights)

    model = StudentPolicy(in_dim=X.shape[1])
    opt = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)

    best_acc = 0.0
    best_state = None
    for ep in range(epochs):
        model.train()
        perm = torch.randperm(len(Xtr_t))
        total_loss = 0.0
        for i in range(0, len(Xtr_t), batch_size):
            b = perm[i:i + batch_size]
            xb = Xtr_t[b]
            yb = ytr_t[b]
            logits = model(xb)
            loss = F.cross_entropy(logits, yb, weight=w_t)
            opt.zero_grad()
            loss.backward()
            opt.step()
            total_loss += loss.item() * len(b)
        # Validate.
        model.eval()
        with torch.no_grad():
            vlogits = model(Xva_t)
            vpred = vlogits.argmax(-1)
            vloss = F.cross_entropy(vlogits, yva_t, weight=w_t).item()
            vacc = (vpred == yva_t).float().mean().item()
            # Per-class accuracy.
            per_cls = {}
            for c in range(N_CLASSES):
                mask = (yva_t == c)
                if mask.sum() > 0:
                    per_cls[c] = (vpred[mask] == c).float().mean().item()
        if vacc > best_acc:
            best_acc = vacc
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        if (ep + 1) % 5 == 0 or ep == 0:
            print(f"ep{ep+1:2d} loss={total_loss/len(Xtr_t):.4f} "
                  f"vloss={vloss:.4f} vacc={vacc:.3f} per_cls={ {k: round(v,2) for k,v in per_cls.items()} }")

    # Restore best.
    if best_state is not None:
        model.load_state_dict(best_state)
    print(f"best val acc: {best_acc:.3f}")

    # Save model.
    torch.save({"state_dict": model.state_dict(),
                "in_dim": X.shape[1],
                "hidden": 64,
                "n_classes": N_CLASSES,
                "feature_dim": FEATURE_DIM}, MODEL_PT)
    # Save JSON weights for stdlib inference (optional).
    _save_json_weights(model, X.shape[1])
    with open(FEATURE_DIM_FILE, "w") as f:
        f.write(str(FEATURE_DIM))
    print(f"saved {MODEL_PT} and {MODEL_JSON}")
    return best_acc


def _save_json_weights(model, in_dim):
    """Export to a JSON for a pure-stdlib/numpy MLP inference at runtime."""
    sd = model.state_dict()
    out = {"in_dim": in_dim, "hidden": 64, "n_classes": N_CLASSES,
           "layers": []}
    keys = [k for k in sd.keys() if k.endswith(".weight") or k.endswith(".bias")]
    # net is Sequential; we just dump linear layers in order.
    lin_w = [k for k in sd if k.endswith(".weight")]
    lin_w.sort(key=lambda k: int(k.split('.')[1]))
    for k in lin_w:
        prefix = k.rsplit('.', 1)[0]
        b = prefix + '.bias'
        out["layers"].append({
            "w": sd[k].cpu().numpy().tolist(),
            "b": sd[b].cpu().numpy().tolist(),
        })
    with open(MODEL_JSON, "w") as f:
        json.dump(out, f)


if __name__ == "__main__":
    train()
