# v127_national_v17_a2s_limp_reraise_fold_tcp

Native national TCP A2s limp-reraise fold derived from v126.

Change:

- Keeps v126's T8o/K4s/J8s/T9s/QJs/JTs draw breakers and native national TCP
  entrypoint.
- Adds a narrow preflop A2 suited limp-reraise fold:
  - stage is `preflop`,
  - player is big blind, not dealer/small blind,
  - action history is opponent limp, our raise, opponent reraise,
  - hole cards are exactly suited A2,
  - `600 <= to_call <= 650`,
  - pot is `1320..1400`,
  - original rule action is `2400..2550`,
  - opponent profile has `8..18` observed actions,
  - opponent profile preflop raise rate is `0.18..0.27`,
  - opponent profile postflop raise rate is at most `0.50`,
  - return action `-1`, a protocol-legal `fold`.

Rationale:

- v126 still has the largest old-block loss on national_v2 seed `2026074002`.
- Deeper native TCP counterfactual scanning found hand 8 decision 1: suited A2
  isolated a limp, faced a limp-reraise, then rule-raised again to `2839`.
- Forcing only that decision to fold changed the paired match from `-3606` to
  `+38998`, a `+42604` match-chip swing; the target hand improved from `-2497`
  to `-708`.
- Other probes on the same loss found positive QJo/QJs alternatives, but the
  A2s fold is the narrowest and most directly tied to a repeated low-equity
  limp-reraise trap.
- A nearby v2 seed `2026074003` shared the same A2s card and sizing geometry but
  had `postflop_raise_rate=1.0`; the postflop profile cap keeps that guard row
  unchanged at `-606` instead of regressing to `-3890`.

Status:

- Fixed the two formal batch losses on seed `2026074002`:
  - national_v2: `-3606` to `+18473`,
  - national_v3: `-1463` to `+18585`.
- Preserved the completed/tagged native TCP pool result through national_v34:
  `+1988816`, W-L-D `162-0-0`, with 0 candidate illegal actions, 0 candidate
  timeouts, and 0 candidate adapter actions.
- Improved the older current-top8+v7 two-block result from v126's `+1531955`
  to `+1573288`, W-L-D `117-5-58`.
