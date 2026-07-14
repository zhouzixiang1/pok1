# Shared research contracts

This directory is the policy-neutral substrate for three independent research
candidates. It contains no trained strategy, blueprint, value network, solver,
opponent model, or match-control parameter.

The frozen game API is `NationalGameState`. It is immutable, preserves an
append-only street-labelled action history, exposes separate single-hand
public/information, match-context, observation and full-state hashes, and uses exactly the national
raise-to/check/call/all-in semantics. `NationalProtocolSession` reconstructs
that state from an unframed TCP stream. It only infers a missing peer close when
a later street or disclosed showdown proves the unique call/check; a bare
`earnChips` is not enough. Decisions use one-shot IDs and may only be submitted
by the socket-owner thread.

The evaluation contract treats one complete 70-hand native TCP match as a
sample. Every deck seed is run twice with swapped connection/seat mapping. The
paired seed block is the independent bootstrap cluster; final net-chip sign is
the outcome, while magnitude is secondary. Candidate crashes, timeouts,
illegal actions and resource violations remain losses. Infrastructure reruns
retain the original record and use the identical seed.

## Frozen inputs

- `contracts/national_game_v1.json` — exact rules and wire invariants.
- `contracts/evaluation_v1.json` — metrics, sample floors and resource fairness.
- `contracts/resource_v1.json` — observed host and online anytime envelope.
- `contracts/development_seed_manifest_v1.json` — reproducible train/dev/validation
  roots only; it contains no final material.
- `contracts/current_pool_snapshot_20260712.json` — content-bound visible
  reference pool from one immutable daemon cycle and the frozen universe for a
  later random heldout selection. Its identities are not claimed to be blind.
- `contracts/opponent_splits_public_v1.json` — visible train/dev/validation
  opponents and the post-freeze heldout derivation contract.
- `contracts/final_randomness_v1.json` — separates reproducible diagnostic
  derivation from formal future entropy. Formal issuance requires an
  independently witnessed freeze, Bitcoin block `H+12`, and verified drand
  material; that authority is not installed on this host.
- `manifests/common_sources_v1.json` — hashes of the exact rule/oracle inputs.

There is deliberately no same-user "secret" file: Unix mode bits would not
isolate Codex agents that share one account. Diagnostic randomness is fetched
only after the complete matrix root is frozen, and it never acquires strength
authority. The formal design additionally requires entropy that a route could
not know at the witnessed freeze: the fixed future Bitcoin block `H+12`,
verified drand material, and independent chainwork/witness receipts. The
official drand client must verify the selected round and three relays must agree
before either path derives seeds or opponent order.

The freeze timestamp is not a caller-entered epoch. The canonical
`candidate-freeze-record-v1` is timestamped outside this code, then the
diagnostic API runs the locked `opentimestamps-client==0.7.2` in verify-only
mode. It disables the cache and remote calendars and can check a caller-selected
loopback Bitcoin Core mainnet node with six confirmations, but that boundary
cannot prove independent chainwork, sync state, or the real freeze time and is
therefore permanently diagnostic. `unstamped`,
`pending_bitcoin`, `invalid`, and `verifier_error` all keep final evaluation
locked. The repository currently contains neither the fixed root-owned freeze
authority nor a candidate-specific independent witness, so this contract does
not claim a formal freeze pass.

After the diagnostic freeze path selects its deterministic future round, fetch
and verify drand in two separate steps. The fetcher saves raw
chain/current/previous JSON from all three frozen relays.
`FinalEvaluationPlan.verify_beacon()` reopens those files,
recomputes every digest, validates the chain/public key/round/randomness/
signature/previous-signature link, then invokes the hash-pinned official
`drand-client@1.4.2` bundle through the pinned Node runtime.  It does not accept
a caller `bls_verified` field.

Formal evaluators must derive RNG streams through
`derive_formal_deck_root_pool()` and `derive_formal_policy_seeds()`.  The deck
helper always materializes the complete frozen 8,192-root pool as unsigned
256-bit big-endian HMAC values; a formal matrix consumes its exact block-index
prefix without re-deriving a shorter pool.  Policy seeds use the first eight
HMAC bytes, interpreted big-endian and masked to 63 bits, for the exact frozen
matrix count. Both helpers bind streams to the candidate-neutral seed-cohort
digest; policy streams additionally follow the artifact identity across seat
swaps. The compatibility
`derive_seeds()` entry point rejects every namespace outside those two frozen
templates and rejects a short deck pool, so hand-written ad-hoc namespaces or
count-dependent deck roots cannot silently become formal evidence.

```bash
# Provision the exact official drand module recorded in the lock.
python -m bots.research_native_lab.common_contracts.tools.verify_drand_beacon \
  provision --cache-dir /path/to/read-only-verifier-cache

# Run only after FinalEvaluationPlan fixes the future round.
python -m bots.research_native_lab.common_contracts.tools.verify_drand_beacon \
  fetch --round <frozen-round> --output-dir /path/to/new-empty-evidence-dir
```

The OpenTimestamps verifier expects the exact offline wheel set listed in
`tools/verify_candidate_freeze.lock.json`; this lock is currently for the
observed Linux x86-64 host.  The drand lock also pins `/usr/bin/node` by version
and binary digest.  A platform or OS runtime update therefore fails closed
until a reviewed lock refresh.  Supplying credentials in a
`--bitcoin-node` URL exposes them transiently in the local process list; prefer
a narrowly permissioned loopback RPC setup.  Neither verifier contacts a
strategy source or contributes strength evidence.

Formal execution has a separate fixed-authority boundary. A same-UID launcher
is diagnostic only. Strength evidence requires the root-owned supervisor
contract, signed readiness and prelaunch authorization, isolated candidate
UID/cgroup/namespace/socket identities, exact raw wire and replay hashes,
per-decision hard-stop events bound back to wire records, signed cleanup, and a
no-clobber consumption ledger. Every launch—including failure before a replay
exists—must also appear in one contiguous, signed, closed attempt journal. The
matrix result consumes that journal so failed starts cannot be hidden by
selecting a later successful run. A failure without a replay and original
`MatchObservation` invalidates the formal scope: its signed journal label alone
does not authorize an infrastructure retry. Replay-bearing infrastructure
attribution requires the exact `AuthorizedSupervisorLeg`, the original
`MatchObservation`, and its authorized closed attempt journal; it re-derives
the failure domain, time, affected runs, and incident binding from the signed
wire, replay, decision/fault, termination, resource, and journal facts.

`DecisionEnforcementEvent` v3 represents a parser-committed normal action, a
tokenless timeout, and tokenless crash, resource, protocol, or infrastructure
faults after a decision lease has opened. Crash/resource/protocol/
infrastructure events may also retain a committed client token when the fault
occurs after ingress. A non-timeout tokenless close must occur strictly before
the exact 60-second platform deadline; at or after that boundary timeout takes
precedence. A tokenless event proves zero client bytes in the lease and an
exact server-to-peer `fold` close. It does not yet represent malformed
half-token bytes; a parser-committed illegal token is separately replayable as
a protocol loss. Failures before the first decision lease likewise cannot yet
be scored and invalidate the formal scope rather than receiving a replacement
seed.

None of the required fixed supervisor installation or independent final-
entropy authority exists on this host, so both formal execution and formal
final evaluation intentionally fail closed.

## Validation

```bash
python -m pytest bots/research_native_lab/common_contracts/tests -q
python -m pytest sever/tests/test_national_platform_alignment.py -q
python -m bots.research_native_lab.common_contracts.tools.probe_hardware
```

The official EXE and Arena remain compliance/diagnostic surfaces with zero
strength weight. Nothing in this research directory is a `national_v<N>` bot,
a completion marker, a formal certificate, or an approval to promote.

Blueprint/CFR/PBS keys must use `hand_public_state_id()` or
`information_state_id(player)`. They must not use `observation_id()` or
`match_context_id()`; those are reserved for the separately ablated 70-hand
controller and would otherwise leak match objectives into the single-hand game.
