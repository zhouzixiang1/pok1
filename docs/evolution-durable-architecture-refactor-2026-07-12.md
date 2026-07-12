# Durable Evolution and Poker Runtime Refactor

Date: 2026-07-12

## Decision

The repository needs an architectural refactor, not another repair branch in
`execute_workers`.

The v149 incident exposed four simultaneous authorities for one logical
operation:

1. the mutable pipeline `stage`;
2. the independent infrastructure overlay and its `resume_stage` lease;
3. repair tasks, feedback, counters, and artifact hashes spread across several
   checkpoint fields;
4. the mutable candidate directory as implicit state.

Re-entering an imperative tool function reconstructed those authorities from
live files and repeated one-time preparation. The resulting
`rework_running -> repair_planned` write was correctly rejected by the lease,
but the system had no single history from which to derive the next command.
Adding more stage guards would leave the same failure class available under a
different combination of counters, prompt additions, or artifact bytes.

The target has two independent boundaries:

- a deterministic evolution control plane; and
- a stable poker strategy data plane.

LLMs remain proposal and implementation activities. They do not own workflow
state, source identity, evidence, retry identity, action legality, or promotion.

This change is the first production slice, not a claim that the whole pipeline
is already event-sourced or that a poker solver has been delivered. It replaces
the structurally broken Worker retry/preparation loop with a durable journal and
fenced effect registry. Direction, Master, quality, precommit, official
certification, Git publication, and archive still use the versioned JSON
checkpoint until later slices migrate them.

## Research basis

### Durable execution

- [Temporal Workflows](https://docs.temporal.io/workflows) and
  [Event History](https://docs.temporal.io/encyclopedia/event-history) separate
  deterministic workflow replay from external Activities.
- [Temporal Activities](https://docs.temporal.io/activity-definition) may run
  more than once, so externally observed completion needs stable idempotency
  keys rather than an "exactly once execution" claim.
- [Azure Durable Orchestrations](https://learn.microsoft.com/en-us/azure/durable-task/common/durable-task-orchestrations)
  rebuild state from an append-only history and require deterministic
  orchestrator code.
- [Beldi](https://www.usenix.org/conference/osdi20/presentation/zhang-haoran)
  uses intents and durable logs to provide observed exactly-once semantics over
  re-executed serverless work.
- [Chubby](https://research.google.com/archive/chubby-osdi06.pdf) uses lock
  generation/sequencer values so a stale holder cannot commit after a new
  holder takes over.
- SQLite's [WAL](https://www.sqlite.org/wal.html) and
  [serializable isolation](https://sqlite.org/isolation.html) fit this
  repository's single-machine, single-writer runtime without adding a service.

The implementation adopts these semantics locally. It does not deploy
Temporal: a service would add an operations surface while file, Git, official
EXE, and candidate-artifact idempotency would still need project-specific
receipts.

### Weak-model reliability

- [Self-Consistency](https://arxiv.org/abs/2203.11171) supports diverse
  independent paths followed by aggregation, not one longer greedy answer.
- [Multi-agent debate](https://arxiv.org/abs/2305.14325) can improve reasoning,
  but LLM judges retain position and verbosity biases documented by
  [MT-Bench/Chatbot Arena](https://arxiv.org/abs/2306.05685); debate therefore
  remains advisory.
- [Reflexion](https://arxiv.org/abs/2303.11366) supports episodic feedback, but
  coding retries here must store compiler, test, replay, and boundary evidence,
  not unverified self-opinion.
- [CodeT](https://arxiv.org/abs/2207.10397) and
  [execution-based minimum Bayes risk selection](https://arxiv.org/abs/2204.11454)
  support generating several isolated code candidates and selecting with
  executable agreement/oracles.
- [RAG](https://arxiv.org/abs/2005.11401) supports explicit non-parametric
  memory. Repository evidence packets must be small, digest-bound, and
  role-specific rather than one shared long context.

### Poker computation

- [DeepStack](https://poker.cs.ualberta.ca/publications/17science.pdf) performs
  continual public-state resolving over both players' ranges, not a scalar
  hand-strength rule.
- [Libratus](https://noambrown.com/papers/17-Science-Superhuman.pdf) separates an
  offline blueprint, nested online solving, and background self-improvement.
- [Depth-Limited Solving](https://arxiv.org/abs/1805.08195) shows that useful
  HUNL search is possible on four CPUs and 16 GB when leaf evaluation gives the
  opponent multiple continuation policies.
- [Pluribus](https://doi.org/10.1126/science.aay2400) combines an offline
  blueprint with bounded real-time search and action/information abstraction.
- [ReBeL](https://arxiv.org/abs/2007.13544) identifies the public belief state
  as the correct search state, but its poker training scale and unavailable
  poker implementation make direct adoption inappropriate here.
- [Potential-aware abstraction](https://ojs.aaai.org/index.php/AAAI/article/view/8816)
  warns against bucketing hands only by current mean equity; future strength
  distributions and draw potential matter.
- [PH Evaluator](https://github.com/HenryRLee/PokerHandEvaluator) demonstrates
  the high return of a small perfect-hash table for seven-card evaluation.
- [Bayes' Bluff](https://poker.cs.ualberta.ca/publications/UAI05.pdf) distinguishes
  showdown likelihood from censored fold observations and shares inference
  across similar information sets.

## Target control plane

```text
LLM/API command
      |
      v
Generation actor (one writer per run_id, fenced epoch)
      |
      +-- one SQLite transaction: append event + enqueue effect/outbox
      v
Effect runner (LLM, Worker, battle, EXE, Git)
      |
      +-- immutable input digest + immutable result receipt/artifact
      v
Generation actor validates epoch/digest and appends completion
```

### Target source of truth

The end-state makes `workflow_events` authoritative. A reducer is a pure
function:

```text
state(n+1) = reduce(state(n), event(n+1))
```

It cannot read the clock, environment, filesystem, network, model, or mutable
evaluation results.

In Slice 1 this rule is complete only for the Worker cycle. `pipeline_state.json`
remains the compatibility authority for non-Worker stages. Worker replay may
project `stage`, but an already-recorded Worker activity cannot be reconstructed
from live files, experience, or caller arguments.

### Storage

The local kernel uses SQLite with `journal_mode=WAL`, `synchronous=FULL`, foreign
keys, and a busy timeout. The minimal schema is:

- `workflow_instances(run_id, definition_version, stream_version, status,
  fence_epoch)`;
- `workflow_events(run_id, seq, event_type, schema_version, payload,
  payload_digest, UNIQUE(run_id, causation_id))`;
- `effects(effect_id UNIQUE, run_id, kind, input_digest, status, attempt,
  lease_epoch, lease_until, result_digest)`;
- `outbox(effect_id UNIQUE, available_at, dispatched_at)`;
- `inbox(completion_id UNIQUE, effect_id, lease_epoch)`.

Appending `EffectRequested` and its outbox record happens in the same database
transaction. Completion and its consuming Worker event are also one transaction.
The current implementation rejects stale completion by effect status plus
`lease_epoch`; terminal abandon atomically marks active effects abandoned and
closes their outbox rows. `workflow_instances.fence_epoch` is retained for
future cross-effect actor fencing but is not falsely described as the current
completion predicate. Claim/attempt/lease state is an indexed mutable effect
registry, not a domain event.

### Worker activity

One logical Worker transaction freezes:

- `run_id`, source/target version, workflow definition version;
- exact prepared candidate artifact hash;
- exact tasks, repair feedback, runtime contract, and work item;
- Worker template hash and bounded dynamic prompt context;
- precommit/official repair counters;
- source artifact hash and backend contract;
- a content-addressed prepared artifact snapshot.

Its event history is:

```text
RepairCycleOpened
RepairPrepared                 # exactly once
WorkerEffectRequested
WorkerAttemptFailed(infra)     # same logical effect, next attempt
WorkerEffectRequested
WorkerOutputReady              # immutable output artifact exists
WorkerProjected                # compatibility checkpoint advanced
```

An infrastructure failure never changes the repair stage or rebuilds the
prompt. Preparation freezes tasks, feedback, source artifact, template/backend,
dynamic Worker context, exhausted-direction evidence, and its immutable staging
snapshot before publishing a receipt. A semantic failure is a separate event
containing external validator evidence. Candidate code is edited only in a
lease-epoch isolated workspace; the canonical bot directory is a crash-recovered
materialized projection of an immutable artifact.

### Versioning and migration

Every workflow instance is permanently bound to `definition_version`. Changing
the event order or activity input contract requires a new version or an
explicit tested migration. In-flight v149 state is not imported. Its real
checkpoint, candidate, evidence, and selected log identities were captured in
`docs/evolution-v149-legacy-receipt-2026-07-12.json`; the centralized forced
abandon path must clear it before the new kernel starts from a clean baseline.

## Target poker data plane

```text
Protocol Event Ledger
        -> Canonical Hand State
        -> Belief Engine (self/opponent 1326-combo ranges)
        -> Decision Portfolio
             - preflop blueprint
             - O(1) safe policy/fallback
             - exact river resolver
             - sampled turn resolver
             - confidence-capped opponent response
        -> Legal Action Projector
        -> Attribution and timing telemetry
```

The protocol ledger owns missing street closure, terminal fold/call, showdown,
pot, stacks, and action history. Strategy modules consume canonical state and
cannot maintain competing pot/history guesses.

Decision modules return a distribution, confidence, compute cost, and
attribution. A single portfolio arbitrates them before the existing legal
action projector. A module being called and a module changing the final action
are separate telemetry counters.

### Protocol authority and migration boundary

The current native wrapper is the only owner of TCP framing, omitted-action
inference, chip movement, pot, street closure, settlement, and showdown
ingestion.  Before a new street clears stage bets, it first records only the
call/check that is proven by the official-EXE boundary.  Relayed terminal folds
and relayed or boundary-inferred calls enter the same incremental opponent
tracker, so later decisions no longer depend on a lossy cross-hand replay of
old request/response arrays.  Showdown cards update a bounded, explicitly
`reached_showdown_only` posterior whose influence is capped for selection bias.

The wrapper publishes two bounded strategy ABIs:

- `hand_runtime`: preflop aggressor/spot, semantic street history, donk and
  delayed-probe opportunities, pot, stacks, SPR, pot odds, and line tags;
- `opponent_runtime`: sample counts, priors, confidence, terminal responses,
  street profiles, and the capped showdown posterior.

Publishing fields is not acceptance.  The legacy strategy seed currently
consumes neither terminal-response nor showdown-range evidence and does not
prove separate donk/delayed-probe effects on the final sanitized wire action.
The first strict migration generation is therefore bound to one system-owned,
all-or-nothing contract with four final-blocking counterfactuals:

1. `terminal_response_adaptation`;
2. `showdown_range_adaptation`;
3. `donk_line_reachability`;
4. `delayed_probe_line_reachability`.

The weak planner cannot omit, rename, split, or replace this bundle with an
ordinary one-primary innovation.  The deterministic plan compiler restores
the exact checks and writable consumer surface, and quality independently
reruns the dynamic positive/control probes.  Normal one-primary architectural
innovation resumes only after all four consumers pass together.

Published bots whose immutable `national_bot.py` predates the current stream
decoder and decision runtime are content/tag/tree-bound in the protocol
quarantine.  They are never executed bilaterally or admitted to ratings.  The
exact national_v142 artifact may seed strategy files for the first migration,
but the system replaces its launcher with the current wrapper before freezing
the candidate.  After the first strict publication this exception closes.

The zero/one-strict-bot bootstrap never launders quarantined match history into
planning. Direction becomes a deterministic digest-bound neutral receipt;
Master receives explicit no-strength placeholders instead of historical
ratings, H2H, replay, action/opponent profiles, experience-pool, critic, or
official-result feedback. It may use only the prepared target source, typed
strategy references, the pinned official oracles, and the architecture
contract. With one strict bot, the same no-peer-evidence rule builds a distinct
second bot; ordinary strength selection starts only once two strict bots exist.

The first candidate parks at `official_bootstrap_required` only after its exact
artifact, complete local gates, workflow/evaluation contract, empty strict
pool, transition receipt, and unpublished status are frozen. The operator
authorization is embedded in the selection, job request, and envelope, then
recomputed immediately before process spawn, worker claim, and every formal
round. After a successful 5+3x70 suite consumes the root, `commit_bot` performs
a consumed-root-aware rebind against the still-parked checkpoint when it first
reuses the certificate and again immediately before Git publication. None of
these checks can start a second EXE job or consume the root again.

### Central managed execution boundary

Every native bot execution path now converges on one host-owned launcher:
local strength, runtime probes, Arena diagnostics, and formal official-EXE
certification no longer maintain independent sandbox policies.  The launcher:

- opens and validates one exact loopback TCP stream on the host, then hands
  only that descriptor to the child;
- creates user, IPC, PID, network, UTS, and cgroup namespaces with Bubblewrap;
- mounts no host root, mounts only the read-only runtime and sealed bot
  artifact, clears the environment, drops capabilities, and applies resource
  limits;
- installs an argument-aware seccomp BPF that forbids new INET sockets,
  `connect` (including `AF_UNSPEC` disconnect/reconnect), listener APIs,
  namespace/mount APIs, and namespace-bearing `clone` while preserving normal
  threads/process pools; and
- records the exact launcher source, tools, libseccomp, BPF, policy, endpoint
  lease, and both A/B process identities in formal evidence.

There is no permissive fallback.  Missing isolation support makes the run
ineligible rather than silently executing a candidate on the host.

### Formal evidence boundary

The official Windows EXE remains the sole protocol-compliance oracle and has
zero strength weight.  Signatures are an internal publication seal, not a
competition requirement: they bind a frozen candidate, opponent selection,
execution profile, evidence archive, certificate, and one-time bootstrap
ledger transition.  They are used only during formal finalization, never in
the decision path, Arena, native strength, Glicko, or prompts.

The current signer and the Claude/Orchestrator processes run under the same
Unix uid.  Consequently `0600` key permissions do not make the signature
resistant to a malicious same-uid LLM.  The signature authenticates a
publication only when the later verifier independently anchors the expected
public-key fingerprint; if Git is already the sole trust root, Git objects and
canonical hashes provide the content binding.  Candidate isolation comes from
the managed sandbox, not from the signature.  The signature is not claimed as
an agent-isolation boundary.  Such a claim requires a different OS identity or
host/HSM behind a non-generic finalizer that validates the complete receipt
before signing.

The crash-consistent verdict history validates an unconsumed bootstrap root and
appends its consuming verdict under one lock.  Complete signed suffixes left by
a crash are rolled forward; partial suffixes are truncated to the prior signed
head.  A failed official run never consumes the one-time root, and concurrent
successful requests cannot both consume it.  Because ledger and head share one
same-uid writable filesystem and there is no external latest-head anchor, an
older valid pair can still be restored; the history is an operational
serialization boundary, not a transparency log or cryptographic rollback
defense.

### Space-for-time asset layer

Large data is system generated and shared, never imagined or rewritten by an
LLM. Each asset has a schema, generator identity, SHA-256, key/value semantics,
entry/byte bound, load-time/RSS measurement, consumers, and packaging receipt.

The code in this slice is only the asset ABI prototype:

- a system-owned, offline builder;
- a content-addressed binary blob plus atomic manifest pointer;
- schema, key domain, semantics, generator commit, and three SHA-256 receipts;
- read-only mmap and O(1) lookup for the real 1,326 unordered hole-card
  combinations and their 169-class metadata.

It is approximately 18.7 KiB and deliberately contains no evaluator, equity,
opponent range, or policy values. It has no bot decision consumer yet and is not
presented as strategy innovation.

Future implementation order, admitted only with a measured consumer:

1. perfect-hash/bit-mask seven-card evaluator;
2. symmetric 1326 x 1326 preflop all-in equity matrix (quantized integer);
3. stack/history keyed preflop blueprint;
4. potential-aware histogram prototypes for leaf bucketing;
5. suit-isomorphic, bounded rollout cache.

No full board x combo equity dictionary is admitted. Larger assets without a
proven decision consumer fail the gate.

## LLM proposal and coding plane

The stochastic boundary becomes explicit:

1. bind the frozen Master evidence/context digest and a system-rendered exact
   source-symbol/call-leaf index;
2. sample three independent typed proposals;
3. reject proposals with missing evidence IDs, source symbols, reachability,
   control, or falsifier before any LLM ranking;
4. anonymize and randomize proposal order for criterion-specific critics;
5. require the Master to select one `proposal_id`; compile the selected
   structural change, expected diff, falsifier, reachable chain, and contract
   digest into every matching Worker prompt and immutable envelope;
6. for high-risk changes, generate two to four patches in isolated workspaces;
7. select using compile, tests, reachability/control, mutation checks, and a
   bounded native probe; use LLM judgment only for remaining ties.

Each scout or critic gets at most one local schema-only repair call before the
ensemble fails closed; a malformed ballot does not force a whole-generation
free-form fallback.

More token budget is spent on independent calls rather than trusting one greedy
answer. The current scouts still receive the same frozen Master packet plus a
role-specific lens; compact retrieval by failure signature is a later
optimization, not an implemented claim. Reflexion memory contains only typed,
externally-verified failure signatures and outcomes.

### Cost is telemetry, not an evolution objective

The former fixed generation-cost stop has been removed. The default policy
records every paid model attempt against a stable `workflow_run_id`, warns at
the operator threshold, and never interrupts evolution. Empty-output/schema
retries are accounted as separate paid attempts rather than charging only the
last successful response. Restart/resume preserves both usage and any known
accounting error, and stable event IDs make replay idempotent.

An operator may explicitly configure a finite emergency circuit breaker in the
parent process. It is deliberately described as a post-call breaker, not a
strict dollar ceiling: already-running parallel calls can overshoot before the
next launch is stopped. Model output, MCP arguments, prompts, and checkpoint
fields cannot enable or raise it. The default remains monitor-only because
token cost is not a strategy-quality gate.

## Implementation slices

### Slice 1 (this change)

- add the SQLite-WAL workflow kernel, pure replay reducer, outbox/effect lease,
  fencing, and crash-safe tests;
- migrate Worker preparation/retry/output projection to a versioned durable
  activity with content-addressed artifact snapshots and a generation lock;
- make the old JSON checkpoint a compatibility projection for Worker state;
- freeze dynamic Worker prompt additions in the activity input;
- add proposal IDs, evidence references, anonymous randomized criterion
  ballots, and unique Master selection contracts;
- add the shared poker asset ABI and real hole-combination metadata prototype
  without claiming evaluator/equity values or granting candidate-owned file I/O.

### Slice 1b (this change)

- centralize all native launches behind the fail-closed managed executor and
  bind its measured identity into the official execution profile;
- quarantine every published legacy runtime by content and Git identity, with
  a one-way strategy-only migration path for the first strict bot;
- make omitted street closure, terminal actions, settlement, showdown, and
  bounded opponent state wrapper-owned canonical facts;
- require the four-consumer legacy migration bundle before ordinary strategy
  innovation resumes;
- remove the default cost stop and make paid-attempt accounting durable,
  idempotent, and monitor-only; and
- make the one-time official bootstrap consumption and ledger crash recovery
  one locked, tested publication transaction while explicitly documenting the
  signer's same-uid limitation.

### Slice 2

- wrap Direction Audit, literature, Master, quality, precommit, official EXE,
  commit, and archive as effects;
- stop writing active JSON checkpoints and project dashboard state from events;
- move Git/tag/push to idempotent roll-forward reconciliation.

### Slice 3

- migrate the native bot shell to canonical-state/belief/portfolio interfaces;
- introduce the exact river resolver, then sampled turn resolving;
- generate larger blueprints and potential buckets only after asset packaging
  and decision-influence gates pass.

This is also where machine and decision-budget utilization belongs. The current
host has 32 logical CPUs, about 62 GiB RAM, and roughly 1.6 TiB free storage,
but today candidate decision work is normally single-process and local strength
validation uses a 1.8 s refinement envelope even though the formal ceiling is
54 s. A future system-owned fixed process pool, packaged read-only mmap assets,
and a `0.25/2/8/30/54 s` shadow budget ladder must demonstrate monotonic trusted
work and action/EV influence before the system can claim it uses that capacity.

## Deployment and rollback boundary

Deploy this slice only in the following order:

1. keep the evolution runtime stopped;
2. verify the v149 receipt and both official-oracle SHA-256 values;
3. invoke the centralized abandon path for the legacy v149 checkpoint;
4. merge and push infrastructure from the operator checkout;
5. fetch and fast-forward the runtime checkout from `origin/main`;
6. run preflight tests and verify there is no active legacy checkpoint;
7. start a wholly new generation and reset clean-generation observation count.

The pinned preflight values are:

- raise oracle: `a83a1ec2680577d71ddb985ddba00c5bcda40817ef2fb92c0c41938dccef3756`;
- terminal-settlement oracle: `ad96bc4fbe7939597b7a86ff6f9193ed2e50891be9b6b9c074883f5750c23bd9`.

Once the new runtime has emitted Worker events, rollback cannot resume its
active checkpoint with old code. First use the new code to terminal-abandon the
active workflow, then roll back the checkout. Never copy checkpoint, bot, or
artifact files between the two checkouts.

## Verification completed in Slice 1

- the complete Web/evolution suite passes (`2634 passed, 7 skipped, 1 deselected`)
  and the national TCP suite passes (`33 passed`);
- identical histories produce byte-identical state and commands;
- crash injection before/after event commit, outbox dispatch, artifact
  materialization, and completion acceptance converges without duplicate
  preparation;
- stale lease epochs and duplicate/out-of-order completions are rejected;
- an artifact receipt written before a crash is reconciled without a second LLM
  call;
- workflow version mismatch fails closed;
- multiple concurrent commands yield one contiguous event sequence;
- event payload, envelope, projection, journal, and database schema corruption
  fail closed;
- the metadata asset verifies combination coverage, indexing, manifest/header/
  payload hashes, concurrent build, corruption rejection, and size bounds;
- official raise and terminal-settlement oracle documents remain byte-identical
  and the formal EXE is not rerun merely for control-plane changes.

## Future acceptance gates

- the remaining pipeline stages become versioned effects and the active JSON
  checkpoint can be removed as an authority;
- evaluator equivalence and large-equity symmetry/card-overlap/suit-isomorphism
  pass before those assets are admitted;
- load time, RSS, packaging, 2/8/30/54-second behavior, and live decision
  influence are measured on the actual consumer;
- artifact/workspace/database reference tracking, GC, quota, and disk alarms are
  in place.
