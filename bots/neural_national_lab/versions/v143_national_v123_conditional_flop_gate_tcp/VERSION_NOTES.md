# v143 national_v123 river thin-bet fold veto TCP

Archive status: unfinished diagnostic WIP. This candidate was never promoted,
completion-tagged, or certified under the current signed 5+3x70 official EXE
policy. Its results are historical debugging evidence, not strength evidence.

Parent: `v142_national_v123_oldpool_probe_tcp`.

## Motivation

A fresh, larger v142 baseline on 2026-07-10 showed:

- current pool national_v119-v123, 50 paired matches (7000 hands), seed4400:
  `+231 / 7000`, W28-L22-D0. Marginal; national_v119 and national_v121 are net
  losers (nemesis opponents) and their losses are small-to-medium showdown pots
  (~-200 to -440 chips per lost hand, not all-in swings). v142 pays off thin
  river calls with weak made hands against value-heavy opponents.
- old top8+v7, 36 paired matches (5040 hands), seed4500:
  `+204507 / 5040`, W28-L8-D0. Strong; v142's flop-gate-off fix must be kept.

An earlier v143 attempt re-enabled the `flop_late_weak_highcard_free_raise_check`
veto with a raise-to-pot ratio condition. It regressed the old pool
(`+106280 / 5040`, down from v142's `+204507`), because any re-enable of that
veto re-introduced bad over-folds at the v2/v3 late-flop spots. That attempt was
discarded.

## Change

v143 keeps the v142 flop gate **off** (byte-identical behavior there) and adds a
new, profile-aware defensive guard instead:

- New guard: `_river_thin_bet_fold_veto` (config prefix
  `river_thin_bet_fold_veto_`).
- It only fires on the river, facing a medium bet (to_call 150-450, pot
  400-1200), when the rule base chose `call`, the hand is genuinely thin (bare
  low/mid pair up to rank 8, or sub-ace high card), and the opponent profile is
  value-heavy (postflop_raise_rate >= 0.18 AND check_rate <= 0.45). It converts
  the call to a fold.
- It never fires against all-ins, free actions, two-pair-or-better, ace-high,
  or against passive/checking old-pool opponents (the profile gate excludes
  them).

This is a profile-aware fold improvement targeting the v119/v121 nemesis pattern
while leaving the old pool and the proven v142 flop-gate behavior untouched. No
new positive action is introduced.
