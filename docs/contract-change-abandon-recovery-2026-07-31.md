# Contract-change abandon recovery (2026-07-31)

## The deadlock

A generation that reached a **publication-family stage** (`verified`,
`publishing`, `official_certifying`) and is then hit by a **contract-critical
deploy** (a change to any evaluation-contract path, e.g. `web/core/
publication_transaction.py`) enters a deadlock with no supported recovery:

- **Cannot resume.** Those stages' head-drift resume policy has
  `requires_contract_unchanged=True` (`pipeline_state.py`
  `HEAD_DRIFT_RESUME_POLICY`). A deploy that changed an evaluation-contract
  path makes `evaluate_head_drift` return `contract_unchanged=False`, so
  `can_resume=False` (`pipeline_recovery.py:517-529`) and the launch barrier
  fires `pipeline_recovery_blocked(repo_baseline_head_mismatch)`. The
  `repo_baseline` record carries a saved `evaluation_contract.hash` over the
  exact-file contents at the old HEAD, so the contract change is content-bound,
  not just a HEAD label.
- **Cannot be generically abandoned.** `verified`/`publishing`/
  `official_certifying` are in the `never_disposable` set
  (`pipeline_state.py:1645-1652`), so `generic_abandon_block` refuses with
  `publication_or_certification_stage_not_disposable` and `POST
  /api/control/abandon` mirrors that guard.
- **The bootstrap bypass does not apply.** `_bootstrap_contract_change_abandon_
authority` (which skips the `never_disposable` guard when non-`None`) is
hard-bound to `next_v == FIRST_STRICT_POLICY_VERSION` and
`stage == official_bootstrap_required`
(`bootstrap_contract_recovery.py:1142-1150`,
`bootstrap_contract_recovery_historical.py:188-190`).

This was a **recovery-design gap**: a non-bootstrap generation stranded at a
non-disposable stage by a required contract-critical fix had neither a resume
path nor an abandon path.

## The live incident (v25)

- v25 reached `verified` (rev=17, `generation:25:workflow-v1`, staging tier).
- It then hit a separate production bug: the staging publication-intent
  validator demanded phantom checkpoint stage names (`precommit_passed` /
  `staging_publishing`) that no code ever writes, so every `commit_bot`
  looped on `publication_intent_origin_stage_invalid` for 3+ hours (fixed in
  `c94c8a7b` — staging must publish from the real stage `verified`).
- Deploying that required fix changed `publication_transaction.py`, an
  evaluation-contract path, which then stranded v25 in the deadlock above.

## The recovery path (added 2026-07-31)

A general, opt-in **contract-change abandon authority**,
`_operator_contract_change_proof`, mirrors the bootstrap authority's safety
contract:

- **Opt-in / default-guard-preserving.** Returns `None` when no proof is
  supplied, so the default `never_disposable` guard is untouched for every
  other caller (MCP tool, HTTP `/api/control/abandon`, orchestrator routes).
- **Rebuilt on every lock boundary.** The proof is re-derived from the live
  checkpoint + Git state inside `_do_abandon_generation` (initial guard +
  workflow-fence recompute + publication-lock recompute), exactly like
  `validate_claim_for_checkpoint`. A reviewed dry-run digest must match the
  live proof at execute time.
- **Proves the contract change.** Uses `evaluate_head_drift(root,
  baseline_head, current_head, ...)` with `baseline_head` read from
  `checkpoint["repo_baseline"]["head"]` and `current_head` from `git rev-parse
  HEAD`. Requires `contract_unchanged is False` and non-empty
  `head_contract_paths` — i.e. the deploy genuinely changed evaluation-contract
  paths, so the candidate is unrecoverable under the new source.
- **Tamper-proof.** The proof's `baseline_head`, `current_head`,
  `changed_contract_paths`, and 5-field checkpoint identity must each match the
  live values; the `claim_digest` is the canonical digest over those
  revalidated fields (not a caller-supplied token).
- **Reason prefix isolation.** `national_contract_change_abandon:<digest>`,
  distinct from `official_bootstrap_contract_change:`, so the bootstrap-only
  external binding validator (`validate_canonical_abandon_external_binding`)
  stays a no-op for these claims.

The transaction body (`_finalize_checkpoint_abandon_transaction`) is unchanged
and stage/version-agnostic: it quarantines the candidate via atomic
`os.replace` (bytes preserved, not deleted), clears the checkpoint by exact
CAS, and appends the terminal `abandoned_versions.jsonl` receipt. The only
addition is the authority that crosses the `never_disposable` boundary.

## Operator procedure

`scripts/abandon_contract_change_generation.py` drives the dry-run → review →
execute flow (mirrors `scripts/abandon_parked_bootstrap_contract_change.py`).
It only accepts publication-family stages and only when the contract genuinely
changed.

```bash
cd /home/ubuntu/pok1/.evolution_pok   # must run from the runtime checkout
sudo systemctl stop pok-evolution      # abandon requires a stopped runtime

# 1. Dry run: prints the proof (changed_contract_paths, claim_digest, ...)
python /home/ubuntu/pok1/scripts/abandon_contract_change_generation.py \
  --expected-workflow-run-id generation:25:workflow-v1 \
  --expected-next-v 25 --expected-source-v 1 \
  --expected-checkpoint-revision 17 --expected-checkpoint-stage verified

# 2. Review changed_contract_paths and claim_digest.

# 3. Execute with the reviewed digest:
python /home/ubuntu/pok1/scripts/abandon_contract_change_generation.py --execute \
  --acknowledge-runtime-checkout --claim-digest <reviewed-digest> \
  --expected-workflow-run-id generation:25:workflow-v1 \
  --expected-next-v 25 --expected-source-v 1 \
  --expected-checkpoint-revision 17 --expected-checkpoint-stage verified

# 4. Verify: checkpoint cleared, bots/national_cloud_v25/ moved to
#    RESULTS_DIR/policy_epoch_abandon_transactions/<txid>/candidate,
#    abandoned_versions.jsonl appended a v25 receipt. Then restart.
sudo systemctl start pok-evolution
```

## Tests

- `tests/test_abandon_helper.py::test_contract_change_authority_is_none_without_proof`
  — default guard preserved.
- `tests/test_abandon_helper.py::test_contract_change_authority_rejects_out_of_scope_stage`
  — only publication-family stages eligible.
- `tests/test_abandon_helper.py::test_contract_change_authority_validates_real_drift`
  — real drift passes, forged/understated proofs rejected.
- `tests/test_abandon_helper.py::test_verified_stage_still_refused_without_contract_change_proof`
  — negative anchor: `verified` still `never_disposable` without a proof.
- `tests/test_abandon_helper.py::test_verified_stage_abandons_with_valid_contract_change_proof`
  — positive anchor: candidate quarantined, checkpoint cleared, receipt appended.
- `tests/test_abandon_helper.py::test_verified_stage_abandon_rejects_mismatched_contract_change_proof`
  — mismatched proof surfaces typed `contract_change_authority_invalid`.
