<!-- From: /Users/zhouzixiang/Documents/pok/AGENTS.md -->
# AGENTS.md — Poker Bot Evolution Framework

This file provides essential context for AI coding agents working with this repository. The project is a Texas Hold'em poker AI bot framework targeting the Botzone online platform (botzone.org.cn), with a sophisticated LLM-driven evolution pipeline for iterative bot improvement.

---

## Project Overview

This project develops and benchmarks heads-up (2-player) No-Limit Texas Hold'em bots. It has two main facets:

1. **Game Engine & Battle System** (`engine/`): A complete local poker engine with card evaluation, game state machine, subprocess-based bot battles, mirror-pair fairness matches, round-robin ELO tournaments, and anchor benchmarking.
2. **LLM-Driven Evolution** (`web/`): The unified entry point containing all evolution logic, web backend, and dashboard frontend. It implements an automated multi-agent pipeline where LLMs (Master Architect → Workers → Reviewer → Critic) iteratively improve bots based on match results, Glicko-2 ratings, and an experience pool of strategic lessons.

Bots are written in Python (standard library only), tested locally, then uploaded to compete on Botzone. The evolution system can run via web dashboard (FastAPI + React), Textual TUI, or headless CLI.

---

## Technology Stack

| Layer | Tech |
|-------|------|
| Language | Python 3 (bots, engine, evolution) |
| Frontend | React 19 + Vite + Tailwind CSS 4 + TypeScript |
| Backend | FastAPI + uvicorn + sse-starlette |
| Bot Engine | Pure Python stdlib (no external deps) |
| Evolution SDK | `claude_agent-sdk` (async LLM queries) |
| Rating Systems | Glicko-2 (evolution), ELO (ladder) |
| Bot Platform | Botzone (botzone.org.cn) |

**Key external dependencies (evolution/dashboard only):**
- `rich`, `textual` (TUI)
- `fastapi`, `uvicorn`, `sse-starlette` (dashboard backend)
- `claude_agent-sdk` (LLM orchestration)
- Node.js/npm (frontend build)

**Bots and core engine have zero external dependencies.**

---

## Project Structure

```
.
├── engine/                     # Core game and battle engine
│   ├── judge.py                # Holdem state machine, card eval, Botzone protocol
│   ├── battle.py               # Subprocess battles, mirror battles, human mode
│   ├── ladder.py               # Round-robin ELO tournament with parallel workers
│   └── anchor_runner.py        # One bot vs all others, mirror pairs, parallel execution
│
├── bots/                       # All bot implementations
│   ├── bot1/ .. bot6/          # Hand-crafted baseline bots (modular multi-file)
│   ├── claude_v1/ .. v{N}/     # LLM-evolved bots (single-file or modular)
│   └── graveyard/              # Culled weak bots (gitignored)
│
├── web/                        # Unified evolution entry point
│   ├── main.py                 # Unified launcher: web server OR Textual TUI
│   ├── tui.py / tui.tcss       # Textual TUI dashboard (frontend-inspired dark theme)
│   ├── requirements.txt        # Python backend deps (fastapi, uvicorn, sse-starlette, pydantic)
│   ├── core/                   # All evolution business logic
│   │   ├── evolution_core.py   # Main loop, LLM orchestration, ratings, git mgmt
│   │   ├── orchestrator.py     # LLM-driven orchestrator with MCP tools
│   │   ├── tools.py            # MCP server tool definitions
│   │   ├── elo_daemon.py       # Background Glicko-2 rating daemon
│   │   ├── glicko2.py          # Glicko-2 math implementation
│   │   ├── web_ui.py           # SSE broadcaster + WebUI bridge
│   │   ├── commentary.py       # Match commentary generation
│   │   ├── smoke_tester.py     # 1-mirror-game crash test
│   │   ├── decision_tester.py  # Scenario-based decision validation
│   │   ├── test_scenarios.json # Decision test scenarios (preflop/flop/turn/river)
│   │   ├── experience_pool.md  # Accumulated strategic lessons
│   │   ├── prompts/            # LLM prompt templates
│   │   │   ├── initial_prompt.md
│   │   │   ├── master_prompt.md
│   │   │   ├── worker_prompt.md
│   │   │   ├── reviewer_prompt.md
│   │   │   ├── crossover_prompt.md
│   │   │   ├── critic_prompt.md
│   │   │   └── orchestrator.md
│   │   ├── reference_bots/     # Baseline bots (bot1-bot6) for seeding
│   │   └── results/            # Ratings, logs, history, match replays
│   ├── server/                 # FastAPI backend (modular routes)
│   │   ├── app.py              # FastAPI app with lifespan
│   │   ├── state.py            # Thread-safe global state
│   │   └── routes/             # API route modules
│   │       ├── bots.py
│   │       ├── control.py
│   │       ├── data_stream.py
│   │       ├── evolution.py
│   │       ├── logs.py
│   │       ├── matches.py
│   │       ├── pipeline.py
│   │       ├── prompts.py
│   │       └── ratings.py
│   └── frontend/               # React 19 + Vite + Tailwind dashboard
│       ├── package.json        # Frontend deps and build scripts
│       ├── vite.config.ts      # Vite config with /api proxy
│       ├── tsconfig.json       # TypeScript project references
│       └── src/
│           ├── App.tsx
│           ├── api/            # Typed fetch wrappers (client.ts, types.ts)
│           ├── components/     # Reusable UI (PokerTable, charts, common, ui)
│           ├── context/        # React contexts (DataProvider, Sidebar, Theme)
│           ├── hooks/          # Custom hooks (useGoBack, useModal)
│           ├── icons/          # SVG icons
│           ├── layout/         # AppLayout, AppHeader, AppSidebar
│           └── pages/          # 10 pages: Overview, EvolutionMonitor, BotManager, etc.
│
├── archive/                    # Archived legacy directories (preserved for history)
│   ├── evolution_workspace/    # Old evolution core (superseded by web/core/)
│   ├── orchestrator/           # Old orchestrator (merged into web/core/)
│   └── dashboard/              # Old dashboard (merged into web/server/ + web/frontend/)
│
├── scripts/                    # Botzone integration tools
│   ├── botzone_upload_match.py # Full Botzone client (upload, rooms, matches)
│   ├── botzone_room_series.py  # Batch room matches
│   ├── botzone_multi_account_upload.py
│   ├── ref_strategy_labels.py  # Offline strategy analysis
│   ├── reset_evolution.py      # Reset evolution to baseline (v1-v6)
│   └── test_claude_cli.py
│
├── docs/
│   └── multi_ai_bot_design.md  # Multi-AI evolution design document (Chinese)
│
├── ref/                        # Botzone reference materials
│   ├── player_api.js           # Botzone player API
│   └── TexasHoldem2p.html      # Game viewer HTML
│
├── results/                    # Local battle result JSONs (gitignored)
├── ladder_results/             # Ladder and anchor runner outputs
│
├── .gitignore                  # Excludes __pycache__, results/*.json, .env, graveyard/
├── .vscode/settings.json       # VS Code: conda env manager
├── AGENTS.md                   # This file
├── CLAUDE.md                   # Claude Code guidance (root level)
├── web/CLAUDE.md               # Claude Code guidance (web-specific)
└── ONBOARDING.md               # Team onboarding guide
```

---

## Build and Run Commands

### Local Bot Battles

```bash
# Standard head-to-head (subprocess mode, each decision spawns fresh process)
python engine/battle.py bots/bot5/main.py bots/bot4/main.py -n 50 -v -d
# -n: number of games (each game = 50 hands, 20000 chips per hand)
# -v: verbose progress every 10 games
# -d: debug: print bot stderr output

# Ladder (round-robin ELO tournament)
python engine/ladder.py -v                              # all bots, 50 games/matchup, 8 workers
python engine/ladder.py -b 1 4 7 -n 20 -j 4            # specific bots, 20 games, 4 workers
python engine/ladder.py --continue ladder_results/ladder_XXX/checkpoint.json -v
# Results go to ladder_results/

# Anchor runner (one bot vs all others, mirror pairs)
python engine/anchor_runner.py 5 -n 100 -j 24
# Results go to ladder_results/
```

### Evolution System (Unified Web Entry Point)

```bash
# Web server mode (default: orchestrator + daemon + frontend)
python web/main.py

# Classic evolution loop (web server)
python web/main.py --mode classic

# Manual mode (daemon only, no evolution loop)
python web/main.py --mode manual

# Textual TUI mode (frontend-inspired dashboard in terminal)
python web/main.py --tui
python web/main.py --tui --mode classic
python web/main.py --tui --no-daemon --workers 8 --pairs 3

# Standalone TUI (direct)
python web/tui.py --mode orchestrator
python web/tui.py --mode classic --no-daemon

# Standalone background daemon
python web/core/elo_daemon.py --pairs 5 --workers 28 --verbose

# Smoke test (1 mirror game vs reference bot)
python web/core/smoke_tester.py bots/claude_v1/main.py

# Decision scenario tests
python web/core/decision_tester.py bots/claude_v11/main.py --verbose
```

### Web Dashboard

```bash
# Development mode (backend + frontend dev servers)
cd web/frontend && npm run dev   # Vite dev server on :5173, proxies /api to :8000
python web/main.py --dev         # FastAPI backend on :8000 with auto-reload

# Production mode (build frontend, serve from FastAPI)
python web/main.py --no-build    # Skip build if already done

# Frontend build only
cd web/frontend && npm run build  # Outputs to web/server/static/
```

### Botzone Upload & Match

```bash
# Upload bot code to Botzone
python scripts/botzone_upload_match.py upload --source bots/bot5/main.py --bot-name test --execute

# Ranked match on Botzone
python scripts/botzone_upload_match.py rank-match --bot-name test --execute

# Run room series
python scripts/botzone_upload_match.py run-room-series --bot-name test --execute

# Reset evolution to baseline (keeps v1-v6, deletes everything above)
python scripts/reset_evolution.py --force --keep 6

# Credentials via BOTZONE_EMAIL / BOTZONE_PASSWORD env vars or --email/--password flags
```

---

## Bot Protocol & Conventions

### Communication Protocol

Each bot is a standalone Python script that reads JSON from stdin and writes JSON to stdout:

```python
# Input (from judge via battle engine)
{
  "requests": [...],      # list of request dicts for this player
  "responses": [...],     # list of this player's previous actions
  "data": ...             # optional persistent state (bot-defined)
}

# Output (from bot)
{"response": <int action>, "data": ...}
```

### Action Encoding

| Value | Meaning |
|-------|---------|
| `0` | Call / Check |
| `-1` | Fold |
| `-2` | All-in |
| `>0` | Raise amount (must be ≥ `round_raise` and ≤ `chips - 1`) |

### Card Representation

- Cards are integers `0–51`
- Rank: `card // 4 + 2` (2–14, where 14 = Ace)
- Suit: `card % 4` (0=Heart♥, 1=Diamond♦, 2=Spade♠, 3=Club♣)

### Game Parameters

- 2 players, No-Limit Texas Hold'em
- 50 hands per match, 20000 starting chips per hand
- Small blind = 50, Big blind = 100
- Decision timeout = 30 seconds (subprocess timeout)
- Botzone game ID: `63dcfaddee1bce5e6c8f4b53`

### Code Constraints for Bots

- **Single-file Python script** preferred (or modular multi-file for complex bots)
- **No external dependencies** (stdlib only: `json`, `random`, `itertools`, `math`, `collections`, etc.)
- Use `.get(key, default)` for all field access; do not assume fields exist
- Handle edge cases: first hand (no history), all-in situations, opponent crashes
- Must output valid JSON within 30 seconds; no extraneous stdout
- Monte Carlo simulation counts should have an upper bound (existing bots use 500–900)
- Single-file bots have a soft limit of ~1000 lines

---

## Code Style Guidelines

- Python 3 only. No Python 2 compatibility required.
- Bots: keep entry points simple; delegate to strategy modules for complex bots.
- Comments: mix of English and Chinese is acceptable (project convention). Engine core files tend to have Chinese comments; evolution workspace uses English docstrings.
- Prefer explicit over implicit. Use `.get()` for dict access in bot protocol handling.
- Bots must sanitize actions before output (see `sanitize_action()` patterns in `bots/bot5/main.py` and `bots/bot6/main.py`).
- Use `json.dumps(payload, separators=(',', ':'))` for compact JSON in subprocess communication.
- No formal Python packaging; modules use `sys.path.insert()` to resolve imports.

---

## Testing and Evaluation

### Three-Layer Evaluation

| Layer | Tool | Purpose |
|-------|------|---------|
| Quick | `battle()` 10–20 games | Validate code runs, basic strategy works |
| Standard | `mirror_battle()` 50 games | Fair evaluation (swapped hole cards) |
| Full | `ladder.py` round-robin | Comprehensive ranking with ELO/Glicko-2 |

### Mirror Battle Fairness

Each matchup plays twice with the same deck:
1. **Normal game**: standard deal
2. **Mirror game**: swap hole cards (deck[-4:] rearranged), same community cards

Winner determined by combined chip difference across both games. This eliminates luck from hole card variance.

### Quality Gates in Evolution

1. **Syntax check**: `python -c "import py_compile; py_compile.compile('bot.py')"`
2. **Smoke test**: 1 mirror game vs reference bot to catch crashes
3. **Decision tests**: Scenario validation (`decision_tester.py` + `test_scenarios.json`)
4. **Code size check**: Single-file ≤ 1000 lines
5. **Basic competitiveness**: Must win at least some games

### Decision Test Scenarios

`web/core/test_scenarios.json` contains ~15 predefined poker scenarios testing for catastrophic blunders:
- AA/KK/QQ preflop: must not fold
- 7-2 offsuit: must not go all-in
- Top set on dry board: must not fold
- Nut hands on river: must bet for value

Pass rate threshold: **≥70%**.

---

## Rating Systems

- **ELO** (ladder): Initial 1200, K=40 (first 30 games), K=20 (stable). Rank titles: 青铜(<1000), 白银, 黄金, 铂金, 钻石, 大师, 王者(2000+).
- **Glicko-2** (evolution): Initial r=1500, rd=350, sigma=0.06. Conservative rating = `r - 2*rd`. RD gates confidence: <50 green, 50-100 yellow, 100-200 orange, >200 red.

---

## Evolution System Architecture

The evolution workspace implements an automated multi-agent LLM pipeline:

### Per-Generation Pipeline

1. **Experience Pool Trim**: Keeps last 8 generation entries (`experience_pool.py`)
2. **Stagnation Detection**: If ≥2 consecutive generations fail to improve, branch from highest-rated bot (git-based lineage)
3. **Reaper**: If active bots > 30, move weakest (by conservative rating) to `bots/graveyard/`
4. **Evaluation**: Daemon mode waits for ≥20 matches + RD < 40. Inline mode runs up to 10 opponents × 5 games.
5. **Master Architect** (`master_prompt.md`): Analyzes ratings, leaderboard top 3, experience pool, reference bots, rating trend. Outputs JSON plan with:
   - **Direction A**: Algorithmic logic changes
   - **Direction B**: Hyperparameter tuning only (no control flow changes)
6. **Workers** (`worker_prompt.md`): Execute tasks in parallel via `asyncio.gather`. Up to 4 retries with compile/smoke test error injection.
7. **Quality Gates**: Code size check + decision tests (≥70% pass rate, no catastrophic blunders)
8. **Reviewer** (`reviewer_prompt.md`): Validates dual-track boundary and plan adherence. 3 retry attempts.
9. **Git Commit**: Structured commit `evolve: v{N} → v{M}` + annotated tag `bot-v{M}`

### Git-Based Version Management

- Every generation: structured commit + annotated tag
- Commit message format: `evolve: v{N} → v{M}\n\nparent: claude_v{N}\nstrategy: ...\nrating: r=... rd=...`
- Lineage traced via `git_get_parent()` parsing tag messages
- Seeded reference bots get tags `bot-v{1-6}`
- Incomplete generations (no `.completed` marker) are rolled back on restart

### Background Daemon (`elo_daemon.py`)

- Continuously runs mirror battles via `ProcessPoolExecutor`
- Match selection: 60% under-evaluated pairs + 40% rating-diverse pairs
- Batch-updates Glicko-2 ratings after each period
- Appends snapshots to `results/rating_history.jsonl`
- File locking (`fcntl`) for concurrent access with manager

---

## Deployment (Botzone)

The primary deployment target is Botzone (botzone.org.cn):

- Max upload size: 4MB (Python bots are well under this)
- Authentication: `BOTZONE_EMAIL` / `BOTZONE_PASSWORD` env vars
- The `scripts/botzone_upload_match.py` handles full lifecycle: login (with captcha), upload, room creation, match execution, result parsing, CSV export, log archiving.
- Room series and multi-account upload are also supported.

---

## Security Considerations

- **Credentials**: Botzone credentials via environment variables. `.env` is gitignored.
- **Subprocess isolation**: Bots run in fresh subprocesses per decision with 30s timeout. Malicious/crashing bots are treated as fold.
- **No network access for bots**: Bots are pure stdlib Python; the battle engine does not grant network access.
- **File locking**: Glicko-2 ratings use `fcntl` shared/exclusive locks to prevent corruption during concurrent daemon/manager access.
- **Cleanup**: `ladder.py` and `anchor_runner.py` have signal handlers and stale process cleanup to prevent orphan workers.

---

## Key Files for Agent Context

| Purpose | File |
|---------|------|
| Game rules & protocol | `engine/judge.py` |
| Battle & mirror logic | `engine/battle.py` |
| ELO tournament | `engine/ladder.py` |
| Anchor benchmarking | `engine/anchor_runner.py` |
| **Unified entry point** | **`web/main.py`** |
| **Textual TUI** | **`web/tui.py`** |
| Evolution core logic | `web/core/evolution_core.py` |
| Orchestrator (LLM agent) | `web/core/orchestrator.py` |
| MCP tools | `web/core/tools.py` |
| Glicko-2 math | `web/core/glicko2.py` |
| Background daemon | `web/core/elo_daemon.py` |
| SSE / WebUI bridge | `web/core/web_ui.py` |
| Decision tests | `web/core/decision_tester.py` + `web/core/test_scenarios.json` |
| Strategic lessons | `web/core/experience_pool.md` |
| LLM prompts | `web/core/prompts/*.md` |
| FastAPI backend | `web/server/app.py` |
| Dashboard frontend | `web/frontend/src/` |
| Botzone client | `scripts/botzone_upload_match.py` |
| Baseline bot (most sophisticated) | `bots/bot5/main.py` + modules |
| Latest evolved bot | `bots/claude_v{N}/main.py` (highest numbered version) |

---

## Post-Task Workflow

After completing each task, always commit and push changes:
```bash
git add -A
git commit -m "<descriptive message>"
git push
```
