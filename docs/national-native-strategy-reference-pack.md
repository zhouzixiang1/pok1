# National TCP policy reference pack

This is the human-readable companion to
`web/core/strategy_reference_pack.py`.  The Python registry is authoritative
for generation contracts; this document explains why the cards are intentionally
small.

## Purpose

The system already preserves protocol correctness, a bounded opponent posterior,
and a native 60-second deadline.  A weak planning model should not be asked to
rediscover a full poker solver from a long generic prompt.  It should instead
choose a source-controlled, falsifiable card and prove that its code consumes
live `decision_context` state and changes a validated typed intent.

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

### `range_weighted_candidate_batch_v1`

Use this only for a deadline-aware candidate batch.  A legal baseline must be
available before refinement.  Precomputed pure facts may avoid rebuilding
hole-card/board buckets, but all further work has a finite sample cap and uses
the bounded terminal/showdown posterior.  Fixed-seed budget controls must show
additional trusted work and at least one changed final action.

### `equity_ev_anytime_v1`

Use this when the structural change is the computation path itself. The fast
path reads the system-owned 169-class prior or exact made-hand rank. The
refinement path consumes the absolute monotonic deadline, draws from a stable
seed in finite batches, evaluates both seven-card hands, and converts the
weighted estimate into call/fold/raise/jam EV. The falsifier checks known hands,
random parity with `sever/engine/evaluator.py`, expired-deadline behavior,
short/long trusted sample counts, and a same-shape/different-value table control
at the final typed/wire boundary.

### `polarized_spr_geometry_v1`

Use this for a structural action-abstraction change. It derives a finite set of
stage-total `raise_to` candidates from live pot, SPR, effective stack and the
reachable donk/delayed-probe line, then lets equity/EV select among those
candidates. Its call chain must reach `_raise_intent` and the runtime socket
validator. A coherent SPR control must change the candidate geometry or final
raise-to; moving one sizing threshold is not the innovation.

### `robust_exploit_mixture_v1`

Use this when the primary mechanism is opponent adaptation. Terminal response
rates—including `fold_to_raise`, `fold_to_jam`, and river overcall—affect
fold/call EV through confidence-scaled weights. Showdown buckets
may affect sampled range mass only when the reducer supplies both
`selection_scope=reached_showdown_only` and
`selection_bias_guard=reach_rate_discount_and_capped_influence`; the already
reach-discounted adaptation weight is capped again in policy. An otherwise
identical unguarded payload must have no showdown-range effect.

## Space-for-time boundary

The native template now owns `HOLE_COMBO_FACTS` (1,326), a canonical 169-class
preflop prior, `STRAIGHT_HIGH_BY_MASK` (8,192),
`FIVE_OF_SEVEN_INDICES` (21), exact five-/seven-card evaluators, a 52-card deck
helper, and deterministic draw-without-replacement. These objects are created
once when the persistent policy worker imports `precompute.py`; policy decisions
perform no file I/O and do not rebuild them.

The 169 values are a compact ordering/calibration prior, not an exact solution
or a claim of heads-up strength. Postflop comparisons use the complete evaluator;
uncertain future cards use bounded deterministic samples. Foundation facts do
not become an innovation until a same-shape/value or coherent-state control
changes a live legal intent at the socket validator.

Never build ranges, deck combinations, files, or caches inside a policy
decision callback.
Do not raise the bound merely to call a table an innovation; first prove the
table's value changes a final validated typed intent under a live control pair.
The trusted probe must also see more than one lookup key across its coherent
states: `TABLE.get(0)` plus unrelated state reads is not a live table use.

## Local-strength runtime parity

Every active bot uses the same versioned system TCP/deadline runtime contract.
Local strength, precommit, Arena diagnostics, and formal submission resolve a
content-bound bot specification and execute that bot's `policy.py`; none loads
a retired wrapper or synthesizes a compatibility overlay. Runtime and packaged
asset digests are recorded in the evaluator identity so comparisons fail closed
when their execution contracts differ.

## Deferred large equity/blueprint assets

Retired experiments contain candidate equity matrices, but files below
`archive/` are not active dependencies or planning evidence. No policy may read
them directly.

The source-controlled evaluator and compact 169-class prior are admitted parts
of the exact five-file submission ABI. Admission of any larger exact equity or
blueprint table still requires a system-owned asset registry,
license and source provenance, source and decoded SHA-256 values, a deterministic
read-only packed or `mmap` loader, card/key schema, byte and startup limits,
empty/corrupt fallback, official bundle compatibility, and a measured live
consumer. A Worker may select or consume an admitted system asset but may not
generate an opaque replacement or open arbitrary files. Until that contract is
complete, policies use only the source-controlled compact evaluator/tables
inside `precompute.py`.

## Deadline and evidence boundary

`iter_decisions(context, baseline, deadline)` receives an absolute
`time.monotonic()` timestamp. It must stop on that value, not treat it as a
relative number of seconds. The official runtime can expose roughly 54 seconds
of refinement while the local strength runtime uses a much shorter envelope;
the same finite-batch iterator serves both. The system worker independently
records iterator steps, CPU time and elapsed time. Candidate `sample_count` and
`confidence` metadata are diagnostics, never promotion authority.

The baseline is measured separately and must remain available under 250 ms.
Every refinement is optional: timeout, exception or worker termination leaves
the socket owner with its latest previously legalized typed intent. Local
native precommit and signed official EXE compliance still decide admission;
this architecture makes no standalone claim that a particular policy is the
strongest bot.
