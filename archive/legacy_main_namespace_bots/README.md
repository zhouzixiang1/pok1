# Legacy main-namespace bots (archived on `tencent-cloud-runtime`)

These directories (`national_v143`, `national_v156`) are **published history
from the `main` branch** — the original `national_tcp_policy_v1` epoch that
started version numbering at 143. They were committed under `bots/` on `main`
and inherited by the `tencent-cloud-runtime` branch when it forked.

## Why they are here

On the cloud branch the evolution system was refactored to restart version
numbering from **1** under the `national_cloud_v` namespace
(`ARCHIVED_VERSION_HIGH_WATER = 0`, `FIRST_STRICT_POLICY_VERSION = 1`). The
cloud epoch authority already ignores these main-namespace directories
entirely — `active_bots = []`, `version_authority_high_water = 0`,
`next_v = 1`.

They were moved here so `bots/` contains **only** cloud-namespace candidates
(`national_cloud_v*`), keeping the working tree clean and unambiguous.

## Authority

These artifacts are **legacy-untrusted** per `AGENTS.md`. Active code must
never import, execute, scan, or rate them. Their original publication tags
(`national-bot-v143`, `national-bot-v156`, `national-high-water-v143/v156`)
remain valid history on `main` and are untouched.
