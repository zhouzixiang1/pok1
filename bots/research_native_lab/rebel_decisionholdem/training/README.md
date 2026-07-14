# Route A training boundary through M4

M3's deterministic Kuhn and limit-Leduc LCFR solvers remain exact small-game
oracles. The old `tools/train_blueprint.py` Leduc projection remains a labelled
historical packaging prototype and is not the M4 HUNL artifact.

M4 adds `HUNLExternalSamplingLCFR`, a correctness-first external-sampling
Linear CFR backend whose environment is the Common 20,000/50/100 four-street
HUNL state machine. One iteration samples one exact nine-card deal and traverses
once for each player. Regret and average-strategy sums are separate; the global
iteration index weights both updates. The average is the two-player SIMPLE
external-sampling estimator: on player `p`'s regret traversal, each sampled
decision node owned by `1-p` contributes `t * current_policy`. It is not a
full-tree reach-weighted average.

The checked-in `configs/hunl_m4_smoke.json` preregisters candidates
`[2, 4, 8, 16, 32]`; the training-only first-pass rule freezes 32 iterations
over 32 independently counter-generated deals (64 player traversals). It
produces `artifacts/hunl_m4_smoke_blueprint.json` and retains a durable,
gitignored fixed-workspace checkpoint/selection journal plus heartbeat.
`evidence/m4_scale_gate.json` records nodes, rows, current-process elapsed/RSS,
artifact/checkpoint bytes, plus a deliberately caveated linear estimate.
Its `scale_authorized` field is false.

The artifact contains exact LCFR average-strategy rows plus a fixed hierarchy
of trained backoff rows. Backoff rows sum the LCFR linear-weighted SIMPLE
sampled `strategy_sums` action mass by public action context, then
street/position/legal signature, then legal signature, and normalize. They do
not aggregate final regrets across infosets. The uniform legal policy is
emergency-only. Training,
TCP deck, and the two runtime-policy roots are distinct, predeclared, domain-
separated, and manifest-checked; smoke cards and results are not builder inputs.

Checkpoint segments are sequential continuations bound to the complete input
checkpoint digest and the same frozen training-identity digest. That identity
contains the complete Common file map, rules and transition/card/action/utility
semantics, critical trainer/abstraction/blueprint-builder source hashes, and a
no-external-assets declaration. Different segment sizes and save/resume boundaries are
bit-equivalent. They are not independently trainable or mergeable shards and
do not yet use multiple cores. A corrupted checkpoint/segment is fully
validated before any live trainer state changes.

The retained run journal is bound to the full config source bytes, output and
workspace paths, source identity, candidate sequence and target. Every segment
is atomically persisted, resume deterministically replays all recorded
candidate results, and a real regular `CANCEL` marker stops only at a durable
checkpoint. The milestone tool independently reruns the generator, reloads the
identity-bound trainer payload, rebuilds exact and hierarchical backoff
policies, and compares the full artifact byte-for-byte.

Rebuild:

```bash
python -m bots.research_native_lab.rebel_decisionholdem.tools.train_hunl_blueprint \
  --config bots/research_native_lab/rebel_decisionholdem/configs/hunl_m4_smoke.json \
  --scale-evidence /tmp/route-a2-m4-scale.json
```

To start and then strictly resume an explicit durable workspace:

```bash
python -m bots.research_native_lab.rebel_decisionholdem.tools.train_hunl_blueprint \
  --checkpoint /tmp/route-a2-hunl-smoke-checkpoint.json \
  --heartbeat /tmp/route-a2-hunl-heartbeat.json \
  --output /tmp/route-a2-hunl-smoke-blueprint.json

python -m bots.research_native_lab.rebel_decisionholdem.tools.train_hunl_blueprint \
  --resume /tmp/route-a2-hunl-smoke-checkpoint.json \
  --heartbeat /tmp/route-a2-hunl-heartbeat.json \
  --output /tmp/route-a2-hunl-smoke-blueprint.json
```

Do not start large training merely because the smoke completes. Expansion is
blocked until fallback coverage, sampled convergence/variance, abstraction
falsifiers, parallel deterministic segment design, resource growth, official
raw transport, official EXE compliance, and independent strength gates are
defined and passed. No neural training or online resolving is part of M4.
