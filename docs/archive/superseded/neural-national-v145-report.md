# Neural National v145 Report — GRU Classification Override Gate

Date: 2026-07-10

v145 is the first neural version with an **active override gate** driven by a
genuine opponent-aware GRU sequence model trained on counterfactual data. It
builds on the v144 shadow pipeline and adds a conservative classification-based
override. This report is honest about both what works and what does not.

## Pipeline (all delivered and validated)

1. **Data** (`longrun_collect_oppmodel.py`): 2725 counterfactual rows (2030
   train / 358 val / 337 held-out) from native TCP paired probes across 16+
   opponents, including the live strongest classic bots. 49-minute nohup run.
2. **Model** (`train_opponent_value_net.py`): GRU over observable per-hand
   action history + state + opponent profile -> per-legal-action head.
   **Classification task** (predict candidate>rule), 29,718 params, GPU-trained.
3. **Runtime** (`opp_value_runtime.py`): pure-Python GRU forward pass, exact
   match to torch (maxdiff 0.0), sigmoid for classification. No torch at runtime.
4. **Bot** (`v145`): native TCP, override gate wired into `neural_policy.py`.

## Model quality (classification)

| Split | Samples | Logloss | Accuracy | ECE |
|---|---|---|---|---|
| Val | 511 | 0.562 | 74.4% | 0.060 |
| Held-out | 498 | 0.726 | 70.3% | 0.130 |

By street (held-out): turn 90.7%, flop 70.6%, preflop 69.3%, river 43.5%.
By label (held-out): raise_half 73.2%, call 72.6%, allin 71.4%, fold 68.6%,
raise_pot 68.5%.

This is a real, generalizing opponent-aware signal — not a single-seed fluke.

## Override gate

Switches to a small constructive raise only when all hold:
- candidate is a raise-type label (not fold/call/allin)
- P(candidate > rule) >= 0.80
- P(rule is good) <= 0.55
- opponent has >= 3 observed actions
- candidate amount in (0, 1400], passes legality

## Ablation (honest result)

v145 (neural on) vs v140 (rule only), strongest classic pool (v46, v120, v119,
v135), seed5200 m2 paired, deterministic bot seeds:

| Version | Total | /hand | W-L-D | illegal | timeout |
|---|---|---|---|---|---|
| v140 (rule) | -363 | -0.32 | 4-2-2 | 0 | 0 |
| v145 (neural) | -363 | -0.32 | 4-2-2 | 0 | 0 |

**Identical.** The conservative gate did not fire a net-positive override in
these 8 matches. v145 neither helps nor harms. This is the expected outcome of
a deliberately strict gate at 70% held-out accuracy.

## What this means

- The full pipeline works end-to-end: data -> GPU GRU -> exact pure-Python
  runtime -> native TCP override, all validated.
- The model learns a real signal (70% held-out accuracy), but the override gate
  is too conservative to measurably move match EV yet.
- **This is NOT a strength claim.** Per the objective's success bar, v145 does
  not yet achieve "significant positive EV vs the strongest classic pool." That
  requires either (a) more/better data so the model finds higher-value spots,
  or (b) a cross-hand opponent encoder, or (c) a calibrated gate that can act
  on 70%-accurate predictions safely.

## Reproduction

```bash
# Data (49 min nohup):
nohup python bots/neural_national_lab/tools/longrun_collect_oppmodel.py \
  --candidate bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp \
  --out-dir bots/neural_national_lab/data/oppmodel/longrun --passes 40 --workers 3 &

# Train classification (GPU):
python bots/neural_national_lab/tools/train_opponent_value_net.py \
  --data bots/neural_national_lab/data/oppmodel/longrun/cf_train.jsonl \
  --val-data bots/neural_national_lab/data/oppmodel/longrun/cf_val.jsonl \
  --out weights/opp_value_gru_cls.json --task classification \
  --hidden 96 --gru-hidden 48 --epochs 12 --device cuda --seed 4002

# Ablation (local native TCP):
python bots/neural_national_lab/tools/native_tcp_evaluate.py \
  --candidate bots/neural_national_lab/versions/v145_national_v140_gru_cls_override_tcp \
  --opponent bots/national_v46 --opponent bots/national_v120 \
  --opponent bots/national_v119 --opponent bots/national_v135 \
  --hands 70 --matches 2 --seed-base 5200 --paired --bot-seed-base 1000
```
