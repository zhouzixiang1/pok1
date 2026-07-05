# Evolution Dual-Checkout Sync Policy

This repository intentionally uses two local checkouts under `/home/zzx/project/pok`:

- `/home/zzx/project/pok` is the operator and infrastructure checkout. Human and agent changes to `engine/`, `web/`, `sever/`, `rl/`, prompts, tests, docs, and project scripts should be developed from this side, or from a temporary worktree created under this directory.
- `/home/zzx/project/pok/.evolution_pok` is the autonomous evolution runtime clone. The running web/orchestrator/daemon process, candidate bot directories, live ratings, and generation checkpoints live here.

The two checkouts must stay synchronized through `origin/main`. Do not copy files between them by hand.

## Required Invariant

Both checkouts must point at the same remote repository:

```bash
git remote get-url origin
```

Committed infrastructure state is synchronized only by git:

1. A change made in `/home/zzx/project/pok` is committed, pushed to `origin/main`, then fetched/merged into `/home/zzx/project/pok/.evolution_pok` at a safe point.
2. A bot version produced in `/home/zzx/project/pok/.evolution_pok` is complete only after `commit_bot` commits it, creates `bot-v{N}`, and pushes both `main` and the tag. The outer checkout must then fetch tags and merge or rebase `origin/main` before editing related bot/evaluation code.
3. If either checkout is ahead of or behind `origin/main`, do not start new infrastructure work until the intended sync direction is explicit.

## Directory Ownership

Infrastructure edits belong in the outer project checkout:

- `engine/`
- `sever/`
- `web/`
- `rl/`
- `scripts/`
- `docs/`
- root project guidance such as `AGENTS.md`, `CLAUDE.md`, and `.gitignore`

Autonomous evolution runtime state belongs only in `.evolution_pok`:

- active `python web/main.py` and `elo_daemon.py` processes
- candidate `bots/claude_v{N}/` directories before `commit_bot`
- `web/core/results/`
- `web/logs/`
- generated match/replay/runtime outputs

Do not run the long-lived evolution process from the outer checkout. Do not use `.evolution_pok` as a normal development worktree while a generation is running.

## How The Current Guard Works

The current implementation uses an evaluation-contract guard rather than a blanket "any remote change blocks evolution" rule:

- `web/core/evaluation_contract.py` defines evaluation-sensitive paths: `engine/`, `sever/`, `web/core/`, `web/tests/`, `web/main.py`, plus the active candidate/source/opponent bot versions recorded in the checkpoint.
- `web/core/evolution_infra.py` writes that contract into `web/core/results/pipeline_state.json` as `repo_baseline.evaluation_contract`.
- `web/core/tool_runtime_guard.py`, `web/core/orchestrator.py`, and `web/core/pipeline_recovery.py` allow unrelated HEAD drift only when the changed paths do not touch the active evaluation contract.
- `web/core/publish_reconcile.py` retries a rejected push by fetching `origin/main`; it auto-merges remote changes only when they are evaluation-contract neutral. If remote changes touch the contract, it blocks with `remote_contract_changed`.

This means documentation-only or unrelated experiment changes can usually be reconciled automatically, but changes to the national server, local engine, evolution core, tests, or active bots require an explicit restart/resume decision.

## Sync Procedures

For infrastructure or documentation work:

```bash
cd /home/zzx/project/pok
git fetch --tags origin
git status --short --branch
git diff --name-only HEAD..origin/main
```

If the outer checkout is dirty, use a temporary worktree inside `/home/zzx/project/pok/.claude/worktrees/` or another ignored path under `/home/zzx/project/pok`; do not switch the user's dirty branch. Commit and push the task branch, merge it to `main`, then remove the temporary worktree.

For evolution output:

```bash
cd /home/zzx/project/pok/.evolution_pok
git status --short --branch
git fetch --tags origin
```

Do not switch branches or reset this checkout while the evolution service is running. If the incoming change touches the evaluation contract, stop the evolution service, merge/pull, restart from the new baseline, and observe the next generation. If the incoming change is contract-neutral, it may be merged at the next safe point or reconciled automatically when evolution publishes its next commit.

After `.evolution_pok` publishes a bot:

```bash
cd /home/zzx/project/pok
git fetch --tags origin
git diff --name-only HEAD..origin/main
```

Merge or rebase `origin/main` into the outer branch before editing bots or evaluation infrastructure. Leaving the outer checkout behind is acceptable only while unrelated user work is in progress; it must not be treated as the canonical infrastructure state.

## What Not To Do

- Do not `cp`, `rsync`, or hand-apply code from one checkout to the other as a sync mechanism.
- Do not stage `.evolution_pok/` from the outer checkout.
- Do not run evolution from both checkouts at the same time.
- Do not make infrastructure edits directly in `.evolution_pok` while a generation is active unless the edit is an emergency repair and the generation is restarted afterward.
- Do not assume a highest-numbered `bots/claude_v*` directory is complete. The annotated `bot-v{N}` tag is the completion proof.
