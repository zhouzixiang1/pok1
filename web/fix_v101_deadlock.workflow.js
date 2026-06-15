export const meta = {
  name: 'fix-v101-deadlock',
  description: 'Fix v101 precommit-retry deadlock: (1) orchestrator timeout extension false-complete at critic_checked, (2) precommit FAILED has no circuit breaker / no directive. Parallel 4-file edit + verify.',
  phases: [
    { title: 'Implement', detail: '4 parallel agents: evolution_infra / tool_eval / orchestrator / orchestrator_context' },
    { title: 'Verify', detail: 'pytest suite + adversarial post-edit bug-check' },
  ],
}

// ╔════════════════════════════════════════════════════════════════╗
// ║ GLOBAL INTERFACE CONTRACT — every agent MUST follow exactly      ║
// ╚════════════════════════════════════════════════════════════════╝
const CONTRACT = `
## GLOBAL INTERFACE CONTRACT (all 4 implement agents share this — follow EXACTLY so the 4 files stay consistent)

1. pipeline_state.json GAINS a new field: "precommit_attempt" (int, default 0). Meaning: number of times run_precommit_eval has been called against the CURRENT bot code for this generation. Resets to 0 when workers rework the bot (stage regresses to workers_done or earlier).

2. evolution_infra.py GAINS constant: MAX_PRECOMMIT_RETRIES = 3.

3. write_pipeline_checkpoint signature GAINS two trailing kwargs (backward-compatible, add at END of param list):
   precommit_attempt=None, reset_precommit_attempt=False
   Merge semantics (mirror existing generation_attempt pattern):
     - if reset_precommit_attempt is True -> precommit_attempt = 0
     - elif precommit_attempt is not None -> precommit_attempt = passed value
     - else preserve existing.precommit_attempt (default 0 if none)
   AUTO-RESET: if the new stage is one of ("prepared","direction_audited","master_planned","workers_done","quality_passed","reviewed","critic_checked") AND old stage was later ("verified","archived") — i.e. stage regressed due to worker rework — set precommit_attempt=0. (Worker rework = new bot code = precommit counter restarts.)

4. Sentinel cost value: -3.0 returned by _run_one_cycle means "timeout extension granted, cycle NOT complete". Main loop MUST handle: cost == -3.0 -> continue to next iteration WITHOUT post_generation_cleanup, WITHOUT logging 'gen complete', WITHOUT any backoff sleep. (Distinct from -0.5 infra / -1.0 generic / <0 auth.)

5. precommit FAILED directive text (tool_eval returns this in result["directive"]):
   - When precommit_attempt < MAX_PRECOMMIT_RETRIES:
     "Precommit FAILED (attempt {N}/{MAX}) — bot code is UNCHANGED since the last attempt, so retrying precommit will give the SAME result. Do NOT call run_precommit_eval again. You MUST either (a) rework the bot: call execute_workers with reviewer_feedback explaining the loss vs {worst_opponent} ({wins}W-{losses}L), targeting that matchup; or (b) abandon this generation and start fresh from a different direction."
   - When precommit_attempt >= MAX_PRECOMMIT_RETRIES:
     "PRECOMMIT HARD LIMIT REACHED ({MAX}/{MAX} attempts). The current bot cannot pass precommit. Do NOT retry precommit or workers. Abandon this generation (the pipeline will reset on the next cycle with a new master plan)."
`

const REPORT_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    file: { type: "string", description: "path of the file edited" },
    summary: { type: "string", description: "one-line summary of the fix" },
    changes: { type: "array", items: { type: "string" }, description: "list of concrete edits made (anchor + what changed)" },
    test_file: { type: "string", description: "test file created/extended, or 'none'" },
    py_compile_ok: { type: "boolean", description: "did py_compile pass after edits" },
    risks: { type: "string", description: "any residual risk or 'none identified'" },
  },
  required: ["file", "summary", "changes", "py_compile_ok", "risks"],
}

const VERIFY_SCHEMA = {
  type: "object",
  additionalProperties: false,
  properties: {
    verdict: { type: "string", enum: ["pass", "fail"] },
    details: { type: "string", description: "evidence: pytest counts or specific bugs found" },
    issues: { type: "array", items: { type: "string" }, description: "list of concrete issues (empty if none)" },
  },
  required: ["verdict", "details", "issues"],
}

// ── Agent 1: evolution_infra.py (interface first — defines the contract primitives) ──
const PROMPT_EVOL = CONTRACT + `

## YOUR FILE: web/core/evolution_infra.py

### Task A — add constant
Near the other MAX_* constants (around line 74, MAX_GENESIS_RETRIES / MAX_GEN_COST), add:
    MAX_PRECOMMIT_RETRIES = 3

### Task B — extend write_pipeline_checkpoint (around line 272-365)
Read the full function first. It uses read-merge-write under LOCK_EX. Add two trailing kwargs:
    precommit_attempt=None, reset_precommit_attempt=False
Add merge logic (mirror the generation_attempt pattern at lines 300-329):
  - Initialize existing_precommit_attempt = precommit_attempt
  - In the existing-merge block: if precommit_attempt is None: existing_precommit_attempt = existing.get("precommit_attempt", 0)
  - After reset flags: if reset_precommit_attempt: existing_precommit_attempt = 0
  - AUTO-RESET on stage regression: define EARLY = {"prepared","direction_audited","master_planned","workers_done","quality_passed","reviewed","critic_checked"} and LATE = {"verified","archived"}. If old_stage in LATE and new stage in EARLY (regression from worker rework): existing_precommit_attempt = 0. Place this AFTER old_stage is known (line ~350) and BEFORE the state dict is built.
  - Add "precommit_attempt": existing_precommit_attempt to the state dict (line ~359).

### Task C — test
Create web/tests/test_precommit_attempt_checkpoint.py with 3-4 tests:
  - fresh checkpoint has precommit_attempt defaulting to 0
  - write_pipeline_checkpoint(precommit_attempt=2) persists 2
  - reset_precommit_attempt=True resets to 0
  - stage regression verified->workers_done auto-resets precommit_attempt to 0
Use tmp_path + monkeypatch PIPELINE_STATE_FILE like existing tests (see web/tests/conftest.py for fixtures, and test_rc3_daemon_grace.py for the isolation pattern). Do NOT depend on the real bots/ dir.

### Self-check
Run: cd web && python -c "import py_compile; py_compile.compile('core/evolution_infra.py', doraise=True)"
Then: cd web && python -m pytest tests/test_precommit_attempt_checkpoint.py -v

Return the REPORT_SCHEMA. Read the file before editing.`

// ── Agent 2: tool_eval.py (precommit counter increment + FAILED directive) ──
const PROMPT_TOOLEVAL = CONTRACT + `

## YOUR FILE: web/core/tool_eval.py (focus: run_precommit_eval, lines ~98-548)

### Task A — increment precommit_attempt at start of run_precommit_eval
Near the top of run_precommit_eval (after _resolve_version_args, before the idempotency guard at line ~112), read the current checkpoint's precommit_attempt, increment by 1, and persist it. Use the existing checkpoint helpers (look at how the file imports _matching_checkpoint / read_pipeline_checkpoint / write_pipeline_checkpoint / _record_gate — grep the imports at top of tool_eval.py). Persist via write_pipeline_checkpoint(v, source_v, current_stage, precommit_attempt=new_count) — pass the CURRENT stage unchanged (read it from the checkpoint) so you don't accidentally advance the stage. If write_pipeline_checkpoint is not directly imported, use whatever helper the file already uses to update checkpoint fields, but the field MUST end up persisted.

### Task B — FAILED directive (around line 501-548)
Currently when passed=False the result dict has no directive. Add:
  - Read precommit_attempt (the incremented count from Task A).
  - worst opponent = the matchup with the most losses (or the first blocker with reason lost_to_parent / lost_to_opponent).
  - Build result["directive"] per the CONTRACT text (use MAX_PRECOMMIT_RETRIES imported from evolution_infra, and the actual N / worst_opponent / wins-losses numbers).
  - When precommit_attempt >= MAX_PRECOMMIT_RETRIES, also log_system_event("pipeline.precommit_hard_limit", "warn", ...) so it shows in system_events.jsonl.
Keep passed=True path unchanged (it already has its own directive at line 120).

### Task C — test
Create/extend web/tests/test_precommit_eval_directive.py:
  - FAILED result contains a directive string mentioning "execute_workers" when precommit_attempt < MAX
  - FAILED result directive mentions "HARD LIMIT" when precommit_attempt >= MAX
  - precommit_attempt increments across two mock calls (mock mirror_battle to return a losing matchup; mock the opponent selection)
Mirror the mocking pattern from existing tool_eval tests if any (grep web/tests for run_precommit_eval). Use tmp_path + monkeypatch; do NOT touch real bots/.

### Self-check
cd web && python -c "import py_compile; py_compile.compile('core/tool_eval.py', doraise=True)"
cd web && python -m pytest tests/test_precommit_eval_directive.py -v

Return REPORT_SCHEMA. Read the file before editing. Import MAX_PRECOMMIT_RETRIES from evolution_infra (the evolution_infra agent is adding it; the import path is the same as other evolution_infra imports already in tool_eval.py).`

// ── Agent 3: orchestrator.py (timeout extension: verified-only + counter + sentinel) ──
const PROMPT_ORCH = CONTRACT + `

## YOUR FILE: web/core/orchestrator.py (focus: timeout extension lines ~256-330, main loop ~788-830)

### Task A — tighten the stage condition (line ~282)
Change:
    if _ckpt and _ckpt.get("stage") in ("verified", "critic_checked"):
to:
    if _ckpt and _ckpt.get("stage") == "verified":
Rationale: critic_checked is NOT commit-imminent — verified (precommit passed) + archived (commit) still follow. Only "verified" means commit is the next gate. This alone stops the false-complete at critic_checked that caused the v101 death loop.

### Task B — add an extension counter so "ONE extension" is real
Currently every timeout at the eligible stage re-grants. Add a per-version counter persisted in the checkpoint as field "timeout_extensions" (int, default 0). In the extension branch:
  - Read current timeout_extensions from _ckpt (default 0).
  - If timeout_extensions >= 1: this is NOT the first extension — do NOT grant. Fall through to normal timeout handling (let the existing non-extension path run, which returns a negative cost / handles cleanup).
  - If timeout_extensions == 0: grant, and increment timeout_extensions to 1 in the checkpoint (write it back via the same locked_file rewrite already done at lines ~300-312, just add "timeout_extensions": 1 to _ckpt_ext).

### Task C — grant must NOT look like success (sentinel -3.0)
At lines ~315-319 the extension branch currently does:
    if ui: return ui.gen_cost_total - _cost_at_start
    return total_cost
This makes the main loop (cost >= 0) run post_generation_cleanup + log "gen complete" — the false-complete bug. Change BOTH returns to:
    return -3.0
Keep the _clear_orchestrator_session() call and the checkpoint timestamp refresh (they're correct — the session is dead after timeout).

### Task D — main loop handles -3.0 sentinel (lines ~788-830)
Read the full main loop cost-handling block first (from "Phase 3: Cleanup" through the auth/infra backoff). The structure is:
    if cost >= 0: post_generation_cleanup + log 'gen complete' + cycle_done
    if cost < 0: auth/429 handling, then cost == -0.5 infra 15s backoff, ...
INSERT a new branch for the extension sentinel BEFORE the cost >= 0 success block:
    if cost == -3.0:
        # timeout extension granted mid-cycle; cycle NOT complete — do not cleanup, do not log complete, just continue
        if ui: ui.log_history("Orchestrator: cycle timed out but commit was imminent — granted extension, resuming from checkpoint next cycle (no commit yet).", "warn")
        continue
This must come BEFORE "if cost >= 0" so -3.0 is never treated as success. Verify -3.0 is NOT caught by any existing cost<0 branch (auth checks cost<0 but checks rate_limiter.is_blocked() first; -0.5 is exact-match; -3.0 is neither so it would fall through — confirm it won't hit an unintended backoff; if the final else would sleep/backoff on -3.0, add an explicit "elif cost == -3.0: continue" guard there too).

### Task E — test
Create web/tests/test_orchestrator_timeout_extension.py:
  - stage=="verified" + timeout_extensions==0 -> grants (returns -3.0, checkpoint timeout_extensions becomes 1)
  - stage=="critic_checked" -> does NOT grant (falls through; this is the regression test for the v101 bug)
  - stage=="verified" + timeout_extensions>=1 -> does NOT grant (second extension refused)
  - main loop: cost == -3.0 does NOT call post_generation_cleanup / does NOT emit cycle_done
Mock _stream_response / _read_ckpt / post_generation_cleanup as needed. Use tmp_path; do NOT start real orchestrator.

### Self-check
cd web && python -c "import py_compile; py_compile.compile('core/orchestrator.py', doraise=True)"
cd web && python -m pytest tests/test_orchestrator_timeout_extension.py -v

Return REPORT_SCHEMA. Read the file (especially lines 256-330 and 785-835) BEFORE editing. Be very careful not to break the existing -0.5 infra / auth / 429 cost branches — those are correct and must be preserved.`

// ── Agent 4: orchestrator_context.py (inject precommit history into LLM context) ──
const PROMPT_CONTEXT = CONTRACT + `

## YOUR FILE: web/core/orchestrator_context.py (focus: _format_checkpoint_info, lines ~100-124)

### Task A — inject precommit_attempt + last failure into the LLM context
Read _format_checkpoint_info fully. It currently injects generation_attempt retries (lines 113-118). After that block, add precommit status injection:
  - Read precommit_attempt = checkpoint.get("precommit_attempt", 0)
  - Read the last precommit gate result: precommit_gate = checkpoint.get("gate_results", {}).get("precommit_eval", {})
  - If precommit_attempt > 0:
      last_result = ""
      if precommit_gate: build "last: {wins}W-{losses}L-{draws}D vs {n} opps" from precommit_gate fields (total_wins/total_losses/total_draws; be defensive — use .get with defaults)
      Append a line:
        f"PRECOMMIT STATUS: {precommit_attempt}/{MAX_PRECOMMIT_RETRIES} attempts. {last_result}. Bot code is unchanged across attempts — retrying run_precommit_eval gives the SAME result. If failed, rework the bot (execute_workers) or abandon — do NOT loop on precommit."
  - Import MAX_PRECOMMIT_RETRIES from evolution_infra (same import style as other imports at top of orchestrator_context.py — grep for existing evolution_infra imports). If precommit_attempt >= MAX_PRECOMMIT_RETRIES, append: "PRECOMMIT HARD LIMIT reached — abandon this generation."

### Task B — test
Create web/tests/test_context_precommit_injection.py:
  - checkpoint with precommit_attempt=0 -> no PRECOMMIT STATUS line
  - checkpoint with precommit_attempt=2 + gate_results.precommit_eval{total_wins:8,total_losses:11} -> context contains "PRECOMMIT STATUS: 2/3" and "8W-11L"
  - checkpoint with precommit_attempt=3 -> context contains "HARD LIMIT"
Call _format_checkpoint_info(checkpoint, lines) directly with a synthetic checkpoint dict; assert on the joined lines string.

### Self-check
cd web && python -c "import py_compile; py_compile.compile('core/orchestrator_context.py', doraise=True)"
cd web && python -m pytest tests/test_context_precommit_injection.py -v

Return REPORT_SCHEMA. Read the file before editing. Be defensive with .get() — gate_results may be missing or the precommit_eval entry may have varying field names (check what tool_eval.py actually writes via _gate_payload — it writes total_wins/total_losses/total_draws per the CONTRACT).`

// ════════════════════════════════════════════════════════════════
// PHASE 1 — IMPLEMENT (4 agents, parallel, different files = no conflict)
// ════════════════════════════════════════════════════════════════
phase('Implement')
log('Phase 1: 4 parallel implement agents (evolution_infra / tool_eval / orchestrator / orchestrator_context)')
const reports = await parallel([
  () => agent(PROMPT_EVOL,     { label: 'evolution_infra',      schema: REPORT_SCHEMA }),
  () => agent(PROMPT_TOOLEVAL, { label: 'tool_eval',            schema: REPORT_SCHEMA }),
  () => agent(PROMPT_ORCH,     { label: 'orchestrator',         schema: REPORT_SCHEMA }),
  () => agent(PROMPT_CONTEXT,  { label: 'orchestrator_context', schema: REPORT_SCHEMA }),
])

const ok = reports.filter(Boolean)
log(`Phase 1 done: ${ok.length}/4 agents reported. py_compile_ok: ${ok.map(r => r.file.split('/').pop() + '=' + r.py_compile_ok).join(', ')}`)
for (const r of ok) {
  log(`  [${r.file}] ${r.summary}`)
}

// ════════════════════════════════════════════════════════════════
// PHASE 2 — VERIFY (pytest suite + adversarial bug-check)
// ════════════════════════════════════════════════════════════════
phase('Verify')
log('Phase 2: pytest full suite + adversarial post-edit bug-check')
const [pytestRes, bugRes] = await parallel([
  () => agent(
    `Run the full backend test suite and report results. ` +
    `cd /home/zzx/project/pok/web && python -m pytest tests/ -v 2>&1 | tail -40. ` +
    `Report verdict=pass if all tests pass (or only pre-existing skips), fail otherwise. ` +
    `In details give the pass/fail/skip counts and any failure names. ` +
    `In issues list any failing test names with one-line cause. ` +
    `Do NOT edit any files — this is verification only.`,
    { label: 'pytest-suite', schema: VERIFY_SCHEMA }
  ),
  () => agent(
    `You are an adversarial post-edit bug checker. Four files were just edited to fix a v101 precommit-retry death loop: ` +
    `web/core/evolution_infra.py (added precommit_attempt checkpoint field + MAX_PRECOMMIT_RETRIES + auto-reset on stage regression), ` +
    `web/core/tool_eval.py (run_precommit_eval increments precommit_attempt + FAILED directive), ` +
    `web/core/orchestrator.py (timeout extension: verified-only + extension counter + sentinel -3.0 + main-loop -3.0 branch), ` +
    `web/core/orchestrator_context.py (inject precommit status into LLM context). ` +
    `Read all four files (focus on the changed regions) and the git diff: cd /home/zzx/project/pok && git diff --stat && git diff web/core/ | head -400. ` +
    `Hunt for: (1) the 4 files' shared interface contract is consistent (field name precommit_attempt, MAX_PRECOMMIT_RETRIES import, sentinel -3.0); ` +
    `(2) orchestrator -3.0 sentinel is not accidentally caught by existing cost<0 branches (auth/-0.5 infra) or causes an unintended backoff; ` +
    `(3) write_pipeline_checkpoint merge doesn't clobber precommit_attempt on unrelated stage writes; ` +
    `(4) tool_eval increment doesn't advance the stage accidentally; ` +
    `(5) any regression to the existing infra-backoff / auth / critic paths. ` +
    `Report verdict=pass if sound, fail if any real bug. List concrete issues with file:line. Do NOT edit — report only.`,
    { label: 'bug-checker', schema: VERIFY_SCHEMA, agentType: 'post-edit-bug-checker' }
  ),
])

return {
  implement_reports: ok,
  pytest: pytestRes,
  bug_check: bugRes,
}
