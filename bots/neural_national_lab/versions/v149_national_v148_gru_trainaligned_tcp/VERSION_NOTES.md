# v149 - align legacy GRU runtime with its padded training graph

Parent: `v148_national_v147_zeroaware_typegate_tcp`.

This diagnostic version keeps the v146 weights, v147 stream-safe parser, and
v148 zero-aware profile fix. It changes only legacy GRU inference when the
current-hand action history has between 1 and 15 entries.

The v146 trainer always ran 16 GRU steps, padding a non-empty short history
with zero rows. Its pure-Python runtime ran only the observed steps. A one-step
audit found a classification-probability max difference of 0.0493. v149 pads
non-empty histories to 16 steps at runtime, reproducing the graph that trained
the existing weights. Empty histories still use the trainer's masked zero
embedding.

This is not the new match-aware multi-task model and is not presumed stronger.
It exists to measure deployment drift with paired deterministic evaluation.
