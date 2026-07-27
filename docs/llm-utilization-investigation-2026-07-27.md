# LLM Utilization Investigation — 2026-07-27

**Branch**: `tencent-cloud-runtime`
**Scope**: GLM-5.2 dispatch through `claude_agent_sdk`; token cost explicitly
out-of-scope per operator direction ("不计 token 成本"), so "utilization" here
means **dispatch throughput and generation latency**, not token efficiency.

## Current state (measured)

### Dispatch topology

- **Single chokepoint**: `web/core/llm_query.py::run_claude_query` (L2346).
  Every Master/Reviewer/Critic/Worker/audit/crossover/cycle_archivist call
  flows through it.
- **Global concurrency cap**: `POK_GLOBAL_LLM_CONCURRENCY=2` (env-overridable).
  A process-wide `asyncio.Semaphore` in `web/core/llm_concurrency.py` is
  acquired inside `run_claude_query` at L2813-2820, immediately before
  `_run_stream_with_signature_retry`. FIFO (deque-backed); no starvation,
  no priority.
- **Per-role timeouts** (production `deploy/tencent-cloud/env.runtime`):
  every role has STALL=1200s, IDLE=1800s, TOTAL=3600s. CYCLE_TIMEOUT=14400s
  (4h), WATCHDOG_TIMEOUT=28800s (8h).
- **Thinking budget**: `POK_LLM_THINKING_MODE=enabled`,
  `POK_LLM_THINKING_BUDGET=64000`, `POK_LLM_EFFORT=max`. GLM treats the budget
  as a soft target, so a large budget gives full reasoning depth.

### Per-generation dispatch count (happy path)

| Stage | Dispatches | Concurrency |
|---|---|---|
| direction_audit | 1 | serial (stage machine) |
| Master Scouts (proposals) | 3 | concurrent via `gather_llm_fail_fast` |
| Master critics (ballots) | 2 | concurrent via `gather_llm_fail_fast` |
| FINAL Master | 1 | serial |
| Workers | 1-3 | parallel only if disjoint target files |
| Review | 1 | serial |
| Critic review | 1 | serial |
| Reviewer verdict retries | up to 2× | serial |
| **Happy-path total** | **~9-12** | capped to 2-wide at the provider |

With retries/crossover: 15-25 dispatches.

### Role-attempt budgets

- Master Scouts: 3 (`_MASTER_PROPOSAL_DIRECTIONS`)
- Master critics: 2 + one schema-retry round each
- Workers: `MAX_WORKER_RETRIES=4`
- Crossover: `MAX_CROSSOVER_RETRIES=3`
- Reviewer verdict: `MAX_REVIEW_VERDICT_ATTEMPTS=2`
- Signature/SDK stream: `_SIGNATURE_MAX_ATTEMPTS=5` (entire loop holds one permit)
- Master ensemble parks a provider after `role_attempt >= 3`

## Bottlenecks identified

### 1. Global concurrency cap = 2 (primary lever)

The 3 Master Scouts and 2 Master critics are *dispatched* concurrently via
`gather_llm_fail_fast`, but only **2 streams actually run at once**; the
other 3 queue on the FIFO semaphore. Raising `POK_GLOBAL_LLM_CONCURRENCY`
would directly widen all ensembles.

**Risk**: GLM-5.2 may rate-limit at the account level. The
`api_concurrency` adaptive backoff already halves the cap per 429, so a
too-aggressive default would self-correct downward, but the steady-state
ceiling is set by the provider, not the client.

### 2. 5-attempt signature retry holds a permit for its whole duration

`_run_stream_with_signature_retry_attempts` (L2078) retries up to 5 times with
exponential backoff via `_signature_retry_sleep`, **all inside the held
semaphore permit**. A transient SDK stream error can burn 5× backoff against
one slot, blocking the FIFO queue behind it.

**Risk of moving retry outside the permit**: a freed permit could be acquired
by a different role, breaking the FIFO ordering guarantee and potentially
starving the retried role. The current design trades throughput for fairness.

### 3. `effort=max` applied uniformly to every role

`effort=max` is applied to *every* role including lightweight selectors (FINAL
Master is a zero-tool compiler). The env config does not differentiate effort
by role complexity, so a 30-second role can occupy a permit for 5-15 minutes.

**Risk of differentiation**: GLM's `effort` is a single dial; per-role
overrides would need a new env map and careful validation that lowering
effort on, e.g., the FINAL selector does not degrade proposal quality.

### 4. Stall gate is permissive, not aggressive

Production STALL=1200s means a genuinely stuck stream can occupy a permit for
20 minutes before being killed. The earlier "death-loop" diagnosis (commit
history) was a misattribution: streams were killed at 900s while GLM was still
productively reasoning at 27k+ thinking tokens. Current values avoid that
but leave permits idle during legitimate slow GLM streams.

### 5. Workers fall back to sequential on overlapping targets

`agent_workers.py:1526-1534`: Workers dispatch in parallel only when target
files are disjoint AND no task has empty `target_files`. Single-file rework
generations (the common case for quality/precommit repair) run sequentially.

## Recent inefficiency fixes (death-loop class)

| Commit | Class | Root cause |
|---|---|---|
| `884e6465` | repair-routing gap | `_ARCHITECTURE_CHECK_FILES` missing 2 keys → spurious terminal abandon |
| `459cb2f2` | stale prompt text | FINAL Master had a second static copy of an impossible `change_symbol` constraint |
| `f218ed9d` | prompt/validator contradiction | `change_symbol` unconditionally forced to a call-graph root → 8 straight abandons |
| `0be001d4` | missing wiring | GLM 429 quota bodies never reached `rate_limiter.parse_429` |
| `449d994a` | observer CPU steal | `/api/bots` stalled the event loop, stealing time from LLM streams |

**Common pattern**: contract contradictions and missing wiring caused retry
storms that burned full Scout timeouts per retry before abandoning. These are
now fixed; the dominant *current* waste is the per-permit signature-retry
hold (bottleneck #2).

## Safe improvement candidates

Ordered by safety / impact. Each is reversible (env-only or one-flag) and
does not touch the dispatch contract.

### Tier A — env-only, reversible, low-risk

1. **Raise `POK_GLOBAL_LLM_CONCURRENCY` from 2 → 3 (or 4)**.
   - Trade-off: more in-flight streams; if GLM rate-limits, `api_concurrency`
     backoff halves the cap automatically. Start at 3, watch for 429 storms.
   - Reversal: `sudo systemctl restart pok-evolution` after editing
     `env.runtime`.
   - **Impact**: cuts ensemble wall-time by up to 33% (3 scouts / 3-wide =
     1 round vs 2 rounds).

2. **Per-role `effort` map** (deferred — needs schema work).
   - Idea: keep `effort=max` for Master Scouts/FINAL/Workers/Review; lower to
     `effort=high` for direction_audit, cycle_archivist, critic (advisory).
   - **Blocker**: requires a new env map in `llm_query.py::_llm_thinking_options`
     and validation that advisory roles still produce schema-valid output.

### Tier B — code changes, medium-risk, needs test coverage

3. **Move signature-retry backoff outside the semaphore permit**.
   - Idea: release the permit before each `_signature_retry_sleep`, re-acquire
     after. Preserves total attempt budget but stops backoff from blocking
     the queue.
   - **Risk**: breaks FIFO ordering; a retried role could be preempted by a
     later-arriving role. Mitigation: a separate "retry queue" with priority
     over fresh dispatches.
   - **Test impact**: `test_llm_query_*` retry tests need a FIFO assertion.

4. **Parallelize Worker CoT audits** (`agent_workers.py:1623`).
   - Currently sequential after a parallel worker batch. They are LLM-free
     unless evidence is weak, but the sequential pattern adds latency.
   - **Risk**: low (read-only audits); mainly a `gather` refactor.

### Tier C — architectural, deferred

5. **Tiered concurrency**: separate semaphores for Master ensemble vs Workers
   vs advisory roles, so a slow Scout cannot block a quick Critic.
   - **Blocker**: needs careful design to avoid deadlock when roles depend
     on each other (Critic consumes Review).

6. **Speculative execution**: dispatch the FINAL Master speculatively
   alongside the Scouts, discard if a Scout produces a better proposal.
   - **Blocker**: doubles Master cost; only worth it if Scout rejection rate
     is high.

## Recommendation for this session

Given the operator directive ("不计 token 成本" + "提高 LLM 利用率") and the
constraint that we must not destabilize the 10-generation tracking run:

- **Apply Tier A.1 immediately**: raise `POK_GLOBAL_LLM_CONCURRENCY` from 2
  to 3 in `deploy/tencent-cloud/env.runtime`. This is the single highest-
  impact, lowest-risk change. Reversible by restart.
- **Defer Tier B/C**: they need dedicated test coverage and could destabilize
  the tracking run. Track as follow-up wave after the 10-generation goal.

## Validation plan for Tier A.1

After raising the cap to 3:

1. Watch `journalctl -u pok-evolution -f` for 429 storms in the first 30 min.
2. Compare per-generation wall-time against the pre-change baseline (the
   `generation_cost_ledger.jsonl` records `cycle_started_at` /
   `published_at`).
3. If 429s exceed 10% of dispatches, revert to 2 and investigate the GLM
   account's actual concurrency ceiling.
4. If stable, consider 4 after 3 successful generations.

## File-level pointers (for future waves)

- `web/core/llm_query.py:2346` — `run_claude_query` (entry point)
- `web/core/llm_query.py:2813-2820` — semaphore acquire site
- `web/core/llm_query.py:2078` — signature-retry loop (Tier B.3 target)
- `web/core/llm_concurrency.py` — global semaphore module
- `web/core/llm_role_observability.py:52-97` — `_ROLE_TIMEOUT_DEFAULTS`
- `web/core/rate_limiter.py` — quota pause/resume
- `web/core/agent_master_ensemble.py:316,661` — concurrent Scout/Critic dispatch
- `web/core/agent_workers.py:1526-1534` — Worker parallel/serial decision
- `deploy/tencent-cloud/env.runtime:106-123` — production timeout/concurrency overrides
