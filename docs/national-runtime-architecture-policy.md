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
7. Logs, file reads, network calls, and subprocess work stay outside the live
   decision call graph.

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
- Master emits a structured `RuntimeContract`; worker target/owner files must
  cover it; reviewer compares it with detector evidence; quality gates rerun
  the detector against parent and candidate.
- Scheduler-produced stagnation/match/performance evidence is persisted as a
  digest-bound Master context.  The outer orchestrator may transport or display
  it but cannot rewrite it into planning instructions.  Persisted direction
  audit and literature-probe results likewise override caller paraphrases.
- On the initial `master_planned` worker pass, checkpoint tasks are the sole
  execution authority.  A non-empty caller task list must be structurally
  identical; `tasks=[]` loads a defensive copy.  The checkpoint and plan
  runtime ledgers must validate and rebuild to the same digest before any
  Worker LLM can edit code.
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

## Non-Goals

- Do not allocate the whole 60 seconds merely because it is available.
- Do not generate enormous tables without an explicit bound and live consumer.
- Do not infer a reliable opponent type from a few actions.
- Do not run a full CFR/ReBeL solver inside a generated stdlib bot unless a
  separately benchmarked engine and submission budget make that feasible.
