# Official certifier signer rotation

> **Branch note (`tencent-cloud-runtime`).** This branch uses a **server-owned
> Ed25519 signer at epoch 3** (fingerprint
> `SHA256:5C70Tt/aIzq60HlCQBXLZ0MdTWN3vIWk6HjkEU+nsTk`). The text below
> describes the cloud runtime's signer state. The older operator-host epoch-2
> rotation narrative is retained as historical context but is **stale**
> relative to this branch's server-owned epoch-3 key.

The epoch-1 official-certifier private key must be treated as exposed. Its
public key remains in `web/core/official_certifier_allowed_signers` solely so
the application can validate the exact historical national_v141 certificate
and ledger chain. That chain has no executable bootstrap authority. Application
policy rejects every other epoch-1 record even when its
OpenSSH signature is cryptographically valid.

## Current active signer — epoch 3 (server-owned, this branch)

Epoch 3 was activated for the `tencent-cloud-runtime` cloud line. The tracked
policy (`web/core/official_certifier_trust_policy.json`) records
`current_epoch: 3`, `state: active`, and binds this exact signer identity:

- fingerprint: `SHA256:5C70Tt/aIzq60HlCQBXLZ0MdTWN3vIWk6HjkEU+nsTk`;
- normalized public-key SHA-256:
  `ac0fcb6685b585f081f40c67e7551781d95b7527f62aa93634410203f89ea986`.

The epoch-3 private key is **server-owned** (generated and held on the Tencent
Cloud runtime host), not the operator's local epoch-2 key. It is never stored
in Git. The allowed-signers file
(`web/core/official_certifier_allowed_signers`) lists all three epoch keys
under namespace `pok-official-cert-v4`:

- `pok-official-certifier-v4` (epoch 1, retired validation-only);
- `pok-official-certifier-epoch-2` (epoch 2, historical);
- `pok-official-certifier-epoch-3` (epoch 3, active).

## Historical context — epochs 1 and 2

Epoch 1 (retired, validation-only) is tied to the retired `national_v141`
signed-ledger chain. Only the exact policy-pinned epoch-1 records validate
under the retired key; all new issuance is rejected under it.

Epoch 2 was activated on a trusted operator host on 2026-07-12 with
fingerprint `SHA256:93sCwWhJf1/y3HGhZOaOdHmiZHfb1VjMgg5jVZ/2urQ` and private
key at `~/.config/pok/official_certifier_ed25519_epoch2`. On the cloud branch
this operator-host key is unavailable; the server-owned epoch-3 key supersedes
it for all cloud-line certificate issuance. Epoch 2 remains listed in
`allowed_signers` for historical validation of certificates it produced before
the rotation.

### Explicit threat-model boundary

The host runs the orchestrator, Claude SDK / `claude_agent_sdk` tool processes,
official finalizer, and key owner as the same Unix uid (or, on the cloud
runtime, the `ubuntu` service user). File mode `0600` prevents other OS users
from reading the key, but it does **not** isolate the key from Bash or Python
launched by a same-uid LLM agent. The SDK hooks are workflow guards, not an OS
security boundary; path-string denial rules are bypassable and must not be
described as key isolation.

Accordingly, a signature authenticates certificate/ledger bytes only for a
verifier that independently anchors the expected public-key fingerprint outside
the repository being verified. If the repository commit is already the
verifier's sole trust root, canonical hashes and Git objects already bind the
same content. The signature does not prove that a malicious same-uid
orchestrator/LLM could not issue another signature. The signing report and
`doctor` output expose this as `same_uid_llm_resistance: false` and
`out_of_band_public_key_anchor: required-for-independent-verification`.
Signatures remain confined to formal certificate/ledger finalization and have
no runtime, match-strength, Glicko, or decision-hot-path authority.

Resistance to a malicious agent requires moving the private key to a different
OS identity (or host/HSM) behind a non-generic finalizer that independently
validates the complete formal receipt before signing. Until that boundary is
implemented and tested, the service account itself remains trusted.

## Verification before official work

Before starting or resuming any official certification job on this branch,
verify signer readiness and the existing signed ledger together:

```bash
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

Activation is not permission to bypass the normal official workflow. The first
strict candidate uses the explicit, one-time `bootstrap-first-strict` command
and the current system-owned `first_strict_control_v1`; archived v141 bytes are
never an input. Normal candidates continue to use `full`; neither path may be
invoked merely to test signer readiness.

## Rotation procedure for a future epoch

Stop the official certifier and any process that can append the verdict ledger.
Do not invoke `render-rotation` against the currently active policy: it is
designed to fail unless a reviewed trust-policy transition has first placed a
new epoch in `rotation-required` state.

A future rotation must be implemented and reviewed as a complete trust-epoch
change. It must:

1. pin every epoch-3 record that must remain historically valid by exact record
   and signature hashes;
2. move epoch 3 to `historical-validation-only`, increment `current_epoch`, and
   place that new epoch in `rotation-required` state;
3. preserve the already pinned epoch-1 v141 chain unchanged;
4. generate a fresh server-owned (or HSM-backed) Ed25519 key at a new
   epoch-specific identity;
5. use `render-rotation` only after the tracked pending policy is deployed, then
   review both rendered trust files before activation; and
6. rerun the readiness-plus-ledger check above before any official job resumes.

The renderer reads only a `.pub` file; it never creates, reads, moves, or
overwrites private material. A rollback may stop issuance, but must never
reactivate a retired private key as a current signer. Quarantine or securely
destroy retired private material according to the host key-handling policy.
