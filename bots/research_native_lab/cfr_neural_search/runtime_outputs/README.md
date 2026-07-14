# Runtime workspaces

This directory is the fail-closed workspace root for M4 selection and training.
Checkpoints, atomic heartbeats, cancellation sentinels, temporary artifacts,
selector traces, and authoritative per-event files under `events/` are
intentionally ignored and must not be committed. `events.jsonl` is only an
atomically regenerated view of that no-clobber SHA chain; it is never the crash
recovery authority.

An invalid run receives a no-clobber `INVALIDATED.json`. Every path entry with
that name is fail-closed, including a directory or dangling symlink. The valid
marker is also copied into the source-bound permanent registry under
`../manifests/invalidated_selector_runs/`, so deleting this ignored runtime
marker never restores selection, training, freeze, or publication authority.

`.m4-publication-invalidation.lock` is the ignored shared authority lease used
to serialize invalidation with publication/render/write/verify. It carries no
strategy or recovery evidence and must never be copied into an artifact.

The reviewed compact blueprint and deterministic evidence are published under
`../artifacts/m4/` only after their hashes are bound by the M4 gate manifest.
