# AGENTS.md - Poker Bot Evolution Framework

This file is the working map for AI coding agents in this repository. The project started as a Botzone Texas Hold'em bot evolution framework, but the current repository has four important code paths that should not be collapsed into one mental model:

1. `engine/` - local Botzone-style subprocess battle engine for Python bots.
2. `web/` - unified evolution system, FastAPI backend, and React dashboard.
3. `sever/` - national competition TCP self-play platform based on the documents in `sever/国赛平台/`.
4. `rl/` - reinforcement learning experiments that wrap the local Hold'em engine.

The old `web/tui.py` Textual TUI no longer exists. Treat `web/main.py` as a web app launcher, not a TUI or mode-switching CLI.

---

## Project Overview

The main goal is to build, evaluate, and evolve heads-up No-Limit Texas Hold'em bots. There are two protocol families:

- Botzone/local protocol: bots are Python subprocesses that read JSON from stdin and write JSON to stdout. This is implemented by `engine/` and kept for legacy regression and archived Botzone-era bots.
- National competition protocol: AI engines connect to a TCP server and exchange raw short socket messages such as `preflop|SMALLBLIND|<0,3><1,3>`, `raise 200`, `call`, `check`, `fold`, `allin`. The official Windows platform does not guarantee newline delimiters or TCP message boundaries; native bots must split sticky packets themselves. Local helpers live in `sever/`.

The active evolution epoch is `national_native_v1`. New evolution output must be native national TCP bots under `bots/national_v<N>/` with completion tags `national-bot-v<N>`. Old `claude_v*` directories and `bot-v*` tags are legacy history and must not determine current version numbers, active pool membership, ratings, H2H, experience injection, or precommit pass/fail.

The evolution pipeline lives under `web/core/`. It uses LLM agents, Glicko-2 ratings, national TCP native battles, quality gates, precommit regression evaluation, and accumulated strategy lessons to generate new bot versions.

---

## Technology Stack

| Area | Tech |
|---|---|
| Core language | Python 3 |
| Local battle engine | Python stdlib subprocess engine |
| Web backend | FastAPI, uvicorn, sse-starlette, pydantic |
| Frontend | React 19, Vite 6, Tailwind CSS 4, TypeScript |
| Evolution LLM SDK | `claude_agent_sdk` |
| Ratings | Glicko-2 for evolution, ELO for ladder |
| RL experiments | torch, numpy, gymnasium |
| National TCP server | asyncio TCP server + FastAPI/SSE dashboard |

Most production-style bots and the local battle engine should stay stdlib-only for portability. Exceptions already exist: `engine/aivat.py` optionally uses numpy, `bots/neural_national_lab/` uses experiment data, archived `bots/neural_bot/` used numpy/torch, and `rl/` depends on torch/numpy/gymnasium.

---

## Top-Level Structure

```text
.
├── engine/                     # Local Botzone-style battle engine
│   ├── judge.py                # Stateless Hold'em judge and Botzone-compatible state machine
│   ├── battle.py               # Battle/mirror battle runners, persistent bot subprocesses
│   ├── ladder.py               # Round-robin ELO tournament for botN directories
│   ├── anchor_runner.py        # One anchor bot vs discovered opponents
│   └── aivat.py                # Variance-reduction helpers, optional numpy acceleration
├── bots/                       # Active national_v<N> bots plus neural_national_lab experiments
├── web/                        # Evolution system + FastAPI backend + React dashboard
│   ├── main.py                 # Web launcher only
│   ├── core/                   # Evolution pipeline, daemon, MCP tools, ratings, prompts
│   ├── server/                 # FastAPI app and route modules
│   ├── frontend/               # React dashboard
│   ├── core/national_arena/    # Local national TCP presentation/diagnostic sessions
│   └── tests/                  # Backend and evolution regression tests
├── sever/                      # National competition TCP platform
│   ├── 国赛平台/                # Original competition docs and Windows reference platform
│   ├── engine/                 # TCP game engine, validator, THP recorder
│   ├── server/                 # Protocol codec and asyncio TCP server
│   ├── web/                    # Small FastAPI/SSE dashboard for TCP matches
│   ├── main.py                 # Starts TCP :10001 and Web :18080 by default
│   └── bot_adapter.py          # Bridges Botzone JSON bots to TCP protocol
├── rl/                         # DMC/RL training experiments
├── scripts/                    # Botzone upload/match/reset utilities
├── docs/                       # Architecture, audit, research, and fix documents
├── ref/                        # Botzone refs plus DanLM/neuron_poker reference code
├── archive/                    # Deprecated or preserved historical code/logs
├── results/                    # Fresh epoch local outputs; legacy payload archived
└── ladder_results/             # Fresh epoch ladder outputs; legacy payload archived
```

`ref/DanLM` and `ref/neuron_poker` are gitlinks in the index, but this checkout currently has no `.gitmodules`; `git submodule status` may fail until that is repaired.

## Dual-Checkout Evolution Runtime

There are two local checkouts under `/home/zzx/project/pok`, and the runtime
split is intentional:

- `/home/zzx/project/pok` is the operator/infrastructure checkout. Make normal code, prompt, test, and documentation changes here, or in a temporary ignored worktree under this directory.
- `/home/zzx/project/pok/.evolution_pok` is the actual long-running autonomous evolution checkout. The active `web/main.py`, rating daemon, candidate bot directories, and runtime result files belong there. When monitoring or restarting evolution, use this directory unless the user explicitly says otherwise.

The two checkouts must be synchronized through `origin/main`; never copy files between them by hand. Infrastructure changes made from the outer checkout must be pushed, then fetched/merged into `.evolution_pok` at a safe point. Bot versions produced by `.evolution_pok` must be pushed with their `national-bot-v{N}` tags, then fetched/merged back into the outer checkout before related infrastructure or bot work continues. The detailed policy is in `docs/evolution-dual-checkout-sync-policy.md`.

Before starting work, update remote state. In a clean checkout on the branch you will edit, run `git pull --ff-only --tags`; if the checkout is dirty, on a user branch, or cannot be fast-forwarded safely, run `git fetch --tags origin` and create a temporary worktree from the updated `origin/main` instead of working from a stale local HEAD.

Do not switch branches, reset, or directly develop infrastructure inside `.evolution_pok` while a generation is running. Do not use broad directory names such as `engine/`, `sever/`, `web/core/`, `web/tests/`, or every `bots/national_v*/` directory as automatic stop conditions. The stop/restart decision is owned by the active exact-file contract built in `web/core/evaluation_contract.py`: only changes to files in the current stage contract, or to the current candidate/source/parent/opponent bot versions recorded in the checkpoint, require restarting from a new baseline. Contract-neutral changes such as docs, frontend, observability, launchers, unrelated historical bot versions, or unrelated experiment files may be reconciled by the existing `evaluation_contract`/`publish_reconcile` guard.

If stale unfinished bot directories appear in the outer checkout, treat them as
operator checkout debris unless they have both a committed bot directory and the
matching annotated `national-bot-v{N}` tag. Do not promote or hand-complete such bots.
Investigate the checkpoint/logs, then remove the untracked candidate and clear
any stale active checkpoint so the outer checkout cannot resume an abandoned
generation by accident.

---

## Common Commands

### Local Bot Battles

```bash
# Standard battle. Each game is 70 hands by default.
python engine/battle.py archive/evolution_epochs/<epoch>/legacy_bots/bot5/main.py archive/evolution_epochs/<epoch>/legacy_bots/bot4/main.py -n 50 -v -d

# Legacy ladder. Active national-native evaluation is owned by web/core national TCP gates.
python engine/ladder.py -v
python engine/ladder.py -b 1 4 7 -n 20 -j 4
python engine/ladder.py --continue ladder_results/ladder_XXX/checkpoint.json -v

# Anchor runner.
python engine/anchor_runner.py 5 -n 100 -j 24
python engine/anchor_runner.py archive/evolution_epochs/<epoch>/legacy_bots/bot5 --dry-run
```

### Evolution Web App

```bash
# Full web app on :8000. Builds frontend unless --no-build is passed.
python web/main.py
python web/main.py --port 3000
python web/main.py --no-daemon      # Orchestrator without the rating daemon
python web/main.py --view-only      # Dashboard/API only; evolution stays stopped
python web/main.py --no-build
python web/main.py --dev

# Standalone orchestrator CLI.
python web/core/orchestrator.py
python web/core/orchestrator.py --one-gen
python web/core/orchestrator.py --dry-run
python web/core/orchestrator.py --no-daemon

# Standalone rating daemon.
python web/core/elo_daemon.py --pairs 5 --workers 12 -v
python web/core/elo_daemon.py --once
```

`--no-build` is valid only when `web/server/static/index.html` and
`web/server/static/assets/` already exist in that checkout. On a fresh
`.evolution_pok` clone, start without `--no-build` once so `web/main.py` can run
`npm ci` if needed and build the React dashboard.

### Web Tests And Frontend

```bash
cd web && python -m pytest tests/ -v
cd web && python -m pytest tests/test_routes_*.py -v
cd web && python -m pytest tests/test_logic_*.py -v

cd web/frontend && npm run dev
cd web/frontend && npm run build
```

`npm run build` writes `web/frontend/dist/` and copies the build to `web/server/static/`; both are ignored/generated outputs.

### National TCP Platform

```bash
cd sever && python main.py
cd sever && python main.py --tcp-port 20001 --web-port 28080

# Two test clients are needed to start a match.
cd sever && python test_client.py 127.0.0.1 10001 BotA
cd sever && python test_client.py 127.0.0.1 10001 BotB

# Bridge an existing Botzone-style bot to the TCP platform.
cd sever && python bot_adapter.py --bot ../archive/evolution_epochs/<epoch>/legacy_bots/claude_v224 --name legacy-test

# National protocol regression tests.
python -m pytest sever/tests -q

# Official Windows platform compliance oracle under Wine/Xvfb.
# Use this only to confirm official-protocol legality. Strength tracking,
# generation observation, and precommit regression use local native TCP gates.
python scripts/official_platform_acceptance.py \
  --candidate bots/national_v<N> \
  --opponent /home/zzx/project/pok/bots/national_v70 \
  --self-play-rounds 1 \
  --opponent-rounds 1 \
  --target-hands 70

# Local Web Arena. This is presentation/diagnostics only and never certifies a bot.
python scripts/national_arena.py serve --view-only
python scripts/national_arena.py run --mode managed \
  --top-bot national_v141 --bottom-bot national_v142 --hands 70 --wait

# Formal signed EXE certification required for every new bot before commit/tag.
python scripts/official_certify.py full bots/national_v<N> --wait-if-busy

# One-time recovery root only: never selected by the normal full path.  A
# successful 5+3x70 run consumes this repository-pinned signed-ledger root.
python scripts/official_certify.py bootstrap-full bots/national_v<N> \
  --root-id national-v141-official-full-v5-signed-ledger-root \
  --acknowledge-one-time-ledger-bootstrap --wait-if-busy
```

### Reinforcement Learning

```bash
python -m rl.scripts.train
python -m rl.scripts.train --model transformer
python -m rl.scripts.evaluate --checkpoint rl/checkpoints/best_model.pt
python engine/battle.py archive/evolution_epochs/<epoch>/legacy_bots/bot5/main.py rl/scripts/rl_bot.py -n 50 -v
```

### Botzone

```bash
python scripts/botzone_upload_match.py upload --source archive/evolution_epochs/<epoch>/legacy_bots/bot5/main.py --bot-name test --execute
python scripts/botzone_upload_match.py rank-match --bot-name test --execute
python scripts/botzone_upload_match.py run-room-series --bot-name test --execute
python scripts/reset_evolution.py --force --keep 6
```

Credentials are read from `BOTZONE_EMAIL` / `BOTZONE_PASSWORD` or explicit flags.

---

## Botzone/Local Bot Protocol

Each bot is usually a standalone Python script:

```python
# stdin payload
{
  "requests": [...],
  "responses": [...],
  "data": ...
}

# stdout response
{"response": <int action>, "data": ...}
```

Action encoding:

| Value | Meaning |
|---|---|
| `0` | Call or check, depending on context |
| `-1` | Fold |
| `-2` | All-in |
| `>0` | Raise to this stage total, not a delta amount |

Card encoding:

- Cards are integers `0..51`.
- Rank is `card // 4 + 2` where Ace is 14.
- Suit is `card % 4`, with `0=Heart`, `1=Diamond`, `2=Spade`, `3=Club`.

Game parameters in the current local engine:

- 2 players, No-Limit Texas Hold'em.
- `DEFAULT_N_HANDS = 70`, `INITIAL_CHIPS = 20000`.
- Small blind 50, big blind 100.
- `engine/battle.py` gives bot decisions 60 seconds. `web/core/decision_tester.py` uses a shorter per-scenario timeout.
- `battle.py` and mirror battle use persistent bot subprocesses per game by default; fresh subprocess calls are mainly the non-persistent/debug path.
- Botzone game ID remains `63dcfaddee1bce5e6c8f4b53`.

Bot implementation guidance:

- Keep Botzone-uploadable bots stdlib-only.
- Use `.get(key, default)` for protocol fields.
- Sanitize actions before returning.
- Never write debug text to stdout; use stderr if needed.
- Bound Monte Carlo counts and CPU usage.
- Treat active evolved versions as non-continuous. The highest `national_v*` directory is not necessarily completed, tagged, or committed.

---

## National TCP Platform (`sever/`)

`sever/` implements the national competition self-play platform described by:

- `sever/国赛平台/通信协议.docx`
- `sever/国赛平台/自对弈平台使用及通信协议补充说明.docx`
- `sever/国赛平台/非法行为说明.docx`
- `sever/国赛平台/德州扑克规则.doc`
- `sever/国赛平台/中国计算机博弈锦标赛棋（牌）谱标准说明书.pdf`

Core protocol facts from those documents:

- Transport: TCP socket. Platform is server, AI engine is client.
- Default TCP port is `10001`; `sever/main.py` also starts a Web dashboard on `18080`.
- A match is 70 hands. Each hand resets both players to 20000 chips.
- Blinds are 50/100. Small blind acts first preflop; big blind acts first on flop/turn/river.
- Each decision has a 60 second limit. Timeout is treated as fold.
- Client actions are raw socket strings with no trailing newline required or expected by the official EXE: `raise <amount>`, `fold`, `call`, `check`, `allin`.
- The official EXE is timing-sensitive. Formal native bots must throttle action sends in the TCP wire layer after platform messages; the current template uses `POK_OFFICIAL_ACTION_DELAY` with a default of `0.30` seconds. Local strength evaluation may set `POK_NATIVE_LOCAL_ACTION_DELAY=0`, but generated/submitted bot entries must keep the default official-safe behavior.
- Do not copy the official sample's unsolicited timeout-rescue loop into generated bots. A bot may only send an action while the platform is waiting for its current decision; repeated `call`/`check` sends without a pending action are treated as protocol risk.
- The official EXE uses TCP streams, so inbound data may arrive as sticky packets such as `earnChips -100preflop|...` or `raise 200call`; native bots must split protocol tokens before updating state.
- `raise <amount>` must use exactly one space between keyword and amount; leading/trailing spaces, tabs, and extra spaces are illegal protocol formats.
- `bet` must not be sent; the protocol uses `raise` in place of bet.
- `raise X` means raise to total stage bet `X`, not add `X`.
- Consecutive raises must be at least 2x the previous raise-to value. A controlled
  official-EXE oracle run on 2026-07-11 confirmed that `raise 200` followed by
  exact `raise 400` is accepted and relayed. Generated bots may still use
  `2x + 1` as conservative sizing headroom, but that is policy, not legality.
- Postflop first action `call` is illegal. Postflop after any first action, `check` is illegal; when one player checks first, the second player passes the street by sending `call`.
- After `allin` is called, clients should only receive runout cards and settlement messages for that hand; they must not act again before `earnChips`.
- Server card format is `<suit,rank>` with `suit 0=Spade, 1=Heart, 2=Diamond, 3=Club` and `rank 0=2 .. 12=Ace`.
- Important server-to-client messages include `name`, `preflop|SMALLBLIND|...`, `preflop|BIGBLIND|...`, `flop|...`, `turn|...`, `river|...`, `earnChips <amount>`, and `oppo_hands|...`.
- Official captures show that `earnChips` is the current seat's signed per-hand
  net change and paired seat values are zero-sum. `oppo_hands` appears only at
  showdown. The EXE can suppress a street-closing peer `call`/`check` and jump
  directly to the next street or settlement; generated native runtimes must
  infer only actions proven by that boundary. The EXE sends no cumulative match
  result TCP token. At the natural end of hand 70, the 2021 EXE omits the final
  `earnChips` pair but writes `STATE:69` and the cumulative result in its THP.
  Formal v5 completion therefore cross-binds wire hands/earnings 1..69 to THP
  states 0..68 and uses strict state 69 plus the footer as the independent final
  proof; a bare 69-settlement count still fails. None of these chip/win outputs
  may enter strength ratings.

All illegal actions are treated as fold. The validator implements the national document's bet/call/check/raise/allin restrictions in `sever/engine/validator.py`.

When two clients are connected, `sever/server/tcp_server.py` starts the match automatically. The Web `/api/start` endpoint is retained as a dashboard control/fallback and rejects duplicate starts while a match task is running.

Card conversion matters:

- Local `engine/judge.py` integer suits are `Heart=0, Diamond=1, Spade=2, Club=3`.
- TCP protocol suits are `Spade=0, Heart=1, Diamond=2, Club=3`.
- `sever/bot_adapter.py` must map suits, not reuse the integer directly.

THP records:

- `sever/engine/thp_recorder.py` writes national standard THP text records.
- Filename style is `THP-{teamA} vs {teamB}-{winner}胜-{yyyymmddHHMM}-CCGC.txt`, sanitized for filesystem-unsafe characters.
- Each hand line is `STATE:N:actions:cards:earnings:players;`.
- Actions use `r{amount}` for bet/raise, `c` for check/call, `f` for fold, with `/` separating streets.
- Cards use rank/suit strings such as `Ah`, `Ts`; hand cards are recorded as big blind first, then small blind.
- Earnings and players are also recorded in the hand's big-blind-first order, matching the hand-card order.
- Export encoding is GB2312.

The main Web application's `/arena` page is a local presentation and diagnostic
surface over the shared national TCP transport and game runtime. Its result
authority is permanently `diagnostic_only`: Arena events, local THP files,
wire logs, and match completion never update Glicko, certify a bot, or satisfy
an evolution gate. Every new bot must separately pass the signed official
Windows EXE full policy: five 70-hand self-play rounds plus three 70-hand rounds
against an eligible opponent. Historical content-bound grandfather grants are
temporary migration eligibility and must never be displayed as an EXE pass.
Normal formal selection accepts only a published full-v5 certificate. The
signed-ledger bootstrap root is an explicit operator-only recovery command,
never an automatic fallback; its immutable receipt is written into the signed
ledger only after a successful full run and then cannot be reused.
Arena and the official EXE share an exclusive lease for TCP port 10001; pending
formal certification has priority, so use a different Arena port for concurrent
local presentation work.

National-native strength has a separate, explicit ordering contract. One
strength sample is one complete 70-hand local native TCP match. The primary
outcome is the sign of final net chips: positive is a win, negative is a loss,
and zero is a draw. Glicko/H2H/`selection_score` are derived from those match
outcomes and remain the primary ranking evidence. Final net-chip magnitude is
secondary and may only break an equal primary score. Official EXE and Web Arena
chip results have zero strength weight.

---

## Evolution System (`web/core/`)

`web/core/evolution_core.py` is now a re-export facade for backward compatibility. The active implementation is split across focused modules:

- `evolution_infra.py` - constants, git helpers, file locks, checkpoints, ratings.
- `generation_scheduler.py` - three-phase generation cycle.
- `evaluation_bundle.py`, `evidence_snapshot.py`, `rating_snapshot.py` - immutable daemon-cycle publication, strict generation cutoffs, and unified strength rows.
- `bot_action_stats.py`, `replay_spotlight.py` - committed native tracker aggregation and hand-level diagnostic evidence.
- `master_context_contract.py`, `plan_compiler.py`, `strategy_reference_pack.py` - digest-bound planning evidence, deterministic worker-contract compilation, and typed local strategy cards.
- `workflow_kernel.py`, `worker_workflow.py` - Worker-only SQLite-WAL journal, fenced effect/outbox leases, pure replay reducer, immutable candidate artifacts, and crash-safe projection. The rest of the pipeline still uses the versioned JSON checkpoint; do not describe the whole pipeline as event-sourced.
- `poker_assets.py` - system-owned content-addressed/read-only mmap ABI for the real 1,326 hole-combination and 169-class metadata. It is an asset-contract prototype, not an evaluator, equity table, policy, or current bot decision consumer.
- `orchestrator.py` - Claude agent loop with MCP tools.
- `tools.py`, `tool_planning.py`, `tool_gates.py`, `tool_eval.py`, `tool_commit.py`, `tool_status.py`, `tool_bot_management.py` - MCP tools.
- `agent_master.py`, `agent_workers.py`, `agent_review.py`, `audit_agents.py` - LLM roles and advisory audits.
- `llm_query.py` - direct `claude_agent_sdk` calls, streaming, retry, prompt budget.
- `elo_daemon.py`, `battle_scheduler.py`, `daemon_management.py` - continuous and scheduled evaluation.
- `glicko2.py` - rating implementation.

Current generation stages are:

1. prepare: `prepare_next_gen` or `run_crossover`
2. direction audit: `run_direction_audit`
3. optional literature probe when stagnant: `run_literature_probe`
4. master planning: `run_master`
5. workers: `execute_workers`
6. quality gates: `run_quality_gates`
7. review: `run_review`
8. critic: `run_critic`
9. verification: `run_precommit_eval`
10. official EXE full certification: `commit_bot` starts/polls the durable 5+3x70 job
11. commit: `commit_bot` verifies and publishes the signed certificate with the bot/tag
12. archivist: `run_archivist`

`run_crossover` is only a recombination-baseline preparation step. On success
it checkpoints `prepared`; it never substitutes for direction audit, literature
research, Master planning, or Worker implementation. The exact child content,
component provenance, LOC, frozen H2H identity, and capability snapshot are
digest-bound in the checkpoint; later planning and final regression gates use
that prepared child as the effective baseline. A deterministic provenance gate
rejects threshold-only/novel crossover mutations not traceable to Parent B and
covers the complete artifact manifest: non-Python tables/models/configs must be
exact Parent-B bytes. `run_crossover` also requires the scheduler-selected
`(Parent A, Parent B, target)` tuple. Infrastructure resume is bound to complete
Parent/Child artifact hashes; a paused child that drifts is abandoned rather
than inheriting stale provenance/runtime evidence.

Every preparation path, including single-parent copy, freezes a common complete
artifact manifest/hash before Master. Workers may create declared binary assets,
but they edit only lease-epoch isolated workspaces. Their full batch is
byte-snapshotted and atomic; any partial failure restores the pre-batch artifact,
and successful output is content-addressed before actor-serialized materialization
to the canonical candidate. Infrastructure retry reuses the frozen tasks,
feedback, source/template/backend hashes, dynamic context, exhausted-direction
evidence, and preparation snapshot instead of reconstructing them from live
files. Gate-derived rework feedback/tasks are checkpoint-owned,
and caller echoes cannot add files or blockers. `must_change_files` never grants
write authority. Quality binds repair baselines, removes transient caches/context,
forbids candidate-owned external/import-time I/O, and evaluates prepared→final
regular-file changes. Publication cross-checks working bytes, staged Git blobs,
and the immutable tag tree; ignored/empty/nested-Git artifacts are rejected.

The checked-in strategy reference pack is intentionally a small typed registry,
not a prose dump. Master first samples three independent proposals, validates
their source symbols and syntactic call leaves, obtains two anonymous criterion
ballots, and selects exactly one proposal ID. The selected structural change,
expected diff, falsifier, call chain, and contract digest are system-compiled
into the matching Worker prompts and frozen envelope. Compact import-time facts
(1,326 hole combinations, 8,192 rank masks, 21 five-of-seven selections) are
allowed. The shared mmap prototype currently contains metadata only. Larger
evaluator/equity/blueprint assets remain deferred until a system-owned packager,
hash/key/consumer contract, official submission compatibility, and measured
decision influence exist; candidate file I/O is currently forbidden.

Important current thresholds:

- `MAX_ACTIVE_BOTS = 30`.
- `MIN_GAMES_FOR_EVAL = 100`.
- Daemon evaluation can early-exit when enough games are available and RD is low enough.
- Core strategy files (`strategy.py`, `postflop.py`) have a 2000 line base limit.
- Helper `.py` files have a 1500 line base limit.
- Hard cap is 2500 lines, with a 15 percent growth budget from the source bot.
- Decision tests require pass rate at least 70 percent and no critical scenario failures.
- `run_quality_gates` runs the national protocol regression shard that matches the active execution mode. `national_native` runs `sever/tests/test_national_platform_alignment.py` without importing the legacy adapter; adapter workflows still run `sever/tests/test_national_alignment.py`.
- Worker concurrency is capped by `MAX_PARALLEL_WORKERS = 3`, with adaptive throttling under API pressure.
- `pipeline_state.json` carries immutable `workflow_run_id` and monotonic `checkpoint_revision` CAS. Active legacy checkpoints without those fields must be centrally abandoned while stopped; never upgrade them implicitly.
- `docs/official-raise-boundary-oracle-2026-07-11.md` and `docs/official-terminal-settlement-oracle-2026-07-11.md` are always-critical exact-file evaluation inputs. Their SHA-256 values are pinned by `runtime_architecture_policy.py`; control-plane changes verify hashes but do not rerun the official EXE.
- Critic is advisory strategy evidence. A successful schema-valid Critic run is required, but its score does not schedule repair or decide acceptance; native TCP precommit is the local strategy gate.
- `run_precommit_eval` is the final local strength/regression gate. It cannot replace official EXE compliance.
- `commit_bot` cannot commit/tag a new national bot until a content-bound, signed full official EXE certificate validates.
- Source selection is owned by `generation_scheduler._decide_strategy`. LLM `recommended_source` and `branch_from` suggestions are accepted only when they point to an active bot backed by normal completion discovery (`.completed` plus `national-bot-v{N}` tag); rejected suggestions are logged as `pipeline.source_selection_rejected`.
- Daemon strength evidence is published as one immutable, content-addressed cycle transaction: H2H, bot stats, Glicko ratings, daemon stats, append-log cutoffs, and derived selection rows are committed under the evaluation-cycle lock, then exposed by `evaluation_cycle_manifest.json`. Generation planning copies that exact bundle into `web/core/results/v<N>/evidence_snapshot/`; Combined analysis, deterministic source/leader/oscillation logic, crossover parent selection, and Master must use the frozen cutoff rather than reopen live strength files. Native action diagnostics may be at most five same-identity cycles stale and must publish their source save/digest; their per-replay tracker contributions are cached in `.stats_etag.json` so unchanged replays are not reparsed.

The daemon writes live data under `web/core/results/`, including Glicko ratings, H2H matrix, match history, replays, scheduler files, costs, and system events. These files are runtime data and are gitignored.

Legacy Botzone-era runtime data was archived out of the active results directories when `national_native_v1` was created. See `docs/national-native-epoch-reset.md`.

---

## FastAPI Backend And Frontend

Backend entry point:

- `web/main.py` calls `uvicorn.run("server.app:app", ...)`.
- `web/server/app.py` creates the FastAPI app, starts the orchestrator in lifespan, includes route modules, and serves the built React SPA.
- Route modules currently include `ratings`, `matches`, `evolution`, `logs`, `control`, `bots`, `pipeline`, `prompts`, `data_stream`, `scheduler`, `certification`, and `national_arena`.
- SSE endpoints include `/api/data/stream` and `/api/evolution/stream`.
- Shared cached reads use `web/server/cache.py` with a short TTL and `fcntl` locks.

Frontend facts:

- Source is under `web/frontend/src/`.
- Routes: `/`, `/evolution`, `/matches`, `/rating-trends`, `/match-matrix`, `/logs`, `/control`, `/bots`, `/experience`, `/prompts`, `/arena`.
- `DataProvider` uses `/api/data/stream`; `EvolutionMonitor` uses `/api/evolution/stream`.
- The frontend is based on a TailAdmin template; package name is still `tailadmin-react`.
- It imports routing APIs from `react-router` v7, not `react-router-dom`.

Known production quirk:

- FastAPI mounts `/assets` and falls back all other paths to the SPA. A direct `/favicon.png` request from the built HTML may return the SPA HTML unless a favicon route/static mount is added.

---

## Ratings And Evaluation

Local ladder ELO:

- Initial ELO 1200.
- K=40 for first 30 games, then K=20.
- Ladder/anchor discovery focuses on `botN` style directories unless explicit paths/labels are given.

Evolution Glicko-2:

- Defaults: `r=1500`, `rd=350`, `sigma=0.06`; the Glicko-2 volatility constant is `TAU=0.3` in `glicko2.py` (NOT 0.5).
- Conservative rating is `r - 2 * rd`.
- Each national strength sample is exactly one completed 70-hand native TCP match. Match-result sign is primary; net-chip magnitude is retained only as a secondary tie-breaker.
- The daemon maintains H2H and bot statistics in addition to ratings.
- Reaping sorts by conservative Glicko rating as the primary cull key. Reap events include `selection_key=conservative_glicko`, `conservative_rating`, `leaderboard_score`, and `h2h_avg_wr` so logs show both the actual decision key and contextual matchup metrics.

Quality and regression gates may include:

- `py_compile`
- smoke mirror battle
- decision scenario tests
- critical scenario check
- code-size limits
- actual-code-change check
- fix verification and telemetry/placement-shadow checks
- precommit mirror battle regression evaluation
- advisory LLM audits such as precommit semantic checks and regression guardian

---

## Repository Hygiene

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

National lifecycle state is also Git-backed. `national-reaped-v{N}` annotated
tags are permanent active-pool tombstones,
`national-reaped-registry-v1` marks the completed legacy-ledger migration, and
`national-high-water-v{N}` prevents version/sunset rollback. Do not delete or
retarget these tags. Run `scripts/migrate_national_epoch_registry.py` without
flags for a dry-run; `--apply --push` is allowed only at a stopped, reviewed
runtime migration point. After the marker exists, a reaped bot must be retired
through `record_reaped_bot`/`reap_weakest`, never by deleting `.completed`
directly.

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
