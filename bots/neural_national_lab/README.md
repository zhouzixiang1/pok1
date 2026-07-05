# Neural National Lab

Neural-enhanced national bot experiments.

- `versions/`: complete runnable bot snapshots. Add a new directory for each
  experiment version.
- `tools/`: teacher-data collection, tiny-MLP training, and mirror evaluation.
- `data/`: small generated datasets, metrics, and evaluation reports.
- `external/`: ignored external poker-AI reference clones for research scans.

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
Two newer directions sharpen that into an engineering rule for this repo:
Real-Time Parallel CFR (`https://arxiv.org/abs/2605.19928`) uses CPU/GPU
parallelism and batched neural leaf evaluation to get more solving iterations
inside a fixed decision budget, while Deep Predictive Discounted CFR
(`https://arxiv.org/abs/2511.08174`) fits variance-reduced sampled advantages
with a value network and CFR-style discounting. The local analogue is active
sampling of decision-changing seeds plus variance-aware value targets, not
blind threshold tuning of a sparse gate.

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

`active_divergence_scan.py` is the active-sampling companion for that problem.
It replays a baseline and candidate against the same opponent, deck seed, and
bot RNG seeds, then records the first action divergence for our bot in each
normal/mirror side. Use explicit `--pair-index` values to reproduce known
paired outliers without scanning all earlier seeds. For broader collection,
use `--stop-after-divergence-pairs` so the sampler stops after enough
decision-changing rows instead of exhausting every requested seed:

```bash
python bots/neural_national_lab/tools/active_divergence_scan.py \
  --baseline bots/neural_national_lab/versions/v043_v254_cf_handstrength_veto_p268_h32_t050 \
  --candidate bots/neural_national_lab/versions/v052_v254_cf_value_veto_mix_p063_h32_bm050 \
  --opponent bots/claude_v279 \
  --pair-index 21 \
  --pair-index 48 \
  --workers 2 \
  --seed-base 2026071503 \
  --bot-seed-base 202609190000 \
  --output bots/neural_national_lab/data/divergence_v043_v052_vs_v279_pair21_48_seed2026071503_botrng.json
```

The early-stop path was smoke-tested on pair21/pair48 with target `1`; it
submitted one task, found pair21's divergence, skipped pair48, and wrote
`data/divergence_earlystop_v043_v052_vs_v279_pair21_48_target1_seed2026071503_botrng.json`.

That scan found real v043-v052 action divergence on both known nonzero seeds:
pair21 was `-3243` chips and pair48 was `+1727`. In both cases the first
divergence was the mirror-side flop free action after check-through preflop:
v043 raised to `101`, while v052 checked/called `0`. The same action template
therefore has opposite outcomes depending on cards and board, so v053 should
not be another global threshold move. It should collect active divergence rows
around this template and train a local value/variance model that can separate
the losing pair21 context from the winning pair48 context.

The remaining nonzero v052-v043 paired seeds against `claude_v279` were also
scanned: pair29 was `+150` and pair38 was `-102`, and both had the same first
divergence template (`raise 101 -> 0`) on the flop after check-through preflop.
`build_divergence_value_data.py` converts these active-divergence JSON files
into a compact supervised JSONL format for the next value model:

```bash
python bots/neural_national_lab/tools/build_divergence_value_data.py \
  --input bots/neural_national_lab/data/divergence_v043_v052_vs_v279_pair21_48_seed2026071503_botrng.json \
  --input bots/neural_national_lab/data/divergence_v043_v052_vs_v279_pair29_38_seed2026071503_botrng.json \
  --output bots/neural_national_lab/data/divergence_value_v043_v052_vs_v279_nonzero_p004_seed2026071503.jsonl \
  --summary-output bots/neural_national_lab/data/divergence_value_v043_v052_vs_v279_nonzero_p004_seed2026071503.summary.json
```

The first dataset has only 4 rows, input dimension 48, and a balanced `2/2`
positive/negative split. It is a data-contract artifact, not a training set.
Before v053, expand this template to at least 40-60 decision-changing rows
across `claude_v279`, `claude_v283`, and `claude_v284`, then train a local
value head or nearest-neighbor veto and evaluate it with
`multi_opponent_paired_evaluate.py`.

The first cross-opponent expansion shows why this must be active rather than
blind. Replaying the same pair indices (`21/29/38/48`) against `claude_v283`
and `claude_v284` produced only one extra divergence row, `claude_v283` pair21,
and its paired delta was exactly zero. The merged dataset
`divergence_value_v043_v052_crossopponent_p005_seed2026071503.jsonl` therefore
has only 5 rows: 2 positive, 1 zero, and 2 negative. A fresh `claude_v279`
search over 8 new seeded pairs (`seed_base=2026072300`) found no divergences at
all. The next scale step should not spend full match-delta scans on broad
blind ranges; add or use a cheaper action-divergence prefilter to find seeds
where v043 and v052 actually choose different actions, then run full delta
labeling only on those hits.

`action_divergence_prefilter.py` is that cheaper prefilter. It synchronously
replays baseline and candidate only until our first action differs, then stops
without computing final paired chip deltas. Use it to find candidate seed hits,
then feed those hit indices into `active_divergence_scan.py` for full delta
labels:

```bash
python bots/neural_national_lab/tools/action_divergence_prefilter.py \
  --baseline bots/neural_national_lab/versions/v043_v254_cf_handstrength_veto_p268_h32_t050 \
  --candidate bots/neural_national_lab/versions/v052_v254_cf_value_veto_mix_p063_h32_bm050 \
  --opponent bots/claude_v279 \
  --games 64 \
  --workers 8 \
  --stop-after-hits 16 \
  --stop-pair-after-first-side \
  --seed-base 2026072400 \
  --bot-seed-base 202611240000 \
  --output bots/neural_national_lab/data/action_prefilter_v043_v052_vs_v279_seed2026072400.json
```

A target-1 smoke on the known pair21/pair48 range submitted one task, skipped
one, and found pair21's mirror-side divergence after only 8 compared own
decisions on that side. This confirms the prefilter is suitable for locating
decision-changing seeds before paying for full match-delta labels.

The first real prefilter collection used `claude_v279`, `seed_base=2026072400`,
32 requested pairs, `workers=4`, and a 40-own-decision cap per side. It
submitted 29 tasks before early stopping, skipped 3, and found 4 hits
(`idx18`, `idx19`, `idx25`, `idx28`) for a hit rate of about 13.8 percent.
Full `active_divergence_scan.py` labels on those hits produced deltas
`+39755`, `-6`, `+1621`, and `0`. The merged dataset
`divergence_value_v043_v052_prefilter_p009_seed2026071503_2026072400.jsonl`
therefore has 9 rows: 4 positive, 2 zero, and 3 negative. This is still below
the v053 training threshold, but it validates the intended pipeline: prefilter
for action hits first, then spend full match-delta labeling only on hits.

A second `claude_v279` prefilter range (`seed_base=2026072500`) requested 64
pairs with the same 40-own-decision cap and found only 2 hits (`idx11`,
`idx23`), a lower 3.1 percent hit rate. Their full labels were `-19` and
`+1546`, bringing the merged dataset
`divergence_value_v043_v052_prefilter_p011_seed2026071503_2026072500.jsonl`
to 11 rows: 5 positive, 2 zero, and 4 negative. Continue collecting, but do
not assume hit density is stable; the next batch should test either a wider
own-decision cap or additional opponents to avoid spending many full scans on
empty windows.

The follow-up tests showed that neither simple lever was sufficient by itself.
With `claude_v279`, `seed_base=2026072600`, and a wider 80-own-decision cap,
32 requested pairs produced zero hits. Switching opponent to `claude_v285`
with `seed_base=2026072700` and the original 40-decision cap also produced
zero hits across 32 pairs. The next collection step should therefore be more
targeted than range scanning: use the known hit templates to search or generate
flop free-action `raise 101 -> 0` contexts, then full-label those candidates.
Blindly increasing cap, opponent count, or seed ranges is not currently an
efficient path to the 40-60 row training threshold.

`template_action_prefilter.py` is the narrower search tool for that template.
It follows the baseline plus opponent trajectory, asks the candidate on each
baseline decision state, then forces candidate history back to the baseline
action so later comparisons stay on the same path. The default template is
flop `baseline_action=101` and `candidate_action=0`. The tool now defaults to
a process executor, so the local judge and scan loop can use multiple CPU
cores instead of only relying on bot subprocess concurrency:

```bash
python bots/neural_national_lab/tools/template_action_prefilter.py \
  --baseline bots/neural_national_lab/versions/v043_v254_cf_handstrength_veto_p268_h32_t050 \
  --candidate bots/neural_national_lab/versions/v052_v254_cf_value_veto_mix_p063_h32_bm050 \
  --opponent bots/claude_v279 \
  --games 64 \
  --workers 16 \
  --executor process \
  --stop-after-hits 6 \
  --stop-pair-after-first-side \
  --max-own-decisions-per-side 80 \
  --seed-base 2026072800 \
  --bot-seed-base 202611280000 \
  --output bots/neural_national_lab/data/template_prefilter_v043_v052_vs_v279_seed2026072800.json
```

The process-pool smoke on the known `pair21/pair48` seed range found both
mirror-side template hits. Because worker tasks are submitted in parallel,
`--stop-after-hits` can overshoot by up to the active worker batch; use
`--workers 1` only when strict minimum submission matters. A 64-pair
`claude_v279` search at `seed_base=2026072800`, with an 80-own-decision cap,
completed all pairs and found zero hits. That makes the next v053 data step
clearer: do not spend the main CPU budget on blind exact-template ranges.
Generate or replay neighborhoods around the known hit contexts, then run
`active_divergence_scan.py` only on the candidates that actually reproduce a
decision-changing state.

That targeted path now has a first working loop. `template_neighborhood_prefilter.py`
loads template hits, reconstructs the exact seeded side deck, mutates one
hole/flop card at a time within a rank-neighborhood, and reuses the template
scan to keep only variants that still reproduce `raise 101 -> 0`. On the
known pair21/pair48 contexts it generated 103 variants before early stop and
found 33 hits, a 33 percent hit rate versus zero hits in the prior blind
64-seed search. `label_neighborhood_divergences.py` then full-labeled all 33
hits as active-scan-compatible rows: pair21 contributed 14 negative examples
at `-3243`, while pair48 contributed 19 positive examples at `+1727`.

The 48-dimension divergence feature dataset
`divergence_value_v043_v052_neighborhood_p044_seed2026071503_2026072500.jsonl`
has 44 rows: 24 positive, 2 zero, and 18 negative. That file is useful for
offline analysis, but it is not directly runtime-compatible with the current
value veto, whose feature contract is the 70-dimension `_advantage_features`
shape. `build_runtime_value_data_from_divergence.py` fixes that gap by loading
the bot's neural policy, recomputing the actual top label/confidence, and
training on `baseline_minus_candidate` value for the neural action. Merging
the 33 runtime neighborhood rows with the earlier 63 matchscope rows produced
`runtime_value_v043_v052_mix_p096_seed2026071503_2026072500.jsonl`.

`v053_v254_cf_neighborhood_value_veto_p096_h32_b050` is the first runtime bot
from that loop. It is a v052 copy with only `value_veto_weights.json` replaced
by the CUDA-trained h32 p096 value model (`val_sign_acc=0.75`,
`val_mae=0.51`). Targeted replay against `claude_v279` fixed the known bad
pair21 path (`v043` vs `v053` had no divergence) while preserving the known
good pair48 veto (`+1727`). A fresh 32-pair common-deck smoke against
`claude_v279` was positive but not significant: `+73.48` chips per 70 hands,
95 percent CI `[-65.91, 212.88]`, median `0`, with only two nonzero deltas
(`+4554`, `+149`). This is meaningful progress from blind sparse sampling to a
runtime-compatible local repair, but it is not a clear promotion. The next
step is multi-opponent and larger-seed validation of v053, plus collecting
more runtime-feature neighborhoods so the model is not dominated by two source
contexts.

The first multi-opponent validation confirmed that caution. Against
`claude_v279`, `claude_v283`, `claude_v284`, and `claude_v285`, with 8 paired
common-deck samples per opponent, v053 had 31 zero deltas and one small
negative delta. The aggregate was `-1.30` chips per 70 hands with 95 percent
CI `[-3.84, 1.24]`; only `claude_v279` changed at all, with idx3 producing
`-83`. Active replay of that seed showed the same flop free-action template:
v043 raised to `101`, while v053 checked `0`, but this context was a slight
loss for the veto.

`v054_v254_cf_neighborhood_regression_repair_p097_h32_b050` is the recorded
attempt to add that one v053 regression as a runtime positive row and retrain
the value head. It did not work: the p097 h32 model's validation sign accuracy
dropped to `0.50`, and targeted replay of the exact idx3 seed still produced
`-83`. Keep v054 as a failed boundary-repair artifact. The next useful move is
not another one-row retrain; collect a larger set of true v043-v053 runtime
divergences across opponents, then retrain only if the new rows cover multiple
positive and negative contexts instead of a single seed.

That larger collection now has a first targeted shard. A multi-opponent
template prefilter for v043 versus v053 over `claude_v279`, `claude_v283`,
`claude_v284`, and `claude_v285` used 16 process workers, 32 paired seeds per
opponent, and stopped after sparse template hits. It completed 128 tasks and
found only two hit pairs, a 1.56 percent hit rate: `claude_v279` idx6 on the
mirror side and `claude_v285` idx3 on the normal side. Active labeling with 8
workers showed that only the `claude_v279` idx6 hit was actually harmful
(`-92` chips); the `claude_v285` idx3 hit changed the action but had zero net
delta, and the same pair indexes did not reproduce divergences against
`claude_v283` or `claude_v284`.

`template_neighborhood_prefilter.py` now accepts `--source-opponent-label`, so
a multi-opponent source file can feed a single-opponent neighborhood scan
without accidentally expanding unrelated opponent hits. Using that filter on
the `claude_v279` idx6 source generated 54 one-card neighborhood variants and
20 template hits, a 37.04 percent hit rate. Full active labels for those 20
hits were all negative for v053 versus v043: mean `-2561.15`, median `-92`,
and three flop2 variants at `-16552`. Converted into runtime-compatible value
rows, this shard became
`runtime_value_v043_v053_idx6_neighborhood_p020_seed2026073100.jsonl`: 20
positive `baseline_minus_candidate` rows, all with the `101->0|mirror`
template.

Merging that p020 shard with the earlier p097 set produced
`runtime_value_v043_v053_mix_p117_seed2026071503_2026073100.jsonl`: 117
runtime rows, 70 input features, 60 positive, 3 zero, 54 negative, and mean
delta `435.19`. The value heads were trained on CUDA, then distilled back to
the same JSON-only stdlib runtime format. The h16 seed581 model had the best
validation sign accuracy (`0.7083`, versus h32 `0.6250` and h8 `0.6667`) and
became `v055_v254_cf_runtime_divergence_repair_p117_h16_b050`.

Targeted v055 replay fixed the newly collected `claude_v279` idx6 regression:
v043 versus v055 had zero delta and no divergence on that seed. It also kept
the older targeted behavior intact: pair21 stayed zero and pair48 still
produced the known `+1727` delta. A 32-sample multi-opponent validation on the
same v053 smoke range against `claude_v279`, `claude_v283`, `claude_v284`, and
`claude_v285` produced 32 zero deltas, aggregate `0.00` chips per 70 hands
with CI `[0.00, 0.00]`. Treat v055 as a safer repair candidate than v053/v054,
not as a promoted strength improvement yet; the next promotion-quality step is
more true runtime-divergence collection across opponents and larger
multi-opponent paired validation.

`v056_v254_cf_value_ensemble_p117_h8_h16_h32_m050_mm105` turns that repair into
a small variance-aware runtime experiment. It copies v055, adds the h8, h16,
and h32 p117 value heads, and changes only the value veto path: all three JSON
MLPs are loaded at runtime and a neural action must pass both the ensemble mean
threshold (`value_veto_mean_min=-0.5`) and the weakest-member floor
(`value_veto_member_min=-1.05`). This follows the DeepStack/ReBeL/Deep-CFR
lesson already noted above: trust compact neural value evidence only when it is
robust across approximators, while still distilling back to stdlib-only JSON
runtime artifacts.

The first v056 checks preserved the v055 safety envelope. Targeted replay kept
the `claude_v279` idx6 fix at zero delta and no divergence, preserved pair21 at
zero, preserved pair48 at `+1727`, and kept the old idx3 regression seed at
zero. The same four-opponent 8-pair validation window as v055 was also neutral:
32 zero deltas across `claude_v279`, `claude_v283`, `claude_v284`, and
`claude_v285`.

A new multicore template prefilter at `seed_base=2026080100` is the first
non-neutral v056 window. Over 48 paired seeds per opponent and four strong
opponents, 16 process workers scanned 192 pair/opponent tasks and found four
template hits, all against `claude_v279` (hit rate 2.08 percent). Active labels
for idx10, idx18, idx29, and idx36 across all four opponents showed that only
`claude_v279` changed: `+10`, `+4166`, `+6590`, and `-50`, for 3 positive and
1 negative v279 deltas. The cross-opponent replays for the same indexes were
all zero against `claude_v283`, `claude_v284`, and `claude_v285`.

The full 48-pair common-deck evaluation against `claude_v279` on the same seed
range confirmed the prefilter signal without proving promotion strength:
`+111.63` chips per 70 hands with CI `[-46.04, 269.29]`. This is the first
v056 window with a meaningful positive mean after the previous all-zero smoke,
but the interval still crosses zero and the effect is concentrated in one
opponent family. The next useful step is to harvest more v279-like positive
windows, label nearby neighborhoods around idx18/idx29, and then test whether
those value patterns survive larger multi-opponent validation rather than
raising thresholds blindly.

That neighborhood harvest found a much clearer local signal. Expanding the
first three v056 `claude_v279` template hits (idx10, idx18, idx29) with
one-card rank-neighborhood mutations generated 151 variants and 54 template
hits, a 35.76 percent hit rate. Full active labels for those 54 hit variants
were strongly positive overall: 49 positive, 5 negative, mean `+3868.37` chips
per 70 hands, 95 percent CI `[+3105.12, +4631.62]`. The important split is by
source: idx18 was 14/14 positive at `+4166`, idx29 was 23/23 positive at
`+6590`, while idx10 was mixed and net negative (`-58.94`, 5 negative of 17).
This confirms that idx18/idx29 are real local value pockets, not one-card
accidents; idx10 should not be treated as a clean positive pattern.

`v057_v254_cf_candidate_value_p175_h16_t000` is a recorded failed attempt to
make the value-head target semantics cleaner. It inverted the old p117 data to
`candidate_minus_baseline`, added the 54 neighborhood rows plus the four exact
v056 hit rows, and trained a CUDA h16 p175 value head (`val_sign_acc=0.80` for
seed604). The runtime gate then required score `>= 0.0`. That was too blunt:
targeted replay reintroduced idx6 `-92`, pair21 `-3243`, and idx3 `-83`,
blocked pair48 to zero, and also blocked idx18's `+4166` positive action. Keep
v057 as a failed sign-semantics artifact. The next version should not replace
the proven v056 p117 ensemble wholesale; instead, add a secondary positive
support model or source-specific confidence guard around idx18/idx29 while
leaving the v056 repair gate in place.

`build_runtime_value_data_from_divergence.py` now has an explicit
`--rule-action-source {baseline,candidate}` switch so runtime value rows can
encode the action context intended for a gate instead of silently assuming the
candidate's final action. Rebuilding the v056 positive-support rows with
baseline/candidate-minus-baseline semantics produced
`runtime_support_v043_v056_positive_guard_p059_rulebase_seed2026071503_2026080100.jsonl`:
59 rows, 70 input features, 53 positive, 6 negative, mean delta `+3751.44`.
The CUDA h16 support head (`support_gate_v043_v056_p059_rulebase_h16_seed621`)
had validation MAE `0.1150` and sign accuracy `0.9167`; h8 was retained as a
comparison artifact.

`v058_v254_cf_support_guard_p059_h16_s064` keeps the v056 ensemble intact and
adds only a narrow low-support override around value-vetoed flop free-action
small raises. If the original v056 gates pass, v058 does nothing. If the v056
value ensemble would block a `raise_half` candidate from a call/check rule
action, v058 scores the p059 support head using the candidate raise context; a
score below `0.64` allows the raise to recover, while supported blocks remain
blocked. This is deliberately an override for the known `101->0` family, not a
general replacement for the p117 ensemble.

Targeted v058 replay repaired the known v056 negative without losing the
positive pockets: idx6 stayed `0`, pair21 stayed `0`, pair48 stayed `+1727`,
idx3 stayed `0`, and the v279 hit set became idx10 `+10`, idx18 `+4166`,
idx29 `+6590`, idx36 `0`. The same 48-pair v279 window improved only
microscopically versus v056 because the fix removes a single `-50` pair:
`+112.15` chips per 70 hands with CI `[-45.49, +269.78]`. A four-opponent
8-pair smoke against `claude_v279`, `claude_v283`, `claude_v284`, and
`claude_v285` produced 32 zero deltas, so no cross-opponent regression was
observed. Treat v058 as a cleaner/safely guarded v056-family artifact, not as
a statistically promoted bot.

`sweep_template_windows.py` batches `template_action_prefilter.py` across
deterministic seed windows and is the first scalable active-search helper for
the narrow `101->0` support family. A four-window `claude_v279` sweep
(`seed_start=2026081000`, 32 paired seeds per window) scanned 128 paired tasks
and found six v058 template hits across three windows. True active labels were
mixed and exposed the weakness in v058's p059 guard: `+2`, `-2050`, `+784`,
`-70`, `-5374`, and `-68`. That means the old support head could still let
several bad small-raise recoveries through when the search moved outside the
original v056 window.

`build_runtime_value_data_from_divergence.py` now supports
`--extra-features hand_strength_v1`, appending the same compact hand-strength
feature suffix used by earlier interaction/value work. Rebuilding the v058
sweep labels added six new rows to the positive-support pool. The combined
p065 support set has 65 rows, 55 positive and 10 negative; the hand-strength
variant has 88 input features. Because positives still dominated, the selected
training set oversamples negatives into
`runtime_support_v043_v058_positive_guard_p105_rulebase_hs_negbal_seed2026071503_2026083000.jsonl`
(115 rows, 55 positive and 60 negative). The chosen CUDA h32 model
(`support_gate_v043_v058_p105_rulebase_hs_negbal_h32_seed661`) had validation
MAE `0.1656` and sign accuracy `0.7826`. This was selected for conservative
blocking behavior, not for maximizing the number of recovered raises.

`v059_v254_cf_support_guard_hs_negbal_h32_s040` keeps the v058/v056 runtime
structure but swaps the support model for that hand-strength, negative-balanced
h32 head and raises the support threshold to `0.40`. Targeted replay preserved
the large positive v279 pockets at idx18 `+4166` and idx29 `+6590`, fixed the
newly discovered bad sweep rows to zero, and kept idx6, idx3, pair21, and idx36
neutral. It deliberately sacrifices weaker positives such as idx10 and pair48
to avoid opening new negative raise recoveries. The same 48-pair v279 window
remains positive but not significant: `+112.04` chips per 70 hands, 95 percent
CI `[-45.60, +269.68]`. Treat v059 as a safer v058-family artifact and a
better data point for the next sampler, not as a promoted strength jump. The
four-opponent 8-pair smoke on the v053/v055 validation window remained inactive:
32 zero deltas against `claude_v279`, `claude_v283`, `claude_v284`, and
`claude_v285`.

The July 2026 full-clone scan under `external/` sharpens the scale-up path.
Deep-CFR, PokerRL, OpenSpiel, RLCard, pyCFR, poker-cfr, and ReBeL all point to
the same local next step: keep the national runtime compact and native, but
turn the offline loop into parallel actor sampling plus a GPU learner over
legal action masks and vector action-value targets. Concretely, the next
neural generation should stop adding one-off binary gates and instead collect,
for each replayable decision point, a legal abstract action menu
(`fold`, `check/call`, half-pot/small raise, pot raise, overpot/all-in as
legal) plus a delta or regret value for every legal action. The national
raise-to-total sanitizer must remain the only component that turns a raise
bucket into protocol text.

`multi_action_counterfactual_probe.py` is the first concrete implementation of
that vector-target path. It enumerates the six fixed abstract labels at one
bot0 decision, sanitizes each candidate through the version under test, runs
each legal unique final action from the same judge prefix/deck/bot RNG seeds,
and exports `state_features`, `advantage_features`, `legal_mask`,
`action_values`, `delta_vs_rule`, and `regret_vs_mean`. It also records
`unique_final_actions` and `final_action_counts` so a learner can tell the
difference between fixed-label training outputs and sanitizer-collapsed branch
actions. The v059 versus `claude_v279` smoke at `seed_base=2026080100` wrote
two ok flop rows with six legal labels and six unique final actions per row.
This validates the data contract only; it is not a performance claim.

`multi_action_shard_runner.py` is the multicore actor wrapper for that probe:

```bash
python bots/neural_national_lab/tools/multi_action_shard_runner.py \
  --version bots/neural_national_lab/versions/v059_v254_cf_support_guard_hs_negbal_h32_s040 \
  --opponent bots/claude_v279 \
  --shards 8 \
  --workers 8 \
  --games-per-shard 2 \
  --max-rows-per-shard 8 \
  --stage flop \
  --branch-scope hand \
  --seed-base 2026080200 \
  --bot-seed-base 202650020000 \
  --output bots/neural_national_lab/data/multiaction_shards_v059_vs_v279_seed2026080200.json
```

The first 2-shard/2-worker smoke wrote
`data/multiaction_shards_v059_vs_v279_smoke_s002_seed2026080200.json`: 2 ok
rows, 6 fixed legal labels per row, mean 6.5 evaluated branches, and one
off-menu rule baseline (`raise 107`). Off-menu rule raises are evaluated as
`rule_branch` for a correct `delta_vs_rule` baseline, but they are not added to
the fixed six-label training vector.

`build_multi_action_value_data.py` and `train_multi_action_value.py` are the
first learner side of the same path. The builder turns multi-action rows into
JSONL records with a fixed six-output target vector and target mask; the
trainer fits a compact JSON-exported MLP with one value/regret output per
abstract label. A first p024 CUDA run used eight shards, four workers, and
`seed_base=2026080300` against `claude_v279`. It produced 24 ok flop rows,
including five off-menu rule baselines. The delta-vs-rule data had a positive
`raise_half` bucket (`+85.04` chips/hand-scope decision, 95 percent CI
`[+9.67, +160.41]`) and a very noisy negative all-in bucket. The h16 delta
model (`multiaction_value_v059_vs_v279_p024_delta_adv_h16_seed701_weights.json`)
trained on CUDA with validation MAE `0.1777`; the regret-target comparison was
weaker (`val_best_label_acc=0.20`). Treat these as learner-contract artifacts,
not as runtime bot weights. The next v060 attempt needs larger multi-opponent
vector shards before wiring a multi-action head into the already conservative
v059 gate stack.

The first multi-opponent vector shard confirms that caution. Adding p008 flop
rows each against `claude_v283`, `claude_v284`, and `claude_v285` produced the
p048 dataset
`multiaction_value_v059_multiopponent_p048_delta_adv_seed2026080300_0600.jsonl`.
All 48 rows were valid and 15 used off-menu rule baselines. The aggregate
`raise_half` delta fell to only `+25.27`, with opponent splits of `+85.04`
against v279, `-148.88` against v283, `-57.50` against v284, and `+102.88`
against v285. The h16 CUDA head fit this mixed data better than h32
(`val_mae=0.1267` versus `0.2456`), so capacity is not the next bottleneck.
The next useful data step is broader active sampling by opponent and board
texture; do not promote a v060 runtime multi-action head from this p048 shard.

The second-window expansion added p008 shards for `claude_v279`, `claude_v283`,
`claude_v284`, `claude_v285`, and the newer `claude_v288`, producing p088.
All 40 new rows were valid, but the aggregate remained too noisy for runtime:
most label medians stayed at `0`, `raise_half` was only `+54.75`, and all-in
had a `-19900` minimum with `-793.93` mean. `build_multi_action_value_data.py`
now reports min/max/p10/p90 and `masked_best_label_counts` so these outliers
are visible. Because v059 does not allow runtime all-in, a no-allin p088 target
set was also built with `--drop-label allin`; it changed target mean from
`-124.73` to `+9.10`. The noallin h16/h32 CUDA heads improved over all-label
h16 but still had weak best-label validation (`0.22` and `0.33`). This confirms
the immediate next step: collect active, nonzero, noallin vector rows by
opponent/board texture before creating v060.

A follow-up active-target build used `--drop-label allin --drop-zero-targets
--clip-target 1000`. It kept all 88 rows but masked training loss to 190
nonzero noallin targets, with 96 positive, 94 negative, median `+125.5`, and no
zero targets. The resulting masked-best distribution was still fold-heavy
(`51` fold, `28` raise_half), so the data mostly teaches which candidate labels
are bad rather than supplying enough positive action coverage. The trainer now
computes best-label accuracy with `target_mask`, matching the dropped-target
loss contract. On CUDA, h16 reached `val_mae=0.4169` and active-target
`val_best_label_acc=0.67`; h32 overfit more (`val_mae=0.5095`,
`val_best_label_acc=0.56`). Treat this as a target-processing validation
artifact, not as v060 runtime weight evidence.

`multi_action_counterfactual_probe.py` and `multi_action_shard_runner.py` now
also support active row filters: `--active-drop-label`, `--active-min-targets`,
`--active-min-positive-targets`, and `--active-min-abs-target`. This lets shard
workers keep scanning until they find rows with nonzero positive noallin action
values instead of spending the row budget on all-zero or all-in-dominated
states. A five-opponent active flop collection against v279/v283/v284/v285/v288
wrote 45 rows and 167 nonzero noallin targets. Compared with the p088
post-filtered set, the target distribution is more useful: 138 positive, 29
negative, median `+200`, and masked-best counts of 33 `raise_half`, 7 `call`,
4 `raise_pot`, and 1 `fold`.

The active p045 h32 CUDA head
(`multiaction_value_v059_active_multiopponent_p045_delta_adv_noallin_nonzero_clip1000_h32_seed832_weights.json`)
had the best validation MAE among h16/h32/h64 (`0.2397`, versus `0.2595` and
`0.2962`) but still weak best-label validation (`0.44`). It became
`versions/v060_v254_active_multiaction_value_p045_h32`, a conservative v059
fork that only adds a multi-action value support gate after the existing policy,
advantage, interaction, value-veto, and support gates have already accepted a
small flop `raise_half` candidate. It does not expand the runtime action space.
In paired smoke, v060 was positive but not significant versus v059: against
v279 over 16 pairs it averaged `+397.25` chips per 70 hands with 95 percent CI
`[-151.47, +945.97]`; across five opponents at four pairs each it averaged
`+107.08` with CI `[-64.32, +278.47]`, split 3 positive, 17 zero, 0 negative.
This is the first multi-action value runtime candidate worth scaling, not a
completed successor.

Scaling v060 to five opponents at eight pairs each exposed the expected failure
mode of a hard support gate. It remained positive overall (`+160.76` chips per
70 hands) but had one v288 negative sample (`-5045` raw chips), making the split
3 positive, 36 zero, 1 negative and the CI still wide
(`[-296.14, +617.66]`). Trace replay showed v060 did not add a bad action; it
blocked a v059 flop `raise_half` that had strong existing interaction support
(`interaction_score=0.956`) because the new p045 value head scored it just
below the hard threshold. `versions/v061_v254_active_value_interaction_override_p045_h32`
therefore keeps the same multi-action value model but lets high interaction
support (`>=0.90`) override the p045 value veto. It preserves the v279 positive
veto case where interaction support was weaker (`0.774`) while restoring the
v288 blocked raise.

The v061 g008 five-opponent check repaired the v288 loss: mean `+223.83`
chips per 70 hands, CI `[-212.86, +660.51]`, split 3 positive, 37 zero,
0 negative. Extending the same deterministic run to g016x5 produced 80 paired
samples with mean `+112.28`, CI `[-106.08, +330.63]`, split 4 positive,
75 zero, 1 small negative (`-49` raw chips). v061 is safer than v060 and a
better recorded candidate, but still not a statistically clear successor. The
next useful step is more active sampling around low-interaction harmful v059
raises, not a larger model.

The low-interaction sampler now supports pre-decision filters for rule label,
top neural label/confidence, free-action spots, and interaction-score bands.
A five-opponent targeted run collected 57 flop rows where the rule action was
`call`, the neural top label was `raise_half`, the action was free, and
interaction support was below `0.90`. Training h16/h32/h64 CUDA value heads on
the clipped no-allin delta targets showed that h16 was the safest calibrated
runtime head: at the original `0.12` threshold it passed 26 sampled rows with
16 known positive raise targets and no known negative raise targets.

`versions/v062_v254_lowint_value_h16_p057` replaced the p045 multi-action
weights with that h16 low-interaction head, but g016x5 exposed a v284 mirror
regression: one newly allowed flop `raise_half 101` produced a `-4502` raw-chip
delta. Raising the gate to `0.20` created
`versions/v063_v254_lowint_value_h16_p057_min020`, which blocks that replayed
v284 hand and still keeps seven known-positive sampled raise targets. On the
same five-opponent g016 run, v063 averaged `+120.41` chips per 70 hands with
CI `[-21.05, +261.87]`, split 7 positive, 71 zero, 2 small negative, and worst
sample `-109`.

Scaling v063 to g032x5 exposed another v284 mirror-only boundary failure:
sample idx31 lost `-28992` raw chips after the first divergent decision changed
a free flop `check` into `raise 101`. The neural top confidence was `0.8846`,
above `raise_conf=0.88` but below `interaction_apply_min_conf=0.89`, so the
low-interaction veto did not apply despite `interaction_score=0.412`.
`versions/v064_v254_lowint_h16_min020_inter088` keeps the same h16 p057 weights
and `multi_action_value_min=0.20`, but lowers `interaction_apply_min_conf` to
`0.88`. Active replay of the v284 idx13 and idx31 failures then produced zero
delta and no divergence. In the same deterministic g032x5 run, v064 averaged
`+80.76` chips per 70 hands with CI `[-4.25, +165.77]`, split 12 positive,
141 zero, 7 negative, and worst sample `-723`; v284 was exactly neutral across
all 32 pairs. This repairs the v063 safety bug, but the CI still crosses zero;
treat v064 as the current conservative experiment, not a promoted successor.

The next boundary-data pass sampled v064 decisions where the rule action was
`call`, the neural top label was `raise_half`, confidence was `0.88..0.92`,
the action was free on the flop, and interaction support was at most `0.65`.
A narrow `0.88..0.89` pilot against v284 found no rows; the wider five-opponent
run added 16 rows, including one v288 case where `raise_half` was `-10560`
raw chips worse than call. Mixing those rows with the original p057 data
produced `multiaction_value_v064_lowint_boundary_mix_p073...jsonl` with 73
rows and 166 nonzero clipped targets. CUDA training produced h16/h32/h64 heads.
The h16 p073 head looked safe in offline gate replay, but
`versions/v065_v254_lowint_boundary_h16_p073_inter088` reopened the old v284
idx13 replay (`-4502` raw chips), so v065 is a recorded failed version.
`versions/v066_v254_lowint_boundary_h64_p073_inter088` uses the more
conservative h64 p073 head. It blocks both v284 idx13 and idx31 replays. In a
g016x5 paired smoke against v064, v066 averaged `+81.94` chips per 70 hands
with CI `[-71.37, +235.26]`, split 4 positive, 73 zero, 3 negative, worst
sample `-224`, and best sample `+12504`. This is not statistically clear, but
it is a safer boundary-data successor candidate than v065.

Extending that same five-opponent paired evaluation to g032x5 did not clear
the promotion bar. Across v279/v283/v284/v285/v288, v066 averaged `+58.52`
chips per 70 hands versus v064 with CI `[-30.91, +147.96]`, split 8 positive,
146 zero, and 6 negative paired deltas. The best sample remained a large v288
gain (`+12504`), while v279 moved negative overall (`-23.38`, CI
`[-74.72, +27.97]`) and added a `-1429` worst sample. Keep v066 as the safer
p073 boundary candidate, but do not treat it as a significant upgrade; the
next data step should replay the v279 negative divergences and the v285/v288
large positive divergences into targeted active counterfactual rows before
spending on g064 or larger model families.

`build_outlier_multi_action_value_data.py` converts those paired divergence
windows back into runtime-compatible multi-action value rows. The first pass
used the 14 nonzero v066-vs-v064 g032 samples, clipped them to `+/-1000`, and
merged them with the p073 boundary set to create an 87-row p087 training file.
CUDA h16/h32/h64 training selected the h32 head for replay: it blocked all five
negative first-divergence targets in the local outlier score check while
keeping the v285/v288 large positive windows. The resulting
`versions/v067_v254_outlier_boundary_h32_p087_inter088` keeps v066's runtime
gates and only replaces the multi-action value head. On the same g032x5
paired evaluation versus v064, v067 improved the average to `+91.14` chips per
70 hands with CI `[-12.34, +194.62]`, split 10 positive, 147 zero, and 3
negative samples. It reduced the worst sample from `-1429` to `-796`, but the
CI still crosses zero and v279 remains slightly negative (`-6.64`, CI
`[-33.07, +19.79]`). Treat v067 as a better diagnostic candidate, not a
promoted successor. The next active-learning row should target the new v279
idx22 `-796` divergence before widening games or model size.

The v279 idx22 repair pass replayed the four new nonzero v067-vs-v064 v279
windows and converted their first divergences into four more clipped
multi-action rows. Mixing those with p087 produced p091. CUDA h16/h32/h64
training did not yield a cleaner runtime head: h16 blocked everything, h32 was
too conservative, and h64 reopened older large negatives. The accepted
artifact is therefore the threshold-calibrated
`versions/v068_v254_outlier_boundary_h32_p087_min040`, which keeps the v067
p087 h32 weights but raises `multi_action_value_min` from `0.20` to `0.40`.
Targeted replay closed the new v279 idx12 `-14` and idx22 `-796` windows,
kept idx20 `+290` and idx23 `+95`, preserved the old v279 idx18/idx29 fixes,
but lost the old idx6 `+564` and reopened only a small idx13 `-80`.
On the same g032x5 five-opponent evaluation, v068 averaged `+93.20` chips per
70 hands versus v064 with CI `[-10.12, +196.52]`, split 10 positive, 148 zero,
and 2 negative samples. v279 moved slightly positive (`+5.05`, CI
`[-4.62, +14.72]`) with worst sample `-80`; the global worst sample was now
`-90` instead of v067's `-796`. Keep v068 as a safer calibration artifact, not
a statistically clear successor. The next useful work is to add richer active
rows or context features that recover suppressed positives without reopening
the large v279/v284 negative windows.

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
