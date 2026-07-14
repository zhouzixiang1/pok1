"""Fd-bound stable file/tree I/O for route-A training and evidence inputs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Iterable


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def pretty_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8") + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def strict_json_loads(data: str | bytes) -> Any:
    def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is forbidden: {value}")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"non-finite JSON number is forbidden: {value}")
        return parsed

    try:
        return json.loads(
            data,
            object_pairs_hook=strict_object,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise ValueError("payload is not strict UTF-8 JSON") from exc


def stable_stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def inode_identity(value: os.stat_result) -> tuple[int, int, int]:
    return value.st_dev, value.st_ino, value.st_mode


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )


def assert_real_directory(path: Path) -> None:
    if not path.is_absolute() or not path.is_dir():
        raise ValueError(f"trusted directory must be existing and absolute: {path}")
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise ValueError(f"trusted directory must not contain symlink aliases: {path}")


def _open_absolute_directory_chain(
    path: Path,
) -> tuple[int, int, list[tuple[int, str, int, os.stat_result]]]:
    """Open every absolute path component with openat/O_NOFOLLOW and retain it."""

    assert_real_directory(path)
    if ".." in path.parts:
        raise ValueError("trusted directory path must not contain parent aliases")
    anchor_fd = os.open(Path(path.anchor), _directory_flags())
    current_fd = anchor_fd
    chain: list[tuple[int, str, int, os.stat_result]] = []
    try:
        for part in path.parts[1:]:
            child_fd = open_stable_directory_at(current_fd, part)
            child_before = os.fstat(child_fd)
            chain.append((current_fd, part, child_fd, child_before))
            current_fd = child_fd
        return current_fd, anchor_fd, chain
    except BaseException:
        for _, _, child_fd, _ in reversed(chain):
            os.close(child_fd)
        os.close(anchor_fd)
        raise


def _verify_absolute_directory_chain(
    anchor_fd: int,
    chain: list[tuple[int, str, int, os.stat_result]],
) -> None:
    if not stat.S_ISDIR(os.fstat(anchor_fd).st_mode):
        raise RuntimeError("absolute filesystem anchor is no longer a directory")
    for parent_fd, part, child_fd, child_before in chain:
        if (
            inode_identity(os.fstat(child_fd)) != inode_identity(child_before)
            or inode_identity(
                os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
            )
            != inode_identity(child_before)
        ):
            raise RuntimeError(
                f"absolute directory ancestry changed during access: {part}"
            )


def _close_absolute_directory_chain(
    anchor_fd: int,
    chain: list[tuple[int, str, int, os.stat_result]],
) -> None:
    for _, _, child_fd, _ in reversed(chain):
        os.close(child_fd)
    os.close(anchor_fd)


def read_stable_regular_at(directory_fd: int, name: str) -> bytes:
    before_name = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(before_name.st_mode):
        raise ValueError(f"stable input is not a regular file: {name}")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        before_fd = os.fstat(descriptor)
        identity = stable_stat_identity(before_fd)
        if identity != stable_stat_identity(before_name):
            raise RuntimeError(f"stable input changed before fd acquisition: {name}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_fd = os.fstat(descriptor)
        after_name = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            stable_stat_identity(after_fd) != identity
            or stable_stat_identity(after_name) != identity
        ):
            raise RuntimeError(f"stable input changed during read: {name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def open_stable_directory_at(parent_fd: int, name: str) -> int:
    before_name = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before_name.st_mode):
        raise ValueError(f"stable input is not a directory: {name}")
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    opened = os.fstat(descriptor)
    if stable_stat_identity(opened) != stable_stat_identity(before_name):
        os.close(descriptor)
        raise RuntimeError(f"stable directory changed before fd acquisition: {name}")
    return descriptor


def stable_read_relative(root: Path, relative: Path) -> bytes:
    assert_real_directory(root)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("stable relative input must remain below its root")
    root_fd, anchor_fd, root_chain = _open_absolute_directory_chain(root)
    directories: list[tuple[int, str, int, os.stat_result]] = []
    try:
        root_before = os.fstat(root_fd)
        current_fd = root_fd
        for part in relative.parts[:-1]:
            child_fd = open_stable_directory_at(current_fd, part)
            child_before = os.fstat(child_fd)
            directories.append((current_fd, part, child_fd, child_before))
            current_fd = child_fd
        data = read_stable_regular_at(current_fd, relative.parts[-1])
        for parent_fd, part, child_fd, child_before in reversed(directories):
            identity = stable_stat_identity(child_before)
            if (
                stable_stat_identity(os.fstat(child_fd)) != identity
                or stable_stat_identity(
                    os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                )
                != identity
            ):
                raise RuntimeError(
                    f"stable input parent changed during access: {part}"
                )
        root_identity = stable_stat_identity(root_before)
        if (
            stable_stat_identity(os.fstat(root_fd)) != root_identity
            or stable_stat_identity(os.stat(root, follow_symlinks=False))
            != root_identity
        ):
            raise RuntimeError("stable input root changed during access")
        _verify_absolute_directory_chain(anchor_fd, root_chain)
        return data
    finally:
        for _, _, child_fd, _ in reversed(directories):
            os.close(child_fd)
        _close_absolute_directory_chain(anchor_fd, root_chain)


def stable_read_path(path: str | Path) -> bytes:
    value = Path(path)
    if not value.is_absolute():
        value = Path.cwd() / value
    return stable_read_relative(value.parent, Path(value.name))


def _secure_file_map_once(
    root: Path,
    *,
    ignored_directories: Iterable[str] = (),
    excluded: Iterable[str] = (),
) -> dict[str, str]:
    assert_real_directory(root)
    ignored = frozenset(ignored_directories)
    excluded_set = frozenset(excluded)
    root_fd, anchor_fd, root_chain = _open_absolute_directory_chain(root)
    files: dict[str, str] = {}

    def visit(directory_fd: int, prefix: tuple[str, ...]) -> None:
        before = os.fstat(directory_fd)
        for name in sorted(os.listdir(directory_fd)):
            parts = prefix + (name,)
            relative = "/".join(parts)
            if name in ignored:
                continue
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode):
                raise ValueError(f"stable tree must not contain symlinks: {relative}")
            if stat.S_ISDIR(entry.st_mode):
                child_fd = open_stable_directory_at(directory_fd, name)
                child_before = os.fstat(child_fd)
                try:
                    visit(child_fd, parts)
                    identity = stable_stat_identity(child_before)
                    if (
                        stable_stat_identity(os.fstat(child_fd)) != identity
                        or stable_stat_identity(
                            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        )
                        != identity
                    ):
                        raise RuntimeError(
                            f"stable tree directory changed during scan: {relative}"
                        )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(entry.st_mode):
                continue
            if relative in excluded_set or name.endswith(".pyc"):
                continue
            files[relative] = sha256_bytes(
                read_stable_regular_at(directory_fd, name)
            )
        if stable_stat_identity(os.fstat(directory_fd)) != stable_stat_identity(before):
            raise RuntimeError(
                "stable tree directory changed during scan: "
                + ("/".join(prefix) or ".")
            )

    try:
        root_before = os.fstat(root_fd)
        visit(root_fd, ())
        identity = stable_stat_identity(root_before)
        if (
            stable_stat_identity(os.fstat(root_fd)) != identity
            or stable_stat_identity(os.stat(root, follow_symlinks=False)) != identity
        ):
            raise RuntimeError("stable tree root changed during scan")
        _verify_absolute_directory_chain(anchor_fd, root_chain)
    finally:
        _close_absolute_directory_chain(anchor_fd, root_chain)
    return dict(sorted(files.items()))


def secure_file_map(
    root: Path,
    *,
    ignored_directories: Iterable[str] = (),
    excluded: Iterable[str] = (),
) -> dict[str, str]:
    """Return one complete tree map only when two stable scans agree exactly."""

    ignored = tuple(ignored_directories)
    excluded_names = tuple(excluded)
    first = _secure_file_map_once(
        root,
        ignored_directories=ignored,
        excluded=excluded_names,
    )
    second = _secure_file_map_once(
        root,
        ignored_directories=ignored,
        excluded=excluded_names,
    )
    if first != second:
        raise RuntimeError("stable tree changed between complete snapshot scans")
    return first


def stable_selected_file_map(root: Path, names: Iterable[str]) -> dict[str, str]:
    """Read a fixed selected file set twice to reject mixed-time source maps."""

    selected = tuple(names)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("selected stable file names must be non-empty and unique")
    if any(Path(name).is_absolute() or ".." in Path(name).parts for name in selected):
        raise ValueError("selected stable file names must remain below the root")

    def scan() -> dict[str, str]:
        return {
            name: sha256_bytes(stable_read_relative(root, Path(name)))
            for name in selected
        }

    first = scan()
    second = scan()
    if first != second:
        raise RuntimeError("selected source files changed between snapshot scans")
    return first


def atomic_json_write(path: str | Path, payload: object) -> None:
    target = Path(path)
    if not target.is_absolute():
        target = Path.cwd() / target
    parent = target.parent
    assert_real_directory(parent)
    directory_fd, anchor_fd, parent_chain = _open_absolute_directory_chain(parent)
    temporary = f".{target.name}.{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    try:
        try:
            existing = os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise ValueError("atomic JSON target must be absent or a regular file")
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        initial_identity = (
            None if existing is None else stable_stat_identity(existing)
        )
        data = pretty_json_bytes(payload)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        temporary_stat = os.fstat(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            current = os.stat(
                target.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current = None
        current_identity = (
            None if current is None else stable_stat_identity(current)
        )
        if current_identity != initial_identity:
            raise RuntimeError("atomic JSON target changed before publication")
        _verify_absolute_directory_chain(anchor_fd, parent_chain)
        os.replace(
            temporary,
            target.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        published = os.stat(
            target.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            published.st_dev,
            published.st_ino,
            published.st_mode,
            published.st_size,
            published.st_mtime_ns,
        ) != (
            temporary_stat.st_dev,
            temporary_stat.st_ino,
            temporary_stat.st_mode,
            temporary_stat.st_size,
            temporary_stat.st_mtime_ns,
        ):
            raise RuntimeError("atomic JSON publication lost its temporary inode")
        _verify_absolute_directory_chain(anchor_fd, parent_chain)
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        _close_absolute_directory_chain(anchor_fd, parent_chain)
