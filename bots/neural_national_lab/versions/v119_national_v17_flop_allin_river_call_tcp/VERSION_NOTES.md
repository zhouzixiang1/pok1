# v119_national_v17_flop_allin_river_call_tcp

Native national TCP small-all-in and river thin-raise cleanup derived from v118.

Change:

- Keeps v118's low-trips flop `raise_pot -> call` gate.
- Adds a small flop all-in value veto:
  - stage is `flop`,
  - original rule label is `allin`,
  - facing a non-all-in bet with `1200 <= to_call <= 2500`,
  - pot is `2500..5000`,
  - multi-action value scores all-in at most `-0.25`,
  - fold value is at least `1.0` above all-in,
  - return action `-1`, which is a protocol-legal `fold`.
- Adds a river paired-ace small-bet call gate:
  - stage is `river`,
  - original rule label is `raise_pot`,
  - facing a small bet with `250 <= to_call <= 450`,
  - pot is `800..1200`,
  - rule raise-to action is `1150..1450`,
  - board contains exactly two aces,
  - our best hand is two pair without a hole ace,
  - the hole-made pair rank is at most jack,
  - return action `0`, which is a protocol-legal `call`.

Rationale:

- Current completed `.evolution_pok` top10 on seed block `2026074100` left v118
  with one negative paired match against `national_v2`, seed `2026074105`.
- The largest remaining leak was a flop all-in with 8-5 suited on a T-7-2 flop.
  v118's own multi-action value head scored all-in at `-0.343` and fold at
  `0.863`; forcing only that action to fold improved the paired match by
  `+19,231` chips.
- A repeated river thin value raise on paired-ace boards appeared in five v2
  seeds. Forcing those raises to call improved all five paired matches, with
  aggregate delta `+39,256` chips. v119 uses call rather than fold because it
  preserves showdown value while removing the thin pot-raise.

Status:

- Must beat v118 on the latest completed top10 seed block `2026074100`, then
  pass older current-top8+v7 regression blocks before promotion.
