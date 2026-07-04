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

## 2026 Clone Scan Refresh

Scanned additional shallow clones under ignored `external/` on 2026-07-04:

- `uzlez/fullhouse-engine`: useful as a modern competition platform reference.
  It uses a strict validator, short decision budget, no network, read-only
  runtime data, and raise amounts as total bets. Lesson for this repo: keep the
  national bot package self-contained, validate protocol formatting separately,
  and treat invalid runtime behavior as an auto-fold risk.
- `advitrocks9/fullhouse-bot`: most relevant implementation reference. It uses
  offline MCCFR/Deep-CFR training, a fixed 51-feature/5-action runtime contract,
  pure NumPy inference from packaged `.npz` weights, and benchmark promotion by
  confidence interval plus held-out opponents. It explicitly rejects stronger
  checkpoints whose feature/action shape cannot be served. Lesson: our next
  neural line should be a native blueprint with stable features and action
  masks, not an advisor overlay that changes a few rule decisions.
- `dberweger2017/deepcfr-texas-no-limit-holdem-6-players`: useful for trainer
  workflow. It separates random-opponent training, self-play from checkpoints,
  mixed checkpoint-pool training, action mapping/fallback utilities, and an
  experimental opponent-modeling track. It is 6-player and dependency-heavy, so
  it is an offline trainer reference rather than national-runtime code.

Updated design consequence:

- Stop investing in larger teacher-action imitation alone. v013/v014-style
  weighted imitation still produced noisy or negative paired deltas.
- Add a leak-diagnostic tool before more model tuning: trace changed decisions
  by street, position, amount owed, pot size, action bucket, and hand delta.
- For the next serious neural bot, define one fixed contract first:
  `features -> legal action mask -> abstract action probabilities -> national
  raise-to-total sanitizer`. Train against that contract and export the exact
  same shape to runtime.
- Keep overlays explicitly labelled. A push/fold chart, preflop table, or
  rule-based safety fallback can be useful, but it should not hide whether the
  learned blueprint itself improved.

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
