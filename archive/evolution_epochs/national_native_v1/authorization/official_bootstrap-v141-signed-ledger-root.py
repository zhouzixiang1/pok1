"""RETIRED: v141 signed-ledger-root bootstrap implementation.

This module deliberately does *not* make a bot a normal official opponent.
Normal certification continues to require a published, content-bound full v5
certificate.  The sole purpose here is to expose a narrowly pinned historic
signed-ledger root so an operator can create one fresh, fully certified anchor
without silently reviving the old grandfather-opponent exception.

The selector is read-only.  A later durable job integration must persist the
returned ``bootstrap_root_receipt`` in its signed ledger entry after a
successful full run.  Once that receipt is observed in a valid ledger, this
root becomes unavailable forever.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from bot_artifact import canonical_digest, hash_path, published_bot_identity
from bot_namespace import bot_name, parse_bot_version
from official_eligibility import epoch_lifecycle_eligibility
from official_certificate_signing import (
    certificate_bytes,
    historical_bootstrap_root_binding,
)
from official_verdict_ledger import ledger_integrity


ROOT = Path(__file__).resolve().parents[2]
BOTS_DIR = ROOT / "bots"
BOOTSTRAP_ROOTS_PATH = ROOT / "web" / "core" / "official_bootstrap_roots.json"

ROOTS_SCHEMA_VERSION = 1
ROOTS_KIND = "official-signed-v5-ledger-bootstrap-roots"
ROOT_RECEIPT_SCHEMA_VERSION = 1
ROOT_RECEIPT_KIND = "signed-v5-ledger-bootstrap-root-receipt"
ROOT_SELECTION_KIND = "signed-v5-ledger-bootstrap-selection"
PARKED_REQUEST_SCHEMA_VERSION = 1
PARKED_REQUEST_KIND = "official-bootstrap-parked-candidate-request"
OPERATOR_AUTHORIZATION_SCHEMA_VERSION = 1
OPERATOR_AUTHORIZATION_KIND = "official-bootstrap-operator-authorization"
FULL_V5_POLICY_ID = "official-full-v5"
DEFAULT_BOOTSTRAP_ROOT_ID = "national-v141-official-full-v5-signed-ledger-root"

_PARKED_FACT_FIELDS = (
    "candidate_path",
    "candidate_label",
    "candidate_version",
    "candidate_hash",
    "source_v",
    "workflow_run_id",
    "checkpoint_contract_digest",
    "evaluation_contract_version",
    "evaluation_contract_hash",
    "protocol_bootstrap_receipt",
    "protocol_bootstrap_receipt_digest",
    "transition_receipt_digest",
    "active_bots",
    "strict_published_bots",
    "root_id",
)

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ROOT_ENTRY_FIELDS = (
    "sequence",
    "entry_digest",
    "candidate_label",
    "candidate_hash",
    "policy_id",
    "mode",
    "outcome",
    "authoritative",
    "blocking",
    "classification",
    "certificate_digest",
)


class BootstrapRootConfigurationError(ValueError):
    """The repository-owned fixed root manifest is malformed."""


def _digest_bound(payload: dict[str, Any], *, field: str = "receipt_digest") -> dict[str, Any]:
    return {**payload, field: canonical_digest(payload)}


def _is_hex(value: object, length: int) -> bool:
    return isinstance(value, str) and bool((_HEX40 if length == 40 else _HEX64).fullmatch(value))


def _read_roots(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise BootstrapRootConfigurationError("bootstrap roots manifest is missing or not regular")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BootstrapRootConfigurationError(
            f"bootstrap roots manifest unreadable:{type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise BootstrapRootConfigurationError("bootstrap roots manifest must be an object")
    return payload


def _validate_root(root: object) -> dict[str, Any]:
    if not isinstance(root, dict):
        raise BootstrapRootConfigurationError("bootstrap root must be an object")
    root_id = root.get("root_id")
    bot = root.get("bot")
    version = root.get("version")
    if not isinstance(root_id, str) or not root_id.strip():
        raise BootstrapRootConfigurationError("bootstrap root_id is missing")
    if type(version) is not int or version <= 0:
        raise BootstrapRootConfigurationError(f"bootstrap root version invalid:{root_id}")
    if not isinstance(bot, str) or parse_bot_version(bot) != version or bot_name(version) != bot:
        raise BootstrapRootConfigurationError(f"bootstrap root bot/version invalid:{root_id}")
    if root.get("tag") != f"national-bot-v{version}":
        raise BootstrapRootConfigurationError(f"bootstrap root tag invalid:{root_id}")
    for field, length in (
        ("artifact_hash", 64),
        ("tag_object", 40),
        ("completion_tree_oid", 40),
    ):
        if not _is_hex(root.get(field), length):
            raise BootstrapRootConfigurationError(f"bootstrap root {field} invalid:{root_id}")
    if root.get("max_successful_consumptions") != 1:
        raise BootstrapRootConfigurationError(
            f"bootstrap root must be one-time:{root_id}"
        )
    entry = root.get("ledger_entry")
    if not isinstance(entry, dict):
        raise BootstrapRootConfigurationError(f"bootstrap root ledger entry missing:{root_id}")
    if entry.get("candidate_label") != bot or entry.get("candidate_hash") != root.get("artifact_hash"):
        raise BootstrapRootConfigurationError(f"bootstrap root ledger identity mismatch:{root_id}")
    if entry.get("policy_id") != FULL_V5_POLICY_ID or entry.get("mode") != "full":
        raise BootstrapRootConfigurationError(f"bootstrap root ledger policy invalid:{root_id}")
    if entry.get("outcome") != "official-certified" or entry.get("authoritative") is not True:
        raise BootstrapRootConfigurationError(f"bootstrap root ledger outcome invalid:{root_id}")
    if entry.get("blocking") is not False or entry.get("classification") != "pass":
        raise BootstrapRootConfigurationError(f"bootstrap root ledger verdict invalid:{root_id}")
    if type(entry.get("sequence")) is not int or int(entry["sequence"]) <= 0:
        raise BootstrapRootConfigurationError(f"bootstrap root ledger sequence invalid:{root_id}")
    for field in ("entry_digest", "candidate_hash", "certificate_digest"):
        if not _is_hex(entry.get(field), 64):
            raise BootstrapRootConfigurationError(f"bootstrap root ledger {field} invalid:{root_id}")
    return root


def _validate_retired_signer_root_binding(
    manifest: dict[str, Any],
    manifest_path: Path,
    roots: list[dict[str, Any]],
) -> None:
    """Cross-bind each bootstrap root to an exact retired-signer exception.

    The retired key is not a general grandfather issuer.  Selection is allowed
    only while both the root manifest bytes and its semantic root/ledger fields
    still match the application trust policy that retained the v141 chain.
    """
    file_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    canonical_sha256 = hashlib.sha256(certificate_bytes(manifest)).hexdigest()
    for root in roots:
        root_id = str(root["root_id"])
        try:
            binding = historical_bootstrap_root_binding(root_id)
        except Exception as exc:
            raise BootstrapRootConfigurationError(
                f"bootstrap root signer policy invalid:{root_id}:{type(exc).__name__}"
            ) from exc
        if not isinstance(binding, dict):
            raise BootstrapRootConfigurationError(
                f"bootstrap root signer policy missing:{root_id}"
            )
        comparisons = {
            "bootstrap_manifest_file_sha256": file_sha256,
            "bootstrap_manifest_canonical_sha256": canonical_sha256,
            "candidate_label": root["bot"],
            "candidate_hash": root["artifact_hash"],
            "certificate_digest": root["ledger_entry"]["certificate_digest"],
            "ledger_sequence": root["ledger_entry"]["sequence"],
            "ledger_entry_digest": root["ledger_entry"]["entry_digest"],
            "bootstrap_root_id": root_id,
        }
        for field, actual in comparisons.items():
            if binding.get(field) != actual:
                raise BootstrapRootConfigurationError(
                    f"bootstrap root signer policy mismatch:{root_id}:{field}"
                )


def load_signed_v5_ledger_bootstrap_roots(
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Load and strictly validate the repository-owned one-time root manifest."""
    manifest_path = Path(path) if path is not None else BOOTSTRAP_ROOTS_PATH
    payload = _read_roots(manifest_path)
    if payload.get("schema_version") != ROOTS_SCHEMA_VERSION:
        raise BootstrapRootConfigurationError("bootstrap roots schema version invalid")
    if payload.get("kind") != ROOTS_KIND:
        raise BootstrapRootConfigurationError("bootstrap roots kind invalid")
    if not isinstance(payload.get("policy_id"), str) or not payload["policy_id"].strip():
        raise BootstrapRootConfigurationError("bootstrap roots policy_id missing")
    roots = payload.get("roots")
    if not isinstance(roots, list) or not roots:
        raise BootstrapRootConfigurationError("bootstrap roots list missing")
    ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for root in roots:
        item = _validate_root(root)
        root_id = str(item["root_id"])
        if root_id in ids:
            raise BootstrapRootConfigurationError(f"duplicate bootstrap root_id:{root_id}")
        ids.add(root_id)
        validated.append(item)
    _validate_retired_signer_root_binding(payload, manifest_path, validated)
    return {**payload, "roots": validated}


def _configured_root(root_id: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        manifest = load_signed_v5_ledger_bootstrap_roots()
    except Exception as exc:
        return None, f"bootstrap_root_configuration_invalid:{type(exc).__name__}:{str(exc)[:180]}"
    root = next(
        (item for item in manifest["roots"] if item.get("root_id") == root_id),
        None,
    )
    if root is None:
        return None, "bootstrap_root_unknown"
    return root, None


def _root_path(root: dict[str, Any]) -> Path:
    return ROOT / "bots" / str(root["bot"])


def _native_contract_errors(path: Path) -> list[str]:
    try:
        from national_native import check_native_contract

        return list(check_native_contract(path))
    except Exception as exc:
        return [f"native_contract_check_error:{type(exc).__name__}:{str(exc)[:180]}"]


def _validated_ledger_entries() -> tuple[list[dict[str, Any]], list[str]]:
    """Read the fully signature-validated append-only history under its lock.

    ``official_verdict_ledger`` intentionally exposes only latest-by-candidate
    publicly.  Root-consumption detection needs the complete signed history,
    so it uses its internal validated read boundary rather than parsing JSONL
    independently.
    """
    try:
        from official_verdict_ledger import _locked_ledger, _read_validated

        with _locked_ledger() as path:
            entries, issues = _read_validated(path)
        return list(entries), list(issues)
    except Exception as exc:
        return [], [f"official_verdict_ledger_read_error:{type(exc).__name__}:{str(exc)[:180]}"]


def _matching_root_entry(
    root: dict[str, Any], entries: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    expected = root["ledger_entry"]
    matches = [
        entry
        for entry in entries
        if entry.get("entry_digest") == expected.get("entry_digest")
    ]
    if len(matches) != 1:
        return None, "bootstrap_root_ledger_entry_missing"
    entry = matches[0]
    for field in _ROOT_ENTRY_FIELDS:
        if entry.get(field) != expected.get(field):
            return None, f"bootstrap_root_ledger_entry_mismatch:{field}"
    # A later signed blocking failure for the same immutable artifact revokes
    # this historic exception instead of letting an old pass outrank it.
    for later in entries:
        if int(later.get("sequence") or 0) <= int(entry.get("sequence") or 0):
            continue
        if (
            later.get("candidate_hash") == root.get("artifact_hash")
            and later.get("authoritative") is True
            and later.get("outcome") != "official-certified"
        ):
            return None, "bootstrap_root_superseded_by_authoritative_verdict"
    return entry, None


def _root_receipt(root: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    root_manifest = {
        key: root.get(key)
        for key in (
            "root_id",
            "bot",
            "version",
            "artifact_hash",
            "tag",
            "tag_object",
            "completion_tree_oid",
            "max_successful_consumptions",
            "ledger_entry",
        )
    }
    payload = {
        "schema_version": ROOT_RECEIPT_SCHEMA_VERSION,
        "kind": ROOT_RECEIPT_KIND,
        "role": "official_bootstrap_root",
        "root_id": root["root_id"],
        "policy_id": FULL_V5_POLICY_ID,
        "bot": root["bot"],
        "artifact_hash": root["artifact_hash"],
        "tag": root["tag"],
        "tag_object": root["tag_object"],
        "completion_tree_oid": root["completion_tree_oid"],
        "ledger_sequence": entry["sequence"],
        "ledger_entry_digest": entry["entry_digest"],
        "certificate_digest": entry["certificate_digest"],
        "root_manifest_digest": canonical_digest(root_manifest),
        "max_successful_consumptions": root["max_successful_consumptions"],
    }
    return _digest_bound(payload)


def _consumption_fields(entry: dict[str, Any]) -> tuple[str, str]:
    """Return root-id/receipt-digest fields reserved for future job entries.

    The ledger schema currently has no bootstrap fields.  Supporting these
    exact spellings now makes a later append-only schema extension observable
    without granting anything based on an unbound marker.
    """
    nested = entry.get("bootstrap_root")
    nested = nested if isinstance(nested, dict) else {}
    root_id = str(
        entry.get("bootstrap_root_id")
        or entry.get("signed_v5_ledger_bootstrap_root_id")
        or nested.get("root_id")
        or ""
    )
    receipt_digest = str(
        entry.get("bootstrap_root_receipt_digest")
        or entry.get("signed_v5_ledger_bootstrap_receipt_digest")
        or nested.get("receipt_digest")
        or ""
    )
    return root_id, receipt_digest


def _consumption_report(
    root: dict[str, Any],
    receipt: dict[str, Any],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    matched: list[dict[str, Any]] = []
    successful: list[dict[str, Any]] = []
    issues: list[str] = []
    expected_digest = str(receipt["receipt_digest"])
    for entry in entries:
        entry_root_id, entry_receipt_digest = _consumption_fields(entry)
        if entry_root_id != root["root_id"]:
            continue
        matched.append(entry)
        digest = str(entry.get("entry_digest") or "")
        if entry_receipt_digest != expected_digest:
            issues.append(f"bootstrap_root_consumption_receipt_mismatch:{digest}")
            continue
        if (
            entry.get("outcome") == "official-certified"
            and entry.get("policy_id") == FULL_V5_POLICY_ID
            and entry.get("mode") == "full"
            and entry.get("authoritative") is True
            and entry.get("blocking") is False
            and entry.get("classification") == "pass"
        ):
            successful.append(entry)
    return {
        "root_id": root["root_id"],
        "receipt_digest": expected_digest,
        "consumed": len(successful) >= int(root["max_successful_consumptions"]),
        "successful_count": len(successful),
        "max_successful_consumptions": int(root["max_successful_consumptions"]),
        "matched_entry_digests": [str(item.get("entry_digest") or "") for item in matched],
        "successful_entry_digests": [str(item.get("entry_digest") or "") for item in successful],
        "issues": issues,
    }


def signed_v5_ledger_bootstrap_root_consumption(root_id: str) -> dict[str, Any]:
    """Report only signed, receipt-bound successful root consumption.

    It never assumes that a missing future field means an existing root was
    consumed.  Conversely, an entry that claims this root with a mismatched or
    missing receipt is surfaced as a fail-closed integrity issue.
    """
    root, error = _configured_root(root_id)
    if root is None:
        return {"root_id": root_id, "valid": False, "reason": error, "consumed": False}
    health = ledger_integrity()
    if not health.get("valid"):
        return {
            "root_id": root_id,
            "valid": False,
            "reason": "bootstrap_root_signed_ledger_invalid",
            "ledger_issues": list(health.get("issues") or []),
            "consumed": False,
        }
    entries, issues = _validated_ledger_entries()
    if issues:
        return {
            "root_id": root_id,
            "valid": False,
            "reason": "bootstrap_root_signed_ledger_invalid",
            "ledger_issues": issues,
            "consumed": False,
        }
    entry, entry_error = _matching_root_entry(root, entries)
    if entry is None:
        return {"root_id": root_id, "valid": False, "reason": entry_error, "consumed": False}
    receipt = _root_receipt(root, entry)
    report = _consumption_report(root, receipt, entries)
    return {"valid": not report["issues"], "reason": "ok" if not report["issues"] else "bootstrap_root_consumption_invalid", **report}


def _validate_root_runtime(
    root: dict[str, Any], entries: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    path = _root_path(root)
    identity = published_bot_identity(path)
    if not identity.get("published"):
        return None, None, "bootstrap_root_not_published"
    for field in ("label", "version", "artifact_hash", "tag", "tag_object", "completion_tree_oid"):
        expected = root["bot"] if field == "label" else root.get(field)
        if identity.get(field) != expected:
            return None, None, f"bootstrap_root_identity_mismatch:{field}"
    if not (path / ".completed").is_file():
        return None, None, "bootstrap_root_missing_completed_sentinel"
    lifecycle = epoch_lifecycle_eligibility(int(root["version"]))
    if not lifecycle.get("eligible"):
        return None, None, "bootstrap_root_lifecycle_ineligible"
    native_errors = _native_contract_errors(path)
    if native_errors:
        return None, None, "bootstrap_root_native_contract_failed"
    entry, entry_error = _matching_root_entry(root, entries)
    if entry is None:
        return None, None, entry_error
    return identity, entry, None


def _candidate_binding(
    candidate_path: str | Path,
    root: dict[str, Any],
    *,
    allow_published: bool = False,
) -> tuple[dict[str, Any] | None, str | None]:
    path = Path(candidate_path).expanduser().resolve()
    version = parse_bot_version(path.name)
    if version is None:
        return None, "bootstrap_candidate_invalid_national_label"
    if path == _root_path(root).resolve() or version == int(root["version"]):
        return None, "bootstrap_candidate_cannot_be_root"
    if version <= int(root["version"]):
        return None, "bootstrap_candidate_not_newer_than_root"
    if not path.is_dir() or not (path / "national_bot.py").is_file():
        return None, "bootstrap_candidate_missing_native_entry"
    # The one-time root can establish exactly one *new* full-v5 anchor.  It
    # must never be used to retrofit an already published/completed artifact.
    if (path / ".completed").exists() and not allow_published:
        return None, "bootstrap_candidate_already_completed"
    try:
        published = published_bot_identity(path)
    except Exception as exc:
        return None, f"bootstrap_candidate_publication_check_error:{type(exc).__name__}"
    if published.get("published") and not allow_published:
        return None, "bootstrap_candidate_already_published"
    lifecycle = epoch_lifecycle_eligibility(version)
    if not lifecycle.get("eligible"):
        return None, "bootstrap_candidate_lifecycle_ineligible"
    native_errors = _native_contract_errors(path)
    if native_errors:
        return None, "bootstrap_candidate_native_contract_failed"
    try:
        from national_protocol_quarantine import (
            current_system_native_runtime_errors,
        )

        runtime_errors = current_system_native_runtime_errors(path)
    except Exception as exc:
        runtime_errors = [
            "system_owned_runtime_check_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        ]
    if runtime_errors:
        return None, "bootstrap_candidate_system_owned_runtime_failed"
    try:
        artifact_hash = hash_path(path)
    except Exception as exc:
        return None, f"bootstrap_candidate_artifact_hash_error:{type(exc).__name__}"
    if artifact_hash == root["artifact_hash"]:
        return None, "bootstrap_candidate_artifact_clone"
    payload = {
        "kind": ROOT_SELECTION_KIND,
        "root_id": root["root_id"],
        "candidate": str(path),
        "candidate_label": path.name,
        "candidate_version": version,
        "candidate_hash": artifact_hash,
    }
    return _digest_bound(payload, field="candidate_binding_digest"), None


def _completion_tag_exists(version: int) -> bool:
    result = subprocess.run(
        [
            "git",
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/tags/national-bot-v{int(version)}",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def _checkpoint_gate_contract_projection(checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Return only the parked candidate/gate authority that must remain stable."""

    audit = checkpoint.get("audit_context") or {}
    audit = audit if isinstance(audit, dict) else {}
    gates = checkpoint.get("gate_results") or {}
    gates = gates if isinstance(gates, dict) else {}
    return {
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "workflow_run_id": checkpoint.get("workflow_run_id"),
        "workflow_profile_id": checkpoint.get("workflow_profile_id"),
        "national_execution_mode": checkpoint.get("national_execution_mode"),
        "runtime_contract_ledger": checkpoint.get("runtime_contract_ledger"),
        "master_plan_runtime_contract_ledger": (
            (checkpoint.get("master_plan") or {}).get("runtime_contract_ledger")
            if isinstance(checkpoint.get("master_plan"), dict)
            else None
        ),
        "protocol_bootstrap": audit.get("protocol_bootstrap"),
        "protocol_bootstrap_prepare": audit.get("protocol_bootstrap_prepare"),
        "prepared_artifact_contract": audit.get("prepared_artifact_contract"),
        "precommit_eval_plan": audit.get("precommit_eval_plan"),
        "quality_gate": gates.get("quality"),
        "review_gate": gates.get("review"),
        "critic_gate": gates.get("critic"),
        "precommit_gate": gates.get("precommit_eval"),
    }


def _current_pipeline_checkpoint() -> dict[str, Any] | None:
    try:
        from evolution_infra import read_pipeline_checkpoint

        value = read_pipeline_checkpoint()
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _current_operator_bootstrap_facts(
    candidate_path: str | Path,
    root_id: str,
    *,
    checkpoint: dict[str, Any] | None,
    expected_stage: str,
    expected_candidate_hash: str | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Read-only current-state proof for parking, enqueue, and delayed launch."""

    issues: list[str] = []
    ckpt = checkpoint if isinstance(checkpoint, dict) else None
    if ckpt is None:
        return None, ["official_bootstrap_checkpoint_missing"]
    if ckpt.get("stage") != expected_stage:
        issues.append(
            "official_bootstrap_checkpoint_stage_mismatch:"
            f"expected={expected_stage}:actual={ckpt.get('stage')}"
        )
    path = Path(candidate_path).expanduser().resolve()
    version = parse_bot_version(path.name)
    if version is None:
        issues.append("official_bootstrap_candidate_label_invalid")
        return None, issues
    canonical_path = (BOTS_DIR / bot_name(version)).resolve()
    if path != canonical_path:
        issues.append("official_bootstrap_candidate_path_mismatch")
    try:
        if int(ckpt.get("next_v")) != int(version):
            issues.append("official_bootstrap_candidate_version_mismatch")
    except (TypeError, ValueError):
        issues.append("official_bootstrap_checkpoint_version_invalid")
    try:
        source_v = int(ckpt.get("source_v"))
    except (TypeError, ValueError):
        source_v = -1
        issues.append("official_bootstrap_checkpoint_source_invalid")
    if path.is_symlink() or not path.is_dir() or not (path / "national_bot.py").is_file():
        issues.append("official_bootstrap_candidate_artifact_missing")
    try:
        candidate_hash = hash_path(path)
    except Exception as exc:
        candidate_hash = ""
        issues.append(
            "official_bootstrap_candidate_hash_error:"
            f"{type(exc).__name__}"
        )
    if expected_candidate_hash and candidate_hash != str(expected_candidate_hash):
        issues.append("official_bootstrap_candidate_hash_mismatch")
    if (path / ".completed").exists():
        issues.append("official_bootstrap_candidate_already_completed")
    try:
        if _completion_tag_exists(version):
            issues.append("official_bootstrap_candidate_already_tagged")
    except Exception as exc:
        issues.append(
            "official_bootstrap_candidate_tag_check_error:"
            f"{type(exc).__name__}"
        )

    root, root_error = _configured_root(root_id)
    if root is None:
        issues.append(str(root_error or "bootstrap_root_unknown"))

    try:
        from evolution_infra import get_active_bots_read_only
        from national_protocol_quarantine import (
            select_protocol_bootstrap_source,
            strict_published_bot_names,
            validate_protocol_bootstrap_receipt,
        )

        active_bots = list(get_active_bots_read_only())
        strict_bots = list(strict_published_bot_names())
        if active_bots:
            issues.append("official_bootstrap_active_pool_not_empty")
        if strict_bots:
            issues.append("official_bootstrap_strict_publication_exists")
        audit = ckpt.get("audit_context") or {}
        protocol_receipt = (
            audit.get("protocol_bootstrap") if isinstance(audit, dict) else None
        )
        receipt_errors = validate_protocol_bootstrap_receipt(
            protocol_receipt,
            active_bots=active_bots,
        )
        issues.extend(
            f"official_bootstrap_transition:{item}" for item in receipt_errors
        )
        transition = select_protocol_bootstrap_source(
            active_bots,
            force_refresh=True,
        )
        if (
            transition.get("available") is not True
            or transition.get("reason") != "legacy_strategy_migration"
            or transition.get("source_v") != source_v
            or transition.get("receipt") != protocol_receipt
        ):
            issues.append("official_bootstrap_protocol_transition_mismatch")
    except Exception as exc:
        active_bots = []
        strict_bots = []
        protocol_receipt = None
        transition = {}
        issues.append(
            "official_bootstrap_protocol_transition_error:"
            f"{type(exc).__name__}:{str(exc)[:160]}"
        )

    try:
        from tool_commit import validate_commit_gate_ledger

        gate_ledger = validate_commit_gate_ledger(
            version,
            source_v,
            ckpt,
            bot_dir=path,
        )
        if gate_ledger.get("ok") is not True:
            issues.append("official_bootstrap_parked_gate_ledger_invalid")
            issues.extend(
                f"official_bootstrap_parked_gate_missing:{item}"
                for item in (gate_ledger.get("missing_gates") or [])[:8]
            )
            issues.extend(
                "official_bootstrap_parked_gate_failed:"
                f"{item.get('gate', 'unknown')}"
                for item in (gate_ledger.get("failed_gates") or [])[:8]
                if isinstance(item, dict)
            )
        ledger_hash = str(gate_ledger.get("current_code_fingerprint") or "")
        if candidate_hash and ledger_hash != candidate_hash:
            issues.append("official_bootstrap_gate_candidate_hash_mismatch")
    except Exception as exc:
        gate_ledger = {}
        issues.append(
            "official_bootstrap_parked_gate_validation_error:"
            f"{type(exc).__name__}:{str(exc)[:160]}"
        )

    contract_projection = _checkpoint_gate_contract_projection(ckpt)
    try:
        from evaluation_contract import build_evaluation_contract

        live_evaluation_contract = build_evaluation_contract(
            ROOT,
            candidate_v=version,
            source_v=source_v,
            checkpoint=ckpt,
            stage=expected_stage,
            include_hash=True,
        )
        live_evaluation_contract_hash = str(
            live_evaluation_contract.get("hash") or ""
        )
        if not live_evaluation_contract_hash:
            issues.append("official_bootstrap_evaluation_contract_hash_missing")
    except Exception as exc:
        live_evaluation_contract = {}
        live_evaluation_contract_hash = ""
        issues.append(
            "official_bootstrap_evaluation_contract_error:"
            f"{type(exc).__name__}:{str(exc)[:160]}"
        )
    facts = {
        "candidate_path": str(path),
        "candidate_label": path.name,
        "candidate_version": int(version),
        "candidate_hash": candidate_hash,
        "source_v": source_v,
        "workflow_run_id": str(ckpt.get("workflow_run_id") or ""),
        "checkpoint_contract_digest": canonical_digest(contract_projection),
        "evaluation_contract_version": live_evaluation_contract.get("version"),
        "evaluation_contract_hash": live_evaluation_contract_hash,
        "protocol_bootstrap_receipt": protocol_receipt,
        "protocol_bootstrap_receipt_digest": str(
            (protocol_receipt or {}).get("receipt_digest") or ""
        ),
        "transition_receipt_digest": str(
            ((transition or {}).get("receipt") or {}).get("receipt_digest") or ""
        ),
        "active_bots": active_bots,
        "strict_published_bots": strict_bots,
        "root_id": str(root_id),
    }
    return facts, list(dict.fromkeys(issues))


def build_operator_bootstrap_parked_request(
    candidate_path: str | Path,
    checkpoint: dict[str, Any],
    *,
    root_id: str = DEFAULT_BOOTSTRAP_ROOT_ID,
    candidate_hash: str | None = None,
) -> dict[str, Any]:
    """Build the immutable request stored while moving verified -> parked."""

    facts, issues = _current_operator_bootstrap_facts(
        candidate_path,
        root_id,
        checkpoint=checkpoint,
        expected_stage="verified",
        expected_candidate_hash=candidate_hash,
    )
    if issues or facts is None:
        return {"valid": False, "issues": issues, "request": None}
    payload = {
        "schema_version": PARKED_REQUEST_SCHEMA_VERSION,
        "kind": PARKED_REQUEST_KIND,
        **facts,
    }
    request = _digest_bound(payload, field="request_digest")
    return {"valid": True, "issues": [], "request": request}


def _parked_request_issues(
    parked: Any,
    facts: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(parked, dict):
        return ["official_bootstrap_parked_request_missing"]
    issues: list[str] = []
    parked_payload = {
        key: value for key, value in parked.items() if key != "request_digest"
    }
    if parked.get("schema_version") != PARKED_REQUEST_SCHEMA_VERSION:
        issues.append("official_bootstrap_parked_request_schema_mismatch")
    if parked.get("kind") != PARKED_REQUEST_KIND:
        issues.append("official_bootstrap_parked_request_kind_mismatch")
    if parked.get("request_digest") != canonical_digest(parked_payload):
        issues.append("official_bootstrap_parked_request_digest_mismatch")
    if isinstance(facts, dict):
        for field in _PARKED_FACT_FIELDS:
            if parked.get(field) != facts.get(field):
                issues.append(
                    f"official_bootstrap_parked_request_{field}_mismatch"
                )
    return issues


def _operator_bootstrap_authorization(
    selection: dict[str, Any],
    root_id: str,
    parked: dict[str, Any],
    facts: dict[str, Any],
) -> dict[str, Any]:
    root_receipt = selection.get("bootstrap_root_receipt") or {}
    payload = {
        "schema_version": OPERATOR_AUTHORIZATION_SCHEMA_VERSION,
        "kind": OPERATOR_AUTHORIZATION_KIND,
        "root_id": str(root_id),
        "parked_request_digest": parked["request_digest"],
        "checkpoint_contract_digest": facts["checkpoint_contract_digest"],
        "evaluation_contract_version": facts["evaluation_contract_version"],
        "evaluation_contract_hash": facts["evaluation_contract_hash"],
        "workflow_run_id": facts["workflow_run_id"],
        "candidate_path": facts["candidate_path"],
        "candidate_version": facts["candidate_version"],
        "candidate_hash": facts["candidate_hash"],
        "protocol_bootstrap_receipt_digest": facts[
            "protocol_bootstrap_receipt_digest"
        ],
        "transition_receipt_digest": facts["transition_receipt_digest"],
        "root_receipt_digest": str(root_receipt.get("receipt_digest") or ""),
        "root_candidate_binding_digest": str(
            (selection.get("candidate_binding") or {}).get(
                "candidate_binding_digest"
            )
            or ""
        ),
        "active_bots": [],
        "strict_published_bots": [],
    }
    return _digest_bound(payload, field="authorization_digest")


def authorize_operator_bootstrap_selection(
    selection: dict[str, Any],
    root_id: str,
    candidate_path: str | Path,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a root selection to the exact currently parked zero-pool request."""

    ckpt = checkpoint if isinstance(checkpoint, dict) else _current_pipeline_checkpoint()
    parked = (
        ((ckpt or {}).get("audit_context") or {}).get("official_bootstrap_request")
        if isinstance((ckpt or {}).get("audit_context"), dict)
        else None
    )
    if not isinstance(parked, dict):
        return {
            "valid": False,
            "reason": "official_bootstrap_parked_request_missing",
            "issues": ["official_bootstrap_parked_request_missing"],
        }
    facts, fact_issues = _current_operator_bootstrap_facts(
        candidate_path,
        root_id,
        checkpoint=ckpt,
        expected_stage="official_bootstrap_required",
        expected_candidate_hash=str(parked.get("candidate_hash") or ""),
    )
    issues = [*_parked_request_issues(parked, facts), *fact_issues]
    validation = validate_signed_v5_ledger_bootstrap_selection(
        selection,
        root_id,
        candidate_path,
        allow_consumed=False,
    )
    if validation.get("valid") is not True:
        issues.extend(
            f"official_bootstrap_root_selection:{item}"
            for item in (validation.get("issues") or [validation.get("reason")])
            if item
        )
    if issues or facts is None:
        return {
            "valid": False,
            "reason": issues[0] if issues else "official_bootstrap_authorization_invalid",
            "issues": list(dict.fromkeys(issues)),
        }
    authorization = _operator_bootstrap_authorization(
        selection,
        root_id,
        parked,
        facts,
    )
    authorized_selection = {
        **selection,
        "operator_bootstrap_authorization": authorization,
    }
    return {
        "valid": True,
        "reason": "ok",
        "issues": [],
        "selection": authorized_selection,
        "authorization": authorization,
    }


def validate_operator_bootstrap_authorized_selection(
    selection: dict[str, Any],
    root_id: str,
    candidate_path: str | Path,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recompute the parked authorization immediately before a durable launch."""

    if not isinstance(selection, dict):
        return {
            "valid": False,
            "reason": "official_bootstrap_selection_missing",
            "issues": ["official_bootstrap_selection_missing"],
        }
    supplied = selection.get("operator_bootstrap_authorization")
    unsigned = {
        key: value
        for key, value in selection.items()
        if key != "operator_bootstrap_authorization"
    }
    current = authorize_operator_bootstrap_selection(
        unsigned,
        root_id,
        candidate_path,
        checkpoint=checkpoint,
    )
    if current.get("valid") is not True:
        return current
    if supplied != current.get("authorization"):
        return {
            "valid": False,
            "reason": "official_bootstrap_authorization_drift",
            "issues": ["official_bootstrap_authorization_drift"],
            "expected_authorization": current.get("authorization"),
        }
    return current


def validate_completed_operator_bootstrap_authorization(
    status: dict[str, Any],
    candidate_path: str | Path,
    *,
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebind a consumed bootstrap pass to the current parked generation.

    This is a read-only publication guard.  It validates the already completed
    certificate and its single signed-ledger consumption; it never selects a
    new root, starts a job, launches the EXE, or appends another ledger entry.
    """

    if not isinstance(status, dict):
        return {
            "valid": False,
            "reason": "official_bootstrap_completed_status_missing",
            "issues": ["official_bootstrap_completed_status_missing"],
        }
    issues: list[str] = []
    candidate = Path(candidate_path).expanduser().resolve()
    identity = (
        status.get("certification_identity")
        if isinstance(status.get("certification_identity"), dict)
        else {}
    )
    spec = identity.get("spec") if isinstance(identity.get("spec"), dict) else {}
    root_id = str(spec.get("bootstrap_root_id") or "")
    candidate_hash = str(identity.get("candidate_hash") or "")
    if not root_id:
        issues.append("official_bootstrap_completed_root_id_missing")
    if status.get("status") != "official-certified":
        issues.append("official_bootstrap_completed_status_not_certified")
    if status.get("mode") != "full" or status.get("policy_id") != FULL_V5_POLICY_ID:
        issues.append("official_bootstrap_completed_policy_mismatch")
    if Path(str(spec.get("candidate") or "")).expanduser().resolve() != candidate:
        issues.append("official_bootstrap_completed_candidate_path_mismatch")
    if candidate.name != str(status.get("bot") or ""):
        issues.append("official_bootstrap_completed_candidate_label_mismatch")

    selection = (
        status.get("opponent_selection")
        if isinstance(status.get("opponent_selection"), dict)
        else {}
    )
    envelope = (
        status.get("official_job_envelope")
        if isinstance(status.get("official_job_envelope"), dict)
        else {}
    )
    if selection != envelope.get("opponent_selection"):
        issues.append("official_bootstrap_completed_envelope_selection_mismatch")

    try:
        from official_certification import (
            _load_certificate_container,
            official_full_certified,
        )

        if not official_full_certified(status, candidate):
            issues.append("official_bootstrap_completed_certificate_invalid")
        record_path = Path(str(status.get("certificate_path") or ""))
        record, _attestation, container_issues = _load_certificate_container(
            record_path
        )
        issues.extend(
            f"official_bootstrap_completed_certificate:{item}"
            for item in container_issues
        )
        if not isinstance(record, dict):
            issues.append("official_bootstrap_completed_certificate_record_missing")
        else:
            if record.get("certificate_digest") != status.get("certificate_digest"):
                issues.append(
                    "official_bootstrap_completed_certificate_digest_mismatch"
                )
            if record.get("identity") != identity:
                issues.append("official_bootstrap_completed_certificate_identity_mismatch")
            if record.get("opponent_selection") != selection:
                issues.append(
                    "official_bootstrap_completed_certificate_selection_mismatch"
                )
            if record.get("job_envelope") != envelope:
                issues.append(
                    "official_bootstrap_completed_certificate_envelope_mismatch"
                )
    except Exception as exc:
        issues.append(
            "official_bootstrap_completed_certificate_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        )

    try:
        from official_job_envelope import job_envelope_issues

        issues.extend(
            f"official_bootstrap_completed_envelope:{item}"
            for item in job_envelope_issues(
                envelope,
                expected_candidate_hash=candidate_hash,
                expected_opponent_hash=str(identity.get("opponent_hash") or ""),
            )
        )
    except Exception as exc:
        issues.append(
            "official_bootstrap_completed_envelope_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        )

    ckpt = checkpoint if isinstance(checkpoint, dict) else _current_pipeline_checkpoint()
    parked = (
        ((ckpt or {}).get("audit_context") or {}).get("official_bootstrap_request")
        if isinstance((ckpt or {}).get("audit_context"), dict)
        else None
    )
    try:
        facts, fact_issues = _current_operator_bootstrap_facts(
            candidate,
            root_id,
            checkpoint=ckpt,
            expected_stage="official_bootstrap_required",
            expected_candidate_hash=candidate_hash,
        )
    except Exception as exc:
        facts, fact_issues = None, [
            "official_bootstrap_completed_checkpoint_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        ]
    issues.extend(fact_issues)
    issues.extend(_parked_request_issues(parked, facts))
    if isinstance(parked, dict) and isinstance(facts, dict):
        expected_authorization = _operator_bootstrap_authorization(
            selection,
            root_id,
            parked,
            facts,
        )
        if selection.get("operator_bootstrap_authorization") != expected_authorization:
            issues.append("official_bootstrap_completed_authorization_drift")

    try:
        root_validation = validate_signed_v5_ledger_bootstrap_selection(
            selection,
            root_id,
            candidate,
            allow_consumed=True,
        )
    except Exception as exc:
        root_validation = {
            "valid": False,
            "issues": [
                "selection_validation_error:"
                f"{type(exc).__name__}:{str(exc)[:180]}"
            ],
        }
    if root_validation.get("valid") is not True:
        issues.extend(
            f"official_bootstrap_completed_root:{item}"
            for item in (
                root_validation.get("issues")
                or [root_validation.get("reason") or "selection_invalid"]
            )
        )

    entries, ledger_issues = _validated_ledger_entries()
    issues.extend(
        f"official_bootstrap_completed_ledger:{item}" for item in ledger_issues
    )
    root_receipt = (
        selection.get("bootstrap_root_receipt")
        if isinstance(selection.get("bootstrap_root_receipt"), dict)
        else {}
    )
    root_receipt_digest = str(root_receipt.get("receipt_digest") or "")
    successful = [
        entry
        for entry in entries
        if entry.get("bootstrap_root_id") == root_id
        and entry.get("bootstrap_root_receipt_digest") == root_receipt_digest
        and entry.get("outcome") == "official-certified"
        and entry.get("authoritative") is True
        and entry.get("blocking") is False
        and entry.get("classification") == "pass"
    ]
    if len(successful) != 1:
        issues.append(
            "official_bootstrap_completed_consumption_count_mismatch:"
            f"actual={len(successful)}"
        )
        successful_entry = None
    else:
        successful_entry = successful[0]
        deterministic = (
            status.get("official_deterministic_status_receipt")
            if isinstance(status.get("official_deterministic_status_receipt"), dict)
            else {}
        )
        expected_ledger_fields = {
            "candidate_label": candidate.name,
            "candidate_hash": candidate_hash,
            "policy_id": FULL_V5_POLICY_ID,
            "mode": "full",
            "outcome": "official-certified",
            "authoritative": True,
            "blocking": False,
            "classification": "pass",
            "certificate_digest": str(status.get("certificate_digest") or ""),
            "deterministic_status_receipt_digest": str(
                deterministic.get("receipt_digest") or ""
            ),
            "job_envelope_digest": str(envelope.get("envelope_digest") or ""),
            "request_started_ns": status.get("request_started_ns"),
            "request_completed_ns": status.get("request_completed_ns"),
            "bootstrap_root_id": root_id,
            "bootstrap_root_receipt_digest": root_receipt_digest,
        }
        for field, expected in expected_ledger_fields.items():
            if successful_entry.get(field) != expected:
                issues.append(
                    f"official_bootstrap_completed_consumption_{field}_mismatch"
                )
        if status.get("official_verdict_ledger_entry") != successful_entry:
            issues.append("official_bootstrap_completed_status_ledger_entry_mismatch")

    try:
        consumption = signed_v5_ledger_bootstrap_root_consumption(root_id)
        consumption_valid = bool(
            consumption.get("valid") is True
            and consumption.get("consumed") is True
            and int(consumption.get("successful_count") or 0) == 1
        )
    except Exception as exc:
        consumption = {
            "issues": [f"{type(exc).__name__}:{str(exc)[:180]}"],
        }
        consumption_valid = False
    if not consumption_valid:
        issues.append("official_bootstrap_completed_root_consumption_invalid")
    elif successful_entry is not None and consumption.get(
        "successful_entry_digests"
    ) != [successful_entry.get("entry_digest")]:
        issues.append("official_bootstrap_completed_root_consumption_entry_mismatch")

    unique = list(dict.fromkeys(str(item) for item in issues if str(item)))
    return {
        "valid": not unique,
        "reason": "ok" if not unique else unique[0],
        "issues": unique,
        "root_id": root_id,
        "candidate_hash": candidate_hash,
        "workflow_run_id": (facts or {}).get("workflow_run_id"),
        "evaluation_contract_hash": (facts or {}).get(
            "evaluation_contract_hash"
        ),
        "certificate_digest": status.get("certificate_digest"),
        "job_envelope_digest": envelope.get("envelope_digest"),
        "ledger_entry_digest": (
            successful_entry.get("entry_digest")
            if isinstance(successful_entry, dict)
            else None
        ),
    }


def _selection_projection(selection: Any) -> dict[str, Any]:
    selection = selection if isinstance(selection, dict) else {}
    opponent = selection.get("opponent") if isinstance(selection.get("opponent"), dict) else {}
    return {
        "selected": selection.get("selected") is True,
        "eligible": selection.get("eligible") is True,
        "reason": selection.get("reason"),
        "root_id": selection.get("root_id") or selection.get("bootstrap_root_id"),
        "candidate": str(selection.get("candidate") or ""),
        "candidate_binding": selection.get("candidate_binding"),
        "bootstrap_root_receipt": selection.get("bootstrap_root_receipt"),
        "opponent": {
            key: opponent.get(key)
            for key in (
                "bot",
                "path",
                "artifact_hash",
                "tag",
                "tag_object",
                "completion_tree_oid",
                "eligible",
                "reason",
                "eligibility_receipt",
            )
        },
    }


def _expected_selection(
    root: dict[str, Any],
    identity: dict[str, Any],
    entry: dict[str, Any],
    candidate_binding: dict[str, Any] | None,
    consumption: dict[str, Any],
) -> dict[str, Any]:
    receipt = _root_receipt(root, entry)
    return {
        "selected": True,
        "eligible": True,
        "reason": "signed_v5_ledger_bootstrap_root",
        "root_id": root["root_id"],
        "candidate": candidate_binding["candidate"] if candidate_binding else None,
        "candidate_binding": candidate_binding,
        "opponent": {
            "bot": identity["label"],
            "path": identity["path"],
            "artifact_hash": identity["artifact_hash"],
            "tag": identity["tag"],
            "tag_object": identity["tag_object"],
            "completion_tree_oid": identity["completion_tree_oid"],
            "eligible": True,
            "reason": "signed_v5_ledger_bootstrap_root",
            "eligibility_receipt": receipt,
        },
        "bootstrap_root_receipt": receipt,
        "consumption": consumption,
    }


def validate_signed_v5_ledger_bootstrap_selection(
    selection: Any,
    root_id: str,
    candidate_path: str | Path,
    *,
    allow_consumed: bool = False,
) -> dict[str, Any]:
    """Validate every immutable bootstrap receipt field for a durable record.

    ``allow_consumed`` is only for validating the resulting certificate after
    its successful ledger append.  It never makes a consumed root selectable
    for a new formal run; only :func:`select_signed_v5_ledger_bootstrap_root`
    can do that and it always rejects prior consumption.
    """
    entries, ledger_issues = _validated_ledger_entries()
    if ledger_issues:
        return {
            "valid": False,
            "reason": "bootstrap_root_signed_ledger_invalid",
            "issues": ["bootstrap_root_signed_ledger_invalid", *ledger_issues],
        }
    return validate_signed_v5_ledger_bootstrap_selection_from_entries(
        selection,
        root_id,
        candidate_path,
        entries,
        allow_consumed=allow_consumed,
    )


def validate_signed_v5_ledger_bootstrap_selection_from_entries(
    selection: Any,
    root_id: str,
    candidate_path: str | Path,
    validated_entries: list[dict[str, Any]],
    *,
    allow_consumed: bool = False,
) -> dict[str, Any]:
    """Validate a bootstrap selection against an already locked ledger view.

    ``validated_entries`` must come from ``official_verdict_ledger._read_validated``
    while the caller still owns that ledger lock.  This boundary deliberately
    performs no ledger I/O: the append transaction can therefore validate the
    one-time root and append its consuming entry against one atomic view without
    recursively acquiring ``flock``.
    """

    root, configuration_error = _configured_root(root_id)
    if root is None:
        return {
            "valid": False,
            "reason": configuration_error,
            "issues": [configuration_error],
        }
    if not isinstance(validated_entries, list) or any(
        not isinstance(entry, dict) for entry in validated_entries
    ):
        return {
            "valid": False,
            "reason": "bootstrap_root_locked_ledger_view_invalid",
            "issues": ["bootstrap_root_locked_ledger_view_invalid"],
        }
    entries = list(validated_entries)
    identity, entry, runtime_error = _validate_root_runtime(root, entries)
    if identity is None or entry is None or runtime_error:
        reason = runtime_error or "bootstrap_root_runtime_validation_failed"
        return {"valid": False, "reason": reason, "issues": [reason]}
    candidate_binding, candidate_error = _candidate_binding(
        candidate_path,
        root,
        # After a successful signed append the candidate may subsequently be
        # published/tagged.  Historical certificate validation must still
        # verify the same bound hash, but cannot authorize a new run.
        allow_published=allow_consumed,
    )
    if candidate_binding is None:
        reason = candidate_error or "bootstrap_candidate_binding_invalid"
        return {"valid": False, "reason": reason, "issues": [reason]}
    receipt = _root_receipt(root, entry)
    consumption = _consumption_report(root, receipt, entries)
    issues = list(consumption.get("issues") or [])
    if consumption.get("consumed") and not allow_consumed:
        issues.append("bootstrap_root_already_consumed")
    expected = _expected_selection(root, identity, entry, candidate_binding, consumption)
    if _selection_projection(selection) != _selection_projection(expected):
        issues.append("bootstrap_root_selection_receipt_mismatch")
    return {
        "valid": not issues,
        "reason": "ok" if not issues else issues[0],
        "issues": list(dict.fromkeys(issues)),
        "expected_selection": expected,
        "consumption": consumption,
    }


def select_signed_v5_ledger_bootstrap_root(
    root_id: str,
    candidate_path: str | Path | None = None,
) -> dict[str, Any]:
    """Select the one explicitly pinned root, or return a fail-closed reason.

    This has no effect on regular official-opponent selection.  Callers must
    use the returned immutable receipt only in an explicit operator bootstrap
    path and write ``bootstrap_root_id`` plus ``bootstrap_root_receipt_digest``
    into the successful signed ledger entry.
    """
    root, configuration_error = _configured_root(root_id)
    if root is None:
        return {
            "selected": False,
            "eligible": False,
            "root_id": root_id,
            "reason": configuration_error,
        }
    health = ledger_integrity()
    if not health.get("valid"):
        return {
            "selected": False,
            "eligible": False,
            "root_id": root_id,
            "reason": "bootstrap_root_signed_ledger_invalid",
            "ledger_issues": list(health.get("issues") or []),
        }
    entries, ledger_issues = _validated_ledger_entries()
    if ledger_issues:
        return {
            "selected": False,
            "eligible": False,
            "root_id": root_id,
            "reason": "bootstrap_root_signed_ledger_invalid",
            "ledger_issues": ledger_issues,
        }
    identity, entry, runtime_error = _validate_root_runtime(root, entries)
    if runtime_error:
        return {
            "selected": False,
            "eligible": False,
            "root_id": root_id,
            "reason": runtime_error,
        }
    assert identity is not None and entry is not None
    receipt = _root_receipt(root, entry)
    consumption = _consumption_report(root, receipt, entries)
    if consumption["issues"]:
        return {
            "selected": False,
            "eligible": False,
            "root_id": root_id,
            "reason": "bootstrap_root_consumption_invalid",
            "consumption": consumption,
        }
    if consumption["consumed"]:
        return {
            "selected": False,
            "eligible": False,
            "root_id": root_id,
            "reason": "bootstrap_root_already_consumed",
            "consumption": consumption,
        }
    candidate_binding = None
    if candidate_path is not None:
        candidate_binding, candidate_error = _candidate_binding(candidate_path, root)
        if candidate_binding is None:
            return {
                "selected": False,
                "eligible": False,
                "root_id": root_id,
                "reason": candidate_error,
            }
    return _expected_selection(root, identity, entry, candidate_binding, consumption)


__all__ = [
    "BOOTSTRAP_ROOTS_PATH",
    "DEFAULT_BOOTSTRAP_ROOT_ID",
    "BootstrapRootConfigurationError",
    "authorize_operator_bootstrap_selection",
    "build_operator_bootstrap_parked_request",
    "build_signed_v5_ledger_bootstrap_root_receipt",
    "load_signed_v5_ledger_bootstrap_roots",
    "select_signed_v5_ledger_bootstrap_root",
    "signed_v5_ledger_bootstrap_root_consumption",
    "validate_completed_operator_bootstrap_authorization",
    "validate_operator_bootstrap_authorized_selection",
    "validate_signed_v5_ledger_bootstrap_selection",
    "validate_signed_v5_ledger_bootstrap_selection_from_entries",
]


def build_signed_v5_ledger_bootstrap_root_receipt(root_id: str) -> dict[str, Any] | None:
    """Expose a validated root receipt without granting an opponent selection."""
    root, error = _configured_root(root_id)
    if root is None:
        return None
    health = ledger_integrity()
    if not health.get("valid"):
        return None
    entries, issues = _validated_ledger_entries()
    if issues:
        return None
    identity, entry, runtime_error = _validate_root_runtime(root, entries)
    if identity is None or entry is None or runtime_error:
        return None
    return _root_receipt(root, entry)
