"""Deterministic content-addressed storage for official EXE evidence bundles."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
from typing import Any

from bot_artifact import canonical_digest


ARCHIVE_SCHEMA_VERSION = 1
MANIFEST_NAME = "OFFICIAL_EVIDENCE_MANIFEST.json"
DEFAULT_EVIDENCE_STORE = Path.home() / ".local" / "share" / "pok" / "official-evidence"
MAX_ARCHIVE_FILES = 5000
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


def evidence_store() -> Path:
    return Path(os.environ.get("POK_OFFICIAL_EVIDENCE_STORE", str(DEFAULT_EVIDENCE_STORE)))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files_under(root: Path) -> list[tuple[str, Path]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"official evidence root must be a regular directory: {root}")
    files: list[tuple[str, Path]] = []
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError(f"official evidence symlink is forbidden: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"official evidence entry is not a regular file: {path}")
        relative = path.relative_to(root).as_posix()
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or relative == MANIFEST_NAME:
            raise ValueError(f"unsafe official evidence path: {relative}")
        size = path.stat().st_size
        total += size
        if len(files) >= MAX_ARCHIVE_FILES:
            raise ValueError("official evidence archive exceeds file-count limit")
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("official evidence archive exceeds uncompressed-size limit")
        files.append((relative, path))
    if not files:
        raise ValueError("official evidence archive is empty")
    return files


def _manifest(files: list[tuple[str, Path]]) -> dict[str, Any]:
    entries = [
        {
            "path": relative,
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        for relative, path in files
    ]
    payload = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "file_count": len(entries),
        "total_bytes": sum(item["size_bytes"] for item in entries),
        "files": entries,
    }
    payload["manifest_digest"] = canonical_digest(payload)
    return payload


def _tar_bytes(files: list[tuple[str, Path]], manifest: dict[str, Any]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        manifest_bytes = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_bytes)
        info.mode = 0o644
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        archive.addfile(info, io.BytesIO(manifest_bytes))
        for relative, path in files:
            data = path.read_bytes()
            info = tarfile.TarInfo(relative)
            info.size = len(data)
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def build_evidence_archive(suite_dir: str | Path) -> dict[str, Any]:
    root = Path(suite_dir).expanduser().resolve()
    files = _files_under(root)
    manifest = _manifest(files)
    tar_bytes = _tar_bytes(files, manifest)
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0, compresslevel=9) as stream:
        stream.write(tar_bytes)
    archive_bytes = buffer.getvalue()
    archive_sha256 = _sha256_bytes(archive_bytes)
    store = evidence_store()
    store.mkdir(parents=True, exist_ok=True)
    destination = store / f"{archive_sha256}.tar.gz"
    if destination.exists():
        if _sha256_file(destination) != archive_sha256:
            raise RuntimeError(f"content-addressed evidence collision: {destination}")
    else:
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(archive_bytes)
        temporary.replace(destination)
    return {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "storage": "content-addressed-local",
        "path": str(destination),
        "archive_sha256": archive_sha256,
        "archive_size_bytes": len(archive_bytes),
        "manifest_digest": manifest["manifest_digest"],
        "file_count": manifest["file_count"],
        "total_bytes": manifest["total_bytes"],
        "evidence_sha256": next(
            (
                item["sha256"]
                for item in manifest["files"]
                if item.get("path") == "official_evidence.json"
            ),
            "",
        ),
    }


def _resolved_archive_path(receipt: dict[str, Any]) -> Path:
    digest = str(receipt.get("archive_sha256") or "")
    return evidence_store() / f"{digest}.tar.gz"


def _strict_nonnegative_int(value: Any, issue: str, issues: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        issues.append(issue)
        return None
    return value


def validate_evidence_archive_receipt(
    receipt: Any,
    *,
    expected_evidence_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate the portable content-addressed receipt without local bytes."""
    if not isinstance(receipt, dict):
        return {"valid": False, "issues": ["evidence_archive_receipt_missing"]}
    issues: list[str] = []
    if receipt.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
        issues.append("evidence_archive_receipt_schema_invalid")
    if receipt.get("storage") != "content-addressed-local":
        issues.append("evidence_archive_storage_invalid")
    digest_keys = ["archive_sha256", "manifest_digest"]
    if expected_evidence_sha256 is not None:
        digest_keys.append("evidence_sha256")
    for key in digest_keys:
        digest = str(receipt.get(key) or "")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.lower()):
            issues.append(f"evidence_archive_{key}_invalid")
    archive_size = _strict_nonnegative_int(
        receipt.get("archive_size_bytes"),
        "evidence_archive_receipt_size_invalid",
        issues,
    )
    file_count = _strict_nonnegative_int(
        receipt.get("file_count"),
        "evidence_archive_receipt_file_count_invalid",
        issues,
    )
    total_bytes = _strict_nonnegative_int(
        receipt.get("total_bytes"),
        "evidence_archive_receipt_total_bytes_invalid",
        issues,
    )
    if expected_evidence_sha256 is not None and archive_size == 0:
        issues.append("evidence_archive_receipt_empty")
    if expected_evidence_sha256 is not None and file_count == 0:
        issues.append("evidence_archive_receipt_no_files")
    if expected_evidence_sha256 is not None and total_bytes == 0:
        issues.append("evidence_archive_receipt_no_content")
    if (
        expected_evidence_sha256 is not None
        and receipt.get("evidence_sha256") != expected_evidence_sha256
    ):
        issues.append("evidence_archive_official_evidence_sha256_mismatch")
    return {
        "valid": not issues,
        "issues": list(dict.fromkeys(issues)),
        "archive_sha256": str(receipt.get("archive_sha256") or ""),
        "retained": False,
    }


def validate_evidence_archive(
    receipt: Any,
    *,
    expected_evidence_sha256: str | None = None,
) -> dict[str, Any]:
    receipt_validation = validate_evidence_archive_receipt(
        receipt,
        expected_evidence_sha256=expected_evidence_sha256,
    )
    if not isinstance(receipt, dict):
        return receipt_validation
    issues: list[str] = list(receipt_validation.get("issues") or [])
    digest = str(receipt.get("archive_sha256") or "")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.lower()):
        return {"valid": False, "issues": [*issues, "evidence_archive_digest_invalid"]}
    receipt_size = _strict_nonnegative_int(
        receipt.get("archive_size_bytes"),
        "evidence_archive_receipt_size_invalid",
        issues,
    )
    receipt_file_count = _strict_nonnegative_int(
        receipt.get("file_count"),
        "evidence_archive_receipt_file_count_invalid",
        issues,
    )
    receipt_total_bytes = _strict_nonnegative_int(
        receipt.get("total_bytes"),
        "evidence_archive_receipt_total_bytes_invalid",
        issues,
    )
    path = _resolved_archive_path(receipt)
    try:
        if path.is_symlink():
            issues.append("evidence_archive_symlink")
        if not path.is_file():
            return {
                "valid": False,
                "issues": [*issues, "evidence_archive_missing"],
                "path": str(path),
            }
        if _sha256_file(path) != digest:
            issues.append("evidence_archive_sha256_mismatch")
        if receipt_size is None or path.stat().st_size != receipt_size:
            issues.append("evidence_archive_size_mismatch")
    except OSError as exc:
        issues.append(f"evidence_archive_read_error:{type(exc).__name__}:{str(exc)[:160]}")
        return {"valid": False, "issues": issues, "path": str(path)}
    if issues:
        return {"valid": False, "issues": issues, "path": str(path)}

    seen: set[str] = set()
    manifest: dict[str, Any] | None = None
    extracted: dict[str, tuple[int, str]] = {}
    evidence_bundle_bytes: bytes | None = None
    total = 0
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive.getmembers():
                pure = PurePosixPath(member.name)
                if pure.is_absolute() or ".." in pure.parts or member.name in seen:
                    issues.append("evidence_archive_unsafe_member")
                    continue
                seen.add(member.name)
                if not member.isfile() or member.issym() or member.islnk():
                    issues.append("evidence_archive_non_regular_member")
                    continue
                total += int(member.size)
                if len(seen) > MAX_ARCHIVE_FILES + 1 or total > MAX_UNCOMPRESSED_BYTES:
                    issues.append("evidence_archive_expansion_limit")
                    break
                stream = archive.extractfile(member)
                data = stream.read() if stream is not None else b""
                if member.name == MANIFEST_NAME:
                    manifest = json.loads(data.decode("utf-8"))
                else:
                    extracted[member.name] = (len(data), _sha256_bytes(data))
                    if member.name == "official_evidence.json":
                        evidence_bundle_bytes = data
    except Exception as exc:
        issues.append(f"evidence_archive_read_error:{type(exc).__name__}:{str(exc)[:160]}")
    if not isinstance(manifest, dict):
        issues.append("evidence_archive_manifest_missing")
    else:
        manifest_payload = {key: value for key, value in manifest.items() if key != "manifest_digest"}
        try:
            computed_manifest_digest = canonical_digest(manifest_payload)
        except Exception as exc:
            computed_manifest_digest = ""
            issues.append(
                f"evidence_archive_manifest_digest_error:{type(exc).__name__}:{str(exc)[:120]}"
            )
        if manifest.get("schema_version") != ARCHIVE_SCHEMA_VERSION:
            issues.append("evidence_archive_manifest_schema_invalid")
        if manifest.get("manifest_digest") != computed_manifest_digest:
            issues.append("evidence_archive_manifest_digest_mismatch")
        if manifest.get("manifest_digest") != receipt.get("manifest_digest"):
            issues.append("evidence_archive_receipt_manifest_mismatch")
        expected: dict[str, tuple[int, str]] = {}
        manifest_files = manifest.get("files")
        if not isinstance(manifest_files, list):
            issues.append("evidence_archive_manifest_files_invalid")
            manifest_files = []
        for item in manifest_files:
            if not isinstance(item, dict):
                issues.append("evidence_archive_manifest_entry_invalid")
                continue
            name = str(item.get("path") or "")
            pure = PurePosixPath(name)
            if not name or pure.is_absolute() or ".." in pure.parts or name == MANIFEST_NAME:
                issues.append("evidence_archive_manifest_path_invalid")
                continue
            if name in expected:
                issues.append("evidence_archive_manifest_duplicate_path")
                continue
            entry_size = _strict_nonnegative_int(
                item.get("size_bytes"),
                "evidence_archive_manifest_entry_size_invalid",
                issues,
            )
            entry_digest = str(item.get("sha256") or "")
            if len(entry_digest) != 64 or any(
                ch not in "0123456789abcdef" for ch in entry_digest.lower()
            ):
                issues.append("evidence_archive_manifest_entry_digest_invalid")
            if entry_size is not None:
                expected[name] = (entry_size, entry_digest)
        if expected != extracted:
            issues.append("evidence_archive_file_manifest_mismatch")
        manifest_file_count = _strict_nonnegative_int(
            manifest.get("file_count"),
            "evidence_archive_manifest_file_count_invalid",
            issues,
        )
        manifest_total_bytes = _strict_nonnegative_int(
            manifest.get("total_bytes"),
            "evidence_archive_manifest_total_bytes_invalid",
            issues,
        )
        if manifest_file_count is None or len(expected) != manifest_file_count:
            issues.append("evidence_archive_file_count_mismatch")
        actual_total_bytes = sum(size for size, _digest in extracted.values())
        if manifest_total_bytes is None or actual_total_bytes != manifest_total_bytes:
            issues.append("evidence_archive_total_bytes_mismatch")
        if receipt_file_count is None or receipt_file_count != manifest_file_count:
            issues.append("evidence_archive_receipt_file_count_mismatch")
        if receipt_total_bytes is None or receipt_total_bytes != manifest_total_bytes:
            issues.append("evidence_archive_receipt_total_bytes_mismatch")
    if expected_evidence_sha256 is not None:
        evidence_entry = extracted.get("official_evidence.json")
        if evidence_entry is None:
            issues.append("evidence_archive_official_evidence_missing")
        elif evidence_entry[1] != str(expected_evidence_sha256):
            issues.append("evidence_archive_official_evidence_sha256_mismatch")
    if evidence_bundle_bytes is not None:
        try:
            evidence_bundle = json.loads(evidence_bundle_bytes.decode("utf-8"))
        except Exception as exc:
            evidence_bundle = None
            issues.append(
                f"evidence_archive_official_evidence_invalid:{type(exc).__name__}:{str(exc)[:120]}"
            )
        if isinstance(evidence_bundle, dict):
            artifact_count = 0
            for round_item in evidence_bundle.get("rounds") or []:
                if not isinstance(round_item, dict):
                    issues.append("evidence_archive_round_manifest_invalid")
                    continue
                artifacts = round_item.get("artifacts")
                if not isinstance(artifacts, dict):
                    issues.append("evidence_archive_round_artifacts_invalid")
                    continue
                for value in artifacts.values():
                    items = value if isinstance(value, list) else [value]
                    for item in items:
                        if not isinstance(item, dict) or not item.get("path"):
                            continue
                        artifact_count += 1
                        archive_path = str(item.get("archive_path") or "")
                        pure = PurePosixPath(archive_path)
                        if (
                            not archive_path
                            or pure.is_absolute()
                            or ".." in pure.parts
                            or archive_path == MANIFEST_NAME
                        ):
                            issues.append("evidence_archive_artifact_path_unbound")
                            continue
                        try:
                            expected_size = item.get("size_bytes")
                            if isinstance(expected_size, bool) or not isinstance(expected_size, int):
                                raise ValueError("size_bytes must be an integer")
                            expected_digest = str(item.get("sha256") or "")
                            expected_entry = (expected_size, expected_digest)
                        except Exception:
                            issues.append("evidence_archive_artifact_manifest_invalid")
                            continue
                        if extracted.get(archive_path) != expected_entry:
                            issues.append(
                                f"evidence_archive_artifact_mismatch:{archive_path}"
                            )
            if artifact_count == 0:
                issues.append("evidence_archive_artifact_manifest_missing")
    return {
        "valid": not issues,
        "issues": list(dict.fromkeys(issues)),
        "path": str(path),
        "archive_sha256": digest,
    }
