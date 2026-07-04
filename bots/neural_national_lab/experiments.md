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

## v005_v279_call_rescue214

- Base: `v004_v279_guarded214`.
- Change: neural advisor can only rescue a rule fold into a cheap call when the
  model has very high call confidence. Fold, raise, and all-in overrides are
  disabled.
- Rationale: v004 and v003 showed that broad action replacement hurts; this
  version tests whether the network is useful only as a low-cost anti-overfold
  prior.

## v006_v279_call_rescue254

- Base: `v005_v279_call_rescue214`.
- Change: same call-rescue-only runtime policy, but the tiny MLP is distilled
  from `claude_v254` instead of `claude_v214`.
- Rationale: v254 exposed the largest gap for v279 in early battle samples, so
  this tests whether a teacher closer to that counter-strategy gives a more
  useful low-cost call prior.

## v007_v254_call_rescue254

- Base: `claude_v254`.
- Neural source: `teacher254_round1` MLP.
- Runtime policy: same call-rescue-only advisor as v006.
- Rationale: v006 showed that v254's call prior is harmful on a v279 base; this
  version tests whether the same neural prior works when paired with its native
  teacher-family rule base.

## Round 1 Notes

- Training data: `teacher214_round1.jsonl`, 272 teacher decisions from
  `claude_v214` against `claude_v279` and `claude_v254`.
- Training metrics: train accuracy 0.91, validation accuracy 0.71, average
  confidence 0.90.
- A single ordinary 70-hand battle for `v001_v279_teacher214` vs `claude_v279`
  completed; v001 lost that one-game sample.
- A single ordinary 70-hand battle for `v004_v279_guarded214` vs `claude_v279`
  completed; v004 won that one-game smoke sample.
- The mirror evaluation runner was terminated by SIGTERM in this environment,
  so current battle evidence is only a smoke/small-sample signal.

## Round 2 Notes

- `v004_v279_guarded214` vs `claude_v279`, ordinary battle 3 games:
  `[-50, -72, -3001]`, mean `-1041`.
- `claude_v279` vs itself under the same ordinary-battle setup:
  `[10075, 47, 1032]`, mean `3718`, so v004 is materially below the local
  baseline in this non-mirrored sample.
- `claude_v214` vs `claude_v279`, ordinary battle 3 games:
  `[10139, -9731, 143]`, mean about `184`.
- `v003_v214_hybrid_prior` vs `claude_v279` had a first completed game of
  `-913` before the long run was interrupted, so v003 is not promising without
  tighter gating.
- `v005_v279_call_rescue214` vs `claude_v279`, ordinary battle 3 games:
  `[-364, -785, 111]`, mean `-346`.
- `v005_v279_call_rescue214` vs `claude_v254`, two separate 2-game batches:
  first `[-5677, 11766]`, second `[-19992, 127]`, combined mean about `-3444`.
- `claude_v279` vs `claude_v254`, ordinary battle 2 games:
  `[-16717, -180]`, mean `-8448.5`; v005 reduced the loss in this small sample
  but did not produce a stable positive edge.
- `teacher254_round1.jsonl`: 210 teacher decisions from `claude_v254` against
  `claude_v279` and `claude_v214`; train accuracy `0.85`, validation accuracy
  `0.71`, average confidence `0.84`.
- `v006_v279_call_rescue254` failed: vs `claude_v254` `[-14859, -20018]`, vs
  `claude_v279` `[-4208, -95]`. This suggests the v254 teacher prior is not
  portable to the v279 base under call-rescue gating.
- `v007_v254_call_rescue254` vs `claude_v279`, ordinary battle 4 games across
  two batches: `[630, -51, 10049, 6420]`, combined mean `4262`, record `3-1`.
- Same first batch baseline, `claude_v254` vs `claude_v279`: `[-136, 16100]`,
  mean `7982`. v007 is positive against v279 but not proven better than its
  v254 base.
- Current best neural variant by positive battle evidence: `v007_v254_call_rescue254`.
  Evidence is still non-mirrored and low-sample; the next step is larger
  incremental battle batches and a faster mirror-compatible evaluator.
