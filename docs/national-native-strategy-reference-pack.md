# National-native strategy reference pack

This is the human-readable companion to
`web/core/strategy_reference_pack.py`.  The Python registry is authoritative
for generation contracts; this document explains why the cards are intentionally
small.

## Purpose

The system already preserves protocol correctness, a bounded opponent posterior,
and a native 60-second deadline.  A weak planning model should not be asked to
rediscover a full poker solver from a long generic prompt.  It should instead
choose a source-controlled, falsifiable card and prove that its code consumes
live state and changes a sanitized wire action.

The cards are not an equilibrium policy and do not certify strength.  Local
native TCP precommit remains the strength gate; the official Windows EXE remains
the protocol/compliance oracle only.

Master chooses the typed runtime contract, reference card, implementation
scope, behavior hypothesis, and controls.  After that choice, the plan compiler
copies the card/runtime contract's exact literal execution anchors into the
worker prompt before schema validation.  This is an idempotent serialization
step, not semantic auto-repair: an unknown work primitive, mismatched card,
invalid owner, missing contract section, or oversized prompt is still rejected.
It prevents a weaker planner from losing a word such as `control` while keeping
all strategic and safety judgments behind the existing hard gates.

## Design sources

- [FunSearch](https://www.nature.com/articles/s41586-023-06924-6) starts from
  an implementation/skeleton, samples a program database, and closes the loop
  with an objective evaluator; its result supports parent-derived mutation, not
  single-lineage hill climbing.
- [AlphaEvolve](https://arxiv.org/abs/2506.13131) extends that pattern to
  codebases with a program database, prompt sampling, automated evaluators, and
  an evolutionary population.  For this repository the transferable boundary
  is “LLM proposes; deterministic/native evaluators select,” not dependence on
  a particular frontier model.
- [DeepStack](https://arxiv.org/abs/1701.01724) motivates continual, public-state
  reasoning rather than one global static policy.
- [Depth-Limited Solving for Imperfect-Information Games](https://arxiv.org/abs/1805.08195)
  motivates bounded, robust computation rather than an unbounded per-action
  search.
- [OpenSpiel's algorithm reference](https://openspiel.readthedocs.io/en/latest/algorithms.html)
  is a practical reference for CFR-style finite action abstractions and bounded
  sampling, not a dependency of production bots.

## Evolution topology

Deriving a candidate from a proven bot is reasonable: it preserves a legal
baseline, gives paired parent comparison, and makes attribution possible.  It
becomes unreasonable when “derive” means one incumbent, one prompt, and another
threshold patch forever.  Parentage is therefore a safety/baseline mechanism,
while novelty must come from population/source selection, crossover when
structurally justified, explicit unresolved architecture focuses, typed
strategy cards, and measured action-level counterfactuals.  A candidate that
only renames a helper or adjusts an old margin has not earned novelty merely by
being a new version.

The LLM is used as a mutation/planning operator.  It does not own source
identity, evidence transport, executable contract serialization, worker task
authority, legality, or promotion.  Those are deterministic because a weaker
model is most useful when spending tokens on alternative mechanisms and code,
not on copying hashes, enum literals, or checkpoint state without error.

## Current cards

### `lead_sizing_geometry_v1`

Use this only for an offensive lead-sizing innovation.  It must read
`hand_runtime`'s street, SPR, pot, effective stack, position, preflop aggressor,
and street-open state.  It also needs confidence-scaled terminal or showdown
evidence from `opponent_runtime`.  A compact precomputed grid may accelerate
pure card/texture buckets, but the grid must not be a fixed sizing policy: an
SPR or posterior control must change a legal final raise-to action.

### `range_weighted_candidate_batch_v1`

Use this only for a deadline-aware candidate batch.  A legal baseline must be
available before refinement.  Precomputed pure facts may avoid rebuilding
hole-card/board buckets, but all further work has a finite sample cap and uses
the bounded terminal/showdown posterior.  Fixed-seed budget controls must show
additional trusted work and at least one changed final action.

## Space-for-time boundary

The native template's `HOLE_COMBO_FACTS` (1,326), `STRAIGHT_HIGH_BY_MASK`
(8,192), and `FIVE_OF_SEVEN_INDICES` (21) are foundation facts.  They make
repeated calculation cheaper but do not constitute a strategy innovation until
they influence a live, legal decision.  The current contract intentionally caps
an auditable mapping at 65,536 entries and 8 MiB.  That is enough for a compact
offline-generated canonical grid (for example hand class × street × texture ×
range bucket) without letting an LLM hand-write a giant opaque table.

Never build ranges, deck combinations, files, or caches during `get_action`.
Do not raise the bound merely to call a table an innovation; first prove the
table's value changes a final sanitized wire action under a live control pair.
The trusted probe must also see more than one lookup key across its coherent
states: `TABLE.get(0)` plus unrelated state reads is not a live table use.

## Local-strength runtime parity

Strategy comparisons must not accidentally compare a new wrapper against an
old wrapper.  Native local strength and precommit use a bilateral temporary
overlay of the current system TCP/deadline wrapper, while retaining each bot's
policy modules.  The overlay is recorded in every replay and in the evaluator
identity.  It is never used for the raw candidate smoke or the official Windows
EXE: those paths execute the submitted, content-bound artifact itself.

The overlay only provisions the standard pure-fact `precompute.py` when a
historical bot has none.  If a bot owns a real strategy precompute artifact, it
is retained and its digest/provenance is reported.  A future full split should
move immutable system facts into `native_system_facts.py` and reserve
`precompute.py` for strategy-owned tables; overwriting a strategy table during
evaluation would hide the very capability being tested.

## Candidate exact preflop-equity asset

The isolated neural-lab checkout at
`bots/neural_national_lab/external/poker-cfr` contains an exact 1,326 × 1,326
heads-up preflop equity matrix from `b-inary/poker-cfr`.  The source is
BSD-2-Clause; the tracked asset was introduced by upstream commit
`f3c04b645ed9ff70e558a9bbed0e0de40eeb112a`, is 7,033,112 bytes, and has SHA-256
`006404b36d257fc9455da0d0f0ab89aef3e80ece56c8f3e770bad926cfe5ec8a`.
It is a bincode `Vec<u32>`: an eight-byte length followed by 1,758,276 counters.
This release worktree does **not** vendor, load, or authorize that nested-repo
file for formal bots, so it is intentionally **not an implicit dependency** of
generated candidates.

The raw file fits under 8 MiB, but decoding 1.76 million values into ordinary
Python integers does not.  Admission therefore needs a hash-pinned, read-only
`mmap`/packed-row loader (or a smaller canonical row store), plus a measured
weighted 1,225-combo query path.  That would be substantially faster and less
noisy than rebuilding a Monte Carlo estimate without pretending the table is a
complete strategy.

Before it is provisioned, it needs a system-owned asset registry, license
notice, source/decoded SHA-256 values, deterministic loader, empty/corrupt
fallback, and exhaustive evaluator checks.  The asset must be copied into each
bot artifact by a deterministic prepare hook, not read from an experiment
directory or generated by a worker.  Until that separate admission is merged,
workers must use only the source-controlled foundation facts already in their
artifact.  This keeps a large table as an auditable space-for-time primitive
that can be range-weighted by `opponent_runtime`, rather than opaque
LLM-written policy data.
