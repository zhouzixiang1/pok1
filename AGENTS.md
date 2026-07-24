# AGENTS.md — National TCP Poker Evolution

This is the working contract for coding agents in this repository. The sole
active poker-bot architecture is `national_tcp_policy_v1`, built directly on
the national competition raw TCP protocol.

> **Branch note (`tencent-cloud-runtime`).** This branch is an **isolated
> cloud evolution line** that restarts version numbering from **1** under the
> `national_cloud_v` namespace, runs on Tencent Cloud as the systemd service
> `pok-evolution.service`, and publishes only into
> `origin/tencent-cloud-runtime` — its products never enter `origin/main`.
> The text below describes this cloud line; the `main` branch keeps the
> canonical `national_v` / 142→143 history unchanged and is intentionally not
> disturbed. Namespace, version-floor, path, LLM, and signer details that
> differ from main are called out inline.

## Trust boundary

Active code consists of:

1. `sever/` — national rules, validator, TCP server, THP output, and diagnostic
   web surface.
2. `web/` — evolution control plane, native TCP evaluation, immutable evidence,
   prompts, gates, certification, and dashboard.
3. `bots/national_cloud_v<N>/` — strict policy artifacts created by the active
   epoch (on this branch the namespace is `national_cloud_v`; `bots/` is empty
   until the first cloud candidate `national_cloud_v1` is published).
4. `scripts/` — national diagnostics, evaluation identity, and official EXE
   certification.

`archive/` contains retired protocol engines, adapters, bots, experiments,
tests, prompts, runtime output, and documentation. Archived files are
`legacy-untrusted`. Active code must never import, execute, dynamically load,
scan, copy, branch from, cross over, certify, rate, or summarize them. Never add
an archive directory to `sys.path` or `PYTHONPATH`.

The version-authority high-water on this branch is **0**
(`ARCHIVED_VERSION_HIGH_WATER = 0` in `web/core/bot_namespace.py`), so the first
strict target is `national_cloud_v1` (`FIRST_STRICT_POLICY_VERSION = 1`). No
main-namespace version history (142/143/156) is carried into this epoch — that
is identity continuity only and it carries no source bytes, ratings, H2H,
experience, capabilities, or certification. Legacy main-namespace bots
(`national_v143`, `national_v156`) inherited from `main` are archived under
`archive/legacy_main_namespace_bots/` and ignored by the cloud epoch authority
(`active_bots = []`, `version_authority_high_water = 0`).

Only annotated completion/high-water tags in the **active namespace**
(`national-cloud-bot-v*` / `national-cloud-high-water-v*`) advance that
namespace. An untracked directory, abandoned checkpoint, log filename, or
runtime counter never does. A fresh cloud checkout has no paired cloud tags,
so `resolve_version_namespace_authority` falls back to the archived high-water
(0) and the epoch initializes via the `fresh_bootstrap_ready` path — no seed
tag is required for the version-1 floor.

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

- `/home/ubuntu/pok1` is the operator/infrastructure checkout. Develop code,
  tests, prompts, and docs here or in a temporary ignored worktree.
- `/home/ubuntu/pok1/.evolution_pok` is the long-running autonomous runtime
  checkout. Candidate directories, checkpoints, ratings, and live result files
  belong there. Its directory name (`.evolution_pok`) together with
  `POK_CLOUD_RUNTIME=1` triggers the namespace seed block in `web/main.py`.

Synchronize only through `origin/tencent-cloud-runtime`; never copy files
between checkouts. The `main` branch remains canonical for the `national_v`
line and is intentionally not disturbed. Before work, update remote state. In a
clean editable checkout use:

```bash
git pull --ff-only --tags
```

If dirty, on a user branch, or not safely fast-forwardable, use
`git fetch --tags origin` and create a temporary worktree from updated
`origin/tencent-cloud-runtime`. Do not switch branches, reset, or develop
infrastructure inside `.evolution_pok` while a generation runs.

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
- After a called all-in, clients must not act again before the next hand. The
  2021 EXE may omit every not-yet-sent public street and jump directly to
  settlement/`oppo_hands`; the system must not fabricate the unseen board.
  Formal replay requires complementary cross-wire actions and exact all-in net
  settlement, then binds every omitted hand to a strict THP board that is
  either the exact observed wire prefix (0/3/4 cards) or a complete five-card
  board, plus the THP terminal action, blind/name order, revealed holes, and
  earnings. During live causal capture only a same-connection raw action
  awaiting its bounded flush
  may make `street_boundary_unproved` provisional; finalized replay remains
  strict and never invents a missing card or action.
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
than relaying easier local-only terminal tokens or a called-all-in future board.
It keeps the complete board as authoritative internal/THP state while omitting
those future street messages, and also omits the natural hand-70 wire settlement
below.

At natural hand 70 the 2021 EXE omits the last `earnChips` pair. Formal v5
certification cross-binds wire settlements for hands 1..69 to THP states 0..68,
then uses strict THP state 69 and the footer as independent final proof.

These exact oracle files are always-critical evaluation inputs and their hashes
are pinned by `runtime_architecture_policy.py`:

- `docs/official-raise-boundary-oracle-2026-07-11.md`
- `docs/official-terminal-settlement-oracle-2026-07-11.md`
- `docs/official-allin-runout-wire-oracle-2026-07-19.md`

Do not edit or reinterpret them casually. Control-plane changes verify their
hashes; they do not rerun the official EXE.

## Strict candidate ABI

Every active Bot directory contains exactly five executable/identity files: system-owned
`national_bot.py` and `precompute.py`, candidate-owned `policy.py`, plus
`national_runtime_manifest.json` and `policy_epoch_receipt.json`. Candidate
helpers and candidate-owned assets are not part of this ABI. This is not a
blanket prohibition on compact tables or models: a file-backed table/model may
exist only outside the bot directory as a separately versioned, system-owned
asset. It must have a registry/issuance receipt and content-bound manifest,
bounded bytes and queries, no-follow read-only verification, a system broker
with nonce/quota-bound access, one resolver used by every native/precommit/
probe/Arena/official launch path, and an observed decision-influence probe.
Until that complete asset ABI is implemented and admitted, candidate policy has
no file-backed asset access. Candidate code must never load an arbitrary path
or own the asset bytes.

Candidate policy receives a schema-versioned `decision_context` containing
authoritative public state, legality, pot/stacks/contributions, opponent
tracker snapshot, and time budget. It returns a typed intent only:

- `fold`
- `pass`
- `allin`
- `raise` with integer `raise_to`

Runtime v10 additionally publishes a system-derived
`hand.match_control` proof and
`betting.call_closes_allin_runout`. Policy may lock a match by folding only
when every match-control field is internally consistent and
`fold_locks_win` is strictly true; equality is not a win. The all-in closure
boolean is authoritative over action-text heuristics. Missing, malformed, or
contradictory values are neutral/fail-closed.

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
Fresh first-strict roles (binding `national_cloud_v1`) may read only the
prepared `national_cloud_v1` artifact; normal planning and review roles may
read only the exact current source, target, and frozen generation snapshot
assigned to them; Workers may read only their lease candidate. `.git`, any
archive path, unlisted bots, other live results, operator delivery documents,
symlinks, parent aliases, globs, shell/Python wrappers, and indirect
configuration-file reads are denied. Dynamic candidate execution belongs to
system quality gates; Workers get bounded inspection and exact-file
`py_compile` only.

Each Agent SDK attempt owns its exact subprocess transport. A timeout or
cancel-resistant stream must close that transport and prove both the original
process and pending stream tasks exited before schema, signature, overload, or
cycle retry. An unresolved owned attempt is an infrastructure failure and
blocks further provider dispatch; the runtime never kills a process whose
ownership it cannot prove.

### LLM provider and extended thinking (cloud runtime)

The cloud runtime drives every Master/Reviewer/Critic/Worker role through
`claude_agent_sdk` (latest stable `claude-agent-sdk`, currently 0.2.126) against
**GLM-5.2** via the Anthropic-compatible endpoint
(`ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic`, model id
`glm-5.2`; all of Haiku/Sonnet/Opus route to `glm-5.2`). Extended thinking is
configured in `web/core/llm_query.py::_llm_thinking_options` and applied to
every direct sub-agent dispatch:

- `thinking = {"type": "enabled", "budget_tokens": 32000}` — GLM treats the
  budget as a soft target, so deep reasoning is preserved while the model still
  converges and emits a detailed answer. The legacy `{"type": "adaptive"}` mode
  is **known to hang on GLM**: it emits 16k–19k+ thinking tokens without ever
  producing visible output, exhausting the productive-silence ceiling and
  abandoning the generation at the Master proposal stage.
- `effort` — **intentionally unset.** `effort=max` is harmful on this endpoint:
  it drives GLM into an infinite thinking loop (64k+ thinking tokens, zero text
  output) on complex Master-proposal prompts, regardless of `budget_tokens`.
  Leaving it unset lets GLM use its default reasoning depth, which converges
  reliably. Set `POK_LLM_EFFORT` only if a future GLM build fixes the loop.

All three are environment-overridable: `POK_LLM_THINKING_MODE`
(`enabled`/`adaptive`/`disabled`, default `enabled`),
`POK_LLM_THINKING_BUDGET` (default `32000`), `POK_LLM_EFFORT` (default unset).
The committed defaults live in `deploy/tencent-cloud/env.runtime`.

Because GLM-5.2 with `effort=max` + a large budget routinely spends 250–320s
thinking before emitting visible text on complex Master-proposal prompts, the
default `MASTER_PROPOSAL` role timeouts in `web/core/llm_query.py`
(`first_activity=120s`, `stall=360s`, `idle=360s`, `total=900s`) are too tight.
The cloud runtime raises them via role-scoped env overrides
(`POK_LLM_MASTER_PROPOSAL_STALL_TIMEOUT=600`,
`POK_LLM_MASTER_PROPOSAL_IDLE_TIMEOUT=600`,
`POK_LLM_MASTER_PROPOSAL_TOTAL_TIMEOUT=1200`). The `stall` gate (productive-
message silence) is the one that fires first and must be raised, not just
`idle`.

## Space-for-time assets

Compact system-owned import-time facts are allowed and measured: 1,326 hole
combinations, a calibrated 169-class heads-up preflop equity table, 8,192 rank
masks, and 21 five-of-seven selections. The table's fixed-seed producer binds
the official evaluator/Card sources and the exact CPython RNG build identity;
the producer is an evaluation-contract-critical path. System
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
- `runtime_architecture_policy.py` — architecture policy identity and gate.
  The frozen `source_capability_digest` binds the source bot's **identity**,
  which must be a pure content-addressable function of its static AST-contract
  capabilities. Both the planner (`_build_generation_architecture_policy`) and
  the gate (`evaluate_architecture_transition`) feed static capabilities into
  `build_architecture_policy` for the source anchor, so a frozen policy can
  always match a freshly recomputed gate value. The typed runtime probe still
  runs and is enforced, but as an independent dynamic gate (candidate
  regression / runtime floor), not as an input to the source identity digest —
  static AST checks are the authoritative capability fingerprint, the probe is
  a live counterfactual confirmation. A bounded identity-replan circuit breaker
  abandons a generation when the same identity error fingerprint survives
  repeated recovery attempts, so a frozen-vs-recomputed mismatch cannot loop
  forever burning LLM budget;
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

`official_certifying` normally means one attached official job and, including
ordinary HEAD-drift recovery, permits only `commit_bot` polling. The sole
dynamic exception is a checkpoint whose `gate_results.official_full` contains
the complete exact marker
`outcome=quality_admission_blocked`, `failure_class=quality`, and
`quality_admission_refresh=true`. Only that marker may route to
`run_quality_gates`; it keeps the evaluation contract unchanged and persists
the transition through the exact checkpoint revision, stage, and workflow CAS.
Missing, partial, conflicting, or infrastructure-class markers remain the
normal `commit_bot` path. The exception never authorizes Workers, an EXE retry,
or reuse of the previous official job.

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
scope. Because the `national_cloud_v` pool is empty at the first-strict reset
(high-water 0, no published bots), that scope is empty and its prompt carries
an explicit no-strength contract; it must not open rating, H2H, replay, Arena,
official, retired-bot, or historical-experience material. Any strict journal,
prompt, context, or invocation-evidence violation canonically abandons the
generation with zero provider-infrastructure retry debt. A terminal strict
Master slot, including exhausted schema repair, is disposable only while the
checkpoint is `direction_audited`; its exact control-plane reason must fence
both journals and complete canonical abandon instead of re-entering
`run_master`. It cannot authorize abandon at any later gate.

Master proposal Scouts receive a compact proposal contract plus system-rendered
frozen planning facts, never the complete final-Master tutorial or final-plan
output schema. During empty-pool bootstrap their read capability is the target
artifact only. During normal evolution it is the exact source, target, and one
assigned frozen evidence snapshot; delivery documentation and every other
results path remain forbidden. The system renders a verified preferred current
chain reachable from the policy ABI entrypoints. Proposal symbols and chain
members must come from that current index, and the validator rejects a chain
outside the policy-ABI reachable closure; future edges belong only in the
proposed diff, never in the claimed current chain. Bootstrap projection failures
append stable field-level error codes to the durable strict rejection. Normal
evolution content-binds the same deterministic codes into its one local repair
prompt and provenance. Both paths enforce the current falsifier enum. Granular
diagnostics do not widen the two-attempt budget or turn rejected reads into
evidence.

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

A provider stream may treat a vanished checkpoint as a completed abandon only
when the current authorized owner tool returned one unique canonical result,
flattened or nested, containing `workflow_run_id` plus the exact transaction,
abandon-ledger, finalize-receipt, and checkpoint identities. Recovery reopens
the transaction at the current Git and ledger heads and replays every event in
both the outer Worker and strict-authority journals: sequence numbers must be
continuous, every payload digest must match, the single `abandoned` event must
be last, and no live effect may remain. Missing, duplicated, ambiguous, stale,
or unreadable proof is `recovery_blocked`; it never becomes permission to
prepare a successor. A terminal result must bind exactly one pending
route-mutating ToolUse by explicit tool/parent id or the SDK's bounded
sole-pending form. Unknown, reused, swapped-owner, multi-pending, unsettled, or
read-only-owner results block recovery. A genuinely absent checkpoint is a provider-stream
boundary: the provider ends the stream and only the outer scheduler may call
the non-MCP `prepare_generation`. `prepare_next_gen` is legal only through an
exact validated `selected` first-materialization route or `preparing`
crash-recovery route. Both timeout states remain active checkpoint leases and
cannot be overwritten by a restart or successor. Plain `timed_out` is allowed
only from the fixed disposable-stage allowlist and canonically abandons.
`infra_timed_out` is allowed only over `critic_checked`; before retry it must
re-prove the full artifact, current quality/review/critic identities, and
quality fingerprint = repair baseline = live bytes, then exact-CAS back to
`critic_checked`. An unbound target preimage found during selected/preparing
materialization causes system-owned canonical abandon/quarantine, never
adoption or deletion.
After commit, pending/running/blocked post-publication handoff state also makes
the provider end its stream; only outer deterministic recovery owns
`run_archivist`.

`orchestrator.py --one-gen` owns one workflow/generation, not one provider
session. It may open fresh provider streams and execute deterministic routes
until that same workflow publishes and completes cleanup, canonically abandons,
parks for an operator action, or blocks recovery. It must never prepare a
successor after abandon, treat failed post-publication cleanup as success, or
collapse abandon/operator/recovery/accounting outcomes into one success code.

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

Control health publishes no executable route when checkpoint revalidation or
recovery is blocked, and `/api/control/start` applies that same launch barrier
before resetting stability or owning a task. Operator actions are a distinct
409 boundary. With an initialized epoch but no checkpoint or handoff, health may
publish only a typed outer-scheduler boundary: provider `end_stream`, non-MCP
`prepare_generation`, authoritative `next_v`, and `source_v=null` because parent
selection has not happened. The frontend must validate that projection, disable
Start on blocked/operator authority, and clear detailed checkpoint state after
a failed poll; it may not infer a route or source from `current_v`. Checkpoint
absence uses a before/read/after observation: unreadable, disappearing, terminal
looking, or incomplete bytes never become a clean scheduler boundary. Process
launch additionally distinguishes a resumable pending/dead-owner handoff from a
live foreign owner. Owner reservation double-samples one fence digest; AppState
and the global LLM shutdown manager are both owner-CAS fenced, and an unowned or
failed lifespan may not alter the live owner's running/UI/manager state.

An app lifespan stops only a runtime owner it registered, but registration is
performed for both lifespan launch and a later successful `/api/control/start`.
At shutdown it resolves the current fenced owner and shutdown manager through
`AppState`, rather than retaining a startup-time manager, so a later registered
control-start owner receives its own graceful stop. A task projection with no
authority is emitted as typed `task_authority_lost`, never a fabricated
`task_owner` row or synthetic `R+1` lifecycle revision. HTTP null/malformed
task projections and malformed SSE `status`/`task_owner` data clear transient
text. They retain the last verified fence: a later exact valid projection at the
same revision may restore authority, while a contradictory same-revision
projection remains blocked until a genuinely newer revision arrives.

Native precommit cancellation is attempt-local and monotonic. The exact token
is passed into the real 70-hand loop, checked before every opponent/repeat and
after each complete match/journal, and permanently set on timeout/cancellation.
Reset rotates only an already-cancelled token. A new attempt cannot revive old
detached work, admit its late match, or let it launch the next sample. The
first-strict system-control execution scope is frozen in the checkpoint so an
infra retry recovers the same journal identity rather than repeating a match.

## Codex-only Worker MCP

`pok_worker` is an external Codex desktop/CLI control-plane helper. It is not a
poker-evolution Worker. Its actual persistent registration is operator-owned in
Codex configuration and user services; this file documents the repository-side
usage contract but does not register, install, start, or supervise the server.

A Codex session may delegate a bounded task only after it independently:

1. discovers exactly `submit`, `get_status`, `get_result`, `list`, `cancel`, and
   `healthcheck` under `pok_worker`;
2. calls `healthcheck` and receives overall `status=healthy`;
3. submits an exact repository, immutable base commit, explicit allowed paths,
   mandatory forbidden paths, acceptance criteria, execution limits, and a
   unique idempotency key;
4. polls `get_status`, reads `get_result`, and independently reviews the actual
   diff and reruns final tests before accepting any result.

Every new logical user goal or independent work unit requires a fresh `submit`
with a new unique `idempotency_key`, then consumes only its returned `task_id`.
Only if that same submit response is lost or its transport outcome is uncertain
may the exact same envelope and key be retried; accept its explicit
`idempotent_replay=true` and reuse the returned task. Reusing a key with a
changed envelope fails closed. Follow-up turns for the same work unit reuse its
`task_id` without submitting again. Never choose a terminal task or prior
`get_result` as a substitute for fresh work. `list` defaults to non-terminal
recovery state, and terminal history is allowed only for explicit user-approved
recovery or audit.

Never place a model credential, HTTP access token, secret, `.evolution_pok`, or
archive path in a task envelope. Treat Worker output as untrusted proposed work:
the Worker may not commit, push, deploy, modify the primary checkout, widen its
path scope, or become certification/evidence authority. `cancel` applies only
to the exact owned task. Worktree cleanup requires the durable task row, exact
owner marker, configured root, terminal state, and a clean snapshot.

No executable path under `web/`, `sever/`, `bots/`, `scripts/`, the
Orchestrator, rating daemon, candidate generation, or `.evolution_pok` may
import, start, supervise, or call `worker_mcp`. MCP installation and restart are
separate operator actions. Before either action, require zero non-terminal MCP
tasks and an explicit safe window; preserve SQLite, use the owner-aware cleanup
contract, and re-prove a fresh six-tool discovery, health, real task, and
restart recovery. Missing tools or unhealthy status fail closed for delegation
and never authorize a poker-runtime restart.

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
# Cloud runtime (this branch) — managed by systemd as pok-evolution.service.
# WorkingDirectory=/home/ubuntu/pok1/.evolution_pok; env from
# deploy/tencent-cloud/env.runtime (+ gitignored env.runtime.local for secrets).
sudo systemctl start pok-evolution      # start (foreground via systemd)
sudo systemctl restart pok-evolution    # restart after code/env changes
sudo systemctl status pok-evolution     # health + recent journal
journalctl -u pok-evolution -f          # live logs

# Manual launch (foreground, same env) — only for debugging, not for production:
cd /home/ubuntu/pok1/.evolution_pok && \
  set -a && . deploy/tencent-cloud/env.runtime && \
  . deploy/tencent-cloud/env.runtime.local && set +a && \
  /home/ubuntu/pok1/.venv/bin/python web/main.py --host 127.0.0.1 --port 8000 --no-build

# Web application (generic forms; the cloud runtime uses the systemd launch above)
python web/main.py
python web/main.py --view-only
python web/main.py --no-daemon

# Evolution CLI / rating daemon
python web/core/orchestrator.py --one-gen
python web/core/elo_daemon.py --once

# Tests
export PYTHON=/path/to/project-python
"$PYTHON" -m pytest sever/tests -q
(
  cd web && "$PYTHON" -m pytest tests -q
)
(
  cd web/frontend && PYTHON="$PYTHON" npm test && npm run lint && npm run build
)

# National TCP platform
cd sever && python main.py

# Diagnostic Arena only
python scripts/national_arena.py serve --view-only

# Official acceptance and required certification
python scripts/official_platform_acceptance.py \
  --candidate bots/national_cloud_v<N> --opponent bots/national_cloud_v<M> \
  --self-play-rounds 1 --opponent-rounds 1 --target-hands 70
python scripts/official_certify.py full bots/national_cloud_v<N> --wait-if-busy

# One-time empty-pool bootstrap for the first strict bot only
python scripts/official_certify.py bootstrap-first-strict bots/national_cloud_v1 \
  --control-id first_strict_control_v1 \
  --acknowledge-one-time-first-strict-control --wait-if-busy

# Only after the jobs API projects ready_to_finalize for that exact certificate
python scripts/official_certify.py finalize-first-strict \
  --acknowledge-publish-first-strict
```

Normal certification is five 70-hand self-play rounds plus three 70-hand rounds
against an eligible strict-policy opponent. The first-strict-only
(`national_cloud_v1`) system-control bootstrap and finalize steps are
operator-only, zero-strength, and never an automatic fallback. The LLM/HTTP
control plane can perform neither step. The checked-in
`first_strict_control_v1` artifact hash is
`b37cd019fe6b635a119950adb5f7ecf10ddceeafacfbed6b4c3a0955064516e2`.
Its valid, unused `0/1` consumption state and a green official doctor prove the
5+3 dependency exists; they do not unlock the command. Bootstrap becomes
available only after the exact `national_cloud_v1` checkpoint parks at
`official_bootstrap_required`.
The archived v141 signed-ledger chain is validation history and is not executable.

### Official certifier signer (cloud runtime)

Official EXE certificates are signed with the **server-owned Ed25519 signer at
epoch 3** (fingerprint
`SHA256:5C70Tt/aIzq60HlCQBXLZ0MdTWN3vIWk6HjkEU+nsTk`). The trust policy
(`web/core/official_certifier_trust_policy.json`) records `current_epoch: 3`
as active, with epoch 1 retained as historical-validation-only (tied to the
retired `national_v141` signed-ledger chain). The allowed-signers file
(`web/core/official_certifier_allowed_signers`) lists all three epoch keys
under namespace `pok-official-cert-v4`. The companion
`docs/official-signer-rotation.md` documents this epoch-3 server-owned key
and retains the older operator-host epoch-2 narrative as historical context.

## Working rules

- Search with `rg`/`rg --files` first.
- Use `apply_patch` for hand edits; preserve unrelated dirty changes.
- Never reset, checkout, or delete user work to obtain a clean tree.
- Keep bot/runtime code stdlib-only unless an existing system boundary says
  otherwise.
- Test in proportion to risk: compile touched Python, run focused tests, then
  the relevant native protocol/evolution shards.
- When a change affects behavior, an ABI/protocol, gate, prompt contract, data
  schema, lifecycle, test harness, or expected failure mode, update the
  necessary focused/full test process, fixtures, regression anchors, and
  operator test commands in the same change. Never preserve a green result by
  skipping, weakening, or reclassifying the affected test without an explicit
  fail-closed replacement and documented reason.
- **Synchronize documentation with every functional change.** A feature added,
  removed, or modified must update the relevant docs in the same change:
  `AGENTS.md` and `CLAUDE.md` for cross-cutting contracts, `docs/` for
  architecture/policy/oracle detail, `deploy/` for operational deployment.
  Version numbers, namespace identifiers, paths, LLM/signer configuration,
  command examples, file lists, and ABI/protocol/gate/lifecycle descriptions
  must stay consistent with the code they describe. Do not leave a working
  feature or a deployed change with stale documentation.
- `web/main.py` is a web launcher, not a TUI or mode-switching CLI.
- Generated frontend output is ignored; do not treat it as source.
- The highest numbered bot directory is not completion proof. Require current
  epoch artifact metadata, `.completed`, annotated completion tag, and the
  role-specific certificate.
