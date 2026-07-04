# Neural National Lab

Neural-enhanced national bot experiments.

- `versions/`: complete runnable bot snapshots. Add a new directory for each
  experiment version.
- `tools/`: teacher-data collection, tiny-MLP training, and mirror evaluation.
- `data/`: small generated datasets, metrics, and evaluation reports.
- `external/`: ignored shallow clones for research scans.

Runtime bots stay stdlib-only. Training may use PyTorch/NumPy. The neural model
acts after the rule strategy and before `sanitize_action`; native TCP output is
still produced by the national entrypoint.

## Scale-Up Gate

Do not move straight from whole-match outcome labels to large-scale training.
The current reliable next step is hand-scoped counterfactual rollout:

```bash
python bots/neural_national_lab/tools/counterfactual_rollout_probe.py \
  --version bots/neural_national_lab/versions/v022_v254_sharded_h96_raise_only254 \
  --opponent bots/claude_v279 \
  --games 3 \
  --max-probes 24 \
  --max-scan-decisions 400 \
  --kind to_raise \
  --branch-scope hand \
  --output bots/neural_national_lab/data/counterfactual_v022_vs_v279_g3_p24.json
```

As of the first probe run, v022 flop `to_raise` interventions have positive
small-sample hand deltas, but the sample is still too small for a large-scale
training decision. Move toward large scale only after all of these hold:

- at least 100 hand-scoped counterfactual probes for the target intervention;
- positive mean primary delta with a 95 percent confidence interval above zero;
- no obvious negative bucket by street, action type, or price band;
- candidate collection can be sharded/replayed without relying on slow full
  match rescans.

`counterfactual_rollout_probe.py` now uses a persistent process for the scanned
match by default and applies a cheap neural/street/confidence prefilter before
running the expensive rule-strategy analysis. Use `--no-scan-persistent` only
when debugging subprocess-state issues.

Run street-specific probes before training a gate. For example, v022 permits
both preflop and flop raise interventions, so flop-positive evidence must not
be blended with preflop data until the preflop bucket is independently checked.

For reproducible scale-up sampling, use deterministic seeds and shard outputs:

```bash
python bots/neural_national_lab/tools/counterfactual_shard_runner.py \
  --version bots/neural_national_lab/versions/v022_v254_sharded_h96_raise_only254 \
  --opponent bots/claude_v279 \
  --shards 8 \
  --workers 4 \
  --games-per-shard 3 \
  --max-probes-per-shard 8 \
  --stage flop \
  --kind to_raise \
  --seed-base 20260704 \
  --bot-seed-base 202607040000 \
  --output bots/neural_national_lab/data/counterfactual_v022_flop_seeded_shards.json
```

The shard runner writes per-shard JSON files next to the merged output, so a
negative bucket or suspicious outlier can be replayed with the same seed range.
Existing shard files are reused by default; pass `--rerun-existing` when the
probe code or filters changed and the shards should be regenerated. Increase
`--workers` to run independent shard subprocesses in parallel. Pass
`--bot-seed-base` when either bot uses process-local randomness; the probe
runner seeds the scanned match and gives each baseline/candidate forced branch
the same branch-local bot RNG seeds, so action-value labels are replayable.
The first seeded smoke run (`seed_base=2026070401`, 2 shards, 4 total probes)
found flop `to_raise` mean primary delta `-64.25` with a very wide confidence
interval. Treat that as a warning against shipping or training from optimistic
unseeded micro-samples; it is not enough evidence to reject the bucket by
itself.

A larger parallel smoke run (`seed_base=2026070410`, 8 shards, 10 total probes)
found flop `to_raise` mean primary delta `-5.8` with 95 percent CI
`[-179.39, 167.79]`. The largest negative outlier used `advised_final=200`,
while most `advised_final=101` probes were neutral or positive, so scale-up
analysis should check `by_advised_final` and `by_raise_delta_band` before
training or shipping a raise gate.

`versions/v025_v254_low_raise_gate_h96_254` is a narrow v022 fork created from
that observation. It keeps the same h96 policy weights but limits neural raise
interventions to low-size raises (`max_raise_delta=125`,
`max_raise_pot_ratio=0.65`). It is an experiment candidate, not a validated
successor, until paired battles and larger counterfactual shards are positive.
On the same p16 seed range, v025 produced 9 low-raise flop probes with mean
primary delta `+111.11` and no high-raise probes. A one-pair common-deck mirror
smoke against `claude_v279` was roughly neutral versus v022 (`-82` chips over
140 hands). This supports keeping v025 for larger tests, not promoting it.

The larger p120 deterministic shard run (`seed_base=2026070500`, 40 shards)
crossed the counterfactual scale-up gate: 108 hand-scoped flop `to_raise`
probes, mean primary delta `+90.68`, 95 percent CI `[69.62, 111.73]`, and all
probes in `raise_le_125`. A two-pair common-deck mirror against `claude_v279`
was still inconclusive versus v022 (`[-9095, +14398]` chips over paired
140-hand samples), so the next step is more paired battle volume before calling
v025 a clear performance improvement.

`paired_evaluate.py` supports deterministic deck seeds, optional bot-subprocess
RNG seeds, parallel workers, and `--resume` for extending an existing seeded
run without replaying completed pairs. Use `--seed-base` to fix the local judge
decks and `--bot-seed-base` when the compared bots use Python `random` inside
their simulation code. The 16-pair seeded mirror run (`seed_base=2026070600`,
`workers=8`) kept v025 positive on average versus v022 against `claude_v279`
(`+796.44` chips per 70 hands), but the 95 percent CI still crossed zero
(`[-2089.29, 3682.16]`). Extending the same run to 32 pairs improved the mean
to `+1170.11` chips per 70 hands, but the 95 percent CI still crossed zero
(`[-528.02, 2868.23]`). Extending again to 64 pairs reduced the mean to
`+686.17` chips per 70 hands with 95 percent CI `[-602.07, 1974.41]`. v025
remains promising at the counterfactual-action level, but it is not a clear
end-to-end winner.

A 4-pair smoke with both deck and bot RNG seeds (`seed_base=2026070800`,
`bot_seed_base=202607080000`) produced byte-identical JSON on rerun, confirming
the seeded subprocess path is suitable for larger deterministic evaluation.
Extending that deterministic setup to 32 pairs made the v025 signal essentially
neutral versus v022: `+95.09` chips per 70 hands, 95 percent CI
`[-701.02, 891.21]`, median paired delta `0`, with 11 positive, 11 zero, and
10 negative samples. Do not spend the next scale step on more v025 threshold
tuning; move to better action-value targets.

The counterfactual target generator now also supports deck plus bot RNG seeds.
A v025 flop `to_raise` smoke (`seed_base=2026070900`,
`bot_seed_base=202607090000`, 2 probes) reran byte-identical and wrote scan
`bot_seeds` plus per-probe `branch_bot_seeds`. This is the minimum data
contract before larger sampling: expand probes only from reproducible
action-value labels, not from unseeded trace outliers.

`versions/v026_v254_flop_low_raise_h96_254` narrows v025 to the only bucket
covered by the p120 counterfactual evidence: low-size flop free-action raises.
The direct 16-pair check against v025 was positive on average (`+1133.59` chips
per 70 hands), but the 95 percent CI still crossed zero
(`[-1027.52, 3294.71]`). A direct 32-pair check against v022 was slightly
negative (`-301.33` chips per 70 hands, 95 percent CI
`[-2241.13, 1638.48]`). v026 is therefore a recorded experiment, not a
successor.

The first counterfactual-trained advantage branch is also only a recorded
experiment. `counterfactual_rollout_probe.py` now exports the same 70-dimension
advantage features used by runtime gates and seeds the local analysis RNG in
addition to deck and bot subprocess RNG. A 62-probe p64 run for v025 flop
low-raise labels stayed positive (`+59.13`, 95 percent CI `[30.97, 87.29]`),
but the h32 gate trained from it did not transfer: v027 lost slightly to v025
over 16 paired mirrors (`-107.28` chips per 70 hands), and v028 with a lower
gate threshold was also slightly negative over 8 pairs. The next scale step
needs broader counterfactual coverage, especially examples that prevent the
gate from blocking rare high-value v025 raises.

`versions/v029_v254_cf_advantage_pair11_h32_t090` adds the worst v027 outlier
seed back into the counterfactual training set. That repaired the specific
over-filtering failure: v029 restored v025's 3 neural raises on the outlier
trace. It still did not beat v025 over 16 paired mirrors (`-2.25` chips per
70 hands, 95 percent CI `[-12.45, 7.95]`). Treat v029 as a diagnostic repair,
not a stronger bot.

The next broader dataset, `counterfactual_v025_flop_botrng_p192.json`, added
190 reproducible flop low-raise probes with mean primary delta `+95.74` and
95 percent CI `[79.05, 112.42]`. Combining it with the earlier p64 and pair11
focus runs produced 255 runtime-compatible advantage rows. The nonnegative
h32 gate trained from those rows became `v030_v254_cf_nonneg_gate_h32_t085`.
It was positive versus v025 after 16 paired mirrors, but extending the same
deck plus bot-RNG run to 64 pairs reduced the edge to `+189.90` chips per
70 hands with 95 percent CI `[-484.94, 864.74]` and median delta `0`. This is
enough evidence to scale data collection, not enough evidence to scale model
size or promote the bot.

The drop-zero follow-up (`v032`/`v033`) tested whether removing zero-delta
training rows would avoid over-suppressing useful raises. It did not produce a
promotable bot: `v032_v254_cf_dropzero_h16_t095` repaired some small positives
but had a 16-pair result of only `+233.56` chips per 70 hands with 95 percent
CI `[-2109.38, 2576.51]`; `v033_v254_cf_dropzero_h32_t050` was negative
(`-242.09`, 95 percent CI `[-590.94, 106.75]`). A targeted p257 repair
(`v034_v254_cf_pair11_p257_h32_t090`) fixed one catastrophic over-filtered
seed, but its 16-pair result still crossed zero (`+693.72`, 95 percent CI
`[-738.72, 2126.16]`) and had a negative median. The new lesson is that
single-action hand-scoped labels are not enough: some seeds need grouped or
match-scope interaction tests because locally positive raises can combine into
bad match paths.

`build_group_interaction_advantage_data.py` adds the first reusable path for
that issue: it maps paired mirror deltas back onto probe features so grouped
failures can become training rows. The first p260 attempt (`v035`-`v037`) found
a threshold conflict rather than a new winner. `v035`/`v037` repaired pair03
but destroyed the pair11 repair, while `v036` preserved pair11 but left pair03
unchanged. This means grouped labels should probably become a separate
interaction-veto head, not more examples inside the same single-action
advantage classifier.

`v038` and `v039` implemented that separate interaction-veto head. They used
the p260 grouped model after the main p257 advantage gate, with `v039` adding
an interaction confidence floor so lower-confidence useful repairs could skip
the second gate. This fixed the pair03 disaster (`v034` was `-5699` raw chips
versus v025 on the targeted pair; `v039` was `-227`) but exposed a new pair11
path failure (`v039` was `-19119` raw). The parallel trace tool now exports the
70-dimension advantage feature vector plus both gate scores for every candidate
and actual intervention, so these failures can become direct training rows
instead of hand-written threshold rules.

`v040_v254_cf_trace_veto_p266_h32_t070` trained an interaction-veto model on
the p260 rows plus six trace-supervised rows: the three pair03 `v034` raises as
negative examples, the two pair11 `v034` repairs as positives, and the pair11
`v039` mirror hand55 raise as a negative. It repaired the exact pair11 collapse
from `-19119` raw to `-14` raw while preserving the pair03 fix (`-227` raw).
Over 16 deterministic common-deck mirror pairs against `claude_v279`, v040 was
`+344.56` chips per 70 hands versus v025 with 95 percent CI
`[-348.15, 1037.28]`. This is a real repair and a better candidate than v039,
but still not a statistically clear successor.

`v041_v254_cf_ensemble_veto_p266_h32_m070_min040` tested a four-seed
interaction ensemble. High-confidence actions had to pass both mean score
`>=0.70` and member minimum `>=0.40`; lower-confidence actions still used the
v040 bypass. The ensemble repaired one small negative 16-pair sample
(`pair5: -185 -> +6` raw delta) but introduced a matching small regression
(`pair15: 0 -> -200` raw delta). Its 16-pair result was effectively unchanged
from v040: `+344.31` chips per 70 hands, 95 percent CI
`[-348.48, 1037.05]`.

`v042_v254_cf_ensemble_veto_p266_h32_m063_min050` tried to resolve that
pair5/pair15 boundary by lowering the mean threshold to `0.63` and raising the
member minimum to `0.50`. It preserved pair03 and pair5 and restored pair15,
but broke pair11 (`17542 -> -3594` raw trace result). This confirms the current
feature set is under-specified around high-confidence mirror raises: tiny
threshold movements can flip different seeds in opposite directions. Do not
scale this ensemble threshold family further without adding better context
features or a branch-level value target for mirror-side high-confidence raises.

`v043_v254_cf_handstrength_veto_p268_h32_t050` adds those missing local
context features to the interaction-veto head. The augmented dataset appends
18 runtime-computable hand-strength features, including made-hand class, hole
card participation, pair/trips/two-pair flags, flush pressure, straight draw
density, and board pairing. It also adds two trace-supervised rows from the
v040 pair5/pair15 boundary and trains three h32 seeds on the p268 augmented
set; seed270 became the deployed model. Exact paired checks preserved the
known repaired seeds and improved the pair5 boundary (`-185` raw in v040 to
`+63` raw) without reintroducing the pair15 regression. Over the same 16
common-deck mirror pairs against `claude_v279`, v043 was `+971.44` chips per
70 hands versus v025 with 95 percent CI `[-376.65, 2319.52]`. That is a
stronger signal than v040/v041, but still not statistically clear because the
interval crosses zero and one very large positive pair contributes heavily.
The 64-pair multi-worker rerun confirmed that caution: v043 dropped to
`+221.27` chips per 70 hands versus v025 with 95 percent CI
`[-350.43, 792.97]` and a `30/8/26` positive/zero/negative split. Against v040
on the same 64 seeds, v043 was only `+173.95` chips per 70 hands with 95
percent CI `[-128.07, 475.98]`; 53 of 64 pairs were identical. The largest
v025-relative negative outliers were pair18 (`-20407` raw), pair23 (`-11119`),
and pair47 (`-6790`). Exact bot-RNG trace files for those outliers show actual
neural raises in the bad paths, but v043 is not a promotion candidate. The next
experiment should convert those exact trace contexts into match-scope
interaction rows and require better robustness before increasing model size.

`v044` through `v047` tested that match-scope idea and rejected the first
implementation. `v044_v254_cf_handstrength_veto_allconf_t050` kept the v043
model but removed the low-confidence interaction bypass; targeted checks showed
no useful repair. The p278 match-scope dataset then added 10 exact trace rows:
pair6/pair23 as positive contexts and pair42/pair47/pair21/pair28 as negative
contexts. Single replacement models (`v045` seed282 at threshold `0.30` and
`v046` seed280 at threshold `0.25`) both preserved pair6 but broke pair23 by
allowing or blocking different mirror-side paths. `v047` kept the v043
interaction gate and added the p278 model as a second veto, but its 64-pair
run versus v043 was negative: `-306.10` chips per 70 hands with 95 percent CI
`[-729.15, 116.95]` and a `6/51/7` positive/zero/negative split. The practical
lesson is that weak pair-level deltas are not reliable labels for individual
flop probe actions. Future match-scope training needs trajectory-level credit
assignment, or at minimum counterfactual reruns that isolate one candidate
change at a time before it becomes a supervised row.

`counterfactual_rollout_probe.py` now also supports direct game/side-level
process parallelism with `--workers` and `--max-probes-per-task`. The worker
boundary is one deterministic game side, so each process owns its bot module
load, local judge state, and bot subprocesses; the parent only merges probes
and writes JSON. On this 32-thread machine, an 8-worker smoke run over v043
produced 9 isolated hand-scope probes with mean primary delta `+144.33`, and a
16-worker follow-up produced 13 more probes with mean `+86.00`. The merged
22-row training set had 13 positives, 9 non-positives, and input dimension 70.
Training ran on CUDA (`NVIDIA GeForce RTX 4060 Laptop GPU`); h16/h8/h4 models
all had validation accuracy `0.60`, so h8 was selected for calibration rather
than capacity. `v048_v254_cf_isolated_veto_p022_h8_t080` used h8 with
`advantage_min=0.8`; it failed end-to-end versus v043 over 32 paired mirrors
(`-125.45` chips per 70 hands, 95 percent CI `[-1016.44, 765.53]`) due to
large negative outliers. `v049_v254_cf_isolated_veto_p022_h8_t090` raised the
threshold to `0.9`; it improved the 32-pair sample but did not survive a
64-pair extension (`+70.88` chips per 70 hands, 95 percent CI
`[-556.85, 698.60]`). Keep v048/v049 as artifacts proving the isolated-label
pipeline and multicore sampler, not as stronger bots. The next attempt needs
more isolated probes around the newly exposed negative outliers, not a lower
threshold on the same 22-row dataset.

`v050_v254_cf_isolated_aux_veto_p033_h8_b040` keeps the v043 hand-strength
gate and adds the p33 isolated model only as a conservative auxiliary veto:
block only when the original gate is very confident and the p33 model scores
the action below `0.40`. The v049 64-pair failure was traced to missing v043
rescue raises on exact seeds, especially pair42 and pair51; a replacement
isolated gate blocked those raises instead of repairing bad extra actions.
The follow-up hand-scope shard over pairs42..52 added 11 probes and looked
positive (`+99.91`, 95 percent CI `[29.89, 169.93]`), but the match-scope
rerun on the same area was not significant (`+14.78`, 95 percent CI
`[-252.71, 282.27]`). The p33 and p42 auxiliary datasets both trained on CUDA
but had weak validation accuracy (`0.57` and `0.56`). v050 preserved the traced
pair42/pair51 rescue actions and avoided v049's catastrophic paths, but its
64-pair evaluation versus v043 was essentially neutral: `-0.91` chips per
70 hands with 95 percent CI `[-2.48, 0.67]`. Treat v050 as a safety experiment,
not a promotion. The next useful step is trajectory-level credit assignment,
or targeted match-scope branch data, before changing the runtime gate again.

`v051_v254_cf_matchscope_aux_veto_p049_h16_b030` tested that next step in a
small form. A 24-worker match-scope probe run was stopped after valid partial
output because full-match branch sampling was too slow; it still produced 49
usable single-action full-match probes. The important result was the split
between scopes: the same probes had hand_delta mean `+93.78` with 95 percent CI
`[61.69, 125.86]`, but match_delta mean `-603.04` with 95 percent CI
`[-1957.60, 751.52]`. h8/h16/h32 CUDA classifiers trained from those rows had
validation accuracy `0.50`, `0.50`, and `0.40`. The h16 model was nevertheless
wrapped as a very conservative auxiliary veto at threshold `0.30`; targeted
trace on pairs42..52 preserved the known pair42/pair51 rescue raises and did
not reintroduce v049's pair52 extra raise. End-to-end paired evaluation still
rejected it: v051 versus v043 over 64 mirror pairs was `-1.32` chips per
70 hands with 95 percent CI `[-434.58, 431.94]`. The conclusion is not that
match-scope labels are useless; it is that a tiny binary classifier over sparse
single-action full-match deltas is still too noisy. The next implementation
should collect paired trajectory credit/value targets before another runtime
gate.

`v052_v254_cf_value_veto_mix_p063_h32_bm050` tried that value-target variant.
`train_value_gate.py` trains a tiny one-hidden-layer regressor on
`tanh(delta / 1000)` instead of a binary good/bad label and exports the same
JSON-only stdlib runtime artifact shape as the previous gates. The first p49
v279-only value models were too weak, so a 16-worker v043 versus `claude_v283`
match-scope probe was attempted with early stopping. Probe density was low:
after 140 completed match-side tasks it had only 14 probes, so the valid
partial output was kept but not expanded. Merging those rows with the existing
p49 v279 rows produced a 63-row mixed dataset with 25 positive and 38
non-positive labels. The h32 CUDA value model had validation sign accuracy
`0.77`, validation MAE `0.62`, and average prediction `-0.053`, so it was
wrapped as a high-confidence auxiliary veto: only neural actions with policy
confidence at least `0.94` can be blocked, and only when the predicted value is
below `-0.5`.

The targeted trace looked useful but the end-to-end gate still did not promote.
On pairs42..52 against `claude_v279`, v052 improved the traced aggregate from
v051's `-6816` to `-4986` chips and repaired pair48 from `-1602` to `+125`
while preserving the earlier pair42/pair51/pair52 behavior. The broader
64-pair deterministic mirror run versus v043 was effectively neutral:
`-11.47` chips per 70 hands with 95 percent CI `[-68.17, 45.24]`. The nonzero
paired deltas were pair21 `-3243`, pair29 `+150`, pair38 `-102`, and pair48
`+1727`. Replaying pair21 with the exact paired bot RNG seeds showed
`final_changed=0`, so that large loss is not a value-veto action; it is an
inherited strategy-path difference relative to v043. Keep v052 as the first
value-regression artifact, not as a stronger bot. The next iteration should
train and evaluate against the immediate parent as well as v043, then use
paired trajectory credit or an ensemble/variance-aware value target before
adding another runtime gate.

Recent literature and open-source scans point to the next useful local
direction:

- Deep CFR and Single Deep CFR use neural approximators for cumulative
  counterfactual regrets/strategies rather than a one-shot policy classifier.
  Full adoption would require a dedicated traversal environment, but the
  immediate lesson is to train on replayable counterfactual advantages and to
  keep action-value targets separate from policy imitation.
- DeepStack and ReBeL both combine neural value estimates with search or
  re-solving at decision time. A full public-belief-state search is outside the
  current compact runtime, but the local analogue is a cheap value/veto head
  that only allows a neural action when the value evidence is robust.
- Pluribus showed that large off-tree search can be reduced by distilling into
  compact runtime policies. For this repo, any heavier PyTorch/GPU experiment
  should still distill back into JSON weights and stdlib-only runtime code.
- Robust Deep MCCFR-style work highlights variance, non-stationary labels, and
  action-support collapse as practical failure modes. The next experiment
  should therefore test seed ensembles or variance-aware gates before scaling a
  single p266 classifier.

References used for this direction include Deep CFR
(`https://arxiv.org/abs/1811.00164`), DeepStack
(`https://arxiv.org/abs/1701.01724`), ReBeL
(`https://arxiv.org/abs/2007.13544`), Pluribus
(`https://www.science.org/doi/10.1126/science.aay2400`), and the open-source
Deep-CFR/PokerRL/ReBeL implementations
(`https://github.com/EricSteinberger/Deep-CFR`,
`https://github.com/EricSteinberger/PokerRL`,
`https://github.com/facebookresearch/REBEL`).
The July 2026 follow-up scan also points to AutoCFR-style meta-search
(`https://www.sciencedirect.com/science/article/abs/pii/S0004370224001681`)
and Robust Deep MCCFR diagnostics
(`https://arxiv.org/abs/2509.00923`) as useful constraints: do not trust one
small neural gate unless it survives variance, target-shift, and action-support
checks across opponents.

This machine has 32 CPU threads, so deterministic paired evaluation and
counterfactual shards can use `--workers 12` to `--workers 16` for practical
speedups when the evolution daemon is quiet. During concurrent daemon runs,
keep ad-hoc paired evaluation lower, such as `--workers 4`, because each worker
launches multiple bot subprocesses and full saturation can increase timeout
risk and scheduling overhead.

For the current neural workflow, spend multicore budget on
`paired_evaluate.py`, `counterfactual_shard_runner.py`, and
`counterfactual_rollout_probe.py --workers`; do not use the trace tool as the
large-scale loop. Trace runs are for explaining exact outliers after the
parallel paired/counterfactual samplers identify them.

`multi_opponent_paired_evaluate.py` wraps `paired_evaluate.py` for the next
scale step: it runs the same baseline/candidate comparison against multiple
strong opponents, writes one resumable paired file per opponent, and writes an
aggregate JSON with concatenated paired deltas plus per-opponent splits. Use it
when a candidate looks neutral-positive against one bot but may be overfitted:

```bash
python bots/neural_national_lab/tools/multi_opponent_paired_evaluate.py \
  --baseline bots/neural_national_lab/versions/v043_v254_cf_handstrength_veto_p268_h32_t050 \
  --candidate bots/neural_national_lab/versions/v052_v254_cf_value_veto_mix_p063_h32_bm050 \
  --opponent bots/claude_v279 \
  --opponent bots/claude_v283 \
  --opponent bots/claude_v284 \
  --games 16 \
  --workers 8 \
  --seed-base 2026072200 \
  --bot-seed-base 202611220000 \
  --resume \
  --summary-output bots/neural_national_lab/data/multiopponent_v043_v052_seed2026072200.json
```

A smoke run with two paired samples each against `claude_v279`, `claude_v283`,
and `claude_v284` wrote
`data/multiopponent_v043_v052_vs_v279_v283_v284_seed2026072200_smoke.json`.
All six deltas were zero, which is useful tooling evidence: v052's conservative
value veto is too sparse to be a reliable next search direction unless active
sampling first finds states where it actually changes decisions.

`counterfactual_rollout_probe.py` now uses bounded parallel submission. With
`--workers > 1`, it only keeps one batch of worker tasks in flight and stops
submitting new game/side tasks once merged probes reach `--max-probes`; pass
`--no-parallel-early-stop` to force the old exhaustive behavior. Smoke
validation with `--workers 4 --max-probes 2 --max-probes-per-task 1` reached
2 probes after submitting 12 of 32 tasks and skipped the remaining 20. This
does not make match-scope labels less noisy, but it removes the main CPU waste
seen in the v051 partial run.

Move toward large scale in stages:

- now: larger reproducible counterfactual data shards for disputed gates,
  multiple opponents, active-learning outlier seeds, and grouped/match-scope
  checks for high-interaction seeds;
- after a candidate is positive over at least 64 paired mirrors with median
  above zero and no large negative bucket: larger offline value or advantage
  training on GPU;
- only after the larger model distills back into a compact JSON runtime and
  beats the rule base with a confidence interval above zero: broader promotion
  runs and national-platform stress testing.
