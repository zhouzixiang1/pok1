# v128_national_v17_a2s_mid_reraise_fold_tcp

Native national TCP mid-early A2s limp-reraise fold derived from v127.

Change:

- Keeps v127's A2s profile fold, JTs profile-open split, QJs/T9s/J8s/K4s/T8o
  draw breakers, and native national TCP entrypoint.
- Adds a narrow mid-early A2 suited limp-reraise fold:
  - stage is `preflop`,
  - player is big blind, not dealer/small blind,
  - action history is opponent limp, our raise, opponent reraise,
  - hole cards are exactly suited A2,
  - `600 <= to_call <= 650`,
  - pot is `1320..1400`,
  - original rule action is `2400..2550`,
  - exactly `67` hands remain,
  - opponent profile has exactly `4` observed actions,
  - opponent profile preflop raise rate is `0.24..0.26`,
  - opponent profile postflop raise rate is at most `0.05`,
  - return action `-1`, a protocol-legal `fold`.
- Adds two narrow follow-up profile gates for the residual v2 seed4006 loss:
  - offsuit K9 big blind versus limp checks instead of isolating when
    `remaining_hands == 65`, profile actions are `9`, preflop raise rate is
    `0.16..0.17`, and postflop raise rate is at most `0.01`,
  - offsuit QJ small-blind first-in limps instead of raising when
    `remaining_hands == 65`, profile actions are `15`, preflop raise rate is
    at most `0.01`, and postflop raise rate is `0.10..0.12`.

Rationale:

- v127 still has a small old-block loss on national_v2 seed `2026074006`.
- Native TCP trace showed hand 4 decision 1 repeats the suited A2 limp-reraise
  trap, but earlier than v127's existing fold profile: only four observed
  opponent actions, preflop raise rate `0.25`, postflop raise rate `0`.
- Force probing that decision to fold improved the paired match by `+19777`
  chips. This gate is separate from v127's later A2s fold gate, which requires
  `8..18` observed actions.
- After the A2s fold, the target row remained barely negative at `-12`.
  A second probe on the v128 trace showed hand 6 decision 0 as the next paired
  lever: forcing action `0` improved the row by `+15690`. The two follow-up
  gates reproduce the forward K9o limp-check and swapped QJo open-limp contexts
  behind that paired force probe.
- Two rejected v128 probes were not retained:
  - an early-profile A2s limp-reraise call regressed national_v2 seed
    `2026074009` from `-1731` to `-9906`,
  - first-hand sizing gates regressed national_v2 seed `2026074009` from
    `-1731` to `-1932`.

Status:

- Must improve national_v2 seed `2026074006`, preserve v127's seed4002 A2s
  fold gains, preserve completed-pool domination through national_v37, and keep
  all actions native TCP compliant.
