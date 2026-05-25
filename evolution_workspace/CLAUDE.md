# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

An automated LLM-driven evolution framework for iteratively improving Texas Hold'em poker bots. A multi-agent LLM pipeline (Master Architect → Workers → Reviewer) analyzes match results, designs improvement tasks, implements changes, and validates output. Bots compete in a Glicko-2 rated population, with a background daemon continuously running matches. A "Reaper" culls weak bots when the pool exceeds 30.

This workspace is a subdirectory of the parent poker project (`/Users/zhouzixiang/Documents/pok`). It imports `engine.battle` from the parent for running matches.

## Running the Evolution

```bash
# From the project root (../)
python evolution_workspace/evolution_manager.py                       # Rich TUI mode (requires `rich`)
python evolution_workspace/evolution_manager.py --no-tui              # Plain text mode
python evolution_workspace/evolution_manager.py --no-daemon           # Inline eval (no background daemon)
python evolution_workspace/evolution_manager.py --workers 8 --pairs 3 # Custom daemon settings
```

The manager is an infinite async loop: it finds the latest completed generation, evaluates it, delegates improvements to LLM workers, reviews the result, and advances to the next generation.

## Standalone Evaluation & Testing

```bash
# Evaluate one bot against opponents (mirror pairs)
python evolution_workspace/fast_evaluator.py bots/claude_v1/main.py bots/bot1/main.py bots/bot2/main.py -n 5 --output-dir results/test

# Smoke test a single bot (1 mirror game vs reference bot)
python evolution_workspace/smoke_tester.py bots/claude_v1/main.py

# Run background rating daemon standalone
python evolution_workspace/elo_daemon.py --pairs 5 --workers 14 --verbose

# Single rating period (daemon runs once then exits)
python evolution_workspace/elo_daemon.py --once
```

## Architecture

### Key Files

```
evolution_workspace/
├── evolution_manager.py   — Main async loop, Rich TUI, Glicko-2 ratings, bot lifecycle, LLM orchestration
├── elo_daemon.py          — Background Glicko-2 rating daemon (ProcessPoolExecutor, continuous battles)
├── glicko2.py             — Glicko-2 rating math (Glicko2Player, update_rating_period, decay_rd)
├── fast_evaluator.py      — Evaluates one bot against multiple opponents → summary.json
├── smoke_tester.py        — Runs 1 mirror game to catch runtime crashes (subprocess, exit code 0/1)
├── experience_pool.md     — Accumulated strategic lessons across generations (auto-appended by Master)
├── prompts/
│   ├── initial_prompt.md  — Genesis agent: creates first bot (v1) from scratch
│   ├── master_prompt.md   — Master Architect: analyzes results, outputs JSON plan with tasks
│   ├── worker_prompt.md   — Worker agent: executes tasks (Direction A: logic / Direction B: hyperparams)
│   ├── reviewer_prompt.md — Reviewer agent: validates worker output, enforces dual-track boundary
│   └── crossover_prompt.md — Crossover agent: combines two elite bots + mutation
├── reference_bots/
│   └── bot{1-6}/          — Fixed baseline bots (multi-file: main.py, strategy.py, state.py, simulation.py, etc.)
└── results/
    ├── glicko_ratings.json    — Glicko-2 ratings for all active bots (file-locked)
    ├── elo_daemon_stats.json  — Daemon match counts per pair + total periods
    ├── v{N}/logs/             — Per-generation LLM conversation logs
    └── round_{N}/             — Anchor runner results from manual evaluation rounds
```

### Multi-Agent LLM Pipeline (per generation)

1. **Reaper** (if >30 bots): Sort by conservative rating (r - 2*rd), move weakest to `bots/graveyard/`.
2. **Evaluation**: Daemon mode: wait for 10 matches via background daemon. Inline mode: run up to 10 opponents × 5 games each via `mirror_battle()`.
3. **Master Architect** (`master_prompt.md`): Analyzes bot rating + leaderboard top 3 + experience pool + reference bots. Outputs JSON plan with tasks split into:
   - **Direction A — Algorithmic Logic Architect**: Refactors/adds logic, fuses algorithms from reference bots.
   - **Direction B — Hyperparameter Tuner**: Only adjusts numeric constants/thresholds. Forbidden from changing control flow.
   - Appends new strategic lessons to `experience_pool.md`.
4. **Workers** (`worker_prompt.md`): Each executes one task from the Master's plan. Workers modify files in the next generation's directory. Up to 4 retries with compile/smoke test error injection.
5. **Reviewer** (`reviewer_prompt.md`): Validates that workers followed the plan and the dual-track boundary (logic vs. hyperparameters). Can reject and force a retry with feedback. 3 retry attempts.

### LLM Invocation (claude-agent-sdk)

- Uses `claude_agent_sdk` Python package — async `query()` with `ClaudeAgentOptions`.
- Configuration: `model="sonnet"`, `permission_mode="bypassPermissions"`, `cwd=project_root`.
- The SDK spawns `claude` CLI with `--output-format stream-json`, inheriting all `~/.claude/settings.json` config.
- Currently connects to GLM-5.1 via `ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic` (Anthropic-compatible API).
- **Structured output NOT supported** by this third-party API. JSON is extracted via regex `parse_json_output()`.
- Typed message handling: `TextBlock` (output), `ThinkingBlock` (shows `[thinking...]`), `ToolUseBlock` (shows `[tool: name]`).
- Cost tracking: `ResultMessage.total_cost_usd` + `usage` captured per agent, displayed in TUI.
- All LLM I/O is logged to `results/v{N}/logs/` (master_io.txt, worker_{id}_io.txt, reviewer_io.txt).

### Glicko-2 Rating System

- Uses `glicko2.py` (local implementation). Each bot is a `Glicko2Player` with rating (r), rating deviation (rd), and volatility (sigma).
- Initial values: r=1500, rd=350, sigma=0.06.
- **Background daemon** (`elo_daemon.py`): Continuously runs mirror battles via `ProcessPoolExecutor`. Prioritizes under-evaluated pairs. Updates ratings after each period (batch of matches).
- Conservative rating: `r - 2*rd` (used for Reaper ranking).
- RD confidence display: <50 very confident (green), 50-100 confident (yellow), 100-200 uncertain (orange), >200 very uncertain (red).
- File locking (`fcntl`) for concurrent access between manager and daemon.

### Bot Lifecycle

- Bots live in `bots/claude_v{N}/` (relative to project root, not this workspace).
- `.completed` marker file indicates a generation finished successfully.
- Incomplete generations (no `.completed`) are rolled back on restart.
- Initial bots (v1-v6) are seeded from `reference_bots/` on first run.
- When active bots exceed 30 (`MAX_ACTIVE_BOTS`), Reaper moves the lowest-rated bot to `bots/graveyard/`.
- Genesis creation (`initial_prompt.md`): if no bots exist at all, creates v1 from scratch.

### Reference Bots

Six fixed baseline bots in `reference_bots/bot{1-6}/`. All share the same modular file structure:
`main.py` (entry), `strategy.py`, `state.py`, `simulation.py`, `postflop.py`, `opponent.py`, `tournament.py`, `card_utils.py`, `constants.py`

| Bot | Strategy |
|-----|----------|
| bot1 | Comprehensive postflop analysis, board texture, pair evaluation |
| bot2 | 169-hand preflop lookup (Chen formula), CBet tracking, concept drift detection, 3Bet/4Bet logic |
| bot3 | EXP3 multi-style meta-learner, dynamic style switching based on opponent |
| bot4 | Balanced fundamentals, solid value tiering and draw analysis |
| bot5 | Anti-exploitation framework, Bot4 detection/counter, gift tracking |
| bot6 | Slim implementation, core fundamentals, minimal complexity |

### UI System (evolution_manager.py)

- `BaseUI` (abstract): interface with methods for history, status, I/O stream, eval table, daemon status, cost tracking.
- `TextUI`: minimal print-based implementation for `--no-tui` mode.
- `EvolutionUI`: Rich terminal UI with multi-panel layout:
  - Header (generation + total cost)
  - Status spinner / Daemon monitor (PID, matches, periods)
  - History log (20 messages with icons)
  - Glicko-2 leaderboard with confidence bars
  - Match feed (recent results)
  - Cost tracker (per-agent costs, generation total, grand total)
  - LLM stream (color-coded: prompt/claude/thinking/tool/error)

## Data Flow

```
evolution_manager.py (async main_loop)
  ├── seeds reference_bots/ → bots/claude_v{1-6}/
  ├── calls run_claude_query() for Master/Worker/Reviewer via claude-agent-sdk
  ├── starts elo_daemon.py subprocess (or --no-daemon for inline eval)
  ├── reads/writes results/glicko_ratings.json (file-locked)
  ├── logs to results/v{N}/logs/
  └── updates experience_pool.md after Master analysis

elo_daemon.py (background process)
  ├── scans bots/ for completed claude_v* directories
  ├── picks under-evaluated pairs → runs mirror_battle() in parallel
  ├── batch-updates Glicko-2 ratings via update_rating_period()
  └── saves to results/glicko_ratings.json + results/elo_daemon_stats.json
```

## Conventions

- Bot protocol: read JSON from stdin, output `{"response": <int>}` to stdout. Action encoding: `0`=call/check, `-1`=fold, `-2`=all-in, `>0`=raise amount.
- Cards: integers 0-51. `number = card // 4 + 2` (2-14), `suit = card % 4`.
- Each game: 50 hands, 20000 chips, SB=50, BB=100.
- All Python 3. Bots/engine have no external dependencies. Manager requires `rich` and `claude-agent-sdk` packages.
