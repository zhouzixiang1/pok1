# Evolution Dual-Checkout Sync Policy

This repository intentionally uses two local checkouts under `/home/zzx/project/pok`:

- `/home/zzx/project/pok` is the operator and infrastructure checkout. Human and agent changes to `web/`, `sever/`, prompts, tests, docs, and project scripts should be developed from this side, or from a temporary worktree created under this directory.
- `/home/zzx/project/pok/.evolution_pok` is the autonomous evolution runtime clone. The running web/orchestrator/daemon process, candidate bot directories, live ratings, and generation checkpoints live here.

The two checkouts must stay synchronized through `origin/main`. Do not copy files between them by hand.

## Required Invariant

Both checkouts must point at the same remote repository:

```bash
git remote get-url origin
```

Committed infrastructure state is synchronized only by git:

1. A change made in `/home/zzx/project/pok` is committed, pushed to `origin/main`, then fetched/merged into `/home/zzx/project/pok/.evolution_pok` at a safe point.
2. A bot version produced in `/home/zzx/project/pok/.evolution_pok` is complete only after `commit_bot` commits it, creates `national-bot-v{N}`, and pushes both `main` and the tag. The outer checkout must then fetch tags and merge or rebase `origin/main` before editing related bot/evaluation code.
3. If either checkout is ahead of or behind `origin/main`, do not start new infrastructure work until the intended sync direction is explicit.
4. Before starting any task, update remote state first. In a clean checkout on the branch you will edit, run `git pull --ff-only --tags`. If the checkout is dirty, on a user branch, or cannot be fast-forwarded safely, run `git fetch --tags origin` and create a temporary worktree from the updated `origin/main`; do not begin from a stale local HEAD.

## Directory Ownership

Infrastructure edits belong in the outer project checkout:

- `sever/`
- `web/`
- `scripts/`
- `docs/`
- root project guidance such as `AGENTS.md`, `CLAUDE.md`, and `.gitignore`

Retired engines, experiments, bots, and evidence under `archive/` are historical
records. They are never copied into the runtime checkout as active inputs.

Autonomous evolution runtime state belongs only in `.evolution_pok`:

- active `python web/main.py` and `elo_daemon.py` processes
- candidate `bots/national_v{N}/` directories before `commit_bot`
- `web/core/results/`
- `web/logs/`
- generated match/replay/runtime outputs

Do not run the long-lived evolution process from the outer checkout. Do not use `.evolution_pok` as a normal development worktree while a generation is running.

The one-time `national_tcp_policy_v1` reset is runtime authority, not ordinary
repository cleanup. After infrastructure is merged/pushed and the autonomous
checkout is stopped, clean, on `main`, and exactly synchronized to
`origin/main`, execute it only there:

```bash
cd /home/zzx/project/pok/.evolution_pok
python3 scripts/reset_national_tcp_policy_epoch.py \
  --execute --acknowledge-runtime-checkout
```

The script rejects execution from the outer checkout and refuses any existing
completed or interrupted reset claim. Old ignored output in the outer checkout
has no runtime authority and must never be copied to `.evolution_pok`; use a
non-executing dry run there for inspection, then clean/archive it separately
only after the authoritative runtime reset is verified.

## How The Current Guard Works

The current implementation uses an evaluation-contract guard rather than a blanket "any remote change blocks evolution" rule:

- `web/core/evolution_scope.py` defines file-scoped evaluation-sensitive paths. `CRITICAL_PREFIXES` must stay empty unless a future change has a specific path-pattern reason; do not lock all of `sever/`, `web/core/`, or `web/tests/`.
- The hard contract is the union of named exact-file groups for raw national-TCP parsing and legality, the system-owned bot runtime, typed policy ABI, gates/precommit, generation/recovery/publication, and active prompt templates. There is no active local-engine or adapter contract. Runtime observability files such as `web/core/event_bus.py`, `web/core/system_log.py`, `web/core/web_ui.py`, launcher files such as `web/main.py` and `sever/main.py`, docs, and frontend assets are contract-neutral unless they are promoted into a named exact-file group with tests.
- `web/core/evaluation_contract.py` builds the active contract from those exact files plus only the active candidate/source/parent/opponent bot versions recorded in the checkpoint. The contract is stage-sensitive at the level of pipeline logic, not directories: selected/preparing/crossover stages track prepare+crossover files, `prepared` tracks direction-audit files, `direction_audited` tracks master-planning files, `master_planned` and repair stages track worker/repair files, and post-worker stages track only hard evaluation/runtime files. Guard files such as `evaluation_contract.py`, `evolution_scope.py`, `tool_runtime_guard.py`, `orchestrator.py`, and `pipeline_recovery.py` stay critical at every active stage. Dirty worktree checks use the same checkpoint bot-version set: an unrelated historical `bots/national_v*/` directory is not a stop condition unless that version is part of the current contract.
- `web/core/evolution_infra.py` writes that contract into `web/core/results/pipeline_state.json` as `repo_baseline.evaluation_contract`.
- `web/core/tool_runtime_guard.py`, `web/core/orchestrator.py`, and `web/core/pipeline_recovery.py` allow unrelated HEAD drift only when the changed paths do not touch the active evaluation contract.
- `web/core/publish_reconcile.py` retries a rejected push by fetching `origin/main`; it auto-merges remote changes only when they are evaluation-contract neutral. If remote changes touch the contract, it blocks with `remote_contract_changed`.

This means documentation-only, observability-only, launcher-only, frontend, or unrelated experiment changes can usually be reconciled automatically. Changes to named rule/evaluation/generation contract files require an explicit restart/resume decision only when they are in the active stage contract. Changes to active candidate/source/parent/opponent bot versions remain contract-critical.

## Sync Procedures

For infrastructure or documentation work:

```bash
cd /home/zzx/project/pok
git pull --ff-only --tags
git status --short --branch
git diff --name-only HEAD..origin/main
```

If the outer checkout is dirty, on a user branch, or cannot be fast-forwarded safely, do not force a pull over it. Run `git fetch --tags origin`, then use a temporary worktree inside `/home/zzx/project/pok/.claude/worktrees/` or another ignored path under `/home/zzx/project/pok`; do not switch the user's dirty branch. Commit and push the task branch, merge it to `main`, then remove the temporary worktree.

For evolution output:

```bash
cd /home/zzx/project/pok/.evolution_pok
git status --short --branch
git fetch --tags origin
```

Do not switch branches or reset this checkout while the evolution service is running. If the service is stopped and the checkout is clean, update it with `git pull --ff-only --tags` before restarting. If the incoming change touches the active evaluation contract reported by `web/core/evaluation_contract.py`, stop the evolution service, merge/pull, restart from the new baseline, and observe the next generation. If the incoming change is contract-neutral for the active stage, it may be merged at the next safe point or reconciled automatically when evolution publishes its next commit.

### Parked first-strict contract-change recovery

`official_bootstrap_required` remains non-disposable. One narrower
operator-only recovery exists for an unpublished first strict Bot whose
explicit bootstrap job is terminal, inconclusive, and ran zero of eight
rounds, when a reviewed descendant HEAD changes an always-critical official
contract. Never call the MCP `abandon_generation`, rewrite the checkpoint,
retry the old authorization under new bytes, or delete the durable job.

After stopping every runtime/official process, fast-forward the autonomous
checkout to the exact reviewed `origin/main`. Run this command once without
`--execute`, using the exact old checkpoint/head/hash and terminal job:

```bash
python scripts/abandon_parked_bootstrap_contract_change.py \
  --expected-baseline-head <40-hex-old-head> \
  --expected-baseline-contract-hash <64-hex-old-contract-hash> \
  --expected-current-head <40-hex-reviewed-origin-main> \
  --expected-workflow-run-id <exact-workflow-run-id> \
  --expected-checkpoint-revision <exact-revision> \
  --expected-candidate-hash <64-hex-artifact-hash> \
  --expected-terminal-job-id <64-hex-job-id>
```

Review the complete claim and repeat the same command with
`--execute --acknowledge-runtime-checkout --claim-digest <dry-run-digest>`.
It writes an immutable no-follow `O_EXCL`/fsync external claim, then calls the
existing workflow/strict-authority fence, candidate quarantine, abandon-ledger
and checkpoint-CAS transaction. A crash retry reopens the same claim and
canonical finalize receipt; it never deletes state directly. Fresh v143 job
discovery excludes the old job only while that claim, terminal result, signed
inconclusive ledger row, canonical abandon receipt and quarantined candidate
still validate.

Before restarting after an evaluator-identity migration, establish the rating
identity and the independent official-verdict authority in this order:

```bash
cd /home/zzx/project/pok/.evolution_pok
python3 scripts/evaluation_data_identity.py
python3 scripts/official_certify.py doctor
# Only when doctor reports official_verdict_ledger_missing on a new operator host:
python3 scripts/official_certify.py init-ledger
python3 scripts/official_certify.py doctor
```

If the evaluation-data command reports an identity mismatch with existing
authoritative rating payloads, use its explicit `--archive-and-initialize`
workflow before restart. That explicit rotation archives both the top-level
rating/H2H/history payloads and every per-generation `evidence_snapshot/`
derived from them; otherwise an abandoned generation can retain an old H2H
manifest and correctly fail the new identity's integrity check forever. The
snapshot validator must remain fail-closed -- do not use `force=True` or accept
an old schema as a migration shortcut. If the verdict ledger is corrupt or
truncated, do not initialize over it; recover the operator ledger/history and
run doctor again.

`pokctl.sh` must launch the Web process with the same project Python used for
the verified runtime, rather than silently choosing an arbitrary system
`python3`. It resolves, in order, an explicit `POK_PYTHON`, an active virtual
environment, an active Conda environment, `.venv`, then a verified PATH
fallback; it checks `fastapi`, `sse_starlette`, and `uvicorn` in isolated mode
before it detaches a process. For a service or
remote shell that has no activated environment, start explicitly, for example:

```bash
cd /home/zzx/project/pok/.evolution_pok
POK_PYTHON=/absolute/path/to/project-python \
  ./pokctl.sh start --host 0.0.0.0 --port 8000 --no-build
```

An interpreter validation failure is a launcher failure, not a reason to
reuse an old running process, disable dynamic gates, or restart an old
checkpoint. Repair/select the interpreter, rerun the stopped-state
diagnostics, and launch only the current `origin/main` checkout.

`scripts/pok_restart_observe.sh` obtains that same verified interpreter through
`./pokctl.sh resolve-python` before it stops the owned service. It uses that
path for its durable AppState transaction, HTTP health check, and observer. A
missing interpreter or an unimportable config writer therefore fails before any
avoidable downtime; the helper must never fall back to a bare `python` after a
service has stopped.

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
- Do not assume a highest-numbered `bots/national_v*` directory is complete.
  Current publication requires the `national_tcp_policy_v1` five-file artifact,
  current completion metadata, annotated `national-bot-v{N}` tag, and
  role-appropriate signed full-v5 certificate. A retired tag or untagged higher
  directory is not active completion proof.
