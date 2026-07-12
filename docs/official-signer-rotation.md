# Official certifier signer rotation

The epoch-1 official-certifier private key must be treated as exposed.  Its
public key remains in `web/core/official_certifier_allowed_signers` solely so
the application can validate the exact, policy-pinned national_v141 bootstrap
chain.  Application policy rejects every other epoch-1 record even when its
OpenSSH signature is cryptographically valid.

Epoch 2 was activated on the trusted host on 2026-07-12.  The tracked policy is
now `current_epoch: 2`, `state: active` and binds this exact signer identity:

- fingerprint: `SHA256:93sCwWhJf1/y3HGhZOaOdHmiZHfb1VjMgg5jVZ/2urQ`;
- normalized public-key SHA-256:
  `196ebd37a4a365021c2bce2f3cada30f3e8bf19630a72aa57e03da3f310a9a54`;
- trust-policy digest:
  `5c4fbf0f1418a66162e2ca85a02833992328fe70d401b66c49d4c251d85faf15`.

The operator-local private key is
`~/.config/pok/official_certifier_ed25519_epoch2`; it is never stored in Git.
The previous private-key filename has been removed from service configuration
and its retained local material is quarantined as
`official_certifier_ed25519_epoch1_REVOKED` with mode `000`.  Application policy
would still reject it for new issuance even if its filesystem mode changed.

### Explicit threat-model boundary

This host currently runs the orchestrator, Claude SDK tool processes, official
finalizer, and key owner as the same Unix uid.  File mode `0600` prevents other
OS users from reading the key, but it does **not** isolate the key from Bash or
Python launched by a same-uid LLM agent.  The current SDK hooks are workflow
guards, not an OS security boundary; path-string denial rules would be
bypassable and must not be described as key isolation.

Accordingly, the epoch-2 signature authenticates certificate/ledger bytes only
for a verifier that independently anchors the expected public-key fingerprint
outside the repository being verified.  If the repository commit is already
the verifier's sole trust root, canonical hashes and Git objects already bind
the same content.  The signature does not prove that a malicious same-uid
orchestrator/LLM could not issue another signature.  The signing report and
`doctor` output expose this as `same_uid_llm_resistance: false` and
`out_of_band_public_key_anchor: required-for-independent-verification`.
Signatures remain confined to formal certificate/ledger finalization and have
no runtime, match-strength, Glicko, or decision-hot-path authority.

The ledger and its signed head currently live on the same same-uid writable
filesystem and have no independently protected latest-head checkpoint.  A
rollback to an older, internally valid ledger/head pair is therefore outside
the threat model.  Per-entry and head signatures support portable validation
and crash recovery relative to the present head; they do not create a
transparency log or monotonic anti-rollback authority.

Resistance to a malicious agent requires moving the private key to a different
OS identity (or host/HSM) behind a non-generic finalizer that independently
validates the complete formal receipt before signing.  Until that boundary is
implemented and tested, the operator account itself remains trusted.

The active trust boundary therefore has the following behavior:

- existing pinned v141 certificate, archive receipt, ledger sequence 1, signed
  head, and bootstrap root remain readable;
- only the exact policy-pinned epoch-1 records validate under the retired key;
- all new certificate, ledger-entry, and ledger-head payloads must bind epoch 2,
  its fingerprint, and the tracked policy digest; and
- `signing_environment_report()` fails closed if the operator-local key, its
  public half, either tracked trust file, or the epoch binding drifts.

## Verification before official work

The launcher defaults `POK_OFFICIAL_SIGNING_KEY` to the epoch-2 path.  Before
starting or resuming any official certification job, verify signer readiness
and the existing signed ledger together:

```bash
export POK_OFFICIAL_SIGNING_KEY="$HOME/.config/pok/official_certifier_ed25519_epoch2"
PYTHONPATH=web/core python - <<'PY'
import json
from official_certificate_signing import signing_environment_report
from official_verdict_ledger import ledger_integrity

signing = signing_environment_report()
ledger = ledger_integrity()
print(json.dumps({"signing": signing, "ledger": ledger}, indent=2))
raise SystemExit(0 if signing.get("ok") and ledger.get("valid") else 1)
PY
```

Activation is not permission to bypass the normal official workflow.  The
first frozen strict candidate still uses the explicit, one-time
`bootstrap-full` command and the repository-pinned v141 root.  Normal candidates
continue to use `full`; neither path may be invoked merely to test signer
readiness.

## Rotation procedure for a future epoch

Stop the official certifier and any process that can append the verdict ledger.
Do not invoke `render-rotation` against the currently active policy: it is
designed to fail unless a reviewed trust-policy transition has first placed a
new epoch in `rotation-required` state.

A future rotation must be implemented and reviewed as a complete trust-epoch
change.  It must:

1. pin every epoch-2 record that must remain historically valid by exact record
   and signature hashes;
2. move epoch 2 to `historical-validation-only`, increment `current_epoch`, and
   place that new epoch in `rotation-required` state;
3. preserve the already pinned epoch-1 v141 chain unchanged;
4. generate a fresh operator-local Ed25519 key at a new epoch-specific path;
5. use `render-rotation` only after the tracked pending policy is deployed, then
   review both rendered trust files before activation; and
6. rerun the readiness-plus-ledger check above before any official job resumes.

The renderer reads only a `.pub` file; it never creates, reads, moves, or
overwrites private material.  A rollback may stop issuance, but must never
reactivate a retired private key as a current signer.  Quarantine or securely
destroy retired private material according to the host key-handling policy.
