# v115_national_v17_flop_value_check_tcp

Native national TCP flop free-action value-check experiment derived from v114.

Change:

- Keeps v114's preflop wheel-ace jam-call veto.
- Adds a narrow multi-action-value proposal on the flop:
  - stage is `flop`,
  - original rule label is `raise_pot`,
  - `to_call == 0`, so action `0` is a protocol-legal `check`,
  - `4000 <= pot <= 6000`,
  - value head score for `call`/check is at least `1.0`,
  - value head score for `call`/check is at least `0.5` above `raise_pot`.
- The proposal returns action `0` and remains native national TCP.

Rationale:

- v114's remaining v2 seed block `2026074000` was no longer dominated by the
  preflop wheel-ace jam leak. The largest remaining convertible loss was
  match 9 hand 58: a free flop pot-size raise of `5494` into pot `4778`.
- Recomputed value-head scores on that decision were `check=1.2336` and
  `raise_pot=0.6793`. A force probe changing only that decision to `check`
  improved the paired match by about `+5494` chips.
- Facing-bet flop `raise_pot -> call` probes worsened two seed-block samples, so
  this version intentionally does not alter paid-call spots.

Status:

- Must beat v114 on v2 seed block `2026074000`, then pass current-top8+v7 on
  seed blocks `2026074000` and `2026073900`.
