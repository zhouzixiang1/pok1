# National TCP Poker Evolution Dashboard

This React 19 application is the operator dashboard for the repository's
`national_tcp_policy_v1` evolution runtime. It is a read-mostly projection of
the FastAPI contracts under `web/server/routes/`; it is not an independent
source of bot, version, rating, certification, or protocol authority.

## Development and build

Use the repository-pinned lockfile:

```bash
cd web/frontend
npm ci
PYTHON=/path/to/project-python npm test
npm run lint
npm run build
```

The SSE producer contract imports the live Web producer graph, including its
FastAPI and LLM-SDK dependencies. `PYTHON` must therefore name the same project
Python environment used for `web` tests; a bare system `python3` is rejected
rather than silently validating a substitute producer graph.

`npm run dev` starts the Vite development server. `npm run build` writes
`web/frontend/dist/` and copies the generated application to
`web/server/static/`. Both locations are generated outputs. The production
launcher is `python web/main.py`; on a fresh checkout, run it once without
`--no-build`.

The application uses React, TypeScript, Tailwind CSS, ApexCharts, and
`react-router` v7. The package name retains historical template provenance,
but this source tree is the poker evolution dashboard and should be documented
and tested as such.

## Authority model

Every page fails closed against `/api/control/status`:

- The only canonical epoch is `national_tcp_policy_v1`.
- Before the signed reset receipt is valid, API evidence is shown as empty and
  old ratings, matches, logs, checkpoints, and in-memory SSE events are not
  reused.
- `v142` is only the immutable pre-policy numeric high-water. In the explicit
  `reset_required` state, the first strict target is `v143`.
- Recovery and unavailable states do not claim a next version.
- A durable runtime-reconciliation claim is a hard launch barrier. The UI
  exposes the backend-owned continuation command and clears all stream state;
  it never treats the claim as an ordinary stopped checkpoint.
- Directories such as an uncommitted/untagged `national_v155` are debris, not a
  published bot, candidate, generation result, or version authority.
- The current published pool comes from strict epoch projection. The first
  published bot may legitimately appear before its first matching evaluation
  cycle; during that interval the UI shows “awaiting first rating cycle” and
  never invents a zero/default selection score.

One strength sample is one complete, compliant 70-hand local native TCP match.
Selection score and Glicko/H2H rows come only from the immutable evaluation
cycle whose identity matches the exact current published pool. Net-chip
magnitude is secondary evidence, not a replacement score.

## Certification and Arena

Formal publication authority is a backend-validated, content-bound
`official-full-v5` certificate. Normal strict generations run five 70-hand
self-play rounds plus three 70-hand rounds against an eligible strict opponent.
The one-time v143 `first_strict_control_v1` profile instead binds the authorized
system control and has zero strength and strategy-evidence weight; the browser
does not describe it as a normal strict-pool H2H certification. Certificates
are signed and bound to the official verdict ledger. The browser consumes `formal_certified` and
`formal_authority=signed_full_v5`; it does not reconstruct certificate validity
from a loose summary.

The retired HTTP certification enqueue endpoint is intentionally absent from
the client. Normal certification is advanced by the checkpointed orchestrator;
the first strict bootstrap uses the explicit acknowledged operator CLI path.
While `official_bootstrap_required` is active, `/api/certification/jobs` may
expose exactly one request-bound v143 job as
`formal_authority=operator_bootstrap_full_v5_job`, `read_only=true`, and
`cancel_allowed=false`. The dashboard may display its exact rounds and the
operator command, but cannot start or cancel it. Unrelated bootstrap jobs,
v155 debris, and old-epoch jobs remain invisible.

The v143 operator transition is also server-owned. The dashboard distinguishes
`bootstrap_required`, `bootstrap_running`, `bootstrap_failed`, and
`ready_to_finalize`; it never derives the finalize command from a generic job
state or treats an operator pause as a failed certification.

National Arena sessions are presentation and protocol diagnostics only:

- `result_authority=diagnostic_only`
- `affects_glicko=false`
- `official_exe_certification=false`
- `can_certify=false`

Arena results, wire logs, and local THP files never update strength ratings or
grant publication eligibility.

## Read and mutation boundaries

Ratings, replay, bot inventory/source, prompt contracts, pipeline state, logs,
and certification progress are read-only dashboard surfaces. Prompt changes
must be reviewed in source control and synchronized through the repository's
dual-checkout policy; the browser cannot hot-edit them. There is no generic MCP
tool runner or arbitrary certification launcher in the UI.

The limited operator mutations (orchestrator start/stop/config, session clear,
and Arena lifecycle) use the shared `POK_CONTROL_TOKEN` backend authority and
the `X-Control-Token` request header. The token entered in the control panel is
held only in the current JavaScript process memory and is cleared by a page
reload. It is never written to local storage.

## Primary data contracts

- `/api/control/status` plus `/api/control/health`: epoch/version authority,
  active task liveness, the checkpoint-validated deterministic next route,
  daemon intent versus actual heartbeat health, and a TTL-bound background
  verification of the persistent 10-generation observation. Browser polling
  pairs the two snapshots, rejects identity drift, and never overlaps requests.
- `/api/ratings`, `/api/history`, `/api/matches/*`: current immutable strength
  bundle only.
- `/api/bots`: current strict published inventory; unpublished and historical
  directories are excluded.
- `/api/certification/*`: formal full-v5 status and exact current durable-job
  progress; enqueue is retired and v143 bootstrap is read-only.
- `/api/evolution/state` and `/api/evolution/stream`: current initialized epoch
  only. One backend-issued SHA-256 stream identity binds the reset receipt,
  published high-water, and active strict pool. The browser, live clients, and
  replay ring all use that exact digest; publication clears the preceding ring.
- `/api/data/stream`: immutable strength projections plus daemon liveness. A
  disconnect, reset, or publication-identity movement clears the full cached
  projection and cannot remain green merely because the last event said
  `active`.
- `/api/national-arena/*`: epoch-bound diagnostic sessions with explicit
  non-authority metadata.
- `/api/prompts`: source-controlled, read-only prompt contracts.

When a route returns 404, 409, 410, or 503 because identity or authority moved,
the page should clear the affected projection and explain the rejection. It
must not retain stale data, synthesize a fallback, or retry a retired mutation.
