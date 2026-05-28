# AGENTS.md — Poker Bot Evolution Framework

This file provides essential context for AI coding agents working with this repository. The project is a Texas Hold'em poker AI bot framework targeting the Botzone online platform (botzone.org.cn), with a sophisticated LLM-driven evolution pipeline for iterative bot improvement.

---

## Project Overview

This project develops and benchmarks heads-up (2-player) No-Limit Texas Hold'em bots. It has two main facets:

1. **Game Engine & Battle System** (`engine/`): A complete local poker engine with card evaluation, game state machine, subprocess-based bot battles, mirror-pair fairness matches, round-robin ELO tournaments, and anchor benchmarking.
2. **LLM-Driven Evolution** (`evolution_workspace/`): An automated multi-agent pipeline where LLMs (Master Architect → Workers → Reviewer) iteratively improve bots based on match results, Glicko-2 ratings, and an experience pool of strategic lessons.

Bots are written in Python (standard library only), tested locally, then uploaded to compete on Botzone. The evolution system can run headless or via a React/Vite dashboard with SSE streaming.

---

## Technology Stack

| Layer | Tech |
|-------|------|
| Language | Python 3 (bots, engine, evolution) |
| Frontend | React 19 + Vite + Tailwind CSS 4 + TypeScript |
| Backend | FastAPI + uvicorn + sse-starlette |
| Bot Engine | Pure Python stdlib (no external deps) |
| Evolution SDK | `claude-agent-sdk` (async LLM queries) |
| Rating Systems | Glicko-2 (evolution), ELO (ladder) |
| Bot Platform | Botzone (botzone.org.cn) |

**Key external dependencies (evolution/dashboard only):**
- `rich`, `textual` (TUI)
- `fastapi`, `uvicorn`, `sse-starlette` (dashboard backend)
- `claude-agent-sdk` (LLM orchestration)
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
│   ├── claude_v1/ .. v17/      # LLM-evolved bots (single-file or modular)
│   └── graveyard/              # Culled weak bots (gitignored)
│
├── evolution_workspace/        # LLM-driven evolution framework
│   ├── evolution_manager.py    # Thin entry point (TUI or text mode)
│   ├── evolution_core.py       # Main loop, LLM orchestration, ratings, git mgmt
│   ├── elo_daemon.py           # Background Glicko-2 rating daemon
│   ├── glicko2.py              # Glicko-2 math implementation
│   ├── tui.py / tui.tcss       # Textual TUI dashboard
│   ├── fast_evaluator.py       # One bot vs multiple opponents
│   ├── smoke_tester.py         # 1-mirror-game crash test
│   ├── decision_tester.py      # Scenario-based decision validation
│   ├── test_scenarios.json     # Decision test scenarios
│   ├── experience_pool.md      # Accumulated strategic lessons
│   ├── prompts/                # LLM prompt templates
│   │   ├── initial_prompt.md   # Genesis bot creation
│   │   ├── master_prompt.md    # Analysis & planning
│   │   ├── worker_prompt.md    # Code improvement tasks
│   │   ├── reviewer_prompt.md  # Output validation
│   │   └── crossover_prompt.md # Bot crossover + mutation
│   ├── reference_bots/         # Baseline bots (bot1-bot6) for seeding
│   └── results/                # Ratings, logs, history
│
├── orchestrator/               # Higher-level LLM orchestration layer
│   ├── orchestrator.py         # Continuous evolution via LLM agent loop
│   ├── tools.py                # MCP server tools for orchestrator
│   └── prompts/orchestrator.md
│
├── dashboard/                  # Web dashboard for monitoring evolution
│   ├── backend/app.py          # FastAPI with integrated evolution loop
│   ├── backend/web_ui.py       # SSE broadcaster + WebUI bridge
│   ├── backend/commentary.py   # Match commentary generation
│   ├── frontend/               # React + Vite app
│   └── start.sh                # Dev/prod startup script
│
├── scripts/                    # Botzone integration tools
│   ├── botzone_upload_match.py # Full Botzone client (upload, rooms, matches)
│   ├── botzone_room_series.py  # Batch room matches
│   ├── botzone_multi_account_upload.py
│   └── ref_strategy_labels.py  # Offline strategy analysis
│
├── docs/
│   └── multi_ai_bot_design.md  # Multi-AI evolution design document (Chinese)
│
├── ref/                        # Botzone reference materials
│   ├── player_api.js           # Botzone player API
│   └── TexasHoldem2p.html      # Game viewer HTML
│
└── results/                    # Local battle result JSONs
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

### Evolution System

```bash
# Textual TUI mode (default)
python evolution_workspace/evolution_manager.py

# Plain text mode
python evolution_workspace/evolution_manager.py --no-tui

# Inline evaluation (no background daemon)
python evolution_workspace/evolution_manager.py --no-daemon

# Custom daemon settings
python evolution_workspace/evolution_manager.py --workers 8 --pairs 3

# Standalone background daemon
python evolution_workspace/elo_daemon.py --pairs 5 --workers 14 --verbose

# Fast evaluation (one bot vs opponents)
python evolution_workspace/fast_evaluator.py bots/claude_v1/main.py bots/bot1/main.py bots/bot2/main.py -n 5 --output-dir results/test

# Smoke test (1 mirror game vs reference bot)
python evolution_workspace/smoke_tester.py bots/claude_v1/main.py

# Decision scenario tests
python evolution_workspace/decision_tester.py bots/claude_v11/main.py --verbose
```

### Dashboard

```bash
# Development mode (backend + frontend dev servers)
./dashboard/start.sh
# Backend: http://localhost:8000
# Frontend dev: http://localhost:5173

# Production mode (build frontend, serve from FastAPI)
./dashboard/start.sh --build

# Dashboard only, no evolution
./dashboard/start.sh --no-evolve
```

### Botzone Upload & Match

```bash
# Upload bot code to Botzone
python scripts/botzone_upload_match.py upload --source bots/bot5/main.py --bot-name test --execute

# Ranked match on Botzone
python scripts/botzone_upload_match.py rank-match --bot-name test --execute

# Run room series
python scripts/botzone_upload_match.py run-room-series --bot-name test --execute

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

### Rating Systems

- **ELO** (ladder): Initial 1200, K=40 (first 30 games), K=20 (stable). Rank titles: 青铜(<1000), 白银, 黄金, 铂金, 钻石, 大师, 王者(2000+).
- **Glicko-2** (evolution): Initial r=1500, rd=350, sigma=0.06. Conservative rating = `r - 2*rd`. RD gates confidence: <50 green, 50-100 yellow, 100-200 orange, >200 red.

---

## Code Style Guidelines

- Python 3 only. No Python 2 compatibility required.
- Bots: keep entry points simple; delegate to strategy modules for complex bots.
- Comments: mix of English and Chinese is acceptable (project convention). Engine core files tend to have Chinese comments; evolution workspace uses English docstrings.
- Prefer explicit over implicit. Use `.get()` for dict access in bot protocol handling.
- Bots must sanitize actions before output (see `sanitize_action()` patterns in `bots/bot5/main.py` and `bots/bot6/main.py`).
- Use `json.dumps(payload, separators=(',', ':'))` for compact JSON in subprocess communication.

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
| Evolution entry | `evolution_workspace/evolution_manager.py` |
| Evolution core logic | `evolution_workspace/evolution_core.py` |
| Glicko-2 math | `evolution_workspace/glicko2.py` |
| Background daemon | `evolution_workspace/elo_daemon.py` |
| Decision tests | `evolution_workspace/decision_tester.py` + `test_scenarios.json` |
| Strategic lessons | `evolution_workspace/experience_pool.md` |
| LLM prompts | `evolution_workspace/prompts/*.md` |
| Dashboard backend | `dashboard/backend/app.py` |
| Dashboard startup | `dashboard/start.sh` |
| Botzone client | `scripts/botzone_upload_match.py` |
| Baseline bot (most sophisticated) | `bots/bot5/main.py` + modules |
| Latest evolved bot | `bots/claude_v16/main.py` (or highest v{N}) |
