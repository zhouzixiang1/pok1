# v099_national_v17_v2v3_raise_pot_pairgate_tcp

Hard-negative v2/v3 native TCP action-value fork from v098.

Change:

- Keeps native national TCP and the 78-feature `native_context_action` value
  contract.
- Replaces the v098 head with
  `native_context_action_current_plus_v2v3_hardneg_h64_seed3421.json`.
- Adds version-local pair-specific proposal thresholds so different
  label/rule-label pairs can use different value margins.
- Preserves the strict v098 `raise_pot -> call` proposal at threshold/margin
  `0.50`.
- Adds a narrow preflop `call/fold -> raise_pot` proposal at threshold/margin
  `0.15`, using the same runtime raise gate and final sanitizer.
- Neural fold and all-in remain disabled.

Training data:

- New hard-negative input:
  `native_tcp_cf_shards_v085_v2v3_hardneg_s10_h12_seed2026073400.json`.
- The hard-negative collection used only protocol-native `.evolution_pok`
  opponents `national_v2` and `national_v3`. The long shard run was interrupted
  after completed shard files were available, then merged with `--merge-only`.
  It produced 120 rows, 119 ok rows, and 286 raw target samples.
- Mixed training set:
  `native_tcp_value_v085_current_plus_v2v3_hardneg_d307_context_action_clip4000_noallin.jsonl`,
  built from the v098 current-pool data plus the v2/v3 hard-negative merge. It
  has 307 rows, 933 target samples, 78 input features, clipped `delta_vs_rule`
  targets at +/-4000 chips, and drops the all-in target.
- CUDA h64 training metrics for seed 3421: validation MAE `0.1333`,
  validation RMSE `0.2229`, validation best-label accuracy `0.5323`.

Offline gate scan:

- Mixed h64 `call` over `raise_pot`:
  - threshold `0.50`: 9 samples, mean target `+3609.3`, 9 positive,
    0 negative.
  - threshold `0.60`: 7 samples, mean target `+4000.0`, 7 positive,
    0 negative.
- Mixed h64 `raise_pot` over `call`:
  - threshold `0.10`: 14 samples, mean target `+1161.1`, 12 positive,
    0 negative, 2 zero.
  - threshold `0.15`: 5 samples, mean target `+2244.4`, 5 positive,
    0 negative.
- Mixed h64 `raise_pot` over `fold`:
  - threshold `0.10`: 11 samples, mean target `+1086.8`, 9 positive,
    0 negative, 2 zero.
  - threshold `0.15`: 5 samples, mean target `+1836.8`, 5 positive,
    0 negative.

Status:

- Paired native TCP evaluation on seed block `2026073300`, current top8 plus
  v7, 45 paired matches / 6300 hands: `-293357` chips absolute, 0 wins,
  45 losses, 0 draws.
- Diff versus v098 on the same seed block: `+40125` chips. It sharply improved
  the v2/v3 bucket, but every non-v2/v3 opponent became negative.
- Compliance was clean: 45/45 compliant, 0 illegal actions, 0 timeouts,
  0 adapter actions.
- Verdict: useful ablation, not a candidate. The broad low-threshold
  fold-raise branch pollutes the pool.
