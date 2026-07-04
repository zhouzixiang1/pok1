# Experiments

## v001_v279_teacher214

- Base: `claude_v279`.
- Teacher: `claude_v214`.
- Integration: conservative neural advisor.
- Status: planned for first runnable snapshot.

## v002_v279_aggressive214

- Base: `claude_v279`.
- Teacher: `claude_v214`.
- Integration: lower advisor thresholds for exploration.
- Status: planned for first runnable snapshot.

## v003_v214_hybrid_prior

- Base: `claude_v214`.
- Integration: national-native version with neural prior hook.
- Status: planned for first runnable snapshot.

## v004_v279_guarded214

- Base: `v001_v279_teacher214`.
- Change: much stricter neural gating after v002/v003 showed harmful
  intervention in small samples.
- Runtime policy: high-confidence fold/call only in low-risk spots; raise and
  all-in are effectively disabled until the dataset contains enough examples.
- Status: added as the next stability baseline.

## v005_v279_call_rescue214

- Base: `v004_v279_guarded214`.
- Change: neural advisor can only rescue a rule fold into a cheap call when the
  model has very high call confidence. Fold, raise, and all-in overrides are
  disabled.
- Rationale: v004 and v003 showed that broad action replacement hurts; this
  version tests whether the network is useful only as a low-cost anti-overfold
  prior.

## v006_v279_call_rescue254

- Base: `v005_v279_call_rescue214`.
- Change: same call-rescue-only runtime policy, but the tiny MLP is distilled
  from `claude_v254` instead of `claude_v214`.
- Rationale: v254 exposed the largest gap for v279 in early battle samples, so
  this tests whether a teacher closer to that counter-strategy gives a more
  useful low-cost call prior.

## v007_v254_call_rescue254

- Base: `claude_v254`.
- Neural source: `teacher254_round1` MLP.
- Runtime policy: same call-rescue-only advisor as v006.
- Rationale: v006 showed that v254's call prior is harmful on a v279 base; this
  version tests whether the same neural prior works when paired with its native
  teacher-family rule base.

## v008_v254_free_check_rescue254

- Base: `v007_v254_call_rescue254`.
- Change: still disables paid broad action replacement, but allows the neural
  policy to rescue a rule fold into check/call when `to_call == 0`.
- Rationale: advisor analysis found a real battle spot where the rule strategy
  folded for free while the neural prior had `call` confidence `0.9998`; this is
  the lowest-risk place to make the neural module actually intervene.

## v009_v254_active_rescue254

- Base: `v008_v254_free_check_rescue254`.
- Change: active but bounded rescue gate. Free check rescue threshold lowered
  to `0.65`; paid call rescue is allowed only for `to_call <= 250`, pot-odds
  ratio `<= 0.28`, and call confidence `>= 0.90`.
- Rationale: v008 analysis showed 12 rule-fold/top-call candidates in one game
  but zero final interventions because thresholds were too strict.

## v010_v254_active_rescue254

- Base: `v009_v254_active_rescue254`.
- Change: paid rescue remains small, but `max_call_ratio` is widened from
  `0.28` to `0.32` and `max_call_chips` from `250` to `350`.
- Rationale: v009 analysis found a high-confidence call candidate at
  `to_call=128`, `pot=328`, but the ratio was about `0.281` and missed the
  previous threshold by less than one basis point.

## v011_v254_wide_call_rescue254

- Base: `v010_v254_active_rescue254`.
- Change: keeps the same v254 rule base and teacher254 MLP, but widens paid
  fold-to-call rescue to `call_conf >= 0.78`, `max_call_chips <= 500`,
  `max_call_ratio <= 0.30`, and `max_call_pot_ratio <= 0.07`.
- Rationale: v010's mirrored result was negative and its advisor analysis
  showed only `1/375` final interventions. v011 deliberately tests a more
  active neural-call prior while still blocking raises, all-ins, and neural
  folds.

## v012_v254_free_only_rescue254

- Base: `v011_v254_wide_call_rescue254`.
- Change: adds an `allow_paid_call_rescue` runtime switch and disables paid
  call rescue for this version. Free check/call rescue remains enabled.
- Rationale: v011's paired delta was positive but noisy. Its advisor examples
  suggested many interventions were free check/call rescues, so v012 isolates
  that safer part of the neural prior from paid call risk.

## v015_v254_flop_band_rescue254

- Base: `v011_v254_wide_call_rescue254`.
- Change: disables free check rescue and narrows paid rescue to flop-only,
  `to_call` in `[120, 140]`, `call_conf >= 0.90`.
- Rationale: `trace_advice_outcomes.py` found one positive v011 flop rescue
  around `to_call=136`, while preflop/free rescue and several small flop calls
  were negative. v015 tests whether keeping only that narrow band still creates
  measurable activity.

## v016_v254_call_fold_veto254

- Base: `v011_v254_wide_call_rescue254`.
- Change: disables call rescue and enables a high-confidence neural fold veto
  only when the rule strategy's final action is call and `to_call` is in
  `[100, 1200]`.
- Rationale: counterfactual trace showed several high-confidence
  `rule call -> neural fold` candidates on large losing hands. v016 tests that
  veto separately from the earlier fold-to-call advisor.

## v017_v254_blueprint_contract254

- Base: `v016_v254_call_fold_veto254`.
- Change: first fixed-contract blueprint attempt. The data collector writes
  legal action masks, the trainer exports `tiny_mlp_policy_v2` with
  `blueprint_policy_v1`, and runtime uses a fixed six-action abstraction before
  handing every candidate through the normal sanitizer.
- Runtime policy: allows bounded neural fold, call, and raise proposals under
  confidence and price gates. The native TCP entry also calls the same neural
  policy, so Botzone JSON and national-native execution share the learned
  action layer.
- Safety: if neural advice raises an exception, both `main.py` and
  `national_bot.py` fall back to the rule action and then sanitize; they do not
  continue with an unclean positive action.

## v018_v254_flop_raise_blueprint254

- Base: `v017_v254_blueprint_contract254`.
- Change: keeps the fixed-contract model and native TCP integration, but
  disables neural fold/call/all-in and permits only high-confidence free-action
  flop raises from a rule check/call.
- Rationale: v017's broad blueprint gate was active but negative in the first
  paired smoke. Trace suggested the least bad slice was small flop pressure
  when `to_call == 0`; v018 isolates that slice while preserving v017 for
  replay.

## v019_v254_outcome_blueprint_mix254

- Base: `v018_v254_flop_raise_blueprint254`.
- Change: replaces the teacher-imitation blueprint weights with
  `policy_outcome_weighted_round1.json`, trained from teacher decisions weighted
  by the teacher's final hand outcome. Runtime re-enables bounded neural folds
  and preflop/flop free-action raises, while keeping calls and all-ins disabled.
- Rationale: v017 showed that broad imitation was harmful and v018 was nearly
  inactive. v019 tests whether outcome-weighted labels can make the learned
  action prior useful without changing the fixed contract or native TCP path.

## v020_v254_outcome_raise_resize254

- Base: `v019_v254_outcome_blueprint_mix254`.
- Change: disables neural folds, widens raise sizing, and allows turn
  free-action raises plus `raise_pot` rule-label resizing.
- Rationale: v019 trace showed positive-looking blocked raise candidates,
  especially around free-action pressure. v020 deliberately tests whether
  broader raise resizing is a scalable path.

## v021_v254_outcome_raise_only254

- Base: `v019_v254_outcome_blueprint_mix254`.
- Change: keeps the outcome-weighted model but disables neural folds, leaving
  only high-confidence preflop/flop free-action raises from rule call/check.
- Rationale: v019 trace showed two neural folds on small losing hands and
  mostly positive flop free-action raises. v021 isolates that narrower signal
  while preserving v019 and v020 as replayable comparison points.

## v022_v254_sharded_h96_raise_only254

- Base: `v021_v254_outcome_raise_only254`.
- Change: keeps the narrow raise-only runtime gate but replaces the weights
  with a 96-hidden-unit model trained from a sharded 5608-row outcome-weighted
  dataset. The trainer now supports mini-batches and `--device auto/cpu/cuda`;
  this run used CUDA and exported JSON weights for the stdlib runtime.
- Rationale: v021 had a positive but outlier-sensitive signal. v022 tests
  whether a larger multi-teacher dataset and modestly wider MLP can make the
  same narrow gate more stable without changing protocol behavior.

## v023_v254_flop_resize_h96_254

- Base: `v022_v254_sharded_h96_raise_only254`.
- Change: keeps the same h96 weights but makes the gate flop-only and allows
  neural raise proposals when the rule action is already a raise. Preflop,
  turn, paid-call, fold, and all-in neural interventions remain disabled.
- Rationale: v022 trace showed actual flop free-action raises were positive
  while many blocked turn/preflop candidates were dangerous. v023 tests only
  the smallest safe-looking resize expansion.

## v024_v254_advantage_gate_h96_254

- Base: `v022_v254_sharded_h96_raise_only254`.
- Change: adds a second JSON-exported MLP, `advantage_weights.json`, that
  vetoes policy-model suggestions unless a trace-trained binary gate predicts
  positive local hand delta. The gate only filters neural advice; it does not
  create actions and every surviving action still passes the normal sanitizer.
- Rationale: v022 trace showed actual gated raises were positive while
  aggregate match results were noisy. v024 tests whether a small advantage
  classifier trained from v019/v022 trace candidates can reduce harmful
  interventions.

## Round 1 Notes

- Training data: `teacher214_round1.jsonl`, 272 teacher decisions from
  `claude_v214` against `claude_v279` and `claude_v254`.
- Training metrics: train accuracy 0.91, validation accuracy 0.71, average
  confidence 0.90.
- A single ordinary 70-hand battle for `v001_v279_teacher214` vs `claude_v279`
  completed; v001 lost that one-game sample.
- A single ordinary 70-hand battle for `v004_v279_guarded214` vs `claude_v279`
  completed; v004 won that one-game smoke sample.
- The mirror evaluation runner was terminated by SIGTERM in this environment,
  so current battle evidence is only a smoke/small-sample signal.

## Round 2 Notes

- `v004_v279_guarded214` vs `claude_v279`, ordinary battle 3 games:
  `[-50, -72, -3001]`, mean `-1041`.
- `claude_v279` vs itself under the same ordinary-battle setup:
  `[10075, 47, 1032]`, mean `3718`, so v004 is materially below the local
  baseline in this non-mirrored sample.
- `claude_v214` vs `claude_v279`, ordinary battle 3 games:
  `[10139, -9731, 143]`, mean about `184`.
- `v003_v214_hybrid_prior` vs `claude_v279` had a first completed game of
  `-913` before the long run was interrupted, so v003 is not promising without
  tighter gating.
- `v005_v279_call_rescue214` vs `claude_v279`, ordinary battle 3 games:
  `[-364, -785, 111]`, mean `-346`.
- `v005_v279_call_rescue214` vs `claude_v254`, two separate 2-game batches:
  first `[-5677, 11766]`, second `[-19992, 127]`, combined mean about `-3444`.
- `claude_v279` vs `claude_v254`, ordinary battle 2 games:
  `[-16717, -180]`, mean `-8448.5`; v005 reduced the loss in this small sample
  but did not produce a stable positive edge.
- `teacher254_round1.jsonl`: 210 teacher decisions from `claude_v254` against
  `claude_v279` and `claude_v214`; train accuracy `0.85`, validation accuracy
  `0.71`, average confidence `0.84`.
- `v006_v279_call_rescue254` failed: vs `claude_v254` `[-14859, -20018]`, vs
  `claude_v279` `[-4208, -95]`. This suggests the v254 teacher prior is not
  portable to the v279 base under call-rescue gating.
- `v007_v254_call_rescue254` vs `claude_v279`, ordinary battle 4 games across
  two batches: `[630, -51, 10049, 6420]`, combined mean `4262`, record `3-1`.
- Same first batch baseline, `claude_v254` vs `claude_v279`: `[-136, 16100]`,
  mean `7982`. v007 is positive against v279 but not proven better than its
  v254 base.
- Current best neural variant by positive battle evidence: `v007_v254_call_rescue254`.
  Evidence is still non-mirrored and low-sample; the next step is larger
  incremental battle batches and a faster mirror-compatible evaluator.

## Round 3 Notes

- `analyze_advice.py` was added to measure whether neural advisors actually
  change actions.
- `v007_v254_call_rescue254` advisor analysis, one ordinary game vs
  `claude_v279`: `final_changed=0/148`; neural top labels included `call=50`,
  but the current gates prevented all action changes.
- In that same analysis, one rule fold had neural top `call` with confidence
  `0.9998` while `to_call=0`; v008 was created to rescue exactly this class of
  free-check spots.
- `v008_v254_free_check_rescue254` advisor analysis, one ordinary game vs
  `claude_v279`: `final_changed=0/113`, but found 12 rule-fold/top-call
  candidates. Several were free checks with call confidence around `0.70-0.78`;
  two paid calls had very high confidence at `to_call=201` and `to_call=50`.
  v009 uses these observations to make bounded interventions.
- `v009_v254_active_rescue254` advisor analysis, one ordinary game vs
  `claude_v279`: `final_changed=0/137`; one high-confidence paid call candidate
  had `to_call=128`, `pot=328`, `call_conf=0.9905`, but ratio just exceeded
  `0.28`. v010 widens only that small-call ratio.
- `v010_v254_active_rescue254` advisor analysis, one ordinary game vs
  `claude_v279`: `final_changed=1/127`, type `fold_to_call`. The intervention
  changed a rule fold at `to_call=50`, `pot=150`, call confidence `0.99998`,
  and the actual bot response matched the advised call.
- `v010_v254_active_rescue254` vs `claude_v279`, ordinary battle 3 games:
  `[1477, 5133, 13500]`, mean `6703`, record `3-0`.
- `v010_v254_active_rescue254` vs `claude_v254`, ordinary battle 2 games:
  `[14661, -382]`, mean `7139.5`, record `1-1`.
- Current best neural variant with confirmed intervention: `v010_v254_active_rescue254`.
  It has actual advisor changes plus positive small-sample results against both
  `claude_v279` and `claude_v254`; next proof step is larger and mirrored
  batches.

## Round 4 Notes

- `evaluate_versions.py` now reports sample count, standard deviation, standard
  error, 95 percent CI, and normalized `mean_per_70_hands`; mirror rows report
  mirror-pair net chips but normalize to one 70-hand match for comparison.
- Literature note updated with RL-CFR and Deep Predictive Discounted CFR:
  next training work should treat raise buckets/action abstraction and
  regret/advantage targets as learnable, not just imitate teacher actions.
- `v010_v254_active_rescue254` vs `claude_v279`, mirror 10 pairs:
  `[3096, -10632, 69, -1904, -3577, -17646, -944, 6624, -133, 7435]`.
  Mean per 70 hands: `-880.6`, 95 percent CI `[-3235.0, 1473.8]`, record
  `4-6`. This invalidates the earlier ordinary 3-game positive sample as
  insufficient evidence.
- `claude_v254` vs `claude_v279`, mirror 6 pairs:
  `[-1459, 8208, -2187, 631, 132, 4963]`. Mean per 70 hands: `857.3`,
  95 percent CI `[-759.8, 2474.5]`, record `4-2`. The v254 base remains a
  stronger reference than v010 in this short mirrored sample.
- `v010_v254_active_rescue254` advisor analysis, 3 ordinary games vs
  `claude_v279`: `final_changed=1/375`, type `fold_to_call`; this is too
  conservative to materially affect play.
- `v011_v254_wide_call_rescue254` advisor analysis, 3 ordinary games vs
  `claude_v279`: `final_changed=26/314`, all `fold_to_call`. The wider gate
  successfully makes the neural module active.
- `v011_v254_wide_call_rescue254` vs `claude_v279`, ordinary battle 3 games:
  `[6298, -1334, -40]`, mean `1641.3`, record `1-2`.
- `v011_v254_wide_call_rescue254` vs `claude_v279`, mirror 6 pairs:
  `[-3183, 58, -3795, 3418, 18744, -6860]`. Mean per 70 hands: `698.5`,
  95 percent CI `[-2981.0, 4378.0]`, record `3-3`.
- Current status: v011 is the best active neural-advisor variant by mirrored
  mean, but it is not statistically significant and does not yet prove an edge
  over the v254 base. Next step is common-deck paired evaluation and a trainer
  that learns action-bucket/regret targets rather than only a call-rescue prior.

## Round 5 Notes

- `paired_evaluate.py` was added for common-deck mirror-pair evaluation. It
  generates one `initdata`, runs the baseline and candidate over the exact same
  normal and mirrored decks, and reports paired delta CI normalized to 70 hands.
  This directly measures whether the neural layer improves its rule base.
- Smoke paired run, `v011_v254_wide_call_rescue254` vs `claude_v254`, both
  against `claude_v279`, 2 mirror pairs: paired deltas `[101, 306]`, mean
  `+101.8 chips/70`, 95 percent CI `[+1.3, +202.2]`. This was treated only as
  a smoke because the sample was too small.
- Larger paired run, same setup, 6 mirror pairs: deltas
  `[-1358, 674, -61, 12489, -1298, -1557]`, mean `+740.8 chips/70`,
  95 percent CI `[-1444.9, +2926.4]`. v011 has a positive paired mean but is
  driven by one large outlier and is not yet significant.
- `v012_v254_free_only_rescue254` advisor analysis, 3 ordinary games vs
  `claude_v279`: `final_changed=0/350`. This sample showed that free-only
  rescue can be inactive for long stretches.
- Smoke paired run, `v012_v254_free_only_rescue254` vs `claude_v254`, 2 mirror
  pairs: deltas `[5278, 1068]`, mean `+1586.5 chips/70`, but the result did not
  reproduce.
- Larger paired run, same v012 setup, 6 mirror pairs:
  `[-8585, -2210, -80, 1208, 1156, -838]`, mean `-779.1 chips/70`,
  95 percent CI `[-2249.3, +691.2]`. This rejects the free-only hypothesis for
  now.
- Current status: v011 remains the best neural-advisor candidate by paired mean,
  but the evidence is not yet strong enough to claim a clear edge. The next
  productive direction is a richer trainer: more teacher/self-play data plus
  action-bucket or regret-style targets, then paired evaluation against v254 and
  v279 on common decks.

## Round 6 Notes

- Added `trace_advice_outcomes.py`. It runs common-deck normal/mirror matches,
  attaches neural-advisor changes to per-hand chip deltas, and records
  high-confidence counterfactual candidates that were blocked by the runtime
  gate. This is modeled after the Fullhouse-style leak diagnosis workflow.
- `v011_v254_wide_call_rescue254` trace, 6 normal/mirror pairs vs
  `claude_v279`: 7 actual `fold_to_call` changes, changed-hand delta sum
  `-414`, while total bot0 delta was `-25387`. The actual advisor changes are
  sparse and slightly negative; most losses are not caused by the neural layer.
- `v011_v254_wide_call_rescue254` counterfactual trace, 4 pairs with
  `candidate_conf >= 0.90`: 28 actual `fold_to_call` changes with changed-hand
  delta sum `-3134`. It also exposed high-confidence `rule call -> neural fold`
  candidates on losing hands, which motivated v016.
- `v015_v254_flop_band_rescue254` advisor analysis and 6-pair trace: 0 final
  changes. The narrow positive-sample band became a no-op, so no paired
  promotion run was useful.
- `v016_v254_call_fold_veto254` advisor analysis, 3 ordinary games vs
  `claude_v279`: 2 final changes, both `to_fold`. Trace over 6 pairs found 8
  vetoes, all on small losing folded hands, but paired evaluation against
  `claude_v254` failed badly.
- `v016_v254_call_fold_veto254` vs `claude_v254`, common-deck mirror 6 pairs
  against `claude_v279`: paired deltas
  `[-20035, 684, -3817, -5751, 125, -1906]`, mean `-2558.3 chips/70`,
  95 percent CI `[-5637.0, +520.4]`. This rejects simple call-fold veto as an
  improvement.
- Current status: v011 is still the best neural-advisor candidate by paired
  mean, but it is not significant. The advisor approach is producing sparse,
  noisy, hard-to-credit interventions. The next serious attempt should move to
  a native fixed-contract blueprint trainer rather than more threshold tuning.

## Round 7 Notes

- Added `blueprint_contract.py` and upgraded the teacher-data/training path to
  carry a stable `blueprint_policy_v1` contract: feature vector, six action
  labels, legal mask, optional sample weight, and sanitized action conversion.
- `teacher254_blueprint_round1.jsonl`: 573 `claude_v254` teacher decisions from
  mirror sampling against `claude_v279` and `claude_v214`. Label counts were
  `fold=126`, `call=312`, `raise_half=67`, `raise_pot=58`, `raise_2pot=3`,
  `allin=7`.
- `teacher254_blueprint_round1_metrics.json`: 64-hidden-unit MLP, validation
  accuracy `0.730`, masked validation accuracy `0.739`, average masked
  confidence `0.882`.
- `v017_v254_blueprint_contract254` advisor analysis, 2 ordinary games vs
  `claude_v279`: `final_changed=8/182`, with `4` `to_raise` and `4` `to_fold`
  changes. This proved the fixed-contract runtime can make real interventions.
- `v017` trace, 1 common-deck normal/mirror pair before manual interruption:
  16 changes, 39 high-confidence counterfactual candidates, changed-hand delta
  sum `-572`, total bot0 delta `-264`. This is diagnostic only, not a full
  2-pair trace.
- `v017` paired smoke vs `claude_v254`, both against `claude_v279`, 2 mirror
  pairs: deltas `[-14083, 2473]`, mean `-2902.5 chips/70`, 95 percent CI
  `[-11014.9, +5209.9]`. This rejects the broad blueprint gate for now.
- `v018_v254_flop_raise_blueprint254` narrows v017 to flop-only free-action
  raises and disables neural fold/call/all-in. Advisor analysis, 2 ordinary
  games vs `claude_v279`: `final_changed=1/232`, type `to_raise`.
- `v018` paired smoke was manually interrupted after 1 mirror pair. The single
  completed pair delta was `+21` total chips, or `+10.5 chips/70`; this only
  shows the narrowed gate avoided v017's immediate large loss in that one pair,
  not that it improved strength.
- Current status: fixed-contract infrastructure is in place and native TCP uses
  the neural layer, but no neural blueprint variant has shown a significant
  edge. The next useful step is better targets, not another round of manual
  gate tuning: collect counterfactual/regret-style outcomes or train on larger
  self-play/teacher pools with held-out common-deck evaluation.

## Round 8 Notes

- Added `collect_outcome_teacher_data.py`. It keeps the same
  `blueprint_policy_v1` feature/action/mask contract, but assigns each teacher
  decision a sample weight from the teacher's final hand chip delta. This is a
  lightweight advantage-style target, not pure action imitation.
- `teacher_outcome_weighted_round1.jsonl`: 919 samples from `claude_v254` and
  `claude_v279` teachers against `claude_v279`, `claude_v254`, and
  `claude_v214`. Label counts were `fold=242`, `call=437`,
  `raise_half=71`, `raise_pot=93`, `raise_2pot=60`, `allin=16`.
- `teacher_outcome_weighted_round1_metrics.json`: 64-hidden-unit MLP,
  validation accuracy `0.592`, masked validation accuracy `0.592`, average
  confidence `0.771`. Accuracy is lower than the imitation model because the
  weighting deliberately de-emphasizes actions from losing hands.
- `v019` advisor analysis, 2 ordinary games vs `claude_v279`:
  `final_changed=14/257`, with `11` `to_raise` and `3` `to_fold` changes.
  Paired common-deck 4 mirror pairs vs `claude_v254`, both against
  `claude_v279`: deltas `[6706, 2767, 1896, -3959]`, mean `+926.2 chips/70`,
  95 percent CI `[-1231.4, +3083.9]`. This is positive but not significant.
- `v019` trace, 2 common-deck pairs vs `claude_v279`: 20 actual changes,
  changed-hand delta sum `+396`, total hand delta sum `-965`. Actual flop
  free-action raises were the only useful-looking slice; neural folds were
  small negative.
- `v020` advisor analysis, 2 ordinary games vs `claude_v279`:
  `final_changed=12/242`, all `to_raise`. Paired common-deck 4 mirror pairs:
  deltas `[830, -16234, -5780, -16347]`, mean `-4691.4 chips/70`, 95 percent
  CI `[-8817.5, -565.3]`. This significantly rejects broad turn/raise-pot
  resizing.
- `v021` advisor analysis, 2 ordinary games vs `claude_v279`:
  `final_changed=8/275`, all `to_raise`. Paired common-deck 4 mirror pairs:
  deltas `[102, 357, 309, 10760]`, median `+333`, mean `+1441.0 chips/70`,
  95 percent CI `[-1133.1, +4015.1]`. This is the best current outcome-weighted
  signal, but it is outlier-sensitive and not significant at 4 pairs.
- Current status: outcome-weighting plus a narrow free-action raise gate is
  more promising than teacher imitation and broad hand-tuned gates, but it is
  not yet a proven edge. The next scale step should enlarge data and paired
  evaluation first: target 50k-200k decisions, multi-teacher/multi-opponent
  sampling, and at least 20 common-deck mirror pairs before increasing model
  complexity or GPU training.

## Round 9 Notes

- Added `collect_outcome_shards.py`, a parallel sampler that splits
  teacher/opponent mirror battles into independent shards while preserving the
  same `blueprint_policy_v1` JSONL contract. This moves the pipeline toward
  large datasets without changing the runtime bot protocol.
- Updated `train_policy_mlp.py` with mini-batch training and optional device
  selection. The round-2 h96 run used `--device auto`, selected CUDA, and still
  exported plain JSON weights for the portable runtime.
- `teacher_outcome_weighted_round2_sharded.jsonl`: 5608 samples from
  `claude_v254`, `claude_v279`, and `v021` teachers against `claude_v279`,
  `claude_v254`, and `claude_v214`. Label counts were `fold=1394`,
  `call=2789`, `raise_half=652`, `raise_pot=613`, `raise_2pot=101`,
  `allin=59`.
- `teacher_outcome_weighted_round2_h96_metrics.json`: 96-hidden-unit MLP,
  validation accuracy `0.645`, masked validation accuracy `0.645`, average
  confidence `0.814`, trained with batch size `512` on CUDA.
- `v022` advisor analysis, 2 ordinary games vs `claude_v279`:
  `final_changed=7/257`, all `to_raise`.
- Common-deck 4-pair comparison on the same decks, both against `claude_v279`:
  `v021` deltas `[-5132, -2491, 159, 273]`, mean `-898.9 chips/70`;
  `v022` deltas `[785, 2419, 338, -13]`, mean `+441.1 chips/70`, 95 percent
  CI `[-85.8, +968.0]`. v022 is clearly better than v021 on this deck set, but
  not yet significant.
- `v022` larger 8-pair run vs `claude_v254`, both against `claude_v279`:
  deltas `[185, 6293, -137, -100, 251, -8730, 267, 18804]`, median `+218`,
  mean `+1052.1 chips/70`, 95 percent CI `[-1676.7, +3780.8]`. The mean is
  positive but dominated by both positive and negative outliers.
- `v022` 4-pair trace vs `claude_v279`: 14 actual changes out of 983 decisions,
  all `to_raise`; changed-hand delta sum `+1400` while total hand delta was
  `-19012`. Actual gated flop/preflop free-action raises were positive in this
  trace, but blocked preflop and turn raise candidates were strongly negative.
- `v023` advisor analysis, 2 ordinary games vs `claude_v279`:
  `final_changed=5/269`, all `to_raise`. Paired 4-pair result:
  deltas `[-119, 1734, 28, -1351]`, median `-45.5`, mean `+36.5 chips/70`,
  95 percent CI `[-585.0, +658.0]`. Allowing flop resize did not improve v022.
- Current status: v022 is the best scale-up candidate so far by median/trace,
  but still not a statistically clear edge. The next useful step is not more
  gate widening; it is a real value/advantage target that can separate positive
  neural raises from outlier-driven match results.

## Round 10 Notes

- Added `collect_advantage_trace_data.py` and `train_advantage_gate.py`.
  These convert existing trace candidate/change rows into a 70-feature binary
  dataset and train a small JSON-exported advantage gate. This was the first
  attempt to move from whole-hand outcome weighting toward a local action
  advantage filter.
- `advisor_advantage_v019_v022_trace.jsonl`: 250 samples from `v019` and
  `v022` traces, with 179 positive and 71 negative examples. This dataset is
  intentionally small and diagnostic; it is not enough to claim a robust value
  model.
- `advantage_gate_v019_v022_h32_metrics.json`: 32-hidden-unit binary MLP,
  validation accuracy `0.720`, average predicted good probability `0.600`,
  trained on CUDA with batch size `64`.
- `v024` advisor analysis, 2 ordinary games vs `claude_v279`:
  `final_changed=3/155`, all `to_raise`. The gate made the neural layer much
  more conservative than v022.
- `v024` paired common-deck 4-pair result vs `claude_v254`, both against
  `claude_v279`: deltas `[-5372, -22030, -10380, -6340]`, mean
  `-5515.2 chips/70`, 95 percent CI `[-9262.3, -1768.2]`. This significantly
  rejects the trace-derived binary gate.
- Current status: v024 is a useful negative result. A binary classifier trained
  on trace rows still inherits whole-hand outcome noise and can veto helpful
  neural raises. The next serious advantage attempt should collect explicit
  single-decision counterfactual rollouts on the same deck, then train from
  action delta rather than from observed hand delta.
