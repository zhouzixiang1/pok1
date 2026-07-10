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
manifest and sweep summary, and marks `deployment_eligible` true only when both
gates pass. It exits nonzero on failure unless an explicit diagnostic override
is supplied; that override never changes deployment eligibility.

A two-architecture CUDA smoke exercised the complete
train -> calibration -> validation selection -> held-out path. A second run
required one post-selection override where the tiny smoke model made none: it
returned exit code 1 while preserving all model, manifest, summary, and gate
failure artifacts. This verifies pipeline semantics only; the synthetic smoke
data and relaxed selection thresholds are not strength evidence.

The live 70-hand collector had completed 9 of 160 passes at the time of this
update, with 412 train, 108 validation, and 108 held-out value rows plus 2,604
opponent-action rows. Collection remains active, and these append-only files
must not be treated as a frozen training dataset.

## Next Evidence

1. Run validation-only ranking-weight ablations on larger match-cluster counts.
2. Repeat the full GRU, GRU+MoE, Deep Sets, and Transformer scaling grid after
   the 160-pass dataset is frozen.
3. Use calibration only for output calibration and coverage diagnostics; keep
   held-out completely outside architecture, recipe, and policy selection.
4. Use offline clustered policy selection before creating an active TCP bot.
5. Treat native paired classic-pool EV, not these supervised metrics, as the
   eventual strength criterion.
