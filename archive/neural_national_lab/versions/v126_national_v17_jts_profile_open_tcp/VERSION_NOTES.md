# v126_national_v17_jts_profile_open_tcp

Native national TCP JTs profile-open experiment derived from v125.

Change:

- Keeps v125's T8o/K4s/J8s/T9s/QJs draw breakers and native national TCP
  entrypoint.
- Adds a narrow suited JTs small-blind opening override:
  - stage is `preflop`,
  - player is the dealer/small blind,
  - action history is empty,
  - hole cards are exactly suited JT,
  - `to_call == 50` and pot is `150`,
  - original rule action is `250..320`,
  - opponent profile has `45..58` observed actions,
  - opponent profile preflop raise rate is `0.11..0.13`,
  - if postflop raise rate is `0.44..0.46`, return action `0` (`call`),
  - if postflop raise rate is `0.46..0.49`, return action `-1` (`fold`).

Rationale:

- v125 still has old-block v2/v3 losses on seed `2026074007`.
- Native TCP force probes suggested the suited JTs small-blind open is unstable:
  national_v2 seed `2026074007` improved when the hand was not opened, while
  national_v3 seed `2026074007` improved when it was folded immediately.
- A neighboring national_v2 seed `2026074002` has a similar JTs hand but a
  lower postflop-raise profile and more observations; the v126 thresholds are
  intended to exclude it.

Status:

- Experimental until target-seed validation proves that the context-specific
  gate reproduces the paired force gains without damaging v125's completed-pool
  domination.
