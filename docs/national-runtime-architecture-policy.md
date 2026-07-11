# National Native Runtime Architecture Policy

This policy translates poker-AI research into constraints that are executable
in the current stdlib-only national bot runtime. It does not claim that a rule
bot implements Libratus, Pluribus, or ReBeL. It adopts the parts that fit the
actual 60-second decision contract and uninterrupted 70-hand TCP process.

## Research Basis

- Libratus separates an offline blueprint from nested online refinement and a
  post-match self-improver. The applicable design is a stable legal baseline,
  bounded online refinement, and evidence-driven repair rather than replacing
  the baseline on every decision. Sources: [Science/PubMed](https://pubmed.ncbi.nlm.nih.gov/29249696/),
  [safe and nested subgame solving](https://arxiv.org/abs/1705.02955).
- Pluribus combines self-play with limited-lookahead search. The applicable
  design is a strict depth/sample/deadline bound around refinement, not an
  unbounded Monte Carlo loop. Source: [Science](https://doi.org/10.1126/science.aay2400).
- ReBeL represents imperfect-information search with a public belief state.
  The applicable design is a compact current public-state/range summary passed
  into decision code instead of rescanning the complete request history.
  Source: [NeurIPS 2020](https://papers.nips.cc/paper_files/paper/2020/hash/c61f571dbd2fb949d3fe5ae1608dd48b-Abstract.html).
- Student of Games combines guided search, self-play learning, and
  game-theoretic reasoning across perfect- and imperfect-information games.
  The applicable design is a stable baseline with a separately budgeted online
  improvement layer, not training or unbounded search in the socket callback.
  Source: [Science Advances 2023](https://www.science.org/doi/10.1126/sciadv.adg3256).
- Bayes' Bluff separates game uncertainty from opponent-strategy uncertainty
  and updates a posterior from observed play. The applicable design is a prior,
  sufficient statistics, sample confidence, and bounded adaptation rather than
  treating early frequencies as truth. Source: [UAI paper](https://webdocs.cs.ualberta.ca/~mbowling/papers/05uai.pdf).

## Runtime Contract

Every formal native bot must preserve these boundaries:

1. The socket-owning thread computes a legal baseline before optional strategy
   work and owns the only send path.
2. Decision work uses `time.monotonic()` and three ordered boundaries: a 55
   second socket hard deadline, a 250ms strategy-baseline target, and a 54
   second refinement budget. The gap before the official 60 second timeout and
   the gap between refinement and hard return are reserved for sanitization,
   action throttling, logging, and scheduler jitter.
3. The socket owns an always-legal fallback before untrusted strategy starts.
   Refinement has both a finite work cap and its own deadline check. A timeout
   or exception returns the latest sanitized baseline. A late worker result can
   never reach the socket.
4. Pure reusable poker facts counted toward the formal precompute contract are
   inspectable collections built at module import. Every artifact has a measured
   entry/recursive-byte bound and a proven consumer in the decision call graph.
   Cold LRU caches, opaque objects, labels, and unused tables do not satisfy the
   contract; connection/match-start caches remain allowed but are not credited
   until the harness can prove their construction boundary.
5. `OpponentTracker` lives for one TCP connection, resets at connection start,
   and updates on hand start, opponent/hero actions, settlement, and showdown.
   Recent raw state is bounded to at most 70 hands; decisions consume the
   incremental `opponent_runtime` snapshot instead of scanning full history.
6. Exploit changes are multiplied by confidence/adaptation weight and capped.
   Sparse evidence therefore stays close to the baseline. Match memory is not
   a license for unconstrained opponent-specific strategy replacement.
7. Candidate-owned modules contain no filesystem, network, or subprocess I/O,
   including import-time and nominally unreachable functions. The system-owned
   native wrapper alone owns the socket, logs, deadlines, and worker process.

## Local References And Space-Time Tradeoff

The weak-model answer is not “give it more prose.” The repository supplies a
versioned `strategy_reference_pack.py` with machine-checked implementation
cards, exact live inputs, owner files, bounded-work rules, forbidden axes, and
counterfactual proofs. Master chooses a typed primary; the plan compiler binds
the selected card into the Worker prompt, while schema and runtime gates reject
made-up card ids or dead consumers. Token budget may be large, but authority is
kept in these local contracts rather than model memory.

The v1 pack is deliberately small. Its two cards cover a proactive sizing
geometry and a range-weighted bounded candidate batch, with falsifiable action
counterfactuals. New cards should be added only after replay/H2H evidence shows
an uncovered mechanism and the capability harness can verify its live
consumer; a large uncurated strategy encyclopedia would recreate the same weak-
model ambiguity under a larger token budget.

Every fresh native candidate also receives compact import-time poker facts from
`NATIVE_PRECOMPUTE_TEMPLATE`: all 1,326 hole-card combinations, all 8,192
13-rank straight masks, and the 21 five-of-seven index combinations. This is a
useful space-for-time foundation, but not an innovation by itself; a selected
precompute primary must prove a state-varying lookup changes a final sanitized
action and that its empty-table fallback remains legal.

Do not check in a multi-million-entry Python dict merely because memory is
available. Candidate file I/O is currently forbidden, so file-backed packed or
mmap lookup is intentionally deferred. A larger equity/abstraction table is
justified only after the official submission bundle accepts the artifact and a
system-owned immutable loader can pin its content hash, entry/byte/build limits,
card encoding, lookup key, and live consumer. Until then, compact generated
facts plus bounded candidate refinement use the machine more reliably and do
not turn startup/object expansion into a hidden timeout.

## Evolution Enforcement

- `national_capability_contract.py` proves data flow into action-affecting
  sinks; labels, unused reads, no-op trackers, cold caches, and dead helpers do
  not pass.
- `national_runtime_probe.py` imports candidate modules only in a resource-
  limited subprocess, measures declared table size, replays a 70-hand tracker
  lifecycle, verifies connection reset and bounded confidence, and runs
  low/high-adaptation metamorphic decisions. Static and dynamic evidence must
  agree.
- `runtime_architecture_policy.py` preserves every capability already present
  in the parent and selects one complete unresolved architecture bundle for the
  generation. A candidate may not trade away an existing capability.
- Every single-parent and crossover generation freezes the complete prepared
  candidate manifest and hash before Direction Audit/Master. Crossover adds a
  richer Parent-A/Parent-B provenance and capability receipt, but it does not
  get a weaker baseline. Final quality compares regular-file bytes from this
  common prepared boundary to the final candidate; source-to-child Python diff
  is telemetry only.
- Crossover is a preparation operator, not an alternate fast path around
  planning. Its preplan transition must preserve parent capabilities and prove
  the system-owned native wrapper, while source debt listed in
  `plan_required_floor_checks` remains explicitly deferred. A successful child
  enters `prepared`, then follows direction audit, optional literature probe,
  Master, Workers, and the full final transition gate.
- The accepted crossover child is frozen as a digest-bound prepared-baseline
  contract: exact artifact/code hashes, component provenance, Python LOC,
  frozen H2H identity, and the child capability snapshot. Master computes debt,
  runtime feedback, and line budget from that child. Final quality preserves
  `Parent A passed ∪ prepared child acquired`; stale identity fails closed and
  never resets the child to Parent A while retaining crossover lineage. Final
  quality also requires a post-Master artifact delta from this frozen baseline;
  the Parent-A crossover diff cannot hide a no-op Worker generation.
- Crossover provenance is also deterministic: an exact Parent-B module passes;
  a composed Python file may add only symbol-bound glue rooted in a Parent-B
  import/definition. The gate covers the complete artifact manifest, so a
  non-Python table/model/config must be exact Parent-B bytes and cannot smuggle
  an independently generated policy into crossover. A threshold-only change,
  novel heuristic, arbitrary call, or unrelated file is rejected before
  Master. If no component is safely portable, an unchanged Parent-A baseline
  is valid and Master owns innovation.
- Scheduler lineage is an execution contract, not prompt advice:
  `run_crossover` must match the checkpoint's exact Parent-A, Parent-B, and
  target tuple. A bounded infrastructure retry binds all three complete parent/
  child artifact hashes and retains its ledger until the `prepared` checkpoint
  is atomically published; artifact drift abandons the generation.
- Dynamic decision evidence separates killability from strategy latency.
  Runtime-version mismatch, missing socket fallback, failed worker-tree kill,
  or failed next-decision restart can fail `killable_decision_runtime`; a
  baseline over 250ms fails `fast_strategy_baseline` instead and cannot
  masquerade as a worker-termination defect.
- Master emits a structured `RuntimeContract`; worker target/owner files must
  cover it; reviewer compares it with detector evidence; quality gates rerun
  the detector against parent and candidate.
- Scheduler-produced stagnation/match/performance evidence is persisted as a
  digest-bound Master context.  The outer orchestrator may transport or display
  it but cannot rewrite it into planning instructions.  Persisted direction
  audit and literature-probe results likewise override caller paraphrases.
- When canonical stagnation or repetition requires research, the state machine
  routes only to `run_literature_probe`; `run_master` refuses to run without an
  identity-bound receipt carrying the exact Master-context digest, Direction-Audit digest, and
  requirement-context digest. A governed skip, timeout, or provider failure
  counts as a receipt only for that exact context, so stale research cannot be
  replayed while infrastructure cannot cause an unbounded orchestration loop.
- On the initial `master_planned` worker pass, checkpoint tasks are the sole
  execution authority.  A non-empty caller task list must be structurally
  identical; `tasks=[]` loads a defensive copy.  The checkpoint and plan
  runtime ledgers must validate and rebuild to the same digest before any
  Worker LLM can edit code.
- Rework feedback and tasks are also checkpoint-owned. Caller-supplied values
  are transport echoes only and must exactly match the gate-derived canonical
  repair contract; `must_change_files` is a completion assertion and never adds
  write authority beyond `target_files/files_allowed`. Each Worker batch is an
  all-or-nothing transaction over the complete artifact, including binary
  tables. A partial success followed by any Worker/boundary/cleanup failure is
  restored byte-for-byte before recovery is checkpointed.
- Worker and publication manifests fail closed above 1,024 regular files,
  2,048 entries, depth 64, 16 MiB per file, or 64 MiB total, and validate all
  metadata before reading payloads. Transient `.task_context`, bytecode, and
  test caches are removed and cannot be referenced by candidate code.
- Quality/official repair checkpoints bind the exact pre-repair artifact hash;
  same-file out-of-band edits cannot piggyback on an otherwise legal repair
  scope. Publication then compares worktree bytes to staged Git blobs and the
  immutable completion-tag tree. Ignored files, empty directories, nested Git
  repositories/gitlinks, and tag/worktree hash drift are rejected.
- Candidate imports and live strategy workers run under memory/file/descriptor/
  process resource limits. Import-time external I/O and module-level allocation
  bombs therefore cannot consume the long-running orchestrator even before the
  static contract reports them.
- The official EXE remains a protocol/compliance oracle only. Runtime
  architecture and poker strength are evaluated by local native harnesses and
  H2H evidence, never by EXE chip outcomes.
- One strength sample is one complete 70-hand local native TCP match. The sign
  of final net chips is authoritative for the primary result: positive is a
  win, negative is a loss, and zero is a draw. Outcome-derived
  Glicko/H2H/`selection_score` ranks bots first; net-chip magnitude is retained
  only as a secondary tie-breaker. Arena and official EXE earnings never enter
  this ordering.

## Evidence And LLM Boundary

The official harness follows the subject-binding principle used by the
[in-toto Attestation Framework](https://github.com/in-toto/attestation/blob/main/spec/README.md)
and [SLSA provenance](https://slsa.dev/spec/v1.0/provenance): a claim identifies
and hashes the exact subject artifact, then a tracked trust policy verifies the
signature. A certificate for one `national_vN` label cannot be republished for
another label even when the source bytes match; migration readiness also counts
unique artifact hashes rather than directory copies.

LLM analysis stays outside that authority boundary. Published studies of LLM
judges document position, verbosity, self-enhancement, and other systematic
biases ([MT-Bench/Chatbot Arena](https://arxiv.org/abs/2306.05685),
[CALM](https://arxiv.org/abs/2410.02736)). The official analyst must cite an
allowlisted deterministic evidence ID and may only explain a failure or propose
a bounded repair. A clean deterministic pass clears all LLM repair feedback.
No LLM field can pass, fail, revoke, certify, rate, or tune a bot.

## Measured Follow-ups

The current release intentionally leaves four evidence-driven improvements for
later rather than pretending they are solved by a larger prompt:

1. Local strength selection runs at roughly 2.0/1.8 seconds while the formal
   runtime permits 55/54 seconds. Protocol/runtime probes validate long-budget
   safety, but do not directly measure the marginal poker value after two
   seconds. Add a sampled multi-fidelity native gate (short control plus a small
   long-budget stratum) before rewarding expensive refinement across every
   generation.
2. Admit large exact equity/abstraction data only through a system-owned,
   immutable packed loader with packaging and live-consumer proof.
3. Move compiled Worker briefs out of the candidate tree. They are excluded
   from identity, do not grant scope, and are hard-cleaned today, but an external
   read-only control directory would make the ownership boundary simpler.
4. Revisit the uncapped `1/(1+children)` parent-diversity penalty with replay/H2H
   evidence. A capped penalty can preserve exploration without ranking a weak,
   unused parent above a repeatedly successful source.

## Non-Goals

- Do not allocate the whole 60 seconds merely because it is available.
- Do not generate enormous tables without an explicit bound and live consumer.
- A future large exact table must use a hash-pinned, system-owned read-only
  loader; candidate-side packed/mmap file access is not currently admitted.
  Python object expansion of a multi-million-entry table is not an acceptable
  way to spend memory.
- Do not infer a reliable opponent type from a few actions.
- Do not run a full CFR/ReBeL solver inside a generated stdlib bot unless a
  separately benchmarked engine and submission budget make that feasible.
