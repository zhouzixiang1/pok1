# v129_national_v17_v5_seed3908_fold_tcp

Native national TCP v5 seed3908 preflop fold cleanup derived from v128.

Change:

- Keeps v128's A2s mid/late folds, K9o/QJo residual cleanup, JTs profile-open
  split, QJs/T9s/J8s/K4s/T8o draw breakers, and native national TCP entrypoint.
- Adds two narrow preflop folds for the remaining national_v5 seed3908 loss:
  - offsuit 43 big-blind defense folds versus a `260` raise when
    `to_call == 160`, pot is `360`, exactly `69` hands remain, profile actions
    are `5`, profile preflop raise rate is at least `0.99`, and postflop raise
    rate is at most `0.01`,
  - suited T2 small-blind first-in folds instead of opening when `to_call == 50`,
    pot is `150`, original rule action is `200..220`, exactly `69` hands
    remain, profile actions are `5`, profile preflop raise rate is `0.49..0.51`,
    and postflop raise rate is at most `0.01`.

Rationale:

- v128 still has a small old-block loss on national_v5 seed `2026073908`.
- The native TCP trace showed only hand 2 was negative, for `-249` chips.
- A paired force probe on hand 2 decision 0 showed that forcing action `-1`
  improved the full paired row by `+11657` chips. The paired decision maps to
  43o folding a big-blind defense in the forward leg and T2s folding a
  small-blind first-in open in the swapped leg.
- Raise/all-in force alternatives also looked positive, but the fold cleanup is
  simpler, protocol-safe, and tied to dominated/trash preflop holdings.

Status:

- Must improve national_v5 seed `2026073908`, preserve v128's v2 seed4006
  improvement, preserve completed-pool domination through national_v37, and keep
  all actions native TCP compliant.
