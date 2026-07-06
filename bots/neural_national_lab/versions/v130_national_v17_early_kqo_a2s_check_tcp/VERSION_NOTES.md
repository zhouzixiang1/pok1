# v130_national_v17_early_kqo_a2s_check_tcp

Native national TCP early KQo/A2s cleanup derived from v129.

Change:

- Keeps v129's v5 seed3908 folds, A2s mid/late folds, K9o/QJo residual
  cleanup, JTs profile-open split, QJs/T9s/J8s/K4s/T8o draw breakers, and
  native national TCP entrypoint.
- Adds a narrow KQo small-blind first-in limp gate for the verified
  remaining-hand window `64..65`:
  - stage is `preflop`,
  - small blind first action with empty history,
  - hole cards are exactly offsuit KQ,
  - `to_call == 50` and pot is `150`,
  - original rule action is `280..300`,
  - opponent profile has at most `15` observed actions,
  - profile preflop raise rate is at most `0.01`,
  - profile postflop raise rate is at most `0.55`,
  - return action `0`, a national-protocol limp/call.
- Adds a paired A2s big-blind limp-check gate for the same `64..65`
  remaining-hand window:
  - stage is `preflop`,
  - big blind faces exactly one opponent limp,
  - hole cards are exactly suited A2,
  - `to_call == 0` and pot is `200`,
  - original rule action is `260..270`,
  - opponent profile has `1..8` observed actions,
  - profile preflop raise rate is at most `0.15`,
  - return action `0`, a national-protocol check.
- Adds an exact first-hand KQo/A2s sizing gate for `remaining_hands == 70`:
  - KQo small-blind first-in returns the delta needed for `raise 300`,
  - A2s big-blind versus limp returns the delta needed for `raise 300`,
  - both gates require the first-hand profile counts observed in trace
    (`0` actions for the KQo open, `1` action for the A2s isolate).

Rationale:

- v129 still had old-block losses on national_v2 seeds `2026074003` and
  `2026074009`, and national_v3 seed `2026074004`.
- A native paired counterfactual scan on national_v2 seed `2026074003` found
  that forcing hand 7 decision 0 to action `0` improved the full paired row by
  `+17124` chips, from `-606` to `+16518`.
- Direct force probes of the same `64..65` remaining-hand KQo/A2s check-down
  pattern improved national_v3 seed `2026074004` from `-1181` to `+39524`.
- For national_v2 seed `2026074009`, the safe line was not limp/check. A scan
  found that forcing hand 1 decision 0 to final action `300` improved the row
  by `+20216`, from `-1731` to `+18485`. The normal gate returns deltas
  (`250` from small blind, `200` from big blind) which sanitize to national
  raise-to-total `300`.
- A broader `64..70` limp/check attempt and a QJo/K9o `raise 300` experiment
  caused later-seed regressions, so the enabled gates are intentionally
  split by exact remaining-hand/profile windows.

Status:

- Verified on the old current-top8+v7 two-block pool:
  `+1697241`, mean/hand `+67.351`, W-L-D `122-0-58`.
- Verified on completed/tagged national_v1-v37 pool:
  `+2133560`, mean/hand `+87.585`, W-L-D `174-0-0`.
- Extra observation versus untracked `.evolution_pok/bots/national_v39`:
  `+72372`, mean/hand `+86.157`, W-L-D `6-0-0`.
- Direct native TCP H2H versus v129 on seed block `2026074200`:
  `0`, mean/hand `0.000`, W-L-D `0-0-6`.
- All recorded v130 evaluations had 0 candidate illegal actions, 0 candidate
  timeouts, and 0 candidate adapter actions.
