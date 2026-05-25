# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Texas Hold'em poker AI bot framework (Chinese: 德州扑克). The project develops and benchmarks heads-up (2-player) No-Limit Texas Hold'em bots for the Botzone platform (botzone.org.cn). Bots are written in Python, tested locally, then uploaded to compete on the Botzone leaderboard.

## Common Commands

### Local Bot vs Bot
```bash
python engine/battle.py bots/bot5/main.py bots/bot4/main.py -n 50 -v -d
```
- `-n` number of games (each game = 50 hands, 20000 chips per hand)
- `-v` verbose progress every 10 games
- `-d` debug: print bot stderr output

### Ladder (Round-Robin Tournament)
```bash
python engine/ladder.py -v                    # all bots, 50 games/matchup, 8 workers
python engine/ladder.py -b 1 4 7 -n 20 -j 4  # specific bots, 20 games, 4 workers
python engine/ladder.py --continue ladder_results/ladder_XXX/checkpoint.json -v
```
Results go to `ladder_results/`.

### Anchor Runner (One Bot vs All Others, Mirror Pairs)
```bash
python engine/anchor_runner.py 5 -n 100 -j 24
```
Uses mirror pairs (same deck, swapped hole cards) for fairness. Results go to `ladder_results/`.

### Botzone Upload & Match
```bash
python scripts/botzone_upload_match.py upload --source bots/bot5/main.py --bot-name test --execute
python scripts/botzone_upload_match.py rank-match --bot-name test --execute
python scripts/botzone_upload_match.py run-room-series --bot-name test --execute
```
Credentials via `BOTZONE_EMAIL` / `BOTZONE_PASSWORD` env vars or `--email`/`--password` flags.

## Architecture

### Game Engine (`engine/judge.py`)
- `Holdem` class: full game state machine — dealing, blinds, betting rounds (preflop/flop/turn/river), showdown
- `Card` / `Suit` / `HandType`: card representation and hand evaluation
- `hand_type_of_cards()` / `compare_full_cards()`: 5-card hand classification and comparison
- `find_max_hand_type()`: finds best 5-card hand from 7 cards (exhaustive C(7,5))
- `judge()`: Botzone-compatible entry point; reads JSON log, advances game state, returns JSON
- Protocol: bots receive `{"requests": [...], "responses": [...]}` via stdin, output `{"response": <action>}` via stdout
- Action encoding: `0` = call/check, `-1` = fold, `-2` = all-in, `>0` = raise amount

### Battle Engine (`engine/battle.py`)
- `battle()`: standard head-to-head (subprocess mode, each decision spawns a fresh process)
- `mirror_battle()`: fairness-enhanced — plays each matchup twice with swapped hole cards
- `battle_generator()`: streaming event-based variant (yields display/game_end/match_end events)
- `human_battle_generator()`: human vs bot mode
- Bots communicate via subprocess stdin/stdout JSON, 30s timeout per decision
- Battle results saved to `results/` as JSON

### Bot Protocol
Each bot reads JSON from stdin, writes JSON to stdout:
```python
# Input (from judge via battle engine)
{"requests": [...], "responses": [...], "data": ...}

# Output (from bot)
{"response": <int action>, "data": ...}
```
The `data` field is optional state the bot can persist across decisions within one game.

### Bot Development (`bots/`)
- Each bot in its own directory: `bots/bot<N>/main.py`, numbered sequentially (higher = newer version)
- Each bot is a standalone Python script following the request/response protocol
- Bot 5 (~2500 lines) is the most sophisticated: uses Monte Carlo simulation, opponent modeling (VPIP/PFR/aggression), board texture analysis, draw evaluation, value betting tiers, blocker bluff profiles, match pressure management, and ELO-aware endgame strategy
- Bots evolve iteratively — newer versions typically build upon strategies from earlier ones

### Engine (`engine/`)
- `judge.py`: game state machine, card evaluation, Botzone-compatible judge
- `battle.py`: battle engine (subprocess bots, mirror battles, human mode)
- `ladder.py`: round-robin tournament with ELO rating, parallel execution via subprocess workers
- `anchor_runner.py`: run one bot against all others with parallel mirror pairs

### Scripts (`scripts/`)
- `botzone_upload_match.py`: full Botzone client — upload code, create rooms, start matches, parse results, archive logs (handles login, captcha, Socket.IO room protocol)
- `botzone_room_series.py`: batch Botzone room matches against ranked opponents
- `botzone_multi_account_upload.py`: multi-account batch upload to Botzone
- `ref_strategy_labels.py`: offline strategy analysis tool

## Key Conventions

- Cards represented as integers 0–51: `number = card // 4 + 2` (2–14 = 2–A), `suit = card % 4` (0=heart, 1=diamond, 2=spade, 3=club)
- Each game is 50 hands, starting chips 20000, small blind 50, big blind 100
- Botzone game ID: `63dcfaddee1bce5e6c8f4b53` (2-player Texas Hold'em)
- All code is Python 3, no external dependencies for bots or core engine
