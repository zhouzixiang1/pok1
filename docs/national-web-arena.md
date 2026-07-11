# National Web Arena

## Purpose And Authority

The National Web Arena is the repository's local presentation and diagnostic
surface for national TCP matches. It supports external engines and managed
active-pool bots, persists semantic and wire evidence, renders the match in the
main React application, and exports THP records.

Arena completion is **not** official compliance evidence:

- `result_authority` is always `diagnostic_only`.
- `official_exe_certification` is always `false`.
- `compliance_oracle` is always `official_windows_exe`; these values are
  enforced when persisted sessions are loaded, not merely UI labels.
- Arena results never update Glicko, source selection, active-pool membership,
  completion tags, or the official certificate store.
- New evolved bots still require the signed, content-bound Windows EXE full
  suite: 5 self-play rounds plus 3 opponent rounds, 70 hands per round.
- The UI labels a finished Arena match as `本地完成`, never `国赛通过`.

Arena findings are repair hints only. They cannot issue or replace an EXE
certificate, even when the local match completed without a visible error.
Every new evolution bot must still pass the signed full official-EXE suite: five
70-hand self-play rounds and three 70-hand rounds against an eligible opponent.

## Architecture

The Arena reuses the same protocol and rules implementation as native
evaluation:

```text
external bot / managed national_bot.py
                |
        national_transport.py
                |
      national_game_runtime.py
                |
        sever.engine.GameEngine
                |
   semantic events + THP + wire journal
                |
 NationalArenaManager -> FastAPI/SSE -> React /arena
```

- `web/core/national_transport.py` owns newline-free bot-to-server framing. It
  preserves illegal whitespace, waits for an idle boundary before committing a
  variable-length raise, handles split UTF-8 names, rejects oversized buffers,
  and never translates illegal `bet` into `raise`.
- `web/core/national_game_runtime.py` owns the reusable TCP-backed game engine.
  It imports `sever` as a normal package; there is no `sys.modules` replacement.
- `web/core/national_arena/manager.py` owns the single-active-session state
  machine, listener, managed process groups, cleanup, and SSE notifications.
- `web/core/national_arena/storage.py` owns locked metadata, semantic events,
  wire journals, bot logs, and THP artifacts.
- `web/core/runtime_capacity.py` provides 12 host-shared cross-process match
  slots under `/tmp/pok-runtime-capacity-<uid>` by default. The path is
  independent of a checkout and can be overridden with
  `POK_RUNTIME_CAPACITY_ROOT`. A managed Arena holds two slots; every native TCP
  match acquires one in the shared runner, while legacy daemon matches retain a
  single outer lease.
- `web/core/official_platform_resource.py` owns the cross-process official-port
  lease. An Arena on port 10001 and the Windows EXE can never run concurrently.
  A queued formal certification has priority over a new Arena; use another
  Arena port when certification work is pending.

The first release intentionally permits one active Arena session. A second
start returns a conflict instead of sharing a port or replacing live state.

## Session Lifecycle

```text
created -> starting -> waiting_for_players -> ready -> running
                                                    -> finished
                                                    -> failed
                                                    -> stopped
```

`starting` is claimed under the manager lock before a task or listener is
created. On Web restart, an interrupted session becomes `failed`. Managed
process records bind PID, PGID, Linux process start ticks, and
`POK_ARENA_SESSION_ID`; recovery kills a process group only when every identity
field still matches. Graceful or failed matches export a full or partial
GB2312 THP record when at least one hand exists.

## Modes

### Managed Bots

Only bots from the read-only active catalog can launch. They must already be
completed, tagged, native-contract compliant, epoch-active, and official-pool
eligible through a full EXE certificate or a content-bound transitional grant.
The process starts its own `national_bot.py`; no adapter or wrapper is allowed.

Managed processes inherit a small environment allowlist. API/model keys and
unrelated operator secrets are not passed to bot code. Stdout, stderr, decision
logs, process identity, and the complete TCP wire stream are session artifacts.

### External TCP

The requested IP/port is bound and the first two clients become top and bottom
players. A third client is rejected. Team names and actions need no newlines.
The server-to-bot direction remains newline-compatible with native bots.

## Storage

Runtime data is gitignored under:

```text
web/core/results/national_arena/<session_id>/
├── session.json
├── events.jsonl
├── .lock
└── artifacts/
    ├── wire.jsonl
    ├── match.thp.txt or partial.thp.txt
    ├── top.stdout.log / top.stderr.log / top.decision.log
    └── bottom.stdout.log / bottom.stderr.log / bottom.decision.log
```

Semantic events and wire records have independent monotonic IDs. Wire records
use a bounded queue and ordered batch writer so disk latency cannot delay a
poker action. Saturation sets `wire_log_complete=false`; it is not hidden.

## API And UI

The React page is `/arena`. The API prefix is `/api/national-arena`:

- `GET /health`
- `GET /bots`
- `GET|POST /sessions`
- `GET /sessions/{id}`
- `POST /sessions/{id}/start|stop`
- `GET /sessions/{id}/events` and `/events/history`
- `GET /sessions/{id}/wire/history`
- `GET /sessions/{id}/thp`
- `GET /sessions/{id}/artifacts/{key}`

Mutation endpoints require same-origin browser access. Remote automation must
set `POK_ARENA_CONTROL_TOKEN` and send `X-Arena-Token`.

An SSE first connection receives an absolute snapshot. A reconnect with
`Last-Event-ID` replays every missing semantic event. Terminal streams close
after all persisted events are sent.

## CLI

The CLI talks to the same FastAPI manager:

```bash
python scripts/national_arena.py serve --view-only
python scripts/national_arena.py run --mode external --host 0.0.0.0 --port 10001 --wait
python scripts/national_arena.py run --mode managed \
  --top-bot national_v141 --bottom-bot national_v142 --hands 70 --wait
python scripts/national_arena.py status <session_id>
python scripts/national_arena.py events <session_id>
python scripts/national_arena.py wire <session_id>
python scripts/national_arena.py stop <session_id>
python scripts/national_arena.py export-thp <session_id> --output /tmp/match.txt
```

`pokctl.sh stop` runs content-bound orphan cleanup after the Web process exits.
It does not use process-name matching.

## Verification

Automated tests cover stream framing, storage/owner locks, atomic starts,
capacity leases, environment isolation, API authority labels, control security,
SSE snapshot/replay, CLI behavior, game observer events, and standalone `sever`
compatibility. Release verification must additionally run:

1. A managed 70-hand native TCP match with no illegal action, timeout, process
   failure, adapter, or JSON stdout.
2. An external-client match through the fixed listener.
3. Frontend TypeScript build and lint.
4. The full Web regression suite.
5. The separate official Windows EXE full certification gate. Arena success
   does not satisfy item 5.
