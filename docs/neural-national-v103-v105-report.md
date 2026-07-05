# Neural National v103-v105 Report

Date: 2026-07-06

Scope:

- Native national TCP only. No national adapter path was used in data collection
  or evaluation.
- Versions were created as separate bot directories:
  `bots/neural_national_lab/versions/v103_national_v17_v3smallneg_guarded_raise_tcp`,
  `v104_national_v17_v3_raise_guard_tcp`, and
  `v105_national_v17_v3_raise_t060_tcp`.
- Opponents were rule bots from `.evolution_pok/bots/national_v*`, not neural
  self-play only.

Data and model:

- Added targeted v102 counterfactual data against
  `.evolution_pok/bots/national_v3`, `national_v9`, `national_v8`, and
  `national_v14`.
- The long shard run was interrupted after 5 successful shards because several
  v3/v9 shards hit the 420 second timeout. Merged output:
  `bots/neural_national_lab/data/native_tcp_cf_shards_v102_v3smallneg_s6_h14_seed2026073500.json`.
- Merged counterfactual summary: 49 rows, 42 ok rows, 110 target samples.
- Rebuilt action-value training set:
  `native_tcp_value_v102_v3smallneg_plus_current_d349_context_action_clip4000_noallin.jsonl`
  with 349 rows, 1067 target samples, input dim 78.
- Trained h64 and h96 CUDA models. h64 was selected:
  validation MAE `0.2080`, validation best-label accuracy `0.500`.

Version summary:

- v103 enabled guarded defensive `fold/call` proposals plus stricter
  `raise_pot` proposals. It improved v8/v9/v14 but badly regressed v3.
- v104 removed defensive `fold/call` proposals and kept only `raise_pot`
  proposals over rule `call/fold` at threshold/margin `0.40`.
- v105 kept v104's narrow proposal surface and raised threshold/margin to
  `0.60`.

Focused evaluation, seed block `2026073600`, v3/v8/v9/v14:

| Bot | Matches | Hands | Total chips | v3 chips | Compliance |
|---|---:|---:|---:|---:|---|
| v102 | 12 | 1680 | -27357 | -21507 | clean |
| v103 | 12 | 1680 | -2210 | -90353 | clean |
| v104 | 12 | 1680 | +17157 | +15423 | clean |
| v105 | 12 | 1680 | +20304 | +18570 | clean |

Wider evaluation, seed block `2026073700`, current top8 plus v7
(`national_v2`, `v3`, `v5`, `v7`, `v8`, `v9`, `v14`, `v15`, `v16`):

| Bot | Matches | Hands | Total chips | v2 chips | v3 chips | Compliance |
|---|---:|---:|---:|---:|---:|---|
| v102 | 27 | 3780 | -186443 | -86812 | -87178 | clean |
| v104 | 27 | 3780 | -200623 | -86486 | -86529 | clean |
| v105 | 27 | 3780 | -200929 | -86597 | -86724 | clean |

Conclusion:

- v104/v105 are real focused improvements on the v3/v8/v9/v14 seed block, but
  neither is a general upgrade over v102 on the wider strong-rule pool.
- The main failure mode is broad pollution: the stricter v105 threshold still
  worsens seven non-v2/v3 opponents by small repeated amounts while only
  slightly reducing the large v2/v3 losses on the wider seed block.
- Goal status remains incomplete. These versions should be treated as evidence
  and ablations, not promoted as the active strongest neural bot.

Next step:

- Collect a larger balanced hard-negative set that explicitly includes:
  v2/v3 catastrophic-loss seeds from `2026073700`, non-v2/v3 pollution cases,
  and the focused positive v3 cases from `2026073600`.
- Add decision-level tracing for multi-action proposals so future evaluations
  can count actual neural overrides by label and opponent.
- Train with opponent-bucket balancing or per-source weighting before another
  proposal-threshold sweep.
