# National Native Runtime Telemetry

The national TCP path records two independent timing sources for every bot
action:

- `server_action_latency`: elapsed time from the local server requesting an
  action until it receives a wire action. This includes scheduling, transport,
  strategy work, and any wire-layer delay.
- `bot_decision_latency`: elapsed strategy time reported by the generated
  native entrypoint between `DECIDE start` and `DECIDE done`.

The clocks must remain separate. Their difference is useful for diagnosing
transport and harness overhead; combining them would hide whether a timeout
was caused by strategy code or communication.

## Data Flow

1. `sever/engine/game.py` adds `decision_wait_sec` and
   `timeout_budget_sec` to every action event.
2. Generated `national_bot.py` entrypoints are launched with `--log` during
   local native acceptance when they support that argument.
3. `web/core/national_runtime_telemetry.py` deterministically parses bounded
   timing, stage, hand-bucket, send-count, and exception fields.
4. `web/core/national_native.py` puts the compact summary into the national
   acceptance report and quality scorecard.
5. Temporary local bot logs are deleted after parsing. Full raw communication
   and decision logs are retained only by the official EXE evidence bundle,
   where they are needed for protocol forensics.

The local path disables the official action delay, so local strength runs do
not spend 0.30 seconds per action. The official EXE path keeps that delay and
records it separately as `official_action_delay`.

## Interpretation

The official decision budget is 60 seconds. This is a hard ceiling, not a
target runtime. Runtime summaries expose maximum budget utilization and stage
and ten-hand buckets so later evolution work can distinguish stable compute
from late-match growth.

Per-match percentiles use exact nearest-linear interpolation. A summary merged
from several matches labels its P95 method as
`conservative_max_of_group_p95`; it is deliberately not presented as an exact
global percentile when raw samples have already been discarded.

Runtime telemetry is evidence, not a strength score. It must not change Glicko
or H2H ratings. Future deadline, precomputation, and persistent opponent-model
work should first consume these fields in shadow mode, then add explicit gates
only after stable baselines exist.
