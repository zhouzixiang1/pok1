# v102_national_v17_v2v3_call_raise_t015_fold_t025_tcp

Hybrid follow-up after v100/v101.

Change:

- Keeps the same mixed h64 value head as v099:
  `native_context_action_current_plus_v2v3_hardneg_h64_seed3421.json`.
- Keeps v099's version-local pair-specific multi-action proposal support.
- Preserves the v098/v099 `raise_pot -> call` proposal at threshold/margin
  `0.50`.
- Keeps v100's conservative preflop `fold -> raise_pot` threshold/margin
  `0.25`.
- Re-enables only the v099 preflop `call -> raise_pot` branch at
  threshold/margin `0.15`.
- Neural fold and all-in remain disabled.

Rationale:

- v099 improved the seed `2026073300` v2/v3 bucket from v098's combined
  `-337048` to `-131745`, with 0 illegal actions, 0 timeouts, and 0 adapter
  actions.
- The same full-pool test still scored `-293357` and made every non-v2/v3
  opponent negative. The paired diff versus v098 was positive overall
  (`+40125`) only because the v2/v3 gain outweighed broad small losses.
- v100 removed `call -> raise_pot` and reduced broad pollution, scoring
  `-195583` full-pool absolute and `+137899` versus v098, but it gave back most
  of v099's v2 improvement.
- v101 lowered only `fold -> raise_pot` to `0.15` and regressed to `-322408`,
  so the fold threshold should stay at v100's `0.25`.
- This version isolates the remaining hypothesis: v099's v2 gain may come from
  `call -> raise_pot`, not from the lower fold threshold.

Status:

- Tuned seed block `2026073300`, current top8 plus v7, 45 paired matches /
  6300 hands: `-165428` chips absolute. Diff versus v100: `+30155`; diff
  versus v098: `+168054`.
- Holdout seed block `2026073200` on the same pool: `+15685` chips absolute,
  5 wins, 27 losses, 13 draws. V098 on that holdout block was `-195235`, so
  the paired diff is `+210920`.
- Per-opponent holdout highlights: `national_v2 +35883`, `national_v5 +6775`,
  `national_v3 -13875`; most other non-v2 opponents were small negatives.
- Compliance was clean on both reports: 0 illegal actions, 0 timeouts,
  0 adapter actions.
- Verdict: current best neural artifact and clear protocol-native performance
  result, but not comprehensive rule-bot domination. Keep it as the next source
  for v3-specific hard-negative data and held-out training.
