"""Complete-tree content binding for deployable research artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

from .constants import CONTRACT_VERSION


REQUIRED_BINDINGS = (
    "entrypoint",
    "config_digest",
    "dependency_digest",
    "resource_profile_digest",
    "oracle_fixture_digest",
    "action_set_digest",
    "build_command",
    "run_command",
)
_DIGEST_BINDINGS = {
    "config_digest",
    "dependency_digest",
    "resource_profile_digest",
    "oracle_fixture_digest",
    "action_set_digest",
}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_bindings(bindings: Mapping[str, Any]) -> dict[str, str]:
    if set(bindings) != set(REQUIRED_BINDINGS):
        missing = sorted(set(REQUIRED_BINDINGS) - set(bindings))
        extra = sorted(set(bindings) - set(REQUIRED_BINDINGS))
        raise ValueError(f"tree binding keys differ: missing={missing}, extra={extra}")
    normalized = {name: str(bindings[name]) for name in REQUIRED_BINDINGS}
    if any(not value for value in normalized.values()):
        raise ValueError("tree bindings cannot be empty")
    for name in _DIGEST_BINDINGS:
        try:
            value = bytes.fromhex(normalized[name])
        except ValueError as exc:
            raise ValueError(f"{name} is not hexadecimal") from exc
        if len(value) != 32:
            raise ValueError(f"{name} must be a 32-byte digest")
    entrypoint = Path(normalized["entrypoint"])
    if entrypoint.is_absolute() or ".." in entrypoint.parts:
        raise ValueError("entrypoint must be a safe relative path")
    normalized["entrypoint"] = entrypoint.as_posix()
    return normalized


def _reject_xattrs(path: Path, relative: str) -> None:
    try:
        xattrs = os.listxattr(path, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"cannot audit xattrs for {relative}") from exc
    if xattrs:
        raise ValueError(f"extended attributes are forbidden: {relative}: {sorted(xattrs)}")


def _sealed_root(root: str | Path) -> Path:
    candidate = Path(root)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise ValueError("sealed root must be an existing real directory") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("sealed root must be a real directory, not a symlink")
    _reject_xattrs(candidate, ".")
    return candidate.resolve(strict=True)


def _scan_complete_tree(
    root: Path,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int | str]]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("sealed root must be a real directory")
    root_metadata = root.lstat()
    _reject_xattrs(root, ".")
    directories: dict[str, dict[str, int]] = {
        ".": {"mode": stat.S_IMODE(root_metadata.st_mode)}
    }
    entries: dict[str, dict[str, int | str]] = {}

    def fail_walk(error: OSError) -> None:
        raise ValueError(f"cannot traverse sealed tree: {error.filename}") from error

    for directory, dirnames, filenames in os.walk(
        root, followlinks=False, onerror=fail_walk
    ):
        dirnames.sort()
        filenames.sort()
        directory_path = Path(directory)
        for name in tuple(dirnames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if name == ".git":
                raise ValueError("nested git metadata is forbidden in sealed artifacts")
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"symlink directory is forbidden: {relative}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"non-directory tree node is forbidden: {relative}")
            _reject_xattrs(path, relative)
            directories[relative] = {"mode": stat.S_IMODE(metadata.st_mode)}
        for name in filenames:
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if name == ".git":
                raise ValueError("nested git metadata is forbidden in sealed artifacts")
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"only regular non-symlink files may be sealed: {relative}")
            _reject_xattrs(path, relative)
            entries[relative] = {
                "sha256": sha256_file(path),
                "bytes": metadata.st_size,
                "mode": stat.S_IMODE(metadata.st_mode),
            }
    return dict(sorted(directories.items())), dict(sorted(entries.items()))


def build_tree_manifest(root: str | Path, bindings: Mapping[str, Any]) -> dict[str, Any]:
    root_path = _sealed_root(root)
    normalized_bindings = _validate_bindings(bindings)
    directories, files = _scan_complete_tree(root_path)
    if normalized_bindings["entrypoint"] not in files:
        raise ValueError("bound entrypoint is not a sealed regular file")
    body = {
        "schema_version": 2,
        "contract_version": CONTRACT_VERSION,
        "bindings": normalized_bindings,
        "directories": directories,
        "files": files,
    }
    body["tree_digest"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return body


def verify_tree_manifest(root: str | Path, manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema_version") != 2:
        raise ValueError("tree manifest schema mismatch")
    if manifest.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("tree manifest contract version mismatch")
    expected_bindings = _validate_bindings(manifest.get("bindings", {}))
    expected_directories = manifest.get("directories")
    if not isinstance(expected_directories, Mapping):
        raise ValueError("tree manifest directories mapping missing")
    expected_files = manifest.get("files")
    if not isinstance(expected_files, Mapping):
        raise ValueError("tree manifest files mapping missing")
    actual = build_tree_manifest(root, expected_bindings)
    if actual["directories"] != dict(expected_directories):
        expected_names = set(expected_directories)
        actual_names = set(actual["directories"])
        raise ValueError(
            "sealed directory tree mismatch: "
            f"missing={sorted(expected_names - actual_names)}, "
            f"extra={sorted(actual_names - expected_names)}, "
            f"changed={sorted(name for name in expected_names & actual_names if expected_directories[name] != actual['directories'][name])}"
        )
    if actual["files"] != dict(expected_files):
        expected_names = set(expected_files)
        actual_names = set(actual["files"])
        raise ValueError(
            "sealed tree mismatch: "
            f"missing={sorted(expected_names - actual_names)}, "
            f"extra={sorted(actual_names - expected_names)}, "
            f"changed={sorted(name for name in expected_names & actual_names if expected_files[name] != actual['files'][name])}"
        )
    if actual["tree_digest"] != manifest.get("tree_digest"):
        raise ValueError("tree digest or frozen bindings mismatch")
