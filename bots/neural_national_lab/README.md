# Neural National Lab

Neural-enhanced national bot experiments.

- `versions/`: complete runnable bot snapshots. Add a new directory for each
  experiment version.
- `tools/`: teacher-data collection, tiny-MLP training, and mirror evaluation.
- `data/`: small generated datasets, metrics, and evaluation reports.
- `external/`: ignored shallow clones for research scans.

Runtime bots stay stdlib-only. Training may use PyTorch/NumPy. The neural model
acts after the rule strategy and before `sanitize_action`; native TCP output is
still produced by the national entrypoint.

## Scale-Up Gate

Do not move straight from whole-match outcome labels to large-scale training.
The current reliable next step is hand-scoped counterfactual rollout:

```bash
python bots/neural_national_lab/tools/counterfactual_rollout_probe.py \
  --version bots/neural_national_lab/versions/v022_v254_sharded_h96_raise_only254 \
  --opponent bots/claude_v279 \
  --games 3 \
  --max-probes 24 \
  --max-scan-decisions 400 \
  --kind to_raise \
  --branch-scope hand \
  --output bots/neural_national_lab/data/counterfactual_v022_vs_v279_g3_p24.json
```

As of the first probe run, v022 flop `to_raise` interventions have positive
small-sample hand deltas, but the sample is still too small for a large-scale
training decision. Move toward large scale only after all of these hold:

- at least 100 hand-scoped counterfactual probes for the target intervention;
- positive mean primary delta with a 95 percent confidence interval above zero;
- no obvious negative bucket by street, action type, or price band;
- candidate collection can be sharded/replayed without relying on slow full
  match rescans.

`counterfactual_rollout_probe.py` now uses a persistent process for the scanned
match by default and applies a cheap neural/street/confidence prefilter before
running the expensive rule-strategy analysis. Use `--no-scan-persistent` only
when debugging subprocess-state issues.

Run street-specific probes before training a gate. For example, v022 permits
both preflop and flop raise interventions, so flop-positive evidence must not
be blended with preflop data until the preflop bucket is independently checked.

For reproducible scale-up sampling, use deterministic seeds and shard outputs:

```bash
python bots/neural_national_lab/tools/counterfactual_shard_runner.py \
  --version bots/neural_national_lab/versions/v022_v254_sharded_h96_raise_only254 \
  --opponent bots/claude_v279 \
  --shards 8 \
  --workers 4 \
  --games-per-shard 3 \
  --max-probes-per-shard 8 \
  --stage flop \
  --kind to_raise \
  --seed-base 20260704 \
  --bot-seed-base 202607040000 \
  --output bots/neural_national_lab/data/counterfactual_v022_flop_seeded_shards.json
```

The shard runner writes per-shard JSON files next to the merged output, so a
negative bucket or suspicious outlier can be replayed with the same seed range.
Existing shard files are reused by default; pass `--rerun-existing` when the
probe code or filters changed and the shards should be regenerated. Increase
`--workers` to run independent shard subprocesses in parallel. Pass
`--bot-seed-base` when either bot uses process-local randomness; the probe
runner seeds the scanned match and gives each baseline/candidate forced branch
the same branch-local bot RNG seeds, so action-value labels are replayable.
The first seeded smoke run (`seed_base=2026070401`, 2 shards, 4 total probes)
found flop `to_raise` mean primary delta `-64.25` with a very wide confidence
interval. Treat that as a warning against shipping or training from optimistic
unseeded micro-samples; it is not enough evidence to reject the bucket by
itself.

A larger parallel smoke run (`seed_base=2026070410`, 8 shards, 10 total probes)
found flop `to_raise` mean primary delta `-5.8` with 95 percent CI
`[-179.39, 167.79]`. The largest negative outlier used `advised_final=200`,
while most `advised_final=101` probes were neutral or positive, so scale-up
analysis should check `by_advised_final` and `by_raise_delta_band` before
training or shipping a raise gate.

`versions/v025_v254_low_raise_gate_h96_254` is a narrow v022 fork created from
that observation. It keeps the same h96 policy weights but limits neural raise
interventions to low-size raises (`max_raise_delta=125`,
`max_raise_pot_ratio=0.65`). It is an experiment candidate, not a validated
successor, until paired battles and larger counterfactual shards are positive.
On the same p16 seed range, v025 produced 9 low-raise flop probes with mean
primary delta `+111.11` and no high-raise probes. A one-pair common-deck mirror
smoke against `claude_v279` was roughly neutral versus v022 (`-82` chips over
140 hands). This supports keeping v025 for larger tests, not promoting it.

The larger p120 deterministic shard run (`seed_base=2026070500`, 40 shards)
crossed the counterfactual scale-up gate: 108 hand-scoped flop `to_raise`
probes, mean primary delta `+90.68`, 95 percent CI `[69.62, 111.73]`, and all
probes in `raise_le_125`. A two-pair common-deck mirror against `claude_v279`
was still inconclusive versus v022 (`[-9095, +14398]` chips over paired
140-hand samples), so the next step is more paired battle volume before calling
v025 a clear performance improvement.

`paired_evaluate.py` supports deterministic deck seeds, optional bot-subprocess
RNG seeds, parallel workers, and `--resume` for extending an existing seeded
run without replaying completed pairs. Use `--seed-base` to fix the local judge
decks and `--bot-seed-base` when the compared bots use Python `random` inside
their simulation code. The 16-pair seeded mirror run (`seed_base=2026070600`,
`workers=8`) kept v025 positive on average versus v022 against `claude_v279`
(`+796.44` chips per 70 hands), but the 95 percent CI still crossed zero
(`[-2089.29, 3682.16]`). Extending the same run to 32 pairs improved the mean
to `+1170.11` chips per 70 hands, but the 95 percent CI still crossed zero
(`[-528.02, 2868.23]`). Extending again to 64 pairs reduced the mean to
`+686.17` chips per 70 hands with 95 percent CI `[-602.07, 1974.41]`. v025
remains promising at the counterfactual-action level, but it is not a clear
end-to-end winner.

A 4-pair smoke with both deck and bot RNG seeds (`seed_base=2026070800`,
`bot_seed_base=202607080000`) produced byte-identical JSON on rerun, confirming
the seeded subprocess path is suitable for larger deterministic evaluation.
Extending that deterministic setup to 32 pairs made the v025 signal essentially
neutral versus v022: `+95.09` chips per 70 hands, 95 percent CI
`[-701.02, 891.21]`, median paired delta `0`, with 11 positive, 11 zero, and
10 negative samples. Do not spend the next scale step on more v025 threshold
tuning; move to better action-value targets.

The counterfactual target generator now also supports deck plus bot RNG seeds.
A v025 flop `to_raise` smoke (`seed_base=2026070900`,
`bot_seed_base=202607090000`, 2 probes) reran byte-identical and wrote scan
`bot_seeds` plus per-probe `branch_bot_seeds`. This is the minimum data
contract before larger sampling: expand probes only from reproducible
action-value labels, not from unseeded trace outliers.

`versions/v026_v254_flop_low_raise_h96_254` narrows v025 to the only bucket
covered by the p120 counterfactual evidence: low-size flop free-action raises.
The direct 16-pair check against v025 was positive on average (`+1133.59` chips
per 70 hands), but the 95 percent CI still crossed zero
(`[-1027.52, 3294.71]`). A direct 32-pair check against v022 was slightly
negative (`-301.33` chips per 70 hands, 95 percent CI
`[-2241.13, 1638.48]`). v026 is therefore a recorded experiment, not a
successor.

The first counterfactual-trained advantage branch is also only a recorded
experiment. `counterfactual_rollout_probe.py` now exports the same 70-dimension
advantage features used by runtime gates and seeds the local analysis RNG in
addition to deck and bot subprocess RNG. A 62-probe p64 run for v025 flop
low-raise labels stayed positive (`+59.13`, 95 percent CI `[30.97, 87.29]`),
but the h32 gate trained from it did not transfer: v027 lost slightly to v025
over 16 paired mirrors (`-107.28` chips per 70 hands), and v028 with a lower
gate threshold was also slightly negative over 8 pairs. The next scale step
needs broader counterfactual coverage, especially examples that prevent the
gate from blocking rare high-value v025 raises.

`versions/v029_v254_cf_advantage_pair11_h32_t090` adds the worst v027 outlier
seed back into the counterfactual training set. That repaired the specific
over-filtering failure: v029 restored v025's 3 neural raises on the outlier
trace. It still did not beat v025 over 16 paired mirrors (`-2.25` chips per
70 hands, 95 percent CI `[-12.45, 7.95]`). Treat v029 as a diagnostic repair,
not a stronger bot.
