# Neural National v146-v150 Re-audit

Date: 2026-07-10

## Decision

v146 does not meet the neural-line success criteria and must not be merged as a
successful candidate. v147-v150 are protocol, diagnosis, and integration
versions; none is a strength-qualified replacement yet.

## Current-pool result

The pool was read from the live `.evolution_pok` conservative Glicko snapshot:
`national_v119`, `v72`, `v57`, `v66`, `v120`, `v141`, and `v142`.

The comparison used identical 70-hand paired seats, nine deck seeds, and exact
bot seeds for v140 and v146. Each version ran 63 paired rows / 8,820 hands with
zero illegal actions, timeouts, adapters, or wrappers.

| Result | v140 | v146 | v146-v140 |
|---|---:|---:|---:|
| Total chips | +183,898 | +306,568 | +122,670 |
| Chips/hand | +20.85 | +34.76 | +13.91 |

The positive point estimate is not statistically established:

- ordinary paired-row bootstrap CI: `[-3387.871, +7206.747]`;
- opponent-stratified bootstrap CI: `[-3086.610, +6974.093]`.

The direct v146 result against `national_v141` was `-109,652`, or about
`-87.0 chips/hand`. This is a clear nemesis failure.

Evidence is under
`bots/neural_national_lab/data/v146_livepool_20260710/`, including exact v140
and v146 reports, the paired difference report, and first-divergence traces.

## Invalidated assumptions

1. The reported reproduction path `data/oppmodel/combined/cf_train.jsonl` does
   not exist in the committed branch. The available long-run data has 2,030
   train, 358 validation, and 337 held-out rows.
2. The old split rotated the same opponents through train/validation/held-out;
   it was not an opponent-held-out test.
3. The old collector read the wrong ratings path and expected `r` while the
   live file uses `rating`, so it silently used a fallback pool.
4. Two-hand probes and earliest-decision selection provided almost no genuine
   middle/late-match supervision for the cross-hand encoder.
5. The trainer exported metadata too weak to reproduce the original model
   byte-for-byte and originally exported the final epoch rather than the best
   validation checkpoint.
6. The trainer sent non-empty histories through 16 GRU steps with zero padding;
   the native runtime used only observed steps. A one-action history produced a
   maximum probability difference of `0.0493`. The corrected implementation is
   within about `2e-7` for history lengths 0, 1, 3, 8, 15, and 16.

## Version outcomes

- **v147**: fixes numeric TCP stream fragmentation. A trailing `raise 2` or
  `earnChips -1` is held through a short quiet window so a split numeric suffix
  cannot corrupt `raise 200` or `earnChips -100`.
- **v148**: preserves valid zero-valued PFR/aggression instead of applying the
  `value or 0.5` default. It improved one v119 replay but did not remove the
  v141/v57/v66 failures.
- **v149**: reproduces the old padded training graph at runtime. Worst-seed
  paired results remained v141 `-39,241`, v119 `-20,022`, v57 `-2,960`, and
  v66 `-3,268`. Deployment drift was real but not the main strength failure.
- **v150**: integrates a 742,506-parameter multi-task model in shadow mode. It
  leaves v149 actions unchanged and is not a strength candidate.

## Replacement data path

The revised native counterfactual path now:

- reads the live Glicko pool and accepts only content present in an annotated
  `national-bot-vN` tag;
- holds opponents in stable train/validation/held-out partitions;
- runs complete 70-hand matches and samples decisions uniformly across hand
  windows;
- records hand, tail, and full-match deltas;
- confirms the forced action in the candidate decision trace;
- excludes illegal/noncompliant forced labels;
- emits leakage-free hero-action to immediate-opponent-response rows;
- limits global native-match concurrency to four;
- uses an exclusive lock, content contract, idempotent decision keys, and
  pass-level resume state.

The old three-opponent benchmark needed 147 seconds for six value rows. The
revised probe needed 85 seconds and also emitted 177 opponent-response rows.
The first six-opponent large-data pass emitted 48 value and 359 behavior rows
in 531 seconds. A resumed second pass correctly skipped eight already-written
rows and added 40 value plus 300 behavior rows in 383 seconds.

The initial long dataset under `matchscope_v150_large/` was stopped after an
action-coverage audit found that fixed-order truncation never probed
`raise_2pot` or `allin` as alternatives. It remains diagnostic evidence only.
The active balanced dataset is
`bots/neural_national_lab/data/oppmodel/matchscope_v151_balanced/`. It rotates
two alternatives deterministically across every non-rule action class. Its fixed
partitions are:

- train includes the live pool and the v141 nemesis;
- validation: v142 and v98;
- held-out: v57 and v66.

## Multi-task model path

`train_opponent_multitask_net.py` trains a shared state + intra-hand GRU +
cross-hand opponent encoder with:

- hand, tail, and match mean-value heads;
- lower-quantile heads used for risk-aware decisions;
- opponent fold/check/call/raise/allin prediction;
- opponent raise-size prediction.

The corrected training/runtime contract additionally:

- conditions every relative-value prediction on the rule action one-hot;
- masks private-card state features from OpponentActionNet;
- uses a separate public-state response MLP to avoid value-task interference;
- restores the best validation checkpoint and exports complete data hashes;
- reserves v98 for architecture/policy selection and v142 for lower-quantile
  plus response-probability calibration;
- uses v57/v66 only after selection and calibration are frozen;
- supports a multi-seed ensemble whose LCB combines calibrated member lower
  bounds with `mean - standard_deviation`;
- freezes runtime policy weights/margins from an offline bootstrap search and
  emits no-response and mean-only ablations.

The pure-Python runtime matches Torch output in tests. A preliminary 32-value
row / 240-behavior row smoke compared 57k, 200k, and 742k parameter models over
two seeds. The large model had the best median validation score and lowest seed
variance, but held-out response behavior collapsed. This only justifies
continuing the large-model experiment; it is not strength evidence.

The 742k runtime loads its 15MB JSON in about 0.13 seconds and runs value plus
response inference in about 0.058 seconds. A native TCP shadow smoke had a
maximum server-observed action latency of 0.196 seconds versus the 60-second
budget, with zero illegal actions, timeouts, adapters, or wrappers. A paired
four-hand check produced an identical per-hand vector for v149 and v150 while
recording multi-task shadow telemetry.

## Remaining gates

Do not activate or certify a model until all of these hold:

1. freeze and audit the scaled opponent-disjoint dataset;
2. select architecture and seed on train/validation only, then open held-out;
3. create a new active child with uncertainty-driven policy and legal fallback;
4. show neural, cross-hand, match/lower-head, and response-head ablations;
5. beat the real-time completed classic pool with ordinary and stratified CI
   lower bounds above zero and at least `+5 chips/hand` incremental EV;
6. show no negative-mean core nemesis, especially v141;
7. pass full official EXE acceptance only after the strength gates pass.
