"""Content identity helpers for published national bots."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from bot_namespace import bot_tag, parse_bot_version


ROOT = Path(__file__).resolve().parents[2]

ARTIFACT_MANIFEST_SCHEMA = 1
_EXCLUDED_DIRECTORY_NAMES = frozenset({"__pycache__"})
_EXCLUDED_FILE_NAMES = frozenset({".completed"})
_EXCLUDED_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})


class ArtifactIntegrityError(RuntimeError):
    """Raised when a bot artifact cannot be identified without ambiguity."""

    def __init__(self, root: Path, offending_path: Path, reason: str) -> None:
        self.root = root
        self.offending_path = offending_path
        self.reason = reason
        try:
            display_path = offending_path.relative_to(root).as_posix() or "."
        except ValueError:
            display_path = str(offending_path)
        super().__init__(
            f"invalid bot artifact {root}: {reason} at {display_path}"
        )


def _absolute_without_resolving(path_or_token: str | Path) -> Path:
    """Return an absolute path without following a possibly unsafe symlink."""
    return Path(os.path.abspath(os.fspath(Path(path_or_token).expanduser())))


def _entry_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _raise_invalid(root: Path, path: Path, reason: str) -> None:
    raise ArtifactIntegrityError(root, path, reason)


def _read_file_digest(root: Path, path: Path, expected: os.stat_result) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _raise_invalid(root, path, f"cannot safely open regular file ({exc})")

    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            _raise_invalid(root, path, "artifact file is not regular")
        if _entry_identity(opened) != _entry_identity(expected):
            _raise_invalid(root, path, "artifact changed while hashing")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        finished = os.fstat(descriptor)
        if _entry_identity(finished) != _entry_identity(opened):
            _raise_invalid(root, path, "artifact changed while hashing")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _is_excluded_file(path: Path) -> bool:
    return (
        path.name in _EXCLUDED_FILE_NAMES
        or path.suffix.lower() in _EXCLUDED_FILE_SUFFIXES
    )


def _scan_directory(
    root: Path,
    directory: Path,
    entries: list[dict[str, Any]],
    *,
    excluded: bool = False,
) -> None:
    try:
        children = sorted(directory.iterdir(), key=lambda item: os.fsencode(item.name))
    except OSError as exc:
        _raise_invalid(root, directory, f"cannot enumerate directory ({exc})")

    for child in children:
        try:
            metadata = child.lstat()
        except OSError as exc:
            _raise_invalid(root, child, f"cannot inspect artifact entry ({exc})")

        if stat.S_ISLNK(metadata.st_mode):
            _raise_invalid(root, child, "symbolic links are forbidden")

        relative = child.relative_to(root).as_posix()
        if stat.S_ISDIR(metadata.st_mode):
            child_excluded = excluded or child.name in _EXCLUDED_DIRECTORY_NAMES
            if not child_excluded:
                entries.append({"path": relative, "type": "directory"})
            # Excluded cache trees are still inspected so a symlink cannot hide in one.
            _scan_directory(root, child, entries, excluded=child_excluded)
            continue

        if stat.S_ISREG(metadata.st_mode):
            if excluded or _is_excluded_file(child):
                continue
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "size": metadata.st_size,
                    "sha256": _read_file_digest(root, child, metadata),
                }
            )
            continue

        _raise_invalid(root, child, "only regular files and directories are allowed")


def artifact_manifest(path_or_token: str | Path) -> dict[str, Any]:
    """Build a deterministic manifest covering every executable artifact entry."""
    path = _absolute_without_resolving(path_or_token)
    try:
        metadata = path.lstat()
    except OSError as exc:
        _raise_invalid(path, path, f"artifact path is unavailable ({exc})")

    if stat.S_ISLNK(metadata.st_mode):
        _raise_invalid(path, path, "symbolic links are forbidden")

    if stat.S_ISREG(metadata.st_mode):
        if _is_excluded_file(path):
            _raise_invalid(path, path, "artifact root cannot be a runtime cache file")
        entries = [
            {
                "path": path.name,
                "type": "file",
                "size": metadata.st_size,
                "sha256": _read_file_digest(path, path, metadata),
            }
        ]
        artifact_type = "file"
    elif stat.S_ISDIR(metadata.st_mode):
        entries = [{"path": ".", "type": "directory"}]
        _scan_directory(path, path, entries)
        artifact_type = "directory"
    else:
        _raise_invalid(path, path, "artifact root must be a regular file or directory")

    return {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA,
        "artifact_type": artifact_type,
        "entries": entries,
    }


def hash_path(path_or_token: str | Path) -> str:
    """Hash the deterministic manifest for a valid bot artifact."""
    return canonical_digest(artifact_manifest(path_or_token))


def canonical_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "surrogateescape")
    return hashlib.sha256(raw).hexdigest()


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _tag_metadata(tag: str) -> dict[str, str]:
    result = _git("for-each-ref", "--format=%(contents)", f"refs/tags/{tag}")
    metadata: dict[str, str] = {}
    if result.returncode != 0:
        return metadata
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        normalized = key.strip().lower().replace("_", "-")
        if separator and normalized.startswith("official-"):
            metadata[normalized] = value.strip()
    return metadata


def published_bot_identity(path_or_token: str | Path) -> dict[str, Any]:
    """Resolve the immutable Git publication identity for a national bot."""
    path = _absolute_without_resolving(path_or_token)
    version = parse_bot_version(path.name)
    issues: list[str] = []
    try:
        relative = path.relative_to(ROOT)
    except ValueError:
        relative = None
        issues.append("bot_path_outside_project")

    tag = bot_tag(version) if version is not None else ""
    tag_type = ""
    tag_object = ""
    commit_oid = ""
    completion_tree_oid = ""
    current_tree_oid = ""
    main_commit_oid = ""
    main_tree_oid = ""
    tag_commit_on_main = False
    completion_tree_matches_main = False
    tracked_tree_matches = False
    working_tree_matches_main = False
    untracked_files: list[str] = []
    if version is None:
        issues.append("invalid_national_bot_label")
    elif relative is not None:
        ref = f"refs/tags/{tag}"
        type_result = _git("cat-file", "-t", ref)
        tag_type = type_result.stdout.strip() if type_result.returncode == 0 else ""
        if tag_type != "tag":
            issues.append("missing_annotated_completion_tag")
        object_result = _git("rev-parse", ref)
        tag_object = object_result.stdout.strip() if object_result.returncode == 0 else ""
        if len(tag_object) != 40:
            issues.append("completion_tag_object_invalid")
        commit_result = _git("rev-list", "-n", "1", tag)
        commit_oid = commit_result.stdout.strip() if commit_result.returncode == 0 else ""
        if len(commit_oid) != 40:
            issues.append("completion_tag_commit_invalid")
        tree_result = _git("rev-parse", f"{tag}^{{commit}}:{relative.as_posix()}")
        completion_tree_oid = tree_result.stdout.strip() if tree_result.returncode == 0 else ""
        current_tree_result = _git("rev-parse", f"HEAD:{relative.as_posix()}")
        current_tree_oid = (
            current_tree_result.stdout.strip()
            if current_tree_result.returncode == 0
            else ""
        )
        main_commit_result = _git(
            "rev-parse",
            "--verify",
            "refs/heads/main^{commit}",
        )
        main_commit_oid = (
            main_commit_result.stdout.strip()
            if main_commit_result.returncode == 0
            else ""
        )
        if not main_commit_oid:
            issues.append("main_branch_commit_unavailable")
        else:
            tag_commit_on_main = bool(
                commit_oid
                and _git(
                    "merge-base",
                    "--is-ancestor",
                    commit_oid,
                    main_commit_oid,
                ).returncode
                == 0
            )
            if commit_oid and not tag_commit_on_main:
                issues.append("completion_tag_commit_not_on_main")
            main_tree_result = _git(
                "rev-parse",
                f"{main_commit_oid}:{relative.as_posix()}",
            )
            main_tree_oid = (
                main_tree_result.stdout.strip()
                if main_tree_result.returncode == 0
                else ""
            )
            if not main_tree_oid:
                issues.append("main_missing_bot_tree")
            completion_tree_matches_main = bool(
                completion_tree_oid
                and main_tree_oid
                and completion_tree_oid == main_tree_oid
            )
            if completion_tree_oid and main_tree_oid and not completion_tree_matches_main:
                issues.append("completion_tag_bot_tree_differs_from_main")
            working_main_diff = _git(
                "diff",
                "--quiet",
                main_commit_oid,
                "--",
                relative.as_posix(),
            )
            working_tree_matches_main = working_main_diff.returncode == 0
            if not working_tree_matches_main:
                issues.append("working_bot_differs_from_main")
        if not commit_oid or not completion_tree_oid:
            issues.append("completion_tag_missing_bot_tree")
        if not current_tree_oid:
            issues.append("current_head_missing_bot_tree")
        diff_result = _git("diff", "--quiet", "HEAD", "--", relative.as_posix())
        tracked_tree_matches = diff_result.returncode == 0
        if not tracked_tree_matches:
            issues.append("working_bot_differs_from_current_head")
        untracked_result = _git(
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            relative.as_posix(),
        )
        if untracked_result.returncode == 0:
            untracked_files = [line for line in untracked_result.stdout.splitlines() if line]
        if untracked_files:
            issues.append("working_bot_has_untracked_files")

    artifact_hash = hash_path(path)
    published = bool(
        path.is_dir()
        and tag_type == "tag"
        and len(tag_object) == 40
        and len(commit_oid) == 40
        and commit_oid
        and completion_tree_oid
        and current_tree_oid
        and main_commit_oid
        and main_tree_oid
        and tag_commit_on_main
        and completion_tree_matches_main
        and tracked_tree_matches
        and working_tree_matches_main
        and not untracked_files
    )
    return {
        "label": path.name,
        "version": version,
        "path": str(path),
        "artifact_hash": artifact_hash,
        "tag": tag,
        "tag_type": tag_type,
        "tag_object": tag_object,
        "commit_oid": commit_oid,
        "completion_tree_oid": completion_tree_oid,
        "current_tree_oid": current_tree_oid,
        "main_commit_oid": main_commit_oid,
        "main_tree_oid": main_tree_oid,
        "tag_commit_on_main": tag_commit_on_main,
        "completion_tree_matches_main": completion_tree_matches_main,
        "working_tree_matches_main": working_tree_matches_main,
        "migrated_since_completion": bool(
            completion_tree_oid
            and current_tree_oid
            and completion_tree_oid != current_tree_oid
        ),
        "tag_metadata": _tag_metadata(tag) if tag else {},
        "published": published,
        "issues": list(dict.fromkeys(issues)),
    }


def validate_completion_tag(
    path_or_token: str | Path,
    *,
    expected_metadata: dict[str, str],
    certificate_path: str,
) -> dict[str, Any]:
    """Validate the exact annotated publication tag used by commit recovery."""
    identity = published_bot_identity(path_or_token)
    issues = list(identity.get("issues") or [])
    tag = str(identity.get("tag") or "")
    if identity.get("tag_type") != "tag":
        issues.append("completion_tag_not_annotated")
    tag_object = str(identity.get("tag_object") or "")
    if len(tag_object) != 40:
        issues.append("completion_tag_object_invalid")
    if identity.get("migrated_since_completion"):
        issues.append("completion_tag_bot_tree_differs_from_head")
    expected_hash = str(expected_metadata.get("official-candidate-hash") or "")
    if not expected_hash or identity.get("artifact_hash") != expected_hash:
        issues.append("completion_tag_candidate_hash_mismatch")

    contents_result = _git(
        "for-each-ref",
        "--format=%(contents)",
        f"refs/tags/{tag}",
    )
    metadata_values: dict[str, list[str]] = {}
    if contents_result.returncode != 0:
        issues.append("completion_tag_contents_unavailable")
    else:
        for line in contents_result.stdout.splitlines():
            key, separator, value = line.partition(":")
            normalized = key.strip().lower().replace("_", "-")
            if separator and normalized.startswith("official-"):
                metadata_values.setdefault(normalized, []).append(value.strip())
    for key, expected in expected_metadata.items():
        if metadata_values.get(key, []) != [str(expected)]:
            issues.append(f"completion_tag_metadata_mismatch:{key}")

    commit_oid = str(identity.get("commit_oid") or "")
    if not commit_oid:
        issues.append("completion_tag_commit_missing")
    elif _git("merge-base", "--is-ancestor", commit_oid, "main").returncode != 0:
        issues.append("completion_tag_commit_not_on_main")
    if certificate_path:
        listed = _git(
            "ls-tree",
            "-r",
            "--name-only",
            f"{tag}^{{commit}}",
            "--",
            certificate_path,
        )
        if listed.returncode != 0 or listed.stdout.strip() != certificate_path:
            issues.append("completion_tag_certificate_missing")
        if _git("diff", "--quiet", tag, "--", certificate_path).returncode != 0:
            issues.append("completion_tag_certificate_differs_from_head")
    return {
        "valid": not issues,
        "issues": list(dict.fromkeys(issues)),
        "identity": identity,
    }
