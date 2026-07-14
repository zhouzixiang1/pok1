# Handoff — national_native evolution runtime hardening (2026-07-09)

This document hands off the in-progress work to the next AI session. Read it
fully before acting. Authoritative goal is the long-running objective
(`national_native` evolution to a long-term-stable state; track 10 consecutive
clean generations). The repo obeys `AGENTS.md` (dual-checkout, git hygiene).

## Where things stand (TL;DR)

- **4 code fixes are DONE, committed, pushed to `origin/main`, and synced to
  `.evolution_pok`.** They fully resolve the original blocker: an **infinite
  crossover-timeout deadlock** that made evolution hang forever.
- **Evolution is PAUSED** (`/api/control/stop`; web server still up on :8000,
  `running=False`, `next_v=133`). It was paused because the **LLM backend
  (deepseek-v4-pro behind cc-switch) is intermittently stalling on every
  complex streaming role**, so no generation can currently finish.
- The system is now **safe** (no deadlock; stalls are caught fast and recover
  gracefully), but **cannot complete generations** until the backend stabilizes.
- The "10 consecutive clean generations" target is **NOT met** (blocked by
  backend instability, not by code).

## Commits on `origin/main` (all pushed, all synced to `.evolution_pok`)

| Fix | Commit | What it does | Verified |
|---|---|---|---|
| A | `6afa1fc0` | Crossover that exhausts LLM retries now **abandons cleanly** with a `CROSSOVER_LLM_EXHAUSTED` token instead of an infinite re-route deadlock. Files: `web/core/tool_commit.py`, `web/core/orchestrator.py` (`_is_crossover_llm_exhausted_result`). | v126/v131 abandoned cleanly; system advances versions |
| B | `6afa1fc0` | `SystemMessage(init/thinking_tokens)` no longer satisfies the substantive first-activity gate → init-then-stall is caught at `first_activity_timeout` (240/180s) not `idle_timeout` (420/360s). File: `web/core/llm_query.py` (`substantive_activity_logged`). | Live: DIRECTION AUDITOR / REVIEWER / CROSSOVER_COMPAT stalls caught at first_activity |
| C | `d881658c` | Sub-role `stall_timeout` (~55% of idle, clamped 60–180s) for mid-tool-loop stalls; `POK_LLM_<ROLE>_STALL_TIMEOUT` env. File: `web/core/llm_query.py`. | Live: WORKER `llm_role_stall_timeout` fired |
| D | `680b3292` | Orchestrator **main-agent** stream-stall ceiling `ORCH_STREAM_STALL_TIMEOUT` (default 300s, was unbounded→`CYCLE_TIMEOUT` 5400s) for early-cycle stalls with no checkpoint. Files: `web/core/orchestrator.py` (`_OrchStreamStallTimeout`, `_await_next_stream_message`). | Unit test (no-checkpoint case) |

Note: `dc8700e2 "Add neural national v140 candidate"` (between A and C/D) is an
**unrelated commit from a different Claude session** — neural_national_lab
experiment files + a report doc. Not mine; do not revert it (per AGENTS.md, do
not touch unrelated changes).

## Root cause of the recurring stalls (ENVIRONMENT, not code)

- The LLM path is: `claude_agent_sdk` → `cc-switch` proxy (127.0.0.1:15721) →
  `deepseek-v4-pro` at `api.deepseek.com/anthropic/v1/messages`.
- `cc-switch` itself is **healthy** (logs show clean INFO forwarding; simple
  `curl` to the proxy returns 200 in ~1–2s with full text). The user confirmed
  cc-switch is fine.
- The **stall** is intermittent and affects complex multi-turn tool-using roles
  (MASTER, CROSSOVER, WORKER, even COMBINED ANALYST). Signature: a `tool_use` is
  emitted but its `tool_result` never returns, OR the model stops streaming
  mid-think. Simple prompts work; long tool loops stall.
- System load is persistently high (10.5–12.5) during the session.
- There were **3 machine reboots** during the session (killed all evolution
  processes each time). Reboots clear accumulated state and the backend works
  briefly, then stalls recur.
- The backend is **intermittently usable**: v130 once reached `reviewed`
  (CROSSOVER completed + quality_passed + review_passed) before stalling at
  critic-rework. So if the backend has a stable window, generations CAN finish.

## Evidence (in `.evolution_pok`)

- Abandoned gens: v126 (crossover_llm_exhausted), v127 (master_analysis_failed),
  v128 (crossover_llm_exhausted), v129 (master_analysis_failed), v130
  (repo_baseline_head_mismatch — from Fix D sync, expected), v131
  (crossover_llm_exhausted), v132 (crossover_llm_exhausted). v133 started then I
  paused.
- Last 2h: 16 `first_activity_timeout` + 5 `stall_timeout` events = the fixes
  ARE catching stalls (that's the fixes working, not failing).
- `~/.cc-switch/logs/cc-switch.log` — clean INFO forwarding, no errors.

## What is DONE

1. Deadlock fully resolved (Fixes A+B+C+D) and verified live + unit tests.
2. Evolution paused safely (control API stop; web server up).

## What is NOT done (the remaining objective work)

These were scoped but NOT started (I paused before starting them):

1. **Official EXE hard gate** — currently the official EXE certification is
   **advisory/async**. Only a `blocking` `official-failed` retroactively
   disqualifies a parent (`web/core/official_certification.py` ~line 509–513,
   `parent_eligible`). `national_v30` is stuck `official-pending` since
   2026-07-08 (queue worker never processed it). The goal wants official EXE
   pass to be a HARD prerequisite for active pool / commit-tag / opponent.
   - **EXE harness IS available**: Wine 9.0 + Xvfb + valid PE32+ EXE +
     `scripts/official_platform_acceptance.py --check-env` returns
     `{ok:true}`. Wineprefix has the Chinese font. So the harness can run.
   - **WARNING / design risk**: making official-pass a hard pool-membership
     gate would disqualify ALL 30 current active bots at once (none have passed
     official EXE), nuking the pool/ratings/H2H/opponents. Must design with
     grandfathering or a flag or enforce-only-for-new-bots. Analyze before
     coding. (My analysis agent was cancelled before reporting.)

2. **Phase-1 smoke tests** (per objective stage 1):
   - review-rejection → `auto_review_repair` checkpoint synthesis (target_files
     from reviewer main blocker; no `auto_quality_repair_gate_constants_py`).
   - national native TCP 70-hand smoke.
   - official EXE compliance smoke (no illegal check/call/allin/raise, no 60s
     timeout, no sticky-packet parse failure, no stdout pollution).

3. **Historical bot protocol audit/remediation**: audit active leaderboard
   `national_v*` for national TCP protocol issues (illegal check/call, raise
   format, allin timing, sticky-packet splitting, postflop first-action rules,
   card suit/rank mapping). Fix or isolate non-compliant bots. Do NOT loosen
   `sever/engine/validator.py` standards to make bad bots pass.

4. **Resume + track 10 clean generations** — only after backend stabilizes.

## How to resume

1. **First check backend health** before resuming evolution:
   ```bash
   cd /home/zzx/project/pok/.evolution_pok
   # simple backend probe (should be http=200 ~1-2s)
   curl --max-time 15 -s -o /dev/null -w 'http=%{http_code} t=%{time_total}s\n' \
     -X POST http://127.0.0.1:15721/v1/messages -H 'Content-Type: application/json' \
     -H 'x-api-key: PROXY_MANAGED' -H 'anthropic-version: 2023-06-01' \
     -d '{"model":"sonnet","max_tokens":20,"messages":[{"role":"user","content":"Say OK"}]}'
   # then a multi-tool SDK probe to check the stall is gone (see git history of
   # this session for the reproduction script; a healthy run does 6 tool calls in ~30s)
   ```
   If the multi-tool SDK probe still stalls, **do not resume evolution** — the
   backend is still broken. Work on tasks (a)/(b)/(c) instead (they don't need
   the evolving LLM).

2. **Sync state** (in case the other window pushed anything):
   ```bash
   git -C /home/zzx/project/pok fetch --tags origin
   git -C /home/zzx/project/pok/.evolution_pok checkout main && git pull --ff-only --tags
   ```
   Both checkouts should be at `680b3292` unless new commits landed.

3. **Resume evolution** (only if backend healthy):
   ```bash
   curl -X POST http://127.0.0.1:8000/api/control/start
   # or restart: cd .evolution_pok && nohup python3 web/main.py --host 0.0.0.0 --port 8000 --no-build > web/logs/restart_<ts>.log 2>&1 &
   ```
   There is NO checkpoint right now (paused clean). It will start a fresh gen.

## Key facts / gotchas

- Operator checkout = `/home/zzx/project/pok`; evolution checkout =
  `/home/zzx/project/pok/.evolution_pok`. Make infra changes in operator (or a
  worktree), test, commit, push, then `git pull --ff-only` into `.evolution_pok`.
  Never edit infra inside a running `.evolution_pok`.
- `.evolution_pok` MUST run on branch `main` (runtime branch guard stops
  evolution on any other branch — `repo.runtime_branch_drift_cleanup`).
- If you push a new infra commit and pull it into `.evolution_pok`, the runtime
  guard stops evolution and any in-flight generation's `repo_baseline.head`
  mismatches → that generation is **abandon-on-restart** (the v130 case). Clear
  the checkpoint + remove the incomplete `bots/national_vN/` dir before restart.
- `MAX_CROSSOVER_RETRIES = 3` (`evolution_infra.py:106`). Orchestrator main agent:
  `ORCH_FIRST_ACTIVITY_TIMEOUT=600`, `CYCLE_TIMEOUT=5400`, now
  `ORCH_STREAM_STALL_TIMEOUT=300` (Fix D). Sub-role timeouts in
  `llm_query.py:_ROLE_TIMEOUT_DEFAULTS`.
- The `rg`/`grep` alias in this shell throws "互相冲突的匹配器" on some pipes;
  use `find`/`pgrep`/python or absolute paths, and be explicit about cwd (the
  bash cwd does NOT persist across tool calls).
- Tests: `cd web && python3 -m pytest tests/ -q` is the full suite (slow, >5min).
  Fast relevant subsets: `test_llm_role_observability.py`,
  `test_crossover_compat_recovery.py`, `test_rc1_orchestrator_infra.py`,
  `test_orchestrator_timeout_extension.py`. Some tests need committed rated
  bots (`active_bot_version` fixture) — they FAIL in the operator checkout (no
  committed bots) but PASS in `.evolution_pok` (30 active bots). That is
  pre-existing environmental, not a regression.

## Process IDs at handoff (may be stale after reboot)

- web: 194330 (still up, evolution paused). daemon: stopped.
- cc-switch: pid in `/usr/bin/cc-switch` (GNOME app, parent gnome-shell).

## Do NOT

- Do not mark the objective complete — 10 clean generations are not achieved.
- Do not bypass the official EXE hard gate by marking bots "passed" without
  running the EXE.
- Do not loosen `sever/engine/validator.py` to make non-compliant bots pass.
- Do not hand-complete or hand-tag bot versions; only the orchestrator
  `commit_bot` flow (gates passed + `national-bot-v{N}` tag) completes a bot.
- Do not revert `dc8700e2` (unrelated neural experiment commit).
