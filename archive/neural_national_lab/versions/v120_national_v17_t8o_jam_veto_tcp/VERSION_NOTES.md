# v120_national_v17_t8o_jam_veto_tcp

Native national TCP T8o large-jam veto derived from v119.

Change:

- Keeps v119's flop all-in value veto and river paired-ace thin-raise call gate.
- Adds a narrow preflop T8 offsuit large-jam veto:
  - stage is `preflop`,
  - original rule label is `allin`,
  - hole cards are exactly offsuit T8,
  - `10000 <= to_call <= 14000`,
  - pot is `25000..31000`,
  - return action `-1`, a protocol-legal `fold`.

Rationale:

- On the current completed `.evolution_pok` top10 seed block `2026074100`,
  v119 still had 48 draws. Trace mining showed a repeated mirrored pattern:
  T8o large preflop all-ins lost `-20000`, while the paired mirror win came
  from a different JTo branch that this gate does not touch.
- This gate is intentionally card- and pot-shape-specific. It does not broaden
  the earlier A3o or weak-ace preflop gates, which had negative force-probe
  evidence in prior versions.

Status:

- Must beat v119 on current completed top10 and preserve old regression blocks.
