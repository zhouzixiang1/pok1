# v101_national_v17_v2v3_fold_raise_t015_tcp

Lower-threshold fold-raise follow-up to v100.

Change:

- Keeps the same mixed h64 value head as v099:
  `native_context_action_current_plus_v2v3_hardneg_h64_seed3421.json`.
- Keeps v099's version-local pair-specific multi-action proposal support.
- Preserves the v098/v099 `raise_pot -> call` proposal at threshold/margin
  `0.50`.
- Keeps the aggressive branch limited to preflop `fold -> raise_pot`, but
  lowers threshold/margin from v100's `0.25` to `0.15`.
- Still disables `call -> raise_pot`, because v099's first full-pool online
  test improved v2/v3 but created steady small losses against the rest of the
  pool.
- Neural fold and all-in remain disabled.

Rationale:

- v100 removed v099's broad `call -> raise_pot` pollution and improved the
  same seed `2026073300` full-pool score from v098 by `+137899` chips, with
  0 illegal actions, 0 timeouts, and 0 adapter actions.
- v100 still left the v2/v3 bucket around `-80k` chips per opponent, so this
  version tests whether the all-positive offline `fold -> raise_pot` samples
  at threshold `0.15` can recover more of the hard-negative bucket without
  reopening the `call -> raise_pot` branch.
- Offline mixed h64 scan for `raise_pot` over `fold` had 5 all-positive
  samples at threshold `0.15`, compared with 3 at threshold `0.25`.

Status:

- Paired native TCP evaluation on seed block `2026073300`, current top8 plus
  v7, 45 paired matches / 6300 hands: `-322408` chips absolute, 0 wins,
  45 losses, 0 draws.
- Diff versus v100: `-126825`. The v2/v3 bucket barely improved while
  non-v2/v3 losses returned to the v099 pollution pattern.
- Compliance was clean: 45/45 compliant, 0 illegal actions, 0 timeouts,
  0 adapter actions.
- Verdict: rejected. Lowering `fold -> raise_pot` to `0.15` is the bad branch.
