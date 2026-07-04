# Experiments

## v001_v279_teacher214

- Base: `claude_v279`.
- Teacher: `claude_v214`.
- Integration: conservative neural advisor.
- Status: planned for first runnable snapshot.

## v002_v279_aggressive214

- Base: `claude_v279`.
- Teacher: `claude_v214`.
- Integration: lower advisor thresholds for exploration.
- Status: planned for first runnable snapshot.

## v003_v214_hybrid_prior

- Base: `claude_v214`.
- Integration: national-native version with neural prior hook.
- Status: planned for first runnable snapshot.

## v004_v279_guarded214

- Base: `v001_v279_teacher214`.
- Change: much stricter neural gating after v002/v003 showed harmful
  intervention in small samples.
- Runtime policy: high-confidence fold/call only in low-risk spots; raise and
  all-in are effectively disabled until the dataset contains enough examples.
- Status: added as the next stability baseline.

## Round 1 Notes

- Training data: `teacher214_round1.jsonl`, 272 teacher decisions from
  `claude_v214` against `claude_v279` and `claude_v254`.
- Training metrics: train accuracy 0.91, validation accuracy 0.71, average
  confidence 0.90.
- A single ordinary 70-hand battle for `v001_v279_teacher214` vs `claude_v279`
  completed; v001 lost that one-game sample.
- The mirror evaluation runner was terminated by SIGTERM in this environment,
  so current battle evidence is only a smoke/small-sample signal.
