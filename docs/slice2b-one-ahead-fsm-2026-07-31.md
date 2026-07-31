# Slice-2b one-ahead: persisted candidate lifecycle FSM (2026-07-31)

This document records the re-enablement of Slice-2b one-ahead after fixing
**defect E** (the seal never completed end-to-end) and replacing the in-memory
validation ledger with a **persisted candidate lifecycle state machine** so
one-ahead survives a process restart.

It closes out `docs/abandon-death-loop-and-workflow-id-reuse-2026-07-30.md`
§Defect E and supersedes the "needs option-d fix before re-enabling" note in
the disabled-state env comment.

## Background: why slice2b was disabled

Slice-2b (`POK_SLICE2B_ENABLED`) was disabled on 2026-07-30 (commit `7682ce95`)
because it had **never completed an end-to-end seal** — an audit of the live DB
(`web/core/results/workflow/events.sqlite3`) found **0 producer-consumer
effects across all 67 workflow instances**. Every seal crashed the orchestrator.

The seal (at `workers_done`) deliberately reuses the worker-journal `run_id`
(`orchestrator_deterministic_route.py:225`, `snapshot["workflow_run_id"]`) and
submits a `producer-consumer-job:quality-static` effect against it through
`WorkflowStore.request_effect`. Two independent crashes sat on that reuse path:

### Defect E (a) — definition_version mismatch — FIXED earlier (commit `aa8e86ec`)

`submit()` called `ensure_instance(run_id, definition_version=1)` (the adapter
default) while the reused worker-journal row was created at
`WORKER_WORKFLOW_DEFINITION_VERSION = 3` →
`WorkflowConflict("definition version mismatch ... stored=3 requested=1")`.

Fix: `producer_consumer_workflow_store.py:188-197` now inherits the persisted
instance's `definition_version`. Regression:
`web/tests/test_producer_consumer_workflow_store.py::test_submit_inherits_existing_worker_journal_definition_version`.

### Defect E (b) — `completed` vs `running` status mismatch — FIXED 2026-07-31 (this change)

Even after (a), the seal still crashed because the reused worker-journal
instance is `status="completed"` by the time the seal runs:
`worker_workflow.projected("workers_done")` (`tool_planning_worker_durable.py`)
calls `append_event_and_set_status(..., status="completed")`. But
`request_effect`'s status gate (`workflow_kernel.py:913`, pre-fix) required
`status=="running"` for **every** effect kind:

```python
if instance is None or str(instance["status"]) != "running":
    raise WorkflowConflict(f"workflow instance is not running: {run_id}")
```

→ `WorkflowConflict` → mapped to `producer_consumer_submit_conflict` →
orchestrator crash. This was crash #2; the (a) fix only cleared the first.

**Why no test caught it:** every slice2b unit test constructs a fresh empty
`WorkflowStore`, so `ensure_instance` INSERTs a brand-new `running` instance
and the status gate passes trivially. No test pre-seeded a `completed` worker
journal — the exact production state at `workers_done`.

## Fix 1 — status gate narrowed by effect kind

`workflow_kernel.py::request_effect` (around `:909-917`) now admits a
`producer-consumer-job:*` effect on a `completed` instance, while every other
kind still requires `running`:

```python
status = str(instance["status"]) if instance is not None else None
running_ok = status == "running"
seal_on_completed = (
    status == "completed"
    and isinstance(kind, str)
    and kind.startswith(PRODUCER_CONSUMER_EFFECT_KIND_PREFIX)
)
if instance is None or not (running_ok or seal_on_completed):
    raise WorkflowConflict(f"workflow instance is not running: {run_id}")
```

`PRODUCER_CONSUMER_EFFECT_KIND_PREFIX = "producer-consumer-job:"` is defined in
`workflow_kernel.py` (mirroring `producer_consumer_workflow_store.EFFECT_KIND_PREFIX`;
duplicated on purpose — the kernel owns no poker/LLM-domain imports, enforced by
`test_production_entrypoints_do_not_import_inert_slice_modules`).

**Worker-effect safety is unchanged**: worker effects use kinds `worker_llm` /
`system_blueprint` (`worker_workflow.py:1327-1343`), which never match the
prefix and still require `running`. The idempotent replay CAS
(`workflow_kernel.py:918-933`) now also reaches seal effects on a completed
instance (previously the gate defeated replay).

Regressions (`web/tests/test_workflow_kernel.py`):
- `test_request_effect_accepts_producer_consumer_kind_on_completed_instance`
- `test_request_effect_rejects_worker_kind_on_completed_instance`
- `test_request_effect_rejects_unknown_kind_on_completed_instance`
- `test_request_effect_still_requires_running_for_worker_kind_on_running`
- `test_request_effect_rejects_any_kind_on_missing_instance`

## Fix 2 — persisted candidate lifecycle FSM

The former in-memory `ValidationLedger` (`producer_consumer_slice2b.py`,
`self._entries: dict`) lost every in-flight candidate on a process crash. On
restart `_slice2b_consumer_in_flight` read an empty `_sealed_snapshots` → the
inline `run_quality_gates` re-ran from the persisted `workers_done` checkpoint.
That was safe/idempotent, but **one-ahead degenerated to serial on every
restart**, losing the parallelism that is the entire point of slice2b.

### The state machine

```
[none] --start()--> [SEALED] --record_gate()*--> [SEALED]
                                |--promote()--> [PROMOTED] (terminal; commit_bot may publish)
                                `--reject()---> [REJECTED] (terminal; generation must abandon)
```

`SEALED` spans both "envelope submitted, consumer not yet leased" and
"consumer is running the gate chain" — the durable envelope outbox + fenced
lease already own lease discipline, so the lifecycle only distinguishes
sealed-and-unresolved from terminal.

### Persistence

`class CandidateLifecycle` (`producer_consumer_slice2b.py`) replaces
`ValidationLedger`. It owns its own sqlite db
(`RESULTS_DIR/workflow/slice2b_lifecycle.sqlite3`, keyed by `candidate_id`,
deliberately NOT piggybacked on the worker-journal event stream — different
key, simpler invariants). Schema:

```sql
CREATE TABLE slice2b_candidate_lifecycle(
    candidate_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,              -- sealed | promoted | rejected
    sealed_artifact_hash TEXT,
    envelope_effect_id TEXT,
    envelope_digest TEXT,
    gate_results_json TEXT,          -- gate_name -> {outcome, digest, ...}
    promotion_receipt_json TEXT,
    terminal_reason TEXT,
    completed_at REAL,
    sealed_snapshot_json TEXT,       -- the immutable snapshot (boot recovery)
    updated_at REAL
);
```

All mutations go through a single atomic `_transition(candidate_id, *,
to_state, mutator)` that enforces the transition whitelist
(`_ALLOWED_STATE_TRANSITIONS`) under `BEGIN IMMEDIATE`. Gate-record mutations
use `_transition_with_self_state` (stays `SEALED`). Illegal transitions raise
`Slice2bError("candidate_lifecycle_illegal_transition:...")`.

The public API mirrors the former `ValidationLedger` exactly (`start`,
`record_gate`, `promote`, `reject`, `snapshot`, `is_terminal`, `is_promoted`),
so `ConsumerDispatcher` and `OneAheadCoordinator` are unchanged. New methods:
`non_terminal_candidates()` and `recover_snapshot()` (boot recovery).
`ValidationLedger = CandidateLifecycle` alias keeps existing imports resolving.

### Boot recovery

`Slice2bActivation.recover_at_boot()` (called from
`_slice2b_ensure_activation`, `orchestrator_deterministic_route.py`, right
after the activation is constructed):

1. Scans `ledger.non_terminal_candidates()` (every `SEALED` row).
2. For each: recovers the sealed snapshot via `ledger.recover_snapshot()`,
   rebuilds `_sealed_snapshots` / `_dispatch_clocks`, and re-schedules the
   canonical gate-runner factory derived from the snapshot's `next_v`/`source_v`.
3. The next `ensure_consumer_running` (driven by the orchestrator loop or the
   promotion barrier) materializes the consumer `asyncio.Task`.

Safe to call multiple times; re-scheduling a running candidate is a no-op.

### Death-proof resolver

The consumer dispatcher's `adapter.recover(...)` now carries a death-proof
resolver (`Slice2bActivation.death_proof_resolver`), wired into
`ConsumerDispatcher.__init__`. After a restart, every previously-leased
consumer task is gone (its owner pid does not exist in the new process); the
resolver proves that by checking `self._consumer_tasks` — if no live task owns
the effect, the prior owner is dead and the lease may be reclaimed. Before
this, `recover()` raised `producer_consumer_recovery_death_proof_required`
for any `running` effect, so post-crash consumer resume was non-functional.

### `_slice2b_consumer_in_flight`

`orchestrator_deterministic_route.py::_slice2b_consumer_in_flight` now queries
the **persisted** `ledger.snapshot(candidate_id)` instead of the in-memory
`_sealed_snapshots`, so it stays accurate after a restart (a
sealed-but-unresolved candidate is still "in flight" even before
`recover_at_boot` rehydrates the in-memory registries).

## Regressions (new)

`web/tests/test_producer_consumer_slice2b.py`:
- `test_lifecycle_persists_across_store_reopen` — core durability invariant.
- `test_lifecycle_terminal_candidate_not_in_non_terminal`
- `test_lifecycle_reject_moves_to_rejected_terminal`
- `test_lifecycle_illegal_transition_promote_after_rejected_raises`
- `test_lifecycle_promote_without_start_raises`
- `test_lifecycle_start_is_idempotent_on_replay`
- `test_lifecycle_record_gate_persists_and_reloads`
- `test_validation_ledger_alias_is_candidate_lifecycle`
- `test_recover_at_boot_reschedules_non_terminal_candidates`
- `test_recover_at_boot_skips_terminal_candidates`
- `test_death_proof_resolver_marks_absent_owner_dead`

## Honest expectations

- **Parallelism**: one-ahead overlaps gen N+1 prepare (Master/Workers) with
  gen N gates (quality/review/critic) only while the process stays up. After a
  restart, the persisted FSM now recovers the parallel consumer instead of
  falling back to serial inline.
- **Concurrency bound**: `MAX_SEALED_AWAITING_VALIDATION = 1` — at most one
  sealed candidate is in flight. This is a design constraint, not a defect.
- **LLM contention**: slice2b genuinely doubles LLM contention (producer +
  consumer share one `asyncio.Semaphore(POK_GLOBAL_LLM_CONCURRENCY=2)`). This
  change does NOT partition the semaphore (deferred — "option C"). If GLM 429
  pressure rises after re-enablement, that is the next lever.

## Related

- `[[abandon-death-loop-fix-2026-07-30]]` — the broader workflow-id-reuse fix.
- `[[slice2b-never-worked-2026-07-30]]` — the disabled-state diagnosis this
  closes out.
- `docs/abandon-death-loop-and-workflow-id-reuse-2026-07-30.md` §Defect E.
