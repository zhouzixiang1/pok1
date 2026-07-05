# v114_national_v17_wheel_ace_jam_veto_tcp

Native national TCP wheel-ace jam-call veto derived from v113.

Change:

- Keeps v113's filtered preflop small-jam veto and flop large-commit veto.
- Adds a narrow preflop jam-call veto:
  - stage is preflop,
  - original rule label is `call`,
  - opponent is all-in,
  - `to_call >= 15000`,
  - `pot >= 20000`,
  - hole cards are suited `A2` through `A5`.
- The veto returns `fold`.

Rationale:

- v113's remaining v2 seed block `2026074000` match losses were dominated by
  two identical suited wheel-ace jam calls: the bot raised `A2s`, faced a
  preflop all-in for about `17161`, called, and lost `-20000`.
- Force probes showed folding the seed `2026074000` occurrence changed that
  paired match from `-11934` to `+19036`. Folding the seed `2026074001`
  occurrence improved the single hand but worsened later trajectory, so this is
  an experimental rule that must pass full-pool regression before promotion.

Status:

- Must beat v113 on v2 seed block `2026074000`, then pass current-top8+v7 on
  seed blocks `2026074000` and `2026073900`.
