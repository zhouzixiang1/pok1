"""System-owned policy-ABI opponent for the first strict publication.

The control is materialized from fresh checked-in policy bytes and the current
system runtime.  It has no historical bot source, cannot enter ratings, and is
valid only while the active national TCP policy pool is empty.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any, Iterable

from bot_artifact import artifact_manifest, canonical_digest, hash_path
from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    DECISION_CONTEXT_SCHEMA_VERSION,
    EVALUATION_EPOCH,
    FIRST_STRICT_POLICY_VERSION,
    NATIONAL_ENTRYPOINT,
    NATIONAL_PROTOCOL_ID,
    NATIONAL_RUNTIME_CONTRACT_ID,
    NATIONAL_RUNTIME_MANIFEST,
    NATIONAL_RUNTIME_MANIFEST_SCHEMA_VERSION,
    NATIONAL_RUNTIME_SCHEMA_VERSION,
    NATIONAL_STREAM_SCHEMA_VERSION,
    POLICY_DECISION_SCHEMA_VERSION,
    POLICY_ENTRYPOINT,
    POLICY_EPOCH_RECEIPT,
    POLICY_EPOCH_RECEIPT_SCHEMA_VERSION,
    PRECOMPUTE_ENTRYPOINT,
    artifact_contract_digest,
    epoch_receipt_errors,
    runtime_manifest_errors,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = (
    Path(__file__).resolve().parent
    / "bootstrap_assets"
    / "first_strict_control_v1"
)
MANIFEST_PATH = ASSET_DIR / "manifest.json"
CONTROL_CACHE_ROOT = (
    Path(__file__).resolve().parent
    / "results"
    / "system_controls"
)

CONTROL_ID = "first_strict_control_v1"
CONTROL_AUTHORITY = "system_first_strict_control"
CONTROL_REASON = "first_strict_bootstrap_control"
CONTROL_GATE_PROFILE_ID = "first_strict_bootstrap_regression_v1"
CONTROL_EXACT_SAMPLES = 8
# Compatibility name retained for callers and old checkpoint projections.  The
# execution contract is now exact, not merely a lower bound.
CONTROL_MIN_SAMPLES = CONTROL_EXACT_SAMPLES
CONTROL_MIN_MATCH_SCORE = 0.625
CONTROL_RECEIPT_KIND = "system-first-strict-control-receipt"
CONTROL_RECEIPT_SCHEMA_VERSION = 1
SOURCE_VERSION = ARCHIVED_VERSION_HIGH_WATER
CONTROL_POLICY_VERSION = FIRST_STRICT_POLICY_VERSION
_ASSET_FILES = frozenset({"policy.py"})
_ARTIFACT_FILES = frozenset({
    "national_bot.py",
    "policy.py",
    "precompute.py",
    NATIONAL_RUNTIME_MANIFEST,
    POLICY_EPOCH_RECEIPT,
})


class FirstStrictControlError(RuntimeError):
    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = tuple(str(item) for item in issues if str(item))
        super().__init__("; ".join(self.issues) or "first strict control invalid")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _receipt(payload: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(payload)
    value["receipt_digest"] = canonical_digest(value)
    return value


def load_control_manifest() -> dict[str, Any]:
    try:
        value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FirstStrictControlError([
            f"first_strict_control_manifest_unreadable:{type(exc).__name__}"
        ]) from exc
    if not isinstance(value, dict):
        raise FirstStrictControlError(["first_strict_control_manifest_not_object"])
    return value


def _runtime_bytes() -> dict[str, bytes]:
    from national_native import NATIVE_BOT_TEMPLATE, NATIVE_PRECOMPUTE_TEMPLATE

    return {
        "national_bot.py": NATIVE_BOT_TEMPLATE.encode("utf-8"),
        "precompute.py": NATIVE_PRECOMPUTE_TEMPLATE.encode("utf-8"),
    }


def _runtime_manifest_payload(core: dict[str, bytes]) -> dict[str, Any]:
    return {
        "schema_version": NATIONAL_RUNTIME_MANIFEST_SCHEMA_VERSION,
        "contract_id": NATIONAL_RUNTIME_CONTRACT_ID,
        "epoch": EVALUATION_EPOCH,
        "protocol": NATIONAL_PROTOCOL_ID,
        "runtime_schema_version": NATIONAL_RUNTIME_SCHEMA_VERSION,
        "stream_schema_version": NATIONAL_STREAM_SCHEMA_VERSION,
        "decision_context_schema_version": DECISION_CONTEXT_SCHEMA_VERSION,
        "policy_decision_schema_version": POLICY_DECISION_SCHEMA_VERSION,
        "entrypoint": NATIONAL_ENTRYPOINT,
        "policy_entrypoint": POLICY_ENTRYPOINT,
        "precompute_entrypoint": PRECOMPUTE_ENTRYPOINT,
        "files": {
            relative: _sha256_bytes(core[relative])
            for relative in (NATIONAL_ENTRYPOINT, POLICY_ENTRYPOINT, PRECOMPUTE_ENTRYPOINT)
        },
    }


def _epoch_receipt_payload(runtime_manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": POLICY_EPOCH_RECEIPT_SCHEMA_VERSION,
        "epoch": EVALUATION_EPOCH,
        "version": CONTROL_POLICY_VERSION,
        "lineage": {
            "mode": "fresh_bootstrap",
            "parent_versions": [],
            "version_authority_high_water": ARCHIVED_VERSION_HIGH_WATER,
            "source_artifact_inherited": False,
        },
        "artifact_contract_digest": artifact_contract_digest(runtime_manifest),
    }


def _expected_artifact_bytes() -> dict[str, bytes]:
    payload = _runtime_bytes()
    for relative in sorted(_ASSET_FILES):
        payload[relative] = (ASSET_DIR / relative).read_bytes()
    runtime_manifest = _runtime_manifest_payload(payload)
    payload[NATIONAL_RUNTIME_MANIFEST] = (
        json.dumps(runtime_manifest, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    epoch_receipt = _epoch_receipt_payload(runtime_manifest)
    payload[POLICY_EPOCH_RECEIPT] = (
        json.dumps(epoch_receipt, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    return payload


def _standard_cache_relatives() -> frozenset[str]:
    """Return only cache nodes CPython may create for the checked-in sources."""

    rows = {"__pycache__"}
    for relative in sorted(_ASSET_FILES):
        source = ASSET_DIR / relative
        for optimization in ("", 1, 2):
            cache = Path(importlib.util.cache_from_source(
                str(source),
                optimization=optimization,
            ))
            rows.add(cache.relative_to(ASSET_DIR).as_posix())
    return frozenset(rows)


def _is_ignorable_standard_cache(relative: str, path: Path) -> bool:
    """Ignore generated bytecode only when both its name and node type match."""

    if relative not in _standard_cache_relatives():
        return False
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    if relative == "__pycache__":
        return stat.S_ISDIR(mode) and not stat.S_ISLNK(mode)
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def validate_control_package(
    manifest: dict[str, Any] | None = None,
) -> list[str]:
    try:
        manifest = manifest or load_control_manifest()
    except FirstStrictControlError as exc:
        return list(exc.issues)

    issues: list[str] = []
    if manifest.get("schema_version") != 1:
        issues.append("first_strict_control_manifest_schema_mismatch")
    if manifest.get("control_id") != CONTROL_ID:
        issues.append("first_strict_control_id_mismatch")
    if manifest.get("authority") != CONTROL_AUTHORITY:
        issues.append("first_strict_control_authority_mismatch")
    if manifest.get("policy") != "deterministic_passive_typed_pass_v1":
        issues.append("first_strict_control_policy_mismatch")
    if manifest.get("gate_profile_id") != CONTROL_GATE_PROFILE_ID:
        issues.append("first_strict_control_gate_profile_mismatch")
    if manifest.get("gate_contract") != {
        "sample_unit": "complete_70_hand_match",
        "exact_samples": CONTROL_EXACT_SAMPLES,
        "minimum_samples": CONTROL_MIN_SAMPLES,
        "minimum_match_score": CONTROL_MIN_MATCH_SCORE,
        "draw_score": 0.5,
    }:
        issues.append("first_strict_control_gate_contract_mismatch")

    actual_nodes = {
        path.relative_to(ASSET_DIR).as_posix(): path
        for path in ASSET_DIR.rglob("*")
    }
    ignored_cache_nodes = {
        relative
        for relative, path in actual_nodes.items()
        if _is_ignorable_standard_cache(relative, path)
    }
    if set(actual_nodes) - ignored_cache_nodes != {"manifest.json", *_ASSET_FILES}:
        issues.append("first_strict_control_package_entries_mismatch")
    for relative, path in actual_nodes.items():
        if relative in ignored_cache_nodes:
            continue
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            issues.append(
                f"first_strict_control_package_unreadable:{relative}:{type(exc).__name__}"
            )
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            issues.append(f"first_strict_control_package_node_not_regular:{relative}")

    declared_files = manifest.get("files")
    if not isinstance(declared_files, dict) or set(declared_files) != _ASSET_FILES:
        issues.append("first_strict_control_declared_files_mismatch")
        declared_files = declared_files if isinstance(declared_files, dict) else {}
    for relative in sorted(_ASSET_FILES):
        path = ASSET_DIR / relative
        if not path.is_file() or path.is_symlink():
            issues.append(f"first_strict_control_asset_invalid:{relative}")
            continue
        if declared_files.get(relative) != _sha256_file(path):
            issues.append(f"first_strict_control_asset_hash_mismatch:{relative}")

    runtime = manifest.get("runtime") if isinstance(manifest.get("runtime"), dict) else {}
    try:
        runtime_bytes = _runtime_bytes()
        if runtime.get("national_bot_sha256") != _sha256_bytes(
            runtime_bytes["national_bot.py"]
        ):
            issues.append("first_strict_control_national_runtime_hash_mismatch")
        if runtime.get("precompute_sha256") != _sha256_bytes(
            runtime_bytes["precompute.py"]
        ):
            issues.append("first_strict_control_precompute_hash_mismatch")
        from national_native import NATIONAL_DECISION_RUNTIME_VERSION

        if runtime.get("decision_runtime_version") != NATIONAL_DECISION_RUNTIME_VERSION:
            issues.append("first_strict_control_decision_runtime_version_mismatch")
        if runtime.get("stream_decoder_version") != 2:
            issues.append("first_strict_control_stream_decoder_version_mismatch")
    except Exception as exc:
        issues.append(f"first_strict_control_runtime_identity_error:{type(exc).__name__}")

    scope = manifest.get("execution_scope") if isinstance(
        manifest.get("execution_scope"), dict
    ) else {}
    expected_scope = {
        "lineage_high_water": SOURCE_VERSION,
        "policy_version": CONTROL_POLICY_VERSION,
        "epoch": EVALUATION_EPOCH,
        "source_artifact_inherited": False,
        "requires_empty_strict_pool": True,
        "precommit_gate_admitted": True,
        "formal_bootstrap_opponent_admitted": True,
        "formal_bootstrap_scope": "first_policy_bot_empty_pool_only",
        "strength_admitted": False,
        "rating_eligible": False,
        "official_opponent_eligible": False,
    }
    if scope != expected_scope:
        issues.append("first_strict_control_execution_scope_mismatch")

    expected_hash = str(manifest.get("expected_artifact_hash") or "")
    if len(expected_hash) != 64 or any(
        ch not in "0123456789abcdef" for ch in expected_hash
    ):
        issues.append("first_strict_control_expected_artifact_hash_invalid")
    return list(dict.fromkeys(issues))


def control_gate_contract() -> dict[str, Any]:
    """Return the validated manifest-owned execution/gate shape.

    Callers must not reinterpret an LLM-supplied ``n_games`` value as control
    authority.  The checked-in manifest is the sole owner of the exact sample
    count and score floor; the module constants only validate that immutable
    package ABI.
    """

    manifest = load_control_manifest()
    issues = validate_control_package(manifest)
    if issues:
        raise FirstStrictControlError(issues)
    return deepcopy(manifest.get("gate_contract") or {})


def _artifact_path(manifest: dict[str, Any]) -> Path:
    return CONTROL_CACHE_ROOT / str(manifest.get("expected_artifact_hash") or "invalid")


def validate_materialized_control(
    path: str | Path,
    *,
    manifest: dict[str, Any] | None = None,
) -> list[str]:
    manifest = manifest or load_control_manifest()
    issues = validate_control_package(manifest)
    control_path = Path(path).absolute()
    expected_path = _artifact_path(manifest).absolute()
    if control_path != expected_path:
        issues.append("first_strict_control_path_mismatch")
    try:
        observed_manifest = artifact_manifest(control_path)
        entries = observed_manifest.get("entries") or []
        files = {
            str(item.get("path"))
            for item in entries
            if item.get("type") == "file"
        }
        directories = {
            str(item.get("path"))
            for item in entries
            if item.get("type") == "directory"
        }
        if files != _ARTIFACT_FILES or directories != {"."}:
            issues.append("first_strict_control_artifact_entries_mismatch")
        try:
            runtime_manifest = json.loads(
                (control_path / NATIONAL_RUNTIME_MANIFEST).read_text(encoding="utf-8")
            )
            epoch_receipt = json.loads(
                (control_path / POLICY_EPOCH_RECEIPT).read_text(encoding="utf-8")
            )
            issues.extend(runtime_manifest_errors(control_path, runtime_manifest))
            issues.extend(
                epoch_receipt_errors(
                    control_path,
                    CONTROL_POLICY_VERSION,
                    runtime_manifest,
                    epoch_receipt,
                )
            )
        except Exception as exc:
            issues.append(f"first_strict_control_epoch_contract_error:{type(exc).__name__}")
        observed_hash = canonical_digest(observed_manifest)
        if observed_hash != manifest.get("expected_artifact_hash"):
            issues.append("first_strict_control_artifact_hash_mismatch")
    except Exception as exc:
        issues.append(
            f"first_strict_control_artifact_validation_error:{type(exc).__name__}"
        )
    return list(dict.fromkeys(issues))


def materialize_control() -> Path:
    manifest = load_control_manifest()
    package_issues = validate_control_package(manifest)
    if package_issues:
        raise FirstStrictControlError(package_issues)
    target = _artifact_path(manifest)
    if target.exists():
        issues = validate_materialized_control(target, manifest=manifest)
        if issues:
            raise FirstStrictControlError(issues)
        return target

    CONTROL_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{CONTROL_ID}-",
        dir=str(CONTROL_CACHE_ROOT),
    ))
    try:
        for relative, payload in sorted(_expected_artifact_bytes().items()):
            destination = temporary / relative
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            descriptor = os.open(destination, flags, 0o444)
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(descriptor, payload[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            destination.chmod(0o444)
        temporary.chmod(0o555)
        if hash_path(temporary) != manifest.get("expected_artifact_hash"):
            raise FirstStrictControlError([
                "first_strict_control_materialized_hash_mismatch"
            ])
        try:
            os.replace(temporary, target)
        except OSError:
            if not target.exists():
                raise
        issues = validate_materialized_control(target, manifest=manifest)
        if issues:
            raise FirstStrictControlError(issues)
        return target
    finally:
        if temporary.exists():
            temporary.chmod(0o755)
            shutil.rmtree(temporary, ignore_errors=True)


def control_identity(path: str | Path | None = None) -> dict[str, Any]:
    manifest = load_control_manifest()
    if path is None:
        path = materialize_control()
    issues = validate_materialized_control(path, manifest=manifest)
    if issues:
        raise FirstStrictControlError(issues)
    payload = {
        "schema_version": 1,
        "authority": CONTROL_AUTHORITY,
        "control_id": CONTROL_ID,
        "policy": manifest.get("policy"),
        "gate_profile_id": CONTROL_GATE_PROFILE_ID,
        "gate_contract": deepcopy(manifest.get("gate_contract") or {}),
        "artifact_hash": manifest.get("expected_artifact_hash"),
        "manifest_sha256": _sha256_file(MANIFEST_PATH),
        "manifest_contract_digest": canonical_digest(manifest),
        "runtime": deepcopy(manifest.get("runtime") or {}),
        "path": str(Path(path).absolute()),
        "precommit_gate_admitted": True,
        "formal_bootstrap_opponent_admitted": True,
        "formal_bootstrap_scope": "first_policy_bot_empty_pool_only",
        "strength_admitted": False,
        "rating_eligible": False,
        "official_opponent_eligible": False,
    }
    return {**payload, "identity_digest": canonical_digest(payload)}


def _authority_issues(
    checkpoint: Any,
    *,
    active_bots: Iterable[str] | None = None,
    force_protocol_refresh: bool = True,
    pending_local_publication: dict[str, Any] | None = None,
) -> list[str]:
    issues: list[str] = []
    if not isinstance(checkpoint, dict):
        return ["first_strict_control_checkpoint_missing"]
    try:
        from system_strict_bootstrap import is_declared_native_bootstrap

        if not is_declared_native_bootstrap(checkpoint):
            issues.append("first_strict_control_not_declared_bootstrap")
    except Exception as exc:
        issues.append(f"first_strict_control_declaration_error:{type(exc).__name__}")
    _source_v = checkpoint.get("source_v")
    if int(_source_v if _source_v is not None else -1) != SOURCE_VERSION:
        issues.append("first_strict_control_source_version_mismatch")
    audit = checkpoint.get("audit_context") or {}
    protocol = audit.get("protocol_bootstrap") or {}
    if protocol.get("active_bots") != []:
        issues.append("first_strict_control_receipt_pool_not_empty")
    try:
        if active_bots is None:
            from evolution_infra import get_active_bots_read_only

            active_bots = get_active_bots_read_only()
        from system_strict_bootstrap import validate_fresh_bootstrap_receipt

        issues.extend(
            "first_strict_control_" + str(item)
            for item in validate_fresh_bootstrap_receipt(
                protocol,
                active_bots=list(active_bots),
            )
        )
    except Exception as exc:
        issues.append(f"first_strict_control_protocol_error:{type(exc).__name__}")
    return list(dict.fromkeys(issues))


def build_control_receipt(
    checkpoint: dict[str, Any],
    *,
    active_bots: Iterable[str] | None = None,
) -> dict[str, Any]:
    issues = _authority_issues(checkpoint, active_bots=active_bots)
    if issues:
        raise FirstStrictControlError(issues)
    path = materialize_control()
    protocol = (checkpoint.get("audit_context") or {}).get("protocol_bootstrap") or {}
    return _receipt({
        "schema_version": CONTROL_RECEIPT_SCHEMA_VERSION,
        "kind": CONTROL_RECEIPT_KIND,
        "candidate_version": int(checkpoint.get("next_v")),
        "source_version": SOURCE_VERSION,
        "protocol_bootstrap_receipt": deepcopy(protocol),
        "protocol_bootstrap_receipt_digest": protocol.get("receipt_digest"),
        "active_policy_bots": [],
        "control": control_identity(path),
        "gate_profile_id": CONTROL_GATE_PROFILE_ID,
        "gate_contract": control_gate_contract(),
        "precommit_gate_admitted": True,
        "formal_bootstrap_opponent_admitted": True,
        "formal_bootstrap_scope": "first_policy_bot_empty_pool_only",
        "strength_admitted": False,
        "rating_eligible": False,
        "official_opponent_eligible": False,
    })


def validate_control_receipt(
    receipt: Any,
    *,
    checkpoint: dict[str, Any] | None = None,
    candidate_version: int | None = None,
    source_version: int | None = None,
    active_bots: Iterable[str] | None = None,
    force_protocol_refresh: bool = True,
    pending_local_publication: dict[str, Any] | None = None,
) -> list[str]:
    if not isinstance(receipt, dict):
        return ["first_strict_control_receipt_missing"]
    issues: list[str] = []
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    if receipt.get("receipt_digest") != canonical_digest(unsigned):
        issues.append("first_strict_control_receipt_digest_mismatch")
    if receipt.get("schema_version") != CONTROL_RECEIPT_SCHEMA_VERSION:
        issues.append("first_strict_control_receipt_schema_mismatch")
    if receipt.get("kind") != CONTROL_RECEIPT_KIND:
        issues.append("first_strict_control_receipt_kind_mismatch")
    expected_candidate = (
        int(candidate_version)
        if candidate_version is not None
        else int((checkpoint or {}).get("next_v") or -1)
    )
    _ckpt_source_v = (checkpoint or {}).get("source_v")
    expected_source = (
        int(source_version)
        if source_version is not None
        else int(_ckpt_source_v if _ckpt_source_v is not None else SOURCE_VERSION)
    )
    if receipt.get("candidate_version") != expected_candidate:
        issues.append("first_strict_control_receipt_candidate_mismatch")
    if receipt.get("source_version") != expected_source or expected_source != SOURCE_VERSION:
        issues.append("first_strict_control_receipt_source_mismatch")
    expected_flags = {
        "precommit_gate_admitted": True,
        "formal_bootstrap_opponent_admitted": True,
        "strength_admitted": False,
        "rating_eligible": False,
        "official_opponent_eligible": False,
    }
    for field, value in expected_flags.items():
        if receipt.get(field) is not value:
            issues.append(f"first_strict_control_receipt_{field}_mismatch")
    if receipt.get("formal_bootstrap_scope") != "first_policy_bot_empty_pool_only":
        issues.append("first_strict_control_receipt_formal_scope_mismatch")
    if receipt.get("gate_profile_id") != CONTROL_GATE_PROFILE_ID:
        issues.append("first_strict_control_receipt_gate_profile_mismatch")
    if receipt.get("gate_contract") != (
        load_control_manifest().get("gate_contract") or {}
    ):
        issues.append("first_strict_control_receipt_gate_contract_mismatch")
    if receipt.get("active_policy_bots") != []:
        issues.append("first_strict_control_receipt_pool_mismatch")
    if active_bots is None and isinstance(
        receipt.get("active_policy_bots"), list
    ):
        # The signed receipt's empty policy pool is the comparison input.  A
        # newly published policy bot invalidates this one-time path.
        active_bots = list(receipt.get("active_policy_bots") or [])
    try:
        expected_control = control_identity()
        if receipt.get("control") != expected_control:
            issues.append("first_strict_control_receipt_identity_mismatch")
    except Exception as exc:
        issues.append(f"first_strict_control_identity_error:{type(exc).__name__}")

    protocol = receipt.get("protocol_bootstrap_receipt") or {}
    if receipt.get("protocol_bootstrap_receipt_digest") != protocol.get(
        "receipt_digest"
    ):
        issues.append("first_strict_control_protocol_digest_mismatch")
    if checkpoint is not None:
        checkpoint_protocol = (
            (checkpoint.get("audit_context") or {}).get("protocol_bootstrap") or {}
        )
        if protocol != checkpoint_protocol:
            issues.append("first_strict_control_checkpoint_protocol_mismatch")
        issues.extend(_authority_issues(
            checkpoint,
            active_bots=active_bots,
            force_protocol_refresh=force_protocol_refresh,
            pending_local_publication=pending_local_publication,
        ))
    else:
        try:
            if active_bots is None:
                from evolution_infra import get_active_bots_read_only

                active_bots = get_active_bots_read_only()
            from system_strict_bootstrap import validate_fresh_bootstrap_receipt

            issues.extend(
                "first_strict_control_" + str(item)
                for item in validate_fresh_bootstrap_receipt(
                    protocol,
                    active_bots=list(active_bots),
                )
            )
        except Exception as exc:
            issues.append(f"first_strict_control_protocol_error:{type(exc).__name__}")
    return list(dict.fromkeys(issues))


def opponent_from_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    _rcv = receipt.get("candidate_version")
    _rsv = receipt.get("source_version")
    issues = validate_control_receipt(
        receipt,
        candidate_version=int(_rcv if _rcv is not None else -1),
        source_version=int(_rsv if _rsv is not None else -1),
        active_bots=list(receipt.get("active_policy_bots") or []),
        force_protocol_refresh=False,
    )
    if issues:
        raise FirstStrictControlError(issues)
    control = receipt.get("control") or {}
    return {
        "name": CONTROL_ID,
        "reason": CONTROL_REASON,
        "path": str(control.get("path") or ""),
        "authority": CONTROL_AUTHORITY,
        "control_receipt": deepcopy(receipt),
        "precommit_gate_admitted": True,
        "formal_bootstrap_opponent_admitted": True,
        "formal_bootstrap_scope": "first_policy_bot_empty_pool_only",
        "strength_admitted": False,
        "rating_eligible": False,
        "official_opponent_eligible": False,
    }


_NON_AUTHORITY_FIELDS = frozenset({
    "strength_authoritative",
    "strength_admitted",
    "rating_eligible",
    "official_opponent_eligible",
})
_EMBEDDED_EXECUTION_FIELDS = frozenset({
    "raw",
    "events",
    "settlements",
    "hand_records",
    "execution",
})


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _control_gate_summary(net_chips: Iterable[int]) -> dict[str, Any]:
    from strength_order import summarize_match_outcomes

    contract = control_gate_contract()
    values = [int(value) for value in net_chips]
    summary = summarize_match_outcomes(
        sum(1 for value in values if value > 0),
        sum(1 for value in values if value < 0),
        sum(1 for value in values if value == 0),
    )
    return {
        **summary,
        "gate_profile_id": CONTROL_GATE_PROFILE_ID,
        "exact_samples": int(contract["exact_samples"]),
        "minimum_samples": int(contract["minimum_samples"]),
        "minimum_match_score": float(contract["minimum_match_score"]),
        "sample_unit": str(contract["sample_unit"]),
        "strength_admitted": False,
        "rating_eligible": False,
        "official_opponent_eligible": False,
    }


def _nested_non_authority_issues(value: Any, path: str = "result") -> list[str]:
    issues: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in _NON_AUTHORITY_FIELDS and item is not False:
                issues.append(
                    f"first_strict_control_nested_{key}_mismatch:{child_path}"
                )
            issues.extend(_nested_non_authority_issues(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            issues.extend(_nested_non_authority_issues(
                item,
                f"{path}[{index}]",
            ))
    return issues


def _embedded_execution_issues(value: Any, path: str = "result") -> list[str]:
    """Reject replay bodies smuggled beside content-bound receipt references."""

    issues: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in _EMBEDDED_EXECUTION_FIELDS:
                issues.append(
                    "first_strict_control_embedded_execution_field:"
                    f"{child_path}"
                )
            issues.extend(_embedded_execution_issues(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            issues.extend(_embedded_execution_issues(
                item,
                f"{path}[{index}]",
            ))
    return issues


def _validate_expected_sample_plan(
    sample_plan: Any,
) -> tuple[list[str], list[dict[str, Any]]]:
    exact_samples = int(control_gate_contract()["exact_samples"])
    issues: list[str] = []
    if not isinstance(sample_plan, list):
        return ["first_strict_control_sample_plan_missing"], []
    rows = [dict(item) for item in sample_plan if isinstance(item, dict)]
    if len(rows) != len(sample_plan) or len(rows) != exact_samples:
        issues.append("first_strict_control_sample_plan_shape_invalid")
    expected_repeats = list(range(1, exact_samples + 1))
    repeats = [item.get("repeat") for item in rows]
    if repeats != expected_repeats:
        issues.append("first_strict_control_sample_plan_repeats_invalid")
    if any(item.get("opponent") != CONTROL_ID for item in rows):
        issues.append("first_strict_control_sample_plan_opponent_invalid")
    if any(item.get("opponent_index") != 0 for item in rows):
        issues.append("first_strict_control_sample_plan_index_invalid")
    seed_pairs: list[tuple[int, int]] = []
    for item in rows:
        deck_seed = item.get("deck_seed_base")
        bot_seed = item.get("bot_seed_base")
        if not _plain_int(deck_seed) or not _plain_int(bot_seed):
            issues.append("first_strict_control_sample_plan_seed_invalid")
            continue
        if bot_seed != deck_seed + 1_000_000_000:
            issues.append("first_strict_control_sample_plan_seed_relation_invalid")
        seed_pairs.append((deck_seed, bot_seed))
    if len(seed_pairs) != len(set(seed_pairs)):
        issues.append("first_strict_control_sample_plan_seed_duplicate")
    return list(dict.fromkeys(issues)), rows


def validate_control_result(
    result_or_matchups: Any,
    *,
    expected_sample_plan: list[dict[str, Any]] | None = None,
    expected_execution_scope: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Validate the complete non-strength control evidence projection.

    The same validator is replayed immediately after the native runtime and at
    the final commit ledger.  It derives W/L/D exclusively from the signed
    net-chip vector, binds every repeat to the immutable seed plan, and rejects
    any nested strength, rating, or official-opponent authority.
    """

    exact_samples = int(control_gate_contract()["exact_samples"])
    issues: list[str] = []
    expected_timing_plan = None
    if expected_execution_scope is not None:
        try:
            from first_strict_execution_journal import normalize_execution_scope
            from national_native import (
                LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
                build_native_match_timing_plan,
            )

            normalized_expected_scope = normalize_execution_scope(
                expected_execution_scope
            )
            expected_timing_plan = build_native_match_timing_plan(
                hands=70,
                requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            )
            if (
                normalized_expected_scope.get("native_match_timing_plan_digest")
                != expected_timing_plan.digest()
            ):
                issues.append(
                    "first_strict_control_scope_timing_plan_digest_mismatch"
                )
        except Exception as exc:
            issues.append(
                "first_strict_control_expected_timing_plan_invalid:"
                f"{type(exc).__name__}"
            )
    outer: dict[str, Any] | None = None
    envelope: dict[str, Any] | None = None
    if isinstance(result_or_matchups, dict):
        outer = result_or_matchups
        nested = outer.get("national")
        envelope = nested if isinstance(nested, dict) else outer
        raw_matchups = envelope.get("matchups")
    else:
        try:
            raw_matchups = list(result_or_matchups)
        except TypeError:
            raw_matchups = []
            issues.append("first_strict_control_matchups_missing")

    if not isinstance(raw_matchups, list):
        issues.append("first_strict_control_matchups_missing")
        matchups: list[Any] = []
    else:
        matchups = raw_matchups
    control_rows = [
        item for item in matchups
        if isinstance(item, dict)
        and (
            item.get("opponent") == CONTROL_ID
            or item.get("evaluation_authority") == CONTROL_GATE_PROFILE_ID
        )
    ]
    if len(matchups) != 1 or len(control_rows) != 1:
        issues.append("first_strict_control_matchup_shape_invalid")
    matchup = control_rows[0] if control_rows else {}

    expected_matchup_values = {
        "opponent": CONTROL_ID,
        "reason": CONTROL_REASON,
        "evaluation_authority": CONTROL_GATE_PROFILE_ID,
        "opponent_runtime_mode": "system_first_strict_control",
        "protocol": "national_native_tcp",
        "hands_per_match": 70,
        "matches": exact_samples,
        "samples_expected": exact_samples,
        "n_played": exact_samples,
        "hands_played_total": 70 * exact_samples,
    }
    for field, expected in expected_matchup_values.items():
        if matchup.get(field) != expected:
            issues.append(f"first_strict_control_matchup_{field}_mismatch")
    expected_matchup_flags = {
        "precommit_gate_admitted": True,
        "strength_authoritative": False,
        "strength_admitted": False,
        "rating_eligible": False,
        "official_opponent_eligible": False,
        "migration_projection": False,
    }
    for field, expected in expected_matchup_flags.items():
        if matchup.get(field) is not expected:
            issues.append(f"first_strict_control_matchup_{field}_mismatch")
    for field in (
        "candidate_compliance_issues",
        "opponent_compliance_issues",
    ):
        if matchup.get(field) != []:
            issues.append(f"first_strict_control_matchup_{field}_not_empty")

    raw_net_chips = matchup.get("net_chips")
    if not isinstance(raw_net_chips, list) or any(
        not _plain_int(value) for value in raw_net_chips
    ):
        issues.append("first_strict_control_net_chips_invalid")
        net_chips: list[int] = []
    else:
        net_chips = list(raw_net_chips)
    if len(net_chips) != exact_samples:
        issues.append("first_strict_control_net_chips_count_mismatch")

    repeats = matchup.get("repeats")
    if not isinstance(repeats, list):
        repeats = []
        issues.append("first_strict_control_repeats_missing")
    if len(repeats) != exact_samples:
        issues.append("first_strict_control_repeat_count_mismatch")
    repeat_net_chips: list[int] = []
    observed_sample_plan: list[dict[str, Any]] = []
    repeat_artifacts: list[dict[str, Any]] = []
    execution_receipt_ids: list[str] = []
    execution_match_run_ids: list[str] = []
    observed_execution_scopes: list[dict[str, Any]] = []
    for index, repeat in enumerate(repeats, start=1):
        prefix = f"first_strict_control_repeat_{index}"
        if not isinstance(repeat, dict):
            issues.append(f"{prefix}_invalid")
            continue
        expected_repeat_values = {
            "repeat": index,
            "hands_played": 70,
            "opponent_runtime_mode": "system_first_strict_control",
            "evaluation_authority": CONTROL_GATE_PROFILE_ID,
        }
        for field, expected in expected_repeat_values.items():
            if repeat.get(field) != expected:
                issues.append(f"{prefix}_{field}_mismatch")
        expected_repeat_flags = {
            "complete": True,
            "passed_compliance": True,
            "sample_valid": True,
            "precommit_gate_admitted": True,
            "strength_admitted": False,
            "rating_eligible": False,
            "official_opponent_eligible": False,
            "migration_projection": False,
            "artifact_execution_valid": True,
        }
        for field, expected in expected_repeat_flags.items():
            if repeat.get(field) is not expected:
                issues.append(f"{prefix}_{field}_mismatch")
        for field in ("candidate_issues", "opponent_issues"):
            if repeat.get(field) != []:
                issues.append(f"{prefix}_{field}_not_empty")

        net = repeat.get("net_chips")
        if not _plain_int(net):
            issues.append(f"{prefix}_net_chips_invalid")
        else:
            repeat_net_chips.append(net)
        deck_seed = repeat.get("deck_seed_base")
        bot_seed = repeat.get("bot_seed_base")
        if not _plain_int(deck_seed) or not _plain_int(bot_seed):
            issues.append(f"{prefix}_seed_invalid")
        elif bot_seed != deck_seed + 1_000_000_000:
            issues.append(f"{prefix}_seed_relation_invalid")
        observed_sample_plan.append({
            "opponent": CONTROL_ID,
            "opponent_index": 0,
            "repeat": repeat.get("repeat"),
            "deck_seed_base": deck_seed,
            "bot_seed_base": bot_seed,
            "native_match_timing_plan_digest": (
                ((repeat.get("local_runtime_budget") or {}).get(
                    "timing_plan_digest"
                ))
            ),
        })

        if expected_timing_plan is not None:
            local_budget = repeat.get("local_runtime_budget") or {}
            if (
                not isinstance(local_budget, dict)
                or local_budget.get("timing_plan")
                != expected_timing_plan.snapshot()
                or local_budget.get("timing_plan_digest")
                != expected_timing_plan.digest()
            ):
                issues.append(f"{prefix}_timing_plan_mismatch")

        artifact_execution = repeat.get("artifact_execution")
        expected_artifacts = {
            str((expected_execution_scope or {}).get("candidate_label") or ""): str(
                (expected_execution_scope or {}).get("candidate_artifact_hash") or ""
            ),
            str((expected_execution_scope or {}).get("control_id") or ""): str(
                (expected_execution_scope or {}).get("control_artifact_hash") or ""
            ),
        }
        try:
            from national_native import _artifact_execution_is_valid

            artifact_valid = (
                all(expected_artifacts)
                and all(expected_artifacts.values())
                and _artifact_execution_is_valid(
                    artifact_execution,
                    expected_artifacts,
                )
            )
        except Exception as exc:
            artifact_valid = False
            issues.append(
                f"{prefix}_artifact_execution_validation_error:{type(exc).__name__}"
            )
        if not artifact_valid:
            issues.append(f"{prefix}_artifact_execution_invalid")
        repeat_artifacts.append(
            artifact_execution if isinstance(artifact_execution, dict) else {}
        )
        if "raw" in repeat:
            issues.append(f"{prefix}_raw_execution_embedded")
        try:
            from first_strict_execution_journal import (
                read_control_execution_receipt,
            )

            execution_evidence, execution_issues = read_control_execution_receipt(
                repeat.get("execution_receipt"),
                expected_scope=expected_execution_scope,
            )
        except Exception as exc:
            execution_evidence = None
            execution_issues = [
                f"first_strict_execution_authority_error:{type(exc).__name__}"
            ]
        issues.extend(f"{prefix}_{item}" for item in execution_issues)
        if isinstance(execution_evidence, dict):
            execution_input = execution_evidence.get("input") or {}
            execution_result = execution_evidence.get("result") or {}
            execution = execution_evidence.get("execution") or {}
            execution_scope = execution_evidence.get("scope") or {}
            observed_execution_scopes.append(execution_scope)
            execution_receipt_ids.append(str(
                execution_result.get("receipt_id") or ""
            ))
            execution_match_run_ids.append(str(
                execution_result.get("match_run_id") or ""
            ))
            expected_execution_input = {
                "repeat": index,
                "deck_seed_base": deck_seed,
                "bot_seed_base": bot_seed,
                "hands": 70,
            }
            for field, expected in expected_execution_input.items():
                if execution_input.get(field) != expected:
                    issues.append(f"{prefix}_execution_{field}_mismatch")
            if expected_timing_plan is not None and (
                execution_input.get("timing_plan")
                != expected_timing_plan.snapshot()
                or execution_input.get("timing_plan_digest")
                != expected_timing_plan.digest()
            ):
                issues.append(f"{prefix}_execution_timing_plan_mismatch")
            if execution.get("hands_played") != 70:
                issues.append(f"{prefix}_execution_hands_played_mismatch")
            if execution.get("net_chips_a") != net:
                issues.append(f"{prefix}_execution_net_chips_mismatch")
            if execution.get("passed_compliance") is not True:
                issues.append(f"{prefix}_execution_compliance_mismatch")
            if expected_timing_plan is not None:
                try:
                    from national_native import validate_native_match_timing_evidence

                    timing_issues = validate_native_match_timing_evidence(
                        execution,
                        timing_plan=expected_timing_plan,
                    )
                except Exception:
                    timing_issues = ["native_match_timing_evidence_validator_failed"]
                issues.extend(
                    f"{prefix}_execution_{item}" for item in timing_issues
                )

    if repeat_net_chips != net_chips:
        issues.append("first_strict_control_repeat_net_chips_mismatch")
    if matchup.get("artifact_executions") != repeat_artifacts:
        issues.append("first_strict_control_matchup_artifact_executions_mismatch")
    if len(observed_sample_plan) != len({
        (
            item.get("opponent"),
            item.get("repeat"),
            item.get("deck_seed_base"),
            item.get("bot_seed_base"),
        )
        for item in observed_sample_plan
    }):
        issues.append("first_strict_control_observed_sample_duplicate")
    if len(execution_receipt_ids) != len(repeats) or len(
        set(execution_receipt_ids)
    ) != len(execution_receipt_ids):
        issues.append("first_strict_control_execution_receipt_duplicate")
    if len(execution_match_run_ids) != len(repeats) or len(
        set(execution_match_run_ids)
    ) != len(execution_match_run_ids):
        issues.append("first_strict_control_execution_run_duplicate")
    if len(observed_execution_scopes) != len(repeats) or any(
        scope != observed_execution_scopes[0]
        for scope in observed_execution_scopes[1:]
    ):
        issues.append("first_strict_control_execution_scope_mismatch")

    summary = _control_gate_summary(net_chips)
    declared_wld = {
        "wins": summary["wins"],
        "losses": summary["losses"],
        "draws": summary["draws"],
    }
    for field, expected in declared_wld.items():
        if matchup.get(field) != expected:
            issues.append(f"first_strict_control_matchup_{field}_sign_mismatch")

    normalized_expected: list[dict[str, Any]] | None = None
    if expected_sample_plan is not None:
        plan_issues, normalized_expected = _validate_expected_sample_plan(
            expected_sample_plan
        )
        issues.extend(plan_issues)
        if expected_timing_plan is not None and any(
            item.get("native_match_timing_plan_digest")
            != expected_timing_plan.digest()
            for item in normalized_expected
        ):
            issues.append("first_strict_control_sample_plan_timing_plan_mismatch")
        if observed_sample_plan != normalized_expected:
            issues.append("first_strict_control_observed_sample_plan_mismatch")

    if envelope is not None:
        if envelope.get("evaluation_protocol") != "national_native_tcp":
            issues.append("first_strict_control_result_protocol_mismatch")
        if envelope.get("passed") is not True:
            issues.append("first_strict_control_result_passed_mismatch")
        if envelope.get("blockers") != []:
            issues.append("first_strict_control_result_blockers_not_empty")
        resolved = envelope.get("opponents")
        if not isinstance(resolved, list) or len(resolved) != 1 or not isinstance(
            resolved[0] if isinstance(resolved, list) and resolved else None,
            dict,
        ):
            issues.append("first_strict_control_result_opponents_invalid")
        else:
            opponent = resolved[0]
            expected_opponent_values = {
                "name": CONTROL_ID,
                "reason": CONTROL_REASON,
                "runtime_mode": "system_first_strict_control",
            }
            for field, expected in expected_opponent_values.items():
                if opponent.get(field) != expected:
                    issues.append(
                        f"first_strict_control_result_opponent_{field}_mismatch"
                    )
            expected_opponent_flags = {
                "precommit_gate_admitted": True,
                "strength_admitted": False,
                "rating_eligible": False,
                "official_opponent_eligible": False,
            }
            for field, expected in expected_opponent_flags.items():
                if opponent.get(field) is not expected:
                    issues.append(
                        f"first_strict_control_result_opponent_{field}_mismatch"
                    )
        for field, expected in declared_wld.items():
            if envelope.get(f"total_{field}") != expected:
                issues.append(f"first_strict_control_result_total_{field}_mismatch")
        if envelope.get("aggregate_net_chips") != net_chips:
            issues.append("first_strict_control_result_aggregate_net_chips_mismatch")
        if expected_timing_plan is not None and (
            envelope.get("native_match_timing_plan")
            != expected_timing_plan.snapshot()
            or envelope.get("native_match_timing_plan_digest")
            != expected_timing_plan.digest()
        ):
            issues.append("first_strict_control_result_timing_plan_mismatch")
        observed_execution_scope = (
            observed_execution_scopes[0]
            if len(observed_execution_scopes) == len(repeats)
            and observed_execution_scopes
            else None
        )
        if envelope.get("control_execution_scope") != observed_execution_scope:
            issues.append("first_strict_control_result_execution_scope_mismatch")
        if expected_sample_plan is not None and envelope.get(
            "sample_plan"
        ) != normalized_expected:
            issues.append("first_strict_control_result_sample_plan_mismatch")
        elif expected_sample_plan is None and envelope.get("sample_plan") not in (
            None,
            [],
            observed_sample_plan,
        ):
            issues.append("first_strict_control_result_sample_plan_mismatch")
        paired = envelope.get("paired_bootstrap")
        if not isinstance(paired, dict):
            issues.append("first_strict_control_paired_bootstrap_missing")
        else:
            expected_paired = {
                "protocol": "national_native_tcp",
                "hands_per_match": 70,
                "matches_per_opponent": exact_samples,
                "net_chips_samples": exact_samples,
                "strength_net_chips_samples": 0,
            }
            for field, expected in expected_paired.items():
                if paired.get(field) != expected:
                    issues.append(
                        f"first_strict_control_paired_{field}_mismatch"
                    )
            if paired.get("first_strict_control_gate") != summary:
                issues.append("first_strict_control_paired_gate_summary_mismatch")

    if outer is not None and envelope is not outer:
        if outer.get("matchups") != envelope.get("matchups"):
            issues.append("first_strict_control_outer_matchups_mismatch")
        for field in ("wins", "losses", "draws"):
            if outer.get(f"total_{field}") != envelope.get(f"total_{field}"):
                issues.append(f"first_strict_control_outer_total_{field}_mismatch")
        expected_outer_flags = {
            "precommit_gate_admitted": True,
            "strength_admitted": False,
            "rating_eligible": False,
            "official_opponent_eligible": False,
        }
        for field, expected in expected_outer_flags.items():
            if outer.get(field) is not expected:
                issues.append(f"first_strict_control_outer_{field}_mismatch")
        if outer.get("first_strict_control_gate") != summary:
            issues.append("first_strict_control_outer_gate_summary_mismatch")
        if outer.get("control_execution_scope") != envelope.get(
            "control_execution_scope"
        ):
            issues.append("first_strict_control_outer_execution_scope_mismatch")
        strength_order = outer.get("strength_order")
        if not isinstance(strength_order, dict) or strength_order.get("samples") != 0:
            issues.append("first_strict_control_outer_strength_samples_nonzero")
        gate_order = outer.get("precommit_gate_order")
        if not isinstance(gate_order, dict) or any((
            gate_order.get("samples") != exact_samples,
            gate_order.get("positive_matches") != summary["wins"],
            gate_order.get("negative_matches") != summary["losses"],
            gate_order.get("zero_matches") != summary["draws"],
        )):
            issues.append("first_strict_control_outer_gate_order_mismatch")

    issues.extend(_nested_non_authority_issues(result_or_matchups))
    issues.extend(_embedded_execution_issues(result_or_matchups))
    return list(dict.fromkeys(issues)), summary


def control_gate_blockers(
    result_or_matchups: Any,
    *,
    expected_sample_plan: list[dict[str, Any]] | None = None,
    expected_execution_scope: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply the exact, content-bound, non-strength admission floor."""

    payload = (
        result_or_matchups
        if isinstance(result_or_matchups, dict)
        else list(result_or_matchups)
    )
    validation_issues, summary = validate_control_result(
        payload,
        expected_sample_plan=expected_sample_plan,
        expected_execution_scope=expected_execution_scope,
    )
    blockers: list[dict[str, Any]] = []
    if validation_issues:
        blockers.append({
            "reason": "first_strict_control_result_invalid",
            "details": ";".join(validation_issues[:20]),
        })
    exact_samples = int(summary["exact_samples"])
    minimum_match_score = float(summary["minimum_match_score"])
    if summary["samples"] != exact_samples:
        blockers.append({
            "reason": (
                "first_strict_control_sample_shortfall"
                if summary["samples"] < exact_samples
                else "first_strict_control_sample_count_excess"
            ),
            "details": (
                f"Control admission requires exactly {exact_samples} "
                f"complete 70-hand samples; observed {summary['samples']}."
            ),
        })
    score = summary.get("primary_match_score")
    if score is None or score < minimum_match_score:
        blockers.append({
            "reason": "first_strict_control_score_below_floor",
            "details": (
                f"Control admission score {score!r} is below the content-bound "
                f"floor {minimum_match_score:.3f} over complete 70-hand W/L/D."
            ),
        })
    return blockers, summary


__all__ = [
    "ASSET_DIR",
    "CONTROL_AUTHORITY",
    "CONTROL_EXACT_SAMPLES",
    "CONTROL_GATE_PROFILE_ID",
    "CONTROL_ID",
    "CONTROL_MIN_MATCH_SCORE",
    "CONTROL_MIN_SAMPLES",
    "CONTROL_REASON",
    "FirstStrictControlError",
    "build_control_receipt",
    "control_identity",
    "control_gate_contract",
    "control_gate_blockers",
    "load_control_manifest",
    "materialize_control",
    "opponent_from_receipt",
    "validate_control_package",
    "validate_control_receipt",
    "validate_control_result",
    "validate_materialized_control",
]
