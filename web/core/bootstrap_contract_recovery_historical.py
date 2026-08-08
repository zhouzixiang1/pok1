"""Historical-reopen subsystem extracted from bootstrap_contract_recovery.py.

This companion houses the cohesive cluster that reopens immutable old
terminal-job bytes and validates the canonical abandon transaction that
consumed an external contract-change claim:

* ``_validate_claim_envelope`` - validates the external schema-2 claim
  envelope (fields, digest, recovery profile, crossbinding) and dynamically
  reopens its first-strict journal proof.
* ``_historical_terminal_job_matches`` - reopens immutable
  job/result/verdict/ledger bytes without a live old candidate, recomputing
  each contract-failure diagnosis proof and requiring byte-identical output.
* ``_finalized_canonical_abandon`` - validates the exact canonical abandon
  transaction (claim + receipt + quarantined candidate) bound to the external
  proof.
* ``_finalized_canonical_abandon_matches`` - boolean wrapper.
* ``finalized_claim_result`` - returns the exact completed terminal result
  after checkpoint clearance (public operator entry point).
* ``incomplete_claim_resume_identity`` - reopens the checkpoint-cleared,
  finalize-receipt-missing crash prefix (public operator entry point).
* ``is_finalized_historical_bootstrap_job`` - guards a new v143 workflow
  against treating an old immutable job as a live ambiguous authorization
  (public operator entry point).

``bootstrap_contract_recovery.py`` retains thin delegate shells for every
moved symbol so external ``from bootstrap_contract_recovery import <name>``
and ``monkeypatch.setattr(bootstrap_contract_recovery, "<name>", ...)``
keep resolving exactly as before.

CRITICAL (wave-3 lesson, same as bootstrap_contract_recovery_legacy_wire):
EVERY intra-companion call to a symbol that is monkeypatched on the parent
module MUST route through ``_bcr.<name>(...)`` instead of the local name,
so a patch applied to ``bootstrap_contract_recovery.<name>`` propagates.
The monkeypatched parent symbols referenced by this cluster are:

* ``_historical_terminal_job_matches``        (patched in test suite)
* ``_finalized_canonical_abandon_matches``    (patched in test suite)
* ``_finalized_canonical_abandon``            (parent-resident; routed for parity)
* ``load_claim``                              (patched in test suite)
* ``_read_regular_exact``                     (patched in test suite)
* ``_legacy_causal_failure_diagnosis``        (patched in test suite)
* ``_called_allin_runout_failure_diagnosis``  (patched in test suite)
* ``_v65_contract_failure_diagnosis``         (patched in test suite)

Non-monkeypatched parent-resident helpers (``abandon_reason``,
``validate_canonical_abandon_external_binding``, ``_require_regular_directory``,
``_validate_contract_failure_diagnosis_envelope``) are also routed through
``_bcr.`` so the cluster has a single, uniform parent-attribute discipline
matching the legacy-wire precedent.  Only module-level constants
(``CLAIM_DIRNAME``, ``_HEX64``, ``_CAUSAL_FAILURE_DIAGNOSIS_KIND``,
``_CALLED_ALLIN_DIAGNOSIS_KIND``, ``_CALLED_ALLIN_PROFILE_ID``,
``_V65_DIAGNOSIS_KIND``, ``_V65_PROFILE_ID``) and the pure
``bot_artifact`` helpers (``canonical_digest``, ``hash_path``) are imported
once at top level, because frozenset constants are never monkeypatched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bot_artifact import canonical_digest, hash_path
from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
)

# Constants only (never monkeypatched): safe to bind once at import time.
from bootstrap_contract_recovery import (
    CLAIM_DIRNAME,
    CLAIM_KIND,
    CLAIM_SCHEMA_VERSION,
    EVALUATION_EPOCH,
    _CLAIM_FIELDS,
    _CAUSAL_FAILURE_DIAGNOSIS_KIND,
    _CALLED_ALLIN_DIAGNOSIS_KIND,
    _CALLED_ALLIN_PROFILE_ID,
    _HEX40,
    _HEX64,
    _STRICT_FILES,
    _V65_BASELINE_CONTRACT_VERSION,
    _V65_DIAGNOSIS_KIND,
    _V65_PROFILE_ID,
    _V65_REPAIR_CONTRACT_VERSION,
    BootstrapContractRecoveryError,
)

# Parent alias: every monkeypatched or parent-resident helper reference inside
# the cluster bodies below routes through ``_bcr.<name>`` so test patches on
# ``bootstrap_contract_recovery.<name>`` propagate.  Imported lazily inside a
# function would also work, but a single module-level alias matches the
# legacy-wire companion precedent and keeps the bodies verbatim-adjacent.
import bootstrap_contract_recovery as _bcr  # noqa: E402


def _validate_claim_envelope(
    claim: Any,
    expected_digest: str,
) -> dict[str, Any]:
    """Validate the external envelope and dynamically reopen its journal proof."""

    if (
        not isinstance(claim, dict)
        or set(claim) != _CLAIM_FIELDS
        or claim.get("schema_version") != CLAIM_SCHEMA_VERSION
        or claim.get("kind") != CLAIM_KIND
        or claim.get("evaluation_epoch") != EVALUATION_EPOCH
        or claim.get("claim_digest") != expected_digest
        or canonical_digest({
            key: value for key, value in claim.items()
            if key != "claim_digest"
        }) != expected_digest
    ):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_claim_invalid"
        ])
    success = _bcr.validate_first_strict_execution_success(
        claim.get("first_strict_execution_success")
    )
    scope = success["scope"]
    old = claim.get("old_checkpoint")
    candidate = claim.get("candidate")
    migration = claim.get("git_contract_migration")
    terminal_job = claim.get("terminal_job")
    if not isinstance(terminal_job, dict):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_claim_terminal_job_invalid"
        ])
    diagnosis = terminal_job.get("contract_failure_diagnosis")
    recovery_profile = terminal_job.get("recovery_profile")
    diagnosis_kind = (
        diagnosis.get("kind") if isinstance(diagnosis, dict) else None
    )
    rounds_completed = terminal_job.get("rounds_completed")
    rounds_run = terminal_job.get("rounds_run")
    if (
        terminal_job.get("rounds_requested") != 8
        or (rounds_completed, rounds_run) not in {(0, 0), (8, 8)}
        or (rounds_completed == 0 and diagnosis is not None)
        or (rounds_completed == 8 and not isinstance(diagnosis, dict))
        or recovery_profile
        not in {None, _CALLED_ALLIN_PROFILE_ID, _V65_PROFILE_ID}
        or (
            recovery_profile == _CALLED_ALLIN_PROFILE_ID
            and diagnosis_kind != _CALLED_ALLIN_DIAGNOSIS_KIND
        )
        or (
            diagnosis_kind == _CALLED_ALLIN_DIAGNOSIS_KIND
            and recovery_profile != _CALLED_ALLIN_PROFILE_ID
        )
        or (
            recovery_profile == _V65_PROFILE_ID
            and diagnosis_kind != _V65_DIAGNOSIS_KIND
        )
        or (
            diagnosis_kind == _V65_DIAGNOSIS_KIND
            and recovery_profile != _V65_PROFILE_ID
        )
    ):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_claim_recovery_profile_invalid"
        ])
    if diagnosis is not None:
        _bcr._validate_contract_failure_diagnosis_envelope(diagnosis)
    if (
        not isinstance(old, dict)
        or set(old) != {
            "digest",
            "workflow_run_id",
            "next_v",
            "source_v",
            "stage",
            "checkpoint_revision",
        }
        or not isinstance(candidate, dict)
        or set(candidate) != {"path", "artifact_hash", "files"}
        or not isinstance(migration, dict)
        or set(migration) != {
            "baseline_head",
            "baseline_contract_hash",
            "current_head",
            "current_contract_hash",
            "changed_paths",
            "contract_paths",
        }
        or old.get("next_v") != FIRST_STRICT_POLICY_VERSION
        or old.get("source_v") != ARCHIVED_VERSION_HIGH_WATER
        or old.get("stage") != "official_bootstrap_required"
        or not _HEX64.fullmatch(str(old.get("digest") or ""))
        or not isinstance(old.get("workflow_run_id"), str)
        or not old.get("workflow_run_id")
        or type(old.get("checkpoint_revision")) is not int
        or int(old["checkpoint_revision"]) < 1
        or scope.get("workflow_run_id") != old.get("workflow_run_id")
        or scope.get("candidate_version") != old.get("next_v")
        or scope.get("candidate_label") != bot_name(old.get("next_v"))
        or type(scope.get("checkpoint_revision")) is not int
        or not 1 <= int(scope["checkpoint_revision"]) <= int(
            old["checkpoint_revision"]
        )
        or candidate.get("path") != f"bots/{bot_name(old.get('next_v'))}"
        or candidate.get("artifact_hash")
        != scope.get("candidate_artifact_hash")
        or candidate.get("files") != sorted(_STRICT_FILES)
        or not _HEX40.fullmatch(str(migration.get("current_head") or ""))
        or claim.get("disposition")
        != "canonical_abandon_and_quarantine_without_evidence_migration"
    ):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_claim_crossbinding_invalid"
        ])
    if diagnosis is not None and diagnosis.get("kind") == (
        _CALLED_ALLIN_DIAGNOSIS_KIND
    ):
        incident = diagnosis.get("incident_identity") or {}
        consumption = terminal_job.get("control_consumption") or {}
        if (
            terminal_job.get("recovery_profile")
            != _CALLED_ALLIN_PROFILE_ID
            or terminal_job.get("job_id") != incident.get("job_id")
            or terminal_job.get("result_digest")
            != incident.get("job_result_digest")
            or terminal_job.get("rounds_requested")
            != incident.get("rounds_requested")
            or terminal_job.get("rounds_completed")
            != incident.get("rounds_completed")
            or terminal_job.get("rounds_run")
            != incident.get("rounds_run")
            or old.get("workflow_run_id")
            != incident.get("workflow_run_id")
            or old.get("checkpoint_revision")
            != incident.get("checkpoint_revision")
            or candidate.get("artifact_hash")
            != incident.get("candidate_artifact_hash")
            or migration.get("baseline_head")
            != incident.get("baseline_head")
            or migration.get("baseline_contract_hash")
            != incident.get("baseline_contract_hash")
            or consumption.get("valid") is not True
            or consumption.get("successful_count") != 0
            or consumption.get("max_successful_consumptions") != 1
        ):
            raise BootstrapContractRecoveryError([
                "bootstrap_contract_called_allin_claim_crossbinding_invalid"
            ])
    if diagnosis is not None and diagnosis.get("kind") == (
        _V65_DIAGNOSIS_KIND
    ):
        incident = diagnosis.get("incident_identity") or {}
        consumption = terminal_job.get("control_consumption") or {}
        if (
            terminal_job.get("recovery_profile") != _V65_PROFILE_ID
            or terminal_job.get("job_id") != incident.get("job_id")
            or terminal_job.get("result_digest")
            != incident.get("job_result_digest")
            or terminal_job.get("rounds_requested")
            != incident.get("rounds_requested")
            or terminal_job.get("rounds_completed")
            != incident.get("rounds_completed")
            or terminal_job.get("rounds_run")
            != incident.get("rounds_run")
            or old.get("workflow_run_id") != incident.get("workflow_run_id")
            or old.get("checkpoint_revision")
            != incident.get("checkpoint_revision")
            or candidate.get("artifact_hash")
            != incident.get("candidate_artifact_hash")
            or migration.get("baseline_head") != incident.get("baseline_head")
            or migration.get("baseline_contract_hash")
            != incident.get("baseline_contract_hash")
            or incident.get("baseline_contract_version")
            != _V65_BASELINE_CONTRACT_VERSION
            or incident.get("repair_contract_version")
            != _V65_REPAIR_CONTRACT_VERSION
            or consumption.get("valid") is not True
            or consumption.get("successful_count") != 0
            or consumption.get("max_successful_consumptions") != 1
        ):
            raise BootstrapContractRecoveryError([
                "bootstrap_contract_v65_claim_crossbinding_invalid"
            ])
    return claim


def _historical_terminal_job_matches(
    claim: dict[str, Any],
    directory: Path,
    *,
    root: Path | None = None,
) -> bool:
    """Reopen immutable job/result/verdict bytes without a live old candidate.

    Certification system removed: the official bootstrap / certification job
    modules no longer exist, so no historical terminal job can be matched.
    Returns False unconditionally.
    """

    return False

    # Legacy cert-bound body retained for reference; unreachable.  The
    # former official_bootstrap / official_certification_job imports have been
    # removed; the names below are intentionally left undefined.

    expected = claim.get("terminal_job") or {}
    if (
        not _HEX64.fullmatch(directory.name)
        or directory.name != expected.get("job_id")
    ):
        return False
    try:
        _bcr._require_regular_directory(directory)
        with _job_lock(directory):
            request = _read_json(directory / "request.json") or {}
            state = _read_json(directory / "state.json") or {}
            if _validate_request(request):
                return False
            public = _public_state(directory, state)
            result = _result_payload(directory, state) or {}
        status = result.get("status") if isinstance(result.get("status"), dict) else {}
        progress = public.get("progress") if isinstance(public.get("progress"), dict) else {}
        diagnosis = expected.get("contract_failure_diagnosis")
        diagnosis_kind = (
            diagnosis.get("kind") if isinstance(diagnosis, dict) else None
        )
        legacy_causal_profile = diagnosis_kind == (
            _CAUSAL_FAILURE_DIAGNOSIS_KIND
        )
        called_allin_profile = diagnosis_kind == (
            _CALLED_ALLIN_DIAGNOSIS_KIND
        )
        v65_profile = diagnosis_kind == _V65_DIAGNOSIS_KIND
        if diagnosis is not None:
            _bcr._validate_contract_failure_diagnosis_envelope(diagnosis)
        expected_rounds = (
            8
            if legacy_causal_profile or called_allin_profile or v65_profile
            else 0
        )
        if (
            public.get("state") != "completed"
            or public.get("pending") is not False
            or request.get("request_digest") != expected.get("request_digest")
            or state.get("revision") != expected.get("state_revision")
            or result.get("result_digest") != expected.get("result_digest")
            or canonical_digest(status) != expected.get("status_digest")
            or progress.get("rounds_requested") != 8
            or progress.get("rounds_completed") != expected_rounds
            or (status.get("summary") or {}).get("rounds_run")
            != expected_rounds
            or status.get("status") != (
                "official-failed"
                if legacy_causal_profile or v65_profile
                else "official-inconclusive"
            )
            or (
                called_allin_profile
                and expected.get("recovery_profile")
                != _CALLED_ALLIN_PROFILE_ID
            )
            or (
                v65_profile
                and expected.get("recovery_profile") != _V65_PROFILE_ID
            )
        ):
            return False
        if legacy_causal_profile:
            project_root = Path(root).resolve() if root is not None else directory.parents[5]
            rebuilt_diagnosis = _bcr._legacy_causal_failure_diagnosis(
                project_root,
                directory,
                request=request,
                state=state,
                status=status,
                candidate_hash=str((claim.get("candidate") or {}).get("artifact_hash") or ""),
                expected_baseline_head=str(
                    (claim.get("git_contract_migration") or {}).get("baseline_head") or ""
                ),
                expected_repair_head=str(
                    (claim.get("git_contract_migration") or {}).get("current_head") or ""
                ),
                # A later reviewed wire implementation must not invalidate the
                # immutable old-job exclusion.  Historical reopen still binds
                # both Git blobs and recomputes the raw proof; only live claim
                # construction requires checkout bytes to equal repair_head.
                require_live_repair_source=False,
            )
            if rebuilt_diagnosis != diagnosis:
                return False
        elif called_allin_profile:
            project_root = (
                Path(root).resolve()
                if root is not None
                else directory.parents[5]
            )
            incident = diagnosis.get("incident_identity") or {}
            rebuilt_diagnosis = _bcr._called_allin_runout_failure_diagnosis(
                project_root,
                directory,
                request=request,
                state=state,
                status=status,
                candidate_hash=str(
                    (claim.get("candidate") or {}).get("artifact_hash") or ""
                ),
                workflow_run_id=str(
                    (claim.get("old_checkpoint") or {}).get(
                        "workflow_run_id"
                    ) or ""
                ),
                checkpoint_revision=int(
                    (claim.get("old_checkpoint") or {}).get(
                        "checkpoint_revision", 0
                    ) or 0
                ),
                job_result_digest=str(result.get("result_digest") or ""),
                expected_evaluation_contract_version=int(
                    incident.get("baseline_contract_version", 0) or 0
                ),
                expected_evaluation_contract_hash=str(
                    (claim.get("git_contract_migration") or {}).get(
                        "baseline_contract_hash"
                    ) or ""
                ),
                expected_repair_contract_version=int(
                    incident.get("repair_contract_version", 0) or 0
                ),
                expected_baseline_head=str(
                    (claim.get("git_contract_migration") or {}).get(
                        "baseline_head"
                    ) or ""
                ),
                expected_repair_head=str(
                    (claim.get("git_contract_migration") or {}).get(
                        "current_head"
                    ) or ""
                ),
                control_consumption=expected.get("control_consumption") or {},
                require_live_repair_source=False,
            )
            if rebuilt_diagnosis != diagnosis:
                return False
        elif v65_profile:
            project_root = (
                Path(root).resolve()
                if root is not None
                else directory.parents[5]
            )
            incident = diagnosis.get("incident_identity") or {}
            rebuilt_diagnosis = _bcr._v65_contract_failure_diagnosis(
                project_root,
                directory,
                request=request,
                state=state,
                status=status,
                candidate_hash=str(
                    (claim.get("candidate") or {}).get("artifact_hash") or ""
                ),
                workflow_run_id=str(
                    (claim.get("old_checkpoint") or {}).get(
                        "workflow_run_id"
                    ) or ""
                ),
                checkpoint_revision=int(
                    (claim.get("old_checkpoint") or {}).get(
                        "checkpoint_revision", 0
                    ) or 0
                ),
                job_result_digest=str(result.get("result_digest") or ""),
                expected_evaluation_contract_version=int(
                    incident.get("baseline_contract_version", 0) or 0
                ),
                expected_evaluation_contract_hash=str(
                    (claim.get("git_contract_migration") or {}).get(
                        "baseline_contract_hash"
                    ) or ""
                ),
                expected_repair_contract_version=int(
                    incident.get("repair_contract_version", 0) or 0
                ),
                expected_baseline_head=str(
                    (claim.get("git_contract_migration") or {}).get(
                        "baseline_head"
                    ) or ""
                ),
                expected_repair_head=str(
                    (claim.get("git_contract_migration") or {}).get(
                        "current_head"
                    ) or ""
                ),
                control_consumption=expected.get("control_consumption") or {},
                require_live_repair_source=False,
            )
            if rebuilt_diagnosis != diagnosis:
                return False
        entries, issues = _validated_ledger_entries()
        if issues:
            return False
        matches = [
            entry for entry in entries
            if entry.get("entry_digest") == expected.get("ledger_entry_digest")
        ]
        if len(matches) != 1 or status.get("official_verdict_ledger_entry") != matches[0]:
            return False
        entry = matches[0]
        return bool(
            entry.get("sequence") == expected.get("ledger_sequence")
            and entry.get("outcome") == (
                "official-failed"
                if legacy_causal_profile or v65_profile
                else "official-inconclusive"
            )
            and entry.get("classification") == (
                "protocol"
                if legacy_causal_profile or v65_profile
                else "harness"
            )
            and entry.get("authoritative")
            is (legacy_causal_profile or v65_profile)
            and entry.get("blocking")
            is (legacy_causal_profile or v65_profile)
            and entry.get("certificate_digest") in {None, ""}
            and entry.get("strength_evaluation") == "not_applicable"
        )
    except Exception:
        return False


def _finalized_canonical_abandon(
    root: Path,
    claim: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate the canonical transaction that consumed the external proof."""

    from epoch_authority import (
        validate_abandon_finalize_receipt,
        validate_abandon_ledger_history,
    )
    from evolution_infra import (
        load_abandoned_version_receipts,
        _abandoned_version_receipt_identity_digest,
    )

    results = root / "web" / "core" / "results"
    transactions = results / "policy_epoch_abandon_transactions"
    expected_reason = _bcr.abandon_reason(claim["claim_digest"])
    expected_checkpoint = claim.get("old_checkpoint") or {}
    try:
        rows = load_abandoned_version_receipts(
            path=results / "abandoned_versions.jsonl",
            project_root=root,
        )
        matches: list[tuple[dict[str, Any], dict[str, Any], Path, dict[str, Any] | None]] = []
        for directory in transactions.iterdir():
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                canonical_claim = json.loads(
                    _bcr._read_regular_exact(directory / "claim.json").decode("utf-8")
                )
                receipt = json.loads(
                    _bcr._read_regular_exact(directory / "receipt.json").decode("utf-8")
                )
            except Exception:
                continue
            checkpoint = canonical_claim.get("checkpoint") or {}
            if (
                canonical_claim.get("abandon_reason") == expected_reason
                and checkpoint.get("workflow_run_id")
                == expected_checkpoint.get("workflow_run_id")
                and checkpoint.get("digest") == expected_checkpoint.get("digest")
                and checkpoint.get("checkpoint_revision")
                == expected_checkpoint.get("checkpoint_revision")
            ):
                if directory.name != canonical_claim.get("transaction_id"):
                    continue
                if _bcr.validate_canonical_abandon_external_binding(
                    root,
                    canonical_claim,
                ) != claim:
                    continue
                validate_abandon_finalize_receipt(canonical_claim, receipt, rows)
                matched_abandon_receipt = validate_abandon_ledger_history(
                    canonical_claim, rows, require_active_head=False
                )
                matches.append((canonical_claim, receipt, directory, matched_abandon_receipt))
        if len(matches) != 1:
            return None
        canonical_claim, _receipt, directory, matched_abandon_receipt = matches[0]
        quarantine = directory / "candidate"
        candidate = canonical_claim.get("candidate") or {}
        if candidate.get("present") is not True or not quarantine.is_dir() or quarantine.is_symlink():
            return None
        if hash_path(quarantine) != (claim.get("candidate") or {}).get("artifact_hash"):
            return None
        return {
            "transaction_id": directory.name,
            "finalize_receipt_digest": _receipt.get("receipt_digest"),
            "abandon_receipt_digest": (
                _abandoned_version_receipt_identity_digest(matched_abandon_receipt)
                if matched_abandon_receipt is not None
                else None
            ),
            "candidate_state": _receipt.get("candidate_state"),
        }
    except Exception:
        return None


def _finalized_canonical_abandon_matches(
    root: Path,
    claim: dict[str, Any],
) -> bool:
    return _bcr._finalized_canonical_abandon(root, claim) is not None


def finalized_claim_result(
    root: str | Path,
    claim_digest: str,
) -> dict[str, Any] | None:
    """Return the exact completed terminal result after checkpoint clearance."""

    root = Path(root).resolve()
    try:
        claim = _bcr.load_claim(root, claim_digest)
        # official_certification_job module removed: no historical terminal job
        # can match, so this recovery route yields no terminal.
        if True or not _bcr._historical_terminal_job_matches(  # noqa: SIM222
            claim,
            Path(str((claim.get("terminal_job") or {}).get("job_id") or "")),
            root=root,
        ):
            return None
        terminal = _bcr._finalized_canonical_abandon(root, claim)
        if terminal is None:
            return None
        return {
            "status": "already_abandoned",
            "claim_digest": claim_digest,
            "old_workflow_run_id": (claim.get("old_checkpoint") or {}).get(
                "workflow_run_id"
            ),
            **terminal,
        }
    except Exception:
        return None


def incomplete_claim_resume_identity(
    root: str | Path,
    claim_digest: str,
) -> dict[str, Any] | None:
    """Reopen the checkpoint-cleared, finalize-receipt-missing crash prefix."""

    root = Path(root).resolve()
    try:
        claim = _bcr.load_claim(root, claim_digest)
        from tool_bot_management import _load_live_abandon_claim

        job_id = str((claim.get("terminal_job") or {}).get("job_id") or "")
        # official_certification_job module removed: no historical terminal job
        # can match, so this recovery route yields no canonical abandon claim.
        if True or not _bcr._historical_terminal_job_matches(  # noqa: SIM222
            claim,
            Path(job_id),
            root=root,
        ):
            return None
        canonical = _load_live_abandon_claim()
        if not isinstance(canonical, dict):
            return None
        expected = claim.get("old_checkpoint") or {}
        observed = canonical.get("checkpoint") or {}
        if (
            canonical.get("abandon_reason") != _bcr.abandon_reason(claim_digest)
            or observed.get("workflow_run_id") != expected.get("workflow_run_id")
            or observed.get("next_v") != expected.get("next_v")
            or observed.get("source_v") != expected.get("source_v")
            or observed.get("stage") != expected.get("stage")
            or observed.get("checkpoint_revision")
            != expected.get("checkpoint_revision")
            or observed.get("digest") != expected.get("digest")
        ):
            return None
        return {
            "workflow_run_id": observed["workflow_run_id"],
            "next_v": observed["next_v"],
            "source_v": observed["source_v"],
            "stage": observed["stage"],
            "checkpoint_revision": observed["checkpoint_revision"],
        }
    except Exception:
        return None


def is_finalized_historical_bootstrap_job(
    root: str | Path,
    *,
    current_workflow_run_id: str,
    job_directory: str | Path,
) -> bool:
    """Return true only for an exact old job consumed by canonical abandon.

    This prevents a new v143 workflow from treating either supported immutable
    old job profile (which has the same candidate path) as a live ambiguous
    authorization.  A changed request/result/verdict/claim/transaction remains
    related-invalid.
    """

    root = Path(root).resolve()
    directory = Path(job_directory)
    if directory.is_symlink() or not directory.is_dir() or not _HEX64.fullmatch(directory.name):
        return False
    claims = root / "web" / "core" / "results" / CLAIM_DIRNAME
    try:
        if claims.is_symlink() or not claims.is_dir():
            return False
        candidates = sorted(claims.glob(f"*.json"))
    except OSError:
        return False
    matches = []
    for path in candidates:
        digest = path.stem
        if not _HEX64.fullmatch(digest):
            continue
        try:
            claim = _bcr.load_claim(root, digest)
        except Exception:
            continue
        old = claim.get("old_checkpoint") or {}
        if (
            old.get("workflow_run_id") == current_workflow_run_id
            or (claim.get("terminal_job") or {}).get("job_id") != directory.name
        ):
            continue
        if (
            _bcr._historical_terminal_job_matches(claim, directory, root=root)
            and _bcr._finalized_canonical_abandon_matches(root, claim)
        ):
            matches.append(digest)
    return len(matches) == 1
