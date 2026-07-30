# Observer cache availability — `/api/control/health` persistent 503

Date: 2026-07-28
Branch: `fix/observer-503-authority-availability` (off `tencent-cloud-runtime`)
Symptom: dashboard homepage shows **「无法确认版本与运行权威」** and operators
report **HTTP 502**.

## TL;DR

The "502" was a **deliberate HTTP 503** from `/api/control/health`, faithfully
proxied by nginx (not a proxy fault). The backend was up (`/` returned 200), the
epoch was genuinely initialized and authoritative (`strict_published`,
high-water 11, valid reset receipt). **This was an availability bug, not an
authority-correctness bug.** Three compounding causes, fixed in one change:

1. **Primary — observer cache tuned for a build that no longer exists.**
2. **Secondary — first-load frontend flipped a transient refresh to a red
   authority-failure banner.**
3. **Contributing — a misconfigured daemon launch crashed with a confusing
   indirect error.**

## Diagnosis

### What the 503 actually was

`GET /api/control/health` (and `/status`) returned:

```json
{"detail":{"code":"observer_projection_refreshing",
           "reason":"observer_projection_refresh_in_progress"
                     | "observer_projection_authority_changed_during_refresh",
           "retryable":true,"authority":"strict_epoch_projection"}}
```

with HTTP 503 and `Retry-After: 1`. nginx proxies 5xx verbatim, so a 503 from
the app surfaces as 503 (not 502). uvicorn was up and answering loopback.

### Why the cache never served a result

`_sync_evolution_fields` (`web/server/routes/control.py`) builds the read-only
control snapshot. It samples `strict_epoch_projection` **up to three times**:
twice inside `stable_epoch_handoff_sample` (a before/after bracket that proves
the epoch did not move while the handoff was read) and once inside
`_refined_operator_transition`'s resample (which re-confirms transition
identity). **Each resample is a load-bearing churn/transition check — they are
not redundant and must not be deduped** (deduping would create exactly the
torn-snapshot condition the bracket exists to prevent).

Each `strict_epoch_projection` call reopens signed verdict ledgers; the whole
build takes **~76s** (measured). But the cache constants were calibrated for a
"30-80ms" build:

- `_OBSERVER_CACHE_TTL_SEC = 1.0` — a successful build evicted after 1 second.
- `_OBSERVER_HTTP_RETRY_DELAY_SEC = 0.15` with `for attempt in range(2)` — a
  ~0.30s retry window.
- A same-key follower **failed fast** with `observer_projection_refresh_in_progress`.

A 76s build can never finish inside a 0.30s retry window, and a 1.0s TTL evicts
a successful build almost immediately. So every dashboard poll during active
generation hit an in-flight (or freshly-evicted) cache and got a retryable 503.

The prior fix `5fca50e4` raised the retry delay 0.025s → 0.15s, removed a
churning key component, and taught the frontend to keep the last good status on
a retryable 503. That helped a populated page, but on a **first page load**
there is no previous status, so the dashboard still flipped to the red banner.

### Why the frontend showed the red banner

The exact string lives only at `EpochAuthorityStatus.tsx`, rendered iff
`status == null && !loading`. `useControlStatus` polls `/api/control/health`
every 5s. A retryable 503 (`RetryableControlError`) keeps the previous status —
but on first load there is none, so `status` stayed null and `loading` flipped
false, rendering the red banner under a transient backend build. The backend
said "retryable / refreshing"; the first-load UI said "authority failure".

### Why a daemon crash churned the authority (the contributing cause)

At 22:39 the rating daemon crashed in `save_cycle` with
`stored_h2h_raw_history_mismatch` → `cannot canonicalize H2H`. Root cause: that
daemon invocation was launched **without `POK_CLOUD_RUNTIME=1`**, so
`ACTIVE_BOT_PREFIX` defaulted to `national_v` and `validate_native_replay`
rejected every `national_cloud_v*` replay. The fail-closed H2H check is
**correct and the data was consistent** — it was purely an env/launch defect.
The system self-corrected (the restarted daemon had the right env), but the
indirect "H2H mismatch" crash was confusing and churned evaluation/authority
digests while it was crashing.

## The fix

All changes touch **read-only cache timing / first-load UX / a startup guard**
only. No authority digest, CAS publication, checkpoint identity, or cache-key
computation changed. The fail-closed invariants are preserved.

### 1. Cooperative await + TTL (`web/server/routes/control.py`)

- **Same-key follower → cooperative await.** A follower that arrives while a
  build for the **same** key/generation is in flight no longer fails fast; it
  parks on the cache's `threading.Condition` and is woken when the build
  completes, then served its frozen snapshot (bounded by
  `_OBSERVER_FOLLOWER_AWAIT_TIMEOUT_SEC = 90s`). This is the canonical
  singleflight pattern. The await runs on the follower's own off-loop worker
  thread (one isolated `ThreadPoolExecutor(max_workers=1)` per HTTP request —
  see `web/core/blocking_runtime.py`), so the ASGI event loop is never blocked.
  If the build does not resolve within the window, the follower still surfaces a
  retryable `refresh_in_progress` 503.
- **Changed-key follower → unchanged fail-closed.** A follower whose key moved
  while a build is in flight still raises
  `observer_projection_authority_changed_during_refresh` immediately. A
  superseded authority's bytes are **never** served under a new key.
- **`_OBSERVER_CACHE_TTL_SEC` 1.0 → 15.0.** The cache is synchronously
  invalidated on every mutation (`_invalidate_observer_projection_cache` is
  called from every config/start/stop/owner path), so a longer TTL never serves
  stale data across a write. It only reduces redundant rebuilds during
  steady-state polling.

### 2. Frontend first-load neutral state (`web/frontend/src/hooks/useControlStatus.ts`, `EpochAuthorityStatus.tsx`, `lib/controlFirstLoadState.ts`)

- New pure state machine `lib/controlFirstLoadState.ts` with three phases:
  `first_load_refreshing`, `fail_closed`, `resolved`.
- `useControlStatus` tracks `seenResolved` and keeps `loading=true` while the
  first observation has not resolved **and** the latest error is retryable, so
  the dashboard renders the neutral "正在核对…" / "正在刷新运行权威…" state. A
  populated page keeps its last good status on a retryable refresh (unchanged).
  Only a genuine non-retryable error fails closed (`setStatus(null)`).

### 3. Daemon startup namespace guard (`web/core/elo_daemon.py`, `elo_daemon_persistence.py`)

- `_assert_bot_namespace_matches_env()` runs in the `_single_writer_daemon`
  decorator before `RESULTS_DIR` is created. It lists `BOTS_DIR` directly (not
  `get_active_bots()`, which is prefix-filtered and would silently return `[]`
  under a wrong prefix) and fails fast with an actionable "namespace mismatch"
  error naming the missing `POK_CLOUD_RUNTIME` env var when on-disk bots belong
  to a different namespace. An empty pool (the legitimate first-strict state)
  is allowed through.

## Invariants preserved (constraint self-check)

- **Only read-side cache timing and first-load UX changed.** No authority
  digest, CAS publication, checkpoint identity, or cache-key computation was
  touched. The authoritative `epoch_stream_authority_digest` is computed inside
  the builder, downstream of (and unaffected by) cache timing.
- **Changed-key fail-closed behavior is unchanged** — a superseded authority's
  bytes are never served under a new key.
- **`strict_epoch_projection` call count and bracketing are unchanged** — the
  churn/transition coherence checks are load-bearing and must not be deduped.
- **The daemon's fail-closed H2H canonicalization is not weakened** — the new
  guard is an earlier, clearer startup check, not a relaxation.
- **No durable/CAS state was edited by hand.** All state transitions go through
  documented APIs/scripts.

## Tests

- `web/tests/test_control_observer_cache.py`:
  `test_observer_cache_cooperative_await_same_key_follower_single_builder`
  (rewritten from the old fail-fast test) asserts cooperative await + single
  builder + deepcopy isolation; new
  `test_observer_cache_same_key_follower_timeout_falls_back_to_retryable_503`
  covers the bounded-timeout fallback. The changed-key/drift/event-loop tests
  (`test_zero_stale_cache_changed_key_never_waits_for_old_builder`,
  `test_control_health_maps_expected_authority_drift_to_retryable_503`,
  `test_status_and_health_snapshot_builds_do_not_block_event_loop`) stay green
  and guard the preserved invariants.
- `web/tests/test_epoch_launch_guard.py`: new
  `test_daemon_startup_rejects_namespace_prefix_mismatch`,
  `test_daemon_startup_allows_empty_bot_pool`,
  `test_daemon_startup_allows_matching_namespace`.
- `web/frontend/tests/controlFirstLoadState.test.mjs`: new domain tests for the
  three phases.
- Pre-existing stale test `frontend validates but never derives canonical
  generation identity` (`sseController.test.mjs`) fixed: the validator splits
  out-of-range `canonical_version` from an expected-version mismatch
  (`canonical_version_expected`); the stale `national_v143` main-namespace
  assertion was updated to assert the modern truth (per the repo rule that every
  test failure must be root-caused and fixed).

## Verification

After restarting via systemd, `/api/control/health` resolves to 200 within the
first ~76s (the cooperative-await followers receive the single build's result)
instead of returning continuous 503, and the homepage no longer shows the red
banner on first load. A changed-key drift still returns the fail-closed 503.

## Follow-up (2026-07-30): draft checkpoint in observer key

`_observer_authority_content_key` also watches
`infra.pipeline_state_path("draft")` (`pipeline_state_draft.json`). Without
that token, a Slice-2b one-ahead draft stage move left the cached
`active_generations` projection stale until primary checkpoint / TTL churn.
Phase A control status blocks (`active_generations`, `pipeline_mode`,
`async_certification`, `eval_wait`, `feature_flags`, `version_authority`,
daemon `configured_*` / `env_*` / `effective_*` / `pairs_drift`) are
attached inside `_sync_evolution_fields`; Evolution SSE remains primary-slot
poll-supplement only for multi-slot UI. Staging bots may project
`formal_authority=staging_uncertified`; operator abandon is
`POST /api/control/abandon` (`abandon_active_generation`). Cloud
`POK_GLOBAL_LLM_CONCURRENCY=2` (Phase B) is unrelated to observer latency but
is co-deployed with this projection in Phase E.
