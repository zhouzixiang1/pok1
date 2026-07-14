# Route A2 M4 real-HUNL blueprint-only validation

Date: 2026-07-14 (Asia/Shanghai)

Route baseline: `59275e9bf63cfd03d66df9d8a232040586465e65`

Common dependency commit: `a938d7cbc36016cb7b5cb444a7eb2e0f00cae73e`

## Result

M4 passes as a **low-budget real-HUNL blueprint-only vertical slice**. It does
not pass as a strong bot, a dense or converged blueprint, an official-platform
certificate, or a reproduction of DecisionHoldem's unpublished assets.

The old Leduc projection remains historical and is not used by this result.
Training, terminal utility, card evaluation and every legal action interval use
the Common `NationalGameState`/`Action`/`LegalActionSet` contracts. The only
match backend is `sever.engine.game.GameEngine` over TCP; no top-level
`engine/`, Botzone JSON subprocess, or `engine/battle.py` path is imported.

## Reproduction commands

```bash
/home/zzx/.cache/pok-research-py312/bin/python -m \
  bots.research_native_lab.rebel_decisionholdem.tools.train_hunl_blueprint \
  --config bots/research_native_lab/rebel_decisionholdem/configs/hunl_m4_smoke.json \
  --scale-evidence /tmp/route-a2-m4-scale.json

/home/zzx/.cache/pok-research-py312/bin/python -m \
  bots.research_native_lab.rebel_decisionholdem.tools.run_hunl_tcp_smoke \
  --blueprint bots/research_native_lab/rebel_decisionholdem/artifacts/hunl_m4_smoke_blueprint.json \
  --deck-root-seed 20260714 \
  --client-policy-seeds 2026071403 2026071404 \
  --output /tmp/route-a2-m4-sever-70h.json

/home/zzx/.cache/pok-research-py312/bin/python -m pytest \
  bots/research_native_lab/rebel_decisionholdem/tests -q
python -m pytest bots/research_native_lab/rebel_decisionholdem/tests -q

/home/zzx/.cache/pok-research-py312/bin/python -m \
  bots.research_native_lab.rebel_decisionholdem.tools.milestone_manifest
```

The scale and TCP evidence committed in `evidence/` contains measured timing,
so a rerun is expected to reproduce semantic counters/digests but not identical
wall-clock fields.

## HUNL abstraction

- Rules: two players, 20,000 chips each, blinds 50/100, preflop/flop/turn/river.
- Private path: all 1,326 exact Common combos and exactly 169 preflop classes.
- Postflop: exact made-hand category plus a high-rank strength band, board
  pairing, suit texture, straight connectivity, hole rank blockers, dominant
  suit blockers. Exact combo index and legal opponent-combo count after card
  removal are retained as diagnostic metadata only and do not enter the policy
  key.
- Public betting key: street, SB/BB position, pot bucket, SPR bucket, call-ratio
  bucket, abstract complete action line, street raise count and legal signature.
- Actions: fold, check/call, Common exact min raise, approximately 0.5/1.0/1.5
  pot raise-to totals, and all-in. Invalid sizes are omitted and equal wire
  actions are deduplicated. Golden Common boundary `raise 200 -> raise 400`
  passes.

The abstraction is deterministic under hole/board reordering and global suit
permutation for the tested feature buckets. It is a clean-room functional
abstraction, not DecisionHoldem's missing cluster files.

## External-sampling Linear CFR

Each iteration samples one counter-based exact nine-card deal and executes one
external-sampling traversal per player. At a traverser node with current policy
`sigma_t`, regrets use:

```text
v = sum_a sigma_t(a) * v(a)
R(I,a) += t * (v(a) - v)
```

The average policy is a separate, linear-weighted SIMPLE external-sampling
average. During player `p`'s regret traversal, only sampled nodes owned by
`1-p` update:

```text
S(I,a) += t * sigma_t(a)    when actor(I) != traverser
```

This follows OpenSpiel's two-player `AverageType::kSimple` update site with an
LCFR iteration multiplier. It is deliberately not a full-tree reach-weighted
average. A fixed HUNL trajectory oracle verifies both the update location and
the exact delta. A two-step Kuhn-shaped counterexample whose deep infoset is
reachable only after an opponent action, with changing opponent reach across
iterations, proves the retired `traverser + own_reach` estimator differs.

The regret equation matches an independently written small oracle. Regret and
average-strategy tables are distinct. The strategy for an iteration is frozen
from the starting regret snapshot; counter-based deal/opponent draws depend on
global iteration and path, not mutable RNG state.

Checkpoint and sequential segment properties tested:

- atomic JSON replacement and complete content SHA-256;
- strict schema, canonical infoset/action signature and finite exact numerics;
- duplicate JSON key and `NaN`/`Infinity` rejection;
- resume and different sequential segment sizes are byte-identical;
- each segment binds the complete starting checkpoint digest;
- corrupted segment/result validation completes before live state adoption.

These “segments” are sequential continuations. M4 has no independently
mergeable parallel shard algorithm and makes no multicore-training claim.

## Frozen smoke artifact and scale gate

- Artifact: `artifacts/hunl_m4_smoke_blueprint.json`
- Preregistered candidates: `2, 4, 8, 16, 32`; the first training-only
  material-policy pass is frozen at `32` iterations/deals and `64` traversals.
- Exact artifact/checkpoint/identity digests, bytes, node/row counts and
  current-process resource measurements are content-bound in
  `evidence/m4_scale_gate.json` and cross-checked by the milestone manifest.
- The checkpoint/selection journal and heartbeat are retained in a gitignored
  fixed workspace for strict resume and audit; they are not committed.

At the predeclared materiality threshold `L1(policy, uniform) > 1e-6`, the
scale evidence records exact and all three backoff row/material counts and
maximum L1 distances. Candidate selection reads only these training tables;
TCP cards, actions, earnings and timing are excluded.

That estimate explicitly ignores infoset growth, cache behavior and variance.
`scale_authorized` and `parallel_checkpoint_segment_merge_supported` are both
false. No large job was started.

## Common policy/runtime integration

The strict artifact binds rules, abstraction, algorithm, config/seed, Common
package tree, route trainer/consumer source hashes, counters, checkpoint digest,
fidelity and every policy row. The loader recomputes these bindings and rejects
unknown/missing actions, bool/string numerics, nonfinite values, duplicate keys,
bad hashes and Common/source drift.

Checkpoint v5 closes the resume-history gap: its body includes a v4 frozen
training identity containing the complete Common file map/tree, 20k/50/100
rules, transition/card/action/utility semantics, the abstraction, external
sampler, blueprint builder and training-tool source hashes, and an explicit
empty external-asset list. Sequential segment v5 binds that identity digest in
addition to the complete starting checkpoint digest. A body whose hash is
re-signed after identity mutation still fails; simulated current-code/rule
drift fails closed before state adoption.

The durable checkpoint is intentionally gitignored rather than committed. Therefore the
`trained-derived` evidence boundary is generator plus manifest, not the
artifact loader alone: the manifest deterministically trains, materializes the
identity-bound checkpoint payload, reloads it through the strict checkpoint
validator, rebuilds exact and backoff tables, and requires byte equality with
the frozen artifact. Merely re-signing an arbitrary artifact body is not this
proof.

`CommonA2StrategyRuntime` first uses an exact average-strategy row. An exact
miss then walks a fixed, artifact-bound hierarchy: public action context,
street/position/legal signature, then legal signature. Those coarse policies
sum LCFR linear iteration-weighted SIMPLE sampled `strategy_sums` action mass
under the declared key and normalize; they never aggregate final regrets across
infosets. They never inspect the smoke deck,
opponent, or chip result. Uniform current-legal-signature policy is retained
only as an emergency after all trained levels miss. Every source is traced.

The four RNG roots are predeclared and distinct: training `2026071402`, local
TCP deck `20260714`, and client policy sampling `2026071403`/`2026071404`.
Their hash domains and the fact that TCP inputs are excluded from blueprint
construction are bound in both evidence files and verified by the milestone
manifest. Blueprint-only tests disable network and resolve entry points and
still decide legally. Sticky/split input, suppressed closing action, ordered
one-shot name/decision leases, stale binding and all-in no-extra-send paths pass.

## TCP evidence: two separate claims

Frozen local evidence:

- Backend: unmodified `sever.engine.game.GameEngine`, validator and server
  protocol over real asyncio TCP sockets.
- Client framing: explicit `sever-local-line-adapter`, because the local server
  reads newline-delimited client actions.
- Hands/local settlements: `70`; both clients reconstructed `70/70`.
- Illegal actions and timeouts are both zero. Exact action/lookup counts,
  whole-run elapsed, process peak RSS, per-client compute maxima and server
  wait maxima are bound in `evidence/m4_sever_tcp_70h.json`.
- The evidence includes a timing-free semantic projection of every server
  event and deterministic client counter. A second real fixed-seed 70-hand run
  must reproduce it exactly; only declared wall-clock/wait/latency fields are
  excluded.

Deterministic local net chips and per-hand earnings are retained as a
reproducible diagnostic record. They have zero acceptance or strength weight.

The influence gate was declared independently of the chip result. It requires
each client to consume at least one trained-derived policy and at least one
non-uniform trained-derived policy. Both clients passed; the exact/coarse/
emergency counts above are the stronger coverage evidence. This is policy-
wiring evidence, not evidence that the learned strategy is good.

Official-facing framing is tested separately with a real socket pair: inbound
`name` plus split/sticky preflop and split numeric `raise 2` + `00call` decode
correctly, while official-mode name/action sends contain no CR/LF bytes. The
production client default delay is 0.30 seconds. The local 70-hand line-adapter
run does **not** prove official raw framing acceptance, and its 70 settlements
do **not** prove the official EXE/THP special final-hand completion rule.

## Falsifiers before expansion

Large training remains blocked until all of these have explicit gates:

1. exact/coarse/emergency coverage meets a separately frozen bound on held-out
   deck seeds and opponents; zero emergency use on this one smoke is insufficient;
2. abstraction resolution and card-removal features beat simpler baselines;
3. sampled exploitability/convergence or an accepted HUNL proxy improves with
   more iterations and multiple seeds rather than regressing;
4. deterministic independently mergeable parallel segments are designed and
   proven equivalent before claiming multicore utilization;
5. measured RSS/disk/time growth stays inside a frozen resource plan;
6. official raw transport and the final-hand THP rule pass the official oracle;
7. complete-match W/L/D strength evaluation is performed separately from
   compliance and diagnostic chip totals.

Safe resolve/search, neural leaf values, opponent-specific online adaptation,
official certification and strength freezing remain later milestones.
