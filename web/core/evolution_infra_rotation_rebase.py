"""events.jsonl rotation rebase v2: gzip archive + live re-head + watermark rebase.

Layered ON TOP of the unchanged per-publication copy-only rotation in
``evolution_infra_archive_rotation`` (which is not replaced and, beyond
the watermark-v3 loader branch and the recovery entry calls, is not
modified).  Scope is exactly one source: ``events.jsonl``.
``match_history.jsonl`` is the replay-GC reference set and must never be
re-headed here.

Digest chain
------------
``v2 watermark @R`` (plain batches cover ``[0..R)``)
  -> gzip batch receipt (``[R..C)``, dual digest: plaintext + gzip)
  -> rebase state (prepared -> completed, binds ``kept_prefix_sha256``)
  -> v3 baseline watermark (end 0, pwd = COMPLETED rebase-state digest)
  -> next publication rotation writes v2 again (chains onto the v3 digest).

Crash safety / recovery (review B1/B2, 2026-10-21)
--------------------------------------------------
* ``prepared`` written, replace not yet effective, process dies: event
  writers immediately resume appending, so recovery decides by HASH --
  ``sha256(live[0..drop_offset)) == dropped_sha256`` on the pinned
  (dev,ino) -- never by size equality.  The redo rebuilds the tail from
  the CURRENT live end, so the post-crash delta is preserved with zero
  byte loss.
* replace effective, v3 watermark not yet written: the stale v2
  watermark end exceeds the re-headed live length and every watermark
  consumer would raise.  ``recover_pending_rebase`` converges this, and
  ``ensure_rotation_source_reconciled`` is invoked at the entry of every
  watermark-consuming rotation path (build/validate/rotate/receipts) so
  recovery NEVER depends on ``POK_EVENTS_REBASE_ENABLED`` or
  ``POK_DISK_HYGIENE_ENABLED``: finishing an already-committed rebase is
  recovery, not rebase enablement.  The app lifespan should also call
  ``ensure_rotation_source_reconciled`` once at startup (B-track).
* R1: the v3 watermark's ``previous_watermark_digest`` binds the digest
  of the COMPLETED record (``completed_at`` is fixed at prepare time so
  the completed digest is precomputable and deterministic); the
  watermark->completed window is a deliberate transient inconsistency
  converged by recovery branch 2.  pwd = prepared digest is forbidden.
* R2: the full superseded watermark object is preserved inside the
  rebase state (``superseded_watermark``), so the chain stays walkable
  after the v3 baseline overwrites the watermark file.

Locking
-------
The re-head critical section holds the source sidecar EX
(``events.jsonl.lock`` -- the same lock as every writer, rotation and
reader) for milliseconds: all heavy IO (gzip, tail staging, dropped
prefix hashing) happens lock-free against a pinned append-only view.
While holding that EX the code never acquires the bot publication lock,
the handoff journal lock, or any lock whose holder may want the events
lock (no ABBA); the handoff safety-window check uses the lock-free
``pending_handoff_route`` reader only.

Rollback runbook (deploy rollback / wedged states)
--------------------------------------------------
1. Rebase already happened, watermark file is v3 and old code cannot
   load it: stop the service, delete
   ``<ARCHIVE_DIR>/events.rotation-watermark.json`` and set
   ``POK_EVENTS_REBASE_ENABLED=0``.  Old code then falls back to the
   base watermark (end 0) and re-plans copy-only rotation against the
   re-headed live; no bytes are lost (the gzip batch + receipt remain).
2. v2 watermark + already re-headed live (crash window or hand-restored
   v2 bytes): same action -- delete the watermark file; the base
   watermark is valid against any live prefix and planning restarts
   from offset 0.
3. A stuck ``prepared`` state self-heals on the next
   ``ensure_rotation_source_reconciled`` call; manual intervention is
   never required for it.

Retention horizon
-----------------
``apply_archive_batch_retention`` deletes archive batches older than the
retention window.  Within the horizon every non-terminal handoff receipt
remains re-provable; beyond it only the digest chain head is verifiable
(``verify_rebase_chain``).  Never deleted: the live watermark's
``last_version`` batch pair, any non-terminal handoff's version pair,
the current rebase state and its own gzip batch, and every
``rotation-set-v*.plan.json`` authority.  Receipts are durably appended
to the retention ledger BEFORE each unlink.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import evolution_infra as _ei
import evolution_infra_archive_rotation as _ar

# ── frozen janitor entry constants ────────────────────────────────────
REBASE_SOURCE_NAME = "events.jsonl"  # v2 sole admitted source
EVENTS_REBASE_ENABLED_ENV = "POK_EVENTS_REBASE_ENABLED"
EVENTS_REBASE_MIN_COLD_BYTES_ENV = "POK_EVENTS_REBASE_MIN_COLD_BYTES"
EVENTS_REBASE_MAX_SPAN_BYTES_ENV = "POK_EVENTS_REBASE_MAX_SPAN_BYTES"
EVENTS_REBASE_MIN_INTERVAL_SEC_ENV = "POK_EVENTS_REBASE_MIN_INTERVAL_SEC"
ROTATION_ARCHIVE_RETENTION_DAYS_ENV = "POK_ROTATION_ARCHIVE_RETENTION_DAYS"

REBASE_STATE_KIND = "append-log-rebase-state-v1"
WATERMARK_V3_KIND = "append-log-archive-watermark-v3"
GZIP_BATCH_KIND = "append-log-gzip-batch-v1"
RETENTION_LEDGER_KIND = "append-log-archive-retention-v1"

_EVENTS_REBASE_ENABLED_DEFAULT = True
_EVENTS_REBASE_MIN_COLD_BYTES_DEFAULT = 64 * 1024 * 1024
_EVENTS_REBASE_MAX_SPAN_BYTES_DEFAULT = 256 * 1024 * 1024
_EVENTS_REBASE_MIN_INTERVAL_SEC_DEFAULT = 21600.0
_ROTATION_ARCHIVE_RETENTION_DAYS_DEFAULT = 30.0
_RETENTION_MIN_INTERVAL_SEC_DEFAULT = 3600.0
# Staging chunk for gzip/tail streams (bounded memory).
_REBASE_STREAM_CHUNK_BYTES = 1024 * 1024
# Crash-leftover temps in ARCHIVE_DIR self-swept after this age (R4: the
# disk-hygiene janitor only reaps top-level RESULTS_DIR temps).
_TMP_SWEEP_AGE_SEC = 3600.0

_REBASE_STATE_KEYS = frozenset({
    "schema_version", "kind", "source", "rebase_id", "state",
    "old_size", "drop_offset", "dropped_sha256", "kept_size",
    "kept_prefix_sha256", "pinned_dev", "pinned_ino", "gzip_batches",
    "superseded_watermark_digest", "superseded_watermark",
    "previous_rebase_digest", "created_at", "completed_at", "digest",
})
_GZIP_BATCH_KEYS = frozenset({
    "schema_version", "kind", "source", "rebase_id", "range_start",
    "range_end", "plaintext_sha256", "gzip_sha256", "plaintext_bytes",
    "gzip_bytes", "previous_watermark_digest", "digest",
})
_GZIP_BATCH_ENTRY_KEYS = frozenset({
    "range_start", "range_end", "plaintext_sha256", "gzip_sha256",
    "plaintext_bytes", "gzip_bytes",
})
_RETENTION_LEDGER_KEYS = frozenset({
    "schema_version", "kind", "source", "name", "version", "rebase_id",
    "bytes", "sha256", "deleted_at", "digest",
})


# ── test/regression seams ─────────────────────────────────────────────
def _os_replace(source, destination):
    """Live re-head rename seam (crash-injection tests patch this).

    Only the SOURCE re-head goes through this seam; archive file
    publications use ``os.replace`` directly so an injected first-call
    crash always lands on the live re-head boundary.
    """

    return os.replace(source, destination)


def _disk_free_bytes(path) -> int:
    return shutil.disk_usage(str(path)).free


def _pending_handoff_route():
    """Lock-free pending-handoff probe (never takes the journal lock)."""

    from post_publication_handoff import pending_handoff_route

    return pending_handoff_route()


# ── env/param resolution ──────────────────────────────────────────────
def _resolve_flag(raw_value, default: bool) -> bool:
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    text = str(raw_value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_float(raw_value, default: float, env_key: str) -> float:
    if raw_value is not None:
        return float(raw_value)
    text = os.environ.get(env_key) if env_key else None
    if text is None:
        return default
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _resolve_int(raw_value, default: int, env_key: str) -> int:
    if raw_value is not None:
        return int(raw_value)
    text = os.environ.get(env_key)
    if text is None:
        return default
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


def _skip(source_path: Path, reason: str, *, error: bool = False) -> dict:
    return {
        "ok": False,
        "status": "error" if error else "skipped",
        "reason": reason,
        "source": Path(source_path).name,
    }


# ── frozen path helpers ───────────────────────────────────────────────
def rebase_state_path(source_path: Path) -> Path:
    return Path(_ei.ARCHIVE_DIR) / f"{Path(source_path).stem}.rebase-state.json"


def gzip_batch_paths(source_path: Path, rebase_id: str) -> tuple[Path, Path]:
    label = str(rebase_id)[:16]
    root = Path(_ei.ARCHIVE_DIR)
    stem = Path(source_path).stem
    return (
        root / f"{stem}.rebase-{label}.jsonl.gz",
        root / f"{stem}.rebase-{label}.receipt.json",
    )


def archive_retention_ledger_path(source_path: Path) -> Path:
    return (
        Path(_ei.ARCHIVE_DIR)
        / f"{Path(source_path).stem}.archive-retention.jsonl"
    )


# ── streaming helpers ─────────────────────────────────────────────────
def _iter_raw_range(raw, start: int, end: int):
    offset = start
    while offset < end:
        stop = min(end, offset + _REBASE_STREAM_CHUNK_BYTES)
        chunk = (
            raw[offset:stop]
            if isinstance(raw, (bytes, bytearray))
            else raw.read_range(offset, stop)
        )
        if not chunk:
            raise OSError("rebase source range unavailable")
        yield chunk
        offset += len(chunk)


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_REBASE_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _read_path_range(path: Path, start: int, end: int) -> bytes:
    parts = []
    with open(path, "rb") as handle:
        handle.seek(start)
        remaining = end - start
        while remaining > 0:
            chunk = handle.read(min(remaining, _REBASE_STREAM_CHUNK_BYTES))
            if not chunk:
                raise OSError("rebase live range unavailable")
            parts.append(chunk)
            remaining -= len(chunk)
    return parts[0] if len(parts) == 1 else b"".join(parts)


def _sha256_prefix_of_path(path: Path, length: int) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(remaining, _REBASE_STREAM_CHUNK_BYTES))
            if not chunk:
                raise OSError("rebase live prefix unavailable")
            hasher.update(chunk)
            remaining -= len(chunk)
    return hasher.hexdigest()


def _snap_forward_to_line_end(raw, candidate: int, size: int) -> int:
    """Smallest line boundary (offset just past a ``\\n``) >= candidate.

    Falls back to the last boundary below candidate when the tail is an
    unterminated line, and to 0 when the region holds no boundary at
    all.  The re-headed live must start on a line boundary.
    """

    if size <= 0:
        return 0
    candidate = max(0, min(candidate, size))

    def _read(start: int, stop: int) -> bytes:
        return (
            raw[start:stop]
            if isinstance(raw, (bytes, bytearray))
            else raw.read_range(start, stop)
        )

    position = candidate
    while position < size:
        stop = min(size, position + _ar._ROTATION_SCAN_CHUNK_BYTES)
        chunk = _read(position, stop)
        index = chunk.find(b"\n")
        if index >= 0:
            return position + index + 1
        position = stop
    if candidate == size and _read(size - 1, size) == b"\n":
        return size
    # No boundary at/after candidate: settle on the last one below it.
    position = min(candidate, size)
    while position > 0:
        start = max(0, position - _ar._ROTATION_SCAN_CHUNK_BYTES)
        chunk = _read(start, position)
        index = chunk.rfind(b"\n")
        if index >= 0:
            return start + index + 1
        position = start
    return 0


# ── record IO ─────────────────────────────────────────────────────────
def _validate_gzip_batches(value) -> list:
    if not isinstance(value, list) or not value:
        raise RuntimeError("rebase gzip batches invalid")
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != set(
            _GZIP_BATCH_ENTRY_KEYS
        ):
            raise RuntimeError("rebase gzip batch fields mismatch")
        if (
            type(entry.get("range_start")) is not int
            or type(entry.get("range_end")) is not int
            or not 0 <= entry["range_start"] < entry["range_end"]
            or type(entry.get("plaintext_bytes")) is not int
            or type(entry.get("gzip_bytes")) is not int
            or not _ar._rotation_digest_value(entry.get("plaintext_sha256"))
            or not _ar._rotation_digest_value(entry.get("gzip_sha256"))
        ):
            raise RuntimeError("rebase gzip batch values invalid")
    return value


def load_rebase_state(source_path: Path) -> dict | None:
    """Read + digest-verify the rebase state; None when absent.

    Raises RuntimeError on empty/corrupt/mismatched records (exact key
    set + canonical digest, same discipline as ``_rotation_record``).
    """

    path = rebase_state_path(source_path)
    record = _ei._rotation_record(
        path,
        kind=REBASE_STATE_KIND,
        keys=_REBASE_STATE_KEYS,
    )
    if record is None:
        return None
    if (
        record.get("state") not in {"prepared", "completed"}
        or type(record.get("old_size")) is not int
        or type(record.get("drop_offset")) is not int
        or type(record.get("kept_size")) is not int
        or type(record.get("pinned_dev")) is not int
        or type(record.get("pinned_ino")) is not int
        or not 0 < record["drop_offset"] <= record["old_size"]
        # kept covers at least [drop_offset..old_size); it grows beyond
        # when writers appended after the pin (phase-4 delta / crash B1).
        or record["kept_size"] < record["old_size"] - record["drop_offset"]
        or not _ar._rotation_digest_value(record.get("dropped_sha256"))
        or not _ar._rotation_digest_value(record.get("kept_prefix_sha256"))
        or not _ar._rotation_digest_value(
            record.get("superseded_watermark_digest")
        )
        or not isinstance(record.get("superseded_watermark"), dict)
        or (
            record.get("previous_rebase_digest") is not None
            and not _ar._rotation_digest_value(
                record.get("previous_rebase_digest")
            )
        )
    ):
        raise RuntimeError(f"rebase state values invalid: {path.name}")
    _validate_gzip_batches(record.get("gzip_batches"))
    return record


def _read_watermark_record_light(source_path: Path) -> dict:
    """Kind-agnostic watermark read: exact keys + digest, no live-prefix
    hashing (kept out of the EX critical section)."""

    from bot_artifact import canonical_digest

    _archive, _plan, watermark_path = _ar._rotation_paths(
        Path(source_path), 0
    )
    with _ei._locked_state_sidecar(watermark_path, lock_type=fcntl.LOCK_SH):
        existed = os.path.lexists(watermark_path)
        raw = _ei._read_regular_state_text(watermark_path, allow_missing=True)
    if not existed or not raw.strip():
        return _ar._base_rotation_watermark(Path(source_path))
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"rebase watermark record invalid JSON: {watermark_path.name}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != set(_ar._ROTATION_WATERMARK_KEYS)
        or payload.get("digest")
        != canonical_digest(
            {key: value for key, value in payload.items() if key != "digest"}
        )
    ):
        raise RuntimeError(
            f"rebase watermark record invalid: {watermark_path.name}"
        )
    return payload


def load_rotation_watermark_v3(source_path: Path, raw) -> dict:
    """Validate the rebase-baseline watermark (kind v3) and return it.

    Requires end_offset == 0, the empty-prefix digest, null
    last_version/last_rotation_id/last_plan_digest, and
    previous_watermark_digest == digest of the COMPLETED rebase state.
    Any deviation raises RuntimeError.
    """

    source_path = Path(source_path)
    _archive, _plan, watermark_path = _ar._rotation_paths(source_path, 0)
    watermark = _ei._rotation_record(
        watermark_path,
        kind=WATERMARK_V3_KIND,
        keys=_ar._ROTATION_WATERMARK_KEYS,
    )
    if watermark is None:
        raise RuntimeError(
            f"archive watermark v3 missing: {source_path.name}"
        )
    if (
        watermark.get("source") != source_path.name
        or watermark.get("end_offset") != 0
        or watermark.get("prefix_sha256") != _ei._rotation_digest(b"")
        or watermark.get("last_version") is not None
        or watermark.get("last_rotation_id") is not None
        or watermark.get("last_plan_digest") is not None
        or not _ar._rotation_digest_value(
            watermark.get("previous_watermark_digest")
        )
    ):
        raise RuntimeError(
            f"archive watermark v3 baseline invalid: {source_path.name}"
        )
    state = load_rebase_state(source_path)
    if (
        state is None
        or state.get("state") != "completed"
        or state.get("digest") != watermark["previous_watermark_digest"]
    ):
        raise RuntimeError(
            f"archive watermark v3 rebase binding invalid: {source_path.name}"
        )
    return watermark


def ensure_rotation_source_reconciled(source_path: Path) -> dict | None:
    """B2: heal a prepared rebase before any watermark consumer runs.

    Called at the entry of every watermark-consuming rotation path
    (build/validate/rotate/receipts loops) and intended for the app
    lifespan startup.  Runs recovery regardless of
    ``POK_EVENTS_REBASE_ENABLED`` / ``POK_DISK_HYGIENE_ENABLED``.
    Returns the recovery outcome dict when a prepared state was
    converged, else None.  A corrupt rebase state raises (fail-closed).
    """

    source_path = Path(source_path)
    if not os.path.lexists(source_path):
        return None
    state = load_rebase_state(source_path)
    if state is None or state.get("state") != "prepared":
        return None
    return recover_pending_rebase(source_path)


# ── staging (lock-free, against the pinned append-only view) ─────────
def _stage_gzip_batch(raw, start: int, end: int, gz_path: Path) -> dict:
    """Stream [start..end) into a gzipped temp beside ``gz_path``."""

    temp_path = gz_path.with_name(f".{gz_path.name}.{uuid.uuid4().hex}.tmp")
    plaintext_hasher = hashlib.sha256()
    plaintext_bytes = 0
    try:
        with open(temp_path, "wb") as raw_handle:
            with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=0) as gz:
                for chunk in _iter_raw_range(raw, start, end):
                    gz.write(chunk)
                    plaintext_hasher.update(chunk)
                    plaintext_bytes += len(chunk)
            raw_handle.flush()
            os.fsync(raw_handle.fileno())
        gzip_bytes = temp_path.stat().st_size
        gzip_sha256 = _sha256_file(temp_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return {
        "temp_path": temp_path,
        "gzip_path": gz_path,
        "gzip_sha256": gzip_sha256,
        "plaintext_sha256": plaintext_hasher.hexdigest(),
        "plaintext_bytes": plaintext_bytes,
        "gzip_bytes": gzip_bytes,
    }


def _stage_tail_to_temp(target_path: Path, read_range, start: int, end: int):
    """Stream [start..end) into a same-directory temp + incremental hash.

    Returns (temp_path, hasher).  The temp lives beside the live source
    so the final rename is same-filesystem and atomic.
    """

    temp_path = target_path.with_name(
        f".{target_path.name}.{uuid.uuid4().hex}.tmp"
    )
    hasher = hashlib.sha256()
    try:
        with open(temp_path, "wb") as handle:
            offset = start
            while offset < end:
                stop = min(end, offset + _REBASE_STREAM_CHUNK_BYTES)
                chunk = read_range(offset, stop)
                if not chunk:
                    raise OSError("rebase tail range unavailable")
                handle.write(chunk)
                hasher.update(chunk)
                offset += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path, hasher


def _publish_gzip_batch(staged: dict) -> None:
    """Create-once publish of the staged gzip file (digest-compare when
    it already exists); the receipt is written by the caller after."""

    gz_path = Path(staged["gzip_path"])
    temp_path = Path(staged["temp_path"])
    with _ei._locked_state_sidecar(gz_path, lock_type=fcntl.LOCK_EX):
        if os.path.lexists(gz_path):
            if _sha256_file(gz_path) != staged["gzip_sha256"]:
                raise RuntimeError(
                    f"rebase gzip batch already differs: {gz_path.name}"
                )
            _ei._fsync_regular_state_file_and_parent(gz_path)
            temp_path.unlink(missing_ok=True)
            return
        _ei._assert_safe_state_parent(gz_path)
        os.replace(temp_path, gz_path)
        _ei._fsync_directory(gz_path.parent)


def _write_v3_watermark(source_path: Path, completed_digest: str) -> None:
    _archive, _plan, watermark_path = _ar._rotation_paths(
        Path(source_path), 0
    )
    _ei._write_rotation_record(
        watermark_path,
        {
            "schema_version": 2,
            "kind": WATERMARK_V3_KIND,
            "source": Path(source_path).name,
            "end_offset": 0,
            "prefix_sha256": _ei._rotation_digest(b""),
            "last_version": None,
            "last_rotation_id": None,
            "last_plan_digest": None,
            "previous_watermark_digest": completed_digest,
        },
        kind=WATERMARK_V3_KIND,
        keys=_ar._ROTATION_WATERMARK_KEYS,
    )


def _write_rebase_state(unsigned: dict) -> dict:
    return _ei._write_rotation_record(
        rebase_state_path(Path(unsigned["source"])),
        unsigned,
        kind=REBASE_STATE_KIND,
        keys=_REBASE_STATE_KEYS,
    )


def _finalize_rebase(source_path: Path, completed_unsigned: dict) -> dict:
    """Idempotent ⑥+⑦: v3 baseline watermark then completed state (R1).

    The v3 ``previous_watermark_digest`` binds the digest of the
    COMPLETED record (precomputed here); when the watermark already
    carries exactly that binding it is left untouched, which is what
    makes the ⑥->⑦ crash window converge without a rewrite.
    """

    from bot_artifact import canonical_digest

    completed_digest = canonical_digest(completed_unsigned)
    existing = _read_watermark_record_light(source_path)
    if not (
        existing.get("kind") == WATERMARK_V3_KIND
        and existing.get("previous_watermark_digest") == completed_digest
    ):
        _write_v3_watermark(source_path, completed_digest)
    final = _write_rebase_state(completed_unsigned)
    if final["digest"] != completed_digest:
        raise RuntimeError(
            "rebase completed digest drifted from v3 binding: "
            f"{Path(source_path).name}"
        )
    return final


# ── frozen janitor entries ────────────────────────────────────────────
def try_rebase_rotation_source(
    source_path: Path,
    *,
    results_dir: Path | None = None,
    keep_lines: int | None = None,
    max_span_bytes: int | None = None,
    min_cold_bytes: int | None = None,
    min_interval_sec: float | None = None,
    enabled: bool | None = None,
    now: float | None = None,
) -> dict:
    """Idempotent single rebase cycle: recover -> plan -> stage -> commit.

    ``keep_lines`` is accepted for signature compatibility and is
    reserved (rebase v2 coordinates are byte-based).  Skips return
    ``{"ok": False, "status": "skipped", "reason": <token>}`` without
    raising; only programmatic errors raise RuntimeError.
    """

    source_path = Path(source_path)
    if now is None:
        now = time.time()
    if source_path.name != REBASE_SOURCE_NAME:
        return _skip(source_path, "not_rebase_source")
    if results_dir is not None and (
        Path(results_dir) / REBASE_SOURCE_NAME != source_path
    ):
        return _skip(source_path, "not_rebase_source")

    # Phase 0 -- recovery first, independent of every gate (review B2).
    recovery = recover_pending_rebase(source_path, now=now)
    if recovery.get("status") == "error":
        recovery.setdefault("source", source_path.name)
        return recovery
    if recovery.get("applied"):
        return {
            "ok": True,
            "status": "recovered",
            "source": source_path.name,
            "recovery": recovery,
        }

    # Phase 1 -- gates.
    flag = _resolve_flag(
        enabled if enabled is not None else os.environ.get(
            EVENTS_REBASE_ENABLED_ENV
        ),
        _EVENTS_REBASE_ENABLED_DEFAULT,
    )
    if not flag:
        return _skip(source_path, "disabled")
    if not os.path.lexists(source_path):
        return _skip(source_path, "source_missing")
    min_cold = _resolve_int(
        min_cold_bytes,
        _EVENTS_REBASE_MIN_COLD_BYTES_DEFAULT,
        EVENTS_REBASE_MIN_COLD_BYTES_ENV,
    )
    max_span = _resolve_int(
        max_span_bytes,
        _EVENTS_REBASE_MAX_SPAN_BYTES_DEFAULT,
        EVENTS_REBASE_MAX_SPAN_BYTES_ENV,
    )
    min_interval = _resolve_float(
        min_interval_sec,
        _EVENTS_REBASE_MIN_INTERVAL_SEC_DEFAULT,
        EVENTS_REBASE_MIN_INTERVAL_SEC_ENV,
    )
    prior = load_rebase_state(source_path)
    if (
        prior is not None
        and prior.get("state") == "completed"
        and now - float(prior.get("completed_at") or 0.0) < min_interval
    ):
        return _skip(source_path, "interval")
    try:
        route = _pending_handoff_route()
    except Exception:
        return _skip(source_path, "window_unreadable")
    if route.get("status") != "none":
        return _skip(source_path, "handoff_active")

    # Phase 2 -- plan under SH with the pinned view (kept open for the
    # lock-free phase 3; the append-only contract keeps [0..size) stable
    # across the unlock).
    raw = None
    tail_temp = None
    try:
        try:
            with _ei.locked_file(
                source_path, "rb", lock_type=fcntl.LOCK_SH
            ):
                raw = _ar._open_rotation_source_view(source_path)
                watermark = _ar._load_rotation_watermark(source_path, raw)
                size = len(raw)
                pinned = os.lstat(source_path)
        except RuntimeError:
            return _skip(source_path, "watermark_invalid", error=True)
        rotation_end = int(watermark["end_offset"])
        if size - rotation_end < min_cold:
            return _skip(source_path, "below_threshold")
        candidate = min(rotation_end + max_span, size)
        drop_offset = _snap_forward_to_line_end(raw, candidate, size)
        if drop_offset <= rotation_end:
            return _skip(source_path, "below_threshold")
        needed = (size - drop_offset) + (drop_offset - rotation_end) + 1
        if _disk_free_bytes(source_path.parent) < needed:
            return _skip(source_path, "insufficient_disk")

        # Phase 3 -- heavy IO lock-free against the pinned view.
        rebase_id = uuid.uuid4().hex
        gz_path, receipt_path = gzip_batch_paths(source_path, rebase_id)
        staged = _stage_gzip_batch(raw, rotation_end, drop_offset, gz_path)
        dropped_sha256 = _ar._rotation_digest_range(raw, 0, drop_offset)
        tail_temp, kept_hasher = _stage_tail_to_temp(
            source_path, raw.read_range, drop_offset, size
        )
    finally:
        if raw is not None:
            _ar._close_rotation_source(raw)

    _publish_gzip_batch(staged)
    _ei._write_rotation_record(
        receipt_path,
        {
            "schema_version": 2,
            "kind": GZIP_BATCH_KIND,
            "source": source_path.name,
            "rebase_id": rebase_id,
            "range_start": rotation_end,
            "range_end": drop_offset,
            "plaintext_sha256": staged["plaintext_sha256"],
            "gzip_sha256": staged["gzip_sha256"],
            "plaintext_bytes": staged["plaintext_bytes"],
            "gzip_bytes": staged["gzip_bytes"],
            "previous_watermark_digest": watermark["digest"],
        },
        kind=GZIP_BATCH_KIND,
        keys=_GZIP_BATCH_KEYS,
    )

    # Phase 4 -- millisecond EX critical section on the source sidecar.
    try:
        with _ei._locked_state_sidecar(
            source_path, lock_type=fcntl.LOCK_EX
        ):
            live = os.lstat(source_path)
            if (live.st_dev, live.st_ino) != (
                pinned.st_dev,
                pinned.st_ino,
            ):
                return _skip(source_path, "live_changed")
            size_now = live.st_size
            if size_now < size:
                return _skip(source_path, "live_changed")
            if size_now > size:
                # Capture the concurrent appends: zero event loss.
                delta = _read_path_range(source_path, size, size_now)
                with open(tail_temp, "ab") as handle:
                    handle.write(delta)
                    handle.flush()
                    os.fsync(handle.fileno())
                kept_hasher.update(delta)
            try:
                route = _pending_handoff_route()
            except Exception:
                return _skip(source_path, "window_unreadable")
            if route.get("status") != "none":
                return _skip(source_path, "handoff_active")
            superseded_now = _read_watermark_record_light(source_path)
            previous_state = load_rebase_state(source_path)
            prepared_unsigned = {
                "schema_version": 2,
                "kind": REBASE_STATE_KIND,
                "source": source_path.name,
                "rebase_id": rebase_id,
                "state": "prepared",
                "old_size": size,
                "drop_offset": drop_offset,
                "dropped_sha256": dropped_sha256,
                "kept_size": size_now - drop_offset,
                "kept_prefix_sha256": kept_hasher.hexdigest(),
                "pinned_dev": pinned.st_dev,
                "pinned_ino": pinned.st_ino,
                "gzip_batches": [
                    {
                        "range_start": rotation_end,
                        "range_end": drop_offset,
                        "plaintext_sha256": staged["plaintext_sha256"],
                        "gzip_sha256": staged["gzip_sha256"],
                        "plaintext_bytes": staged["plaintext_bytes"],
                        "gzip_bytes": staged["gzip_bytes"],
                    }
                ],
                "superseded_watermark_digest": superseded_now["digest"],
                "superseded_watermark": superseded_now,
                "previous_rebase_digest": (
                    previous_state["digest"]
                    if previous_state is not None
                    else None
                ),
                "created_at": now,
                # R1: fixed at prepare so the completed digest is
                # precomputable and survives the ⑥->⑦ crash window.
                "completed_at": now,
            }
            _write_rebase_state(prepared_unsigned)
            _os_replace(tail_temp, source_path)
            tail_temp = None
            _ei._fsync_directory(source_path.parent)
            completed_unsigned = dict(prepared_unsigned)
            completed_unsigned["state"] = "completed"
            _finalize_rebase(source_path, completed_unsigned)
    finally:
        if tail_temp is not None:
            tail_temp.unlink(missing_ok=True)
    return {
        "ok": True,
        "status": "rebased",
        "source": source_path.name,
        "rebase_id": rebase_id,
        "old_size": size,
        "new_size": size_now - drop_offset,
        "drop_offset": drop_offset,
        "gzip_bytes": staged["gzip_bytes"],
        "superseded_watermark_digest": superseded_now["digest"],
    }


def recover_pending_rebase(source_path: Path, *, now: float | None = None):
    """Idempotently finish a ``prepared`` rebase state (crash recovery).

    Branch 1 (B1): live is still the pinned inode -- proven by HASH over
    ``[0..drop_offset)`` (never by size equality; writers may have
    appended after the crash); the tail is rebuilt from the CURRENT live
    end so no post-crash byte is lost, then the re-head is redone.  It
    defers while a handoff is pending (the old v2 watermark is still
    valid against the un-re-headed live, so deferral loses nothing).
    Branch 2: live is already re-headed -- proven by the kept-prefix
    hash; the v3 watermark (idempotent) and the completed state are
    written unconditionally (nothing is left to protect).  Anything else
    is ``live_unreconcilable`` and left untouched.
    """

    source_path = Path(source_path)
    if now is None:
        now = time.time()
    try:
        state = load_rebase_state(source_path)
    except RuntimeError as exc:
        return {
            "ok": False,
            "status": "error",
            "reason": "state_unreadable",
            "source": source_path.name,
            "detail": str(exc),
        }
    if state is None or state.get("state") != "prepared":
        return {
            "ok": True,
            "status": "recovered",
            "applied": False,
            "reason": "no_pending_rebase",
            "source": source_path.name,
        }
    if not os.path.lexists(source_path):
        return _skip(source_path, "source_missing", error=True)
    drop_offset = int(state["drop_offset"])
    dropped_sha256 = state["dropped_sha256"]
    pinned = (int(state["pinned_dev"]), int(state["pinned_ino"]))

    # B1: prove the old-live prefix by hash OUTSIDE the EX section (the
    # dropped prefix can be ~1 GB; the EX hold stays millisecond-scale).
    # The view's own (dev,ino) is compared against the path so a replace
    # between open and lstat can never mis-attribute the proof.
    prefix_ok = False
    raw = None
    try:
        raw = _ar._open_rotation_source_view(source_path)
        identity = os.fstat(raw._descriptor)  # same-package pinned view
        path_identity = os.lstat(source_path)
        if (identity.st_dev, identity.st_ino) == (
            path_identity.st_dev,
            path_identity.st_ino,
        ) and len(raw) >= drop_offset:
            prefix_ok = (
                _ar._rotation_digest_range(raw, 0, drop_offset)
                == dropped_sha256
            )
    except OSError:
        prefix_ok = False
    finally:
        if raw is not None:
            _ar._close_rotation_source(raw)

    with _ei._locked_state_sidecar(source_path, lock_type=fcntl.LOCK_EX):
        state = load_rebase_state(source_path)
        if state is None or state.get("state") != "prepared":
            return {
                "ok": True,
                "status": "recovered",
                "applied": False,
                "reason": "converged",
                "source": source_path.name,
            }
        live = os.lstat(source_path)
        kept_size = int(state["kept_size"])
        kept_sha = state["kept_prefix_sha256"]
        if (live.st_dev, live.st_ino) == pinned and prefix_ok:
            # Branch 1: still the old (possibly grown) live.
            try:
                route = _pending_handoff_route()
            except Exception:
                return _skip(source_path, "window_unreadable", error=True)
            if route.get("status") != "none":
                return _skip(source_path, "handoff_active", error=True)
            size_now = live.st_size
            if size_now < drop_offset:
                return _skip(
                    source_path, "live_unreconcilable", error=True
                )
            _tail_temp, hasher = _stage_tail_to_temp(
                source_path,
                lambda start, end: _read_path_range(
                    source_path, start, end
                ),
                drop_offset,
                size_now,
            )
            kept_size = size_now - drop_offset
            kept_sha = hasher.hexdigest()
            _os_replace(_tail_temp, source_path)
            _ei._fsync_directory(source_path.parent)
        else:
            # Branch 2: prove the already-re-headed prefix (append-only
            # growth beyond kept_size is fine; only the prefix binds).
            if live.st_size < kept_size or (
                _sha256_prefix_of_path(source_path, kept_size) != kept_sha
            ):
                return _skip(
                    source_path, "live_unreconcilable", error=True
                )
        completed_unsigned = {
            key: value
            for key, value in state.items()
            if key != "digest"
        }
        completed_unsigned["state"] = "completed"
        completed_unsigned["kept_size"] = kept_size
        completed_unsigned["kept_prefix_sha256"] = kept_sha
        _finalize_rebase(source_path, completed_unsigned)
    return {
        "ok": True,
        "status": "recovered",
        "applied": True,
        "source": source_path.name,
        "drop_offset": drop_offset,
        "kept_size": kept_size,
    }


# ── retention ─────────────────────────────────────────────────────────
def _last_ledger_deletion_ts(ledger: Path) -> float | None:
    if not ledger.is_file():
        return None
    try:
        rows = [
            line
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not rows:
            return None
        return float(json.loads(rows[-1]).get("deleted_at"))
    except Exception:
        return None


def _append_retention_ledger_line(ledger: Path, row: dict) -> None:
    from bot_artifact import canonical_digest

    final = {**row, "digest": canonical_digest(row)}
    line = json.dumps(final, ensure_ascii=False, sort_keys=True) + "\n"
    with _ei._locked_state_sidecar(ledger, lock_type=fcntl.LOCK_EX):
        existed = os.path.lexists(ledger)
        with open(ledger, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            _ei._fsync_directory(ledger.parent)


def apply_archive_batch_retention(
    source_path: Path,
    *,
    retention_days: float | None = None,
    min_interval_sec: float | None = None,
    now: float | None = None,
) -> dict:
    """Delete aged archive batches receipt-first within the retention
    window.  Never touches: the live watermark's last_version pair, any
    non-terminal handoff's version pair, the rebase state and its own
    gzip batch, rotation-set plan authorities, or the ledger itself.
    Also self-sweeps this module's stale staging temps in ARCHIVE_DIR
    (R4: the disk-hygiene janitor only reaps RESULTS_DIR temps)."""

    source_path = Path(source_path)
    if now is None:
        now = time.time()
    days = _resolve_float(
        retention_days,
        _ROTATION_ARCHIVE_RETENTION_DAYS_DEFAULT,
        ROTATION_ARCHIVE_RETENTION_DAYS_ENV,
    )
    min_interval = _resolve_float(
        min_interval_sec, _RETENTION_MIN_INTERVAL_SEC_DEFAULT, ""
    )
    if days < 0:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "disabled",
            "freed_bytes": 0,
            "removed": [],
        }
    archive = Path(_ei.ARCHIVE_DIR)
    stem = source_path.stem
    ledger = archive_retention_ledger_path(source_path)
    if not archive.is_dir():
        return {
            "ok": True,
            "status": "skipped",
            "reason": "archive_missing",
            "freed_bytes": 0,
            "removed": [],
        }
    last_deletion = _last_ledger_deletion_ts(ledger)
    if last_deletion is not None and now - last_deletion < min_interval:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "interval",
            "freed_bytes": 0,
            "removed": [],
        }

    protected_versions: set[int] = set()
    try:
        route = _pending_handoff_route()
    except Exception:
        route = None
    if route is None or route.get("status") not in {"none", "pending"}:
        # Unreadable or blocked discovery: the safety window cannot be
        # proven, fail this pass closed.
        return {
            "ok": True,
            "status": "skipped",
            "reason": "window_unreadable",
            "freed_bytes": 0,
            "removed": [],
        }
    if route.get("status") == "pending" and type(
        route.get("version")
    ) is int:
        protected_versions.add(int(route["version"]))
    try:
        watermark = _read_watermark_record_light(source_path)
    except RuntimeError:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "watermark_invalid",
            "freed_bytes": 0,
            "removed": [],
        }
    if (
        watermark.get("kind") == _ar._ROTATION_WATERMARK_KIND
        and type(watermark.get("last_version")) is int
    ):
        protected_versions.add(int(watermark["last_version"]))
    try:
        state = load_rebase_state(source_path)
    except RuntimeError:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "state_unreadable",
            "freed_bytes": 0,
            "removed": [],
        }
    protected_names: set[str] = {
        rebase_state_path(source_path).name,
        ledger.name,
    }
    if state is not None:
        gz_path, receipt_path = gzip_batch_paths(
            source_path, state["rebase_id"]
        )
        protected_names.update({gz_path.name, receipt_path.name})

    plain_pattern = re.compile(rf"^{re.escape(stem)}_v(\d+)\.jsonl$")

    def _aged(path: Path) -> bool:
        return (now - path.stat().st_mtime) > days * 86400.0

    candidates: list[tuple[Path, int | None, str | None]] = []
    for entry in sorted(archive.iterdir()):
        if not entry.is_file() or entry.name in protected_names:
            continue
        if entry.name.endswith(".tmp"):
            # R4: this module's crash-leftover staging temps (hidden or
            # stem-prefixed); never publishable, so no receipt needed.
            if (
                now - entry.stat().st_mtime
            ) > _TMP_SWEEP_AGE_SEC and entry.name.lstrip(".").startswith(
                f"{stem}."
            ):
                entry.unlink(missing_ok=True)
            continue
        match = plain_pattern.match(entry.name)
        if match is not None:
            version = int(match.group(1))
            if version in protected_versions or not _aged(entry):
                continue
            candidates.append((entry, version, None))
            plan = entry.with_name(f"{stem}_v{version}.rotation.json")
            if plan.is_file() and _aged(plan):
                candidates.append((plan, version, None))
            continue
        if entry.name.startswith(f"{stem}.rebase-") and (
            entry.name.endswith(".jsonl.gz")
            or entry.name.endswith(".receipt.json")
        ):
            if not _aged(entry):
                continue
            label = entry.name.split(".rebase-", 1)[1].split(".", 1)[0]
            candidates.append((entry, None, label))

    removed: list[str] = []
    freed = 0
    for path, version, label in candidates:
        size = path.stat().st_size
        digest = _sha256_file(path)
        _append_retention_ledger_line(
            ledger,
            {
                "schema_version": 2,
                "kind": RETENTION_LEDGER_KIND,
                "source": source_path.name,
                "name": path.name,
                "version": version,
                "rebase_id": label,
                "bytes": size,
                "sha256": digest,
                "deleted_at": now,
            },
        )
        path.unlink()
        _ei._fsync_directory(archive)
        removed.append(path.name)
        freed += size
    return {
        "ok": True,
        "status": "applied" if removed else "skipped",
        "freed_bytes": freed,
        "removed": removed,
        "receipt_path": str(ledger),
    }


# ── offline chain verification (R3) ───────────────────────────────────
def verify_rebase_chain(source_path: Path) -> dict:
    """Reprove the within-horizon rebase chain: decompress every gzip
    batch, check BOTH digests + sizes, range continuity from the
    superseded watermark end to ``drop_offset``, the receipt binding,
    and the kept prefix on the live source."""

    source_path = Path(source_path)
    issues: list[str] = []
    try:
        state = load_rebase_state(source_path)
    except RuntimeError as exc:
        return {
            "ok": False,
            "issues": [f"state_unreadable:{exc}"],
            "batches": [],
        }
    if state is None:
        return {
            "ok": False,
            "issues": ["rebase_state_missing"],
            "batches": [],
        }
    if state.get("state") != "completed":
        issues.append("rebase_state_not_completed")
    batches = sorted(
        _validate_gzip_batches(state.get("gzip_batches")),
        key=lambda item: item["range_start"],
    )
    prev_end = int(
        (state.get("superseded_watermark") or {}).get("end_offset") or 0
    )
    gz_path, receipt_path = gzip_batch_paths(
        source_path, state["rebase_id"]
    )
    for batch in batches:
        if batch["range_start"] != prev_end:
            issues.append("gzip_batch_range_discontinuity")
        if not gz_path.is_file():
            issues.append("gzip_batch_missing")
            prev_end = batch["range_end"]
            continue
        if gz_path.stat().st_size != batch["gzip_bytes"]:
            issues.append("gzip_batch_size_mismatch")
        if _sha256_file(gz_path) != batch["gzip_sha256"]:
            issues.append("gzip_batch_digest_mismatch")
        hasher = hashlib.sha256()
        total = 0
        try:
            with gzip.open(gz_path, "rb") as handle:
                while True:
                    chunk = handle.read(_REBASE_STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    total += len(chunk)
        except OSError:
            issues.append("gzip_batch_decompress_failed")
        else:
            if hasher.hexdigest() != batch["plaintext_sha256"]:
                issues.append("gzip_batch_plaintext_digest_mismatch")
            if total != batch["plaintext_bytes"]:
                issues.append("gzip_batch_plaintext_bytes_mismatch")
        prev_end = batch["range_end"]
    if batches and prev_end != int(state["drop_offset"]):
        issues.append("drop_offset_uncovered")
    if batches:
        receipt = _ei._rotation_record(
            receipt_path,
            kind=GZIP_BATCH_KIND,
            keys=_GZIP_BATCH_KEYS,
        )
        first = batches[0]
        if receipt is None:
            issues.append("gzip_receipt_missing")
        elif (
            receipt.get("rebase_id") != state["rebase_id"]
            or receipt.get("range_start") != first["range_start"]
            or receipt.get("range_end") != first["range_end"]
            or receipt.get("plaintext_sha256") != first["plaintext_sha256"]
            or receipt.get("gzip_sha256") != first["gzip_sha256"]
            or receipt.get("plaintext_bytes") != first["plaintext_bytes"]
            or receipt.get("gzip_bytes") != first["gzip_bytes"]
            or receipt.get("previous_watermark_digest")
            != state["superseded_watermark_digest"]
        ):
            issues.append("gzip_receipt_binding_mismatch")
    kept_size = int(state["kept_size"])
    if os.path.lexists(source_path) and (
        os.lstat(source_path).st_size >= kept_size
    ):
        if (
            _sha256_prefix_of_path(source_path, kept_size)
            != state["kept_prefix_sha256"]
        ):
            issues.append("kept_prefix_mismatch")
    else:
        issues.append("kept_prefix_unverifiable")
    return {"ok": not issues, "issues": issues, "batches": batches}
