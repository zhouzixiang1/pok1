# Audited M4 outputs

Only the compact frozen blueprint and its content-bound evidence belong here:

- `blueprint.rbbp`
- `training_scale_selection.json`
- `local_native_evidence.json`
- `selector_events/` (complete authoritative no-clobber event-file tree)
- `selector_events.jsonl` (atomic derived view of the authoritative event chain)
- `selector_heartbeat.json` (completed and exactly bound to the chain tip)

These generated files are excluded explicitly from the training source
snapshot to avoid a self-reference cycle. The current M4 manifest binds every
byte, while the embedded formal run contract binds the complete non-generated
Route-B and Common source snapshot used for training.

Publication compares the JSONL canonical bytes to `selector_events/`, binds the
full event-file manifest/tree digest and durable tip, and records the complete
source-controlled invalid-run registry snapshot. Runtime JSONL or heartbeat
alone can never authorize publication.

Replacement is transactional with respect to Python-visible failures: the
publisher first takes a stable byte/tree backup under the shared publication /
invalidation lease. Any failure restores every prior scalar, removes newly
introduced event files, restores the prior event bytes, restores the manifest
last, and verifies the complete rollback.
