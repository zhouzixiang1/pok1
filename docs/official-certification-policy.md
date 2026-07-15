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
- the exact natural-hand-70 proof emitted by the official 2021 EXE: wire hand
  starts 1..70, wire settlements 1..69, and a strict 70-state THP whose named
  earnings and footer cross-bind the omitted terminal settlement; a synthetic
  70th wire settlement is not accepted by the formal profile;
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
use `official-job-v3` in `web/core/official_certification_job.py`. Retired queue
implementations are archived and are not importable by the active control
plane.

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

## Eligibility And Epoch Bootstrap

Eligibility is role-specific:

- `official_opponent`: may be used in a formal EXE suite;
- `parent_source`: may seed a generation or crossover;
- `rating_pool`: may participate in native strength evaluation.

Every active role in `national_tcp_policy_v1` requires the strict typed-policy
artifact contract. `official_opponent` additionally requires a published,
signed, content-valid `official-full-v5` certificate. Retired epoch grants and
artifacts have no `parent_source`, `rating_pool`, or ordinary opponent role;
their ratings and head-to-head rows are not migrated into the new epoch.

The sole one-time ceremony uses `first_strict_control_v1`, materialized from the
current system-owned typed-policy runtime and its checked-in policy asset. It
can certify only the fresh, unpublished `national_v143` candidate while both
the active policy pool and strict publication pool are empty. The control is
not a normal opponent, parent, rating bot, active-pool fallback, or automatic
evolution choice; its wire results have no strength or prompt authority. The
old v141 signed-ledger root is retained only as historical signature-validation
metadata. Its archived bot bytes cannot be resolved, parsed as a bot artifact,
sealed, selected, or executed.

The operator runs `bootstrap-first-strict`. Its selection binds the exact
candidate hash, control artifact hash, current runtime/control manifests,
empty-pool receipt, parked checkpoint/evaluation contract, and full 5+3x70
suite. Only a successful signed `official-full-v5` verdict consumes that exact
receipt; failed or inconclusive runs do not create a normal opponent.
This is an operational state-machine guarantee, not a cryptographic guarantee
against a same-uid rollback of both the ledger and its signed head; durable
rollback resistance would require an independently protected monotonic anchor.

After that manual suite succeeds, the jobs API may project
`ready_to_finalize` only for the exact existing certificate that passes the
complete content-bound validator (candidate hash, signed receipt, evidence,
ledger, control-selection receipt, job envelope, parked checkpoint,
workflow/evaluation contract, and policy). The operator must then run
`finalize-first-strict --acknowledge-publish-first-strict`. That CLI establishes
a process-ID-scoped guard immediately around the internal publication handler,
which reruns the consumed-control-aware authorization before staging and
tagging. `commit_bot` remains unavailable to the LLM and ordinary HTTP path. A
status label, mutable JSON, or ledger entry alone is insufficient. Publishing
that first attestation creates the first normal full-v5 opponent; subsequent
candidates use the ordinary policy path.

When the first verified candidate finds no normal opponent, the pipeline parks
at `official_bootstrap_required`. This is a deliberate stop barrier:
`next_tool` is empty, automatic recovery exits, and the LLM cannot call
`commit_bot` again or initiate bootstrap. The runtime guard unlocks publication
only inside the acknowledged operator finalize process and only after the
external `bootstrap-first-strict` result passes the full validator. Missing,
forged, stale, or candidate-mismatched certificates remain fail-closed.

While that exact v143 checkpoint remains parked, its sole request-bound manual
bootstrap durable job is visible read-only through
`GET /api/certification/jobs` and `GET /api/certification/jobs/{job_id}` with
`formal_authority=operator_bootstrap_full_v5_job`. This projection grants no
launch or cancellation authority: HTTP enqueue remains retired with status
410, the retired `/api/certification/queue` route is absent, and
`POST /api/certification/jobs/{job_id}/cancel` returns 404 for the bootstrap
job. Unbound bootstrap jobs, old-epoch jobs, v155 debris, identity drift, and
ambiguous duplicate jobs are hidden fail-closed.

The jobs projection also exposes a digest-bound operator transition:
`bootstrap_required` when no exact job exists, `bootstrap_running` for a live
job, `bootstrap_failed` with an explicit `--force` retry command for a terminal
failure, and `ready_to_finalize` only for the fully revalidated certificate and
completed bootstrap authorization. Every bootstrap job declares
`first_strict_control_v1`, `system_control`, exact 5/3×70, and zero strategy and
strength weight. Published certificate status derives the same profile from the
signed certificate spec rather than version arithmetic or mutable status JSON.

Lifecycle state is durable in annotated Git tags:

- `national-reaped-vN` is a permanent retirement tombstone;
- `national-reaped-registry-v1` proves legacy ledger migration;
- `national-high-water-vN` preserves the monotonic version floor.

Retired bots are not recertified into the new pool. A policy bot that fails is
repaired as a new content-bound candidate; the validator is never relaxed to
keep it active.

## Operator Commands

```bash
# Platform + signer + verdict-ledger readiness
python3 scripts/official_certify.py doctor

# First-host signed genesis (explicit and idempotent), then verify all readiness
python3 scripts/official_certify.py init-ledger
python3 scripts/official_certify.py doctor

# Durable full 5+3x70 request and wait for its terminal result
python3 scripts/official_certify.py full bots/national_v<N> --wait-if-busy

# One-time first-anchor bootstrap; only national_v143 in an empty strict pool
python3 scripts/official_certify.py bootstrap-first-strict bots/national_v143 \
  --control-id first_strict_control_v1 \
  --acknowledge-one-time-first-strict-control \
  --wait-if-busy

# Publish only the exact ready_to_finalize first-strict certificate
python3 scripts/official_certify.py finalize-first-strict \
  --acknowledge-publish-first-strict

# Inspect/reconcile durable jobs
python3 scripts/official_certify.py jobs-status
python3 scripts/official_certify.py reconcile-jobs --limit 4
```

`jobs-status` and `reconcile-jobs` operate directly on durable job directories.
The retired JSONL queue is neither a production path nor a compatibility API.
