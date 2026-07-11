# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Texas Hold'em poker AI bot framework that started as a Botzone evolution project and now has four important code paths. Do not collapse them into one mental model:

1. `engine/` — local Botzone-style subprocess battle engine for Python bots.
2. `web/` — unified evolution system, FastAPI backend, and React dashboard.
3. `sever/` — national competition TCP self-play platform based on the documents in `sever/国赛平台/`.
4. `rl/` — reinforcement learning experiments that wrap the local Hold'em engine.

There are two protocol families:

- Botzone/local protocol: bots are Python subprocesses that read JSON from stdin and write JSON to stdout. This is implemented by `engine/` and remains useful for legacy regression and old bots.
- National competition protocol: AI engines connect to a TCP server and exchange raw short socket messages such as `preflop|SMALLBLIND|<0,3><1,3>`, `raise 200`, `call`, `check`, `fold`, `allin`. The official Windows EXE does not guarantee newline delimiters or TCP message boundaries, and it is timing-sensitive; native bots must split sticky packets and keep the official action-send throttle in the TCP wire layer. This is implemented by `sever/`. New evolved bots are expected to be national-native and expose a direct TCP entrypoint; `sever/bot_adapter.py` is a legacy bridge/regression path, not the formal shape for new submissions.

The evolution pipeline lives under `web/core/`. It uses LLM agents, Glicko-2 ratings, mirror battles, quality gates, precommit regression evaluation, and accumulated strategy lessons to generate new bot versions.

The old `web/tui.py` Textual TUI no longer exists. Treat `web/main.py` as a web app launcher, not a TUI or mode-switching CLI.

## Active Evolution Runtime

The actual long-running autonomous evolution service for this machine runs from
`/home/zzx/project/pok/.evolution_pok`, not from the outer operator checkout.
Use that directory when checking health, logs, active candidates, or restarting
the evolution loop:

```bash
cd /home/zzx/project/pok/.evolution_pok
./pokctl.sh status
./scripts/pok_restart_observe.sh --no-build --daemon-workers 12 --daemon-pairs 5 --observe-generations 10
```

Use `/home/zzx/project/pok` for normal infrastructure, prompt, test, and
documentation edits. Before editing here, pull remote state with
`git pull --ff-only --tags` on a clean tracked branch. If unfinished bot
directories appear in the outer checkout without `.completed`, a committed bot
directory, and the matching `national-bot-v{N}` tag, treat them as stale abandoned
candidates: inspect logs/checkpoints, remove the untracked candidate, and clear
any stale active checkpoint instead of resuming it from the operator checkout.

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

Use `--no-build` only after `web/server/static/index.html` and
`web/server/static/assets/` already exist in the same checkout. On a fresh
`.evolution_pok` runtime clone, start without `--no-build` once so `web/main.py`
can install frontend dependencies with `npm ci` if needed and build the React
dashboard.

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
python archive/cleanup_20260708/root_experiments/merge_bot.py bots/national_v49/  # Archived legacy merge helper
python archive/cleanup_20260708/root_experiments/merge_bot.py --all               # Archived legacy batch merge
```

### TCP Competition Server (`sever/`)

```bash
cd sever && python main.py                    # Start TCP :10001 + Web :18080
cd sever && python main.py --tcp-port 20001 --web-port 28080
cd sever && python test_client.py 127.0.0.1 10001 BotA
cd sever && python test_client.py 127.0.0.1 10001 BotB
cd sever && python bot_adapter.py --bot ../archive/evolution_epochs/<epoch>/legacy_bots/claude_v224 --name legacy-test  # Legacy bridge only
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
6. **Quality Gates** (automated, no LLM): `py_compile`, runtime import contract, smoke test, decision tests (≥70% pass), national TCP protocol regression tests matching the active execution mode (`national_native` uses the adapter-free platform shard; adapter workflows use the legacy adapter shard), declared-scope/protected-contract checks, mandatory fix verification, telemetry/reachability checks, file size ≤2000 lines (core strategy files) / ≤1500 lines (helpers), adaptive limit based on source bot size + 15% growth budget, hard cap 2500.
7. **Code Reviewer** (`prompts/reviewer_prompt.md`): LLM reviews diff, enforces role boundaries, scores 1-10. Up to 3 retries.
8. **Critic** (`prompts/critic_prompt.md`): Independent advisory strategic review. A schema-valid run is required and its evidence is preserved, but native TCP precommit owns strategy acceptance and repair routing.
9. **Pre-commit Eval**: Mirror battle regression check vs parent + top opponents.
10. **Commit**: Git commit + `national-bot-v{N}` annotated tag. Tags are authoritative completion proof.
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

Note: Stagnation Analyst and Performance Analyst have been merged into `combined_analyst.py` (single LLM call). The separate `stagnation_analyzer.py` is a legacy standalone implementation; `combined_analyst.py` does not import or call it.

### Data Flow

```
Workers edit bots/national_v{N}/  (LLM-driven code changes)
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
  ├── pipeline_state.json    ← Pipeline checkpoint for crash recovery (tools write)
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

Entry point: `web/main.py` → `web/server/app.py`. Twelve route modules in `server/routes/` (excluding `__init__.py` and `_helpers.py`): `ratings`, `matches`, `evolution`, `logs`, `control`, `bots`, `pipeline`, `prompts`, `data_stream`, `scheduler`, `certification`, `national_arena`:

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
- `/api/national-arena/*` — Local national TCP sessions, SSE, wire logs, THP, and managed bot launch. This API is diagnostic-only and cannot certify bots.
- `/api/certification/*` — Durable official Windows EXE jobs and signed compliance status.

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
| `/arena` | NationalArena | Local national TCP session API + SSE; never official certification |

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

The main Web `/arena` experience reuses the shared national transport/game
runtime for presentation and diagnostics. It does not replace the official
Windows EXE and never writes ratings or certificates. A new evolution bot is
formal only after the official full policy completes five self-play and three
eligible-opponent rounds of 70 hands and publishes a valid signed,
content-bound certificate with its commit/tag.

The ordinary formal path accepts only published full-v5 certified opponents.
`scripts/official_certify.py bootstrap-full` is a separate, explicitly
acknowledged one-time recovery path: it can use only the repository-pinned
signed-ledger root, binds its receipt into the durable job/certificate, and a
successful full run consumes that root in the signed verdict ledger. It is
never an automatic active-pool or grandfather fallback.

Strength is evaluated separately. One sample is one complete 70-hand local
native TCP match. Positive final net chips is a win, negative is a loss, and
zero is a draw. Outcome-derived Glicko/H2H/`selection_score` is primary;
net-chip magnitude is only a secondary tie-breaker when the primary score is
equal. Official EXE and Arena chip outcomes have zero strength weight.

**Startup & smoke testing:**
```bash
cd sever && python main.py                    # TCP :10001 + Web :18080
cd sever && python test_client.py 127.0.0.1 10001 BotA
cd sever && python test_client.py 127.0.0.1 10001 BotB
python -m pytest sever/tests -q
```

**Structure:** `engine/game.py` (stateful GameEngine), `engine/validator.py` (13-rule validation), `engine/evaluator.py` (hand comparison), `engine/deck.py` (Card class, `<suit,rank>`), `server/tcp_server.py` (async TCP), `server/protocol.py` (message encode/decode), `bot_adapter.py` (bridges `engine/judge.py` bots to TCP server), `web/app.py` (FastAPI + SSE dashboard).

**Protocol differences from `engine/`:**
- Raw short TCP messages (not subprocess JSON and not guaranteed line-delimited by the official EXE)
- Card format: `<suit,rank>` tuples where `suit ∈ {0=♠,1=♥,2=♦,3=♣}`, `rank ∈ {0=2..12=A}`
- Actions: exact text strings (`"call"`, `"check"`, `"fold"`, `"allin"`, `"raise 200"`). `raise` uses exactly one space before the amount; `bet` is always illegal.
- Official EXE timing: generated `national_bot.py` entries must keep `POK_OFFICIAL_ACTION_DELAY` and send through `_send_wire_action` with a default around `0.30s`. Local strength evaluation may disable this delay with environment, but submitted/formal entries must default to official-safe timing.
- Do not copy timeout-rescue fallback loops that send unsolicited `call` or `check`; a bot may only send an action while the platform is waiting for its current decision.
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

**Minimum raise rules:**

| Rule | `engine/judge.py` | `sever/` |
|------|--------------------|----------|
| Tracking variable | `last_raise_to` (last raise-to total) | `last_raise_to` (last raise-to total) |
| Preflop first raise | total ≥ 200 (derived from `big_blind`) | total ≥ 200 (explicit check) |
| Postflop first raise | total ≥ 100 (derived from `big_blind // 2`) | total ≥ 100 (explicit check) |
| Re-raise minimum | legacy/local judge currently uses total > `last_raise_to * 2` | official-compatible national validator accepts total ≥ `last_raise_to * 2` |

**Re-raise boundary clarification**: the official Windows EXE is the authority
for protocol legality. A controlled two-seat oracle run on 2026-07-11 showed
`raise 200` followed by exact `raise 400` being relayed, followed by `fold` and
zero-sum `earnChips ±200`; exact 2x is therefore legal. `raise 401` also passed
as a control. Native templates, the legacy adapter, and legacy fix injection may
continue to choose `2x + 1` as conservative cross-path headroom, but must not
describe exact 2x as illegal. See `docs/official-raise-boundary-oracle-2026-07-11.md`.

**`bot_adapter.py` bridge:** Converts bot integer output directly for actions: `>0` → `raise {value}`. Since both engines use raise-to-total, the action amount does not need delta conversion.

### `sever/` Game Flow & Rules Summary

**Action order:** Preflop → SB first; Flop/Turn/River → BB first. 70 hands, alternating SB/BB.

**TCP message sequence per hand:**
1. Server sends `name` → client responds with bot name
2. Server sends `preflop|{ROLE}|<s,r><s,r>` → SB acts first
3. Opponent actions forwarded: `call`, `fold`, `check`, `raise X`, `allin`
4. Stage cards: `flop|<s,r><s,r><s,r>`, `turn|<s,r>`, `river|<s,r>` → BB acts first
5. Settlement: `earnChips {amount}` (net change), `oppo_hands|<s,r><s,r>` (showdown only). The 2021 EXE omits the hand-70 `earnChips` pair at natural match end but records that hand and the cumulative result in THP; formal v5 certification requires a wire/THP cross-bound completion proof rather than treating 69 as sufficient.

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

- Active evolution epoch: `national_native_v1`. Bots live in `bots/national_v{N}/` and completion tags are `national-bot-v{N}`. Old `claude_v*` directories and `bot-v*` tags are legacy history and must not affect active version numbering, source selection, ratings, H2H, prompt memory, or pass/fail gates.
- Evolution-generated bot versions are complete only when the orchestrator `commit_bot` flow has passed gates, committed the bot, and created the annotated `national-bot-v{N}` tag.
- Pool capped at 30 active; weakest culled by conservative Glicko score (`r - 2*rd`) to `bots/graveyard/`, with sample-starved bots protected until the hard overflow rules apply. H2H average win rate is reported as context, not the primary reap key.
- Source selection is owned by `generation_scheduler._decide_strategy`. LLM `recommended_source` and `branch_from` suggestions are accepted only for active, completion-backed bots (`.completed` plus `national-bot-v{N}` tag); rejected suggestions emit `pipeline.source_selection_rejected`.
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
| `UNDER_EVAL_BASELINE` | 90 | Baseline for under-evaluated calculation |
| `UNDER_EVAL_WEIGHT` | 0.6 | Weight for under-evaluated pair selection |
| `DIVERSITY_WEIGHT` | 0.4 | Weight for rating-diverse pair selection |
| `RATING_GAP_SCALE` | 200 | Diversity calculation scale |
| `DIVERSITY_COUNT_DECAY` | 100 | Diversity count decay factor |

### Glicko-2 Daemon (`elo_daemon.py`)

Background subprocess continuously running mirror battles. Match selection: 60% under-evaluated pairs (`UNDER_EVAL_WEIGHT = 0.6`) + 40% rating-diverse pairs (`DIVERSITY_WEIGHT = 0.4`). Per-game Glicko-2 updates (not batch). Writes to all result files with `fcntl` locking. Continuous scheduling via `ProcessPoolExecutor`. Replay files capped at 2000 (`MAX_REPLAY_FILES`). Responds to `.reap_signal` for immediate bot list refresh after commit.

Defaults: `r=1500`, `rd=350`, `sigma=0.06`; the Glicko-2 volatility constant is `TAU=0.3` in `glicko2.py` (not 0.5). Confidence levels: rd<50 `very_confident`, 50-100 `confident`, 100-200 `uncertain`, >200 `very_uncertain`.

### Process Lifecycle & Recovery

- **ShutdownManager** (`shutdown_manager.py`): Asyncio-native SIGINT/SIGTERM handler. Double-signal kills process. All three generation phases check `shutdown_mgr.is_shutting_down` between operations.
- **Orchestrator session persistence**: `orchestrator_session.json` stores the session ID. On restart, the Orchestrator resumes the exact LLM conversation. Cleared on natural cycle completion.
- **Pipeline checkpoint**: `STAGE_ORDER` (defined in `pipeline_state.py`, re-exported by `evolution_infra.py`) defines the 19-stage state machine: `selected` → `preparing` → `prepared` → `crossover_running` → `direction_audited` → `master_planned` → `workers_done` → `quality_failed` → `quality_passed` → `reviewed` → `critic_checked` → `precommit_failed` → `repair_planned` → `rework_running` → `verified` → `official_certifying` → `official_failed|official_inconclusive` → `archived`. `STAGE_GATE_ALLOWLIST` enforces cumulative quality, review, critic, precommit, and official-full ledgers. `commit_bot` cannot publish until the content-bound signed EXE certificate validates. `pipeline_state.json` persists the current stage and durable official job attachment for crash recovery.
- **Daemon lifecycle**: `start_daemon()` spawns `elo_daemon.py` as subprocess. `daemon_monitor_thread()` watches for crashes and auto-restarts. Daemon auto-exits on parent death via `getppid()==1` check.
- **Orphan detection**: JSON PID file (`.daemon_pid`) for daemon process tracking. 5s orphan detection interval.

### Web Core Module Map

Line counts are point-in-time snapshots (`wc -l`); they drift as code evolves,
so treat them as approximate ordering cues, not contracts. This is a map of the
major and national-runtime-specific modules, not an exhaustive file inventory.
It is sorted by current line count and excludes `__init__.py`,
`reference_bots/`, and `__pycache__/`.

| File | Lines | Role |
|---|---|---|
| `tool_planning.py` | 7727 | Pipeline tools: direction audit, master planning, workers, literature, repair synthesis |
| `official_certification.py` | 3944 | Official EXE policy, identity, evidence validation, signed certificates and explicit one-time bootstrap binding |
| `tool_gates.py` | 3146 | Pipeline tools: quality gates, code prep, review, advisory critic execution |
| `orchestrator.py` | 3082 | LLM-driven Orchestrator: pipeline loop and recovery routing |
| `national_native.py` | 2996 | Native national TCP execution backend for evolved bots |
| `tool_eval.py` | 2708 | Frozen-contract precommit and diagnostic inline evaluation |
| `llm_query.py` | 2605 | `run_claude_query()` primitive, output parsing, prompt budgets, sandboxing |
| `evolution_infra.py` | 2405 | Shared infra: constants, git, checkpoints, ratings and publication |
| `generation_scheduler.py` | 2296 | Three-phase generation scheduler and source selection |
| `tool_commit.py` | 2036 | Commit, signed official full gate, archivist, crossover |
| `elo_daemon.py` | 2025 | Background rating daemon: native matches, Glicko-2, H2H and chip telemetry |
| `national_arena/manager.py` | 1925 | Local diagnostic/presentation Arena session lifecycle |
| `official_platform_harness.py` | 1818 | Wine/Xvfb driver for official EXE evidence |
| `national_capability_contract.py` | 1433 | Static/runtime data-flow capability evidence for native bots |
| `agent_workers.py` | 1409 | Worker execution with retries and runtime-contract isolation |
| `decision_tester.py` | 1318 | Decision scenarios and dynamic regression generation |
| `battle_experience.py` | 1308 | Incremental match analysis via background LLM thread |
| `official_llm_analysis.py` | 1073 | Advisory-only EXE evidence explanation and repair guidance |
| `official_certification_job.py` | 1036 | Durable process-owned official EXE job state machine |
| `code_verification.py` | 1005 | Compile, size, smoke, decisions, reachability and runtime evidence |
| `tool_helpers.py` | 991 | Shared MCP tool helpers, strength snapshots, gates and repair contracts |
| `orchestrator_context.py` | 933 | Orchestrator context building and PreCompact hook |
| `engine/battle.py` | 918 | Local legacy battle runner used only by legacy paths |
| `runtime_architecture_policy.py` | 889 | Parent capability preservation and one-bundle runtime evolution policy |
| `national_epoch_registry.py` | 834 | Active epoch completion/reaping registry |
| `audit_agents.py` | 824 | Schema-gated pipeline audit agents |
| `official_wire_probe.py` | 814 | Raw EXE TCP capture and deterministic protocol replay |
| `national_runtime_probe_worker.py` | 744 | Resource-limited dynamic native capability probe worker |
| `tool_runtime_guard.py` | 734 | Exact-contract git/worktree runtime guard |
| `agent_review.py` | 710 | Reviewer, advisory Critic, performance and crossover agents |
| `pipeline_state.py` | 676 | Authoritative 19-stage machine and cumulative gate policy |
| `official_evidence.py` | 675 | Standardized EXE evidence bundle assembly |
| `bot_action_stats.py` | 666 | Bot action statistics extraction from replay files |
| `candidate_store.py` | 654 | Candidate ledger plus SQLite query store |
| `replay_analysis.py` | 639 | Replay statistics and behavior fingerprints |
| `official_bootstrap.py` | 642 | Fail-closed pinned v5 signed-ledger bootstrap root selector and receipt validator |
| `evaluation_contract.py` | 618 | Active exact-file evaluation contract and HEAD drift policy |
| `evidence_snapshot.py` | 604 | Immutable per-generation evidence snapshots |
| `engine/judge.py` | 592 | Poker hand judge: suits, hand types, game engine rules |
| `national_acceptance.py` | 566 | In-process national-platform acceptance runner (gate API) |
| `daemon_management.py` | 525 | Daemon subprocess lifecycle: start/stop/monitor/orphan detect |
| `tool_status.py` | 522 | Non-pipeline MCP tools: status, daemon control, analysis |
| `agent_master.py` | 514 | Master Architect plans and runtime contracts |
| `event_bus.py` | 497 | Unified event bus with correlation schema |
| `engine/aivat.py` | 488 | AIVAT all-in variance reduction for heads-up mirror battles |
| `national_runtime_probe.py` | 487 | Trusted launcher and result contract for runtime probes |
| `map_elites.py` | 479 | MAP-Elites 5x5 behavior archive (advisory diversity signal) |
| `exploitability_prober.py` | 474 | Exploitability probe scoring across strategic axes |
| `replay_spotlight.py` | 463 | Replay spotlight: identify critical chip-swing hands |
| `battle_scheduler.py` | 454 | File-based (fcntl-locked JSONL) battle job queue for daemon |
| `pipeline_recovery.py` | 450 | Shared recovery diagnostics for active checkpoints |
| `rating_snapshot.py` | 448 | Unified sign-first strength snapshot with chip tie-break telemetry |
| `output_schema.py` | 448 | Pydantic models for structured LLM output |
| `combined_analyst.py` | 447 | Combined stagnation and performance analyst |
| `bot_artifact.py` | 446 | Immutable, content-addressed bot artifacts |
| `behavior_diversity.py` | 440 | Behavior diversity metrics via fingerprints and Vendi Score |
| `qd_async_eval.py` | 436 | Async QD background fitness evaluation |
| `tool_bot_management.py` | 431 | Reaping, cleanup, abandonment and experience management |
| `official_evidence_archive.py` | 428 | Immutable official evidence archive |
| `evolution_scope.py` | 434 | Runtime change classification and protected scopes |
| `national_arena/sandbox.py` | 408 | Read-only managed-bot Arena sandbox |
| `spot_analyzer.py` | 396 | Diff analyzer identifying changed .py files/functions in bot dirs |
| `official_verdict_ledger.py` | 359 | Signed append-only official verdict ledger, including successful bootstrap consumption |
| `psro_meta_solver.py` | 350 | PSRO meta-solver (fictitious play / uniform) over H2H payoffs |
| `orchestrator_session.py` | 345 | Orchestrator session persistence and startup recovery |
| `research_governance.py` | 342 | Ratchet-style governance for web-retrieved strategy candidates |
| `eval_rounds.py` | 340 | Cycle-based deterministic evaluation rounds for Glicko daemon |
| `official_attribution.py` | 332 | Candidate/opponent/harness fault attribution |
| `experience_archivist.py` | 317 | Experience pool consolidation and archivist analysis |
| `precommit_eval_contract.py` | 314 | Frozen evaluator/opponent/deck/seed precommit contract |
| `web_ui.py` | 314 | `EventBroadcaster` (ring buffer 500) + `WebUI` (terminal + SSE) |
| `pipeline_infrastructure.py` | 303 | Identity-bound infrastructure retry overlay |
| `fix_verification.py` | 301 | Structural/runtime verification of mandatory bot fixes |
| `glicko2.py` | 294 | Glicko-2 rating implementation, `TAU=0.3` |
| `official_eligibility.py` | 282 | Official certificate/grandfather pool eligibility |
| `reset.py` | 282 | Reset evolution state to baseline (v1-v6 only) |
| `experience_attribution.py` | 266 | Experience-pool Ratchet retirement for lessons |
| `national_eval.py` | 259 | National-platform performance eval backend for evolution gates |
| `national_transport.py` | 255 | Shared delimiter-free national TCP transport |
| `stagnation_analyzer.py` | 240 | Legacy standalone stagnation analysis (supplanted by combined_analyst; not imported by it) |
| `official_certificate_signing.py` | 239 | Certificate signing and trust verification |
| `national_arena/storage.py` | 237 | Persistent Arena event, wire and THP storage |
| `logging_config.py` | 232 | Centralized logging: color console, rotating files, SSE |
| `direction_auditor.py` | 230 | Direction Auditor: detect repetitive evolution directions via LLM |
| `pipeline_schema.py` | 223 | Structured pipeline records for gates/candidates |
| `rate_limiter.py` | 216 | Global 429 rate-limit handler for LLM API quota exhaustion |
| `repo_state.py` | 204 | Git/worktree observability helpers for the pipeline |
| `evaluation_data_identity.py` | 198 | Semantic identity and archive boundary for rating datasets |
| `skill_library.py` | 198 | Offline poker skill-library metadata for prompts/harnesses/gates |
| `worker_boundary.py` | 192 | Worker/candidate editable-boundary file checks |
| `qd_fitness.py` | 192 | Phase 4 QD k=3 fitness: median over 3 mirror-battle evals |
| `runtime_capacity.py` | 172 | Cross-process shared host-capacity leases |
| `official_bot_sandbox.py` | 172 | Official managed-bot read-only sandbox |
| `eval_stats.py` | 158 | Precommit eval stats: paired bootstrap CI + anytime-valid CS |
| `publish_reconcile.py` | 138 | Contract-neutral publication reconciliation |
| `nemesis_archive.py` | 134 | FAMOU nemesis archive: persistent nemesis/champion relationships |
| `commentary.py` | 129 | Lightweight deterministic match commentary generator (no LLM) |
| `national_bot_launcher.py` | 126 | Shared native bot launch specification |
| `plan_compiler.py` | 124 | Deterministic Master-plan compilation (brief-file offload) |
| `national_runtime_probe_scenarios.py` | 122 | Deterministic runtime probe scenarios |
| `tools.py` | 120 | MCP tools re-export facade + server registration (17 tools) |
| `protected_contracts.py` | 120 | Protocol-boundary checks for legacy Botzone JSON bot entries |
| `official_execution_profile.py` | 118 | Tracked official execution profile parser |
| `workflow_profiles.py` | 117 | Conservative workflow profiles for the evolution pipeline |
| `gate_execution.py` | 112 | Process-isolated blocking gate execution |
| `pipeline_contracts.py` | 111 | Code-level stage contracts registry (single source of truth) |
| `official_job_envelope.py` | 106 | Signed official worker job envelope |
| `system_log.py` | 105 | Structured system event logger (`system_events.jsonl`) |
| `national_arena/models.py` | 105 | Arena session/event wire models |
| `evolution_core.py` | 98 | Core business logic re-export facade for backward compatibility |
| `api_concurrency.py` | 88 | Adaptive concurrency control under API rate-limiting/backoff |

**Supporting subdirectories:**
- `engine/` — Copy of `engine/battle.py` + modified `engine/judge.py` + `aivat.py` for web context
- `probe_bots/` — Probe bots (always_caller, check_raiser, min_bettor, overbettor) for exploitability scoring
- `reference_bots/bot1`-`bot6` — Reference bot implementations used by the evolution pipeline
- `prompts/` — LLM prompt templates for orchestration, planning, workflow-specific workers, review, critic, crossover, official evidence analysis, audits, experience updates, and dynamic tests. Use the files present in `web/core/prompts/` as authoritative; this directory currently contains 31 templates.

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
| `engine/battle.py` | 899 | CLI battle runner, mirror_battle, battle_generator, human_battle_generator |
| `engine/judge.py` | 592 | Stateless Holdem judge: Holdem class, Suit/HandType/Card enums, judge() function |
| `engine/anchor_runner.py` | 642 | One bot vs all others, supports --dry-run, --exclude, per-opponent parallel workers |
| `engine/aivat.py` | 488 | AIVAT all-in variance reduction for heads-up mirror battles |

### Scripts

| Script | Purpose |
|---|---|
| `scripts/botzone_upload_match.py` | Full Botzone client: upload, rooms, matches, ranking |
| `scripts/botzone_room_series.py` | Batch room matches on Botzone |
| `scripts/botzone_multi_account_upload.py` | Multi-account bot upload |
| `scripts/ref_strategy_labels.py` | Offline strategy analysis / labeling |
| `scripts/reset_evolution.py` | Reset evolution to baseline (keeps v1-v6) |
| `scripts/test_claude_cli.py` | Claude CLI testing utility |
| `archive/cleanup_20260708/root_experiments/merge_bot.py` | Archived legacy multi-file bot merge helper |
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

### Dual-checkout sync rule

The intended local layout has two checkouts under `/home/zzx/project/pok`:

- `/home/zzx/project/pok` is the operator/infrastructure checkout. Make ordinary code, prompt, test, and documentation changes here, or in a temporary ignored worktree under this directory.
- `/home/zzx/project/pok/.evolution_pok` is a separate clone reserved for the autonomous evolution process. Long-running `web/main.py`, the rating daemon, live candidate bot directories, and runtime result files belong there.

Synchronize both checkouts only through `origin/main`; do not copy files between them. Infrastructure changes from the outer checkout must be pushed and then fetched/merged into `.evolution_pok` at a safe point. Completed evolution bots from `.evolution_pok` must be pushed with `national-bot-v{N}` tags and then fetched/merged into the outer checkout before related work continues. See `docs/evolution-dual-checkout-sync-policy.md` for the full policy and command checklist.

Before starting work, update remote state. In a clean checkout on the branch you will edit, run `git pull --ff-only --tags`; if the checkout is dirty, on a user branch, or cannot be fast-forwarded safely, run `git fetch --tags origin` and create a temporary worktree from the updated `origin/main` instead of working from a stale local HEAD.

Do not switch branches, reset, or do normal infrastructure development inside `.evolution_pok` while a generation is running. Contract-neutral changes may be tolerated by `web/core/evaluation_contract.py` and `web/core/publish_reconcile.py`; do not treat whole directories such as `engine/`, `sever/`, `web/core/`, `web/tests/`, or every `bots/national_v*/` directory as stop conditions. The active exact-file contract in `web/core/evaluation_contract.py`, plus the current candidate/source/parent/opponent bot versions recorded in the checkpoint, determines whether an incoming change requires an explicit evolution restart/resume decision.

Important generated or runtime locations:

- `.evolution_pok/`
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
- 开始工作前必须先拉取远端状态：干净可快进的工作区先运行 `git pull --ff-only --tags`；如果当前工作区有用户脏改、在用户分支上、或不能安全快进，则先 `git fetch --tags origin`，再从最新 `origin/main` 开临时 worktree 工作。
- 改代码前先从 `main` 开任务分支，默认命名 `codex/<task-name>`；在分支内完成修改和提交，再切回 `main` 合并、push，然后删除该任务分支。任务分支只用于隔离开发，合并后即失去意义，不要长期保留已合并的 `codex/*` 分支。
- 只改当前任务需要的文件，不顺手重构、不统一无关风格、不碰运行产物。
- 协议、adapter、THP、card mapping、质量门、进化提示词等边界变更必须配套测试或至少明确的冒烟验证。
- Web 进化相关变更要同时检查 Python 逻辑、prompt 文档、quality gates，避免旧提示词继续生成旧行为。
- 文档与代码同步：当模块结构、文件清单、行数、关键常量值（如 Glicko-2 的 TAU=0.3）或 prompt 模板数量发生变动时，必须同步更新 CLAUDE.md 与 AGENTS.md 中的模块地图、行数表及相关计数，避免文档与代码漂移；完成代码改动后，顺手校正当前任务触及部分的文档。
- 最终汇报必须说明改了什么、跑了什么验证、提交/推送结果，以及哪些已有脏项未触碰。

Stage only the files changed for the current task. Do not use `git add -A` unless the user explicitly asks for a full repository snapshot. Runtime/generated paths such as `web/core/results/`, `web/logs/`, `web/frontend/dist/`, `web/server/static/`, `results/*.json`, `ladder_results/`, `bots/graveyard/`, and `.completed` sentinels should not be staged unless the task is specifically about them.

Evolution-generated bot versions are complete only when the orchestrator `commit_bot` flow has passed its gates, committed the bot, and created the annotated `national-bot-v{N}` tag. Do not hand-edit bot lineage tags or `.completed` sentinels unless the task is explicitly about evolution recovery.

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
git branch -d codex/<task-name>   # 合并并推送后删除任务分支，已合并分支用 -d 安全删除
```

If the repository is dirty before the task, mention that in the final response and avoid mixing unrelated files into the commit. If commit or push fails because of credentials, remote state, hooks, or network problems, report the exact failure and leave the worktree otherwise intact.
