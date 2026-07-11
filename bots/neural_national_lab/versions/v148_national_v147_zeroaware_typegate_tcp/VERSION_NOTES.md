# v148 - zero-aware passive opponent gate

Parent: `v147_national_v146_streamsafe_tcp`.

This version keeps the v146 model and the v147 stream-safe TCP parser. It fixes
one opponent-gate bug found by paired decision-trace replay: Python's
`value or 0.5` converted valid zero PFR/aggression rates into the unknown-value
default. As a result, clearly passive profiles such as `PFR=0.0,
aggression=0.0` bypassed the gate and triggered the first GRU raise override.

The fix preserves finite zero values and uses `0.5` only for missing, `None`,
or non-numeric profile values. No thresholds or model weights change.

The triggering evidence is in the v146 live-pool trace reports: the worst
`national_v119` and `national_v141` seed-7400 matches first diverged from v140
on hand 7 with at least 13 observed actions and a zero-valued passive profile.
