# Precommit evaluation products and the fresh-seed repair round

Status: implemented and tested 2026-10-22; deploy/restart is an operator
decision. Native precommit remains the sole publication authority.

## Why this exists

The long output drought (v188 2026-08-16 -> v566 2026-10-17: 377 abandons,
one publication) was not an LLM failure. In the final 24h the pipeline made
136 LLM calls with 0 failures, p50 125s/p95 261s and zero semaphore waits;
the saturator consumed 95.5% of tokens but did not crowd out pipeline permits.
The real losses were native precommit verdicts and an unbounded post-publication
Archivist retry loop.

v589 is the concrete example: quality/review/critic passed, the frozen
48-match plan ran for ~94 minutes (v585 12, v173 24, v185 12), and the result
was 20W-28L: parent 4-8, pool weakest 12-12, aggregate loss margin 8, net
chips -1889.5, CI [-5962, +2501]. It was genuinely weaker, so publishing it
would have violated the evidence contract. With
`POK_MAX_PRECOMMIT_REWORK_ROUNDS=0` the failure went straight to the circuit
breaker and the only durable trace was the reason token.

## Evaluation report

Every precommit event now writes `RESULTS_DIR/precommit_reports/v<N>.json`:

- version/source/stage/candidate/run identity;
- aggregate W/L/draws/samples/score/net chips/CI;
- per-opponent W/L/draws/samples/score/margin;
- blockers, failure class, frozen plan shape (opponents and sample depths);
- `precommit_passed` plus a conservative `publishable` flag;
- a self-digest (`report_digest`).

The report is seed-free: it is built from a whitelist of scorecard fields, not
from the frozen plan. The append is best-effort inside
`candidate_store.append_candidate_event`; the operator CLI backfills from the
append-only ledger:

```bash
python scripts/export_precommit_reports.py --version 589
```

For a failed candidate the exporter also writes
`RESULTS_DIR/diagnostics/v<N>/manifest.json`, pointing at the exact five-file
candidate preserved by the canonical abandon transaction. It is marked
`non_publishable`; the code is never copied into a live bot directory or run
by this exporter. v589's report and manifest were generated from the real
ledger on 2026-10-22.

## Automatic diagnostic manifests

The report is written while precommit fails, but the quarantine directory only
appears later when the canonical abandon transaction runs. The web lifespan
therefore starts a low-frequency scanner
(`web/core/diagnostic_manifest.py::run_diagnostic_manifest_loop`) beside the
LLM saturator and disk janitor, and cancels it on shutdown:

- `POK_DIAGNOSTIC_MANIFEST_ENABLED` (default `1`);
- `POK_DIAGNOSTIC_MANIFEST_INTERVAL_SEC` (default `120`, clamped to 60-300).

Each cycle lists only `RESULTS_DIR/precommit_reports/v<N>.json` (the bounded,
one-file-per-version projection of precommit ledger events) and makes one
receipt walk over `policy_epoch_abandon_transactions/`. For every report whose
`precommit_passed` is exactly `False`, it writes the manifest once a
*completed* abandon transaction for that version postdates the report (terminal
`receipt.json` proves `candidate_state=quarantine` and `checkpoint_cleared`;
its mtime is the abandon time). An older quarantine left by a previous attempt
at a re-allocated version is never bound, so the manifest always waits for the
current attempt's abandon. It never rescans the multi-hundred-MB
`candidates.jsonl`, never copies/imports/executes candidate code, never writes
reports/checkpoints/abandon transactions, and never grants publication
authority. The operation is idempotent: a manifest already bound to the same
`report_digest` and quarantine snapshot is left byte-identical; a newer failed
report (e.g. a repair round) rewrites only the manifest digest. A later passing
report for the same version suppresses manifest creation. Failures are
logged and never block the lifespan or the pipeline.

Tests: `web/tests/test_diagnostic_manifest.py` covers report-without-quarantine
(no manifest), quarantine arrival (manifest written, report bytes untouched),
idempotency, digest refresh, passing-report suppression, and malformed/symlink
report rejection.

## One fresh-seed repair round

`POK_MAX_PRECOMMIT_REWORK_ROUNDS=1` now means:

1. The first precommit runs the checkpoint-frozen plan (seed round 1).
2. After a Workers repair round (`precommit_rework_count >= 1`), the next
   precommit rebuilds the plan with a **fresh independent one-time nonce**
   while carrying over the opponent set, per-opponent sample depths and every
   gate threshold. The rotated plan is persisted with an exact checkpoint CAS
   fence and a `pipeline.precommit_seed_rotated` event.
3. Both attempts' complete scorecards are stored in the checkpoint
   `precommit_eval_attempts` history and the candidate ledger.
4. A third independent sample is refused (`precommit_repair_round_exhausted`).
   There is no retry-until-pass. Infrastructure retries keep the same plan.

The unchanged native precommit gate must pass for publication. A pass on the
repair round is the same gate, same alpha, with a different deck schedule.

## Sample-power calibration

`POK_PRECOMMIT_PARENT_N_GAMES` can freeze a deeper parent leg (6-48) at plan
time; unset inherits the plan-wide count. It is deliberately left off:
the parent gate is a strict-majority screen, so its false-admission rate is
~0.39 at n=12 and ~0.42 at n=24; the benefit is mainly lower score variance
(SD 0.144 -> 0.102) and a modest power gain for a true 0.55 edge (~0.56 ->
0.61), at ~12 extra 70-hand matches (~24 minutes). The fresh-seed repair
round is the chosen output lever; increase this dial only after an operator
accepts the cost.

## Archivist retry bound

A persistent `POST_PUBLICATION_ARCHIVIST_PENDING` used to re-enter the
provider stream indefinitely (v585: 214 retries over 4.1h). The root cause was
fixed by b50e19c8; the route is now bounded as well: 30s exponential backoff,
600s cap, at most 8 attempts per handoff identity, then a typed
`pipeline.post_publication_archivist_blocked` operator boundary. The retry
path never completes or clears the handoff journal, so the launch barrier
remains in force until a real Archivist completion.

## Saturator consumption (operator directive 2026-10-21)

The earlier 8 -> 4 conservation cap is removed. Per the operator directive,
LLM consumption must not be clamped and may exhaust the provider quota early:
`POK_LLM_SATURATOR_MAX_INFLIGHT=16` may use the entire global permit pool
(`POK_GLOBAL_LLM_CONCURRENCY=16`) whenever pipeline roles are idle. The
provider's 1302/1308 response and the AIMD remain the dynamic bound;
`POK_LLM_QUOTA_WINDOW_BUDGET_TOKENS` stays 0 (no pacer).
`POK_LLM_SATURATOR_MIN_FREE_MB=300` is only a last-resort cgroup/OOM guard,
not a throughput cap. Raise `GLOBAL` beyond 16 only with memory monitoring:
the cgroup MemoryMax is 3.4G and each stream accounts ~130MB.
`POK_LLM_SATURATOR_MAX_READ_CALLS=40` is unchanged (per-stream read budget,
not concurrency).

## Verification

```bash
cd web
TMPDIR=/tmp python -m pytest \
  tests/test_precommit_report_export.py \
  tests/test_diagnostic_manifest.py \
  tests/test_precommit_independent_seed_repair_round_20261022.py \
  tests/test_precommit_attempt_checkpoint.py \
  tests/test_precommit_eval_contract.py \
  tests/test_precommit_rework_rounds_default_20261016.py \
  tests/test_post_publication_archivist_retry_20261021.py \
  tests/test_handoff_head_advance_convergence_20261017.py -q \
  --basetemp=/tmp/pytest-precommit-suite
```

The observed real-data product is:

- `web/core/results/precommit_reports/v589.json`
- `web/core/results/diagnostics/v589/manifest.json`
