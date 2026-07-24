# Web Evolution System

Read the repository `AGENTS.md` first. `web/` is the control plane for the sole
active `national_tcp_policy_v1` architecture.

## Entry points

- `main.py` launches FastAPI/uvicorn and optionally the evolution orchestrator.
  It is not a TUI.
- `core/orchestrator.py` runs the generation state machine.
- `core/elo_daemon.py` publishes complete 70-hand raw native TCP strength
  matches.
- `server/app.py` serves APIs, SSE, and the built React application.

## Non-negotiable boundaries

- Active evaluation is raw national TCP only.
- Candidate strategy lives in `policy.py`; system-owned runtime code owns TCP,
  protocol state, deadline, legality, and send.
- Do not add active execution profiles, adapter fallbacks, subprocess-JSON
  smoke tests, or dynamic archive loaders.
- Do not read retired result files or archive analyses into Master/Worker
  prompts. Planning evidence must come from the immutable current-epoch
  snapshot and carry its identity/digest.
- Proposal, ballot, Reviewer, and Critic evidence must bind the durable provider
  effect to one `RESULTS_DIR/v<N>/logs/strict_invocations/<invocation_id>` log.
  Reject flat/foreign roots and duplicate trailers. Backend reads use opaque
  ids and a no-follow results-root descriptor walk; frontend only validates and
  renders those ids.
- Canonical abandon fences the outer Worker journal and creates or terminally
  transitions the strict child. Real and replay dispatch require `running` and
  cannot resurrect a stale pre-dispatch descriptor.
- An exhausted strict Master slot canonically abandons only at
  `direction_audited`; never re-enter `run_master` or extend that reason across
  Review, Critic, precommit, certification, or publication.
- Proposal Scouts receive compact frozen facts, not the full final-Master
  tutorial/output schema. Bootstrap read scope is target-only; normal scope is
  exact source, target, and one frozen snapshot. A system-verified current
  ABI-reachable chain is the preferred proposal chain and dead-helper chains are
  rejected. Stable field-level projection errors persist in the bootstrap strict
  journal; normal evolution content-binds the same errors into its sole repair
  prompt/provenance. A denied docs or other out-of-scope read contributes no
  evidence.
- A disappeared checkpoint is a completed abandon only when one unique
  canonical result from the current authorized owner tool includes
  `workflow_run_id` and exactly re-proves the current transaction,
  ledger/finalize receipts, checkpoint identity, and both complete terminal
  journals. It must bind one pending route-mutating ToolUse through an explicit
  id/parent id or the bounded sole-pending SDK form; unknown, reused,
  swapped-owner or unsettled ids block. Invalid or unreadable authority blocks recovery. A genuinely absent
  checkpoint ends the provider stream; the outer scheduler alone owns non-MCP
  `prepare_generation`. Exact `selected`/`preparing` routes own first/crash
  recovery `prepare_next_gen`, but unbound target bytes trigger system canonical
  abandon. Both timeout states are active leases: `timed_out` abandons and
  `infra_timed_out` retries only after full artifact/gate/baseline reproof and
  exact CAS.
- A pending/running/blocked post-publication handoff makes the provider
  `end_stream`; outer deterministic recovery alone owns `run_archivist`.
- `orchestrator.py --one-gen` means one complete workflow/generation, not one
  provider session. Abandon, operator action, recovery failure, accounting
  failure, and successful publication/cleanup remain distinct terminal states.
- Control health withholds a route and `/api/control/start` returns 409 before
  any stability reset when recovery or an operator boundary blocks launch. A
  checkpoint-free scheduler projection carries authoritative `next_v` but a
  null source: parent selection remains owned by non-MCP `prepare_generation`.
  Checkpoint observation is before/read/after fail-closed. Frontend Start mirrors
  the exact active-generation, post-publication, or clean scheduler boundary.
  Live foreign handoff owners block a second runtime; AppState and the process
  LLM shutdown manager are both exact-owner fenced.
- Native precommit owns a monotonic per-attempt cancellation token and a frozen
  first-strict execution scope. Late complete matches are not admitted and no
  next sample starts after cancellation; retries reuse the same control journal.
- The first-strict (`national_cloud_v1` on this branch) 5+3 dependency is present
  only when doctor is green and `first_strict_control_v1` hash
  `b37cd019fe6b635a119950adb5f7ecf10ddceeafacfbed6b4c3a0955064516e2`
  is valid, unused, and `0/1`. Even then, only
  `official_bootstrap_required` unlocks the operator action.
- Arena and official EXE chip output have zero strength authority.
- The official raise-boundary, terminal-settlement, and called-all-in runout
  wire oracle files are exact, pinned evaluation inputs. The last permits no
  fabricated public cards and requires complementary cross-wire actions, exact
  all-in net settlement, and strict THP exact-prefix-or-five-card
  board/action/blind/hole/earnings proof. A live deferred raw action may be a
  provisional warning; finalized replay is always strict.

## Generation stages

Outer `prepare_generation` selection → exact routed materialization
(`prepare_next_gen` or crossover) → direction audit → governed literature probe
when required → Master → Workers → quality → review → critic → native TCP
precommit → signed official full certificate → commit/tag → provider end-stream
→ outer deterministic Archivist.

Crossover prepares a baseline only. It does not bypass Master, Workers, or any
gate. Worker tasks and artifacts are checkpoint-owned, digest-bound, and
projected atomically.

## Verification

```bash
cd web
python -m pytest tests -q
python -m pytest tests/test_national_platform_alignment.py -q  # if present

cd frontend
npm run build
```

For protocol rules run the repository shard instead:

```bash
python -m pytest sever/tests -q
```

Generated frontend outputs under `frontend/dist/` and `server/static/` are not
source files.
