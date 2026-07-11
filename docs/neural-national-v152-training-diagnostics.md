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

## Match-Cluster Bootstrap A/B

The three random-seed members above still trained on exactly the same 78 match
clusters, so seed disagreement could not measure uncertainty in which matches
happened to be collected. The trainer now has an optional opponent-stratified
match-cluster bootstrap. Each member samples the same number of whole matches
with replacement inside every training opponent and applies identical cluster
multiplicities to the value and opponent-action rows. The source manifests
remain unchanged while model metadata records the complete bootstrap draw and
effective row counts.

A controlled pass-22 A/B trained the same GRU and GRU+MoE architectures at
lower-ranking weights 0 and 0.5 with seeds 101/211/307. Each member drew 78
clusters; only 53-58 were unique because each training opponent had just four
or five source matches. The table reports median supervised metrics and the
highest-coverage diagnostic policy with a nonnegative per-decision hand LCB.

| Encoder | Lower rank weight | Match dir. balanced | Lower dir. balanced | Overrides/clusters | CI lower / stratified | Result |
|---|---:|---:|---:|---:|---:|---|
| GRU | 0.0 | 72.6% | 61.4% | 8 / 3 | -231.50 / -227.95 | reject: unsafe and sparse |
| GRU | 0.5 | 71.6% | 72.7% | 14 / 4 | -190.99 / -183.07 | reject: v98 uncovered |
| GRU+MoE | 0.0 | 71.9% | 59.0% | 16 / 5 | -193.69 / -186.59 | reject: CI and coverage |
| GRU+MoE | 0.5 | 72.1% | 70.8% | 12 / 5 | -299.90 / -308.57 | reject: negative hand value and v98 |

All four ensembles remained fast at 65-76 ms for three-member stdlib
value-plus-response inference. None passed policy selection, held-out stayed
unopened, and no bot was created. The architecture summary SHA-256 was
`9e801f50e12ac621211aa830facc14b406787b3553001949412e54ebe8bc2e87`.

The bootstrap is behaving conservatively rather than improving the small
checkpoint: it exposes substantial match-sampling sensitivity and removes the
apparently positive sparse policies seen when every member shares the same
matches. It remains useful as a larger-data robustness control, but this
pass-22 result does not justify replacing the non-bootstrap recipe. Pass 40
will therefore keep the full non-bootstrap sweep as its primary experiment and
repeat a targeted bootstrap A/B on the same frozen prefix.

## Opponent-Balanced Loss A/B

The pass-22 behavior stream contained 69-320 rows per training opponent while
the value stream contained 43-60. A first match-balanced smoke was rejected
before the A/B because one-action short matches received up to 39.4x row
weight. The retained optional mode equalizes total loss weight per opponent but
preserves the observed event frequency inside each opponent. Its value weights
ranged from 0.88x to 1.23x and behavior weights from 0.62x to 2.86x. Weighted
class and action frequencies are used consistently by the response and ranking
losses, and the complete weighting report is exported with each model.

The same 12-model GRU/GRU+MoE, lower-weight 0/0.5 experiment did not improve
policy selection:

| Encoder | Lower rank weight | Match dir. balanced | Lower dir. balanced | Overrides/clusters | CI lower / stratified | Result |
|---|---:|---:|---:|---:|---:|---|
| GRU | 0.0 | 70.5% | 60.6% | 11 / 4 | +1.16 / +1.16 | reject: sparse clusters |
| GRU | 0.5 | 71.0% | 71.1% | 10 / 6 | +3.12 / +3.12 | reject: sparse clusters |
| GRU+MoE | 0.0 | 71.3% | 60.4% | 10 / 4 | +1.65 / +1.65 | reject: v98 uncovered |
| GRU+MoE | 0.5 | 73.1% | 73.5% | 11 / 9 | -313.15 / -318.64 | reject: v142 negative |

The unweighted GRU weight-0.5 reference covered seven clusters, so the closest
balanced model regressed to six despite a positive point estimate. Its v142
total was also dominated by one observed +20100 preflop all-in while the model
predicted only about +397 chips of hand LCB. This is not sufficient evidence of
a calibrated low-risk exploit. All runtimes remained below 100 ms, held-out was
not opened by policy selection, and no bot was created. The architecture
summary SHA-256 was
`41a4f6ac74044467687c9ed781906a480e8706fbdcba88d71c7672525cc4bbd3`.
Opponent balancing remains an explicit ablation but is not part of the pass-40
primary recipe.

## Long-Horizon Target Leverage

Dataset audit schema 2 now reports target distributions and long-horizon
leverage separately by split. Before policy selection, held-out target values,
action classes, and leverage summaries are redacted; only structural counts,
opponent isolation, integrity checks, and file hashes remain visible.

On pass-22 train data, 134 of 1,784 alternative probes (7.51%) had absolute
tail delta of at least 10000 chips. Thirty-one probes (1.74%) changed the
current hand by at most 200 chips but changed the later match by at least 10000;
large tails appeared in 31 of 78 train match clusters. Validation was more
volatile: 87 of 515 probes (16.89%) crossed 10000 and 38 (7.38%) combined a
small hand delta with a large tail, spread across 15 of 22 clusters.

One train-only v141 cluster illustrates the leverage: the baseline finished at
+19922, while several different early single-decision branches finished near
-18150. This can contain genuine opponent-profile cascade, Monte Carlo random
stream divergence after a changed action path, or both; one rollout per branch
cannot identify the components. Clipping, robust losses, hand-LCB gating, and
whole-match bootstrap limit the damage but cannot create missing independent
counterfactual evidence. A stable per-hand common-random-number probe and/or
replicated branch rollouts must be evaluated as a new dataset recipe rather
than mixed silently into the current collection.

## Five-Percent Risk Quantile A/B

The train alternatives also explain why the existing 0.2 lower quantile can
miss full-stack risk. Of 460 forced all-ins, 39 (8.48%) lost at least 5000
chips. Their empirical hand-delta 5th, 10th, and 20th percentiles were -19900,
0, and 0 respectively. A controlled three-seed GRU experiment therefore
changed only `lower_quantile` from 0.20 to 0.05 while retaining lower-ranking
weight 0.5 and the corrected rule-relative-zero contract.

The median match/lower direction balanced accuracies remained 72.2%/72.0%, but
the calibrated hand-lower positive rates for seeds 101/211/307 were 0%, 0%,
and 1.2%. Every one of the 90 strict policy configurations produced zero
override after the nonnegative hand-LCB floor. This rejects a global 5%
quantile as the runtime risk mechanism on the current single-rollout data: it
removes the jackpot-driven all-ins but collapses all useful action coverage as
well. The policy report SHA-256 was
`b1cb35e6c104a98b03c99c5dacbddd695281e2301a92055f823484656c176f43`.
The next risk model must separate catastrophe probability/severity from the
ordinary hand-value lower bound instead of forcing one quantile to perform both
jobs.

## Pass-40 Catastrophe-Head Checkpoint

The pass-40 atomic freeze contains 1,652 value and 6,086 behavior training
rows after moving v121/v135 to calibration, plus 470/2,323 validation and
475/1,323 held-out value/behavior rows. Train, calibration, validation, and
held-out remain opponent-disjoint. The train value stream now covers 142
whole-match clusters and validation covers 40. Held-out targets and action
classes remained redacted. The freeze manifest and selection audit SHA-256
values are `99f900e13416382bf28707018646380a056b8fb7fc7bbe9e445404cee3e4010f`
and `6d4e50036dedba1b0c51c2f3c2ff76c845e676fef60b3dfc779a0c0cef24d848`.
This is still an explicitly incomplete 40/160 checkpoint.

A separate 4,940-parameter risk head now freezes each base model's exact
opponent-aware latent and predicts per-action:

- `P(delta_vs_rule <= -5000)`;
- clipped loss severity conditional on that event;
- expected catastrophic loss.

Each head JSON is bound to the exact base-model SHA-256. The stdlib ensemble
uses the mean plus one member standard deviation as its probability upper
bound. Match-cluster bootstrap is applied independently to the risk-head
members. A global Platt scale and bias are fit on calibration opponents only.
The base value model is unchanged, making risk-head on/off a controlled
ablation.

Unweighted BCE immediately exposed a classifier-selection failure: NLL kept
improving as validation AUROC fell below 0.3. The early-stop score now makes
AUROC and average precision primary and treats NLL as a calibration-quality
term. Unit coverage explicitly rejects the all-negative collapse. Even with
the repaired score, unweighted heads selected epoch 1 and achieved only
0.485-0.521 AUROC. Square-root positive weighting plus post-hoc Platt
calibration improved the three GRU weight-0 heads to 0.759/0.762/0.782 AUROC
and 0.080-0.099 average precision on validation, whose catastrophe prevalence
was 2.99%. Their SHA-256 values are:

- `fe41ce3a7c47bc34c098d48ee620a8e6a465358e5d3e253fbbc27848f7e540fb`;
- `458734a087d77e7b7542d2c4c6741ae2aee681653f596099aa4665ab92153fe9`;
- `680309b59c75fc735d89e3d6baeb97699384505838d916baffa243e56332eecf`.

The improved supervised metric did not pass the offline policy gate. For GRU
weight 0, a 0.05 probability-UCB ceiling retained 43 overrides over 15
clusters, but 39 were all-ins, observed hand mean was -272, and ordinary and
opponent-stratified cluster CI lower bounds were approximately -167/-171.
Reducing the ceiling to 0.005 improved hand mean to +21.6 and removed one of
two observed catastrophes, but the remaining rare preflop call left CI lower
bounds near -82. The head assigned that event a probability upper bound of
only 0.00015 before its observed `-19900` hand delta. The low-grid policy
report SHA-256 is
`5a5a673cd8c727321b08a326c05a876d2079862431ef21d977e5beee5cf5d09f`.

GRU weight 0.5 was also rejected before the risk filter: its closest base
policy had 10 overrides over 9 clusters, hand mean -1276, and ordinary/
stratified CI lower bounds near -170/-176. Its base policy report SHA-256 is
`cf78b38d9726ba83dec577caa5fa3d5e21c8c3490aebfe7c1f5dbc84fe8d3df7`.
With a catastrophe ceiling of 0.01, only four overrides over four clusters
remained and both CI lower bounds were near -84. At 0.001 there were no
overrides; at 0.05 the unsafe base coverage returned. The risk-filter report
SHA-256 is
`702682c57a0fded81cd94a5a3036a68642d4c668f4925e78c1d276dccf9e5a94`.
Held-out was not opened for either experiment and no bot was created.

This rejects the hypothesis that an action-level catastrophe classifier can
repair the current single-runout labels by threshold tuning alone. The head
has useful ranking signal, but rare call and all-in false negatives still
dominate clustered policy evidence. `native_tcp_conditional_runout_probe.py`
therefore adds a separate native-TCP diagnostic: it fixes every card dealt
before a selected decision, reshuffles only the unseen suffix of that hand,
and gives the rule and forced branches the same suffix. It checks the complete
pre-force request/state and sanitized rule action before accepting a replicate,
then reports conditional catastrophe rate and bootstrap CI. Its optional
`--through-match` mode continues both branches through hand 70 while preserving
all future deck seeds, and separately reports immediate-hand, future-tail, and
full-match deltas. A terminal rule fold may reuse its recorded baseline because
the reshuffled undealt cards are never exposed; other rule branches are rerun
under the same conditional deck. Deck-prefix, context-integrity, long-horizon
accounting, terminal-baseline reuse, and statistical helpers pass unit and
async integration tests. The queued pass-160 diagnostic uses full-match mode.
It has not yet run an end-to-end TCP replicate while the four-slot long-run
collector is active, so it remains tooling rather than evidence at this
checkpoint.

## Completed Pass-40 Sweep And Q10 Control

The fixed `rule_relative_zero_v1` pass-40 sweep subsequently completed. The
ordinary-row training sweep evaluated GRU and GRU+MoE encoders at lower-ranking
weights 0, 0.1, 0.25, and 0.5 over seeds 101/211/307. None of its eight
ensembles passed the validation policy gate. Its architecture report SHA-256
is `34e5af186d164d6f36315b6f09e128992751375a1535d9f896780b3f32b5d4c8`.

The controlled whole-match-cluster bootstrap rerun evaluated weights 0 and 0.5.
Only the 106,046-parameter GRU+MoE weight-0 ensemble passed validation
selection. Its three-member stdlib runtime was about 61 ms. The selected policy
made 15 overrides over 10 of 40 match clusters, all of them `allin`. Validation
match value per opportunity was +86.06, with ordinary, match-cluster, and
opponent-stratified CI lower bounds of only +0.0128, +0.0129, and +0.0129.
Those near-zero bounds were selection evidence, not deployable strength.

The policy was frozen before post-selection evaluation and then failed to
generalize. On calibration opponents v121/v135 it made four overrides over
three clusters, returned -5.80 match chips per opportunity, and had a
match-cluster CI of `[-17.61, +0.84]`; both opponent means were negative. On
v57/v66 held-out it made eight overrides over five clusters and returned only
+0.21 per opportunity with both CI lower bounds equal to zero. Consequently
`offline_candidate_eligible=false`, no native candidate was created, and no
strength claim is attached to this model. The architecture, sweep, and
post-selection report SHA-256 values are:

- `5f068b583e437044579dfd3bb9aa6902d9e570486c8dbb50870602824ad7ec10`;
- `8e1a915d1eea5cbcc3af63c2094d4610c97ac1679f1d60ba4dbdb6fd664ff2f0`;
- `cb79d5810686472f28fff35b2f1d858a55f5606607149afbb4683b246e8e39c7`.

Because the post-selection report exposed v57/v66 outcomes, those opponents
remain valid audit evidence for this already-frozen pass-40 policy but are no
longer an untouched final test for future recipes. The pass-160 process must
reserve newly unseen tagged opponents before final training and must not open
them when calibration fails.

A separate q=0.10 GRU control completed over the same three seeds. Median
validation match-direction and lower-direction balanced accuracy were 75.2%
and 74.6%, but the policy gate still rejected every configuration. The
highest-CI grid entries reduced to one v98 call override in one cluster, whose
single observed +20,100 result left both clustered CI lower bounds at zero.
This is jackpot sparsity, not evidence of a usable policy. Held-out was not
opened by this control. Its report SHA-256 is
`1a5c96fafaa17a2a187690153015a52c3244139e6f1bf9a9f39459198acbd3d5`.

At 2026-07-11 09:30 +08:00, the live 70-hand collection had reached 96/160
completed passes. The completed state contained 4,446/1,132/1,117 value rows
and 18,723/5,968/3,275 opponent-action rows in train/validation/held-out.
The conditional common-runout diagnostic remains queued until the four native
TCP collection slots are released.

## Hero-Hand Representation Audit

The legacy 48-dimensional state encoder is not sufficient for postflop value
learning. It contains scalar hole-card ranks, whether the two hole cards are
suited, and board-only rank/suit texture, but it does not encode the exact
relationship between the hole cards and board. A constructive collision proves
the information loss: `AsKs` and `AhKh` produce the same legacy vector on a
`Qs7s2h` flop, although the first hand has a four-card flush draw and the second
has only a three-card suit concentration. Increasing hidden width cannot recover
information that the input has discarded.

`hand_context_features.py` therefore introduces the separately versioned
18-dimensional `hero_hand_context_v1` representation. It adds the exact best
made-hand class and primary rank, best-five hole-card usage, board-rank matches,
overcards, combined hole/board suit concentration, flush-draw indicators,
straight-window density and hole contribution, and paired/suited board pressure.
The representation is bounded, stdlib-only, and invariant to arbitrary suit
renaming. Six focused tests cover the legacy collision, a board-only straight
flush, suit permutation, malformed/missing cards, and dimension bounds.

This feature set is not yet strength evidence. A pass-98 snapshot is running the
existing 48-dimensional model across GRU, GRU+MoE, Deep Sets, and Transformer
encoders at four parameter scales as the fixed control. After that sweep
finishes, the same frozen rows, cluster-bootstrap seeds, architecture recipe,
and policy gates will be rerun with the 18 dimensions appended. Held-out must
remain unopened unless calibration passes.

## Next Evidence

1. Finish and atomically freeze the 160-pass opponent-disjoint collection.
2. Repeat the full GRU, GRU+MoE, Deep Sets, and Transformer scaling grid after
   the 160-pass dataset is frozen.
3. Run a controlled legacy-48 versus legacy-48-plus-hand-context feature
   ablation before selecting the pass-160 architecture.
4. Use calibration only for output calibration and coverage diagnostics;
   reserve fresh tagged opponents outside all current partitions for the final
   blind test and do not open them after a failed calibration gate.
5. Use offline clustered policy selection before creating an active TCP bot.
6. Treat native paired classic-pool EV, not these supervised metrics, as the
   eventual strength criterion.
7. Use the catastrophe head as an ablation and uncertainty signal, not a
   release gate, until targeted data closes its rare-action false negatives.
8. Run conditional common-runout replication on the first-divergence failures,
   then compare a separately frozen replicated-label dataset before treating
   match/tail labels as causal long-horizon targets.
