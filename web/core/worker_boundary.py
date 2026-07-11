"""Worker and candidate editable-boundary checks.

The LLM worker already receives an allowed write directory, but that only
prevents writes outside the candidate bot directory.  These checks enforce the
stronger contract that a worker may only change its declared target files and
explicitly allowed helper files.

Worker boundaries deliberately use the same artifact surface as
``bot_artifact``.  Decision assets are not necessarily Python source: a worker
may own a packed lookup table, model fragment, or another binary file.  The
legacy public helper names are retained because several pipeline modules import
them, but they now snapshot and restore every artifact file, byte-for-byte.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from bot_artifact import (
    ARTIFACT_MAX_DIRECTORY_DEPTH,
    ARTIFACT_MAX_ENTRY_COUNT,
    ARTIFACT_MAX_FILE_BYTES,
    ARTIFACT_MAX_FILE_COUNT,
    ARTIFACT_MAX_TOTAL_BYTES,
    _EXCLUDED_DIRECTORY_NAMES,
    _EXCLUDED_FILE_NAMES,
    _EXCLUDED_FILE_SUFFIXES,
    artifact_manifest,
)
from bot_namespace import bot_relpath


_BINARY_ARTIFACT_SUFFIXES = frozenset({
    ".bin", ".dat", ".npy", ".npz", ".onnx", ".pt", ".pth",
    ".pkl", ".pickle", ".db", ".sqlite", ".sqlite3",
})

# Worker rollback snapshots are intentionally bounded in memory. Runtime
# precompute contracts permit at most 8 MiB per artifact; 16 MiB per file and
# 64 MiB total leave room for multiple tables plus source while preventing a
# sparse/oversized file from forcing an unbounded bytearray allocation before
# quality gates run.
WORKER_SNAPSHOT_MAX_FILE_COUNT = ARTIFACT_MAX_FILE_COUNT
WORKER_SNAPSHOT_MAX_ENTRY_COUNT = ARTIFACT_MAX_ENTRY_COUNT
WORKER_SNAPSHOT_MAX_DIRECTORY_DEPTH = ARTIFACT_MAX_DIRECTORY_DEPTH
WORKER_SNAPSHOT_MAX_FILE_BYTES = ARTIFACT_MAX_FILE_BYTES
WORKER_SNAPSHOT_MAX_TOTAL_BYTES = ARTIFACT_MAX_TOTAL_BYTES


def is_binary_artifact_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _BINARY_ARTIFACT_SUFFIXES


def _entry_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


@dataclass(frozen=True)
class ArtifactSnapshotIssue:
    path: str
    reason: str


class ArtifactSnapshotError(RuntimeError):
    """The candidate contains entries that cannot be snapshotted safely."""

    def __init__(self, root: Path, issues: Iterable[ArtifactSnapshotIssue]):
        self.root = Path(root)
        self.issues = tuple(issues)
        self.violation_files = sorted({issue.path for issue in self.issues})
        detail = "; ".join(
            f"{issue.path}: {issue.reason}" for issue in self.issues[:5]
        )
        super().__init__(f"invalid bot artifact snapshot at {self.root}: {detail}")


class ArtifactFileSnapshot(dict[str, bytes]):
    """Byte snapshot plus the non-excluded directory shape of an artifact."""

    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        *,
        directories: Iterable[str] = (),
    ) -> None:
        super().__init__(files or {})
        self.directories = frozenset(directories)


@dataclass
class BoundaryAuditResult:
    passed: bool
    changed_files: list[str] = field(default_factory=list)
    allowed_files: list[str] = field(default_factory=list)
    ignored_changed_files: list[str] = field(default_factory=list)
    violation_files: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    artifact_integrity_failed: bool = False

    def to_gate_metrics(self) -> dict[str, Any]:
        return {
            "changed_files": self.changed_files,
            "allowed_files": self.allowed_files,
            "ignored_changed_files": self.ignored_changed_files,
            "violation_files": self.violation_files,
            "violation_count": len(self.violations),
            "artifact_integrity_failed": self.artifact_integrity_failed,
        }


def _normalize_rel(path: str | Path, next_v: int | None = None) -> str | None:
    text = str(path).strip()
    if not text:
        return None
    text = text.replace("\\", "/")
    marker = bot_relpath(next_v) + "/" if next_v is not None else ""
    if marker and marker in text:
        text = text.split(marker, 1)[1]
    if text.startswith("./"):
        text = text[2:]
    if text.startswith("/"):
        return None
    parts = Path(text).parts
    if not parts or any(part in ("..", "") for part in parts):
        return None
    normalized = Path(*parts).as_posix()
    if normalized in ("", "."):
        return None
    return normalized


def _is_excluded_file(path: Path) -> bool:
    return (
        path.name in _EXCLUDED_FILE_NAMES
        or path.suffix.lower() in _EXCLUDED_FILE_SUFFIXES
    )


def read_regular_file_bytes(
    root: Path,
    path: Path,
    expected: os.stat_result,
) -> bytes:
    if expected.st_size > WORKER_SNAPSHOT_MAX_FILE_BYTES:
        raise OSError(
            "snapshot per-file byte limit exceeded "
            f"({expected.st_size}>{WORKER_SNAPSHOT_MAX_FILE_BYTES})"
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OSError(f"cannot safely open regular file ({exc})") from exc

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("artifact file is not regular")
        if _entry_identity(opened) != _entry_identity(expected):
            raise OSError("artifact changed while opening snapshot")
        # Fill one fixed-size buffer.  The old chunk-list + join pattern held a
        # second full copy of large precomputed tables during every snapshot.
        buffer = bytearray(opened.st_size)
        view = memoryview(buffer)
        offset = 0
        while offset < len(buffer):
            end = min(offset + 1024 * 1024, len(buffer))
            if hasattr(os, "readv"):
                count = os.readv(descriptor, [view[offset:end]])
            else:  # pragma: no cover - POSIX production path has readv
                chunk = os.read(descriptor, end - offset)
                count = len(chunk)
                view[offset:offset + count] = chunk
            if not count:
                raise OSError("artifact truncated while reading snapshot")
            offset += count
        if os.read(descriptor, 1):
            raise OSError("artifact grew while reading snapshot")
        finished = os.fstat(descriptor)
        if _entry_identity(finished) != _entry_identity(opened):
            raise OSError("artifact changed while reading snapshot")
    finally:
        os.close(descriptor)
    return bytes(buffer)


def snapshot_artifact_files(root: Path) -> ArtifactFileSnapshot:
    """Capture every bot artifact file as raw bytes.

    Exclusions are imported from :mod:`bot_artifact`, so ``.completed``, Python
    bytecode, cache trees, and ``.task_context`` have exactly one authority.
    Excluded trees are still inspected for symlinks and special files, matching
    publication identity validation; they simply do not contribute contents.
    """
    root = Path(root)
    if not root.exists() and not root.is_symlink():
        return ArtifactFileSnapshot()

    files: dict[str, bytes] = {}
    directories: set[str] = set()
    issues: list[ArtifactSnapshotIssue] = []
    regular_files: list[tuple[str, Path, os.stat_result]] = []
    file_count = 0
    entry_count = 0
    total_bytes = 0

    try:
        root_meta = root.lstat()
    except OSError as exc:
        raise ArtifactSnapshotError(
            root, [ArtifactSnapshotIssue(".", f"cannot inspect root ({exc})")]
        ) from exc
    if stat.S_ISLNK(root_meta.st_mode):
        raise ArtifactSnapshotError(
            root, [ArtifactSnapshotIssue(".", "symbolic links are forbidden")]
        )
    if not stat.S_ISDIR(root_meta.st_mode):
        raise ArtifactSnapshotError(
            root, [ArtifactSnapshotIssue(".", "artifact root must be a directory")]
        )

    def scan(
        directory: Path,
        *,
        excluded: bool = False,
        depth: int = 0,
    ) -> None:
        nonlocal file_count, entry_count, total_bytes
        if depth > WORKER_SNAPSHOT_MAX_DIRECTORY_DEPTH:
            rel = directory.relative_to(root).as_posix() or "."
            issues.append(ArtifactSnapshotIssue(
                rel,
                "snapshot directory-depth limit exceeded "
                f"({depth}>{WORKER_SNAPSHOT_MAX_DIRECTORY_DEPTH})",
            ))
            return
        try:
            children = os.scandir(directory)
        except OSError as exc:
            rel = directory.relative_to(root).as_posix() or "."
            issues.append(
                ArtifactSnapshotIssue(rel, f"cannot enumerate directory ({exc})")
            )
            return

        try:
            with children:
                for child_entry in children:
                    child = Path(child_entry.path)
                    rel = child.relative_to(root).as_posix()
                    entry_count += 1
                    if entry_count > WORKER_SNAPSHOT_MAX_ENTRY_COUNT:
                        issues.append(ArtifactSnapshotIssue(
                            rel,
                            "snapshot entry-count limit exceeded "
                            f"({entry_count}>{WORKER_SNAPSHOT_MAX_ENTRY_COUNT})",
                        ))
                        return
                    try:
                        metadata = child.lstat()
                    except OSError as exc:
                        issues.append(
                            ArtifactSnapshotIssue(rel, f"cannot inspect entry ({exc})")
                        )
                        continue

                    if stat.S_ISLNK(metadata.st_mode):
                        issues.append(ArtifactSnapshotIssue(
                            rel, "symbolic links are forbidden"
                        ))
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        child_excluded = (
                            excluded or child.name in _EXCLUDED_DIRECTORY_NAMES
                        )
                        if not child_excluded:
                            directories.add(rel)
                        # Validate excluded trees too, without recording contents.
                        scan(
                            child,
                            excluded=child_excluded,
                            depth=depth + 1,
                        )
                        if issues and any(
                            "limit exceeded" in issue.reason for issue in issues
                        ):
                            return
                        continue
                    if stat.S_ISREG(metadata.st_mode):
                        file_count += 1
                        if file_count > WORKER_SNAPSHOT_MAX_FILE_COUNT:
                            issues.append(ArtifactSnapshotIssue(
                                rel,
                                "snapshot file-count limit exceeded "
                                f"({file_count}>{WORKER_SNAPSHOT_MAX_FILE_COUNT})",
                            ))
                            return
                        if excluded or _is_excluded_file(child):
                            continue
                        if metadata.st_size > WORKER_SNAPSHOT_MAX_FILE_BYTES:
                            issues.append(ArtifactSnapshotIssue(
                                rel,
                                "snapshot per-file byte limit exceeded "
                                f"({metadata.st_size}>{WORKER_SNAPSHOT_MAX_FILE_BYTES})",
                            ))
                            return
                        total_bytes += metadata.st_size
                        if total_bytes > WORKER_SNAPSHOT_MAX_TOTAL_BYTES:
                            issues.append(ArtifactSnapshotIssue(
                                rel,
                                "snapshot total byte limit exceeded "
                                f"({total_bytes}>{WORKER_SNAPSHOT_MAX_TOTAL_BYTES})",
                            ))
                            return
                        regular_files.append((rel, child, metadata))
                        continue
                    issues.append(ArtifactSnapshotIssue(
                        rel, "only regular files and directories are allowed"
                    ))
        except OSError as exc:
            rel = directory.relative_to(root).as_posix() or "."
            issues.append(
                ArtifactSnapshotIssue(rel, f"cannot enumerate directory ({exc})")
            )

    scan(root)
    if issues:
        raise ArtifactSnapshotError(root, issues)

    # Phase two: only allocate/read after the complete metadata surface passed
    # every count and byte cap. read_regular_file_bytes revalidates the pinned
    # lstat identity before allocating, closing a size-swap race between phases.
    for rel, path, metadata in regular_files:
        try:
            files[rel] = read_regular_file_bytes(root, path, metadata)
        except OSError as exc:
            issues.append(ArtifactSnapshotIssue(rel, str(exc)))
    if issues:
        raise ArtifactSnapshotError(root, issues)
    return ArtifactFileSnapshot(files, directories=directories)


def snapshot_python_files(root: Path) -> ArtifactFileSnapshot:
    """Compatibility alias: capture the complete bot artifact, not just Python."""
    return snapshot_artifact_files(root)


def _changed_file_paths(
    before: dict[str, bytes],
    after: dict[str, bytes],
) -> set[str]:
    changed = set(before) ^ set(after)
    for rel in set(before) & set(after):
        if before[rel] != after[rel]:
            changed.add(rel)
    return changed


def diff_file_snapshot(root: Path, before: dict[str, bytes]) -> list[str]:
    """Return only changed/created/deleted regular artifact file relpaths."""
    after = snapshot_artifact_files(Path(root))
    return sorted(_changed_file_paths(before, after))


def diff_snapshot(root: Path, before: dict[str, bytes]) -> list[str]:
    """Return changed, created, or deleted artifact relpaths vs a snapshot."""
    root = Path(root)
    after = snapshot_artifact_files(root)
    changed = _changed_file_paths(before, after)

    before_dirs = set(getattr(before, "directories", ()))
    after_dirs = set(after.directories)
    changed.update(before_dirs ^ after_dirs)
    # A file<->directory replacement has the same relpath in the two unions but
    # still changes the artifact entry type.
    changed.update(set(before) & after_dirs)
    changed.update(before_dirs & set(after))
    return sorted(changed)


def hash_changed_files(root: Path, changed_files: list[str]) -> str:
    """Stable diff-ish hash for arbitrary changed artifact files."""
    root = Path(root)
    # Publication manifest hashing streams each file and retains only metadata;
    # do not materialize every large lookup table merely to hash a small delta.
    manifest = artifact_manifest(root)
    entries = {
        str(entry.get("path")): entry
        for entry in manifest.get("entries", [])
        if isinstance(entry, dict)
    }
    h = hashlib.sha256()
    for rel in sorted(changed_files):
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        entry = entries.get(rel)
        if entry and entry.get("type") == "file":
            h.update(b"<file>")
            h.update(str(entry.get("size", 0)).encode("ascii"))
            h.update(b":")
            h.update(str(entry.get("sha256", "")).encode("ascii"))
        elif entry and entry.get("type") == "directory":
            h.update(b"<directory>")
        else:
            h.update(b"<deleted>")
        h.update(b"\0")
    return h.hexdigest()


def _is_tuner_task(task: dict[str, Any]) -> bool:
    role = str(task.get("role", "")).lower()
    return "tuner" in role or "hyperparameter" in role


def allowed_files_for_task(task: dict[str, Any], next_v: int | None = None) -> list[str]:
    allowed: set[str] = set()
    for key in ("target_files", "files_allowed"):
        for item in task.get(key, []) or []:
            rel = _normalize_rel(item, next_v)
            if rel:
                allowed.add(rel)
    if _is_tuner_task(task):
        allowed = {rel for rel in allowed if rel == "constants.py"}
    return sorted(allowed)


def audit_worker_boundary(
    root: Path,
    task: dict[str, Any],
    before_snapshot: dict[str, bytes],
    *,
    next_v: int | None = None,
    ignored_changed_files: list[str] | set[str] | tuple[str, ...] | None = None,
) -> BoundaryAuditResult:
    allowed = allowed_files_for_task(task, next_v)
    try:
        changed = diff_snapshot(root, before_snapshot)
    except ArtifactSnapshotError as exc:
        violations = [
            f"{issue.path}: invalid bot artifact entry ({issue.reason})"
            for issue in exc.issues
        ]
        return BoundaryAuditResult(
            passed=False,
            changed_files=exc.violation_files,
            allowed_files=allowed,
            violation_files=exc.violation_files,
            violations=violations,
            artifact_integrity_failed=True,
        )

    allowed_set = set(allowed)
    ignored_set = {
        rel for rel in (
            _normalize_rel(item, next_v) for item in (ignored_changed_files or [])
        )
        if rel
    }
    changed_ignored_files = ignored_set & set(changed)
    ignored_ancestor_dirs: set[str] = set()
    for rel in changed_ignored_files:
        parent = Path(rel).parent
        while parent.as_posix() not in ("", "."):
            ignored_ancestor_dirs.add(parent.as_posix())
            parent = parent.parent
    ignored = sorted(
        rel for rel in changed
        if rel in ignored_set or rel in ignored_ancestor_dirs
    )
    # Creating a declared nested file necessarily creates its missing ancestor
    # directories. Those directory-shape deltas are implicit consequences of
    # the declared file only when that exact descendant also changed. Merely
    # creating an empty ancestor directory therefore remains a violation.
    changed_allowed_files = allowed_set & set(changed)
    allowed_ancestor_dirs: set[str] = set()
    for rel in changed_allowed_files:
        parent = Path(rel).parent
        while parent.as_posix() not in ("", "."):
            allowed_ancestor_dirs.add(parent.as_posix())
            parent = parent.parent
    violation_files = sorted(
        rel for rel in changed
        if rel not in allowed_set
        and rel not in allowed_ancestor_dirs
        and rel not in ignored_set
        and rel not in ignored_ancestor_dirs
    )
    violations = [
        f"{rel}: changed outside declared target_files/files_allowed"
        for rel in violation_files
    ]
    return BoundaryAuditResult(
        passed=not violations,
        changed_files=changed,
        allowed_files=allowed,
        ignored_changed_files=ignored,
        violation_files=violation_files,
        violations=violations,
    )


def _remove_entry(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def restore_artifact_files(
    root: Path,
    before_snapshot: dict[str, bytes],
    changed_files: list[str],
) -> None:
    """Restore changed artifact files/directories to a prior byte snapshot."""
    root = Path(root)
    normalized = {
        rel for rel in (_normalize_rel(item) for item in changed_files) if rel
    }
    if not normalized:
        return

    before_dirs = set(getattr(before_snapshot, "directories", ()))
    # If a directory itself was replaced by a symlink/special entry, restoring
    # only that path must also restore all files that used to live below it.
    for rel in tuple(normalized):
        prefix = rel + "/"
        normalized.update(item for item in before_snapshot if item.startswith(prefix))
        normalized.update(item for item in before_dirs if item.startswith(prefix))

    # Remove current entries deepest-first without following symlinks.
    for rel in sorted(normalized, key=lambda item: (item.count("/"), item), reverse=True):
        _remove_entry(root / rel)

    # Recreate the trusted directory shape before writing files.
    needed_dirs = {
        directory
        for directory in before_dirs
        if directory in normalized
        or any(rel.startswith(directory + "/") for rel in normalized)
    }
    for rel in sorted(needed_dirs, key=lambda item: (item.count("/"), item)):
        (root / rel).mkdir(parents=True, exist_ok=True)

    for rel in sorted(normalized):
        if rel not in before_snapshot:
            continue
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(before_snapshot[rel])


def restore_complete_artifact_snapshot(
    root: Path,
    before_snapshot: dict[str, bytes],
) -> None:
    """Replace a failed Worker batch with its complete trusted byte snapshot.

    Diff-based restoration cannot enumerate a candidate after a Worker creates
    a symlink/FIFO inside an excluded tree such as ``.task_context``: the safe
    snapshot scanner correctly rejects that tree before it can produce a diff.
    A batch rollback already owns the complete pre-batch snapshot, so remove
    current root entries without following links and reconstruct only that
    trusted regular-file/directory surface.  Excluded caches/control artifacts
    are intentionally discarded; they are never part of bot identity.
    """
    root = Path(root)
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        root.mkdir(parents=True, exist_ok=True)
    else:
        if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
            _remove_entry(root)
            root.mkdir(parents=True, exist_ok=True)

    try:
        children = list(os.scandir(root))
    except OSError as exc:
        raise RuntimeError(f"cannot enumerate artifact root for rollback ({exc})") from exc
    for child in children:
        _remove_entry(Path(child.path))

    before_dirs = set(getattr(before_snapshot, "directories", ()))
    for rel in sorted(before_dirs, key=lambda item: (item.count("/"), item)):
        normalized = _normalize_rel(rel)
        if not normalized:
            raise RuntimeError(f"invalid trusted rollback directory: {rel}")
        (root / normalized).mkdir(parents=True, exist_ok=True)
    for rel in sorted(before_snapshot):
        normalized = _normalize_rel(rel)
        if not normalized:
            raise RuntimeError(f"invalid trusted rollback file: {rel}")
        path = root / normalized
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(before_snapshot[rel])


def restore_python_files(
    root: Path,
    before_snapshot: dict[str, bytes],
    changed_files: list[str],
) -> None:
    """Compatibility alias: restore arbitrary artifact files byte-for-byte."""
    restore_artifact_files(root, before_snapshot, changed_files)


def audit_changed_files_against_plan(
    changed_files: list[str],
    tasks: list[dict[str, Any]],
    *,
    next_v: int | None = None,
) -> BoundaryAuditResult:
    """Audit final candidate diff against the master plan's declared files."""
    allowed: set[str] = set()
    for task in tasks or []:
        allowed.update(allowed_files_for_task(task, next_v))
    normalized_changed = []
    for rel in changed_files:
        normalized = _normalize_rel(rel, next_v)
        if normalized:
            normalized_changed.append(normalized)
    violations = [
        f"{rel}: changed outside master plan target_files/files_allowed"
        for rel in normalized_changed
        if rel not in allowed
    ]
    violation_files = sorted(
        rel for rel in normalized_changed if rel not in allowed
    )
    return BoundaryAuditResult(
        passed=not violations,
        changed_files=sorted(normalized_changed),
        allowed_files=sorted(allowed),
        violation_files=violation_files,
        violations=violations,
    )
