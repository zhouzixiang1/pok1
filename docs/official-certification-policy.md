# Official EXE Certification Policy

> The National Web Arena (`/arena`) is a local diagnostic and presentation
> tool. Its events, THP files, and results have `diagnostic_only` authority and
> can never create, replace, or satisfy an official Windows EXE certificate.

The Windows national platform is a protocol and runtime-compliance oracle. It
never contributes to Glicko, H2H, source-selection strength, chip-EV, or poker
strategy scores.

## Formal Certificate

`official-full-v5` is the immutable formal profile for every new national bot:

- five candidate self-play rounds;
- three rounds against one policy-eligible native opponent;
- exactly 70 hands in every round;
- an exact completion proof: normally 70 paired TCP settlements, or the
  official 2021 EXE terminal form with wire hands 1..70, wire settlements
  1..69, and a strict 70-state THP whose named earnings and footer cross-bind
  the omitted terminal settlement;
- complete THP, wire capture/replay, bot logs, stdout/stderr, platform log, and
  screenshot evidence;
- sealed read-only bot artifacts launched in an isolated network namespace with
  only one host-preconnected wire-proxy socket descriptor;
- no deterministic candidate-side protocol, communication, timeout,
  state-machine, or obvious decision-state violation;
- every round countable under `official-attribution-v1`.

The deterministic parser is the only pass/fail authority. LLM analysis is a
separate schema-v2 sidecar with `authority=advisory_only`. It may cite stable
evidence IDs, explain a likely cause, and suggest a bounded repair. It cannot
emit or change pass, fail, blocking, certificate, rating, or strength fields.
Uncited feedback and strength-tuning text are discarded. LLM failure, timeout,
or absence never changes the deterministic result.

A v5 certificate binds:

- the exact candidate and opponent artifact hashes;
- the full profile and official-opponent selection receipt;
- EXE, Wine/UI, harness, wire parser, attribution, and policy fingerprints;
- a deterministic 5+3 round receipt;
- each round's completed-hand method and, for the terminal form, the raw wire
  hash, THP hash, prefix-earnings digest, final state, named totals, and footer;
- the official evidence manifest and content-addressed archive receipt;
- the issuing Ed25519 identity.

The certificate is signed with the local key selected by
`POK_OFFICIAL_SIGNING_KEY` and verified against the tracked trust root
`web/core/official_certifier_allowed_signers`. The private key is never stored
in Git. Production verification cannot replace this trust root through an
environment variable; tests may inject a temporary trust file only through an
explicit API. This is an integrity/publication-binding mechanism for keyless
verifiers, not isolation from the orchestrator: the current LLM tool processes
and key owner share one Unix uid, so a malicious same-uid agent is outside the
signature threat model. Independent authentication also requires the verifier
to anchor the expected public-key fingerprint outside the repository being
verified; when the repository commit is already the sole trust root, its Git
objects and canonical hashes already provide the content binding and the
signature adds no separate trust assertion. The doctor reports these boundaries
explicitly. The doctor also validates the crash-consistent signed verdict
history. It never creates or
guesses authority state; under the ledger lock it may only finish the defined
append crash protocol: roll a valid signed-entry suffix into a new signed head,
or truncate an incomplete write exactly to the previously signed head byte
boundary. Complete invalid suffixes remain blocking evidence. The ledger and
its head share one same-uid writable filesystem and have no external latest-head
anchor, so restoring an older valid pair is not detected; this is not a
transparency log or a same-uid rollback defense. Run doctor before an expensive
suite:

```bash
python3 scripts/official_certify.py doctor
```

On a new operator host, create the signed empty genesis only through the
explicit idempotent command, then re-run doctor:

```bash
python3 scripts/official_certify.py init-ledger
python3 scripts/official_certify.py doctor
```

`init-ledger` first requires a healthy signing identity. It creates nothing when
the existing ledger is already valid and refuses to overwrite a corrupt,
truncated, or unsigned history. Formal production preflight checks the ledger
again and stops before launching the EXE when genesis/history is unavailable.

Full certification fails before launching the EXE when signing or trust-root
preflight fails. Before Git commit, the candidate hash is checked again. The
annotated `national-bot-vN` tag records `official-certificate`,
`official-candidate-hash`, and `official-policy`.

`official_certificates/national_vN.json` is the portable signed attestation.
Large raw evidence stays in `POK_OFFICIAL_EVIDENCE_STORE` and is not committed.
When raw bytes are present, validation reopens and hashes every retained
artifact. A clone can still verify the signed portable receipt when the local
archive is unavailable; the API reports `raw_evidence_available=false` rather
than pretending the raw files were inspected. Altering the attestation,
certificate, deterministic receipt, or any recomputed digest invalidates the
signature.

The signed record binds both the candidate artifact hash and its
`national_vN` label. Copying identical bytes into another version cannot reuse
or republish the certificate. Migration readiness counts distinct certified
artifact hashes, so duplicate directories cannot retire the bootstrap anchor.

Every deterministic run also writes a content-bound status receipt that binds
the bot label/hash, mode, policy, evidence digest, archive digest, and parser
verdict. Only a valid receipt can make `official-failed` block a parent or
override an older published pass. Mutable `issues` text and evidence-summary
fields are diagnostic only. Full-mode evidence is content-addressed for pass,
candidate failure, and inconclusive outcomes; failure to retain the archive
makes the result inconclusive.

## Durable Job Lifecycle

All production smoke, precommit compliance, and formal certification requests
use `official-job-v3` in `web/core/official_certification_job.py`. The old JSONL
queue helpers remain legacy regression APIs and have no production caller.

The job manager provides:

- content-bound request/job identities with volatile selection diagnostics
  removed;
- one global EXE process group at a time;
- atomic state, heartbeat, live round progress, and checkpoint attachment;
- process/boot/start-time/claim-token ownership checks before cleanup;
- recovery of interrupted `cancel_requested` states without job resurrection;
- crash resume within the same suite without rerunning completed failed rounds;
- explicit terminal retry in a new suite attempt, forcing all 5+3 rounds again;
- active-owner-first reconciliation so queued jobs cannot starve stale cleanup.
- shared official-port leasing with the Web Arena: Arena cannot take port 10001
  while a formal job is pending, and a job remains queued while an existing
  Arena session owns that port instead of producing a false infrastructure
  failure.

`commit_bot` moves the checkpoint to `official_certifying` and polls the same
job every 30 seconds. A deterministic candidate failure becomes
`official_failed` and enters worker repair. Platform, Wine, signer, or evidence
ambiguity is infrastructure failure and never becomes bot repair. Commit/tag is
impossible until the signed full certificate validates.

## Eligibility And Migration

Eligibility is role-specific:

- `official_opponent`: may be used in a formal EXE suite;
- `parent_source`: may seed a generation or crossover;
- `rating_pool`: may participate in native strength evaluation.

New candidates at or above the tracked cutoff can never be grandfathered.
Historical grants in `web/core/official_grandfathering.json` bind an annotated
tag, exact artifact hash, roles, and migration policy. Known blocking evidence
overrides every grant.

Normal `official_opponent` eligibility requires a published, signed,
content-valid `official-full-v5` certificate. Historical grants, including
v142, are limited to their explicitly recorded parent/rating roles and can
never satisfy a formal opponent selection. They sunset at the tracked migration
boundary, which preserves continuity in the local rating population without
turning grandfathering into a certification path for new output.

The repository-pinned v141 signed-ledger root is a one-time operator bootstrap,
not a normal opponent, active-pool fallback, or automatic evolution choice. It
can certify exactly one fresh, unpublished candidate through the explicit
`bootstrap-full` command. Only a successful certificate appended to the signed
verdict ledger consumes the root; failed or inconclusive runs do not create a
normal opponent. The normal locked workflow will not replay a consumed root.
This is an operational state-machine guarantee, not a cryptographic guarantee
against a same-uid rollback of both the ledger and its signed head; durable
rollback resistance would require an independently protected monotonic anchor.

After that manual suite succeeds, `commit_bot` may reuse only the exact existing
certificate that passes the complete content-bound validator (candidate hash,
signed receipt, evidence, ledger, selection receipt, job envelope, parked
checkpoint, workflow/evaluation contract, and policy). It skips a second
opponent selection/job for that handoff, then reruns the consumed-root-aware
authorization immediately before staging and tagging. A status label, mutable
JSON, or ledger entry alone is insufficient. Publishing that first attestation
creates the first normal full-v5 opponent; subsequent candidates use the
ordinary policy path.

When the first verified candidate finds no normal opponent, the pipeline parks
at `official_bootstrap_required`. This is a deliberate stop barrier:
`next_tool` is empty, automatic recovery exits, and the LLM cannot call
`commit_bot` again or initiate bootstrap. The runtime guard unlocks the manual
commit handoff only after the external `bootstrap-full` result passes the full
validator. Missing, forged, stale, or candidate-mismatched certificates remain
fail-closed.

Lifecycle state is durable in annotated Git tags:

- `national-reaped-vN` is a permanent retirement tombstone;
- `national-reaped-registry-v1` proves legacy ledger migration;
- `national-high-water-vN` preserves the monotonic version floor.

Historical bots are recertified in batches. A failed bot is removed from
eligible roles or repaired; the validator is never relaxed to keep it active.

## Operator Commands

```bash
# Platform + signer + verdict-ledger readiness
python3 scripts/official_certify.py doctor

# First-host signed genesis (explicit and idempotent), then verify all readiness
python3 scripts/official_certify.py init-ledger
python3 scripts/official_certify.py doctor

# Durable full 5+3x70 request and wait for its terminal result
python3 scripts/official_certify.py full bots/national_v<N> --wait-if-busy

# One-time first-anchor bootstrap for a fresh unpublished candidate
python3 scripts/official_certify.py bootstrap-full bots/national_v<N> \
  --root-id national-v141-official-full-v5-signed-ledger-root \
  --acknowledge-one-time-ledger-bootstrap \
  --wait-if-busy

# Inspect/reconcile durable jobs
python3 scripts/official_certify.py queue-status
python3 scripts/official_certify.py process-queue --limit 4
```

`queue-status` and `process-queue` keep their CLI names for compatibility, but
they operate on durable job directories, not the retired JSONL queue.
