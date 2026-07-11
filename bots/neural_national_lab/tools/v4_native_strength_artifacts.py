#!/usr/bin/env python3
"""Canonical, durable directory snapshots for native v4 strength tools."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from typing import Any, Iterable


class ArtifactError(ValueError):
    """Raised when a runtime artifact cannot be identified without ambiguity."""


def read_regular_bytes_with_stat(path: Path) -> tuple[bytes, dict[str, int]]:
    """Read one regular file through one descriptor and bind its stat receipt."""

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArtifactError(f"cannot safely open artifact file {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ArtifactError(f"artifact path must be a regular file: {path}")
        receipt = {
            "st_dev": int(before.st_dev),
            "st_ino": int(before.st_ino),
            "st_mode": int(before.st_mode),
            "st_size": int(before.st_size),
            "st_mtime_ns": int(before.st_mtime_ns),
            "st_ctime_ns": int(before.st_ctime_ns),
        }
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        after_receipt = {
            "st_dev": int(after.st_dev),
            "st_ino": int(after.st_ino),
            "st_mode": int(after.st_mode),
            "st_size": int(after.st_size),
            "st_mtime_ns": int(after.st_mtime_ns),
            "st_ctime_ns": int(after.st_ctime_ns),
        }
        raw = b"".join(chunks)
        if after_receipt != receipt:
            raise ArtifactError(f"artifact changed while reading {path}")
        if len(raw) != before.st_size:
            raise ArtifactError(f"artifact byte count differs from fstat: {path}")
        return raw, receipt
    finally:
        os.close(descriptor)


def _git(root: Path, *args: str, binary: bool = False) -> bytes | str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=not binary,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr if not binary else result.stderr.decode("utf-8", "replace")
        stdout = result.stdout if not binary else result.stdout.decode("utf-8", "replace")
        raise ArtifactError(
            f"git {' '.join(args)} failed: {(stderr or stdout).strip()[:500]}"
        )
    return result.stdout


def _read_regular_file(root: Path, path: Path, expected: os.stat_result) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ArtifactError(f"cannot safely read artifact file {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        expected_identity = (
            expected.st_dev,
            expected.st_ino,
            expected.st_mode,
            expected.st_size,
            expected.st_mtime_ns,
            expected.st_ctime_ns,
            expected.st_nlink,
        )
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            opened.st_nlink,
        )
        if not stat.S_ISREG(opened.st_mode) or opened_identity != expected_identity:
            raise ArtifactError(f"artifact changed while opening {path.relative_to(root)}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        if (
            finished.st_dev,
            finished.st_ino,
            finished.st_mode,
            finished.st_size,
            finished.st_mtime_ns,
            finished.st_ctime_ns,
            finished.st_nlink,
        ) != opened_identity:
            raise ArtifactError(f"artifact changed while reading {path.relative_to(root)}")
        raw = b"".join(chunks)
        if len(raw) != opened.st_size:
            raise ArtifactError(
                f"artifact byte count differs from fstat: {path.relative_to(root)}"
            )
        return raw
    finally:
        os.close(descriptor)


def read_regular_bytes(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ArtifactError(f"artifact file is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ArtifactError(f"artifact file must be regular: {path}")
    return _read_regular_file(path.parent, path, metadata)


def _digest_tree_entries(
    entries: Iterable[tuple[bytes, bytes, int, bytes]],
) -> str:
    digest = hashlib.sha256()
    for kind, relative, mode, content in sorted(entries, key=lambda row: row[1]):
        digest.update(kind)
        digest.update(mode.to_bytes(4, "big"))
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _filesystem_tree_entries(
    path: Path,
    *,
    canonical_read_only_modes: bool,
) -> list[tuple[bytes, bytes, int, bytes]]:
    root = Path(os.path.abspath(os.fspath(path.expanduser())))
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise ArtifactError(f"artifact directory is unavailable: {root}") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ArtifactError(f"artifact root must be a real directory: {root}")
    if os.listxattr(root):
        raise ArtifactError(f"artifact extended attributes are forbidden: {root}")
    entries: list[tuple[bytes, bytes, int, bytes]] = []

    def effective_mode(metadata: os.stat_result, *, directory: bool) -> int:
        if canonical_read_only_modes:
            if directory:
                return 0o555
            return 0o555 if metadata.st_mode & 0o111 else 0o444
        return stat.S_IMODE(metadata.st_mode)

    entries.append((b"d", b".", effective_mode(root_stat, directory=True), b""))

    def scan(directory: Path) -> None:
        try:
            children = sorted(directory.iterdir(), key=lambda item: os.fsencode(item.name))
        except OSError as exc:
            raise ArtifactError(
                f"cannot enumerate artifact directory {directory}: {exc}"
            ) from exc
        for child in children:
            try:
                metadata = child.lstat()
            except OSError as exc:
                raise ArtifactError(f"cannot inspect artifact entry {child}: {exc}") from exc
            if metadata.st_dev != root_stat.st_dev:
                raise ArtifactError(f"artifact crosses a filesystem boundary: {child}")
            if stat.S_ISLNK(metadata.st_mode):
                raise ArtifactError(f"artifact symbolic links are forbidden: {child}")
            if os.listxattr(child):
                raise ArtifactError(
                    f"artifact extended attributes are forbidden: {child}"
                )
            relative = os.fsencode(child.relative_to(root).as_posix())
            if stat.S_ISDIR(metadata.st_mode):
                entries.append((b"d", relative, effective_mode(metadata, directory=True), b""))
                scan(child)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise ArtifactError(f"artifact hard links are forbidden: {child}")
                entries.append(
                    (
                        b"f",
                        relative,
                        effective_mode(metadata, directory=False),
                        _read_regular_file(root, child, metadata),
                    )
                )
            else:
                raise ArtifactError(f"artifact contains a non-regular entry: {child}")

    scan(root)
    return entries


def tree_digest(path: Path) -> str:
    return _digest_tree_entries(
        _filesystem_tree_entries(path, canonical_read_only_modes=False)
    )


def canonical_tree_digest(path: Path) -> str:
    return _digest_tree_entries(
        _filesystem_tree_entries(path, canonical_read_only_modes=True)
    )


def code_artifact_hashes(source_root: Path, roots: Iterable[Path]) -> dict[str, str]:
    """Hash Python closure paths while rejecting symlink identity aliases."""

    paths: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        root_metadata = root.lstat()
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise ArtifactError(f"code artifact root must be a real directory: {root}")
        for item in root.rglob("*"):
            metadata = item.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ArtifactError(f"code artifact symlinks are forbidden: {item}")
            if (
                stat.S_ISREG(metadata.st_mode)
                and item.suffix == ".py"
                and "__pycache__" not in item.parts
            ):
                absolute = Path(os.path.abspath(os.fspath(item)))
                if not absolute.is_relative_to(source_root):
                    raise ArtifactError(f"code artifact escaped source root: {item}")
                paths.add(absolute)
    artifacts: dict[str, str] = {}
    for path in sorted(paths):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise ArtifactError(f"required code artifact is unavailable: {path}") from exc
        artifacts[str(path.relative_to(source_root))] = hashlib.sha256(raw).hexdigest()
    if not artifacts:
        raise ArtifactError("code artifact closure is empty")
    return artifacts


def _git_tree_entries(
    root: Path,
    label: str,
    commit: str,
) -> tuple[str, list[tuple[bytes, bytes, int, bytes]]]:
    prefix = f"bots/{label}/".encode("utf-8")
    tree_oid = str(_git(root, "rev-parse", f"{commit}:bots/{label}")).strip()
    if len(tree_oid) != 40 or any(char not in "0123456789abcdef" for char in tree_oid):
        raise ArtifactError(f"Git commit has no bot tree for {label}")
    raw = bytes(
        _git(root, "ls-tree", "-r", "-z", commit, "--", f"bots/{label}", binary=True)
    )
    files: list[tuple[bytes, bytes, int, bytes]] = []
    directories: set[bytes] = {b"."}
    native_entry = False
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, git_path = record.split(b"\t", 1)
            raw_mode, object_type, object_oid = header.split(b" ", 2)
        except ValueError as exc:
            raise ArtifactError(f"malformed Git tree record for {label}") from exc
        if object_type != b"blob" or raw_mode not in {b"100644", b"100755"}:
            raise ArtifactError(f"unsupported Git entry in completed bot {label}")
        if not git_path.startswith(prefix):
            raise ArtifactError(f"Git tree escaped bot root for {label}")
        relative = git_path[len(prefix):]
        if not relative:
            raise ArtifactError(f"invalid Git path in completed bot {label}")
        parts = relative.split(b"/")
        for index in range(1, len(parts)):
            directories.add(b"/".join(parts[:index]))
        content = bytes(_git(root, "cat-file", "blob", object_oid.decode("ascii"), binary=True))
        files.append((b"f", relative, 0o555 if raw_mode == b"100755" else 0o444, content))
        native_entry = native_entry or relative == b"national_bot.py"
    if not files or not native_entry:
        raise ArtifactError(f"Git tree lacks native entry for {label}")
    entries = [(b"d", directory, 0o555, b"") for directory in directories]
    entries.extend(files)
    return tree_oid, entries


def tag_directory_identity(root: Path, label: str, commit: str) -> tuple[str, str]:
    tree_oid, entries = _git_tree_entries(root, label, commit)
    return tree_oid, _digest_tree_entries(entries)


def make_read_only(path: Path) -> None:
    items = list(path.rglob("*"))
    for item in sorted(items, key=lambda value: len(value.parts), reverse=True):
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ArtifactError(f"snapshot contains a symbolic link: {item}")
        if stat.S_ISDIR(metadata.st_mode):
            os.chmod(item, 0o555)
        elif stat.S_ISREG(metadata.st_mode):
            os.chmod(item, 0o555 if metadata.st_mode & 0o111 else 0o444)
        else:
            raise ArtifactError(f"snapshot contains a non-regular entry: {item}")
    os.chmod(path, 0o555)


def assert_read_only_tree(path: Path) -> None:
    root = path.lstat()
    if stat.S_ISLNK(root.st_mode) or not stat.S_ISDIR(root.st_mode):
        raise ArtifactError(f"frozen output must be a real directory: {path}")
    for item in [path, *path.rglob("*")]:
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ArtifactError(f"frozen output contains a symbolic link: {item}")
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise ArtifactError(f"frozen output contains a non-regular entry: {item}")
        if os.listxattr(item):
            raise ArtifactError(f"frozen output contains extended attributes: {item}")
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise ArtifactError(f"frozen output contains a hard link: {item}")
        if stat.S_IMODE(metadata.st_mode) & 0o222:
            raise ArtifactError(f"frozen output is writable: {item}")


def restore_owner_access(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    for directory, directories, files in os.walk(path, topdown=True, followlinks=False):
        directory_path = Path(directory)
        try:
            mode = directory_path.lstat().st_mode
            os.chmod(directory_path, mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass
        for name in [*directories, *files]:
            item = directory_path / name
            try:
                metadata = item.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                owner_bits = stat.S_IRUSR | stat.S_IWUSR
                if stat.S_ISDIR(metadata.st_mode):
                    owner_bits |= stat.S_IXUSR
                os.chmod(item, metadata.st_mode | owner_bits)
            except OSError:
                pass


def directory_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ArtifactError(f"artifact path is not a real directory: {path}")
    return int(metadata.st_dev), int(metadata.st_ino)


def path_matches_directory_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        return directory_identity(path) == identity
    except (ArtifactError, FileNotFoundError, OSError):
        return False


def remove_tree(path: Path, expected_identity: tuple[int, int] | None = None) -> None:
    target = path
    if expected_identity is not None:
        target = path.with_name(
            f".{path.name}.remove-{os.getpid()}-{time.monotonic_ns()}"
        )
        publish_tree_noreplace(path, target)
        if not path_matches_directory_identity(target, expected_identity):
            try:
                publish_tree_noreplace(target, path)
            finally:
                fsync_directory(path.parent)
            raise ArtifactError(f"refusing to remove replaced artifact path: {path}")
    restore_owner_access(target)
    if expected_identity is not None and not path_matches_directory_identity(
        target, expected_identity
    ):
        raise ArtifactError(f"quarantined artifact identity changed: {target}")
    if target.exists() or target.is_symlink():
        shutil.rmtree(target)
    if target.exists() or target.is_symlink():
        raise ArtifactError(f"failed to remove temporary artifact tree: {target}")
    fsync_directory(target.parent)


def publish_tree_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename one directory without replacing any destination."""

    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise ArtifactError("renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise ArtifactError(f"output directory already exists: {destination}")
        raise OSError(error, os.strerror(error), str(destination))


def copy_tree_snapshot(source: Path, destination: Path) -> str:
    before = canonical_tree_digest(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    copied = canonical_tree_digest(destination)
    after = canonical_tree_digest(source)
    if before != copied or before != after:
        raise ArtifactError(f"artifact changed while freezing {source}")
    make_read_only(destination)
    if before != tree_digest(destination):
        raise ArtifactError(f"artifact mode canonicalization changed {source}")
    return before


def copy_git_tree_snapshot(
    root: Path,
    commit: str,
    label: str,
    destination: Path,
) -> tuple[str, str]:
    tree_oid, entries = _git_tree_entries(root, label, commit)
    destination.mkdir(parents=True, exist_ok=False)
    for kind, relative, mode, content in sorted(entries, key=lambda row: row[1]):
        if relative == b".":
            continue
        target = destination / Path(os.fsdecode(relative))
        if kind == b"d":
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ArtifactError(f"short write while materializing {label}")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(target, mode)
    make_read_only(destination)
    digest = tree_digest(destination)
    if digest != _digest_tree_entries(entries):
        raise ArtifactError(f"materialized Git tree changed for {label}")
    return tree_oid, digest


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def mkdir_parents_fsync(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    metadata = current.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ArtifactError(f"output ancestor must be a real directory: {current}")
    ancestor = current
    while True:
        ancestor_metadata = ancestor.lstat()
        if stat.S_ISLNK(ancestor_metadata.st_mode) or not stat.S_ISDIR(
            ancestor_metadata.st_mode
        ):
            raise ArtifactError(
                f"output ancestor must be a real directory: {ancestor}"
            )
        if ancestor.parent == ancestor:
            break
        ancestor = ancestor.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory)
        except FileExistsError:
            existing = directory.lstat()
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
                raise ArtifactError(
                    f"output parent raced with a non-directory: {directory}"
                )
        fsync_directory(directory)
        fsync_directory(directory.parent)


def write_json_fsync(path: Path, payload: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


def fsync_tree(path: Path) -> None:
    files: list[Path] = []
    directories: list[Path] = [path]
    for item in path.rglob("*"):
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ArtifactError(f"cannot fsync symbolic link in frozen output: {item}")
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(item)
        elif stat.S_ISREG(metadata.st_mode):
            files.append(item)
        else:
            raise ArtifactError(f"cannot fsync non-regular frozen entry: {item}")
    for item in files:
        descriptor = os.open(item, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        fsync_directory(directory)
