# v111_national_v17_flop_call_redirect_tcp

Native national TCP flop all-in-to-call redirect version derived from v110.

Change:

- Keeps v110's risk-veto-only structure:
  - `allow_call = false`,
  - `allow_raise = false`,
  - `multi_action_value_propose_enabled = false`.
- Keeps v110's flop-specific large-commit thresholds:
  - `large_commit_veto_flop_min_to_call = 2000`,
  - `large_commit_veto_flop_min_pot = 5000`,
  - `large_commit_veto_flop_max_rule_value = -0.20`,
  - `large_commit_veto_flop_min_best_margin = 0.45`.
- Adds a narrow flop all-in redirect:
  - only after the large-commit veto has already triggered,
  - only when the original rule label is `allin`,
  - return `call` instead of `fold` when the learned call value is at least
    `0.05`, at least `0.25` above fold, and at least `0.45` above the original
    all-in value.

Rationale:

- v110 turned v108's seed block `2026074000` full-pool result from `-31443` to
  `+327367` and made the two-block full-pool aggregate positive against every
  opponent.
- v110 still had a small v2 seed block `2026074000` deficit (`-2250`).
- v110 trace on v2 seed block `2026074000` showed the only neural changes were
  10 flop `allin -> fold` vetoes. These likely saved all-in losses, but the
  learned value head often preferred `call` over both `fold` and the original
  all-in.
- This version tests whether using that learned call value can reduce v2's
  remaining deficit without reintroducing broad neural proposals.

Status:

- Must be evaluated against v2 seed block `2026074000` first. If it beats v110
  there, run v2/v3 and current-top8+v7 on both seed blocks before promotion.
