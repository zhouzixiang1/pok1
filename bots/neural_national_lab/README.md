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
  --games-per-shard 3 \
  --max-probes-per-shard 8 \
  --stage flop \
  --kind to_raise \
  --seed-base 20260704 \
  --output bots/neural_national_lab/data/counterfactual_v022_flop_seeded_shards.json
```

The shard runner writes per-shard JSON files next to the merged output, so a
negative bucket or suspicious outlier can be replayed with the same seed range.
Existing shard files are reused by default; pass `--rerun-existing` when the
probe code or filters changed and the shards should be regenerated.
The first seeded smoke run (`seed_base=2026070401`, 2 shards, 4 total probes)
found flop `to_raise` mean primary delta `-64.25` with a very wide confidence
interval. Treat that as a warning against shipping or training from optimistic
unseeded micro-samples; it is not enough evidence to reject the bucket by
itself.
