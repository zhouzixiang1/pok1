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
`state_feature_schema.py` binds the legacy and extended state dimensions to the
same versioned contract and derives the complete response-head private mask;
the extended schema masks both legacy hole-card fields and all appended
hand-context fields from OpponentActionNet.

This feature set is not yet strength evidence. A pass-98 snapshot is running the
existing 48-dimensional model across GRU, GRU+MoE, Deep Sets, and Transformer
encoders at four parameter scales as the fixed control. After that sweep
finishes, the same frozen rows, cluster-bootstrap seeds, architecture recipe,
and policy gates will be rerun with the 18 dimensions appended. Held-out must
remain unopened unless calibration passes.

## Statistical Validity Audit At Pass 100

An independent audit at 2026-07-11 10:30 +08:00 found that the old collection
and evaluation seed plans did not create independent 70-hand match clusters.
The old collector used `5000 + pass * 17` as its deck seed base, while a match
consumes `base` through `base + 69`. Adjacent passes therefore shared 53 of 70
decks. Multiple opponents in one split also received the same base and were
then treated as separate `(opponent, deck_seed_base, bot_seed_base)` bootstrap
clusters. The old evaluator defaulted to a seed stride of one and the v146 live
report used bases `7000/7001/7002`, `7400/7401/7402`, and
`7800/7801/7802`. Those are only three connected deck windows, not nine
independent blocks. Removing the strongly positive 7400 window changes the
v146 relative mean from positive to negative. This reinforces the existing
conclusion that v146 strength is not established and invalidates any nominal
CI that counted the overlapping matches as independent.

The original 160-pass collection was stopped after the atomic pass-100 state:
4,638/1,180/1,160 value rows and 19,333/6,230/3,422 behavior rows in
train/validation/so-called held-out. It remains useful for exploratory
representation and architecture training, including the already-running
pass-98 scaling control, but is now explicitly `exploratory-only`. It cannot be
used for final confidence intervals or a release claim.

The replacement collection contract makes every `(pass, opponent)` consume a
separate `hands + guard` deck block and a unique bot seed. A persisted pass
plan prevents an interrupted pass from assigning an already consumed block to
a different live-pool opponent. Each opponent is copied read-only from the
checkout that owns the live ratings into a content-addressed execution
snapshot. The manifest records that checkout commit, the exact execution
directory SHA-256, the generation tag/tree SHA-256, and whether later global
protocol fixes made the execution tree differ from its generation tag. The
candidate is also content-addressed and snapshotted. A two-opponent native TCP
smoke verified distinct blocks, exact snapshot execution, and zero protocol
failures.

The old deterministic decision subsampling also lacked a recoverable inclusion
probability. Replacement `uniform` sampling is seeded independently from bot
randomness and samples without replacement. Every value row now records the
eligible and selected counts, sampling seed, inclusion probability, and inverse
probability weight, plus stage/action population counts in the probe report.
`sampling_weights.py` now defines self-normalized IPW and
opponent-balanced-IPW contracts for the next controlled trainer. It applies
decision IPW only to counterfactual value rows because behavior rows come from
the complete baseline trace. The running pass-98 trainer does not consume those
weights, so this is not a retroactive correction of old models.

The audit identified three further estimand boundaries:

- offline policy EV sums mutually exclusive one-decision interventions from a
  common baseline. It is an action-uplift screening statistic, not the value of
  a deployed policy that performs all selected overrides in one trajectory;
- current model calibration labels are reused by the policy gate, validation is
  reused for early stopping and grid selection, and v57/v66 have already been
  exposed. The next pipeline must use opponent-disjoint early-stop,
  model-calibration, policy-selection, policy-gate, and one-time final-blind
  roles with an exposure ledger. v57/v66 are regression opponents only;
- the conditional runout tool fixes the opponent's hidden cards and future
  schedule. It measures board sensitivity conditional on that hidden state,
  not decision-time Bayes risk. It remains a failure diagnostic until an outer
  loop samples opponent range, future decks, and bot seeds.

Accordingly, offline uplift, supervised AUROC, nominal selection CI, and the
queued conditional runout are diagnostic metrics. Release strength must come
from a frozen native policy executing all overrides on fresh, non-overlapping
70-hand paired blocks, followed by opponent/block-aware uncertainty,
leave-one-block-out sensitivity, and a one-time blind classic-pool test.

`native_tcp_evaluate.py --strength-evidence` now enforces that boundary before
and after execution. It requires at least three deterministic blocks per
opponent, 70 hands per seat, paired seats, unique bot seeds, no forced action,
native entries only, and at most four workers. Default deck stride is
`hands + 10`; different opponents receive disjoint 10-million-seed regions.
After execution, every row must contain both complete 70-hand legs, 70 paired
hand deltas, zero illegal/timeout/adapter/wrapper events, no issues, and stable
candidate/opponent directory SHA-256 values. Strict `--strength-evidence` runs
additionally discard the inherited parent environment, inject one exact
two-thread runtime envelope, fix the match timeout and decision budgets, and
forbid trace/force controls; that runtime contract is repeated by the frozen
plan and both reports.
`native_tcp_report_diff.py --strength-evidence` may validate the two input
receipts, but its v3 output is explicitly unregistered diagnostic-only evidence:
it reports 70-hand outcome uplift first, treats chip delta and leave-one-block-
out sensitivity as secondary, and keeps every strength/deployment field false.
Only the raw-replayed frozen-plan verdict can make the later false-authority
development-pool decision. The old adjacent-seed v146 reports intentionally
fail this contract.

The offline policy evaluator now labels its estimand
`single_decision_action_uplift_ipw_v2`, set `deployment_policy_value=false` and
`strength_evidence=false`, and uses IPW for means, per-opponent summaries, and
ordinary, match-cluster, and opponent-stratified cluster bootstrap intervals.
Its old `held_out` argument remains only a policy gate, not a final blind test.
The standalone evaluator does not read, stat, or hash that next file after an
earlier gate fails. However, the old scaling sweep defeats that protection by
running a whole-directory audit at startup, which parses and hashes every old
split before training. It also reuses validation for checkpoint and policy
selection, then reuses model-calibration rows as a policy gate. Therefore the
running pass-98 sweep is architecture diagnostics only regardless of its final
numbers. The new sweep must consume the five roles lazily after it finishes.

`opponent_exposure_ledger.py` adds an append-only, file-locked role ledger.
An opponent opened for train, early stop, model calibration, policy selection,
policy gate, development native evaluation, or regression can never be
reserved as final blind. A final-blind reservation blocks every other reader;
opening is conservative and one-time, after which the opponent may only be
used as regression data. The local runtime ledger was initialized from the
pass-100 snapshots: 19 train opponents, v98/v142 as early-stop opponents, and
v57/v66 as policy-gate opponents. Consequently no currently used member of
that pool is a legitimate final blind opponent; final release evidence must
reserve newly completed classic versions that appear later in the live pool.
The ledger itself is runtime data and is not committed with source code.
Final-blind reservations are bound to the frozen candidate directory SHA-256;
opening the blind result requires both the same candidate digest and the
evaluation artifact digest. This prevents a reserved unseen opponent from
being reused for a different post-tuning candidate under the same run ID.

Three versioned input contracts have also been added without silently changing
the running v150-format trainer or runtime:

- `current_hand_actor_event_v2` is a 24-dimensional, actor-relative history
  event. It removes hero/opponent collisions and uses event-local pot/stack
  fields when present. Current collected rows contain actor identity but do not
  contain `pot_after` for every event, so those availability bits remain zero;
  a future trace-capable collection version must capture the missing fields.
- `public_decision_context_v1` is a 15-dimensional public-only context with
  opponent/effective stack, minimum raise, all-in call amount, unsaturated pot
  and match score, remaining-match pressure, and a six-action legal mask. These
  values can be reconstructed from the current raw request/state rows. The
  combined `legacy48_plus_hero_hand_public_decision_v1` value-state contract is
  81 dimensional: legacy 48 + hero hand/draw 18 + public decision 15. Response
  masking removes the legacy private-card and hero-hand dimensions while
  retaining all 15 public decision dimensions.
- `v140_strategy_context_v1` is a 66-dimensional value-head-only contract for
  exact preflop strength, weighted equity, range-distribution summaries,
  made/draw/value profiles, opponent estimates, board/spot risk, and the rule
  value plan. It is deliberately forbidden from the opponent-response head.
  Current rows did not capture this exact rule-path context, so it cannot be
  reconstructed faithfully or used until a new native trace version records
  it with a dedicated deterministic decision RNG.

These files define migration boundaries, not evidence that the new inputs have
already trained or improved a deployed policy. Adoption requires a new model
format, matching stdlib runtime validation, and paired regression against the
unchanged v140 rule trajectory.

`model_input_schema.py` now composes the 81-dimensional state and 24-dimensional
actor-aware history through one bounded encoder shared by the future trainer
and stdlib runtime. Its response mode derives the private mask from the state
schema instead of hard-coding legacy indices, and metadata explicitly records
that exact rule strategy context is still absent. This prevents either side
from silently adopting a different feature order or dimension.

The independent native version
`v152_national_v140_strategy_context_trace_tcp` captures that missing context
inside the original v140 strategy call after its exact equity/range and
postflop profile computations. Trace encoding consumes no random numbers and is
disabled unless `POK_TRACE_DECISIONS=1`; the full range vector is reduced to
six distribution summaries before serialization. Deterministic unit scenarios
covering preflop and postflop currently produce identical v140/v152 actions,
and the native trace row carries 66 finite bounded values. This is a diagnostic
data-source version, not a neural strength candidate. Independent 70-hand
native paired parity remains required before a context-aware collection starts.
`strategy_context_trace_rows.py` provides the strict value-row join: it binds
on hand, in-hand decision index, and match decision serial; requires the traced
final action to equal the row's rule action; validates all 66 values; and stores
a canonical context digest. The joined fields explicitly forbid use by the
opponent-response head.
`native_tcp_strategy_context_probe.py` keeps the active legacy probe untouched:
it first completes the normal counterfactual runs, then replays one trace-only
baseline with the same deck and bot seeds. Context is attached only if native
compliance is clean, baseline net chips are identical, and every selected rule
action matches its trace. This extra replay is why context collection will run
as a separate dataset rather than being mixed into the active collection.

The same new probe now emits the versioned
`national_opponent_response_v2` supervision contract. It reconstructs the
public state immediately before the opponent acts and uses the national
platform validator as the legality oracle for the five response classes. Every
observed row carries a legal-action mask, response-target mask, and explicit
response eligibility. The full decision trace is reconciled with the observed
rows, so hero folds, calls that close a street, all-in runouts, and preflop big
blind checks are counted as no-response outcomes instead of silently entering
or disappearing from the action target. Raise events retain their protocol
raise-to-total value while sizing supervision uses the incremental commitment;
all-in events are interpreted as remaining-stack increments. Both are encoded
against the pot after the hero action, with a separate stack fraction and
versioned log-pot target. A read-only audit of 1,351 complete behavior rows from
the running independent collection passed this reconstruction and the official
validator with zero failures. Those old rows remain immutable and exploratory;
only a future strategy-context collection receives the v2 fields, and the
trainer must not consume them until its role-isolated refactor is complete.

`freeze_opponent_role_dataset.py` replaces the ambiguous four-way development
freeze for the next training run. It emits five explicit opponent-disjoint
roles: train, early stop, model calibration, policy selection, and policy gate.
It snapshots only the atomic `collector_state.json` completed-pass prefix, then
binds every row to a persisted pass-plan cluster. The freeze rejects reused or
overlapping 70-hand deck blocks, reused bot seeds, source-split drift, invalid
or duplicate counterfactual decisions, inconsistent uniform-sampling IPW, and
candidate/opponent snapshot digest changes. Final blind is intentionally not a
dataset role; it remains an external one-time native evaluation reserved in the
exposure ledger after the candidate is frozen. Its manifest records requested
and completed pass counts plus an explicit collection-complete flag, so a live
prefix can be used for smoke tests but cannot pass the future candidate gate.

The initial pass-2 smoke validated role partitioning but used the now-superseded
v1 behavior-row contract. A new `opponent_role_dataset_v2` smoke froze the
atomic pass-7 boundary with v141 for early stopping, v142 for model calibration,
v98 for policy selection, and v57/v66 for the policy gate. It contains 494
counterfactual rows and 2,304 canonical response-v2 rows. All 2,304 responses
were observed and legal; 574 carry an aggressive-size target. The manifest
binds the response schema and each behavior output declares its row schema.
These counts only validate the freeze contract; the source is explicitly
incomplete at 7/160 passes and cannot train or support a strength claim.

`role_dataset_access.py` is the matching lazy reader. Construction reads only
`role_manifest.json`; role JSONL files are not opened, statted, or hashed. Each
role exposure is written to the append-only ledger before its two data files
are read and hash/row/opponent validated, so a corrupt read remains
conservatively exposed. Policy selection requires the three model-development
roles to have been opened by the same run. Policy gate additionally requires
the same frozen candidate and a `passed=true` policy-selection result bound to
that run, candidate, and role-manifest digest. A failed result cannot touch or
register the gate data. The active pass-98 sweep predates this reader; it will
be integrated only after that process stops reading the old trainer from disk.

`multitask_training_data.py` now provides the trainer-facing staged assembly
contract without changing the trainer file used by the active exploratory
sweep. The training phase can open only train and early-stop roles. Model
calibration remains inaccessible until it receives a frozen-checkpoint
authorization bound to the same run id, role-manifest digest, exact train and
early-stop artifact digests, and completed early stopping. Policy-selection and
policy-gate roles have no entry point in this assembly layer. Value rows in all
three model-development roles receive opponent-balanced decision-sampling IPW;
behavior rows receive opponent-balanced weights without counterfactual IPW.
Only train rows expose gradient weights, while early-stop and calibration rows
carry a distinct evaluation-weight field. Behavior rows are upgraded in memory
to the national response v2 contract and rejected if their observed target is
outside the reconstructed legal mask. The shared encoder produces the new
81-dimensional state and 24-dimensional actor-aware history, masks all derived
private hero features for the response head, and keeps the 66-dimensional exact
strategy context value-head-only. The pass-7 v2 frozen-prefix smoke opened
290/1,431 train value/behavior rows, 36/205 early-stop rows, and 36/137
calibration rows in the required order without touching policy data.

`multitask_calibration.py` defines the post-checkpoint calibration math that the
refactored trainer will call. Lower-value offsets use the calibration role's
opponent-balanced IPW and a weighted residual quantile; an action-specific
offset is allowed only when both its row count and effective sample size pass
fixed thresholds, otherwise it falls back to the weighted global offset.
Opponent-response temperature is selected by weighted NLL after masking every
illegal national action, so an impossible class with a large raw logit cannot
distort calibration. Reports include per-opponent before/after NLL and effective
sample sizes. The final calibration artifact is deterministically bound to the
frozen checkpoint, role manifest, and model-calibration artifact and declares
that no policy evidence was used. This remains a standalone interface until the
active old-data scaling process releases the trainer file.

`policy_role_evidence.py` adds the next protected boundary. A frozen candidate
and calibration payload digest are recorded before the policy-selection role is
opened. The selection decision is recomputed from minimum override and
match-cluster coverage, positive ordinary and opponent-stratified cluster CI
lower bounds, and nonnegative per-opponent means. Its signed v2 result binds the
role artifact, calibration payload, complete evaluation report, and selected
policy digests. It also records `deployment_policy_value=false` and
`strength_evidence=false`: single-decision IPW uplift can screen a policy but
cannot prove deployed trajectory value. `role_dataset_access.py` verifies every
binding before it opens or records exposure to policy-gate rows. The policy gate
therefore remains unopened after any failed or mismatched selection result, and
native TCP evaluation remains the only release-strength authority. A temporary
pass-7 access-chain smoke opened 48/312 policy-selection value/behavior rows and,
only after a fully bound synthetic pass credential, 84/219 v57/v66 policy-gate
rows; both phases retained false deployment-value and strength-evidence flags.

The next model implementation is isolated in
`opponent_multitask_model_v3.py`; it does not modify the trainer still used by
the pass-98 exploratory GPU sweep. Its shared inputs are the versioned
81-dimensional decision state, 24-dimensional actor-aware current-hand history,
12-dimensional opponent profile, and up to 32 prior-hand summaries with 16
public features each. Cross-hand ablations support no encoder, Deep Sets, GRU,
and GRU+MoE. An explicit opponent interaction conditions both task paths, while
the 66-dimensional exact rule-strategy context is available only to value
heads. The value path predicts hand, tail, and match means plus monotonic
q05/q10/q20/q50 values for every legal action. The response path predicts the
five nationally legal action classes and two aggressive-size targets, with
private hero state masked both during row encoding and inside the network.

`opponent_profile_schema.py`, the extended `multitask_training_data.py`, and
`opponent_multitask_batch_v3.py` form the corresponding strict input path. They
reject profile/request disagreement, row/request cross-hand disagreement,
unknown schemas, malformed dimensions, non-finite targets, targets outside
their masks, illegal rule actions, and non-positive role weights. The tensor
collator uses explicit lengths for right-zero-padded current-hand and
cross-hand sequences and produces kwargs that can be passed directly to either
v3 forward path. With a Deep Sets cross-hand encoder, the declared small,
medium, and large scales contain 173,185, 646,817, and 2,495,713 parameters.

An atomic pass-9 freeze of the running independent collection contains 638
value rows and 2,854 canonical response rows. Its role counts are 386/1,729 for
training, 36/205 for early stopping, 48/232 for model calibration, 60/404 for
policy selection, and 108/284 for policy gate. All 3,492 rows passed strict v3
encoding, and complete neural-lab regression testing passed 246 tests. The
source is still incomplete at 9/160 passes. The v3 network therefore has no
trained checkpoint, stdlib export, active TCP policy, or strength claim yet;
these results establish only the model/data interface needed for the future
independent-corpus scaling run.

`train_opponent_multitask_v3.py` now closes the model-development portion of
that interface without reading policy-selection or policy-gate data. It cycles
value and response batches to balance the tasks, combines clipped Smooth L1
means with q05/q10/q20/q50 pinball losses and match-direction losses, masks
illegal response logits, applies opponent-balanced row weights, and restores
the best opponent-disjoint early-stop checkpoint. The checkpoint is written and
hashed before a run-bound authorization can open model calibration. Its payload
binds the role manifest, train/early-stop artifacts, collection boundary, model
metadata, training configuration, and SHA-256 values for all seven code modules
needed to reproduce it. A strict loader reconstructs the architecture and
rejects metadata or state drift. Incomplete collections are rejected unless the
caller explicitly enables `--allow-incomplete-smoke`. The default command stops
after train/early-stop and leaves model calibration unopened; calibration
requires a separate explicit `--open-model-calibration` flag. One-epoch
train-only and explicit-calibration smokes produced byte-identical checkpoints,
while their ledgers contained two and three roles respectively.

The first explicitly calibrated CUDA smoke exposed and fixed a second
extreme-value path: the
trainer clipped targets for gradient updates but initially used raw near-20,000
chip labels for early-stop MAE and lower-value calibration. That produced a
tail offset near -16,938 and reintroduced all-in domination after training. The
early-stop estimand and calibration residual now use the same declared symmetric
clip, while raw MAE is retained only as a diagnostic. A pass-9 small Deep Sets
run with seed 101 completed train, early stop, checkpoint authorization, and
calibration on the RTX 4060. Two independent three-epoch executions produced
the byte-identical checkpoint SHA-256
`9b0085f4948934da0fadc20607275f98782a42995d9cc1a01d814b374fea2316`
and identical early-stop score 1.032205. Their exposure ledgers contain only
train, early-stop, and model-calibration events. The report and artifact
manifest explicitly retain `strength_evidence=false`,
`deployment_policy_value=false`, and `source_collection_complete=false`; this
is deterministic pipeline evidence, not model selection or Bot strength.

`run_opponent_multitask_v3_scaling.py` is the protected architecture/seed
sweep. Every `(scale, encoder, seed)` receives its own run id, checkpoint,
report, and log. The parent validates that child reports opened exactly train
and early-stop, wrote no calibration artifact, made no policy or strength
claim, and completed every requested seed before a configuration can be
selected. Formal selection requires a complete source, at least three seeds,
and every requested configuration to finish. An incomplete pass-9 smoke ran
one small no-cross-hand model and one small Deep Sets model for one epoch. Both
completed and their ledger contained only train/early-stop events. Although the
no-cross-hand score was lower in that tiny run, the summary correctly emitted
only `provisional_best_configuration`, set `selection_eligible=false`, and left
`selected_configuration=null`; it is orchestration evidence, not an encoder
result.

`calibrate_opponent_multitask_v3_ensemble.py` consumes that selection without
retraining or calibrating every architecture. Formal mode accepts only a
complete scaling summary, the full selected configuration's at-least-three
distinct seed checkpoints, identical non-seed training contracts, unchanged
artifact hashes, and a complete role dataset. It freezes those members into an
ensemble checkpoint manifest and uses that manifest's SHA-256 for one new
run-bound train/early-stop/model-calibration authorization. The calibrated
value lower bound is the mean member q20 minus a configurable multiple of the
standard deviation of member mean values; response logits are averaged before
legal-mask temperature calibration. The artifact records every member hash,
aggregation rule, disagreement diagnostic, clip policy, and retains false
deployment/strength claims. A provisional one-member pass-9 smoke completed
the full artifact chain, exposed exactly the three model-development roles,
and produced zero ensemble disagreement as expected. The same summary was
rejected without the explicit incomplete-smoke flag, so it cannot enter formal
calibration accidentally.

`multitask_training_data.py` and `opponent_multitask_batch_v3.py` now expose a
separate inference-only context contract. Value inference requires the legal
mask, rule action, observable state, opponent profile, current-hand history,
cross-hand sequence, and optional strategy context, but no role weight or
target. Hypothetical response inference reconstructs whether the opponent must
act and its exact legal mask through the national validator; folds, street-
closing calls, and other settled actions produce no response row. The response
path retains the complete private-state mask. Supervised collation still
requires the original role weight and legal target masks, so this interface
does not weaken training validation merely to support policy inference.

`select_opponent_multitask_v3_policy.py` completes the protected offline
selection boundary. Before opening policy-selection data it verifies every
ensemble member and calibration artifact, then freezes the ensemble,
calibration payload/report, inference device and batch contract, policy grid,
and all participating code hashes into a candidate manifest. Value lower
predictions use calibrated mean-member q20 minus between-member mean-value
standard deviation. Opponent response logits use legal-mask temperature
calibration, while the bounded response-risk signal also consumes predicted
aggressive size. Selection reuses the declared single-decision IPW cluster
estimator and can emit a passing credential only with enough overrides and
independent clusters, positive ordinary and opponent-stratified CI lower
bounds, and nonnegative per-opponent means. It always retains
`deployment_policy_value=false` and `strength_evidence=false`.

The pass-9 incomplete smoke opened 60 v98 value rows and 404 behavior rows only
after candidate SHA-256
`015026b5c7f6506e2b6fa2faa44bfed6914c0be85edc8ed27ed700fa2be6da0b`
froze. It evaluated all 60 decisions, including 97 hypothetical opponent
responses and 90 policy-grid configurations. The provisional ensemble's very
negative calibration offsets selected zero overrides, so the result failed all
coverage and positive-CI gates. Its ledger added only `policy_selection`;
`policy_gate` remained unopened. Formal mode rejected the incomplete role
manifest before opening selection data. The complete neural-lab regression
suite now passes 269 tests. At the contemporaneous atomic collection boundary,
the replacement independent corpus had completed 13/160 passes with
609/156/151 train/validation/held-out value rows and 2,785/899/429 response
rows. These are pipeline and collection-progress facts, not strength evidence.

`export_opponent_multitask_v3.py` and
`opponent_multitask_runtime_v3.py` now provide a deterministic JSON export and
stdlib-only forward path for every supported cross-hand encoder: none, Deep
Sets, GRU, GRU+MoE, and temporal Transformer. The loader derives the exact matrix
shape from the frozen hidden-size and model metadata contracts, rejects missing
or extra tensors, checks every value and the total parameter count, and masks
response-private state again at inference. It implements linear/ReLU stacks,
PyTorch-compatible GRU gates, Deep Sets masked mean/max, MoE routing,
monotonic softplus quantiles, sigmoid size heads, and legal-action logit masks
without Torch, NumPy, or network access. Focused tests cover all five
encoders, empty histories, private-state invariance, malformed weights, and
byte-deterministic export. Across the five random small-model parity cases, all
97 value/response outputs differed from Torch by less than `1e-5` (observed
maxima were approximately `1.7e-7` to `2.6e-7`).

The actual pass-9 small no-cross-hand checkpoint exported to a 3,462,371-byte
JSON artifact with SHA-256
`8761501aefd6e53686faa1451e2cbc5abaa4680d3d075dd54e107958a8a0528a`.
On a real v98 policy-selection row its maximum value and response differences
from Torch were `1.94e-7` and `4.41e-8`; one-member stdlib value inference
averaged about 3.5 ms on the local CPU. The export deliberately carries false
deployment/strength flags. Formal ensemble runtime, selected-policy binding,
and native TCP joint-policy evaluation are still required.

`export_opponent_multitask_ensemble_v3.py` and
`opponent_multitask_ensemble_runtime_v3.py` extend that contract to the full
calibrated ensemble and selected policy. Every nested member has a canonical
payload hash and a unique checkpoint binding; members must share identical
metadata and hidden dimensions. Runtime value aggregation reproduces the Torch
selection path in chips, response aggregation applies the frozen legal-mask
temperature, and policy scoring requires calibrated lower values, nonnegative
hand/tail/match/response weights, and value weights summing to one. A selected
policy is accepted only when its canonical hash and passing-selection flag
agree. A calibration-only bundle has no action-selection method in effect.
Five focused tests cover three-member value and response parity, LCB margin
selection, member/policy hash drift, and the non-selecting calibration-only
state; the complete suite now passes 282 tests.

The current incomplete pass-9 artifacts exported twice to byte-identical
3,464,538-byte bundles with SHA-256
`87549761e18692eee72919c0ff9fdbdcf0a1dd1773b9c8356d0061ace9d468d2`.
Its Torch-versus-stdlib calibrated value and response differences on a real
v98 row were at most `2.44e-4` chips and `4.91e-8`, respectively. Because the
protected policy selection failed, the bundle records
`policy_selection_passed=false`, has `selected_policy=null`, and remains unable
to override the rule policy. This is the required failure-safe behavior, not a
candidate result.

`evaluate_opponent_multitask_v3_policy_gate.py` now implements the next
opponent-disjoint boundary. It accepts only a complete role dataset, formally
calibrated ensemble, and passing hash-bound policy-selection result. The gate
opens through `RoleDatasetAccess.open_role("policy_gate")`, evaluates exactly
the frozen selected policy without another grid search, and recomputes override
coverage, ordinary match-cluster CI, opponent-stratified cluster CI, and every
opponent mean. Its result binds the selection-result file, gate-role artifact,
selected-policy, and complete evaluation hashes. A pass authorizes only
construction of a native development candidate; deployment-value and strength
flags remain false. Synthetic tests demonstrate a pass with 16 overrides over
eight clusters and two positive opponents, and rejection when either opponent
mean is negative. Policy substitution or gate-time grid search is rejected as
an invalid evidence contract.

The real pass-9 invocation was intentionally rejected because the source
collection is incomplete. The exposure ledger remained unchanged with no
`policy_gate` event and no output directory was created. The complete neural-
lab regression suite now passes 287 tests. This proves fail-closed gate wiring,
not that any current policy has passed the gate.

The native evaluator now treats the sign of each complete 70-hand leg as the
primary strength outcome. `seventy_hand_outcomes` reports wins, losses, draws,
positive rate, ordinary paired-block cluster bootstrap CI, opponent-stratified
cluster bootstrap CI, and opponents below 50 percent before reporting chip
magnitude. Recomputing the historical v146 live-pool rows gives 67 positive and
59 negative 70-hand legs (53.17 percent), with ordinary and stratified 95
percent intervals of [44.44, 61.90] and [45.24, 61.11] percent. v119, v141, and
v142 are below 50 percent. These old rows also use overlapping deck windows, so
the numbers are descriptive and fail the new win-rate evidence gate.

`v3_native_policy.py` and
`build_opponent_multitask_v3_native_candidate.py` now close the gap between a
passing offline gate and a development bot. The wrapper reproduces the
collector's one-action-per-label support, consumes the exact 81-dimensional
state, 24-dimensional current-hand history, 12-dimensional opponent profile,
16-dimensional cross-hand summaries, and captured 66-dimensional strategy
context, and optionally evaluates the calibrated opponent-response head. It
keeps raise-to-total actions distinct from legacy raise deltas and returns the
already-sanitized rule action on every model, context, or policy exception.
Environment switches disable the full v3 policy, cross-hand encoder, or
risk/match contribution for later ablations.

The builder refuses to create or overwrite a version unless the policy gate,
role manifest, selected policy, selection result, evaluation, and exported
ensemble all agree by SHA-256 and carry no deployment or strength claim. An
authorized build copies the v152 strategy context, v151/v147 stream-safe TCP
transport and cross-hand lifecycle, local national validator, stdlib ensemble,
and policy bundle into a new version directory. It rejects Torch, NumPy,
adapter, and repository-relative server imports. The real incomplete pass-9
bundle was rejected without creating the requested version. Focused build and
fallback tests, the full neural-lab suite (296 tests), and all 30 national
protocol tests pass. No formal new bot exists yet; construction still waits for
a complete collection and a genuinely passing policy gate.

The stdlib multi-task runtime was hardened separately. Model dimensions,
versioned state schema, response private-state mask, every context input, and
linear/GRU weight shapes are now checked exactly. A malformed or mismatched
model returns no neural prediction so the sanitized rule action remains in
control; it can no longer silently truncate vectors through Python `zip`.
The historical v150 context-only encoder and newer rule-conditioned encoder are
distinguished from their first-layer widths and tested as explicit contracts.

## Win-First 70-Hand Evidence Contract

The primary objective is now explicit throughout the protected data and policy
path: a complete national match is successful only when net chips after exactly
70 hands are greater than zero. Chip magnitude is considered only after this
outcome criterion. `match_outcome_schema.py` cross-checks
`baseline_match_net_chips`, every observed `match_action_values` entry,
`match_delta_vs_rule`, the rule action, and every confirmed probe. Partial,
non-finite, non-70-hand, or internally inconsistent evidence fails closed in a
formal role freeze. The existing v3 model format remains unchanged; encoded v3
rows carry optional outcome supervision so a separately versioned future model
can add an outcome head without silently changing old checkpoint semantics.

The formal role dataset contract is now `opponent_role_dataset_v4`. In addition
to `national_70_hand_outcome_validated=true`, its consumers recompute the exact
pass-plan prefix, source split mapping, five-way opponent partition, deck/bot
seed disjointness, candidate/opponent snapshots, frozen pool/registry copies,
and current freeze-tool hash before accepting the 160/160 boundary. Historical
v3 smoke manifests remain diagnostics and cannot authorize formal training.
Policy evaluation uses the versioned
`single_decision_70_hand_positive_outcome_uplift_clustered_v1` diagnostic. It
first collapses sampled decision opportunities within each match cluster, then
bootstraps one point per 70-hand match, both ordinarily and within opponent
strata. Reports include candidate and rule positive rates, positive-outcome
uplift, sign-flip counts, complete row/cluster coverage, and per-opponent rates.
These remain single-intervention diagnostics with
`deployment_policy_value=false` and `strength_evidence=false`; only fresh native
joint-policy matches can prove bot strength.

Formal policy selection and the fixed protected gate now require full outcome
coverage, both candidate positive-rate CI lower bounds above 50 percent,
nonnegative ordinary and stratified positive-outcome uplift lower bounds, and
at least 50 percent positive rate with nonnegative uplift for every opponent.
Only after those checks do the existing chip-EV CI and per-opponent mean checks
rank or authorize a policy. Thresholds below these floors are rejected, and the
native candidate builder independently rechecks the same evidence, so old
chip-first gate artifacts cannot authorize a build.

Against the live independent collector at its atomic 24/160-pass boundary, a
smoke freeze validated 1,688 value rows and 7,481 response rows. It froze 1,031
train, 91 early-stop, 144 model-calibration, 144 policy-selection, and 278
policy-gate value rows, correctly truncating concurrently appended files to the
completed collector state. The neural-lab suite passes 306 tests. This proves
the data and gate contract on real collection output, not that a policy or bot
has passed the new strength criterion.

`opponent_multitask_model_v4.py` adds the first separately versioned neural
consumer of that supervision. It preserves all v3 distributional value and
opponent-response heads, but adds one absolute 70-hand positive-outcome logit
per action from the shared opponent-aware value latent. Its masked objective is
binary cross entropy plus within-decision positive-versus-nonpositive action
ranking. Early stopping compares a lexicographic key in this order: flip-subset
balanced error, overall outcome balanced error, outcome NLL, then the old v3
value/response score. Thus a secondary chip/value improvement cannot replace a
checkpoint whose primary outcome behavior worsened.

An incomplete CUDA smoke on the atomic 24-pass role freeze trained a small GRU
v4 model with 179,415 parameters for two epochs on the RTX 4060. Epoch two
improved the secondary v3 score but worsened the primary flip-outcome error, so
the trainer correctly restored epoch one. Its early-stop outcome balanced
accuracy was 57.06 percent, flip-subset balanced accuracy 77.42 percent, NLL
0.6670, and Brier score 0.2373. These numbers use only one early-stop opponent,
24/160 collection passes, and two epochs; they are pipeline diagnostics, not a
model-strength result.

`opponent_multitask_runtime_v4.py` and
`export_opponent_multitask_v4.py` extend the strict stdlib runtime without
changing the v3 format. The smoke checkpoint exported to a 3,750,782-byte JSON
artifact (SHA-256
`b5bf27225507db89683cff82a8bfb8fbccf7df50fbe9a81085adbada221bd359`).
On a real frozen `national_v135` row, Torch-versus-stdlib maximum differences
were `2.02e-7` for distributional values and `6.94e-8` for outcome logits. The
raw head still emits explicitly uncalibrated logits; probability calibration is
a separate checkpoint-bound artifact rather than hidden model state.

## Protected Outcome Probability Calibration

`calibrate_match_outcome_v4.py` now opens only the frozen
`model_calibration` role after verifying the early-stop checkpoint,
authorization, training-role artifact hashes, run ID, and role manifest. It
fits a positive global logit scale and bias by weighted binary NLL. Identity
calibration is retained as an optimizer candidate, so an optimization-step
bookkeeping mismatch cannot associate a measured loss with different
parameters or return a worse regularized fit. Calibration JSON is self-hashed,
bound to the exact checkpoint and model format, and explicitly carries false
deployment and strength claims.

The incomplete pass-24 smoke opened `national_v142` for model calibration and
left policy-selection and policy-gate roles unopened. It used 144 source rows
with 429 observed action outcomes. The fitted scale was `0.5265807` and bias
was `0.3616051`; weighted NLL changed from `0.66576` to `0.65158`, Brier score
from `0.23673` to `0.22974`, and ECE from `0.12033` to `0.03332`. However, its
0.5-threshold balanced accuracy fell from 56.0 percent to 50.0 percent because
all observations became positive predictions. The monotone transform preserves
ranking but this tiny one-opponent split does not demonstrate useful policy
discrimination. These results validate calibration mechanics only.

`export_opponent_multitask_v4.py` can embed the calibration only when its
self-hash, checkpoint SHA-256, and model format match the exported checkpoint.
The strict stdlib runtime retains both raw logits and calibrated probabilities
and rejects provenance or payload drift. The calibrated smoke export was
3,753,996 bytes with SHA-256
`154e899f2a692df444b9674a3845a41339d5a96368b1a7e5458aea7e8cf10948`.
On a real frozen `national_v142` row, Torch-versus-stdlib maximum differences
were `5.47e-8` for raw logits and `2.88e-8` after calibration. The calibration
payload SHA-256 was
`bf0df5bc0eaf88e2f09fc4a624c829a8f9774bd41c2401a19670ede817ca21b9`.
The full neural-lab suite now passes 332 tests, and all 30 national protocol
tests pass. A complete independently collected role dataset, multi-seed
selection, protected gate, and fresh native TCP matches remain mandatory before
this head can influence a formal candidate.

## Runtime Win-First Policy Contract

`win_first_policy_v4.py` makes the user's strength priority executable rather
than leaving it only in the post-hoc gate. Calibrated probabilities from each
seed are aggregated as a mean plus/minus a configurable population-standard-
deviation uncertainty radius. A candidate is ineligible unless its absolute
70-hand positive-outcome probability lower bound is at least 50 percent, its
lower bound strictly exceeds the rule action's probability upper bound by the
global uplift margin, its immediate-hand LCB is nonnegative, and its weighted
chip LCB exceeds a nonnegative margin. Per-opponent fields and weakened
positive-probability floors are rejected by the exact policy schema.

Among eligible actions, selection is lexicographic: candidate positive-
probability LCB first, conservative probability uplift second, and chip LCB
score third. A controlled test therefore selects a higher-win-probability
action with a one-chip score over a lower-win-probability action with a
thousand-chip score. This intentionally encodes “finish the 70-hand match
positive first; maximize chips second” rather than blending both objectives
into one scalar that chips can dominate.

`opponent_multitask_ensemble_runtime_v4.py` validates every calibrated v4
member and its canonical payload hash, requires all members to use the same
model-calibration role, reuses the tested v3 value/response aggregation through
an exact base-model projection, and adds the outcome uncertainty path. A
calibration-only bundle cannot select an action; a selected policy must be
hash-bound and carry false deployment/strength claims. Eight focused tests
cover uncertainty arithmetic, lexicographic priority, rule-UCB comparison,
threshold weakening, malformed outcome bounds, uncalibrated members, and
member-binding drift. The full neural-lab suite now passes 340 tests, with all
30 national protocol tests still passing.

## Protected V4 Selection And Native Construction

The v4 contract is now connected through the complete protected development
path. Formal `run_opponent_multitask_v4_scaling.py` requires the exact atomic
160/160 collection boundary, CUDA training, every small/medium/large parameter
scale, every Deep Sets/GRU/GRU+MoE/temporal-Transformer encoder, and at least
three distinct seeds. `none` remains an explicit cross-hand ablation and is not
eligible to substitute for a formal architecture. The default formal matrix is
therefore 3 scales x 4 encoders x seeds 101/211/307, or 36 training jobs. It
aggregates the four-component early-stop key lexicographically, so chip/value
metrics cannot outrank the 70-hand outcome errors. Calibration verifies every
real checkpoint/report/authorization in the full Cartesian grid on CPU,
recomputes every seed aggregate and the global winner with the shared scaling
code, and retains only the winning members on the requested device. Forging a
non-selected run or editing the selected configuration cannot redirect formal
calibration.

The ensemble calibration artifact binds every unique seed and checkpoint to
the same role manifest, train/early-stop artifacts, model-calibration artifact,
and model-calibration opponents. Value lower offsets and response temperature
retain the v3 semantics. Each member fits its own checkpoint-bound outcome
scale and bias before probabilities are aggregated. Calibration opens only
train, early stop, and model calibration; it has no path to policy selection,
policy gate, or old held-out files. Formal mode requires at least three members,
both uncertainty weights exactly 1.0, current trainer dependency hashes, and
the self-hashed full-grid verification proof. Every calibration, report, and
manifest keeps `deployment_policy_value=false` and
`strength_evidence=false`.

The formal report and artifact schemas are now v2 and bind the complete
calibration code closure, including outcome fitting, v3/v4 scaling and
aggregation semantics, and the file-snapshot/publication helper. Scaling
summary, role manifest, train/early-stop/model-calibration files, exposure
events, and every selected member's checkpoint/report/authorization/manifest
are recorded as one self-hashed input receipt. Each member checkpoint is loaded
from the same descriptor snapshot whose bytes were hashed, rather than hashing
one pathname read and loading another. Before publication, the tool holds the
ledger lock, rechecks every external receipt and the exact 160-pass provenance,
runs the strict ensemble loader against the staging tree on CPU, fsyncs the
complete flat tree, and uses `renameat2(RENAME_NOREPLACE)`. A later loader
requires the original train/early-stop/model-calibration exposure events but
allows the append-only ledger to gain policy-selection or policy-gate events;
normal downstream role opening therefore does not invalidate calibration.

`select_opponent_multitask_v4_policy.py` reuses the v3 value/response inference
preparation but does not reuse the v3 scalar action selector. For every Torch
inference row it calls the same
`win_first_policy_v4.aggregate_member_probabilities` and
`win_first_policy_v4.select_candidate` functions used by the stdlib runtime.
The action floors are fixed globally at positive-probability LCB 0.5,
probability-uplift LCB 0, and immediate-hand LCB 0. Only nonnegative chip
margins are searched. The observed forced 70-hand outcomes then enter ordinary
and opponent-stratified whole-match cluster bootstraps. Formal selection fixes
a minimum of 2,000 bootstrap samples plus 12 overrides, eight selection and
override clusters, and four overrides per opponent. Before gate data can be
opened, the system reopens only the already exposed selection role, rebuilds
the complete candidate-bound grid and winner from the protected rows and
current checkpoints, and compares the full evaluation. Probability/uplift
domains, bootstrap seed, grid membership, and the prior ledger exposure are
also checked. Thus synchronized edits to the result JSON and its hashes cannot
manufacture a passing selection. A separate v4 evidence schema prevents a v3
selection credential from opening the v4 gate.

`export_opponent_multitask_ensemble_v4.py` emits a calibration-only bundle when
selection has not passed. After a formal selection, it deterministically
exports every member from the verified checkpoint. The gate reconstructs that
bundle byte-for-byte before it opens policy-gate rows, then evaluates exactly
the frozen policy without grid search and applies both cluster bootstraps and
the per-opponent checks again. The builder later reloads the calibration and
policy artifacts and independently replays both selection and gate evidence.
It therefore rejects self-consistent evidence edits as well as a changed model
weight with a recomputed bundle-internal hash. A formal selected bundle requires
three unique seeds, complete data, one shared model-calibration role, and false
evidence claims.

`v4_native_policy.py` constructs the same one-action-per-label alternatives as
the collector and delegates scoring to the shared v4 ensemble runtime. This
collection's baseline is the exact v140 snapshot final action after its legacy
overlay and single `sanitize_action` call; it is not the raw strategy action.
The native wrapper never reinterprets that already sanitized integer. Any
model, input, response, scoring, or final-sanitization exception returns it
unchanged. If final sanitization changes a selected candidate action or label,
the wrapper also returns the baseline rather than executing an unscored action.

The collection contains no strategy-context payload, so training saw the
66-dimensional context as all zeros. The frozen runtime contract therefore
forces that context to zero while retaining the v151 cross-hand lifecycle and
sticky-packet transport. The builder derives the complete v140 strategy/legacy
overlay tree from the role manifest's snapshot path and digest, locks the v151
transport, and verifies the role manifest plus gate exposures in the ledger.
Gate artifacts also bind the builder/patch helper, validator, response schema,
and transport inputs. Candidate construction uses one byte snapshot of those
inputs, checks a recursive stdlib/local import allowlist, compiles and smoke
loads the result, and cleans read-only temporary trees on failure. At native
load time the build manifest and copied gate evidence externally anchor the
bundle and every runtime artifact; a post-build bundle edit falls back to the
v140 baseline. The candidate still declares native strength, official
acceptance, deployment eligibility, deployment policy value, and strength
evidence false.

The final incomplete end-to-end smoke used the atomic 40/160 prefix. Its role
manifest SHA-256 was
`0b7846e0bea016a57a1815ea11a12a0e1512a75d0c2b950667182d6e2cbf1c7f`
and contained 1,712/158/230/240/465 value rows and
7,898/877/1,101/1,382/1,384 behavior rows for train, early stop, model
calibration, policy selection, and policy gate. A one-epoch, one-seed small-GRU
CUDA run exercised scaling and ensemble calibration; this deliberately cannot
meet the formal all-scale, all-required-encoder, three-seed contract. The
ensemble and outcome calibration payload SHA-256 values were
`0a170a7262920229e8e11780926c1545976bbedbe82b9e4abdcc406fdca8aa3a`
and `5ee1017a361aa09b64f7f83f18fd22c37dc077d478e28ac8c9d620995163adc5`.

The protected selector evaluated all 240 v98 rows but wrote a failed result
with `selected_policy_sha256=null`, `formal_selection=false`, and both evidence
flags false. The calibration-only stdlib bundle was byte-validated with SHA-256
`834e3c0d3efe61a58664c0701f46aba495426e60975b4248346f5ce5e36c3132`;
it has `selected_policy=null` and cannot override the rule policy. The formal
gate rejected the incomplete role manifest before opening policy-gate data,
the exposure ledger contains no policy-gate event, and the builder created no
version directory. This is fail-closed pipeline evidence only. No new bot
version or strength claim exists. The complete neural-lab suite now passes 449
tests, and all 30 national protocol tests pass.

## Formal V4 Architecture Grid Hardening

The v4 architecture contract now includes a single-layer temporal Transformer
as another cross-hand opponent encoder inside the same network, rather than as
a copied trainer or a separate policy path. It consumes the same at-most-32
completed-hand sequence as Deep Sets, GRU, and GRU+MoE; the current-hand GRU,
opponent fusion, value/response/outcome heads, losses, role boundaries, and
win-first selector remain shared. The block uses a learned position table,
multi-head self-attention, two residual LayerNorm stages, and a two-layer ReLU
feed-forward network, then returns the last valid completed-hand token. Empty
history remains an exact zero embedding.

The stdlib runtime implements the same bounded attention and validates every
projection, position, LayerNorm, and feed-forward tensor shape before it can
run. Torch-versus-stdlib tests cover empty input and the full 32-hand window,
and the three-member ensemble export/reload path now exercises Transformer
members directly. Invalid head divisibility and metadata drift fail closed.
Training checkpoints additionally bind the inherited v3 model and batch code,
which are direct semantic dependencies of v4.

The scaling summary and independently recomputed calibration proof were
versioned forward. Formal selection now rejects any grid missing one of the
three scales, any required encoder, any of three seeds, the exact 160/160
boundary, or CUDA execution. These are architecture-chain guarantees only:
they do not open policy-selection, policy-gate, or held-out roles, and they do
not create strength evidence before the independent collection is complete.
After this hardening, the complete neural-lab suite passes 484 tests and all 33
national protocol tests pass.

## Formal V4 Grid Resumability And Proof Binding

The 36-job formal grid can now resume without treating a directory name or an
old summary row as evidence. Before any trainer starts,
`run_opponent_multitask_v4_scaling.py` atomically publishes a self-hashed
`scaling_run_contract.json`. The contract freezes the exact Cartesian matrix,
run IDs, output paths, commands, per-seed `PYTHONHASHSEED`, training options,
device environment, Git commit, Python executable, trainer and transitive code
hashes, role-manifest bytes, exact train/early-stop file bytes and row counts,
candidate snapshot, and false deployment/strength claims. A resume may change
only runner concurrency; any semantic or provenance change requires a new
output root.

Root initialization uses a same-filesystem sibling temporary directory,
`fsync`, and rename. Each trainer similarly writes a PID-named temporary
directory, rechecks its frozen code and environment immediately before
publication, and renames only a complete manifest/checkpoint/report set.
Completed jobs are reusable only after strict directory allowlisting, manifest
and file rehashing, safe `torch.load(weights_only=True)`, exact checkpoint/report
cross-binding, strict model-state loading, and reconstruction of the full
CLI-derived training configuration. A dead training temporary is preserved in
an abandoned-partial quarantine. An invalid completed artifact is preserved in
an invalid-job quarantine and retrained; protected-role ledger contamination is
never repaired by moving files or retraining. A nonblocking root lock excludes
concurrent runners and is released in every success or exception path. A
metadata temporary left by process termination between `fsync` and rename is
also quarantined on resume rather than blocking an otherwise valid run root.

Training-role evidence is checked from the raw append-only ledger rather than
its expanded per-opponent view. The complete ledger must have canonical fields,
continuous non-boolean sequence numbers, canonical opponent groups, valid UTC
timestamps and hashes, and known event/role combinations. Each training run ID
must have exactly two complete group events in `train` then `early_stop` order,
with no candidate hash or other role. A canonical per-run receipt digest enters
the scaling row and formal proof. The final all-job verification uses one
shared-lock ledger snapshot and holds that lock through atomic summary
publication, so unrelated concurrent runs may append before or after the
barrier but the published grid cannot race a same-run mutation.

Formal calibration no longer trusts even a self-consistent embedded proof. It
reloads every real member on CPU, recomputes the winner, and binds each verified
row back to the current manifest, train/early-stop composite digests, current
code, scaling tool, trainer, requested CUDA matrix, planned run ID/output,
exact command, environment, training configuration, role counts, and raw-ledger
receipt. The downstream proof validator repeats those bindings from a single
ledger snapshot and requires exact proof/row field sets. Re-signing a changed
contract, command, environment, role count, code digest, or exposure receipt is
therefore rejected.

These changes make interruption and reuse auditable; they do not make an
incomplete collection deployable. Incomplete smoke output still carries
`deployment_policy_value=false` and `strength_evidence=false`, cannot become a
formal selected configuration, and cannot create a Bot. No formal CUDA grid was
run while the independent collector remained incomplete. The complete
neural-lab suite now passes 498 tests and all 33 national protocol tests pass.

## Protected V4 Deployment Runtime Budget

V4 selection now has an immutable deployment-feasibility guard before it can
open `policy_selection`. The selector first exports a policy-free calibrated
stdlib image and enforces a 49,000,000-byte preselection ceiling. An isolated
`python -I` child then loads only the copied runtime directory and executes two
warmups plus seven measured maximum-shape neural override paths. Each path has
one value forward, one calibrated outcome forward, five response forwards, and
one call to the shared `win_first_policy_v4` selector. The fixed inputs contain
the full 81-dimensional state, 12-dimensional profile, 16x24 current-hand
history, 32x16 cross-hand history, and 66-dimensional strategy context. Formal
eligibility requires the maximum `process_time_ns` result to be at most five
seconds; wall time is diagnostic. Exceptions, non-finite output, incomplete
measurements, a child timeout, and incomplete source data all fail closed.

The preselection `runtime_budget.json` is self-hashed and binds the exact
benchmark bytes plus a policy-independent runtime identity. That identity
includes every member payload hash, checkpoint/calibration projection and role
provenance, and the exporter contract with every copied runtime-module hash. It
deliberately excludes the selected policy and its evidence hashes, avoiding a
hash cycle while still rejecting member, calibration, or runtime-code drift.
The artifact must not claim an earlier runtime-budget parent. Its payload hash
and identity are copied into every selection document and are reconstructed
from current checkpoints and calibration artifacts during protected replay.

The final exporter independently caps canonical output at exactly 50,000,000
bytes before it creates an output path. A selected bundle must carry the same
preselection identity; the exporter and gate recompute it rather than trusting
the embedded digest. After the builder copies the final bundle and all stdlib
modules into its temporary native candidate, it runs the same isolated
benchmark again and writes `V4_RUNTIME_BUDGET.json`. This second artifact binds
the final bundle byte count and SHA-256, stable identity, and preselection
artifact SHA-256. Benchmark failure removes the temporary tree and publishes no
candidate. The native loader verifies the sidecar and all manifest/gate/source
cross-bindings from single byte snapshots without rerunning a benchmark at
startup. A missing, changed, concurrently replaced, or inconsistent sidecar,
bundle, or gate document disables the neural policy before model loading,
leaving the already sanitized rule action as the runtime fallback.

These hashes are content-integrity and provenance bindings, not a cryptographic
signature against an actor who can rewrite the entire candidate, its loader,
and its manifest together. A formal release must therefore anchor the complete
candidate tree and runtime-budget sidecar in the task commit/tag and in the
later official-platform evidence. No formal candidate or such release anchor
exists at this incomplete stage.

The deterministic three-small-member test image is 11,169,406 bytes. On the
current host its seven measured override-path CPU times were 147.6-151.9 ms,
with a maximum of 151,897,359 ns. This is portability-chain smoke, not official
platform timing, deployment value, or strength evidence. The complete
neural-lab suite now passes 530 tests and all 33 national protocol tests pass.

The independent collection originally stopped after its atomic pass-75 state.
All six pass-76 tasks and their appended row tails were complete, but the pool
snapshot and self-hashed collector state had not advanced. The failure window
coincided with replacement of the live ratings file between two reads by the
old collector. The strongest diagnosis was a transient missing-file read, not
resource exhaustion. Current collection code instead freezes one ratings byte
snapshot per pass and validates the persisted plan before execution.

After explicit operator authorization, the reviewed recovery advanced the exact
atomic prefix to pass 76 without launching a probe or reading the current
ratings file. Value train/validation/held-out counts are now 3,539/882/887 and
behavior counts are 17,122/4,845/2,424. The self-hashed legacy-recovery receipt
is
`f33e5cee103baab94bcc12c88afa5ab4b0e1ebbd3f1ae3e0589233755c64c41d`.
Recovery kept `probe_execution_count=0`, `read_current_ratings=false`,
`deployment_policy_value=false`, and `strength_evidence=false`. Pass 76 is data
continuity evidence only; formal model scaling, selection, gate, candidate
creation, and any strength claim remain prohibited until the source reaches an
exact 160/160 boundary.

`recover_legacy_oppmodel_collection.py` is the narrow recovery path for this
specific schema-4-to-5 interruption. Its default is read-only and it cannot
launch probes or read the replacement live ratings file. An apply requires an
external reviewed-expectations document that binds the schema-4 manifest and
state, every completed plan, the pass-76 plan, all six cumulative JSONL files,
the opponent registry, both collector code roots, and the archived ratings and
identity-migration receipt. It scans every row after the atomic state limit,
requires exact task/split/deck/bot-seed membership and unique row keys, and
requires a complete 12-value-row tail plus the reviewed behavior count through
hand 69 for every task. Publication holds the collector lock, first installs a
poison manifest, journals durable before/after images, and either publishes all
four metadata targets or rolls back without overwriting unknown concurrent
bytes. This recovery is deliberately pinned to collector schema 5 and pass-plan
schema 2. It cannot issue a schema-6 collection contract; that later scheduling
change requires its own reviewed migration and receipt.

The first 75 plans cannot be retroactively given ratings bytes that the
schema-4 collector never saved. Formal freeze therefore accepts that legacy
prefix only when the self-hashed recovery receipt binds every original plan,
the exact reviewed pool/data prefixes, the archived-ratings transition, and
the recovery/current-collector code roots. It replays every old task's split,
snapshot, tag, deck block, bot seed, and pool row with the current seed formula.
The recovered pass and every later pass still require the full current plan and
frozen-ratings schema. A bare old plan, a re-signed inconsistent receipt, or a
changed collector/probe/cross-hand code root remains a hard failure.

The recovery audit found exactly 72 value rows and 370 behavior rows in the
pass-76 tail: train added 48/256, validation added 12/73, and held-out added
12/41. Its archived-ratings SHA-256 is
`fef82eafd22fb7e5c4900e8b1bdd4ce898f7cf3d40ff6ffabc8034deb2b8a3f6`;
the legacy and schema-5 collector hashes bound by that historical audit are
respectively
`fea501c6fd5ad893d5c1f82ffdbad8b238ffa0dd5c3fb6aff81dac406e6184ee`
and `431ddf87dba9f3e7efbc7491d899689102d0e6adb051bc1aea53dee71dc6cc89`.

`migrate_oppmodel_collector_concurrency.py` is the only authorized schema-5 to
schema-6 scale transition. It requires the idle collector lock, the exact
pass-76 boundary, and the reviewed 1x4 source topology. Read-only mode is the
default. An explicit apply atomically changes only `schema_version`, `workers`,
`probe_workers`, and `collector_sha256` in the resume contract, and adds a
top-level `concurrency_migration` receipt with mode
`atomic_collector_concurrency_schema5_to_schema6_v1`. The receipt embeds and
hashes the complete previous manifest and collector state; it also binds the
first 76 pool rows, all pass plans 1 through 76, and the exact row count, byte
count, and prefix SHA-256 of each of the six cumulative JSONL files. It never
executes a probe, reads current ratings, or grants deployment or strength
authority.

Schema 6 was the reviewed 6x2 intermediate topology, capped at 12 concurrent
native matches. `migrate_oppmodel_collector_capacity.py` is the only authorized
schema-6-to-7 transition. It takes the idle collector lock at an exact atomic
boundary, preserves and replays the complete schema-5-to-6 receipt, and accepts
either the exact completed plan prefix or that prefix plus exactly one already
persisted next-pass plan. The receipt does not claim that the plan never ran:
it records `execution_status=unknown_no_published_output` and proves zero rows
from it entered the atomic prefix. The byte/hash-bound plan is safe to reuse on
resume because publication is idempotent at that boundary. A second extra plan,
temporary probe output, or data past `collector_state.json` fails closed.
Dry-run is read-only and apply uses an fsync-backed atomic manifest replacement.

The reviewed schema-7 topology is 6 outer workers by 4 probe workers, with at
most 24 native matches. It explicitly gives native probe children capacity
slots 4 through 27 from a 28-slot host namespace, reserving slots 0 through 3
for operator services. `runtime_capacity.py` remains backward compatible for
ordinary callers, whose default range is slots 0 through 11; the collector
passes the reserved layout through strict environment bindings so the unchanged
`national_native.py` consumer acquires 4 through 27. Invalid, noncanonical, or
out-of-range layout values fail closed. The schema-7 resume contract and
migration receipt bind the collector, probe, cross-hand helper,
`runtime_capacity.py`, and the consuming `national_native.py` code hashes.
Both value and opponent-action prefixes carry a receipt over the stable row
identity `(opponent, deck seed, bot seed, hand, hand decision index)`; duplicate
identities fail at migration build, receipt replay, final role freeze, and
protected-role provenance validation.

Formal role freezing replays passes 1 through 76 against the embedded schema-5
contract, the next historical segment against schema 6, and post-capacity
migration passes against schema 7. It reconstructs the complete legacy-recovery
to concurrency-migration to capacity-migration to current-code trust chain;
changing data, plans, ratings receipts, either topology boundary, reserved-slot
layout, or any bound producer/consumer code root fails closed.

The durable launch plan uses `systemd-run --user` to create a transient unit
named `neural-v4-collector.service`, with `Type=exec`, `Restart=no`, and
`KillMode=control-group`. It starts the collector from a dedicated pinned
runtime worktree with `--workers 6 --probe-workers 2`, the existing absolute
data directory, and the reviewed 70-hand/seed/split arguments. After the atomic
schema-7 migration, the reviewed resume arguments are `--workers 6
--probe-workers 4 --max-active-native-matches 24 --capacity-total-slots 28
--capacity-first-slot 4`. Numeric-library
thread variables remain fixed at two, no CPU affinity or CPU quota hides any of
the host's 32 cores, and
stdout/stderr append to the collector log while `progress.log` remains
the business-progress authority. `Restart=no` makes an integrity failure stop
for inspection instead of silently replaying work, while the control group
ensures an operator stop also terminates every probe child. The current user
manager has linger disabled, so this is durable across shell and Codex process
lifetimes while a user login remains, but not across a final logout or reboot;
full unattended reboot durability would separately require administrator
authorization for `loginctl enable-linger zzx`.

Capacity apply additionally requires the explicit collector unit name and does
not trust `.collector.lock` or `MainPID` alone. Immediately before building and
again immediately before manifest replacement, it machine-verifies a loaded
transient unit with `KillMode=control-group`, `Restart=no`, `MainPID=0`, and
`ActiveState=inactive`; every `cgroup.procs` in its control-group subtree must
be empty, and a same-UID `/proc` scan must find no long-run collector or native
probe command line containing the absolute source directory. The quiescence
proof is receipt-bound. The pre-publish check also revalidates the exact state,
pool prefix, completed plan set, optional next plan, all six data prefixes and
row identities, opponent registry and snapshots, temporary-file absence, and
code hashes, so a non-cooperating writer cannot race build-to-replace.

The collector now treats a probe timeout, nonzero exit, missing output,
noncompliant baseline, malformed JSONL, or any summary/JSONL mismatch as a hard
pass failure before cumulative rows or completion metadata are published. It
also prepends the pinned runtime root to the probe subprocess `PYTHONPATH` while
preserving any inherited entries; a real one-hand native TCP smoke produced one
value row and two behavior rows from an isolated temporary directory.
Regression coverage includes both migrations, three-topology formal replay,
planned-tail reuse, reserved-slot isolation, producer/consumer hash tamper
rejection, and probe fail-closed behavior.

## Candidate-Only Native V4 Ablation Contract

Native joint-policy diagnostics now have four canonical modes: the complete v4
policy, neural disabled, cross-hand history disabled, and outcome uncertainty
plus match-outcome selection disabled. `native_tcp_evaluate.py` applies the
selected diagnostic environment only to the candidate process in its current
seat. It explicitly clears all three v4 ablation variables for the opponent,
then reverses those mappings with the seats in the swapped leg. This prevents a
parent shell setting from contaminating either side. The per-process mapping
also clears legacy v3 ablations and explicitly binds or removes trace and
force-action controls, so a parent-shell probe cannot disagree with the report.
The legacy debug wrapper accepts the same cleanup mapping, while non-full modes
reject that wrapper and any request to label the run as strength evidence.

The outcome/uncertainty/match-off mode is a runtime component ablation, not a
retrained headless architecture. It skips calibrated match-outcome inference
and the formal win-first selector, removes match/tail lower-bound contribution,
and uses the immediate-hand mean in place of its uncertainty lower bound before
delegating to the existing value/response selector. Likewise, cross-hand-off
zeros the runtime history input; it is not a substitute for training and
calibrating the formal `cross_encoder=none` architecture. The modes cannot be
combined, and every exception still falls back to the already sanitized rule
action.

`summarize_v4_native_ablations.py` accepts exactly one raw native report for
each mode and discovers no ratings, role manifest, exposure ledger, or
protected dataset. It requires identical stable candidate and opponent trees,
the same deterministic non-overlapping deck and bot-seed plan, at least three
seed blocks, one to four workers, two compliant native 70-hand legs per row,
and zero wrapper, adapter, illegal-action, timeout, force-action, or process
failures. Missing hands, changed artifacts, changed identities, duplicate rows,
overlapping per-player bot-seed windows, or a candidate/opponent seat mismatch
fail closed. A single 70-hand leg value is also bounded by the 20,000-chip
per-seat stack and its paired-seat hand sum by 40,000 chips; impossible but
arithmetically self-consistent chip rows are rejected. Every raw report must
also carry the evaluator's explicit strength
result; a positive deployment, native-strength, protected-data, or
official-acceptance claim is rejected rather than copied into the summary. The
full-mode report alone may reuse a separately requested strength-compliance run
when that request passed with no errors; the resulting ablation summary remains
diagnostic and cannot inherit that status. All three non-full inputs must have
unrequested, false, error-free strength fields.

For every `(opponent, seed)` block, the forward and swapped legs remain together
in all resamples. The primary diagnostic is the paired difference between full
and ablated `net_chips > 0` indicators for the individual 70-hand legs. Both an
ordinary complete-seed-block bootstrap and an equal-opponent-stratified version
are emitted. Net-chip delta per hand and its corresponding intervals are
secondary only and cannot change the primary direction or ordering. The
self-hashed summary has an exact root schema and keeps diagnostic-only true and
all protected-data, eligibility, deployment, native-strength, official-EXE,
strength-evidence, and formal-release-evidence fields false. These diagnostics
cannot be authoritatively validated from their self-hash alone: validation
requires all four raw report byte streams and recomputes the entire identity,
counts, bootstrap intervals, chip diagnostics, directions, and controls for
exact equality. They can guide the eventual pass-160 architecture study;
they cannot open `policy_selection` or `policy_gate`, create a Bot, or establish
strength. After this diagnostic chain and its contamination hardening, the
complete neural-lab suite passes 576 tests, the focused native workflow suite
passes 86 tests, and all 33 national protocol tests pass.

## Dynamic Native Strength Plan And Outcome-First Verdict

The evaluator's former `strength_evidence.passed` field proved only that a
paired native run finished without protocol failures. A candidate could lose
every complete match and still receive that name. The v2 outcome-first receipt
now separates execution-contract and statistical gates. A strict
`--strength-evidence` request
passes only when every planned native leg is complete and compliant, the
ordinary complete-seed-block positive-rate bootstrap lower bound is strictly
above 50 percent, the equal-opponent-stratified lower bound is also strictly
above 50 percent, and every opponent's observed 70-hand positive rate is at
least 50 percent. A mechanically clean all-loss report is therefore an explicit
failure. Such requests also require at least 2,000 bootstrap resamples, and
report publication uses a same-directory fsync-and-rename rather than exposing
a partial JSON file.

`freeze_v4_native_strength_pool.py` freezes one evaluation before any match is
run. It accepts only the canonical
`.evolution_pok/web/core/results/glicko_ratings.json`, opens it through one
descriptor, rejects duplicate
keys, non-finite values, aliases, malformed rows, and unknown labels, and binds
the exact raw bytes plus file metadata. Eligible opponents are the intersection
of that snapshot with annotated `national-bot-vN` completion tags after durable
reaped versions are removed. There is no fallback list. Ranking uses unrounded
`r - 2*rd`, with version as a deterministic tie break; the latest eligible
completion is included when it is not already in the configured top pool.
An arbitrary candidate basename cannot mask a completed opponent: exclusion is
permitted only when the candidate path is the repository's actual
`bots/national_vN` directory. Classic opponents are materialized from the one
synchronized `HEAD == main == origin/main` Git tree, never from possibly dirty
working files. The original completion tag and the later mainline execution
tree are separately bound so protocol migrations remain visible without
allowing an uncommitted downgrade. Candidate identity covers all on-disk regular
files, including ignored sentinels or bytecode, plus empty directories and
effective execute/read modes. Classic-opponent identity instead covers exactly
the tracked Git files, directories implied by those paths, and Git execute
modes; ignored `.completed` files and Git-unrepresentable empty directories are
not part of an opponent snapshot. Both use typed length-delimited records. The
full output, including its plan, is fsynced
and chmod-read-only before an atomic no-replace publication. Exact root layout,
staging inode identity, cleanup identity, and parent-directory durability are
checked; concurrent destination creation cannot be overwritten.

Deck and bot-seed windows, paired 70-hand legs, one-to-four workers, the full
conservative Python evaluator/TCP runtime code closure, and all false-authority
fields are frozen with the snapshots. Bootstrap sample count and seed are also
pre-registered in the plan and both reports must repeat them exactly, preventing
post-result seed selection. Freeze-time input must be the canonical live ratings
file; later validation binds its raw bytes and canonical source path embedded in
the plan and does not require the live ratings file to remain unchanged. It also
requires every authority tag ref recorded at freeze time, the recorded mainline
and completion Git trees, the live code closure, and the read-only snapshots.
It accepts no policy role, ledger, held-out, selection, or gate input. A later tag
does not rewrite an already frozen historical plan, but every new invocation
re-reads the current lifecycle; deleting or moving any recorded ref invalidates
that plan.

Read-only mode bits are integrity checks for normal evaluation, not a security
boundary against candidate code running under the same Unix UID: that process
could chmod and perform an ABA mutation. A hostile-candidate strength claim
would require a separate UID, read-only mount, or equivalent sandbox. This
development verdict therefore remains false-authority and cannot substitute
for an independently anchored match ledger and official-platform evidence.

Ratings are mutable runtime input, so this document deliberately does not name
a reusable opponent list. Every freeze must re-read the then-current canonical
ratings and lifecycle tags; the plan itself records the exact raw snapshot,
hash, ranking, and selected versions used by that invocation.

`evaluate_v4_native_strength_verdict.py` consumes the raw frozen plan, one
`full` candidate report, and a `neural_off` report for the same content-bound
candidate snapshot and identical opponent/seed/seat/runtime plan. Its primary gate is the
candidate's absolute 70-hand positive rate under both cluster bootstraps and
for every opponent. Outcome uplift versus the rule baseline is reported as a
diagnostic but cannot replace the absolute gate. Only after that gate passes
does the secondary paired-EV contract matter: ordinary and equal-opponent-
stratified chips-per-hand interval lower bounds must both be strictly positive,
the point estimate must be at least +5 chips/hand, and no opponent may have a
negative candidate direct EV or a negative full-minus-rule mean. Forward and
swapped legs remain one complete seed block in every resample.

The verdict binds all three raw inputs by byte count and SHA-256 and can be
validated only by replaying them; recalculating a self-hash after editing a
count, CI, identity, direction, or pass flag does not validate. Even a passing
development classic-pool verdict keeps `strength_evidence`, deployment,
official-EXE, and formal-release fields false. Final blind opponents remain a
separate strength gate. Official compliance separately requires a signed
`official-full-v5` certificate covering five 70-hand self-play rounds and three
70-hand rounds against one eligible opponent; official chip/outcome results
retain zero weight in strength, ratings, and H2H.

This replay proves content consistency, not match authenticity against an actor
who can rewrite both raw reports and all their self-declared native receipts.
A formal strength claim therefore still needs an independently anchored local
match ledger plus the task commit/tag. Publication separately requires the
signed `official-full-v5` compliance certificate; that certificate is not
strength evidence. The current phase intentionally publishes neither authority.
A real three-seed-block, eight-opponent, four-worker freeze-only smoke exercises
the then-live dynamic pool and binds every opponent to the synchronized
`HEAD == main == origin/main` commit; the ephemeral artifact records the exact
ratings hash, versions, commit, and tree digests. It launches no matches and is
removed after validation. No formal plan or verdict is retained for incomplete
v4 data, and no Bot is created by this tooling. With the freeze/report/verdict
integration and adversarial contract
tests included, the complete neural-lab suite passes 646 tests, all 33 national
protocol tests pass, and the focused national registry/native/runtime workflow
shard passes 130 tests.

## Literature Recheck And Search Direction

The architecture decision is also constrained by established imperfect-
information poker results. DeepStack combines depth-limited continual
re-solving with a learned counterfactual value function rather than asking one
network to emit the final action directly. ReBeL similarly combines self-play
reinforcement learning, a public-belief representation, and search for
imperfect-information games. These results support using the opponent-aware
network as a leaf/value and response model inside bounded online search, while
retaining the current rule policy as an immediate legal fallback. They do not
support assuming that a larger direct override MLP is sufficient.

Primary references:

- Moravcik et al., *DeepStack: Expert-Level Artificial Intelligence in
  No-Limit Poker*, 2017: <https://arxiv.org/abs/1701.01724>;
- Brown et al., *Combining Deep Reinforcement Learning and Search for
  Imperfect-Information Games*, 2020:
  <https://papers.nips.cc/paper/2020/hash/c61f571dbd2fb949d3fe5ae1608dd48b-Abstract.html>.

Quantile Regression Distributional RL provides a second relevant result: a
return distribution can be represented by multiple learned quantiles instead
of one mean. The current model's single q20 head is not a CVaR estimate and
cannot represent how severe outcomes below q20 are. The pass-98 data confirms
this matters for all-in labels. A future controlled model should predict a
fixed quantile grid, including q05/q10/q20, and derive a lower-tail aggregate;
it must still be selected by clustered policy EV, not quantile loss alone.
Conditional common-runout labels are required before interpreting extreme
quantiles causally. Reference: Dabney et al., *Distributional Reinforcement
Learning with Quantile Regression*, 2018:
<https://ojs.aaai.org/index.php/AAAI/article/view/11791>.

## Next Evidence

1. At an idle atomic boundary, dry-run and explicitly apply the reviewed
   schema-6-to-7 capacity migration, then let `neural-v4-collector.service`
   complete the remaining prefix through exactly 160/160 with the 6x4 topology
   and slots 4 through 27. Reuse a receipt-bound next-pass plan if one was
   already persisted. Stop through the systemd control group, never by killing
   only `MainPID`, and pass `--collector-unit neural-v4-collector.service` so
   both dry-run and apply bind the zero-process quiescence proof. Monitor the
   self-hashed atomic state and `progress.log`; never commit the corpus, probe
   traces, logs, or control receipts to Git.
2. Only after an exact 160/160 state, freeze the opponent-disjoint role datasets
   from that atomic prefix. Recheck the legacy-recovery, concurrency-migration,
   and capacity-migration chain, every plan and data prefix, and the exposure
   ledger before opening any protected role.
3. On GPU, compare multiple architectures and parameter scales with at least
   three seeds each. Preserve the predeclared early-stop, model-calibration,
   policy-selection, policy-gate, and final-blind opponent boundaries; a failed
   role must not cause the next role to be read or hashed.
4. Build the formal multi-seed ensemble calibration artifact only from the one
   `model_calibration` role. Every member must bind its checkpoint and role
   manifest; calibration must not read `policy_selection`, `policy_gate`, or
   held-out/final-blind data.
5. Run protected policy selection with the shared `win_first_policy_v4.py`
   aggregation and scoring implementation. Enforce the predeclared probability,
   rule-uplift, immediate-hand, and chip-LCB qualifications and their exact
   outcome-first lexicographic order, using both ordinary match-cluster and
   opponent-stratified bootstrap.
6. Open `policy_gate` only after selection passes, and gate the frozen joint
   policy on real observed 70-hand match clusters with both bootstrap schemes.
   Keep `deployment_policy_value=false` and `strength_evidence=false` throughout
   incomplete collection, training, calibration, and failed selection/gate
   paths.
7. Only a passing protected chain may feed the bounded exporter, native wrapper,
   and candidate builder. Runtime load or inference failure must return the
   already sanitized rule action; no formal Bot exists until the separate native
   strength and official-platform requirements also pass.
8. Freeze each later classic-pool evaluation from the then-current ratings and
   eligible completion tags rather than reusing a static opponent list. Treat
   fresh non-overlapping paired 70-hand outcomes as primary strength evidence,
   net-chip magnitude as secondary, and official-platform results as compliance
   evidence with zero strength weight.
