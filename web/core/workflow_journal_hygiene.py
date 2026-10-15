"""Conservative retention janitor for the shared workflow journal store.

``RESULTS_DIR/workflow/events.sqlite3`` plus ``workflow/artifacts/`` grow
without a bound: every generation appends a worker-journal run
(``generation:<N>:workflow-v<attempt>``) and — for strict generations — a
strict-authority child run (``<worker run>:strict-authority-v3``) into the
same SQLite store, and every Worker envelope/output captures one or more
content-addressed trees under ``artifacts/<sha256>/``.  Nothing ever removes
them, so the two roots reached 150M + 274M on the cloud VM.

The recovery contract treats these journals as authority: a vanished-checkpoint
recovery replays **both** journals of the generation (outer Worker and strict
child) end-to-end — sequence numbers contiguous from 1, every payload digest
matching, the single ``abandoned`` event last, no live effect remaining.  Any
cleanup must therefore delete a generation's journals *whole* and leave every
surviving run fully replayable.

This module deletes a run only when it can **prove** all of:

1. terminal instance status (``abandoned`` / ``completed``);
2. last journal activity older than the retention window (default 30 days);
3. the run id — and, for a worker run, its strict-authority child — is absent
   from every live reference surface:
   - ``pipeline_state*.json`` checkpoints (primary, draft slots, consumer
     slots) via their ``workflow_run_id`` field; every recovery path derives
     its run id from the live checkpoint;
   - non-terminal rows of ``workflow/slice2b_lifecycle.sqlite3`` (a sealed or
     consuming candidate's frozen snapshot embeds the worker run id);
4. the pair is deletable as a whole: a worker run is removed only when its
   strict child is absent or deleted in the same pass, and vice versa;
5. no effect of the run is still live (``requested``/``retry``/``running``)
   and no outbox row is still undispatched;
6. the store's own per-run ``command_lock`` can be acquired without blocking.

Artifacts are reclaimed only for content-addressed roots
(``artifacts/<64-hex>/``) whose reference set — computed from the hex64 tokens
of every surviving run's journal payloads plus the live surfaces above — is
contained in the deleted runs.  A digest referenced by anything that survives
is kept.  Erroneous extra references can only under-delete, never over-delete.

Deliberately NOT treated as live references (documented decisions):

- ``abandoned_versions.jsonl`` receipts embed historical ``workflow_run_id``
  values, but a completed receipt is an immutable record that is never
  re-resolved against the journals on load;
- ``worker_failures.jsonl`` is a diagnostic log, not a recovery surface;
- ``workflow_locks/`` files are keyed by ``sha256(run_id)`` and are left in
  place (zero-byte flock files);
- ``artifacts/.retained_projections``, ``workspaces``, ``.materializations``
  and ``.materialization_receipts`` are not content-addressed roots and are
  never touched here.

Every pass writes a self-digesting receipt under
``workflow/journal_retention_receipts/`` recording what was deleted, what was
kept and why, and the post-deletion dual-journal replay verification.  The
pass is wired into :mod:`disk_hygiene` behind ``POK_WORKFLOW_JOURNAL_HYGIENE_ENABLED``
(default on), a 30-day default window
(``POK_WORKFLOW_JOURNAL_RETENTION_DAYS``) and a minimum pass interval
(``POK_WORKFLOW_JOURNAL_HYGIENE_MIN_INTERVAL_SEC``, default 24h) enforced from
the newest receipt so the 300s janitor loop does not rescan the journal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from workflow_kernel import WorkflowBusy, WorkflowStore, content_digest

log = logging.getLogger("pok.workflow_journal")

TERMINAL_INSTANCE_STATUSES = frozenset({"abandoned", "completed"})
TERMINAL_EFFECT_STATUSES = frozenset({"completed", "exhausted", "abandoned"})
# Mirror of ``strict_authority_workflow.RUN_SUFFIX``.  The literal is
# duplicated on purpose (same policy as the kernel's producer-consumer prefix
# mirror): this janitor must stay importable without pulling the strict
# authority cluster — and its ``claude_agent_sdk`` import — into the
# disk-hygiene lifespan.
STRICT_AUTHORITY_RUN_SUFFIX = "strict-authority-v3"
WORKER_EFFECT_ID_RE = re.compile(r"\bworker:(?P<run_id>.+):cycle-\d+:")
_HEX64_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_ARTIFACT_DIR_RE = re.compile(r"^[0-9a-f]{64}$")

TERMINAL_SLICE2B_STATES = frozenset({"promoted", "rejected"})

DEFAULT_RUN_RETENTION_SEC = 30.0 * 24.0 * 3600.0
DEFAULT_MIN_PASS_INTERVAL_SEC = 86400.0
RECEIPT_SCHEMA_VERSION = 1
RECEIPTS_DIR_NAME = "journal_retention_receipts"


def strict_authority_child_run_id(run_id: str) -> str:
    """Derive the strict-authority child run id for one worker run id."""

    return f"{run_id}:{STRICT_AUTHORITY_RUN_SUFFIX}"


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _hex64_tokens(text: str) -> set[str]:
    return set(_HEX64_RE.findall(text or ""))


def _checkpoint_run_ids(payload: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for key in ("workflow_run_id", "run_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            found.add(value)
            found.add(strict_authority_child_run_id(value))
    return found


def _slice2b_live_references(db_path: Path) -> tuple[set[str], set[str]]:
    """Live run ids and artifact digests pinned by non-terminal candidates."""

    run_ids: set[str] = set()
    digests: set[str] = set()
    if not db_path.is_file() or db_path.is_symlink():
        return run_ids, digests
    try:
        connection = sqlite3.connect(db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT candidate_id, envelope_effect_id, sealed_snapshot_json,
                       promotion_receipt_json, gate_results_json
                FROM slice2b_candidate_lifecycle
                WHERE state NOT IN ('promoted', 'rejected')
                """
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        # A present-but-unreadable lifecycle database could hide live refs;
        # fail closed instead of guessing.
        raise RuntimeError(
            f"slice2b lifecycle database unreadable: {db_path} ({exc})"
        ) from exc
    for row in rows:
        texts: list[str] = []
        for column in (
            "envelope_effect_id",
            "sealed_snapshot_json",
            "promotion_receipt_json",
            "gate_results_json",
        ):
            try:
                value = row[column]
            except (IndexError, KeyError):
                continue
            if isinstance(value, str) and value:
                texts.append(value)
        for text in texts:
            digests.update(_hex64_tokens(text))
            for match in WORKER_EFFECT_ID_RE.finditer(text):
                run_id = str(match.group("run_id"))
                if run_id:
                    run_ids.add(run_id)
                    run_ids.add(strict_authority_child_run_id(run_id))
            # Snapshots embed ``workflow_run_id`` as a plain JSON field; a
            # stray non-journal run id here can only under-delete.
            for key in ("workflow_run_id", "run_id"):
                for match in re.finditer(
                    rf'"{key}"\s*:\s*"([^"{{}}]+)"', text
                ):
                    value = match.group(1).strip()
                    if value:
                        run_ids.add(value)
                        run_ids.add(strict_authority_child_run_id(value))
    return run_ids, digests


def live_workflow_run_references(
    results_dir: str | Path,
) -> tuple[set[str], set[str]]:
    """Collect live run ids and artifact digests from the durable surfaces.

    Sources: every ``pipeline_state*.json`` checkpoint (primary, draft slots,
    consumer slots) and the non-terminal rows of the Slice-2b candidate
    lifecycle database.  An unparsable checkpoint aborts the pass: it could
    be the one file that pins the live generation.
    """

    results_dir = Path(results_dir)
    run_ids: set[str] = set()
    digests: set[str] = set()
    for path in sorted(results_dir.glob("pipeline_state*.json")):
        if path.name.endswith((".lock", ".tmp")):
            continue
        payload = _load_json(path)
        if payload is None:
            if path.exists():
                raise RuntimeError(
                    f"live checkpoint unreadable: {path}"
                )
            continue
        run_ids.update(_checkpoint_run_ids(payload))
        digests.update(_hex64_tokens(json.dumps(payload, default=str)))
    slice2b_runs, slice2b_digests = _slice2b_live_references(
        results_dir / "workflow" / "slice2b_lifecycle.sqlite3"
    )
    run_ids.update(slice2b_runs)
    digests.update(slice2b_digests)
    return run_ids, digests


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _run_last_activity(
    connection: sqlite3.Connection, run_id: str
) -> tuple[float, dict[str, Any]]:
    """Return the run's last-activity timestamp plus a small projection."""

    instance = connection.execute(
        "SELECT status, updated_at, stream_version FROM workflow_instances "
        "WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if instance is None:
        return 0.0, {}
    event_time = connection.execute(
        "SELECT MAX(created_at) AS t, COUNT(*) AS n FROM workflow_events "
        "WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    effect_time = connection.execute(
        "SELECT MAX(updated_at) AS t FROM effects WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    last_event = connection.execute(
        "SELECT event_type FROM workflow_events WHERE run_id = ? "
        "ORDER BY seq DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    activity = float(instance["updated_at"] or 0.0)
    for candidate in (event_time["t"] if event_time else None,
                      effect_time["t"] if effect_time else None):
        try:
            if candidate is not None and float(candidate) > activity:
                activity = float(candidate)
        except (TypeError, ValueError):
            continue
    return activity, {
        "status": str(instance["status"]),
        "stream_version": int(instance["stream_version"] or 0),
        "event_count": int(event_time["n"] or 0) if event_time else 0,
        "last_event_type": (
            str(last_event["event_type"]) if last_event is not None else ""
        ),
    }


def _run_live_effects(connection: sqlite3.Connection, run_id: str) -> list[str]:
    rows = connection.execute(
        "SELECT DISTINCT status FROM effects WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    return [
        str(row["status"])
        for row in rows
        if str(row["status"]) not in TERMINAL_EFFECT_STATUSES
    ]


def _run_pending_outbox(connection: sqlite3.Connection, run_id: str) -> int:
    row = connection.execute(
        """
        SELECT COUNT(*) AS n
        FROM outbox o JOIN effects e ON o.effect_id = e.effect_id
        WHERE e.run_id = ? AND o.dispatched_at IS NULL
        """,
        (run_id,),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def _run_payload_tokens(connection: sqlite3.Connection, run_id: str) -> set[str]:
    tokens: set[str] = set()
    for row in connection.execute(
        "SELECT payload FROM workflow_events WHERE run_id = ?", (run_id,)
    ):
        tokens.update(_hex64_tokens(row["payload"] or ""))
    for row in connection.execute(
        "SELECT input_payload, result_payload FROM effects WHERE run_id = ?",
        (run_id,),
    ):
        tokens.update(_hex64_tokens(row["input_payload"] or ""))
        result = row["result_payload"]
        if isinstance(result, str) and result:
            tokens.update(_hex64_tokens(result))
    return tokens


def plan_workflow_journal_retention(
    workflow_dir: str | Path,
    *,
    results_dir: str | Path | None = None,
    now: float | None = None,
    retention_sec: float = DEFAULT_RUN_RETENTION_SEC,
    live_run_ids: set[str] | None = None,
    live_artifact_digests: set[str] | None = None,
) -> dict[str, Any]:
    """Plan the conservative deletion set. Read-only; deletes nothing."""

    workflow_dir = Path(workflow_dir)
    db_path = workflow_dir / "events.sqlite3"
    now = time.time() if now is None else float(now)
    cutoff = now - float(retention_sec)
    results_dir = (
        Path(results_dir) if results_dir is not None else workflow_dir.parent
    )
    if live_run_ids is None or live_artifact_digests is None:
        ref_runs, ref_digests = live_workflow_run_references(results_dir)
        live_run_ids = ref_runs if live_run_ids is None else set(live_run_ids)
        live_artifact_digests = (
            ref_digests
            if live_artifact_digests is None
            else set(live_artifact_digests)
        )

    plan: dict[str, Any] = {
        "workflow_dir": str(workflow_dir),
        "now": now,
        "retention_sec": float(retention_sec),
        "cutoff": cutoff,
        "delete_runs": [],
        "kept_runs": [],
        "candidate_artifact_digests": [],
        "live_run_ids": sorted(live_run_ids),
        "unreadable": None,
    }
    if not db_path.is_file():
        plan["unreadable"] = "workflow_db_missing"
        return plan

    connection = _connect(db_path)
    try:
        instances: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            "SELECT run_id, status, updated_at, stream_version "
            "FROM workflow_instances"
        ):
            instances[str(row["run_id"])] = {
                "status": str(row["status"]),
                "updated_at": float(row["updated_at"] or 0.0),
                "stream_version": int(row["stream_version"] or 0),
            }

        base_candidates: set[str] = set()
        kept: list[dict[str, Any]] = []
        for run_id, instance in instances.items():
            activity, projection = _run_last_activity(connection, run_id)
            if run_id in live_run_ids:
                kept.append({"run_id": run_id, "reason": "live_reference"})
                continue
            if instance["status"] not in TERMINAL_INSTANCE_STATUSES:
                kept.append({"run_id": run_id, "reason": "non_terminal"})
                continue
            if activity >= cutoff:
                kept.append({"run_id": run_id, "reason": "too_young"})
                continue
            live_effects = _run_live_effects(connection, run_id)
            if live_effects:
                kept.append({
                    "run_id": run_id,
                    "reason": "live_effects",
                    "detail": live_effects,
                })
                continue
            if _run_pending_outbox(connection, run_id) > 0:
                kept.append({"run_id": run_id, "reason": "pending_outbox"})
                continue
            base_candidates.add(run_id)

        # Pair closure: a worker run and its strict child leave together or
        # not at all (the recovery contract replays both journals).
        delete_runs: set[str] = set()
        for run_id in base_candidates:
            if run_id.endswith(f":{STRICT_AUTHORITY_RUN_SUFFIX}"):
                parent = run_id[: -len(STRICT_AUTHORITY_RUN_SUFFIX) - 1]
                if parent in instances and parent not in base_candidates:
                    kept.append({
                        "run_id": run_id,
                        "reason": "pair_not_deletable",
                    })
                    continue
            else:
                child = strict_authority_child_run_id(run_id)
                if child in instances and child not in base_candidates:
                    kept.append({
                        "run_id": run_id,
                        "reason": "pair_not_deletable",
                    })
                    continue
            delete_runs.add(run_id)
        for run_id in delete_runs:
            kept = [item for item in kept if item["run_id"] != run_id]

        artifact_root = workflow_dir / "artifacts"
        existing_digests: set[str] = set()
        if artifact_root.is_dir() and not artifact_root.is_symlink():
            try:
                for name in os.listdir(artifact_root):
                    path = artifact_root / name
                    if (
                        _ARTIFACT_DIR_RE.fullmatch(name)
                        and path.is_dir()
                        and not path.is_symlink()
                    ):
                        existing_digests.add(name)
            except OSError:
                existing_digests.clear()

        candidate_tokens: set[str] = set()
        for run_id in delete_runs:
            candidate_tokens.update(_run_payload_tokens(connection, run_id))
        plan["candidate_artifact_digests"] = sorted(
            candidate_tokens & existing_digests
        )
        plan["delete_runs"] = sorted(delete_runs)
        plan["kept_runs"] = kept
        plan["instance_count"] = len(instances)
    finally:
        connection.close()
    return plan


def _delete_run_rows(db_path: Path, run_id: str, cutoff: float) -> bool:
    """Delete one run's rows in a single transaction after re-checking state.

    Returns ``False`` (deleting nothing) when the instance changed between
    planning and this transaction — conservative under-deletion.
    """

    connection = _connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            instance = connection.execute(
                "SELECT status, updated_at FROM workflow_instances "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if (
                instance is None
                or str(instance["status"]) not in TERMINAL_INSTANCE_STATUSES
            ):
                connection.rollback()
                return False
            activity, _ = _run_last_activity(connection, run_id)
            if activity >= cutoff:
                connection.rollback()
                return False
            if _run_live_effects(connection, run_id):
                connection.rollback()
                return False
            if _run_pending_outbox(connection, run_id) > 0:
                connection.rollback()
                return False
            connection.execute(
                "DELETE FROM inbox WHERE effect_id IN "
                "(SELECT effect_id FROM effects WHERE run_id = ?)",
                (run_id,),
            )
            connection.execute(
                "DELETE FROM outbox WHERE effect_id IN "
                "(SELECT effect_id FROM effects WHERE run_id = ?)",
                (run_id,),
            )
            connection.execute(
                "DELETE FROM effects WHERE run_id = ?", (run_id,)
            )
            connection.execute(
                "DELETE FROM workflow_events WHERE run_id = ?", (run_id,)
            )
            connection.execute(
                "DELETE FROM workflow_instances WHERE run_id = ?", (run_id,)
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, dirnames, filenames in os.walk(path, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [
            name for name in dirnames if not (root_path / name).is_symlink()
        ]
        for name in filenames:
            file_path = root_path / name
            try:
                if file_path.is_symlink():
                    continue
                total += int(file_path.stat().st_size)
            except OSError:
                continue
    return total


def _verify_surviving_journals(db_path: Path) -> dict[str, Any]:
    """Reprove the replay contract for every surviving run.

    Mirrors ``WorkflowStore.events`` (contiguous seq from 1, payload digest
    match, ``stream_version == event count``) plus the dual-journal terminal
    shape: an ``abandoned`` instance has exactly one ``WorkerAbandoned`` /
    ``StrictAuthorityAbandoned`` event and it is the last event (a legacy
    empty tombstone with zero events and ``stream_version == 0`` is allowed).
    """

    result: dict[str, Any] = {
        "ok": True,
        "runs_verified": 0,
        "errors": [],
    }
    connection = _connect(db_path)
    try:
        instances = {
            str(row["run_id"]): (
                str(row["status"]),
                int(row["stream_version"] or 0),
            )
            for row in connection.execute(
                "SELECT run_id, status, stream_version FROM workflow_instances"
            )
        }
        for run_id, (status, stream_version) in sorted(instances.items()):
            rows = connection.execute(
                "SELECT seq, payload, payload_digest, event_type "
                "FROM workflow_events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
            errors: list[str] = []
            for expected_seq, row in enumerate(rows, start=1):
                if int(row["seq"]) != expected_seq:
                    errors.append(
                        f"{run_id}: non-contiguous seq "
                        f"{row['seq']} != {expected_seq}"
                    )
                    break
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    errors.append(f"{run_id}: unparsable payload")
                    break
                if content_digest(payload) != str(row["payload_digest"]):
                    errors.append(f"{run_id}: payload digest mismatch")
                    break
            if len(rows) != stream_version:
                errors.append(
                    f"{run_id}: stream_version {stream_version} != "
                    f"{len(rows)} events"
                )
            if status == "abandoned" and rows:
                terminal = [
                    row
                    for row in rows
                    if str(row["event_type"]) in (
                        "WorkerAbandoned",
                        "StrictAuthorityAbandoned",
                    )
                ]
                if len(terminal) != 1 or int(terminal[0]["seq"]) != len(rows):
                    errors.append(
                        f"{run_id}: abandoned terminal event is not sole-last"
                    )
            if errors:
                result["ok"] = False
                result["errors"].extend(errors)
                continue
            result["runs_verified"] += 1
    finally:
        connection.close()
    return result


def _write_receipt(workflow_dir: Path, body: dict[str, Any]) -> Path:
    receipts_dir = workflow_dir / RECEIPTS_DIR_NAME
    receipts_dir.mkdir(parents=True, exist_ok=True)
    unsigned = {
        key: value for key, value in body.items() if key != "receipt_digest"
    }
    body["receipt_digest"] = content_digest(unsigned)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(body.get("created_at") or time.time()))
    path = receipts_dir / f"{stamp}-{uuid.uuid4().hex[:8]}.json"
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    descriptor = os.open(receipts_dir, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def apply_workflow_journal_retention(
    workflow_dir: str | Path,
    plan: dict[str, Any],
    *,
    results_dir: str | Path | None = None,
    now: float | None = None,
    retention_sec: float | None = None,
    live_run_ids: set[str] | None = None,
    live_artifact_digests: set[str] | None = None,
    vacuum: bool = True,
) -> dict[str, Any]:
    """Execute a retention plan under per-run locks and write the receipt."""

    workflow_dir = Path(workflow_dir)
    db_path = workflow_dir / "events.sqlite3"
    now = time.time() if now is None else float(now)
    retention = (
        float(plan.get("retention_sec") or DEFAULT_RUN_RETENTION_SEC)
        if retention_sec is None
        else float(retention_sec)
    )
    cutoff = now - retention
    results_dir = (
        Path(results_dir) if results_dir is not None else workflow_dir.parent
    )
    if live_run_ids is None or live_artifact_digests is None:
        ref_runs, ref_digests = live_workflow_run_references(results_dir)
        live_run_ids = ref_runs if live_run_ids is None else set(live_run_ids)
        live_artifact_digests = (
            ref_digests
            if live_artifact_digests is None
            else set(live_artifact_digests)
        )
    live_run_ids = set(live_run_ids)

    store = WorkflowStore(db_path)
    planned_runs = [str(run) for run in (plan.get("delete_runs") or [])]
    deleted_runs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = [
        {"run_id": str(item.get("run_id")), "reason": str(item.get("reason"))}
        for item in (plan.get("kept_runs") or [])
    ]
    deleted_run_ids: set[str] = set()
    deleted_tokens: set[str] = set()

    # Strict children are processed before their worker parents so that a
    # parent can require its child to be gone already (plain lexical order
    # would put the shorter parent id first).
    def _order(run_id: str) -> tuple[int, str]:
        return (
            0 if run_id.endswith(f":{STRICT_AUTHORITY_RUN_SUFFIX}") else 1,
            run_id,
        )

    db_bytes_before = (
        db_path.stat().st_size if db_path.is_file() else 0
    )
    for run_id in sorted(planned_runs, key=_order):
        if run_id in live_run_ids:
            skipped.append({"run_id": run_id, "reason": "live_reference"})
            continue
        if not run_id.endswith(f":{STRICT_AUTHORITY_RUN_SUFFIX}"):
            child = strict_authority_child_run_id(run_id)
            if child in planned_runs and child not in deleted_run_ids:
                skipped.append({
                    "run_id": run_id,
                    "reason": "strict_pair_not_deleted",
                })
                continue
        try:
            with store.command_lock(run_id):
                connection = _connect(db_path)
                try:
                    _, projection = _run_last_activity(connection, run_id)
                    tokens = _run_payload_tokens(connection, run_id)
                finally:
                    connection.close()
                if not _delete_run_rows(db_path, run_id, cutoff):
                    skipped.append({
                        "run_id": run_id,
                        "reason": "state_changed_during_apply",
                    })
                    continue
        except WorkflowBusy:
            skipped.append({"run_id": run_id, "reason": "command_lock_busy"})
            continue
        deleted_run_ids.add(run_id)
        deleted_tokens.update(tokens)
        deleted_runs.append({
            "run_id": run_id,
            **(projection or {}),
        })

    # Content-addressed artifact roots: only digests whose entire reference
    # set is inside the deleted runs (survivors and live surfaces protect).
    artifact_root = workflow_dir / "artifacts"
    survivor_tokens: set[str] = set()
    connection = _connect(db_path)
    try:
        for row in connection.execute(
            "SELECT payload FROM workflow_events"
        ):
            survivor_tokens.update(_hex64_tokens(row["payload"] or ""))
        for row in connection.execute(
            "SELECT input_payload, result_payload FROM effects"
        ):
            survivor_tokens.update(_hex64_tokens(row["input_payload"] or ""))
            result = row["result_payload"]
            if isinstance(result, str) and result:
                survivor_tokens.update(_hex64_tokens(result))
    finally:
        connection.close()

    deleted_artifact_digests: list[str] = []
    kept_shared_digests = 0
    artifacts_bytes = 0
    protected = survivor_tokens | set(live_artifact_digests)
    if artifact_root.is_dir() and not artifact_root.is_symlink():
        for name in sorted(os.listdir(artifact_root)):
            if not _ARTIFACT_DIR_RE.fullmatch(name):
                continue
            if name not in deleted_tokens:
                continue
            path = artifact_root / name
            try:
                if not path.is_dir() or path.is_symlink():
                    continue
            except OSError:
                continue
            if name in protected:
                kept_shared_digests += 1
                continue
            artifacts_bytes += _dir_bytes(path)
            shutil.rmtree(path, ignore_errors=True)
            try:
                gone = not path.exists()
            except OSError:
                gone = False
            if gone:
                deleted_artifact_digests.append(name)
            else:
                kept_shared_digests += 1
                artifacts_bytes -= _dir_bytes(path)

    # Crossover projection journals are keyed by sha256(run_id); remove only
    # files attributable to runs deleted here (forward-hash attribution).
    projections_dir = workflow_dir / "crossover_projections"
    removed_projections = 0
    for run_id in deleted_run_ids:
        key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        path = projections_dir / f"{key}.json"
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
                removed_projections += 1
        except OSError:
            continue

    verification = _verify_surviving_journals(db_path)

    vacuum_ok = False
    db_bytes_after = db_path.stat().st_size if db_path.is_file() else 0
    if vacuum and deleted_run_ids:
        try:
            connection = _connect(db_path)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
                vacuum_ok = True
            finally:
                connection.close()
            db_bytes_after = (
                db_path.stat().st_size if db_path.is_file() else 0
            )
        except sqlite3.Error as exc:
            log.warning("workflow journal vacuum failed: %s", exc)

    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": "workflow_journal_retention",
        "created_at": time.time(),
        "evaluated_at": now,
        "retention_sec": retention,
        "cutoff": cutoff,
        "workflow_dir": str(workflow_dir),
        "deleted_runs": deleted_runs,
        "skipped_runs": skipped,
        "deleted_artifact_digests": deleted_artifact_digests,
        "kept_shared_artifact_digests": kept_shared_digests,
        "removed_crossover_projections": removed_projections,
        "artifacts_bytes_freed": artifacts_bytes,
        "db_bytes_before": db_bytes_before,
        "db_bytes_after": db_bytes_after,
        "vacuum": vacuum_ok,
        "post_replay_verified": bool(verification.get("ok")),
        "runs_verified": int(verification.get("runs_verified") or 0),
        "verification_errors": verification.get("errors") or [],
    }
    receipt_path = _write_receipt(workflow_dir, receipt)
    receipt["receipt_path"] = str(receipt_path)
    if not verification.get("ok"):
        raise RuntimeError(
            "workflow journal post-deletion replay verification failed: "
            + "; ".join(verification.get("errors") or [])
        )
    return receipt


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def run_workflow_journal_hygiene(
    results_dir: str | Path,
    *,
    now: float | None = None,
    retention_sec: float | None = None,
    min_interval_sec: float | None = None,
) -> dict[str, Any]:
    """Interval-gated entry point used by the disk-hygiene loop."""

    results_dir = Path(results_dir)
    report: dict[str, Any] = {
        "ok": True,
        "bytes_freed": 0,
        "removed": 0,
        "skipped": None,
    }
    if not _env_flag("POK_WORKFLOW_JOURNAL_HYGIENE_ENABLED", "1"):
        report["skipped"] = "disabled"
        return report
    workflow_dir = results_dir / "workflow"
    db_path = workflow_dir / "events.sqlite3"
    if not db_path.is_file():
        report["skipped"] = "workflow_db_missing"
        return report
    if retention_sec is None:
        retention_sec = (
            _env_float("POK_WORKFLOW_JOURNAL_RETENTION_DAYS", 30.0)
            * 24.0
            * 3600.0
        )
    if min_interval_sec is None:
        min_interval_sec = _env_float(
            "POK_WORKFLOW_JOURNAL_HYGIENE_MIN_INTERVAL_SEC",
            DEFAULT_MIN_PASS_INTERVAL_SEC,
        )
    receipts_dir = workflow_dir / RECEIPTS_DIR_NAME
    if receipts_dir.is_dir():
        try:
            newest = max(
                path.stat().st_mtime
                for path in receipts_dir.iterdir()
                if path.is_file() and path.name.endswith(".json")
            )
        except (OSError, ValueError):
            newest = None
        if newest is not None and (time.time() - newest) < min_interval_sec:
            report["skipped"] = "interval"
            return report
    try:
        plan = plan_workflow_journal_retention(
            workflow_dir,
            results_dir=results_dir,
            now=now,
            retention_sec=float(retention_sec),
        )
        receipt = apply_workflow_journal_retention(
            workflow_dir,
            plan,
            results_dir=results_dir,
            now=now,
            retention_sec=float(retention_sec),
        )
    except Exception as exc:  # fail the step, never the janitor loop
        log.warning("workflow journal hygiene pass failed: %s", exc)
        report["ok"] = False
        report["skipped"] = f"error:{exc}"
        return report
    report["bytes_freed"] = int(receipt.get("artifacts_bytes_freed") or 0)
    report["removed"] = len(receipt.get("deleted_runs") or []) + len(
        receipt.get("deleted_artifact_digests") or []
    )
    report["deleted_runs"] = len(receipt.get("deleted_runs") or [])
    report["deleted_artifact_digests"] = len(
        receipt.get("deleted_artifact_digests") or []
    )
    report["receipt_path"] = receipt.get("receipt_path")
    return report
