# v109_national_v17_commit_veto_only_tcp

Native national TCP risk-veto-only ablation derived from v108.

Change:

- Keeps v108's stage-specific large-commit value veto, including guarded
  preflop veto for very large pots.
- Disables ordinary neural call/raise advice:
  - `allow_call = false`,
  - `allow_raise = false`,
  - `multi_action_value_propose_enabled = false`.

Rationale:

- v108 fixed v107's seed `2026073900` collapse, but seed `2026074000` still
  regressed hard against v2/v3.
- Trace of v108 on v2/v3 seed block `2026074000` showed that ordinary preflop
  `fold/call -> raise` proposals were the main neural-change loss source.
- This version tests whether the robust component is the learned large-commit
  risk veto itself, rather than broad preflop action proposals.

Status:

- Must be evaluated against the same current-top8+v7 pool on seed blocks
  `2026073900` and `2026074000` before promotion.
