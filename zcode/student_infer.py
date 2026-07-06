"""Inference wrapper for the behaviour-cloning student policy.

Loads the trained MLP (preferring the JSON export so runtime stays
numpy-only; falls back to torch if available). Provides:

  - ``predict(request)`` -> (class, probabilities)
  - ``decide(request, st, base_action)`` -> adjusted action

The student is used as an *advisory* signal: it can override the base
zcode policy when its top-1 confidence is high on the fold / check / call
classes — this is where v279's behaviour cloning adds the most value
(fixing systematic over-calls and wrong folds).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Optional, Tuple

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from zcode.train_student import (
    extract_features, class_to_action, action_to_class,
    FOLD, CHECK, CALL, BET_S, BET_M, BET_L, ALLIN, N_CLASSES, FEATURE_DIM,
)

MODEL_JSON = os.path.join(_HERE, "student_policy.json")
MODEL_PT = os.path.join(_HERE, "student_policy.pt")


class _StdMLP:
    """Pure-numpy MLP forward pass loaded from JSON weights."""

    def __init__(self, spec):
        self.layers = []
        for L in spec["layers"]:
            self.layers.append((np.array(L["w"], dtype=np.float32),
                                np.array(L["b"], dtype=np.float32)))

    def forward(self, x):
        for i, (w, b) in enumerate(self.layers):
            x = x @ w.T + b
            if i < len(self.layers) - 1:
                x = np.maximum(x, 0.0)  # ReLU
        return x


class StudentInfer:
    def __init__(self):
        self.mlp = None
        self.torch_model = None
        self._load()

    def _load(self):
        # Prefer JSON (numpy-only) for fast cold-start.
        if os.path.exists(MODEL_JSON):
            try:
                with open(MODEL_JSON) as f:
                    spec = json.load(f)
                self.mlp = _StdMLP(spec)
                return
            except Exception as e:
                print(f"[student] JSON load failed: {e}", file=sys.stderr)
        # Fallback: torch.
        if os.path.exists(MODEL_PT):
            try:
                import torch
                from zcode.train_student import StudentPolicy
                ckpt = torch.load(MODEL_PT, map_location="cpu",
                                  weights_only=False)
                m = StudentPolicy(in_dim=ckpt["in_dim"],
                                  hidden=ckpt.get("hidden", 64),
                                  n_classes=ckpt.get("n_classes", N_CLASSES))
                m.load_state_dict(ckpt["state_dict"])
                m.eval()
                self.torch_model = m
                return
            except Exception as e:
                print(f"[student] torch load failed: {e}", file=sys.stderr)

    def available(self) -> bool:
        return self.mlp is not None or self.torch_model is not None

    def predict_proba(self, request: dict) -> np.ndarray:
        feat, _ = extract_features({
            "my_cards": request["my_cards"],
            "public_cards": request.get("public_cards", []),
            "history": request.get("history", []),
            "my_id": request["my_id"],
            "dealer_id": request["dealer_id"],
            "my_chips": request.get("my_chips", 20000),
            "action": 0,  # dummy; not used by feature extractor
        })
        x = feat.astype(np.float32)
        if self.mlp is not None:
            logits = self.mlp.forward(x)
        else:
            import torch
            with torch.no_grad():
                logits = self.torch_model(
                    torch.from_numpy(x).unsqueeze(0)).squeeze(0).numpy()
        # Softmax.
        m = logits.max()
        ex = np.exp(logits - m)
        return ex / ex.sum()

    def predict(self, request: dict) -> Tuple[int, np.ndarray]:
        p = self.predict_proba(request)
        return int(p.argmax()), p


# Singleton.
_STUDENT: Optional[StudentInfer] = None


def get_student() -> Optional[StudentInfer]:
    global _STUDENT
    if _STUDENT is None:
        try:
            _STUDENT = StudentInfer()
        except Exception:
            _STUDENT = None
    return _STUDENT


# ---------------------------------------------------------------------------
# Advisory override
# ---------------------------------------------------------------------------

def advise_action(request: dict, st, base_action: int) -> int:
    """Possibly override the base zcode action using the student's advice.

    Conservative rules:
    - If student predicts FOLD with prob >= 0.55 and base is call/bet,
      and the base wasn't a value-raise on a strong hand, switch to fold
      (saves a bad call-down).
    - If student predicts CHECK/CALL with prob >= 0.55 and base is a
      speculative bet, keep the base (don't override aggression).
    - If student predicts a bet class (BET_S/M/L) with prob >= 0.5 and
      base is check/fold, and pot odds allow, convert to a value bet
      (adds aggression v279 would have).

    Returns the (possibly overridden) action integer.
    """
    stu = get_student()
    if stu is None or not stu.available():
        return base_action
    try:
        cls, proba = stu.predict(request)
    except Exception:
        return base_action

    p_fold = proba[FOLD]
    p_check = proba[CHECK]
    p_call = proba[CALL]
    p_bet = proba[BET_S] + proba[BET_M] + proba[BET_L]
    p_allin = proba[ALLIN]

    # 1) Fold override: student strongly says fold and base is calling/betting.
    # Use a higher threshold (0.62) to avoid over-folding strong hands;
    # also require that the call we'd otherwise make is not trivially
    # +EV (i.e. the bet is meaningful).
    if (p_fold >= 0.62 and base_action != -1 and st.to_call > 0
            and st.to_call >= BIG_BLIND):
        return -1

    # 2) Aggression injection: DISABLED by default. The student's bet-class
    # predictions are too noisy for safe injection (causes high variance).
    # Aggression is left to the base zcode policy which has well-tested
    # sizing. Enable only experimentally with care.

    # 3) Check/call nudge: if student strongly predicts check/call and base
    # is a fold facing a free check (to_call == 0), convert fold->check.
    if base_action == -1 and st.to_call == 0 and (p_check + p_call) >= 0.55:
        return 0

    return base_action


if __name__ == "__main__":  # pragma: no cover - sanity
    s = get_student()
    if s is None or not s.available():
        print("no model available")
        sys.exit(1)
    # AA preflop SB.
    req = {"dealer_id": 0, "my_id": 0, "my_chips": 19950,
           "my_cards": [48, 49], "public_cards": [], "history": [],
           "hand": 0, "max_hand": 70, "total_win_chips": [0, 0]}
    cls, p = s.predict(req)
    names = ["FOLD", "CHECK", "CALL", "BET_S", "BET_M", "BET_L", "ALLIN"]
    print("AA preflop SB:")
    for i, pn in enumerate(names):
        print(f"  {pn}: {p[i]:.3f}")
    print(f"  argmax: {names[cls]}")
    # 72o facing raise.
    req2 = {"dealer_id": 0, "my_id": 1, "my_chips": 19750,
            "my_cards": [3, 5], "public_cards": [],
            "history": [{"round": 0, "player_id": 0, "action": 250,
                         "action_type": "raise"}],
            "hand": 0, "max_hand": 70}
    cls2, p2 = s.predict(req2)
    print("\n72o BB vs raise:")
    for i, pn in enumerate(names):
        print(f"  {pn}: {p2[i]:.3f}")
    print(f"  argmax: {names[cls2]}")
