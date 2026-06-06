# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Texas Hold'em poker AI bot self-evolution framework. The system uses a multi-agent LLM pipeline (Master Architect → Worker Agents → Code Reviewer → Critic) to iteratively improve heads-up No-Limit Texas Hold'em bots for the Botzone platform (botzone.org.cn). A background Glicko-2 daemon continuously evaluates bots through mirror battles, and a React + FastAPI dashboard provides real-time monitoring.

The project has three independent poker engines serving different purposes:
- `engine/` — CLI battle runner for local bot testing (subprocess JSON protocol, used by the evolution system)
- `sever/` — TCP competition server for network-based bot matches (git submodule, independent codebase)
- `engine/judge.py` — Stateless judge function used by both `engine/battle.py` and Botzone

## Common Commands

### Evolution System

```bash
python web/main.py                           # Full stack: orchestrator + daemon + frontend on :8000
python web/main.py --no-daemon               # No background daemon
python web/main.py --dev                     # Enable uvicorn auto-reload
python web/main.py --no-build                # Skip frontend build

# Standalone orchestrator CLI (no web server)
python web/core/orchestrator.py              # Continuous evolution
python web/core/orchestrator.py --one-gen    # One generation then stop

# Standalone Glicko-2 daemon
python web/core/elo_daemon.py --workers 28 --pairs 5 -v
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

### Utilities

```bash
python merge_bot.py bots/claude_v49/             # Merge multi-file bot into single file
python merge_bot.py --all                        # Batch merge all bot directories
```

### TCP Competition Server (`sever/`)

```bash
cd sever && python main.py                    # Start TCP :10001 + Web :18080
cd sever && python bot_adapter.py --bot ../bots/claude_v49 --name test  # Bridge bot to TCP server
cd sever && python -m pytest tests/ -v        # All tests (evaluator, validator, game, protocol, integration)
```

## Architecture

### Three-Phase Generation Cycle

Each evolution generation follows a three-phase cycle managed by `generation_scheduler.py`:

1. **Phase 1 — `prepare_generation()`**: Code-layer analysis (stagnation, matches, performance verification). Decides strategy: `master` (evolve from ancestor) or `crossover` (merge two parents). Creates `GenerationContext` with pre-computed data. **Disposable** — safe to re-run on interrupt.
2. **Phase 2 — `_run_one_cycle()` in `orchestrator.py`**: LLM-driven pipeline execution. Orchestrator Claude agent calls MCP tools in sequence. **Preserves state** on interrupt via session + checkpoint files.
3. **Phase 3 — `post_generation_cleanup()`**: Reap weakest bot if pool > 30, consolidate experience pool every 3 gens. **Idempotent** — safe to re-run.

### Per-Generation Pipeline (inside Phase 2)

The Orchestrator LLM calls these MCP tools in order:

1. **Direction Auditor**: Pre-Master LLM gate that checks git history for repetitive evolution directions. Forces structural alternatives if stuck.
2. **Master Architect** (`prompts/master_prompt.md`): Analyzes ratings, experience pool, match data. Produces JSON task plan with 2 worker assignments — one "Algorithmic Logic Architect" (structural changes) and one "Hyperparameter Tuner" (constants only). Can set `branch_from` to evolve from a different ancestor.
3. **Workers** (`prompts/worker_prompt.md`): Execute tasks in parallel (max 3 via semaphore), 4 retries each. Workers directly edit bot source files using Bash/Read/Edit tools.
4. **Quality Gates** (automated, no LLM): `py_compile` check, 1 mirror battle smoke test, decision tests (≥70% pass), file size ≤1500 lines (core strategy files) / ≤1200 lines (helpers).
5. **Code Reviewer** (`prompts/reviewer_prompt.md`): LLM reviews diff, enforces role boundaries, scores 1-10. Up to 3 retries.
6. **Critic** (`prompts/critic_prompt.md`): Independent strategic quality gate. Score ≥6 to approve. Up to 2 intra-generation retries feeding feedback back to workers.
7. **Pre-commit Eval**: Mirror battle regression check vs parent + top opponents.
8. **Commit**: Git commit + `bot-v{N}` annotated tag. Tags are authoritative completion proof.
9. **Archivist**: Snapshot, rotate, and verify old generation files.

### LLM Integration

Uses `claude_agent_sdk` (not the Anthropic SDK directly). Two distinct patterns:

**Pattern 1 — MCP Tool Server (Orchestrator only):**
`orchestrator.py` → `create_sdk_mcp_server()` registers `@tool()` decorated functions from `tool_pipeline.py` + `tool_status.py`. The Orchestrator agent calls these tools (run_master, execute_workers, run_quality_gates, run_review, run_critic, commit_bot, etc.) to drive evolution. Each tool function receives `args` dict, runs business logic (often calling `run_claude_query()` for sub-agents), and returns MCP-formatted results. Session ID persisted for crash recovery (`orchestrator_session.json`). PreCompact hook injects pipeline state to survive LLM context compaction.

**Pattern 2 — Direct `run_claude_query()` (Master, Workers, Reviewer, Critic, Analysts):**
`evolution_infra.py:run_claude_query()` → `llm_query.py:run_claude_query()` sends a prompt + context files to Claude. 700K char prompt budget — context files proportionally compressed when exceeded. Streaming via `AssistantMessage`/`ResultMessage` types. Output captured as text, cost tracked per role. Each agent gets specific tool access: Workers get Bash/Read/Edit, Reviewer/Critic get Bash/Read, Analysts get no tools. API rate limit (529) handled with automatic retry + exponential backoff (30s, 60s, 120s).

**LLM agent roles and their tools:**

| Agent | Tools | Purpose |
|---|---|---|
| Orchestrator | MCP tools only | Drives pipeline, decides evolution flow |
| Master | Bash, Read | Analyzes state, plans worker tasks |
| Workers | Bash, Read, Edit | Modify bot source code |
| Reviewer | Bash, Read | Reviews diff, scores quality |
| Critic | Bash, Read | Strategic assessment, score 1-10 |
| Direction Auditor | None | Detects repetitive evolution directions |
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

- `/api/data/stream` — Periodic SSE pushing dashboard data at 3s/10s/15s intervals.
- `/api/evolution/stream` — Event-driven SSE from `EventBroadcaster` (ring buffer 500 events, per-client asyncio.Queue).
- `/api/control/tool/{name}` — Invokes any MCP tool manually.
- `/api/control/start|stop` — Start/stop the orchestrator loop.
- REST endpoints for ratings, bots, matches, logs, prompts, experience pool, pipeline state.

Shared utilities:
- `server/cache.py` — In-memory 2s TTL cache with `fcntl.LOCK_SH` reads. All route modules share one `_CACHE` dict.
- `server/state.py` — Thread-safe `AppState` singleton (RLock-protected). Manages daemon config, generation counter, orchestrator task reference, decisions log.
- `server/routes/_helpers.py` — Pure data-building functions shared across routes (build_rating_row, build_ranked_ratings, build_match_matrix, etc.).

### Frontend (React 19 + Vite + Tailwind 4)

`DataProvider` context opens a single `EventSource` to `/api/data/stream`. Pages consume typed hooks (`useRatings()`, `useBots()`, etc.) for auto-refreshing data.

| Path | Page | Data source |
|------|------|-------------|
| `/` | Overview | DataProvider hooks |
| `/evolution` | EvolutionMonitor | Own SSE to `/api/evolution/stream` |
| `/matches` | MatchReplay | REST (replay detail) |
| `/rating-trends` | RatingTrends | DataProvider hooks |
| `/match-matrix` | MatchMatrix | DataProvider hooks |
| `/logs` | Logs | REST (log content) |
| `/control` | ControlPanel | REST (control API) |
| `/bots` | BotManager | REST (bot detail, code) |
| `/experience` | ExperiencePool | REST (experience read/write) |
| `/prompts` | PromptEditor | REST (prompt read/write) |

Shared components in `src/components/shared/`: Card, Badge, MetricCard, Skeleton, SegmentedControl, StatusDot, EmptyState. All UI labels are in Chinese.

### Engine (`engine/`)

Local CLI poker battle system for testing bots offline.

**Card protocol:** Integers 0-51. `number = card // 4 + 2` (2-14 = 2-A), `suit = card % 4` (0=♥, 1=♦, 2=♠, 3=♣).

**Bot subprocess protocol:** JSON on stdin/stdout. Input: `{"requests": [...], "responses": [...], "data": ...}`. Output: `{"response": ACTION, "data": ...}`. Actions: `0`=check/call, `-1`=fold, `-2`=all-in, `>0`=raise. 30s timeout per decision.

**Two process modes:** `_PersistentBot` (one Popen per game, line-delimited JSON) for performance, `_call_bot_subprocess()` (fresh process per decision) for debug.

**Battle types:** `battle()` for standard matches, `mirror_battle()` plays each hand twice with swapped hole cards to eliminate luck of the deal.

**Game format:** 50 hands per game, 20000 starting chips, blinds 50/100.

### TCP Server (`sever/` — git submodule)

A self-contained poker competition platform. Bots connect as TCP clients and play 70-hand matches. Has its own engine (`sever/engine/`), validator (13-rule action legality), web dashboard (`:18080`), and test suite.

**Startup & testing:**
```bash
cd sever && python main.py                    # TCP :10001 + Web :18080
cd sever && python -m pytest tests/ -v        # All tests (evaluator, validator, game, protocol, integration)
```

**Structure:** `engine/game.py` (stateful GameEngine), `engine/validator.py` (13-rule validation), `engine/evaluator.py` (hand comparison), `engine/deck.py` (Card class, `<suit,rank>`), `server/tcp_server.py` (async TCP), `server/protocol.py` (message encode/decode), `bot_adapter.py` (bridges `engine/judge.py` bots to TCP server), `web/app.py` (FastAPI + SSE dashboard).

**Protocol differences from `engine/`:**
- Line-delimited text over TCP (not subprocess JSON)
- Card format: `<suit,rank>` tuples where `suit ∈ {0=♠,1=♥,2=♦,3=♣}`, `rank ∈ {0=2..12=A}`
- Actions: text strings (`"call"`, `"fold"`, `"raise 200"`, `"allin"`)
- Stateful `GameEngine` object (not stateless `judge()` function)
- Strict action validation via 13-rule validator (illegal = auto-fold)
- 70 hands per match, 20000 starting chips, blinds 50/100, 60s decision timeout

### CRITICAL: Raise Semantics — Two Different Conventions

The two engines interpret a bot's positive raise value differently:

**`engine/judge.py` (Botzone): raise-as-increment**
- Bot output `>0` = additional chips to add to current bet
- Internally: `round_player_bet[idx] += bet` (adds increment)
- Example: SB (already bet 50) wants to raise to 200 total → bot outputs `150`

**`sever/` TCP server: raise-to-total**
- `raise X` means "raise stage bet TO X" (total amount after raise)
- Internally: `additional = raise_to - player_bets_this_stage[idx]` (derives increment from total)
- Example: SB (already bet 50) wants to raise to 200 → sends `raise 200`

**`bot_adapter.py` bridge:** Connects `engine/judge.py`-style bots to the TCP server. Converts bot integer output directly: `>0` → `raise {value}`. The adapter's comment says `>0 = raise到的金额（真实值）` — it expects the bot to output the total raise-to amount. Bots designed for `engine/judge.py` (which output increments) would produce incorrect results when used with the adapter.

**Minimum raise rules also differ:**

| Rule | `engine/judge.py` | `sever/` |
|------|--------------------|----------|
| Tracking variable | `round_raise` (max increment seen) | `last_raise_to` (last raise-to total) |
| Preflop first raise | total ≥ `round_raise * 2` = 200 | total ≥ 200 (explicit check) |
| Postflop first raise | total ≥ `round_raise * 2` = 100 | total ≥ 100 (explicit check) |
| Re-raise minimum | total ≥ `round_raise * 2` | total ≥ `last_raise_to * 2` |
| Example: raise to 200, re-raise | `round_raise=150`, min total = 300 | `last_raise_to=200`, min total = 400 |

The `sever/` re-raise rule is stricter — it doubles the total raise-to, not the increment.

### `sever/` Game Flow & Rules Summary

**Action order:** Preflop → SB first; Flop/Turn/River → BB first. 70 hands, alternating SB/BB.

**TCP message sequence per hand:**
1. Server sends `name` → client responds with bot name
2. Server sends `preflop|{ROLE}|<s,r><s,r>` → SB acts first
3. Opponent actions forwarded: `call`, `fold`, `check`, `raise X`, `allin`
4. Stage cards: `flop|<s,r><s,r><s,r>`, `turn|<s,r>`, `river|<s,r>` → BB acts first
5. Settlement: `earnChips {amount}` (net change), `oppo_hands|<s,r><s,r>` (showdown only)

**13-rule action validation** (`sever/engine/validator.py`):
1. `bet` always illegal
2. Postflop first action `call` → illegal
3. Preflop BB call after SB call → illegal
4. Postflop non-first action `check` → illegal
5. Preflop check only allowed as BB's first action
6-9. Minimum raise constraints (200 preflop, 100 postflop, 2x for re-raises)
10. Raise exceeding available chips → illegal
11. Raise equaling all chips → must use `allin`
12. After opponent allin → only `call`/`fold`
13. Two consecutive `allin` → second illegal

**Card conversion** (`bot_adapter.py`): Server `<suit,rank>` → Bot integer: `card = rank * 4 + suit`. The formula is mathematically identical to `engine/judge.py`'s `(number-2)*4 + suit`, but the **suit mapping differs**: `engine/judge.py` uses `{♥=0, ♦=1, ♠=2, ♣=3}` while `sever/` uses `{♠=0, ♥=1, ♦=2, ♣=3}`. The same real-world card gets a different integer in each system (e.g., ♠A = 50 in engine vs 48 via adapter). This doesn't break hand evaluation because all cards are converted consistently within a session, but suit-specific bot logic would misinterpret suits.

### Bot Versioning & Conventions

- Bots: `bots/claude_v{N}/` (N monotonically increasing). `.completed` sentinel + `bot-v{N}` git tag.
- Pool capped at 30 active; weakest culled by H2H average win rate to `bots/graveyard/`.
- Botzone game ID: `63dcfaddee1bce5e6c8f4b53`.

### Key Constants (evolution_infra.py)

| Constant | Value | Purpose |
|---|---|---|
| `MAX_ACTIVE_BOTS` | 30 | Pool cap before reaping |
| `MAX_LINES_PER_FILE` | 1500 | LOC limit for core strategy files (strategy.py, postflop.py) |
| `MAX_LINES_HELPER` | 1200 | LOC limit for helper .py files |
| `MIN_DECISION_PASS_RATE` | 0.7 | Decision test threshold |
| `MAX_WORKER_RETRIES` | 4 | Retries per worker |
| `MAX_MASTER_RETRIES` | 3 | Retries for Master plan |
| `WORKER_TIMEOUT` | 1000s | Per-worker LLM call timeout |
| `MAX_PARALLEL_WORKERS` | 3 | Concurrency cap |
| `DAEMON_EVAL_TIMEOUT` | 600s | Wait for sufficient matches |
| `MIN_GAMES_FOR_EVAL` | 100 | Min games for reliable rating |

### Glicko-2 Daemon (`elo_daemon.py`)

Background subprocess continuously running mirror battles. Match selection: 60% under-evaluated pairs + 40% rating-diverse pairs. Per-game Glicko-2 updates (not batch). Writes to all result files with `fcntl` locking. Continuous scheduling via `ProcessPoolExecutor`. Replay files capped at 200. Responds to `.reap_signal` for immediate bot list refresh after commit.

Defaults: `r=1500`, `rd=350`, `sigma=0.06`, `tau=0.5`. Confidence levels: rd<50 green, 50-100 yellow, 100-200 orange, >200 red.

### Process Lifecycle & Recovery

- **ShutdownManager** (`shutdown_manager.py`): Asyncio-native SIGINT/SIGTERM handler. Double-signal kills process. All three generation phases check `shutdown_mgr.is_shutting_down` between operations.
- **Orchestrator session persistence**: `orchestrator_session.json` stores the session ID. On restart, the Orchestrator resumes the exact LLM conversation. Cleared on natural cycle completion.
- **Pipeline checkpoint**: `pipeline_state.json` tracks stage (`prepared` → `direction_audited` → `master_planned` → `workers_done` → `quality_passed` → `reviewed` → `critic_checked` → `verified` → `archived`), gate results, and master plan. Tools enforce stage ordering — `run_review` blocks if quality gates haven't passed, `commit_bot` blocks if any gate is missing.
- **Daemon lifecycle**: `start_daemon()` spawns `elo_daemon.py` as subprocess. `daemon_monitor_thread()` watches for crashes and auto-restarts. Daemon auto-exits on parent death via `getppid()==1` check.
- **Orphan detection**: JSON PID file for daemon process tracking. 5s orphan detection interval.

### Web Core Module Map

| File | Lines | Role |
|---|---|---|
| `elo_daemon.py` | 712 | Background mirror battle subprocess with Glicko-2 updates |
| `evolution_infra.py` | 690 | Constants, git ops, file locking, checkpoints, bot directory, ratings, archiving |
| `orchestrator.py` | 534 | LLM-driven orchestrator loop, three-phase generation cycle |
| `tool_status.py` | 503 | Non-pipeline MCP tools (queries, daemon control, analysis) |
| `tool_helpers.py` | 503 | Shared helpers: UI injection, checkpoint gates, H2H utilities, boundary validation |
| `tool_gates.py` | 398 | Pipeline tools: quality gates, prepare_next_gen, review, critic |
| `tool_commit.py` | 357 | Pipeline tools: commit, archivist, crossover |
| `tool_eval.py` | 344 | Pipeline tools: precommit eval, inline eval |
| `tool_planning.py` | 334 | Pipeline tools: direction audit, master, workers |
| `agent_review.py` | 296 | Critic, Performance Verification, Crossover agents |
| `web_ui.py` | 292 | `EventBroadcaster` (ring buffer 500) + `WebUI` (terminal + SSE dual output) |
| `llm_query.py` | 278 | `run_claude_query()`, `parse_json_output()`, prompt budgeting |
| `orchestrator_context.py` | 241 | Context string builder + PreCompact hook |
| `generation_scheduler.py` | 236 | Three-phase cycle: prepare, run, post-cleanup |
| `agent_workers.py` | 227 | Worker execution: parallel/serial dispatch, timeout isolation, retry logic |
| `glicko2.py` | 222 | Pure Glicko-2 rating algorithm |
| `reset.py` | 222 | Wipe evolution state to baseline |
| `decision_tester.py` | 202 | Predefined scenario tests against bots |
| `daemon_management.py` | 191 | Daemon subprocess lifecycle: start, stop, monitor, orphan detection |
| `agent_master.py` | 187 | Master Architect + match analysis |
| `replay_analysis.py` | 178 | Replay data summarization (pure data, no LLM) |
| `stagnation_analyzer.py` | 171 | Rating trend stagnation analysis via LLM |
| `tool_bot_management.py` | 170 | Bot reaping, cleanup, abandonment, experience pool tools |
| `output_schema.py` | 85 | Pydantic models for validating structured LLM output |
| `experience_archivist.py` | 139 | Experience pool consolidation + archivist analysis |
| `direction_auditor.py` | 127 | Pre-Master repetition detection via LLM |
| `orchestrator_session.py` | 122 | Session persistence, startup recovery, log rotation |
| `commentary.py` | 129 | Deterministic match replay commentary (no LLM) |
| `logging_config.py` | 121 | Structured logging: colored console, rotating file, SSE handler |
| `code_verification.py` | 65 | Compile check, file size, smoke test, decision tests |
| `shutdown_manager.py` | 80 | Asyncio-native SIGINT/SIGTERM handler |
| `tools.py` | 107 | MCP server registration + tool aggregation |
| `tool_pipeline.py` | 6 | Re-export shim: `from tool_planning/gates/eval/commit import *` |
| `evolution_core.py` | 85 | Re-export facade: aggregates all sub-modules for backward compatibility |
| `evolution_core.py` | 68 | Re-export facade for backward compatibility |
| `smoke_tester.py` | 34 | Run 1 mirror match as sanity check |
| `system_log.py` | 33 | Structured JSONL event logger |
| `experience_pool.py` | 40 | Experience pool trim logic (keep under 120 lines) |

### Scripts

| Script | Purpose |
|---|---|
| `scripts/botzone_upload_match.py` | Full Botzone client: upload, rooms, matches, ranking |
| `scripts/botzone_room_series.py` | Batch room matches on Botzone |
| `scripts/botzone_multi_account_upload.py` | Multi-account bot upload |
| `scripts/ref_strategy_labels.py` | Offline strategy analysis / labeling |
| `scripts/reset_evolution.py` | Reset evolution to baseline (keeps v1-v6) |
| `scripts/test_claude_cli.py` | Claude CLI testing utility |

## Key Conventions

- All shared files use `fcntl` file locking for concurrent access between daemon subprocess, orchestrator, and API server
- Worker role boundaries enforced by prompts and reviewer: Logic Architects cannot tune constants, Hyperparameter Tuners cannot add functions
- `_validate_worker_boundaries()` checks edits don't cross role boundaries after each worker run
- Worker failures recorded to `worker_failures.jsonl` and injected into future worker prompts as memory
- Experience pool consolidated by LLM every 3 generations
- `_BLOCKED_MCP_TOOLS` in `evolution_infra.py` blocks external MCP tools from sub-agents
- `_WORKER_SEMAPHORE` (asyncio.Semaphore, max 3) limits concurrent LLM worker calls
- `_PersistentBot` keeps one Popen alive for an entire game (2x battle speedup vs per-decision subprocess)
- Tests use `starlette.testclient.TestClient` with no lifespan (no orchestrator/daemon startup)
- Test naming: `test_routes_*.py` (HTTP endpoints), `test_logic_*.py` (pure functions), `test_mcp_*.py` (MCP tool handlers)

## Post-Task Workflow

After completing each task, you MUST do both of the following:

1. **Git commit and push** all changes:
```bash
git add -A
git commit -m "<descriptive message>"
git push
```

2. **Update memory** in `~/.claude/projects/-Users-zhouzixiang-Documents-pok/memory/`. Save what you learned during the task — surprising findings, user corrections, non-obvious constraints, or validated approaches. Check existing memories first to avoid duplicates; update stale ones rather than creating new ones.
