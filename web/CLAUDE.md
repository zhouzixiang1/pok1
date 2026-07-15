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
- Arena and official EXE chip output have zero strength authority.
- The official raise-boundary and terminal-settlement oracle files are exact,
  pinned evaluation inputs.

## Generation stages

Prepare → direction audit → governed literature probe when required → Master →
Workers → quality → review → critic → native TCP precommit → signed official
full certificate → commit/tag → archivist.

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
