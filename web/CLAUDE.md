# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

The **Self-Evolution System** (`web/`) is an LLM-driven poker bot evolution framework. It uses Claude (via `claude_agent_sdk`) as a multi-agent pipeline — Master Architect, Worker Agents, Code Reviewer, and Orchestrator — to iteratively improve poker bots. A background Glicko-2 daemon continuously evaluates bots through mirror battles, and a React dashboard provides real-time monitoring via SSE.

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
python web/core/elo_daemon.py --workers 14 --pairs 5 -v

# Frontend development
cd web/frontend && npm run dev               # Vite dev server
npm run build                                # Build + copy to web/server/static/
```

## Architecture

### Orchestrator-Driven Evolution

An LLM agent (`orchestrator.py`) receives MCP tools and autonomously decides the evolution flow. It calls tools like `run_master()`, `execute_workers()`, `run_quality_gates()` in whatever order it deems optimal. The Orchestrator can retry, deviate from the standard pipeline, or trigger crossovers as needed.

### The Multi-Agent Evolution Pipeline (per generation)

1. **Master Architect** (`prompts/master_prompt.md`): Analyzes ratings, experience pool, and current bot code. Produces a JSON task plan with 2 worker assignments — one "Algorithmic Logic Architect" (structural changes) and one "Hyperparameter Tuner" (numeric constants only). Can set `branch_from` to evolve from a different ancestor.

2. **Worker Agents** (`prompts/worker_prompt.md`): Each worker receives a role-constrained prompt. Logic Architects can add functions/refactor; Hyperparameter Tuners can only change numeric constants. Workers directly edit bot source files. Parallel execution attempted first, serial fallback on failure.

3. **Quality Gates** (automated, no LLM): Compile check (`py_compile`), smoke test (1 mirror battle vs bot6), decision tests (predefined scenarios — reject bots that fold AA preflop etc.), file size check (max 1000 lines per file).

4. **Code Reviewer** (`prompts/reviewer_prompt.md`): LLM reviews the diff, enforces role boundaries, scores quality 1-10. Can reject with feedback that triggers worker revision.

5. **Commit**: Git commit + `bot-v{N}` tag. Tags are the authoritative completion proof.

### Key Components

| File | Role |
|---|---|
| `core/evolution_core.py` | Re-export shell — imports from sub-modules for backward compatibility. |
| `core/evolution_infra.py` | Shared infrastructure: constants, git ops, daemon management, ratings, `run_claude_query()`, code verification. |
| `core/agent_master.py` | Master Architect agent + analysis helpers (stagnation, match analysis, experience consolidation, replay summarization). |
| `core/agent_workers.py` | Worker agent execution: parallel/serial dispatch, timeout isolation, retry logic. |
| `core/agent_review.py` | Review agents: Critic, Performance Verification, Crossover. |
| `core/orchestrator.py` | LLM-driven orchestrator: spawns a Claude agent with MCP tools, streams output, logs to file. |
| `core/tools.py` | Re-export shell — registers MCP server from all tool sub-modules. |
| `core/tool_helpers.py` | Shared tool helpers: UI injection, checkpoint gates, boundary validation. |
| `core/tool_pipeline.py` | Core pipeline MCP tools: Master → Workers → Quality → Review → Critic → Precommit → Commit. |
| `core/tool_status.py` | Non-pipeline MCP tools: status queries, daemon control, bot management, analysis. |
| `core/elo_daemon.py` | Background subprocess that continuously runs mirror battles and updates Glicko-2 ratings. Prioritizes under-evaluated pairs. |
| `core/glicko2.py` | Glicko-2 rating system implementation (r, rd, volatility). |
| `core/web_ui.py` | SSE broadcaster + WebUI adapter. Dual output: terminal + dashboard. |
| `core/experience_pool.md` | Accumulated strategic lessons. Trimmed to 8 entries, consolidated by LLM every 3 gens. |
| `core/prompts/*.md` | LLM prompt templates for each agent role (master, worker, reviewer, crossover, orchestrator, initial). |
| `server/app.py` | FastAPI app with lifespan that starts orchestrator + daemon. Serves React SPA from `server/static/`. |
| `server/state.py` | Thread-safe global state (daemon config, generation counter). |
| `frontend/` | React + Vite + Tailwind dashboard. Build outputs to `server/static/`. |

### Data Flow

```
Bots (bots/claude_v{N}/)  ← Workers edit these
        ↓
elo_daemon.py  ← Runs mirror battles via engine/battle.py
        ↓
results/glicko_ratings.json  ← Glicko-2 ratings (fcntl-locked)
results/rating_history.jsonl ← Rating snapshots per period
results/match_history.jsonl  ← Match summaries (JSONL)
results/match_replay/        ← Full replay JSONs (capped at 200)
        ↓
Master reads ratings + experience_pool.md → plans workers
        ↓
Workers modify bot code → Reviewer validates → Git commit + tag
```

### Bot Versioning

- Bots live in `bots/claude_v{N}/` (N is monotonically increasing)
- `.completed` sentinel file marks a finished bot
- `bot-v{N}` git tags are the authoritative completion proof (dual validation: `.completed` + tag)
- Parent tracking via git tag messages (`parent: claude_v{M}`)
- Pool capped at 30 active bots; weakest culled by H2H average win rate
- Retired bots moved to `bots/graveyard/`

### Stagnation Handling

- Analyzed via `analyze_stagnation` MCP tool (LLM determines if stagnation is real or Glicko noise)
- At stagnation ≥3: **crossover** between top-2 bots (`prompts/crossover_prompt.md`)
- Master can `branch_from` a different ancestor

## Key Conventions

- All shared files (ratings, stats) use `fcntl` file locking for concurrent access
- Worker role boundaries are enforced by both prompts and reviewer: Logic Architects cannot tune constants, Hyperparameter Tuners cannot add functions
- Max 1000 lines per `.py` file — reviewer rejects oversized files
- Decision test pass rate must be ≥70% (prevents catastrophic regressions)
- Experience pool trimmed when >120 lines (keeps last 100), LLM-consolidated every 3 generations
- API rate limit (529) handled with automatic retry + exponential backoff (30s, 60s, 120s)
- Worker failures recorded to `results/worker_failures.jsonl` and injected into future worker prompts
- `claude_agent_sdk` is the LLM interface — not the Anthropic SDK directly
- `commit_bot()` has hard guards: compile errors, decision tests <70%, or `review_approved=false` all block the commit
- Orchestrator prompt context includes: current bot rd reliability, incomplete-gen warning, recent git tags, recent worker failures
- `_consolidate_experience_pool` writes LLM output back to file directly (no dependency on agent using Edit tool)
- `_analyze_recent_matches` collects both losses AND close wins (margin ≤2) for balanced Master context

## Dependencies

- Backend: `fastapi`, `uvicorn`, `sse-starlette`, `pydantic` (see `requirements.txt`)
- Frontend: React 19, Vite, TailwindCSS 4, TypeScript (see `frontend/package.json`)
- LLM: `claude_agent_sdk` (provides `query`, `ClaudeAgentOptions`, streaming types, `@tool` decorator, `create_sdk_mcp_server`)
- Battle engine: `engine/battle.py` (mirror_battle, subprocess-based bot execution)
- No external Python dependencies for bots or core engine
