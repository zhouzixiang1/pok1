# v098_national_v17_currentpool_action_h64_call_t050_tcp

Current-pool action-context native TCP value probe forked from v095.

Change:

- Keeps native national TCP and the call-only neural intervention path.
- Replaces the v095 value head with
  `native_context_action_current_top8_plus_v7_h64_seed3112.json`.
- Uses the same 78-feature `native_context_action` contract: state features,
  native opponent profile, and rule-action context.
- Keeps neural fold, raises, and all-ins disabled.
- Keeps the runtime scope to preflop `raise_pot -> call`, but raises the
  value threshold/margin from v095's `0.40` to `0.50`.

Training data:

- Built from native TCP counterfactual shards collected with
  `v085_national_v17_profile_trace_tcp` against the current `.evolution_pok`
  conservative-Glicko leaders and hard negative:
  `national_v3`, `national_v2`, `national_v8`, `national_v14`,
  `national_v5`, `national_v9`, `national_v15`, `national_v16`, and
  `national_v7`.
- Inputs:
  - `native_tcp_cf_shards_v085_current_top8_plus_v7_smoke_s1_h12_seed2026073000.json`
  - `native_tcp_cf_shards_v085_current_top8_plus_v7_s3_h12_seed2026073100.json`
- The merged training set
  `native_tcp_value_v085_current_top8_plus_v7_d188_context_action_clip4000_noallin.jsonl`
  has 188 ok rows, 575 target samples, 78 input features, clipped
  `delta_vs_rule` targets at +/-4000 chips, and drops the all-in target.
- CUDA h64 training metrics for seed 3112: validation MAE `0.1122`,
  validation RMSE `0.1944`, validation best-label accuracy `0.6842`.

Offline gate scan:

- On the merged current-pool training set, the h64 head's `call` over
  `raise_pot` runtime-style gate selected:
  - threshold `0.40`: 11 samples, mean target `+2959.3`, 10 positive,
    1 negative.
  - threshold `0.50`: 9 samples, mean target `+3609.3`, 9 positive,
    0 negative.
  - threshold `0.60`: 9 samples, mean target `+3609.3`, 9 positive,
    0 negative.
- v098 chooses `0.50` as the first online gate because it removes the one
  offline negative while keeping the same sample count as `0.60`.

Status:

- Paired native TCP evaluation used protocol-native opponents only, no adapter:
  current `.evolution_pok` conservative-Glicko leaders plus hard negative
  `national_v3`, `national_v2`, `national_v8`, `national_v14`, `national_v5`,
  `national_v9`, `national_v15`, `national_v16`, and `national_v7`.
- Seed block `2026073200`, 45 paired matches / 6300 hands:
  v098 `-195235`, v095 `-198052`, v082 `-202194`. Diffs were v098-v095
  `+2817` and v098-v082 `+6959`. All reports had 0 candidate illegal actions,
  0 timeouts, and 0 adapter actions.
- Seed block `2026073300`, 45 paired matches / 6300 hands:
  v098 `-333482`, v095 `-543380`; diff v098-v095 `+209898` with 17 positive,
  0 negative, and 28 zero rows. v098 still lost heavily to `national_v2` and
  `national_v3` (`-168524` each).
- Combined across the two v098-v095 seed blocks, v098 is a positive
  call-gate update versus v095, but the absolute result is still not
  promotion-grade. The main remaining weakness is the current `national_v2` /
  `national_v3` bucket, not protocol compliance.
