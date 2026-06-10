# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the System

```bash
# Start the full stack (FastAPI backend + frontend + orchestrator + daemon)
python web/main.py                           # Orchestrator mode on port 8000
python web/main.py --no-daemon               # No background evaluation
python web/main.py --dev                     # Enable uvicorn auto-reload
python web/main.py --no-build                # Skip frontend build
python web/main.py --port 3000               # Custom port

# Standalone orchestrator CLI (no web server)
python web/core/orchestrator.py              # Continuous evolution
python web/core/orchestrator.py --one-gen    # One generation then stop
python web/core/orchestrator.py --dry-run    # Status check only

# Standalone Glicko-2 daemon
python web/core/elo_daemon.py --workers 28 --pairs 5 -v

# Frontend development
cd web/frontend && npm run dev               # Vite dev server
npm run build                                # Build + copy to web/server/static/

# Tests
cd web && python -m pytest tests/ -v              # All backend tests
cd web && python -m pytest tests/test_routes_*.py  # Route endpoint tests
cd web && python -m pytest tests/test_logic_*.py   # Pure logic tests
cd web && python -m pytest tests/test_mcp_*.py     # MCP tool tests
```

## Architecture — Two Patterns of LLM Usage

### Pattern 1: MCP Tool Server (Orchestrator)

The Orchestrator (`core/orchestrator.py`) runs as a Claude agent with registered MCP tools. It receives an `evolution` MCP server created by `create_sdk_mcp_server()` from `tools.py`, which aggregates `@tool()`-decorated functions from `tool_pipeline.py` (pipeline stages) and `tool_status.py` (queries/analysis). The Orchestrator agent decides autonomously which tools to call and in what order.

Each tool function receives an `args` dict, executes business logic (often spawning sub-agent LLM calls via `run_claude_query()`), and returns MCP-formatted results. Key pipeline tools:

| Tool | Stage | What it does |
|---|---|---|
| `prepare_next_gen` | Setup | Copy source bot dir, write `prepared` checkpoint |
| `run_master` | Planning | Call Master LLM → returns JSON task plan with worker assignments |
| `execute_workers` | Coding | Call Worker LLMs (parallel via semaphore, max 3) to edit bot code |
| `run_quality_gates` | Validation | Automated checks: compile, smoke test, decision tests, file size |
| `run_review` | Review | Call Reviewer LLM to score diff quality, enforce role boundaries |
| `run_critic` | Critique | Call Critic LLM for strategic assessment (score ≥6 = approved) |
| `run_precommit_eval` | Pre-commit | Mirror battle regression check vs parent + top opponents |
| `commit_bot` | Commit | Git commit + tag, enforced by gate ledger (all gates must pass) |

### Pattern 2: Direct LLM Calls (Sub-agents)

`run_claude_query()` in `evolution_infra.py` is the primitive for all non-Orchestrator LLM calls. It sends prompt + context files, streams `AssistantMessage`/`ResultMessage`, tracks cost, and handles 529 retries. Different agents get different tools:

- **Master** (`agent_master.py`): Bash, Read — analyzes ratings/experience/match data, produces worker task plan
- **Workers** (`agent_workers.py`): Bash, Read, Edit — directly modify bot source files
- **Reviewer/Critic** (`agent_review.py`): Bash, Read — evaluate diffs
- **Analysts** (stagnation, match, performance, experience consolidation): No tools — JSON-only output

## Data Flow

```
Workers edit bots/claude_v{N}/  (LLM-driven code changes)
        ↓
elo_daemon.py  ← Background subprocess, mirror battles via engine/battle.py
        ↓           ProcessPoolExecutor, per-game Glicko-2 updates
        ↓
core/results/
  ├── glicko_ratings.json    ← Glicko-2 ratings (fcntl-locked, daemon writes)
  ├── rating_history.jsonl   ← Periodic rating snapshots (daemon writes on save cycle)
  ├── head_to_head.json      ← Win/loss matrix per pair (daemon writes)
  ├── bot_stats.json         ← Per-bot aggregated stats (daemon writes)
  ├── match_history.jsonl    ← Match summaries as JSONL (daemon writes per match)
  ├── match_replay/          ← Full replay JSONs (daemon writes, capped at 200)
  ├── pipeline_state.json    ← Pipeline checkpoint for crash recovery (tools write)
  ├── worker_failures.jsonl  ← Worker failure records (agent_workers writes)
  ├── orchestrator_session.json ← Session ID for Orchestrator crash recovery
  ├── app_config.json        ← Daemon config persisted across restarts
  └── llm_costs.jsonl        ← Cumulative LLM cost log (WebUI writes)
        ↓
FastAPI backend reads files (fcntl.LOCK_SH + 2s TTL cache via server/cache.py)
        ↓
Two SSE streams:
  /api/data/stream      ← Periodic (3s/10s/15s): ratings, bots, matches, matrix, history
  /api/evolution/stream ← Event-driven: LLM output, tool calls, cost, status
        ↓
React frontend:
  DataProvider   ← SSE to /api/data/stream → useRatings(), useBots(), etc.
  EvolutionMonitor ← Separate SSE to /api/evolution/stream
  Other pages    ← REST calls (replay, logs, prompts, experience pool)
```

## Key Backend Files

| File | Role |
|---|---|
| `core/evolution_core.py` | Re-export shell — imports from sub-modules for backward compatibility |
| `core/evolution_infra.py` | Constants, git ops, daemon management, ratings, `run_claude_query()`, code verification |
| `core/agent_master.py` | Master Architect + analysis helpers (stagnation, match analysis, experience consolidation, replay summarization) |
| `core/agent_workers.py` | Worker execution: parallel/serial dispatch, timeout isolation, retry logic |
| `core/agent_review.py` | Critic, Performance Verification, Crossover agents |
| `core/orchestrator.py` | LLM-driven orchestrator: spawns Claude agent with MCP tools, streams output, logs to file |
| `core/tools.py` | Re-export shell — registers MCP server from tool sub-modules |
| `core/tool_helpers.py` | Shared tool helpers: UI injection, checkpoint gates, boundary validation |
| `core/tool_pipeline.py` | Core pipeline MCP tools (prepare → master → workers → quality → review → critic → commit) |
| `core/tool_status.py` | Non-pipeline MCP tools (status queries, daemon control, bot management, analysis) |
| `core/web_ui.py` | `EventBroadcaster` (ring buffer 500) + `WebUI` adapter (dual output: terminal + SSE broadcast) |
| `core/elo_daemon.py` | Background subprocess: continuous mirror battles, per-game Glicko-2 updates, `.reap_signal` listener |
| `core/glicko2.py` | Glicko-2 rating system implementation |
| `core/experience_pool.py` | Experience pool trimming logic |
| `core/experience_pool.md` | Accumulated strategic lessons, LLM-consolidated every 3 gens |
| `core/prompts/*.md` | LLM prompt templates for each agent role |
| `server/app.py` | FastAPI app with lifespan that starts orchestrator + daemon. Serves React SPA |
| `server/state.py` | Thread-safe `AppState` singleton (daemon config, generation counter, decisions log) |
| `server/cache.py` | Shared 2-second TTL file read cache used by route modules |
| `server/routes/data_stream.py` | Periodic SSE endpoint — pushes ratings/bots/matches/history on 3s/10s/15s intervals |
| `server/routes/evolution.py` | Event-driven SSE endpoint for real-time LLM streaming + state snapshot |
| `server/routes/control.py` | Manual tool invocation (`/api/control/tool/{name}`), start/stop orchestrator, session management, evolution reset |

## Key Conventions

- All shared files use `fcntl` file locking for concurrent access between daemon subprocess, orchestrator, and API server
- Worker role boundaries enforced by prompts and reviewer: Logic Architects cannot tune constants, Hyperparameter Tuners cannot add functions
- Max 2000 lines for core strategy files (strategy.py, postflop.py), 1500 lines for other `.py` files — adaptive from source bot size + 15% growth budget, hard cap 2500. Reviewer rejects oversized files.
- Decision test pass rate ≥70% (prevents catastrophic regressions like folding AA preflop)
- `commit_bot()` uses checkpoint-based gate ledger: verifies all gates passed in checkpoint, plus `review_approved=true` parameter
- Pipeline checkpoint enforces stage ordering via gate ledger — each stage records pass/fail, next stage verifies previous gates
- Orchestrator session persistence for crash recovery: session file cleared on natural completion, preserved on kill
- Worker failures recorded to `worker_failures.jsonl` and injected into future worker prompts as memory
- Experience pool consolidated by LLM every 3 generations (direct write, no dependency on agent Edit tool)
- `_BLOCKED_MCP_TOOLS` in `evolution_infra.py` blocks external MCP tools from sub-agents

## Test Conventions

Tests in `web/tests/` use `starlette.testclient.TestClient` with a FastAPI app that has all routers but no lifespan (no orchestrator/daemon startup). Fixtures in `conftest.py` provide `client`, `sample_ratings`, `sample_h2h`, `temp_experience`, `temp_prompt_dir`.

Test naming: `test_routes_*.py` (HTTP endpoint tests), `test_logic_*.py` (pure function tests), `test_mcp_*.py` (MCP tool handler tests), `test_helpers.py` (shared test utilities).

## Dependencies

- Backend: `fastapi`, `uvicorn`, `sse-starlette`, `pydantic` (see `requirements.txt`)
- Frontend: React 19, Vite, TailwindCSS 4, TypeScript, ApexCharts (see `frontend/package.json`)
- LLM: `claude_agent_sdk` (provides `query`, `ClaudeAgentOptions`, streaming types, `@tool` decorator, `create_sdk_mcp_server`)
- Battle engine: `engine/battle.py` (mirror_battle, subprocess-based bot execution)

## Post-Task Workflow

After completing each task, you MUST do both of the following:

1. **Git commit and push** all changes:
```bash
git add -A
git commit -m "<descriptive message>"
git push
```

2. **Update memory** in `~/.claude/projects/-Users-zhouzixiang-Documents-pok/memory/`. Save what you learned during the task — surprising findings, user corrections, non-obvious constraints, or validated approaches. Check existing memories first to avoid duplicates; update stale ones rather than creating new ones.
