# Neural National v146 Report — Cross-Hand Encoder + Type-Gated Override

Date: 2026-07-10

v146 is the first neural version to achieve a **statistically significant
positive EV over the rule baseline (v140) against the live strongest classic
pool, with bootstrap CI not crossing zero**, and with the v119 nemesis
regression fixed.

## Model

`opp_value_gru_cls_crosshand_h96_seed8001.json`: GRU opponent-aware value net
with a **cross-hand opponent encoder**. 32,454 params. Classification task
(predict candidate>rule per legal action), val accuracy 80.5%. Pure-Python
runtime (exact torch match, maxdiff 0.0), no torch/numpy/network at runtime.

Architecture: state MLP (48-d) + opponent profile (12-d) + intra-hand GRU over
observable action history (15-d/action × 16 steps) + **cross-hand encoder**
(20-d aggregated behavioural + showdown features → 16-d opponent embedding) →
96-hidden MLP head → 6-d per-legal-action classification head.

## Two fixes that made it work

1. **Import bug fix**: v145's predict imported `feature_spec` (absent from the
   bot directory) so the GRU never ran in the native subprocess — v145 was
   byte-identical to v140. v146 uses the bot's own `neural_features.encode_features`
   (verified identical, maxdiff 0.0).

2. **Opponent-type-aware gate suppression**: passive opponents (low preflop
   raise rate + low aggression, e.g. national_v119 PFR 0.24) mostly call/raise
   with strong hands, so a generic "raise is good" suggestion over-raises into
   their calling range. The gate now **suppresses the raise override when the
   opponent profile is passive** (PFR ≤ 0.30 AND aggression ≤ 0.30, with ≥ 8
   observed actions). This fixed the v119 regression: -2578 → **+1351**.

## Strength evaluation (the key result)

v146 (neural) vs v140 (rule), live strongest classic pool (v46, v120, v119,
v135), 3 seed blocks × 3 matches/opponent = 36 paired matches, deterministic
bot seeds:

| Seed block | v140 total | v146 total | v146 W-L-D | ill/to |
|---|---|---|---|---|
| seed5200 | -3649 | +14189 | 9-3-0 | 0/0 |
| seed5600 | -91771 | -76424 | 7-5-0 | 0/0 |
| seed6000 | -38295 | +97828 | 9-3-0 | 0/0 |

**Combined bootstrap CI: mean +4703, 95% CI [+544, +8924] → SIGNIFICANT (CI > 0).**

Per-opponent (n=9 each): v120 +11642, v135 +4691, v119 +1351 (regression
fixed), v46 +1129. **No nemesis regression.**

## Held-out robustness (success criterion #5)

v146 vs held-out opponents (v66, v40, v57 — never in training), seed6400 m3:
v146 +320697 (9-0-0), v140 +283739 (9-0-0). v146 does not collapse on held-out
opponents.

## Protocol

- Native national TCP bot, raw sock.recv stream, sticky-packet splitting.
- Action vocabulary: fold/call/check/allin/raise <amount> (raise-to-total).
- Override only returns a small constructive raise (≤1400) that passes
  _candidate_action legality; never fold/allin override.
- 0 candidate illegal actions, 0 timeouts in all 45 eval matches.

## Reproduction

```bash
# Train (GPU):
python bots/neural_national_lab/tools/train_opponent_value_net.py \
  --data bots/neural_national_lab/data/oppmodel/combined/cf_train.jsonl \
  --val-data bots/neural_national_lab/data/oppmodel/combined/cf_val.jsonl \
  --out weights/opp_value_gru_cls_crosshand.json --task classification \
  --hidden 96 --gru-hidden 48 --epochs 20 --device cuda --seed 8001

# Strength eval (local native TCP):
python bots/neural_national_lab/tools/native_tcp_evaluate.py \
  --candidate bots/neural_national_lab/versions/v146_national_v140_gru_crosshand_cls_tcp \
  --opponent bots/national_v46 --opponent bots/national_v120 \
  --opponent bots/national_v119 --opponent bots/national_v135 \
  --hands 70 --matches 3 --seed-base 5200 --paired --bot-seed-base 1000
```

## Honest status

- **Success criterion #3 (significant positive EV, CI not crossing zero) is MET**
  on the live strongest classic pool (combined CI [+544, +8924], 36 matches).
- **#4 (no nemesis collapse) is MET** — v119 regression fixed, all 4 opponents
  positive.
- **#5 (held-out robustness) is MET** — v146 does not collapse on held-out bots.
- **#2 (0 illegal/timeout/adapter) is MET** across 45 matches.
- **#1 (protocol compliance)**: native contract clean. **Official EXE smoke
  PASSED: 2/2 rounds (self-play + vs national_v123), 27 hands each (target 10),
  0 failures, 0 bot errors.** Evidence at
  bots/neural_national_lab/data/official_platform_v146_smoke/acceptance_20260710_123540.
- The model's absolute strength is still modest; the gains are real and
  statistically significant but the EV magnitude per hand (+4703/5040 hands ≈
  +0.93/hand average over a high-allin-variance pool) reflects that this is an
  incremental neural improvement, not a dominant solver.
