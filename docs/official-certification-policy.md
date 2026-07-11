# Official EXE Certification Policy

> The National Web Arena (`/arena`) is a local diagnostic and presentation
> tool. Its events, THP files, and results have `diagnostic_only` authority and
> can never create, replace, or satisfy an official Windows EXE certificate.

The Windows national platform is a protocol and runtime-compliance oracle. It
never contributes to Glicko, H2H, source-selection strength, chip-EV, or poker
strategy scores.

## Formal Certificate

`official-full-v4` is the immutable formal profile for every new national bot:

- five candidate self-play rounds;
- three rounds against one policy-eligible native opponent;
- exactly 70 hands in every round;
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

A v4 certificate binds:

- the exact candidate and opponent artifact hashes;
- the full profile and official-opponent selection receipt;
- EXE, Wine/UI, harness, wire parser, attribution, and policy fingerprints;
- a deterministic 5+3 round receipt;
- the official evidence manifest and content-addressed archive receipt;
- the issuing Ed25519 identity.

The certificate is signed with the local key selected by
`POK_OFFICIAL_SIGNING_KEY` and verified against the tracked trust root
`web/core/official_certifier_allowed_signers`. The private key is never stored
in Git. Production verification cannot replace this trust root through an
environment variable; tests may inject a temporary trust file only through an
explicit API. The doctor also validates the signed append-only verdict ledger;
it never creates or repairs authority state. Run it before an expensive suite:

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

The v142 official-opponent anchor is readiness-gated: it remains available only
until two other signed full certificates are usable. Historical parent/rating
grants sunset at the tracked migration boundary. This avoids deleting the
existing rating population at once while ensuring grandfathering cannot become
a permanent path for new output.

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

# Inspect/reconcile durable jobs
python3 scripts/official_certify.py queue-status
python3 scripts/official_certify.py process-queue --limit 4
```

`queue-status` and `process-queue` keep their CLI names for compatibility, but
they operate on durable job directories, not the retired JSONL queue.
