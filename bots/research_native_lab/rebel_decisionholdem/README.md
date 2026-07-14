# Route A research package

This isolated package contains two clean-room research candidates:

- A1: ReBeL-like public-belief and counterfactual-value experiments;
- A2: DecisionHoldem-like blueprint and later safe-resolving experiments.

The package now contains two separately bounded frozen gates. A2 remains at
its **M4** real-HUNL blueprint-only vertical slice. A1 has advanced to **M5a**,
an exact-oracle PBS and value-label contract correctness gate. M5a is not the
later neural-value or online-search milestone.

A2's “real HUNL” claim means the 20,000-chip, 50/100-blind, four-street game is traversed through the
separately owned Common `NationalGameState`, `Action`, `LegalActionSet`, card
evaluator, and terminal utility. It does not mean the 32-iteration smoke
blueprint is strong, submission-ready, or a reproduction of DecisionHoldem's
missing production assets. The current training-only selection uses 32
deterministic deals and 64 player traversals.

No DecisionHoldem AGPL code was copied. The papers and public repository were
used for algorithm, symbol, availability, and license audits only. Source
identities and downloaded-paper hashes are in `manifests/sources.json`.

## A1 M5a PBS and exact-label gate

`rebel_like/hunl_pbs.py` defines the HUNL public belief state over Common's
fixed 1,326-combination order. Its two 1,326-vectors are explicitly **reach
factors**, not true private-hand marginals. For board `B`, the true joint and
projected marginals are derived from

```text
J(h0,h1) = K_B(h0,h1) * beta0(h0) * beta1(h1) / Z.
```

`K_B` enforces public-board and cross-player card removal. A public action
multiplies and normalizes only the actor's factor; both projected marginals can
still change. The factor normalizer and blocker-aware public event probability
are separate quantities. Legal, positive-factor, and label-valid masks are
exported explicitly.

HUNL construction accepts only the exact nonterminal Common
`hand_public_dict()` schema after terminal fields are removed. Direct payloads
must survive a Common history replay and byte-equivalent public round trip;
unknown fields, bool-as-player aliases, invalid boards/histories, private
cards, outcome, match context, seed, timing, and future-card data fail closed.
Uniform factors may be initialized only at a true new-hand root. Later states
must come from proven action/chance transitions or explicitly validated
factors. Public-node identity, mathematical PBS identity, and path provenance
are separate: the value-net-shaped PBS identity contains public state plus the
two factors, while the policy/action trace is audit metadata and never a model
feature.

Observed HUNL raises are included at the exact Common raise-to amount or
rejected; no nearest-action translation exists. A relayed street-closing action
and the same uniquely inferred official boundary update the PBS exactly once.

The committed `artifacts/m5a_exact_label_fixture.json` covers every frozen
public decision node under the four-iteration exact average policies: 4 Kuhn
and 96 Leduc nodes. Its public-family split is 80 train, 15 validation, and 5
test examples, with the complete identity-list digest frozen in
`configs/m5a_pbs_label_contract.json`. Every example keeps three different
namespaces:

- posterior-normalized on-policy private values for both fixed players;
- posterior-normalized forced-action conditional Q for the actor;
- standard unnormalized CFR action values, which omit own reach.

Label weighting uses blocker-aware projected marginals, never reach factors.
The validator retrains the frozen exact LCFR solvers, compares embedded
checkpoint/current/average-profile payloads and hashes, replays every public
action to reconstruct factors, conditions Leduc public chance, then recomputes
all three label namespaces from complete terminal trees. A second test oracle
does not call the production PBS label helpers and brute-forces all 100
examples independently. Every example must also exhibit a nonzero Q/CFV
separation.

The artifact binds its config, solver and both policy variants, exact critical
source closure, full mathematical PBS digest, separate trace provenance, and
exact recomputation certificate. Strict JSON, duplicate-key/nonfinite
rejection, deterministic rebuild, and before/after source verification are
part of the gate.

M5a does **not** generate HUNL value labels, train a value or policy network,
implement CFR/CFR-D/CFR-AVG depth-limited search, run online resolving, or
claim a ReBeL root value. All corresponding config and artifact flags are
false.

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
python -m bots.research_native_lab.rebel_decisionholdem.tools.m5a_manifest
```

The default fixed workspace is under the package's ignored `checkpoints/`
directory. If its run journal already exists, start with `--resume <journal>`
or choose a different output/workspace; the CLI never silently overwrites a
prior run. Large intermediate files must not be committed.

## Reproduce A1 M5a

From the repository root:

```bash
# Rebuild the complete 100-node exact fixture.
python -m bots.research_native_lab.rebel_decisionholdem.tools.build_m5a_label_fixture

# Revalidate config, source closure, solver/profile payloads, all labels and
# the frozen complete-tree identity/split contract without writing.
python -m bots.research_native_lab.rebel_decisionholdem.tools.build_m5a_label_fixture \
  --verify

python -m pytest \
  bots/research_native_lab/rebel_decisionholdem/tests/test_m5a_hunl_pbs.py \
  bots/research_native_lab/rebel_decisionholdem/tests/test_m5a_label_contract.py -q

# Verify the additive M5a manifest and its unchanged historical M4 anchors.
python -m bots.research_native_lab.rebel_decisionholdem.tools.m5a_manifest
```

Building to a second path with `--output` must produce byte-identical JSON.
The fixture is a small exact correctness artifact, not a training dataset or
authorization for a large job.

## Honest stage boundary

M4 proves a runnable blueprint-only HUNL path and its correctness/resume/resource
contracts. M5a separately proves a public-belief and exact-label contract. They
do not prove equilibrium quality, exploitability in HUNL,
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
