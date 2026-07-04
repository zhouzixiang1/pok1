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

## External Clone Scan

Scanned shallow clones under ignored `external/`:

- `EricSteinberger/PokerRL`: useful for evaluation discipline. Its tournament
  runner evaluates both seats and reports a confidence interval, which matches
  the variance problem in 70-hand matches better than a single ordinary battle.
- `datamllab/rlcard`: its no-limit Hold'em abstraction uses fold, check/call,
  half-pot raise, pot raise, and all-in. This supports keeping the neural policy
  head compact and translating raise buckets into the national raise-to-total
  protocol only after legal-action validation.
- `tamlhp/deepbot-poker`: useful for feature design rather than runtime code.
  It combines street one-hots, equity, pot/call ratios, stack/blind context, and
  opponent action memory. A small recurrent opponent model is the main idea to
  borrow later; direct PyTorch/LSTM runtime is too heavy for portable bot
  submissions.
- `mzarejko/pokerBot`: Deep CFR implementation for HULH. It separates regret
  networks, average strategy memory, legal-action sampling, and periodic model
  checkpoints. This is a good template for an offline trainer, not for direct
  stdlib runtime.

Practical takeaways for this repo:

- Keep the national bot runtime native and stdlib-only where possible. Export
  neural weights to JSON and run a small MLP in pure Python.
- Use the neural model first as an advisor that can rescue narrow rule mistakes,
  then measure `raw_changed`, `final_changed`, and actual response mismatches.
- Move evaluation toward mirrored seat batches with confidence intervals before
  promoting a neural variant; one-seat samples can hide variance.
- Add sequence or opponent-memory features offline, but compile them into simple
  rolling counters for runtime.
- If moving beyond imitation, train Deep-CFR-style raise-bucket policies against
  the local engine, then pass every action through the native national sanitizer.

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

## Scale-Up Gate

- Immediate scale step: collect value or advantage targets instead of more
  threshold-tuning data for v025. Use deck plus bot RNG seeds for all new
  promotion and counterfactual-target runs, and collect 50k-200k decisions from
  multiple teachers, opponents, and seeds only after the target generator can
  produce reproducible local action-value labels with legal masks and JSON
  export.
- Promotion gate before larger models: a candidate should stay positive over
  at least 20 common-deck mirror pairs against its rule base, with median delta
  above zero and no protocol/illegal-action regression.
- Larger-model step: replace the pure policy classifier with a value or
  advantage head trained from outcome/self-play data. Deep-CFR-style regret
  replay is the preferred direction; direct LLM calls during play are not
  suitable for the 60-second national protocol.
