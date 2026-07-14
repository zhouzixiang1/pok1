# M0-M2 common-contract gate

Status on 2026-07-14: the Common M0-M2 implementation passed its development
and route-integration gate. Formal strength execution remains deliberately
unavailable on this host. This report is therefore not a formal-strength
certificate and is not permission to start large HUNL training.

## Audit and reboot snapshot

- The Common worktree and both route branches were created from
  `6ee160c93cee8d0afdad111c4c82bc6ddb6012ca`. The Common branch intentionally
  remains based on that SHA while its isolated work is reviewed.
- At this update, `origin/main` and the operator checkout were at
  `f0f3e5f228afd80915d13ed1196c9794636ab8ae`. The autonomous evolution
  checkout was at `52c16115c76ac973f1eab81b9b4a1a844be01cc9`; its checkpoint was
  `generation:155:workflow-v1`, stage `direction_audited`, revision 7. No
  evolution web app, orchestrator, rating daemon, or collector process was
  observed after the reboot. Neither operator checkout was modified by this
  audit.
- Hardware was reprobed after the reboot: Intel i9-13900HX (24 physical cores,
  32 logical CPUs), 65,571,756 KiB RAM (about 62.5 GiB), no swap, and an RTX
  4060 Laptop GPU with 8,188 MiB VRAM. The filesystem containing the repository
  is 1,005,923,561,472 bytes (about 937 GiB) total with about 765 GB (712 GiB)
  available at the observation. The runtime is Python 3.14.4, PyTorch
  2.12.0+cu132, CUDA 13.2, and NVIDIA driver 595.71.05.
- The conservative initial training envelope remains 24 CPU threads, 50 GiB
  RAM, 7,000 MiB VRAM, one online worker, and no swap dependency. These are
  limits, not a claim that training may begin.

## M0 - repository and source boundary

- Repository policy, the dual-checkout boundary, worktrees, remote state, tags,
  official documents, controlled EXE oracle notes, validator, game engine, and
  evaluator were audited.
- Exact repository rule/oracle inputs and the locked diagnostic verifier
  distributions are hash-bound in `manifests/common_sources_v1.json`.
- No AGPL or other external strategy implementation is included in Common.
  Route-specific papers, source-fidelity checks, and license matrices remain
  route responsibilities.

## M1 - frozen common contracts

The candidate contract set covers national cards and actions, the TCP stream
state machine, native raw-wire capture, replay verification, 70-hand match
semantics, resource/deadline enforcement, complete formal-matrix planning,
paired evaluation, statistical analysis, candidate-neutral seed cohorts, and
final-randomness derivation. The current public pool remains a visible
reference universe, not a falsely labelled blind set.

### Final randomness: diagnostic derivation only

The checked-in OpenTimestamps adapter can verify a candidate record against a
caller-selected loopback Bitcoin Core RPC for reproducibility diagnostics. It
cannot establish independent chainwork, node synchronization, the real freeze
time, or that later entropy was unknown. Its authority is permanently
`caller-rpc-ots-diagnostic-only`; no caller-supplied epoch, RPC response,
`bls_verified` flag, copied dataclass, or local capability token can upgrade it.

Formal final randomness requires a fixed independent root-owned freeze and
chainwork authority, a signed freeze receipt that fixes future Bitcoin block
`H+12`, that future block hash, independently saved drand payloads agreeing
across three relays, and locked BLS verification of the selected future drand
round. The formal entropy mix binds the future block, drand randomness, and the
independent receipts before deriving the candidate-neutral seed cohort for the
complete formal matrix.

That independent authority is not installed and no final candidate freeze or
formal final seeds exist. Accordingly,
`contracts/final_randomness_v1.json` records
`formal_strength_available: false` and a fail-closed formal status. Diagnostic
seed derivation may exercise code paths, but it has zero formal-strength
authority.

### Formal matrix and plan binding

The old exactly-three-candidate bundle is explicitly non-formal. Each formal
cell must receive a `FormalEvaluationPlanBridge` issued from the frozen
`CompleteFormalMatrix` projection. The bridge binds the complete matrix root,
projection, artifact identities, candidate-neutral seed cohort, opponent and
time-budget strata, resource profile, randomness contract, analysis code,
stopping rule, and retry policy. A cell result and the final matrix ledger must
revalidate those exact bindings rather than reconstruct them from live files.

### Resource and supervisor authority: unavailable, fail closed

The same-UID cgroup runner is diagnostic-only. A formal leg requires the fixed
`/etc/pok/formal-resource-supervisor-v1.json` contract and an external uid-0
service with a pinned executable, external signature verifier and key,
authenticated control channel, two isolated candidate UIDs/namespaces, a
supervisor-owned cgroup-v2 subtree and global lease, a root-owned read-only
artifact CAS, and root-owned no-clobber ledgers.

The signed supervisor bridge binds readiness and prelaunch records, exact
launch/artifact/profile identities, ordered socket ownership, capture session,
raw wire, independently derived wire semantics, raw replay, complete decision
and fault traces, cleanup, termination, and a one-use receipt-consumption
ledger entry. Formal replay can be constructed only from an
`AuthorizedSupervisorLeg`; the legacy receipts-only factory rejects formal
authority.

`DecisionEnforcementEvent` v3 binds a global decision identity to the actor,
hand, street, request raw-record sequence, all-or-none parser-committed client
token fields, and mandatory server-to-peer close record. A normal action must
have a committed client token and ingress timestamp strictly between the
request and close. A timeout has all three client-token fields explicitly null,
proves the 54-second compute hard stop and exact 60-second platform deadline,
and closes only at the peer `fold` relay. Crash, resource, protocol, and
infrastructure faults can be token-bearing when they occur after ingress or
tokenless when the supervisor adjudicates the fault after the decision lease
opens but before any client token. The peer relay can never masquerade as a
client token.

Every tokenless branch proves zero client bytes inside the lease and the exact
server-to-peer `fold` close. A non-timeout tokenless crash/resource/protocol/
infrastructure close must be strictly earlier than the 60-second platform
deadline; at or after that boundary timeout attribution takes precedence.
`native_wire.py` reconstructs these facts independently from raw capture, and
`native_replay.py` requires an exact bijection between replay fault markers and
the signed decision/fault trace rather than trusting event fields alone.

Every authorized launch outcome must appear in a root-owned, no-clobber,
externally signed attempt journal. Sequences start at one, link to the previous
entry, cover launch/capture/cleanup/infrastructure failure as well as success,
and finish with a signed closed-scope head. Formal aggregation rereads the root
files and accepts exactly one final completed row for each planned leg.

The frozen cap is at most two infrastructure retries per leg. A failed attempt
without a replay and original observation now invalidates the entire formal
scope: a `launch_failed`, `capture_failed`, `cleanup_failed`, or
`infrastructure_failed` journal label by itself cannot distinguish an early
candidate failure from bot-independent infrastructure and therefore grants no
retry authority. `aborted` also invalidates the scope, and a candidate fault is
never retryable. A replay-bearing infrastructure failure may be followed only
when its exact original `MatchObservation` is retained and the verified
`RetryLedger` contains the corresponding adjacent edge. This prevents a later
success from hiding or relabelling an earlier result.

Formal replay-bearing infrastructure attribution is implemented through
`InfrastructureAttributionReceipt.from_authorized_supervisor_failure`. It
requires the exact formal plan, exact `AuthorizedSupervisorLeg`, original
replay-verified `MatchObservation`, and the authorized closed attempt journal
containing the matching `infrastructure_failed` row. The binding reasserts the
capabilities and re-derives the failure domain, fault time, affected run IDs,
and incident digest from signed supervisor decision/fault, replay, termination,
resource, cleanup, consumption-ledger, and journal facts. The caller-mintable
development receipt remains diagnostic-only and cannot acquire that formal
binding.

The formal infrastructure-retry success path cannot be exercised end to end
on this host because the fixed root supervisor, its external signature
authority, closed root journal, and formal final-entropy authority do not
exist. The contract and rejection paths are executable, but no test fabricates
those missing host capabilities. This boundary is recorded as operationally
unavailable, not as a passed formal retry run.

This host has none of the fixed `/etc/pok` supervisor installation, privileged
service, key/verifier, isolated identities, CAS, authenticated channel, global
lease, consumption ledger, or signed closed attempt journal. Therefore formal
resource and strength authority is intentionally unavailable. Test fixtures
that mock the installation boundary do not change that host fact.

## M2 - verification surface and present limits

The focused tests cover all 52 card mappings, all 1,326 private combinations,
evaluator differential checks, reachable-state differentials against
`sever/engine/validator.py`, exact 2x re-raise and street rules, all-in runout,
zero-sum settlement, serialization and distinct-history state identities,
every sticky-stream byte split, numeric-token ambiguity, suppressed peer
closes, hand-70 settlement evidence, disjoint seed commitments, paired blocks,
bootstrap/Holm/stopping logic, complete-matrix bindings, raw-evidence
uniqueness, one-use supervisor capabilities, and attempt-journal anti-selection
rules.

### Post-M4 integration audit hardening

A route-integration audit on 2026-07-14 found two Python/API state-machine
aliases that the original M2 suite did not exercise.  `submit_action` now
accepts only an exact `int` decision ID, so `True == 1`, floats, strings and
integer subclasses cannot consume a one-shot lease.  Rejection occurs before
action parsing or state mutation and leaves the valid pending lease intact.

The connection handshake is now ordered and one-shot as well: platform state
cannot be accepted before `name` is received and its response is consumed;
duplicate, stale or unsolicited name requests/responses fail closed.  A
combined decoder/session regression feeds the sticky raw byte sequence
`namepreflop|...` and proves that the name response is authorized before the
first hand opens decision lease 1.  These are Common protocol hardenings, not
evidence of official EXE acceptance.  The local `sever` LF adapter and the
official raw/no-delimiter transport remain separate test authorities.

A positive integration test now starts from a real 70-hand socket capture,
uses a test-only signed supervisor fixture that mocks only the unavailable fixed
installation/signature/root-readback boundary, and exercises the production
chain:

```text
AuthorizedSupervisorLeg
  -> bind_authorized_supervisor_replay
  -> verify_native_replay
  -> ReplayVerificationReceipt
  -> MatchObservation
```

It also rejects a signed decision event whose wire binding was altered. This is
useful executable evidence for the bridge; it is not a formal host attestation.

The v3 tokenless branch now represents crash, timeout, resource, protocol, and
infrastructure faults after a decision lease has opened and before any client
token, while token-bearing variants retain faults that occur after parser
ingress. Its present boundary is deliberately exact: tokenless evidence permits
zero client bytes only. Malformed half-token bytes are not yet representable by
that branch and fail closed pending a future raw fault-span/digest schema;
parser-committed illegal tokens are already replayable as protocol losses.

Failures before the first decision lease—including startup or name-handshake
failure—still lack a replay-backed `MatchObservation`. They cannot currently be
scored and make the formal scope fail closed instead of authorizing a seed
replacement. Thus candidate crashes, timeouts, replayable illegal protocol,
and resource overruns count as losses once evidenced inside the supported
lease/replay boundary, and none can be retried as infrastructure.

### Regression status

The final acceptance rerun was repeated after the 2026-07-14 handshake and
decision-ID hardening:

- Python 3.12 with `POK_RUN_SLOW_M2=1`: 258 Common tests passed, zero skipped
  and zero failed in 199.80 seconds; process peak RSS was 296,152 KiB.
- Default Python 3.14.4 with `POK_RUN_SLOW_M2=1`: 258 Common tests passed,
  zero skipped and zero failed in 191.40 seconds; process peak RSS was
  306,108 KiB.
- `sever/tests`: 33 tests passed under both interpreters, with zero skipped or
  failed.
- All 41 Common Python files compiled under both interpreters with bytecode
  redirected outside the worktree.
- All eight JSON files under `contracts/` and `manifests/` parsed under both
  interpreters. The Common suites also passed their frozen verifier,
  controller, enforcer, source-manifest, and golden-evidence digest checks.

Common may now be merged into the two route branches for M3 integration.
Large HUNL training still must not start until each route passes its own
paper-equation tests plus Kuhn/Leduc exploitability/best-response gates. Any
later Common contract or implementation change invalidates this recorded test
evidence and requires another full rerun.
