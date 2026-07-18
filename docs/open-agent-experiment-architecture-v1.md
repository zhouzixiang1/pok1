# Open Agent Experiment Architecture v1

Status: proposed target architecture; not an active runtime contract.

This document is an executable migration design for `national_tcp_policy_v1`.
It does not authorize a Bot, checkpoint mutation, rating row, certificate,
runtime restart, or migration of research bytes. Its purpose is to replace a
patch-by-patch evolution workflow with one architecture that gives agents room
to run real strategy experiments while preserving the small set of properties
that the national platform and publication boundary must enforce.

The proposal deliberately separates two questions:

1. **May this program safely and reproducibly play on the national platform?**
   The system answers this with a closed, deterministic compliance plane.
2. **Is this strategy idea worth trying and did it make the Bot stronger?**
   Agents answer this in isolated experiment lanes; native 70-hand evidence,
   review and promotion decide what survives.

The first publishable Bot must not wait for the whole architecture. The current
v143 bootstrap can finish through the existing strict runtime after the v52
state-machine contradiction is resolved by a governed abandon and a fresh
workflow. The new experiment plane is introduced in shadow and then promoted
behind versioned contracts.

## 1. Decisions

### 1.1 What remains hard and closed

The following are the compliance kernel. No Master, Worker, Reviewer, Critic,
experiment, model, table or operator prompt may relax them:

- raw national TCP client/server roles and delimiter-free stream parsing;
- national action legality and typed policy intent;
- exactly one system-owned socket send path;
- the official decision deadline, safe fallback and late-result exclusion;
- process, filesystem, network, import and resource isolation;
- content identity for every executable byte and system asset;
- one complete compliant 70-hand native TCP match per strength sample;
- immutable evaluation cycles, signed publication and annotated completion tag;
- current-epoch evidence only; Arena, official EXE outcomes and retired history
  retain zero strength weight.

These rules belong in a `ComplianceEnvelope`. They are capabilities and
resource bounds, not a required poker strategy.

### 1.2 What becomes open

Within the envelope, an agent may choose and test any strategy architecture:
range construction, abstraction, search, solving, opponent modelling, match
control, bet sizing, value approximation, blueprint use, neural inference, or a
new approach not named in a repository prompt. A generation may run several
independent hypotheses. No keyword, A1/A2/B label, fixed policy skeleton or
preselected threshold is promotion authority.

The open work is frozen in a `GenerationCharter`, executed in one or more
independent `ExperimentLane`s, and admitted only through a
`PromotionReceipt`. Results may produce a bounded
`GenerationExperienceSnapshot` for later agents, but that snapshot is never a
parent-selection, rating or strength authority.

### 1.3 The authoritative two-name identity

The first current-epoch published Bot has one canonical technical identity and
one user-facing ordinal:

| Field | Required value |
|---|---|
| `canonical_version` | `143` |
| `generation_ordinal` | `1` |
| `canonical_bot` | `national_v143` |
| `canonical_tag` | `national-bot-v143` |

`canonical_version=143` preserves the annotated completion/high-water namespace
and existing reset, certificate and tag contracts. `generation_ordinal=1`
states the product truth: no usable Bot has yet been published in this strict
epoch. Renaming the Git identity to v1 would create a second namespace and
break reset receipts, certificate eligibility and historical high-water
continuity; therefore it is not the migration.

The epoch authority/backend is the single producer of the mapping. The
frontend may render `第 1 代 · national_v143 · national-bot-v143`, but it must
never recompute the ordinal from directory names, array position, a maximum
version, logs or tags. An attempt/workflow counter such as `workflow-v52` is not
a Bot version and does not consume an ordinal.

The candidate path, while a governed v143 workflow exists, is
`/home/zzx/project/pok/.evolution_pok/bots/national_v143/`. It is not usable
until exact artifact identity, `.completed`, signed certificate, annotated tag
and publication transaction all agree. After publication and ordinary Git
sync, the same tracked repository path is visible as
`/home/zzx/project/pok/bots/national_v143/`; a directory alone is never proof.

## 2. Current dynamic evidence: workflow v52

The stopped runtime at source identity `a734faba` exposed a useful end-to-end
counterexample. This is migration acceptance evidence, not a strength result:

- workflow `generation:143:workflow-v52` prepared the five-file v143 candidate;
- Master selected an `action_profile_confidence_v1` experiment whose required
  consumer chain included `_bounded_action_profile` and
  `_action_profile_adjusted_intent`;
- deterministic Worker execution completed without materializing that chain;
- generic quality reported `all checks passed`, including a 70-hand-per-pair
  national native acceptance;
- Lead Reviewer read the actual candidate, found the selected mechanism absent,
  rejected it with score 3, and identified missing strict confidence fallback,
  the wrong `fold_to_raise` input, boolean numeric acceptance, missing influence
  telemetry and an apparently unreachable helper;
- the review path requested `system_strict_bootstrap_review_rejected` abandon,
  but the state guard refused it at `quality_passed` because the reason was not
  authorized at that stage;
- recovery repeatedly replayed the same accepted Reviewer result, obtained the
  same rejection, tried the same refused abandon and left the checkpoint at
  `quality_passed`;
- the operator-facing page could still report an old `Master planning` phrase,
  contradicting the checkpoint and the review result.

This is not evidence that Reviewer freedom should be reduced. It proves that
the current system conflates four authorities: generic platform quality,
charter-specific implementation, review disposition and state routing. The
target architecture must make each explicit.

P0 acceptance therefore uses v52 as a mandatory scenario:

1. generic legality/native quality may pass, but charter-specific promotion
   quality must fail when the selected causal chain is absent;
2. a binding Reviewer rejection must create one durable `review_failed`
   disposition and one legal next transition;
3. the same immutable review receipt may be read idempotently, but it must not
   execute the same transition repeatedly;
4. failed repair/withdrawal must reach a canonical abandon transaction from
   the owning state;
5. UI/API/SSE must project the checkpoint disposition, not a transient prior
   role phrase;
6. after the fix, v52 is canonically abandoned and a fresh workflow is started;
   its candidate is not reused, relabelled, rated or certified.

## 3. Target architecture

```text
epoch authority + immutable parent/rating snapshot
                         |
                         v
               GenerationCharter
             /         |          \
            v          v           v
       Lane A       Lane B       Lane C       (independent scratch/worktree)
            \          |          /
             frozen ExperimentLane receipts
                         |
                         v
             promotion selection/review
                         |
                         v
                 PromotionReceipt
                         |
          all-launch resolver + ComplianceEnvelope
          /        /         |          \          \
      quality   runtime     native     Arena      official
                         |
        signed publish + canonical identity map
                         |
      immutable rating cycle + 70-hand replays
                         |
          GenerationExperienceSnapshot
                         |
           next generation evidence snapshot
```

The architecture is a set of content-addressed contracts, not a set of prompt
conventions. Every contract has one producer, explicit consumers and a schema
validator. A role may propose content; only the system freezes authority.

### 3.1 `ComplianceEnvelope`

The envelope is the immutable safety and execution boundary for one generation.
It is produced by the scheduler from source-controlled policy plus the frozen
epoch and parent identities. The Master cannot edit it.

Minimum fields:

```json
{
  "schema_version": 1,
  "evaluation_epoch": "national_tcp_policy_v1",
  "canonical_version": 143,
  "generation_ordinal": 1,
  "runtime_profile_id": "national-runtime-v1",
  "runtime_profile_digest": "<sha256>",
  "protocol_oracle_digests": {"raise": "<sha256>", "terminal": "<sha256>"},
  "policy_intent_schema": "national-policy-intent-v1",
  "decision_context_schema": "<version-and-digest>",
  "deadline_profile_digest": "<sha256>",
  "sandbox_profile_digest": "<sha256>",
  "system_asset_profile": {"id": "no-external-assets-v1", "digest": "<sha256>"},
  "allowed_provider_slots": [],
  "native_evaluation_identity": "<sha256>",
  "official_profile_digest": "<sha256>",
  "envelope_digest": "<sha256>"
}
```

For a future profile, `allowed_provider_slots` may name bounded system-owned
interfaces such as `blueprint_query`, `value_lookup` or `policy_logits`. It may
never contain a host path, candidate-supplied loader, arbitrary import, network
endpoint or credential. Required slots fail closed if unavailable; optional
slots have an explicit legal empty-provider behavior.

An envelope says what the candidate may do, not how it should play. Strategy
terms such as “must use CFR”, “must be A1” or “raise at threshold 0.42” are
invalid envelope fields.

### 3.2 `GenerationCharter`

The Charter is the frozen experiment contract. The Master proposes it from the
current evidence snapshot; anonymous critics/reviewer validate its falsifiers;
the system compiler freezes it. It replaces rigid strategy directives with a
bounded space in which agents can make genuine choices.

Required fields:

- charter identity and schema;
- exact `ComplianceEnvelope` digest;
- canonical version/ordinal and exact published parent identity or bootstrap
  control identity;
- frozen evidence-snapshot and selection-snapshot digests;
- strategic objective and known uncertainty;
- one to N independently executable hypotheses;
- for each hypothesis: mechanism, allowed files/provider slots, resource budget,
  control, intervention, expected observation, falsifier and rollback behavior;
- lane count/concurrency cap, wall/CPU/memory/query budgets and deterministic
  seed policy where applicable;
- promotion metrics, minimum evidence, multiple-comparison treatment and
  tie/no-winner rule;
- explicit statements that diagnostic Arena and official certification carry
  zero strength weight;
- Charter digest.

The Charter may permit a broad implementation target (for example “improve
river strategy under this resource budget”) rather than prescribe a code shape.
Quality checks only claims the chosen hypothesis actually makes. A lane that
claims no opponent model is not rejected for lacking one; a lane that claims
one must prove a reachable, observed consumer.

### 3.3 `ExperimentLane`

An Experiment Lane is an isolated execution unit, never the active candidate
directory. Each lane receives the same immutable baseline artifact,
ComplianceEnvelope, Charter subset and evidence cut-off.

Each lane owns:

- a unique UUID lane ID and lease/fencing token;
- a detached worktree or equivalent immutable-base overlay;
- a lane-local scratch root outside the five-file Bot artifact;
- a bounded journal of agent effects;
- optional lane-local build/cache products that can never be promoted;
- test, resource and observed-influence receipts;
- a terminal `ExperimentLaneReceipt` binding the base, resulting five code/
  identity files, system asset requests, commands, test outputs and status.

Agents may create helpers, notebooks, generated tables, temporary models and
instrumentation in scratch. That is their freedom to experiment. Scratch has
no import path into the candidate and no automatic promotion. If an experiment
needs a durable model/table, it emits a declarative system-asset build request;
the asset builder independently creates and registers content under the system
asset authority. Candidate bytes never nominate an arbitrary local file.

Lane isolation rules:

- no lane writes another lane, the operator checkout, `.evolution_pok` active
  candidate, ratings, checkpoints or publication files;
- no lane reads live results after its evidence cut-off;
- all lane effects are lease-fenced, journalled and crash recoverable;
- lanes may run concurrently when CPU/memory/port budgets permit;
- a failed or timed-out lane cannot reserve the canonical candidate path;
- deleting scratch is cleanup, never an evidence or state transition.

### 3.4 `PromotionReceipt`

The Promotion Receipt is the only bridge from experiments to the candidate
pipeline. It is system-produced after lane freeze and selection. It binds:

- Charter and ComplianceEnvelope digests;
- all terminal lane receipt digests, including failed lanes;
- selection method and predeclared metric/falsifier outcomes;
- selected lane ID, or an explicit `no_promotion` result;
- exact five-file candidate artifact hashes;
- exact registered system asset/profile hashes, if any;
- charter-specific implementation checks and generic compliance checks;
- Reviewer disposition and any accepted repair lineage;
- strength-evidence scope and uncertainty; precommit/official/rating evidence is
  added only by later monotonic receipts, never anticipated;
- promotion digest.

The selected lane cannot copy scratch into the Bot directory. Promotion
re-materializes only the strict five-file artifact plus references to
system-owned assets in a versioned manifest. Hash drift after selection
invalidates the receipt and returns to lane freeze or controlled abandon.

### 3.5 `GenerationExperienceSnapshot`

This is a constrained handoff from one published generation to later agents,
not a free-standing memory file. It may contain structured observations such as
which hypothesis was attempted, which falsifier fired, which poker situations
changed, uncertainty, resource cost and unresolved questions.

Every observation must bind all of:

- exact published artifact and annotated tag tree;
- complete raw native 70-hand replay bytes and replay hashes;
- parser, protocol and strict runtime identity;
- immutable evaluation-cycle identity and match-history cut-off;
- derivation implementation/version, inputs and derivation digest;
- producer role/receipt and immutable snapshot digest.

The snapshot is produced only after publication/evaluation and consumed only
through the next generation's frozen evidence snapshot. It cannot choose a
parent, alter a rating, count as a match, override the selection snapshot, or
claim official/Arena strength. Missing any binding drops the affected
observation; it does not fall back to prose, logs, archive or live history.

### 3.6 Versioned runtime and system asset broker

The current global template pinning must become a versioned registry. A
published v143 remains permanently verifiable under `national-runtime-v1`; a
future runtime/profile release does not rewrite the expected bytes of old Bots.

The registry maps an immutable `runtime_profile_id` to:

- system `national_bot.py` and `precompute.py` identities;
- decision-context and typed-intent schemas;
- sandbox, timing and managed-executor profiles;
- asset-broker protocol and allowed provider slot schemas;
- compatible official execution and probe identities;
- deprecation state that may stop new issuance but never erase verification.

Large tables, blueprints or models live under a system-owned immutable asset
registry. Issuance binds builder source/environment, content digest, encoding,
byte limits, key/query schema and build receipt. The candidate receives an
opaque, nonce/context/quota-bound provider facade. It never sees a host path,
file descriptor, credential, registry root or arbitrary query language.

The broker must enforce no-follow content reads, exact digests, request and byte
quotas, deadline propagation and deterministic error classification. A required
asset that fails to load makes the candidate ineligible; an optional asset uses
the envelope's declared empty fallback. A system-observed influence probe must
show the provider reaches the final typed intent before the capability is
credited.

### 3.7 One all-launch resolver

Every execution path must ask one resolver for an `ExecutableBotSpec`:

- import/static and dynamic quality;
- decision-context/capability probes;
- native TCP precommit;
- rating daemon matches;
- decision tester and wire replay;
- diagnostic Arena;
- official harness and certification.

The spec contains the exact artifact, runtime profile, asset profile,
ComplianceEnvelope, sealed projection and resolver digest. A launcher cannot
construct a partial spec or read a Bot directory itself. Resolver output is
content-identical across paths; the only permitted differences are declared
execution budgets and zero-strength diagnostic labels. Any bypass is a release
blocker.

## 4. Canonical state machine

One state reducer owns transitions, route allowlists, retry budgets, checkpoint
validation, recovery and UI projection. Tools request events; they do not write
stage strings directly. A transition returns one immutable effect receipt and
is idempotent by event ID.

| State | Required authority | Normal next event/state | Failure transition |
|---|---|---|---|
| `idle` | no active checkpoint | allocate identity → `allocated` | infrastructure pause |
| `allocated` | epoch + canonical version/ordinal | freeze envelope → `enveloped` | canonical abandon |
| `enveloped` | valid ComplianceEnvelope | freeze Charter → `chartered` | canonical abandon |
| `chartered` | valid Charter | create lanes → `experimenting` | canonical abandon |
| `experimenting` | live fenced lanes | freeze lane receipts → `lanes_frozen` | per-lane fail; all fail → no-promotion |
| `lanes_frozen` | all lanes terminal | select → `promotion_selected` or `no_promotion` | canonical abandon |
| `promotion_selected` | PromotionReceipt + exact bytes | generic/charter quality → `quality_passed` | `quality_failed` |
| `quality_failed` | typed gate findings | revised Charter/new lanes or abandon | canonical abandon |
| `quality_passed` | generic + Charter checks | review → `review_passed` | `review_failed` |
| `review_failed` | one bound review receipt | repair lane, re-charter or abandon | canonical abandon |
| `review_passed` | approved review receipt | advisory Critic → `critic_checked` | invalid Critic retries Critic only |
| `critic_checked` | schema-valid advisory receipt | native precommit → `precommit_passed` | measured regression → repair lane |
| `precommit_passed` | complete compliant native receipt | official certification → `official_certifying` | repair/abandon by typed class |
| `official_certifying` | current live admission | signed result → `certified` | quality drift → refresh; infra → bounded retry |
| `certified` | signed certificate | transactional commit/tag → `published` | publication reconcile or stop |
| `published` | tag/tree/certificate agree | immutable rating cycle → `rated` | awaiting cycle, not hidden |
| `rated` | content-addressed cycle | derive experience → `experience_published` | omit invalid observations |
| `experience_published` | bound snapshot or explicit empty snapshot | archive/next generation | stop on identity drift |
| `abandoning` | typed reason valid for source state | terminal receipt → `abandoned` | stay `abandoning`; never delete state |

`review_failed`, `quality_failed`, `no_promotion`, `abandoning`, `abandoned`,
`awaiting_rating`, `paused` and `infrastructure_blocked` are explicit product
states. They are not encoded as transient log text.

The reducer owns a typed disposition matrix. In particular, review rejection
at `quality_passed` is legal and cannot be refused by an unrelated stage
allowlist. Replaying the same review event returns the same transition receipt
without running Reviewer or abandon again.

## 5. Producer and consumer ownership

| Data/contract | Sole producer | Consumers | Forbidden use |
|---|---|---|---|
| canonical version/ordinal map | epoch authority/backend projection | checkpoint, API, frontend, certificate display | frontend/directory/tag sorting |
| ComplianceEnvelope | scheduler + source policy compiler | Charter compiler, lanes, resolver, all gates | agent mutation or strategy prescription |
| GenerationCharter | Master proposal + system freeze after review | lanes, promotion selection, charter-specific quality | direct candidate/publication authority |
| ExperimentLane journal/receipt | fenced lane executor | promotion selector, audit, cleanup | active checkpoint/rating mutation |
| registered system asset | asset builder/registry | broker, resolver, identity gates | candidate path/file I/O or research-byte copy |
| PromotionReceipt | system promotion selector | quality, Reviewer, precommit, certification, publication | strength claim before native cycle |
| native replay/match row | managed native runner/admission | immutable cycle, evidence snapshot, experience derivation | incomplete hand/Arena/official substitution |
| immutable rating cycle | rating-cycle publisher | parent selection, next generation evidence | experience/prompt override |
| GenerationExperienceSnapshot | bound derivation pipeline | next frozen evidence snapshot | parent/rating/certificate authority |
| checkpoint/product status | canonical state reducer | recovery, API/SSE/UI | WebUI transient phrase as authority |

## 6. Reuse, adapt and replace

### Reuse as hard kernel

- `sever/engine/`, `sever/server/transport.py` and the pinned official oracles;
- strict `decision_context`, typed intent, opponent tracker and the single socket
  owner;
- managed sealed execution, no-follow reads and sandbox/resource controls;
- artifact hashing, native runtime probes and complete 70-hand native runner;
- immutable evaluation snapshots/cycles, formal certification, certificate
  signing and transactional Git publication;
- checkpoint CAS, effect journals, leases and dual-checkout synchronization.

Reuse means preserve behavior and migrate the call site to new contracts; it
does not mean freeze current module boundaries forever.

### Adapt behind versioned interfaces

- strict five-file code/identity ABI remains, while manifests gain a versioned
  system asset profile binding;
- generation scheduling gains ordinal mapping, envelope and Charter identities;
- Master/Worker/Reviewer/Critic prompts render typed contract projections rather
  than a fixed strategy recipe;
- quality separates generic compliance from Charter-specific causal claims;
- evidence snapshot adds the validated experience projection without reopening
  live or retired history;
- backend/frontend consume the reducer's product status and dual identity.

### Replace, do not extend with more special cases

- replace scattered stage sets and ad-hoc abandon reason checks with the single
  event reducer/disposition matrix;
- replace transient `WebUI.set_status` ownership as product truth with canonical
  checkpoint projection; transient text remains progress detail only;
- replace one active mutable candidate as the experiment workspace with isolated
  lanes and scratch;
- replace strategy-specific hard directives with an open GenerationCharter;
- replace global current-template pinning with immutable runtime profiles;
- replace direct/partial Bot resolution in launch adapters with the all-launch
  resolver;
- replace generic lesson prose with the provenance-complete experience snapshot;
- replace “highest number means newest product” with the epoch-produced dual
  identity mapping.

## 7. Migration plan

Every phase is independently testable and revertible at its declared boundary.
No phase grants strength, rating or publication authority merely by landing
source code.

### P0 — Recover and publish the first Bot without waiting for the refactor

1. Keep the runtime stopped while fixing the root v52 state/disposition and
   Charter-specific quality mismatch in current contracts.
2. Prove the v52 acceptance scenario: absent selected mechanism fails promotion
   quality; Reviewer rejection routes once; API/UI show `review_failed`; exact
   replay is idempotent; canonical abandon is legal.
3. Execute the existing governed abandon transaction for workflow v52. Do not
   delete or edit its checkpoint/candidate and do not reuse its candidate.
4. Start one fresh v143 workflow on the current strict-v1/no-external-assets
   profile. R0 assets, multi-lane execution and experience snapshots are not v143
   prerequisites.
5. Complete quality, review, advisory Critic, native precommit, one-time v143
   operator bootstrap, signed certificate, commit and annotated
   `national-bot-v143` tag.
6. Publish the backend mapping `canonical_version=143`,
   `generation_ordinal=1`, then expose it in the UI.

The P0 implementation may introduce the canonical disposition reducer for the
current stages first, but it must be the same reducer extended by later phases,
not a v52-only `if` branch.

### P1 — Identity and state authority

- introduce schema-versioned dual identity projection;
- introduce the canonical event reducer and typed disposition matrix;
- route checkpoint writer, recovery, tools, API, SSE and UI through it;
- keep old checkpoint schemas readable under the legacy runtime profile;
- shadow-emit reducer decisions beside existing routing until byte-for-byte
  agreement is established, except for the intentional v52 correction;
- migrate status UI to reducer state plus separately labelled transient detail.

### P2 — Open Charter and independent lanes

- implement ComplianceEnvelope and GenerationCharter schemas/compilers;
- start with two bounded lanes in shadow, using immutable candidate baselines;
- add scratch ownership, leases, quotas, journal recovery and terminal receipts;
- update agent prompts to describe outcomes/falsifiers/resources rather than
  prescribe an implementation;
- compare shadow-selected output to the existing single Worker without allowing
  shadow results to alter the active candidate.

### P3 — Promotion and experience closure

- make PromotionReceipt the only experiment-to-candidate bridge;
- split generic compliance checks from Charter-specific claim checks;
- make Reviewer disposition a first-class state transition;
- implement GenerationExperienceSnapshot derivation and frozen injection;
- prove every consumed observation has artifact/replay/parser/runtime/cycle/
  derivation binding and cannot affect parent/rating authority.

### P4 — Versioned runtime and system asset broker

- freeze current runtime as immutable `national-runtime-v1` verification data;
- introduce runtime-profile and system-asset registries;
- implement the broker facade with nonce/context/quota/deadline binding;
- migrate every launch path to the all-launch resolver;
- add required/optional provider failure behavior and observed influence probes;
- issue new asset-enabled profiles only after native, Arena and official paths
  prove identical resolved identity.

### P5 — Default switch, retirement and continuous acceptance

- switch new generations to envelope/Charter/lane/promotion authority;
- stop issuing legacy profile generations while retaining old Bot verification;
- archive superseded prompt/routing/experiment facilities as legacy-untrusted;
- publish the second Bot, establish the first immutable two-Bot native rating
  cycle and use its frozen selection snapshot for parent choice;
- complete ten consecutive generations after the final repair/restart, each with
  at least one complete admitted 70-hand native strength sample; any code repair,
  manual state intervention or restart resets the count to zero;
- record runtime, checkpoint, certificate, tag, rating cycle and per-generation
  evidence in the delivery ledger and executable cross-layer matrix.

## 8. Regression and acceptance matrix

Tests are part of each phase's design. If an architecture change changes a
workflow or ABI, its focused/full test workflow changes in the same batch.

| Contract | Positive regression | Negative regression / fail-closed result |
|---|---|---|
| dual identity | published first strict artifact projects ordinal 1 and canonical v143/tag consistently through authority, API and UI | directory `national_v999`, workflow-v52, list order or frontend sorting cannot change ordinal; mismatch blocks projection |
| ComplianceEnvelope | two different strategies execute under identical legal/timing/sandbox bounds | unknown field, oracle/runtime/deadline/profile drift or strategy prescription in envelope rejects freeze |
| GenerationCharter | Master freezes distinct legal hypotheses with explicit controls/falsifiers and no fixed strategy keyword requirement | missing envelope/evidence/parent digest, live history reference, non-falsifiable metric or official/Arena strength claim rejects Charter |
| lane isolation | two lanes run concurrently from identical base; one failure leaves the other and active runtime unchanged | cross-lane write, symlink escape, live-result read, direct candidate/checkpoint/rating write or expired lease kills only offending lane |
| lane scratch | model/table/helper can be built and tested in scratch; declarative asset request is emitted | scratch import, copy into Bot, host path in policy/manifest, candidate loader or unregistered bytes reject receipt/promotion |
| PromotionReceipt | exact selected lane bytes re-materialize and all lane outcomes/metrics bind | changed byte, omitted failed lane, post-hoc metric, unselected lane, missing Charter claim or scratch byte blocks quality/publication |
| generic vs Charter quality | legal candidate plus implemented selected causal path passes both classifications | v52 fixture: generic native/legality passes but absent action-profile consumer fails Charter quality before review |
| review transition | one rejection event produces one `review_failed` receipt and routes to repair/re-charter/abandon | replaying identical receipt cannot invoke Reviewer or abandon again; invalid disposition cannot leave `quality_passed` loop |
| abandon | typed rejection from `review_failed` completes one digest-chained abandon then permits fresh allocation | wrong owner/stage/reason, partial transaction or direct checkpoint deletion cannot clear/reserve identity |
| state/UI | checkpoint `review_failed` renders non-green rejection and exact next action; transient detail is subordinate | stale `Master planning`, prior owner/revision, stopped task or stage mismatch is suppressed on JSON and SSE |
| experience | bound artifact + full replay + parser/runtime + immutable cycle + derivation produces a cited observation in next frozen snapshot | missing/edited replay, wrong runtime/cycle, archive prose, Arena/official outcome, free text or digest drift drops observation and cannot affect parent/rating |
| runtime profile | v143 continues to verify under v1 after v2 issuance | rotating global template invalidates no old Bot; unknown/retired-for-issuance profile cannot launch a new candidate |
| asset broker | registered provider query is quota-bound and an intervention changes an observed final typed intent | missing required asset, hash/encoding/key drift, wrong nonce/context, quota/deadline breach, path leak or arbitrary query fails before use |
| all-launch resolver | quality, probe, native, rating, Arena and official paths report the same artifact/runtime/asset/resolver digests | any adapter opens Bot/asset directly, supplies a partial spec or resolves different identity; release gate fails |
| native strength | admitted complete 70-hand raw match publishes one W/L/D sample in immutable cycle | 69 hands, wrong mode/epoch/artifact/plan, replay drift, Arena or official result yields no row |
| publication | working bytes, staged blobs, tag tree, certificate and dual identity agree | any drift prevents `.completed`/tag/remote publication and no ordinal advances |

Minimum phase gates:

- focused schema/state/resolver tests and positive/negative fixtures;
- full Web and Sever test suites;
- frontend lint, cross-language schema tests and production build;
- compile and `git diff --check`;
- native process/sandbox/socket/probe regressions;
- stopped-state checkpoint recovery diagnostics;
- for P4, all-launch identity matrix and real broker failure injection;
- for P5, signed certification, annotated tags, immutable rating-cycle identities
  and per-generation 70-hand replay hashes.

No “existing tests passed”, keyword grep, local Arena result or documentation
review substitutes for these causal tests.

## 9. Old checkpoint compatibility and rollback

Compatibility is explicit and asymmetric:

- an old checkpoint with none of the new envelope/Charter/lane/promotion fields
  remains readable only under its pinned legacy runtime/workflow profile;
- the reader must not synthesize authoritative new fields into that checkpoint;
- a half-present new contract, digest mismatch or unknown schema fails closed;
- workflow v52 is terminalized through canonical abandon, not upgraded in place;
- a fresh workflow creates a fresh complete contract set;
- old published Bots remain verifiable through immutable runtime profiles;
- no old Bot, checkpoint, rating or evidence is copied into a new epoch as
  strategy/strength authority.

Each P1-P4 feature starts in shadow/read-only mode. Rollback before activation
disables its producer and ignores shadow outputs without deleting durable data.
After a checkpoint contains an activated new authority, an older binary must
refuse to run it; rollback means restore the compatible new binary or canonically
abandon under the current binary, never silently downgrade or hand-edit state.

Asset-profile rollback stops new issuance and selects a prior registered profile
for a *new* generation. It never changes a published Bot's bound profile. Lane
cleanup requires terminal lane receipts, owner markers and clean worktrees; an
unknown/dirty lane is preserved for review.

Runtime source synchronization continues only through `origin/main`, at a
stage-safe stopped boundary. No source, candidate, checkpoint or asset is copied
between the operator checkout and `.evolution_pok`.

## 10. Completion definition

The architecture is not complete when schemas exist. Completion requires:

1. the v52 acceptance scenario closes without candidate reuse or an abandon
   loop;
2. a fresh v143 publishes as canonical version 143 / ordinal 1 with real native
   evidence, signed certificate and annotated tag;
3. agents can run at least two isolated materially different experiment lanes
   without affecting each other or the active runtime;
4. a PromotionReceipt proves which exact result became the candidate;
5. a later generation consumes a provenance-complete experience observation and
   a poison test proves it has no parent/rating authority;
6. a system-owned asset-enabled Bot resolves identically across every launch
   path and fails closed under missing/drifted asset injections;
7. the second Bot publishes and the two-Bot immutable native rating cycle is
   content-addressed and selectable;
8. ten consecutive post-repair/post-restart generations each retain complete
   admitted 70-hand native replay evidence;
9. backend/frontend display canonical product state and the authoritative dual
   identity without deriving either from runtime debris.

Until then, this document is a migration contract. It is not permission to
weaken compliance, publish an unreviewed candidate, transfer research strength,
or delay the first Bot behind future architecture work.
