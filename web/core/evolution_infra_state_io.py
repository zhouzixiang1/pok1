"""Atomic, crash-safe, sidecar-locked state-file I/O trust layer.

Extracted from evolution_infra.py as a single business responsibility: the
locked-file / locked-state-sidecar / atomic-publish / JSON+JSONL read-write-
append primitives that every other evolution_infra cluster stands on.

All public symbols are re-exported by evolution_infra.py (as thin delegate
shells) for backward compatibility.  External modules should keep importing
these names from ``evolution_infra``; the shells preserve every existing
``from evolution_infra import <name>`` site and every test monkeypatch on the
``evolution_infra`` namespace.

Cross-references inside this module route through ``_ei.<NAME>`` so that
runtime monkeypatches applied to ``evolution_infra.<NAME>`` (notably
``_atomic_publish_state_text``, ``_locked_state_sidecar`` and
``_fsync_directory``) take effect even when the call originates from a body
that now lives here.
"""

import os
import json
import asyncio
import threading
import uuid
import stat

import fcntl
from contextlib import contextmanager
from pathlib import Path

import evolution_infra as _ei  # for RESULTS_DIR, MAX_PARALLEL_WORKERS,
                               # _WORKER_SEMAPHORE, and the thin delegate
                               # shells that pick up test monkeypatches.


# Process-local thread locks keyed by resolved sidecar/lock path.  These are
# only ever referenced from the moved sidecar/locked-file bodies, so they live
# here alongside the bodies that own them.
_FILE_THREAD_LOCKS: "dict[str, threading.RLock]" = {}
_FILE_THREAD_LOCKS_GUARD = threading.Lock()
_STATE_SIDECAR_LOCAL = threading.local()


def _get_worker_semaphore() -> "asyncio.Semaphore":
    """Return (creating if needed) the worker semaphore for the current adaptive
    level. 503/限速时 level 升 → worker 并发自动降(base>>level)."""
    try:
        from api_concurrency import get_adaptive_limit, get_level
        _lvl = get_level()
        _limit = get_adaptive_limit(_ei.MAX_PARALLEL_WORKERS)
    except Exception:
        _lvl, _limit = 0, _ei.MAX_PARALLEL_WORKERS
    sem = _ei._WORKER_SEMAPHORE.get(_lvl)
    if sem is None:
        sem = asyncio.Semaphore(_limit)
        _ei._WORKER_SEMAPHORE[_lvl] = sem
    return sem


def _thread_lock_for(path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _FILE_THREAD_LOCKS_GUARD:
        return _FILE_THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _locked_file_os(path, mode='r', lock_type=None, encoding=None):
    """Context manager for file operations with fcntl locking.

    For mode='w': opens with 'r+' if file exists (to avoid truncating before
    the lock is acquired), then truncates after locking. If file doesn't exist,
    uses 'w' to create it (safe — no data to lose).
    """
    if lock_type is None:
        lock_type = fcntl.LOCK_EX if ('w' in mode or 'a' in mode or '+' in mode) else fcntl.LOCK_SH
    open_kwargs = {}
    if encoding is not None:
        open_kwargs["encoding"] = encoding
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    actual_mode = mode
    truncate_after_lock = False
    if mode == 'w':
        if Path(path).exists():
            actual_mode = 'r+'
            truncate_after_lock = True
    try:
        f = open(path, actual_mode, **open_kwargs)
    except FileNotFoundError:
        if mode == 'w':
            f = open(path, 'w', **open_kwargs)
        else:
            raise
    with f:
        fcntl.flock(f, lock_type)
        if truncate_after_lock:
            f.seek(0)
            f.truncate()
        try:
            yield f
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def locked_file(path, mode='r', lock_type=None, encoding=None):
    """Open data only after acquiring its stable sidecar lock.

    Locking the replaceable data inode is unsafe: a waiter may open the old
    inode before an atomic writer replaces the path, then later acquire a lock
    that no longer serializes the live file.  Every reader, writer, appender and
    archival scanner therefore locks ``<path>.lock`` first and opens the data
    path only inside that critical section.
    """

    path = Path(path)
    if lock_type is None:
        lock_type = (
            fcntl.LOCK_EX
            if any(flag in mode for flag in ("w", "a", "x", "+"))
            else fcntl.LOCK_SH
        )
    normalized = mode.replace("b", "").replace("t", "")
    flags_by_mode = {
        "r": os.O_RDONLY,
        "r+": os.O_RDWR,
        # Truncating modes are published from a private inode below.  Never
        # put O_TRUNC on an open of the live path: a path swapped to a
        # hardlink after lstat() would otherwise damage the linked victim
        # before descriptor/path authenticity could be checked.
        "w": os.O_WRONLY | os.O_CREAT,
        "w+": os.O_RDWR | os.O_CREAT,
        "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        "a+": os.O_RDWR | os.O_CREAT | os.O_APPEND,
        "x": os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        "x+": os.O_RDWR | os.O_CREAT | os.O_EXCL,
    }
    if normalized not in flags_by_mode:
        raise ValueError(f"unsupported locked_file mode: {mode}")
    creating = any(flag in normalized for flag in ("w", "a", "x"))
    if creating:
        _ei._assert_safe_state_parent(path)
    with _ei._locked_state_sidecar(path, lock_type=lock_type):
        existing = None
        if os.path.lexists(path):
            existing = os.lstat(path)
            if (
                not stat.S_ISREG(existing.st_mode)
                or stat.S_ISLNK(existing.st_mode)
                or existing.st_nlink != 1
            ):
                raise OSError("locked data path must be a single-link regular file")
        if normalized in {"w", "w+"}:
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temp_flags = flags_by_mode[normalized] | os.O_EXCL
            temp_flags |= getattr(os, "O_CLOEXEC", 0)
            temp_flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = None
            temporary_identity = None
            try:
                descriptor = os.open(temp, temp_flags, 0o600)
                binary = "b" in mode
                open_kwargs = (
                    {} if binary or encoding is None else {"encoding": encoding}
                )
                with os.fdopen(descriptor, mode, **open_kwargs) as handle:
                    descriptor = None
                    opened = _ei._assert_open_regular_path(
                        temp,
                        handle,
                        label="locked state temporary data",
                    )
                    try:
                        yield handle
                    finally:
                        finished = _ei._assert_open_regular_path(
                            temp,
                            handle,
                            label="locked state temporary data",
                        )
                        if (
                            finished.st_dev,
                            finished.st_ino,
                        ) != (
                            opened.st_dev,
                            opened.st_ino,
                        ):
                            raise OSError(
                                "locked state temporary data inode changed"
                            )
                    handle.flush()
                    os.fsync(handle.fileno())
                    temporary_identity = os.fstat(handle.fileno())

                # Fail closed if a writer that ignored the sidecar changed the
                # destination while the private inode was being populated.
                # Even a last-moment race after this proof is harmless to an
                # external hardlink victim: os.replace() only removes the
                # destination directory entry and never writes its inode.
                if existing is None:
                    if os.path.lexists(path):
                        raise OSError(
                            "locked state data target appeared during atomic write"
                        )
                else:
                    try:
                        current = os.lstat(path)
                    except OSError as exc:
                        raise OSError(
                            "locked state data target changed during atomic write"
                        ) from exc
                    if (
                        not stat.S_ISREG(current.st_mode)
                        or stat.S_ISLNK(current.st_mode)
                        or current.st_nlink != 1
                        or (current.st_dev, current.st_ino)
                        != (existing.st_dev, existing.st_ino)
                    ):
                        raise OSError(
                            "locked state data target changed during atomic write"
                        )

                os.replace(temp, path)
                published = os.lstat(path)
                if (
                    temporary_identity is None
                    or not stat.S_ISREG(published.st_mode)
                    or stat.S_ISLNK(published.st_mode)
                    or published.st_nlink != 1
                    or (published.st_dev, published.st_ino)
                    != (temporary_identity.st_dev, temporary_identity.st_ino)
                    or published.st_size != temporary_identity.st_size
                ):
                    raise OSError(
                        "locked state publication did not retain the temporary inode"
                    )
                _ei._fsync_directory(path.parent)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            return
        flags = flags_by_mode[normalized] | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        binary = "b" in mode
        open_kwargs = {} if binary or encoding is None else {"encoding": encoding}
        with os.fdopen(descriptor, mode, **open_kwargs) as handle:
            opened = _ei._assert_open_regular_path(
                path,
                handle,
                label="locked state data",
            )
            if opened.st_nlink != 1:
                raise OSError("locked state data must have one link")
            try:
                yield handle
            finally:
                finished = _ei._assert_open_regular_path(
                    path,
                    handle,
                    label="locked state data",
                )
                if finished.st_nlink != 1:
                    raise OSError("locked state data link count changed")
                if lock_type == fcntl.LOCK_SH and (
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                ) != (
                    finished.st_size,
                    finished.st_mtime_ns,
                    finished.st_ctime_ns,
                ):
                    raise OSError("locked state data changed during shared read")


def _fsync_directory(path):
    """Durably publish a directory-entry mutation.

    File ``fsync`` plus ``os.replace`` is atomic for readers, but the rename or
    unlink is not power-loss durable until the containing directory is synced.
    Publication/checkpoint code deliberately lets an ``OSError`` escape here:
    claiming a durable state after a failed directory sync would be unsafe.
    """

    directory = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_regular_state_file_and_parent(path):
    """Re-prove a published state inode and its directory durability."""

    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        live = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(live.st_mode)
            or opened.st_nlink != 1
            or live.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino)
        ):
            raise OSError("state durability target is unsafe")
        os.fsync(descriptor)
        live_after = os.lstat(path)
        if (
            live_after.st_nlink != 1
            or (live_after.st_dev, live_after.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("state durability target changed")
    finally:
        os.close(descriptor)
    _ei._fsync_directory(path.parent)


def _sidecar_lock_path(path):
    path = Path(path)
    return path.with_suffix(path.suffix + ".lock")


def _assert_safe_state_parent(path):
    """Reject state publication through a symlink/non-directory parent."""

    parent = Path(path).parent
    if not os.path.lexists(parent):
        parent.mkdir(parents=True, exist_ok=True)
    try:
        parent_stat = os.lstat(parent)
    except OSError as exc:
        raise OSError(
            f"state parent metadata unavailable: {type(exc).__name__}"
        ) from exc
    if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
        raise OSError("state parent must be a non-symlink directory")


def _preflight_state_sidecar(path):
    _ei._assert_safe_state_parent(path)
    lock_path = _ei._sidecar_lock_path(path)
    if os.path.lexists(lock_path):
        metadata = os.lstat(lock_path)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise OSError("state sidecar lock must be a non-symlink regular file")


def _assert_open_regular_path(path, handle, *, label):
    """Bind an opened descriptor to the still-live regular-file path."""

    try:
        path_stat = os.lstat(path)
        file_stat = os.fstat(handle.fileno())
    except OSError as exc:
        raise OSError(
            f"{label} metadata unavailable: {type(exc).__name__}"
        ) from exc
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or not stat.S_ISREG(file_stat.st_mode)
        or path_stat.st_nlink != 1
        or file_stat.st_nlink != 1
        or path_stat.st_dev != file_stat.st_dev
        or path_stat.st_ino != file_stat.st_ino
    ):
        raise OSError(f"{label} path is not the opened safe regular file")
    return file_stat


@contextmanager
def _locked_state_sidecar(path, *, lock_type):
    """Lock a stable, no-follow sidecar inode shared by readers and writers."""

    path = Path(path)
    lock_path = _ei._sidecar_lock_path(path)
    _ei._preflight_state_sidecar(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with _thread_lock_for(lock_path):
        held_map = getattr(_STATE_SIDECAR_LOCAL, "held", None)
        if held_map is None:
            held_map = {}
            _STATE_SIDECAR_LOCAL.held = held_map
        lock_key = str(lock_path.resolve())
        held = held_map.get(lock_key)
        if held is not None:
            # Re-entering an exclusive lock as EX or SH is safe and must not
            # open/flock a second descriptor: several publication transactions
            # deliberately call checkpoint readers while owning the checkpoint
            # CAS lock.  A SH -> EX upgrade is rejected instead of deadlocking
            # or silently weakening the outer reader lease.
            if held["lock_type"] != fcntl.LOCK_EX and lock_type == fcntl.LOCK_EX:
                raise OSError("state sidecar shared lock cannot be upgraded")
            held["depth"] += 1
            integrity_error = None
            try:
                _ei._assert_open_regular_path(
                    lock_path,
                    held["handle"],
                    label="state sidecar lock",
                )
                yield held["handle"]
            finally:
                try:
                    _ei._assert_open_regular_path(
                        lock_path,
                        held["handle"],
                        label="state sidecar lock",
                    )
                except BaseException as exc:
                    integrity_error = exc
                held["depth"] -= 1
                if integrity_error is not None:
                    raise integrity_error
            return
        descriptor = os.open(lock_path, flags, 0o600)
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle, lock_type)
            integrity_error = None
            try:
                _ei._assert_open_regular_path(
                    lock_path,
                    handle,
                    label="state sidecar lock",
                )
                held_map[lock_key] = {
                    "handle": handle,
                    "lock_type": lock_type,
                    "depth": 1,
                }
                yield handle
            finally:
                # Run the exit proof even when the protected body raises.  A
                # body failure must not hide that the lock inode was swapped
                # while the supposedly serialized effect was in flight.
                try:
                    _ei._assert_open_regular_path(
                        lock_path,
                        handle,
                        label="state sidecar lock",
                    )
                except BaseException as exc:
                    integrity_error = exc
                held_map.pop(lock_key, None)
                fcntl.flock(handle, fcntl.LOCK_UN)
                if integrity_error is not None:
                    raise integrity_error


@contextmanager
def bot_publication_lock(*, results_dir=None):
    """Lock the one stable no-follow publication/cleanup linearization inode."""

    root = Path(results_dir) if results_dir is not None else Path(_ei.RESULTS_DIR)
    lock_path = root / ".bot_publication.lock"
    _ei._assert_safe_state_parent(lock_path)
    if os.path.lexists(lock_path):
        metadata = os.lstat(lock_path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise OSError(
                "bot publication lock must be a single-link regular file"
            )
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    with _thread_lock_for(lock_path):
        descriptor = os.open(lock_path, flags, 0o600)
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            opened = os.fstat(handle.fileno())
            fcntl.flock(handle, fcntl.LOCK_EX)
            integrity_error = None
            try:
                live = os.lstat(lock_path)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or not stat.S_ISREG(live.st_mode)
                    or opened.st_nlink != 1
                    or live.st_nlink != 1
                    or (opened.st_dev, opened.st_ino)
                    != (live.st_dev, live.st_ino)
                ):
                    raise OSError("bot publication lock path is unsafe")
                yield handle
            finally:
                try:
                    live_after = os.lstat(lock_path)
                    opened_after = os.fstat(handle.fileno())
                    if (
                        opened_after.st_nlink != 1
                        or live_after.st_nlink != 1
                        or (opened_after.st_dev, opened_after.st_ino)
                        != (opened.st_dev, opened.st_ino)
                        or (live_after.st_dev, live_after.st_ino)
                        != (opened.st_dev, opened.st_ino)
                    ):
                        raise OSError("bot publication lock inode changed")
                except BaseException as exc:
                    integrity_error = exc
                fcntl.flock(handle, fcntl.LOCK_UN)
                if integrity_error is not None:
                    raise integrity_error


def _read_regular_state_text(path, *, allow_missing):
    """Read one state file without following links and revalidate after read."""

    path = Path(path)
    if not os.path.lexists(path):
        if allow_missing:
            return ""
        raise FileNotFoundError(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        _ei._assert_open_regular_path(path, handle, label="state data")
        raw = handle.read()
        _ei._assert_open_regular_path(path, handle, label="state data")
    return raw


def _atomic_publish_state_text(path, raw):
    """Publish complete UTF-8 state bytes atomically; caller owns sidecar EX."""

    path = Path(path)
    _ei._assert_safe_state_parent(path)
    if os.path.lexists(path):
        path_stat = os.lstat(path)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or path_stat.st_nlink != 1
        ):
            raise OSError(
                "state data target must be a single-link non-symlink regular file"
            )
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    temporary_identity = None
    try:
        descriptor = os.open(temp, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_identity = os.fstat(handle.fileno())
        os.replace(temp, path)
        published_stat = os.lstat(path)
        if (
            temporary_identity is None
            or not stat.S_ISREG(published_stat.st_mode)
            or stat.S_ISLNK(published_stat.st_mode)
            or published_stat.st_nlink != 1
            or (published_stat.st_dev, published_stat.st_ino)
            != (temporary_identity.st_dev, temporary_identity.st_ino)
            or published_stat.st_size != temporary_identity.st_size
        ):
            raise OSError(
                "atomic state publication did not retain the temporary inode"
            )
        _ei._fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def read_locked_json(path, default=None):
    """Read a JSON file with shared lock. Returns default on any error."""
    try:
        with _ei.locked_file(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


def read_and_maybe_unlink_locked_text(path, should_unlink):
    """Read and conditionally consume one state inode under its EX sidecar.

    The predicate runs while the stable sidecar is exclusively held.  When it
    returns true, this function re-proves that the live path is still the exact
    no-follow, single-link inode that was read before unlinking it and syncing
    the containing directory.  A cooperating atomic writer therefore runs
    wholly before the read or wholly after the durable unlink; a later write is
    never mistaken for the inode selected for consumption.

    Return ``(raw_text, consumed)``.  A missing path is not an error and returns
    ``(None, False)``.  Predicate, authenticity, unlink, and durability errors
    are fail-closed and propagate to the caller.
    """

    if not callable(should_unlink):
        raise TypeError("state consumption predicate must be callable")
    path = Path(path)
    with _ei._locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return None, False

        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            opened = _ei._assert_open_regular_path(
                path,
                handle,
                label="consumable state data",
            )
            raw = handle.read()
            finished = _ei._assert_open_regular_path(
                path,
                handle,
                label="consumable state data",
            )
            if (
                (finished.st_dev, finished.st_ino)
                != (opened.st_dev, opened.st_ino)
                or (
                    finished.st_size,
                    finished.st_mtime_ns,
                    finished.st_ctime_ns,
                )
                != (
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
            ):
                raise OSError("consumable state data changed during read")

            consume = bool(should_unlink(raw))
            if not consume:
                return raw, False

            # The decision and this final path/inode proof share one EX
            # sidecar lease.  In particular, never perform a path-only unlink
            # after releasing the lock: an atomic writer may have installed a
            # new inode by then.
            current = _ei._assert_open_regular_path(
                path,
                handle,
                label="consumable state data",
            )
            if (
                (current.st_dev, current.st_ino)
                != (opened.st_dev, opened.st_ino)
                or (
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                )
                != (
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                )
            ):
                raise OSError("consumable state data changed before unlink")

            os.unlink(path)
            post_unlink_error = None
            try:
                retired = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(retired.st_mode)
                    or (retired.st_dev, retired.st_ino)
                    != (opened.st_dev, opened.st_ino)
                    or retired.st_nlink != 0
                ):
                    raise OSError("consumed state inode retirement is unsafe")

                # An uncooperative writer may create a later inode without the
                # sidecar.  Do not remove it: only prove that it is distinct
                # from the inode selected above.  Cooperating writers cannot
                # reach this point until the sidecar is released.
                if os.path.lexists(path):
                    replacement = os.lstat(path)
                    if (
                        not stat.S_ISREG(replacement.st_mode)
                        or stat.S_ISLNK(replacement.st_mode)
                        or replacement.st_nlink != 1
                        or (replacement.st_dev, replacement.st_ino)
                        == (opened.st_dev, opened.st_ino)
                    ):
                        raise OSError(
                            "replacement state path after consumption is unsafe"
                        )
            except BaseException as exc:
                post_unlink_error = exc

            try:
                _ei._fsync_directory(path.parent)
            except BaseException as sync_exc:
                if post_unlink_error is not None:
                    raise post_unlink_error from sync_exc
                raise
            if post_unlink_error is not None:
                raise post_unlink_error
            return raw, True


def write_locked_json(path, data, indent=2):
    """Atomically and durably publish JSON under the stable sidecar lock."""
    path = Path(path)
    raw = json.dumps(
        data,
        indent=indent,
        ensure_ascii=False,
        allow_nan=False,
    )
    with _ei._locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        _ei._atomic_publish_state_text(path, raw)


def append_locked_jsonl(path, entry):
    """Durably append one JSON row under the same stable sidecar lock."""
    path = Path(path)
    raw = json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n"
    with _ei._locked_state_sidecar(path, lock_type=fcntl.LOCK_EX):
        existed = os.path.lexists(path)
        if existed:
            metadata = os.lstat(path)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise OSError("JSONL append target is unsafe")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            opened = os.fstat(descriptor)
            live = os.lstat(path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or live.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino)
            ):
                raise OSError("JSONL append target identity changed")
            encoded = raw.encode("utf-8")
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("JSONL append made no progress")
                offset += written
            os.fsync(descriptor)
            opened_after = os.fstat(descriptor)
            live_after = os.lstat(path)
            if (
                opened_after.st_nlink != 1
                or live_after.st_nlink != 1
                or (opened_after.st_dev, opened_after.st_ino)
                != (opened.st_dev, opened.st_ino)
                or (live_after.st_dev, live_after.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                raise OSError("JSONL append target changed during write")
        finally:
            os.close(descriptor)
        if not existed:
            _ei._fsync_directory(path.parent)
