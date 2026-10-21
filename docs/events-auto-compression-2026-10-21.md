# Events auto-compression (gzip + digest-proven re-head)

Status: implemented on `tencent-cloud-runtime` (2026-10-21); the running
service must be restarted to load the new janitor entry and the committed env
flag.

## Problem

`web/core/results/events.jsonl` is append-only and grows with every role,
scheduler, rating, replay, and disk-governance event. On 2026-10-21 it grew
from ~419 MB (04:57 rotation watermark) to ~1.02 GB by 15:57, i.e. roughly
55 MB/hour. Publication archive rotation is intentionally copy-only, so it
does not reclaim live bytes; the janitor's ordinary orphan reaping frees only
~0.1-0.3 MB per five-minute cycle. With a 40 GB root disk at ~93% used the
runtime needed a bounded, crash-safe way to re-head the live file.

## Architecture

The janitor step is
`disk_hygiene._events_auto_rotation_step` (`web/core/disk_hygiene.py:1290`).
It is gated by all of:

1. `POK_EVENTS_AUTO_ROTATION_ENABLED=1` (default off in code);
2. the janitor safe window is open (no pending post-publication handoff, no
   live ahead draft, and the checkpoint stage is one of `selected`,
   `prepared`, `crossover_running`, `direction_audited`);
3. the disk tier is `critical` (`free < POK_DISK_MIN_FREE_GB`, default 4 GiB).

When all gates pass it calls the frozen A-track entry
`evolution_infra_archive_rotation.autonomous_rotate_append_only_sources(
*, actor, sources=None, max_batch_bytes=None)`. The entry supports the single
admitted source `events.jsonl`; it maps each applied rebase to a janitor
receipt with `source_preserved_append_only=False` and `reheaded=True`.

The actual byte movement belongs to
`evolution_infra_rotation_rebase.try_rebase_rotation_source`
(`web/core/evolution_infra_rotation_rebase.py:640`):

1. recover any `prepared` rebase first (independent of every env gate);
2. under a shared source lock, pin the inode, load the archive watermark, and
   choose `[watermark_end .. min(watermark_end + max_span, size)]`, snapping
   forward to a newline;
3. lock-free, stream that range into a gzip batch with both plaintext and gzip
   SHA-256 digests, and stream the kept tail into a same-directory temp;
4. in a millisecond exclusive sidecar section, re-check inode/size/handoff,
   capture concurrent appends, write the `prepared` rebase state, atomically
   replace the live file with the kept tail, and finalize the v3 baseline
   watermark plus the `completed` state.

Crash recovery distinguishes a still-pinned live file (proof by hash over the
dropped prefix; the tail is rebuilt from the current end so post-crash appends
are never lost) from an already re-headed file (proof by kept-prefix hash).
Both branches are idempotent and are also invoked by the publication
rotation entry before it freezes a plan.

After a successful rebase the adapter calls
`apply_archive_batch_retention`, which deletes only aged gzip batches
receipt-first and protects the live watermark pair, a pending handoff pair,
the current rebase state, and its own batch. The live `events.jsonl` is
re-headed, not copied-and-preserved; the dropped prefix remains recoverable
from the digest-bound gzip batch.

## Configuration

| Variable | Value | Meaning |
|---|---|---|
| `POK_EVENTS_AUTO_ROTATION_ENABLED` | `1` | Master switch for the janitor step |
| `POK_EVENTS_REBASE_ENABLED` | unset (`1`) | Rebase library gate; leave default |
| `POK_EVENTS_REBASE_MIN_COLD_BYTES` | `67108864` | Minimum cold backlog per rebase |
| `POK_EVENTS_REBASE_MAX_SPAN_BYTES` | `268435456` | Maximum single dropped span |
| `POK_EVENTS_REBASE_MIN_INTERVAL_SEC` | `1800` | Rebase interval; shortened from the 6h default because growth outpaces it |
| `POK_ROTATION_ARCHIVE_RETENTION_DAYS` | `30` | Aged gzip-batch retention |

The committed values live in `deploy/tencent-cloud/env.runtime`.

## Verification

- Unit/integration:
  `python -m pytest web/tests/test_events_rotation_rebase_v2.py
  web/tests/test_disk_hygiene_governance_steps.py -q`.
  The integration test drives the real janitor step and asserts byte
  conservation, gzip equality with the dropped prefix, intact live tail,
  completed rebase state, and the done event.
- Runtime: after a safe window, look for
  `pipeline.events_auto_rotation_done` and check that `events.jsonl` shrank
  while `archive/events.rebase-*.jsonl.gz` and
  `events.rebase-state.json` exist.
- Chain proof: `evolution_infra_rotation_rebase.verify_rebase_chain(source)`
  consumes both digests of every archive batch.
- Isolated drill (2026-10-21, first 64 MiB of the real live events file):
  one 33.5 MiB cold span was re-headed, the live tail was 33.5 MiB, and the
  gzip batch was 2.2 MiB (6.9% of plaintext). A production pass at the
  256 MiB span cap therefore reclaims roughly 240 MiB net per pass; with the
  30-minute interval it stays ahead of the measured ~55 MiB/hour growth.

## Rollback

Set `POK_EVENTS_AUTO_ROTATION_ENABLED=0` and restart/deploy. A pending
`prepared` rebase is still converged by recovery on the next rotation entry;
disabling the feature never abandons a half-committed re-head. Do not delete
rebase state, the v3 watermark, or gzip batches manually.
