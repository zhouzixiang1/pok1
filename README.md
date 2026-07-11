# Poker Bot Evolution Framework

A heads-up No-Limit Texas Hold'em bot AI framework that builds, evaluates, and evolves bots for the national (Chinese Computer Game Championship) TCP platform. It has four code paths that should not be collapsed into one mental model:

- **`engine/`** — local Botzone-style subprocess battle engine for Python bots (JSON on stdin/stdout).
- **`web/`** — unified LLM-driven evolution system, FastAPI backend, and React dashboard.
- **`sever/`** — national competition TCP self-play platform (the official competition transport).
- **`rl/`** — reinforcement-learning experiments that wrap the local Hold'em engine.

Two protocol families coexist: the **Botzone/local** family (Python subprocesses exchanging JSON, owned by `engine/`) and the **national TCP** family (AI engines connect to a TCP server and exchange raw short socket messages such as `preflop|SMALLBLIND|<0,3><1,3>`, `raise 200`, `call`, `fold`, `allin`; owned by `sever/`). The active evolution epoch is `national_native_v1`: new evolved bots live under `bots/national_v<N>/` and are complete only when tagged `national-bot-v<N>`. Old `claude_v*` directories and `bot-v*` tags are legacy history.

## Repo Layout

```text
.
├── engine/          # Local JSON battle engine: judge.py, battle.py, ladder.py, aivat.py
├── bots/            # Active bots/national_v<N>/ + neural_national_lab/ experiments
├── web/             # Evolution system: web/core (pipeline+daemon), web/server (API), web/frontend (React)
├── sever/           # National TCP platform: 国赛平台/ docs, engine/, server/, web/ dashboard
├── rl/              # DMC/RL training (DanLM-inspired, wraps engine/judge.py)
├── scripts/         # Botzone upload, official EXE certification, reset utilities
├── docs/            # Architecture, oracle, audit, and design documents (+ docs/archive/)
├── ref/             # Botzone refs + DanLM / neuron_poker reference code
├── archive/         # Deprecated or preserved historical code/logs
├── results/         # Fresh-epoch local competition outputs (gitignored)
└── ladder_results/  # Fresh-epoch ladder outputs (gitignored)
```

Module entry points: `engine/judge.py` (stateless judge), `web/main.py` (web app launcher), `sever/main.py` (TCP `:10001` + Web `:18080`), `rl/scripts/train.py` (RL training).

## Quick Start

```bash
# Evolution web app (orchestrator + rating daemon + React dashboard) on :8000
python web/main.py
python web/main.py --view-only        # Dashboard/API only; evolution stays stopped

# Native national TCP platform: start server, then connect two clients to begin a match
cd sever && python main.py
cd sever && python test_client.py 127.0.0.1 10001 BotA   # in one shell
cd sever && python test_client.py 127.0.0.1 10001 BotB   # in another shell

# Local JSON battle between two bots (70 hands per game)
python engine/battle.py <bot_a>/main.py <bot_b>/main.py -n 50 -v

# Tests
cd web && python -m pytest tests/ -v     # evolution / backend regression
python -m pytest sever/tests -q          # national TCP protocol alignment

# RL training (MLP Q-network; --model transformer for the Transformer variant)
python -m rl.scripts.train
```

Formal bot certification is required before every `commit_bot`/tag. It runs the signed official Windows EXE under Wine/Xvfb:

```bash
python scripts/official_certify.py full bots/national_v<N> --wait-if-busy
```

## Strength & Evaluation Model

One strength sample is exactly **one complete 70-hand local native TCP match** (20,000 chips reset per hand, blinds 50/100, 60s decision limit). The primary outcome is the **sign of final net chips**: positive = win, negative = loss, zero = draw. Glicko-2 ratings, head-to-head results, and `selection_score` are all derived from those match outcomes and are the primary ranking evidence. Final **net-chip magnitude is secondary** and may only break an equal primary score.

The **official Windows EXE and the local Web Arena have zero strength weight.** The EXE is compliance-only: it certifies protocol legality (five 70-hand self-play rounds plus three 70-hand rounds against an eligible opponent; a signed certificate is required for every commit). The Web Arena (`/arena`) is presentation/diagnostics only and never updates Glicko, certifies a bot, or satisfies an evolution gate.

The active pool is capped at 30 bots. Reaping sorts by **conservative Glicko rating** (`r - 2*rd`) as the primary cull key; H2H average win rate is reported as context, not the reap key.

## Dual-Checkout Runtime

There are two local checkouts under `/home/zzx/project/pok`, and the split is intentional:

- `/home/zzx/project/pok` — the **operator/infrastructure checkout** (this one). Make code, prompt, test, and documentation changes here.
- `/home/zzx/project/pok/.evolution_pok` — the **long-running autonomous evolution checkout**. The active `web/main.py`, rating daemon, live candidate bot directories, and runtime result files belong there.

The two checkouts synchronize **only through `origin/main`**; never copy files between them. Infrastructure changes are pushed from here, then fetched/merged into `.evolution_pok` at a safe point. Completed bots are pushed from `.evolution_pok` with their `national-bot-v{N}` tags, then fetched/merged back. Do not develop infrastructure inside `.evolution_pok` while a generation is running. Full policy: `docs/evolution-dual-checkout-sync-policy.md`.

## Where To Learn More

- **`AGENTS.md`** — the working map for AI coding agents (start here; most complete).
- **`CLAUDE.md`** — detailed project instructions, module maps, constants, and conventions.
- **`ONBOARDING.md`** — teammate usage guide and workflow breakdown.
- **`SETUP_GUIDE.md`** — remote deployment tutorial.
- **`docs/`** — active references, including:
  - `docs/evolution-dual-checkout-sync-policy.md` — dual-checkout sync policy.
  - `docs/national-platform-alignment-report.md` — authoritative national TCP platform alignment.
  - `docs/official-exe-platform-analysis.md`, `docs/official-wire-probe.md`, `docs/official-platform-harness.md` — official Windows EXE behavior and wire analysis.
  - `docs/rating_strength_alignment_report.md` — strength / evaluation model.
  - `docs/llm-stages.md`, `docs/multi_ai_bot_design.md` — evolution runtime data flow and design.
  - `docs/holdem_rl_design.md`, `docs/rl_improvement_research.md` — RL design and research.
  - `docs/archive/` — superseded audits, implemented fix plans, and pre-GRU version-history reports (kept for historical context).
