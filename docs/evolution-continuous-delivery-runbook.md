# Strict National TCP Continuous Delivery Runbook

## Purpose

Operate `national_tcp_policy_v1` from infrastructure change through safe
runtime recovery, strict publication, immutable rating cycles, and ten
consecutive generations. This runbook does not authorize manual checkpoint,
rating, bot, certificate, or tag edits.

## Checkout ownership

- `/home/zzx/project/pok`: operator/infrastructure checkout; preserve all user
  changes.
- a clean temporary worktree from current `origin/main`: implementation and
  tests.
- `/home/zzx/project/pok/.evolution_pok`: stopped/running autonomous checkout;
  never use it for infrastructure development.
- `/home/zzx/project/pok-arena`: explicitly retained diagnostic worktree.

Infrastructure moves between checkouts only through `origin/main`. Candidate,
checkpoint, result, log, or certificate files are never copied by hand.

## Infrastructure delivery

1. Stop autonomous processes and record PID/heartbeat/checkpoint/HEAD.
2. Fetch tags and verify the intended base against `origin/main`.
3. Develop in a clean `codex/` worktree.
4. Update the alignment matrix and delivery ledger in the same change batch.
5. Run focused tests for every changed producer/consumer/fail-closed path.
6. Run:
   - `python -m pytest sever/tests -q`;
   - `cd web && python -m pytest tests -q`;
   - `cd web/frontend && npm test && npm run lint && npm run build`;
   - enumerate active Python with `rg --files sever web scripts -g '*.py'
     -g '!**/archive/**' -g '!web/core/results/**'`, then pass those exact
     files to `python -m py_compile` (never scan runtime results or archive);
   - `git diff --check`.
7. Run host capability/official doctor checks separately. `probe_infra` is
   neither a bot failure nor a pass.
8. Commit, push, and merge to `origin/main`.
9. At a stopped safe point, fast-forward `.evolution_pok` from `origin/main`.
10. Run canonical checkpoint recovery diagnostics. If the active-stage
    evaluation contract changed, use the governed abandon/re-prepare route;
    never delete checkpoint files.

## Stopped-runtime epoch reconciliation

Version namespace and publication authority are separate. A number advances
the namespace only when both exact annotated refs, `national-bot-v<N>` and
`national-high-water-v<N>`, peel to the same commit. For v143 and later, that
numeric pair is still not an executable publication: the exact five-file
artifact, tag/tree identity and signed eligible certificate must independently
validate. A lone, lightweight or wrong-commit ref is an interrupted or invalid
effect and never allocates a label.

The pre-authority runtime ledger in the stopped `.evolution_pok` checkout has
zero allocation, strength and prompt authority. After this infrastructure is
merged to `origin/main`, the runtime is stopped, and its clean tracked `main`
is fast-forwarded to that commit, first run the read-only plan:

```bash
python scripts/reconcile_national_policy_epoch.py \
  --quarantine-legacy-ledger-and-abandon-checkpoint
```

Review the raw ledger/checkpoint/candidate hashes, target-successor decision,
workflow identity and archive destination. This command does not rerun the
one-time reset. It requires the existing schema-2 execute reset receipt and the
paired v142 refs. Unknown, malformed or partial legacy rows remain raw
`legacy-untrusted` bytes with weight zero. A schema-1 checkpoint may be
upgraded only inside this terminal quarantine transaction and only when its
target is the exact live allocation successor; it is never rewritten as an
active resumable checkpoint.

Only after the dry-run is accepted may the stopped runtime operator execute:

```bash
python scripts/reconcile_national_policy_epoch.py --execute \
  --acknowledge-runtime-checkout \
  --quarantine-legacy-ledger-and-abandon-checkpoint
```

The command publishes a durable reconciliation claim before moving control
state. That claim is a launch barrier, and a second process scan closes the
stop/check race. It quarantines legacy bytes, fences the exact Worker workflow,
and writes at most one schema-2, checkpoint-envelope-bound abandon receipt.
The strict receipt chain binds its previous digest; its head digest and floor
are captured in every new checkpoint allocation envelope.

Normal abandonment uses this crash order under the same publication lock used
by commit:

`Worker fence → transaction claim file+parent fsync → live launch-barrier
claim file+parent fsync → reopen and revalidate the complete claim/Git/
candidate/checkpoint/ledger state → append/replay exact receipt and re-fsync ledger
inode+parent → re-prove candidate manifest/untracked/unpublished predicates →
atomic rename into content-addressed transaction quarantine → source and
quarantine parent fsync → full-identity checkpoint CAS clear → terminal
transaction receipt fsync → live-claim clear+parent fsync`.

The quarantine is retained as zero-authority preimage evidence; it is not an
active bot and is never copied back or injected into a prompt. A `.completed`
sentinel, completion/high-water ref, git-tracked path, symlink, hard link,
special file, manifest drift, ambiguous source/quarantine state, failed Git
predicate, rename/fsync failure or checkpoint CAS conflict leaves the typed
claim in place and fails closed. A retry must re-fsync a prior receipt and any
already-completed directory mutation before proceeding. If an older binary
cleared the checkpoint before deleting its exact schema-1 claim-bound
candidate, the same recovery command can finish only that historical
compatibility window. Inspect first, then run:

```bash
python scripts/reconcile_national_policy_epoch.py \
  --finalize-recorded-abandon-checkpoint

python scripts/reconcile_national_policy_epoch.py --execute \
  --acknowledge-runtime-checkout \
  --finalize-recorded-abandon-checkpoint
```

While a claim is active, recorded-finalize accepts only its exact successor at
the current strict chain head and the exact claim-bound checkpoint/candidate
preimage; candidate, Git, checkpoint or ledger drift is preserved for operator
inspection. Once a schema-2 terminal receipt is durable and the live claim has
been cleared, later legitimate commits and ledger successors do not invalidate
that historical transaction: validation reopens its immutable claim, original
prefix, exact successor row and terminal receipt without treating it as today's
chain head. The narrow schema-1 compatibility path still requires its historical
unique-head window. Do not manually remove the claim, ledger, checkpoint or
candidate.

Preparation follows the same ownership rule. An existing target directory is
never deleted or adopted from its name. Only a `prepared` checkpoint whose
content-bound `prepared_artifact_contract` revalidates the exact live bytes may
return an idempotent resume; every other preimage is preserved and routed to
the canonical abandon/quarantine transaction. `selected→preparing` is an exact
workflow/revision/stage CAS that must be re-read before candidate bytes are
written; `preparing→prepared` is a second exact CAS. If selected/preparing sees
target bytes without the prepared contract, the system-owned prepare route
invokes canonical abandon with
`stale_blueprint_rejection:prepare_preimage_unbound`. It never adopts, removes,
or continues those bytes.

## Provider-stream terminal handoff

The absence of `pipeline_state.json` is not by itself proof that a generation
finished. Each provider stream binds the full checkpoint identity it opened.
If that checkpoint disappears, the stream may report `generation_abandoned`
only when the current authorized owner tool result contains one unambiguous
canonical result, flattened or nested, with all of:

- `workflow_run_id`;
- `abandon_transaction_id`;
- `abandon_receipt_digest`;
- `finalize_receipt_digest`;
- `abandon_checkpoint_identity`;
- `abandoned=true` and `cleared_checkpoint=true`.

Recovery reopens the exact transaction claim/receipt, current Git and abandon
ledger authority, proves the original checkpoint identity, and scans every row
of both the outer Worker and strict-authority journals through each declared
`stream_version`. Sequences must be exactly `1..N`; every schema, JSON payload
and payload digest must validate; the unique `abandoned` event must be last;
and no live effect may remain. A nested duplicate result, stale ledger head,
changed Git/candidate identity, journal gap/drift, live claim/effect, unreadable
checkpoint, or checkpoint read race is a recovery block, never a successful
handoff. A flattened and nested duplicate is ambiguous rather than two
corroborating proofs. The accepted result must bind exactly one pending
route-mutating ToolUse. Typed results use their ToolUse id; SDK metadata may use
an explicit `tool_use_id`, `parent_tool_use_id`, or only when exactly one use is
pending the bounded sole-pending association. Unknown, reused, swapped-owner,
multi-pending, unsettled, EOF-pending, and read-only-owner results all block.

When no stream-owned checkpoint ever existed and no post-publication handoff is
active, the provider must end its stream. It has no MCP tool authorized to
allocate a generation. The outer scheduler alone calls the non-MCP
`prepare_generation`, which freezes the source, target, parents and evidence;
`prepare_next_gen` is used only for the exact validated `selected` first
materialization or `preparing` crash-recovery route. `timed_out` and
`infra_timed_out` remain active leases: neither can be overwritten by a same
identity restart or new generation. Plain timeout may overlay only `selected`,
`preparing`, `prepared`, `crossover_running`, `direction_audited`,
`master_planned`, `workers_done`, or `quality_failed`, then routes only to
schema-2 canonical abandon. Infra timeout may overlay only `critic_checked`;
retry proves the live full-artifact fingerprint, current quality/review/critic
identities, and `quality fingerprint = repair baseline = live bytes`, then
exact-CAS restores `critic_checked`. Any mismatch preserves the overlay without
calling the native backend.

`python web/core/orchestrator.py --one-gen` follows one workflow, not one SDK
stream. It may execute multiple fresh provider streams and deterministic routes
for that workflow, then stops with a distinct outcome: successful publication
and verified cleanup (exit 0), canonical abandon (2), operator action required
(3), recovery blocked (4), generic startup/control failure (5), or accounting
blocked (6). It never allocates the successor after abandon, and a failed or
timed-out post-publication cleanup is recovery blocked rather than success.

The Web control plane consumes the same boundary. Checkpoint revalidation or
`checkpoint_recovery_diagnostics(...).recoverable=false` makes health
`blocked=true` and withholds `route`; an operator action is reported separately.
`POST /api/control/start` repeats that authority check and returns 409 before
resetting the stability observation or acquiring the runtime task. For a clean,
initialized, checkpoint-free state, health publishes a `scheduler_boundary`
with `provider_action=end_stream`, `scheduler_action=prepare_generation`, the
epoch-owned `next_v`, and `source_v=null`. Source/parent selection has not run
yet and must never be inferred from `current_v`. Browser controls validate the
whole boundary, disable Start when blocked, and clear a previously fetched
checkpoint when polling fails. Epoch projection samples checkpoint existence
before and after the read; unreadable, disappearing, `archived`/`abandoned`,
missing-stage, or missing-target bytes never become the clean scheduler
boundary. Browser Start mirrors exactly one of three backend boundaries: a
content-matched active checkpoint route, a content-matched post-publication
route, or the complete clean scheduler projection.

## Durable post-publication handoff

The signed publication transaction and the post-publication Archivist are two
parts of one launch barrier. While still holding the publication lock,
publication must durably create the exact schema-2 handoff record, active
pointer, and archive base snapshot before clearing the publishing checkpoint.
On first creation of either authority directory, the implementation fsyncs the
child directory and its parent and rechecks both inode identities; a successful
file write inside a directory whose entry is not durable is not accepted.

Any provider that observes a pending, running, or blocked handoff must
`end_stream` without another MCP call. Only the outer deterministic recovery
path may invoke or resume `run_archivist`; provider ownership does not follow
from the capability catalog, a completed commit result, or checkpoint absence.
This generation-scheduling fence is distinct from process ownership: a pending
or dead-owner handoff permits one runtime to start deterministic recovery, while
a live foreign owner blocks a second runtime. HTTP exposes only the bounded
`none/current_process/foreign_process` scope. Runtime-owner reservation samples
one launch fence before and after atomic ownership. Every setup exception,
including cancellation-class exceptions, releases only that unattached owner.
Unowned/failed lifespans cannot change a live owner's running/UI state or its
AppState/process-wide LLM shutdown manager; both managers use exact owner CAS.

`run_archivist` executes or resumes these eight ordered journal steps for the
same version, source, workflow, checkpoint digest, publication id, commit,
artifact, certificate, local tags/tree, remote main, and archive base digest:

1. `stability_observation` records this exact publication in the operational
   uninterrupted-delivery state;
2. `reap_signal` publishes the content-bound rating-daemon refresh capability;
3. `priority_eval` publishes the exact new-bot/minimum-games request;
4. `archive_rotation` executes one high-level plan that froze all managed
   append-only log sources before any per-source effect;
5. `log_cleanup` writes immutable strict-generation log tar/manifests without
   deleting or moving the live log tree or touching sibling generation files;
6. `pool_reap` executes a schema-2 frozen active-pool and selection snapshot,
   deterministic target sequence, and required multi-reap count;
7. `cycle_annotation` runs the zero-memory Archivist against only the exact
   committed archive projection and records its semantic digest;
8. `housekeeping` proves the dependent receipts, publication HEAD, clean
   worktree, archives, tombstones and annotation without creating a tracked
   housekeeping commit.

Plans and outputs use exact key sets and content digests. Recomputing an
alternate victim, omitting a required archive, using an empty receipt set to
hide work, or re-signing a forged output is invalid. Before final completion,
the journal reopens the stability observation and its exact publication row,
idempotently republishes the frozen refresh/priority payloads, and re-derives
every non-operational external effect. Rotation retains the live append-only
source and advances only a durable cold-prefix watermark. Pool reaping must
re-prove each local/required-remote tombstone, registry row and absent
`.completed` capability.

The signal writers and rating-daemon consumers use the same stable sidecar
exclusive lock. Read-and-unlink is therefore linearized with atomic
publication, and a crash retry may reissue the same one-shot bytes. Any corrupt
pointer/record, dead or conflicting owner, plan/output mismatch, source drift,
missing receipt, or failed operational/external reproof projects the handoff
as pending/running/blocked and prevents prepare/post-cleanup from advancing.

HTTP status, health and SSE must double-sample epoch/handoff authority and bind
the stability projection to that same sample. The frontend accepts only the
typed epoch, handoff and stability identities; on epoch change, stale sequence,
stream loss or blocked handoff it clears derived state and disables controls.
It never turns a handoff gap into an idle pipeline or recomputes authority from
the bot list.

## Native precommit cancellation boundary

Every `run_precommit_eval` attempt captures one monotonic Event and passes that
same object into the real native 70-hand loop. Timeout/cancellation permanently
sets the captured token; reset rotates only an already-cancelled token and never
clears or detaches live work. The loop checks before each opponent/repeat and
after each complete match or first-strict execution-journal receipt. A late
complete match is not admitted into aggregates or a terminal gate, and no next
sample starts. A new attempt receives a new token, so old detached work remains
cancelled forever. For the first strict control, the initial execution scope is
frozen into checkpoint `audit_context`; infra-timeout or bare cancellation
reuses that exact journal scope and completed match rather than incrementing an
identity that would force duplicate execution.

## First strict publication

For v143, no historical bot, rating, replay, experience, official result, or
v142 source is admissible. Drive the canonical stages:

`prepare → direction audit → Master → Worker → quality → review → advisory
Critic → native 70-hand precommit → official_bootstrap_required`.

The LLM path must park there. An operator first runs the one-time bootstrap
command from `AGENTS.md` with `first_strict_control_v1`. The exact durable job
must then project `ready_to_finalize`, a valid certificate digest, and the same
workflow/candidate/parked-request identity. Only then may the operator run
`finalize-first-strict --acknowledge-publish-first-strict`; neither the LLM nor
HTTP can substitute `commit_bot` for this second boundary. Publication is
complete only when
the five-file artifact, `.completed`, signed certificate, annotated
`national-bot-v143` tag, tag tree, and pushed `main` agree.
Do not prepare v144 until the v143 durable handoff reaches `completed` and its
final operational/external reproof succeeds.

The current checked-in first-strict control is not missing: artifact
  `b37cd019fe6b635a119950adb5f7ecf10ddceeafacfbed6b4c3a0955064516e2`
is valid, unused, and has consumption `0/1`, while official doctor is green.
That proves the dependencies for five 70-hand self-play rounds plus three
70-hand system-control rounds; it does not bypass the stage gate. Until the
exact v143 checkpoint reaches `official_bootstrap_required`, bootstrap remains
locked.

## Second bot and rating readiness

v144 and later candidates use normal `official-full-v5`: five 70-hand
self-play rounds plus three 70-hand rounds against an eligible strict opponent.
After v143 and v144 are active, the rating daemon must publish one immutable,
content-addressed cycle containing admitted complete native matches, Glicko,
H2H, selection rows, cutoffs, and replay citations under one evaluation
identity. Official and Arena outcomes remain excluded from strength.
Every later publication has the same eight-step handoff barrier; a daemon or
orchestrator restart resumes its exact active pointer before scheduling a new
generation.

A fresh Orchestrator provider stream owns two related fences. At a returned
provider-message boundary, exact workflow/revision/stage/version drift proves a
new effect and permits immediate handoff. While a nested MCP tool is still
running and the provider stream is silent, the actionable-stage poller instead
compares workflow, stage, versions, authoritative `next_tool`, and route
`intent`, deliberately ignoring same-route revision metadata. This lets a long
Master audit retry retain `direction_audited` ownership, while a different
authoritative route projected for the same stage still becomes recoverable.
Treating the unchanged startup route as a new effect creates a restart
livelock; treating an in-flight revision update as abandoned cancels healthy
work. The generic no-progress stream ceiling remains active for a genuinely
dead owner. Background stability verification follows the same progress rule:
every thread exit, including cancellation-class exceptions, releases the
single-flight slot and projects a failed refresh before a later retry.

First-strict Master recovery has a second immutable boundary. The first
durable proposal effect freezes the authority-phase checkpoint revision for
all three proposals, both anonymous ballots, and final Master output. A
partial packet may advance checkpoint retry metadata, but a restart must
replay already accepted slots, continue only a missing slot's remaining schema
budget, and keep every later Master receipt on the frozen revision. Before
using that anchor, recovery reopens the append-only journal and proves the
effect input digest, workflow/generation binding, stage, role/purpose, and
per-slot context binding. Multiple phase revisions, a checkpoint revision
rollback, same-slot context drift, or any other binding mismatch is a
control-plane failure, never provider unavailability and never permission to
open a second authority budget.

Proposal Scouts receive only the compact proposal contract and the frozen
semantic facts needed for their slot. Do not embed the complete final-Master
tutorial or final-plan output schema in Scout planning context. Bootstrap scope
permits the exact target artifact only; normal scope permits the exact source,
target, and assigned frozen snapshot. Documentation, archive, Git, operator
records, and all other results paths remain denied. The system supplies a
preferred current chain proven reachable from the policy ABI entrypoints;
Scouts copy that chain, the validator rejects dead-helper chains, and future
edges appear only in the proposed diff. On bootstrap rejection, persist the
generic failure and stable field-level codes in the strict journal. On normal
evolution rejection, content-bind the same deterministic codes into the sole
local repair prompt and its renderer provenance. Both receive the exact prior
defect without expanding read scope or retry count.

Each accepted proposal, ballot, Reviewer, and Critic effect also owns one
content-bound invocation-evidence receipt. It uses the accepted effect's final
provider-visible prompt digest and binds terminal output, provider result/usage,
deterministic role projection, and the exact role log. If the process stops
after acceptance but before this binding, recovery may add the one matching
trailer to an existing non-empty regular provider log, or reuse that exact
trailer read-only. Missing, empty, non-regular, multiply marked, mismatched, or
subsequently changed logs fail closed. Such failures, including those found
while constructing or rendering Reviewer/Critic descriptors before provider
dispatch, use the canonical control-plane abandon transaction with zero LLM
infrastructure retry debt.

The log is not version/role append state. Every strict dispatch has one
immutable path derived from its durable generation and invocation identities:
`RESULTS_DIR/v<N>/logs/strict_invocations/<invocation_id>/<role>_io.txt`.
Foreign roots, wrong versions, flat role logs, and a second marker are rejected.
The generation-log API publishes only a validated opaque identifier
`strict@<invocation_id>@<basename>` and opens every directory component from
the trusted results root with no-follow descriptors; React validates and URL-
encodes that identifier and never derives an on-disk path.

Canonical generation abandonment fences both journals before candidate or
checkpoint cleanup. If the strict child does not exist, abandonment creates a
terminal tombstone; if it exists, all unfinished effects are cancelled. Both a
new provider dispatch and an accepted-effect replay recheck that the child is
running, preventing a stale pre-dispatch descriptor from resurrecting work.
An exhausted strict Master slot is already a terminal control-plane result, not
another provider retry opportunity. Its exact
`system_strict_authority_invalid:` reason is disposable only at
`direction_audited`; the tool layer must complete the canonical abandon
transaction and return `abandoned=true`. The same reason remains forbidden at
Review, Critic, precommit, certification, and publication stages. If an older
runtime instead emits `pipeline.abandon_refused_state_guard`, stop it before it
can loop over the exhausted journal, canonically abandon the unchanged Master
checkpoint, deliver the control-plane repair, and prepare a new workflow.

The 2026-07-16 dynamic audit proved this terminal path three times. Workflows
v24, v25, and v26 each completed exactly one canonical abandon after a terminal
proposal slot, with abandon-ledger receipts respectively
`f9eb8cf5c87c848df546ac1a0dfb1fdb14ecd54cafb6406c96c5ff75356999de`,
`c58fb7fec0eee9d66d4c5688cd57486e8e24a4599f6f0e6e3b0a222875988faf`,
and `38a7754cb2b67da865ac87646d50bf49c7c94825ac7c630c346a1da58a2c86b1`.
Across those workflows the read-scope guard rejected 12 attempted documentation
reads (2, 2, and 8); none of the rejected bytes entered a prompt, projection,
or evidence receipt. The service was then stopped with workflow-v27 active at
`direction_audited`, revision 4, audit attempt 0. On the unchanged pre-repair
runtime HEAD, the exact canonical transaction fenced both journals, quarantined
the candidate and cleared the checkpoint: abandon receipt
`40f2fecb8ec3524bc1632d54380a030140d5842f41a166a1e03a5a35880f1f09`,
transaction `ddc338ed1f1d876112ee72c6725dae6166d522e0b83633a1e50161206d23be85`,
finalize receipt
`627869541aab54b63dcdb85f839c3a31e44ba5a8330913611c571ddaff4d8706`.
No active checkpoint or candidate remains at the synchronization boundary.

The first-strict Reviewer and Critic never rebuild their prompts from live
checkpoint or evidence state after call creation. Their descriptor freezes the
semantic renderer inputs and the checked-in producer/template identity. The
Critic also freezes its read scope; for the empty v143 pool the scope is `None`
and the prompt explicitly prohibits every strength/history source. The normal
post-v143 Critic continues to use only its immutable generation snapshot.

Frontend operational detail follows the same identity boundary. The separately
polled schema-2 checkpoint must carry a positive `checkpoint_revision` and is
displayed only when revision, epoch, versions, stage, run, and workflow match
the paired active-generation state. Critic `approved=true` records completed
advisory execution; only `advisory_approved` supplies the displayed
recommendation. The supported `--no-daemon` mode projects heartbeat
`not_applicable` and does not treat an absent PID file as degraded. An enabled
daemon with no PID, or a live daemon while disabled, remains a health failure.

## Ten-generation observation

The count begins with the first successful publication after the final code or
configuration repair and process restart. Increment only when the new strict
bot has a complete publication identity and the generation required no manual
state mutation or restart.

Reset to zero on:

- infrastructure/configuration repair;
- web/orchestrator/daemon restart;
- manual checkpoint/result/candidate cleanup;
- evaluation-contract or HEAD drift outside a permitted recovery;
- incomplete/partial publication;
- certificate, tag, tree, cycle, cutoff, or source-selection mismatch.

The configured daemon enabled/workers/pairs tuple is identity-bearing. Status
reads never run remote Git/certificate checks on the event loop: they return a
coalesced verification snapshot. Only `verification.state=fresh` with an
unexpired `fresh_until` may expose N/10; pending, stale, and failed snapshots
are fail-closed.

The runtime branch and exact local HEAD are also identity-bearing. A generation
may advance that HEAD only to the exact publication commit whose annotated
completion/high-water tags and `origin/main` all resolve to that commit. One
direct governed publication commit preserves the streak. A detached/changed
branch, a different local or remote HEAD, or more than one intervening commit
suppresses the count immediately and persists a `repository_head_drift` reset
before any later publication can become the first row of a new streak.

For each generation record the bot/tag/tree/certificate, evaluation identity,
cycle/cutoff, workflow journal, selected source, native 70-hand samples,
official job, process boot identity, HEAD/contract digest, heartbeat, failures,
and current N/10. Lack of measured strength improvement is not hidden: the
stagnation/diversity/literature/crossover route must activate from frozen
evidence where required.

## Final cleanup

After the task commits are confirmed in `origin/main`, remove the clean task
worktree and delete its merged local branch with `git branch -d`. Delete a
remote task branch only after confirming the merge and repository policy.
Retain Arena, runtime state, unmerged work, and every dirty user checkout.
