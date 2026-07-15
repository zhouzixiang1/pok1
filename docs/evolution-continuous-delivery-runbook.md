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
the canonical abandon/quarantine transaction.

## Durable post-publication handoff

The signed publication transaction and the post-publication Archivist are two
parts of one launch barrier. While still holding the publication lock,
publication must durably create the exact schema-2 handoff record, active
pointer, and archive base snapshot before clearing the publishing checkpoint.
On first creation of either authority directory, the implementation fsyncs the
child directory and its parent and rechecks both inode identities; a successful
file write inside a directory whose entry is not durable is not accepted.

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

A fresh Orchestrator provider stream owns the exact checkpoint identity present
when that stream starts. It may hand off only after an MCP effect advances the
workflow revision or stage; treating the unchanged startup checkpoint as a new
effect creates a restart livelock. Background stability verification follows
the same progress rule: every thread exit, including cancellation-class
exceptions, releases the single-flight slot and projects a failed refresh
before a later retry.

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
