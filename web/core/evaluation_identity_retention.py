"""Operator-approved retention window for archived evaluation identities.

Every evaluator migration (:func:`evaluation_data_identity.archive_and_initialize`)
freezes one timestamped snapshot below ``results/archive/evaluation_identity/``.
Nothing ever removed them, so the cloud runtime accumulated 83 snapshots
(553 MiB; 26-117 MiB each).  The operator approved keeping the most recent
``KEEP_RECENT_SNAPSHOTS`` (20) snapshots; older ones are *not* deleted — they
move to the retirement staging area ``results/archive/evaluation_identity_retired/``
where an operator can physically purge them after confirming stability.

The pass runs from the migration writer itself, inside the same
``evaluation_cycle_lock(exclusive=True)`` + identity manifest lock the
migration already holds, and is reference-safe before it moves anything: a
snapshot whose archived ``evaluation_data_manifest.json`` digest is still
referenced by a live surface is never moved.  Live surfaces scanned:

- the live ``evaluation_data_manifest.json`` (``manifest_digest``);
- the live ``evaluation_cycle_manifest.json`` (``evaluation_identity_digest``);
- generation evidence snapshots ``v*/evidence_snapshot/manifest.json``
  (``evaluation_identity_digest``);
- live ``pipeline_state*.json`` checkpoints (hex64 token containment — a
  checkpoint that binds an identity digest by any field name protects it).

An unparsable-but-present reference surface fails the pass closed (nothing is
moved); an unreadable snapshot manifest protects that snapshot (cannot prove
it unreferenced).  Both postures can only under-retire, never over-retire.

Every pass writes one self-digesting receipt under
``archive/evaluation_identity_retired/retention_receipts/`` recording the
retired list, the kept window, every protection decision, and why.  The
receipt is the operator's manifest for the later physical purge.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from bot_artifact import canonical_digest

log = logging.getLogger("pok.evaluation_identity")

KEEP_RECENT_SNAPSHOTS = 20
ARCHIVE_DIRNAME = "evaluation_identity"
RETIRED_DIRNAME = "evaluation_identity_retired"
RECEIPTS_DIRNAME = "retention_receipts"
RECEIPT_SCHEMA_VERSION = 1
SNAPSHOT_NAME_RE = re.compile(r"^[0-9]{8}_[0-9]{6}$")
_HEX64_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")


class EvaluationIdentityRetentionError(RuntimeError):
    pass


def archive_dir(results_dir: str | Path) -> Path:
    return Path(results_dir) / "archive" / ARCHIVE_DIRNAME


def retired_dir(results_dir: str | Path) -> Path:
    return Path(results_dir) / "archive" / RETIRED_DIRNAME


def _load_json_object(path: Path) -> dict[str, Any] | None:
    """Load a JSON object; ``None`` only when the path is absent."""

    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise EvaluationIdentityRetentionError(
            f"live reference surface unreadable: {path}"
        )
    if not isinstance(payload, dict):
        raise EvaluationIdentityRetentionError(
            f"live reference surface malformed: {path}"
        )
    return payload


def _harvest_hex64_tokens(path: Path, into: set[str]) -> None:
    """Conservative token containment: unreadable bytes protect nothing less.

    A read failure means the surface could not be inspected; fail closed so
    the pass aborts rather than retiring a possibly-referenced snapshot.
    """

    if not path.is_file() or path.is_symlink():
        return
    try:
        into.update(_HEX64_RE.findall(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError) as exc:
        raise EvaluationIdentityRetentionError(
            f"live reference surface unreadable: {path}"
        ) from exc


def live_evaluation_identity_references(results_dir: str | Path) -> set[str]:
    """Identity digests referenced by live checkpoints / evaluation cycles.

    Raises :class:`EvaluationIdentityRetentionError` when a present reference
    surface cannot be read — an unreadable surface could be the one file that
    pins a still-live identity, so the retention pass must not guess.
    """

    root = Path(results_dir)
    references: set[str] = set()

    identity_manifest = _load_json_object(root / "evaluation_data_manifest.json")
    if identity_manifest is not None:
        digest = str(identity_manifest.get("manifest_digest") or "")
        if digest:
            references.add(digest)

    cycle_manifest = _load_json_object(root / "evaluation_cycle_manifest.json")
    if cycle_manifest is not None:
        digest = str(cycle_manifest.get("evaluation_identity_digest") or "")
        if digest:
            references.add(digest)

    # Frozen generation evidence snapshots bind the identity digest under
    # which their cycle was computed (evidence_snapshot.py manifests).
    if root.is_dir():
        for version_dir in sorted(root.iterdir()):
            if not re.fullmatch(r"v[0-9]+", version_dir.name):
                continue
            manifest_path = (
                version_dir / "evidence_snapshot" / "manifest.json"
            )
            if not manifest_path.exists():
                continue
            snapshot_manifest = _load_json_object(manifest_path)
            if snapshot_manifest is not None:
                digest = str(
                    snapshot_manifest.get("evaluation_identity_digest") or ""
                )
                if digest:
                    references.add(digest)

    # Live checkpoints: bind by containment so a renamed field still protects.
    for path in sorted(root.glob("pipeline_state*.json")):
        if path.name.endswith((".lock", ".tmp")):
            continue
        _harvest_hex64_tokens(path, references)

    return references


def _snapshot_manifest_digest(snapshot: Path) -> str | None:
    path = snapshot / "evaluation_data_manifest.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise EvaluationIdentityRetentionError(
            f"archived snapshot manifest unreadable: {path}"
        )
    if not isinstance(payload, dict):
        raise EvaluationIdentityRetentionError(
            f"archived snapshot manifest malformed: {path}"
        )
    digest = str(payload.get("manifest_digest") or "")
    return digest or None


def _dir_bytes(path: Path) -> int:
    total = 0
    for current, dirnames, filenames in os.walk(path, followlinks=False):
        current_path = Path(current)
        dirnames[:] = [
            name for name in dirnames if not (current_path / name).is_symlink()
        ]
        for name in filenames:
            file_path = current_path / name
            try:
                if file_path.is_symlink():
                    continue
                total += int(file_path.stat().st_size)
            except OSError:
                continue
    return total


def plan_evaluation_identity_retention(
    results_dir: str | Path,
    *,
    keep: int = KEEP_RECENT_SNAPSHOTS,
    live_references: set[str] | None = None,
) -> dict[str, Any]:
    """Plan the retirement set. Read-only; moves nothing.

    The window keeps the newest ``keep`` snapshots by timestamp name
    (``%Y%m%d_%H%M%S`` sorts chronologically).  Older snapshots are retirement
    candidates unless a live reference still binds their archived manifest
    digest — those stay in place even if that leaves more than ``keep``
    snapshots in the archive.
    """

    root = Path(results_dir)
    keep = max(1, int(keep))
    if live_references is None:
        live_references = live_evaluation_identity_references(root)
    live_references = set(live_references)

    plan: dict[str, Any] = {
        "results_dir": str(root),
        "keep": keep,
        "snapshots": [],
        "retire": [],
        "protected": [],
        "ignored_entries": [],
        "live_reference_count": len(live_references),
    }

    archive = archive_dir(root)
    if archive.is_symlink():
        raise EvaluationIdentityRetentionError(
            f"unsafe archive directory: {archive}"
        )
    if not archive.is_dir():
        return plan
    names = sorted(
        name
        for name in (entry.name for entry in archive.iterdir())
        if SNAPSHOT_NAME_RE.fullmatch(name)
    )
    cutoff = names[-keep:] if names else []
    for name in names:
        snapshot = archive / name
        if snapshot.is_symlink() or not snapshot.is_dir():
            plan["ignored_entries"].append(
                {"name": name, "reason": "unsafe_entry"}
            )
            continue
        try:
            digest = _snapshot_manifest_digest(snapshot)
        except EvaluationIdentityRetentionError:
            digest = None
            protected_reason = "snapshot_manifest_unreadable"
        else:
            protected_reason = ""
        entry = {"name": name, "manifest_digest": digest}
        plan["snapshots"].append(entry)
        if name in cutoff:
            continue
        if protected_reason:
            plan["protected"].append({**entry, "reason": protected_reason})
        elif digest is not None and digest in live_references:
            plan["protected"].append({**entry, "reason": "live_reference"})
        else:
            plan["retire"].append(entry)
    return plan


def _write_receipt(receipts_dir: Path, body: dict[str, Any]) -> Path:
    receipts_dir.mkdir(parents=True, exist_ok=True)
    unsigned = {
        key: value for key, value in body.items() if key != "receipt_digest"
    }
    body["receipt_digest"] = canonical_digest(unsigned)
    stamp = time.strftime(
        "%Y%m%dT%H%M%S", time.gmtime(body.get("created_at") or time.time())
    )
    path = receipts_dir / f"{stamp}-{uuid.uuid4().hex[:8]}.json"
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(receipts_dir, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def apply_evaluation_identity_retention(
    results_dir: str | Path,
    plan: dict[str, Any],
    *,
    triggered_by: str = "",
) -> dict[str, Any]:
    """Execute a retention plan and write the run receipt.

    Moves are single ``rename`` operations inside the same ``archive/``
    tree, so each is atomic; a failure on one snapshot only under-retires.
    Snapshots are never deleted here — the retirement area is the staging
    zone an operator purges after confirming stability.
    """

    root = Path(results_dir)
    archive = archive_dir(root)
    retired = retired_dir(root)
    if retired.is_symlink():
        raise EvaluationIdentityRetentionError(
            f"unsafe retired directory: {retired}"
        )
    retired_names = set(
        entry.name
        for entry in (retired.iterdir() if retired.is_dir() else [])
        if SNAPSHOT_NAME_RE.fullmatch(entry.name)
    )

    moved: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    bytes_staged = 0
    for entry in plan.get("retire") or []:
        name = str(entry.get("name") or "")
        snapshot = archive / name
        if (
            not SNAPSHOT_NAME_RE.fullmatch(name)
            or snapshot.is_symlink()
            or not snapshot.is_dir()
        ):
            failed.append({"name": name, "reason": "snapshot_missing"})
            continue
        if name in retired_names:
            failed.append({"name": name, "reason": "retired_name_exists"})
            continue
        try:
            retired.mkdir(parents=True, exist_ok=True)
            entry_bytes = _dir_bytes(snapshot)
            shutil.move(str(snapshot), str(retired / name))
        except OSError as exc:
            failed.append({"name": name, "reason": f"move_error:{exc}"})
            continue
        retired_names.add(name)
        bytes_staged += entry_bytes
        moved.append({
            "name": name,
            "manifest_digest": entry.get("manifest_digest"),
            "bytes": entry_bytes,
        })

    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": "evaluation_identity_retention",
        "created_at": time.time(),
        "triggered_by": triggered_by,
        "results_dir": str(root),
        "keep": int(plan.get("keep") or KEEP_RECENT_SNAPSHOTS),
        "retired_dir": str(retired),
        "moved": moved,
        "protected": [
            {
                "name": str(item.get("name")),
                "manifest_digest": item.get("manifest_digest"),
                "reason": str(item.get("reason") or ""),
            }
            for item in (plan.get("protected") or [])
        ],
        "kept_window": [
            str(item.get("name")) for item in (plan.get("snapshots") or [])
        ][-int(plan.get("keep") or KEEP_RECENT_SNAPSHOTS):],
        "failed": failed,
        "ignored_entries": plan.get("ignored_entries") or [],
        "bytes_staged": bytes_staged,
        "live_reference_count": int(plan.get("live_reference_count") or 0),
    }
    receipt_path = _write_receipt(retired / RECEIPTS_DIRNAME, receipt)
    receipt["receipt_path"] = str(receipt_path)
    return receipt


def run_evaluation_identity_retention(
    results_dir: str | Path,
    *,
    triggered_by: str = "",
    keep: int = KEEP_RECENT_SNAPSHOTS,
) -> dict[str, Any]:
    """Writer-side entry point; never raises into the migration caller.

    A retention failure (unreadable live surface, move error) must not fail
    an authority migration whose archive + fresh identity work already
    completed, so every failure is reported in the return value instead.
    """

    try:
        plan = plan_evaluation_identity_retention(
            results_dir, keep=keep
        )
        receipt = apply_evaluation_identity_retention(
            results_dir, plan, triggered_by=triggered_by
        )
    except Exception as exc:
        log.warning("evaluation identity retention pass failed: %s", exc)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "moved": [],
        }
    return {
        "ok": not receipt.get("failed"),
        "moved": receipt.get("moved") or [],
        "protected": receipt.get("protected") or [],
        "failed": receipt.get("failed") or [],
        "receipt_path": receipt.get("receipt_path"),
    }
