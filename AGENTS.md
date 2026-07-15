# AGENTS.md — National TCP Poker Evolution

This is the working contract for coding agents in this repository. The sole
active poker-bot architecture is `national_tcp_policy_v1`, built directly on
the national competition raw TCP protocol.

## Trust boundary

Active code consists of:

1. `sever/` — national rules, validator, TCP server, THP output, and diagnostic
   web surface.
2. `web/` — evolution control plane, native TCP evaluation, immutable evidence,
   prompts, gates, certification, and dashboard.
3. `bots/national_v<N>/` — strict policy artifacts created by the active epoch.
4. `scripts/` — national diagnostics, evaluation identity, and official EXE
   certification.

`archive/` contains retired protocol engines, adapters, bots, experiments,
tests, prompts, runtime output, and documentation. Archived files are
`legacy-untrusted`. Active code must never import, execute, dynamically load,
scan, copy, branch from, cross over, certify, rate, or summarize them. Never add
an archive directory to `sys.path` or `PYTHONPATH`.

The historical completion-tag high-water may be preserved when assigning the
next version number. That is identity continuity only. It does not carry source
bytes, ratings, H2H, experience, capabilities, or certification into this
epoch.

Only annotated completion/high-water tags advance that namespace. An untracked
directory, abandoned checkpoint, log filename, or runtime counter never does.
During the one-time epoch reset, any untagged `national_v143+` directory is
archived as stale unpublished debris together with its checkpoint; for example,
an old-wrapper `national_v155` directory does not move the first strict label
past `national_v143`.

The first strict checkpoint must bind the schema-2 execute receipt from the
stopped autonomous checkout via
`scripts/reset_national_tcp_policy_epoch.py --execute --acknowledge-runtime-checkout`.
The command is rejected in the outer operator checkout. Dry-run receipts,
pre-binding checkpoints, and a second/interrupted reset attempt are not
resumable.

## Repository map

```text
.
├── bots/                         # active strict-policy candidates only
├── sever/
│   ├── 国赛平台/                  # original competition documents/platform
│   ├── engine/                   # national rules, evaluator, validator, THP
│   ├── server/                   # raw TCP codec and asyncio server
│   └── web/                      # diagnostic SSE dashboard
├── web/
│   ├── core/                     # evolution/evaluation/certification
│   ├── server/                   # FastAPI backend
│   ├── frontend/                 # React dashboard
│   └── tests/
├── scripts/                      # national and official-platform tools
├── docs/                         # current architecture and oracle documents
└── archive/                      # immutable retired history; zero authority
```

The old root `engine/`, adapter, decision tester, smoke/probe/QD facilities,
RL tree, neural lab, and mixed-ABI bot epoch are not active components.

## Dual-checkout runtime

- `/home/zzx/project/pok` is the operator/infrastructure checkout. Develop code,
  tests, prompts, and docs here or in a temporary ignored worktree.
- `/home/zzx/project/pok/.evolution_pok` is the long-running autonomous runtime
  checkout. Candidate directories, checkpoints, ratings, and live result files
  belong there.

Synchronize only through `origin/main`; never copy files between checkouts.
Before work, update remote state. In a clean editable checkout use:

```bash
git pull --ff-only --tags
```

If dirty, on a user branch, or not safely fast-forwardable, use
`git fetch --tags origin` and create a temporary worktree from updated
`origin/main`. Do not switch branches, reset, or develop infrastructure inside
`.evolution_pok` while a generation runs.

Restart decisions are governed by the exact active-stage contract in
`web/core/evaluation_contract.py`, not broad directory names. See
`docs/evolution-dual-checkout-sync-policy.md`.

## National TCP protocol

- Platform is TCP server; each AI is a client. Default port is `10001`.
- One match is 70 independent hands. Each hand starts each player at 20000
  chips, with blinds 50/100.
- Each decision has a 60 second official limit; timeout folds.
- Client actions are raw strings with no delimiter: `raise <amount>`, `fold`,
  `call`, `check`, `allin`. Never append `\n` or `\r\n`.
- TCP recv boundaries are not message boundaries. Sticky data such as
  `earnChips -100preflop|...` must be split by the system decoder.
- The formal runtime retains the official-safe action-send delay. Local
  strength runs may set the documented local delay override to zero.
- `raise X` means raise to total street contribution `X`. Exact `raise 400`
  following `raise 200` is accepted by the official EXE. Conservative headroom
  is strategy policy, not protocol legality.
- Postflop opening `call` is illegal. After a first postflop action, `check` is
  illegal. When the first player checks, the second closes the street with
  `call`.
- After a called all-in, clients receive runout/settlement only and must not
  act again before the next hand.
- TCP cards use `<suit,rank>`, with suit 0=Spade, 1=Heart, 2=Diamond, 3=Club and
  rank 0=2 through 12=Ace.
- `earnChips` is the receiving seat's signed per-hand net. `oppo_hands` appears
  only at showdown.

The official EXE can suppress a street-closing peer call/check and jump to the
next street or settlement. The runtime may infer only the unique action proven
by that boundary. It must apply the inferred contribution before clearing
street bets, so pot, stacks, SPR, odds, sizing, and range weights stay correct.
Terminal peer fold/call and showdown cards must update the connection-lived
opponent tracker before the next hand.

`sever/engine/game.py` deliberately mirrors that proven wire omission, rather
than relaying easier local-only terminal tokens. It also keeps the authoritative
internal/THP result while omitting the natural hand-70 wire settlement below.

At natural hand 70 the 2021 EXE omits the last `earnChips` pair. Formal v5
certification cross-binds wire settlements for hands 1..69 to THP states 0..68,
then uses strict THP state 69 and the footer as independent final proof.

These exact oracle files are always-critical evaluation inputs and their hashes
are pinned by `runtime_architecture_policy.py`:

- `docs/official-raise-boundary-oracle-2026-07-11.md`
- `docs/official-terminal-settlement-oracle-2026-07-11.md`

Do not edit or reinterpret them casually. Control-plane changes verify their
hashes; they do not rerun the official EXE.

## Strict candidate ABI

Every active bot is exactly five artifact files: system-owned
`national_bot.py` and `precompute.py`, candidate-owned `policy.py`, plus
`national_runtime_manifest.json` and `policy_epoch_receipt.json`. Candidate
helpers and candidate-owned assets are not part of this ABI.

Candidate policy receives a schema-versioned `decision_context` containing
authoritative public state, legality, pot/stacks/contributions, opponent
tracker snapshot, and time budget. It returns a typed intent only:

- `fold`
- `pass`
- `allin`
- `raise` with integer `raise_to`

The system runtime maps `pass` to legal wire `call` or `check`, validates
`raise_to`, applies fallback, throttles, and owns the single socket send path.
Candidate code must not:

- parse TCP, retain raw socket bytes, or send wire tokens;
- reconstruct a parallel request/response history;
- return integer/string actions or direct `call`/`check` intents;
- perform filesystem, network, subprocess, or external import-time I/O;
- scan the full hand history during each decision;
- access any file under `archive/`.

Managed launches that declare a host process owner use a one-shot Bubblewrap
`--block-fd` start barrier. Before release, the host must observe exactly the
single owner marker in `/proc/<pid>/environ`; only the observed transient empty
Bubblewrap setup window may be retried for a short bounded interval. Any other
value, timeout, read failure, or release failure terminates and reaps the
process before returning. The owner marker is never injected into the sandbox,
and launches without an owner do not acquire this barrier.

The runtime computes an always-legal fallback before candidate work. It targets
a 250 ms policy baseline, allows bounded refinement through 54 seconds, and
returns by a 55 second hard deadline, reserving the remaining official minute
for sanitization, scheduling jitter, send throttle, and logging. Late worker
results cannot reach the socket.

`OpponentTracker` persists for one TCP connection and resets at connection
start. It incrementally records hand starts, both players' actions, inferred
boundary closures, terminal response outcomes, settlements, and showdown range
evidence. Adaptation is confidence-weighted and capped; sparse samples stay
near the baseline.

Every LLM role has a resolved-path read capability supplied by the system.
Fresh v143 roles may read only the prepared v143 artifact; normal planning and
review roles may read only the exact current source, target, and frozen
generation snapshot assigned to them; Workers may read only their lease
candidate. `.git`, any archive path, unlisted bots, other live results,
operator delivery documents, symlinks, parent aliases, globs, shell/Python
wrappers, and indirect configuration-file reads are denied. Dynamic candidate
execution belongs to system quality gates; Workers get bounded inspection and
exact-file `py_compile` only.

Each Agent SDK attempt owns its exact subprocess transport. A timeout or
cancel-resistant stream must close that transport and prove both the original
process and pending stream tasks exited before schema, signature, overload, or
cycle retry. An unresolved owned attempt is an infrastructure failure and
blocks further provider dispatch; the runtime never kills a process whose
ownership it cannot prove.

## Space-for-time assets

Compact system-owned import-time facts are allowed and measured: 1,326 hole
combinations, 8,192 rank masks, and 21 five-of-seven selections. System
precompute must have a bounded size, content-bound manifest, live decision
consumer, and legal empty-table fallback.

Do not add a giant Python dictionary merely because memory is available.
File-backed packed/mmap equity or blueprint assets require a system-owned
immutable loader, submission compatibility, hash/key/encoding contract, build
and byte limits, and measured decision influence. Candidate file I/O remains
forbidden.

## Evolution system

Active implementation is under `web/core/`. Major responsibilities include:

- `epoch_authority.py`, `checkpoint_schema.py` — canonical version/reset state
  and fail-closed durable checkpoint identity; UI, scheduler, and recovery must
  not recompute these from directory names or retired runtime files;
- `generation_scheduler.py` — prepare and cleanup scheduling;
- `evaluation_bundle.py`, `evidence_snapshot.py`, `rating_snapshot.py` — frozen
  evaluation publication and generation cutoffs;
- `master_context_contract.py`, `plan_compiler.py`,
  `strategy_reference_pack.py` — typed, digest-bound planning evidence;
- `workflow_kernel.py`, `worker_workflow.py` — Worker journal, fenced effects,
  immutable artifacts, crash-safe projection;
- `national_native.py`, `national_game_runtime.py`, and
  `sever/server/transport.py` — strict raw TCP runtime with one shared stream
  parser;
- `national_capability_contract.py`, `national_runtime_probe.py` — static and
  dynamic policy-ABI enforcement;
- `elo_daemon.py` — internal native-match scheduling and immutable evaluation-cycle publication;
- `tool_gates.py`, `tool_eval.py`, `tool_commit.py` — quality, precommit, signed
  publication;
- `post_publication_handoff.py`, `cycle_archivist.py` — publication-linearized,
  crash-safe post-publication journal and immutable archive annotation;
- `stability_observation.py` — operator-only uninterrupted-delivery acceptance;
  zero strategy/strength weight.

Generation order:

1. prepare single-parent artifact or crossover baseline;
2. direction audit;
3. governed literature probe when required;
4. Master selects one of three proposals after two anonymous ballots;
5. Workers implement the compiled, checkpoint-owned contract;
6. quality gates;
7. review;
8. advisory schema-valid critic;
9. native TCP precommit regression;
10. signed official EXE full certification;
11. commit and annotated `national-bot-v<N>` tag;
12. archivist/cleanup.

Crossover is preparation only and never skips planning or gates. Every prepared
artifact has a complete manifest/hash. Worker writes are lease-isolated,
snapshotted, and atomic. Publication cross-checks working bytes, staged Git
blobs, and immutable tag tree.

The first-strict authority journal freezes one checkpoint revision for all six
Master slots at the first durable provider effect. Later checkpoint metadata or
infrastructure-overlay revisions may only move forward; accepted slots replay,
missing slots consume their original bounded schema budget, and ballots/final
remain on that frozen phase revision. The journal must have one internally
consistent generation/stage/role/input binding, one context binding per slot,
and one phase revision; mixed revisions, rollback, same-slot context drift, or a
new workflow fail closed. Proposal, ballot, Reviewer, and Critic execution
evidence additionally binds the accepted effect's provider-visible prompt,
terminal output, result/usage identity, role projection, and exact append-only
role log. Each call owns exactly
`RESULTS_DIR/v<N>/logs/strict_invocations/<invocation_id>/<role>_io.txt`;
the generation binding derives `N`, so a flat, foreign-version, or arbitrary
log root cannot become evidence. Backend log reads expose these files only
through a validated opaque invocation id and a no-follow descriptor walk from
`RESULTS_DIR`; the frontend never reconstructs a filesystem path. A crash
between acceptance and evidence binding may append or reuse exactly one
matching evidence trailer; a missing/empty/non-regular log, duplicate trailer,
mismatch, or later byte drift is a control-plane failure.

First-strict Reviewer and Critic prompts render only from their durable call
descriptors, which bind the exact semantic inputs plus checked-in
producer/template identities. The Critic descriptor also owns its evidence read
scope. Because the v143 pool is empty, that scope is empty and its prompt carries
an explicit no-strength contract; it must not open rating, H2H, replay, Arena,
official, retired-bot, or historical-experience material. Any strict journal,
prompt, context, or invocation-evidence violation canonically abandons the
generation with zero provider-infrastructure retry debt.

Publishing does not authorize the next generation by itself. Before the
publishing checkpoint is cleared, the publication lock creates and fsyncs an
exact schema-2 post-publication handoff plus its archive base snapshot. The
handoff then owns eight ordered steps: `stability_observation`, `reap_signal`,
`priority_eval`, `archive_rotation`, `log_cleanup`, `pool_reap`,
`cycle_annotation`, and `housekeeping`. Every step has an exact-key,
content-bound plan and output receipt; a re-signed alternate shape is invalid.
Crash recovery resumes the same publication/workflow identity and never skips
a completed-looking step merely because its receipt digest is syntactically
valid.

Final handoff completion reopens the operational stability row, reissues the
exact daemon refresh and priority capabilities, and independently re-proves
rotation archives, strict-log archives, reap tombstones, Cycle Archivist
annotation, Git HEAD, and clean worktree. Archive rotation first freezes one
high-level plan for every managed append-only source and preserves live source
bytes. Strict-generation log archival is non-destructive: it emits immutable
archives/manifests while retaining the live log tree and every generation
sibling. Pool reaping is a schema-2 frozen selection snapshot and target
sequence, including the zero-target case; it cannot recompute victims after a
crash. Signal producers and daemon consumers share the same stable sidecar
lock, so publish/read/unlink cannot race. A missing, corrupt, ambiguous, or
unreprovable handoff is an active launch barrier.

Generation abandonment is a publication-linearized schema-2 transaction, not
directory cleanup. Its transaction id binds the exact checkpoint CAS identity,
reason, candidate manifest, fixed quarantine contract, abandon-ledger prefix and
Git state. After both the transaction claim and live launch barrier are durable,
the outer Worker journal is terminally fenced and the strict-authority child
gets an `abandoned` tombstone even when no provider effect has yet been
dispatched. Real and replay dispatch both require a running child journal, so a
stale descriptor cannot recreate a child after abandonment. The runtime then
must revalidate those complete live facts before appending the
irreversible abandon receipt. It then atomically moves only the claim-bound,
untracked and unpublished candidate into the transaction quarantine, syncs both
parents, clears only the exact checkpoint by CAS, writes the terminal receipt,
and finally clears the live claim. Any active claim, valid or corrupt, makes
epoch initialization false and exposes no active bots. A completed historical
receipt remains valid after later legitimate commits and ledger rows because it
binds its original prefix and exact successor row; it never adopts later bytes.

The operator stability projection reaches 10/10 only for ten consecutive
fully published generations under one web process, one live rating-daemon
identity, one effective runtime-configuration digest, and one evaluation-contract
hash, with no repair, abandonment, version gap, configuration change, restart,
incomplete publication, or authority drift. Its HTTP projection is served only
from a coalesced background verification snapshot; pending, expired, or failed
verification suppresses N/10. Every row binds workflow/gate/certificate/tag/tree/remote
main, the selected source and frozen cycle/cutoffs; final completion also
requires the latest bot in the current strict cycle with an admitted complete
70-hand native sample. The projection is never prompt, selection, rating, or
strategy evidence.

Backend HTTP and SSE projections bracket the canonical epoch, post-publication
handoff, and stability identities. A changed sample is withheld rather than
combined across revisions. The frontend consumes those typed identities,
rejects stale/out-of-order epoch or handoff events, clears state after stream
loss, and displays `pending`, `running`, or `blocked` without deriving
authority from bot directories or local component state. An independently
fetched pipeline checkpoint is rendered only when its schema-2 positive
`checkpoint_revision` and full epoch/version/stage/run/workflow identity match
the paired active-generation projection; a same-stage older revision is stale.
Critic `approved` means the advisory role completed, while
`advisory_approved` is the actual non-authoritative recommendation; UI text must
never substitute one for the other. `daemon_enabled=false` is a supported
runtime mode: an absent daemon PID is `not_applicable`, while a live disabled
daemon or an enabled-but-missing daemon remains unhealthy.

## Evidence authority

One strength sample is one complete 70-hand raw native TCP match. Win/loss/draw
is the sign of final net chips. Net magnitude is only a secondary tie-breaker.
Glicko/H2H/selection rows are published as one immutable content-addressed
cycle, then copied into a generation evidence snapshot. Match-history cutoffs
and deterministic replay-spotlight text/citations are frozen in that same
snapshot with source replay hashes; Master and citation gates never reopen live
replay files or a process-global spotlight manifest.

Official EXE results and Arena results have zero strength weight. Archived
ratings, H2H, replays, action stats, experience, exhausted directions,
spotlights, failure summaries, neural reports, and local-engine output have zero
authority and must not be injected. There is no active free-standing lesson or
experience store. Any future lesson facility must first bind the exact active
bot artifact, complete replay, parser/runtime identity, evaluation cycle, and
derivation digest through a frozen producer-to-consumer contract.

## Commands

```bash
# Web application
python web/main.py
python web/main.py --view-only
python web/main.py --no-daemon

# Evolution CLI / rating daemon
python web/core/orchestrator.py --one-gen
python web/core/elo_daemon.py --once

# Tests
python -m pytest sever/tests -q
cd web && python -m pytest tests -q
cd web/frontend && npm test && npm run lint && npm run build

# National TCP platform
cd sever && python main.py

# Diagnostic Arena only
python scripts/national_arena.py serve --view-only

# Official acceptance and required certification
python scripts/official_platform_acceptance.py \
  --candidate bots/national_v<N> --opponent bots/national_v<M> \
  --self-play-rounds 1 --opponent-rounds 1 --target-hands 70
python scripts/official_certify.py full bots/national_v<N> --wait-if-busy

# One-time empty-pool bootstrap for the first strict bot only
python scripts/official_certify.py bootstrap-first-strict bots/national_v143 \
  --control-id first_strict_control_v1 \
  --acknowledge-one-time-first-strict-control --wait-if-busy

# Only after the jobs API projects ready_to_finalize for that exact certificate
python scripts/official_certify.py finalize-first-strict \
  --acknowledge-publish-first-strict
```

Normal certification is five 70-hand self-play rounds plus three 70-hand rounds
against an eligible strict-policy opponent. The v143-only system-control
bootstrap and finalize steps are operator-only, zero-strength, and never an
automatic fallback. The LLM/HTTP control plane can perform neither step.
The archived v141 signed-ledger chain is validation history and is not executable.

## Working rules

- Search with `rg`/`rg --files` first.
- Use `apply_patch` for hand edits; preserve unrelated dirty changes.
- Never reset, checkout, or delete user work to obtain a clean tree.
- Keep bot/runtime code stdlib-only unless an existing system boundary says
  otherwise.
- Test in proportion to risk: compile touched Python, run focused tests, then
  the relevant native protocol/evolution shards.
- `web/main.py` is a web launcher, not a TUI or mode-switching CLI.
- Generated frontend output is ignored; do not treat it as source.
- The highest numbered bot directory is not completion proof. Require current
  epoch artifact metadata, `.completed`, annotated completion tag, and the
  role-specific certificate.
