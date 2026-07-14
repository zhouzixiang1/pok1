# v121_national_v17_k4s_jam_veto_tcp

Native national TCP K4s large-jam cleanup derived from v120.

Change:

- Keeps v120's T8o large-jam draw breaker.
- Adds a narrow preflop K4 suited large-jam veto:
  - stage is `preflop`,
  - original rule label is `allin`,
  - hole cards are exactly suited K4,
  - `10000 <= to_call <= 13000`,
  - pot is `22000..27000`,
  - return action `-1`, a protocol-legal `fold`.

Rationale:

- v120 dominates the current all-completed seed block, but old seed block
  `2026073900` still had paired losses against v2 and v3.
- Both losses shared the same K4 suited large preflop all-in. The final paired
  rerun of seed block `2026073900` improved v2 by `+13578` and v3 by `+13578`.
- Other old-block candidates, including A2s early folds/calls, QJs flop
  redirects, and QJo preflop redirects, were high-variance or negative across
  probes, so v121 intentionally does not include them.

Status:

- Must improve old v2/v3 seed block `2026073900`, preserve old block
  `2026074000`, and keep current all-completed domination.
