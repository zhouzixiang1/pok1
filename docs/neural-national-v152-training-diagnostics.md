# v152 Training Diagnostics

Date: 2026-07-10

This report records early pipeline diagnostics from the still-running v152
collection. It is not a strength report and must not be used to choose a formal
bot from held-out results.

## Pass-5 Snapshot

The temporary four-way freeze used v121 and v135 for calibration, v98 and v142
for validation, and v57 and v66 for held-out. After removing calibration from
train, it contained 196 value rows and 846 opponent-response rows. Most
opponents still had only one match cluster, so all measurements below are
deliberately preliminary.

The alternative value labels exposed a structural MAE problem:

- hand delta: 36.8% exact zero, with observed extremes beyond +/-27000;
- tail delta: 75.4% exact zero, with observed extremes beyond +/-36000;
- match delta: 36.1% exact zero, with observed extremes beyond +/-38000;
- match direction beyond a +/-100 dead zone: 87 positive and 131 negative
  training alternatives.

Smooth-L1 and clipped quantile losses bound the extremes, but an all-zero mean
can still look competitive under MAE while being unusable for action selection.

## Architecture Diagnostic

An MAE-only 8-architecture, 3-seed CUDA sweep selected small Deep Sets. A
separate stdlib replay over validation labels showed that its rule-relative
match-direction balanced accuracy was only 51.5% median. The comparable
pre-ranking medians were:

| Encoder | Match direction balanced accuracy |
|---|---:|
| small GRU | 57.3% |
| medium GRU+MoE | 57.9% |
| small Deep Sets | 51.5% |
| medium Deep Sets | 51.7% |

This demonstrates that MAE and opponent-response accuracy alone are not a safe
model-selection objective.

## Pairwise Ranking Change

The trainer now adds a rule-relative ranking loss on match-delta alternatives
outside a +/-100 chip dead zone. Positive/negative examples and action classes
are weighted from the training split only. No new deployment head is added: the
existing match-value means supply the candidate-minus-rule logit.

Validation now reports overall, balanced, positive-rate, and per-action
direction metrics. The architecture selection score includes match-direction
balanced accuracy. On the same pass-5 snapshot, the 4-architecture, 3-seed
ranking sweep selected small GRU, whose validation match-direction balanced
accuracy improved from 57.3% to 61.7% median. The improvement is useful pipeline
evidence, but it is too early to freeze the ranking weight or architecture.

Every exported model records the ranking recipe and trainer SHA-256. Resume
rejects a model if either the recipe or trainer implementation changes, avoiding
silent reuse of old MAE-only weights.

## Ranking-Weight Diagnostic

The scaling runner now treats ranking weight as a validation-only selection
dimension rather than requiring separate manually compared runs. A small-GRU
3-seed grid on the same pass-5 snapshot produced:

| Ranking weight | Median selection score | Match direction balanced | Match MAE | Response balanced |
|---:|---:|---:|---:|---:|
| 0.00 | 1.1580 | 61.7% | 678.7 | 48.5% |
| 0.25 | 1.1471 | 61.0% | 725.0 | 50.4% |
| 0.50 | 1.1517 | 61.7% | 734.8 | 49.4% |
| 1.00 | 1.1444 | 63.0% | 752.2 | 49.2% |

Weight 1.0 won this preliminary combined score, but only by 0.0027 over 0.25
and with worse match MAE. This is not enough evidence to freeze the weight.
Formal scaling will jointly compare architecture, size, weight, and seeds after
the dataset contains many independent clusters per opponent.

## Policy-Level Failure Audit

A deliberately relaxed offline-policy run on the pass-5 ranking winner found a
validation policy with only four overrides across three match clusters. Those
four labels happened to total +78094 match chips, yielding an apparently
positive opponent-stratified CI. This was sparse jackpot evidence, not a robust
policy:

- calibration v121/v135: zero overrides, so no calibration coverage;
- held-out overall: -287.9 chips per opportunity;
- held-out v66: -1690.9 chips per opportunity;
- held-out overrides: all 25 were all-in actions.

The offline evaluator now distinguishes total observed clusters from clusters
that actually receive overrides. Selection requires minimum override-cluster
coverage, minimum overrides against every validation opponent, and nonnegative
per-opponent means. A separately reported calibration gate requires override
coverage and positive ordinary and opponent-stratified cluster CI lower bounds.
Failed selection and calibration reports are still written with model/data
hashes. Under these strict
defaults the pass-5 policy is correctly rejected before any active bot is made.

## Policy-First Scaling Gate

The scaling runner now defaults to policy-level selection. For every
architecture, size, ranking weight, and seed ensemble it freezes an override
policy on validation rows, then ranks eligible configurations by:

1. opponent-stratified whole-match cluster CI lower bound;
2. ordinary whole-match cluster CI lower bound;
3. match delta per decision opportunity;
4. override-cluster coverage;
5. supervised validation score only as a final tie-break.

Eligibility requires both cluster CI lower bounds to be positive, enough
independent override clusters, enough overrides against every validation
opponent, and no negative validation-opponent mean. Thus a sweep where every
configuration has an uncertain policy now fails instead of selecting the least
bad supervised model.

After architecture and policy hyperparameters are frozen, the complete seed
ensemble is rerun without changing its validation score. The fixed policy is
then evaluated on calibration and held-out data. Both post-selection gates
require override coverage, positive ordinary and opponent-stratified cluster CI
lower bounds, and nonnegative means for every opponent. The runner writes
`post_selection_policy.json`, embeds its hash and result in the ensemble
manifest and sweep summary, and exits nonzero on failure unless an explicit
diagnostic override is supplied.

Passing a relaxed diagnostic gate is not deployment evidence. The runner now
reports a separate `offline_candidate_eligible` state that additionally
requires a complete frozen dataset, minimum train rows, at least three seeds,
multiple opponents in every split, and no weakening of the default coverage or
CI thresholds. `deployment_eligible` remains false because native paired
classic-pool evaluation and official EXE acceptance occur after this sweep.

A two-architecture CUDA smoke exercised the complete
train -> calibration -> validation selection -> held-out path. A second run
required one post-selection override where the tiny smoke model made none: it
returned exit code 1 while preserving all model, manifest, summary, and gate
failure artifacts. This verifies pipeline semantics only; the synthetic smoke
data and relaxed selection thresholds are not strength evidence.

Candidate training can now use multiple subprocess workers while preserving a
fixed architecture/seed result order. The default remains one worker for
large-model memory safety; the pass-10 small/medium diagnostic uses three
workers after a two-worker CUDA smoke verified concurrent execution.

The live 70-hand collector had completed 10 of 160 passes at the time of this
update, with 460 train, 120 validation, and 115 held-out value rows plus 2,943
opponent-action rows. Collection remains active, and these append-only files
must not be treated as a frozen training dataset.

The first incomplete diagnostic freeze exposed that cumulative files may
already contain rows from the next in-progress pass before `pool_snapshots` and
`collector_state.json` advance. The old `--allow-incomplete` path incorrectly
included those rows while reporting the previous completed-pass count. The
freeze tool now requires the atomic collector state for incomplete snapshots,
reads exactly its recorded row prefixes, verifies prefix hashes, and rejects a
state/snapshot pass mismatch. The corrected pass-10 freeze contains 412 value
and 1,679 behavior training rows after moving v121/v135 to calibration; v98 and
v142 remain validation-only, while v57 and v66 remain held-out-only.

## Pass-10 Policy Sweep

The atomic pass-10 diagnostic trained 72 CUDA models: eight small/medium
encoder configurations, three ranking weights, and three seeds. All 24 seed
ensembles met the stdlib runtime budget, but none met the validation policy
gate. Every one of the 1,728 policy grid points had fewer than ten overrides
and fewer than eight override clusters.

Ranking supervision still produced a useful supervised signal. Small GRU rose
from 56.7% match-direction balanced accuracy without ranking to about 71% with
ranking weight 0.5 or 1.0. Small MoE reached 68.3%-70.4%. Medium models did not
improve on the small models, so scale has not yet earned its runtime cost.

Only unranked Transformer ensembles approached the policy coverage threshold.
A deliberately relaxed check selected small Transformer with eight overrides
across six validation clusters. It failed calibration: v135 was negative, five
of eight calibration overrides lost match value, and both cluster CI lower
bounds were negative. Held-out deltas were positive but covered only two of ten
clusters, leaving clustered CI lower bounds at zero. No candidate bot was
created.

The new override trace identified a structural selection error rather than a
margin issue. All near-passing policies chose `hand_weight=1.0`, even when the
model's match-value lower bound was strongly negative. The policy grid now
models hand, tail, and match value on a simplex and reserves at least 25% weight
for match value. Pure single-hand policies are not eligible for formal scaling.

The deployment policy reads the lower-quantile head, while the original
pairwise ranking loss trained only the mean head. A new independent lower-head
ranking weight and lower-direction validation metric expose and address this
objective mismatch. On small GRU, lower-ranking weight 0.5 improved median
match lower-direction balanced accuracy from 49.4% to 59.1% and raised raw
positive lower predictions from 0.7% to 12.8%. Lower-quantile coverage remained
observable rather than being hidden by the direction metric.

The pass-10 3-seed policy still rejected every lower-ranking configuration.
After per-model calibration, seed disagreement made the conservative ensemble
lower bound negative for every long-horizon policy point. Calibrated mean-lower
aggregation produced only 5 positive match bounds among 237 validation
alternatives, still below the required override and cluster coverage. This is
useful learning-head progress, not a bot candidate; the experiment must be
repeated as independent match clusters accumulate.

## Pass-22 Lower-Risk Ranking Checkpoint

An atomic prefix freeze at 22 completed collection passes contained 896 value
and 3,352 behavior training rows after moving v121/v135 to calibration. The
calibration, validation, and held-out splits contained 120/477, 259/1,328, and
259/669 value/behavior rows respectively. The freeze remained explicitly
incomplete (`22/160`) and therefore could not qualify a candidate even if a
diagnostic policy passed. Its manifest SHA-256 was
`655e8420b9d8292e52e557b9776ee6de5c13e2e40194ff3d95da50a1b4fb60d5`.

The checkpoint trained 24 CUDA models: small GRU and GRU+MoE encoders, four
lower-head ranking weights, and seeds 101/211/307. The table reports medians
over the three seeds and the closest strict validation policy for each
ensemble. `CI lower` is the opponent-stratified whole-match cluster bootstrap
lower bound in chips per decision opportunity.

| Encoder | Lower rank weight | Match dir. balanced | Lower dir. balanced | Lower positive | Best overrides/clusters | CI lower | Result |
|---|---:|---:|---:|---:|---:|---:|---|
| GRU | 0.00 | 70.6% | 60.7% | 12.9% | 32 / 8 | -152.5 | reject |
| GRU | 0.10 | 69.0% | 63.3% | 21.6% | 35 / 10 | -234.5 | reject |
| GRU | 0.25 | 68.8% | 64.2% | 23.4% | 17 / 10 | -272.3 | reject |
| GRU | 0.50 | 68.9% | 63.4% | 24.3% | 8 / 4 | +1.6 | reject: sparse, v98 uncovered |
| GRU+MoE | 0.00 | 67.7% | 60.9% | 12.6% | 19 / 6 | 0.0 | reject: sparse clusters |
| GRU+MoE | 0.10 | 72.1% | 61.3% | 20.7% | 31 / 9 | -157.8 | reject |
| GRU+MoE | 0.25 | 69.6% | 62.5% | 22.5% | 16 / 7 | -149.6 | reject |
| GRU+MoE | 0.50 | 69.4% | 62.8% | 23.7% | 12 / 6 | -150.9 | reject |

All eight ensembles stayed far below the 5-second stdlib runtime budget; the
measured three-member value-plus-response estimates were 68-74 ms. The sweep
correctly exited nonzero with `no architecture satisfies the policy/runtime
selection gates`. Its architecture summary SHA-256 was
`452a6633f40ddf7a35c9ef9cc3e7b037bafd66ae9d29d621eb9e4079e814fc0c`.
Held-out data was not opened and no bot version was created.

The checkpoint changes the diagnosis without changing the acceptance result.
At pass 10 every long-horizon policy produced zero conservative overrides. At
pass 22 the lower-risk heads produce useful coverage, and GRU+MoE can find
nonnegative overrides on both validation opponents, but too few independent
match clusters support a strictly positive lower bound. The remaining blocker
is cross-match and cross-opponent evidence, not GPU utilization or protocol
integration. Lowering the cluster gates would recreate the sparse-evidence
failure that invalidated v146.

## Rule-Relative Zero Contract A/B

A post-sweep contract audit found that the ranking loss subtracted the model's
predicted rule-action value from each candidate even though every training
target was already `candidate - rule`. The stdlib runtime instead forces the
rule action to exactly zero and uses each candidate value directly. Across 515
validation alternatives, this changed the lower-head sign on 132-189 examples
per model. Predicted rule lower values had roughly -700 to +750 chip 10th-90th
percentile ranges despite their exact-zero targets, so the discrepancy was
large enough to affect policy selection.

The trainer now uses an explicit `rule_relative_zero_v1` reference for ranking,
validation direction metrics, model metadata, and runtime policy semantics. A
unit test varies the unused rule output by +/-1000 while requiring identical
mean- and lower-head ranking loss.

The same pass-22 data then trained a controlled 12-model A/B: GRU and GRU+MoE,
lower-ranking weights 0 and 0.5, and the same three seeds. No new collection
rows were used. The closest strict policy result for each ensemble was:

| Encoder | Lower rank weight | Match dir. balanced | Lower dir. balanced | Overrides/clusters | CI lower / stratified | Remaining errors |
|---|---:|---:|---:|---:|---:|---|
| GRU | 0.0 | 71.3% | 57.0% | 14 / 5 | +3.41 / +2.46 | override clusters < 8 |
| GRU | 0.5 | 72.3% | 71.5% | 20 / 12 | -137.10 / -132.70 | both CIs cross zero |
| GRU+MoE | 0.0 | 72.6% | 58.7% | 15 / 5 | +3.83 / +3.88 | override clusters < 8 |
| GRU+MoE | 0.5 | 72.7% | 72.2% | 16 / 10 | -293.87 / -239.75 | both CIs cross zero |

Before the fix, the corresponding lower-ranking balanced accuracies were 63.4%
for GRU and 62.8% for GRU+MoE. The repaired lower-ranking policies also used
fold, call, half-pot, pot and all-in alternatives rather than collapsing to an
all-in-only gate. Both validation opponents had positive point estimates, but
individual match clusters still contained approximately -20000 to +39000 chip
outcomes, so the cluster intervals correctly remained negative for the broad
policies. The sweep exited nonzero, did not open held-out, and created no bot.
The repaired architecture summary SHA-256 was
`3203e14f0d847684fd619e3c7d97cdfc8c6bb982153828d576076ea3479fff87`.

A second controlled sweep filled in lower-ranking weights 0.1 and 0.25 for
both encoders. All four ensembles were rejected. GRU 0.1 covered 13 clusters
but had negative override hand value and negative CIs; GRU 0.25 and GRU+MoE
0.25 regressed on v98; GRU+MoE 0.1 either covered only v142 or crossed zero
once it covered both opponents. This rules out simple interpolation between
the sparse weight-0 policy and the broad weight-0.5 policy on the pass-22
snapshot. The intermediate-weight architecture summary SHA-256 was
`340cdc0486146074c2b0b9f22ce94fbcaa82989b96e84b6db831ffd530d9f31e`.

The broad policies also exposed a risk-composition flaw: a weighted sum could
accept a candidate whose immediate-hand lower bound was negative because its
noisy match-value prediction was positive. A component-level ablation now
requires every candidate's calibrated hand LCB to be nonnegative before
scoring. On the same GRU weight-0.5 ensemble this retained 10 overrides across
7 clusters, produced no observed negative override, kept v142 and v98 positive,
and yielded ordinary/stratified CI lower bounds of +4.89/+3.84. It remains
rejected because formal evidence requires at least 8 override clusters. Adding
a simultaneous nonnegative match-LCB floor reduced coverage to 6 overrides in
5 clusters and was not adopted. The nonnegative hand-LCB floor is now part of
the formal offline-candidate contract rather than a per-opponent threshold.

## Next Evidence

1. Repeat the fixed `rule_relative_zero_v1` validation-only check at a
   materially larger match-cluster checkpoint; do not tune from held-out
   results.
2. Repeat the full GRU, GRU+MoE, Deep Sets, and Transformer scaling grid after
   the 160-pass dataset is frozen.
3. Use calibration only for output calibration and coverage diagnostics; keep
   held-out completely outside architecture, recipe, and policy selection.
4. Use offline clustered policy selection before creating an active TCP bot.
5. Treat native paired classic-pool EV, not these supervised metrics, as the
   eventual strength criterion.
