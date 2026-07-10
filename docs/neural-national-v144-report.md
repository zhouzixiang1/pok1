# Neural National v144 Report — GRU Opponent-Aware Value Network

Date: 2026-07-10

This report documents the first version in the neural line built around a
**genuine opponent-aware sequence model** (a GRU over the observable per-hand
action history), moving past the hand-crafted VPIP/PFR profile gates of
v027-v140. The objective explicitly required this: build the data ->
GPU-training -> pure-Python-runtime -> native-TCP-shadow pipeline end-to-end,
validated for correctness, before any active override.

## What was built

Three new tools and one new bot version:

| Artifact | Purpose |
|---|---|
| `tools/train_opponent_value_net.py` | PyTorch GRU value-net trainer (GPU) |
| `tools/opp_value_runtime.py` | Pure-Python (stdlib) GRU forward pass for runtime |
| `tools/collect_oppmodel_data.py`, `tools/collect_oppmodel_parallel.py` | Native-TCP counterfactual data collectors |
| `versions/v144_..._gru_opp_value_shadow_tcp/` | v140 + GRU value net in shadow mode |

### Model architecture (OpponentAwareValueNet)

```
state features (48-d encode_features)
+ opponent profile (12-d: confidence, aggression, preflop/postflop raise rate, ...)
+ GRU embedding of observable action history (15-d/action x up to 16 steps, gru_hidden=48)
  -> Linear(48+12+48, 96) -> ReLU -> Dropout
  -> Linear(96, 96) -> ReLU -> Dropout
  -> Linear(96, 6)   # per-legal-action value = chip-EV delta vs rule action
```

29,718 parameters. The GRU input encodes each observed action (mine or
opponent's) as: street one-hot (4) + action-type one-hot (5) + normalized
stage-bet/action/committed/pot (4) + is_raise/is_allin flags (2).

### Training

- Device: CUDA (RTX 4060). MSE on counterfactual chip-delta targets, NaN-masked
  (unprobed legal actions are masked out, not zeroed), gradient-clipped at 1.0.
- Data: 241 train / 28 val / 36 held-out counterfactual rows from native TCP
  paired probes. Opponents span the realtime strong classic pool (v135, v114,
  v73, v63, v121, v122, v54, v70), the old pool (v2, v3, v5, v7, v8, v9, v14,
  v16), and held-out bots (v66, v40, v98, v39, v53, v119, v120, v123).
- Best model: `opp_value_gru_h96_g48_seed2401.json`, validation MAE 317
  (chip-delta units).

### Runtime (no torch at runtime)

The trained weights are exported to JSON (`opp_value_gru_v1` format). At
runtime, `opp_value_runtime.py` runs a **deterministic pure-Python GRU forward
pass** — no torch, no numpy, no network access. This was verified to match the
PyTorch forward pass **exactly** (max abs diff = 0.000000 over 20
history-bearing samples).

### v144 native TCP integration

v144 is v140 plus a config-driven GRU hook in `neural_policy.py`:

- `gru_opp_value_shadow: true` (default): run the network, emit
  `GRU_OPP_SHADOW` predictions to stderr, return None (no action change).
- `gru_opp_value_override: false` (default): override is OFF. When on, it only
  switches to a higher-value candidate if the candidate clears both an absolute
  floor (`min_best_value`) and a margin over the rule action
  (`min_margin_vs_rule`), the candidate is a small raise (not fold/allin), and
  it passes `_candidate_action` + a magnitude cap. The hook can never emit an
  illegal action.

## Verification

| Check | Result |
|---|---|
| py_compile (all v144 .py) | OK |
| `check_native_contract` | 0 errors |
| Pure-Python GRU == torch forward | maxdiff 0.000000 (PASS) |
| v144 shadow == v140 (same play) | identical: both -33 vs v135 seed5000 paired |
| GRU runtime load + predict | OK (6-d value vector produced) |

## Honest status and limitations

1. **Data volume is small** (241 train rows). The validation MAE (317) is
   noisy and the model overfits by epoch ~8. This is the single biggest
   limitation. Data scaling was attempted (a parallel collector with process
   isolation was built and validated) but the sandboxed environment repeatedly
   killed long-running background collection, capping throughput at ~2-hand
   sequential probes. Scaling to thousands of rows requires a longer
   unattended collection run outside this session.

2. **Override is intentionally OFF.** The success bar requires "significant
   positive EV vs the realtime strongest classic pool with CI not crossing
   zero." That is not claimable from 241 rows. The override gate stays off
   until the model is retrained on a larger dataset and shown to improve
   held-out EV without regressions. v144 therefore matches v140 in play
   strength (shadow only).

3. **What IS delivered**: the complete opponent-aware neural pipeline
   (sequence-based opponent encoder, GPU training, exact pure-Python runtime
   distillation, native TCP shadow integration, train/val/held-out data
   splits) — validated end-to-end for correctness. This is the foundation
   that a larger data run turns into a strength candidate.

## Reproduction

```bash
# Collect data (native TCP, ephemeral ports, process-isolated):
python bots/neural_national_lab/tools/collect_oppmodel_parallel.py \
  --candidate bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp \
  --out-dir bots/neural_national_lab/data/oppmodel/large1 \
  --hands 8 --repeats 4 --workers 4 --timeout-sec 70

# Train (GPU):
python bots/neural_national_lab/tools/train_opponent_value_net.py \
  --data bots/neural_national_lab/data/oppmodel/large1/cf_train.jsonl \
  --val-data bots/neural_national_lab/data/oppmodel/large1/cf_val.jsonl \
  --out bots/neural_national_lab/data/oppmodel/weights/opp_value_gru.json \
  --hidden 96 --gru-hidden 48 --epochs 80 --device cuda --seed 2401

# Verify runtime == torch:
python bots/neural_national_lab/tools/train_opponent_value_net.py --help  # see opp_value_runtime test inline

# Shadow eval (local native TCP):
python bots/neural_national_lab/tools/native_tcp_evaluate.py \
  --candidate bots/neural_national_lab/versions/v144_national_v140_gru_opp_value_shadow_tcp \
  --opponent bots/national_v135 --hands 70 --seeds 5000 --paired \
  --bot-seed-base 1000 --bot-seed-stride 1
```
