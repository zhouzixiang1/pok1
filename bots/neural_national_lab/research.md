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

## Scale-Up Gate

- Immediate scale step: collect 50k-200k decisions from multiple teachers,
  opponents, and seeds; keep the six-action abstraction, legal masks, sample
  weights, and JSON export.
- Promotion gate before larger models: a candidate should stay positive over
  at least 20 common-deck mirror pairs against its rule base, with median delta
  above zero and no protocol/illegal-action regression.
- Larger-model step: replace the pure policy classifier with a value or
  advantage head trained from outcome/self-play data. Deep-CFR-style regret
  replay is the preferred direction; direct LLM calls during play are not
  suitable for the 60-second national protocol.
