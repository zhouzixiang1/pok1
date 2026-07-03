# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Texas Hold'em poker AI bot framework that started as a Botzone evolution project and now has four important code paths. Do not collapse them into one mental model:

1. `engine/` — local Botzone-style subprocess battle engine for Python bots.
2. `web/` — unified evolution system, FastAPI backend, and React dashboard.
3. `sever/` — national competition TCP self-play platform based on the documents in `sever/国赛平台/`.
4. `rl/` — reinforcement learning experiments that wrap the local Hold'em engine.

There are two protocol families:

- Botzone/local protocol: bots are Python subprocesses that read JSON from stdin and write JSON to stdout. This is implemented by `engine/` and used by most `bots/claude_v*/` code and the evolution system.
- National competition protocol: AI engines connect to a TCP server and exchange line-delimited strings such as `preflop|SMALLBLIND|<0,3><1,3>`, `raise 200`, `call`, `check`, `fold`, `allin`. This is implemented by `sever/`.

The evolution pipeline lives under `web/core/`. It uses LLM agents, Glicko-2 ratings, mirror battles, quality gates, precommit regression evaluation, and accumulated strategy lessons to generate new bot versions.

The old `web/tui.py` Textual TUI no longer exists. Treat `web/main.py` as a web app launcher, not a TUI or mode-switching CLI.

Additional modules:
- `rl/` — Reinforcement learning training framework (DanLM-inspired DMC self-play). Wraps `engine/judge.py` as a Gymnasium environment, supports MLP and Transformer Q-networks.
- `docs/` — Design documents and analysis reports (RL design, pipeline bottleneck analysis, LLM stages, etc.)
- `ref/` — Reference implementations: DanLM (token-based card game RL, Transformer + DMC self-play), neuron_poker (Gym-based Hold'em with DQN/equity agents), Botzone platform API docs (`player_api.js`, `TexasHoldem2p.html`).
- `archive/` — Deprecated code (old dashboard, orchestrator, evolution_workspace).

Top-level documentation:
- `AGENTS.md` — AI agent onboarding context
- `ONBOARDING.md` — Teammate usage guide
- `SETUP_GUIDE.md` — Remote deployment tutorial

## Common Commands

### Service Management

```bash
./pokctl.sh start                    # Start web service (default port 8000)
./pokctl.sh start --port 3000        # Start on custom port
./pokctl.sh start --no-build         # Skip frontend build
./pokctl.sh stop                     # Stop service
./pokctl.sh status                   # Check service status
./pokctl.sh restart                  # Restart service
./pokctl.sh logs                     # Tail stdout log
./pokctl.sh logs web/logs/app.log    # Tail app log
```

### Evolution System

```bash
python web/main.py                           # Full stack: orchestrator + daemon + frontend on :8000
python web/main.py --port 3000               # Custom port
python web/main.py --no-daemon               # No background daemon
python web/main.py --dev                     # Enable uvicorn auto-reload
python web/main.py --no-build                # Skip frontend build

# Standalone orchestrator CLI (no web server)
python web/core/orchestrator.py              # Continuous evolution
python web/core/orchestrator.py --one-gen    # One generation then stop

# Standalone Glicko-2 daemon
python web/core/elo_daemon.py --workers 12 --pairs 5 -v
```

### Testing

```bash
cd web && python -m pytest tests/ -v              # All backend tests
cd web && python -m pytest tests/test_routes_*.py  # Route endpoint tests only
cd web && python -m pytest tests/test_logic_*.py   # Pure logic tests only
cd web && python -m pytest tests/test_mcp_*.py     # MCP tool tests only
cd web && python -m pytest tests/test_logic_helpers.py -k "test_h2h"  # Single test
python -m pytest sever/tests -q                   # National TCP protocol/adapter tests
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

### Reinforcement Learning

```bash
python -m rl.scripts.train                                # Train MLP Q-network (default)
python -m rl.scripts.train --model transformer            # Train Transformer Q-network
python -m rl.scripts.evaluate --checkpoint rl/checkpoints/best_model.pt
python engine/battle.py bots/bot5/main.py rl/scripts/rl_bot.py -n 50 -v  # Test RL bot
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
cd sever && python main.py --tcp-port 20001 --web-port 28080
cd sever && python test_client.py 127.0.0.1 10001 BotA
cd sever && python test_client.py 127.0.0.1 10001 BotB
cd sever && python bot_adapter.py --bot ../bots/claude_v224 --name test  # Bridge bot to TCP server
python -m pytest sever/tests -q               # Protocol regression suite
```

## Architecture

### Three-Phase Generation Cycle

Each evolution generation follows a three-phase cycle managed by `generation_scheduler.py`:

1. **Phase 1 — `prepare_generation()`**: Code-layer analysis (stagnation + performance verification via `combined_analyst.py`). Decides the source version, target version, and strategy before the Master prompt runs. **Disposable** — safe to re-run on interrupt.
2. **Phase 2 — `_run_one_cycle()` in `orchestrator.py`**: LLM-driven pipeline execution. Orchestrator Claude agent calls MCP tools in sequence. **Preserves state** on interrupt via session + checkpoint files.
3. **Phase 3 — `post_generation_cleanup()`**: Runs only for committed/tagged generations. Reaps weakest bot if pool > 30, consolidates experience every 3 gens or when `RECENT_LESSONS` is crowded, and launches post-commit probes/fingerprints. **Idempotent** — safe to re-run.

### Per-Generation Pipeline (inside Phase 2)

The Orchestrator LLM calls these MCP tools in order:

1. **Prepare/Crossover**: Use `prepare_next_gen` first for normal Master/Worker generations so the target bot dir and `prepared` checkpoint exist. For crossover generations, use `run_crossover` as the alternative setup path.
2. **Direction Auditor**: Pre-Master LLM gate that checks git history for repetitive evolution directions. Forces structural alternatives if stuck.
3. **Optional Literature Probe**: When stagnation or repetition is detected, run `run_literature_probe` before Master so web-derived strategy hypotheses are explicit and governed.
4. **Master Architect** (`prompts/master_prompt.md`): Analyzes ratings, experience pool, match data, and the pre-selected source version. Produces JSON task plan with worker assignments. It must not set `branch_from` or source-override fields; source selection is handled before Master planning.
5. **Workers** (`prompts/worker_prompt.md`): Execute tasks in parallel (max 3 via semaphore), 4 retries each. Workers directly edit bot source files using Bash/Read/Edit tools.
6. **Quality Gates** (automated, no LLM): `py_compile`, runtime import contract, smoke test, decision tests (≥70% pass), national TCP protocol/adapter regression tests, declared-scope/protected-contract checks, mandatory fix verification, telemetry/reachability checks, file size ≤2000 lines (core strategy files) / ≤1500 lines (helpers), adaptive limit based on source bot size + 15% growth budget, hard cap 2500.
7. **Code Reviewer** (`prompts/reviewer_prompt.md`): LLM reviews diff, enforces role boundaries, scores 1-10. Up to 3 retries.
8. **Critic** (`prompts/critic_prompt.md`): Independent strategic review. It records strategic risk and can feed feedback into retries, but the current pipeline treats precommit evaluation as the final regression gate.
9. **Pre-commit Eval**: Mirror battle regression check vs parent + top opponents.
10. **Commit**: Git commit + `bot-v{N}` annotated tag. Tags are authoritative completion proof.
11. **Archivist**: Snapshot, rotate, and verify old generation files.

### LLM Integration

Uses `claude_agent_sdk` (not the Anthropic SDK directly). Two distinct patterns:

**Pattern 1 — MCP Tool Server (Orchestrator only):**
`orchestrator.py` → `tools.py` → `create_sdk_mcp_server()` registers the 17 Orchestrator-visible MCP tools from `tool_planning.py`, `tool_gates.py`, `tool_eval.py`, `tool_commit.py`, `tool_bot_management.py`, and query helpers from `tool_status.py`. `get_status` and maintenance helpers remain HTTP/control-only in `all_tools`; they are not callable from the Orchestrator MCP session. Each tool function receives `args` dict, runs business logic (often calling `run_claude_query()` for sub-agents), and returns MCP-formatted results. Session ID persisted for crash recovery (`orchestrator_session.json`). PreCompact hook injects pipeline state to survive LLM context compaction.

**Pattern 2 — Direct `run_claude_query()` (Master, Workers, Reviewer, Critic, Analysts):**
`evolution_infra.py:run_claude_query()` → `llm_query.py:run_claude_query()` sends a prompt + context files to Claude. 700K char prompt budget (`MAX_PROMPT_CHARS`) — context files proportionally compressed when exceeded. Streaming via `AssistantMessage`/`ResultMessage` types. Output captured as text, cost tracked per role. Each agent gets specific tool access: Workers get Bash/Read/Edit, Reviewer/Critic get Bash/Read, Analysts get no tools. API rate limit (529) handled with automatic retry + exponential backoff (30s, 60s, 120s).

`run_claude_query()` sets the Claude Code working directory to the repository
root. Claude Code therefore auto-loads this root `CLAUDE.md` for direct
sub-agent calls. Keep this root file authoritative for protocol boundaries,
national TCP compatibility via `sever/bot_adapter.py`, and evolution workflow
facts; `web/CLAUDE.md` and `sever/CLAUDE.md` are directory-specific supplements,
not replacements for the auto-loaded root guidance. Direct sub-agent calls also
pass an empty strict MCP config, so they can use only their explicit `tools`
list and do not auto-start user/global MCP servers.

**LLM agent roles and their tools:**

| Agent | Tools | Purpose |
|---|---|---|
| Orchestrator | MCP tools only | Drives pipeline, decides evolution flow |
| Master | Bash, Read | Analyzes state, plans worker tasks |
| Workers | Bash, Read, Edit | Modify bot source code |
| Reviewer | Bash, Read | Reviews diff, scores quality |
| Critic | Bash, Read | Strategic assessment, score 1-10 |
| Direction Auditor | None | Detects repetitive evolution directions |
| Combined Analyst | None | Merged stagnation detection + performance verification (single LLM call) |
| Match Analyst | None | Analyzes replay summaries |
| Experience Consolidator | None | Deduplicates experience pool |

Note: Stagnation Analyst and Performance Analyst have been merged into `combined_analyst.py` (single LLM call). The separate `stagnation_analyzer.py` still exists but is called via `combined_analyst`.

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
  ├── match_replay/          ← Full replay JSONs (daemon writes, capped at 2000)
  ├── commentary/            ← Match replay commentary JSONs (commentary.py writes)
  ├── worker_failures.jsonl  ← Worker failure records (agent_workers writes)
  ├── app_config.json        ← Daemon config persisted across restarts (state.py writes)
  ├── llm_costs.jsonl        ← Cumulative LLM cost log (WebUI writes)
  ├── system_events.jsonl    ← Structured event log (system_log.py writes)
  ├── elo_daemon_stats.json  ← Daemon performance statistics (daemon writes)
  ├── priority_eval.json     ← Priority evaluation queue (daemon reads/writes)
  ├── daemon_crash.log       ← Daemon crash log (daemon writes on error)
  ├── .daemon_pid            ← Daemon PID tracking file (daemon_management writes)
  └── archive/               ← Archived generation files (archivist writes)
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
- `/api/control/status` — Orchestrator status query.
- `/api/control/config` (GET/PUT) — Daemon configuration.
- `/api/control/decisions` — Evolution decisions log.
- `/api/control/tools` — List available MCP tools.
- `/api/control/orchestrator/session` (GET/DELETE) — Session management.
- `/api/control/reset` — Reset evolution state.
- `/api/daemon/status` — Daemon process status.
- `/api/evolution/state` — Current evolution state.
- REST endpoints for ratings, bots, matches, logs, prompts, experience pool, pipeline state.

Shared utilities:
- `server/cache.py` — In-memory 2s TTL cache with `fcntl.LOCK_SH` reads. All route modules share one `_CACHE` dict.
- `server/state.py` — Thread-safe `AppState` singleton (RLock-protected). Manages daemon config, generation counter, orchestrator task reference, decisions log.
- `server/routes/_helpers.py` — Pure data-building functions shared across routes (build_rating_row, build_ranked_ratings, build_match_matrix, etc.).

### Frontend (React 19 + Vite 6 + Tailwind 4)

`DataProvider` context opens a single `EventSource` to `/api/data/stream`. Pages consume typed hooks (`useRatings()`, `useBots()`, `useMatchStats()`, `useDaemonStatus()`, etc.) for auto-refreshing data.

| Path | Page | Data source |
|------|------|-------------|
| `/` | Overview | DataProvider hooks |
| `/evolution` | EvolutionMonitor | Own SSE to `/api/evolution/stream` |
| `/matches` | MatchReplay | REST (replay detail, commentary) |
| `/rating-trends` | RatingTrends | DataProvider hooks |
| `/match-matrix` | MatchMatrix | DataProvider hooks |
| `/logs` | Logs | REST (log content, system events, worker failures) |
| `/control` | ControlPanel | REST (control API, daemon config) |
| `/bots` | BotManager | REST (bot detail, code) |
| `/experience` | ExperiencePool | REST (experience read/write) |
| `/prompts` | PromptEditor | REST (prompt read/write) |

Shared components in `src/components/shared/`: Card, CardHeader, Badge, MetricCard, Skeleton, SegmentedControl, StatusDot, EmptyState. All UI labels are in Chinese.

Feature components:
- `components/evolution/` — CostBreakdown, PipelineStatus, ToolCard, WorkerProgress, icons (used by EvolutionMonitor)
- `components/logs/` — SystemLogTab, WorkerFailuresTab (used by Logs page)
- `components/common/` — PageMeta, ScrollToTop, ThemeToggleButton
- `components/PokerTable.tsx` — Visual poker table (used by MatchReplay)

Infrastructure:
- `context/DataProvider.tsx` — SSE data subscriptions, `context/SidebarContext.tsx` — sidebar state, `context/ThemeContext.tsx` — dark/light theme toggle
- `api/client.ts` — REST client (30s timeout), `api/control.ts` — orchestrator control API, `api/evolution.ts` — SSE hook + state fetch, `api/types.ts` — TypeScript type definitions
- `hooks/useGoBack.ts`, `hooks/useModal.ts` — navigation and UI utility hooks
- `lib/utils.ts` — `cn()` utility (clsx + tailwind-merge)
- `constants/pipeline.ts` — `PIPELINE_STAGES` and `STAGE_LABELS` arrays

Dependencies: react ^19, react-router ^7.1.5, vite ^6.1.0, tailwindcss ^4.0.8, apexcharts ^4.1.0, react-apexcharts ^1.7.0, react-helmet-async ^2.0.5, clsx ^2.1.1, tailwind-merge ^3.0.1.

### Engine (`engine/`)

Local CLI poker battle system for testing bots offline.

**Card protocol:** Integers 0-51. `number = card // 4 + 2` (2-14 = 2-A), `suit = card % 4` (0=♥, 1=♦, 2=♠, 3=♣).

**Bot subprocess protocol:** JSON on stdin/stdout. Input: `{"requests": [...], "responses": [...], "data": ...}`. Output: `{"response": ACTION, "data": ...}`. Actions: `0`=check/call, `-1`=fold, `-2`=all-in, `>0`=raise-to-total. 60s timeout per decision.

**Two process modes:** `_PersistentBot` (one Popen per game, line-delimited JSON) for performance, `_call_bot_subprocess()` (fresh process per decision) for debug. Unified dispatcher: `_call_bot()` selects between modes.

**Battle types:** `battle()` for standard matches, `mirror_battle()` plays each hand twice with swapped hole cards to eliminate luck of the deal. `battle_generator()` yields event dicts for step-by-step consumption. `human_battle_generator()` for human-vs-bot interactive play.

**Game format:** 70 hands per game (`DEFAULT_N_HANDS = 70`), 20000 starting chips (`INITIAL_CHIPS = 20000`), blinds 50/100.

**Note:** `web/core/engine/` contains a copy of `battle.py` and a slightly modified `judge.py` (with postflop check validation). Both are imported by the same top-level `engine/` package via Python path.

### TCP Server (`sever/`)

A self-contained poker competition platform. Bots connect as TCP clients and play 70-hand matches. It has its own engine (`sever/engine/`), validator (13-rule action legality), web dashboard (`:18080`), smoke clients, and a `sever/tests/` national-alignment pytest suite.

**Startup & smoke testing:**
```bash
cd sever && python main.py                    # TCP :10001 + Web :18080
cd sever && python test_client.py 127.0.0.1 10001 BotA
cd sever && python test_client.py 127.0.0.1 10001 BotB
python -m pytest sever/tests -q
```

**Structure:** `engine/game.py` (stateful GameEngine), `engine/validator.py` (13-rule validation), `engine/evaluator.py` (hand comparison), `engine/deck.py` (Card class, `<suit,rank>`), `server/tcp_server.py` (async TCP), `server/protocol.py` (message encode/decode), `bot_adapter.py` (bridges `engine/judge.py` bots to TCP server), `web/app.py` (FastAPI + SSE dashboard).

**Protocol differences from `engine/`:**
- Line-delimited text over TCP (not subprocess JSON)
- Card format: `<suit,rank>` tuples where `suit ∈ {0=♠,1=♥,2=♦,3=♣}`, `rank ∈ {0=2..12=A}`
- Actions: exact text strings (`"call"`, `"check"`, `"fold"`, `"allin"`, `"raise 200"`). `raise` uses exactly one space before the amount; `bet` is always illegal.
- Stateful `GameEngine` object (not stateless `judge()` function)
- Strict action validation via 13-rule validator (illegal = auto-fold)
- 70 hands per match, 20000 starting chips, blinds 50/100, 60s decision timeout
- A match starts automatically after the second client connects; `/api/start` is retained as a dashboard fallback and rejects duplicate running tasks.

### CRITICAL: Raise Semantics — Both Engines Use Raise-to-Total

Both `engine/judge.py` and `sever/` use the same raise-to-total convention:

**`engine/judge.py`: raise-to-total**
- Bot output `>0` = the total stage bet amount to raise TO (not the increment)
- Internally: `raise_to = bet; additional = raise_to - current_bet` (derives increment from total)
- Tracking variable: `last_raise_to` (last raise-to total)
- Example: SB (already bet 50) wants to raise to 200 total → bot outputs `200`

**`sever/` TCP server: raise-to-total**
- `raise X` means "raise stage bet TO X" (total amount after raise)
- Internally: `additional = raise_to - player_bets_this_stage[idx]` (derives increment from total)
- Example: SB (already bet 50) wants to raise to 200 → sends `raise 200`

**Minimum raise rules (both engines):**

| Rule | `engine/judge.py` | `sever/` |
|------|--------------------|----------|
| Tracking variable | `last_raise_to` (last raise-to total) | `last_raise_to` (last raise-to total) |
| Preflop first raise | total ≥ 200 (derived from `big_blind`) | total ≥ 200 (explicit check) |
| Postflop first raise | total ≥ 100 (derived from `big_blind // 2`) | total ≥ 100 (explicit check) |
| Re-raise minimum | total > `last_raise_to * 2` (strictly greater) | total > `last_raise_to * 2` (strictly greater) |

**Re-raise boundary clarification**: "一倍以上" in 非法行为说明.docx means strictly >2x, NOT >=2x. The 补充说明.docx example uses raise 400 → raise 801 (not 800), confirming the strictly-greater interpretation. E.g., after raise 400, minimum re-raise is 801.

**`bot_adapter.py` bridge:** Converts bot integer output directly for actions: `>0` → `raise {value}`. Since both engines use raise-to-total, the action amount does not need delta conversion.

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
4. Postflop non-first action `check` → illegal; after a first-player postflop `check`, the second player must send `call` to pass the street
5. Preflop check only allowed as BB's first action
6-9. Minimum raise constraints (200 preflop, 100 postflop, 2x for re-raises)
10. Raise exceeding available chips → illegal
11. Raise equaling all chips → must use `allin`
12. After opponent allin → only `call`/`fold`
13. Two consecutive `allin` → second illegal

After `allin` is called, the server runs out remaining public cards, records them in THP, and clients must not act again until `earnChips`.

**Card conversion** (`bot_adapter.py`): Server `<suit,rank>` must be mapped to the local bot integer suit order. TCP uses `{♠=0, ♥=1, ♦=2, ♣=3}` while `engine/judge.py` uses `{♥=0, ♦=1, ♠=2, ♣=3}`. The adapter maps TCP suits through `_TCP_TO_JUDGE_SUIT` before computing `rank * 4 + judge_suit`; do not reuse the TCP suit directly.

### Bot Versioning & Conventions

- Bots: `bots/claude_v{N}/` (N monotonically increasing). Treat active evolved versions as non-continuous: the highest `claude_v*` directory is not necessarily completed, tagged, or committed.
- Evolution-generated bot versions are complete only when the orchestrator `commit_bot` flow has passed gates, committed the bot, and created the annotated `bot-v{N}` tag.
- Pool capped at 30 active; weakest culled by conservative Glicko score (`r - 2*rd`) to `bots/graveyard/`, with sample-starved bots protected until the hard overflow rules apply. H2H average win rate is reported as context, not the primary reap key.
- Source selection is owned by `generation_scheduler._decide_strategy`. LLM `recommended_source` and `branch_from` suggestions are accepted only for active, completion-backed bots (`.completed` plus `bot-v{N}` tag); rejected suggestions emit `pipeline.source_selection_rejected`.
- Reap logs and tool results include `selection_key=conservative_glicko`, `conservative_rating`, `leaderboard_score`, and `h2h_avg_wr` so the actual cull key is distinguishable from contextual H2H/leaderboard evidence.
- Botzone game ID: `63dcfaddee1bce5e6c8f4b53`.

### Key Constants (evolution_infra.py)

| Constant | Value | Purpose |
|---|---|---|
| `MAX_ACTIVE_BOTS` | 30 | Pool cap before reaping |
| `MAX_LINES_PER_FILE` | 2000 | LOC limit for core strategy files (`CORE_STRATEGY_FILES = {'strategy.py', 'postflop.py'}`) — base limit, adaptive from source |
| `MAX_LINES_HELPER` | 1500 | LOC limit for helper .py files — base limit |
| `MAX_LINES_HARD_CAP` | 2500 | Hard cap: no .py file may exceed this even with adaptive budget |
| `LINE_GROWTH_BUDGET` | 0.15 | Adaptive limit = max(base, source_lines × 1.15) |
| `MIN_DECISION_PASS_RATE` | 0.7 | Decision test threshold |
| `MAX_WORKER_RETRIES` | 4 | Retries per worker |
| `MAX_MASTER_RETRIES` | 3 | Retries for Master plan |
| `WORKER_TIMEOUT` | 1000s | Per-worker LLM call timeout |
| `MAX_PARALLEL_WORKERS` | 3 | Concurrency cap |
| `DAEMON_EVAL_TIMEOUT` | 600s | Wait for sufficient matches |
| `MIN_GAMES_FOR_EVAL` | 100 | Min games for reliable rating |
| `MAX_PROMPT_CHARS` | 700,000 | Max prompt size for LLM calls |
| `EVAL_RD_THRESHOLD` | 60 | RD threshold for confidence-based early exit |
| `EVAL_RD_MIN_GAMES` | 20 | Min games for confidence-based early exit |
| `MIN_CROSSOVER_DECISION_RATE` | 0.6 | Min decision pass rate for crossover candidates |
| `MAX_CROSSOVER_RETRIES` | 3 | Retries for crossover generation |
| `MAX_GENESIS_RETRIES` | 3 | Retries for genesis (from-scratch) generation |
| `EVOLUTION_BRANCH` | `'main'` | Target git branch for commits |

Additional daemon constants (in `elo_daemon.py`):
| Constant | Value | Purpose |
|---|---|---|
| `MAX_REPLAY_FILES` | 2000 | Replay file cap (raised from 200; match_replay/ holds ~1900 files). Note: code value is authoritative, doc was stale. |
| `SAVE_EVERY_N_GAMES` | 20 | Daemon save frequency (games) |
| `SAVE_INTERVAL_SEC` | 60 | Daemon save frequency (seconds) |
| `UNDER_EVAL_BASELINE` | 50 | Baseline for under-evaluated calculation |
| `UNDER_EVAL_WEIGHT` | 0.6 | Weight for under-evaluated pair selection |
| `DIVERSITY_WEIGHT` | 0.4 | Weight for rating-diverse pair selection |
| `RATING_GAP_SCALE` | 200 | Diversity calculation scale |
| `DIVERSITY_COUNT_DECAY` | 100 | Diversity count decay factor |

### Glicko-2 Daemon (`elo_daemon.py`)

Background subprocess continuously running mirror battles. Match selection: 60% under-evaluated pairs (`UNDER_EVAL_WEIGHT = 0.6`) + 40% rating-diverse pairs (`DIVERSITY_WEIGHT = 0.4`). Per-game Glicko-2 updates (not batch). Writes to all result files with `fcntl` locking. Continuous scheduling via `ProcessPoolExecutor`. Replay files capped at 2000 (`MAX_REPLAY_FILES`). Responds to `.reap_signal` for immediate bot list refresh after commit.

Defaults: `r=1500`, `rd=350`, `sigma=0.06`, `tau=0.5`. Confidence levels: rd<50 `very_confident`, 50-100 `confident`, 100-200 `uncertain`, >200 `very_uncertain`.

### Process Lifecycle & Recovery

- **ShutdownManager** (`shutdown_manager.py`): Asyncio-native SIGINT/SIGTERM handler. Double-signal kills process. All three generation phases check `shutdown_mgr.is_shutting_down` between operations.
- **Orchestrator session persistence**: `orchestrator_session.json` stores the session ID. On restart, the Orchestrator resumes the exact LLM conversation. Cleared on natural cycle completion.
- **Pipeline checkpoint**: `STAGE_ORDER` in `evolution_infra.py` defines stage flow (`prepared` → `direction_audited` → `master_planned` → `workers_done` → `quality_failed` → `quality_passed` → `reviewed` → `critic_checked` → `verified` → `archived`). `STAGE_GATE_ALLOWLIST` enforces stage ordering — `run_review` blocks if quality gates haven't passed, `commit_bot` blocks if any gate is missing or if quality/precommit code fingerprints no longer match the candidate code.
- **Daemon lifecycle**: `start_daemon()` spawns `elo_daemon.py` as subprocess. `daemon_monitor_thread()` watches for crashes and auto-restarts. Daemon auto-exits on parent death via `getppid()==1` check.
- **Orphan detection**: JSON PID file (`.daemon_pid`) for daemon process tracking. 5s orphan detection interval.

### Web Core Module Map

| File | Lines | Role |
|---|---|---|
| `elo_daemon.py` | 738 | Background mirror battle subprocess with Glicko-2 updates |
| `evolution_infra.py` | 742 | Constants, git ops, file locking, checkpoints, bot directory, ratings, archiving |
| `orchestrator.py` | 601 | LLM-driven orchestrator loop, three-phase generation cycle |
| `combined_analyst.py` | 330 | Merged stagnation + performance verification (single LLM call) |
| `tool_status.py` | 504 | Non-pipeline MCP tools (queries, daemon control, analysis) |
| `tool_helpers.py` | 551 | Shared helpers: UI injection, checkpoint gates, H2H utilities, boundary validation |
| `tool_gates.py` | 466 | Pipeline tools: quality gates, prepare_next_gen, review, critic |
| `tool_planning.py` | 431 | Pipeline tools: direction audit, master, workers |
| `tool_commit.py` | 411 | Pipeline tools: commit, archivist, crossover |
| `tool_eval.py` | 371 | Pipeline tools: precommit eval, inline eval |
| `generation_scheduler.py` | 358 | Three-phase cycle: prepare, run, post-cleanup |
| `web_ui.py` | 292 | `EventBroadcaster` (ring buffer 500) + `WebUI` (terminal + SSE dual output) |
| `orchestrator_context.py` | 303 | Context string builder + PreCompact hook |
| `agent_review.py` | 288 | Critic, Performance Verification, Crossover agents |
| `reset.py` | 282 | Wipe evolution state to baseline |
| `llm_query.py` | 259 | `run_claude_query()`, `parse_json_output()`, prompt budgeting |
| `glicko2.py` | 222 | Pure Glicko-2 rating algorithm |
| `daemon_management.py` | 233 | Daemon subprocess lifecycle: start, stop, monitor, orphan detection |
| `agent_workers.py` | 212 | Worker execution: parallel/serial dispatch, timeout isolation, retry logic |
| `tool_bot_management.py` | 216 | Bot reaping, cleanup, abandonment, experience pool tools |
| `decision_tester.py` | 202 | Predefined scenario tests against bots |
| `agent_master.py` | 173 | Master Architect + match analysis |
| `replay_analysis.py` | 178 | Replay data summarization (pure data, no LLM) |
| `stagnation_analyzer.py` | 162 | Rating trend stagnation analysis via LLM (called via combined_analyst) |
| `direction_auditor.py` | 151 | Pre-Master repetition detection via LLM |
| `orchestrator_session.py` | 148 | Session persistence, startup recovery, log rotation |
| `output_schema.py` | 139 | Pydantic models for validating structured LLM output |
| `experience_archivist.py` | 132 | Experience pool consolidation + archivist analysis |
| `commentary.py` | 129 | Deterministic match replay commentary (no LLM) |
| `logging_config.py` | 121 | Structured logging: colored console, rotating file, SSE handler |
| `tools.py` | 107 | MCP server registration + tool aggregation |
| `code_verification.py` | 92 | Compile check, file size, smoke test, decision tests |
| `evolution_core.py` | 83 | Re-export facade: aggregates all sub-modules for backward compatibility |
| `shutdown_manager.py` | 56 | Asyncio-native SIGINT/SIGTERM handler |
| `smoke_tester.py` | 36 | Run 1 mirror match as sanity check |
| `system_log.py` | 33 | Structured JSONL event logger |
| `experience_pool.py` | 40 | Experience pool trim logic (keep under 120 lines) |
| `tool_pipeline.py` | 6 | Re-export shim: `from tool_planning/gates/eval/commit import *` |

**Supporting subdirectories:**
- `engine/` — Copy of `engine/battle.py` + modified `engine/judge.py` for web context
- `reference_bots/bot1`-`bot6` — Reference bot implementations used by the evolution pipeline
- `prompts/` — LLM prompt templates for orchestration, planning, workers, review, critic, crossover, audits, experience updates, and dynamic tests. Use the files present in `web/core/prompts/` as authoritative; this directory currently contains more than the original 14 templates.

### Reinforcement Learning Module (`rl/`)

DanLM-inspired DMC self-play training framework. Wraps `engine/judge.py` as a Gymnasium environment.

| File | Lines | Role |
|---|---|---|
| `core/holdem_env.py` | 556 | Gymnasium environment wrapping engine/judge.py Holdem |
| `core/tokenizer.py` | 247 | Game history tokenizer for Transformer input |
| `core/config.py` | 114 | Training hyperparameters (cycle-based deterministic training) |
| `core/encoder.py` | 76 | State/action encoders (v0: 132-dim flat, v1: token sequence) |
| `training/trainer.py` | 419 | DMC training loop (actor processes + learner) |
| `training/replay_buffer.py` | 136 | Uniform/prioritized replay buffer |
| `models/transformer.py` | 319 | Transformer Q-Network (DanLM-style TinyLM) |
| `models/q_network.py` | 135 | MLP Q-Network (DanZero-style) |
| `scripts/train.py` | 223 | Training entry point |
| `scripts/rl_bot.py` | 247 | RL bot wrapper for engine subprocess protocol |
| `scripts/evaluate.py` | 68 | Evaluation entry point |

### Engine Files

| File | Lines | Role |
|---|---|---|
| `engine/ladder.py` | 954 | Round-robin ELO tournament with checkpoint/restore, rank titles |
| `engine/battle.py` | 727 | CLI battle runner, mirror_battle, battle_generator, human_battle_generator |
| `engine/judge.py` | 576 | Stateless Holdem judge: Holdem class, Suit/HandType/Card enums, judge() function |
| `engine/anchor_runner.py` | 642 | One bot vs all others, supports --dry-run, --exclude, per-opponent parallel workers |

### Scripts

| Script | Purpose |
|---|---|
| `scripts/botzone_upload_match.py` | Full Botzone client: upload, rooms, matches, ranking |
| `scripts/botzone_room_series.py` | Batch room matches on Botzone |
| `scripts/botzone_multi_account_upload.py` | Multi-account bot upload |
| `scripts/ref_strategy_labels.py` | Offline strategy analysis / labeling |
| `scripts/reset_evolution.py` | Reset evolution to baseline (keeps v1-v6) |
| `scripts/test_claude_cli.py` | Claude CLI testing utility |
| `merge_bot.py` | Merge multi-file bot into single file (`merge_bot.py --all` for batch) |
| `pokctl.sh` | Web service management (start/stop/status/restart/logs) |

### Documentation

| File | Purpose |
|---|---|
| `AGENTS.md` | AI agent onboarding context for the project |
| `ONBOARDING.md` | Teammate usage guide and workflow breakdown |
| `SETUP_GUIDE.md` | Remote deployment tutorial |
| `docs/holdem_rl_design.md` | HoldemRL DMC self-play framework design |
| `docs/rl_improvement_research.md` | RL improvement research report |
| `docs/llm-stages.md` | LLM multi-stage runtime data flow documentation |
| `docs/multi_ai_bot_design.md` | Multi-AI iterative bot evolution design document |
| `docs/pipeline-bottleneck-analysis.md` | Evolution pipeline bottleneck analysis |
| `docs/find-current-v-analysis.md` | `find_current_v()` analysis report |

### Reference Implementations (`ref/`)

External projects used as architectural references for the `rl/` module and Botzone integration.

#### DanLM (`ref/DanLM/`)

Game AI for multi-player trick-taking card games (GuanDan, DouDiZhu) that learns entirely from raw game history via self-play RL with zero domain knowledge. Reached #1 on Botzone leaderboards.

- **Paper**: "DanLM: Tokenization Is All You Need to Master Complex Card Games"
- **Architecture**: TinyLM Encoder (causal Transformer on tokenized play records) + Hand MLP + Q-Value Head with auxiliary NTP loss.
- **Training**: DMC (Deep Monte Carlo) self-play, cycle-based. Predecessor: DanZero (AAAI 2023, 567-dim hand-crafted features + MLP).
- **License**: Apache 2.0 + non-commercial restriction (academic/personal use only).

| Subpackage | Role |
|---|---|
| `danzero/config_v3.py` | `DanZeroV3Config` dataclass: cycle-based N/k/S hyperparameters |
| `danzero/encoding/` | State encoding: v0 (567-dim), v1t (964-dim), tokenizer (~90 vocab) |
| `danzero/engine/` | Core GuanDan game engine (cards, actions, rounds, tribute) |
| `danzero/model/` | MLP Q-network (DanZero) + Transformer Q-network (DanLM/TinyLM) |
| `danzero/eval/` | Evaluation: pluggable agent interface, baseline adapter |
| `danzero/explorer/` | Parallel exploration: 5 strategies (Greedy, ε-Greedy, Boltzmann, Diverse, MCTS) |
| `scripts/` | evaluate.py, evaluate_game.py, parallel_explore.py |
| `ui/server.py` | FastAPI interactive play server with AI hints (Q-value estimates) |
| `baselines/` | 16 competition bots from 1st National GuanDan AI Competition (bugs fixed) |
| `ckpts/` | 3 model checkpoints (~80MB): DanLM_v1 (Transformer), DanZero_v3 (MLP), DanZero_v3_rep_v1t |

**Relationship to this project's `rl/` module**: Direct architectural adaptation from GuanDan to heads-up NL Hold'em:

| DanLM | `rl/` | Notes |
|---|---|---|
| `DanZeroV3Config` | `HoldemRLConfig` | Same N/k/S cycle pattern |
| `danzero/encoding/tokenizer` | `rl/core/tokenizer.py` | Same tokenization, ~80 vocab for Hold'em |
| `danzero/model/transformer` | `rl/models/transformer.py` | Same dual-stream TinyLM + Q-Value Head |
| `danzero/engine/` | `rl/core/holdem_env.py` | Gymnasium env wrapping engine/judge.py |
| DanZero MLP | `rl/models/q_network.py` | MLP Q-network baseline |

#### neuron_poker (`ref/neuron_poker/`)

Open-source Texas Hold'em AI training framework (MIT, Nicolas Dickreuter). OpenAI Gym environment for No-Limit Hold'em with multiple agent types.

- **Python**: ~=3.11, **License**: MIT, **Game**: NL Hold'em 2-6 players
- **Action space**: Discrete(8) — fixed raise sizes (3BB, half-pot, pot, 2x pot), no continuous raise
- **Key difference from `engine/`**: Fixed pot-fraction raises vs arbitrary raise amounts; multi-player with side pots vs heads-up only; stack 500/blinds 1/2 vs 20000/50/100

| Component | Role |
|---|---|
| `gym_env/env.py` | `HoldemTable(Env)` — Gym environment with Monte Carlo equity in observations |
| `agents/` | RandomPlayer, KeyPressPlayer, EquityPlayer (threshold-based), DQNPlayer (keras-rl), Custom_Q1 (stub) |
| `tools/montecarlo_*` | Equity calculation: Python, NumPy, C++ (~500x faster) |
| `tools/hand_evaluator.py` | Best 5-card hand evaluation |

Algorithms: random baseline, equity-based threshold, genetic self-improvement (population of equity agents), DQN via keras-rl (3×512 MLP, Boltzmann policy).

#### Botzone Platform API (`ref/player_api.js`, `ref/TexasHoldem2p.html`)

- **`player_api.js`** — Client-side JavaScript API for Botzone game renderers. Two generations: v1 (direct callbacks) and v2 (GSAP TimelineMax animation model). Handles match init, log streaming, player turns, game-over, seek/pause/resume.
- **`TexasHoldem2p.html`** — Botzone's 2-player NL Hold'em game renderer and authoritative protocol reference:
  - **Card format**: Integers 0-51, `suit = card % 4` (h/d/s/c), `rank = card // 4` (0=2..12=A). Exactly matches `engine/judge.py`.
  - **Action format**: `-1`=fold, `-2`=all-in, `0`=check/call, `>0`=raise. Matches `engine/judge.py`.
  - **Game state model**: `round_player_bet` (per-player bets, -1=folded, -2=all-in), `round` (0-4 for preflop→showdown), `round_raise` (max raise seen), `pot`, `player_chips`, `public_cards`, `player_cards`, `last_action`.
  - **Match data**: `hand` (0-indexed), `max_hand`, `total_win_chips`, `temp_result`, `final_result`.
  - **Min raise**: `2 * round_raise` where `round_raise` tracks max raise increment.

## Key Conventions

- All shared files use `fcntl` file locking for concurrent access between daemon subprocess, orchestrator, and API server
- Worker role boundaries enforced by prompts and reviewer: Logic Architects cannot tune constants, Hyperparameter Tuners cannot add functions
- `_validate_worker_boundaries()` checks edits don't cross role boundaries after each worker run
- Worker failures recorded to `worker_failures.jsonl` and injected into future worker prompts as memory
- Experience pool consolidation is tag-gated and runs after commit every 3 generations or when `RECENT_LESSONS` has at least 4 entries
- `_BLOCKED_MCP_TOOLS` in `evolution_infra.py` blocks external MCP tools from sub-agents
- `_WORKER_SEMAPHORE` (asyncio.Semaphore, max 3) limits concurrent LLM worker calls
- `_PersistentBot` keeps one Popen alive for an entire game (2x battle speedup vs per-decision subprocess)
- Tests use `starlette.testclient.TestClient` with no lifespan (no orchestrator/daemon startup)
- Test naming: `test_routes_*.py` (HTTP endpoints), `test_logic_*.py` (pure functions), `test_mcp_*.py` (MCP tool handlers)
- `results/` at project root stores timestamped competition result JSONs (e.g., `20260608_100329_main_vs_main.json`), separate from `web/core/results/` which stores live daemon/orchestrator data
- `archive/` stores deprecated code: old dashboard (backend+frontend), old orchestrator, old evolution_workspace
- `ref/DanLM/` and `ref/neuron_poker/` are gitlinks in this checkout, but `.gitmodules` is currently absent. `git submodule status` may fail or report noise; do not repair or stage gitlink changes unless the task is specifically about references/submodules.

## Repository Hygiene

Important generated or runtime locations:

- `web/core/results/`
- `web/logs/`
- `web/frontend/dist/`
- `web/server/static/`
- `results/*.json`
- `ladder_results/`
- `bots/graveyard/`
- `.completed` sentinel files

## Git And Change Hygiene

The working tree may already contain user changes, evolution-system output, incomplete bot generations, or dirty gitlinks. Treat that as normal. Check `git status --short --branch` before editing and again before committing so unrelated files are visible.

Do not revert, reset, restore, or checkout unrelated changes unless the user explicitly asks for that exact destructive operation. Do not clean untracked bot directories, generated outputs, or gitlink directories as part of an unrelated task.

本仓库改代码规范：

- 先确认模块边界再改代码：`engine/` 是本地 JSON battle，`web/` 是进化系统，`sever/` 是国赛 TCP 平台，`rl/` 是实验；不要把协议和职责混在一起。
- 改代码前先从 `main` 开任务分支，默认命名 `codex/<task-name>`；在分支内完成修改和提交，再切回 `main` 合并并 push。
- 只改当前任务需要的文件，不顺手重构、不统一无关风格、不碰运行产物。
- 协议、adapter、THP、card mapping、质量门、进化提示词等边界变更必须配套测试或至少明确的冒烟验证。
- Web 进化相关变更要同时检查 Python 逻辑、prompt 文档、quality gates，避免旧提示词继续生成旧行为。
- 最终汇报必须说明改了什么、跑了什么验证、提交/推送结果，以及哪些已有脏项未触碰。

Stage only the files changed for the current task. Do not use `git add -A` unless the user explicitly asks for a full repository snapshot. Runtime/generated paths such as `web/core/results/`, `web/logs/`, `web/frontend/dist/`, `web/server/static/`, `results/*.json`, `ladder_results/`, `bots/graveyard/`, and `.completed` sentinels should not be staged unless the task is specifically about them.

Evolution-generated bot versions are complete only when the orchestrator `commit_bot` flow has passed its gates, committed the bot, and created the annotated `bot-v{N}` tag. Do not hand-edit bot lineage tags or `.completed` sentinels unless the task is explicitly about evolution recovery.

`ref/DanLM` and `ref/neuron_poker` are gitlinks in this checkout, but `.gitmodules` is currently absent. `git submodule status` may fail or report noise; do not repair or stage gitlink changes unless the task is specifically about references/submodules.

After a task that changes files, commit and push task-related changes:

```bash
git switch main
git pull --ff-only
git switch -c codex/<task-name>
git add <files you changed>
git commit -m "<descriptive message>"
git switch main
git merge --no-ff codex/<task-name>
git push
```

If the repository is dirty before the task, mention that in the final response and avoid mixing unrelated files into the commit. If commit or push fails because of credentials, remote state, hooks, or network problems, report the exact failure and leave the worktree otherwise intact.
