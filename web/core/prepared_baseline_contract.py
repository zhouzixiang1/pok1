"""Digest-bound contract for a prepared two-parent crossover baseline.

Crossover edits the eventual candidate directory in place.  Before Master and
Workers are allowed to continue, the control plane freezes both the exact child
artifact and the deterministic capability snapshot that was accepted.  Later
stages consume this contract as evidence; LLM-authored compatibility prose is
never granted instruction authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from bot_artifact import artifact_manifest, canonical_digest, hash_path
from bot_namespace import bot_name
from national_runtime_probe import _bot_code_fingerprint
from runtime_architecture_policy import (
    FAILURE_CLASS_NONE,
    validate_prepared_capability_snapshot,
)


PREPARED_BASELINE_CONTRACT_SCHEMA_VERSION = 3
PREPARED_ARTIFACT_CONTRACT_SCHEMA_VERSION = 1
_ERROR_DETAIL_VALUE_LIMIT = 96
_HASH_FIELDS = {
    "parent_a": "parent_a_artifact_hash",
    "parent_b": "parent_b_artifact_hash",
    "prepared": "prepared_artifact_hash",
}
_SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9_.\-/]{1,180}$")


def _json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _files(root: Path) -> dict[str, dict[str, Any]]:
    manifest = artifact_manifest(root)
    return {
        str(item["path"]): {
            "sha256": str(item["sha256"]),
            "size": int(item["size"]),
        }
        for item in manifest.get("entries") or []
        if item.get("type") == "file"
    }


def _safe_file_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({
        str(item)
        for item in value
        if _SAFE_FILE_RE.fullmatch(str(item))
    })[:40]


def _python_line_manifest(root: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        if not path.is_file() or path.is_symlink() or "backup" in path.name:
            continue
        relative = path.relative_to(root).as_posix()
        try:
            result[relative] = sum(1 for _ in path.open(encoding="utf-8"))
        except (OSError, UnicodeError):
            result[relative] = -1
    return result


def _component_diff(
    parent_a_files: dict[str, dict[str, Any]],
    parent_b_files: dict[str, dict[str, Any]],
    child_files: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    changed: list[dict[str, Any]] = []
    for name in sorted(set(parent_a_files) | set(child_files)):
        a = parent_a_files.get(name)
        b = parent_b_files.get(name)
        child = child_files.get(name)
        if a == child:
            continue
        child_sha = str((child or {}).get("sha256") or "")
        a_sha = str((a or {}).get("sha256") or "")
        b_sha = str((b or {}).get("sha256") or "")
        if not child:
            provenance = "removed_from_parent_a"
        elif child_sha and child_sha == b_sha:
            provenance = "exact_parent_b_file"
        elif not a:
            provenance = "new_composed_file"
        else:
            provenance = "composed_parent_components"
        changed.append({
            "path": name,
            "provenance_class": provenance,
            "parent_a_sha256": a_sha,
            "parent_b_sha256": b_sha,
            "prepared_sha256": child_sha,
            "prepared_size": int((child or {}).get("size") or 0),
        })
    return changed


def _contract_payload(contract: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in contract.items() if key != "contract_digest"}


def build_prepared_artifact_contract(
    prepared_dir: str | Path,
    *,
    source_v: int,
    next_v: int,
) -> dict[str, Any]:
    """Freeze the common post-prepare artifact boundary for every generation."""
    prepared_dir = Path(prepared_dir)
    contract = {
        "schema_version": PREPARED_ARTIFACT_CONTRACT_SCHEMA_VERSION,
        "source_v": int(source_v),
        "next_v": int(next_v),
        "prepared_bot": prepared_dir.name,
        "prepared_artifact_hash": hash_path(prepared_dir),
        "prepared_artifact_manifest": artifact_manifest(prepared_dir),
    }
    contract["contract_digest"] = canonical_digest(_contract_payload(contract))
    return contract


def validate_prepared_artifact_contract(
    contract: dict[str, Any] | None,
    *,
    prepared_dir: str | Path | None = None,
    source_v: int | None = None,
    next_v: int | None = None,
    verify_live_content: bool = True,
) -> list[str]:
    if not isinstance(contract, dict):
        return ["prepared_artifact_contract_missing_or_not_object"]
    errors: list[str] = []
    if contract.get("schema_version") != PREPARED_ARTIFACT_CONTRACT_SCHEMA_VERSION:
        errors.append("prepared_artifact_contract_schema_mismatch")
    if contract.get("contract_digest") != canonical_digest(_contract_payload(contract)):
        errors.append("prepared_artifact_contract_digest_mismatch")
    for field, expected in (("source_v", source_v), ("next_v", next_v)):
        if expected is not None and contract.get(field) != int(expected):
            errors.append(f"prepared_artifact_contract_{field}_mismatch")
    manifest = contract.get("prepared_artifact_manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), list):
        errors.append("prepared_artifact_contract_manifest_invalid")
    if prepared_dir is not None:
        prepared_dir = Path(prepared_dir)
        if contract.get("prepared_bot") != prepared_dir.name:
            errors.append("prepared_artifact_contract_bot_mismatch")
        if verify_live_content:
            try:
                if contract.get("prepared_artifact_hash") != hash_path(prepared_dir):
                    errors.append("prepared_artifact_contract_hash_mismatch")
                if manifest != artifact_manifest(prepared_dir):
                    errors.append("prepared_artifact_contract_manifest_mismatch")
            except Exception as exc:
                errors.append(
                    f"prepared_artifact_contract_live_error:{type(exc).__name__}"
                )
    return errors


def build_prepared_baseline_contract(
    parent_a_dir: str | Path,
    parent_b_dir: str | Path,
    prepared_dir: str | Path,
    *,
    source_v: int,
    parent2_v: int,
    next_v: int,
    capability_snapshot: dict[str, Any],
    preplan_transition: dict[str, Any],
    expected_policy_digest: str | None = None,
    prepare_scope_files: list[str] | None = None,
    compatibility: dict[str, Any] | None = None,
    h2h_snapshot_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze exact child content, diff provenance, and accepted capabilities."""
    parent_a_dir = Path(parent_a_dir)
    parent_b_dir = Path(parent_b_dir)
    prepared_dir = Path(prepared_dir)
    # The parent name fields bind the canonical semantic bot identity, never
    # the directory basename: build resolves the parents through frozen
    # content-addressed snapshot directories whose 64-hex basename is not an
    # identity.  The binder validates against the live
    # bots/<semantic-name> directories with the same bot_name() derivation.
    parent_a_bot = bot_name(source_v)
    parent_b_bot = bot_name(parent2_v)
    # The preplan transition computed the parent's capabilities static-only
    # (deterministic source anchor) and the candidate's with its probe.  The
    # revalidation rebuild must reuse those exact capability objects: a
    # from-scratch rebuild re-runs the parent probe and deterministically
    # disagrees with the frozen snapshot on every crossover.
    snapshot_errors = validate_prepared_capability_snapshot(
        capability_snapshot,
        parent_bot_dir=parent_a_dir,
        prepared_bot_dir=prepared_dir,
        parent_capabilities=preplan_transition.get("source_capabilities"),
        prepared_capabilities=preplan_transition.get("candidate_capabilities"),
        parent_bot_label=parent_a_bot,
    )
    if snapshot_errors:
        raise ValueError(
            "invalid prepared capability snapshot: " + "; ".join(snapshot_errors)
        )
    if not preplan_transition.get("ok"):
        raise ValueError("cannot bind a prepared baseline from a rejected transition")
    if preplan_transition.get("evaluation_phase") != "preplan":
        raise ValueError("prepared baseline requires a preplan architecture transition")
    if preplan_transition.get("conclusive") is not True:
        raise ValueError("prepared baseline transition must be conclusive")
    if preplan_transition.get("outcome") != "passed":
        raise ValueError("prepared baseline transition outcome must be passed")
    if preplan_transition.get("failure_class") != FAILURE_CLASS_NONE:
        raise ValueError("prepared baseline transition failure_class must be none")
    blocking_fields = (
        "policy_identity_errors",
        "infrastructure_failures",
        "runtime_floor_failures",
        "regressions",
        "unresolved_focus_checks",
    )
    populated_blockers = [
        field for field in blocking_fields if preplan_transition.get(field)
    ]
    if populated_blockers:
        raise ValueError(
            "prepared baseline transition contains blocking evidence: "
            + ", ".join(populated_blockers)
        )
    transition_policy_digest = str(
        (preplan_transition.get("policy") or {}).get("policy_digest") or ""
    )
    if not re.fullmatch(r"[0-9a-f]{64}", transition_policy_digest):
        raise ValueError("prepared baseline transition policy digest is invalid")
    if (
        expected_policy_digest is not None
        and transition_policy_digest != str(expected_policy_digest)
    ):
        raise ValueError("prepared baseline transition policy digest mismatch")

    a_files = _files(parent_a_dir)
    b_files = _files(parent_b_dir)
    child_files = _files(prepared_dir)
    compatibility = compatibility if isinstance(compatibility, dict) else {}
    compatibility_receipt = {
        "payload_digest": _json_digest(compatibility),
        "compatible": bool(compatibility.get("compatible", True)),
        "compatibility_score": compatibility.get("compatibility_score"),
        "files_to_take_from_a": _safe_file_list(
            compatibility.get("files_to_take_from_a")
        ),
        "files_to_take_from_b": _safe_file_list(
            compatibility.get("files_to_take_from_b")
        ),
        "advisory_only": True,
    }
    transition_receipt = {
        "evaluation_phase": str(preplan_transition.get("evaluation_phase") or ""),
        "policy_digest": str(
            (preplan_transition.get("policy") or {}).get("policy_digest") or ""
        ),
        "runtime_floor_failures": list(
            preplan_transition.get("runtime_floor_failures") or []
        ),
        "regressions": list(preplan_transition.get("regressions") or []),
        "deferred_runtime_floor_checks": list(
            preplan_transition.get("deferred_runtime_floor_checks") or []
        ),
        "deferred_unresolved_focus_checks": list(
            preplan_transition.get("deferred_unresolved_focus_checks") or []
        ),
    }
    contract = {
        "schema_version": PREPARED_BASELINE_CONTRACT_SCHEMA_VERSION,
        "next_v": int(next_v),
        "source_v": int(source_v),
        "parent2_v": int(parent2_v),
        "parent_a_bot": parent_a_bot,
        "parent_b_bot": parent_b_bot,
        "prepared_bot": prepared_dir.name,
        "parent_a_artifact_hash": hash_path(parent_a_dir),
        "parent_b_artifact_hash": hash_path(parent_b_dir),
        "prepared_artifact_hash": hash_path(prepared_dir),
        "prepared_artifact_manifest": artifact_manifest(prepared_dir),
        "prepared_artifact_contract": build_prepared_artifact_contract(
            prepared_dir,
            source_v=source_v,
            next_v=next_v,
        ),
        "prepared_code_fingerprint": _bot_code_fingerprint(prepared_dir),
        "component_diff": _component_diff(a_files, b_files, child_files),
        "prepare_scope_files": _safe_file_list(prepare_scope_files or []),
        "prepared_python_lines": _python_line_manifest(prepared_dir),
        "capability_snapshot": capability_snapshot,
        "preplan_transition": transition_receipt,
        # Exact frozen capability objects from the accepted preplan
        # transition.  Bind-time revalidation forwards these so the rebuild
        # reuses the deterministic static parent anchor instead of re-running
        # the non-deterministic typed runtime probe (schema v3).  Both keys are
        # payload members, so the contract digest covers them.
        "preplan_source_capabilities": deepcopy(
            preplan_transition.get("source_capabilities")
        ),
        "preplan_candidate_capabilities": deepcopy(
            preplan_transition.get("candidate_capabilities")
        ),
        "compatibility_receipt": compatibility_receipt,
        "h2h_snapshot_identity": {
            key: str((h2h_snapshot_identity or {}).get(key) or "")
            for key in (
                "manifest_digest",
                "sha256",
                "h2h_relpath",
                "manifest_relpath",
            )
        },
    }
    contract["contract_digest"] = canonical_digest(_contract_payload(contract))
    return contract


def validate_prepared_baseline_contract(
    contract: dict[str, Any] | None,
    *,
    parent_a_dir: str | Path | None = None,
    parent_b_dir: str | Path | None = None,
    prepared_dir: str | Path | None = None,
    source_v: int | None = None,
    parent2_v: int | None = None,
    next_v: int | None = None,
    verify_live_content: bool = True,
) -> list[str]:
    if not isinstance(contract, dict):
        return ["prepared_baseline_contract_missing_or_not_object"]
    errors: list[str] = []
    if contract.get("schema_version") != PREPARED_BASELINE_CONTRACT_SCHEMA_VERSION:
        errors.append("prepared_baseline_contract_schema_mismatch")
    expected_digest = canonical_digest(_contract_payload(contract))
    if contract.get("contract_digest") != expected_digest:
        errors.append("prepared_baseline_contract_digest_mismatch")
    for field, expected in (
        ("source_v", source_v),
        ("parent2_v", parent2_v),
        ("next_v", next_v),
    ):
        if expected is not None and contract.get(field) != int(expected):
            errors.append(f"prepared_baseline_contract_{field}_mismatch")

    # Mirror the build-time forwarding exactly: rebuild the frozen capability
    # snapshot from the contract's frozen preplan capability objects.  A
    # contract missing them (legacy v2 or tampered) re-derives live and fails
    # closed with prepared_capability_snapshot_current_state_mismatch.
    snapshot_errors = validate_prepared_capability_snapshot(
        contract.get("capability_snapshot"),
        parent_bot_dir=parent_a_dir,
        prepared_bot_dir=prepared_dir,
        parent_capabilities=contract.get("preplan_source_capabilities"),
        prepared_capabilities=contract.get("preplan_candidate_capabilities"),
        parent_bot_label=(
            bot_name(source_v) if source_v is not None else None
        ),
    )
    errors.extend(snapshot_errors)
    errors.extend(
        validate_prepared_artifact_contract(
            contract.get("prepared_artifact_contract"),
            prepared_dir=prepared_dir,
            source_v=source_v,
            next_v=next_v,
            verify_live_content=verify_live_content,
        )
    )
    prepared_artifact_contract = contract.get("prepared_artifact_contract") or {}
    if (
        prepared_artifact_contract.get("prepared_artifact_hash")
        != contract.get("prepared_artifact_hash")
    ):
        errors.append("prepared_baseline_contract_artifact_hash_binding_mismatch")
    if (
        prepared_artifact_contract.get("prepared_artifact_manifest")
        != contract.get("prepared_artifact_manifest")
    ):
        errors.append("prepared_baseline_contract_artifact_manifest_binding_mismatch")

    # Parent name fields bind the canonical semantic bot identity, not the
    # directory basename.  Build freezes the parents under content-addressed
    # 64-hex snapshot directories, so a basename comparison can never hold;
    # content equality stays verified through the hash checks below no matter
    # which directory form (frozen or live) the caller presents.
    expected_names = {
        "parent_a_bot": bot_name(source_v) if source_v is not None else None,
        "parent_b_bot": bot_name(parent2_v) if parent2_v is not None else None,
    }
    for label, directory, name_field, hash_field in (
        ("parent_a", parent_a_dir, "parent_a_bot", "parent_a_artifact_hash"),
        ("parent_b", parent_b_dir, "parent_b_bot", "parent_b_artifact_hash"),
        ("prepared", prepared_dir, "prepared_bot", "prepared_artifact_hash"),
    ):
        expected_name = expected_names.get(name_field)
        if expected_name is None:
            # prepared_bot (and legacy parent callers without a version)
            # keep binding the live directory basename, which is always the
            # semantic bots/<name> directory.
            expected_name = directory.name if directory is not None else None
        if expected_name is None and directory is None:
            continue
        if contract.get(name_field) != expected_name:
            errors.append(f"prepared_baseline_contract_{label}_bot_mismatch")
        if directory is None or not verify_live_content:
            continue
        directory = Path(directory)
        try:
            if contract.get(hash_field) != hash_path(directory):
                errors.append(
                    f"prepared_baseline_contract_{label}_artifact_hash_mismatch"
                )
        except Exception as exc:
            errors.append(
                f"prepared_baseline_contract_{label}_artifact_hash_error:"
                f"{type(exc).__name__}"
            )
    if prepared_dir is not None and verify_live_content:
        try:
            if contract.get("prepared_artifact_manifest") != artifact_manifest(
                Path(prepared_dir)
            ):
                errors.append(
                    "prepared_baseline_contract_prepared_artifact_manifest_mismatch"
                )
            if contract.get("prepared_code_fingerprint") != _bot_code_fingerprint(
                Path(prepared_dir)
            ):
                errors.append("prepared_baseline_contract_code_fingerprint_mismatch")
        except Exception as exc:
            errors.append(
                "prepared_baseline_contract_code_fingerprint_error:"
                f"{type(exc).__name__}"
            )
    return errors


def _detail_text(value: Any) -> str:
    text = "" if value is None else str(value)
    if len(text) > _ERROR_DETAIL_VALUE_LIMIT:
        text = f"{text[:_ERROR_DETAIL_VALUE_LIMIT]}...len={len(text)}"
    return text


def _detail_digest(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text[:16] + f"...len={len(text)}"


def prepared_baseline_contract_error_details(
    contract: Any,
    errors: list[str],
    *,
    parent_a_dir: str | Path | None = None,
    parent_b_dir: str | Path | None = None,
    prepared_dir: str | Path | None = None,
    source_v: int | None = None,
    parent2_v: int | None = None,
    next_v: int | None = None,
) -> dict[str, dict[str, str]]:
    """Return bounded expected/actual pairs for one bind-failure diagnosis.

    Attached to the ``pipeline.master_prepared_baseline_invalid`` system event
    so the next incident is diagnosable from the event log alone.  This is
    operations telemetry only: never prompt or evaluation evidence.  Every
    value is truncated and live reads are wrapped, so the payload stays bounded
    and this helper cannot raise into the abandon path.
    """

    details: dict[str, dict[str, str]] = {}
    try:
        if not isinstance(contract, dict):
            return {"prepared_baseline_contract_missing_or_not_object": {
                "expected": "digest-bound prepared baseline contract object",
                "actual": type(contract).__name__,
            }}
        present = set(errors or [])
        snapshot = (
            contract.get("capability_snapshot")
            if isinstance(contract.get("capability_snapshot"), dict)
            else {}
        )

        def record(code: str, expected: Any, actual: Any) -> None:
            if code in present and code not in details:
                details[code] = {
                    "expected": _detail_text(expected),
                    "actual": _detail_text(actual),
                }

        record(
            "prepared_baseline_contract_schema_mismatch",
            f"schema_version={PREPARED_BASELINE_CONTRACT_SCHEMA_VERSION}",
            f"schema_version={contract.get('schema_version')}",
        )
        record(
            "prepared_baseline_contract_digest_mismatch",
            _detail_digest(contract.get("contract_digest")),
            _detail_digest(canonical_digest(_contract_payload(contract))),
        )
        for field, expected in (
            ("source_v", source_v),
            ("parent2_v", parent2_v),
            ("next_v", next_v),
        ):
            record(
                f"prepared_baseline_contract_{field}_mismatch",
                f"{field}={expected}",
                f"{field}={contract.get(field)}",
            )
        live_names = {
            "parent_a_bot": (
                bot_name(source_v)
                if source_v is not None
                else (Path(parent_a_dir).name if parent_a_dir else None)
            ),
            "parent_b_bot": (
                bot_name(parent2_v)
                if parent2_v is not None
                else (Path(parent_b_dir).name if parent_b_dir else None)
            ),
            "prepared_bot": (
                Path(prepared_dir).name if prepared_dir else None
            ),
        }
        dirs = {
            "parent_a_bot": parent_a_dir,
            "parent_b_bot": parent_b_dir,
            "prepared_bot": prepared_dir,
        }
        labels = {
            "parent_a_bot": "parent_a",
            "parent_b_bot": "parent_b",
            "prepared_bot": "prepared",
        }
        for name_field, expected_name in live_names.items():
            label = labels[name_field]
            record(
                f"prepared_baseline_contract_{label}_bot_mismatch",
                f"expected_bot={expected_name}",
                (
                    f"contract_bot={contract.get(name_field)} "
                    f"live_dir={Path(dirs[name_field]).name}"
                    if dirs[name_field]
                    else f"contract_bot={contract.get(name_field)}"
                ),
            )
            hash_code = (
                f"prepared_baseline_contract_{label}_artifact_hash_mismatch"
            )
            if hash_code in present and dirs[name_field] is not None:
                live_hash = ""
                try:
                    live_hash = hash_path(Path(dirs[name_field]))
                except Exception as exc:
                    live_hash = f"unavailable:{type(exc).__name__}"
                record(
                    hash_code,
                    "contract_hash="
                    f"{_detail_digest(contract.get(_HASH_FIELDS[label]))}",
                    f"live_hash={_detail_digest(live_hash)}",
                )
        record(
            "prepared_capability_snapshot_current_state_mismatch",
            (
                "frozen parent_bot="
                f"{(snapshot or {}).get('parent_bot')} "
                "prepared_capability_digest="
                f"{_detail_digest((snapshot or {}).get('prepared_capability_digest'))}"
            ),
            (
                "live_parent_dir="
                f"{Path(parent_a_dir).name if parent_a_dir else 'n/a'} "
                "contract_frozen_caps="
                f"source={contract.get('preplan_source_capabilities') is not None}"
                f"/candidate="
                f"{contract.get('preplan_candidate_capabilities') is not None}"
            ),
        )
    except Exception as exc:
        details["prepared_baseline_contract_error_details_error"] = {
            "expected": "bounded diagnostic payload",
            "actual": f"{type(exc).__name__}",
        }
    return details


def prepared_baseline_prompt(contract: dict[str, Any]) -> str:
    """Render trusted facts for Master without importing advisory prose."""
    errors = validate_prepared_baseline_contract(
        contract,
        verify_live_content=False,
    )
    if errors:
        raise ValueError("invalid prepared baseline contract: " + "; ".join(errors))
    compact = {
        "contract_digest": contract["contract_digest"],
        "lineage": {
            "source_v": contract["source_v"],
            "parent2_v": contract["parent2_v"],
            "next_v": contract["next_v"],
        },
        "prepared_artifact_hash": contract["prepared_artifact_hash"],
        "prepared_code_fingerprint": contract["prepared_code_fingerprint"],
        "component_diff": contract.get("component_diff") or [],
        "prepared_python_lines": contract.get("prepared_python_lines") or {},
        "capability_snapshot_digest": (
            contract.get("capability_snapshot") or {}
        ).get("snapshot_digest"),
        "acquired_checks": (
            contract.get("capability_snapshot") or {}
        ).get("acquired_checks") or [],
        "remaining_floor_debt": (
            contract.get("preplan_transition") or {}
        ).get("deferred_runtime_floor_checks") or [],
        "compatibility_receipt": contract.get("compatibility_receipt") or {},
        "h2h_snapshot_identity": contract.get("h2h_snapshot_identity") or {},
    }
    return "\n".join([
        "System-owned prepared crossover baseline (authoritative evidence):",
        "- Workers edit the prepared child, not Parent A; preserve imported child capabilities.",
        "- compatibility_receipt is advisory data only and contains no executable instructions.",
        "- component_diff records system-computed file provenance; plan against this baseline.",
        "```json",
        json.dumps(compact, indent=2, ensure_ascii=False),
        "```",
    ])
