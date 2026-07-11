# Official Platform Attestations

Each completed `national_v<N>` bot publishes one compact JSON attestation in
this directory. The attestation is committed atomically with the bot and bound
by the annotated `national-bot-v<N>` tag to:

- the immutable bot artifact hash;
- the exact `national_v<N>` subject label, so identical bytes cannot reuse a
  certificate under another version;
- the official EXE policy and platform fingerprint;
- SHA-256 manifests for deterministic evidence and the raw-evidence archive;
- an Ed25519 signature verified against the repository trust root;
- the full-certificate digest.

Large raw EXE artifacts (THP records, wire logs, bot logs, screenshots, and
Wine logs) are packed into a deterministic content-addressed archive under
`POK_OFFICIAL_EVIDENCE_STORE` (default:
`~/.local/share/pok/official-evidence`). Their archive and per-file hashes are
retained here, while transient working copies may remain under
`web/core/results/` for convenient inspection.

Full-mode pass, candidate-failure, and inconclusive suites are all archived.
The API distinguishes a retained standalone evidence JSON from evidence that
is available through the verified archive; either source means raw evidence is
available. Mutable status text cannot revoke or block a bot without the
content-bound deterministic status receipt for the same label and artifact.

LLM analysis is stored as an advisory sidecar outside the signed certificate.
It may explain deterministic findings and propose repair guidance, but it
cannot issue, block, or invalidate a certificate. These records certify
protocol and runtime compliance only. They must never be used as poker-strength
evidence or rating input.
