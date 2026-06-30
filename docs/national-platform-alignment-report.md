# National Platform Alignment Report

Date: 2026-06-30

## Scope

This repository has two active poker protocols that must stay separate:

- `engine/` and `web/` evolve Botzone/local JSON subprocess bots. A bot reads JSON from stdin and writes `{"response": int}` to stdout.
- `sever/` implements the national competition TCP platform. AI engines connect as TCP clients and send line-delimited text actions.

The Web evolution application is not a native TCP bot platform. Its national-competition path is: evolve a JSON bot under `bots/claude_v*/`, then deploy it to `sever/` through `sever/bot_adapter.py`. The adapter is therefore part of the production compatibility boundary.

## Authoritative Competition Facts

The implementation is aligned to the documents under `sever/国赛平台/`:

- TCP transport. Platform is server, AI engine is client.
- Default TCP port is `10001`; `sever/main.py` also starts the Web dashboard on `18080`.
- One match has 70 hands. Each hand resets both players to 20000 chips.
- Blinds are 50/100. Small blind acts first preflop; big blind acts first on flop, turn, and river.
- Each decision has a 60 second timeout. Timeout is treated as fold.
- Legal client action tokens are `raise <amount>`, `fold`, `call`, `check`, and `allin`.
- `bet` is not a legal wire token. The protocol uses `raise` for both first bets and raises.
- `raise X` means raise to total stage bet `X`, not add `X`.
- The parser accepts `raise <amount>` only with exactly one space. Leading/trailing spaces, tabs, or extra spaces are illegal protocol formats.
- Postflop first action `call` is illegal. Postflop after any first action, `check` is illegal; after the first player checks, the second player sends `call` to pass the street.
- After an `allin` is called, the server runs out remaining public cards and settles the hand; clients should not act again before `earnChips`.
- The repository intentionally keeps the strict re-raise rule: a re-raise must be greater than 2x the previous raise-to value. This follows the `raise 400` then `raise 801` example, even though the wording in the documents is easy to misread as allowing equality.

## End-to-End TCP Flow

1. `sever/main.py` starts the TCP server and the FastAPI/SSE dashboard.
2. Each client connects and receives `name`; it replies with its team/bot name.
3. When the second client connects, `MatchManager` starts the match automatically. The `/api/start` endpoint remains a dashboard fallback and rejects duplicate running tasks.
4. For each hand, the server sends `preflop|SMALLBLIND|...` and `preflop|BIGBLIND|...`, including each player's hole cards.
5. The server validates each client action with `sever/engine/validator.py`. Illegal actions are converted to fold.
6. Opponent actions are forwarded as text. Street messages are sent as `flop|...`, `turn|...`, and `river|...`.
7. If all-in is called before river, the server auto-deals remaining public cards, records those cards in THP, and suppresses further decisions for that hand.
8. Settlement sends `earnChips <amount>` to both clients. Showdown hands are sent with `oppo_hands|...`.
9. At match end, `sever/engine/thp_recorder.py` exports a GB2312 THP file under `sever/records/`.

## THP Alignment

THP output uses national record structure:

- Filename style: `THP-{teamA} vs {teamB}-{winner}胜-{yyyymmddHHMM}-CCGC.txt`, sanitized for filesystem-unsafe characters.
- Each hand line: `STATE:N:actions:cards:earnings:players;`.
- Actions: `r{amount}` for bet/raise, `c` for check/call, `f` for fold, `/` between streets.
- Cards: rank/suit strings such as `Ah` and `Ts`.
- Hand cards are recorded big blind first, then small blind.
- Earnings and player names are also recorded in the hand's big-blind-first order.
- Export encoding is GB2312.

## Web Evolution Alignment

The Web app remains centered on local JSON subprocess bots and mirror battles. The national TCP alignment work adds guardrails rather than changing the core evolution protocol:

- `web/core/prompts/worker_prompt.md`, `reviewer_prompt.md`, `master_prompt.md`, `initial_prompt.md`, and `crossover_prompt.md` now state that evolved bots must remain JSON bots and that TCP deployment goes through `sever/bot_adapter.py`.
- Prompts now forbid relying on `bet` as a wire token, positive raise values that consume the remaining stack, and postflop TCP `check-check`.
- `web/core/prompts/dynamic_test_generator.md` now uses `raise###` in action histories instead of `bet###`.
- `web/core/code_verification.py` exposes `run_national_protocol_tests()`.
- `web/core/tool_gates.py` runs `sever/tests/test_national_alignment.py` during `run_quality_gates`, so adapter/platform protocol drift blocks bot commits.

The existing frontend match replay and rating dashboards still display local JSON battle data from `web/core/results/`. They are not THP parsers and do not claim to visualize national TCP records.

## Code Changes Made

- `sever/server/protocol.py`: strict action parsing; exact `raise <amount>` spacing; `bet` recognized only as illegal, not normalized.
- `sever/engine/validator.py`: postflop second `check` is illegal; postflop pass after check is `call`; raise amounts must be present, positive, and greater than current player bet.
- `sever/engine/game.py`: all-in runout public cards are recorded in THP; postflop `check-check` shortcut removed.
- `sever/server/tcp_server.py`: line reads preserve protocol-invalid spaces; matches auto-start after two clients connect; THP filenames include event suffix and are sanitized.
- `sever/web/app.py`: `/api/start` respects the active match task and avoids duplicate starts.
- `sever/engine/thp_recorder.py`: earnings and player fields use BB|SB order per hand.
- `sever/bot_adapter.py`: all-in runout mode suppresses extra actions; JSON `0` after a postflop check maps to TCP `call`; stack-consuming positive raises become `allin`.
- `sever/test_client.py`: smoke client uses postflop `call` after opponent check and suppresses runout actions after all-in call.
- `sever/tests/test_national_alignment.py`: regression tests cover parser strictness, validator edge cases, THP ordering/runout, and adapter action conversion.

## Validation

Current validation commands:

```bash
python -m py_compile sever/main.py sever/server/tcp_server.py sever/server/protocol.py sever/engine/game.py sever/engine/validator.py sever/engine/thp_recorder.py sever/bot_adapter.py sever/test_client.py sever/tests/test_national_alignment.py
python -m py_compile web/core/code_verification.py web/core/evolution_core.py web/core/tool_gates.py
python -m pytest sever/tests/test_national_alignment.py -q
```

Expected result at the time of this report: py_compile passes and `9 passed` for the national alignment test file.

## Remaining Boundaries

- The Web evolution pipeline still evaluates local JSON bots with `engine/battle.py`; it does not run every candidate through a live TCP match. The new national protocol test gate protects the shared adapter/platform semantics.
- The dashboard replay UI is for local JSON battle replays, not THP files.
- Team name validation is still permissive at connection time. Filenames are sanitized on export, but strict competition-side name enforcement could be added if exact reference-platform behavior is required.
