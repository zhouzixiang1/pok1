# Frontend / backend / business-logic mismatch audit — branch-portability

Date: 2026-07-29
Branch: `tencent-cloud-runtime`
Symptom: the cloud evolution line silently mis-rendered or rejected
first-strict generations because the frontend and test fixtures hardcoded
main-branch version literals (`143`/`142`/`national_v143`/`national-bot-v*`)
that do not exist on the cloud epoch (floor `FIRST_STRICT_POLICY_VERSION=1`,
high-water `ARCHIVED_VERSION_HIGH_WATER=0`, namespace `national_cloud_v`,
tags `national-cloud-bot-v*`).

## Root cause

Three layers hardcoded branch-specific literals instead of validating the
backend-provided authority identity:

1. **Frontend TypeScript validators** pinned the first-strict candidate/source
   version to `143`/`142`, rejecting every valid cloud generation (whose
   `next_v` starts at 1).
2. **Test fixtures** hardcoded `142`/`143` as the epoch high-water / strict
   floor, synthesizing impossible cloud epoch states.
3. **The `_tagged_test_bot_versions` conftest helper** globbed
   `refs/tags/national-bot-v*` (main-branch prefix), returning empty on cloud
   and silently skipping every `@requires_active_bot` test.

## Fix catalog (severity-ranked)

### HIGH — functional breaks (logic, not cosmetic)

| File:line | Was | Now |
|---|---|---|
| `web/frontend/src/api/agentActivity.ts:189` | `value.next_v < 143` rejected all cloud generations | `value.next_v < FIRST_STRICT_POLICY_VERSION` (imported from `canonicalGenerationIdentity.ts`) |
| `web/frontend/src/lib/evolutionStreamController.ts:612` | `isPostPublicationHandoff` required `version >= 143`; cloud handoff SSE events dropped as malformed | `value.version >= FIRST_STRICT_POLICY_VERSION` |
| `web/frontend/src/components/evolution/OfficialCertificationProgress.tsx:32-34` | `transitionMatches` pinned `next_v===143 && source_v===142`; first-strict transition card unreachable on cloud | validates `transition.candidate_version === generation.next_v` and `transition.source_v` against `generation.source_v` (both branch-configurable) |
| `web/frontend/src/api/control.ts:90-91` | `OperatorTransition.candidate_version: 143 \| null` / `source_v: 142 \| null` type literals | `number \| null` (branch-configurable) |
| `web/frontend/src/pages/Overview.tsx:208` | `?? 142` fallback showed wrong high-water | `?? 0` (cloud high-water floor) |
| `web/frontend/src/lib/canonicalGenerationIdentity.ts:7` | `FIRST_STRICT_POLICY_VERSION` was a local const | exported so validators can import it |
| `web/tests/conftest.py:126,141` | tag glob `refs/tags/national-bot-v*` returned empty on cloud → `requires_active_bot` tests silently skipped | `f"refs/tags/{ACTIVE_TAG_PREFIX}*"` + `parse_tag_version()` (branch-aware) |
| `web/tests/conftest.py:428` | bot fixture dir hardcoded `national_v{version}` → `FileNotFoundError` for `isolate_state` tests | `bot_name(fixture_version)` |

### MEDIUM — wrong-namespace test fixtures (now branch-portable)

22 test fixture files + 1 script swept to use `STRICT_TARGET_V`,
`STRICT_SOURCE_V`, `bot_name(v)`, `bot_tag(v)`, `strict_bot_name()`,
`strict_bot_tag()`, `ACTIVE_TAG_PREFIX`, `HIGH_WATER_TAG_PREFIX` instead of
hardcoded `142`/`143`/`national_v14x`/`national-bot-v*`. See the commit for the
full file list.

`scripts/recover_completed_first_strict_publication.py` tag prefix corrected
from hardcoded `national-bot-v`/`national-high-water-v` to
`ACTIVE_TAG_PREFIX`/`HIGH_WATER_TAG_PREFIX`.

### LOW — stale operator-facing message strings

`web/core/generation_scheduler.py` and `web/core/official_certification.py`
operator log/docstring messages referenced literal `v143`/`v142`; corrected to
use `FIRST_STRICT_POLICY_VERSION`/`ARCHIVED_VERSION_HIGH_WATER` so the message
matches the actual version on every branch.

## Invariants preserved (verified)

- No authority digest / CAS / checkpoint identity computation was changed.
- The frontend's fail-closed behavior on unknown/malformed values is unchanged.
- `evolutionStatusMatchesActiveGeneration` still reconciles SSE `status`
  identity against polled `active_generation` (the version fields were never
  part of that reconciliation — it uses run/workflow/revision/stage).
- The `epoch_authority_unavailable` fail-closed sentinel and the observer
  cooperative-await (90s) are unchanged.
- The three `strict_epoch_projection` coherence checks in `_sync_evolution_fields`
  (handoff journal / certification transition / stability observation) are
  unchanged and remain non-redundant.

## Test verification

- Backend `web/tests`: full suite green (post-fix).
- Frontend `npm test` (90 tests) + `npm run lint` + `npm run build`: green.
- `sever/tests`: 36 passed.
- The `_tagged_test_bot_versions` fix was verified to resolve
  `national-cloud-bot-v1` / `national-cloud-bot-v11` → `bots/national_cloud_v1`
  / `bots/national_cloud_v11` on cloud.

## Why the frontend tests did not catch this sooner

The frontend test fixtures (`sseController.test.mjs`, `contractFixtures.test.mjs`,
`domainViews.test.mjs`) intentionally use `national_v143` as arbitrary test
labels because `canonicalGenerationIdentity.ts`'s regex accepts BOTH
`national_v` and `national_cloud_v`. So those tests passed on both branches —
they test the validator's regex, not the business authority. The break was in
the **production validators** (`agentActivity.ts`, `evolutionStreamController.ts`,
`OfficialCertificationProgress.tsx`) that the contract-closure test
(`test_v143_numeric_high_water_source_does_not_select_normal_certification_profile`)
asserted against the OLD branch-specific literals. That test was updated to
assert the new branch-portable contract.
