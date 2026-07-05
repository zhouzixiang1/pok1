# Research Notes

- DeepStack: continual re-solving plus a learned counterfactual value network.
  Lesson: use neural value/prior modules inside explicit legal-action control.
- Deep CFR / Single Deep CFR: neural approximators replace tabular regret and
  average strategy. Lesson: keep data collection, training, and runtime export
  separate.
- Deep Predictive Discounted CFR (2025): neural CFR can approximate stronger
  discounted/clipped regret updates with variance-reduced advantage samples.
  Lesson: the next trainer should save advantage/regret targets, not only
  teacher action labels.
- ReBeL: combines self-play RL with public-belief search. Lesson: range/belief
  features are more useful than calling an LLM during play.
- RL-CFR (2024): learns action abstraction for HUNL and reports gains over
  fixed-abstraction baselines. Lesson: raise buckets should become trainable
  outputs, then be translated through the national raise-to-total sanitizer.
- RLCard: no-limit Hold'em uses a small action abstraction: fold, check/call,
  half-pot raise, pot raise, all-in. This is a practical first policy head.
- `noambrown/poker_solver`: useful reference for a later river-only CFR
  override with bet-size abstraction.

First implementation: distill strong rule bots into a compact MLP that predicts
the 6-class action abstraction, then use it conservatively as a runtime advisor.

## Literature Refresh 2026-07-04

- DeepStack (`https://arxiv.org/abs/1701.01724`) and ReBeL
  (`https://arxiv.org/abs/2007.13544`) both reinforce that neural modules work
  best as value/search components inside explicit imperfect-information
  reasoning, not as direct LLM/runtime policy calls.
- Deep CFR (`https://arxiv.org/abs/1811.00164`) and Single Deep CFR
  (`https://arxiv.org/abs/1901.07621`) train from sampled regret/value targets
  over many traversals. This is a better match for the current bottleneck than
  binary filtering from a few dozen observed interventions.
- RL-CFR (`https://arxiv.org/abs/2403.04344`) frames action abstraction choice
  as expected payoff improvement over a default action. That directly matches
  the counterfactual rollout data this repo is now collecting: every candidate
  raise should be labeled by delta versus the rule/default action, then grouped
  into trainable raise buckets before sanitizer conversion.
- Deep Predictive Discounted CFR (`https://arxiv.org/abs/2511.08174`) points
  toward variance-reduced, discounted regret targets. The practical lesson here
  is to scale target quality and coverage before model size; v027/v028 failed
  because the gate mis-scored rare useful raises, not because the MLP was too
  small.

## External Clone Scan

The July 2026 scan keeps full reference clones under ignored
`bots/neural_national_lab/external/` and commits only distilled notes:

- `EricSteinberger/Deep-CFR`: useful for the offline training architecture, not
  for direct runtime code. `DeepCFR/workers/driver/Driver.py` creates learner
  actors plus per-player parameter servers; `TrainingProfile.py` separates
  inference, training, parameter-server devices, traversal count, mini-batch
  count, and reservoir sizes; `workers/la/buffers/*ReservoirBuffer.py` stores
  public observation, range index, legal action mask, iteration weight, and
  advantage or average-strategy targets.
- `EricSteinberger/PokerRL`: useful for engineering discipline. It wraps local
  workers for Ray-style multi-core/cluster execution, has no-limit bet sets as
  pot-fraction action abstractions in `PokerRL/game/bet_sets.py`, and evaluates
  head-to-head by playing both seats and reporting a 95 percent confidence
  interval in `PokerRL/game/AgentTournament.py`.
- `google-deepmind/open_spiel`: useful as a compact Deep CFR and CFR target
  reference. `open_spiel/python/pytorch/deep_cfr.py` separates advantage memory
  from strategy memory, uses reservoir sampling, applies regret matching over
  legal actions, and trains with much larger buffers than the current p059-p105
  diagnostic sets. Its CFR/MCCFR algorithms confirm that the useful target is a
  legal-action advantage vector, not a single binary gate label.
- `datamllab/rlcard`: useful as a small no-limit action menu reference. Its
  no-limit Hold'em environment uses `fold`, `check/call`, `raise_half_pot`,
  `raise_pot`, and `all_in`. This matches the current six-label contract well
  enough for the next counterfactual target generator.
- `tansey/pycfr` and `b-inary/poker-cfr`: useful for CFR sanity checks and
  fast small abstractions. pyCFR explicitly warns that full Texas Hold'em is
  too slow in pure Python; poker-cfr shows the value of precomputed heads-up
  equity and multi-threaded CFR+ for preflop/push-fold abstractions.
- `facebookresearch/REBEL`: useful as architecture evidence only. The official
  repository contains a Liar's Dice implementation, not HUNL poker code. The
  transferable ideas are CPU/GPU data generation, TorchScript value export,
  recursive/subgame solving, and public-belief-style features.

Practical takeaways for this repo:

- Keep the national bot runtime native and stdlib-only where possible. Export
  neural weights to JSON and run a small MLP in pure Python.
- Move from one-off binary gates to replayable multi-action targets:
  `legal_mask + action_delta/regret_vector + sampled_strategy`. This should be
  generated at one decision point across the same deck and bot RNG seeds.
- The first local version of that target path is
  `tools/multi_action_counterfactual_probe.py`: it enumerates the fixed
  RLCard-style labels, sends only sanitized final actions into the local judge,
  and records both fixed-label vectors and unique final-action branch counts.
  Use it to build small verified vector-target shards before committing to a
  larger actor/learner rewrite.
- `tools/multi_action_shard_runner.py` turns that into the first parallel actor
  path. It preserves the fixed six-label target contract while separately
  evaluating off-menu rule raises as the `delta_vs_rule` baseline, which avoids
  confusing action abstraction with protocol or rule-strategy raise sizing.
- Build a lightweight actor-learner loop before reaching for Ray: actor workers
  scan divergence windows, enumerate abstract legal actions, run
  counterfactual branches, and append reservoir-style JSONL shards; the learner
  consumes those shards on CUDA and exports JSON weights.
- Use PokerRL/RLCard-style action abstraction, but adapt it to national
  protocol semantics. `raise X` is raise-to-stage-total, not a delta; off-tree
  opponent raise sizes should be included in the current legal menu before any
  nearest-bucket projection.
- Add public-belief/range features incrementally: range buckets, board texture,
  betting line, showdown-revealed correction, and opponent fold/call/raise
  frequencies. Do not call an LLM or run a large PyTorch model during play.
- Promotion remains paired common-deck evaluation with both seats, bot RNG
  seeds, multi-opponent smoke, median and CI checks, and national protocol
  alignment tests.

## Advisor-Line Findings

- `trace_advice_outcomes.py` is now the preferred advisor diagnostic. It
  prevents threshold tuning from relying on aggregate match score alone by
  joining each neural intervention to that hand's actual chip delta.
- v011/v015/v016 show that sparse advisor overlays are not yet a reliable path
  to a clear edge. Fold-to-call rescue was usually negative in traced hands;
  narrowing it made v015 inactive; high-confidence call-to-fold veto looked
  plausible in counterfactual trace but failed paired evaluation.
- The next research branch should spend effort on data generation and native
  training targets: regret/advantage samples, legal action masks, and a stable
  action abstraction. More hand-written gates around the current imitation MLP
  are unlikely to produce a significant result.

## Fixed-Contract Blueprint Findings

- The Fullhouse/DeepCFR scans translated into a concrete local contract rather
  than copied runtime code: one feature encoder, six fixed action labels, legal
  masks at data/training/runtime, JSON-exported weights, and sanitizer-owned
  conversion to Botzone or national raise-to-total actions.
- `v017` showed that a broader learned policy overlay can be made native and
  protocol-safe, but the first teacher-imitation model over-intervened and lost
  badly in paired common-deck smoke.
- `v018` showed that narrowing the gate can avoid immediate large damage, but
  it becomes almost inactive. This reinforces the main research lesson: the
  bottleneck is target quality, not threshold polishing.
- Next neural work should generate training rows with outcome or advantage
  targets, especially for raise/no-raise and call/fold counterfactuals, then
  score every candidate against the rule base on common normal/mirrored decks.

## Outcome-Weighted Findings

- `collect_outcome_teacher_data.py` is the first step away from plain
  imitation. It still records the teacher action label, but weights each row by
  the teacher's final hand chip outcome. This gives the tiny MLP a weak
  advantage-style signal while preserving the fixed legal-mask contract.
- The first outcome-weighted model was useful only under narrow gates. v019 had
  a positive 4-pair paired mean but still crossed zero; v020 significantly lost
  when turn and pot-resize raises were allowed; v021 recovered a positive median
  by allowing only preflop/flop free-action raises.
- This supports the literature direction from Deep CFR, Single Deep CFR, and
  ReBeL: scale the target quality first. More data with legal masks,
  outcome/advantage targets, and common-deck paired evaluation is more valuable
  than making the current classifier wider.
- GPU training becomes worthwhile after the data pipeline can produce at least
  tens of thousands of rows with held-out paired validation. The runtime should
  remain small and protocol-safe: train larger value/advantage models offline,
  then distill or export compact weights for the national-native bot.
- The sharded h96 run confirms the split: CUDA/mini-batch training is useful
  offline, but the national bot should still load compact JSON weights and use
  sanitizer-owned action conversion at runtime.
- v022 trace showed the actual narrow-gate neural raises were positive, while
  the full match score still had large outliers. This means aggregate
  outcome-weighting is too noisy as the only target. The next trainer should
  predict local action advantage or value deltas so the model can learn which
  raises are causally good instead of inheriting whole-hand variance.
- v024 tested a trace-derived binary advantage gate and failed badly in paired
  evaluation. The failure mode is informative: observed hand delta is still too
  indirect, even when attached to candidate decisions. The next data collector
  should force one alternative action at a sampled decision, finish the hand on
  the same deck, and train on the resulting action delta.
- Recent CFR work points in the same direction: keep explicit legal-action
  search/control around the model, and use neural networks for batched value or
  advantage approximation. Directly increasing the policy classifier or asking
  an LLM during play does not address the credit-assignment problem.
- v025 crossed the action-level counterfactual gate but not the end-to-end
  paired gate: after 64 common-deck mirror pairs its mean stayed positive but
  the confidence interval still crossed zero. v026 showed that narrowing the
  runtime gate to the counterfactual-supported flop bucket alone did not create
  a reliable improvement. The next useful model change should improve target
  quality or action-value estimation, not only tighten thresholds.
- Replaying v025 trace diagnostics with fixed deck seeds did not reproduce the
  exact paired-match outliers. The deck is deterministic, but the rule bot
  simulation layer still uses process-local randomness. `paired_evaluate.py`
  and `trace_advice_outcomes.py` now support `--bot-seed-base`, which launches
  bot subprocesses through a seeded wrapper before running the bot script. A
  4-pair v022/v025 smoke with both deck and bot RNG seeds reran byte-identical;
  the first 32-pair deterministic v022/v025 check was neutral (`+95.09` chips
  per 70 hands, 95 percent CI `[-701.02, 891.21]`). Larger 96/128-pair
  promotion runs should use both seed families, but v025 itself no longer
  merits that spend.
- `counterfactual_rollout_probe.py` and `counterfactual_shard_runner.py` now
  accept `--bot-seed-base` as well. The scanned match uses seeded subprocesses,
  and every sampled decision gives the baseline and candidate forced branches
  the same branch-local bot RNG seeds. This closes the main reproducibility gap
  in the action-value target path; scaling data before this point would mix
  policy effects with random simulation drift.
- The target path now also seeds the local analysis RNG and exports runtime-
  compatible 70-dimensional advantage features. The first 62-row p64 dataset
  remained positive at the action-value level, but an h32 advantage gate trained
  on it failed in paired play: v027 over-filtered useful v025 raises, and v028
  showed lowering the threshold alone did not fix the issue. The immediate
  research lesson is to collect broader counterfactual coverage before training
  larger gates, especially rare positive raises that a classifier can otherwise
  mis-score as bad.
- Adding the v027 worst outlier seed to the counterfactual set produced v029,
  which repaired that specific over-filtering regression and tied v025 over the
  same 16-pair check. This is progress in the data loop, not a strategic edge:
  single-outlier replay can fix a known miss, but a useful advantage model needs
  systematic active learning over many disputed gates and opponents.
- The p192 counterfactual expansion and v030 nonnegative-good h32 gate show the
  current scale boundary. Action-level labels are now reproducible and strongly
  positive in the narrow flop low-raise bucket, but the first 64-pair
  deterministic end-to-end check against v025 still crossed zero
  (`+189.90` chips per 70 hands, 95 percent CI `[-484.94, 864.74]`, median
  `0`). Scale data coverage now; postpone larger models until a candidate
  clears paired promotion with a positive median and a confidence interval
  above zero.
- v032/v033/v034 show the next data-quality boundary. Drop-zero labels can
  avoid some over-suppression but do not solve rare outliers. Targeted p257
  repair fixed one v032 catastrophe, yet the repaired v034 still failed the
  16-pair gate and exposed an interaction problem: several individually
  positive flop raises can combine into a poor match path. The next useful data
  collector should support grouped intervention sets or match-scope interaction
  labels, then run them in parallel shards (`--workers 12` to `16` on this
  32-thread machine) before training a larger model.
- v035/v036/v037 turned grouped paired deltas into training rows, but mixing
  those labels into the same advantage head created a threshold conflict:
  thresholds that repair pair03 suppress the pair11 repair, while thresholds
  that preserve pair11 leave pair03 unchanged. The next model should separate
  concerns: keep the single-action advantage gate for local value, and add a
  second interaction-veto head trained only on grouped or match-path failures.

## Scale-Up Gate

- Immediate scale step: collect value or advantage targets instead of more
  threshold-tuning data for v025. Use deck plus bot RNG seeds for all new
  promotion and counterfactual-target runs, and collect 50k-200k decisions from
  multiple teachers, opponents, and seeds only after the target generator can
  produce reproducible local action-value labels with legal masks and JSON
  export. The v027/v028 result adds one more condition: the target set must
  cover both negative interventions and rare high-value positives, or a gate can
  look good in-sample while blocking the very raises that carried v025's best
  paired outcomes. The v029 result narrows the next step further: collect
  disputed-gate counterfactuals from many outlier seeds, not just random
  accepted interventions. The v030 result means this is the only scale-up that
  is currently justified. The v034 result adds a stricter condition: include
  grouped or match-scope interaction checks before trusting a larger single-
  action advantage model. The v035-v037 result refines that further: grouped
  labels should feed a separate interaction model, not just shift the existing
  gate's threshold.
- Promotion gate before larger models: a candidate should stay positive over
  at least 64 common-deck mirror pairs against its rule base, with median delta
  above zero, no large negative bucket, and no protocol/illegal-action
  regression.
- Larger-model step: replace the pure policy classifier with a value or
  advantage head trained from outcome/self-play data. Deep-CFR-style regret
  replay is the preferred direction; direct LLM calls during play are not
  suitable for the 60-second national protocol.
