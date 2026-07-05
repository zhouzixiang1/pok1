# v112_national_v17_preflop_small_jam_veto_tcp

Native national TCP preflop small-jam veto version derived from v110.

Change:

- Keeps v110's flop large-commit value veto.
- Keeps ordinary neural call/raise proposals disabled.
- Adds `preflop_small_jam_veto`, a narrow learned-value veto for preflop
  all-in rule actions when:
  - `to_call <= 500`,
  - `pot <= 1000`,
  - the learned value for the original all-in label is at most `-0.30`,
  - the best learned label is at least `0.70` above the all-in label.
- The veto returns `fold`; it does not add broad neural raises or calls.

Rationale:

- v110's two-block full-pool result was positive against every opponent, but
  v2 seed block `2026074000` was still slightly negative.
- v110 trace on v2 seed block `2026074000` showed two `-20000` hands caused by
  preflop small-pot all-in actions with learned all-in value around `-0.45`.
- The same trace bucket contained six such decisions with aggregate hand result
  `-39600`, suggesting a narrow all-in veto can improve v2 without touching
  normal preflop play.

Status:

- Must beat v110 on v2 seed block `2026074000` before any wider promotion.
