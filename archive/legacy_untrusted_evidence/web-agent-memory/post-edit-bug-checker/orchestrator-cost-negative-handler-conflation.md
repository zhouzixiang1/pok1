---
name: orchestrator-cost-negative-handler-conflation
description: orchestrator_loop's cost<0 branch mislabels SDK/generic crashes as "API auth error (401/403)" — diagnostic noise, not a bug
metadata:
  type: project
---

`orchestrator.py:_run_one_cycle` returns -1.0 for BOTH auth_error (real 401/403) AND cycle_failed (any generic Exception incl. SDK stream/signature errors, introduced Jun14 P1 fix). `orchestrator_loop` (line ~764) funnels both into ONE handler that logs "API auth error (401/403). Backing off 300s." and clears the session.

**Why:** Pre-fix, negative cost only came from auth_error + 429. The P1 fix (cycle_failed→-1.0) added SDK/generic crashes to the same funnel to avoid the v84 fake-success deadlock (partial-cost>0 was treated as success).

**How to apply:** This is diagnostic imprecision, NOT a functional bug. The SDK stream error has its OWN precise signal — `pipeline.sdk_stream_error` system event + UI "SDK stream error (corrupted session)" message, both emitted in the except block BEFORE returning -1.0. Other generic crashes log `[Orchestrator] Error: {e}` in the except block. So the precise signal exists upstream; only the loop-level message is generic/mislabeled. Do NOT "fix" this by giving cycle_failed a different return value without also adding a new branch in orchestrator_loop — and note 300s backoff for any crash is acceptable (thinking is disabled so signature errors no longer fire; this is defensive backstop). Related: [[evolution-tracking-v84-jun14]].
