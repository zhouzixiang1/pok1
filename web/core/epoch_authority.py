"""Canonical runtime projection for the strict national-policy epoch.

Version tags, the one-time reset receipt, and the strict checkpoint envelope
are different kinds of evidence.  Status pages and schedulers previously
combined them ad hoc, which allowed a retired ``abandoned_versions.jsonl`` or
an unbound v155 checkpoint to make the fresh target appear to be v168.  This
module keeps those authorities separate and exposes one read-only projection.

The archived v142 tag is a numeric high-water only.  Until either the reset
receipt validates or a strict v143+ tag exists, mutable runtime files belong to
the retired epoch and cannot reserve version numbers or describe an active
generation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    parse_bot_version,
)
from first_strict_control import CONTROL_ID as FIRST_STRICT_CONTROL_ID


RESET_COMMAND = (
    "python scripts/reset_national_tcp_policy_epoch.py --execute "
    "--acknowledge-runtime-checkout"
)
FIRST_STRICT_BOOTSTRAP_COMMAND = (
    "python scripts/official_certify.py bootstrap-first-strict "
    f"bots/national_v{FIRST_STRICT_POLICY_VERSION} "
    f"--control-id {FIRST_STRICT_CONTROL_ID} "
    "--acknowledge-one-time-first-strict-control --wait-if-busy"
)


class PolicyEpochInitializationRequired(RuntimeError):
    """Raised when a mutating runtime is launched before the epoch reset.

    The attached projection is the same canonical state exposed by the status
    API.  Launchers must not manufacture a second interpretation of the reset
    receipt or version tags, and must not persist an event while the results
    directory still belongs to the retired epoch.
    """

    def __init__(self, operation: str, state: dict[str, Any]) -> None:
        self.operation = str(operation)
        self.state = dict(state)
        super().__init__(
            f"{self.operation} requires initialized {state.get('evaluation_epoch')}: "
            f"{state.get('state')}"
        )


def policy_epoch_initialization(
    *, results_dir: str | Path | None = None, current_v: int | None = None
) -> dict[str, Any]:
    """Describe whether mutable runtime data belongs to the strict epoch."""

    import evolution_infra as infra
    from system_strict_bootstrap import (
        POLICY_EPOCH_RESET_RECEIPT_FILENAME,
        load_policy_epoch_reset_receipt,
    )
    from national_runtime_authority import strict_published_bot_names

    root = Path(results_dir) if results_dir is not None else Path(infra.RESULTS_DIR)
    authority_high_water = int(
        infra.find_current_v() if current_v is None else current_v
    )
    receipt_path = root / POLICY_EPOCH_RESET_RECEIPT_FILENAME
    receipt, receipt_errors = load_policy_epoch_reset_receipt(root)
    # A tag number is immutable version authority, but it is not publication
    # authority by itself.  Initialization through publication requires the
    # complete strict artifact, tag/tree binding, and signed full-v5
    # certificate resolved by ``strict_published_bot_names``.  This prevents a
    # stray/manual national-bot-v143+ tag from bypassing the one-time reset.
    strict_bots = list(strict_published_bot_names())
    strict_published = bool(strict_bots)
    reset_valid = receipt is not None
    initialized = strict_published or reset_valid

    if strict_published:
        state = "strict_published"
        operator_action = None
        operator_command = None
    elif reset_valid:
        state = "fresh_bootstrap_ready"
        operator_action = None
        operator_command = None
    elif os.path.lexists(receipt_path):
        # A malformed/interrupted durable claim is deliberately no-clobber.
        # The reset script will refuse a second receipt, so claiming that the
        # normal command is actionable would be misleading and unsafe.
        state = "reset_evidence_requires_recovery"
        operator_action = "inspect_policy_epoch_reset_evidence"
        operator_command = None
    elif authority_high_water >= FIRST_STRICT_POLICY_VERSION:
        # The normal reset is pinned to the v142 high-water.  A later tag with
        # no eligible published artifact is therefore an inconsistent durable
        # claim, not a safe invitation to rerun the reset command.
        state = "version_authority_requires_recovery"
        operator_action = "inspect_strict_version_authority"
        operator_command = None
    else:
        state = "reset_required"
        operator_action = "execute_policy_epoch_reset"
        operator_command = RESET_COMMAND

    return {
        "evaluation_epoch": EVALUATION_EPOCH,
        "state": state,
        "initialized": initialized,
        "strict_published": strict_published,
        "strict_published_bots": strict_bots,
        "reset_receipt_valid": reset_valid,
        "reset_receipt_digest": (
            str(receipt.get("receipt_digest")) if receipt is not None else None
        ),
        "reset_receipt_issues": list(receipt_errors),
        "version_authority_high_water": authority_high_water,
        "first_strict_version": FIRST_STRICT_POLICY_VERSION,
        "operator_action": operator_action,
        "operator_command": operator_command,
    }


def require_policy_epoch_initialized(operation: str) -> dict[str, Any]:
    """Return canonical initialization state or fail before a mutating launch.

    This function is deliberately read-only.  In particular it emits no
    structured event: before initialization, ``results/`` is retired evidence,
    so even a helpful launch-denied event would contaminate the old epoch.
    """

    state = policy_epoch_initialization()
    if not state["initialized"]:
        raise PolicyEpochInitializationRequired(operation, state)
    return state


def strict_epoch_projection(*, include_checkpoint: bool = True) -> dict[str, Any]:
    """Return the sole status/scheduling view of versions and checkpoint state."""

    import evolution_infra as infra
    from checkpoint_schema import (
        checkpoint_epoch_errors,
        live_policy_epoch_reset_receipt_errors,
    )

    current_v = int(infra.find_current_v())
    initialization = policy_epoch_initialization(current_v=current_v)
    published_versions = sorted({
        version
        for name in initialization.get("strict_published_bots", [])
        if (version := parse_bot_version(str(name))) is not None
        and version >= FIRST_STRICT_POLICY_VERSION
    })
    max_committed_v = int(infra.find_max_committed_v())
    abandoned_floor = int(infra.find_abandoned_version_floor())
    next_v = max(current_v, max_committed_v, abandoned_floor) + 1

    # Discovery is strict and publication-bound; untracked candidate directory
    # names such as national_v155 never become active identities.
    active_bots = list(initialization.get("strict_published_bots", []))

    projection: dict[str, Any] = {
        **initialization,
        "current_v": current_v,
        "next_v": next_v,
        "max_committed_v": max_committed_v,
        "abandoned_floor": abandoned_floor,
        "active_bots": active_bots,
        "active_bots_count": len(active_bots),
        "strict_published_versions": published_versions,
        "strict_generation_count": len(published_versions),
        "active_generation": None,
        "ignored_checkpoint": None,
    }
    if not include_checkpoint:
        return projection

    checkpoint = infra.read_pipeline_checkpoint() or {}
    if not isinstance(checkpoint, dict) or not checkpoint:
        return projection
    stage = checkpoint.get("stage")
    if stage in (None, "archived") or checkpoint.get("next_v") is None:
        return projection

    issues = checkpoint_epoch_errors(checkpoint)
    if not issues:
        issues.extend(
            live_policy_epoch_reset_receipt_errors(
                checkpoint,
                project_root=infra.PROJECT_ROOT,
            )
        )
    if issues:
        projection["ignored_checkpoint"] = {
            "next_v": checkpoint.get("next_v"),
            "source_v": checkpoint.get("source_v"),
            "stage": stage,
            "reason": "checkpoint_not_bound_to_strict_epoch",
            "issues": list(dict.fromkeys(map(str, issues))),
        }
        # Before initialization, the central reset archives both the checkpoint
        # and candidate.  It is evidence for that operator action, never an
        # active generation or a version floor.
        if not initialization["initialized"]:
            return projection
        projection["operator_action"] = "archive_incompatible_checkpoint"
        projection["operator_command"] = None
        return projection

    generation_attempt = int(checkpoint.get("generation_attempt") or 0)
    projection["active_generation"] = {
        "next_v": int(checkpoint["next_v"]),
        "source_v": checkpoint.get("source_v"),
        "stage": stage,
        "run_id": checkpoint.get("run_id")
        or f"{int(checkpoint['next_v'])}#{generation_attempt}",
        "workflow_run_id": checkpoint.get("workflow_run_id"),
        "attempt": {
            "generation": generation_attempt,
            "audit": int(checkpoint.get("audit_attempt") or 0),
            "precommit": int(checkpoint.get("precommit_attempt") or 0),
        },
    }
    projection["next_v"] = int(checkpoint["next_v"])
    if (
        stage == "official_bootstrap_required"
        and int(checkpoint["next_v"]) == FIRST_STRICT_POLICY_VERSION
    ):
        projection["operator_action"] = "run_first_strict_official_certification"
        projection["operator_command"] = FIRST_STRICT_BOOTSTRAP_COMMAND
    return projection


def unpublished_candidate_versions() -> list[int]:
    """List on-disk strict-numbered directories only as non-authoritative debris."""

    import evolution_infra as infra

    active = set(strict_epoch_projection(include_checkpoint=False)["active_bots"])
    versions: list[int] = []
    root = Path(infra.BOTS_DIR)
    if not root.is_dir() or root.is_symlink():
        return versions
    for child in root.iterdir():
        version = parse_bot_version(child.name)
        if (
            version is not None
            and version >= FIRST_STRICT_POLICY_VERSION
            and child.name not in active
            and child.is_dir()
            and not child.is_symlink()
        ):
            versions.append(version)
    return sorted(set(versions))


__all__ = [
    "FIRST_STRICT_BOOTSTRAP_COMMAND",
    "PolicyEpochInitializationRequired",
    "RESET_COMMAND",
    "policy_epoch_initialization",
    "require_policy_epoch_initialized",
    "strict_epoch_projection",
    "unpublished_candidate_versions",
]
