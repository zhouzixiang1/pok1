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

### Testing

```bash
cd web && python -m pytest tests/ -v              # All backend tests
cd web && python -m pytest tests/test_routes_*.py  # Route endpoint tests only
cd web && python -m pytest tests/test_logic_*.py   # Pure logic tests only
cd web && python -m pytest tests/test_mcp_*.py     # MCP tool tests only
cd web && python -m pytest tests/test_logic_helpers.py -k "test_h2h"  # Single test
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
2. **Workers** (`prompts/worker_prompt.md`): Execute tasks in parallel (max 3 via semaphore), 4 retries each. Workers directly edit bot source files using Bash/Read/Edit tools.
3. **Quality Gates** (automated, no LLM): `py_compile` check, 1 mirror battle smoke test, decision tests (≥70% pass), file size ≤1000 lines.
4. **Code Reviewer** (`prompts/reviewer_prompt.md`): LLM reviews diff, enforces role boundaries, scores 1-10. Up to 3 retries.
5. **Critic** (`prompts/critic_prompt.md`): Independent strategic quality gate. Score ≥6 to approve. Up to 2 intra-generation retries feeding feedback back to workers.
6. **Commit**: Git commit + `bot-v{N}` annotated tag. Tags are authoritative completion proof.

### LLM Integration

Uses `claude_agent_sdk` (not the Anthropic SDK directly). Two distinct patterns:

**Pattern 1 — MCP Tool Server (Orchestrator only):**
`orchestrator.py` → `create_sdk_mcp_server()` registers `@tool()` decorated functions from `tool_pipeline.py` + `tool_status.py`. The Orchestrator agent calls these tools (run_master, execute_workers, run_quality_gates, run_review, run_critic, commit_bot, etc.) to drive evolution. Each tool function receives `args` dict, runs business logic (often calling `run_claude_query()` for sub-agents), and returns MCP-formatted results. Session ID persisted for crash recovery (`orchestrator_session.json`). PreCompact hook injects pipeline state to survive LLM context compaction.

**Pattern 2 — Direct `run_claude_query()` (Master, Workers, Reviewer, Critic, Analysts):**
`evolution_infra.py:run_claude_query()` sends a prompt + context files to Claude. Streaming via `AssistantMessage`/`ResultMessage` types. Output captured as text, cost tracked per role. Each agent gets specific tool access: Workers get Bash/Read/Edit, Reviewer/Critic get Bash/Read, Analysts get no tools. API rate limit (529) handled with automatic retry + exponential backoff (30s, 60s, 120s).

**LLM agent roles and their tools:**

| Agent | Tools | Purpose |
|---|---|---|
| Orchestrator | MCP tools only | Drives pipeline, decides evolution flow |
| Master | Bash, Read | Analyzes state, plans worker tasks |
| Workers | Bash, Read, Edit | Modify bot source code |
| Reviewer | Bash, Read | Reviews diff, scores quality |
| Critic | Bash, Read | Strategic assessment, score 1-10 |
| Stagnation Analyst | None | JSON-only rating trend analysis |
| Match Analyst | None | Analyzes replay summaries |
| Performance Analyst | None | Synthesizes rating/win-rate trends |
| Experience Consolidator | None | Deduplicates experience pool |

### Data Flow

```
Workers edit bots/claude_v{N}/  (LLM-driven code changes)
        ↓
elo_daemon.py  ← Background subprocess, runs mirror battles via engine/battle.py
        ↓           ProcessPoolExecutor, per-game Glicko-2 updates
        ↓
web/core/results/
  ├── glicko_ratings.json    ← Glicko-2 ratings (fcntl-locked, daemon writes)
  ├── rating_history.jsonl   ← Periodic rating snapshots (daemon writes on save cycle)
  ├── head_to_head.json      ← Win/loss matrix per pair (daemon writes)
  ├── bot_stats.json         ← Per-bot aggregated stats (daemon writes)
  ├── match_history.jsonl    ← Match summaries as JSONL (daemon writes per match)
  ├── match_replay/          ← Full replay JSONs (daemon writes, capped at 200)
  ├── pipeline_state.json    ← Pipeline checkpoint for crash recovery (tools write)
  ├── worker_failures.jsonl  ← Worker failure records (agent_workers writes)
  ├── app_config.json        ← Daemon config persisted across restarts (state.py writes)
  └── llm_costs.jsonl        ← Cumulative LLM cost log (WebUI writes)
        ↓
FastAPI backend reads these files (fcntl.LOCK_SH + 2s TTL cache)
        ↓
Two SSE streams push to frontend:
  /api/data/stream      ← Periodic polling (3s/10s/15s intervals): ratings, bots, matches, matrix, history
  /api/evolution/stream ← Event-driven (EventBroadcaster): LLM output, tool calls, cost, status
        ↓
React frontend:
  DataProvider context   ← Subscribes to /api/data/stream, exposes useRatings(), useBots(), etc.
  EvolutionMonitor page  ← Owns separate SSE to /api/evolution/stream for real-time LLM streaming
  Other pages            ← REST calls for page-specific data (replay details, log content, prompts)
```

### Backend (FastAPI)

Entry point: `web/main.py` → `web/server/app.py`. Nine route modules in `server/routes/`:

- `/api/data/stream` — Periodic SSE pushing dashboard data (ratings, bots, matches, history, etc.) at 3s/10s/15s intervals. Frontend's `DataProvider` context subscribes to this.
- `/api/evolution/stream` — Event-driven SSE from `EventBroadcaster` (ring buffer 500 events, per-client asyncio.Queue). Used by EvolutionMonitor for real-time LLM output streaming. Events: `history`, `status`, `io`, `clear_io`, `eval_table`, `daemon`, `header`, `cost`, `metrics`, `tool_call`.
- `/api/control/tool/{name}` — Invokes any of the MCP tools manually via the tool map. Records decisions in `app_state`.
- `/api/control/start|stop` — Start/stop the orchestrator loop as asyncio tasks.
- REST endpoints for ratings, bots, matches, logs, prompts, experience pool, pipeline state.

All shared file reads use `fcntl.LOCK_SH` (shared) / `fcntl.LOCK_EX` (exclusive) with a 2-second TTL cache (`server/cache.py`).

### Frontend (React 19 + Vite + Tailwind 4)

`DataProvider` context in `App.tsx` opens a single `EventSource` to `/api/data/stream`. Pages consume typed hooks (`useRatings()`, `useBots()`, etc.) for auto-refreshing data. Page-specific data (replay details, log content, prompt editing) uses direct REST calls via `api/client.ts`. EvolutionMonitor has its own dedicated SSE connection to `/api/evolution/stream` for real-time LLM streaming.

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

### Glicko-2 Daemon (`elo_daemon.py`)

Background subprocess continuously running mirror battles. Match selection: 60% under-evaluated pairs + 40% rating-diverse pairs. Per-game Glicko-2 updates (not batch). Writes to all result files with `fcntl` locking. Continuous scheduling via `ProcessPoolExecutor` — replenishes match queue when empty. Replay files capped at 200. Responds to `.reap_signal` for immediate bot list refresh after commit.

Defaults: `r=1500`, `rd=350`, `sigma=0.06`, `tau=0.5`. Confidence levels: rd<50 green, 50-100 yellow, 100-200 orange, >200 red.

### Process Recovery

The system survives crashes via two mechanisms:
- **Orchestrator session persistence**: `orchestrator_session.json` stores the session ID. On restart, the Orchestrator resumes the exact LLM conversation. Cleared on natural cycle completion.
- **Pipeline checkpoint**: `pipeline_state.json` tracks stage (`prepared` → `workers_done` → `quality_passed` → `reviewed` → `critic_checked` → `verified`), gate results, and master plan. Tools enforce stage ordering — `run_review` blocks if quality gates haven't passed, `commit_bot` blocks if any gate is missing.

## Post-Task Workflow

After completing each task, you MUST do both of the following:

1. **Git commit and push** all changes:
```bash
git add -A
git commit -m "<descriptive message>"
git push
```

2. **Update memory** in `~/.claude/projects/-Users-zhouzixiang-Documents-pok/memory/`. Save what you learned during the task — surprising findings, user corrections, non-obvious constraints, or validated approaches. Check existing memories first to avoid duplicates; update stale ones rather than creating new ones.
