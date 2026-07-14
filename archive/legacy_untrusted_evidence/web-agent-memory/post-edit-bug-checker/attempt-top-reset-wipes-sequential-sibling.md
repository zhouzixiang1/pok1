---
name: attempt-top-reset-wipes-sequential-sibling
description: _run_single_worker attempt-loop-top _reset_target_files_to_source wipes preceding worker edits in sequential-overlap mode (B-group regression, uncommitted)
metadata:
  type: project
---

In web/core/agent_workers.py, the uncommitted B-group fix added `_reset_target_files_to_source(task, source_v, next_dir, next_v)` at the TOP of the per-attempt loop in `_run_single_worker` (line ~256), intended to clean up NEW files across worker retries. It is SAFE in parallel mode (disjoint targets pre-verified) and single-task mode (no siblings). But it is UNSAFE in the sequential-overlap fallback path of `_execute_workers` (used when target_files overlap or any task has empty target_files).

**Why:** The reset restores each target file from SOURCE (source_v), not from this worker's input snapshot. So when two tasks share a file (e.g. two Algorithmic Logic Architects both targeting strategy.py — architect-architect overlap is only a WARNING in _validate_master_plan, not an error, so it reaches the sequential fallback), Worker 1's attempt-0 top reset overwrites strategy.py with source, deleting Worker 0's already-successful edits. master_prompt.md explicitly promises "later workers can build on earlier workers' changes"; this breaks that contract.

Reproduced with a temp bots tree: after Worker0 wrote `# WORKER0 NEW FUNCTION` to strategy.py, calling `_reset_target_files_to_source(task1, ...)` yielded `'# original source\n'` — Worker0 work gone. Also makes boundary snapshots inconsistent (worker_snapshots[(1,'strategy.py')] captured Worker0's version, but after reset+worker1 edit the validator sees Worker0-code-removal as Worker1's change).

**How to apply:** When reviewing/fixing, the attempt-top reset (and also the timeout/error rollback) in `_run_single_worker` must, in sequential mode, reset to this worker's per-task input snapshot (worker_snapshots) rather than to source_v — or the attempt-top reset should be gated to `parallel_mode or single_task` only. The timeout/error rollback source-reset pre-existed for sequential mode (so that path's regression is older) but the attempt-TOP reset is newly introduced and fires on attempt 0, making the loss near-certain for any overlapping sequential run. Related: [[rc2b-classify-target-change-empty-dst-bug]] context (same file).
