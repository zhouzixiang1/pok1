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
content-keyed singleflight cache whose builder (`_sync_evolution_fields`) takes
**~76s** (it samples `strict_epoch_projection` up to three times to prove
epoch/handoff/transition identity did not move). A same-key follower
**cooperatively awaits** the single in-flight build instead of returning 503,
so the first dashboard load after a restart may take up to ~76s to populate
(rarely up to `_OBSERVER_FOLLOWER_AWAIT_TIMEOUT_SEC` under load) — this is
normal, not an outage. A **changed-key** drift (authority moved during a build)
still returns a retryable fail-closed 503, which the frontend shows as a neutral
"refreshing" state on first load rather than the red authority banner. Always
start the daemon through the cloud-runtime launcher (which exports
`POK_CLOUD_RUNTIME=1`); launching it without that env var makes the namespace
prefix default to `national_v` and now fails fast at startup with an actionable
"namespace mismatch" error. Full analysis:
[`docs/observer-cache-availability-2026-07-28.md`](../../docs/observer-cache-availability-2026-07-28.md)
and [`proxy-timeout.md`](proxy-timeout.md).

## Sizing notes (4 vCPU / 3.6 GiB VM)

`env.runtime` sets `POK_DAEMON_WORKERS=2` and `POK_DAEMON_PAIRS=2`
(conservative). Each native-TCP match forks workers; raise these if you resize
the instance. Monitor memory: the orchestrator + daemon + match workers should
stay under ~2.5 GiB with these defaults.

## LLM timing (GLM-5.2 deep reasoning)

GLM-5.2 with enabled thinking and `effort=max` spends 4–9 min (up to 15–20 min
during peak provider load) on complex Master-proposal prompts. The default
role timeouts are far too tight and killed Scouts mid-thought. `env.runtime`
raises them via role-scoped env overrides:

- All LLM roles: `total=3600s`, `stall=1200s`, `idle=1800s`
- `CYCLE_TIMEOUT=14400s` (4h), `WATCHDOG_TIMEOUT=28800s` (8h)

The `stall` gate (productive-message silence) is the primary stuck-stream
detector; these generous values avoid killing GLM mid-reasoning while still
catching truly hung streams.

### `effort=max` and thinking budget

`effort=max` is GLM-5.2's strongest reasoning depth. It is **NOT a death-loop**:
thinking tokens grow linearly and GLM eventually emits visible text. The
earlier "infinite loop" diagnosis was a misattribution — the stream was killed
at 900s while GLM was still productively reasoning at 27k+ thinking tokens.

Configuration (in `env.runtime`, all env-overridable):
- `POK_LLM_THINKING_MODE=enabled` (default; `adaptive` is known to hang on GLM)
- `POK_LLM_THINKING_BUDGET=64000` (GLM treats this as a soft target, not a cap)
- `POK_LLM_EFFORT=max`

### GLM 429 quota exhaustion and recovery-window waiting

GLM-5.2 enforces a **5-hour rolling usage cap**. When exhausted, the provider
returns HTTP 429 with a Chinese body containing the reset timestamp:
`Request rejected (429) · [1308][已达到 5 小时的使用上限。您的限额将在 2026-07-25 16:20:12 重置。]`.

The system handles this through the singleton `rate_limiter`
(`web/core/rate_limiter.py`):

1. **Detection** — When any sub-agent LLM call raises a `ClaudeSDKError`
   whose text matches the GLM 429 pattern, `rate_limiter.parse_429()`
   extracts the reset timestamp from the Chinese body. Detection is wired
   at both `ClaudeSDKError` sites in `web/core/llm_query.py` (the
   signature-retry loop fallthrough and the `run_claude_query` outer
   handler). A bare 429 without an explicit reset timestamp does **not**
   set the block — the existing bounded retry behavior is preserved.
2. **Pipeline pause** — `rate_limiter.is_blocked()` returns `True`. The
   orchestrator loop checks this every cycle and blocks the entire
   evolution pipeline via `await rate_limiter.wait_until_reset()` until
   the quota resets. Every `run_claude_query` entry also checks before
   dispatching.
3. **Crash recovery** — The reset timestamp is persisted to
   `web/core/results/rate_limit_state.json`. A service restart re-applies
   the block until the reset time.
4. **Operator visibility** — A `pipeline.llm_quota_exceeded_detected`
   event is emitted with the role and reset time. The UI status shows
   `⏳ 配额等待中 → <reset_time>`.

No environment variable configures the 429 behavior — `rate_limiter` is
always active. The `api_concurrency` adaptive backoff (which halves global
LLM concurrency per 429) still fires as an immediate first reaction.

## What stays out of main

- `bots/national_cloud_v<N>/` — committed to `tencent-cloud-runtime` only
- `official_certificates/national_cloud_v<N>.json` — same
- `national-cloud-bot-v<N>` / `national-cloud-high-water-v<N>` tags — pushed to
  `origin/tencent-cloud-runtime` only
- Runtime data (`web/core/results/`, `web/logs/`) — gitignored everywhere

Pushing this branch to GitHub backs up products off-host without polluting main.
