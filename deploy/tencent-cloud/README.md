# Tencent Cloud Runtime — Isolated Evolution Branch

This directory deploys the national TCP poker evolution control plane on the
Tencent Cloud server as an **isolated evolution line**. Its products (bot
artifacts, certificates, version tags) live on the `tencent-cloud-runtime`
branch in a separate `national_cloud_v` tag namespace and **never enter
`origin/main`**. The main branch keeps the canonical `national_v` line
unchanged; both lines can run evolution concurrently without tag collisions.

## How the isolation works

`commit_bot` historically hard-coded `main` as the publication branch and the
`national_v` / `national-bot-v` / `national-high-water-v` prefixes. This branch
makes all four configurable through environment variables (defaults preserved
exactly, so main's behavior is unchanged):

| Variable | main (default) | this cloud runtime |
|---|---|---|
| `POK_EVOLUTION_BRANCH` | `main` | `tencent-cloud-runtime` |
| `POK_BOT_PREFIX` | `national_v` | `national_cloud_v` |
| `POK_TAG_PREFIX` | `national-bot-v` | `national-cloud-bot-v` |
| `POK_HIGH_WATER_TAG_PREFIX` | `national-high-water-v` | `national-cloud-high-water-v` |

With the cloud values, a generation produces `bots/national_cloud_v1/`,
`official_certificates/national_cloud_v1.json`, and the paired tags
`national-cloud-bot-v1` + `national-cloud-high-water-v1`, all committed to
`refs/heads/tencent-cloud-runtime`. The canonical `national_v1` namespace is
left free for main to use. Tag prefixes are not substrings of each other, so a
`national-bot-v*` glob never matches a `national-cloud-bot-v*` tag.

### Version-1 floor (not seeded from main)

This branch restarts version numbering from **1** (not 143). The version
floor is set in `web/core/bot_namespace.py`:
`ARCHIVED_VERSION_HIGH_WATER = 0`, `FIRST_STRICT_POLICY_VERSION = 1`. A fresh
cloud checkout has no paired cloud tags, so
`resolve_version_namespace_authority` falls back to the archived high-water (0)
and `policy_epoch_initialization` initializes via the `fresh_bootstrap_ready`
path. **No seed tag and no mirrored v143 directory are required.** The legacy
`seed-cloud-namespace.sh` (which seeded from main's v143) is deprecated and
refuses to run; see its header comment. The one-time epoch reset is performed
by `scripts/reset_national_tcp_policy_epoch.py --execute
--acknowledge-runtime-checkout` inside `.evolution_pok`.

## Host prerequisites

### Bubblewrap (bwrap) and unprivileged user namespaces

The quality gates and the typed runtime probe execute candidate `policy.py`
inside a [Bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`)
sandbox that unshares user/ipc/pid/net/uts/cgroup namespaces. On modern
Ubuntu kernels (>= 6.8, e.g. Ubuntu 24.04) **AppArmor restricts unprivileged
user namespaces by default**
(`kernel.apparmor_restrict_unprivileged_userns = 1`), which makes every
`bwrap --unshare-user` invocation fail with one of:

- `bwrap: Unexpected capabilities but not setuid, old file caps config?`
- `bwrap: setting up uid map: Permission denied`
- `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`

Every candidate then fails the `runtime_import` quality gate and the
generation is canonically abandoned, regardless of how correct the Worker's
`policy.py` is. The candidate-side `incremental_opponent_model` /
`typed_runtime_probe` failures are downstream symptoms of this sandbox
failure, not policy bugs.

Fix (one-time, host-level). Disable the AppArmor user-namespace restriction:

```bash
echo 'kernel.apparmor_restrict_unprivileged_userns = 0' \
  | sudo tee /etc/sysctl.d/99-pok-bwrap.conf
sudo sysctl -p /etc/sysctl.d/99-pok-bwrap.conf
# Verify:
cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns   # -> 0
bwrap --unshare-user --unshare-net --ro-bind /usr /usr -- /bin/echo ok   # -> ok
```

This setting survives reboots through `/etc/sysctl.d/99-pok-bwrap.conf`.
It does **not** disable AppArmor itself — only the blanket rejection of
unprivileged user namespaces, restoring the pre-24.04 behavior that
`bwrap` and many other sandboxing tools rely on. If a stricter policy is
required, alternatively install `bwrap` with the setuid bit
(`sudo chmod u+s $(which bwrap)`) or grant it the `cap_setuid` file
capability, but the sysctl approach is the simplest and matches the
upstream `bwrap` deployment guidance for non-setuid installs.

### Node.js (`--no-build` static receipt)

`web/main.py --no-build` refuses to start unless `node` is on `PATH`: it
runs the source-bound frontend static-receipt verifier. Install a real
Node.js (this host uses NodeSource 20.x → `/usr/bin/node`). Do **not**
point `PATH` at an IDE-bundled binary such as `~/.zcode/server`; that
path disappears when the IDE cache is deleted and takes the evolution
service down with it. `deploy/tencent-cloud/env.runtime` keeps a standard
`PATH` that includes `/usr/bin`.

### LLM saturator (idle-permit fill)

`web/core/llm_saturator.py` runs from the FastAPI lifespan and fills free
LLM permits with **bounded packets** (matchup / line-audit / function-trace,
hard-stop at 18 Read turns). It does not raise `POK_GLOBAL_LLM_CONCURRENCY`
(this 3.6Gi host OOMs above 4 streams). Pipeline Scout waves batch-preempt
the youngest packets; new launches refuse when live `claude` children already
match the permit cap or `MemAvailable` is below the floor. A queued pipeline
role does not freeze fill of idle permits. Preempt when the pool is full and
demand has lasted `PREEMPT_AFTER_SEC`. Yield at most `waiting` packets;
leftover ensemble demand (`waiting>=2`) still yields during cooldown. After
a yield wave, `PREEMPT_HOLDOFF_SEC` blocks refill so pipeline waiters take
the freed permits.

| Variable | Default | Meaning |
|---|---|---|
| `POK_LLM_SATURATOR_ENABLED` | `1` | Fill idle LLM permits |
| `POK_LLM_SATURATOR_MAX_INFLIGHT` | `4` | Packets while permits are free |
| `POK_LLM_SATURATOR_PREEMPT_AFTER_SEC` | `15` | Queue age before yielding |
| `POK_LLM_SATURATOR_PREEMPT_COOLDOWN_SEC` | `20` | Min seconds between single-waiter waves |
| `POK_LLM_SATURATOR_PREEMPT_HOLDOFF_SEC` | `15` | After a yield, do not refill |
| `POK_LLM_SATURATOR_MIN_FREE_MB` | `512` | Refuse launches below this `MemAvailable` |

### Disk hygiene (runtime artifact janitor)

`web/core/disk_hygiene.py` runs from the FastAPI lifespan (beside the LLM
saturator) and periodically reaps **non-authority** caches so a 40G cloud
disk cannot return to ENOSPC. It does **not** truncate `events.jsonl`, the
abandon ledger, ratings, match history, or `pipeline_state.json`.

| Variable | Default | Meaning |
|---|---|---|
| `POK_DISK_HYGIENE_ENABLED` | `1` | Start the janitor loop |
| `POK_DISK_HYGIENE_INTERVAL_SEC` | `300` | Seconds between cycles (min 30) |
| `POK_DISK_MIN_FREE_GB` | `4` | Below this, retention tightens |

A cycle emits `pipeline.disk_hygiene_done` when it actually freed bytes or
is in pressure mode. Watch `journalctl -u pok-evolution | grep 'disk hygiene'`.

### Daemon monitor (rating-daemon liveness poll)

`web/core/daemon_management.py` runs `daemon_monitor_thread` to auto-restart a
dead rating daemon and push the dashboard `daemon_stats` projection. Each tick
fingerprint-gates the strict-evaluation read projection on every input the
load consumes (reset receipt plus its epoch-archive claim bindings, evaluation
manifests, the identity-relevant source files, `evaluation_cycles/**`,
`match_replay/**`, and the published active pool; hidden staging entries such
as `match_replay/.pending/` and `evaluation_cycles/.cycle-*` are excluded,
since the load chain never consumes them): while no fingerprinted input
changed, the previous projection is reused instead of SHA256-re-verifying
the whole replay corpus; a failed build is never cached and is retried on the
next tick. While a projection is available, the dashboard figures are the
cycle-frozen numbers inside it, so they refresh when a cycle commits, not on
every tick. The tick used to run every 3 seconds and rebuild that projection
unconditionally — re-verifying ~465 MB of evidence and reaching 67% of the web
process CPU; liveness detection only needs bounded latency, so the tick is now
operator tunable. A dead rating daemon is therefore auto-restarted (and
`rating_daemon_exited` stability resets) up to one tick later than before.
This gates the monitor read projection only: the daemon's `save_cycle` full
re-verification and every other `load_current_strict_evaluation_bundle`
caller keep their direct, uncached path.

| Variable | Default | Meaning |
|---|---|---|
| `POK_DAEMON_MONITOR_INTERVAL_SEC` | `30` | Seconds between monitor ticks, clamped to [3, 600]; non-numeric or non-finite values fall back to the default (read once at import; set it in `env.runtime` and restart the service) |

## Dual-checkout layout

```
/home/ubuntu/pok1                     operator checkout (this repo, tencent-cloud-runtime)
                                      - develop/merge ideas, edit deploy/, run tests
/home/ubuntu/pok1/.evolution_pok      autonomous runtime clone (independent git clone)
                                      - systemd runs web/main.py here
                                      - directory name ".evolution_pok" is REQUIRED by
                                        the runtime identity contract and triggers the
                                        web/main.py namespace seed block
```

The runtime clone is a full clone (not a worktree) on the same
`tencent-cloud-runtime` branch, with `origin` pointing at GitHub. Products
published there are pushed to `origin/tencent-cloud-runtime`.

## One-time setup

```bash
# 1. From the operator checkout (on tencent-cloud-runtime):
cd /home/ubuntu/pok1
git checkout tencent-cloud-runtime
git pull --ff-only origin tencent-cloud-runtime

# 2. Edit env.runtime: set POK_PYTHON to the interpreter that has web/sever
#    requirements installed (e.g. a venv). Defaults to /usr/bin/python3.
#    Also fill ANTHROPIC_API_KEY / POK_LLM_MODEL before running a generation.
$EDITOR deploy/tencent-cloud/env.runtime

# 3. Install web/sever Python deps into that interpreter if not done already.
# 4. Run the one-time deploy script (creates .evolution_pok, seeds the cloud
#    namespace, installs the systemd unit):
bash deploy/tencent-cloud/setup.sh

# 5. Build the frontend ONCE inside the runtime clone (the service uses
#    --no-build and verifies the static receipt):
cd /home/ubuntu/pok1/.evolution_pok/web/frontend
npm install && npm run build

# 6. Start the service:
sudo systemctl start pok-evolution
journalctl -u pok-evolution -f
```

### Why no seed step is needed (version-1 floor)

Earlier versions of this deploy seeded the cloud namespace by mirroring main's
`national-bot-v143` into `national-cloud-bot-v143`. That design is superseded.
This branch sets `ARCHIVED_VERSION_HIGH_WATER = 0`, so a fresh checkout with no
paired cloud tags initializes directly via `fresh_bootstrap_ready` and targets
`national_cloud_v1`. The `seed-cloud-namespace.sh` script is deprecated and
exits as a no-op; `setup.sh` still calls it idempotently for backward
compatibility, but it does nothing.

## Files

| File | Purpose |
|---|---|
| `env.runtime` | systemd EnvironmentFile: namespace vars, POK_PYTHON, daemon sizing, LLM placeholders, thinking config |
| `env.runtime.local` | gitignored secret overlay (real GLM token); loaded after `env.runtime` |
| `pok-evolution.service` | systemd unit running `web/main.py` in the foreground |
| `setup.sh` | one-time deploy: clone runtime, install service (seed step is a deprecated no-op) |
| `seed-cloud-namespace.sh` | **deprecated** — exits as a no-op; the version-1 floor needs no seed |
| `sync-from-main.sh` | merge new ideas from origin/main into this branch |

## Synchronizing ideas from main

main receives new code/thinking. Pull it into the cloud branch **when no
generation is active**:

```bash
cd /home/ubuntu/pok1/.evolution_pok
bash /home/ubuntu/pok1/deploy/tencent-cloud/sync-from-main.sh
```

It merges `origin/main` into `tencent-cloud-runtime`. Cloud-namespace files
(`bots/national_cloud_v*`, `official_certificates/national_cloud_v*`) are kept;
idea files (`web/`, `sever/`, `scripts/`, `docs/`) take main's version. Push
the merged branch afterward so GitHub and the runtime agree.

## Operations

```bash
# service control
sudo systemctl {start,stop,restart,status} pok-evolution
journalctl -u pok-evolution -f          # live logs
journalctl -u pok-evolution --since today

# app-level logs (rotating)
ls /home/ubuntu/pok1/.evolution_pok/web/logs/
#   app.log              (RotatingFileHandler)
#   server.stdout.log    (install scripts/pok.logrotate to rotate this)

# epoch / version state
cd /home/ubuntu/pok1/.evolution_pok/web
python3 -c 'import sys; sys.path.insert(0,"core"); \
  from epoch_authority import policy_epoch_initialization; \
  import json; print(json.dumps(policy_epoch_initialization(), indent=2))'

# run a single generation (bypass the daemon loop for one cycle)
cd /home/ubuntu/pok1/.evolution_pok
python3 web/core/orchestrator.py --one-gen
```

### Dashboard authority endpoints and the observer cache

`/api/control/health` and `/api/control/status` are served through a
content-keyed singleflight cache whose builder (`_sync_evolution_fields`)
samples `strict_epoch_projection` up to three times to prove
epoch/handoff/transition identity did not move, so a build is slow (on the
order of a minute; see the measured values in
[`docs/observer-cache-availability-2026-07-28.md`](../../docs/observer-cache-availability-2026-07-28.md)).
A same-key follower **cooperatively awaits** the single in-flight build instead
of returning 503, so the first dashboard load after a restart may take that
long to populate (rarely up to `_OBSERVER_FOLLOWER_AWAIT_TIMEOUT_SEC` under
load) — this is normal, not an outage. A **changed-key** drift (authority
moved during a build) still returns a retryable fail-closed 503, which the
frontend shows as a neutral "refreshing" state on first load rather than the
red authority banner. Always start the daemon through the cloud-runtime
launcher (which exports `POK_CLOUD_RUNTIME=1`); launching it without that env
var makes the namespace prefix default to `national_v` and now fails fast at
startup with an actionable "namespace mismatch" error. To get the live build
duration, time the observer endpoint directly. Full analysis:
[`docs/observer-cache-availability-2026-07-28.md`](../../docs/observer-cache-availability-2026-07-28.md)
and [`proxy-timeout.md`](proxy-timeout.md).

Phase A poll projection (status/health; Evolution SSE remains primary-slot
only) also attaches cheap multi-slot / Slice-2b fields derived inside
`_sync_evolution_fields`:

| Field | Meaning |
|---|---|
| `active_generations` | primary + draft slots (`slot_id`, stage, `is_draft`, …) |
| `pipeline_mode` | Slice 2b activation / one-ahead coordinator (incl. consumer park) |
| `async_certification` | unpaired staging publications awaiting official cert |
| `eval_wait` | prepare-time strength-sample wait when no active lease |
| `feature_flags` | `POK_SLICE2B_ENABLED`, `POK_ALLOW_STAGING_AS_PARENT`, tag prefixes |
| `version_authority` | high-water / paired / certified / unpaired |
| daemon `configured_*` / `env_*` / `effective_*` / `pairs_drift` | rating-daemon identity |

The observer content key also watches `pipeline_state_draft.json`, so a draft
stage move invalidates the cache. Multi-slot UI must poll `/api/control/status`
rather than infer draft state from SSE. Staging bots may project
`formal_authority=staging_uncertified` / status `official-staging`; the
frontend data-stream validator accepts those values so inventory rows are not
dropped.

Operator abandon for a stuck disposable generation (e.g. `workers_done` /
`rework_running`) is `POST /api/control/abandon` (capability id
`abandon_active_generation`). It stops the live orchestrator task first, then
runs the same canonical `_do_abandon_generation` path as the MCP tool. Typed
409 boundaries: `no_active_generation_to_abandon`, `stage_not_disposable`,
`checkpoint_cas_mismatch`. Do **not** call this while intending to keep the
runtime running — it leaves the service stopped until `/api/control/start`.

### Slice 2b / staging publication env (Phase B)

Relevant `env.runtime` knobs (defaults below match the committed cloud file):

| Variable | Cloud default | Notes |
|---|---|---|
| `POK_SLICE2B_ENABLED` | `1` | one-ahead producer/consumer |
| `POK_ALLOW_STAGING_AS_PARENT` | `1` | pure staging parents (empty cert digest) |
| `POK_DEFAULT_PUBLICATION_TIER` | `staging` | async official cert after publish; first-strict stays certified |
| `POK_GLOBAL_LLM_CONCURRENCY` | see `env.runtime` | see LLM concurrency below |

Draft checkpoints use shadow identity (`is_draft=True`): skip live floor+1
allocation CAS, isolate under `RESULTS_DIR/draft_candidates/`, remap onto
formal `next_v` after primary publish. Async cert scheduling keys off
`strict_published_versions` (not a bare `published_versions` field).

## Sizing notes (4 vCPU / 3.6 GiB VM)

Daemon sizing is controlled by `env.runtime`'s `POK_DAEMON_WORKERS` /
`POK_DAEMON_PAIRS` (query the current committed values with
`grep POK_DAEMON deploy/tencent-cloud/env.runtime`; the code default and the
hard ceiling live in `start_daemon` and `MAX_DAEMON_PAIRS` in
`web/core/daemon_management.py`). The actual in-effect values are best read
from `/api/control/status` — the `configured_workers` / `env_workers` /
`effective_workers` and `configured_pairs` / `env_pairs` / `effective_pairs` /
`pairs_drift` fields show what the running daemon is using and whether it has
drifted from config. Each native-TCP match forks workers; when you resize the
instance, adjust these accordingly and monitor memory (orchestrator + daemon +
match workers).

## LLM timing (deep reasoning)

The current model (see `env.runtime`'s `ANTHROPIC_MODEL` / `POK_LLM_MODEL`;
the model id is also reported by the daemon's status endpoint) with enabled
thinking and `effort=max` is slow on complex Master-proposal prompts — minutes
to tens of minutes, scaling with provider load. This is the dominant
wall-time driver. The role timeouts are deliberately generous to avoid
killing streams mid-reasoning; the per-role timeout/budget values live in
`env.runtime` and are queryable with
`grep -E 'TIMEOUT|CONCURRENCY|THINKING|EFFORT' deploy/tencent-cloud/env.runtime`:

- Per-role `*_STALL_TIMEOUT` / `*_IDLE_TIMEOUT` / `*_TOTAL_TIMEOUT` /
  `*_FIRST_ACTIVITY_TIMEOUT` (Master, Master-proposal, Master-final, Review,
  Critic, Worker)
- `POK_GLOBAL_LLM_CONCURRENCY`, `POK_LLM_THINKING_*`, `POK_LLM_EFFORT`

Note `CYCLE_TIMEOUT` and `WATCHDOG_TIMEOUT` are **code constants, not env
vars** (`web/core/orchestrator_context.py::CYCLE_TIMEOUT`,
`web/core/evolution_infra.py::WATCHDOG_TIMEOUT`; find current values with
`rg -n "^CYCLE_TIMEOUT|^WATCHDOG_TIMEOUT" web/core/`).

The `stall` gate (productive-message silence) is the primary stuck-stream
detector; these generous values avoid killing the model mid-reasoning while
still catching truly hung streams.

### `effort=max` and thinking budget

`effort=max` is the strongest reasoning depth (GLM-5.3-Flash official coding
default). It is **NOT a death-loop**: thinking tokens grow linearly and the
model eventually emits visible text. GLM-5.3-Flash cannot disable thinking;
`thinking.type` stays enabled at the provider, and depth is `POK_LLM_EFFORT`.
Do not send `--max-thinking-tokens` on Flash: the Agent SDK would emit it
from `thinking.budget_tokens`, a fixed budget that holds visible text until
thinking finishes. Interactive `/effort max` does not set that flag; the
5.3 code path ignores `POK_LLM_THINKING_BUDGET` for the same reason. The
earlier "infinite loop" diagnosis was a misattribution — a stream that was
killed mid-reasoning was actually still making progress.
The full tuning history and the mid-reasoning kill diagnosis are recorded in
[`docs/llm-utilization-investigation-2026-07-27.md`](../../docs/llm-utilization-investigation-2026-07-27.md).

Configuration keys live in `env.runtime` (all env-overridable):
- `POK_LLM_THINKING_MODE` (`enabled`; `adaptive` is known to hang on the
  Anthropic-compatible endpoint — see the investigation doc; `disabled` is
  remapped to `enabled` on GLM-5.3*)
- `POK_LLM_THINKING_BUDGET` (ignored on GLM-5.3-Flash so the SDK cannot
  emit `--max-thinking-tokens`; still the enabled+budget path on GLM-5.2)
- `POK_LLM_EFFORT` (`max`)

### Global LLM concurrency (Phase B)

`POK_GLOBAL_LLM_CONCURRENCY` caps all sub-agent LLM calls via a process-wide
FIFO semaphore in `web/core/llm_concurrency.py` (acquired inside
`run_claude_query`); its current value is in `env.runtime`
(`grep POK_GLOBAL_LLM_CONCURRENCY deploy/tencent-cloud/env.runtime`). A prior
raise (the "Tier A.1" experiment) cut Master-ensemble wall-time but increased
429 pressure under Slice 2b one-ahead overlap (primary consumer + draft
producer overlapping); the cap is currently held lower to keep the semaphore
honest while `api_concurrency` adaptive backoff still halves further on 429.
Raise it only after quota headroom is re-measured, then restart the runtime so
the env is reloaded. The full Tier A.1 / Phase B tuning history is in
[`docs/llm-utilization-investigation-2026-07-27.md`](../../docs/llm-utilization-investigation-2026-07-27.md).

### GLM 429: 1308 quota vs 1302 frequency limit

GLM returns HTTP 429 for two different failures:

- **`[1308]` five-hour usage cap** —
  `Request rejected (429) · [1308][已达到 5 小时的使用上限。您的限额将在 <reset_time> 重置。]`.
  Wait for the provider reset; do not retry through the window.
- **`[1302]` account rate limit** —
  `Request rejected (429) · [1302][您的账户已达到速率限制，请您控制请求频率]`.
  Short backoff + `api_concurrency` reduction. **Never** a 5-hour quota
  pause (that misclassification parked the pipeline on 2026-09-09 while
  interactive requests still succeeded).

Classifier redundancy (`web/core/llm_availability.py`):

1. **Error code** — `[1302]` / `控制请求频率` → `service_unavailable` (120s).
2. **Quota body** — `[1308]` or (`已达到` and `使用上限`, not 1302) →
   `quota_429`. Explicit `限额将在 … 重置` sets
   `quota_reset_authority=provider_timestamp`; 1308 without a timestamp
   may use `quota_window_fallback` (`POK_QUOTA_FALLBACK_WINDOW_SEC`).
3. **Bare 429** — HTTP 429 / `Request rejected (429)` without 1308 →
   `service_unavailable`, never an invented 5-hour wait.
4. **Persisted authority** — quota pauses without `quota_reset_authority`
   are dropped on reconcile/persist as
   `untrusted_quota_pause_without_reset_authority` (clears the 1302
   mis-pause on restart).
5. **`rate_limiter.parse_429`** still arms only when the body contains
   `限额将在 … 重置`. `_is_quota_exceeded()` no longer treats a bare
   `Request rejected (429)` as quota.

The 1308 path still uses `rate_limiter` plus the durable availability pause:
`parse_429` extracts `限额将在 … 重置`; the orchestrator waits until that
instant; crash recovery reloads only pauses with a trusted
`quota_reset_authority`. A `pipeline.llm_quota_exceeded_detected` event
and the UI `⏳ 配额等待中` row are 1308-only.

`api_concurrency` still halves global LLM concurrency on 429/529 as the
first reaction to frequency pressure.

Waitable GLM 1302 / bare 429 (`service_unavailable`, 120s cooldown) and a
trusted `quota_429` pause **must not exit `orchestrator_loop`**. The
generation `while` catches `LLMAvailabilityBlocked` from prepare-time
roles (including `DEGENERATION_DIAGNOSIS`) and from `_run_one_cycle`,
then `_resume_generation_loop_after_llm_block` waits the cooldown and
continues. A 2026-09-10 1302 during v423 prepare stopped the orchestrator
task while the lifespan saturator kept spending for ~42 hours after the
pause file had already auto-resumed. Only billing/auth manual pauses
(or shutdown) should set `running=false`.

### Phase E deploy (operator → runtime)

After verifying tests on this operator checkout (`/home/ubuntu/pok1`):

1. Commit/push to `origin/tencent-cloud-runtime` (operator action).
2. In `.evolution_pok`: `git pull --ff-only --tags` (Git sync only; never
   copy files between checkouts).
3. `pokctl restart` / `systemctl restart pok-evolution` so `env.runtime`
   (`POK_GLOBAL_LLM_CONCURRENCY`, staging tier, Slice 2b flags) is
   reloaded.
4. Expect operator stability to reset to **0/10** after restart; schedule
   the restart in a generation empty window when possible.

## What stays out of main

- `bots/national_cloud_v<N>/` — committed to `tencent-cloud-runtime` only
- `official_certificates/national_cloud_v<N>.json` — same
- `national-cloud-bot-v<N>` / `national-cloud-high-water-v<N>` tags — pushed to
  `origin/tencent-cloud-runtime` only
- Runtime data (`web/core/results/`, `web/logs/`) — gitignored everywhere

Pushing this branch to GitHub backs up products off-host without polluting main.
