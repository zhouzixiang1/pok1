# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Texas Hold'em poker AI bot self-evolution framework. The system uses a multi-agent LLM pipeline (Master Architect → Worker Agents → Code Reviewer → Critic) to iteratively improve heads-up No-Limit Texas Hold'em bots for the Botzone platform (botzone.org.cn). A background Glicko-2 daemon continuously evaluates bots through mirror battles, and a React + FastAPI dashboard provides real-time monitoring.

## Common Commands

### Evolution System

```bash
python web/main.py                           # Orchestrator mode + daemon + frontend on :8000
python web/main.py --no-daemon               # No background daemon
python web/main.py --dev                     # Enable uvicorn auto-reload
python web/main.py --no-build                # Skip frontend build

# Standalone orchestrator CLI (no web server)
python web/core/orchestrator.py              # Continuous evolution
python web/core/orchestrator.py --one-gen    # One generation then stop

# Standalone Glicko-2 daemon
python web/core/elo_daemon.py --workers 14 --pairs 5 -v
```

### Frontend

```bash
cd web/frontend && npm run dev    # Vite dev server on :5173, proxies /api to :8000
cd web/frontend && npm run build  # tsc + vite build, copies to web/server/static/
```

### Local Bot Battles

```bash
python engine/battle.py bots/bot5/main.py bots/bot4/main.py -n 50 -v -d  # -n games, -v verbose, -d debug
python engine/ladder.py -v                                                # Round-robin ELO tournament
python engine/ladder.py -b 1 4 7 -n 20 -j 4                              # Specific bots, 4 workers
python engine/anchor_runner.py 5 -n 100 -j 24                            # One bot vs all others
```

### Botzone

```bash
python scripts/botzone_upload_match.py upload --source bots/bot5/main.py --bot-name test --execute
python scripts/botzone_upload_match.py rank-match --bot-name test --execute
```

Credentials via `BOTZONE_EMAIL` / `BOTZONE_PASSWORD` env vars.

## Architecture

### Orchestrator-Driven Evolution

`orchestrator.py` spawns a Claude agent with MCP tools and lets it autonomously decide the evolution flow. Uses `claude_agent_sdk` with `create_sdk_mcp_server()` registering tools from `tools.py`. Business logic is split into focused modules: `evolution_infra.py` (constants, git, daemon, ratings, `run_claude_query()`), `agent_master.py` (Master + analysis), `agent_workers.py` (worker execution), `agent_review.py` (Critic, Verification, Crossover). MCP tools are in `tool_pipeline.py` and `tool_status.py`. `evolution_core.py` and `tools.py` remain as re-export shells for backward compatibility.

### Per-Generation Pipeline

1. **Master Architect** (`prompts/master_prompt.md`): Analyzes ratings, experience pool, match data. Produces JSON task plan with 2 worker assignments — one "Algorithmic Logic Architect" (structural changes) and one "Hyperparameter Tuner" (constants only). Can set `branch_from` to evolve from a different ancestor.
2. **Workers** (`prompts/worker_prompt.md`): Execute tasks in parallel (max 3 via semaphore), 4 retries each. Workers directly edit bot source files.
3. **Quality Gates** (automated): `py_compile` check, 1 mirror battle smoke test, decision tests (≥70% pass), file size ≤1000 lines.
4. **Code Reviewer** (`prompts/reviewer_prompt.md`): LLM reviews diff, enforces role boundaries, scores 1-10. Up to 3 retries.
5. **Critic** (`prompts/critic_prompt.md`): Independent strategic quality gate. Score ≥6 to approve. Up to 2 intra-generation retries feeding feedback back to workers.
6. **Commit**: Git commit + `bot-v{N}` annotated tag. Tags are authoritative completion proof.

### Data Flow

```
Workers edit bots/claude_v{N}/
  → elo_daemon.py runs mirror battles via engine/battle.py
  → results/glicko_ratings.json (fcntl-locked)
  → results/rating_history.jsonl, match_history.jsonl, match_replay/
  → Master reads ratings + experience_pool.md → plans next generation
```

### Backend (FastAPI)

Entry point: `web/main.py` → `web/server/app.py`. Nine route modules in `server/routes/`:

- `/api/data/stream` — Periodic SSE pushing dashboard data (ratings, bots, matches, history, etc.) at 3s/10s/15s intervals. Frontend's `DataProvider` context subscribes to this.
- `/api/evolution/stream` — Event-driven SSE from `EventBroadcaster` (ring buffer 500 events, per-client asyncio.Queue). Used by EvolutionMonitor for real-time LLM output streaming. Events: `history`, `status`, `io`, `clear_io`, `eval_table`, `daemon`, `header`, `cost`, `metrics`, `tool_call`.
- `/api/control/tool/{name}` — Invokes any of the MCP tools manually. Records decisions in `app_state`.
- REST endpoints for ratings, bots, matches, logs, prompts, experience pool, pipeline state.

All shared file reads use `fcntl.LOCK_SH` (shared) / `fcntl.LOCK_EX` (exclusive) with a 2-second TTL cache.

### Frontend (React 19 + Vite + Tailwind 4)

`DataProvider` context in `App.tsx` opens a single `EventSource` to `/api/data/stream`. Pages consume typed hooks (`useRatings()`, `useBots()`, etc.) for auto-refreshing data. Page-specific data (replay details, log content, prompt editing) uses direct REST calls. EvolutionMonitor has its own dedicated SSE connection to `/api/evolution/stream` for real-time LLM streaming.

### Bot Versioning & Conventions

- Bots: `bots/claude_v{N}/` (N monotonically increasing). `.completed` sentinel + `bot-v{N}` git tag.
- Pool capped at 30 active; weakest culled by H2H average win rate to `bots/graveyard/`.
- Cards: integers 0-51. `number = card // 4 + 2` (2-14 = 2-A), `suit = card % 4` (0=♥, 1=♦, 2=♠, 3=♣).
- Bot protocol: JSON on stdin/stdout. `0`=check/call, `-1`=fold, `-2`=all-in, `>0`=raise. 30s timeout per decision.
- Each game = 50 hands, 20000 chips, blinds 50/100. Botzone game ID: `63dcfaddee1bce5e6c8f4b53`.

### Key Constants (evolution_infra.py)

| Constant | Value | Purpose |
|---|---|---|
| `MAX_ACTIVE_BOTS` | 30 | Pool cap before reaping |
| `MAX_LINES_PER_FILE` | 1000 | LOC limit per .py file |
| `MIN_DECISION_PASS_RATE` | 0.7 | Decision test threshold |
| `MAX_WORKER_RETRIES` | 4 | Retries per worker |
| `MAX_MASTER_RETRIES` | 3 | Retries for Master plan |
| `WORKER_TIMEOUT` | 1000s | Per-worker LLM call timeout |
| `MAX_PARALLEL_WORKERS` | 3 | Concurrency cap |
| `DAEMON_EVAL_TIMEOUT` | 600s | Wait for sufficient matches |
| `MIN_GAMES_FOR_EVAL` | 100 | Min games for reliable rating |

### LLM Integration

Uses `claude_agent_sdk` (not the Anthropic SDK directly). Two patterns:
- **Direct `claude_query()`** for Master, Workers, Reviewer, Critic — streaming via `AssistantMessage`/`ResultMessage`.
- **MCP tool server** for Orchestrator — `create_sdk_mcp_server()` registers `@tool()` decorated functions from `tools.py`.

API rate limit (529) handled with automatic retry + exponential backoff (30s, 60s, 120s).

### Glicko-2 Daemon (`elo_daemon.py`)

Background subprocess continuously running mirror battles. Match selection: 60% under-evaluated pairs + 40% rating-diverse pairs. Batch-updates ratings after each period. Writes to `glicko_ratings.json` with `fcntl` locking. Replay files capped at 200.

Defaults: `r=1500`, `rd=350`, `sigma=0.06`, `tau=0.5`. Confidence levels: rd<50 green, 50-100 yellow, 100-200 orange, >200 red.

## Post-Task Workflow

After completing each task, always commit and push changes:
```bash
git add -A
git commit -m "<descriptive message>"
git push
```
