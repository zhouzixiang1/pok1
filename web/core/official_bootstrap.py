"""Fail-closed, one-time bootstrap authority for the first v5 formal run.

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

import json
from pathlib import Path
import re
from typing import Any

from bot_artifact import canonical_digest, hash_path, published_bot_identity
from bot_namespace import bot_name, parse_bot_version
from official_eligibility import epoch_lifecycle_eligibility
from official_verdict_ledger import ledger_integrity


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_ROOTS_PATH = ROOT / "web" / "core" / "official_bootstrap_roots.json"

ROOTS_SCHEMA_VERSION = 1
ROOTS_KIND = "official-signed-v5-ledger-bootstrap-roots"
ROOT_RECEIPT_SCHEMA_VERSION = 1
ROOT_RECEIPT_KIND = "signed-v5-ledger-bootstrap-root-receipt"
ROOT_SELECTION_KIND = "signed-v5-ledger-bootstrap-selection"
FULL_V5_POLICY_ID = "official-full-v5"

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


def load_signed_v5_ledger_bootstrap_roots(
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Load and strictly validate the repository-owned one-time root manifest."""
    payload = _read_roots(Path(path) if path is not None else BOOTSTRAP_ROOTS_PATH)
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
    root, configuration_error = _configured_root(root_id)
    if root is None:
        return {"valid": False, "reason": configuration_error, "issues": [configuration_error]}
    health = ledger_integrity()
    if not health.get("valid"):
        issues = ["bootstrap_root_signed_ledger_invalid", *(health.get("issues") or [])]
        return {"valid": False, "reason": issues[0], "issues": issues}
    entries, ledger_issues = _validated_ledger_entries()
    if ledger_issues:
        return {
            "valid": False,
            "reason": "bootstrap_root_signed_ledger_invalid",
            "issues": ["bootstrap_root_signed_ledger_invalid", *ledger_issues],
        }
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
    "BootstrapRootConfigurationError",
    "build_signed_v5_ledger_bootstrap_root_receipt",
    "load_signed_v5_ledger_bootstrap_roots",
    "select_signed_v5_ledger_bootstrap_root",
    "signed_v5_ledger_bootstrap_root_consumption",
    "validate_signed_v5_ledger_bootstrap_selection",
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
