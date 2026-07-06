# v117_national_v17_weak_ace_4bet_call_tcp

Native national TCP weak-wheel-ace large-reraise downshift derived from v116.

Change:

- Keeps v116's flop free-action value-check and turn free-all-in value-check
  gates.
- Adds a narrow preflop downshift:
  - stage is `preflop`,
  - original rule label is `raise_pot`,
  - hole cards are offsuit `A2` through `A5`,
  - `3000 <= to_call <= 4500`,
  - `7500 <= pot <= 9500`,
  - the rule action is at least `12000`,
  - return action `0`, which is a protocol-legal `call`.

Rationale:

- Fresh native TCP evaluation on latest strong rules exposed a repeated A3o
  overcommit leak on seeds `2026074100` through `2026074105` against v2. The
  bot 4bet/5bet to nearly all chips preflop, then called the final small
  all-in remainder and lost `-20000` in each seed.
- Force probes changing the large preflop reraise decision to `call` improved
  each paired match by about `+13.8k` chips while preserving protocol
  compliance.

Status:

- Must beat v116 on the latest top-rule seed block `2026074100`, then pass
  regression on the older v116 seed blocks `2026074000` and `2026073900`.
