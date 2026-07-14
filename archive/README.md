# Archive Manifest — `/home/zzx/project/pok/archive/`

> **Current authority notice (2026-07-14):** Everything below `archive/` is
> historical `legacy-untrusted` material. Nothing in this manifest is a current
> restore/run/import instruction, and no archived code, prompt, test, bot,
> rating, replay, experience, result, or analysis may enter
> `national_tcp_policy_v1`. Statements below describe the repository at the
> time of earlier reorganizations and may name facilities that no longer exist
> in the active tree. Current authority lives in root `AGENTS.md` and the
> top-level documents under `docs/`; do not follow the historical restore
> commands below without a separate, explicit forensic task.

**Reorg date:** 2026-06-20
**Principle:** 零删除（everything preserved, never `rm`'d）— only `mv` into this tree.
**System safety:** all moves verified against read/write traces; the running
orchestrator + daemon + web UI keep working. Items here are either (a) historical
with no current troubleshooting value, or (b) at risk of being auto-overwritten by
the system's own rotation, so they were rescued here.

This directory already held retired old-layout code (`dashboard/`,
`evolution_workspace/`, `orchestrator/`). The 2026-06-20 reorg added the batches
below. Each entry lists source, count, size, rationale, restore command, and
runtime impact.

> **Git tracking:** `archive/logs/`, `archive/forensic/`, `archive/clutter/` are
> gitignored (their origins `web/logs/`, `web/core/results/` were already
> gitignored runtime data — keeping them out of git avoids committing bulky
> logs/replays/.bak into history). They are **preserved on disk only**.
> `archive/task_context/bots/` IS tracked (small evolution briefs, git-detected
> as renames from their original `bots/claude_v*/.task_context/` paths).

---

## 2026-06-20 batches

### `logs/orchestrator_sessions/` — 1 file, 40K
- **Source:** `web/logs/orchestrator_20260615_155425.txt` (the single session
  beyond the newest 20).
- **Why:** `_rotate_orchestrator_logs` (`keep=20`, orchestrator_session.py) would
  delete it on the next orchestrator boot. The `/api/logs/orchestrator` route
  (logs.py:43-76) only lists/serves the newest 20, so this one is invisible to the
  UI anyway. Archived to rescue the LLM session trace before auto-deletion.
- **Restore:** `mv archive/logs/orchestrator_sessions/*.txt web/logs/` (it will be
  re-pruned to 20 by the next boot if >20 exist).
- **Runtime impact:** NONE.

### `logs/monitor_8h/` — 5 files, 68K
- **Source:** `web/logs/monitor_8h.{log,nohup.log,sh}`, `monitor_8h_CHECKLIST.md`,
  `MONITOR_8H_REPORT.md`.
- **Why:** One-shot 8-hour monitoring run on 2026-06-17. The script had a syntax
  error (`monitor_8h.nohup.log` shows the bash parse error), was never restarted,
  and has no cron/systemd scheduling (verified). No `web/` code imports it.
  Abandoned machinery — no current troubleshooting value.
- **Restore:** `mv archive/logs/monitor_8h/* web/logs/`.
- **Runtime impact:** NONE.

### `logs/monitoring/` — 2 files, 88K
- **Source:** `web/logs/monitoring/collect_health.py`, `health_snapshots.jsonl`.
- **Why:** Standalone health-collector, not wired into the app lifespan; no
  `web/` imports of `collect_health`. Last wrote 2026-06-12.
- **Restore:** `mv archive/logs/monitoring web/logs/`.
- **Runtime impact:** NONE.

### `logs/rotation_backups/` — 2 files, 32M
- **Source:** `web/core/results/battle_exp_llm.log.1` (23M),
  `web/logs/app.log.4` (10M).
- **Why:** Rotation backups that the system would **overwrite** on the next
  rotation cycle (`battle_experience.py` renames `.log`→`.log.1`, deleting any
  pre-existing `.log.1`; `RotatingFileHandler backupCount=5` cycles `.3`→`.4`).
  Archived to preserve those snapshot windows of battle-LLM traces and app log.
- **Restore:** `mv archive/logs/rotation_backups/battle_exp_llm.log.1 web/core/results/`
  and `mv archive/logs/rotation_backups/app.log.4 web/logs/`. (The live writers
  will simply recreate fresh `.log.1` / `.4` on the next rotation if absent.)
- **Runtime impact:** NONE — these are not actively held open; only touched at
  rotation moments.

### `forensic/` — 2 files, 1.5M
- **Source:** `web/core/results/rating_history.jsonl.broken.bak` (1.4M),
  `web/core/results/pipeline_state.json.ghost107.bak`.
- **Why:** Manual forensic snapshots — a corrupted `rating_history.jsonl` that was
  renamed out of the way, and a leftover `pipeline_state.json` from the ghost-107
  incident. No code reads `.bak` files (verified by grep). Kept for post-mortem
  reference, moved out of the live `results/` dir.
- **Restore:** `mv archive/forensic/*.bak web/core/results/`.
- **Runtime impact:** NONE.

### `task_context/bots/` — 10 files, 96K (TRACKED)
- **Source:** `bots/claude_v{102,108,109,110,111,113,114}/.task_context/w*.md`.
- **Why:** Worker task briefs from superseded/completed generations. The Master
  LLM writes these and Workers read them at runtime (master_prompt.md:84), so the
  **active** ones (root, `web/core`, `bots/claude_v121`, `bots/claude_v123`) are
  left in place; only finished generations' briefs were archived.
- **Restore:** `mkdir -p bots/claude_vXXX/.task_context && mv archive/task_context/bots/claude_vXXX/* bots/claude_vXXX/.task_context/`.
- **Runtime impact:** NONE (these generations are already committed/tagged).
- **Git note:** Added `.task_context/` to `.gitignore` to stop churn on the active
  locations. The archived copies here remain tracked (renames).

### `clutter/` — 10 files, 736K
- **Source:** `.playwright-mcp/` (8 Playwright MCP screenshots/console logs,
  Jun 1–3), `.DS_Store` (root), root `package-lock.json` (82B empty stub).
- **Why:** Runtime artifacts / OS junk / stray npm output with zero
  troubleshooting value, but **not deleted** per the zero-deletion policy.
  `.playwright-mcp/` and `package-lock.json` were untracked from git
  (`git rm --cached`) and both added to `.gitignore`.
- **Restore:** `mv archive/clutter/.playwright-mcp . && mv archive/clutter/.DS_Store . && mv archive/clutter/package-lock.json .`
- **Runtime impact:** NONE.

---

## Explicitly NOT moved (kept in place — troubleshooting value or live)

These were considered and deliberately left in the working tree:

- **`web/core/results/match_replay/`** (1992 files, ~4.6G) — replay JSONs that
  diagnose bot decisions/losses; served by `/api/matches/replay/{id}`
  (matches.py:42-77, graceful 404 if missing). Space is not a concern; archiving
  would only create dead links in the Replay UI. The daemon self-caps at
  `MAX_REPLAY_FILES=2000` (elo_daemon.py:76).
- **`web/core/results/v*/logs/*.txt`** (all generations) — per-gen
  master/worker/critic traces, served by `/api/logs/generations/{ver}/{file}`
  (logs.py:25-40). All retained; archiving old gens only empties the Logs page
  for them with no space benefit.
- **`web/core/results/` live data** — `glicko_ratings.json`, `rating_history.jsonl`,
  `system_events.jsonl`, `events.jsonl`, `match_history.jsonl`, `llm_costs.jsonl`,
  `pipeline_state.json`, `head_to_head.json`, per-opp stats, etc. — all actively
  read/written.
- **`web/core/results/experience_pool_audit_io.txt`** (3.8M) — already
  self-rotating at 20MB via `_append_role_io` (llm_query.py:44-99); not unbounded.
  See `scripts/pok.logrotate` for the one genuinely unbounded log.
- **`web/logs/app.log` + `.1/.2/.3`** — active RotatingFileHandler sink.
- **Newest 20 `orchestrator_*.txt`** — active session traces served by the logs
  route (including the current session and the one empty 0B stub, kept per
  zero-deletion).
- **Active `.task_context/`** (root, `web/core`, `bots/claude_v121`,
  `bots/claude_v123`) — runtime Worker inputs.
- **`web/core/results/archive/`** (566 files) — the project's own per-version cost
  archive, read by cost tooling.
- **Root `results/*.json`** (257 files) — legacy competition JSONs, gitignored,
  tiny, zero risk.
- **`ref/DanLM`, `ref/neuron_poker`** — registered as submodule gitlinks (git mode
  160000); the 244M of actual content lives in their embedded `.git/`, not the
  parent repo. Left in place as vendored references.

## Related config changes

- **`.gitignore`** — added `.task_context/`, `.playwright-mcp/`, `/package-lock.json`,
  and `archive/{logs,forensic,clutter}/` (see inline comments there).
- **`scripts/pok.logrotate`** — logrotate config for `web/logs/server.stdout.log`
  (the one genuinely unbounded log; `copytruncate`, `rotate 10`, `compress`,
  `weekly`). Install instructions in the file header. Currently a repo config +
  dry-run validated; activate via `sudo cp scripts/pok.logrotate /etc/logrotate.d/pok`.

## Optional future enhancement (not done — user decides)

To make the system itself preserve more going forward (space is not a concern):
- Raise `MAX_REPLAY_FILES` (elo_daemon.py:76) above 2000.
- Raise `keep` in `_rotate_orchestrator_logs` (orchestrator_session.py) above 20.
