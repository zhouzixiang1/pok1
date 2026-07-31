# Multi-ahead producer-consumer architecture (2026-07-31)

This document describes the refactored Slice-2b producer-consumer pipeline:
a **single persisted candidate-lifecycle FSM** as the source of truth for
every in-flight candidate, generalized from one-ahead to **N-ahead** (multiple
draft generations prepared in parallel behind the in-flight consumer).

It supersedes the one-ahead-only description in
`docs/slice2b-one-ahead-fsm-2026-07-31.md` (kept for the defect-E history).

## Design goal

Refactor the two scattered state machines (the generation stage machine +
the slice2b coordinator's implicit in-memory state) into a clean,
single-direction information flow, and lift the hardcoded "exactly one draft"
limit to a configurable `max_ahead` so the producer can prepare multiple
generations concurrently while the consumer validates earlier ones.

## The single source of truth: `CandidateLifecycle` FSM

`web/core/producer_consumer_slice2b.py::CandidateLifecycle` is a persisted
state machine (sqlite `slice2b_lifecycle.sqlite3`) keyed by `candidate_id`.
Each in-flight candidate is one row:

```
[none] --start()--> [SEALED] --begin_consuming()--> [CONSUMING]
                                   |--promote()--> [PROMOTED]  (terminal)
                                   `--reject()----> [REJECTED] (terminal)
```

- **SEALED**: the producer sealed the candidate at `workers_done`; the consumer
  has not yet leased it.
- **CONSUMING**: the consumer leased the envelope and is running the gate chain
  (quality→review→critic→precommit). Gate records stay in this state.
- **PROMOTED** / **REJECTED**: terminal. `commit_bot` may publish (promoted) or
  the generation must abandon (rejected).

All mutations go through a single atomic `_transition` enforcing a transition
whitelist under `BEGIN IMMEDIATE`. The row also carries the sealed snapshot,
gate results, promotion receipt, and the multi-ahead columns
`reserved_next_v` / `slot_id` / `sealed_at_epoch`.

## Information flow (single-direction)

```
generation stage machine (pipeline_state.py, slice2b-agnostic, UNCHANGED)
   │  seal at workers_done (orchestrator_deterministic_route handoff)
   ▼
CandidateLifecycle FSM  ◄── single source of truth (persisted)
   │  - producer: start() / reserve_draft_version()
   │  - consumer: begin_consuming() / record_gate() / promote() / reject()
   │  - promotion barrier: next_promotable_draft() / wait_for_promotion_readiness()
   ▼
AheadCoordinator (producer_consumer_slice2b.py) ── derives, holds NO state
   producer_may_draft_behind() / in_flight() / next_promotable_draft()
```

The coordinator holds **no candidate state** — every predicate reads the FSM.
This fixed a real recovery gap: the former in-memory `_in_flight` dict was
empty after a restart (recover_at_boot never replayed note_sealed), so
`producer_may_prepare_next()` returned False and the barrier raised
`unknown_candidate`. Now both read the FSM directly and are correct
immediately after a restart.

## Multi-ahead: how N drafts coexist

Four pieces make multi-ahead possible (each was a hardcoded-to-1 gap before
this refactor):

### 1. Version reservation registry (`draft_version_reservation` table)

`reserve_draft_version(slot_id, floor_next_v)` atomically assigns
`max(floor, highest unreleased reservation) + 1` under `BEGIN IMMEDIATE`, so
each draft slot gets a **distinct** `next_v`. Without this, N drafts would all
derive the same `floor+1` and collide on the same bot directory /
`workflow_run_id`. `prepare_generation` (when slice2b active) consults the
registry instead of deriving the draft next_v from the primary checkpoint
alone. `release_draft_version(slot_id)` frees the reservation on
promotion/abandon.

### 2. Slot set abstraction (`evolution_infra.py`)

`is_draft_slot(slot_id)` (prefix match: `draft`, `draft1`, `draft2`, ...)
replaces the literal `== "draft"` compares at the CAS floor+1 escape
(`evolution_infra_checkpoint_cas.py`) and candidate-tree isolation
(`get_bot_dir`). Numbered slots isolate their candidate under
`draft_candidates/<slot_id>/<name>` (legacy `draft` stays at
`draft_candidates/<name>`). `draft_slot_id(n)` returns the n-th slot id.

### 3. Version-ordered promotion (`AheadCoordinator.next_promotable_draft`)

`next_promotable_draft(published_v)` returns ONLY the draft whose reserved
version is exactly `published_v + 1`. This enforces **ordered promotion**:
with N drafts in flight, only the lowest-reserved draft may promote, so draft
N+2 cannot leapfrog draft N+1 onto the N+1 slot. Promotion releases the
reservation, advancing the queue.

### 4. Multi-slot launch (`_try_launch_draft_prepare`)

Loops over draft slots until `producer_may_draft_behind()` is False or no free
slot remains. Single-ahead (`max_ahead==1`) launches the legacy `draft` slot
(unchanged behavior). Multi-ahead launches `draft1`, `draft2`, ... up to
`max_ahead`, each as its own fire-and-forget asyncio task with its own slot
ContextVar binding. `_run_draft_cycle` is parameterized by `slot_id`.

## Configuring max_ahead

`AheadCoordinator(lifecycle, max_ahead=N)`. Default 1 (single-ahead, no
behavior change). The activation layer (`Slice2bActivation`) constructs the
coordinator; raising `max_ahead` there enables N concurrent drafts. There is
no env var yet — it is a construction-time parameter.

## What did NOT change

- **Generation stage machine** (`pipeline_state.py`): zero slice2b symbols,
  fully slice2b-agnostic. The per-generation flow (selected→...→archived) is
  untouched.
- **`commit_bot` publication authority**: the promotion barrier proves the
  consumer promoted (via the FSM), then the unchanged `commit_bot` publishes.
- **Canonical gate chain**: the consumer runs the unchanged
  `run_quality_gates`/`run_review`/`run_critic`/`run_precommit_eval` handlers.
- **CAS / publication integrity**: draft slots skip the floor+1 CAS via the
  `is_draft_slot` escape; non-draft allocations are unchanged.

## Regressions

- `test_coordinator_derives_from_lifecycle_no_in_flight` (recovery-gap fix)
- `test_next_promotable_draft_is_version_ordered`
- `test_multi_ahead_two_drafts_distinct_versions_ordered_promotion`
- `test_multi_ahead_max_ahead_one_preserves_single_ahead_behavior`
- `test_reserve_draft_version_assigns_distinct_versions` (+ 5 reservation tests)
- `test_is_draft_slot_prefix_matches_legacy_and_numbered` (+ 3 slot tests)
- Existing slice2b tests updated to the FSM-derived semantics (110 pass).

## Known follow-ups (not done in this refactor)

- **LLM semaphore partitioning** (step F, deferred): the global
  `Semaphore(POK_GLOBAL_LLM_CONCURRENCY)` is shared by producer + consumer;
  with N drafts the contention scales with N. Partitioning into
  consumer/producer sub-pools would prevent the consumer (critical path) from
  starving behind draft Master calls. Performance item, not correctness.
- **Naming cleanup** (step G, deferred): `producer_may_advance` /
  `producer_may_prepare_next` are kept as backwards-compat aliases of
  `producer_may_draft_behind`; `OneAheadCoordinator` aliases `AheadCoordinator`.
- **Live multi-ahead validation**: max_ahead defaults to 1; raising it to 2+
  needs a live run to validate the parallel drafts + ordered promotion
  end-to-end under real LLM contention.

## Related

- `[[slice2b-never-worked-2026-07-30]]` — defect E history (now fixed + re-enabled).
- `docs/slice2b-one-ahead-fsm-2026-07-31.md` — the earlier one-ahead design.
- `AGENTS.md` §Slice 2b.
