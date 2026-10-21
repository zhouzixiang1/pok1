"""Disk-governance LLM steward: bounded whitelist review under disk pressure.

Chain (designed 2026-10-21, review-hardened same day):

    saturator tick (every ~10 min, beside session housekeeping)
      -> observe gate state (pure read side)
      -> >=3 consecutive closed-door cycles, no unexpired pending request,
         >24h since the last request  =>  create-once ``pending_request.json``
         + ``pipeline.disk_governance_review_requested``
    saturator fill loop (EXPLICIT priority, before the modulo job rotation)
      -> zero-tool single-turn ``SATURATOR DISK GOVERNANCE REVIEW`` packet
         (role name starts with ``SATURATOR`` so provider failures neither
         arm the durable availability pause — llm_query.py::
         _PAUSE_PERSISTENCE_EXEMPT_ROLE_PREFIXES — nor fall out of the
         ``SATURATOR`` substring timeout branch of
         ``llm_role_observability._role_timeout_policy``)
      -> validated recommendation sidecar bound to request id + packet digest
    next steward tick
      -> executor: static dispatch table, hard clamp bounds, the janitor's
         own locks, receipt-first execution, bounded events

Hard boundaries (review blocking items, 2026-10-21):

- The role MUST start with ``SATURATOR``. Without the prefix a GLM 1302
  failure of this background role would arm/extend the 8→120s durable pause
  shared by every pipeline role (the 2026-10-18 amplifier), and the role
  would fall into the DEFAULT timeout bucket (committed
  ``POK_LLM_DEFAULT_TOTAL_TIMEOUT=3600``). The dedicated 45s timeout key
  from the original design is NOT implemented: ``env.runtime`` and
  ``llm_role_observability.py`` are outside this track's file boundary, and
  the review itself measured GLM effort=max first-text latency at ~26s, so
  the shared SATURATOR bucket (committed 900/900/600/7200) is the safe
  reuse; cost stays bounded by the 24h request gate (≤1 review/day).
- The whitelist executor clamps every numeric parameter into hard bounds
  (identity keep [5, operator window], journal retention [7, configured
  window] days, candidate keep [10, library default]) — the underlying
  libraries clamp to max(1, keep) or nothing at all, which an untrusted LLM
  recommendation must never reach. Dispatch is a static table; ``getattr``
  is forbidden. The identity tighten reuses the exact janitor step locks
  (``evaluation_cycle_lock(exclusive=True)`` + ``_manifest_lock``,
  disk_hygiene.py:654-660) — never a bare library call.
- Out-of-bounds / forged / replayed recommendations are discarded WHOLE with
  a bounded event (class + request id + action name only — never the raw LLM
  text) and no LLM re-dispatch from the discard path. Attempts are capped:
  provider-class failures (429/quota/rate/unavailable) do not consume the
  budget; three charged attempts abandon the request.
- Nothing here ever writes ``saturator/findings.jsonl`` or ``match_analysis``
  (llm_saturator.py:516-534 write face, generation_scheduler.py:851-866 the
  only consumer) — governance output is process metadata, not strategy
  evidence.

Honest scope note: ``disk_hygiene.py`` (the janitor) is owned by another
track and is not modified here, and its per-cycle report is not persisted
anywhere (run_disk_hygiene_loop only logs + emits an event). The steward
therefore re-derives the closed-door predicate read-only from the same
signals: the newest ``pipeline.disk_hygiene_done`` event row in
``events.jsonl`` (free bytes + bytes freed for the attributed cycle — the
event fires on every pressure-tier cycle because ``pressure=True`` there)
plus the identical lock-free window pre-checks the janitor steps use.
Anything not provably a no-op fails toward NOT requesting a review.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("pok.disk_governance")

SCHEMA_VERSION = 1
GOVERNANCE_DIR_NAME = "disk_governance"
PENDING_REQUEST_NAME = "pending_request.json"
OBSERVATIONS_NAME = "observations.jsonl"
RECEIPTS_DIR_NAME = "receipts"

EVENT_REVIEW_REQUESTED = "pipeline.disk_governance_review_requested"
EVENT_RECOMMENDATION_RECORDED = "pipeline.disk_governance_recommendation_recorded"
EVENT_RECOMMENDATION_REJECTED = "pipeline.disk_governance_recommendation_rejected"
EVENT_RECOMMENDATION_EXECUTED = "pipeline.disk_governance_recommendation_executed"
EVENT_REVIEW_ABANDONED = "pipeline.disk_governance_review_abandoned"

#: Zero-tool, single-turn review role. The ``SATURATOR`` prefix is
#: load-bearing (see module docstring) — do not rename without re-reading
#: llm_query._PAUSE_PERSISTENCE_EXEMPT_ROLE_PREFIXES and
#: llm_role_observability._role_timeout_policy's substring branch. Equally
#: load-bearing: the label must NOT contain ``REVIEW``/``CRITIC``/``WORKER``
#: — those substring branches sit EARLIER in _role_timeout_policy's elif
#: chain than ``SATURATOR`` (llm_role_observability.py:220-233) and would
#: pull the role out of the SATURATOR timeout bucket.
ROLE_NAME = "SATURATOR DISK GOVERNANCE"

MIB = 1024 * 1024
GIB = 1024 * 1024 * 1024

#: Closed-door bytes threshold: a janitor cycle that freed >= this much is
#: making real progress and does not count as a closed door.
CLOSED_DOOR_MAX_BYTES_FREED = 1 * MIB

#: The janitor's default min-free floor (disk_hygiene.DEFAULT_MIN_FREE_BYTES,
#: 4 GiB; env ``POK_DISK_MIN_FREE_GB``, disk_hygiene.py:1400-1403).
DEFAULT_MIN_FREE_GB = 4.0

#: Operator identity-window knob — the exact env the janitor call site reads
#: (disk_hygiene.IDENTITY_KEEP_ENV, :170; committed value 4,
#: deploy/tencent-cloud/env.runtime:536). The library constant
#: KEEP_RECENT_SNAPSHOTS=20 is only the unset/unparsable FALLBACK, never the
#: ceiling itself (terminal-review B2, 2026-10-21).
IDENTITY_KEEP_ENV = "POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS"
#: Critical-tier cap the janitor applies on top of the env window
#: (disk_hygiene.IDENTITY_KEEP_CRITICAL_CAP, :171; step :925-931).
IDENTITY_KEEP_CRITICAL_CAP = 4
#: Hard clamp floors (review blocking item #2): the libraries clamp to
#: ``max(1, keep)`` (evaluation_identity_retention.py:209) or nothing
#: (workflow_journal_hygiene retention_sec), so the steward owns the floors.
IDENTITY_KEEP_MIN = 5
JOURNAL_DAYS_MIN = 7
CANDIDATE_KEEP_MIN = 10

#: Whitelist: the ONLY actions the executor can ever run. Anything outside
#: this enum is discarded whole (event, no LLM retry). The parameter spec is
#: static — dispatch uses the module-level table below, never ``getattr``.
_ACTION_PARAM_SPECS: dict[str, dict[str, Any]] = {
    "tighten_identity_window": {"param": "keep", "min": IDENTITY_KEEP_MIN},
    "tighten_journal_retention": {"param": "days", "min": JOURNAL_DAYS_MIN},
    "tighten_candidate_keep": {"param": "keep", "min": CANDIDATE_KEEP_MIN},
    "hold": {"param": None},
}
_ALLOWED_OUTPUT_KEYS = frozenset(
    {"request_id", "packet_digest", "action", "keep", "days", "reason"}
)

PACKET_MAX_BYTES = 16 * 1024
MAX_OBSERVATION_ROWS = 200
DIR_KEEP_FILES = 30
_RECOMMENDATION_MAX_REASON_CHARS = 400

TERMINAL_STATES = frozenset({"consumed", "abandoned"})
LIVE_STATES = frozenset({"pending", "dispatching", "awaiting_execution"})


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _observe_interval_sec() -> float:
    return max(30.0, _env_float("POK_DISK_GOVERNANCE_OBSERVE_INTERVAL_SEC", 300.0))


def _request_interval_sec() -> float:
    return max(60.0, _env_float("POK_DISK_GOVERNANCE_REQUEST_INTERVAL_SEC", 86400.0))


def _request_ttl_sec() -> float:
    return max(60.0, _env_float("POK_DISK_GOVERNANCE_REQUEST_TTL_SEC", 86400.0))


def _max_charged_attempts() -> int:
    return max(1, _env_int("POK_DISK_GOVERNANCE_MAX_CHARGED_ATTEMPTS", 3))


def _closed_door_min() -> int:
    return max(1, _env_int("POK_DISK_GOVERNANCE_CLOSED_DOOR_MIN", 3))


def _min_free_bytes() -> int:
    """The janitor's min-free floor, same formula (disk_hygiene.py:1400-1403)."""
    return int(max(0.5, _env_float("POK_DISK_MIN_FREE_GB", DEFAULT_MIN_FREE_GB)) * GIB)


def governance_dir(results_dir: str | Path) -> Path:
    return Path(results_dir) / GOVERNANCE_DIR_NAME


def pending_request_path(results_dir: str | Path) -> Path:
    return governance_dir(results_dir) / PENDING_REQUEST_NAME


def _recommendation_path(results_dir: str | Path, request_id: str) -> Path:
    return governance_dir(results_dir) / f"recommendation-{request_id}.json"


def _receipt_path(results_dir: str | Path, request_id: str) -> Path:
    return governance_dir(results_dir) / RECEIPTS_DIR_NAME / f"{request_id}.json"


def _emit_steward_event(event_type: str, severity: str, message: str, data: dict) -> None:
    try:
        from system_log import log_system_event

        log_system_event(event_type, severity, message, data)
    except Exception as exc:  # never break the caller for an event
        log.warning("disk governance event %s emission failed: %s", event_type, exc)


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _locked_request_file(results_dir: str | Path):
    """Sidecar-exclusive lock over the pending-request state file.

    Every request-state mutation (create, claim, outcome, record, consume)
    happens inside this single lock family, so the consume-time revalidation
    (unconsumed ∧ digest match ∧ no receipt) and the execution are atomic
    against every other observer (review TOCTOU requirement).
    """
    from evolution_infra import _locked_state_sidecar

    path = pending_request_path(results_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    return _locked_state_sidecar(path, lock_type=fcntl.LOCK_EX)


def _load_request(results_dir: str | Path) -> dict[str, Any] | None:
    path = pending_request_path(results_dir)
    try:
        if not path.is_file() or path.is_symlink():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or "request_id" not in payload:
        return None
    return payload


def _save_request(results_dir: str | Path, payload: dict[str, Any]) -> None:
    _write_json_atomic(pending_request_path(results_dir), payload)


# ---------------------------------------------------------------------------
# Read-side gate-state observation (pure; never mutates janitor behavior)
# ---------------------------------------------------------------------------


def classify_pressure_tier(free_bytes: int | None, min_free_bytes: int) -> str | None:
    """ok / tight / critical classification of one free-space sample.

    ok = free >= floor; tight = floor/2 <= free < floor (the janitor's
    ``pressure`` boolean, disk_hygiene.py:1278); critical = below floor/2.
    ``None`` (unreadable) never counts as pressure.
    """
    if free_bytes is None:
        return None
    free = int(free_bytes)
    floor = int(min_free_bytes)
    if free >= floor:
        return "ok"
    if free >= floor // 2:
        return "tight"
    return "critical"


def _identity_keep_window(*, tier: str | None = None) -> int:
    """The operator's identity window, read exactly like the janitor.

    ``POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS`` (committed 4): unset or
    unparsable → library default 20, parsed values clamp to >= 1 — identical
    to ``disk_hygiene._identity_keep_snapshots`` (:683-701). The critical
    disk tier additionally caps the effective keep at
    ``IDENTITY_KEEP_CRITICAL_CAP`` (4), mirroring the janitor step's
    ``identity_keep_cap`` (:838-840, :925-931). Never the bare library
    constant (terminal-review B2, 2026-10-21: the committed window is 4, not
    20 — reading the library constant both mis-advertises the packet range
    and mis-derives the closed-door no-op window).
    """
    try:
        from evaluation_identity_retention import KEEP_RECENT_SNAPSHOTS

        default = max(1, int(KEEP_RECENT_SNAPSHOTS))
    except Exception:
        default = 20
    raw = os.environ.get(IDENTITY_KEEP_ENV)
    keep = default
    if raw is not None and str(raw).strip():
        try:
            keep = max(1, int(str(raw).strip()))
        except (TypeError, ValueError):
            keep = default
    if tier == "critical":
        keep = min(keep, IDENTITY_KEEP_CRITICAL_CAP)
    return keep


def _identity_clamp_ceiling(*, tier: str | None = None) -> int:
    """Executor ceiling for tighten_identity_window: the operator window.

    The operator env window (with the critical-tier cap) is the ceiling;
    the steward floor is IDENTITY_KEEP_MIN (5). When the operator window
    already sits BELOW the floor — the committed env=4 case, or any
    critical tier (cap 4) — the ceiling collapses onto the floor, so every
    executable value is >= the operator's own window: strictly looser,
    never staging more snapshots than the janitor itself would
    (under-retiring only; terminal-review B2 direction analysis).
    """
    return max(IDENTITY_KEEP_MIN, _identity_keep_window(tier=tier))


def _identity_window_state(
    results_dir: Path, *, tier: str | None = None
) -> dict[str, Any]:
    """Lock-free pre-check mirroring the janitor step (disk_hygiene:925-949).

    Counts every non-symlink subdirectory of the archive exactly like the
    janitor's own pre-check (no name filtering there) and compares against
    the SAME env-derived window (plus critical cap) — so the steward's
    no-op judgement and the janitor's skip decision cannot disagree.
    """
    keep = _identity_keep_window(tier=tier)
    archive = results_dir / "archive" / "evaluation_identity"
    if not archive.is_dir():
        return {"keep": keep, "snapshots": 0, "state": "missing"}
    try:
        count = sum(
            1
            for entry in archive.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        )
    except OSError:
        return {"keep": keep, "snapshots": 0, "state": "missing"}
    return {
        "keep": keep,
        "snapshots": count,
        "state": "within_budget" if count <= keep else "over_budget",
    }


def _journal_interval_sec() -> float:
    # workflow_journal_hygiene.DEFAULT_MIN_PASS_INTERVAL_SEC = 86400 (:96).
    return max(0.0, _env_float("POK_WORKFLOW_JOURNAL_HYGIENE_MIN_INTERVAL_SEC", 86400.0))


def _journal_window_state(results_dir: Path, now: float) -> dict[str, Any]:
    """Read-only replication of the journal step's interval gate (:884-901)."""
    retention_days = _env_float("POK_WORKFLOW_JOURNAL_RETENTION_DAYS", 30.0)
    if not _env_flag("POK_WORKFLOW_JOURNAL_HYGIENE_ENABLED", "1"):
        state = "disabled"
    elif not (results_dir / "workflow" / "events.sqlite3").is_file():
        state = "workflow_db_missing"
    else:
        receipts = results_dir / "workflow" / "journal_retention_receipts"
        newest = _newest_receipt_mtime(receipts)
        if newest is not None and (now - newest) < _journal_interval_sec():
            state = "interval"
        else:
            # A due pass MAY free bytes: not provably a no-op (the plan phase
            # belongs to the janitor; the steward never runs it here).
            state = "due"
    return {
        "retention_days": round(retention_days, 3),
        "interval_sec": round(_journal_interval_sec(), 1),
        "state": state,
    }


def _cost_ledger_window_state(results_dir: Path, now: float) -> dict[str, Any]:
    """Read-only skip-state of the cost-ledger compaction (disk_hygiene:1067-1103).

    ``nothing_to_compact`` requires parsing aged rows (bounded at 64 MiB so a
    pathological ledger reads as ``unprovable`` — fail toward no request).
    """
    retention_sec = _env_float("POK_COST_LEDGER_RETENTION_DAYS", 30.0) * 86400.0
    state: str | None = None
    if not _env_flag("POK_COST_LEDGER_COMPACTION_ENABLED", "1"):
        state = "disabled"
    else:
        ledger = results_dir / "generation_cost_ledger.jsonl"
        try:
            present = ledger.is_file() and not ledger.is_symlink()
        except OSError:
            present = False
        if not present:
            state = "ledger_missing"
        else:
            try:
                size = ledger.stat().st_size
            except OSError:
                size = -1
            if size < 0:
                state = "ledger_missing"
            else:
                receipts = results_dir / "cost_ledger_compaction_receipts"
                newest = _newest_receipt_mtime(receipts)
                min_interval = max(
                    0.0,
                    _env_float(
                        "POK_COST_LEDGER_COMPACTION_MIN_INTERVAL_SEC",
                        6.0 * 3600.0,
                    ),
                )
                if newest is not None and (now - newest) < min_interval:
                    state = "interval"
                elif size == 0:
                    state = "ledger_empty"
                elif size > 64 * MIB:
                    state = "unprovable"
                else:
                    state = (
                        "nothing_to_compact"
                        if not _ledger_has_aged_rows(ledger, now - retention_sec)
                        else "due"
                    )
    return {"state": state}


def _ledger_has_aged_rows(ledger: Path, cutoff: float) -> bool:
    try:
        raw = ledger.read_bytes()
    except OSError:
        return True  # unreadable: assume work may exist (conservative)
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        try:
            ts = float(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            return True
    return False


def _candidate_ledger_state(results_dir: Path) -> dict[str, Any]:
    try:
        from candidate_store import DEFAULT_KEEP_VERSIONS
    except Exception:
        DEFAULT_KEEP_VERSIONS = 50
    raw = os.environ.get("POK_CANDIDATE_LEDGER_KEEP_VERSIONS")
    effective = int(DEFAULT_KEEP_VERSIONS)
    if raw is not None and str(raw).strip():
        try:
            effective = max(1, int(str(raw).strip()))
        except (TypeError, ValueError):
            pass
    present = (results_dir / "candidates.jsonl").is_file()
    return {"keep_versions": effective, "state": "present" if present else "absent"}


def _newest_receipt_mtime(receipts_dir: Path) -> float | None:
    try:
        stamps = [
            path.stat().st_mtime
            for path in receipts_dir.iterdir()
            if path.is_file() and not path.is_symlink() and path.name.endswith(".json")
        ]
    except (OSError, ValueError):
        return None
    return max(stamps) if stamps else None


def _latest_hygiene_event(results_dir: Path, *, tail_bytes: int = 256 * 1024) -> dict | None:
    """Newest ``pipeline.disk_hygiene_done`` row from the events tail.

    The janitor emits this row on every pressure-tier cycle (its condition is
    ``freed or pressure`` — disk_hygiene.py:1429 — and tight/critical imply
    pressure), carrying ``free_bytes`` / ``bytes_freed`` for the cycle.
    """
    path = results_dir / "events.jsonl"
    try:
        if not path.is_file() or path.is_symlink():
            return None
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
            raw = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    newest = None
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(row, dict) or row.get("type") != "pipeline.disk_hygiene_done":
            continue
        data = row.get("data")
        if isinstance(data, dict):
            row = dict(row)
            row["_free_bytes"] = _int_or_none(data.get("free_bytes"))
            row["_bytes_freed"] = _int_or_none(data.get("bytes_freed")) or 0
        if newest is None:
            newest = row
            break
    return newest


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def observe_gate_state(results_dir: str | Path, *, now: float | None = None) -> dict[str, Any]:
    """One read-side snapshot of the disk-governance gate state."""
    results_dir = Path(results_dir)
    now = time.time() if now is None else float(now)
    min_free = _min_free_bytes()
    event = _latest_hygiene_event(results_dir)
    tier: str | None = None
    free_bytes: int | None = None
    bytes_freed: int | None = None
    event_age: float | None = None
    if event is not None:
        try:
            event_age = now - float(event.get("ts"))
        except (TypeError, ValueError):
            event_age = None
        if event_age is not None and event_age <= 2.0 * _observe_interval_sec():
            free_bytes = event.get("_free_bytes")
            bytes_freed = event.get("_bytes_freed")
            tier = classify_pressure_tier(free_bytes, min_free)
            # bytes_freed is the attributed cycle's freed total; None here
            # means the row carried no usable number -> unprovable.
            if bytes_freed is None:
                bytes_freed = -1  # sentinel: unknown, cannot prove < threshold
    windows = {
        "evaluation_identity": _identity_window_state(results_dir, tier=tier),
        "workflow_journal": _journal_window_state(results_dir, now),
        "candidate_ledger": _candidate_ledger_state(results_dir),
        "cost_ledger": _cost_ledger_window_state(results_dir, now),
    }
    closed = (
        tier in {"tight", "critical"}
        and bytes_freed is not None
        and 0 <= bytes_freed < CLOSED_DOOR_MAX_BYTES_FREED
        and windows["evaluation_identity"]["state"] in {"missing", "within_budget"}
        and windows["workflow_journal"]["state"]
        in {"disabled", "workflow_db_missing", "interval"}
        and windows["cost_ledger"]["state"]
        in {"disabled", "ledger_missing", "ledger_empty", "interval", "nothing_to_compact"}
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ts": now,
        "tier": tier,
        "free_bytes": free_bytes,
        "min_free_bytes": min_free,
        "bytes_freed": bytes_freed,
        "hygiene_event_age_sec": None if event_age is None else round(event_age, 1),
        "windows": windows,
        "closed": bool(closed),
    }


# ---------------------------------------------------------------------------
# observation ledger
# ---------------------------------------------------------------------------


def _observations_path(results_dir: str | Path) -> Path:
    return governance_dir(results_dir) / OBSERVATIONS_NAME


def _read_observations(results_dir: str | Path) -> list[dict]:
    path = _observations_path(results_dir)
    try:
        if not path.is_file() or path.is_symlink():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows[-MAX_OBSERVATION_ROWS:]
    except OSError:
        return []


def append_observation(results_dir: str | Path, observation: dict) -> None:
    path = _observations_path(results_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(observation)
    row["schema_version"] = SCHEMA_VERSION
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _bound_observations(path)


def _bound_observations(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_OBSERVATION_ROWS:
        return
    kept = lines[-MAX_OBSERVATION_ROWS:]
    tmp = path.with_name(path.name + ".trim.tmp")
    try:
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def count_consecutive_closed_doors(results_dir: str | Path, *, now: float | None = None) -> int:
    """Trailing run of closed-door observations (gaps break the run)."""
    now = time.time() if now is None else float(now)
    rows = _read_observations(results_dir)
    run = 0
    previous_ts: float | None = None
    for row in reversed(rows):
        if not row.get("closed"):
            break
        try:
            ts = float(row.get("ts"))
        except (TypeError, ValueError):
            break
        if previous_ts is not None and (previous_ts - ts) > 4.0 * _observe_interval_sec():
            break  # stale gap: not consecutive cycles
        if now - ts > 4.0 * _request_interval_sec():
            break  # older than any meaningful window
        run += 1
        previous_ts = ts
    return run


# ---------------------------------------------------------------------------
# packet + prompt (system-rendered; the model only echoes and chooses)
# ---------------------------------------------------------------------------


def _measured_bytes(results_dir: Path) -> dict[str, Any]:
    def _size(name: str) -> int | None:
        try:
            path = results_dir / name
            return path.stat().st_size if path.is_file() and not path.is_symlink() else None
        except OSError:
            return None

    return {
        "events_jsonl": _size("events.jsonl"),
        "llm_call_metrics_jsonl": _size("llm_call_metrics.jsonl"),
        "generation_cost_ledger_jsonl": _size("generation_cost_ledger.jsonl"),
    }


def build_packet(
    results_dir: str | Path,
    observation: dict[str, Any],
    *,
    request_id: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Render the bounded review packet (≤ PACKET_MAX_BYTES, enforced)."""
    now = time.time() if now is None else float(now)
    results_dir = Path(results_dir)
    windows = dict(observation.get("windows") or {})
    packet: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "observed_at": round(now, 3),
        "tier": observation.get("tier"),
        "free_bytes": observation.get("free_bytes"),
        "min_free_bytes": observation.get("min_free_bytes"),
        "consecutive_closed_door_cycles": count_consecutive_closed_doors(results_dir),
        "windows": windows,
        "measured_bytes": _measured_bytes(results_dir),
        "whitelist": [
            {
                "action": "tighten_identity_window",
                "param": "keep",
                "range": [
                    IDENTITY_KEEP_MIN,
                    _identity_clamp_ceiling(tier=observation.get("tier")),
                ],
                "meaning": "retire evaluation-identity snapshots beyond the keep window (same-FS rename; bytes freed only after operator purge); the ceiling is the operator window, the floor is the hard safety floor",
            },
            {
                "action": "tighten_journal_retention",
                "param": "days",
                "range": [JOURNAL_DAYS_MIN, int(max(1.0, _env_float("POK_WORKFLOW_JOURNAL_RETENTION_DAYS", 30.0)))],
                "meaning": "tighten workflow-journal retention (subject to the library's own interval gate)",
            },
            {
                "action": "tighten_candidate_keep",
                "param": "keep",
                "range": [CANDIDATE_KEEP_MIN, _candidate_ledger_state(results_dir)["keep_versions"]],
                "meaning": "compact the candidates ledger to the keep window",
            },
            {"action": "hold", "param": None, "meaning": "no safe executable action; escalate with reasoning"},
        ],
    }
    raw = json.dumps(packet, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(raw) > PACKET_MAX_BYTES:
        packet.pop("measured_bytes", None)
        raw = json.dumps(packet, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(raw) > PACKET_MAX_BYTES:  # pragma: no cover - static whitelist is small
        packet["windows"] = {}
        raw = json.dumps(packet, sort_keys=True, ensure_ascii=False).encode("utf-8")
    if len(raw) > PACKET_MAX_BYTES:  # pragma: no cover - defense in depth
        raise ValueError("disk governance packet exceeds bound")
    packet["packet_digest"] = _digest(
        {key: value for key, value in packet.items() if key != "packet_digest"}
    )
    return packet


def build_review_prompt(packet: dict[str, Any]) -> str:
    """Zero-tool, single-turn review prompt rendered from the packet only."""
    packet_json = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=1)
    return (
        "You are the disk-governance reviewer for a cloud evolution runtime.\n"
        "The disk-hygiene janitor has run with every bounded-retention window\n"
        "at a provable no-op for several consecutive cycles while the volume\n"
        "stays in the tight/critical pressure tier. Decide ONE whitelisted\n"
        "action (or hold). You have no tools; answer in one message.\n\n"
        "OBSERVATION PACKET (system-rendered, authoritative):\n"
        f"{packet_json}\n\n"
        "Contract:\n"
        "- Choose exactly one action from the packet's whitelist; values are\n"
        "  integers clamped to the listed range by the executor.\n"
        "- hold is correct when no whitelisted action would free meaningful\n"
        "  bytes (e.g. identity window already within budget) — say why.\n"
        "- Output a single JSON object and nothing else, with fields:\n"
        '  {"request_id": "<echo the packet request_id>",\n'
        '   "packet_digest": "<echo the packet packet_digest>",\n'
        '   "action": "<whitelist action>",\n'
        '   "<param>": <integer>  (only for actions with a param),\n'
        '   "reason": "<= 240 chars"}\n'
        "- Any other key, any action outside the whitelist, or a mismatched\n"
        "  echo causes the whole recommendation to be discarded without\n"
        "  retry. Never propose deleting authority ledgers (events.jsonl,\n"
        "  ratings, checkpoints) — that is not on the whitelist.\n"
    )


# ---------------------------------------------------------------------------
# request lifecycle
# ---------------------------------------------------------------------------


def _last_request_created_at(results_dir: Path) -> float | None:
    """Newest evidence of a prior request (its created_at / receipt mtimes)."""
    stamps: list[float] = []
    live = _load_request(results_dir)
    if live is not None:
        try:
            stamps.append(float(live.get("created_at")))
        except (TypeError, ValueError):
            pass
    receipts = results_dir / GOVERNANCE_DIR_NAME / RECEIPTS_DIR_NAME
    try:
        for path in receipts.iterdir():
            if not path.name.endswith(".json") or path.is_symlink():
                continue
            try:
                stamps.append(path.stat().st_mtime)
            except OSError:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
            if isinstance(payload, dict):
                try:
                    stamps.append(float(payload.get("request_created_at")))
                except (TypeError, ValueError):
                    continue
    except OSError:
        pass
    return max(stamps) if stamps else None


def create_review_request(
    results_dir: str | Path,
    *,
    observation: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    """Build + atomically write one pending request (the ungated internal)."""
    now = time.time() if now is None else float(now)
    results_dir = Path(results_dir)
    request_id = uuid.uuid4().hex
    packet = build_packet(results_dir, observation, request_id=request_id, now=now)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "created_at": now,
        "expires_at": now + _request_ttl_sec(),
        "state": "pending",
        "attempts": 0,
        "charged_attempts": 0,
        "last_attempt_at": None,
        "packet": packet,
        "packet_digest": packet["packet_digest"],
        "consumed_at": None,
        "abandoned_at": None,
        "outcome": None,
    }
    _save_request(results_dir, payload)
    return payload


def maybe_create_review_request(
    results_dir: str | Path, *, now: float | None = None
) -> dict[str, Any] | None:
    """Gate + create-once the bounded review request (all gates must hold)."""
    if not _env_flag("POK_DISK_GOVERNANCE_ENABLED", "1"):
        return None
    now = time.time() if now is None else float(now)
    results_dir = Path(results_dir)
    try:
        with _locked_request_file(results_dir):
            live = _load_request(results_dir)
            if live is not None:
                if live.get("state") in LIVE_STATES:
                    try:
                        expired = float(live.get("expires_at")) <= now
                    except (TypeError, ValueError):
                        expired = True
                    if not expired:
                        return None  # unexpired pending request already exists
                # Terminal or expired: the interval gate below still applies
                # to its creation time before a replacement is minted.
            last_created = _last_request_created_at(results_dir)
            if last_created is not None and (now - last_created) <= _request_interval_sec():
                return None
            closed = count_consecutive_closed_doors(results_dir, now=now)
            if closed < _closed_door_min():
                return None
            # The trigger uses the recorded observation ledger (the same rows
            # the consecutive count came from); the newest row must be a
            # fresh closed door. The live snapshot for this instant was
            # appended by the tick's observation step.
            rows = _read_observations(results_dir)
            latest = rows[-1] if rows else None
            if latest is None or not latest.get("closed"):
                return None
            try:
                latest_age = now - float(latest.get("ts"))
            except (TypeError, ValueError):
                return None
            if latest_age > 2.0 * _observe_interval_sec():
                return None
            observation = dict(latest)
            observation.setdefault("min_free_bytes", _min_free_bytes())
            request = create_review_request(results_dir, observation=observation, now=now)
            _emit_steward_event(
                EVENT_REVIEW_REQUESTED,
                "warn",
                (
                    "Disk governance: janitor closed-door for "
                    f"{closed} consecutive cycles (tier={observation.get('tier')}); "
                    "bounded zero-tool review requested"
                ),
                {
                    "request_id": request["request_id"],
                    "tier": observation.get("tier"),
                    "consecutive_closed_door_cycles": closed,
                    "free_bytes": observation.get("free_bytes"),
                    "packet_digest": request["packet_digest"],
                },
            )
            return request
    except Exception as exc:
        log.warning("disk governance request creation failed: %s", exc)
        return None


def has_dispatchable_request(results_dir: str | Path) -> bool:
    """Lock-free advisory check used by the saturator seam (claim is atomic)."""
    if not _env_flag("POK_DISK_GOVERNANCE_ENABLED", "1"):
        return False
    payload = _load_request(results_dir)
    if payload is None or payload.get("state") != "pending":
        return False
    try:
        if float(payload.get("expires_at")) <= time.time():
            return False
    except (TypeError, ValueError):
        return False
    try:
        return int(payload.get("charged_attempts") or 0) < _max_charged_attempts()
    except (TypeError, ValueError):
        return True


def claim_dispatch_slot(results_dir: str | Path, *, now: float | None = None) -> dict | None:
    """Atomically claim the request for one dispatch attempt."""
    now = time.time() if now is None else float(now)
    try:
        with _locked_request_file(results_dir):
            payload = _load_request(results_dir)
            if payload is None or payload.get("state") != "pending":
                return None
            try:
                if float(payload.get("expires_at")) <= now:
                    return None
            except (TypeError, ValueError):
                return None
            if int(payload.get("charged_attempts") or 0) >= _max_charged_attempts():
                return None
            payload["state"] = "dispatching"
            payload["attempts"] = int(payload.get("attempts") or 0) + 1
            payload["last_attempt_at"] = now
            _save_request(results_dir, payload)
            return {"request": payload}
    except Exception as exc:
        log.warning("disk governance dispatch claim failed: %s", exc)
        return None


def _provider_class_failure(error: object) -> bool:
    """Quota / 429 / rate-limit / unavailable failures are provider-class.

    They must not consume the attempt budget: a request landing inside a GLM
    1308 five-hour window would otherwise burn all three attempts on ~1s
    failures and lose the 24h slot (review revision, 2026-10-21).
    """
    issue = getattr(error, "issue", None)
    if issue is not None:
        return True
    text = str(error or "").lower()
    markers = (
        "quota", "429", "unavailable", "rate limit", "rate_limit",
        "1302", "1308", "overloaded", "too many requests",
    )
    return any(marker in text for marker in markers)


def note_dispatch_outcome(
    results_dir: str | Path,
    request_id: str,
    *,
    error: object,
    now: float | None = None,
    charge: bool | None = None,
) -> dict[str, Any]:
    """Record a failed dispatch; provider-class failures never charge."""
    now = time.time() if now is None else float(now)
    if charge is None:
        charge = not _provider_class_failure(error)
    try:
        with _locked_request_file(results_dir):
            payload = _load_request(results_dir)
            if payload is None or payload.get("request_id") != request_id:
                return {"ok": False, "class": "no_request"}
            if payload.get("state") != "dispatching":
                return {"ok": False, "class": "not_dispatching"}
            if charge:
                payload["charged_attempts"] = int(payload.get("charged_attempts") or 0) + 1
                if int(payload["charged_attempts"]) >= _max_charged_attempts():
                    payload["state"] = "abandoned"
                    payload["abandoned_at"] = now
                    payload["abandon_reason"] = "attempt_cap"
                    _save_request(results_dir, payload)
                    _emit_steward_event(
                        EVENT_REVIEW_ABANDONED,
                        "warn",
                        "Disk governance review abandoned: attempt cap reached",
                        {"request_id": request_id, "reason": "attempt_cap"},
                    )
                    return {"ok": False, "class": "abandoned"}
            payload["state"] = "pending"
            payload["last_error_class"] = (
                "provider" if not charge else "charged_failure"
            )
            _save_request(results_dir, payload)
            return {"ok": True, "class": payload["last_error_class"]}
    except Exception as exc:
        log.warning("disk governance dispatch outcome failed: %s", exc)
        return {"ok": False, "class": "error"}


def _extract_json_object(text: str) -> dict | None:
    blob = str(text or "")
    candidates: list[str] = []
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", blob, re.IGNORECASE)
    if fenced:
        candidates.append(fenced.group(1).strip())
    idx = blob.find("{")
    if idx >= 0:
        candidates.append(blob[idx:])
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            obj, _end = decoder.raw_decode(candidate)
        except Exception:
            continue
        return obj if isinstance(obj, dict) else None
    return None


def _charge_and_requeue(
    results_dir: Path,
    payload: dict[str, Any],
    *,
    reject_class: str,
    action: str,
    now: float,
) -> dict[str, Any]:
    """Discard path: event (bounded, no raw LLM text) + charge + requeue/abandon."""
    request_id = str(payload.get("request_id"))
    payload["charged_attempts"] = int(payload.get("charged_attempts") or 0) + 1
    if int(payload["charged_attempts"]) >= _max_charged_attempts():
        payload["state"] = "abandoned"
        payload["abandoned_at"] = now
        payload["abandon_reason"] = "attempt_cap"
    else:
        payload["state"] = "pending"
    _save_request(results_dir, payload)
    _emit_steward_event(
        EVENT_RECOMMENDATION_REJECTED,
        "warn",
        f"Disk governance recommendation discarded ({reject_class})",
        {
            "request_id": request_id,
            "class": reject_class,
            "action": str(action)[:80],
        },
    )
    if payload["state"] == "abandoned":
        _emit_steward_event(
            EVENT_REVIEW_ABANDONED,
            "warn",
            "Disk governance review abandoned: attempt cap reached",
            {"request_id": request_id, "reason": "attempt_cap"},
        )
    return {"ok": False, "class": reject_class}


def record_review_output(
    results_dir: str | Path,
    request_id: str,
    output_text: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate one zero-tool review output; record or discard whole.

    Discard classes: ``malformed_json`` / ``out_of_bounds`` (enum, parameter
    typing, extra keys) / ``forged_binding_mismatch`` (request id or packet
    digest echo does not match the live request). The discard path never
    re-dispatches the LLM; it charges the attempt and requeues (or abandons
    at the cap).
    """
    now = time.time() if now is None else float(now)
    results_dir = Path(results_dir)
    try:
        with _locked_request_file(results_dir):
            payload = _load_request(results_dir)
            if payload is None or payload.get("request_id") != request_id:
                return {"ok": False, "class": "no_request"}
            if payload.get("state") != "dispatching":
                return {"ok": False, "class": "not_dispatching"}
            raw = _extract_json_object(output_text)
            if raw is None:
                return _charge_and_requeue(
                    results_dir, payload, reject_class="malformed_json",
                    action="", now=now,
                )
            unknown_keys = [key for key in raw if key not in _ALLOWED_OUTPUT_KEYS]
            if unknown_keys:
                return _charge_and_requeue(
                    results_dir, payload, reject_class="out_of_bounds",
                    action=str(raw.get("action"))[:80], now=now,
                )
            if (
                str(raw.get("request_id")) != request_id
                or str(raw.get("packet_digest")) != str(payload.get("packet_digest"))
            ):
                return _charge_and_requeue(
                    results_dir, payload, reject_class="forged_binding_mismatch",
                    action=str(raw.get("action"))[:80], now=now,
                )
            action = str(raw.get("action") or "")
            spec = _ACTION_PARAM_SPECS.get(action)
            if spec is None:
                return _charge_and_requeue(
                    results_dir, payload, reject_class="out_of_bounds",
                    action=action, now=now,
                )
            params: dict[str, int] = {}
            if spec["param"] is not None:
                value = raw.get(spec["param"])
                if isinstance(value, bool) or not isinstance(value, int):
                    return _charge_and_requeue(
                        results_dir, payload, reject_class="out_of_bounds",
                        action=action, now=now,
                    )
                params[spec["param"]] = int(value)
            recommendation = {
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "packet_digest": payload["packet_digest"],
                "action": action,
                "params": params,
                "reason": str(raw.get("reason") or "")[:_RECOMMENDATION_MAX_REASON_CHARS],
                "recorded_at": now,
            }
            recommendation["recommendation_digest"] = _digest(
                {k: v for k, v in recommendation.items() if k != "recommendation_digest"}
            )
            sidecar = _recommendation_path(results_dir, request_id)
            if sidecar.exists() or sidecar.is_symlink():
                # A sidecar already exists for a dispatching request: the
                # earlier bytes were never system-recorded for this attempt.
                # Discard both (event, no execution) and requeue.
                try:
                    sidecar.unlink()
                except OSError:
                    pass
                return _charge_and_requeue(
                    results_dir, payload, reject_class="forged_binding_mismatch",
                    action=action, now=now,
                )
            _write_json_atomic(sidecar, recommendation)
            payload["state"] = "awaiting_execution"
            _save_request(results_dir, payload)
            _emit_steward_event(
                EVENT_RECOMMENDATION_RECORDED,
                "info",
                f"Disk governance recommendation recorded (action={action})",
                {"request_id": request_id, "action": action, "params": params},
            )
            return {"ok": True, "action": action, "params": params}
    except Exception as exc:
        log.warning("disk governance review output recording failed: %s", exc)
        return {"ok": False, "class": "error"}


# ---------------------------------------------------------------------------
# executor (whitelist, clamps, janitor locks, receipt-first)
# ---------------------------------------------------------------------------


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _execute_tighten_identity_window(results_dir: Path, keep: int) -> dict[str, Any]:
    """Identity tighten under the janitor step's exact locks (never bare)."""
    from evaluation_bundle import evaluation_cycle_lock
    from evaluation_data_identity import _manifest_lock
    from evaluation_identity_retention import run_evaluation_identity_retention

    with evaluation_cycle_lock(results_dir, exclusive=True), _manifest_lock(results_dir):
        outcome = run_evaluation_identity_retention(
            results_dir, triggered_by="disk_governance_steward", keep=keep
        )
    return {
        "ok": bool(outcome.get("ok")),
        "moved": len(outcome.get("moved") or []),
        "failed": len(outcome.get("failed") or []),
        "receipt_path": outcome.get("receipt_path"),
    }


def _execute_tighten_journal_retention(results_dir: Path, days: int) -> dict[str, Any]:
    from workflow_journal_hygiene import run_workflow_journal_hygiene

    outcome = run_workflow_journal_hygiene(
        results_dir, retention_sec=float(days) * 86400.0
    )
    return {
        "ok": bool(outcome.get("ok")),
        "bytes_freed": int(outcome.get("bytes_freed") or 0),
        "removed": int(outcome.get("removed") or 0),
        "skipped": outcome.get("skipped"),
    }


def _execute_tighten_candidate_keep(results_dir: Path, keep: int) -> dict[str, Any]:
    from candidate_store import compact_candidate_ledger

    ledger = results_dir / "candidates.jsonl"
    if not ledger.is_file():
        return {"ok": True, "skipped": "ledger_missing"}
    outcome = compact_candidate_ledger(ledger, keep_versions=keep)
    return {
        "ok": True,
        "receipt_persisted": bool(outcome.get("receipt_persisted")),
        "kept_versions": outcome.get("kept_versions"),
    }


def _execute_hold(results_dir: Path) -> dict[str, Any]:
    return {"ok": True, "skipped": None}


#: STATIC dispatch table — the only path from a validated action to an
#: executor. No ``getattr``, no dynamic name resolution.
_ACTION_EXECUTORS: dict[str, Callable[..., dict[str, Any]]] = {
    "tighten_identity_window": _execute_tighten_identity_window,
    "tighten_journal_retention": _execute_tighten_journal_retention,
    "tighten_candidate_keep": _execute_tighten_candidate_keep,
    "hold": _execute_hold,
}


def _reject_execution(
    results_dir: Path,
    payload: dict[str, Any] | None,
    *,
    reject_class: str,
    request_id: str,
    action: str = "",
    drop_sidecar: Path | None = None,
) -> dict[str, Any]:
    if drop_sidecar is not None:
        try:
            drop_sidecar.unlink()
        except OSError:
            pass
    if payload is not None and payload.get("state") in LIVE_STATES:
        # A forged/malformed sidecar is not an LLM attempt: requeue without
        # charging (the recorded output path owns charging).
        payload["state"] = "pending"
        _save_request(results_dir, payload)
    _emit_steward_event(
        EVENT_RECOMMENDATION_REJECTED,
        "warn",
        f"Disk governance recommendation rejected ({reject_class})",
        {"request_id": request_id, "class": reject_class, "action": str(action)[:80]},
    )
    return {"ok": False, "class": reject_class}


def consume_and_execute_recommendation(
    results_dir: str | Path, *, now: float | None = None
) -> dict[str, Any]:
    """Consume the awaiting recommendation and execute it exactly once.

    Inside ONE sidecar-exclusive lock: revalidate (state awaiting, sidecar
    binding digests match, no existing receipt) then receipt-first execute
    via the static whitelist table. A replay (consumed request re-presented)
    or any binding mismatch is rejected with an event and never executes.
    """
    now = time.time() if now is None else float(now)
    results_dir = Path(results_dir)
    try:
        with _locked_request_file(results_dir):
            payload = _load_request(results_dir)
            if payload is None:
                return {"ok": False, "class": "no_request"}
            request_id = str(payload.get("request_id"))
            sidecar = _recommendation_path(results_dir, request_id)
            receipt = _receipt_path(results_dir, request_id)
            if payload.get("state") != "awaiting_execution":
                # Replay detection: a terminal request with its sidecar or
                # receipt still present is a re-presentation, never a re-run.
                if payload.get("state") in TERMINAL_STATES and (
                    receipt.exists() or sidecar.exists()
                ):
                    _emit_steward_event(
                        EVENT_RECOMMENDATION_REJECTED,
                        "warn",
                        "Disk governance recommendation replay rejected",
                        {"request_id": request_id, "class": "replay", "action": ""},
                    )
                    return {"ok": False, "class": "replay"}
                return {"ok": False, "class": "no_request"}
            if receipt.exists() or receipt.is_symlink():
                return _reject_execution(
                    results_dir, payload, reject_class="replay",
                    request_id=request_id,
                )
            try:
                if not sidecar.is_file() or sidecar.is_symlink():
                    return _reject_execution(
                        results_dir, payload, reject_class="no_recommendation",
                        request_id=request_id,
                    )
                recommendation = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                return _reject_execution(
                    results_dir, payload, reject_class="malformed_json",
                    request_id=request_id, drop_sidecar=sidecar,
                )
            if not isinstance(recommendation, dict):
                return _reject_execution(
                    results_dir, payload, reject_class="malformed_json",
                    request_id=request_id, drop_sidecar=sidecar,
                )
            expected_digest = _digest(
                {
                    k: v
                    for k, v in recommendation.items()
                    if k != "recommendation_digest"
                }
            )
            if (
                str(recommendation.get("request_id")) != request_id
                or str(recommendation.get("packet_digest"))
                != str(payload.get("packet_digest"))
                or str(recommendation.get("recommendation_digest")) != expected_digest
            ):
                return _reject_execution(
                    results_dir, payload, reject_class="forged_binding_mismatch",
                    request_id=request_id,
                    action=str(recommendation.get("action"))[:80],
                    drop_sidecar=sidecar,
                )
            action = str(recommendation.get("action") or "")
            executor = _ACTION_EXECUTORS.get(action)
            spec = _ACTION_PARAM_SPECS.get(action)
            if executor is None or spec is None:
                return _reject_execution(
                    results_dir, payload, reject_class="out_of_bounds",
                    request_id=request_id, action=action, drop_sidecar=sidecar,
                )
            params = recommendation.get("params")
            if not isinstance(params, dict):
                params = {}

            # Resolve clamp bounds lazily (operator env is the ceiling). The
            # identity ceiling follows the request-bound tier so the packet's
            # advertised range and the executed clamp can never disagree.
            packet_tier = (payload.get("packet") or {}).get("tier")
            identity_max = _identity_clamp_ceiling(tier=packet_tier)
            journal_max = int(max(JOURNAL_DAYS_MIN, _env_float(
                "POK_WORKFLOW_JOURNAL_RETENTION_DAYS", 30.0
            )))
            candidate_max = int(_candidate_ledger_state(results_dir)["keep_versions"])
            requested = {
                "keep": params.get("keep"),
                "days": params.get("days"),
            }
            effective_keep_identity = effective_days = effective_keep_candidate = None
            if action == "tighten_identity_window":
                effective_keep_identity = _clamp(int(params.get("keep")), IDENTITY_KEEP_MIN, identity_max)
            elif action == "tighten_journal_retention":
                effective_days = _clamp(int(params.get("days")), JOURNAL_DAYS_MIN, journal_max)
            elif action == "tighten_candidate_keep":
                effective_keep_candidate = _clamp(int(params.get("keep")), CANDIDATE_KEEP_MIN, max(CANDIDATE_KEEP_MIN, candidate_max))

            receipt_body: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "kind": "disk_governance_execution",
                "created_at": now,
                "request_id": request_id,
                "request_created_at": payload.get("created_at"),
                "packet_digest": payload.get("packet_digest"),
                "recommendation_digest": recommendation.get("recommendation_digest"),
                "action": action,
                "requested": {k: v for k, v in requested.items() if v is not None},
                "requested_keep": requested.get("keep") if action != "tighten_journal_retention" else None,
                "requested_days": requested.get("days") if action == "tighten_journal_retention" else None,
                "effective_keep": (
                    effective_keep_identity
                    if action == "tighten_identity_window"
                    else effective_keep_candidate
                ),
                "effective_days": effective_days,
                "reason": recommendation.get("reason"),
                "clamp_bounds": {
                    "identity_keep": [IDENTITY_KEEP_MIN, identity_max],
                    "journal_days": [JOURNAL_DAYS_MIN, journal_max],
                    "candidate_keep": [CANDIDATE_KEEP_MIN, max(CANDIDATE_KEEP_MIN, candidate_max)],
                },
            }
            # Receipt FIRST (the disk_hygiene orphan-reap posture, :749-766):
            # once a receipt exists this request can never execute again,
            # even across a crash between the receipt and the tighten. The
            # digest covers the claim fields; the execution outcome is added
            # below WITHOUT breaking it.
            receipt_body["receipt_digest"] = _digest(
                {
                    k: v
                    for k, v in receipt_body.items()
                    if k not in {"receipt_digest", "outcome", "executed_at"}
                }
            )
            _write_json_atomic(receipt, receipt_body)

            if action == "tighten_identity_window":
                outcome = executor(results_dir, effective_keep_identity)
            elif action == "tighten_journal_retention":
                outcome = executor(results_dir, effective_days)
            elif action == "tighten_candidate_keep":
                outcome = executor(results_dir, effective_keep_candidate)
            else:
                outcome = executor(results_dir)

            outcome_summary = {
                "ok": bool(outcome.get("ok")),
                "bytes_freed": int(outcome.get("bytes_freed") or 0),
                "removed": int(outcome.get("removed") or 0),
                "moved": int(outcome.get("moved") or 0),
                "skipped": outcome.get("skipped"),
                "receipt_persisted": bool(outcome.get("receipt_persisted")),
            }
            # Attach the outcome to the same receipt (digest covers the claim
            # fields only, so the append keeps the claim verifiable).
            receipt_body["outcome"] = outcome_summary
            receipt_body["executed_at"] = time.time()
            _write_json_atomic(receipt, receipt_body)

            payload["state"] = "consumed"
            payload["consumed_at"] = now
            payload["outcome"] = outcome_summary
            _save_request(results_dir, payload)
            _housekeep_governance_dir(results_dir)
            _emit_steward_event(
                EVENT_RECOMMENDATION_EXECUTED,
                "info" if outcome.get("ok") is not False else "warn",
                f"Disk governance action executed ({action})",
                {
                    "request_id": request_id,
                    "action": action,
                    "effective_keep": receipt_body.get("effective_keep"),
                    "effective_days": effective_days,
                    "outcome": outcome_summary,
                },
            )
            return {
                "ok": True,
                "action": action,
                "request_id": request_id,
                "receipt_path": str(receipt),
                "outcome": payload["outcome"],
            }
    except Exception as exc:
        log.warning("disk governance execution failed: %s", exc)
        return {"ok": False, "class": "error"}


def _housekeep_governance_dir(results_dir: Path, *, keep_files: int = DIR_KEEP_FILES) -> None:
    """Bound the governance footprint (self-occupancy, design risk #7)."""
    root = governance_dir(results_dir)
    try:
        managed = [
            path
            for path in root.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.name not in {PENDING_REQUEST_NAME, OBSERVATIONS_NAME}
        ]
        receipts = root / RECEIPTS_DIR_NAME
        if receipts.is_dir():
            managed.extend(
                path
                for path in receipts.iterdir()
                if path.is_file() and not path.is_symlink()
            )
        managed.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in managed[keep_files:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass


# ---------------------------------------------------------------------------
# steward tick (deterministic half; called from the saturator loop)
# ---------------------------------------------------------------------------

#: A dispatch whose owner vanished (preempted/cancelled saturator task —
#: CancelledError never reaches note_dispatch_outcome) is reaped back to
#: pending after this window (SATURATOR total timeout 7200s + margin).
DISPATCH_STALE_SEC = float(
    os.environ.get("POK_DISK_GOVERNANCE_DISPATCH_STALE_SEC", "7500")
)


def run_steward_tick(results_dir: str | Path, *, now: float | None = None) -> dict[str, Any]:
    """One deterministic steward cycle: reap, observe, maybe request, execute."""
    now = time.time() if now is None else float(now)
    summary: dict[str, Any] = {
        "disabled": not _env_flag("POK_DISK_GOVERNANCE_ENABLED", "1"),
        "observation_appended": False,
        "request_created": False,
        "executed": None,
        "expired_pending": 0,
        "reaped_stale": 0,
    }
    if summary["disabled"]:
        return summary
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    # A. Stale dispatch reaper (lost/preempted dispatch).
    try:
        with _locked_request_file(results_dir):
            payload = _load_request(results_dir)
            if payload is not None and payload.get("state") == "dispatching":
                last = payload.get("last_attempt_at")
                try:
                    stale = last is not None and (now - float(last)) > DISPATCH_STALE_SEC
                except (TypeError, ValueError):
                    stale = False
                if stale:
                    payload["charged_attempts"] = int(payload.get("charged_attempts") or 0) + 1
                    if int(payload["charged_attempts"]) >= _max_charged_attempts():
                        payload["state"] = "abandoned"
                        payload["abandoned_at"] = now
                        payload["abandon_reason"] = "attempt_cap"
                    else:
                        payload["state"] = "pending"
                    _save_request(results_dir, payload)
                    summary["reaped_stale"] = 1
    except Exception as exc:
        log.warning("disk governance stale-dispatch reap failed: %s", exc)

    # B. Expire a dead pending request so the interval gate can admit a new one.
    try:
        with _locked_request_file(results_dir):
            payload = _load_request(results_dir)
            if payload is not None and payload.get("state") in LIVE_STATES:
                try:
                    expired = float(payload.get("expires_at")) <= now
                except (TypeError, ValueError):
                    expired = True
                if expired:
                    payload["state"] = "abandoned"
                    payload["abandoned_at"] = now
                    payload["abandon_reason"] = "expired"
                    _save_request(results_dir, payload)
                    summary["expired_pending"] = 1
                    _emit_steward_event(
                        EVENT_REVIEW_ABANDONED,
                        "warn",
                        "Disk governance review request expired unfulfilled",
                        {"request_id": payload.get("request_id"), "reason": "expired"},
                    )
    except Exception as exc:
        log.warning("disk governance expiry sweep failed: %s", exc)

    # C. Observation cadence (approximates one janitor cycle per sample).
    try:
        rows = _read_observations(results_dir)
        newest_ts = None
        for row in reversed(rows):
            try:
                newest_ts = float(row.get("ts"))
                break
            except (TypeError, ValueError):
                continue
        if newest_ts is None or (now - newest_ts) >= _observe_interval_sec():
            append_observation(results_dir, observe_gate_state(results_dir, now=now))
            summary["observation_appended"] = True
    except Exception as exc:
        log.warning("disk governance observation failed: %s", exc)

    # D. Maybe mint a bounded review request.
    try:
        request = maybe_create_review_request(results_dir, now=now)
        summary["request_created"] = request is not None
    except Exception as exc:
        log.warning("disk governance request gate failed: %s", exc)

    # E. Execute an awaiting recommendation exactly once.
    try:
        summary["executed"] = consume_and_execute_recommendation(results_dir, now=now)
    except Exception as exc:
        log.warning("disk governance execute step failed: %s", exc)

    return summary


# ---------------------------------------------------------------------------
# LLM role: zero-tool runtime contract (prepended so it resolves before the
# generic ^SATURATOR(?:\s|$) saturator contract registered by llm_saturator)
# ---------------------------------------------------------------------------


def _disk_governance_producer(renderer_inputs):
    """Render the governance prompt into a typed LLMRenderedMaterial."""
    from llm_role_dispatch import LLMRenderedMaterial

    return LLMRenderedMaterial(
        text=str(renderer_inputs.get("prompt", "")),
        evidence_kind="none",
        evidence_provenance={},
    )


def register_disk_governance_role() -> None:
    """Append the zero-tool governance role contract at runtime.

    Mirrors ``llm_saturator._register_saturator_role`` (no semantic-file
    edit; this module and llm_saturator.py are outside SEMANTIC_PATHS and
    the evaluation contract). PREPENDED so the more specific
    ``^SATURATOR DISK GOVERNANCE`` pattern wins over the generic saturator
    contract — first match wins in ``resolve_llm_role_contract``.
    """
    import llm_query as _lq

    if getattr(_lq, "_disk_governance_role_registered", False):
        return
    contract = _lq._llm_role_contract(
        "llm_disk_governance",
        r"^SATURATOR DISK GOVERNANCE(?:\s|$)",
        renderer="disk_governance_steward.py::build_review_prompt",
        producer_file="web/core/disk_governance_steward.py",
        producer_name="_disk_governance_producer",
        template_paths=(),
        evidence_kind="none",
        scope_policy="none",
        tools=((),),  # zero-tool: the reviewer only reads the rendered packet
        read_scope="none_provider_filesystem",
        write_scope="none",
        evidence_policy="system_bound_prompt_only",
        history_policy="forbidden",
        requires_read_scope=False,
        allows_context_files=False,
    )
    _lq.ACTIVE_LLM_ROLE_CONTRACTS = (contract,) + _lq.ACTIVE_LLM_ROLE_CONTRACTS
    _lq._disk_governance_role_registered = True


__all__ = [
    "ROLE_NAME",
    "build_packet",
    "build_review_prompt",
    "classify_pressure_tier",
    "claim_dispatch_slot",
    "consume_and_execute_recommendation",
    "count_consecutive_closed_doors",
    "create_review_request",
    "governance_dir",
    "has_dispatchable_request",
    "maybe_create_review_request",
    "note_dispatch_outcome",
    "observe_gate_state",
    "pending_request_path",
    "record_review_output",
    "register_disk_governance_role",
    "run_steward_tick",
    "PACKET_MAX_BYTES",
    "DISPATCH_STALE_SEC",
]
