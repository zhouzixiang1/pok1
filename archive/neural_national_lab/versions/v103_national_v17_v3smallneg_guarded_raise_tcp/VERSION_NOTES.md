# v103_national_v17_v3smallneg_guarded_raise_tcp

Native national TCP neural overlay derived from v102.

Change:

- Adds a new h64 multi-action value head trained on the previous current-pool
  plus v2/v3 hard-negative set and a v102-targeted v3/v9/v8 shard sample:
  `native_context_action_v102_v3smallneg_plus_current_h64_seed3521.json`.
- Keeps the native TCP bot implementation and version-local proposal support
  from v102; no national adapter is used.
- Enables only high-threshold neural fold proposals against `raise_pot`
  rule actions, using threshold/margin `0.60`.
- Keeps the v102 `call -> raise_pot` correction at threshold/margin `0.50`
  and adds a guarded `call -> raise_2pot` correction at `0.60`.
- Raises aggressive `raise_pot -> call/fold` proposal thresholds from
  v102's `0.15/0.25` to `0.40/0.40`.
- Neural all-in remains disabled.

Rationale:

- v102 is the best prior neural artifact on the holdout pool, but it still had
  a negative v3 slice and several small non-v2 losses.
- The targeted v102 counterfactual probe completed 5 native TCP shards before
  interruption: 49 rows total, 42 ok rows, 110 target samples, no adapter path.
- That increment showed positive `fold` alternatives and negative `raise_pot`
  tendency on the v3/v9/v8 small-negative bucket. It argues for suppressing bad
  raises while keeping only higher-confidence pressure raises.
- h64 beat h96 on the rebuilt 349-row training set:
  validation MAE `0.2080` vs `0.2234`, validation best-label accuracy `0.500`
  vs `0.457`; both trained on CUDA.
- Gate scans for the selected h64 model supported conservative thresholds:
  `fold > raise_pot` at `0.60` selected 21 samples with mean `+3802.4`;
  `call > raise_pot` at `0.50` selected 10 samples with mean `+3600.0`;
  `raise_pot > call` at `0.40` selected 11 samples with mean `+1343.0`;
  `raise_pot > fold` at `0.40` selected 10 samples with mean `+1480.0`.

Status:

- Focused seed block `2026073600`, v3/v8/v9/v14, 12 paired matches /
  1680 hands: `-2210` chips absolute versus v102's `-27357`, a paired
  improvement of `+25147`.
- The improvement was not usable as-is: v8/v9/v14 each improved to `+29381`,
  but v3 regressed from v102's `-21507` to `-90353`.
- Compliance was clean: 0 illegal actions, 0 timeouts, 0 adapter actions.
- Verdict: useful ablation showing defensive `fold/call` proposals help some
  non-v3 opponents but poison v3. Not promoted.
