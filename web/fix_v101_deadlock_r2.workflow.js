export const meta = {
  name: 'fix-v101-deadlock-round2',
  description: 'Round 2: Fix the 3 critical bugs the post-edit checker found in round 1: (1) precommit_attempt auto-reset misses the actual rework path (critic_checked->workers_done), (2) tool_eval increments precommit_attempt before idempotency guard, (3) timeout_extensions field is not merged by write_pipeline_checkpoint. Single thorough agent (each fix is small but they touch the same 3 files; serial avoids merge conflicts).',
  phases: [
    { title: 'Fix3Bugs', detail: 'one careful agent fixes all three bugs in evolution_infra.py + tool_eval.py' },
    { title: 'Verify', detail: 'pytest + adversarial recheck' },
  ],
}

const FIX_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    summary: { type: "string" },
    bug1_fix: { type: "string", description: "concrete change for auto-reset path" },
    bug2_fix: { type: "string", description: "concrete change for increment ordering" },
    bug3_fix: { type: "string", description: "concrete change for timeout_extensions merge" },
    py_compile_ok: { type: "boolean" },
    targeted_tests_pass: { type: "boolean", description: "did the targeted unit tests still pass after fix" },
    risks: { type: "string" },
  },
  required: ["summary", "bug1_fix", "bug2_fix", "bug3_fix", "py_compile_ok", "targeted_tests_pass", "risks"],
}

const VERIFY_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    verdict: { type: "string", enum: ["pass", "fail"] },
    details: { type: "string" },
    issues: { type: "array", items: { type: "string" } },
  },
  required: ["verdict", "details", "issues"],
}

const FIX_PROMPT = `You are fixing 3 critical bugs that an adversarial post-edit checker found in a previous workflow's output. The previous workflow added precommit-retry circuit breaker + timeout_extensions counter to fix a v101 death loop, but the implementation has 3 holes that nearly negate the fix. Read the relevant files first, then apply the fixes serially (same file edits — do them in order to avoid stomping yourself).

## Bug 1 — auto-reset misses the actual rework path
File: web/core/evolution_infra.py, function write_pipeline_checkpoint, around lines 366-375.

Current logic (added in round 1):
    EARLY = {"prepared","direction_audited","master_planned","workers_done","quality_passed","reviewed","critic_checked"}
    LATE  = {"verified","archived"}
    if old_stage in LATE and stage in EARLY:
        existing_precommit_attempt = 0   # only fires verified->early

WRONG because: precommit FAILED leaves the pipeline at stage='critic_checked' (it does NOT advance to 'verified' on failure — verified is set by tool_eval ONLY when passed=True, see tool_eval.py:545: \`stage="verified" if passed else None\`). So when LLM follows the directive and reworks the bot, the actual transition is critic_checked -> master_planned (or workers_done). LATE->EARLY never fires, counter never resets, eventual false hard-limit.

Fix: extend the auto-reset condition to fire whenever the stage REGRESSES along the pipeline (any later stage going back to an earlier stage indicates rework, regardless of whether it ever reached verified). Concretely:

  STAGE_RANK = {
      "prepared": 0, "direction_audited": 1, "master_planned": 2, "workers_done": 3,
      "quality_passed": 4, "reviewed": 5, "critic_checked": 6,
      "verified": 7, "archived": 8,
  }
  # Rework detected when new stage strictly precedes old stage AND new stage is at or before workers_done
  # (i.e. master_planned or workers_done — code is being regenerated; later regressions like
  # critic_checked->reviewed are still considered "same code, just re-evaluating" so don't reset).
  if old_stage and stage in STAGE_RANK and old_stage in STAGE_RANK:
      old_rank = STAGE_RANK[old_stage]
      new_rank = STAGE_RANK[stage]
      if new_rank < old_rank and new_rank <= STAGE_RANK["workers_done"]:
          existing_precommit_attempt = 0

Replace the EARLY/LATE block with this STAGE_RANK-based check. Keep the comment explaining why (rework = bot code is changing).

## Bug 2 — tool_eval increments precommit_attempt before the idempotency guard
File: web/core/tool_eval.py, around lines 156-172.

Current code (added in round 1) increments + persists precommit_attempt at line 156-170, BEFORE the idempotency guard at line 172 ("Idempotency guard: skip if precommit eval already passed") and BEFORE the gate prerequisite check at line 135 (\`if not _quality_gate_ok ... return _state_blocked\`).

Wrong because:
  (a) After precommit passed (stage=verified), if the LLM redundantly calls run_precommit_eval, the increment fires before the idempotency-cache return at line 124, eventually pushing the counter to MAX. Then orchestrator_context injects "HARD LIMIT — abandon" even though the gate already passed and the next step is commit_bot. False abandonment.
  (b) When prerequisite gates haven't passed (_state_blocked at line 136), the increment fires but no actual battle ran — wastes an attempt on a pre-flight rejection.

Fix: MOVE the increment block to AFTER both the idempotency guard (line ~124) and the gate prerequisite check (line ~135-141), but BEFORE the actual battle execution (before line ~159 _select_precommit_opponents OR before the parallel/scheduler battle dispatch — the precise location must be: precommit eval is genuinely about to run a battle for this version). The increment should ONLY happen when a real precommit eval round is starting.

Concretely:
  - Remove the current increment block at lines 156-170.
  - Re-add the same increment+persist logic right after the candidate_main.exists() check (around line 156 in the new line numbering) AND after _select_precommit_opponents returns at least one opponent (around line ~166 — if not opponents, blockers add 'no_opponents' but a battle still runs for any opponents; if opponents is fully empty, you may choose to skip the increment too — be pragmatic: only consume an attempt when at least one real mirror battle is about to execute).
  - Easiest correct location: AFTER \`if not opponents: blockers.append(...)\` and BEFORE the \`if _use_scheduler and opponents:\` dispatch — you have the opponents list at that point.
  - Pass current_stage (read from existing _matching_checkpoint at this point) so write_pipeline_checkpoint doesn't reset stage. Keep the field name precommit_attempt and use the same merge semantics.

## Bug 3 — timeout_extensions is written but not merged
File: web/core/evolution_infra.py, write_pipeline_checkpoint (the same function as Bug 1).
Also affected: web/core/orchestrator.py around lines 322-345 (where it writes _ckpt_ext["timeout_extensions"] = 1 directly via locked_file).

Wrong because: orchestrator.py writes timeout_extensions=1 by serializing the read _ckpt + injecting the field, but write_pipeline_checkpoint (the canonical writer) does not know the field exists. Any subsequent normal checkpoint write (e.g. tool_eval persisting precommit_attempt mid-cycle, or any gate write) reads the file under LOCK_EX, builds the new state dict from explicitly-merged fields only, and silently drops timeout_extensions. Next timeout sees 0, grants again.

Fix:
  - In write_pipeline_checkpoint signature, add a trailing kwarg: timeout_extensions=None (default None = preserve existing value).
  - In the merge block (around lines 306-320 where existing.get is read), add:
      existing_timeout_extensions = existing.get("timeout_extensions", 0) if existing else 0
      if timeout_extensions is not None:
          existing_timeout_extensions = int(timeout_extensions)
  - On the same auto-reset condition that resets precommit_attempt for rework (the STAGE_RANK condition you wrote for Bug 1), ALSO reset existing_timeout_extensions = 0 — a new generation worth of work means a fresh extension budget. Add it inside the same if block.
  - In the state dict around line 388, add: "timeout_extensions": existing_timeout_extensions

Also harden orchestrator.py's direct-write path (lines ~322-345) by switching it to use write_pipeline_checkpoint(..., timeout_extensions=1) instead of hand-serializing the json under locked_file. That is optional but recommended (single source of truth). If you keep the hand-write path, it must continue to work — verify by reading lines 290-345 of orchestrator.py and confirm the surrounding code still logically flows.

## Self-check (run all of these and confirm pass before reporting)

cd /home/zzx/project/pok/web
python -c "import py_compile; py_compile.compile('core/evolution_infra.py', doraise=True); py_compile.compile('core/tool_eval.py', doraise=True); py_compile.compile('core/orchestrator.py', doraise=True)"
python -m pytest tests/test_precommit_attempt_checkpoint.py tests/test_precommit_eval_directive.py tests/test_orchestrator_timeout_extension.py tests/test_context_precommit_injection.py -v

If existing tests fail because of behavior changes (e.g. a test asserted the OLD EARLY/LATE auto-reset semantics, or a test asserted increment-before-guard), update those tests to assert the NEW correct semantics — but be conservative: only update a test when the test is asserting the buggy behavior we just fixed; do not "fix" a test by gutting it.

Add at least one new test for each bug-fix:
  - test that precommit_attempt resets when stage regresses critic_checked -> master_planned (the actual rework path)
  - test that precommit_attempt does NOT increment when run_precommit_eval is called against an already-verified version (idempotent return path)
  - test that timeout_extensions is preserved across a write_pipeline_checkpoint call that doesn't pass it explicitly

Return FIX_SCHEMA. Read the three files thoroughly before editing — especially the new round-1 additions.`

phase('Fix3Bugs')
log('Round 2: fixing 3 critical bugs found by post-edit checker (auto-reset path + increment ordering + timeout_extensions merge)')
const fix = await agent(FIX_PROMPT, { label: 'fix3bugs', schema: FIX_SCHEMA })

if (fix) {
  log(`Fix applied: ${fix.summary}`)
  log(`  Bug1 (auto-reset): ${fix.bug1_fix}`)
  log(`  Bug2 (increment order): ${fix.bug2_fix}`)
  log(`  Bug3 (timeout_extensions merge): ${fix.bug3_fix}`)
  log(`  py_compile_ok=${fix.py_compile_ok}, targeted_tests_pass=${fix.targeted_tests_pass}`)
} else {
  log('Fix agent returned null — abort verify phase.')
  return { fix: null, verify: null, recheck: null }
}

phase('Verify')
log('Round 2 verify: full pytest suite + adversarial recheck')
const [pyt, recheck] = await parallel([
  () => agent(
    `Run cd /home/zzx/project/pok/web && python -m pytest tests/ -q 2>&1 | tail -30. Report pass/fail counts. ` +
    `IMPORTANT: round 1 had 4 failures in tests/test_pipeline_stages.py::TestWorkerFailureCircuitBreaker that PASS when the file is run alone (test isolation issue, NOT a regression). ` +
    `If those 4 are still in the failure list AND test_pipeline_stages.py alone passes, treat them as pre-existing isolation noise and report verdict=pass. ` +
    `Otherwise report fail with concrete failure names. Do NOT edit files.`,
    { label: 'pytest-suite-r2', schema: VERIFY_SCHEMA }
  ),
  () => agent(
    `You are an adversarial bug checker. Round 2 just fixed 3 bugs you found in round 1: ` +
    `(1) precommit_attempt auto-reset now uses STAGE_RANK to detect ANY rework (not just verified->early); ` +
    `(2) tool_eval.run_precommit_eval increment moved AFTER idempotency guard + gate prerequisite checks (only consumes attempt when a real battle is about to run); ` +
    `(3) timeout_extensions is now a first-class merged field in write_pipeline_checkpoint. ` +
    `Read the latest versions of web/core/evolution_infra.py (write_pipeline_checkpoint), web/core/tool_eval.py (run_precommit_eval start + the moved increment), web/core/orchestrator.py (timeout extension block). Run cd /home/zzx/project/pok && git diff web/core/ | head -500. ` +
    `Verify all 3 bugs are actually fixed (not papered over). Specifically check: ` +
    `(a) STAGE_RANK auto-reset fires on critic_checked->master_planned and on critic_checked->workers_done, but NOT on benign forward progressions or critic_checked->reviewed (same code re-eval); ` +
    `(b) increment is reachable ONLY when a real battle is about to run, never on idempotent-cached return or _state_blocked early return; ` +
    `(c) write_pipeline_checkpoint preserves timeout_extensions on writes that don't mention it AND resets it when rework is detected. ` +
    `Also check no NEW bugs were introduced. Report verdict=pass if all 3 bugs genuinely fixed, fail with concrete file:line if not. Do NOT edit files.`,
    { label: 'recheck-r2', schema: VERIFY_SCHEMA, agentType: 'post-edit-bug-checker' }
  ),
])

return { fix, pytest: pyt, bug_check: recheck }
