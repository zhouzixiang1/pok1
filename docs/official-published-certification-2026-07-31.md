# Official `full --published` certification (2026-07-31)

This document records the new `official_certify.py full --published` path that
certifies an **already-published** strict bot whose pipeline checkpoint has been
cleared, so a staging-published bot can be admitted into the rating pool /
official opponent pool without a live checkpoint.

## The gap this closes

`ROLE_RATING_POOL` and `ROLE_OFFICIAL_OPPONENT` **always require a full official
certificate** (`bot_namespace.py:58-62, 820-821`), regardless of
`POK_ALLOW_STAGING_AS_PARENT` (which only relaxes `ROLE_PARENT_SOURCE`). So a
staging-published bot (no official certificate) is admitted to
`get_active_bots()` as a *parent*, but the rating daemon's `bot_path()`
(`elo_daemon_persistence.py:74-87`) re-validates it under the stricter
`ROLE_RATING_POOL` and raises:

```
RuntimeError: rating bot is not a strict published policy artifact:
  national_cloud_v<N>:signed_full_official_certificate_required;official_certificate_digest_invalid
```

→ daemon rc=1 crash-loop → the generation it is rating cannot progress.

Normal `official_certify.py full` builds its quality admission from the **live
pipeline checkpoint** (`build_formal_quality_admission`,
`official_platform_harness.py:311-631`): it reads `pipeline_state.json` and
binds the candidate bytes to the checkpoint-owned quality/capability/probe
ledger. But an **already-published** bot has no live checkpoint (publication
clears it), so `full` fails with `formal-quality-admission-blocked`. This was
a real recovery-design gap: a staging bot could be published but never
certified, so it could never enter the rating pool.

## The `--published` path

`scripts/official_certify.py full --published` substitutes a **published-tag
proof** for the checkpoint admission. The proof basis is strictly stronger:

- `published_bot_identity(candidate)` (`bot_artifact.py:580-761`) returns
  `published: True` only when the on-disk candidate bytes equal the published
  tag bytes (`tag_artifact_hash == artifact_hash == hash_path(candidate)`) AND
  the tag commit is on the publication branch AND the completion tree matches
  main AND the working tree matches main AND there are no untracked files.
- The admission receipt (`build_published_quality_admission`,
  `official_platform_harness.py`) carries a distinct kind
  (`official-published-quality-admission`) with `candidate_hash`, `published_tag`,
  `published_commit_oid`, and a content-bound `admission_digest`.

These published-state invariants cover every threat the checkpoint admission
guards (byte drift, stale receipt, foreign certificate), because publication is
itself a content-bound, immutable proof.

### Spec validation dispatches by admission kind

`normal_full_quality_admission_required` / `normal_full_quality_admission_issues`
(`official_certification.py:185-265`) now dispatch by `quality_admission.kind`:
a published-kind admission is validated by the published-kind structural
validator (`formal_published_quality_admission_integrity_issues`) instead of
the checkpoint-kind validator. This mirrors the existing exemption pattern:
`bootstrap_control_id is not None` already exempts the first-strict bootstrap
path from the checkpoint admission. `--published` is the published-bot
equivalent. The full-v5 profile (5 self-play + 3 opponent × 70 hands,
`FULL_POLICY_ID`) and certificate signing / verdict-ledger flow are unchanged.

### Safety invariants preserved

1. **Candidate bytes == published tag bytes**: enforced by
   `published_bot_identity(candidate)["published"] is True` (which implies
   `tag_artifact_hash == hash_path(candidate)` + tree equality + no untracked).
2. **Tag commit on the publication branch**: prevents a foreign tag.
3. **Full-v5 profile**: `validate_spec` still enforces 5+3×70 + `FULL_POLICY_ID`.
4. **Certificate signing / verdict ledger**: unchanged; the job worker still
   signs the certificate and appends the verdict-ledger entry after the EXE run.
5. **No circular requirement**: the published admission does NOT require the
   candidate to already carry a full certificate (that is what the full job
   itself produces — requiring it here would be circular). It requires only
   that the candidate is a published strict artifact with bytes matching the
   tag, plus an eligible certified opponent to play against.

## First certification vs. re-certification

- **First full certification** of a staging bot: `--published` proves the
  candidate is the published artifact; the opponent (e.g. `national_cloud_v1`
  or `national_cloud_v11`, both already certified) supplies the certified
  adversary. After the EXE run, the job signs the certificate and writes the
  verdict-ledger entry; the bot becomes `official-certified (mode=full)`.
- **Re-certification** of a bot whose bytes drifted after a deploy: the
  published-bot identity check fails (`published: False` because the working
  tree no longer matches the tag), so `--published` fails closed — exactly the
  desired fail-safe.

## Regressions

- `web/tests/test_official_platform_harness.py`:
  `test_formal_published_quality_admission_integrity_rejects_wrong_kind`,
  `test_formal_published_quality_admission_integrity_rejects_tampered_digest`,
  `test_formal_published_quality_admission_integrity_accepts_valid_receipt`,
  `test_build_published_quality_admission_rejects_unpublished_identity`,
  `test_build_published_quality_admission_builds_receipt_for_published_bot`.
- `web/tests/test_official_certify_cli.py`:
  `test_cli_full_published_uses_published_admission_path`,
  `test_cli_full_published_blocks_when_published_admission_invalid`.
- `web/tests/test_official_certification.py`:
  `test_normal_full_quality_admission_dispatches_published_kind`,
  `test_normal_full_quality_admission_published_kind_rejects_invalid_receipt`.

## v27 recovery operation (2026-07-31)

v27 was published as staging (`publication-tier: staging`) but never
full-certified (its only official job was a smoke gate that crashed on the
`import random` bug — fixed separately in commit `ed5f6e02`). To admit it into
the rating pool:

```bash
cd /home/ubuntu/pok1/.evolution_pok
python scripts/official_certify.py full bots/national_cloud_v27 --published \
  --opponent bots/national_cloud_v11 --wait-if-busy
```

(opponent `national_cloud_v11` is `official-certified`, mode=full). After the
EXE completes: `official_certification/status/national_cloud_v27.json` becomes
`official-certified (mode=full)`, the verdict ledger gains a v27 entry, and
`resolve_national_bot_spec("national_cloud_v27", ROLE_RATING_POOL).eligible`
becomes `True` — unblocking the rating daemon.

## Related

- `AGENTS.md` §「Official acceptance and required certification」.
- `docs/abandon-death-loop-and-workflow-id-reuse-2026-07-30.md` (workflow-id
  context).
