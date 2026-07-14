"""Epoch identity for the non-result orchestrator log directory.

``web/logs`` is outside the normal result tree.  The one-time policy reset
therefore writes a directory marker after archiving every prior file.  HTTP
readers accept logs only while that marker matches the validated live reset
receipt; filenames and mtimes never become authority.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bot_artifact import canonical_digest
from bot_namespace import EVALUATION_EPOCH


LOG_EPOCH_MARKER_FILENAME = "policy_epoch_log_identity.json"
LOG_EPOCH_MARKER_KIND = "national-tcp-policy-log-directory-v1"


def build_log_epoch_marker(reset_receipt: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "kind": LOG_EPOCH_MARKER_KIND,
        "evaluation_epoch": EVALUATION_EPOCH,
        "policy_epoch_reset_receipt_digest": reset_receipt.get("receipt_digest"),
        "scope": "web/logs/orchestrator_current_epoch_only",
    }
    return {**payload, "marker_digest": canonical_digest(payload)}


def validate_log_epoch_marker(
    marker: Any,
    reset_receipt: Any,
) -> list[str]:
    if not isinstance(marker, dict):
        return ["log_epoch_marker_missing_or_not_object"]
    errors: list[str] = []
    if marker.get("schema_version") != 1:
        errors.append("log_epoch_marker_schema_mismatch")
    if marker.get("kind") != LOG_EPOCH_MARKER_KIND:
        errors.append("log_epoch_marker_kind_mismatch")
    if marker.get("evaluation_epoch") != EVALUATION_EPOCH:
        errors.append("log_epoch_marker_epoch_mismatch")
    receipt_digest = (
        reset_receipt.get("receipt_digest")
        if isinstance(reset_receipt, dict)
        else None
    )
    if marker.get("policy_epoch_reset_receipt_digest") != receipt_digest:
        errors.append("log_epoch_marker_reset_receipt_mismatch")
    unsigned = {key: value for key, value in marker.items() if key != "marker_digest"}
    if marker.get("marker_digest") != canonical_digest(unsigned):
        errors.append("log_epoch_marker_digest_mismatch")
    return list(dict.fromkeys(errors))


def load_current_log_epoch_identity(
    results_dir: str | Path,
    logs_dir: str | Path,
) -> dict[str, Any] | None:
    from system_strict_bootstrap import load_policy_epoch_reset_receipt

    receipt, receipt_errors = load_policy_epoch_reset_receipt(results_dir)
    if receipt_errors or not isinstance(receipt, dict):
        return None
    path = Path(logs_dir) / LOG_EPOCH_MARKER_FILENAME
    try:
        if path.is_symlink() or not path.is_file():
            return None
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if validate_log_epoch_marker(marker, receipt):
        return None
    return {
        "evaluation_epoch": EVALUATION_EPOCH,
        "policy_epoch_reset_receipt_digest": receipt["receipt_digest"],
        "marker_digest": marker["marker_digest"],
    }


__all__ = [
    "LOG_EPOCH_MARKER_FILENAME",
    "LOG_EPOCH_MARKER_KIND",
    "build_log_epoch_marker",
    "load_current_log_epoch_identity",
    "validate_log_epoch_marker",
]
