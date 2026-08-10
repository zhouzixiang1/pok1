# Pipeline Deep-Parallelism Architecture (2026-08-10)

> **Goal:** keep the LLM provider (GLM-5.2) continuously saturated so tokens/hour
> rises from the measured ~242K baseline toward ~20M (the 8-permit × 85%
> utilization ceiling at ~819 tokens/stream-second).

## Problem (measured)

The LLM pool sat at **3.4% utilization** over a 380-hour window
(91.84M total tokens / 8 permits × 379.75h). The dominant idle window was the
**native precommit** phase: `run_precommit_eval` runs 70-hand TCP matches
(~30-50 min, **zero LLM permits**), and the one-ahead draft *parked* at
`workers_done` waiting for the primary's consumer to finish precommit — so
while one generation ran precommit, **nothing consumed LLM**.

## Design: three decoupled throttles

The refactoring decouples LLM work from native work so neither blocks the other.

| Dimension | Limiter | Behaviour |
|-----------|---------|-----------|
| **LLM stages** (master/worker/review/critic) | existing global `asyncio.Semaphore` (`POK_GLOBAL_LLM_CONCURRENCY=8`) | FIFO queue, natural backpressure |
| **draft creation + sealing** | new `llm_semaphore_has_capacity()` predicate | a draft is launched/sealed only when an LLM permit is likely free; `max_ahead` is a generous backstop (32), not the real cap |
| **native precommit** (matches) | new independent `asyncio.Semaphore` (`POK_NATIVE_PRECOMMIT_CONCURRENCY=1`) | when exhausted, the candidate **pauses locally** (stays at `critic_checked`); it does NOT hold an LLM permit and does NOT block other drafts' LLM work |

**Key invariant:** when native is at its limit, other drafts' LLM stages
(review/critic) keep running — this is what fills the LLM-idle window.

## Change 1 — LLM-capacity-driven draft (`llm_concurrency.py` + `producer_consumer_slice2b.py`)

`llm_semaphore_has_capacity(n=1)` reads `semaphore._value` (the same instantaneous
read the dashboard uses). It is advisory, not a reservation: a draft launched on
an optimistic read simply queues its first LLM call on the FIFO semaphore — which
is the *desired* behaviour (work staged, not dropped).

`AheadCoordinator.producer_may_advance` / `producer_may_draft_ahead_of_eval` now
gate on this predicate first, then `max_ahead` (backstop). Default `max_ahead`
raised from 1 → 32 (the count no longer throttles; LLM capacity does).

## Change 2 — draft drives the full gate chain (`orchestrator_loop_phases.py`)

`_run_draft_cycle` no longer early-returns at `workers_done`. Instead the
deterministic route hits the existing `_slice2b_seal_at_workers_done` seam, which:

1. seals the draft candidate under its **reserved_next_v** (`reserve_draft_version`,
   SQLite `BEGIN IMMEDIATE` atomic allocation);
2. launches a consumer task that runs the full chain
   `quality → review → critic → precommit` in the isolated
   `consumer-candidate-v<reserved>` slot.

The draft cycle returns on the `slice2b_consumer_parked` terminal action
(**preserving** the draft slot, unlike abandon/handoff terminal actions which
clear it).

## Change 3 — native precommit concurrency (`producer_consumer_slice2b.py` + `_activation.py`)

A process-wide `asyncio.Semaphore(POK_NATIVE_PRECOMMIT_CONCURRENCY)` wraps only the
`run_precommit_eval` gate in `ConsumerDispatcher.run_once`. When `sem._value <= 0`,
the dispatcher returns `native_precommit_slot_busy` (dispatched=False). The
consumer gate-loop (`_run_gate_chain`) treats this as *expected backpressure*
(not infra failure): it backs off `POK_NATIVE_BACKOFF_SECONDS` (default 30s,
sliced into 5s cooperative sleeps) and re-dispatches. It does **not** consume the
infra-retry budget.

## Change 4 — event-driven producer launch (`orchestrator_loop_phases.py`)

`_parked_sleep(45s)` is replaced by `_parked_wait`, which awaits an
`asyncio.Event` (`coordinator._slot_freed_event`) fired in `note_terminal`. A
candidate terminating wakes the producer in ~0s instead of up to 45s. A 45s
timeout fallback covers a missed event.

## Change 5 — promotion barrier reuses the tested path

`_promote_draft_to_primary` writes the draft to the primary at `workers_done`
(unchanged) and **preserves the draft's consumer slot**. Because the draft's
consumer `candidate_id` (`candidate-v<reserved_next_v>`) matches the next
primary's `candidate_id`, the primary's seal seam recognizes the candidate as
already-sealed (ALREADY-SEALED GUARD) and the promotion barrier collapses the
verified consumer evidence onto the primary — `commit_bot` publishes without
re-running any gate. No new collapse machinery was needed.

## Utilization math (target validation)

- Measured: ~819 tokens/stream-second (307M all-tokens / 374,978 active-sec).
- At 8 permits × 85% utilization: `0.85 × 8 × 819 × 3600 ≈ 20.0M tokens/hour` ✓

The refactoring targets the 3.4% → ~85% utilization gap by eliminating the
zero-LLM precommit window (drafts fill it with their review/critic) and the
launch latency (event-driven vs 45s poll).

## Configuration (`deploy/tencent-cloud/env.runtime`)

```
POK_GLOBAL_LLM_CONCURRENCY=8              # LLM permits (unchanged)
POK_SLICE2B_MAX_AHEAD=32                  # backstop only; real cap is LLM capacity
POK_NATIVE_PRECOMMIT_CONCURRENCY=1        # native precommit chains in parallel (OOM safety)
POK_NATIVE_BACKOFF_SECONDS=30             # native-slot-busy retry interval
POK_SLICE2B_CONSUMER_INFRA_RETRY_BUDGET=2 # infra-retry budget (unchanged)
```

## Observability

`/api/llm/metrics/live` reports `active_streams`, `capacity`,
`utilization_pct`. The success metric is `utilization_pct` rising from ~3.4%
toward ≥70%, and trailing tokens/hour (derivable from the JSONL `epoch_ts` +
`total_tokens` windowed over 3600s) rising from ~242K toward ~20M.
