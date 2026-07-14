"""Fresh, content-bound bootstrap for ``national_tcp_policy_v1``.

The first active policy bot is v143.  It is not a migration of v142: the old
tag is retained only as immutable version authority.  Preparation materializes
the current system-owned TCP runtime, a checked-in safe policy baseline, and
the two policy-epoch identity documents.  The deterministic Worker may replace
only ``policy.py`` and then must regenerate both identity documents.

Master proposal/ballot governance, Reviewer, Critic, native precommit, and the
official EXE certificate remain mandatory.  This module supplies implementation
bytes and content-chain receipts; it cannot waive any gate.
"""

from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BLUEPRINT_DIR = Path(__file__).resolve().parent / "bootstrap_assets" / "strict_v1"
BLUEPRINT_MANIFEST = BLUEPRINT_DIR / "manifest.json"
EXECUTOR_ID = "system_policy_bootstrap_v1"
SYSTEM_MASTER_RECEIPT_KIND = "system-policy-bootstrap-master-receipt"
SYSTEM_WORKER_RECEIPT_KIND = "system-policy-bootstrap-worker-receipt"
SYSTEM_GATE_RECEIPT_KIND = "system-policy-bootstrap-gate-receipt"
LLM_INVOCATION_EVIDENCE_KIND = "llm-invocation-evidence-v1"
FRESH_BOOTSTRAP_RECEIPT_KIND = "national-tcp-policy-fresh-bootstrap-v1"
POLICY_EPOCH_RESET_RECEIPT_KIND = "national_tcp_policy_epoch_reset"
POLICY_EPOCH_RESET_RECEIPT_FILENAME = "policy_epoch_reset_receipt.json"
POLICY_EPOCH_RESET_CLAIM_FILENAME = "reset_claim.json"
POLICY_EPOCH_RESET_ARCHIVE_RECEIPT_FILENAME = "reset_receipt.json"
_LLM_INVOCATION_LOG_MARKER = "[SYSTEM LLM INVOCATION EVIDENCE]"

_BLUEPRINT_ASSETS = frozenset({"prepared_policy.py", "policy.py"})
_WORKER_CHANGED_FILES = frozenset({
    "policy.py",
    "national_runtime_manifest.json",
    "policy_epoch_receipt.json",
})
_MATERIALIZED_FILES = frozenset({
    "national_bot.py",
    "policy.py",
    "precompute.py",
    "national_runtime_manifest.json",
    "policy_epoch_receipt.json",
})
_ALLOWED_FALSIFIERS = frozenset({
    "fast_policy_baseline",
    "incremental_refinement_protocol",
    "incremental_opponent_model",
    "terminal_response_adaptation",
    "showdown_range_adaptation",
    "donk_line_reachability",
    "delayed_probe_line_reachability",
})

# Numeric namespace boundary only.  It contains no historical source, Git tree,
# match evidence, or executable identity from the archived epoch.
POLICY_VERSION_AUTHORITY = {
    "schema_version": 1,
    "kind": "national-tcp-policy-version-floor",
    "archived_high_water": 142,
    "first_strict_version": 143,
}


class SystemStrictBootstrapError(RuntimeError):
    """A checked-in bootstrap byte or live authority failed closed."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(str(item) for item in errors if str(item))
        super().__init__("; ".join(self.errors) or "system policy bootstrap invalid")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_digest(payload: Any) -> str:
    from bot_artifact import canonical_digest

    return canonical_digest(payload)


def _receipt(payload: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(payload)
    result["receipt_digest"] = _canonical_digest(result)
    return result


def _receipt_errors(receipt: Any, *, kind: str | None = None) -> list[str]:
    if not isinstance(receipt, dict):
        return ["system_bootstrap_receipt_missing_or_not_object"]
    errors: list[str] = []
    if kind is not None and receipt.get("kind") != kind:
        errors.append("system_bootstrap_receipt_kind_mismatch")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    if receipt.get("receipt_digest") != _canonical_digest(unsigned):
        errors.append("system_bootstrap_receipt_digest_mismatch")
    return errors


def _regular_file(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def new_llm_invocation_id() -> str:
    return secrets.token_hex(16)


def llm_result_digest(cost_usd: Any, usage: Any) -> str:
    try:
        return _canonical_digest({"cost_usd": cost_usd, "usage": usage})
    except Exception:
        return _canonical_digest({"cost_usd": repr(cost_usd), "usage": repr(usage)})


def record_llm_invocation_evidence(
    *,
    invocation_id: str,
    purpose: str,
    role: str,
    prompt_digest: str,
    raw_output_digest: str,
    result_digest: str,
    role_result: Any,
    log_file: str | Path,
) -> dict[str, Any]:
    """Seal one completed LLM call to its prompt, result, and append-only log."""

    invocation_id = str(invocation_id)
    purpose = str(purpose)
    role = str(role)
    digests = (prompt_digest, raw_output_digest, result_digest)
    if (
        len(invocation_id) != 32
        or any(char not in "0123456789abcdef" for char in invocation_id)
        or not purpose
        or not role
        or any(
            len(str(value)) != 64
            or any(char not in "0123456789abcdef" for char in str(value))
            for value in digests
        )
    ):
        raise SystemStrictBootstrapError([
            "system_bootstrap_llm_invocation_material_invalid"
        ])
    role_result_digest = _canonical_digest(role_result)
    trailer = {
        "schema_version": 1,
        "kind": LLM_INVOCATION_EVIDENCE_KIND,
        "invocation_id": invocation_id,
        "purpose": purpose,
        "role": role,
        "prompt_digest": prompt_digest,
        "raw_output_digest": raw_output_digest,
        "result_digest": result_digest,
        "role_result_digest": role_result_digest,
        "call_completed": True,
    }
    raw_path = Path(log_file)
    if raw_path.is_symlink():
        raise SystemStrictBootstrapError([
            "system_bootstrap_llm_invocation_log_symlink_forbidden"
        ])
    path = raw_path.resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n{_LLM_INVOCATION_LOG_MARKER}\n"
                + json.dumps(
                    trailer,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink() or not path.is_file():
            raise OSError("LLM role log is not a regular file")
        log_digest = _sha256_file(path)
    except OSError as exc:
        raise SystemStrictBootstrapError([
            f"system_bootstrap_llm_invocation_log_error:{type(exc).__name__}"
        ]) from exc
    return _receipt({**trailer, "io_log_path": str(path), "io_log_digest": log_digest})


def validate_llm_invocation_evidence(
    evidence: Any,
    *,
    expected_purpose: str | None = None,
    expected_role: str | None = None,
    expected_log_name: str | None = None,
) -> list[str]:
    errors = _receipt_errors(evidence, kind=LLM_INVOCATION_EVIDENCE_KIND)
    if not isinstance(evidence, dict):
        return errors
    expected_fields = {
        "schema_version", "kind", "invocation_id", "purpose", "role",
        "prompt_digest", "raw_output_digest", "result_digest",
        "role_result_digest", "call_completed", "io_log_path",
        "io_log_digest", "receipt_digest",
    }
    if set(evidence) != expected_fields:
        errors.append("system_bootstrap_llm_invocation_fields_mismatch")
    invocation_id = str(evidence.get("invocation_id") or "")
    if len(invocation_id) != 32 or any(
        char not in "0123456789abcdef" for char in invocation_id
    ):
        errors.append("system_bootstrap_llm_invocation_id_invalid")
    for field in (
        "prompt_digest", "raw_output_digest", "result_digest",
        "role_result_digest", "io_log_digest",
    ):
        value = str(evidence.get(field) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            errors.append(f"system_bootstrap_llm_invocation_{field}_invalid")
    if evidence.get("schema_version") != 1:
        errors.append("system_bootstrap_llm_invocation_schema_mismatch")
    if evidence.get("call_completed") is not True:
        errors.append("system_bootstrap_llm_invocation_not_completed")
    if expected_purpose is not None and evidence.get("purpose") != expected_purpose:
        errors.append("system_bootstrap_llm_invocation_purpose_mismatch")
    if expected_role is not None and evidence.get("role") != expected_role:
        errors.append("system_bootstrap_llm_invocation_role_mismatch")
    path = Path(str(evidence.get("io_log_path") or ""))
    if expected_log_name is not None and path.name != expected_log_name:
        errors.append("system_bootstrap_llm_invocation_log_name_mismatch")
    try:
        if path.is_symlink() or not path.is_file():
            errors.append("system_bootstrap_llm_invocation_log_not_regular")
        elif _sha256_file(path) != evidence.get("io_log_digest"):
            errors.append("system_bootstrap_llm_invocation_log_digest_mismatch")
        else:
            log_text = path.read_text(encoding="utf-8")
            if _LLM_INVOCATION_LOG_MARKER not in log_text:
                errors.append("system_bootstrap_llm_invocation_log_trailer_missing")
            else:
                trailer_text = log_text.rsplit(_LLM_INVOCATION_LOG_MARKER, 1)[1].strip()
                try:
                    trailer = json.loads(trailer_text)
                except json.JSONDecodeError:
                    trailer = None
                    errors.append("system_bootstrap_llm_invocation_log_trailer_invalid")
                expected_trailer = {
                    key: evidence.get(key)
                    for key in (
                        "schema_version", "kind", "invocation_id", "purpose", "role",
                        "prompt_digest", "raw_output_digest", "result_digest",
                        "role_result_digest", "call_completed",
                    )
                }
                if trailer is not None and trailer != expected_trailer:
                    errors.append("system_bootstrap_llm_invocation_log_trailer_mismatch")
    except (OSError, UnicodeError) as exc:
        errors.append(
            f"system_bootstrap_llm_invocation_log_unreadable:{type(exc).__name__}"
        )
    return list(dict.fromkeys(errors))


def validate_policy_epoch_reset_receipt(receipt: Any) -> list[str]:
    """Validate the one-time local reset proof used by the first strict bot."""

    errors = _receipt_errors(receipt, kind=POLICY_EPOCH_RESET_RECEIPT_KIND)
    if not isinstance(receipt, dict):
        return errors
    from bot_namespace import EVALUATION_EPOCH, FIRST_STRICT_POLICY_VERSION

    expected_keys = {
        "schema_version",
        "kind",
        "epoch",
        "created_at",
        "mode",
        "git_head",
        "archive_root",
        "execution_scope",
        "archived_version_high_water",
        "version_authority_high_water",
        "first_target_version",
        "source_code_inherited",
        "seed_bot",
        "active_namespace",
        "archived_runtime",
        "archived_bot_debris",
        "receipt_digest",
    }
    if set(receipt) != expected_keys:
        errors.append("policy_epoch_reset_keys_mismatch")

    if receipt.get("schema_version") != 2:
        errors.append("policy_epoch_reset_schema_mismatch")
    if receipt.get("epoch") != EVALUATION_EPOCH:
        errors.append("policy_epoch_reset_epoch_mismatch")
    if receipt.get("mode") != "execute":
        errors.append("policy_epoch_reset_not_executed")
    if not isinstance(receipt.get("created_at"), str) or not receipt.get(
        "created_at"
    ):
        errors.append("policy_epoch_reset_created_at_invalid")
    archive_root = str(receipt.get("archive_root") or "")
    archive_path = Path(archive_root)
    expected_prefix = Path(
        "archive/evolution_epochs/national_native_v1/runtime_legacy_untrusted"
    )
    if (
        not archive_root
        or archive_path.is_absolute()
        or ".." in archive_path.parts
        or archive_path.parent != expected_prefix
    ):
        errors.append("policy_epoch_reset_archive_root_invalid")
    execution_scope = receipt.get("execution_scope")
    if not isinstance(execution_scope, dict) or set(execution_scope) != {
        "checkout_role",
        "one_time",
        "prior_reset_evidence_required_empty",
        "claim_digest",
    }:
        errors.append("policy_epoch_reset_execution_scope_invalid")
        execution_scope = execution_scope if isinstance(execution_scope, dict) else {}
    if execution_scope.get("checkout_role") != "autonomous_evolution_runtime":
        errors.append("policy_epoch_reset_checkout_role_invalid")
    if execution_scope.get("one_time") is not True:
        errors.append("policy_epoch_reset_one_time_marker_invalid")
    if execution_scope.get("prior_reset_evidence_required_empty") is not True:
        errors.append("policy_epoch_reset_prior_evidence_contract_invalid")
    claim_digest = str(execution_scope.get("claim_digest") or "")
    if len(claim_digest) != 64 or any(
        char not in "0123456789abcdef" for char in claim_digest
    ):
        errors.append("policy_epoch_reset_claim_digest_invalid")
    if receipt.get("archived_version_high_water") != 142:
        errors.append("policy_epoch_reset_archive_high_water_mismatch")
    if receipt.get("version_authority_high_water") != 142:
        errors.append("policy_epoch_reset_version_authority_mismatch")
    if receipt.get("first_target_version") != FIRST_STRICT_POLICY_VERSION:
        errors.append("policy_epoch_reset_first_target_mismatch")
    if receipt.get("source_code_inherited") is not False:
        errors.append("policy_epoch_reset_inheritance_marker_mismatch")
    if receipt.get("seed_bot") is not None:
        errors.append("policy_epoch_reset_seed_must_be_null")
    expected_namespace = {
        "bot": f"national_v{FIRST_STRICT_POLICY_VERSION}",
        "protocol": "official-national-raw-tcp-v1",
        "policy_abi": "national-tcp-policy-runtime-v1",
    }
    if receipt.get("active_namespace") != expected_namespace:
        errors.append("policy_epoch_reset_namespace_mismatch")
    git_head = str(receipt.get("git_head") or "")
    if len(git_head) not in {40, 64} or any(
        char not in "0123456789abcdef" for char in git_head.lower()
    ):
        errors.append("policy_epoch_reset_git_head_invalid")
    archived_runtime = receipt.get("archived_runtime")
    if not isinstance(archived_runtime, list):
        errors.append("policy_epoch_reset_runtime_rows_invalid")
        archived_runtime = []
    for row in archived_runtime:
        if not isinstance(row, dict) or row.get("trust") != (
            "legacy_untrusted_not_for_prompt_or_rating"
        ):
            errors.append("policy_epoch_reset_runtime_trust_invalid")
            break
        if set(row) != {"label", "from", "to", "trust"}:
            errors.append("policy_epoch_reset_runtime_row_shape_invalid")
            break
        if not str(row.get("to") or "").startswith(archive_root + "/"):
            errors.append("policy_epoch_reset_runtime_destination_invalid")
            break
    archived_bot_debris = receipt.get("archived_bot_debris")
    if not isinstance(archived_bot_debris, list):
        errors.append("policy_epoch_reset_bot_rows_invalid")
        archived_bot_debris = []
    for row in archived_bot_debris:
        if not isinstance(row, dict) or row.get("trust") != "archived_non_executable":
            errors.append("policy_epoch_reset_bot_trust_invalid")
            break
        if set(row) != {"from", "to", "trust", "disposition"}:
            errors.append("policy_epoch_reset_bot_row_shape_invalid")
            break
        if not str(row.get("to") or "").startswith(archive_root + "/"):
            errors.append("policy_epoch_reset_bot_destination_invalid")
            break
        if row.get("disposition") not in {
            "retired_epoch_bot",
            "stale_unpublished_high_version_candidate",
        }:
            errors.append("policy_epoch_reset_bot_disposition_invalid")
            break
    return list(dict.fromkeys(errors))


def validate_policy_epoch_reset_archive(
    receipt: Any,
    *,
    project_root: str | Path = PROJECT_ROOT,
) -> list[str]:
    """Cross-bind the live receipt to the durable no-clobber claim/archive."""

    errors = validate_policy_epoch_reset_receipt(receipt)
    if errors or not isinstance(receipt, dict):
        return errors
    root = Path(project_root)
    archive_root = root / str(receipt.get("archive_root") or "")
    claim_path = archive_root / POLICY_EPOCH_RESET_CLAIM_FILENAME
    archive_receipt_path = (
        archive_root / POLICY_EPOCH_RESET_ARCHIVE_RECEIPT_FILENAME
    )
    try:
        if claim_path.is_symlink() or not claim_path.is_file():
            errors.append("policy_epoch_reset_archive_claim_missing_or_unsafe")
            claim = None
        else:
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        claim = None
        errors.append(
            f"policy_epoch_reset_archive_claim_unreadable:{type(exc).__name__}"
        )
    if isinstance(claim, dict):
        unsigned_claim = {
            key: value for key, value in claim.items() if key != "claim_digest"
        }
        expected_claim = {
            "kind": "national_tcp_policy_epoch_reset_claim",
            "epoch": receipt.get("epoch"),
            "git_head": receipt.get("git_head"),
            "archive_root": receipt.get("archive_root"),
            "first_target_version": receipt.get("first_target_version"),
            "checkout_role": "autonomous_evolution_runtime",
            "one_time": True,
        }
        if claim.get("schema_version") != 1 or any(
            claim.get(key) != value for key, value in expected_claim.items()
        ):
            errors.append("policy_epoch_reset_archive_claim_subject_mismatch")
        if not isinstance(claim.get("created_at"), str) or not claim.get(
            "created_at"
        ):
            errors.append("policy_epoch_reset_archive_claim_created_at_invalid")
        if claim.get("claim_digest") != _canonical_digest(unsigned_claim):
            errors.append("policy_epoch_reset_archive_claim_digest_mismatch")
        if claim.get("claim_digest") != (
            receipt.get("execution_scope") or {}
        ).get("claim_digest"):
            errors.append("policy_epoch_reset_archive_claim_binding_mismatch")
    elif claim is not None:
        errors.append("policy_epoch_reset_archive_claim_not_object")

    try:
        if archive_receipt_path.is_symlink() or not archive_receipt_path.is_file():
            errors.append("policy_epoch_reset_archive_receipt_missing_or_unsafe")
            archived_receipt = None
        else:
            archived_receipt = json.loads(
                archive_receipt_path.read_text(encoding="utf-8")
            )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        archived_receipt = None
        errors.append(
            f"policy_epoch_reset_archive_receipt_unreadable:{type(exc).__name__}"
        )
    if archived_receipt is not None and archived_receipt != receipt:
        errors.append("policy_epoch_reset_archive_receipt_mismatch")

    for row in [
        *(receipt.get("archived_runtime") or []),
        *(receipt.get("archived_bot_debris") or []),
    ]:
        destination = root / str(row.get("to") or "")
        if destination.is_symlink() or not destination.exists():
            errors.append(
                "policy_epoch_reset_archived_destination_missing:"
                f"{row.get('to', '')}"
            )
    return list(dict.fromkeys(errors))


def load_policy_epoch_reset_receipt(
    results_dir: str | Path | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Load the regular-file reset receipt from the active results root."""

    if results_dir is None:
        import evolution_infra

        root = Path(evolution_infra.RESULTS_DIR)
        receipt_project_root = PROJECT_ROOT
    else:
        root = Path(results_dir)
        receipt_project_root = (
            root.parents[2]
            if root.name == "results"
            and root.parent.name == "core"
            and root.parent.parent.name == "web"
            else root
        )
    path = root / POLICY_EPOCH_RESET_RECEIPT_FILENAME
    try:
        if path.is_symlink() or not path.is_file():
            return None, ["policy_epoch_reset_receipt_missing_or_unsafe"]
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [f"policy_epoch_reset_receipt_unreadable:{type(exc).__name__}"]
    errors = validate_policy_epoch_reset_archive(
        value,
        project_root=receipt_project_root,
    )
    return (value if not errors else None), errors


def build_fresh_bootstrap_receipt(
    *,
    active_bots: Iterable[str] = (),
    epoch_reset_receipt_digest: str | None = None,
) -> dict[str, Any]:
    """Bind the empty active pool and numeric namespace floor, never old bytes."""

    from bot_namespace import EVALUATION_EPOCH, FIRST_STRICT_POLICY_VERSION

    active = sorted(map(str, active_bots))
    if active:
        raise SystemStrictBootstrapError(["fresh_bootstrap_requires_empty_active_pool"])
    reset_digest = str(epoch_reset_receipt_digest or "")
    if len(reset_digest) != 64 or any(
        char not in "0123456789abcdef" for char in reset_digest
    ):
        raise SystemStrictBootstrapError([
            "fresh_bootstrap_epoch_reset_receipt_digest_invalid"
        ])
    return _receipt({
        "schema_version": 1,
        "kind": FRESH_BOOTSTRAP_RECEIPT_KIND,
        "mode": "fresh_national_policy_bootstrap",
        "epoch": EVALUATION_EPOCH,
        "source_v": POLICY_VERSION_AUTHORITY["archived_high_water"],
        "next_v": FIRST_STRICT_POLICY_VERSION,
        "source_artifact_inherited": False,
        "active_bots": [],
        "version_authority": deepcopy(POLICY_VERSION_AUTHORITY),
        "epoch_reset_receipt_digest": reset_digest,
    })


def validate_fresh_bootstrap_receipt(
    receipt: Any,
    *,
    active_bots: Iterable[str] | None = None,
    expected_epoch_reset_receipt_digest: str | None = None,
    require_live_epoch_reset: bool = False,
) -> list[str]:
    errors = _receipt_errors(receipt, kind=FRESH_BOOTSTRAP_RECEIPT_KIND)
    if not isinstance(receipt, dict):
        return errors
    reset_digest = str(expected_epoch_reset_receipt_digest or "")
    if require_live_epoch_reset:
        live_receipt, live_errors = load_policy_epoch_reset_receipt()
        errors.extend(live_errors)
        if live_receipt is not None:
            reset_digest = str(live_receipt.get("receipt_digest") or "")
    if not reset_digest:
        reset_digest = str(receipt.get("epoch_reset_receipt_digest") or "")
    try:
        expected = build_fresh_bootstrap_receipt(
            active_bots=active_bots or (),
            epoch_reset_receipt_digest=reset_digest,
        )
    except SystemStrictBootstrapError as exc:
        errors.extend(exc.errors)
        expected = None
    if expected is not None and receipt != expected:
        errors.append("fresh_bootstrap_receipt_subject_mismatch")
    return list(dict.fromkeys(errors))


def load_blueprint_manifest() -> dict[str, Any]:
    try:
        value = json.loads(BLUEPRINT_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemStrictBootstrapError([
            f"system_bootstrap_manifest_unreadable:{type(exc).__name__}"
        ]) from exc
    if not isinstance(value, dict):
        raise SystemStrictBootstrapError(["system_bootstrap_manifest_not_object"])
    return value


def blueprint_identity(manifest: dict[str, Any] | None = None) -> dict[str, str]:
    manifest = manifest or load_blueprint_manifest()
    return {
        "manifest_sha256": _sha256_file(BLUEPRINT_MANIFEST),
        "manifest_contract_digest": _canonical_digest(manifest),
    }


def _runtime_core(policy_bytes: bytes) -> dict[str, bytes]:
    from national_native import NATIVE_BOT_TEMPLATE, NATIVE_PRECOMPUTE_TEMPLATE

    return {
        "national_bot.py": NATIVE_BOT_TEMPLATE.encode("utf-8"),
        "policy.py": policy_bytes,
        "precompute.py": NATIVE_PRECOMPUTE_TEMPLATE.encode("utf-8"),
    }


def _materialized_payload(policy_bytes: bytes, *, version: int = 143) -> dict[str, bytes]:
    from bot_namespace import (
        NATIONAL_RUNTIME_MANIFEST,
        POLICY_EPOCH_RECEIPT,
        build_policy_epoch_receipt,
        build_runtime_manifest,
    )

    core = _runtime_core(policy_bytes)
    with tempfile.TemporaryDirectory(prefix="pok-policy-identity-") as temporary:
        root = Path(temporary)
        for relative, payload in core.items():
            (root / relative).write_bytes(payload)
        runtime_manifest = build_runtime_manifest(root)
        (root / NATIONAL_RUNTIME_MANIFEST).write_bytes(_json_bytes(runtime_manifest))
        epoch_receipt = build_policy_epoch_receipt(root, version, parent_versions=())
    return {
        **core,
        NATIONAL_RUNTIME_MANIFEST: _json_bytes(runtime_manifest),
        POLICY_EPOCH_RECEIPT: _json_bytes(epoch_receipt),
    }


def _write_payload(root: Path, payload: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=False)
    for relative, content in sorted(payload.items()):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def _payload_artifact_hash(payload: dict[str, bytes]) -> str:
    from bot_artifact import hash_path

    with tempfile.TemporaryDirectory(prefix="pok-policy-artifact-") as temporary:
        root = Path(temporary) / "artifact"
        _write_payload(root, payload)
        return hash_path(root)


def materialize_fresh_candidate(
    candidate_dir: str | Path,
    *,
    version: int = 143,
    final_policy: bool = False,
) -> dict[str, Any]:
    """Atomically create a fresh five-file policy artifact."""

    from bot_namespace import FIRST_STRICT_POLICY_VERSION

    if int(version) != FIRST_STRICT_POLICY_VERSION:
        raise SystemStrictBootstrapError(["fresh_bootstrap_target_version_mismatch"])
    candidate = Path(candidate_dir)
    if candidate.exists() or candidate.is_symlink():
        raise SystemStrictBootstrapError(["fresh_bootstrap_target_must_not_exist"])
    policy_name = "policy.py" if final_policy else "prepared_policy.py"
    policy_bytes = (BLUEPRINT_DIR / policy_name).read_bytes()
    payload = _materialized_payload(policy_bytes, version=version)
    staging = candidate.with_name(f".{candidate.name}.fresh-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        _write_payload(staging, payload)
        os.replace(staging, candidate)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "artifact_hash": _payload_artifact_hash(payload),
        "files": {relative: _sha256_bytes(content) for relative, content in payload.items()},
        "source_artifact_inherited": False,
        "policy": policy_name,
    }


def refresh_policy_identity(
    bot_dir: str | Path,
    *,
    version: int,
    parent_versions: Iterable[int] = (),
) -> dict[str, Any]:
    """Regenerate system-owned manifests after an authorized policy edit."""

    from bot_namespace import refresh_policy_identity_documents

    try:
        return refresh_policy_identity_documents(
            bot_dir,
            int(version),
            parent_versions=tuple(map(int, parent_versions)),
        )
    except Exception as exc:
        raise SystemStrictBootstrapError([
            f"system_bootstrap_identity_refresh_failed:{type(exc).__name__}:"
            f"{str(exc)[:300]}"
        ]) from exc


def validate_blueprint_package(
    manifest: dict[str, Any] | None = None,
    *,
    verify_source: bool = True,
) -> list[str]:
    """Validate every bootstrap byte without opening an archived bot tree."""

    del verify_source  # no source artifact exists in this epoch
    try:
        manifest = manifest or load_blueprint_manifest()
    except SystemStrictBootstrapError as exc:
        return list(exc.errors)
    errors: list[str] = []
    from bot_namespace import EVALUATION_EPOCH, FIRST_STRICT_POLICY_VERSION
    from runtime_architecture_policy import OFFICIAL_FULL_POLICY_ID, OFFICIAL_ORACLE_DOC_DIGESTS

    expected_scalars = {
        "schema_version": 2,
        "bundle_id": "national-tcp-policy-bootstrap-v1",
        "mode": "fresh_national_policy_bootstrap",
        "executor": EXECUTOR_ID,
        "epoch": EVALUATION_EPOCH,
        "target_version": FIRST_STRICT_POLICY_VERSION,
        "official_policy_id": OFFICIAL_FULL_POLICY_ID,
    }
    for field, value in expected_scalars.items():
        if manifest.get(field) != value:
            errors.append(f"system_bootstrap_manifest_{field}_mismatch")
    if manifest.get("version_authority") != POLICY_VERSION_AUTHORITY:
        errors.append("system_bootstrap_version_authority_mismatch")
    if manifest.get("official_oracles") != OFFICIAL_ORACLE_DOC_DIGESTS:
        errors.append("system_bootstrap_official_oracle_set_mismatch")
    if set(manifest.get("allowed_falsifiers") or []) != _ALLOWED_FALSIFIERS:
        errors.append("system_bootstrap_allowed_falsifiers_mismatch")

    actual_nodes = {
        path.relative_to(BLUEPRINT_DIR).as_posix(): path
        for path in BLUEPRINT_DIR.rglob("*")
        if "__pycache__" not in path.parts
    }
    if set(actual_nodes) != {"manifest.json", *_BLUEPRINT_ASSETS}:
        errors.append("system_bootstrap_package_entries_mismatch")
    for relative, path in actual_nodes.items():
        if not _regular_file(path):
            errors.append(f"system_bootstrap_package_node_not_regular:{relative}")
    declared_files = manifest.get("files")
    if not isinstance(declared_files, dict) or set(declared_files) != _BLUEPRINT_ASSETS:
        errors.append("system_bootstrap_declared_files_mismatch")
        declared_files = declared_files if isinstance(declared_files, dict) else {}
    for relative in sorted(_BLUEPRINT_ASSETS):
        path = BLUEPRINT_DIR / relative
        if _regular_file(path) and declared_files.get(relative) != _sha256_file(path):
            errors.append(f"system_bootstrap_asset_hash_mismatch:{relative}")

    for relative, expected_hash in sorted(OFFICIAL_ORACLE_DOC_DIGESTS.items()):
        try:
            actual_hash = _sha256_file(PROJECT_ROOT / relative)
        except OSError as exc:
            errors.append(f"system_bootstrap_oracle_unreadable:{relative}:{type(exc).__name__}")
            continue
        if actual_hash != expected_hash:
            errors.append(f"system_bootstrap_oracle_hash_mismatch:{relative}")

    prepared = _materialized_payload((BLUEPRINT_DIR / "prepared_policy.py").read_bytes())
    output = _materialized_payload((BLUEPRINT_DIR / "policy.py").read_bytes())
    expected_runtime = {
        "national_bot_sha256": _sha256_bytes(prepared["national_bot.py"]),
        "precompute_sha256": _sha256_bytes(prepared["precompute.py"]),
        "decision_runtime_version": 9,
        "stream_decoder_version": 2,
    }
    if manifest.get("system_runtime") != expected_runtime:
        errors.append("system_bootstrap_system_runtime_mismatch")
    if manifest.get("prepared_artifact_hash") != _payload_artifact_hash(prepared):
        errors.append("system_bootstrap_prepared_artifact_hash_mismatch")
    if manifest.get("output_artifact_hash") != _payload_artifact_hash(output):
        errors.append("system_bootstrap_output_artifact_hash_mismatch")
    errors.extend(validate_fresh_bootstrap_receipt(build_fresh_bootstrap_receipt(
        epoch_reset_receipt_digest="0" * 64,
    )))
    return list(dict.fromkeys(errors))


def is_declared_native_bootstrap(checkpoint: Any) -> bool:
    if not isinstance(checkpoint, dict):
        return False
    audit = checkpoint.get("audit_context") or {}
    receipt = audit.get("protocol_bootstrap") or {}
    selection = audit.get("selection") or {}
    return bool(
        checkpoint.get("source_v") == 142
        and checkpoint.get("next_v") == 143
        and receipt.get("mode") == "fresh_national_policy_bootstrap"
        and receipt.get("source_artifact_inherited") is False
        and selection.get("bootstrap_without_strength_evidence") is True
        and selection.get("strategy") == "fresh_policy_bootstrap"
    )


def system_recovery_eligible(checkpoint: Any, next_tool: str) -> bool:
    expected_stage = {
        "run_direction_audit": "prepared",
        "run_master": "direction_audited",
    }.get(str(next_tool))
    if expected_stage is None or not isinstance(checkpoint, dict):
        return False
    if checkpoint.get("stage") != expected_stage:
        return False
    try:
        return not validate_bootstrap_checkpoint(
            checkpoint,
            candidate_dir=PROJECT_ROOT / "bots" / f"national_v{checkpoint.get('next_v')}",
            require_direction_audit=next_tool == "run_master",
        )
    except Exception:
        return False


async def abandon_rejected_blueprint(
    checkpoint: Any,
    *,
    reason: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    payload = deepcopy(result)
    payload["action"] = "abandon_generation"
    if not is_declared_native_bootstrap(checkpoint):
        payload.update({
            "abandoned": False,
            "abandon_error": "system_bootstrap_abandon_checkpoint_not_declared",
        })
        return payload
    try:
        from tool_bot_management import _do_abandon_generation

        abandon_result = await _do_abandon_generation(
            reason=str(reason),
            _bypass_rate_limit=True,
            expected_workflow_run_id=str(
                checkpoint.get("workflow_run_id") or checkpoint.get("run_id") or ""
            ),
            expected_next_v=int(checkpoint.get("next_v")),
            expected_source_v=int(checkpoint.get("source_v")),
            expected_checkpoint_revision=int(checkpoint.get("checkpoint_revision") or 0),
        )
    except Exception as exc:
        abandon_result = {
            "abandoned": False,
            "reason": "system_bootstrap_abandon_exception",
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
    payload["abandon_result"] = abandon_result
    payload["abandoned"] = abandon_result.get("abandoned") is True
    if payload["abandoned"]:
        try:
            from orchestrator_session import _clear_orchestrator_session

            _clear_orchestrator_session()
        except Exception:
            pass
        payload["checkpoint_stage"] = "abandoned"
    else:
        payload["abandon_error"] = str(
            abandon_result.get("error") or abandon_result.get("reason") or "cleanup_failed"
        )
    return payload


def validate_bootstrap_checkpoint(
    checkpoint: Any,
    *,
    architecture_policy: dict[str, Any] | None = None,
    candidate_dir: str | Path | None = None,
    active_bots: Iterable[str] | None = None,
    require_direction_audit: bool = False,
    require_live_epoch_reset: bool = True,
) -> list[str]:
    errors = validate_blueprint_package()
    if not isinstance(checkpoint, dict):
        return [*errors, "system_bootstrap_checkpoint_missing"]
    if not is_declared_native_bootstrap(checkpoint):
        errors.append("system_bootstrap_checkpoint_not_native_bootstrap")
    audit = checkpoint.get("audit_context") or {}
    receipt = audit.get("protocol_bootstrap")
    errors.extend(validate_fresh_bootstrap_receipt(
        receipt,
        active_bots=active_bots,
        require_live_epoch_reset=require_live_epoch_reset,
    ))
    prepared = audit.get("prepared_artifact_contract")
    if not isinstance(prepared, dict):
        errors.append("system_bootstrap_prepared_contract_missing")
    else:
        manifest = load_blueprint_manifest()
        if prepared.get("prepared_artifact_hash") != manifest.get("prepared_artifact_hash"):
            errors.append("system_bootstrap_prepared_contract_hash_mismatch")
        try:
            from prepared_baseline_contract import validate_prepared_artifact_contract

            errors.extend(validate_prepared_artifact_contract(
                prepared,
                prepared_dir=candidate_dir,
                source_v=142,
                next_v=143,
                verify_live_content=candidate_dir is not None,
            ))
        except Exception as exc:
            errors.append(f"system_bootstrap_prepared_contract_error:{type(exc).__name__}")
    prepare_receipt = audit.get("protocol_bootstrap_prepare") or {}
    if prepare_receipt.get("receipt_digest") != (receipt or {}).get("receipt_digest"):
        errors.append("system_bootstrap_prepare_receipt_binding_mismatch")
    if prepare_receipt.get("source_artifact_inherited") is not False:
        errors.append("system_bootstrap_prepare_inheritance_marker_mismatch")
    if candidate_dir is not None:
        from bot_namespace import (
            NATIONAL_RUNTIME_MANIFEST,
            POLICY_EPOCH_RECEIPT,
            epoch_receipt_errors,
            runtime_manifest_errors,
        )
        root = Path(candidate_dir)
        try:
            runtime_manifest = json.loads((root / NATIONAL_RUNTIME_MANIFEST).read_text())
            epoch_receipt = json.loads((root / POLICY_EPOCH_RECEIPT).read_text())
            errors.extend(runtime_manifest_errors(root, runtime_manifest))
            errors.extend(epoch_receipt_errors(root, 143, runtime_manifest, epoch_receipt))
        except Exception as exc:
            errors.append(f"system_bootstrap_identity_unreadable:{type(exc).__name__}")
    if architecture_policy is not None:
        from output_schema import NATIONAL_POLICY_FOCUS_ID
        from runtime_architecture_policy import OFFICIAL_ORACLE_DOC_DIGESTS

        focus = architecture_policy.get("selected_focus") or {}
        if focus.get("focus_id") != NATIONAL_POLICY_FOCUS_ID:
            errors.append("system_bootstrap_architecture_focus_mismatch")
        if architecture_policy.get("official_oracle_digests") != OFFICIAL_ORACLE_DOC_DIGESTS:
            errors.append("system_bootstrap_architecture_oracle_digest_mismatch")
    if require_direction_audit:
        direction = checkpoint.get("direction_audit")
        if not isinstance(direction, dict) or direction.get("approved") is not True:
            errors.append("system_bootstrap_direction_audit_missing_or_rejected")
    return list(dict.fromkeys(errors))


def _python_source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if not _regular_file(path) or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _symbol_graph(root: Path) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        if not _regular_file(path) or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_bytes(), filename=relative)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            calls: set[str] = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Name):
                        calls.add(child.func.id)
                    elif isinstance(child.func, ast.Attribute):
                        calls.add(child.func.attr)
            graph[f"{relative}:{node.name}"] = calls
    return graph


def _prepared_graph() -> tuple[dict[str, set[str]], str, list[str]]:
    try:
        with tempfile.TemporaryDirectory(prefix="pok-policy-prepared-") as temporary:
            root = Path(temporary) / "national_v143"
            _write_payload(
                root,
                _materialized_payload((BLUEPRINT_DIR / "prepared_policy.py").read_bytes()),
            )
            return _symbol_graph(root), _python_source_digest(root), []
    except Exception as exc:
        return {}, "", [f"system_bootstrap_prepared_graph_error:{type(exc).__name__}"]


def _chain_errors(graph: dict[str, set[str]], chain: Any) -> list[str]:
    if not isinstance(chain, list) or not chain:
        return ["system_bootstrap_selected_chain_missing"]
    symbols = list(map(str, chain))
    if any(symbol not in graph for symbol in symbols):
        return ["system_bootstrap_selected_chain_symbol_missing"]
    leaf_map: dict[str, list[str]] = {}
    for symbol in graph:
        leaf_map.setdefault(symbol.rsplit(":", 1)[-1].rsplit(".", 1)[-1], []).append(symbol)
    for caller, callee in zip(symbols, symbols[1:]):
        leaf = callee.rsplit(":", 1)[-1].rsplit(".", 1)[-1]
        if leaf not in graph.get(caller, set()) or leaf_map.get(leaf) != [callee]:
            return ["system_bootstrap_selected_chain_unreachable"]
    return []


def validate_selected_proposal_for_blueprint(
    plan: Any,
    *,
    manifest: dict[str, Any] | None = None,
    prepared_baseline_dir: str | Path | None = None,
) -> list[str]:
    manifest = manifest or load_blueprint_manifest()
    if not isinstance(plan, dict):
        return ["system_bootstrap_master_plan_not_object"]
    errors: list[str] = []
    selected_id = str(plan.get("selected_proposal_id") or "")
    binding = plan.get("proposal_binding")
    ensemble = plan.get("proposal_ensemble")
    if not selected_id:
        errors.append("system_bootstrap_selected_proposal_id_missing")
    if not isinstance(binding, dict):
        return [*errors, "system_bootstrap_proposal_binding_missing"]
    if binding.get("selected_proposal_id") != selected_id:
        errors.append("system_bootstrap_selected_proposal_binding_mismatch")
    if not isinstance(ensemble, dict):
        errors.append("system_bootstrap_proposal_ensemble_missing")
        ensemble = {}
    proposals = ensemble.get("ordered_proposals")
    proposal_ids = [
        str(item.get("proposal_id") or "") for item in (proposals or [])
        if isinstance(item, dict)
    ]
    if (
        ensemble.get("valid") is not True
        or ensemble.get("proposal_count") != 3
        or len(proposal_ids) != 3
        or len(set(proposal_ids)) != 3
        or selected_id not in proposal_ids
    ):
        errors.append("system_bootstrap_three_proposal_ensemble_invalid")
    reviews = ensemble.get("critic_reviews")
    if ensemble.get("valid_critic_count") != 2 or not isinstance(reviews, list) or len(reviews) != 2:
        errors.append("system_bootstrap_two_critic_ballots_missing")
    packet_digest = _sha256_bytes(json.dumps(
        ensemble, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode())
    if binding.get("proposal_packet_digest") != packet_digest:
        errors.append("system_bootstrap_proposal_packet_digest_mismatch")
    selected = next(
        (item for item in (proposals or []) if isinstance(item, dict) and item.get("proposal_id") == selected_id),
        None,
    )
    expected_selected = (
        {key: value for key, value in selected.items() if key != "direction"}
        if isinstance(selected, dict) else None
    )
    if binding.get("selected_proposal") != expected_selected:
        errors.append("system_bootstrap_selected_proposal_packet_mismatch")
    files = set(map(str, binding.get("target_files") or []))
    if files != {"policy.py"}:
        errors.append("system_bootstrap_proposal_target_must_be_policy_only")
    falsifier = binding.get("falsifier") or {}
    test_name = str(falsifier.get("test_name") or "")
    if test_name not in set(manifest.get("allowed_falsifiers") or []):
        errors.append("system_bootstrap_selected_falsifier_not_blueprint_capability")
    contract = {
        "schema_version": 1,
        "proposal_id": selected_id,
        "structural_change": str(binding.get("structural_change") or ""),
        "expected_diff": str(binding.get("expected_diff") or ""),
        "reachable_chain": list(binding.get("reachable_chain") or []),
        "falsifier": dict(falsifier),
        "why_not_threshold_tuning": str(binding.get("why_not_threshold_tuning") or ""),
    }
    expected_contract = _sha256_bytes(json.dumps(
        contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode())
    if binding.get("contract_digest") != expected_contract:
        errors.append("system_bootstrap_proposal_contract_digest_mismatch")

    if prepared_baseline_dir is None:
        graph, source_digest, graph_errors = _prepared_graph()
    else:
        root = Path(prepared_baseline_dir)
        graph, source_digest, graph_errors = _symbol_graph(root), _python_source_digest(root), []
    errors.extend(graph_errors)
    if binding.get("source_code_digest") != source_digest:
        errors.append("system_bootstrap_prepared_source_code_digest_mismatch")
    source_symbols = list(map(str, binding.get("source_symbols") or []))
    if not source_symbols or any(symbol not in graph for symbol in source_symbols):
        errors.append("system_bootstrap_selected_source_symbol_missing")
    errors.extend(_chain_errors(graph, binding.get("reachable_chain")))
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        return [*errors, "system_bootstrap_master_requires_one_bound_task"]
    task = tasks[0]
    writable = {
        str(value)
        for key in ("target_files", "files_allowed")
        for value in (task.get(key) or [])
    }
    if writable != {"policy.py"}:
        errors.append("system_bootstrap_master_writable_file_set_mismatch")
    prompt = str(task.get("worker_prompt") or "")
    from plan_compiler import SELECTED_PROPOSAL_BEGIN, SELECTED_PROPOSAL_END
    if any(term not in prompt for term in (
        SELECTED_PROPOSAL_BEGIN, SELECTED_PROPOSAL_END,
        f"proposal_id={selected_id}", f"contract_digest={expected_contract}",
    )):
        errors.append("system_bootstrap_worker_selected_proposal_block_missing")
    return list(dict.fromkeys(errors))


def _master_subject(
    checkpoint: dict[str, Any],
    plan: dict[str, Any],
    *,
    architecture_policy: dict[str, Any],
    candidate_dir: str | Path,
) -> dict[str, Any]:
    manifest = load_blueprint_manifest()
    audit = checkpoint.get("audit_context") or {}
    prepared = audit.get("prepared_artifact_contract") or {}
    bootstrap = audit.get("protocol_bootstrap") or {}
    binding = plan.get("proposal_binding") or {}
    from strict_authority_workflow import (
        MASTER_SLOTS,
        authority_summary,
        expected_master_contexts,
        expected_master_role_results,
        validate_master_final_projection,
    )
    projection, projection_errors = validate_master_final_projection(
        checkpoint, plan, candidate_dir=candidate_dir, project_root=PROJECT_ROOT
    )
    if projection_errors:
        raise SystemStrictBootstrapError(projection_errors)
    llm_authority = authority_summary(
        checkpoint,
        required_slots=MASTER_SLOTS,
        expected_role_results=expected_master_role_results(plan),
        expected_context_bindings=expected_master_contexts(plan),
        require_no_other_accepted=True,
    )
    return {
        "schema_version": 1,
        "kind": SYSTEM_MASTER_RECEIPT_KIND,
        "executor": EXECUTOR_ID,
        "source_v": 142,
        "next_v": 143,
        **blueprint_identity(manifest),
        "bundle_id": manifest.get("bundle_id"),
        "bootstrap_receipt_digest": bootstrap.get("receipt_digest"),
        "version_authority": deepcopy(bootstrap.get("version_authority") or {}),
        "prepared_artifact_hash": prepared.get("prepared_artifact_hash"),
        "prepared_artifact_contract_digest": prepared.get("contract_digest"),
        "expected_output_artifact_hash": manifest.get("output_artifact_hash"),
        "architecture_policy_digest": architecture_policy.get("policy_digest"),
        "runtime_contract_ledger_digest": (
            (plan.get("runtime_contract_ledger") or {}).get("ledger_digest")
        ),
        "plan_digest": _canonical_digest(plan),
        "selected_proposal_id": plan.get("selected_proposal_id"),
        "proposal_contract_digest": binding.get("contract_digest"),
        "selected_falsifier_test": (binding.get("falsifier") or {}).get("test_name"),
        "official_oracle_digests": deepcopy(manifest.get("official_oracles") or {}),
        "llm_authority": llm_authority,
        "master_final_projection": projection,
    }


def build_master_receipt(
    checkpoint: dict[str, Any],
    plan: dict[str, Any],
    *,
    architecture_policy: dict[str, Any],
    candidate_dir: str | Path,
) -> dict[str, Any]:
    errors = validate_bootstrap_checkpoint(
        checkpoint,
        architecture_policy=architecture_policy,
        candidate_dir=candidate_dir,
        require_direction_audit=True,
    )
    errors.extend(validate_selected_proposal_for_blueprint(
        plan, prepared_baseline_dir=candidate_dir
    ))
    if errors:
        raise SystemStrictBootstrapError(errors)
    return _receipt(_master_subject(
        checkpoint, plan, architecture_policy=architecture_policy,
        candidate_dir=candidate_dir,
    ))


def validate_master_receipt(
    checkpoint: dict[str, Any],
    *,
    candidate_dir: str | Path,
    require_prepared_content: bool = True,
) -> list[str]:
    audit = checkpoint.get("audit_context") or {}
    receipt = audit.get("system_strict_bootstrap")
    errors = _receipt_errors(receipt, kind=SYSTEM_MASTER_RECEIPT_KIND)
    if errors or not isinstance(receipt, dict):
        return errors
    plan = checkpoint.get("master_plan") or {}
    errors.extend(validate_selected_proposal_for_blueprint(
        plan,
        prepared_baseline_dir=candidate_dir if require_prepared_content else None,
    ))
    try:
        expected = _receipt(_master_subject(
            checkpoint,
            plan,
            architecture_policy=plan.get("architecture_policy") or {},
            candidate_dir=candidate_dir,
        ))
    except Exception as exc:
        errors.append(f"system_bootstrap_master_subject_error:{type(exc).__name__}:{str(exc)[:300]}")
    else:
        if receipt != expected:
            errors.append("system_bootstrap_master_receipt_subject_mismatch")
    if require_prepared_content:
        errors.extend(validate_bootstrap_checkpoint(
            checkpoint,
            architecture_policy=plan.get("architecture_policy"),
            candidate_dir=candidate_dir,
            require_direction_audit=True,
        ))
    else:
        errors.extend(validate_blueprint_package())
    return list(dict.fromkeys(errors))


def system_worker_backend_contract(master_receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "executor": EXECUTOR_ID,
        "manifest_sha256": str(master_receipt.get("manifest_sha256") or ""),
        "manifest_contract_digest": str(master_receipt.get("manifest_contract_digest") or ""),
        "controller_sha256": _sha256_file(Path(__file__)),
    }


def validate_system_worker_envelope(
    checkpoint: dict[str, Any],
    envelope: dict[str, Any],
    *,
    candidate_dir: str | Path,
) -> list[str]:
    errors = validate_master_receipt(
        checkpoint, candidate_dir=candidate_dir, require_prepared_content=False
    )
    audit = checkpoint.get("audit_context") or {}
    master = audit.get("system_strict_bootstrap") or {}
    policy = envelope.get("execution_policy") or {}
    if policy.get("executor") != EXECUTOR_ID:
        errors.append("system_bootstrap_worker_executor_policy_mismatch")
    if envelope.get("prepared_artifact_hash") != master.get("prepared_artifact_hash"):
        errors.append("system_bootstrap_worker_prepared_hash_mismatch")
    if envelope.get("projection_plan") != checkpoint.get("master_plan"):
        errors.append("system_bootstrap_worker_projection_plan_mismatch")
    if envelope.get("backend_contract") != system_worker_backend_contract(master):
        errors.append("system_bootstrap_worker_backend_contract_mismatch")
    from bot_artifact import hash_path
    try:
        if hash_path(Path(candidate_dir)) != master.get("prepared_artifact_hash"):
            errors.append("system_bootstrap_worker_workspace_hash_mismatch")
    except Exception as exc:
        errors.append(f"system_bootstrap_worker_workspace_error:{type(exc).__name__}")
    return list(dict.fromkeys(errors))


def _file_map(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _sha256_file(path)
        for path in root.rglob("*") if _regular_file(path)
    }


def apply_blueprint(
    workspace: str | Path,
    *,
    checkpoint: dict[str, Any],
    envelope: dict[str, Any],
) -> tuple[dict[tuple[int, str], str | bytes], list[str], dict[str, Any]]:
    """Replace only policy bytes, then re-sign the two system identities."""

    workspace = Path(workspace)
    errors = validate_system_worker_envelope(
        checkpoint, envelope, candidate_dir=workspace
    )
    if errors:
        raise SystemStrictBootstrapError(errors)
    manifest = load_blueprint_manifest()
    from bot_artifact import hash_path

    before_hash = hash_path(workspace)
    if before_hash != manifest.get("prepared_artifact_hash"):
        raise SystemStrictBootstrapError(["system_bootstrap_workspace_prepared_hash_mismatch"])
    before_files = _file_map(workspace)
    snapshots: dict[tuple[int, str], str | bytes] = {}
    for relative in sorted(_WORKER_CHANGED_FILES):
        path = workspace / relative
        snapshots[(0, relative)] = path.read_bytes()
    policy_path = workspace / "policy.py"
    temporary = policy_path.with_name(f".{policy_path.name}.{os.getpid()}.tmp")
    temporary.write_bytes((BLUEPRINT_DIR / "policy.py").read_bytes())
    os.replace(temporary, policy_path)
    refresh_policy_identity(workspace, version=143)

    after_files = _file_map(workspace)
    changed = {
        relative for relative in set(before_files) | set(after_files)
        if before_files.get(relative) != after_files.get(relative)
    }
    if changed != _WORKER_CHANGED_FILES:
        raise SystemStrictBootstrapError([
            "system_bootstrap_changed_file_set_mismatch:"
            f"expected={sorted(_WORKER_CHANGED_FILES)}:actual={sorted(changed)}"
        ])
    output_hash = hash_path(workspace)
    if output_hash != manifest.get("output_artifact_hash"):
        raise SystemStrictBootstrapError([
            "system_bootstrap_output_artifact_hash_mismatch:"
            f"expected={manifest.get('output_artifact_hash')}:actual={output_hash}"
        ])
    from national_capability_contract import evaluate_national_capabilities
    capabilities = evaluate_national_capabilities(workspace)
    checks = capabilities.get("checks_by_id") or {}
    if capabilities.get("ok") is not True:
        raise SystemStrictBootstrapError([
            "system_bootstrap_output_capability_probe_not_ok",
            *list(capabilities.get("required_failures") or [])[:10],
        ])
    selected_test = (
        ((checkpoint.get("master_plan") or {}).get("proposal_binding") or {})
        .get("falsifier", {})
        .get("test_name")
    )
    if not isinstance(checks.get(selected_test), dict) or checks[selected_test].get("passed") is not True:
        raise SystemStrictBootstrapError([
            f"system_bootstrap_selected_capability_not_proven:{selected_test}"
        ])
    worker_receipt = _receipt({
        "schema_version": 1,
        "kind": SYSTEM_WORKER_RECEIPT_KIND,
        "executor": EXECUTOR_ID,
        "source_v": 142,
        "next_v": 143,
        "master_receipt_digest": (
            (checkpoint.get("audit_context") or {})
            .get("system_strict_bootstrap", {})
            .get("receipt_digest")
        ),
        "envelope_digest": envelope.get("envelope_digest"),
        "prepared_artifact_hash": before_hash,
        "output_artifact_hash": output_hash,
        "changed_files": sorted(changed),
        "selected_capability": selected_test,
        "selected_capability_evidence_digest": _canonical_digest(checks[selected_test]),
        **blueprint_identity(manifest),
    })
    return (
        snapshots,
        [
            "System verifier: prove policy.py is the only candidate-owned edit, "
            "the two system identities were regenerated, and runtime bytes stayed exact."
        ],
        worker_receipt,
    )


def bind_worker_effect_receipt(
    receipt: dict[str, Any],
    *, effect_id: str,
    lease_epoch: int,
) -> dict[str, Any]:
    errors = _receipt_errors(receipt, kind=SYSTEM_WORKER_RECEIPT_KIND)
    if errors:
        raise SystemStrictBootstrapError(errors)
    payload = {key: deepcopy(value) for key, value in receipt.items() if key != "receipt_digest"}
    payload["effect_id"] = str(effect_id)
    payload["lease_epoch"] = int(lease_epoch)
    return _receipt(payload)


def _gate_without_receipt(gate: Any) -> dict[str, Any]:
    return {
        key: deepcopy(value) for key, value in (gate if isinstance(gate, dict) else {}).items()
        if key != "system_verifier_receipt"
    }


def _gate_role_result(gate: Any) -> dict[str, Any]:
    if not isinstance(gate, dict):
        return {}
    if isinstance(gate.get("llm_role_result"), dict):
        return deepcopy(gate["llm_role_result"])
    return {
        key: deepcopy(value) for key, value in gate.items()
        if key not in {
            "system_verifier_receipt", "llm_execution_evidence",
            "llm_authority_receipt", "llm_role_result",
        }
    }


def validate_embedded_system_gate(gate: Any, *, gate_name: str) -> list[str]:
    if not isinstance(gate, dict):
        return ["system_gate_not_object"]
    errors: list[str] = []
    if gate.get("approved") is not True or gate.get("passed") is not True:
        errors.append("system_gate_not_approved")
    if gate.get("llm_invoked") is not True or gate.get("schema_valid") is not True:
        errors.append("system_gate_llm_execution_marker_invalid")
    marker = "reviewer_llm_executed" if gate_name == "review" else "critic_llm_executed"
    if gate.get(marker) is not True:
        errors.append(f"system_gate_{marker}_missing")
    authority = gate.get("llm_authority_receipt")
    if not isinstance(authority, dict) or authority.get("slot") != gate_name:
        errors.append("system_gate_llm_authority_receipt_invalid")
    role = "LEAD CODE REVIEWER" if gate_name == "review" else "STRATEGY CRITIC"
    evidence = gate.get("llm_execution_evidence")
    errors.extend(validate_llm_invocation_evidence(
        evidence,
        expected_purpose=f"system_strict_bootstrap_gate:{gate_name}",
        expected_role=role,
        expected_log_name="reviewer_io.txt" if gate_name == "review" else "critic_io.txt",
    ))
    if isinstance(evidence, dict) and evidence.get("role_result_digest") != _canonical_digest(_gate_role_result(gate)):
        errors.append("system_gate_llm_invocation_role_result_mismatch")
    return list(dict.fromkeys(errors))


def _system_gate_subject(
    checkpoint: dict[str, Any],
    *,
    gate_name: str,
    candidate_dir: str | Path,
    llm_gate: dict[str, Any],
    require_exact_authority: bool,
) -> tuple[dict[str, Any], list[str]]:
    errors = validate_embedded_system_gate(llm_gate, gate_name=gate_name)
    candidate = Path(candidate_dir)
    errors.extend(validate_master_receipt(
        checkpoint, candidate_dir=candidate, require_prepared_content=False
    ))
    audit = checkpoint.get("audit_context") or {}
    master = audit.get("system_strict_bootstrap") or {}
    worker = audit.get("system_strict_bootstrap_worker")
    durable = audit.get("durable_worker_output") or {}
    errors.extend(_receipt_errors(worker, kind=SYSTEM_WORKER_RECEIPT_KIND))
    from bot_artifact import hash_path
    candidate_hash = hash_path(candidate)
    expected_output = load_blueprint_manifest().get("output_artifact_hash")
    if candidate_hash != expected_output:
        errors.append("system_bootstrap_gate_candidate_hash_mismatch")
    if isinstance(worker, dict):
        for field, value in {
            "master_receipt_digest": master.get("receipt_digest"),
            "output_artifact_hash": expected_output,
            "envelope_digest": durable.get("envelope_digest"),
            "effect_id": durable.get("effect_id"),
            "lease_epoch": durable.get("lease_epoch"),
        }.items():
            if worker.get(field) != value:
                errors.append(f"system_bootstrap_worker_receipt_{field}_mismatch")
        if set(worker.get("changed_files") or []) != _WORKER_CHANGED_FILES:
            errors.append("system_bootstrap_worker_receipt_changed_files_mismatch")
    gates = checkpoint.get("gate_results") or {}
    quality = gates.get("quality") or {}
    if quality.get("all_passed") is not True or quality.get("critical_scenarios_passed") is not True:
        errors.append("system_bootstrap_quality_not_passed")
    if quality.get("code_fingerprint") != expected_output:
        errors.append("system_bootstrap_quality_candidate_hash_mismatch")
    from strict_authority_workflow import (
        MASTER_SLOTS, authority_summary, expected_master_contexts, gate_call_context,
    )
    required_slots = MASTER_SLOTS + (("review",) if gate_name == "review" else ("review", "critic"))
    try:
        authority = authority_summary(
            checkpoint,
            required_slots=required_slots,
            expected_role_results={gate_name: _gate_role_result(llm_gate)},
            expected_context_bindings={
                **expected_master_contexts(checkpoint.get("master_plan") or {}),
                gate_name: gate_call_context(checkpoint, gate_name=gate_name, candidate_dir=candidate),
            },
            require_no_other_accepted=require_exact_authority,
        )
    except Exception as exc:
        errors.append(f"system_bootstrap_gate_authority_invalid:{type(exc).__name__}:{str(exc)[:300]}")
        authority = None
    review_receipt_digest = None
    if gate_name == "critic":
        review_receipt_digest = (
            ((gates.get("review") or {}).get("system_verifier_receipt") or {}).get("receipt_digest")
        )
    subject = {
        "schema_version": 1,
        "kind": SYSTEM_GATE_RECEIPT_KIND,
        "gate": gate_name,
        "executor": EXECUTOR_ID,
        "source_v": 142,
        "next_v": 143,
        "candidate_artifact_hash": candidate_hash,
        "master_receipt_digest": master.get("receipt_digest"),
        "master_plan_digest": master.get("plan_digest"),
        "worker_receipt_digest": worker.get("receipt_digest") if isinstance(worker, dict) else None,
        "durable_worker_output_digest": _canonical_digest(durable),
        "quality_gate_digest": _canonical_digest(quality),
        "llm_gate_digest": _canonical_digest(_gate_without_receipt(llm_gate)),
        "review_receipt_digest": review_receipt_digest,
        "llm_authority": authority,
        "verification_scope": "policy_content_chain_adjunct_to_schema_valid_llm_gate",
    }
    return subject, errors


def build_system_gate_receipt(
    checkpoint: dict[str, Any],
    *,
    gate_name: str,
    candidate_dir: str | Path,
    llm_gate: dict[str, Any],
    _require_exact_authority: bool = True,
) -> dict[str, Any]:
    subject, errors = _system_gate_subject(
        checkpoint,
        gate_name=gate_name,
        candidate_dir=candidate_dir,
        llm_gate=llm_gate,
        require_exact_authority=_require_exact_authority,
    )
    if errors:
        raise SystemStrictBootstrapError(errors)
    return _receipt(subject)


def validate_system_gate_receipt(
    checkpoint: dict[str, Any],
    *,
    gate_name: str,
    candidate_dir: str | Path | None = None,
) -> list[str]:
    gate = ((checkpoint.get("gate_results") or {}).get(gate_name) or {})
    errors = validate_embedded_system_gate(gate, gate_name=gate_name)
    receipt = gate.get("system_verifier_receipt") if isinstance(gate, dict) else None
    errors.extend(_receipt_errors(receipt, kind=SYSTEM_GATE_RECEIPT_KIND))
    if errors:
        return list(dict.fromkeys(errors))
    candidate = Path(candidate_dir) if candidate_dir is not None else (
        PROJECT_ROOT / "bots" / f"national_v{int(checkpoint.get('next_v'))}"
    )
    try:
        expected = build_system_gate_receipt(
            checkpoint,
            gate_name=gate_name,
            candidate_dir=candidate,
            llm_gate=gate,
            _require_exact_authority=False,
        )
    except SystemStrictBootstrapError as exc:
        return list(exc.errors)
    if receipt != expected:
        errors.append("system_gate_receipt_subject_mismatch")
    return list(dict.fromkeys(errors))


__all__ = [
    "POLICY_VERSION_AUTHORITY",
    "BLUEPRINT_DIR",
    "BLUEPRINT_MANIFEST",
    "EXECUTOR_ID",
    "FRESH_BOOTSTRAP_RECEIPT_KIND",
    "POLICY_EPOCH_RESET_RECEIPT_FILENAME",
    "POLICY_EPOCH_RESET_RECEIPT_KIND",
    "LLM_INVOCATION_EVIDENCE_KIND",
    "SYSTEM_GATE_RECEIPT_KIND",
    "SYSTEM_MASTER_RECEIPT_KIND",
    "SYSTEM_WORKER_RECEIPT_KIND",
    "SystemStrictBootstrapError",
    "abandon_rejected_blueprint",
    "apply_blueprint",
    "bind_worker_effect_receipt",
    "blueprint_identity",
    "build_fresh_bootstrap_receipt",
    "build_master_receipt",
    "build_system_gate_receipt",
    "is_declared_native_bootstrap",
    "llm_result_digest",
    "load_blueprint_manifest",
    "load_policy_epoch_reset_receipt",
    "materialize_fresh_candidate",
    "new_llm_invocation_id",
    "record_llm_invocation_evidence",
    "refresh_policy_identity",
    "system_recovery_eligible",
    "system_worker_backend_contract",
    "validate_blueprint_package",
    "validate_bootstrap_checkpoint",
    "validate_embedded_system_gate",
    "validate_fresh_bootstrap_receipt",
    "validate_llm_invocation_evidence",
    "validate_policy_epoch_reset_archive",
    "validate_policy_epoch_reset_receipt",
    "validate_master_receipt",
    "validate_selected_proposal_for_blueprint",
    "validate_system_gate_receipt",
    "validate_system_worker_envelope",
]
