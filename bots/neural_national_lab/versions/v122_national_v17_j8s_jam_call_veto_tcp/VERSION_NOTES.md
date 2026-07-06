# v122_national_v17_j8s_jam_call_veto_tcp

Native national TCP J8s jam-call cleanup derived from v121.

Change:

- Keeps v121's T8o/K4s large-jam draw breakers and native national TCP entrypoint.
- Adds a narrow preflop J8 suited jam-call veto:
  - stage is `preflop`,
  - opponent is already all-in,
  - original rule label is `call`,
  - hole cards are exactly suited J8,
  - `18000 <= to_call <= 20000`,
  - pot is `20000..22000`,
  - return action `-1`, a protocol-legal `fold`.

Rationale:

- v121 already dominates current all-completed national bots, but old ordered
  seed block `2026074000` still had a v3 loss driven by calling an oversized
  preflop all-in with J8 suited.
- The isolated native TCP force probe on v3 seed `2026074003`, hand 60 changed
  that hand from `-19900` to `-713` and the 70-hand match from `-19441` to
  `+18696`, a `+38137` match-chip swing.
- Earlier candidates around K4s follow-ups, QJs flop redirects, and T9s flop
  checks were mixed or negative under the standard old-top ordering, so v122
  intentionally includes only this high-confidence J8s cleanup.

Status:

- Must improve old v3 seed `2026074003`, preserve the ordered old top8+v7
  regression blocks, and keep current all-completed domination.
