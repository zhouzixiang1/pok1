# 2026-07-15 active-trust cleanup

Everything in this directory is `legacy-untrusted` under the repository root
contract.  It is retained only as historical source and must not be imported,
executed, copied, rated, or used as evolution evidence.

## `legacy_research_eval/`

The two files were moved from active `scripts/research_eval/` during the
national TCP/evolution alignment review.  The retired harness accepted
arbitrary bot entry paths, launched them directly on the host, and referenced
detached research worktrees plus external neural assets.  It therefore violated
the strict five-file ABI, managed execution boundary, and archive-zero-authority
policy.  No active production consumer referenced it; the only active reference
was a test exemption for its hard-coded local paths, which was removed with the
move.
