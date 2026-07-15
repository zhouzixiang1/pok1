# National TCP Policy Epoch

`national_tcp_policy_v1` is the active evolution epoch.

## Reset boundary

The previous `national_native_v1` bots used a national TCP wrapper around a
parallel JSON-derived `main.py` / `state.py` / integer-action strategy ABI. That
design could produce protocol-legal wire actions while carrying incorrect pot,
stack, street-closure, and raise-to semantics inside policy code. It is retired.

All tracked bots from that epoch are stored under:

```text
archive/evolution_epochs/national_native_v1/bots/
```

The old local subprocess engine, adapter, RL stack, tests, tools, prompts, and
runtime analyses are likewise under `archive/`. Active code must not import or
execute them.

## Identity rules

- The first new bot version is derived from the remote completion-tag
  high-water mark. With `national-bot-v142` as the last retired completion tag,
  the first strict-policy label is `national_v143`.
- This preserves monotonic labels only. v143 does not inherit v142 source bytes,
  ratings, H2H rows, experience, capability claims, or certification roles.
- Directories and checkpoints have no version authority. When the completion
  high-water is still v142, an untagged higher directory such as an abandoned
  old-wrapper `national_v155` is archived as `stale_unpublished_high_version_candidate`
  with its runtime state; it neither blocks the reset nor changes the v143 target.
- Active discovery requires the `national_tcp_policy_v1` artifact contract,
  current completion metadata, annotated `national-bot-v<N>` tag, and any
  role-specific certificate.
- A copied or renamed archived bot is not an active bot.

The same boundary applies to durable pipeline state. Every active checkpoint
has `checkpoint_schema_version`, `evaluation_epoch=national_tcp_policy_v1`, and
a digest-bound `epoch_binding`. The only legal origin modes are the fresh v143
bootstrap or a v144+ generation whose complete parent set resolved as
published strict-policy bots when the checkpoint was selected. Fresh v143 also
binds both its bootstrap receipt and the executed one-time
`policy_epoch_reset_receipt.json`; restart diagnostics compare that reset
receipt digest with the live file before resuming.

A pre-policy or field-missing checkpoint is never upgraded in place, even when
its stage name (for example `direction_audited`) still exists. Routing exposes
no Master/Worker/gate tool for such state. The operator must preserve/archive
the checkpoint and unfinished candidate and use the central epoch reset at a
safe point. This prevents an abandoned `v155`/source-`v142` migration checkpoint
from entering the fresh v143 pipeline after a process restart.

Current-epoch abandonment is likewise typed authority rather than deletion by
name. A schema-2 transaction claim has exact keys and binds the checkpoint
digest/workflow/revision/stage, reason, bounded no-link candidate manifest,
fixed content-addressed quarantine contract, current abandon-ledger prefix and
six-field successor receipt, plus the exact Git HEAD, clean tracked tree,
untracked candidate and absent completion/high-water refs. The transaction and
live claim are durable before mutation, and the complete live state is reopened
again before the irreversible ledger append. Source/quarantine must remain an
exclusive-or of the claimed preimage through rename and checkpoint CAS.

The presence of any live reconciliation claim is a launch barrier even when the
claim is malformed: epoch initialization is false and the active-bot projection
is empty until operator inspection or exact recovery completes. A terminal
schema-2 receipt is historical proof, not a permanent chain-head lease; after
the live claim is cleared it remains valid across later legitimate Git commits
and ledger successors by revalidating its original prefix, exact row and final
receipt. It never licenses a same-version source directory that reappears after
checkpoint clear.

`web/core/epoch_authority.py` is the single read-only projection used by
scheduling, operator status, and the dashboard. Before the reset validates it
reports version-authority high-water v142, next target v143, zero strict
generations, no active checkpoint, and any v155-style directory only as
unpublished debris. It never lets a retired `abandoned_versions.jsonl` reserve
a strict-policy version.

## Candidate artifact

The system prepares a fresh artifact containing:

- system-owned `national_bot.py`;
- system-owned compact `precompute.py`;
- candidate-owned `policy.py`;
- `national_runtime_manifest.json` and `policy_epoch_receipt.json`.

Those are the exact five artifact files. Candidate helper modules and
candidate-owned data/model assets are rejected rather than dynamically loaded.

The two JSON files are system-derived outputs, not an expanded Worker write
scope. Preparation first rebinds a copied strict parent to the new version and
frozen scheduler lineage. A Worker or crossover model is then audited against
the complete pre-edit artifact and may have changed only `policy.py`. Only
after that audit succeeds does the host atomically regenerate both canonical
JSON documents. Quality compares the final artifact with the frozen prepared
manifest and exempts that exact two-file consequence only when its refresh
receipt matches the durable Worker effect, final artifact hash, target version,
and lineage. A partial/equivalent-but-reformatted identity rewrite, or any
extra helper, directory, or binary asset, remains an undeclared change and
fails closed.

Candidate code receives a schema-versioned authoritative `decision_context` and
returns only typed `pass`, `fold`, `allin`, or `raise` intents. It never parses
the TCP stream, reconstructs a request list, emits an integer action, or chooses
between wire `call` and `check`.

## Evidence reset

The new epoch starts with an empty frozen prompt-evidence envelope. The
following retired data is `legacy-untrusted` and is never injected:

- old ratings, H2H matrices, replay summaries, and selection scores;
- experience pools, exhausted-direction histories, spotlights, guardian notes,
  Worker failure summaries, archived Critic assessments, live
  `eval_rounds.jsonl` summaries, and pre-v143 completion-tag prose;
- neural/RL experiment reports and generated analysis sidecars;
- local-engine, adapter, Arena, and official chip outcomes.

The one-time reset also archives `web/logs/` together with
`web/core/results/`, root `results/`, and `ladder_results/`. It then writes a
digest-bound `web/logs/policy_epoch_log_identity.json` marker tied to the live
reset receipt. The Web API lists orchestrator conversations only while that
marker validates, and structured events must carry the exact current epoch and
reset-receipt digest. Thus a restart cannot make a retired conversation,
`battle_exp` event, or v155 checkpoint look current.

New strength and diagnostic evidence is created only by complete 70-hand raw
national-TCP matches under the current evaluator identity. Master receives the
frozen evaluation snapshot and its identity-bound native tracker statistics
directly. The same snapshot stores deterministic replay-spotlight text,
citations, and source replay hashes; later planning/retry stages never reopen
the live replay directory or a global citation manifest. There is no active
free-standing lesson store or experience API. A
future cross-generation lesson facility must first prove a complete
producer→frozen-snapshot→consumer chain; an unwired storage prototype has no
active authority.

Dashboard inventory and strength evidence are deliberately separate. A newly
signed/tagged strict bot appears immediately from publication authority; until
the rating daemon publishes an immutable cycle for that exact pool, its rating
fields are explicitly `awaiting_first_rating_cycle` rather than hiding the bot
or borrowing a stale cycle.

Worker inputs are only the checkpoint-owned compiled task, current repair
feedback when that feedback belongs to the same checkpoint, the immutable
candidate snapshot, and system-owned contracts. The durable Worker envelope
has no generic prompt-context field. Combined analysis consumes the frozen
evaluation bundle; Direction Audit may read recent commit prose only after the
corresponding `national_v143+` directory passes the strict published identity
resolver. Orchestrator status never reopens failure logs or the independent
eval-round stream to manufacture planning advice. Critic remains advisory.

The old tracked Markdown pool, background experience bridge and prompts,
consolidator, attribution sidecar, keyword-based hard gate, cross-generation
direction JSONL, guardian-note injection, and `/experience` UI/API are archived
facilities, not empty compatibility stores. Their files may remain in an
archive or a stale runtime directory, but no active prompt, gate, source
selector, Worker context, daemon thread, or Web route opens them. The
content-bound Cycle Archivist writes archive annotations only and cannot create
strategy lessons.

Fresh v143 also requires the schema-2 digest-bound execute receipt written from
the stopped `.evolution_pok` checkout by
`scripts/reset_national_tcp_policy_epoch.py --execute --acknowledge-runtime-checkout`.
The outer operator checkout may run the command without `--execute` to inspect
a plan, but it cannot mint the runtime authority; its ignored files are not
copied into the autonomous checkout. The mutating path first writes a durable
no-clobber claim, cross-binds the final live/archive receipts, and refuses a
second run even before v143 receives a tag. An interrupted claim requires
manual inspection/recovery and can never be replaced automatically. The live
receipt digest is embedded in the fresh-bootstrap checkpoint and revalidated
against the archived claim/receipt on resume and every bootstrap gate. A
dry-run receipt, a missing/edited archive, or a checkpoint created before this
binding cannot be upgraded in place and must be archived.

## Bootstrap and certification

The first strict candidate is fresh system materialization, not crossover or
migration of a retired strategy. While the strict pool is empty, only v143 may
use the content-bound `first_strict_control_v1` as the opponent for the three
formal opponent rounds. That control is materialized from the current
system-owned raw-TCP runtime and a checked-in typed policy; it is not an
archived bot, is not a normal official opponent, and has zero rating or strength
weight. Its one-time authorization and consumption are sealed into the signed
certificate ledger. The retired v141 signed-ledger root is historical evidence
only and must never be resolved or executed. After v143 publishes with a valid
full certificate, all later bots use the ordinary 5+3x70 policy against an
eligible strict-policy opponent.

The prepare gate validates the exact empty-pool fresh-bootstrap receipt before
applying this one exemption: archived high-water `source_v=142` needs no active
strict tag or executable tree because none of its bytes are read or inherited.
The exemption cannot be carried forward. A normal parent, including the sole
parent named by a singleton-bootstrap receipt, must still be present in the
current strict active pool and therefore revalidate its completion tag, signed
full-v5 certificate, lifecycle, ABI, and `parent_source` role at prepare time.

## No compatibility layer

There is no dual-mode candidate loader. A missing `policy.py`, an old entry
file, integer/string action, stale epoch receipt, archived evidence reference,
or mismatched runtime digest fails closed. Repair changes the candidate or
system contract; it does not reactivate an adapter or translate the retired ABI.
