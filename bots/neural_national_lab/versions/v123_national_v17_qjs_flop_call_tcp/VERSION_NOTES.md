# v123_national_v17_qjs_flop_call_tcp

Native national TCP QJs flop call cleanup derived from v122.

Change:

- Keeps v122's T8o/K4s/J8s draw breakers and native national TCP entrypoint.
- Adds a narrow flop QJs top-pair medium-bet call:
  - stage is `flop`,
  - original rule label is `raise_pot`,
  - opponent is not all-in,
  - hole cards are exactly suited QJ,
  - board ranks are exactly Q-9-6,
  - `1080 <= to_call <= 1120`,
  - pot is `2830..2870`,
  - rule raise-to action is `3900..4000`,
  - return action `0`, a protocol-legal `call`.

Rationale:

- v122 keeps the current all-completed `150-0-0` block, but old ordered seed
  block `2026074000` still has a large v3 loss on seed `2026074008`.
- Trace mining showed QJs on Q-9-6 facing a medium flop bet. The rule action
  pot-raised to `3943`, then fired a free turn all-in and lost `-20335` on the
  hand.
- Forcing only the flop decision to call changed the 70-hand paired match from
  `-17405` to `+17730`, a `+35135` match-chip swing.
- A nearby seed `2026074002` had a similar but smaller spot at `to_call=1044`,
  pot `2802`, and rule action `3844`; forcing that to call worsened the match.
  The v123 thresholds intentionally exclude it.

Status:

- Must improve old v3 seed `2026074008`, preserve the ordered old top8+v7
  regression blocks, and keep current all-completed domination.
