# v113_national_v17_filtered_small_jam_veto_tcp

Native national TCP filtered preflop small-jam veto derived from v112.

Change:

- Keeps v112's flop large-commit value veto and ordinary neural proposals
  disabled.
- Keeps v112's preflop small-pot all-in veto, but adds lower bounds:
  - `preflop_small_jam_veto_min_to_call = 150`,
  - `preflop_small_jam_veto_min_pot = 300`.
- Existing upper/value bounds stay unchanged:
  - `to_call <= 500`,
  - `pot <= 1000`,
  - learned all-in value at most `-0.30`,
  - best learned label at least `0.70` above all-in.

Rationale:

- v112 had the highest measured two-block EV, but its seed block `2026074000`
  W-L-D regressed because many prior draws became tiny losses.
- Trace-aligned deltas against v110 showed most small-loss regressions came from
  blind-level all-in vetoes at `to_call=50`, `pot=150`, typically changing
  `+100` into `-50`.
- The large v2 gain came from `to_call=183`, `pot=383` preflop all-in vetoes,
  changing two hands from `-20000` to `-100`.
- This version filters out the blind-level vetoes while preserving the v2
  high-value small-jam veto pattern.

Status:

- Must beat v112 on the full current-top8+v7 seed block `2026074000`, then be
  checked on seed block `2026073900`.
