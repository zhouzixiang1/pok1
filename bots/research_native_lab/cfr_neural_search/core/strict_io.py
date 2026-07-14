"""Fail-closed, content-bound local artifact I/O for Route B."""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping



def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _absolute_without_resolve(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _open_root(root: Path) -> tuple[int, Path]:
    absolute = _absolute_without_resolve(root)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(os.sep, flags)
    try:
        for component in absolute.parts[1:]:
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise ValueError(
                    f"trusted root ancestry contains a symlink/non-directory: {absolute}"
                ) from exc
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, absolute


def validate_real_directory(path: str | Path) -> Path:
    """Validate every supplied directory component before canonicalization."""

    raw = Path(path)
    if ".." in raw.parts:
        raise ValueError("trusted directory path cannot contain parent traversal")
    descriptor, absolute = _open_root(raw)
    os.close(descriptor)
    return absolute


def _stable_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _hash_openat_regular(
    parent_fd: int,
    name: str,
    before: os.stat_result,
    *,
    max_file_bytes: int,
) -> tuple[str, tuple[int, ...]]:
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("tree source entry is not a regular file")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        identity = _stable_metadata(opened)
        if identity != _stable_metadata(before):
            raise ValueError("tree source changed before stable open")
        if opened.st_size > max_file_bytes:
            raise ValueError("tree source file exceeds configured size limit")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, min(1 << 20, max_file_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_file_bytes:
                raise ValueError("tree source file exceeds configured size limit")
            digest.update(chunk)
        after_fd = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if identity != _stable_metadata(after_fd) or identity != _stable_metadata(after_path):
        raise ValueError("tree source file changed during stable hash")
    return digest.hexdigest(), identity


def _capture_tree_pass(
    root: Path,
    *,
    excluded_paths: frozenset[str],
    excluded_directory_names: frozenset[str],
    excluded_suffixes: frozenset[str],
    max_file_bytes: int,
) -> tuple[dict[str, str], dict[str, tuple[int, ...]]]:
    sentinel = root / ".route-b-tree-anchor"
    anchored = _open_parent(sentinel, root, create=False)
    files: dict[str, str] = {}
    identities: dict[str, tuple[int, ...]] = {}
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )

    def walk(directory_fd: int, prefix: tuple[str, ...]) -> None:
        before_directory = os.fstat(directory_fd)
        if not stat.S_ISDIR(before_directory.st_mode):
            raise ValueError("tree traversal reached a non-directory")
        names_before = tuple(sorted(os.listdir(directory_fd)))
        for name in names_before:
            if type(name) is not str or name in {"", ".", ".."} or "/" in name:
                raise ValueError("tree source contains an unsafe entry name")
            relative_parts = prefix + (name,)
            relative = "/".join(relative_parts)
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"tree source contains a symlink: {relative}")
            if relative in excluded_paths:
                continue
            if stat.S_ISDIR(metadata.st_mode):
                if name in excluded_directory_names:
                    continue
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                try:
                    if _directory_identity(os.fstat(child_fd)) != _directory_identity(
                        metadata
                    ):
                        raise ValueError("tree directory changed before stable open")
                    walk(child_fd, relative_parts)
                finally:
                    os.close(child_fd)
                identities[f"{relative}/"] = _stable_metadata(
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                )
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"tree source contains a special file: {relative}")
            if Path(name).suffix in excluded_suffixes:
                continue
            digest, identity = _hash_openat_regular(
                directory_fd,
                name,
                metadata,
                max_file_bytes=max_file_bytes,
            )
            files[relative] = digest
            identities[relative] = identity
        names_after = tuple(sorted(os.listdir(directory_fd)))
        after_directory = os.fstat(directory_fd)
        if (
            names_after != names_before
            or _stable_metadata(after_directory) != _stable_metadata(before_directory)
        ):
            raise ValueError("tree directory changed during stable enumeration")
        key = "/".join(prefix) + ("/" if prefix else "")
        identities[key] = _stable_metadata(after_directory)

    try:
        anchored.verify_reachable()
        walk(anchored.parent_fd, ())
        anchored.verify_reachable()
    finally:
        anchored.close()
    return files, identities


def stable_tree_manifest(
    root: str | Path,
    *,
    excluded_paths: frozenset[str] = frozenset(),
    excluded_directory_names: frozenset[str] = frozenset({"__pycache__"}),
    excluded_suffixes: frozenset[str] = frozenset({".pyc", ".pyo"}),
    max_file_bytes: int = 1 << 30,
) -> dict[str, str]:
    """Return a double-pass, fd-relative full-tree content snapshot.

    Both file hashes and every file/directory identity must match across two
    complete passes.  This rejects mixed-time A/B manifests even when an
    already-read file is rewritten while later entries are enumerated.
    """

    if type(excluded_paths) is not frozenset or any(
        type(value) is not str or not value for value in excluded_paths
    ):
        raise TypeError("excluded_paths must be a frozenset of nonempty strings")
    if type(excluded_directory_names) is not frozenset or any(
        type(value) is not str or not value or "/" in value
        for value in excluded_directory_names
    ):
        raise TypeError("excluded directory names are invalid")
    if type(excluded_suffixes) is not frozenset or any(
        type(value) is not str or not value.startswith(".")
        for value in excluded_suffixes
    ):
        raise TypeError("excluded suffixes are invalid")
    if type(max_file_bytes) is not int or max_file_bytes <= 0:
        raise ValueError("max_file_bytes must be a positive exact integer")
    root_path = validate_real_directory(root)
    first = _capture_tree_pass(
        root_path,
        excluded_paths=excluded_paths,
        excluded_directory_names=excluded_directory_names,
        excluded_suffixes=excluded_suffixes,
        max_file_bytes=max_file_bytes,
    )
    second = _capture_tree_pass(
        root_path,
        excluded_paths=excluded_paths,
        excluded_directory_names=excluded_directory_names,
        excluded_suffixes=excluded_suffixes,
        max_file_bytes=max_file_bytes,
    )
    if first != second:
        raise ValueError("tree source changed across complete snapshot passes")
    return first[0]


def stable_flat_directory_manifest(
    root: str | Path,
    *,
    max_file_bytes: int = 1 << 30,
) -> dict[str, str]:
    """Return a stable manifest only when every direct entry is a real file.

    Unlike :func:`stable_tree_manifest`, this rejects even empty subdirectories.
    It is used for authoritative append-only directories whose complete set of
    names is itself part of the evidence contract.
    """

    root_path = validate_real_directory(root)
    anchored = _open_parent(
        root_path / ".flat-manifest-anchor",
        root_path,
        create=False,
    )

    def capture_flat_pass() -> tuple[
        dict[str, str],
        dict[str, tuple[int, ...]],
        tuple[int, ...],
    ]:
        anchored.verify_reachable()
        before = os.fstat(anchored.parent_fd)
        names = tuple(sorted(os.listdir(anchored.parent_fd)))
        files: dict[str, str] = {}
        identities: dict[str, tuple[int, ...]] = {}
        for name in names:
            metadata = os.stat(
                name,
                dir_fd=anchored.parent_fd,
                follow_symlinks=False,
            )
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"flat manifest contains a non-regular direct entry: {name}"
                )
            digest, identity = _hash_openat_regular(
                anchored.parent_fd,
                name,
                metadata,
                max_file_bytes=max_file_bytes,
            )
            files[name] = digest
            identities[name] = identity
        if tuple(sorted(os.listdir(anchored.parent_fd))) != names or _stable_metadata(
            os.fstat(anchored.parent_fd)
        ) != _stable_metadata(before):
            raise ValueError("flat directory changed during direct enumeration")
        anchored.verify_reachable()
        return files, identities, _stable_metadata(before)

    try:
        first_manifest, first_identities, first_directory = capture_flat_pass()
        second_manifest, second_identities, second_directory = capture_flat_pass()
        anchored.verify_reachable()
    finally:
        anchored.close()
    if (
        first_manifest != second_manifest
        or first_identities != second_identities
        or first_directory != second_directory
    ):
        raise ValueError("flat directory changed across complete snapshot passes")
    return first_manifest


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


@dataclass(slots=True)
class _AnchoredParent:
    anchor_fd: int
    parent_fd: int
    target_name: str
    candidate: Path
    components: tuple[str, ...]
    identities: tuple[tuple[int, int, int], ...]

    def verify_reachable(self) -> None:
        """Rewalk the requested absolute path and compare every directory."""

        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        current = os.dup(self.anchor_fd)
        try:
            if _directory_identity(os.fstat(current)) != self.identities[0]:
                raise ValueError("filesystem anchor identity changed")
            for index, component in enumerate(self.components, start=1):
                try:
                    next_fd = os.open(component, flags, dir_fd=current)
                except OSError as exc:
                    raise ValueError(
                        "requested parent ancestry is no longer reachable"
                    ) from exc
                os.close(current)
                current = next_fd
                if _directory_identity(os.fstat(current)) != self.identities[index]:
                    raise ValueError("requested parent ancestry identity changed")
            if _directory_identity(os.fstat(current)) != _directory_identity(
                os.fstat(self.parent_fd)
            ):
                raise ValueError("held parent fd differs from requested path")
        finally:
            os.close(current)

    def close(self) -> None:
        os.close(self.parent_fd)
        os.close(self.anchor_fd)


def _open_parent(
    path: str | Path,
    root: str | Path | None,
    *,
    create: bool,
) -> _AnchoredParent:
    candidate = Path(path)
    if root is None:
        if not candidate.is_absolute():
            candidate = _absolute_without_resolve(candidate)
        root_path = candidate.parent
    else:
        root_path = Path(root)
        if not candidate.is_absolute():
            candidate = _absolute_without_resolve(root_path) / candidate
    absolute_root = _absolute_without_resolve(root_path)
    absolute_candidate = _absolute_without_resolve(candidate)
    try:
        relative = absolute_candidate.relative_to(absolute_root)
    except ValueError as exc:
        raise ValueError(f"path escapes trusted root: {candidate}") from exc
    if relative == Path(".") or not relative.name or relative.name in {".", ".."}:
        raise ValueError("target must name a file below the trusted root")
    parts = relative.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("target path contains an unsafe component")
    root_components = absolute_root.parts[1:]
    parent_components = absolute_candidate.parent.parts[1:]
    if tuple(parent_components[: len(root_components)]) != tuple(root_components):
        raise ValueError("target parent does not retain the trusted root prefix")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    anchor_fd = os.open(os.sep, directory_flags)
    current_fd = os.dup(anchor_fd)
    identities = [_directory_identity(os.fstat(current_fd))]
    try:
        for index, component in enumerate(parent_components):
            try:
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create or index < len(root_components):
                    raise
                os.mkdir(component, mode=0o700, dir_fd=current_fd)
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            except OSError as exc:
                raise ValueError(
                    f"path component is not a real trusted directory: {component}"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
            identities.append(_directory_identity(os.fstat(current_fd)))
        result = _AnchoredParent(
            anchor_fd=anchor_fd,
            parent_fd=current_fd,
            target_name=parts[-1],
            candidate=absolute_candidate,
            components=tuple(parent_components),
            identities=tuple(identities),
        )
        result.verify_reachable()
        return result
    except BaseException:
        os.close(current_fd)
        os.close(anchor_fd)
        raise


@contextmanager
def exclusive_file_lock(
    path: str | Path,
    *,
    root: str | Path,
):
    """Hold one rooted, no-follow process lock with stable file identity."""

    anchored = _open_parent(path, root, create=True)
    descriptor = -1
    try:
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(
            anchored.target_name,
            flags,
            0o600,
            dir_fd=anchored.parent_fd,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("exclusive lock target is not a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        anchored.verify_reachable()
        current = os.stat(
            anchored.target_name,
            dir_fd=anchored.parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise ValueError("exclusive lock path identity changed")
        yield
        anchored.verify_reachable()
        final = os.stat(
            anchored.target_name,
            dir_fd=anchored.parent_fd,
            follow_symlinks=False,
        )
        if final.st_dev != opened.st_dev or final.st_ino != opened.st_ino:
            raise ValueError("exclusive lock path changed while held")
    finally:
        if descriptor >= 0:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        anchored.close()


def read_regular_bytes(
    path: str | Path,
    *,
    root: str | Path | None = None,
    max_bytes: int = 1 << 30,
) -> bytes:
    """Read one stable regular file descriptor without following a symlink."""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive exact integer")
    anchored: _AnchoredParent | None = None
    try:
        anchored = _open_parent(path, root, create=False)
        anchored.verify_reachable()
        parent_fd = anchored.parent_fd
        name = anchored.target_name
        candidate = anchored.candidate
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ValueError(f"input must be a regular non-symlink file: {candidate}")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("opened input is not a regular file")
            identity = (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_nlink,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            before_identity = (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            if identity != before_identity:
                raise ValueError("input path changed before open")
            if opened.st_size > max_bytes:
                raise ValueError("input exceeds configured size limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(1 << 20, max_bytes - total + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("input exceeds configured size limit")
            after_fd = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        anchored.verify_reachable()
    finally:
        if anchored is not None:
            anchored.close()
    observed_before = (
        opened.st_dev,
        opened.st_ino,
        opened.st_mode,
        opened.st_nlink,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
    )
    observed_after = (
        after_fd.st_dev,
        after_fd.st_ino,
        after_fd.st_mode,
        after_fd.st_nlink,
        after_fd.st_size,
        after_fd.st_mtime_ns,
        after_fd.st_ctime_ns,
    )
    path_after = (
        after_path.st_dev,
        after_path.st_ino,
        after_path.st_mode,
        after_path.st_nlink,
        after_path.st_size,
        after_path.st_mtime_ns,
        after_path.st_ctime_ns,
    )
    if observed_before != observed_after or path_after != identity:
        raise ValueError("input changed during content-bound read")
    return b"".join(chunks)


def strict_json_loads(raw: bytes, *, context: str = "JSON payload") -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON numeric constant {value!r} is forbidden")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if type(key) is not str:
                raise TypeError(f"{context} key must be an exact string")
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    def finite_float(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"non-finite JSON float {value!r} is forbidden")
        return result

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{context} is not UTF-8") from exc
    return json.loads(
        text,
        parse_constant=reject_constant,
        parse_float=finite_float,
        object_pairs_hook=unique_object,
    )


def strict_json_read(
    path: str | Path,
    *,
    root: str | Path | None = None,
    max_bytes: int = 1 << 30,
    context: str = "JSON payload",
) -> Any:
    return strict_json_loads(
        read_regular_bytes(path, root=root, max_bytes=max_bytes),
        context=context,
    )


def atomic_write_bytes(
    path: str | Path,
    content: bytes,
    *,
    root: str | Path | None = None,
    mode: int = 0o600,
) -> None:
    """Atomically replace a rooted regular target and fsync file + directory."""

    if type(content) is not bytes:
        raise TypeError("atomic content must be exact bytes")
    anchored = _open_parent(path, root, create=True)
    parent_fd = anchored.parent_fd
    target_name = anchored.target_name
    try:
        current = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    if current is not None and (
        stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)
    ):
        anchored.close()
        raise ValueError("atomic target must be absent or a regular non-symlink file")
    initial_identity = (
        None
        if current is None
        else (
            current.st_dev,
            current.st_ino,
            current.st_ctime_ns,
            current.st_mtime_ns,
            current.st_size,
        )
    )
    temporary_name = f".{target_name}.{secrets.token_hex(16)}.tmp"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
        dir_fd=parent_fd,
    )
    try:
        view = memoryview(content)
        written = 0
        while written < len(content):
            written += os.write(descriptor, view[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        # Recheck the destination through the already-open trusted parent.  A
        # target symlink introduced after the initial check is never replaced.
        try:
            current = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        current_identity = (
            None
            if current is None
            else (
                current.st_dev,
                current.st_ino,
                current.st_ctime_ns,
                current.st_mtime_ns,
                current.st_size,
            )
        )
        if current_identity != initial_identity:
            raise ValueError("atomic target identity changed before replacement")
        if current is not None and (
            stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)
        ):
            raise ValueError("atomic target changed to a non-regular path")
        anchored.verify_reachable()
        os.replace(
            temporary_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = ""
        anchored.verify_reachable()
        os.fsync(parent_fd)
        anchored.verify_reachable()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        anchored.close()


def atomic_json_write(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    root: str | Path | None = None,
) -> str:
    digest = _payload_sha256(payload)
    envelope = {"payload": payload, "sha256": digest}
    atomic_write_bytes(
        path,
        _canonical_json_bytes(envelope) + b"\n",
        root=root,
    )
    return digest


def atomic_create_bytes(
    path: str | Path,
    content: bytes,
    *,
    root: str | Path | None = None,
    mode: int = 0o600,
) -> None:
    """Atomically publish a new regular file and never replace an old one.

    Publication uses a same-directory hard link from a fully fsynced temporary
    inode.  A host crash can leave the temporary name, but the authoritative
    target is either wholly absent or wholly present and is never truncated.
    """

    if type(content) is not bytes:
        raise TypeError("atomic create content must be exact bytes")
    anchored = _open_parent(path, root, create=True)
    parent_fd = anchored.parent_fd
    target_name = anchored.target_name
    try:
        try:
            os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("atomic create target already exists")
        temporary_name = f".{target_name}.{secrets.token_hex(16)}.tmp"
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(content)
            written = 0
            while written < len(content):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise OSError("atomic create made no write progress")
                written += count
            os.fsync(descriptor)
            created = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            anchored.verify_reachable()
            try:
                os.link(
                    temporary_name,
                    target_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise ValueError("atomic create target appeared before publication") from exc
            target = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(target.st_mode)
                or target.st_dev != created.st_dev
                or target.st_ino != created.st_ino
                or target.st_mode != created.st_mode
                or target.st_size != len(content)
            ):
                raise ValueError("atomic create published an unexpected target identity")
            os.unlink(temporary_name, dir_fd=parent_fd)
            temporary_name = ""
            anchored.verify_reachable()
            os.fsync(parent_fd)
            anchored.verify_reachable()
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
    finally:
        anchored.close()


def atomic_json_create(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    root: str | Path | None = None,
) -> str:
    digest = _payload_sha256(payload)
    envelope = {"payload": payload, "sha256": digest}
    atomic_create_bytes(
        path,
        _canonical_json_bytes(envelope) + b"\n",
        root=root,
    )
    return digest


def append_jsonl_bytes(
    path: str | Path,
    line: bytes,
    *,
    root: str | Path | None = None,
    mode: int = 0o600,
) -> None:
    """Durably append exactly one canonical JSONL record without replacement.

    A crash may leave a partial final line, which strict journal recovery must
    reject.  It can never silently replace, truncate, follow a symlink, or
    append through a parent directory that was renamed and recreated.
    """

    if type(line) is not bytes or not line or not line.endswith(b"\n"):
        raise ValueError("JSONL append requires one nonempty newline-terminated byte line")
    if b"\n" in line[:-1] or b"\r" in line:
        raise ValueError("JSONL append line cannot contain embedded line breaks")
    anchored = _open_parent(path, root, create=True)
    parent_fd = anchored.parent_fd
    target_name = anchored.target_name
    try:
        try:
            before = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            before = None
        if before is not None and (
            stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
        ):
            raise ValueError("append target must be absent or a regular non-symlink file")
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        if before is None:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(target_name, flags, mode, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("opened append target is not a regular file")
            if before is not None and _stable_metadata(opened) != _stable_metadata(before):
                raise ValueError("append target identity changed before open")
            initial_size = opened.st_size
            anchored.verify_reachable()
            written = 0
            view = memoryview(line)
            while written < len(line):
                written += os.write(descriptor, view[written:])
            os.fsync(descriptor)
            after_fd = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(after_fd.st_mode)
            or after_fd.st_dev != opened.st_dev
            or after_fd.st_ino != opened.st_ino
            or after_fd.st_mode != opened.st_mode
            or after_fd.st_nlink != opened.st_nlink
            or after_fd.st_size != initial_size + len(line)
            or _stable_metadata(after_path) != _stable_metadata(after_fd)
        ):
            raise ValueError("append target changed during durable append")
        anchored.verify_reachable()
        os.fsync(parent_fd)
        anchored.verify_reachable()
    finally:
        anchored.close()


def remove_regular_file(
    path: str | Path,
    *,
    root: str | Path | None = None,
    missing_ok: bool = False,
) -> None:
    """Unlink one rooted regular file while retaining ancestry reachability."""

    if type(missing_ok) is not bool:
        raise TypeError("missing_ok must be an exact boolean")
    anchored = _open_parent(path, root, create=False)
    try:
        anchored.verify_reachable()
        try:
            metadata = os.stat(
                anchored.target_name,
                dir_fd=anchored.parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("removal target must be a regular non-symlink file")
        anchored.verify_reachable()
        current = os.stat(
            anchored.target_name,
            dir_fd=anchored.parent_fd,
            follow_symlinks=False,
        )
        if _stable_metadata(current) != _stable_metadata(metadata):
            raise ValueError("removal target identity changed before unlink")
        os.unlink(anchored.target_name, dir_fd=anchored.parent_fd)
        anchored.verify_reachable()
        os.fsync(anchored.parent_fd)
        anchored.verify_reachable()
    finally:
        anchored.close()


def remove_empty_directory(
    path: str | Path,
    *,
    root: str | Path | None = None,
    missing_ok: bool = False,
) -> None:
    """Remove one rooted empty real directory without following symlinks."""

    if type(missing_ok) is not bool:
        raise TypeError("missing_ok must be an exact boolean")
    anchored = _open_parent(path, root, create=False)
    parent_fd = anchored.parent_fd
    name = anchored.target_name
    try:
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("directory removal target must be a real directory")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        try:
            if os.listdir(descriptor):
                raise ValueError("directory removal target is not empty")
            opened = os.fstat(descriptor)
            if _directory_identity(opened) != _directory_identity(metadata):
                raise ValueError("directory removal target changed before open")
        finally:
            os.close(descriptor)
        anchored.verify_reachable()
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _directory_identity(current) != _directory_identity(metadata):
            raise ValueError("directory removal target identity changed")
        os.rmdir(name, dir_fd=parent_fd)
        anchored.verify_reachable()
        os.fsync(parent_fd)
        anchored.verify_reachable()
    finally:
        anchored.close()


def load_hashed_json(
    path: str | Path,
    *,
    root: str | Path | None = None,
    max_bytes: int = 1 << 30,
) -> Mapping[str, Any]:
    envelope = strict_json_read(
        path,
        root=root,
        max_bytes=max_bytes,
        context="hashed JSON envelope",
    )
    if type(envelope) is not dict or set(envelope) != {"payload", "sha256"}:
        raise ValueError("hashed JSON envelope requires exactly payload and sha256")
    payload = envelope["payload"]
    if type(payload) is not dict:
        raise TypeError("hashed JSON payload must be an object")
    expected = envelope["sha256"]
    if type(expected) is not str or len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise ValueError("hashed JSON digest must be lowercase SHA-256")
    if _payload_sha256(payload) != expected:
        raise ValueError("hashed JSON content digest mismatch")
    return payload
