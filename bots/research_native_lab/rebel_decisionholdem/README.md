# Route A research package

This isolated package contains two clean-room research candidates:

- A1: ReBeL-like public-belief and counterfactual-value experiments;
- A2: DecisionHoldem-like blueprint and later safe-resolving experiments.

The current gate is **M4**. A1 remains at its exact Kuhn/Leduc M3 gate. A2 now
has a real, low-budget HUNL blueprint-only vertical slice. “Real HUNL” here
means the 20,000-chip, 50/100-blind, four-street game is traversed through the
separately owned Common `NationalGameState`, `Action`, `LegalActionSet`, card
evaluator, and terminal utility. It does not mean the 32-iteration smoke
blueprint is strong, submission-ready, or a reproduction of DecisionHoldem's
missing production assets. The current training-only selection uses 32
deterministic deals and 64 player traversals.

No DecisionHoldem AGPL code was copied. The papers and public repository were
used for algorithm, symbol, availability, and license audits only. Source
identities and downloaded-paper hashes are in `manifests/sources.json`.

## A2 M4 vertical slice

`decisionholdem_like/hunl_abstraction.py` defines a versioned abstraction:

- all 1,326 exact hole combinations retain a diagnostic Common combo index and
  map to the canonical 169 preflop policy classes; the exact index is not a
  policy-key field;
- postflop buckets record exact made-hand category, high-rank strength band,
  board pairing, suit texture, straight connectivity, rank blockers, dominant
  suit blockers. The legal-opponent-combo count is diagnostic street-level
  metadata, not a claimed policy-key/card-removal feature;
- betting buckets include position, street, pot, SPR, amount to call, public
  action line, raise count, and the current legal action signature;
- actions are derived from Common legality only: fold, check/call, exact minimum
  raise, approximately 0.5-pot, 1.0-pot, 1.5-pot, and all-in. Raise sizes are
  raise-to totals. Collisions and out-of-interval sizes are deterministically
  removed; for example, the opening 0.5-pot size collides with `raise 200`.

`decisionholdem_like/hunl_external_sampling.py` implements deterministic
external-sampling Linear CFR. Each global iteration samples one exact disjoint
52-card deal, then runs one traversal per player. Regrets and linear
weighted simple sampled-average strategy sums are separate tables. During the
traversal that updates one player, `t * current_policy` is accumulated only at
the other player's sampled nodes, matching OpenSpiel's two-player SIMPLE
external-sampling average with LCFR's iteration weight. Counter-based chance
and opponent draws make resume and sequential checkpoint segmentation
bit-equivalent. A “segment/shard” in this M4 code is sequential and
digest-bound; independently mergeable parallel shards and multicore training
are not implemented or claimed.

Checkpoints and segments use strict schemas, duplicate-key/nonfinite rejection,
content hashes, atomic replacement, start-state binding, and validate-before-
adopt transactional application. Checkpoint v5 embeds a frozen identity v4
containing the Common
tree, rules/transition/card/action/utility identity, critical route source
hashes, and the explicit no-external-assets contract. Segment v5 binds the
same identity digest, so old or re-signed state cannot resume after code/rules
drift. `artifacts/hunl_m4_smoke_blueprint.json` is
the small deterministic artifact produced by the checked-in 32-iteration
config. Its wrapper binds rules, abstraction, algorithm, seed/config, Common
tree, route source hashes, training counters, checkpoint digest, fidelity, and
all exact and trained-backoff policy rows.

Sparse coverage is explicit. Exact average-strategy rows take priority. Misses
walk three fixed trained-data aggregations: public action context,
street/position/legal signature, and legal signature. Their policies sum the
LCFR linear-weighted SIMPLE sampled `strategy_sums` action mass under the
declared key and then normalize; final regrets are never aggregated across
infosets.
Only a miss at all three levels uses the artifact-declared uniform emergency
distribution over the current Common-derived legal signature. Every source is
content-bound, opponent-independent, smoke-deck-independent, and recorded.
This hierarchy is intentionally coarse; it is not a hidden heuristic or a
claim of equilibrium quality.

The material-policy threshold is frozen as `L1(policy, uniform) > 1e-6`.
Scale evidence records the exact/backoff row counts and maximum L1 distances;
the TCP influence gate requires both clients to actually consume at least one
materially non-uniform trained-derived distribution. Training retains a
gitignored fixed-workspace checkpoint/selection journal and heartbeat. Every
segment is atomic, resume replays the candidate prefix, and `CANCEL` is honored
only at durable boundaries. Derivation authority remains the deterministic
manifest path: reload the identity-bound checkpoint, rebuild all policy tables,
then require byte equality with the artifact. The artifact wrapper alone is
not treated as proof that a row was trained.

## Runtime and TCP boundary

`CommonA2StrategyRuntime` loads the M4 artifact, consumes Common one-shot
decision leases, samples only the matched legal signature, binds decisions to
the full state for stale-send rejection, and records the abstraction key,
lookup source, legality, fallback use, and compute time. Blueprint-only
decisions import no resolve, search, opponent model, or network service.

`decisionholdem_like/hunl_tcp_client.py` is the route-owned persistent socket
client. Its official-facing default is raw, delimiter-free sends with a 0.30
second action delay. The local `sever/server/tcp_server.py` implementation reads
client lines, so local diagnostic matches must explicitly select
`--sever-line --action-delay 0`. These are two different transport tests:

- `evidence/m4_sever_tcp_70h.json` proves a complete local 70-hand match on the
  unmodified `sever.engine.game.GameEngine`, over real TCP sockets, using the
  explicit sever-local line adapter;
- the raw socket regression proves sticky/split inbound decoding and confirms
  official-mode name/actions contain no CR/LF delimiter.

The local sever match's 70 settlements are not the official Windows EXE's
special hand-70 terminal proof. Neither the local match chips nor its winner is
strength, Glicko, compliance, or certification evidence. No top-level
`engine/`, `engine/battle.py`, Botzone JSON stdin/stdout, or legacy adapter is a
dependency of this route.

## Reproduce M4

From the repository root:

```bash
# Deterministically rebuild the selected 32-iteration smoke and resource gate.
python -m bots.research_native_lab.rebel_decisionholdem.tools.train_hunl_blueprint \
  --scale-evidence /tmp/route-a2-m4-scale.json

# Complete local sever GameEngine/TCP diagnostic. This explicitly uses the
# sever-local line adapter; it is not an official raw-framing certificate.
python -m bots.research_native_lab.rebel_decisionholdem.tools.run_hunl_tcp_smoke \
  --blueprint bots/research_native_lab/rebel_decisionholdem/artifacts/hunl_m4_smoke_blueprint.json \
  --deck-root-seed 20260714 \
  --client-policy-seeds 2026071403 2026071404 \
  --output /tmp/route-a2-m4-sever-70h.json

# A second real fixed-seed run must reproduce the frozen semantic projection;
# wall-clock, wait and latency fields are intentionally excluded.
python -m bots.research_native_lab.rebel_decisionholdem.tools.run_hunl_tcp_smoke \
  --blueprint bots/research_native_lab/rebel_decisionholdem/artifacts/hunl_m4_smoke_blueprint.json \
  --verify-frozen /tmp/route-a2-m4-sever-70h.json

# Official-facing client defaults: raw no delimiter and 0.30 second delay.
python -m bots.research_native_lab.rebel_decisionholdem.decisionholdem_like.hunl_tcp_client \
  --host 127.0.0.1 --port 10001 --name RouteA2 \
  --blueprint bots/research_native_lab/rebel_decisionholdem/artifacts/hunl_m4_smoke_blueprint.json

python -m pytest bots/research_native_lab/rebel_decisionholdem/tests -q
python -m bots.research_native_lab.rebel_decisionholdem.tools.milestone_manifest
```

The default fixed workspace is under the package's ignored `checkpoints/`
directory. If its run journal already exists, start with `--resume <journal>`
or choose a different output/workspace; the CLI never silently overwrites a
prior run. Large intermediate files must not be committed.

## Honest stage boundary

M4 proves a runnable blueprint-only HUNL path and its correctness/resume/resource
contracts. It does not prove equilibrium quality, exploitability in HUNL,
DecisionHoldem fidelity beyond the named clean-room algorithms, official EXE
acceptance, online safe resolving, off-tree translation, neural leaf values, or
match strength.

Before expanding training, the next gate must falsify or address:

- exact/coarse/emergency coverage on held-out seeds and opponents (the frozen
  smoke's zero emergency decisions is only an influence/wiring result);
- inadequate abstraction resolution or suit/card-removal invariance;
- external-sampling variance and absent convergence evidence;
- lack of independently mergeable deterministic parallel shards;
- memory/disk growth beyond the linear smoke estimate;
- any mismatch between local sever line framing and official raw transport;
- absence of official Windows EXE certification and strength evaluation.

M5+ work may add larger training only after those gates. Safe resolve/search and
neural leaf values remain separate later milestones and must not be inferred
from this blueprint-only result.
