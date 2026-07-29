#!/usr/bin/env python3
"""Quarantine pre-authority abandon state and abandon its exact checkpoint.

This is a stopped-runtime, operator-only recovery command.  It never reruns the
one-time epoch reset and never converts legacy abandon rows into allocation
authority.  The old ledger, checkpoint, candidate and sidecars are moved into
``archive/`` under a digest-bound claim.  A schema-v1 checkpoint whose target
is the *current* allocation successor may then produce exactly one schema-v2
terminal receipt; a historical large jump is quarantined without burning any
of the skipped labels.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from bot_artifact import canonical_digest  # noqa: E402
from bot_namespace import (  # noqa: E402
    ARCHIVED_VERSION_HIGH_WATER,
    EVALUATION_EPOCH,
    EVOLUTION_BRANCH,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
    bot_tag,
    high_water_tag,
    resolve_version_namespace_authority,
)
from checkpoint_schema import (  # noqa: E402
    CheckpointSchemaError,
    upgrade_legacy_checkpoint_for_controlled_abandon,
)
from system_strict_bootstrap import validate_policy_epoch_reset_archive  # noqa: E402
from epoch_authority import (  # noqa: E402
    validate_schema2_abandon_claim_structure,
    validate_schema2_abandon_finalize_receipt,
    validate_schema2_abandon_ledger_history,
)


RESULTS = CORE / "results"
BOTS = ROOT / "bots"
ARCHIVE_BASE = ROOT / (
    "archive/evolution_epochs/national_tcp_policy_v1/runtime_reconciliation"
)
RESET_RECEIPT = RESULTS / "policy_epoch_reset_receipt.json"
LEDGER = RESULTS / "abandoned_versions.jsonl"
CHECKPOINT = RESULTS / "pipeline_state.json"
LIVE_CLAIM = RESULTS / "policy_epoch_reconciliation_claim.json"
LIVE_RECEIPT = RESULTS / "policy_epoch_reconciliation_receipt.json"
RECORDED_FINALIZE_RECEIPT = (
    RESULTS / "policy_epoch_recorded_abandon_finalize_receipt.json"
)
RUNTIME_CHECKOUT_NAME = ".evolution_pok"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_COST_LEDGER_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_TREE_BYTES = 64 * 1024 * 1024
MAX_CANDIDATE_TREE_ENTRIES = 10_000
RUNTIME_CONTROL_FILE_ORDER = (
    ".daemon_pid",
    "orchestrator_session.json",
    "generation_cost_pending.json",
)
RUNTIME_CONTROL_FILENAMES = frozenset(RUNTIME_CONTROL_FILE_ORDER)
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

LEGACY_LEDGER_KEYS = frozenset({
    "v",
    "reason",
    "ts",
    "timestamp",
    "workflow_run_id",
    "infra_failure",
})


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"git {args[0]} failed")
    return completed.stdout.strip()


def _git_explicit_presence(*args: str) -> bool:
    """Return a destructive Git predicate only for rc=0/explicit rc=1.

    Git uses rc=1 for the absence reported by ``ls-files --error-unmatch`` and
    ``show-ref --verify``.  Timeout, repository corruption, executable failure,
    or any other return code is unavailable authority and must preserve bytes.
    """

    completed = subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise RuntimeError(
        completed.stderr.strip()
        or f"git {' '.join(args)} failed with rc={completed.returncode}"
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_bytes(payload: dict) -> bytes:
    return (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_directory_chain(
    path: Path,
    *,
    base: Path,
    create: bool,
) -> Path:
    """Validate/create a directory without following a pre-positioned symlink."""

    path = Path(path)
    base = Path(base)
    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise RuntimeError(f"reconciliation directory escaped trust root: {path}") from exc
    if base.is_symlink() or not base.is_dir():
        raise RuntimeError(f"reconciliation directory base is unsafe: {base}")
    cursor = base
    for part in relative.parts:
        cursor = cursor / part
        if os.path.lexists(cursor):
            metadata = os.lstat(cursor)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError(
                    f"reconciliation directory ancestor is unsafe: {cursor}"
                )
            continue
        if not create:
            # Missing descendants are safe for a dry-run claim, provided every
            # existing ancestor above them was a real directory.
            continue
        os.mkdir(cursor, 0o700)
        _fsync_directory(cursor.parent)
        metadata = os.lstat(cursor)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"reconciliation directory creation was unsafe: {cursor}")
    return path


def _validate_runtime_input_roots() -> None:
    """Prove every ROOT→RESULTS/BOTS component is a real directory."""

    for path in (RESULTS, BOTS):
        _safe_directory_chain(path, base=ROOT, create=False)
        if not os.path.lexists(path):
            raise RuntimeError(f"reconciliation runtime root is missing: {path}")
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"reconciliation runtime root is unsafe: {path}")


def _archive_root_from_relative(
    relative: object,
    *,
    create: bool = False,
    require_exists: bool = False,
) -> Path:
    if not isinstance(relative, str) or not relative:
        raise RuntimeError("reconciliation archive path is invalid")
    parsed = Path(relative)
    if (
        parsed.is_absolute()
        or parsed.as_posix() != relative
        or any(part in {"", ".", ".."} for part in parsed.parts)
    ):
        raise RuntimeError("reconciliation archive path is not canonical relative")
    archive_root = ROOT / parsed
    if (
        archive_root.parent != ARCHIVE_BASE
        or re.fullmatch(r"legacy-[0-9a-f]{12}-[0-9a-f]{12}", archive_root.name)
        is None
    ):
        raise RuntimeError("reconciliation archive path is outside canonical root")
    _safe_directory_chain(ARCHIVE_BASE, base=ROOT, create=create)
    _safe_directory_chain(archive_root, base=ROOT, create=create)
    if require_exists and not archive_root.is_dir():
        raise RuntimeError("reconciliation archive root is missing")
    return archive_root


def _safe_read_bytes(path: Path, *, max_bytes: int = MAX_INPUT_BYTES) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"unsafe reconciliation input path: {path}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        live_before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(live_before.st_mode)
            or before.st_nlink != 1
            or live_before.st_nlink != 1
            or before.st_dev != live_before.st_dev
            or before.st_ino != live_before.st_ino
        ):
            raise RuntimeError(f"unsafe reconciliation input path: {path}")
        if before.st_size > max_bytes:
            raise RuntimeError(f"reconciliation input exceeds byte limit: {path}")
        payload = handle.read(max_bytes + 1)
        after = os.fstat(handle.fileno())
        live_after = os.lstat(path)
        if len(payload) > max_bytes or (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_nlink,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_nlink,
        ) or (live_after.st_dev, live_after.st_ino) != (
            before.st_dev,
            before.st_ino,
        ) or live_after.st_nlink != 1:
            raise RuntimeError(f"reconciliation input changed while read: {path}")
    return payload


def _safe_read_prefix_bytes(path: Path, size: int) -> bytes:
    """Read an immutable historical prefix while permitting later appends."""

    if type(size) is not int or not 0 <= size <= MAX_COST_LEDGER_BYTES:
        raise RuntimeError("reconciliation prefix size is invalid")
    if not os.path.lexists(path):
        if size == 0:
            return b""
        raise RuntimeError(f"reconciliation prefix input is missing: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"unsafe reconciliation input path: {path}") from exc
    try:
        before = os.fstat(descriptor)
        live_before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(live_before.st_mode)
            or before.st_nlink != 1
            or live_before.st_nlink != 1
            or (before.st_dev, before.st_ino)
            != (live_before.st_dev, live_before.st_ino)
            or before.st_size < size
        ):
            raise RuntimeError(f"unsafe reconciliation prefix input: {path}")
        first = os.pread(descriptor, size, 0)
        second = os.pread(descriptor, size, 0)
        after = os.fstat(descriptor)
        live_after = os.lstat(path)
        if (
            len(first) != size
            or first != second
            or after.st_nlink != 1
            or live_after.st_nlink != 1
            or after.st_size < size
            or (after.st_dev, after.st_ino)
            != (before.st_dev, before.st_ino)
            or (live_after.st_dev, live_after.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise RuntimeError(
                f"reconciliation prefix changed while read: {path}"
            )
        return first
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(_safe_read_bytes(path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"reconciliation JSON is unreadable: {path}: {type(exc).__name__}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"reconciliation JSON is not an object: {path}")
    return value


def _write_json_exclusive(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        raw = _canonical_bytes(payload)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("reconciliation claim write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _write_bytes_exclusive(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("reconciliation evidence write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _replace_json(path: Path, payload: dict) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        _write_json_exclusive(temp, payload)
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _reconciliation_lock():
    _safe_directory_chain(ARCHIVE_BASE, base=ROOT, create=True)
    lock_path = ARCHIVE_BASE / ".reconciliation.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        live = os.lstat(lock_path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(live.st_mode)
            or (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino)
        ):
            raise RuntimeError("reconciliation lock path is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
        live_after = os.lstat(lock_path)
        if (opened.st_dev, opened.st_ino) != (
            live_after.st_dev,
            live_after.st_ino,
        ):
            raise RuntimeError("reconciliation lock inode changed")
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _publication_linearization_lock():
    """Share the exact publication mutex before checkpoint/ledger sidecars."""

    import evolution_infra

    with evolution_infra.bot_publication_lock(results_dir=RESULTS):
        yield


def _runtime_checkout_identity_errors() -> list[str]:
    errors: list[str] = []
    root = ROOT.resolve()
    if root.name != RUNTIME_CHECKOUT_NAME:
        errors.append(
            "reconciliation_requires_autonomous_runtime_checkout:"
            f"expected={RUNTIME_CHECKOUT_NAME}:actual={root}"
        )
    try:
        if Path(_git("rev-parse", "--show-toplevel")).resolve() != root:
            errors.append("reconciliation_git_root_mismatch")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != EVOLUTION_BRANCH:
            errors.append(f"reconciliation_requires_evolution_branch:{branch}")
        if _git("status", "--porcelain", "--untracked-files=no"):
            errors.append("reconciliation_tracked_worktree_not_clean")
        if _git("rev-parse", "HEAD") != _git("rev-parse", f"origin/{EVOLUTION_BRANCH}"):
            errors.append(
                "reconciliation_runtime_not_synced_to_origin_branch:"
                f"{EVOLUTION_BRANCH}"
            )
    except Exception as exc:
        errors.append(f"reconciliation_git_identity_unavailable:{type(exc).__name__}")
    return list(dict.fromkeys(errors))


def _pid_record_identity(path: Path) -> str:
    try:
        raw = _safe_read_bytes(path, max_bytes=64 * 1024).decode("utf-8").strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = int(raw)
        record = value if isinstance(value, dict) else {"pid": int(value)}
        pid = int(record.get("pid") or 0)
        expected_start = int(record.get("start_ticks") or 0)
    except Exception:
        return "invalid"
    if pid <= 1:
        return "invalid"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        pass
    except OSError:
        return "dead"
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().split()
        actual_start = int(fields[21])
        state = fields[2]
    except (OSError, ValueError, IndexError):
        return "unavailable"
    if state in {"Z", "X"}:
        return "dead"
    if expected_start > 0 and actual_start != expected_start:
        return "reused"
    return "live" if expected_start > 0 else "unverifiable_live"


def _runtime_process_errors() -> list[str]:
    """Require no live runtime process; a proven-dead PID marker is recoverable."""

    errors: list[str] = []
    pid_file = RESULTS / ".daemon_pid"
    if os.path.lexists(pid_file):
        identity = _pid_record_identity(pid_file)
        if identity not in {"dead", "reused"}:
            errors.append(f"reconciliation_daemon_pid_identity_{identity}")
    proc = Path("/proc")
    if proc.is_dir():
        ancestors = {os.getpid()}
        cursor = os.getpid()
        while cursor > 1:
            try:
                fields = (proc / str(cursor) / "stat").read_text().split()
                cursor = int(fields[3])
            except (OSError, ValueError, IndexError):
                break
            ancestors.add(cursor)
        root = ROOT.resolve()
        root_token = os.fsencode(str(root))
        for child in proc.iterdir():
            if not child.name.isdigit() or int(child.name) in ancestors:
                continue
            try:
                cwd = (child / "cwd").resolve(strict=True)
                command = (child / "cmdline").read_bytes().replace(b"\0", b" ")
            except (OSError, PermissionError):
                continue
            in_checkout = cwd == root or root in cwd.parents
            names_checkout = root_token in command
            runtime_command = any(token in command for token in (
                b"web/main.py",
                b"orchestrator.py",
                b"elo_daemon.py",
                b"official_certify.py",
                b"official_platform",
                b"sever/main.py",
                b"wine",
            ))
            # A live runtime process must BOTH look like one of the known
            # runtime commands AND run inside (or reference) the autonomous
            # checkout. Requiring cwd-in-checkout alone produced false
            # positives on shell helpers (head/cat/grep) that happened to
            # inherit the runtime checkout as their working directory.
            if runtime_command and (in_checkout or names_checkout):
                errors.append(f"reconciliation_runtime_process_alive:pid={child.name}")
    return list(dict.fromkeys(errors))


def _version_authority_high_water() -> int:
    return int(resolve_version_namespace_authority(_git).high_water)


def _validated_reset_receipt() -> dict:
    receipt = _read_json(RESET_RECEIPT)
    errors = list(validate_policy_epoch_reset_archive(receipt, project_root=ROOT))
    if errors:
        raise RuntimeError("policy epoch reset receipt invalid: " + "; ".join(errors))
    if (
        receipt.get("schema_version") != 2
        or receipt.get("kind") != "national_tcp_policy_epoch_reset"
        or receipt.get("mode") != "execute"
        or receipt.get("epoch") != EVALUATION_EPOCH
    ):
        raise RuntimeError("reconciliation requires the completed schema-2 reset")
    return receipt


def _legacy_ledger_summary(raw: bytes) -> dict:
    if not raw:
        raise RuntimeError("legacy abandon ledger is empty")
    attempts: list[int] = []
    row_hashes: list[str] = []
    issues: list[str] = []
    if not raw.endswith(b"\n"):
        issues.append("partial_final_row")
    for number, encoded in enumerate(raw.splitlines(), start=1):
        try:
            row = json.loads(encoded.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            issues.append(f"malformed_row:{number}")
            row_hashes.append(_sha256(encoded))
            continue
        if (
            not isinstance(row, dict)
            or not set(row).issubset(LEGACY_LEDGER_KEYS)
            or "v" not in row
            or "reason" not in row
            or "workflow_run_id" not in row
            or not ({"ts", "timestamp"} & set(row))
        ):
            issues.append(f"unknown_shape:{number}")
            row_hashes.append(_sha256(encoded))
            continue
        if row.get("v") != FIRST_STRICT_POLICY_VERSION:
            issues.append(f"unexpected_version:{number}")
            row_hashes.append(_sha256(encoded))
            continue
        match = re.fullmatch(
            r"generation:143:workflow-v([1-9][0-9]*)",
            str(row.get("workflow_run_id") or ""),
        )
        if not match:
            issues.append(f"invalid_workflow:{number}")
            row_hashes.append(_sha256(encoded))
            continue
        attempts.append(int(match.group(1)))
        row_hashes.append(_sha256(encoded))
    expected_attempts = list(range(1, len(raw.splitlines()) + 1))
    recognized = not issues and attempts == expected_attempts
    if not issues and not recognized:
        issues.append("workflow_attempts_not_contiguous")
    return {
        "sha256": _sha256(raw),
        "size": len(raw),
        "rows": len(attempts),
        "attempts": attempts,
        "row_sha256": row_hashes,
        "recognized_legacy_shape": recognized,
        "issues": issues,
        "authority_weight": 0,
    }


def _tree_manifest(path: Path) -> dict:
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"candidate path is missing or unsafe: {path}")
    entries = []
    total_bytes = 0
    for child in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
        if len(entries) >= MAX_CANDIDATE_TREE_ENTRIES:
            raise RuntimeError("candidate quarantine exceeds entry limit")
        relative = child.relative_to(path).as_posix()
        if len(Path(relative).parts) > 32:
            raise RuntimeError("candidate quarantine exceeds depth limit")
        metadata = os.lstat(child)
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"candidate quarantine forbids symlink: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": relative, "kind": "directory"})
        elif stat.S_ISREG(metadata.st_mode):
            raw = _safe_read_bytes(child)
            total_bytes += len(raw)
            if total_bytes > MAX_CANDIDATE_TREE_BYTES:
                raise RuntimeError("candidate quarantine exceeds total byte limit")
            entries.append({
                "path": relative,
                "kind": "file",
                "size": len(raw),
                "sha256": _sha256(raw),
            })
        else:
            raise RuntimeError(f"candidate quarantine forbids special file: {relative}")
    return {
        "entries": entries,
        "digest": canonical_digest(entries),
        "total_bytes": total_bytes,
        "entry_count": len(entries),
    }


def _candidate_publication_errors(
    path: Path,
    version: int,
    *,
    tracked_path: Path | None = None,
) -> list[str]:
    errors: list[str] = []
    relative = (tracked_path or path).relative_to(ROOT).as_posix()
    if _git_explicit_presence("ls-files", "--error-unmatch", relative):
        errors.append("reconciliation_candidate_is_git_tracked")
    for tag in (
        bot_tag(version),
        high_water_tag(version),
    ):
        if _git_explicit_presence(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/tags/{tag}",
        ):
            errors.append(f"reconciliation_candidate_has_publication_ref:{tag}")
    completed = path / ".completed"
    if os.path.lexists(completed):
        errors.append("reconciliation_candidate_has_completed_sentinel")
    return errors


def _destructive_git_snapshot(version: int) -> dict:
    """Capture the exact Git predicates whose absence permits quarantine."""

    target = int(version)
    relative = f"bots/{bot_name(target)}"
    tags = (
        bot_tag(target),
        high_water_tag(target),
    )
    return {
        "head": _git("rev-parse", "HEAD"),
        "tracked_status": _git("status", "--porcelain", "--untracked-files=no"),
        "candidate_tracked": _git_explicit_presence(
            "ls-files",
            "--error-unmatch",
            relative,
        ),
        "publication_refs": {
            tag: _git_explicit_presence(
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/tags/{tag}",
            )
            for tag in tags
        },
    }


def _assert_destructive_git_snapshot(
    snapshot: dict,
    *,
    expected_head: str,
) -> None:
    if snapshot.get("head") != expected_head:
        raise RuntimeError("reconciliation HEAD changed before/after candidate move")
    if snapshot.get("tracked_status"):
        raise RuntimeError("reconciliation tracked worktree changed before/after candidate move")
    if snapshot.get("candidate_tracked") is not False:
        raise RuntimeError("reconciliation candidate became git tracked")
    present = [
        tag
        for tag, exists in (snapshot.get("publication_refs") or {}).items()
        if exists is not False
    ]
    if present:
        raise RuntimeError(
            "reconciliation publication ref appeared before/after candidate move: "
            + ",".join(present)
        )


def _runtime_control_snapshot(
    workflow_run_id: str,
    *,
    cost_ledger_prefix_size: int | None = None,
) -> tuple[dict, bytes]:
    files = []
    for name in RUNTIME_CONTROL_FILE_ORDER:
        path = RESULTS / name
        if not os.path.lexists(path):
            continue
        raw = _safe_read_bytes(path)
        if name == "generation_cost_pending.json":
            try:
                pending = json.loads(raw.decode("utf-8"))
                entries = (pending or {}).get("pending") or {}
            except (UnicodeError, json.JSONDecodeError, AttributeError) as exc:
                raise RuntimeError("generation cost pending state is invalid") from exc
            foreign = [
                event_id
                for event_id, entry in entries.items()
                if isinstance(entry, dict)
                and str(entry.get("generation_id") or "") != workflow_run_id
            ]
            if foreign:
                raise RuntimeError(
                    "generation cost pending state contains another workflow"
                )
        files.append({
            "name": name,
            "sha256": _sha256(raw),
            "size": len(raw),
        })

    cost_path = RESULTS / "generation_cost_ledger.jsonl"
    matching_lines: list[bytes] = []
    ledger_prefix_sha256 = _sha256(b"")
    ledger_prefix_size = 0
    if os.path.lexists(cost_path):
        ledger_raw = _safe_read_bytes(cost_path, max_bytes=MAX_COST_LEDGER_BYTES)
        if cost_ledger_prefix_size is not None:
            if (
                type(cost_ledger_prefix_size) is not int
                or cost_ledger_prefix_size < 0
                or len(ledger_raw) < cost_ledger_prefix_size
            ):
                raise RuntimeError("generation cost ledger prefix is unavailable")
            ledger_raw = ledger_raw[:cost_ledger_prefix_size]
        ledger_prefix_size = len(ledger_raw)
        ledger_prefix_sha256 = _sha256(ledger_raw)
        for number, line in enumerate(ledger_raw.splitlines(keepends=True), start=1):
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"generation cost ledger malformed at row {number}"
                ) from exc
            if (
                isinstance(row, dict)
                and str(row.get("generation_id") or "") == workflow_run_id
            ):
                matching_lines.append(line)
    cost_scope = b"".join(matching_lines)
    return {
        "files": files,
        "cost_ledger_prefix_size": ledger_prefix_size,
        "cost_ledger_prefix_sha256": ledger_prefix_sha256,
        "cost_scope_rows": len(matching_lines),
        "cost_scope_sha256": _sha256(cost_scope),
        "cost_scope_authority_weight": 0,
    }, cost_scope


def _fence_worker_workflow(checkpoint: dict, claim_digest: str) -> dict:
    from worker_workflow import WorkerWorkflow

    workflow = WorkerWorkflow.for_checkpoint(checkpoint)
    with workflow.store.command_lock(workflow.run_id, blocking=True):
        state = workflow.abandon(
            "operator_legacy_reconciliation:" + str(claim_digest)
        )
    if state.get("status") != "abandoned":
        raise RuntimeError("Worker workflow terminal fence did not converge")
    return {
        "workflow_run_id": workflow.run_id,
        "status": "abandoned",
        "last_seq": int(state.get("last_seq") or 0),
        "cycle": int(state.get("cycle") or 0),
    }


def _checkpoint_summary(raw: bytes, legacy_summary: dict) -> tuple[dict, dict]:
    try:
        checkpoint = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("active checkpoint is unreadable") from exc
    if not isinstance(checkpoint, dict):
        raise RuntimeError("active checkpoint is not an object")
    target = checkpoint.get("next_v")
    source = checkpoint.get("source_v")
    revision = checkpoint.get("checkpoint_revision")
    stage = checkpoint.get("stage")
    workflow = checkpoint.get("workflow_run_id")
    if (
        type(target) is not int
        or target < FIRST_STRICT_POLICY_VERSION
        or type(source) is not int
        or type(revision) is not int
        or revision < 1
        or not isinstance(stage, str)
        or not stage.strip()
        or stage in {"archived", "abandoned", "timed_out", "infra_timed_out"}
    ):
        raise RuntimeError("active checkpoint identity is incomplete")
    match = re.fullmatch(
        rf"generation:{target}:workflow-v([1-9][0-9]*)",
        str(workflow or ""),
    )
    if not match:
        raise RuntimeError("active checkpoint workflow identity is invalid")
    attempt = int(match.group(1))
    trusted_attempts = (
        list(legacy_summary.get("attempts") or [])
        if legacy_summary.get("recognized_legacy_shape") is True
        else []
    )
    if (
        target == FIRST_STRICT_POLICY_VERSION
        and trusted_attempts
        and attempt != max(trusted_attempts) + 1
    ):
        raise RuntimeError(
            "active v143 workflow does not immediately follow legacy attempts"
        )
    summary = {
        "sha256": _sha256(raw),
        "size": len(raw),
        "next_v": target,
        "source_v": source,
        "stage": stage,
        "workflow_run_id": workflow,
        "workflow_attempt": attempt,
        "checkpoint_revision": revision,
        "schema_version": checkpoint.get("checkpoint_schema_version"),
    }
    return checkpoint, summary


def _is_hex_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX_DIGEST_RE.fullmatch(value) is not None


def _validate_legacy_claim_summary(legacy: dict) -> None:
    attempts = legacy.get("attempts")
    row_hashes = legacy.get("row_sha256")
    issues = legacy.get("issues")
    rows = legacy.get("rows")
    if (
        not _is_hex_digest(legacy.get("sha256"))
        or type(legacy.get("size")) is not int
        or not 0 < legacy["size"] <= MAX_INPUT_BYTES
        or type(rows) is not int
        or rows < 0
        or not isinstance(attempts, list)
        or any(type(item) is not int or item < 1 for item in attempts)
        or rows != len(attempts)
        or not isinstance(row_hashes, list)
        or len(row_hashes) < rows
        or any(not _is_hex_digest(item) for item in row_hashes)
        or not isinstance(issues, list)
        or any(not isinstance(item, str) or not item for item in issues)
        or not isinstance(legacy.get("recognized_legacy_shape"), bool)
        or type(legacy.get("authority_weight")) is not int
        or legacy.get("authority_weight") != 0
    ):
        raise RuntimeError("live reconciliation legacy summary is invalid")
    if legacy["recognized_legacy_shape"] and (
        issues or attempts != list(range(1, rows + 1)) or len(row_hashes) != rows
    ):
        raise RuntimeError("live reconciliation recognized legacy summary was forged")


def _validate_checkpoint_claim_identity(checkpoint: dict) -> None:
    target = checkpoint.get("next_v")
    workflow_attempt = checkpoint.get("workflow_attempt")
    workflow = checkpoint.get("workflow_run_id")
    if (
        not _is_hex_digest(checkpoint.get("sha256"))
        or type(checkpoint.get("size")) is not int
        or not 0 < checkpoint["size"] <= MAX_INPUT_BYTES
        or type(target) is not int
        or target < FIRST_STRICT_POLICY_VERSION
        or type(checkpoint.get("source_v")) is not int
        or not isinstance(checkpoint.get("stage"), str)
        or not checkpoint["stage"].strip()
        or checkpoint["stage"]
        in {"archived", "abandoned", "timed_out", "infra_timed_out"}
        or type(workflow_attempt) is not int
        or workflow_attempt < 1
        or workflow != f"generation:{target}:workflow-v{workflow_attempt}"
        or type(checkpoint.get("checkpoint_revision")) is not int
        or checkpoint["checkpoint_revision"] < 1
        or checkpoint.get("schema_version") != 1
    ):
        raise RuntimeError("live reconciliation checkpoint identity is invalid")


def _validate_candidate_claim_identity(candidate: dict, checkpoint: dict) -> None:
    target = checkpoint["next_v"]
    if (
        candidate.get("path") != f"bots/{bot_name(target)}"
        or not _is_hex_digest(candidate.get("manifest_digest"))
        or type(candidate.get("entries")) is not int
        or not 0 <= candidate["entries"] <= MAX_CANDIDATE_TREE_ENTRIES
        or type(candidate.get("total_bytes")) is not int
        or not 0 <= candidate["total_bytes"] <= MAX_CANDIDATE_TREE_BYTES
    ):
        raise RuntimeError("live reconciliation candidate identity is invalid")


def _validate_runtime_control_claim(control: dict) -> None:
    files = control.get("files")
    if not isinstance(files, list) or len(files) > len(RUNTIME_CONTROL_FILENAMES):
        raise RuntimeError("live reconciliation runtime control files are invalid")
    names: list[str] = []
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "sha256", "size"}
            or item.get("name") not in RUNTIME_CONTROL_FILENAMES
            or not _is_hex_digest(item.get("sha256"))
            or type(item.get("size")) is not int
            or not 0 <= item["size"] <= MAX_INPUT_BYTES
        ):
            raise RuntimeError("live reconciliation runtime control file is invalid")
        names.append(item["name"])
    if len(names) != len(set(names)):
        raise RuntimeError("live reconciliation runtime control file is duplicated")
    canonical_names = [
        name for name in RUNTIME_CONTROL_FILE_ORDER if name in set(names)
    ]
    if names != canonical_names:
        raise RuntimeError("live reconciliation runtime control file order is invalid")
    if (
        type(control.get("cost_ledger_prefix_size")) is not int
        or not 0 <= control["cost_ledger_prefix_size"] <= MAX_COST_LEDGER_BYTES
        or not _is_hex_digest(control.get("cost_ledger_prefix_sha256"))
        or type(control.get("cost_scope_rows")) is not int
        or control["cost_scope_rows"] < 0
        or not _is_hex_digest(control.get("cost_scope_sha256"))
        or type(control.get("cost_scope_authority_weight")) is not int
        or control["cost_scope_authority_weight"] != 0
    ):
        raise RuntimeError("live reconciliation runtime control scope is invalid")


def _validate_effective_runtime_control_files(
    claimed_control: dict,
    archive_root: Path,
) -> None:
    claimed_items = {
        item["name"]: item for item in claimed_control.get("files") or []
    }
    effective: list[dict] = []
    for name in RUNTIME_CONTROL_FILE_ORDER:
        live = RESULTS / name
        archived = archive_root / "runtime_control" / name
        live_exists = os.path.lexists(live)
        archived_exists = os.path.lexists(archived)
        if live_exists and archived_exists:
            raise RuntimeError(
                f"runtime control exists at both live and archive paths: {name}"
            )
        claimed = claimed_items.get(name)
        observed = archived if archived_exists else live
        if not live_exists and not archived_exists:
            if claimed is not None:
                raise RuntimeError(f"claimed runtime control disappeared: {name}")
            continue
        if claimed is None:
            raise RuntimeError(f"unclaimed runtime control file appeared: {name}")
        raw = _safe_read_bytes(observed)
        if len(raw) != claimed["size"] or _sha256(raw) != claimed["sha256"]:
            raise RuntimeError(f"runtime control file drifted: {name}")
        effective.append(claimed)
    if effective != claimed_control.get("files"):
        raise RuntimeError("runtime control effective file set differs from claim")


def _validated_claim(path: Path) -> dict:
    claim = _read_json(path)
    recorded = claim.get("claim_digest")
    unsigned = {key: value for key, value in claim.items() if key != "claim_digest"}
    if (
        set(claim) != {
            "schema_version",
            "kind",
            "evaluation_epoch",
            "git_head",
            "checkout_role",
            "action",
            "archive_root",
            "inputs",
            "terminal_abandon",
            "claim_digest",
        }
        or claim.get("schema_version") != 1
        or claim.get("kind") != "national-policy-runtime-reconciliation-claim"
        or claim.get("evaluation_epoch") != EVALUATION_EPOCH
        or claim.get("checkout_role") != "autonomous_evolution_runtime"
        or claim.get("action")
        != "quarantine_legacy_ledger_and_abandon_checkpoint"
        or recorded != canonical_digest(unsigned)
    ):
        raise RuntimeError("live reconciliation claim is invalid")
    inputs = claim.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {
        "reset_receipt_digest",
        "published_high_water",
        "legacy_ledger",
        "checkpoint",
        "candidate",
        "runtime_control",
        "target_successor",
        "allocation_receipt_eligible",
        "allocation_receipt_rejection_issues",
    }:
        raise RuntimeError("live reconciliation claim inputs are invalid")
    checkpoint = inputs.get("checkpoint") or {}
    legacy = inputs.get("legacy_ledger") or {}
    candidate = inputs.get("candidate") or {}
    control = inputs.get("runtime_control") or {}
    if set(legacy) != {
        "sha256",
        "size",
        "rows",
        "attempts",
        "row_sha256",
        "recognized_legacy_shape",
        "issues",
        "authority_weight",
    } or set(checkpoint) != {
        "sha256",
        "size",
        "next_v",
        "source_v",
        "stage",
        "workflow_run_id",
        "workflow_attempt",
        "checkpoint_revision",
        "schema_version",
    } or set(candidate) != {
        "path",
        "manifest_digest",
        "entries",
        "total_bytes",
    } or set(control) != {
        "files",
        "cost_ledger_prefix_size",
        "cost_ledger_prefix_sha256",
        "cost_scope_rows",
        "cost_scope_sha256",
        "cost_scope_authority_weight",
    }:
        raise RuntimeError("live reconciliation nested claim fields are invalid")
    _validate_legacy_claim_summary(legacy)
    _validate_checkpoint_claim_identity(checkpoint)
    _validate_candidate_claim_identity(candidate, checkpoint)
    _validate_runtime_control_claim(control)
    published = inputs.get("published_high_water")
    if (
        type(published) is not int
        or published != ARCHIVED_VERSION_HIGH_WATER
        or not _is_hex_digest(inputs.get("reset_receipt_digest"))
        or not re.fullmatch(r"[0-9a-f]{40}", str(claim.get("git_head") or ""))
    ):
        raise RuntimeError("live reconciliation version authority is invalid")
    eligible = checkpoint.get("next_v") == published + 1
    if inputs.get("target_successor") is not eligible:
        raise RuntimeError("live reconciliation target successor was forged")
    rejection_issues = inputs.get("allocation_receipt_rejection_issues")
    if (
        not isinstance(inputs.get("allocation_receipt_eligible"), bool)
        or not isinstance(rejection_issues, list)
        or any(not isinstance(item, str) or not item for item in rejection_issues)
    ):
        raise RuntimeError("live reconciliation receipt eligibility is invalid")
    expected_archive = ARCHIVE_BASE / (
        "legacy-"
        f"{str(checkpoint.get('sha256') or '')[:12]}-"
        f"{str((inputs.get('legacy_ledger') or {}).get('sha256') or '')[:12]}"
    )
    if claim.get("archive_root") != str(expected_archive.relative_to(ROOT)):
        raise RuntimeError("live reconciliation archive identity mismatch")
    _archive_root_from_relative(claim.get("archive_root"), create=False)
    terminal = claim.get("terminal_abandon")
    if not isinstance(terminal, dict) or set(terminal) != {
        "reason",
        "infra_failure",
        "timestamp",
    }:
        raise RuntimeError("live reconciliation terminal payload is invalid")
    expected_infra = {
        "kind": "operator_legacy_quarantine",
        "reconciliation_input_digest": canonical_digest(inputs),
    }
    if (
        terminal.get("reason")
        != "operator_legacy_ledger_quarantine_reprepare"
        or terminal.get("infra_failure") != expected_infra
        or type(terminal.get("timestamp")) not in {int, float}
        or terminal["timestamp"] != terminal["timestamp"]
        or not 0 <= terminal["timestamp"] <= 100_000_000_000
    ):
        raise RuntimeError("live reconciliation terminal payload drifted")
    return claim


def _resume_plan_from_claim(claim: dict) -> dict:
    archive_root = _archive_root_from_relative(
        claim.get("archive_root"),
        create=False,
    )
    inputs = claim.get("inputs")
    if not isinstance(inputs, dict):
        raise RuntimeError("reconciliation claim inputs are missing")
    if claim.get("git_head") != _git("rev-parse", "HEAD"):
        raise RuntimeError("git HEAD changed after reconciliation claim")
    reset = _validated_reset_receipt()
    if reset.get("receipt_digest") != inputs.get("reset_receipt_digest"):
        raise RuntimeError("reset receipt changed after reconciliation claim")
    if _version_authority_high_water() != inputs.get("published_high_water"):
        raise RuntimeError("published high-water changed after reconciliation claim")

    archived_legacy = archive_root / "legacy_abandoned_versions.jsonl"
    legacy_path = archived_legacy if os.path.lexists(archived_legacy) else LEDGER
    legacy = _legacy_ledger_summary(_safe_read_bytes(legacy_path))
    if legacy != inputs.get("legacy_ledger"):
        raise RuntimeError("legacy ledger differs from reconciliation claim")
    archived_checkpoint = archive_root / "pipeline_state.json"
    checkpoint_path = (
        archived_checkpoint if os.path.lexists(archived_checkpoint) else CHECKPOINT
    )
    checkpoint_raw = _safe_read_bytes(checkpoint_path)
    checkpoint, checkpoint_identity = _checkpoint_summary(
        checkpoint_raw,
        legacy,
    )
    if checkpoint_identity != inputs.get("checkpoint"):
        raise RuntimeError("checkpoint differs from reconciliation claim")
    candidate = BOTS / bot_name(checkpoint_identity["next_v"])
    archived_candidate = archive_root / "candidate" / candidate.name
    candidate_for_hash = (
        candidate if os.path.lexists(candidate) else archived_candidate
    )
    candidate_manifest = _tree_manifest(candidate_for_hash)
    claimed_candidate = inputs.get("candidate") or {}
    if (
        candidate_manifest["digest"] != claimed_candidate.get("manifest_digest")
        or candidate_manifest["entry_count"] != claimed_candidate.get("entries")
        or candidate_manifest["total_bytes"] != claimed_candidate.get("total_bytes")
    ):
        raise RuntimeError("candidate differs from reconciliation claim")
    candidate_errors = _candidate_publication_errors(
        candidate_for_hash,
        checkpoint_identity["next_v"],
        tracked_path=candidate,
    )
    if candidate_errors:
        raise RuntimeError("; ".join(candidate_errors))
    claimed_control = inputs.get("runtime_control") or {}
    _validate_effective_runtime_control_files(claimed_control, archive_root)
    current_control, cost_scope = _runtime_control_snapshot(
        checkpoint_identity["workflow_run_id"],
        cost_ledger_prefix_size=claimed_control.get("cost_ledger_prefix_size"),
    )
    for key in (
        "cost_ledger_prefix_size",
        "cost_ledger_prefix_sha256",
        "cost_scope_rows",
        "cost_scope_sha256",
        "cost_scope_authority_weight",
    ):
        if current_control.get(key) != claimed_control.get(key):
            raise RuntimeError(f"generation cost scope drifted: {key}")

    upgraded_checkpoint = None
    recomputed_issues: list[str] = []
    if inputs.get("target_successor") is True:
        try:
            upgraded_checkpoint = upgrade_legacy_checkpoint_for_controlled_abandon(
                checkpoint,
                published_high_water=int(inputs["published_high_water"]),
                abandoned_receipt_floor=0,
                abandoned_receipt_head_digest=None,
            )
        except CheckpointSchemaError as exc:
            recomputed_issues.extend(exc.errors)
    else:
        recomputed_issues.append("target_not_current_allocation_successor")
    if (
        inputs.get("allocation_receipt_eligible")
        != (upgraded_checkpoint is not None)
        or inputs.get("allocation_receipt_rejection_issues") != recomputed_issues
    ):
        raise RuntimeError("allocation receipt eligibility changed after claim")
    return {
        "claim": claim,
        "archive_root": archive_root,
        "checkpoint": checkpoint,
        "upgraded_checkpoint": upgraded_checkpoint,
        "candidate_manifest": candidate_manifest,
        "candidate": candidate,
        "cost_scope": cost_scope,
    }


def _validate_completed_archive_preimage(
    claim: dict,
    archive_root: Path,
) -> dict:
    """Reopen immutable completion evidence without freezing future progress."""

    inputs = claim["inputs"]
    reset = _validated_reset_receipt()
    if reset.get("receipt_digest") != inputs.get("reset_receipt_digest"):
        raise RuntimeError("completed reconciliation reset receipt changed")

    archived_legacy = archive_root / "legacy_abandoned_versions.jsonl"
    legacy = _legacy_ledger_summary(_safe_read_bytes(archived_legacy))
    if legacy != inputs.get("legacy_ledger"):
        raise RuntimeError("completed reconciliation legacy archive drifted")

    archived_checkpoint = archive_root / "pipeline_state.json"
    checkpoint_raw = _safe_read_bytes(archived_checkpoint)
    checkpoint, checkpoint_identity = _checkpoint_summary(
        checkpoint_raw,
        legacy,
    )
    if checkpoint_identity != inputs.get("checkpoint"):
        raise RuntimeError("completed reconciliation checkpoint archive drifted")

    claimed_candidate = inputs.get("candidate") or {}
    archived_candidate = (
        archive_root / "candidate" / bot_name(checkpoint_identity["next_v"])
    )
    candidate_manifest = _tree_manifest(archived_candidate)
    if (
        candidate_manifest["digest"] != claimed_candidate.get("manifest_digest")
        or candidate_manifest["entry_count"] != claimed_candidate.get("entries")
        or candidate_manifest["total_bytes"] != claimed_candidate.get("total_bytes")
    ):
        raise RuntimeError("completed reconciliation candidate archive drifted")

    claimed_control = inputs.get("runtime_control") or {}
    control_archive = archive_root / "runtime_control"
    _safe_directory_chain(control_archive, base=ARCHIVE_BASE, create=False)
    if not control_archive.is_dir() or control_archive.is_symlink():
        raise RuntimeError("completed reconciliation control archive is unsafe")
    claimed_names = {
        item["name"] for item in claimed_control.get("files") or []
    }
    allowed_names = {*claimed_names, "generation_cost_scope.jsonl"}
    observed_names = {child.name for child in control_archive.iterdir()}
    if observed_names != allowed_names:
        raise RuntimeError("completed reconciliation control archive set drifted")
    for item in claimed_control.get("files") or []:
        raw = _safe_read_bytes(control_archive / item["name"])
        if len(raw) != item["size"] or _sha256(raw) != item["sha256"]:
            raise RuntimeError(
                f"completed reconciliation control archive drifted: {item['name']}"
            )

    archived_scope = _safe_read_bytes(
        control_archive / "generation_cost_scope.jsonl",
        max_bytes=MAX_COST_LEDGER_BYTES,
    )
    if (
        _sha256(archived_scope) != claimed_control["cost_scope_sha256"]
        or len(archived_scope.splitlines()) != claimed_control["cost_scope_rows"]
    ):
        raise RuntimeError("completed reconciliation cost scope archive drifted")

    cost_prefix = _safe_read_prefix_bytes(
        RESULTS / "generation_cost_ledger.jsonl",
        claimed_control["cost_ledger_prefix_size"],
    )
    if _sha256(cost_prefix) != claimed_control["cost_ledger_prefix_sha256"]:
        raise RuntimeError("completed reconciliation cost ledger prefix drifted")
    matching_lines: list[bytes] = []
    for number, line in enumerate(cost_prefix.splitlines(keepends=True), start=1):
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"completed cost ledger malformed at prefix row {number}"
            ) from exc
        if (
            isinstance(row, dict)
            and str(row.get("generation_id") or "")
            == checkpoint_identity["workflow_run_id"]
        ):
            matching_lines.append(line)
    matching_scope = b"".join(matching_lines)
    if (
        len(matching_lines) != claimed_control["cost_scope_rows"]
        or _sha256(matching_scope) != claimed_control["cost_scope_sha256"]
        or matching_scope != archived_scope
    ):
        raise RuntimeError("completed reconciliation cost scope derivation drifted")
    return {
        "checkpoint": checkpoint,
        "checkpoint_identity": checkpoint_identity,
    }


def _validated_completed_receipt(path: Path) -> dict:
    receipt = _read_json(path)
    expected_keys = {
        "schema_version",
        "kind",
        "evaluation_epoch",
        "mode",
        "claim_digest",
        "archive_root",
        "legacy_rows_authority_weight",
        "allocation_receipt_digest",
        "abandoned_workflow_run_id",
        "workflow_fence",
        "next_target_version",
        "next_workflow_attempt",
        "receipt_digest",
    }
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    if (
        set(receipt) != expected_keys
        or receipt.get("schema_version") != 1
        or receipt.get("kind") != "national-policy-runtime-reconciliation"
        or receipt.get("evaluation_epoch") != EVALUATION_EPOCH
        or receipt.get("mode") != "execute"
        or receipt.get("legacy_rows_authority_weight") != 0
        or receipt.get("receipt_digest") != canonical_digest(unsigned)
    ):
        raise RuntimeError("completed reconciliation receipt is invalid")
    archive_root = _archive_root_from_relative(
        receipt.get("archive_root"),
        create=False,
        require_exists=True,
    )
    claim = _validated_claim(archive_root / "reconciliation_claim.json")
    if (
        receipt.get("claim_digest") != claim.get("claim_digest")
        or receipt.get("archive_root") != claim.get("archive_root")
    ):
        raise RuntimeError("completed reconciliation receipt claim mismatch")
    historical = _validate_completed_archive_preimage(claim, archive_root)
    checkpoint = claim["inputs"]["checkpoint"]
    expected_attempt = (
        int(checkpoint["workflow_attempt"]) + 1
        if checkpoint["next_v"] == FIRST_STRICT_POLICY_VERSION
        else 1
    )
    fence = receipt.get("workflow_fence")
    if (
        receipt.get("abandoned_workflow_run_id")
        != checkpoint.get("workflow_run_id")
        or receipt.get("next_target_version")
        != int(claim["inputs"]["published_high_water"]) + 1
        or receipt.get("next_workflow_attempt") != expected_attempt
        or not isinstance(fence, dict)
        or fence.get("workflow_run_id") != checkpoint.get("workflow_run_id")
        or fence.get("status") != "abandoned"
        or type(fence.get("last_seq")) is not int
    ):
        raise RuntimeError("completed reconciliation projection mismatch")
    if claim["inputs"]["allocation_receipt_eligible"] is True:
        import evolution_infra

        rows = evolution_infra.load_abandoned_version_receipts(
            path=LEDGER,
            project_root=ROOT,
        )
        matches = [
            row
            for row in rows
            if row.get("workflow_run_id") == checkpoint.get("workflow_run_id")
            and row.get("checkpoint_revision")
            == checkpoint.get("checkpoint_revision")
        ]
        if (
            len(matches) != 1
            or receipt.get("allocation_receipt_digest")
            != matches[0].get("receipt_digest")
        ):
            raise RuntimeError("completed reconciliation allocation receipt mismatch")
    elif receipt.get("allocation_receipt_digest") is not None:
        raise RuntimeError("large-jump quarantine minted allocation authority")
    archive_receipt = _read_json(archive_root / "reconciliation_receipt.json")
    if archive_receipt != receipt:
        raise RuntimeError("completed reconciliation archive receipt mismatch")
    if historical["checkpoint_identity"] != checkpoint:
        raise RuntimeError("completed reconciliation historical plan mismatch")
    return receipt


def _cloud_epoch_ledger_archive_plan(published: int) -> dict:
    """Build a reconciliation plan for a cloud epoch with a stale ledger.

    When bots are already published (published > ARCHIVED_VERSION_HIGH_WATER)
    and there is no live checkpoint, the only problem is a stale abandon ledger
    whose pre-current-epoch entries have incomplete parent identities.  This
    plan archives the stale ledger and writes a fresh empty one so the allocation
    authority can compute next_v from the published high-water.
    """
    import hashlib as _hashlib

    ledger_raw = _safe_read_bytes(LEDGER)
    legacy = _legacy_ledger_summary(ledger_raw)
    archive_digest = _hashlib.sha256(ledger_raw).hexdigest()[:12]
    # The _archive_root_from_relative regex requires exactly 12 hex chars for
    # both segments.  Pad the timestamp to 12 hex chars.
    timestamp_hex = format(int(time.time()), "x").zfill(12)[:12]
    archive_root = ARCHIVE_BASE / f"legacy-{archive_digest}-{timestamp_hex}"
    archive_root.mkdir(parents=True, exist_ok=False)
    archived_ledger = archive_root / "legacy_abandoned_versions.jsonl"
    archived_ledger.write_bytes(ledger_raw)

    inputs = {
        "evaluation_epoch": EVALUATION_EPOCH,
        "published_high_water": published,
        "legacy_ledger": legacy,
        "ledger_sha256": _hashlib.sha256(ledger_raw).hexdigest(),
        "ledger_entry_count": legacy.get("row_count", 0),
        "archive_root": str(archive_root.relative_to(ROOT)),
    }
    claim_payload = {
        "schema_version": 1,
        "kind": "national-policy-runtime-reconciliation-claim",
        "evaluation_epoch": EVALUATION_EPOCH,
        "git_head": _git("rev-parse", "HEAD"),
        "checkout_role": "autonomous_evolution_runtime",
        "action": "cloud_epoch_ledger_archive",
        "archive_root": str(archive_root.relative_to(ROOT)),
        "inputs": inputs,
        "terminal_abandon": {
            "reason": "operator_cloud_epoch_ledger_archive",
            "infra_failure": {
                "kind": "operator_cloud_epoch_ledger_archive",
                "reconciliation_input_digest": canonical_digest(inputs),
            },
            "timestamp": time.time(),
        },
    }
    claim = {**claim_payload, "claim_digest": canonical_digest(claim_payload)}
    return {
        "claim": claim,
        "archive_root": archive_root,
        "cloud_epoch_ledger_archive": True,
        "checkpoint": None,
        "upgraded_checkpoint": None,
        "candidate_manifest": None,
        "candidate": None,
        "cost_scope": None,
    }


def _build_plan() -> dict:
    if os.path.lexists(LIVE_RECEIPT):
        return {"completed_receipt": _validated_completed_receipt(LIVE_RECEIPT)}
    if os.path.lexists(LIVE_CLAIM):
        return _resume_plan_from_claim(_validated_claim(LIVE_CLAIM))

    reset = _validated_reset_receipt()
    published = _version_authority_high_water()
    if published != ARCHIVED_VERSION_HIGH_WATER:
        # Cloud epoch with already-published bots: the legacy quarantine path
        # requires a live checkpoint + candidate to abandon, which does not
        # apply when the only problem is a stale ledger with pre-current-epoch
        # entries whose parent identity is incomplete.  In this state (no live
        # checkpoint, bots already published), archive the stale ledger so a
        # fresh allocation authority can compute next_v from the published
        # high-water without the stale entries blocking the health projection.
        if not os.path.lexists(CHECKPOINT):
            return _cloud_epoch_ledger_archive_plan(published)
        checkpoint_raw = _safe_read_bytes(CHECKPOINT)
        if not checkpoint_raw.strip():
            return _cloud_epoch_ledger_archive_plan(published)
        raise RuntimeError(
            "legacy reconciliation requires unpublished v143 and v142 high-water"
        )
    ledger_raw = _safe_read_bytes(LEDGER)
    legacy = _legacy_ledger_summary(ledger_raw)
    checkpoint_raw = _safe_read_bytes(CHECKPOINT)
    checkpoint, checkpoint_identity = _checkpoint_summary(
        checkpoint_raw,
        legacy,
    )
    candidate = BOTS / bot_name(checkpoint_identity["next_v"])
    candidate_manifest = _tree_manifest(candidate)
    candidate_errors = _candidate_publication_errors(
        candidate,
        checkpoint_identity["next_v"],
    )
    if candidate_errors:
        raise RuntimeError("; ".join(candidate_errors))
    runtime_control, cost_scope = _runtime_control_snapshot(
        checkpoint_identity["workflow_run_id"]
    )

    allocation_floor = published
    target_successor = checkpoint_identity["next_v"] == allocation_floor + 1
    upgraded_checkpoint = None
    allocation_receipt_issues: list[str] = []
    if target_successor:
        try:
            upgraded_checkpoint = upgrade_legacy_checkpoint_for_controlled_abandon(
                checkpoint,
                published_high_water=published,
                abandoned_receipt_floor=0,
                abandoned_receipt_head_digest=None,
            )
        except CheckpointSchemaError as exc:
            allocation_receipt_issues.extend(exc.errors)
    else:
        allocation_receipt_issues.append("target_not_current_allocation_successor")
    allocation_receipt_eligible = upgraded_checkpoint is not None
    archive_root = ARCHIVE_BASE / (
        "legacy-"
        f"{checkpoint_identity['sha256'][:12]}-{legacy['sha256'][:12]}"
    )
    archive_relative = str(archive_root.relative_to(ROOT))
    _archive_root_from_relative(archive_relative, create=False)
    inputs = {
        "reset_receipt_digest": reset.get("receipt_digest"),
        "published_high_water": published,
        "legacy_ledger": legacy,
        "checkpoint": checkpoint_identity,
        "candidate": {
            "path": str(candidate.relative_to(ROOT)),
            "manifest_digest": candidate_manifest["digest"],
            "entries": candidate_manifest["entry_count"],
            "total_bytes": candidate_manifest["total_bytes"],
        },
        "runtime_control": runtime_control,
        "target_successor": target_successor,
        "allocation_receipt_eligible": allocation_receipt_eligible,
        "allocation_receipt_rejection_issues": allocation_receipt_issues,
    }
    terminal_abandon = {
        "reason": "operator_legacy_ledger_quarantine_reprepare",
        "infra_failure": {
            "kind": "operator_legacy_quarantine",
            "reconciliation_input_digest": canonical_digest(inputs),
        },
        "timestamp": time.time(),
    }
    claim_payload = {
        "schema_version": 1,
        "kind": "national-policy-runtime-reconciliation-claim",
        "evaluation_epoch": EVALUATION_EPOCH,
        "git_head": _git("rev-parse", "HEAD"),
        "checkout_role": "autonomous_evolution_runtime",
        "action": "quarantine_legacy_ledger_and_abandon_checkpoint",
        "archive_root": archive_relative,
        "inputs": inputs,
        "terminal_abandon": terminal_abandon,
    }
    claim = {**claim_payload, "claim_digest": canonical_digest(claim_payload)}
    return {
        "claim": claim,
        "archive_root": archive_root,
        "checkpoint": checkpoint,
        "upgraded_checkpoint": upgraded_checkpoint,
        "candidate_manifest": candidate_manifest,
        "candidate": candidate,
        "cost_scope": cost_scope,
    }


def _ensure_claim(path: Path, claim: dict) -> None:
    if os.path.lexists(path):
        if _read_json(path) != claim:
            raise RuntimeError(f"reconciliation claim conflict: {path}")
    else:
        _write_json_exclusive(path, claim)


def _move_exact_file(source: Path, destination: Path, expected_sha256: str) -> None:
    _safe_directory_chain(destination.parent, base=ARCHIVE_BASE, create=True)
    source_exists = os.path.lexists(source)
    destination_exists = os.path.lexists(destination)
    if source_exists and destination_exists:
        raise RuntimeError(f"reconciliation move has both source and destination: {source}")
    if source_exists:
        if _sha256(_safe_read_bytes(source)) != expected_sha256:
            raise RuntimeError(f"reconciliation source bytes drifted: {source}")
        os.replace(source, destination)
        _fsync_directory(source.parent)
        _fsync_directory(destination.parent)
    elif not destination_exists:
        raise RuntimeError(f"reconciliation input disappeared: {source}")
    if _sha256(_safe_read_bytes(destination)) != expected_sha256:
        raise RuntimeError(f"reconciliation archive bytes mismatch: {destination}")


def _move_candidate(
    source: Path,
    destination: Path,
    expected_digest: str,
    *,
    version: int,
) -> None:
    _safe_directory_chain(destination.parent, base=ARCHIVE_BASE, create=True)
    if os.path.lexists(source) and os.path.lexists(destination):
        raise RuntimeError("candidate exists at both live and quarantine paths")
    if os.path.lexists(source):
        publication_errors = _candidate_publication_errors(
            source,
            version,
            tracked_path=source,
        )
        if publication_errors:
            raise RuntimeError("; ".join(publication_errors))
        if _tree_manifest(source)["digest"] != expected_digest:
            raise RuntimeError("candidate changed after reconciliation claim")
        os.replace(source, destination)
        _fsync_directory(source.parent)
        _fsync_directory(destination.parent)
    elif not os.path.lexists(destination):
        raise RuntimeError("candidate disappeared during reconciliation")
    else:
        publication_errors = _candidate_publication_errors(
            destination,
            version,
            tracked_path=source,
        )
        if publication_errors:
            raise RuntimeError("; ".join(publication_errors))
    if _tree_manifest(destination)["digest"] != expected_digest:
        raise RuntimeError("quarantined candidate digest mismatch")


def _validate_recorded_finalize_claim(
    claim: dict,
    *,
    require_current_head: bool = True,
) -> dict:
    if isinstance(claim, dict) and claim.get("schema_version") == 2:
        validate_schema2_abandon_claim_structure(claim)
        if require_current_head:
            snapshot = _destructive_git_snapshot(
                int(claim["checkpoint"]["next_v"])
            )
            current_git_state = {
                "head": snapshot["head"],
                "tracked_worktree_clean": snapshot["tracked_status"] == "",
                "candidate_tracked": snapshot["candidate_tracked"],
                "publication_refs": snapshot["publication_refs"],
            }
            if current_git_state != claim["git_state"]:
                raise RuntimeError("recorded-abandon finalize Git state changed")
        return claim

    old_keys = {
        "schema_version",
        "kind",
        "evaluation_epoch",
        "git_head",
        "checkout_role",
        "checkpoint",
        "abandon_receipt_digest",
        "abandon_reason",
        "candidate",
        "claim_digest",
    }
    unsigned = {key: value for key, value in claim.items() if key != "claim_digest"}
    checkpoint = claim.get("checkpoint")
    candidate = claim.get("candidate")
    common_invalid = (
        claim.get("schema_version") != 1
        or claim.get("kind")
        != "national-policy-recorded-abandon-finalize-claim"
        or claim.get("evaluation_epoch") != EVALUATION_EPOCH
        or claim.get("checkout_role") != "autonomous_evolution_runtime"
        or claim.get("claim_digest") != canonical_digest(unsigned)
        or not isinstance(checkpoint, dict)
        or not isinstance(candidate, dict)
    )
    old_valid = bool(
        not common_invalid
        and claim.get("schema_version") == 1
        and set(claim) == old_keys
        and set(checkpoint) == {
            "sha256", "next_v", "source_v", "stage",
            "workflow_run_id", "checkpoint_revision",
        }
        and set(candidate) == {
            "path", "manifest_digest", "entry_count", "total_bytes",
        }
    )
    if not old_valid:
        raise RuntimeError("recorded-abandon finalize claim is invalid")
    target = checkpoint.get("next_v")
    if (
        type(target) is not int
        or candidate.get("path") != f"bots/{bot_name(target)}"
        or type(candidate.get("entry_count")) is not int
        or type(candidate.get("total_bytes")) is not int
    ):
        raise RuntimeError("recorded-abandon finalize candidate identity is invalid")
    if not re.fullmatch(
        r"[0-9a-f]{64}", str(candidate.get("manifest_digest") or "")
    ):
        raise RuntimeError("recorded-abandon finalize candidate digest is invalid")
    if require_current_head and claim.get("git_head") != _git("rev-parse", "HEAD"):
        raise RuntimeError("recorded-abandon finalize HEAD changed")
    return claim


def _recorded_finalize_chain_receipt(
    claim: dict,
    *,
    require_chain_head: bool,
) -> dict | None:
    """Reopen the exact strict receipt authority bound by an active claim."""

    import evolution_infra

    receipts = evolution_infra.load_abandoned_version_receipts(
        path=LEDGER,
        project_root=ROOT,
    )
    if claim.get("schema_version") == 1:
        matches = [
            receipt
            for receipt in receipts
            if receipt.get("receipt_digest")
            == claim.get("abandon_receipt_digest")
        ]
    else:
        return validate_schema2_abandon_ledger_history(
            claim,
            receipts,
            require_active_head=require_chain_head,
        )
    if len(matches) != 1 or (
        require_chain_head and matches[0] is not receipts[-1]
    ):
        raise RuntimeError(
            "recorded-abandon finalize receipt is not unique/exact"
        )
    receipt = matches[0]
    checkpoint = claim["checkpoint"]
    expected = {
        "version": checkpoint["next_v"],
        "source_v": checkpoint["source_v"],
        "checkpoint_stage": checkpoint["stage"],
        "workflow_run_id": checkpoint["workflow_run_id"],
        "checkpoint_revision": checkpoint["checkpoint_revision"],
        "reason": claim["abandon_reason"],
    }
    mismatches = [
        field for field, value in expected.items() if receipt.get(field) != value
    ]
    if mismatches:
        raise RuntimeError(
            "recorded-abandon finalize receipt identity mismatch: "
            + ",".join(mismatches)
        )
    return receipt


def _recorded_finalize_chain_head(claim: dict) -> dict | None:
    return _recorded_finalize_chain_receipt(
        claim,
        require_chain_head=True,
    )


def _validate_schema2_recorded_finalize_live_state(claim: dict) -> None:
    """Reopen the exact schema-2 transaction for operator dry-run/execute."""

    validate_schema2_abandon_claim_structure(claim)
    checkpoint = claim["checkpoint"]
    version = int(checkpoint["next_v"])
    transaction_dir = (
        RESULTS / "policy_epoch_abandon_transactions" / claim["transaction_id"]
    )
    _safe_directory_chain(transaction_dir, base=RESULTS, create=False)
    transaction_claim = transaction_dir / "claim.json"
    if (
        not os.path.lexists(transaction_claim)
        or _read_json(transaction_claim) != claim
    ):
        raise RuntimeError("recorded-abandon transaction claim mismatch")

    candidate = BOTS / bot_name(version)
    quarantine = transaction_dir / "candidate"
    source_exists = os.path.lexists(candidate)
    quarantine_exists = os.path.lexists(quarantine)
    if source_exists and quarantine_exists:
        raise RuntimeError("recorded-abandon source/quarantine XOR invalid")
    expected = claim["candidate"]
    if expected["present"] is False:
        if source_exists or quarantine_exists:
            raise RuntimeError("recorded-abandon unexpected candidate bytes")
        phase = "absent"
    else:
        if not source_exists and not quarantine_exists:
            raise RuntimeError("recorded-abandon claimed candidate disappeared")
        manifest = _tree_manifest(candidate if source_exists else quarantine)
        if (
            manifest["digest"] != expected["manifest_digest"]
            or manifest["entry_count"] != expected["entry_count"]
            or manifest["total_bytes"] != expected["total_bytes"]
        ):
            raise RuntimeError("recorded-abandon candidate changed after claim")
        phase = "source" if source_exists else "quarantine"

    import evolution_infra

    rows = evolution_infra.load_abandoned_version_receipts(
        path=LEDGER,
        project_root=ROOT,
    )
    abandon_receipt = validate_schema2_abandon_ledger_history(
        claim,
        rows,
        require_active_head=True,
    )
    if os.path.lexists(CHECKPOINT):
        checkpoint_value = _read_json(CHECKPOINT)
        if canonical_digest(checkpoint_value) != checkpoint["digest"]:
            raise RuntimeError("recorded-abandon checkpoint changed after claim")
        if abandon_receipt is None and phase not in {"source", "absent"}:
            raise RuntimeError("recorded-abandon phase invalid before ledger append")
    else:
        if abandon_receipt is None:
            raise RuntimeError("recorded-abandon receipt missing after checkpoint clear")
        if expected["present"] is True and phase != "quarantine":
            raise RuntimeError(
                "recorded-abandon source invalid after checkpoint clear"
            )

    finalize = transaction_dir / "receipt.json"
    if os.path.lexists(finalize):
        validate_schema2_abandon_finalize_receipt(
            claim,
            _read_json(finalize),
            rows,
        )


def _validate_recorded_finalize_candidate(claim: dict, path: Path) -> None:
    observed = _tree_manifest(path)
    expected = claim["candidate"]
    if (
        observed["digest"] != expected["manifest_digest"]
        or observed["entry_count"] != expected["entry_count"]
        or observed["total_bytes"] != expected["total_bytes"]
    ):
        raise RuntimeError("recorded-abandon candidate changed after claim")
    errors = _candidate_publication_errors(
        path,
        int(claim["checkpoint"]["next_v"]),
    )
    if errors:
        raise RuntimeError("; ".join(errors))


def _remove_recorded_finalize_candidate(claim: dict, path: Path) -> None:
    """Finish the old clear-before-rmtree crash window from a durable claim."""

    _validate_recorded_finalize_candidate(claim, path)
    shutil.rmtree(path)
    if os.path.lexists(path):
        raise RuntimeError("recorded-abandon candidate delete did not complete")
    _fsync_directory(path.parent)


def _recorded_finalize_plan() -> dict:
    if os.path.lexists(RECORDED_FINALIZE_RECEIPT):
        receipt = _read_json(RECORDED_FINALIZE_RECEIPT)
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
        if (
            set(receipt) != {
                "schema_version",
                "kind",
                "evaluation_epoch",
                "mode",
                "claim_digest",
                "workflow_run_id",
                "abandon_receipt_digest",
                "checkpoint_cleared",
                "candidate_removed",
                "receipt_digest",
            }
            or receipt.get("schema_version") != 1
            or receipt.get("kind")
            != "national-policy-recorded-abandon-finalize"
            or receipt.get("evaluation_epoch") != EVALUATION_EPOCH
            or receipt.get("mode") != "execute"
            or receipt.get("checkpoint_cleared") is not True
            or receipt.get("candidate_removed") is not True
            or receipt.get("receipt_digest") != canonical_digest(unsigned)
        ):
            raise RuntimeError("recorded-abandon finalize receipt is invalid")
        archived_claim = RESULTS / "policy_epoch_recorded_abandon_finalize_claim.json"
        claim = _validate_recorded_finalize_claim(
            _read_json(archived_claim),
            require_current_head=False,
        )
        if receipt.get("claim_digest") != claim.get("claim_digest"):
            raise RuntimeError("recorded-abandon finalize receipt claim mismatch")
        historical_receipt = _recorded_finalize_chain_receipt(
            claim,
            require_chain_head=False,
        )
        if (
            historical_receipt is None
            or receipt.get("abandon_receipt_digest")
            != historical_receipt.get("receipt_digest")
        ):
            raise RuntimeError(
                "recorded-abandon completed receipt ledger identity mismatch"
            )
        return {"completed_receipt": receipt, "claim": claim}

    if os.path.lexists(LIVE_CLAIM):
        claim = _validate_recorded_finalize_claim(_read_json(LIVE_CLAIM))
        _recorded_finalize_chain_head(claim)
        if claim.get("schema_version") == 2:
            _validate_schema2_recorded_finalize_live_state(claim)
        return {"claim": claim, "resuming": True}

    checkpoint_raw = _safe_read_bytes(CHECKPOINT)
    try:
        checkpoint = json.loads(checkpoint_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("recorded-abandon checkpoint is unreadable") from exc
    if not isinstance(checkpoint, dict):
        raise RuntimeError("recorded-abandon checkpoint is not an object")
    import evolution_infra

    terminal = evolution_infra.recorded_abandon_receipt_for_checkpoint(
        checkpoint,
        path=LEDGER,
        project_root=ROOT,
    )
    if terminal is None:
        raise RuntimeError("checkpoint has no exact recorded abandon chain head")
    target = checkpoint.get("next_v")
    candidate_path = BOTS / bot_name(target)
    candidate_manifest = _tree_manifest(candidate_path)
    candidate_errors = _candidate_publication_errors(candidate_path, int(target))
    if candidate_errors:
        raise RuntimeError("; ".join(candidate_errors))
    claim_payload = {
        "schema_version": 1,
        "kind": "national-policy-recorded-abandon-finalize-claim",
        "evaluation_epoch": EVALUATION_EPOCH,
        "git_head": _git("rev-parse", "HEAD"),
        "checkout_role": "autonomous_evolution_runtime",
        "checkpoint": {
            "sha256": _sha256(checkpoint_raw),
            "next_v": target,
            "source_v": checkpoint.get("source_v"),
            "stage": checkpoint.get("stage"),
            "workflow_run_id": checkpoint.get("workflow_run_id"),
            "checkpoint_revision": checkpoint.get("checkpoint_revision"),
        },
        "abandon_receipt_digest": terminal["receipt_digest"],
        "abandon_reason": terminal["reason"],
        "candidate": {
            "path": str(candidate_path.relative_to(ROOT)),
            "manifest_digest": candidate_manifest["digest"],
            "entry_count": candidate_manifest["entry_count"],
            "total_bytes": candidate_manifest["total_bytes"],
        },
    }
    claim = {**claim_payload, "claim_digest": canonical_digest(claim_payload)}
    return {"claim": claim, "checkpoint": checkpoint, "terminal": terminal}


def _run_recorded_abandon_finalize(
    *,
    execute: bool,
    acknowledge_runtime_checkout: bool,
) -> dict:
    if execute:
        if not acknowledge_runtime_checkout:
            raise RuntimeError("execution requires --acknowledge-runtime-checkout")
        errors = [*_runtime_checkout_identity_errors(), *_runtime_process_errors()]
        if errors:
            raise RuntimeError("; ".join(errors))
    with (
        _reconciliation_lock() if execute else nullcontext()
    ), (
        _publication_linearization_lock() if execute else nullcontext()
    ):
        plan = _recorded_finalize_plan()
        if "completed_receipt" in plan:
            if execute and os.path.lexists(LIVE_CLAIM):
                live_claim = _validate_recorded_finalize_claim(
                    _read_json(LIVE_CLAIM)
                )
                if live_claim.get("claim_digest") != plan["completed_receipt"].get(
                    "claim_digest"
                ):
                    raise RuntimeError(
                        "recorded-abandon completed receipt/live claim mismatch"
                    )
                LIVE_CLAIM.unlink()
                _fsync_directory(RESULTS)
            return plan["completed_receipt"]
        claim = plan["claim"]
        if not execute:
            return {**claim, "mode": "dry_run", "mutates": False}
        if claim.get("schema_version") == 2:
            post_claim_errors = [
                *_runtime_checkout_identity_errors(),
                *_runtime_process_errors(),
            ]
            if post_claim_errors:
                raise RuntimeError("; ".join(post_claim_errors))
            checkpoint_identity = claim["checkpoint"]
            if os.path.lexists(CHECKPOINT):
                checkpoint_value = _read_json(CHECKPOINT)
                if canonical_digest(checkpoint_value) != checkpoint_identity["digest"]:
                    raise RuntimeError("recorded-abandon checkpoint changed after claim")
            _recorded_finalize_chain_head(claim)
            import asyncio
            import tool_bot_management as bot_management

            # The operator command already owns the publication lock.  Reuse
            # the same transaction kernel without acquiring a second flock.
            result = asyncio.run(bot_management._do_abandon_generation(
                reason=claim["abandon_reason"],
                _bypass_rate_limit=True,
                _publication_lock_owned=True,
                expected_workflow_run_id=checkpoint_identity["workflow_run_id"],
                expected_next_v=checkpoint_identity["next_v"],
                expected_source_v=checkpoint_identity["source_v"],
                expected_checkpoint_revision=checkpoint_identity[
                    "checkpoint_revision"
                ],
                expected_checkpoint_stage=checkpoint_identity["stage"],
            ))
            if result.get("abandoned") is not True:
                raise RuntimeError(
                    "recorded-abandon schema2 finalize failed: "
                    + json.dumps(result, ensure_ascii=False, sort_keys=True)[:1000]
                )
            transaction_receipt = (
                RESULTS
                / "policy_epoch_abandon_transactions"
                / claim["transaction_id"]
                / "receipt.json"
            )
            receipt = _read_json(transaction_receipt)
            if receipt.get("claim_digest") != claim.get("claim_digest"):
                raise RuntimeError("recorded-abandon schema2 receipt claim mismatch")
            return receipt
        archived_claim = RESULTS / "policy_epoch_recorded_abandon_finalize_claim.json"
        _ensure_claim(LIVE_CLAIM, claim)
        _ensure_claim(archived_claim, claim)
        post_claim_errors = [
            *_runtime_checkout_identity_errors(),
            *_runtime_process_errors(),
        ]
        if post_claim_errors:
            raise RuntimeError("; ".join(post_claim_errors))
        _recorded_finalize_chain_head(claim)

        checkpoint_identity = claim["checkpoint"]
        candidate_path = ROOT / claim["candidate"]["path"]
        if os.path.lexists(CHECKPOINT):
            if _sha256(_safe_read_bytes(CHECKPOINT)) != checkpoint_identity["sha256"]:
                raise RuntimeError("recorded-abandon checkpoint changed after claim")
            # A previous attempt may already have durably deleted the exact
            # claimed candidate and then lost the checkpoint CAS race.  Missing
            # bytes are therefore an idempotent completed effect; present bytes
            # must still match the immutable preimage before retry.
            if os.path.lexists(candidate_path):
                _validate_recorded_finalize_candidate(claim, candidate_path)
            if os.path.lexists(candidate_path):
                _remove_recorded_finalize_candidate(claim, candidate_path)
            else:
                _fsync_directory(candidate_path.parent)
            import evolution_infra

            if not evolution_infra.clear_pipeline_checkpoint(
                expected_workflow_run_id=checkpoint_identity["workflow_run_id"],
                expected_next_v=checkpoint_identity["next_v"],
                expected_source_v=checkpoint_identity["source_v"],
                expected_checkpoint_revision=checkpoint_identity[
                    "checkpoint_revision"
                ],
                expected_checkpoint_stage=checkpoint_identity["stage"],
            ):
                raise RuntimeError("recorded-abandon checkpoint CAS failed")
        elif os.path.lexists(candidate_path):
            # Compatibility recovery for the former receipt -> checkpoint
            # clear -> candidate delete ordering.  The persisted claim binds
            # the exact candidate manifest and unpublished predicates, so this
            # cannot turn unrelated or drifted bytes into cleanup authority.
            _remove_recorded_finalize_candidate(claim, candidate_path)
        if os.path.lexists(CHECKPOINT):
            raise RuntimeError("recorded-abandon checkpoint still exists")
        if os.path.lexists(candidate_path):
            raise RuntimeError("recorded-abandon candidate still exists")

        payload = {
            "schema_version": 1,
            "kind": "national-policy-recorded-abandon-finalize",
            "evaluation_epoch": EVALUATION_EPOCH,
            "mode": "execute",
            "claim_digest": claim["claim_digest"],
            "workflow_run_id": checkpoint_identity["workflow_run_id"],
            "abandon_receipt_digest": claim["abandon_receipt_digest"],
            "checkpoint_cleared": True,
            "candidate_removed": True,
        }
        receipt = {**payload, "receipt_digest": canonical_digest(payload)}
        if os.path.lexists(RECORDED_FINALIZE_RECEIPT):
            if _read_json(RECORDED_FINALIZE_RECEIPT) != receipt:
                raise RuntimeError("recorded-abandon finalize receipt conflict")
        else:
            _write_json_exclusive(RECORDED_FINALIZE_RECEIPT, receipt)
        LIVE_CLAIM.unlink(missing_ok=True)
        _fsync_directory(RESULTS)
        return receipt


def run(
    *,
    execute: bool,
    acknowledge_runtime_checkout: bool = False,
    quarantine_legacy_ledger_and_abandon_checkpoint: bool = False,
    finalize_recorded_abandon_checkpoint: bool = False,
) -> dict:
    _validate_runtime_input_roots()
    if finalize_recorded_abandon_checkpoint:
        if quarantine_legacy_ledger_and_abandon_checkpoint:
            raise RuntimeError("choose exactly one reconciliation mode")
        return _run_recorded_abandon_finalize(
            execute=execute,
            acknowledge_runtime_checkout=acknowledge_runtime_checkout,
        )
    if not quarantine_legacy_ledger_and_abandon_checkpoint:
        raise RuntimeError(
            "reconciliation requires --quarantine-legacy-ledger-and-abandon-checkpoint"
        )
    if execute:
        if not acknowledge_runtime_checkout:
            raise RuntimeError(
                "execution requires --acknowledge-runtime-checkout"
            )
        errors = [
            *_runtime_checkout_identity_errors(),
            *_runtime_process_errors(),
        ]
        if errors:
            raise RuntimeError("; ".join(errors))

    with (
        _reconciliation_lock() if execute else nullcontext()
    ), (
        _publication_linearization_lock() if execute else nullcontext()
    ):
        plan = _build_plan()
        if "completed_receipt" in plan:
            if execute and os.path.lexists(LIVE_CLAIM):
                claim = _validated_claim(LIVE_CLAIM)
                if claim.get("claim_digest") != plan["completed_receipt"].get(
                    "claim_digest"
                ):
                    raise RuntimeError("completed receipt/live claim mismatch")
                LIVE_CLAIM.unlink()
                _fsync_directory(RESULTS)
            return plan["completed_receipt"]
        claim = plan["claim"]
        if not execute:
            return {
                **claim,
                "mode": "dry_run",
                "mutates": False,
            }

        # Cloud-epoch ledger archive: no checkpoint, no candidate — just archive
        # the stale ledger and write a fresh empty one.  This is a much simpler
        # transaction than the legacy quarantine path.
        if plan.get("cloud_epoch_ledger_archive"):
            import evolution_infra

            archive_root = plan["archive_root"]
            canonical_archive_root = _archive_root_from_relative(
                claim.get("archive_root"),
                create=True,
                require_exists=True,
            )
            if archive_root != canonical_archive_root:
                raise RuntimeError("cloud epoch archive root changed")
            _ensure_claim(LIVE_CLAIM, claim)
            _ensure_claim(archive_root / "reconciliation_claim.json", claim)
            with evolution_infra._locked_state_sidecar(
                LEDGER,
                lock_type=fcntl.LOCK_EX,
            ):
                archived_legacy = archive_root / "legacy_abandoned_versions.jsonl"
                if os.path.lexists(archived_legacy):
                    if (
                        _sha256(_safe_read_bytes(archived_legacy))
                        != claim["inputs"]["ledger_sha256"]
                    ):
                        raise RuntimeError(
                            "cloud epoch archived ledger digest mismatch"
                        )
                else:
                    _move_exact_file(
                        LEDGER,
                        archived_legacy,
                        claim["inputs"]["ledger_sha256"],
                    )
                # Write a fresh empty ledger so the allocation authority can
                # compute next_v from the published high-water.
                LEDGER.write_text("", encoding="utf-8")
                _fsync_directory(LEDGER.parent)
            receipt_payload = {
                "schema_version": 1,
                "kind": "national-policy-runtime-reconciliation-receipt",
                "evaluation_epoch": EVALUATION_EPOCH,
                "claim_digest": claim["claim_digest"],
                "action": "cloud_epoch_ledger_archive",
                "archive_root": str(archive_root.relative_to(ROOT)),
                "published_high_water": claim["inputs"]["published_high_water"],
                "archived_ledger_sha256": claim["inputs"]["ledger_sha256"],
                "archived_ledger_entry_count": claim["inputs"][
                    "ledger_entry_count"
                ],
                "completed_at": time.time(),
            }
            receipt = {
                **receipt_payload,
                "receipt_digest": canonical_digest(receipt_payload),
            }
            _write_bytes_exclusive(LIVE_RECEIPT, (json.dumps(receipt) + "\n").encode("utf-8"))
            _fsync_directory(LIVE_RECEIPT.parent)
            LIVE_CLAIM.unlink()
            _fsync_directory(RESULTS)
            return receipt

        archive_root = plan["archive_root"]
        canonical_archive_root = _archive_root_from_relative(
            claim.get("archive_root"),
            create=True,
            require_exists=True,
        )
        if archive_root != canonical_archive_root:
            raise RuntimeError("reconciliation plan archive root changed")
        for destination_directory in (
            archive_root / "runtime_control",
            archive_root / "candidate",
            archive_root / "checkpoint_auxiliary",
        ):
            _safe_directory_chain(
                destination_directory,
                base=ARCHIVE_BASE,
                create=False,
            )
        _ensure_claim(LIVE_CLAIM, claim)
        _ensure_claim(archive_root / "reconciliation_claim.json", claim)
        # The durable claim is now a launch barrier.  Re-open the process
        # predicate after publishing it so a process that raced the first scan
        # is caught before any control state moves.
        post_claim_errors = [
            *_runtime_checkout_identity_errors(),
            *_runtime_process_errors(),
        ]
        if post_claim_errors:
            raise RuntimeError("; ".join(post_claim_errors))
        workflow_fence = _fence_worker_workflow(
            plan["checkpoint"],
            claim["claim_digest"],
        )

        control_archive = archive_root / "runtime_control"
        for item in claim["inputs"]["runtime_control"]["files"]:
            source = RESULTS / item["name"]
            destination = control_archive / item["name"]
            if os.path.lexists(destination):
                if _sha256(_safe_read_bytes(destination)) != item["sha256"]:
                    raise RuntimeError(
                        f"archived runtime control digest mismatch: {item['name']}"
                    )
            else:
                _move_exact_file(source, destination, item["sha256"])
        cost_scope_path = control_archive / "generation_cost_scope.jsonl"
        if os.path.lexists(cost_scope_path):
            if _sha256(_safe_read_bytes(cost_scope_path)) != (
                claim["inputs"]["runtime_control"]["cost_scope_sha256"]
            ):
                raise RuntimeError("archived generation cost scope digest mismatch")
        else:
            _safe_directory_chain(
                cost_scope_path.parent,
                base=ARCHIVE_BASE,
                create=True,
            )
            _write_bytes_exclusive(cost_scope_path, plan["cost_scope"])

        ledger = claim["inputs"]["legacy_ledger"]
        checkpoint_identity = claim["inputs"]["checkpoint"]
        import evolution_infra

        with evolution_infra._locked_state_sidecar(
            CHECKPOINT,
            lock_type=fcntl.LOCK_EX,
        ), evolution_infra._locked_state_sidecar(
            LEDGER,
            lock_type=fcntl.LOCK_EX,
        ):
            archived_legacy = archive_root / "legacy_abandoned_versions.jsonl"
            if os.path.lexists(archived_legacy):
                if _sha256(_safe_read_bytes(archived_legacy)) != ledger["sha256"]:
                    raise RuntimeError("archived legacy ledger digest mismatch")
            else:
                _move_exact_file(
                    LEDGER,
                    archived_legacy,
                    ledger["sha256"],
                )
            _move_exact_file(
                CHECKPOINT,
                archive_root / "pipeline_state.json",
                checkpoint_identity["sha256"],
            )
        before_git = _destructive_git_snapshot(
            int(claim["inputs"]["checkpoint"]["next_v"])
        )
        _assert_destructive_git_snapshot(
            before_git,
            expected_head=claim["git_head"],
        )
        _move_candidate(
            plan["candidate"],
            archive_root / "candidate" / plan["candidate"].name,
            plan["candidate_manifest"]["digest"],
            version=int(claim["inputs"]["checkpoint"]["next_v"]),
        )
        after_git = _destructive_git_snapshot(
            int(claim["inputs"]["checkpoint"]["next_v"])
        )
        _assert_destructive_git_snapshot(
            after_git,
            expected_head=claim["git_head"],
        )

        # Sidecars/backups are non-authoritative, but quarantine them rather
        # than deleting them.  They are not included in the new receipt chain.
        for auxiliary in sorted(RESULTS.glob("pipeline_state.json.*")):
            if (
                auxiliary.name.endswith(".lock")
                or auxiliary == LIVE_CLAIM
                or auxiliary.is_symlink()
                or not auxiliary.is_file()
            ):
                continue
            raw = _safe_read_bytes(auxiliary)
            _move_exact_file(
                auxiliary,
                archive_root / "checkpoint_auxiliary" / auxiliary.name,
                _sha256(raw),
            )
        allocation_receipt = None
        if plan["upgraded_checkpoint"] is not None:
            allocation_receipt = evolution_infra.append_abandoned_version_receipt(
                plan["upgraded_checkpoint"],
                reason=claim["terminal_abandon"]["reason"],
                infra_failure=claim["terminal_abandon"]["infra_failure"],
                timestamp=claim["terminal_abandon"]["timestamp"],
                path=LEDGER,
                project_root=ROOT,
            )

        receipt_payload = {
            "schema_version": 1,
            "kind": "national-policy-runtime-reconciliation",
            "evaluation_epoch": EVALUATION_EPOCH,
            "mode": "execute",
            "claim_digest": claim["claim_digest"],
            "archive_root": claim["archive_root"],
            "legacy_rows_authority_weight": 0,
            "allocation_receipt_digest": (
                allocation_receipt.get("receipt_digest")
                if allocation_receipt is not None
                else None
            ),
            "abandoned_workflow_run_id": checkpoint_identity["workflow_run_id"],
            "workflow_fence": workflow_fence,
            "next_target_version": ARCHIVED_VERSION_HIGH_WATER + 1,
            "next_workflow_attempt": (
                checkpoint_identity["workflow_attempt"] + 1
                if checkpoint_identity["next_v"] == FIRST_STRICT_POLICY_VERSION
                else 1
            ),
        }
        receipt = {
            **receipt_payload,
            "receipt_digest": canonical_digest(receipt_payload),
        }
        archive_receipt = archive_root / "reconciliation_receipt.json"
        if os.path.lexists(archive_receipt):
            if _read_json(archive_receipt) != receipt:
                raise RuntimeError("archive reconciliation receipt conflict")
        else:
            _write_json_exclusive(archive_receipt, receipt)
        if os.path.lexists(LIVE_RECEIPT):
            if _read_json(LIVE_RECEIPT) != receipt:
                raise RuntimeError("live reconciliation receipt conflict")
        else:
            _write_json_exclusive(LIVE_RECEIPT, receipt)
        LIVE_CLAIM.unlink(missing_ok=True)
        _fsync_directory(RESULTS)
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stopped-runtime quarantine for legacy abandon rows and their exact "
            "active checkpoint. Omit --execute for a read-only digest plan."
        )
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--acknowledge-runtime-checkout", action="store_true")
    parser.add_argument(
        "--quarantine-legacy-ledger-and-abandon-checkpoint",
        action="store_true",
    )
    parser.add_argument(
        "--finalize-recorded-abandon-checkpoint",
        action="store_true",
    )
    args = parser.parse_args()
    result = run(
        execute=args.execute,
        acknowledge_runtime_checkout=args.acknowledge_runtime_checkout,
        quarantine_legacy_ledger_and_abandon_checkpoint=(
            args.quarantine_legacy_ledger_and_abandon_checkpoint
        ),
        finalize_recorded_abandon_checkpoint=(
            args.finalize_recorded_abandon_checkpoint
        ),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
