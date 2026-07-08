# Cleanup Archive 2026-07-08

This archive collects files that are not part of the active national-native
evolution path. The active runtime checkout remains `/home/zzx/project/pok/.evolution_pok`;
do not run evolution from this archive.

## Archived Items

- `legacy_experiments/zcode/`
  - Self-contained experimental bot and student-policy data.
  - Not called by the active `web/core` national-native pipeline or `sever/tests`.
  - Kept for forensic/reference use only.

- `root_experiments/bot.py`
  - Historical reference bot used by old comparison scripts.

- `root_experiments/merge_bot.py`
  - Legacy multi-file bot merge utility. The current national-native pipeline
    commits bot directories directly and does not use this helper.

- `root_experiments/run_ref_comparison.py`
  - Legacy comparison runner. It targets old local/JSON comparison workflows,
    not the current native TCP precommit path.

- `root_experiments/ref_vs_evolved_results.json`
  - Historical output from `run_ref_comparison.py`.

- `reference_snapshots/national_v70/`
  - Untracked reference snapshot that previously lived under `ref/national_v70`.
  - Runtime caches, `.completed`, and local `botA.log`/`botB.log` were removed
    before archiving.
  - The default official-platform opponent now uses the tracked
    `bots/national_v70` directory.

## Not Archived

- `.evolution_pok/`
  - Actual long-running evolution checkout.

- `.codex_worktrees/` and `.claude/worktrees/`
  - Git worktrees. These must be managed with `git worktree remove` or
    `git worktree prune`, not moved into archive.

- `engine/`
  - Not part of the current national-native hard gate, but still imported by
    legacy local probes, RL experiments, neural lab tools, and regression tests.
  - Directly moving it would break `python engine/battle.py`, RL imports, and
    tests that compare root `engine/judge.py` behavior.
  - Archive only after a dedicated refactor updates those imports or adds a
    compatibility shim.

- `sever/engine/`
  - Active national TCP platform engine. Do not archive.

## Multi-Agent Findings

- The active workflow is `national_native`; rating/precommit/smoke use native TCP
  through `web/core/national_native.py` and `sever/`.
- Top-level `engine/` is legacy/local, but still has live references outside the
  native final gate.
- `ref/national_v70` was not present in `.evolution_pok` and was not the active
  runtime candidate/source. `pokctl.sh` now falls back to tracked bot opponents.
