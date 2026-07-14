# v144 — GRU Opponent-Aware Value Network (Shadow)

Parent: `v140_national_v123_overlay_no_large_commit_veto_tcp`

## What changed

v144 adds a **GRU opponent-aware value network** to the v140 rule bot. This is
the first version in the neural line that uses a genuine sequence-based
opponent model (a GRU over the observable per-hand action history), not a
hand-crafted VPIP/PFR gate.

- New model: `opp_value_gru_h96_g48_seed2401.json` — a PyTorch-trained GRU
  value net exported to JSON, run at runtime by a **pure-Python (stdlib-only)
  GRU forward pass** (`opp_value_runtime.py`). No torch, no numpy, no network
  access at runtime.
- Architecture: state MLP (48-d `encode_features`) + opponent profile (12-d)
  + GRU embedding of the observable action history (15-d per action, up to 16
  steps, gru_hidden=48) -> 96-hidden MLP head -> 6-d per-legal-action value
  vector (chip-EV delta vs rule action).
- Training: GPU (CUDA), MSE on counterfactual chip-delta targets, NaN-masked
  loss, grad-clipped. 29,718 params (vs the old h8/h16/h32 single-layer gates).
- **Shadow mode is the default**: the network runs on every eligible preflop/flop
  decision, emits `GRU_OPP_SHADOW` predictions to stderr, and does NOT change
  the action. Override is config-gated off (`gru_opp_value_override: false`).

## Evidence

- Pure-Python GRU runtime matches the PyTorch forward pass **exactly**
  (max abs diff = 0.000000 over 20 history-bearing samples).
- v144 in shadow mode is byte-for-byte identical to v140 in play (same -33 net
  vs national_v135 on seed 5000 paired), confirming shadow mode cannot alter
  protocol behaviour.
- Training data: 241 train / 28 val / 36 held-out counterfactual rows from
  native TCP paired probes across the realtime strong classic pool (v135, v114,
  v73, v63, v121, v122, ...), old pool (v2-v16), and held-out opponents. Best
  validation MAE 317 (chip-delta units). Data is small — scaling is the main
  next step before enabling override.

## Protocol

v144 is a native national TCP bot, unchanged from v140 in protocol. The GRU
hook only ever returns a small constructive raise candidate (when override is
on) or None (shadow); it never returns fold/allin, and the candidate passes a
legality + magnitude check.

## Files added/changed vs v140

- `opp_value_runtime.py` (new) — pure-Python GRU inference.
- `opp_value_gru_h96_g48_seed2401.json` (new) — trained weights.
- `neural_policy.py` — GRU model loader + shadow/override hook + config keys.
- `neural_config.json` — `gru_opp_value_*` keys (shadow on, override off).

## Status

This version establishes the data-collection -> GPU training -> pure-Python
runtime -> native TCP shadow pipeline end-to-end. The override gate is
intentionally OFF until the model is retrained on a larger dataset and shown to
improve held-out EV without regressions.
