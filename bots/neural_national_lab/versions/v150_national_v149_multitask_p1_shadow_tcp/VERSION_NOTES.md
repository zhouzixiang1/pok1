# v150 - 742k-parameter match-aware multi-task shadow

Parent: `v149_national_v148_gru_trainaligned_tcp`.

This integration version adds the first `opp_multitask_gru_v1` model to the
native national TCP bot. The 742,506-parameter network has shared state,
intra-hand GRU, and cross-hand opponent encoders plus:

- hand, tail, and full-match value heads;
- mean and lower-quantile outputs for all six action labels;
- an opponent fold/check/call/raise/allin response head;
- an opponent raise-size head.

The model is the winner of a preliminary two-seed 57k/200k/742k scaling smoke
on only 32 training value rows and 240 behavior rows. It is not strength-ready:
held-out opponent-action accuracy regressed badly. v150 therefore runs it in
shadow mode only and preserves v149 actions exactly. Its purpose is to verify
stdlib-only inference latency, native TCP stability, telemetry, and Torch/runtime
agreement before the full opponent-disjoint dataset is available.

Local synthetic inference measured approximately 0.13 seconds to load the 15MB
JSON and 0.058 seconds for value plus response inference on this machine.
