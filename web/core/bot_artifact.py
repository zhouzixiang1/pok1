"""Content identity helpers for published national bots."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from bot_namespace import bot_tag, parse_bot_version, strict_artifact_layout_errors, EVOLUTION_BRANCH

# Configurable publication-branch ref (default refs/heads/main). Publication
# identity checks verify reachability against this branch so a deployment can
# publish into an isolated branch without touching origin/main.
_LOCAL_PUB_REF = f"refs/heads/{EVOLUTION_BRANCH}"


ROOT = Path(__file__).resolve().parents[2]

ARTIFACT_MANIFEST_SCHEMA = 1
ARTIFACT_MAX_FILE_COUNT = 1024
ARTIFACT_MAX_ENTRY_COUNT = 2048
ARTIFACT_MAX_DIRECTORY_DEPTH = 64
ARTIFACT_MAX_FILE_BYTES = 16 * 1024 * 1024
ARTIFACT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_EXCLUDED_DIRECTORY_NAMES = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".task_context",
})
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
    if expected.st_size > ARTIFACT_MAX_FILE_BYTES:
        _raise_invalid(
            root,
            path,
            f"artifact file exceeds byte limit ({expected.st_size}>{ARTIFACT_MAX_FILE_BYTES})",
        )
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


def _enumerate_directory(
    root: Path,
    directory: Path,
    directory_entries: list[dict[str, Any]],
    file_specs: list[tuple[Path, os.stat_result, bool]],
    state: dict[str, int],
    *,
    excluded: bool = False,
    depth: int = 0,
) -> None:
    if depth > ARTIFACT_MAX_DIRECTORY_DEPTH:
        _raise_invalid(
            root,
            directory,
            f"artifact directory depth exceeds limit ({depth}>{ARTIFACT_MAX_DIRECTORY_DEPTH})",
        )
    children: list[Path] = []
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                state["entry_count"] += 1
                if state["entry_count"] > ARTIFACT_MAX_ENTRY_COUNT:
                    _raise_invalid(
                        root,
                        directory,
                        "artifact entry count exceeds limit "
                        f"({state['entry_count']}>{ARTIFACT_MAX_ENTRY_COUNT})",
                    )
                children.append(directory / entry.name)
    except OSError as exc:
        _raise_invalid(root, directory, f"cannot enumerate directory ({exc})")
    children.sort(key=lambda item: os.fsencode(item.name))

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
                directory_entries.append({"path": relative, "type": "directory"})
            # Transient cache/control trees do not contribute identity, but are
            # still inspected so a symlink or special file cannot hide in one.
            # Strict publication/execution separately rejects cache presence.
            _enumerate_directory(
                root,
                child,
                directory_entries,
                file_specs,
                state,
                excluded=child_excluded,
                depth=depth + 1,
            )
            continue

        if stat.S_ISREG(metadata.st_mode):
            state["file_count"] += 1
            state["total_bytes"] += metadata.st_size
            if state["file_count"] > ARTIFACT_MAX_FILE_COUNT:
                _raise_invalid(
                    root,
                    child,
                    "artifact file count exceeds limit "
                    f"({state['file_count']}>{ARTIFACT_MAX_FILE_COUNT})",
                )
            if metadata.st_size > ARTIFACT_MAX_FILE_BYTES:
                _raise_invalid(
                    root,
                    child,
                    "artifact file exceeds byte limit "
                    f"({metadata.st_size}>{ARTIFACT_MAX_FILE_BYTES})",
                )
            if state["total_bytes"] > ARTIFACT_MAX_TOTAL_BYTES:
                _raise_invalid(
                    root,
                    child,
                    "artifact total bytes exceed limit "
                    f"({state['total_bytes']}>{ARTIFACT_MAX_TOTAL_BYTES})",
                )
            file_specs.append((child, metadata, excluded or _is_excluded_file(child)))
            continue

        _raise_invalid(root, child, "only regular files and directories are allowed")


def artifact_manifest(path_or_token: str | Path) -> dict[str, Any]:
    """Build the deterministic identity manifest for artifact source entries.

    Runtime caches and host control products do not acquire authority merely by
    appearing in this hash.  Strict publication rejects executable caches, and
    managed bot launch exposes only sealed bytes captured from the bound source
    projection.
    """
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
        if metadata.st_size > ARTIFACT_MAX_FILE_BYTES:
            _raise_invalid(
                path,
                path,
                f"artifact file exceeds byte limit ({metadata.st_size}>{ARTIFACT_MAX_FILE_BYTES})",
            )
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
        directory_entries = [{"path": ".", "type": "directory"}]
        file_specs: list[tuple[Path, os.stat_result, bool]] = []
        _enumerate_directory(
            path,
            path,
            directory_entries,
            file_specs,
            {"entry_count": 0, "file_count": 0, "total_bytes": 0},
        )
        # Only after the complete tree passes count/depth/size/type validation
        # do we read any payload bytes.  Sparse or oversized crossover output
        # therefore cannot force unbounded I/O before quality gates.
        entries = list(directory_entries)
        for file_path, expected, excluded in file_specs:
            if excluded:
                continue
            entries.append({
                "path": file_path.relative_to(path).as_posix(),
                "type": "file",
                "size": expected.st_size,
                "sha256": _read_file_digest(path, file_path, expected),
            })
        entries.sort(
            key=lambda item: (
                item.get("path") != ".",
                os.fsencode(str(item.get("path") or "")),
            )
        )
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


def _git_bytes(repo_root: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {args[0]} failed: {result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout


def _manifest_from_git_blobs(
    root: Path,
    repo_root: Path,
    blobs: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """Build the canonical manifest from index/tree blobs, never worktree bytes."""
    if len(blobs) > ARTIFACT_MAX_FILE_COUNT:
        _raise_invalid(
            root,
            root,
            f"git artifact file count exceeds limit ({len(blobs)}>{ARTIFACT_MAX_FILE_COUNT})",
        )
    specs: list[tuple[str, str, int]] = []
    total_bytes = 0
    directories = {"."}
    for relative, mode, oid in sorted(blobs, key=lambda item: os.fsencode(item[0])):
        path = Path(relative)
        if (
            not relative
            or path.is_absolute()
            or any(part in {"", "..", ".git"} for part in path.parts)
        ):
            _raise_invalid(root, root / relative, "invalid git artifact path")
        if mode not in {"100644", "100755"}:
            _raise_invalid(root, root / relative, f"non-blob git mode is forbidden ({mode})")
        size_raw = _git_bytes(repo_root, "cat-file", "-s", oid).strip()
        try:
            size = int(size_raw)
        except (TypeError, ValueError):
            _raise_invalid(root, root / relative, "git blob size is invalid")
        if size > ARTIFACT_MAX_FILE_BYTES:
            _raise_invalid(
                root,
                root / relative,
                f"git blob exceeds byte limit ({size}>{ARTIFACT_MAX_FILE_BYTES})",
            )
        total_bytes += size
        if total_bytes > ARTIFACT_MAX_TOTAL_BYTES:
            _raise_invalid(
                root,
                root / relative,
                f"git artifact total bytes exceed limit ({total_bytes}>{ARTIFACT_MAX_TOTAL_BYTES})",
            )
        specs.append((relative, oid, size))
        parent = path.parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent

    if len(directories) + len(specs) > ARTIFACT_MAX_ENTRY_COUNT:
        _raise_invalid(root, root, "git artifact entry count exceeds limit")
    entries: list[dict[str, Any]] = [
        {"path": directory, "type": "directory"}
        for directory in directories
    ]
    for relative, oid, expected_size in specs:
        payload = _git_bytes(repo_root, "cat-file", "blob", oid)
        if len(payload) != expected_size:
            _raise_invalid(root, root / relative, "git blob changed while reading")
        entries.append({
            "path": relative,
            "type": "file",
            "size": expected_size,
            "sha256": hashlib.sha256(payload).hexdigest(),
        })
    entries.sort(
        key=lambda item: (
            item.get("path") != ".",
            os.fsencode(str(item.get("path") or "")),
        )
    )
    return {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA,
        "artifact_type": "directory",
        "entries": entries,
    }


def git_index_artifact_manifest(
    path_or_token: str | Path,
    *,
    repo_root: str | Path = ROOT,
) -> dict[str, Any]:
    """Build a bot manifest from the staged Git index."""
    repo = _absolute_without_resolving(repo_root)
    root = _absolute_without_resolving(path_or_token)
    try:
        prefix = root.relative_to(repo).as_posix()
    except ValueError:
        _raise_invalid(root, root, "artifact path is outside git repository")
    raw = _git_bytes(repo, "ls-files", "--stage", "-z", "--", prefix)
    blobs: list[tuple[str, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            _raise_invalid(root, root, "git index record is invalid")
        mode, oid, stage = (field.decode("ascii", "strict") for field in fields)
        full_path = raw_path.decode("utf-8", "surrogateescape")
        if stage != "0":
            _raise_invalid(root, root, f"unmerged git index stage is forbidden ({stage})")
        marker = prefix.rstrip("/") + "/"
        if not full_path.startswith(marker):
            _raise_invalid(root, root, "git index path escaped artifact prefix")
        blobs.append((full_path[len(marker):], mode, oid))
    return _manifest_from_git_blobs(root, repo, blobs)


def git_tree_artifact_manifest(
    path_or_token: str | Path,
    ref: str,
    *,
    repo_root: str | Path = ROOT,
) -> dict[str, Any]:
    """Build a bot manifest from an immutable Git tree/tag/commit."""
    repo = _absolute_without_resolving(repo_root)
    root = _absolute_without_resolving(path_or_token)
    try:
        prefix = root.relative_to(repo).as_posix()
    except ValueError:
        _raise_invalid(root, root, "artifact path is outside git repository")
    raw = _git_bytes(repo, "ls-tree", "-r", "-z", ref, "--", prefix)
    blobs: list[tuple[str, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            _raise_invalid(root, root, "git tree record is invalid")
        mode = fields[0].decode("ascii", "strict")
        object_type = fields[1].decode("ascii", "strict")
        oid = fields[2].decode("ascii", "strict")
        if object_type != "blob":
            _raise_invalid(root, root, f"non-blob git object is forbidden ({object_type})")
        full_path = raw_path.decode("utf-8", "surrogateescape")
        marker = prefix.rstrip("/") + "/"
        if not full_path.startswith(marker):
            _raise_invalid(root, root, "git tree path escaped artifact prefix")
        blobs.append((full_path[len(marker):], mode, oid))
    return _manifest_from_git_blobs(root, repo, blobs)


def validate_staged_artifact(
    path_or_token: str | Path,
    *,
    repo_root: str | Path = ROOT,
) -> dict[str, Any]:
    """Cross-bind certified worktree bytes to exactly the staged Git blobs."""
    root = _absolute_without_resolving(path_or_token)
    working = artifact_manifest(root)
    staged = git_index_artifact_manifest(root, repo_root=repo_root)
    working_hash = canonical_digest(working)
    staged_hash = canonical_digest(staged)
    return {
        "valid": working == staged,
        "working_hash": working_hash,
        "staged_hash": staged_hash,
        "working_manifest": working,
        "staged_manifest": staged,
    }


def publication_shape_errors(
    path_or_token: str | Path,
    *,
    repo_root: str | Path = ROOT,
) -> list[str]:
    """Reject worktree shapes Git cannot reproduce from a normal clone."""
    root = _absolute_without_resolving(path_or_token)
    repo = _absolute_without_resolving(repo_root)
    errors: list[str] = []
    # ``artifact_manifest`` deliberately excludes transient work products so
    # Worker rollback hashes remain stable.  Publication is a stronger boundary:
    # an ignored unchecked-hash pyc would be executable when the directory is
    # mounted even though Git and the certificate never bound it.
    errors.extend(strict_artifact_layout_errors(root))
    try:
        relative_root = root.relative_to(repo)
    except ValueError:
        return ["publication_artifact_outside_repository"]

    try:
        for path in root.rglob(".git"):
            errors.append(
                "nested_git_metadata_forbidden:"
                + path.relative_to(root).as_posix()
            )
    except OSError as exc:
        errors.append(f"publication_shape_scan_error:{type(exc).__name__}")
        return errors

    manifest = artifact_manifest(root)
    files = [
        str(item.get("path") or "")
        for item in manifest.get("entries") or []
        if item.get("type") == "file"
    ]
    directories = [
        str(item.get("path") or "")
        for item in manifest.get("entries") or []
        if item.get("type") == "directory" and item.get("path") != "."
    ]
    for directory in directories:
        prefix = directory.rstrip("/") + "/"
        if not any(path.startswith(prefix) for path in files):
            errors.append(f"empty_directory_not_publishable:{directory}")

    if files:
        full_paths = [
            (relative_root / Path(relative)).as_posix()
            for relative in files
        ]
        result = subprocess.run(
            ["git", "check-ignore", "-z", "--stdin"],
            cwd=str(repo),
            input=("\0".join(full_paths) + "\0").encode(
                "utf-8", "surrogateescape"
            ),
            capture_output=True,
            timeout=30,
            check=False,
        )
        # check-ignore returns 1 when no path matches and 0 when at least one
        # matches. Any other status is an integrity-check failure.
        if result.returncode not in {0, 1}:
            errors.append("git_ignore_check_failed")
        else:
            for raw in result.stdout.split(b"\0"):
                if not raw:
                    continue
                ignored = raw.decode("utf-8", "surrogateescape")
                try:
                    display = Path(ignored).relative_to(relative_root).as_posix()
                except ValueError:
                    display = ignored
                errors.append(f"git_ignored_artifact_file:{display}")
    return sorted(set(errors))


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
            f"{_LOCAL_PUB_REF}^{{commit}}",
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
    tag_artifact_hash = ""
    if tag and commit_oid and relative is not None:
        try:
            tag_manifest = git_tree_artifact_manifest(
                path,
                f"{tag}^{{commit}}",
                repo_root=ROOT,
            )
            tag_artifact_hash = canonical_digest(tag_manifest)
        except Exception as exc:
            issues.append(
                "completion_tag_artifact_manifest_invalid:"
                f"{type(exc).__name__}"
            )
    if tag_artifact_hash and tag_artifact_hash != artifact_hash:
        issues.append("completion_tag_artifact_differs_from_worktree")
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
        and tag_artifact_hash
        and tag_artifact_hash == artifact_hash
    )
    return {
        "label": path.name,
        "version": version,
        "path": str(path),
        "artifact_hash": artifact_hash,
        "tag_artifact_hash": tag_artifact_hash,
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
    if not expected_hash or identity.get("tag_artifact_hash") != expected_hash:
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
    elif _git("merge-base", "--is-ancestor", commit_oid, EVOLUTION_BRANCH).returncode != 0:
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
