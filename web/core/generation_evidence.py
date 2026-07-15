"""Checkpoint-bound strength-evidence identity for published generations.

Only the exact publishing checkpoint may explain why a generation had no
strength evidence.  In particular, a version number by itself is never a
bootstrap capability.  The same producer is consumed by the archive snapshot,
the Cycle Archivist validator, and uninterrupted-stability observation.
"""

from __future__ import annotations

from typing import Any

from bot_artifact import canonical_digest
from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
)


SCHEMA_VERSION = 1


class GenerationEvidenceError(RuntimeError):
    """The checkpoint cannot prove an admissible evidence identity."""


def _digest(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _checkpoint_identity_errors(
    checkpoint: Any,
    *,
    version: int,
    source_v: int,
) -> list[str]:
    if not isinstance(checkpoint, dict):
        return ["generation_evidence_checkpoint_missing"]
    errors: list[str] = []
    try:
        from checkpoint_schema import checkpoint_epoch_errors

        errors.extend(
            f"generation_evidence_checkpoint:{item}"
            for item in checkpoint_epoch_errors(checkpoint)
        )
    except Exception as exc:
        errors.append(
            f"generation_evidence_checkpoint_validation_error:{type(exc).__name__}"
        )
    if checkpoint.get("stage") != "publishing":
        errors.append("generation_evidence_checkpoint_not_publishing")
    if checkpoint.get("evaluation_epoch") != EVALUATION_EPOCH:
        errors.append("generation_evidence_epoch_mismatch")
    try:
        if int(checkpoint.get("next_v")) != int(version):
            errors.append("generation_evidence_target_mismatch")
        if int(checkpoint.get("source_v")) != int(source_v):
            errors.append("generation_evidence_source_mismatch")
    except (TypeError, ValueError):
        errors.append("generation_evidence_version_identity_invalid")
    if not str(checkpoint.get("workflow_run_id") or ""):
        errors.append("generation_evidence_workflow_missing")
    try:
        if int(checkpoint.get("checkpoint_revision")) <= 0:
            errors.append("generation_evidence_revision_invalid")
    except (TypeError, ValueError):
        errors.append("generation_evidence_revision_invalid")
    return errors


def _fresh_v143_identity(checkpoint: dict, version: int, source_v: int) -> dict:
    errors: list[str] = []
    if version != FIRST_STRICT_POLICY_VERSION:
        errors.append("fresh_bootstrap_target_not_v143")
    if source_v != ARCHIVED_VERSION_HIGH_WATER:
        errors.append("fresh_bootstrap_source_not_high_water")
    audit = checkpoint.get("audit_context") or {}
    receipt = audit.get("protocol_bootstrap") if isinstance(audit, dict) else None
    selection = audit.get("selection") if isinstance(audit, dict) else None
    try:
        from system_strict_bootstrap import validate_fresh_bootstrap_receipt

        errors.extend(
            f"fresh_bootstrap:{item}"
            for item in validate_fresh_bootstrap_receipt(
                receipt,
                active_bots=(),
            )
        )
    except Exception as exc:
        errors.append(
            f"fresh_bootstrap_receipt_validation_error:{type(exc).__name__}"
        )
    if not isinstance(selection, dict):
        errors.append("fresh_bootstrap_selection_missing")
        selection = {}
    if selection.get("strategy") != "fresh_policy_bootstrap":
        errors.append("fresh_bootstrap_selection_strategy_mismatch")
    if selection.get("bootstrap_without_strength_evidence") is not True:
        errors.append("fresh_bootstrap_selection_weight_mismatch")
    receipt_digest = str((receipt or {}).get("receipt_digest") or "")
    if (
        not _digest(receipt_digest)
        or selection.get("protocol_bootstrap_receipt_digest") != receipt_digest
    ):
        errors.append("fresh_bootstrap_selection_receipt_mismatch")
    evidence = selection.get("evaluation_evidence") or {}
    if (
        not isinstance(evidence, dict)
        or evidence.get("games") != 0
        or evidence.get("cutoffs") != {}
        or evidence.get("readiness_reason") != "fresh_national_policy_bootstrap"
    ):
        errors.append("fresh_bootstrap_evidence_projection_mismatch")
    epoch_binding = checkpoint.get("epoch_binding") or {}
    if (
        not isinstance(epoch_binding, dict)
        or epoch_binding.get("protocol_bootstrap_receipt_digest")
        != receipt_digest
        or epoch_binding.get("source_artifact_inherited") is not False
    ):
        errors.append("fresh_bootstrap_checkpoint_binding_mismatch")
    if errors:
        raise GenerationEvidenceError(";".join(dict.fromkeys(errors)))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "fresh_strict_v143_bootstrap",
        "reason": "empty_strict_policy_pool",
        "strength_evidence_admitted": False,
        "strength_evidence_weight": 0,
        "source_v": source_v,
        "protocol_bootstrap_receipt_digest": receipt_digest,
    }


def _singleton_v144_identity(
    checkpoint: dict,
    version: int,
    source_v: int,
) -> dict:
    errors: list[str] = []
    if version != FIRST_STRICT_POLICY_VERSION + 1:
        errors.append("singleton_bootstrap_target_not_v144")
    if source_v != FIRST_STRICT_POLICY_VERSION:
        errors.append("singleton_bootstrap_source_not_v143")
    audit = checkpoint.get("audit_context") or {}
    receipt = audit.get("protocol_bootstrap") if isinstance(audit, dict) else None
    selection = audit.get("selection") if isinstance(audit, dict) else None
    if not isinstance(receipt, dict):
        errors.append("singleton_bootstrap_receipt_missing")
        receipt = {}
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    receipt_digest = str(receipt.get("receipt_digest") or "")
    if not _digest(receipt_digest) or receipt_digest != canonical_digest(unsigned):
        errors.append("singleton_bootstrap_receipt_digest_mismatch")
    expected_receipt = {
        "schema_version": 1,
        "kind": "national-tcp-policy-singleton-bootstrap-v1",
        "mode": "singleton_strict_bootstrap",
        "epoch": EVALUATION_EPOCH,
        "source_v": source_v,
        "next_v": version,
        "source_artifact_inherited": True,
        "active_bots": [bot_name(source_v)],
    }
    for field, expected in expected_receipt.items():
        if receipt.get(field) != expected:
            errors.append(f"singleton_bootstrap_receipt_{field}_mismatch")
    for field in (
        "source_runtime_manifest_digest",
        "source_epoch_receipt_digest",
        "source_certificate_digest",
    ):
        if not _digest(receipt.get(field)):
            errors.append(f"singleton_bootstrap_receipt_{field}_invalid")
    if not isinstance(receipt.get("source_publication_identity"), dict):
        errors.append("singleton_bootstrap_receipt_publication_identity_missing")
    if not isinstance(selection, dict):
        errors.append("singleton_bootstrap_selection_missing")
        selection = {}
    expected_selection = {
        "strategy": "singleton_strict_bootstrap",
        "parent_a": source_v,
        "parent_b": None,
        "bootstrap_without_strength_evidence": True,
        "protocol_bootstrap_receipt_digest": receipt_digest,
    }
    for field, expected in expected_selection.items():
        if selection.get(field) != expected:
            errors.append(f"singleton_bootstrap_selection_{field}_mismatch")
    evidence = selection.get("evaluation_evidence") or {}
    if (
        not isinstance(evidence, dict)
        or evidence.get("games") != 0
        or evidence.get("cutoffs") != {}
        or evidence.get("readiness_reason") != "singleton_strict_bootstrap"
    ):
        errors.append("singleton_bootstrap_evidence_projection_mismatch")
    epoch_binding = checkpoint.get("epoch_binding") or {}
    if (
        not isinstance(epoch_binding, dict)
        or epoch_binding.get("protocol_bootstrap_receipt_digest")
        != receipt_digest
        or epoch_binding.get("source_artifact_inherited") is not True
        or epoch_binding.get("parent_versions") != [source_v]
    ):
        errors.append("singleton_bootstrap_checkpoint_binding_mismatch")
    parent_rows = (
        epoch_binding.get("published_parent_identities")
        if isinstance(epoch_binding, dict)
        else None
    )
    if not isinstance(parent_rows, list) or len(parent_rows) != 1:
        errors.append("singleton_bootstrap_parent_identity_missing")
    else:
        parent = parent_rows[0]
        if not isinstance(parent, dict) or parent.get("version") != source_v:
            errors.append("singleton_bootstrap_parent_identity_mismatch")
        else:
            comparisons = {
                "runtime_manifest_digest": "source_runtime_manifest_digest",
                "epoch_receipt_digest": "source_epoch_receipt_digest",
                "certificate_digest": "source_certificate_digest",
            }
            for parent_field, receipt_field in comparisons.items():
                if parent.get(parent_field) != receipt.get(receipt_field):
                    errors.append(
                        f"singleton_bootstrap_parent_{parent_field}_mismatch"
                    )
            raw_publication = receipt.get("source_publication_identity") or {}
            stable_publication = {
                "schema_version": 1,
                "published": True,
                "version": source_v,
                "tag": parent.get("completion_tag"),
                "tag_type": "tag",
                "tag_object": parent.get("completion_tag_object_oid"),
                "commit_oid": parent.get("publication_commit_oid"),
                "completion_tree_oid": parent.get("completion_tree_oid"),
                "tag_artifact_hash": parent.get("tag_artifact_hash"),
            }
            if parent.get("publication_identity_digest") != canonical_digest(
                stable_publication
            ):
                errors.append("singleton_bootstrap_parent_publication_binding_invalid")
            for field, expected in (
                ("version", source_v),
                ("tag", parent.get("completion_tag")),
                ("tag_object", parent.get("completion_tag_object_oid")),
                ("commit_oid", parent.get("publication_commit_oid")),
                ("completion_tree_oid", parent.get("completion_tree_oid")),
                ("tag_artifact_hash", parent.get("tag_artifact_hash")),
            ):
                if raw_publication.get(field) != expected:
                    errors.append(
                        f"singleton_bootstrap_receipt_publication_{field}_mismatch"
                    )
    if errors:
        raise GenerationEvidenceError(";".join(dict.fromkeys(errors)))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "singleton_strict_v144_bootstrap",
        "reason": "single_strict_parent_no_peer_pool",
        "strength_evidence_admitted": False,
        "strength_evidence_weight": 0,
        "source_v": source_v,
        "protocol_bootstrap_receipt_digest": receipt_digest,
        "source_publication_identity_digest": canonical_digest(
            receipt["source_publication_identity"]
        ),
    }


def _frozen_native_identity(checkpoint: dict, version: int, source_v: int) -> dict:
    audit = checkpoint.get("audit_context") or {}
    if not isinstance(audit, dict) or audit.get("protocol_bootstrap") is not None:
        raise GenerationEvidenceError("normal_generation_bootstrap_receipt_forbidden")
    selection = audit.get("selection") or {}
    evidence = selection.get("evaluation_evidence") or {}
    cutoffs = evidence.get("cutoffs") or {}
    required = {
        "generation_snapshot_manifest_digest": cutoffs.get(
            "generation_snapshot_manifest_digest"
        ),
        "cycle_manifest_digest": cutoffs.get("cycle_manifest_digest"),
        "h2h_snapshot_manifest_digest": selection.get(
            "h2h_snapshot_manifest_digest"
        ),
        "h2h_snapshot_sha256": selection.get("h2h_snapshot_sha256"),
        "selection_view_digest": evidence.get("selection_view_digest"),
    }
    invalid = [field for field, value in required.items() if not _digest(value)]
    try:
        games = int(evidence.get("games") or 0)
    except (TypeError, ValueError):
        games = 0
    if games <= 0:
        invalid.append("evaluation_evidence_games")
    try:
        from evidence_snapshot import (
            load_generation_evaluation_snapshot,
            load_generation_snapshot_identity,
        )

        frozen = load_generation_snapshot_identity(version)
        bundle = load_generation_evaluation_snapshot(version)
    except Exception as exc:
        raise GenerationEvidenceError(
            f"generation_snapshot_validation_error:{type(exc).__name__}"
        ) from exc
    if frozen.get("available") is not True:
        invalid.append("generation_snapshot_unavailable")
    if bundle.get("available") is not True:
        invalid.append("generation_snapshot_bundle_unavailable")
    cycle = frozen.get("cycle") or {}
    files = frozen.get("files") or {}
    if frozen.get("manifest_digest") != required[
        "generation_snapshot_manifest_digest"
    ]:
        invalid.append("generation_snapshot_manifest_digest_mismatch")
    if cycle.get("manifest_digest") != required["cycle_manifest_digest"]:
        invalid.append("cycle_manifest_digest_mismatch")
    if required["h2h_snapshot_manifest_digest"] != frozen.get("manifest_digest"):
        invalid.append("h2h_snapshot_manifest_digest_mismatch")
    if required["h2h_snapshot_sha256"] != str(
        ((files.get("h2h") or {}).get("sha256"))
    ):
        invalid.append("h2h_snapshot_sha256_mismatch")
    frozen_selection = bundle.get("selection") or {}
    rows = frozen_selection.get("rows")
    source_history = selection.get("selection_view_source_history")
    if (
        not isinstance(rows, list)
        or not rows
        or not all(isinstance(row, dict) for row in rows)
        or not isinstance(source_history, list)
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in source_history)
    ):
        invalid.append("selection_view_replay_inputs_invalid")
        recomputed_selection_view_digest = ""
    else:
        active_bots = tuple(sorted(str(item) for item in cycle.get("active_bots") or []))
        rows_by_name = {str(row.get("name")): dict(row) for row in rows}
        if set(rows_by_name) != set(active_bots):
            invalid.append("selection_view_row_pool_mismatch")
        replay_payload = {
            "active_bots": active_bots,
            "rows": tuple(rows_by_name[name] for name in active_bots if name in rows_by_name),
            "source_history": tuple(source_history),
            "evaluation_cutoffs": cutoffs,
        }
        recomputed_selection_view_digest = canonical_digest(replay_payload)
    if required["selection_view_digest"] != recomputed_selection_view_digest:
        invalid.append("selection_view_digest_mismatch")
    if str(frozen.get("evaluation_identity_digest") or "") != str(
        (bundle.get("manifest") or {}).get("evaluation_identity_digest") or ""
    ):
        invalid.append("evaluation_identity_digest_mismatch")
    if bot_name(source_v) not in set(cycle.get("active_bots") or []):
        invalid.append("source_missing_from_frozen_cycle")
    for role in ("selection", "match_history_index", "replay_spotlight"):
        row = files.get(role)
        if not isinstance(row, dict) or not _digest(row.get("sha256")):
            invalid.append(f"frozen_{role}_hash_invalid")
    if invalid:
        raise GenerationEvidenceError(";".join(dict.fromkeys(invalid)))
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "frozen_native_evaluation",
        "reason": "verified_complete_frozen_cutoff",
        "strength_evidence_admitted": True,
        "strength_evidence_weight": 1,
        "source_v": source_v,
        **{field: str(value) for field, value in required.items()},
        "evaluation_identity_digest": str(
            frozen.get("evaluation_identity_digest") or ""
        ),
        "cycle_save_num": int(cycle.get("save_num", -1)),
        "cycle_daemon_run_id": str(cycle.get("daemon_run_id") or ""),
        "cycle_active_bots": sorted(str(item) for item in cycle.get("active_bots") or []),
        "selection_sha256": str(files["selection"]["sha256"]),
        "match_history_index_sha256": str(files["match_history_index"]["sha256"]),
        "replay_spotlight_sha256": str(files["replay_spotlight"]["sha256"]),
    }


def build_generation_evidence_identity(
    checkpoint: Any,
    *,
    version: int,
    source_v: int,
) -> dict[str, Any]:
    """Build one admitted/zero-weight identity from the exact checkpoint."""

    version = int(version)
    source_v = int(source_v)
    errors = _checkpoint_identity_errors(
        checkpoint,
        version=version,
        source_v=source_v,
    )
    if errors:
        raise GenerationEvidenceError(";".join(dict.fromkeys(errors)))
    audit = checkpoint.get("audit_context") or {}
    receipt = audit.get("protocol_bootstrap") if isinstance(audit, dict) else None
    mode = receipt.get("mode") if isinstance(receipt, dict) else None
    if mode == "fresh_national_policy_bootstrap":
        return _fresh_v143_identity(checkpoint, version, source_v)
    if mode == "singleton_strict_bootstrap":
        return _singleton_v144_identity(checkpoint, version, source_v)
    if receipt is not None:
        raise GenerationEvidenceError("generation_bootstrap_mode_invalid")
    return _frozen_native_identity(checkpoint, version, source_v)


def generation_evidence_identity_errors(
    identity: Any,
    checkpoint: Any,
    *,
    version: int,
    source_v: int,
) -> list[str]:
    """Rebuild and compare an existing evidence identity fail closed."""

    try:
        expected = build_generation_evidence_identity(
            checkpoint,
            version=version,
            source_v=source_v,
        )
    except Exception as exc:
        return [f"generation_evidence_rebuild_failed:{type(exc).__name__}:{str(exc)[:240]}"]
    return [] if identity == expected else ["generation_evidence_identity_mismatch"]


__all__ = [
    "GenerationEvidenceError",
    "build_generation_evidence_identity",
    "generation_evidence_identity_errors",
]
