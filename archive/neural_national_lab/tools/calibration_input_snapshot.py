"""Stable file receipts and no-clobber publication for v4 calibration."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def read_regular_snapshot(
    path: Path, *, field: str
) -> tuple[bytes, dict[str, Any]]:
    """Read one regular file through one fd and bind the returned bytes."""
    source = path.resolve()
    if path.is_symlink():
        raise ValueError(f"{field} must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ValueError(f"cannot read {field}: {source}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{field} is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity_before = (
        before.st_dev, before.st_ino, before.st_size,
        before.st_mtime_ns, before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev, after.st_ino, after.st_size,
        after.st_mtime_ns, after.st_ctime_ns,
    )
    raw = b"".join(chunks)
    if identity_before != identity_after or len(raw) != before.st_size:
        raise ValueError(f"{field} changed while it was read")
    return raw, {
        "path": str(source), "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def json_snapshot(
    path: Path, *, field: str
) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    raw, receipt = read_regular_snapshot(path, field=field)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {field}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{field} must be an object")
    return raw, payload, receipt


def verify_file_receipt(receipt: dict[str, Any], *, field: str) -> None:
    _raw, current = read_regular_snapshot(
        Path(str(receipt.get("path") or "")), field=field
    )
    if current != receipt:
        raise RuntimeError(f"{field} changed after startup")


def member_input_snapshot(
    root: Path, *, expected_files: set[str], schema: str
) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("v4 member training directory is invalid")
    expected = sorted({*expected_files, "artifact_manifest.json"})
    entries = list(root.iterdir())
    if (
        sorted(path.name for path in entries) != expected
        or any(path.is_symlink() or not path.is_file() for path in entries)
    ):
        raise ValueError("v4 member training directory has unexpected artifacts")
    raw_files: dict[str, bytes] = {}
    receipts: dict[str, dict[str, Any]] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for name in expected:
        raw, receipt = read_regular_snapshot(root / name, field=f"v4 member {name}")
        raw_files[name], receipts[name] = raw, receipt
        if name.endswith(".json"):
            try:
                payload = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid v4 member artifact: {name}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"v4 member artifact must be an object: {name}")
            payloads[name] = payload
    contracts = payloads["artifact_manifest.json"].get("files")
    if not isinstance(contracts, dict) or set(contracts) != expected_files:
        raise ValueError("v4 member artifact has an incomplete file contract")
    for name in expected_files:
        contract, receipt = contracts.get(name), receipts[name]
        if (
            not isinstance(contract, dict)
            or set(contract) != {"bytes", "sha256"}
            or contract.get("bytes") != receipt["bytes"]
            or contract.get("sha256") != receipt["sha256"]
        ):
            raise ValueError(f"v4 member artifact changed: {root / name}")
    receipt = {
        "schema": schema, "root": str(root), "entries": expected,
        "files": receipts,
    }
    return {"raw": raw_files, "payloads": payloads, "receipt": receipt}


def verify_member_receipt(
    receipt: dict[str, Any], *, expected_files: set[str], schema: str
) -> None:
    root = Path(str(receipt.get("root") or ""))
    current = member_input_snapshot(
        root, expected_files=expected_files, schema=schema
    )["receipt"]
    if current != receipt:
        raise RuntimeError(f"v4 member inputs changed after startup: {root}")


def build_ledger_state(
    snapshot: dict[str, Any], file_receipt: dict[str, Any], *,
    run_id: str, expected: list[dict[str, Any]],
) -> dict[str, Any]:
    events = [
        event for event in snapshot.get("events", [])
        if isinstance(event, dict) and event.get("run_id") == run_id
    ]
    if len(events) != len(expected):
        raise RuntimeError("v4 calibration ledger roles changed")
    for event, contract in zip(events, expected, strict=True):
        if any(event.get(key) != value for key, value in contract.items()):
            raise RuntimeError("v4 calibration ledger binding changed")
    return {"file": file_receipt, "run_id": run_id, "calibration_events": events}


def validate_current_ledger_state(
    recorded: dict[str, Any], current: dict[str, Any], *, stable_roles: set[str]
) -> None:
    run_id = recorded.get("run_id")
    current_events = [
        event for event in current.get("events", [])
        if isinstance(event, dict) and event.get("run_id") == run_id
        and event.get("role") in stable_roles
    ]
    if current_events != recorded.get("calibration_events"):
        raise ValueError("v4 calibration ledger evidence changed")


def build_receipts(schema: str, **sections: Any) -> dict[str, Any]:
    unsigned = {"schema": schema, **sections}
    return {**unsigned, "payload_sha256": canonical_sha256(unsigned)}


def validate_receipts(payload: Any, *, schema: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("v4 calibration input receipts are missing")
    unsigned = dict(payload)
    digest = str(unsigned.pop("payload_sha256", ""))
    if unsigned.get("schema") != schema or digest != canonical_sha256(unsigned):
        raise ValueError("v4 calibration input receipts changed")
    return payload


def _binding_error() -> ValueError:
    return ValueError("v4 calibration input receipt bindings are invalid")


_UNSET = object()


def _bind_file_receipt(
    receipt: Any, *, path: Path | str | object = _UNSET,
    size: int | object = _UNSET, digest: str | object = _UNSET,
) -> None:
    if not isinstance(receipt, dict) or set(receipt) != {
        "path", "bytes", "sha256",
    }:
        raise _binding_error()
    raw_path = receipt.get("path")
    count = receipt.get("bytes")
    sha256 = receipt.get("sha256")
    if (
        not isinstance(raw_path, str)
        or not Path(raw_path).is_absolute()
        or str(Path(raw_path).resolve()) != raw_path
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
        or (path is not _UNSET and raw_path != str(Path(path).resolve()))
        or (size is not _UNSET and count != size)
        or (digest is not _UNSET and sha256 != digest)
    ):
        raise _binding_error()


def validate_input_receipts_bindings(
    payload: dict[str, Any], *, ensemble: dict[str, Any],
    calibration: dict[str, Any], report: dict[str, Any],
    artifact: dict[str, Any], dataset: Any, run_id: str,
    roles: tuple[str, ...], member_files: set[str], member_schema: str,
) -> dict[str, Any]:
    """Bind every receipt to the exact published calibration inputs."""
    digest = payload.get("payload_sha256")
    if (
        not isinstance(digest, str)
        or calibration.get("input_receipts_sha256") != digest
        or report.get("input_receipts") != payload
        or report.get("input_receipts_sha256") != digest
        or artifact.get("input_receipts") != payload
        or artifact.get("input_receipts_sha256") != digest
    ):
        raise _binding_error()
    summary_path = report.get("scaling_summary")
    if not isinstance(summary_path, str) or not summary_path:
        raise _binding_error()
    _bind_file_receipt(
        payload.get("scaling_summary"), path=summary_path,
        digest=ensemble.get("scaling_summary_sha256"),
    )
    _bind_file_receipt(
        payload.get("role_manifest"), path=dataset.manifest_path,
        digest=dataset.manifest_sha256,
    )
    if report.get("role_manifest") != str(dataset.manifest_path):
        raise _binding_error()
    role_files = payload.get("role_files")
    expected_names = {
        f"{prefix}_{role}.jsonl"
        for role in roles for prefix in ("cf", "opponent_actions")
    }
    if not isinstance(role_files, dict) or set(role_files) != expected_names:
        raise _binding_error()
    for name in expected_names:
        expected = dataset.outputs[name]
        _bind_file_receipt(
            role_files[name], path=dataset.root / name,
            size=expected["bytes"], digest=expected["sha256"],
        )
    ledger = payload.get("exposure_ledger")
    if not isinstance(ledger, dict) or set(ledger) != {
        "file", "run_id", "calibration_events",
    } or ledger.get("run_id") != run_id:
        raise _binding_error()
    _bind_file_receipt(ledger["file"], path=dataset.ledger_path)
    events = ledger.get("calibration_events")
    if not isinstance(events, list) or len(events) != len(roles):
        raise _binding_error()
    previous_sequence = 0
    for event, role in zip(events, roles, strict=True):
        if not isinstance(event, dict) or set(event) != {
            "sequence", "timestamp_utc", "event", "role", "run_id",
            "opponents", "candidate_sha256", "artifact_sha256",
        }:
            raise _binding_error()
        sequence = event.get("sequence")
        if (
            isinstance(sequence, bool) or not isinstance(sequence, int)
            or sequence <= previous_sequence
            or not isinstance(event.get("timestamp_utc"), str)
            or not event["timestamp_utc"]
            or event.get("event") != "open"
            or event.get("role") != role
            or event.get("run_id") != run_id
            or event.get("opponents") != dataset.roles[role]
            or event.get("candidate_sha256") is not None
            or event.get("artifact_sha256")
            != dataset._role_artifact_sha256(role)
        ):
            raise _binding_error()
        previous_sequence = sequence
    members = ensemble.get("members")
    receipts = payload.get("member_inputs")
    if not isinstance(members, list) or not isinstance(receipts, list):
        raise _binding_error()
    expected_members = {}
    for member in members:
        if not isinstance(member, dict):
            raise _binding_error()
        output_dir = member.get("output_dir")
        if (
            not isinstance(output_dir, str) or not Path(output_dir).is_absolute()
            or str(Path(output_dir).resolve()) != output_dir
        ):
            raise _binding_error()
        root = output_dir
        if root in expected_members:
            raise _binding_error()
        expected_members[root] = member
    if len(receipts) != len(expected_members):
        raise _binding_error()
    expected_entries = sorted({*member_files, "artifact_manifest.json"})
    seen = set()
    digest_fields = {
        "checkpoint.pt": "checkpoint_sha256",
        "checkpoint_authorization.json": "checkpoint_authorization_sha256",
        "training_report.json": "training_report_sha256",
        "artifact_manifest.json": "training_artifact_manifest_sha256",
    }
    for receipt in receipts:
        if not isinstance(receipt, dict) or set(receipt) != {
            "schema", "root", "entries", "files",
        } or receipt.get("schema") != member_schema:
            raise _binding_error()
        root = receipt.get("root")
        if (
            not isinstance(root, str) or not Path(root).is_absolute()
            or str(Path(root).resolve()) != root
        ):
            raise _binding_error()
        member = expected_members.get(root)
        files = receipt.get("files")
        if (
            member is None or root in seen
            or receipt.get("entries") != expected_entries
            or not isinstance(files, dict) or set(files) != set(expected_entries)
        ):
            raise _binding_error()
        seen.add(root)
        for name in expected_entries:
            expected_digest = member.get(digest_fields[name])
            if not isinstance(expected_digest, str):
                raise _binding_error()
            _bind_file_receipt(
                files[name], path=Path(root) / name,
                digest=expected_digest,
            )
    if seen != set(expected_members):
        raise _binding_error()
    return payload


def fsync_flat_tree(root: Path, *, expected: set[str]) -> None:
    entries = list(root.iterdir())
    if (
        {path.name for path in entries} != expected
        or any(path.is_symlink() or not path.is_file() for path in entries)
    ):
        raise RuntimeError("v4 calibration staging layout changed")
    for path in entries:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_tree_noreplace(source: Path, destination: Path) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(f"output directory already exists: {destination}")
        raise OSError(error, os.strerror(error), str(destination))
    _fsync_directory(destination.parent)
