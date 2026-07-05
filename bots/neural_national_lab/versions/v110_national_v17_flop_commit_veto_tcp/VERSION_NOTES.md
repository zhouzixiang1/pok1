# v110_national_v17_flop_commit_veto_tcp

Native national TCP flop large-commit risk-veto version derived from v109.

Change:

- Keeps v109's risk-veto-only structure:
  - `allow_call = false`,
  - `allow_raise = false`,
  - `multi_action_value_propose_enabled = false`.
- Extends `large_commit_veto_stages` to include `flop`.
- Adds flop-specific large-commit thresholds:
  - `large_commit_veto_flop_min_to_call = 2000`,
  - `large_commit_veto_flop_min_pot = 5000`,
  - `large_commit_veto_flop_max_rule_value = -0.20`,
  - `large_commit_veto_flop_min_best_margin = 0.45`.

Rationale:

- v109 was positive against v2/v3 on seed block `2026073900`, but still lost
  seed block `2026074000`.
- Recomputed v109 trace analysis on v2/v3 seed block `2026074000` showed zero
  neural changes under v109, so the remaining regression came from rule actions
  that the veto never touched.
- The same trace showed flop all-in risk decisions with `to_call >= 2000`,
  `pot >= 5000`, `rule_value <= -0.20`, and `best_margin >= 0.45` covered 22
  unique hands with aggregate hand result `-320000` in that block.
- This version tests whether the learned value head can veto those flop
  large-commit traps while preserving v109's seed `2026073900` strength.

Status:

- Must be evaluated against v2/v3 seed blocks `2026073900` and `2026074000`,
  then the current-top8+v7 rule pool before promotion.
