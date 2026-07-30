# Reverse proxy timeout settings (503 prevention)

## Context

`pok-evolution.service` runs uvicorn bound to loopback
(`HOST=127.0.0.1`, `PORT=8000`). TLS termination and public exposure are meant
to be handled by a reverse proxy in front of uvicorn — `env.runtime` notes:

```
# Bind loopback by default; put a reverse proxy (nginx/caddy) in front for TLS.
HOST=127.0.0.1
```

No nginx / caddy / apache config is shipped in this repo; the proxy is
provisioned out-of-band on the host. This document records the timeout
settings the proxy **must** use so a briefly-slow backend is not cut off with
an HTTP 503.

## Why the backend is sometimes briefly slow

uvicorn runs **single-worker** and shares its event loop with the orchestrator.
The API handlers were offloaded to isolated worker threads (commit `b9b8b4fe`),
and the expensive read-only projections are mtime-keyed cached (task A3), but
the backend can still take tens of seconds when:

- A poll burst arrives while the orchestrator holds the GIL during a large
  JSON/JSONL read or git publication proof (`git ls-remote` can take 30-60s on
  a constrained link — see `POK_REMOTE_PUBLICATION_CACHE_TTL` in `env.runtime`).
- The rating daemon is forking native-TCP match workers under memory pressure.
- The VM (4 vCPU / 3.6 GiB) is under concurrent LLM-stream load.

The LLM role timeouts themselves are generous (`total=3600s`) and the cycle
timeout is 4h (`CYCLE_TIMEOUT=14400s`). The reverse proxy default timeouts
(nginx: `proxy_read_timeout 60s`, caddy: default 0 / no limit but some setups
inherit a short upstream timeout) are far too short relative to a legitimately
busy backend and will surface as **502/504 (or 503 if the proxy's own upstream
window fires)** to the frontend.

## Recommended settings

### nginx

In the `location` that proxies to uvicorn (`proxy_pass http://127.0.0.1:8000;`):

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;

    # Give the backend up to 5 minutes to respond. The read-only poll endpoints
    # are normally sub-second, but a publication proof or a large snapshot read
    # under load can take a minute or more; 300s covers the worst observed case
    # without letting a truly wedged request hang forever.
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;

    # Connect should still be fast (uvicorn is local); a short connect timeout
    # fails fast if the service is down rather than making the client wait.
    proxy_connect_timeout 10s;

    # SSE / streaming endpoints (e.g. /api/events) must not be buffered.
    proxy_buffering off;
    proxy_cache off;

    # Pass through the real client info.
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Key points:
- `proxy_read_timeout 300s` — the single most important setting. The default
  60s is what produces the 503/504 when the backend is briefly slow.
- `proxy_connect_timeout 10s` — keep short so a down backend fails fast.
- `proxy_buffering off` — required for the SSE event stream; without it,
  streaming events are held until the buffer fills.

### caddy

If using Caddy (Caddyfile), the equivalent is:

```caddy
reverse_proxy 127.0.0.1:8000 {
    # read/send up to 5 minutes
    transport http {
        read_timeout 300s
        write_timeout 300s
        dial_timeout 10s
    }
    # flush immediately for SSE
    flush_interval -1
}
```

## Verification

After applying, with the service running:

```bash
# Backend is reachable through the proxy:
curl -fsS https://<host>/api/daemon/status | jq .

# A deliberately slow read (simulate by watching the metrics file grow) should
# not 503 within 300s.
watch -n2 'curl -sS -o /dev/null -w "%{http_code} %{time_total}s\n" https://<host>/api/llm/metrics/summary'
```

If 503s persist with these timeouts, the cause is **not** the proxy window —
check `journalctl -u pok-evolution -f` and the A3 cache layer instead.

## The persistent 503 on `/api/control/health` (resolved 2026-07-28)

A distinct, recurring 503 on the dashboard's authority endpoints was mislabeled
"502" by some operators. Its root cause was **not** the proxy window — it was
the read-only observer-projection singleflight cache being tuned for a build
that no longer exists. Full analysis in
[`docs/observer-cache-availability-2026-07-28.md`](../../docs/observer-cache-availability-2026-07-28.md);
summary:

- The observer builder (`_sync_evolution_fields` in
  `web/server/routes/control.py`) takes **~76s** because it samples
  `strict_epoch_projection` up to three times to prove epoch/handoff/transition
  identity did not move (those resamples are load-bearing churn checks and are
  **not** redundant). The cache constants were calibrated for a "30-80ms" build:
  `_OBSERVER_CACHE_TTL_SEC = 1.0` (a successful build evicted after 1s) and a
  `0.30s` retry window. A 76s build could never complete inside that window, so
  every poll during active generation returned a retryable 503
  (`observer_projection_refreshing`).
- The previous fix (`5fca50e4`) raised the retry delay from 0.025s to 0.15s and
  taught the frontend to keep the last good status on a retryable 503 — but it
  did not close the gap, and on a **first page load** (no previous status) the
  dashboard still flipped to the red "无法确认版本与运行权威" banner.

Final fix (this branch):

1. **Cooperative await (singleflight).** A same-key follower no longer fails
   fast with 503; it parks on the cache's condition variable and is served the
   single in-flight build's result (bounded by
   `_OBSERVER_FOLLOWER_AWAIT_TIMEOUT_SEC = 90s`). A **changed-key** follower
   still fails closed (`observer_projection_authority_changed_during_refresh`)
   — the never-serve-stale-authority invariant is unchanged. The await runs on
   the follower's own off-loop worker thread (one isolated
   `ThreadPoolExecutor(max_workers=1)` per request), so the ASGI event loop is
   never blocked.
2. **`_OBSERVER_CACHE_TTL_SEC` 1.0 → 15.0.** The cache is synchronously
   invalidated on every mutation (`_invalidate_observer_projection_cache`), so a
   longer TTL never serves stale data across a write — it only reduces redundant
   rebuilds during steady-state polling.
3. **Frontend first-load neutral state.** `useControlStatus` keeps
   `loading=true` while the first observation has not resolved and the latest
   error is a retryable 503, so the dashboard shows the neutral "正在核对…" /
   "正在刷新运行权威…" state instead of the red banner. Only a genuine
   non-retryable authority error fails closed. (Pure state machine:
   `web/frontend/src/lib/controlFirstLoadState.ts`.)
4. **Daemon startup namespace guard.** A daemon launched without
   `POK_CLOUD_RUNTIME=1` now fails fast at startup with an actionable
   "namespace mismatch" error instead of crashing inside `save_cycle` with an
   indirect `stored_h2h_raw_history_mismatch`.

## The actual 502 mechanism: backend down (`Connection refused`), not timeout

A live audit of the deployed nginx error log (2026-07-30) corrected the earlier
hypothesis that 502 = `proxy_read_timeout` too short. The deployed proxy
**already** runs `proxy_read_timeout 300s` / `proxy_send_timeout 300s`, yet 502s
still occur. The real 502 signature is:

```
[error] ... connect() failed (111: Connection refused) while connecting to
upstream, ... upstream: "http://127.0.0.1:8000/api/control/health"
```

That is **`Connection refused`** — uvicorn is not listening on `127.0.0.1:8000`
because the `pok-evolution.service` unit is **stopped / restarting / crashed**.
The proxy never gets to the read-timeout window because the TCP connect itself
fails. Observed bursts coincide exactly with service downtime windows (e.g. a
host reboot, or a `systemctl restart` after code/env changes). During such a
window the frontend's ~5s `/health` poll hits `Connection refused` → nginx
returns **502 Bad Gateway** to the browser.

So the 502 root cause is **backend service availability**, not the proxy
window. The 300s timeout settings remain correct (they prevent a *busy*
backend from being cut off), but they do not and cannot address a *down*
backend.

### Verification (run on the host)

```bash
# 1. Is the timeout actually 300s on the live proxy?
sudo nginx -T 2>/dev/null | grep -iE 'proxy_(read|send)_timeout'

# 2. Is a 502 actually a Connection-refused (backend down), not a timeout?
sudo tail -500 /var/log/nginx/error.log | grep -iE 'connect\(\) failed|upstream timed out' | tail
#   connect() failed (111: Connection refused)   -> backend DOWN (502)
#   upstream timed out (110: Connection timed out)-> backend BUSY (502/504)

# 3. Is the service up?
sudo systemctl status pok-evolution --no-pager | head -5
sudo ss -ltnp | grep ':8000'   # uvicorn must be LISTEN on 127.0.0.1:8000

# 4. 503 (not 502) is the app's intended fail-closed observer projection:
sudo tail -1000 /var/log/nginx/access.log | awk '{print $9}' | sort | grep -E '502|503|504' | sort | uniq -c
```

### Operator guidance

- A **502** during/after a restart or crash is expected and self-heals once
  `pok-evolution.service` reaches `active (running)` and uvicorn binds
  `127.0.0.1:8000`. No code change is required; verify with step 3 above.
- A **503** (`observer_projection_refreshing`, with `Retry-After: 1`) is the
  intended fail-closed during an authority change; the frontend renders it as a
  neutral "正在刷新…" state, not an error.
- A **persistent 502 while the service shows active** would indicate uvicorn
  failed to bind (port conflict / crash in startup) — check
  `journalctl -u pok-evolution -f` for the bind error rather than the proxy.

## The same-key /health 503 (resolved 2026-07-30)

Even after the 2026-07-28 cooperative-await fix, a same-key `/health` follower
during a ~76s build could still occasionally surface a retryable 503, because
the **health** cache (`_OBSERVER_HEALTH_CACHE`) was instantiated with
`stale_while_revalidate_sec=0.0` while the **status** cache had `60.0`. Health
is the more frequently polled endpoint (every observer page), and its builder
re-derives from the same ~76s status projection, so the asymmetry meant a
same-key `/health` read during a build parked on the 90s cooperative await
instead of serving the prior proof.

Fix (`web/server/routes/control.py`): `_OBSERVER_HEALTH_CACHE` now carries
`stale_while_revalidate_sec=60.0`, symmetric with status. A same-key `/health`
read during a build serves the stale prior projection immediately and triggers
one background refresh; a **changed-key** read still fails closed immediately
(`observer_projection_authority_changed_during_refresh`) — the
never-serve-stale-authority invariant is unchanged. Regression:
`tests/test_control_observer_cache.py::test_production_health_cache_serves_stale_same_key_during_build`.

