# Research Notes

- DeepStack: continual re-solving plus a learned counterfactual value network.
  Lesson: use neural value/prior modules inside explicit legal-action control.
- Deep CFR / Single Deep CFR: neural approximators replace tabular regret and
  average strategy. Lesson: keep data collection, training, and runtime export
  separate.
- ReBeL: combines self-play RL with public-belief search. Lesson: range/belief
  features are more useful than calling an LLM during play.
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
