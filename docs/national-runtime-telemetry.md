# National TCP Policy Runtime Telemetry

The national TCP path records two independent timing sources for every bot
action:

- `server_action_latency`: elapsed time from the local server requesting an
  action until it receives a wire action. This includes scheduling, transport,
  strategy work, and any wire-layer delay.
- `bot_decision_latency`: elapsed policy time reported by the generated
  native entrypoint between `DECIDE start` and `DECIDE done`.

The clocks must remain separate. Their difference is useful for diagnosing
transport and harness overhead; combining them would hide whether a timeout
was caused by policy code or communication.

## Data Flow

1. `sever/engine/game.py` adds `decision_wait_sec` and
   `timeout_budget_sec` to every action event.
2. Generated `national_bot.py` entrypoints are launched with `--log` during
   local native acceptance when they support that argument.
3. `web/core/national_runtime_telemetry.py` deterministically parses bounded
   timing, stage, hand-bucket, send-count, exception, trusted iterator-step,
   worker CPU, exhaustion, and termination fields.
4. `web/core/national_native.py` puts the compact summary into the national
   acceptance report and quality scorecard.
5. Temporary logs from local strength runs are deleted after parsing. A Web
   Arena session separately retains its own wire/decision logs with permanent
   `diagnostic_only` authority, while the official EXE evidence bundle retains
   the formal compliance logs. Neither store is rating evidence.

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

Local native strength evaluation, including precommit, deliberately uses a
2.0 second per-action hard deadline, 1.8 second refinement budget, and 420
second whole-match timeout. This is the local strength envelope, not a change
to the official 60 second action limit.
A strategy should finish cheap/obvious decisions early and reserve long formal
refinement for uncertainty-sensitive high-EV decisions; spending the maximum
on every action is a throughput defect.

## Decision Context And Anytime Contract

Generated runtime v10 publishes one schema-versioned authoritative
`decision_context`:

- `decision_context.hand`, `.betting`, `.history`, and `.line` are built by the
  socket owner from the current raw event stream. They carry the repaired
  pot/stacks/street contributions, stable
  preflop aggressor and spot, semantic checked-through street summaries,
  `can_donk`, `can_delayed_probe`, SPR, pot odds, and legal intent bounds.
  Candidate modules do not reconstruct these facts from another request model.
  `hand.match_control` publishes the exact current exposure plus future
  alternating blind-loss bound and marks `fold_locks_win` only for a strict
  match lead. `betting.call_closes_allin_runout` is the authoritative closure
  signal used to suppress inapplicable future-street realization discounts.
  Both consumers fail neutral on absent, malformed, or contradictory fields.
- `decision_context.opponent` is connection-level bounded memory. It records relayed and
  boundary-inferred terminal responses, per-street raise/all-in responses,
  real river overcall samples, and a prior-smoothed `showdown_range`. The latter
  is explicitly labelled `reached_showdown_only`. Its prior is pinned to the
  1,326 uniformly possible hole-card combinations, while showdown reach rate
  discounts its confidence-derived, capped adaptation weight. This prevents a
  selected showdown sample from silently becoming an unconditional range.

The runtime capability probe keeps four counterfactual dimensions separate:
proactive action style, terminal responses, showdown-only range evidence, and
donk/delayed-probe line semantics. One dimension cannot make another pass. The
line cases use legal national transcripts, including the in-position player's
zero-chip `call` that closes a postflop check-through.

Refinement telemetry distinguishes candidate-reported metadata from trusted
runtime evidence. `reported_sample_count`, confidence, and completion are
diagnostic only. The system worker owns iterator `next()` counts, process CPU,
elapsed time, true `StopIteration`, termination reason, and every validated
typed intent in the trajectory. A valid policy publishes a legal baseline within
250 ms, performs at least eight system-observed refinement steps or exhausts a
finite batch of that size, scales under a longer budget (unless both tiers
prove the same finite exhaustion), and changes the validated baseline in at
least one deterministic scenario. A single `complete=True` yield cannot pass.
For action-profile, terminal-response, showdown-range, donk, and delayed-probe
influence, at least one completed counterfactual tier must change the final
validated typed intent. A transient intermediate yield is diagnostic evidence,
not proof that live behavior changed.

The policy worker is non-daemon so a candidate may use a bounded fixed CPU
pool. On POSIX it owns a process group; on Windows the owner uses tree-aware
termination. The socket owner kills the whole compute tree at the action
deadline and marks termination successful only after the worker has exited and
tree-aware termination was confirmed, so multi-core refinement cannot escape
timeout cleanup or accumulate across decisions.

These local runtime probes are not formal completion or strength evidence.
Formal profile `official-full-v5` and the content-pinned 2026-07-11 official
oracles remain authoritative:
`docs/official-raise-boundary-oracle-2026-07-11.md` proves exact consecutive 2x
is legal, and `docs/official-terminal-settlement-oracle-2026-07-11.md` proves a
natural hand-70 finish may have 70 starts but only 69 paired TCP settlements.
That terminal form passes only through the strict, hash-bound THP cross-proof
for `STATE:0..69`. Official/THP outcomes retain zero weight in ratings, H2H,
source selection, precommit strength, and prompt evidence.
