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

## Design sources

- [DeepStack](https://arxiv.org/abs/1701.01724) motivates continual, public-state
  reasoning rather than one global static policy.
- [Depth-Limited Solving for Imperfect-Information Games](https://arxiv.org/abs/1805.08195)
  motivates bounded, robust computation rather than an unbounded per-action
  search.
- [OpenSpiel's algorithm reference](https://openspiel.readthedocs.io/en/latest/algorithms.html)
  is a practical reference for CFR-style finite action abstractions and bounded
  sampling, not a dependency of production bots.

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

An operator-side research checkout contains an exact 1,326 × 1,326 heads-up
preflop equity matrix.  This release worktree does **not** vendor, load, or
authorize that file, so it is intentionally **not an implicit dependency** of
generated bots.  If later admitted, its useful form is a compressed,
hash-pinned row store: it fits inside the current 8 MiB in-memory contract
after loading, and a weighted 1,225-combo query is substantially faster and
less noisy than rebuilding a Monte Carlo estimate.

Before it is provisioned, it needs a system-owned asset registry, license
notice, source/decoded SHA-256 values, deterministic loader, empty/corrupt
fallback, and exhaustive evaluator checks.  The asset must be copied into each
bot artifact by a deterministic prepare hook, not read from an experiment
directory or generated by a worker.  Until that separate admission is merged,
workers must use only the source-controlled foundation facts already in their
artifact.  This keeps a large table as an auditable space-for-time primitive
that can be range-weighted by `opponent_runtime`, rather than opaque
LLM-written policy data.
