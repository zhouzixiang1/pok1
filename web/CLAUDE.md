# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

The **Self-Evolution System** (`web/`) is an LLM-driven poker bot evolution framework. It uses Claude (via `claude_agent_sdk`) as a multi-agent pipeline — Master Architect, Worker Agents, Code Reviewer, and Orchestrator — to iteratively improve poker bots. A background Glicko-2 daemon continuously evaluates bots through mirror battles, and a React dashboard provides real-time monitoring via SSE.

## Running the System

```bash
# Start the full stack (FastAPI backend + frontend + orchestrator + daemon)
python web/main.py                           # Default: orchestrator mode on port 8000
python web/main.py --mode classic            # Classic evolution loop (no LLM orchestrator)
python web/main.py --mode manual --no-daemon # Manual mode, no background evaluation
python web/main.py --dev                     # Enable uvicorn auto-reload

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

### Two Evolution Modes

**Classic mode** (`--mode classic`): A hardcoded Python loop in `evolution_core.py:main_loop()` orchestrates each generation sequentially — evaluate, plan (Master LLM call), execute workers, review, commit.

**Orchestrator mode** (`--mode orchestrator`, default): An LLM agent (`orchestrator.py`) receives MCP tools and autonomously decides the evolution flow. It calls tools like `run_master()`, `execute_workers()`, `run_quality_gates()` in whatever order it deems optimal. This is more flexible — the Orchestrator can retry, deviate from the standard pipeline, or trigger crossovers as needed.

### The Multi-Agent Evolution Pipeline (per generation)

Each generation follows this flow (both modes, but classic is hardcoded while orchestrator decides dynamically):

1. **Master Architect** (`prompts/master_prompt.md`): Analyzes ratings, experience pool, and current bot code. Produces a JSON task plan with 2 worker assignments — one "Algorithmic Logic Architect" (structural changes) and one "Hyperparameter Tuner" (numeric constants only). Can set `branch_from` to evolve from a different ancestor.

2. **Worker Agents** (`prompts/worker_prompt.md`): Each worker receives a role-constrained prompt. Logic Architects can add functions/refactor; Hyperparameter Tuners can only change numeric constants. Workers directly edit bot source files. Parallel execution attempted first, serial fallback on failure.

3. **Quality Gates** (automated, no LLM): Compile check (`py_compile`), smoke test (1 mirror battle vs bot6), decision tests (predefined scenarios — reject bots that fold AA preflop etc.), file size check (max 1000 lines per file).

4. **Code Reviewer** (`prompts/reviewer_prompt.md`): LLM reviews the diff, enforces role boundaries, scores quality 1-10. Can reject with feedback that triggers worker revision.

5. **Commit**: Git commit + `bot-v{N}` tag. Tags are the authoritative completion proof.

### Key Components

| File | Role |
|---|---|
| `core/evolution_core.py` | All business logic: bot management, LLM orchestration, worker execution, git helpers, main classic loop. ~1600 lines. |
| `core/orchestrator.py` | LLM-driven orchestrator: spawns a Claude agent with MCP tools, streams output, logs to file. |
| `core/tools.py` | MCP tool definitions wrapping evolution_core functions. Registered as `evolution` MCP server via `create_sdk_mcp_server()`. |
| `core/elo_daemon.py` | Background subprocess that continuously runs mirror battles and updates Glicko-2 ratings. Prioritizes under-evaluated pairs. |
| `core/glicko2.py` | Glicko-2 rating system implementation (r, rd, volatility). |
| `core/web_ui.py` | SSE broadcaster + WebUI adapter. Dual output: terminal + dashboard. |
| `core/experience_pool.md` | Accumulated strategic lessons. Trimmed to 8 entries, consolidated by LLM every 3 gens. |
| `core/prompts/*.md` | LLM prompt templates for each agent role (master, worker, reviewer, crossover, orchestrator, initial). |
| `server/app.py` | FastAPI app with lifespan that starts evolution + daemon. Serves React SPA from `server/static/`. |
| `server/state.py` | Thread-safe global state (mode, daemon config, generation counter). |
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
- Pool capped at 30 active bots; weakest culled by conservative rating (r - 2*rd)
- Retired bots moved to `bots/graveyard/`

### Stagnation Handling

- Tracked via `git_get_stagnation_count()` (consecutive non-improving ancestors)
- LLM analyst (`_analyze_stagnation`) determines if stagnation is real or Glicko noise
- At stagnation ≥3: **crossover** between top-2 bots (`prompts/crossover_prompt.md`)
- Master can `branch_from` a different ancestor

## Key Conventions

- All shared files (ratings, stats) use `fcntl` file locking for concurrent access
- Worker role boundaries are enforced by both prompts and reviewer: Logic Architects cannot tune constants, Hyperparameter Tuners cannot add functions
- Max 1000 lines per `.py` file — reviewer rejects oversized files
- Decision test pass rate must be ≥70% (prevents catastrophic regressions)
- Experience pool trimmed to 8 entries, LLM-consolidated every 3 generations
- API rate limit (529) handled with automatic retry + exponential backoff (30s, 60s, 120s)
- Worker failures recorded to `results/worker_failures.jsonl` and injected into future worker prompts
- `claude_agent_sdk` is the LLM interface — not the Anthropic SDK directly

## Dependencies

- Backend: `fastapi`, `uvicorn`, `sse-starlette`, `pydantic` (see `requirements.txt`)
- Frontend: React 19, Vite, TailwindCSS 4, TypeScript (see `frontend/package.json`)
- LLM: `claude_agent_sdk` (provides `query`, `ClaudeAgentOptions`, streaming types, `@tool` decorator, `create_sdk_mcp_server`)
- Battle engine: `engine/battle.py` (mirror_battle, subprocess-based bot execution)
- No external Python dependencies for bots or core engine
