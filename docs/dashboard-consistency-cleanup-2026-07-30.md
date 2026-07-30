# Dashboard consistency, redundancy, and 503/502 cleanup — 2026-07-30

**As-of:** 2026-07-30. **Branch:** `tencent-cloud-runtime`. **Status:** all
changes landed; backend + frontend tests green.

This document records the investigation and fixes for three interrelated
operator complaints: frontend/backend/business-logic inconsistencies, large
redundant/unclear surfaces, and frequent 503/502. It was driven by three
parallel read-only audits (consistency / redundancy / performance) plus live
nginx + systemd evidence.

## Diagnosis summary

The three problems are coupled: **redundant polling amplified the 503/perf
issue**, and the perceived "inconsistency" was mostly **ambiguous/redundant
copy**, not real contract drift. The consistency audit found only four real
defects; the contract surface is otherwise well-disciplined.

## A. Performance / 503 / 502

### 503 — observer projection singleflight (root-caused, P1 fix)

`web/server/routes/control.py` serves `/api/control/health` and `/status`
through `_ObserverSingleflightCache`. The observer builder
(`_sync_evolution_fields`) takes **~76s** because it samples
`strict_epoch_projection` up to three times to prove epoch/handoff/transition
identity did not move. **Defect:** `_OBSERVER_STATUS_CACHE` was instantiated
with `stale_while_revalidate_sec=60.0` but **`_OBSERVER_HEALTH_CACHE` with
`0.0`**. `/health` is the more frequently polled endpoint (every observer
page), and its builder re-derives from the same ~76s status projection, so the
asymmetry meant a same-key `/health` read during an active build parked on the
90s cooperative await (`_OBSERVER_FOLLOWER_AWAIT_TIMEOUT_SEC`) instead of
serving the prior proof → occasional retryable 503.

**Fix (P1):** `_OBSERVER_HEALTH_CACHE` now carries
`stale_while_revalidate_sec=60.0`, symmetric with status. A same-key `/health`
read during a build serves the stale prior projection immediately and triggers
one background refresh; a **changed-key** read still fails closed immediately
(`observer_projection_authority_changed_during_refresh`) — the
never-serve-stale-authority invariant is unchanged. Regression:
`tests/test_control_observer_cache.py::test_production_health_cache_serves_stale_same_key_during_build`
(the two existing health fixtures were also updated to the symmetric config).

The **changed-key** 503 and the historical persistent-503 (cache TTL tuned for
a 30-80ms build) and the `/start` HTTP-000 busy-poll starvation are all
**already fixed** with regression tests; the changed-key fail-closed is
intended behavior (never serve a superseded authority).

### 502 — backend down (`Connection refused`), NOT a proxy timeout

A live nginx error-log audit corrected the hypothesis that 502 =
`proxy_read_timeout` too short. The deployed proxy **already** runs
`proxy_read_timeout 300s` / `proxy_send_timeout 300s`, yet 502s still occur.
The real signature is `connect() failed (111: Connection refused) while
connecting to upstream` — uvicorn is not listening on `127.0.0.1:8000` because
`pok-evolution.service` is **stopped / restarting / crashed**. The proxy never
reaches the read-timeout window because the TCP connect itself fails. Observed
bursts coincide exactly with service downtime (host reboot, or `systemctl
restart` after code/env changes). So the 502 root cause is **backend service
availability**, not the proxy window. The 300s settings remain correct (they
prevent a *busy* backend from being cut off). Verification commands and
operator guidance were added to `deploy/tencent-cloud/proxy-timeout.md`. No
code change addresses a *down* backend; it self-heals once the service reaches
`active (running)`.

### Polling load — 15× /health → 1× (P2)

`DataProvider.tsx` already polled `/health` via `useControlStatus(5_000)` but
discarded everything except the stream-authority key, so **every evolution page
instantiated its own `useControlStatus(...)`** — ~13 pages plus the provider =
15× `/health` every 5s (3s on ControlPanel), amplifying load exactly during the
~76s build the cache exists to absorb. **Fix (P2):** `DataProvider` now exposes
the full poll value via a new `useControlStatusValue()` context hook; all 13
live pages consume it and no longer instantiate their own poll. Single 5s poll
app-wide. Pure frontend; no backend contract change.

## B. Consistency (four real defects, rest clean)

The high-risk consistency items (hardcoded main-branch version literals;
approved vs advisory_approved; daemon_enabled=false as supported mode;
publication_tier/certified_tag/staging; stage labels; stability target;
next_v/source_v; route redirects; retryable-503 neutral state) were all
verified **consistent**. The four real defects:

- **M1/M2 — fabricated reason codes.** `web/frontend/src/lib/notStuckReasons.ts`
  carried `quota_wait` and `draft_preparing` codes the backend never emits
  (grep of `web/server`+`web/core` = 0 hits), violating the "frontend must not
  infer authority" rule. Quota state arrives via the SSE data-stream, not a
  control-status reason code. **Fix:** removed both; the file now carries only
  codes the backend actually surfaces, with pure-Chinese operator copy.
- **C1 — orphaned pages + dead chain.** `pages/EvolutionMonitor.tsx` (49 KB)
  and `pages/BotInventory.tsx`, orphaned by the 2026-07-30 IA merge, plus their
  dead chain (`components/evolution/CostBreakdown.tsx`,
  `WorkerProgress.tsx`/`parseWorkerStatus`, `api/evolution.ts::fetchEvolutionState`,
  `evolutionHeaderNotStuckTip`) were deleted. The SSE liveness contract they
  carried (`acceptTransientStatus`, run-flag-without-task, stream-interrupted
  fail-closed) was **migrated to the live `/agents` page**
  (`AgentActivity.tsx`: `runFlagWithoutTask` / `streamInterrupted` +
  operator-visible statusText), and the
  `national_alignment_matrix_data` production-owner entry +
  `test_frontend_contract_closure` liveness assertions were repointed there.
- **C2 — duplicate `/bots` sidebar.** `AppSidebar.tsx` had two `/bots` entries
  (`发布池` + `严格发布 Bot`); collapsed to the single `发布池` entry.

All affected contract-closure/matrix tests were updated in the same change
(including the regenerated
`docs/national-tcp-evolution-alignment-matrix.md` region).

## C. Redundancy / operator copy

- **R1 single-home:** removed the duplicated full `PipelineStatus` stepper from
  `Overview` (full stepper single-homed on `/pipeline`; Overview keeps its
  `<Link to="/pipeline">` plus its now-dead checkpoint poll/state); removed the
  self-polling `OfficialCertificationProgress` from `ControlPanel` (cert
  progress single-homed on `/evidence`); removed the duplicate `pairs_drift`
  badge from the `EvolutionPageHeader` compact variant.
- **R2 operator copy:** `PhaseAProjectionStrip` badges (`hw→版本高水位`,
  `paired→已配对`, `in_flight→在飞数`, `slice2b→并行车道`, `eval_wait→评测等待`,
  `staging parent→允许暂存父本`); `EvolutionPageHeader` now renders
  `epochStateLabels[epoch_state]` instead of the raw enum; `PipelineDiagnostics`
  + `BackgroundStrength` daemon copy (`cfg/eff/drift` → 配置对数/实际对数/不一致,
  `route/owner` → 下一动作/归属); `EvolutionStreamPanel`/`AgentActivity`
  subtitles stripped of SSE/slot/redirect implementation notes; the `park`
  visual tone recolored violet so "停泊/不是卡住" is distinct from a real warning
  (warning-*/amber-* previously resolved to the same hue).

## Verification

- Backend: `tests/test_control_observer_cache.py` (14, incl. new regression),
  `test_blocking_runtime.py`, `test_control_phase_a_projection.py`,
  `test_frontend_contract_closure.py`, `test_national_alignment_matrix.py` —
  all green.
- Frontend: `tsc -b` clean; `eslint .` 0 errors (only pre-existing
  react-refresh warnings); `node --test tests/*.test.mjs` 102/102.

## Scope not taken

The `EvidenceGates` "本次发布对象" identity grid and "恢复与身份核对" block were
intentionally **kept** — they are the page's primary purpose, not pure
duplicates. The `checkpoint` / `agents` / `certs` hooks remain page-scoped
(stage-gated polling); only `/health` was globally converged (it has no
stage-gating and is read by every page).
