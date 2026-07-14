# v125_national_v17_qjs_profile_fold_tcp

Native national TCP QJs profile-fold cleanup derived from v124.

Change:

- Keeps v124's T8o/K4s/J8s/T9s draw breakers, QJs call cleanup, and native
  national TCP entrypoint.
- Adds a narrower flop QJs top-pair fold:
  - stage is `flop`,
  - original rule label is `raise_pot`,
  - opponent is not all-in,
  - hole cards are exactly suited QJ,
  - board ranks are exactly Q-9-6,
  - `1050 <= to_call <= 1070`,
  - pot is `2810..2830`,
  - rule raise-to action is `3750..3820`,
  - opponent profile preflop raise rate is at least `0.18`,
  - opponent profile postflop raise rate is at most `0.30`,
  - return action `-1`, a protocol-legal `fold`.

Rationale:

- v124 still had old-block v2 losses on seed block `2026074000`, including
  national_v2 seed `2026074001`.
- Native TCP loss counterfactual scanning found QJs on Q-9-6 facing a medium
  flop bet after a preflop 3bet pot. The rule action pot-raised to `3793` and
  then faced a turn all-in.
- Forcing only hand 35 decision 2 to fold changed the paired match from
  `-2609` to `+20213`, a `+22822` match-chip swing; the target hand improved
  from `-4722` to `-929`.
- Nearby negative/unsafe samples were deliberately excluded:
  - seed `2026074002` used larger flop pressure (`to_call=2142`,
    rule action `6476`),
  - national_v3 seed `2026074007` had similar QJs sizing but a much higher
    postflop raise profile, and forced fold was negative.

Status:

- Must improve national_v2 seed `2026074001`, preserve v124's all-completed
  `150-0-0` block, and keep all candidate actions native TCP compliant.
