"""Crash-recoverable publication contract for daemon evaluation evidence.

The rating daemon owns several files that together describe one rating period.
Top-level JSON files remain compatibility aliases for dashboards, but they are
not a commit boundary: a process can die after replacing one alias and before
replacing the others.  Every successful save therefore copies the complete
payload into an immutable cycle directory and atomically advances a small
manifest pointer only after the directory is durable.

Generation readers and daemon restart recovery follow that pointer.  A crash
before pointer replacement leaves the previous cycle intact; recovery restores
the aliases and truncates append-only histories to the committed byte cutoffs.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any, Iterator


BUNDLE_SCHEMA_VERSION = 3
MANIFEST_FILENAME = "evaluation_cycle_manifest.json"
SELECTION_FILENAME = "selection_snapshot.json"
CYCLES_DIRNAME = "evaluation_cycles"
LOCK_FILENAME = ".evaluation_cycle.lock"
BUNDLE_FILES = {
    "h2h": "head_to_head.json",
    "bot_stats": "bot_stats.json",
    "ratings": "glicko_ratings.json",
    "selection": SELECTION_FILENAME,
    "daemon_stats": "elo_daemon_stats.json",
}
APPEND_LOGS = {
    "match_history": "match_history.jsonl",
    "rating_history": "rating_history.jsonl",
}
MAX_RETAINED_CYCLES = 3
_CYCLE_NAME_RE = re.compile(r"[0-9]{8}-[A-Za-z0-9_-]+-[0-9a-f]{24}")


def _infra():
    import evolution_infra

    return evolution_infra


def _canonical_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _identity_digest(results_dir: Path) -> str | None:
    path = results_dir / "evaluation_data_manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    claimed = str(payload.get("manifest_digest") or "")
    actual = _canonical_digest({
        key: value for key, value in payload.items() if key != "manifest_digest"
    })
    if not claimed or claimed != actual:
        return None
    try:
        from evaluation_data_identity import base_evaluation_identity

        if payload.get("base_identity") != base_evaluation_identity():
            return None
    except Exception:
        return None
    return claimed


def validated_evaluation_identity_digest(
    results_dir: str | Path | None = None,
) -> str | None:
    """Return the current identity only when its own manifest is authentic."""
    root = Path(results_dir) if results_dir is not None else _infra().RESULTS_DIR
    return _identity_digest(root)


def manifest_path(results_dir: str | Path | None = None) -> Path:
    root = Path(results_dir) if results_dir is not None else _infra().RESULTS_DIR
    return root / MANIFEST_FILENAME


def selection_path(results_dir: str | Path | None = None) -> Path:
    root = Path(results_dir) if results_dir is not None else _infra().RESULTS_DIR
    return root / SELECTION_FILENAME


@contextmanager
def evaluation_cycle_lock(
    results_dir: str | Path | None = None,
    *,
    exclusive: bool,
) -> Iterator[None]:
    """Serialize cycle publication/recovery with generation snapshot readers."""
    root = Path(results_dir) if results_dir is not None else _infra().RESULTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_bytes_locked(path: Path) -> bytes:
    """Read under the enclosing cycle lock without mutating immutable dirs."""

    import stat

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(
            f"unsafe or missing evaluation payload: {path.name}"
        ) from exc
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
            raise ValueError(f"unsafe evaluation payload: {path.name}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        opened_after = os.fstat(descriptor)
        live_after = os.lstat(path)
        if (
            opened_after.st_nlink != 1
            or live_after.st_nlink != 1
            or (
                opened_after.st_dev,
                opened_after.st_ino,
                opened_after.st_size,
                opened_after.st_mtime_ns,
                opened_after.st_ctime_ns,
            )
            != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            or (
                live_after.st_dev,
                live_after.st_ino,
                live_after.st_size,
                live_after.st_mtime_ns,
                live_after.st_ctime_ns,
            )
            != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
        ):
            raise ValueError(f"evaluation payload changed: {path.name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_optional_bytes_locked(path: Path) -> bytes:
    if not path.exists():
        return b""
    return _read_bytes_locked(path)


def _read_manifest_locked(path: Path) -> dict[str, Any] | None:
    payload = _infra().read_locked_json(path, default=None)
    return payload if isinstance(payload, dict) else None


def _write_file_durable(path: Path, payload: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_alias_atomic(path: Path, payload: bytes) -> None:
    """Restore one compatibility alias without parsing/reserializing it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.cycle-restore-{os.getpid()}")
    _write_file_durable(temporary, payload)
    os.replace(temporary, path)


def _cycle_directory(root: Path, relpath: str) -> Path | None:
    candidate = Path(str(relpath or ""))
    if candidate.is_absolute() or len(candidate.parts) != 2:
        return None
    if candidate.parts[0] != CYCLES_DIRNAME or not _CYCLE_NAME_RE.fullmatch(candidate.parts[1]):
        return None
    directory = root / candidate
    if directory.is_symlink() or not directory.is_dir():
        return None
    try:
        if directory.resolve().parent != (root / CYCLES_DIRNAME).resolve():
            return None
    except OSError:
        return None
    return directory


def _validate_selection(
    selection_payload: dict[str, Any],
    *,
    save_num: int,
    active_bots: list[str],
) -> None:
    if int(selection_payload.get("save_num", -1)) != int(save_num):
        raise ValueError("selection snapshot save_num does not match publication")
    selection_active = selection_payload.get("active_bots")
    if not isinstance(selection_active, list) or sorted(
        str(name) for name in selection_active
    ) != active_bots:
        raise ValueError("selection snapshot active pool does not match publication")
    rows = selection_payload.get("rows")
    if not isinstance(rows, list) or sorted(
        str(row.get("name")) for row in rows if isinstance(row, dict)
    ) != active_bots:
        raise ValueError("selection snapshot rows do not match publication active pool")


def _bundle_semantic_issues(
    parsed_files: dict[str, dict[str, Any]],
    *,
    save_num: int,
    active_bots: list[str],
) -> list[str]:
    """Validate relationships that per-file hashes cannot express."""
    issues: list[str] = []
    active = set(active_bots)
    ratings = parsed_files.get("ratings") or {}
    bot_stats = parsed_files.get("bot_stats") or {}
    h2h = parsed_files.get("h2h") or {}
    selection = parsed_files.get("selection") or {}

    if set(str(name) for name in ratings) != active:
        issues.append("ratings_active_pool_mismatch")
    inactive_stats = sorted(str(name) for name in bot_stats if str(name) not in active)
    if inactive_stats:
        issues.append("bot_stats_contains_inactive_bots")

    h2h_games_by_bot = {name: 0 for name in active_bots}
    for key, entry in h2h.items():
        parts = [part.strip() for part in str(key).split(" vs ")]
        if (
            len(parts) != 2
            or parts[0] == parts[1]
            or any(name not in active for name in parts)
            or not isinstance(entry, dict)
        ):
            issues.append(f"h2h_pair_invalid:{key}")
            continue
        try:
            games = int(entry.get("games", 0) or 0)
            a_wins = int(entry.get("a_wins", 0) or 0)
            b_wins = int(entry.get("b_wins", 0) or 0)
            draws = int(entry.get("draws", 0) or 0)
        except (TypeError, ValueError):
            issues.append(f"h2h_counts_invalid:{key}")
            continue
        if min(games, a_wins, b_wins, draws) < 0 or games != a_wins + b_wins + draws:
            issues.append(f"h2h_counts_invalid:{key}")
            continue
        h2h_games_by_bot[parts[0]] += games
        h2h_games_by_bot[parts[1]] += games

    try:
        selection_save_num_matches = int(selection.get("save_num", -1)) == int(save_num)
    except (TypeError, ValueError):
        selection_save_num_matches = False
    if not selection_save_num_matches:
        issues.append("selection_snapshot_save_num_mismatch")
    rows = selection.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            if name not in active:
                continue
            rating = ratings.get(name)
            if isinstance(rating, dict):
                try:
                    expected_fields = {
                        "rating": round(float(rating.get("r", 1500.0)), 1),
                        "rd": round(float(rating.get("rd", 350.0)), 1),
                        "sigma": round(float(rating.get("sigma", 0.06)), 4),
                    }
                except (TypeError, ValueError):
                    issues.append(f"ratings_fields_invalid:{name}")
                    expected_fields = {}
                for field, expected in expected_fields.items():
                    if field not in row:
                        continue
                    try:
                        matches = abs(float(row[field]) - expected) <= 1e-9
                    except (TypeError, ValueError):
                        matches = False
                    if not matches:
                        issues.append(f"selection_{field}_mismatch:{name}")
            if "h2h_games" in row:
                try:
                    matches = int(row["h2h_games"]) == h2h_games_by_bot[name]
                except (TypeError, ValueError):
                    matches = False
                if not matches:
                    issues.append(f"selection_h2h_games_mismatch:{name}")
            if name in bot_stats and "games" in row:
                try:
                    matches = int(row["games"]) == int(
                        (bot_stats.get(name) or {}).get("games", 0) or 0
                    )
                except (TypeError, ValueError, AttributeError):
                    matches = False
                if not matches:
                    issues.append(f"selection_games_mismatch:{name}")
    return issues


def _append_log_semantic_issues(
    raw_append_logs: dict[str, bytes],
    *,
    evaluation_identity_digest: str,
    replay_dir: Path,
) -> list[str]:
    """Reject cross-epoch history before an immutable cycle can bind it."""

    from rating_snapshot import _admitted_70_hand_history_sample

    issues: list[str] = []
    expected_epoch = "national_tcp_policy_v1"
    expected_mode = "native_tcp"
    for role in ("match_history", "rating_history"):
        payload = raw_append_logs.get(role, b"")
        for line_number, raw_line in enumerate(payload.splitlines(), start=1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                issues.append(f"{role}_row_invalid_json:{line_number}")
                continue
            if not isinstance(row, dict):
                issues.append(f"{role}_row_not_object:{line_number}")
                continue
            if row.get("evaluation_epoch") != expected_epoch:
                issues.append(f"{role}_row_epoch_mismatch:{line_number}")
            if row.get("execution_mode") != expected_mode:
                issues.append(f"{role}_row_execution_mode_mismatch:{line_number}")
            if row.get("evaluation_identity_digest") != evaluation_identity_digest:
                issues.append(f"{role}_row_identity_mismatch:{line_number}")
            if (
                role == "match_history"
                and row.get("strength_admitted") is True
                and _admitted_70_hand_history_sample(
                    row,
                    expected_evaluation_identity_digest=(
                        evaluation_identity_digest
                    ),
                    replay_dir=replay_dir,
                ) is None
            ):
                issues.append(
                    f"match_history_strength_admission_invalid:{line_number}"
                )
            if len(issues) >= 32:
                return issues
    return issues


def publish_evaluation_cycle_manifest(
    *,
    save_num: int,
    daemon_run_id: str | None,
    active_bots: list[str] | tuple[str, ...],
    results_dir: str | Path | None = None,
    evaluation_identity_digest: str | None = None,
    expected_previous_manifest_digest: str | None = None,
    expected_previous_save_num: int | None = None,
    require_predecessor_match: bool = False,
    writer_lease_fd: int | None = None,
    _test_only_allow_unleased: bool = False,
    _lock_held: bool = False,
) -> dict[str, Any]:
    """Durably publish one immutable cycle and atomically advance its pointer.

    Normal callers let this function acquire the exclusive cycle lock.  The
    daemon's multi-file writer already owns it and passes ``_lock_held=True``.
    """
    root = Path(results_dir) if results_dir is not None else _infra().RESULTS_DIR
    if not _lock_held:
        with evaluation_cycle_lock(root, exclusive=True):
            return publish_evaluation_cycle_manifest(
                save_num=save_num,
                daemon_run_id=daemon_run_id,
                active_bots=active_bots,
                results_dir=root,
                evaluation_identity_digest=evaluation_identity_digest,
                expected_previous_manifest_digest=expected_previous_manifest_digest,
                expected_previous_save_num=expected_previous_save_num,
                require_predecessor_match=require_predecessor_match,
                writer_lease_fd=writer_lease_fd,
                _test_only_allow_unleased=_test_only_allow_unleased,
                _lock_held=True,
            )

    root.mkdir(parents=True, exist_ok=True)
    if not _test_only_allow_unleased:
        if writer_lease_fd is None:
            raise ValueError("evaluation publication requires daemon writer lease")
        lease_path = root / ".evaluation_daemon_writer.lock"
        try:
            lease_stat = lease_path.stat()
            descriptor_stat = os.fstat(int(writer_lease_fd))
            if (lease_stat.st_dev, lease_stat.st_ino) != (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ):
                raise ValueError("evaluation writer lease descriptor mismatch")
            fcntl.flock(int(writer_lease_fd), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception as exc:
            raise ValueError("evaluation publication writer lease is not held") from exc
    if require_predecessor_match:
        predecessor = _read_manifest_locked(root / MANIFEST_FILENAME)
        if predecessor is None:
            if expected_previous_manifest_digest is not None:
                raise ValueError("evaluation cycle predecessor manifest disappeared")
        else:
            if str(predecessor.get("manifest_digest") or "") != str(
                expected_previous_manifest_digest or ""
            ):
                raise ValueError("evaluation cycle predecessor digest changed")
            if int(predecessor.get("save_num", -1)) != int(
                expected_previous_save_num if expected_previous_save_num is not None else -1
            ):
                raise ValueError("evaluation cycle predecessor save_num changed")
    normalized_active = sorted(str(name) for name in active_bots)
    raw_files: dict[str, bytes] = {}
    parsed_files: dict[str, dict[str, Any]] = {}
    file_contracts: dict[str, dict[str, Any]] = {}
    for role, filename in BUNDLE_FILES.items():
        payload = _read_bytes_locked(root / filename)
        parsed = json.loads(payload.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError(f"evaluation payload is not an object: {filename}")
        raw_files[role] = payload
        parsed_files[role] = parsed
        file_contracts[role] = {
            "filename": filename,
            "sha256": _sha256(payload),
            "bytes": len(payload),
        }
    _validate_selection(
        parsed_files["selection"],
        save_num=save_num,
        active_bots=normalized_active,
    )
    semantic_issues = _bundle_semantic_issues(
        parsed_files,
        save_num=save_num,
        active_bots=normalized_active,
    )
    if semantic_issues:
        raise ValueError(
            "evaluation bundle semantic mismatch: " + ", ".join(semantic_issues)
        )

    append_contracts: dict[str, dict[str, Any]] = {}
    raw_append_logs: dict[str, bytes] = {}
    for role, filename in APPEND_LOGS.items():
        payload = _read_optional_bytes_locked(root / filename)
        raw_append_logs[role] = payload
        append_contracts[role] = {
            "filename": filename,
            "committed_bytes": len(payload),
            "committed_sha256": _sha256(payload),
        }

    current_identity = _identity_digest(root)
    if current_identity is None:
        raise ValueError("valid evaluation identity manifest is required for publication")
    if (
        evaluation_identity_digest is not None
        and str(evaluation_identity_digest) != current_identity
    ):
        raise ValueError("explicit evaluation identity does not match current manifest")
    identity = current_identity
    append_semantic_issues = _append_log_semantic_issues(
        raw_append_logs,
        evaluation_identity_digest=identity,
        replay_dir=root / "match_replay",
    )
    if append_semantic_issues:
        raise ValueError(
            "evaluation append-log semantic mismatch: "
            + ", ".join(append_semantic_issues)
        )
    # `head_to_head.json` is a cache, never independent strength authority.
    # Exact active-pool W/L/D must rederive from the SHA-bound raw replay rows
    # that this same cycle is about to freeze; equal coverage alone is not
    # enough to detect a tampered stored matrix.
    from rating_snapshot import choose_h2h_source

    h2h_selection = choose_h2h_source(
        normalized_active,
        parsed_files["h2h"],
        root / APPEND_LOGS["match_history"],
        expected_evaluation_identity_digest=identity,
    )
    if h2h_selection.get("integrity_ok") is not True:
        raise ValueError(
            "evaluation H2H/raw replay mismatch: "
            + ", ".join(
                str(issue)
                for issue in (h2h_selection.get("integrity_issues") or [])[:8]
            )
        )
    if h2h_selection.get("stored_h2h") != h2h_selection.get("h2h"):
        # Empty/missing cache is repairable *before* publication, but a cycle
        # must never freeze an empty or stale H2H alias while its own verified
        # raw history already proves a different active-pool projection.
        raise ValueError(
            "evaluation H2H cache was not rebuilt from verified raw match history"
        )
    descriptor = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "save_num": int(save_num),
        "daemon_run_id": str(daemon_run_id or "adhoc"),
        "active_bots": normalized_active,
        "evaluation_identity_digest": identity,
        "files": file_contracts,
        "append_logs": append_contracts,
    }
    cycle_digest = _canonical_digest(descriptor)
    safe_run_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(daemon_run_id or "adhoc"))[:32] or "adhoc"
    cycle_name = f"{int(save_num):08d}-{safe_run_id}-{cycle_digest[:24]}"
    cycles_root = root / CYCLES_DIRNAME
    cycles_root.mkdir(parents=True, exist_ok=True)
    target = cycles_root / cycle_name
    if target.is_symlink():
        raise ValueError("unsafe immutable evaluation cycle target")
    if not target.exists():
        temporary = Path(tempfile.mkdtemp(prefix=".cycle-", dir=cycles_root))
        try:
            for role, filename in BUNDLE_FILES.items():
                _write_file_durable(temporary / filename, raw_files[role])
            for role, filename in APPEND_LOGS.items():
                _write_file_durable(temporary / filename, raw_append_logs[role])
            directory_fd = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            os.rename(temporary, target)
            parent_fd = os.open(cycles_root, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
    elif not target.is_dir():
        raise ValueError("immutable evaluation cycle path is not a directory")

    # Verify an existing deterministic target before ever pointing at it.  This
    # covers idempotent publication after a crash between rename and manifest.
    for role, filename in BUNDLE_FILES.items():
        persisted = _read_bytes_locked(target / filename)
        if persisted != raw_files[role]:
            raise ValueError(f"immutable evaluation cycle collision: {role}")
    for role, filename in APPEND_LOGS.items():
        persisted = _read_bytes_locked(target / filename)
        if persisted != raw_append_logs[role]:
            raise ValueError(f"immutable evaluation cycle collision: {role}")

    manifest = {
        **descriptor,
        "published_at": time.time(),
        "cycle_digest": cycle_digest,
        "cycle_dir": f"{CYCLES_DIRNAME}/{cycle_name}",
    }
    manifest["manifest_digest"] = _canonical_digest(manifest)
    _infra().write_locked_json(root / MANIFEST_FILENAME, manifest)
    root_fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    # Bounded retention: immutable JSONL copies make a cycle self-recoverable,
    # so keep only a small forensic window after the pointer is durable.
    cycle_dirs = sorted(
        (
            path for path in cycles_root.iterdir()
            if path.is_dir() and not path.is_symlink() and _CYCLE_NAME_RE.fullmatch(path.name)
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in cycle_dirs[MAX_RETAINED_CYCLES:]:
        if stale != target:
            shutil.rmtree(stale, ignore_errors=True)
    return manifest


def load_published_evaluation_bundle(
    results_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Load the last immutable committed cycle or fail closed."""
    root = Path(results_dir) if results_dir is not None else _infra().RESULTS_DIR
    path = root / MANIFEST_FILENAME
    with evaluation_cycle_lock(root, exclusive=False):
        return _load_published_evaluation_bundle_locked(root, path)


def load_current_strict_evaluation_bundle(
    results_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Bind the immutable cycle to the live strict epoch and published pool.

    This is the single read-only authority for dashboards and operator UI
    projections.  Missing reset proof, an empty/unavailable published pool, or
    a cycle produced for another pool all fail closed without consulting the
    mutable top-level compatibility aliases.
    """

    root = Path(results_dir) if results_dir is not None else _infra().RESULTS_DIR
    try:
        from system_strict_bootstrap import load_policy_epoch_reset_receipt

        reset_receipt, reset_errors = load_policy_epoch_reset_receipt(root)
    except Exception:
        return {"available": False, "reason": "policy_epoch_reset_unavailable"}
    if reset_errors or not isinstance(reset_receipt, dict):
        return {"available": False, "reason": "policy_epoch_reset_unavailable"}
    try:
        from bot_namespace import (
            FIRST_STRICT_POLICY_VERSION,
            parse_bot_version,
            version_sort_key,
        )
        from evolution_infra import get_published_active_bots_read_only

        active_bots = list(get_published_active_bots_read_only())
        if len(active_bots) != len(set(active_bots)):
            raise ValueError("duplicate active bot")
        for name in active_bots:
            version = parse_bot_version(name)
            if version is None or version < FIRST_STRICT_POLICY_VERSION:
                raise ValueError("non-strict active bot")
        active_bots = sorted(active_bots, key=version_sort_key)
    except Exception:
        return {"available": False, "reason": "active_pool_unavailable"}
    if not active_bots:
        return {
            "available": False,
            "reason": "strict_published_active_pool_empty",
            "active_bots": [],
        }

    try:
        bundle = load_published_evaluation_bundle(root)
    except Exception:
        return {"available": False, "reason": "evaluation_bundle_unavailable"}
    if not isinstance(bundle, dict):
        return {"available": False, "reason": "evaluation_bundle_unavailable"}
    if bundle.get("available") is not True:
        return bundle
    manifest = bundle.get("manifest")
    if not isinstance(manifest, dict) or sorted(
        str(name) for name in (manifest.get("active_bots") or [])
    ) != sorted(active_bots):
        return {"available": False, "reason": "evaluation_active_pool_mismatch"}
    return {
        **bundle,
        "active_bots": active_bots,
        "epoch_reset_receipt": reset_receipt,
    }


def _load_published_evaluation_bundle_locked(
    root: Path,
    path: Path,
) -> dict[str, Any]:
    first = _read_manifest_locked(path)
    if first is None:
        return {"available": False, "reason": "cycle_manifest_missing"}

    issues: list[str] = []
    if first.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        issues.append("cycle_manifest_schema_mismatch")
    claimed = str(first.get("manifest_digest") or "")
    actual = _canonical_digest({
        key: value for key, value in first.items() if key != "manifest_digest"
    })
    if claimed != actual:
        issues.append("cycle_manifest_digest_mismatch")
    current_identity = _identity_digest(root)
    if current_identity is None:
        issues.append("cycle_manifest_evaluation_identity_invalid")
    elif first.get("evaluation_identity_digest") != current_identity:
        issues.append("cycle_manifest_evaluation_identity_mismatch")
    cycle_directory = _cycle_directory(root, str(first.get("cycle_dir") or ""))
    if cycle_directory is None:
        issues.append("cycle_manifest_directory_invalid")

    file_contracts = first.get("files")
    if not isinstance(file_contracts, dict):
        issues.append("cycle_manifest_files_missing")
        file_contracts = {}
    parsed_files: dict[str, dict[str, Any]] = {}
    raw_files: dict[str, bytes] = {}
    if cycle_directory is not None:
        for role, expected_filename in BUNDLE_FILES.items():
            contract = file_contracts.get(role)
            if not isinstance(contract, dict):
                issues.append(f"cycle_manifest_{role}_missing")
                continue
            if contract.get("filename") != expected_filename:
                issues.append(f"cycle_manifest_{role}_filename_mismatch")
                continue
            try:
                payload = _read_bytes_locked(cycle_directory / expected_filename)
                parsed = json.loads(payload.decode("utf-8"))
            except Exception as exc:
                issues.append(f"cycle_payload_{role}_read_failed:{type(exc).__name__}")
                continue
            if not isinstance(parsed, dict):
                issues.append(f"cycle_payload_{role}_not_object")
                continue
            if contract.get("sha256") != _sha256(payload):
                issues.append(f"cycle_payload_{role}_digest_mismatch")
            if int(contract.get("bytes", -1)) != len(payload):
                issues.append(f"cycle_payload_{role}_size_mismatch")
            parsed_files[role] = parsed
            raw_files[role] = payload

    append_contracts = first.get("append_logs")
    if not isinstance(append_contracts, dict):
        issues.append("cycle_manifest_append_logs_missing")
        append_contracts = {}
    raw_append_logs: dict[str, bytes] = {}
    for role, expected_filename in APPEND_LOGS.items():
        contract = append_contracts.get(role)
        if not isinstance(contract, dict):
            issues.append(f"cycle_manifest_{role}_missing")
            continue
        if contract.get("filename") != expected_filename:
            issues.append(f"cycle_manifest_{role}_filename_mismatch")
            continue
        try:
            committed_bytes = int(contract.get("committed_bytes", -1))
            if cycle_directory is None:
                continue
            payload = _read_bytes_locked(cycle_directory / expected_filename)
            if len(payload) != committed_bytes:
                issues.append(f"cycle_append_{role}_size_mismatch")
            if contract.get("committed_sha256") != _sha256(payload):
                issues.append(f"cycle_append_{role}_digest_mismatch")
            raw_append_logs[role] = payload
        except Exception as exc:
            issues.append(f"cycle_append_{role}_read_failed:{type(exc).__name__}")

    second = _read_manifest_locked(path)
    if second is None or second.get("manifest_digest") != claimed:
        issues.append("cycle_manifest_changed_while_reading")

    normalized_active = sorted(str(name) for name in (first.get("active_bots") or []))
    selection = parsed_files.get("selection") or {}
    try:
        selection_save_num_matches = int(selection.get("save_num", -1)) == int(
            first.get("save_num", -2)
        )
    except (TypeError, ValueError):
        selection_save_num_matches = False
    if not selection_save_num_matches:
        issues.append("selection_snapshot_save_num_mismatch")
    selection_active = selection.get("active_bots")
    if not isinstance(selection_active, list) or sorted(
        str(name) for name in selection_active
    ) != normalized_active:
        issues.append("selection_snapshot_active_pool_mismatch")
    rows = selection.get("rows")
    if not isinstance(rows, list):
        issues.append("selection_snapshot_rows_missing")
    else:
        row_names = sorted(
            str(row.get("name")) for row in rows if isinstance(row, dict)
        )
        if row_names != normalized_active:
            issues.append("selection_snapshot_row_pool_mismatch")

    if len(parsed_files) == len(BUNDLE_FILES):
        issues.extend(
            _bundle_semantic_issues(
                parsed_files,
                save_num=int(first.get("save_num", -1)),
                active_bots=normalized_active,
            )
        )
    # A valid immutable file digest proves only that the cycle copied its own
    # bytes consistently.  Its active H2H remains strength authority only
    # while the copied history still resolves to the exact SHA-bound raw
    # replays retained under the live results root.
    if (
        current_identity is not None
        and cycle_directory is not None
        and len(parsed_files) == len(BUNDLE_FILES)
        and "match_history" in raw_append_logs
    ):
        append_issues = _append_log_semantic_issues(
            raw_append_logs,
            evaluation_identity_digest=current_identity,
            replay_dir=root / "match_replay",
        )
        issues.extend(
            f"cycle_{issue}" for issue in append_issues
        )
        try:
            from rating_snapshot import choose_h2h_source

            h2h_selection = choose_h2h_source(
                normalized_active,
                parsed_files["h2h"],
                cycle_directory / APPEND_LOGS["match_history"],
                expected_evaluation_identity_digest=current_identity,
                replay_dir=root / "match_replay",
            )
            if h2h_selection.get("integrity_ok") is not True:
                issues.append("cycle_h2h_raw_history_integrity_failure")
            elif h2h_selection.get("stored_h2h") != h2h_selection.get("h2h"):
                issues.append("cycle_h2h_cache_not_exact_raw_history_projection")
        except Exception:
            issues.append("cycle_h2h_raw_history_validation_failed")

    if issues:
        return {
            "available": False,
            "reason": "cycle_bundle_integrity_failure",
            "issues": issues,
            "manifest_digest": claimed,
        }
    return {
        "available": True,
        "manifest": first,
        "manifest_digest": claimed,
        "h2h": parsed_files["h2h"],
        "bot_stats": parsed_files["bot_stats"],
        "ratings": parsed_files["ratings"],
        "selection": parsed_files["selection"],
        "daemon_stats": parsed_files["daemon_stats"],
        "raw_files": raw_files,
        "raw_append_logs": raw_append_logs,
    }


def recover_published_evaluation_bundle(
    results_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Restore aliases/log cutoffs from the last committed immutable cycle."""
    root = Path(results_dir) if results_dir is not None else _infra().RESULTS_DIR
    with evaluation_cycle_lock(root, exclusive=True):
        bundle = _load_published_evaluation_bundle_locked(
            root,
            root / MANIFEST_FILENAME,
        )
        if not bundle.get("available"):
            return bundle
        for role, filename in BUNDLE_FILES.items():
            _write_alias_atomic(root / filename, bundle["raw_files"][role])
        for role, filename in APPEND_LOGS.items():
            _write_alias_atomic(root / filename, bundle["raw_append_logs"][role])
        return bundle
