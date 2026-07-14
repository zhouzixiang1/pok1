"""Strict epoch identity for durable evolution checkpoints.

The pipeline checkpoint is executable control state, not a compatibility
document.  A pre-policy checkpoint must therefore never become resumable just
because its stage name still exists in the current state machine.  This module
owns the small, deterministic identity envelope used by checkpoint creation,
routing, and restart diagnostics.

There are exactly two legal origins in ``national_tcp_policy_v1``:

* the fresh v143 bootstrap, bound to both the content-bound bootstrap receipt
  and the one-time policy-epoch reset receipt; and
* a v144+ generation whose complete parent set resolved as published strict
  policy bots when the checkpoint was created.

The envelope is digest-bound so a later stage CAS can preserve it byte-for-byte
without reopening source selection.  Recovery additionally verifies the live
one-time reset receipt for the exceptional v143 path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    ROLE_PARENT_SOURCE,
    bot_name,
    resolve_national_bot_spec,
)


CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_EPOCH_BINDING_SCHEMA_VERSION = 1
FRESH_BOOTSTRAP_MODE = "fresh_bootstrap"
PUBLISHED_STRICT_PARENT_MODE = "published_strict_parent"
PUBLISHED_PARENT_AUTHORITY = "strict_published_parent_resolution"
POLICY_EPOCH_RESET_RECEIPT_RELATIVE_PATH = Path(
    "web/core/results/policy_epoch_reset_receipt.json"
)
OPERATOR_ARCHIVE_RESET_ACTION = "operator_archive_reset"
OPERATOR_ARCHIVE_RESET_COMMAND = (
    "python scripts/reset_national_tcp_policy_epoch.py --execute "
    "--acknowledge-runtime-checkout"
)

_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BINDING_KEYS = frozenset(
    {
        "schema_version",
        "epoch",
        "mode",
        "next_v",
        "source_v",
        "parent2_v",
        "parent_versions",
        "source_artifact_inherited",
        "parent_authority",
        "published_parent_identities",
        "protocol_bootstrap_receipt_digest",
        "policy_epoch_reset_receipt_digest",
        "binding_digest",
    }
)
_PUBLISHED_PARENT_KEYS = frozenset(
    {
        "version",
        "bot",
        "role",
        "epoch",
        "runtime_manifest_digest",
        "epoch_receipt_digest",
        "publication_identity_digest",
        "certificate_digest",
    }
)
_LEGACY_MIGRATION_VALUE = "legacy_strategy_migration"


class CheckpointSchemaError(RuntimeError):
    """A checkpoint origin cannot be represented by the strict epoch schema."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(dict.fromkeys(str(item) for item in errors if str(item)))
        super().__init__("; ".join(self.errors) or "checkpoint epoch binding invalid")


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strict_int(value: Any) -> int | None:
    return value if type(value) is int else None


def _digest_ok(value: Any) -> bool:
    return isinstance(value, str) and _HEX_SHA256_RE.fullmatch(value) is not None


def _embedded_receipt_digest_valid(receipt: Any) -> bool:
    if not isinstance(receipt, dict) or not _digest_ok(receipt.get("receipt_digest")):
        return False
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    try:
        return receipt["receipt_digest"] == _canonical_digest(unsigned)
    except Exception:
        return False


def _audit_context(checkpoint_or_audit: Any) -> dict[str, Any]:
    if not isinstance(checkpoint_or_audit, dict):
        return {}
    nested = checkpoint_or_audit.get("audit_context")
    if isinstance(nested, dict):
        return nested
    return checkpoint_or_audit


def _legacy_migration_declared(checkpoint: Any) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    audit = _audit_context(checkpoint)
    bootstrap = audit.get("protocol_bootstrap")
    selection = audit.get("selection")
    values = (
        checkpoint.get("generation_mode"),
        (checkpoint.get("epoch_binding") or {}).get("mode")
        if isinstance(checkpoint.get("epoch_binding"), dict)
        else None,
        bootstrap.get("mode") if isinstance(bootstrap, dict) else None,
        bootstrap.get("reason") if isinstance(bootstrap, dict) else None,
        selection.get("strategy") if isinstance(selection, dict) else None,
    )
    return any(value == _LEGACY_MIGRATION_VALUE for value in values)


def _published_parent_identity(spec: Any, version: int) -> dict[str, Any]:
    if not getattr(spec, "eligible", False):
        issues = list(getattr(spec, "issues", ()) or ())
        raise CheckpointSchemaError(
            [
                "checkpoint_parent_not_strict_published",
                *(f"checkpoint_parent_issue:{item}" for item in issues[:8]),
            ]
        )
    if _strict_int(getattr(spec, "version", None)) != version:
        raise CheckpointSchemaError(["checkpoint_parent_version_mismatch"])
    runtime_manifest = getattr(spec, "runtime_manifest", None)
    epoch_receipt = getattr(spec, "epoch_receipt", None)
    publication_identity = getattr(spec, "publication_identity", None)
    certificate_digest = getattr(spec, "certificate_digest", None)
    if not all(
        isinstance(value, dict) and value
        for value in (runtime_manifest, epoch_receipt, publication_identity)
    ) or not _digest_ok(certificate_digest):
        raise CheckpointSchemaError(
            ["checkpoint_parent_publication_identity_incomplete"]
        )
    return {
        "version": version,
        "bot": bot_name(version),
        "role": ROLE_PARENT_SOURCE,
        "epoch": EVALUATION_EPOCH,
        "runtime_manifest_digest": _canonical_digest(runtime_manifest),
        "epoch_receipt_digest": _canonical_digest(epoch_receipt),
        "publication_identity_digest": _canonical_digest(publication_identity),
        "certificate_digest": certificate_digest,
    }


def _binding(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "binding_digest": _canonical_digest(payload)}


def build_checkpoint_epoch_binding(
    *,
    next_v: int,
    source_v: int,
    parent2_v: int | None = None,
    audit_context: dict[str, Any] | None = None,
    repo_root: str | Path | None = None,
    parent_resolver: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Build the immutable origin envelope for a newly selected checkpoint.

    ``parent_resolver`` is injectable only to keep schema tests hermetic.  The
    production writer uses the strict published-role resolver directly.
    """

    target = _strict_int(next_v)
    source = _strict_int(source_v)
    parent2 = _strict_int(parent2_v) if parent2_v is not None else None
    audit = audit_context if isinstance(audit_context, dict) else {}
    parent_resolver = parent_resolver or resolve_national_bot_spec
    synthetic_checkpoint = {
        "audit_context": audit,
        "generation_mode": (audit.get("selection") or {}).get("strategy")
        if isinstance(audit.get("selection"), dict)
        else None,
    }
    if _legacy_migration_declared(synthetic_checkpoint):
        raise CheckpointSchemaError(["checkpoint_legacy_strategy_migration_forbidden"])
    if target is None or source is None:
        raise CheckpointSchemaError(["checkpoint_epoch_version_type_invalid"])
    if target < FIRST_STRICT_POLICY_VERSION:
        raise CheckpointSchemaError(["checkpoint_target_before_strict_epoch"])

    bootstrap = audit.get("protocol_bootstrap")
    if target == FIRST_STRICT_POLICY_VERSION:
        if source != ARCHIVED_VERSION_HIGH_WATER or parent2 is not None:
            raise CheckpointSchemaError(["checkpoint_fresh_bootstrap_identity_mismatch"])
        try:
            from system_strict_bootstrap import validate_fresh_bootstrap_receipt

            receipt_errors = validate_fresh_bootstrap_receipt(
                bootstrap,
                active_bots=(),
            )
        except Exception as exc:
            receipt_errors = [
                "checkpoint_fresh_bootstrap_receipt_validation_error:"
                f"{type(exc).__name__}"
            ]
        if receipt_errors:
            raise CheckpointSchemaError(
                [
                    "checkpoint_fresh_bootstrap_receipt_invalid",
                    *(f"checkpoint_fresh_bootstrap:{item}" for item in receipt_errors),
                ]
            )
        bootstrap_digest = (
            bootstrap.get("receipt_digest") if isinstance(bootstrap, dict) else None
        )
        reset_digest = (
            bootstrap.get("epoch_reset_receipt_digest")
            if isinstance(bootstrap, dict)
            else None
        )
        errors = []
        if not _digest_ok(bootstrap_digest):
            errors.append("checkpoint_fresh_bootstrap_receipt_digest_invalid")
        if not _digest_ok(reset_digest):
            errors.append("checkpoint_policy_epoch_reset_receipt_digest_missing")
        if errors:
            raise CheckpointSchemaError(errors)
        return _binding(
            {
                "schema_version": CHECKPOINT_EPOCH_BINDING_SCHEMA_VERSION,
                "epoch": EVALUATION_EPOCH,
                "mode": FRESH_BOOTSTRAP_MODE,
                "next_v": target,
                "source_v": source,
                "parent2_v": None,
                "parent_versions": [],
                "source_artifact_inherited": False,
                "parent_authority": None,
                "published_parent_identities": [],
                "protocol_bootstrap_receipt_digest": bootstrap_digest,
                "policy_epoch_reset_receipt_digest": reset_digest,
            }
        )

    if source < FIRST_STRICT_POLICY_VERSION or source >= target:
        raise CheckpointSchemaError(["checkpoint_strict_parent_version_invalid"])
    parents = [source]
    if parent2 is not None:
        if parent2 < FIRST_STRICT_POLICY_VERSION or parent2 >= target:
            raise CheckpointSchemaError(["checkpoint_strict_parent2_version_invalid"])
        if parent2 in parents:
            raise CheckpointSchemaError(["checkpoint_strict_parent_versions_duplicate"])
        parents.append(parent2)

    identities: list[dict[str, Any]] = []
    for version in parents:
        try:
            spec = parent_resolver(
                bot_name(version),
                role=ROLE_PARENT_SOURCE,
                repo_root=repo_root,
            )
        except Exception as exc:
            raise CheckpointSchemaError(
                [
                    "checkpoint_parent_resolution_error:"
                    f"v{version}:{type(exc).__name__}"
                ]
            ) from exc
        identities.append(_published_parent_identity(spec, version))

    protocol_digest = None
    if bootstrap is not None:
        if not isinstance(bootstrap, dict):
            raise CheckpointSchemaError(["checkpoint_protocol_bootstrap_not_object"])
        if bootstrap.get("mode") != "singleton_strict_bootstrap":
            raise CheckpointSchemaError(["checkpoint_protocol_bootstrap_mode_invalid"])
        protocol_digest = bootstrap.get("receipt_digest")
        if not _embedded_receipt_digest_valid(bootstrap):
            raise CheckpointSchemaError(
                ["checkpoint_protocol_bootstrap_receipt_digest_invalid"]
            )

    return _binding(
        {
            "schema_version": CHECKPOINT_EPOCH_BINDING_SCHEMA_VERSION,
            "epoch": EVALUATION_EPOCH,
            "mode": PUBLISHED_STRICT_PARENT_MODE,
            "next_v": target,
            "source_v": source,
            "parent2_v": parent2,
            "parent_versions": parents,
            "source_artifact_inherited": True,
            "parent_authority": PUBLISHED_PARENT_AUTHORITY,
            "published_parent_identities": identities,
            "protocol_bootstrap_receipt_digest": protocol_digest,
            "policy_epoch_reset_receipt_digest": None,
        }
    )


def _published_parent_identity_errors(
    identity: Any,
    *,
    expected_version: int,
) -> list[str]:
    if not isinstance(identity, dict):
        return ["checkpoint_published_parent_identity_not_object"]
    errors: list[str] = []
    if set(identity) != _PUBLISHED_PARENT_KEYS:
        errors.append("checkpoint_published_parent_identity_fields_mismatch")
    if identity.get("version") != expected_version:
        errors.append("checkpoint_published_parent_identity_version_mismatch")
    if identity.get("bot") != bot_name(expected_version):
        errors.append("checkpoint_published_parent_identity_bot_mismatch")
    if identity.get("role") != ROLE_PARENT_SOURCE:
        errors.append("checkpoint_published_parent_identity_role_mismatch")
    if identity.get("epoch") != EVALUATION_EPOCH:
        errors.append("checkpoint_published_parent_identity_epoch_mismatch")
    for field in (
        "runtime_manifest_digest",
        "epoch_receipt_digest",
        "publication_identity_digest",
        "certificate_digest",
    ):
        if not _digest_ok(identity.get(field)):
            errors.append(f"checkpoint_published_parent_identity_{field}_invalid")
    return errors


def checkpoint_epoch_errors(checkpoint: Any) -> list[str]:
    """Validate the persisted strict-epoch envelope without filesystem reads."""

    if not isinstance(checkpoint, dict):
        return ["checkpoint_missing_or_not_object"]
    errors: list[str] = []
    if checkpoint.get("checkpoint_schema_version") != CHECKPOINT_SCHEMA_VERSION:
        errors.append("checkpoint_schema_version_missing_or_mismatch")
    if checkpoint.get("evaluation_epoch") != EVALUATION_EPOCH:
        errors.append("checkpoint_evaluation_epoch_missing_or_mismatch")
    if _legacy_migration_declared(checkpoint):
        errors.append("checkpoint_legacy_strategy_migration_forbidden")

    target = _strict_int(checkpoint.get("next_v"))
    source = _strict_int(checkpoint.get("source_v"))
    raw_parent2 = checkpoint.get("parent2_v")
    parent2 = _strict_int(raw_parent2) if raw_parent2 is not None else None
    if target is None or source is None or (
        raw_parent2 is not None and parent2 is None
    ):
        errors.append("checkpoint_epoch_version_type_invalid")
    elif target < FIRST_STRICT_POLICY_VERSION:
        errors.append("checkpoint_target_before_strict_epoch")

    binding = checkpoint.get("epoch_binding")
    if not isinstance(binding, dict):
        errors.append("checkpoint_epoch_binding_missing_or_not_object")
        return list(dict.fromkeys(errors))
    if set(binding) != _BINDING_KEYS:
        errors.append("checkpoint_epoch_binding_fields_mismatch")
    if binding.get("schema_version") != CHECKPOINT_EPOCH_BINDING_SCHEMA_VERSION:
        errors.append("checkpoint_epoch_binding_schema_mismatch")
    if binding.get("epoch") != EVALUATION_EPOCH:
        errors.append("checkpoint_epoch_binding_epoch_mismatch")
    if binding.get("next_v") != target:
        errors.append("checkpoint_epoch_binding_target_mismatch")
    if binding.get("source_v") != source:
        errors.append("checkpoint_epoch_binding_source_mismatch")
    if binding.get("parent2_v") != parent2:
        errors.append("checkpoint_epoch_binding_parent2_mismatch")
    unsigned = {key: value for key, value in binding.items() if key != "binding_digest"}
    try:
        expected_digest = _canonical_digest(unsigned)
    except Exception:
        expected_digest = ""
    if binding.get("binding_digest") != expected_digest:
        errors.append("checkpoint_epoch_binding_digest_mismatch")

    audit = _audit_context(checkpoint)
    bootstrap = audit.get("protocol_bootstrap")
    mode = binding.get("mode")
    if mode == FRESH_BOOTSTRAP_MODE:
        if target != FIRST_STRICT_POLICY_VERSION or source != ARCHIVED_VERSION_HIGH_WATER:
            errors.append("checkpoint_fresh_bootstrap_identity_mismatch")
        if parent2 is not None or binding.get("parent_versions") != []:
            errors.append("checkpoint_fresh_bootstrap_parent_forbidden")
        if binding.get("source_artifact_inherited") is not False:
            errors.append("checkpoint_fresh_bootstrap_inheritance_mismatch")
        if binding.get("parent_authority") is not None:
            errors.append("checkpoint_fresh_bootstrap_parent_authority_forbidden")
        if binding.get("published_parent_identities") != []:
            errors.append("checkpoint_fresh_bootstrap_parent_identity_forbidden")
        try:
            from system_strict_bootstrap import validate_fresh_bootstrap_receipt

            receipt_errors = validate_fresh_bootstrap_receipt(
                bootstrap,
                active_bots=(),
            )
        except Exception as exc:
            receipt_errors = [f"receipt_validation_error:{type(exc).__name__}"]
        if receipt_errors:
            errors.extend(
                f"checkpoint_fresh_bootstrap:{item}" for item in receipt_errors
            )
        protocol_digest = (
            bootstrap.get("receipt_digest") if isinstance(bootstrap, dict) else None
        )
        reset_digest = (
            bootstrap.get("epoch_reset_receipt_digest")
            if isinstance(bootstrap, dict)
            else None
        )
        if (
            not _digest_ok(protocol_digest)
            or binding.get("protocol_bootstrap_receipt_digest") != protocol_digest
        ):
            errors.append("checkpoint_fresh_bootstrap_receipt_binding_mismatch")
        if (
            not _digest_ok(reset_digest)
            or binding.get("policy_epoch_reset_receipt_digest") != reset_digest
        ):
            errors.append("checkpoint_policy_epoch_reset_receipt_binding_mismatch")
    elif mode == PUBLISHED_STRICT_PARENT_MODE:
        if target is not None and target <= FIRST_STRICT_POLICY_VERSION:
            errors.append("checkpoint_strict_parent_target_invalid")
        if source is not None and (
            source < FIRST_STRICT_POLICY_VERSION
            or (target is not None and source >= target)
        ):
            errors.append("checkpoint_strict_parent_version_invalid")
        expected_parents = [source] if source is not None else []
        if parent2 is not None:
            if (
                parent2 < FIRST_STRICT_POLICY_VERSION
                or (target is not None and parent2 >= target)
                or parent2 in expected_parents
            ):
                errors.append("checkpoint_strict_parent2_version_invalid")
            else:
                expected_parents.append(parent2)
        if binding.get("parent_versions") != expected_parents:
            errors.append("checkpoint_strict_parent_versions_mismatch")
        if binding.get("source_artifact_inherited") is not True:
            errors.append("checkpoint_strict_parent_inheritance_mismatch")
        if binding.get("parent_authority") != PUBLISHED_PARENT_AUTHORITY:
            errors.append("checkpoint_strict_parent_authority_mismatch")
        identities = binding.get("published_parent_identities")
        if not isinstance(identities, list) or len(identities) != len(expected_parents):
            errors.append("checkpoint_published_parent_identities_mismatch")
        else:
            for identity, version in zip(identities, expected_parents):
                errors.extend(
                    _published_parent_identity_errors(
                        identity,
                        expected_version=version,
                    )
                )
        if binding.get("policy_epoch_reset_receipt_digest") is not None:
            errors.append("checkpoint_strict_parent_reset_receipt_forbidden")
        if bootstrap is None:
            if binding.get("protocol_bootstrap_receipt_digest") is not None:
                errors.append("checkpoint_strict_parent_bootstrap_binding_unexpected")
        elif not isinstance(bootstrap, dict):
            errors.append("checkpoint_protocol_bootstrap_not_object")
        else:
            if bootstrap.get("mode") != "singleton_strict_bootstrap":
                errors.append("checkpoint_protocol_bootstrap_mode_invalid")
            receipt_digest = bootstrap.get("receipt_digest")
            if (
                not _embedded_receipt_digest_valid(bootstrap)
                or binding.get("protocol_bootstrap_receipt_digest")
                != receipt_digest
            ):
                errors.append("checkpoint_protocol_bootstrap_receipt_binding_mismatch")
    else:
        errors.append("checkpoint_epoch_binding_mode_invalid")
    return list(dict.fromkeys(errors))


def _reset_receipt_digest(receipt: dict[str, Any]) -> tuple[str, list[str]]:
    errors: list[str] = []
    recorded = receipt.get("receipt_digest")
    if recorded is not None:
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
        try:
            expected = _canonical_digest(unsigned)
        except Exception:
            expected = ""
        if not _digest_ok(recorded) or recorded != expected:
            errors.append("policy_epoch_reset_receipt_digest_invalid")
            return "", errors
        return recorded, errors
    # The reset script originally emitted a canonical receipt without a digest.
    # It may remain on disk during the code rollout, but a checkpoint must still
    # bind its exact bytes.  New reset receipts are expected to carry the
    # self-digest; this fallback does not make a missing checkpoint binding valid.
    try:
        return _canonical_digest(receipt), errors
    except Exception:
        return "", ["policy_epoch_reset_receipt_digest_unavailable"]


def live_policy_epoch_reset_receipt_errors(
    checkpoint: Any,
    *,
    project_root: str | Path,
) -> list[str]:
    """Verify the exceptional v143 checkpoint against the live reset receipt."""

    if not isinstance(checkpoint, dict):
        return ["policy_epoch_reset_checkpoint_missing"]
    binding = checkpoint.get("epoch_binding")
    if not isinstance(binding, dict) or binding.get("mode") != FRESH_BOOTSTRAP_MODE:
        return []
    expected_digest = binding.get("policy_epoch_reset_receipt_digest")
    if not _digest_ok(expected_digest):
        return ["policy_epoch_reset_receipt_binding_missing"]
    path = Path(project_root) / POLICY_EPOCH_RESET_RECEIPT_RELATIVE_PATH
    try:
        if path.is_symlink() or not path.is_file():
            return ["policy_epoch_reset_receipt_missing_or_unsafe"]
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"policy_epoch_reset_receipt_unreadable:{type(exc).__name__}"]
    if not isinstance(receipt, dict):
        return ["policy_epoch_reset_receipt_not_object"]
    try:
        from system_strict_bootstrap import validate_policy_epoch_reset_archive

        errors = list(validate_policy_epoch_reset_archive(
            receipt,
            project_root=project_root,
        ))
    except Exception as exc:
        errors = [
            "policy_epoch_reset_receipt_validation_error:"
            f"{type(exc).__name__}"
        ]
    observed_digest, digest_errors = _reset_receipt_digest(receipt)
    errors.extend(digest_errors)
    if observed_digest != expected_digest:
        errors.append("policy_epoch_reset_receipt_live_digest_mismatch")
    return list(dict.fromkeys(errors))


def strict_checkpoint_event_identity(
    checkpoint: Any,
    *,
    expected_gen: int,
    project_root: str | Path,
) -> dict[str, Any]:
    """Resolve durable event identity from one current strict checkpoint.

    Failure/event writers must bind rows while they still have the checkpoint
    that authorizes the work.  They may not infer an epoch or workflow from a
    bot number, and readers may not repair an unbound historical row later.
    This helper deliberately composes the canonical checkpoint-envelope and
    live reset-receipt validators instead of introducing a parallel identity
    schema in each writer.
    """

    errors = checkpoint_epoch_errors(checkpoint)
    if not errors:
        errors.extend(
            live_policy_epoch_reset_receipt_errors(
                checkpoint,
                project_root=project_root,
            )
        )
    if type(expected_gen) is not int:
        errors.append("checkpoint_event_expected_generation_type_invalid")

    checkpoint_obj = checkpoint if isinstance(checkpoint, dict) else {}
    checkpoint_gen = checkpoint_obj.get("next_v")
    if type(checkpoint_gen) is not int or checkpoint_gen != expected_gen:
        errors.append("checkpoint_event_generation_mismatch")

    stage = checkpoint_obj.get("stage")
    if not isinstance(stage, str) or not stage.strip():
        errors.append("checkpoint_event_stage_missing")
    elif stage in {"timed_out", "archived", "abandoned"}:
        errors.append("checkpoint_event_stage_not_active")

    workflow_run_id = checkpoint_obj.get("workflow_run_id")
    if not isinstance(workflow_run_id, str) or not workflow_run_id.strip():
        errors.append("checkpoint_event_workflow_run_id_missing")

    if errors:
        raise CheckpointSchemaError(errors)
    return {
        "gen": checkpoint_gen,
        "evaluation_epoch": EVALUATION_EPOCH,
        "workflow_run_id": workflow_run_id,
    }


def checkpoint_epoch_reset_route(
    checkpoint: Any,
    errors: Iterable[str],
) -> dict[str, Any]:
    """Return the sole safe route for an active incompatible checkpoint."""

    issues = list(dict.fromkeys(str(item) for item in errors if str(item)))
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    return {
        "stage": checkpoint.get("stage"),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "parent2_v": checkpoint.get("parent2_v"),
        "next_tool": None,
        "allowed_tools": [],
        "intent": OPERATOR_ARCHIVE_RESET_ACTION,
        "failure_class": "checkpoint_epoch_incompatible",
        "epoch_issues": issues,
        "operator_action": OPERATOR_ARCHIVE_RESET_ACTION,
        "operator_command": OPERATOR_ARCHIVE_RESET_COMMAND,
        "directive": (
            "This active checkpoint is not bound to national_tcp_policy_v1. "
            "Do not call run_master, prepare, Worker, gate, or commit tools and "
            "do not add missing fields in place. Preserve/archive the checkpoint "
            "and candidate, then perform the central policy-epoch reset through "
            f"`{OPERATOR_ARCHIVE_RESET_COMMAND}` at an operator-controlled safe "
            "point."
        ),
    }


__all__ = [
    "CHECKPOINT_EPOCH_BINDING_SCHEMA_VERSION",
    "CHECKPOINT_SCHEMA_VERSION",
    "CheckpointSchemaError",
    "FRESH_BOOTSTRAP_MODE",
    "OPERATOR_ARCHIVE_RESET_ACTION",
    "OPERATOR_ARCHIVE_RESET_COMMAND",
    "POLICY_EPOCH_RESET_RECEIPT_RELATIVE_PATH",
    "PUBLISHED_STRICT_PARENT_MODE",
    "build_checkpoint_epoch_binding",
    "checkpoint_epoch_errors",
    "checkpoint_epoch_reset_route",
    "live_policy_epoch_reset_receipt_errors",
    "strict_checkpoint_event_identity",
]
