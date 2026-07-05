# v100_national_v17_v2v3_fold_raise_t025_tcp

Tighter follow-up to v099.

Change:

- Keeps the same mixed h64 value head as v099:
  `native_context_action_current_plus_v2v3_hardneg_h64_seed3421.json`.
- Keeps v099's version-local pair-specific multi-action proposal support.
- Preserves the v098/v099 `raise_pot -> call` proposal at threshold/margin
  `0.50`.
- Narrows the new aggressive branch to preflop `fold -> raise_pot` only, with
  threshold/margin `0.25`.
- Disables `call -> raise_pot`, because v099's first full-pool online test
  improved v2/v3 but created steady small losses against the rest of the pool.
- Neural fold and all-in remain disabled.

Rationale:

- v099 improved the seed `2026073300` v2/v3 bucket from v098's combined
  `-337048` to `-131745`, with 0 illegal actions, 0 timeouts, and 0 adapter
  actions.
- The same full-pool test still scored `-293357` and made every non-v2/v3
  opponent negative. The paired diff versus v098 was positive overall
  (`+40125`) only because the v2/v3 gain outweighed broad small losses.
- Offline mixed h64 scan for `raise_pot` over `fold` had 3 all-positive
  samples at threshold `0.25`; the selected high-threshold fold samples were
  concentrated in the v3 hard-negative data.

Status:

- Paired native TCP evaluation on seed block `2026073300`, current top8 plus
  v7, 45 paired matches / 6300 hands: `-195583` chips absolute, 0 wins,
  45 losses, 0 draws.
- Diff versus v098: `+137899`; diff versus v099: `+97774`. The tighter
  fold-raise branch removed most pool-wide pollution, but v2/v3 still stayed
  deeply negative.
- Compliance was clean: 45/45 compliant, 0 illegal actions, 0 timeouts,
  0 adapter actions.
- Verdict: better boundary than v099, but not the best follow-up once v102
  isolated the call-raise component.
