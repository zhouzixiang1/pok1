"""Automatic diagnostic manifests for abandoned precommit failures.

The precommit append hook writes ``RESULTS_DIR/precommit_reports/v<N>.json``
for every precommit outcome.  A failed candidate's exact five-file artifact
only exists in the canonical abandon quarantine *after* the abandon
transaction runs, so its diagnostic manifest (which points at that snapshot)
cannot be written when the report is written.

This module runs a low-frequency, read-only scan from the web lifespan.  For
every non-publishable report it materializes ``diagnostics/v<N>/manifest.json``
once the *completed* abandon transaction for that report exists (a terminal
receipt whose mtime postdates the report, so a re-allocated version never
binds a previous attempt's quarantine).  It:

- scans only ``RESULTS_DIR/precommit_reports/`` (the bounded, one-file-per-
  version projection of precommit ledger events) plus one receipt walk of
  ``policy_epoch_abandon_transactions/`` -- never the multi-hundred-MB
  candidate ledger body and never arbitrary result trees;
- never copies, imports, or executes candidate code;
- never writes reports, checkpoints, the abandon transaction, or any
  publication authority; it only writes the diagnostic manifest via
  ``precommit_report.write_diagnostic_manifest``;
- is idempotent: an existing manifest bound to the same report digest and
  quarantine snapshot is left byte-identical.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import precommit_report

log = logging.getLogger("pok.diagnostics")

DEFAULT_INTERVAL_SEC = 120.0
MIN_INTERVAL_SEC = 60.0
MAX_INTERVAL_SEC = 300.0
# One file per precommit-evaluated version; the cap is a defensive bound, not
# an expected size (the cloud epoch publishes/abandons tens per month).
MAX_REPORTS_PER_CYCLE = 4096
# The terminal abandon receipt is written after the precommit event that
# caused it, so its mtime must be >= the report's ledger timestamp.  The
# tolerance only absorbs coarse/filesystem clock adjustments; re-allocated
# versions are separated by a whole generation (much more than a minute).
QUARANTINE_AFTER_REPORT_TOLERANCE_SEC = 60.0

_REPORT_NAME = re.compile(r"^v(\d+)\.json$")
_YES = {"1", "true", "yes", "on"}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _YES


def _interval_sec() -> float:
    raw = os.environ.get("POK_DIAGNOSTIC_MANIFEST_INTERVAL_SEC")
    try:
        value = float(raw) if raw is not None else DEFAULT_INTERVAL_SEC
    except (TypeError, ValueError):
        value = DEFAULT_INTERVAL_SEC
    return min(MAX_INTERVAL_SEC, max(MIN_INTERVAL_SEC, value))


def _iter_reports(report_dir: Path) -> list[tuple[int, Path]]:
    """Return ``(version, path)`` pairs, oldest first.

    Symlinked report files are ignored: this scan is read-only evidence
    materialization and must not be redirected outside the results tree.
    """

    items: list[tuple[int, Path]] = []
    for entry in report_dir.iterdir():
        match = _REPORT_NAME.match(entry.name)
        if match is None:
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        items.append((int(match.group(1)), entry))
    items.sort()
    return items


def _read_report(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _manifest_matches(
    path: Path, *, report_digest: str, candidate: Path
) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("kind") == "precommit-diagnostic-snapshot-v1"
        and payload.get("non_publishable") is True
        and payload.get("report_digest") == report_digest
        and payload.get("candidate_snapshot") == str(candidate)
    )


def _report_not_before(report: dict[str, Any]) -> float | None:
    value = report.get("source_event_timestamp")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) - QUARANTINE_AFTER_REPORT_TOLERANCE_SEC


def _select_candidate(
    entries: list[tuple[float, Path]], not_before: float | None
) -> Path | None:
    """Newest completed quarantine that postdates the report it serves."""

    for abandoned_at, candidate in entries:
        if not_before is None or abandoned_at >= not_before:
            return candidate
    return None


def _sync_report(
    root: Path,
    version: int,
    report_path_value: Path,
    quarantine_index: dict[int, list[tuple[float, Path]]],
) -> str:
    report = _read_report(report_path_value)
    if (
        report is None
        or report.get("kind") != precommit_report.KIND
        or type(report.get("version")) is not int
        or report.get("version") != version
    ):
        return "skipped_invalid"
    if report.get("precommit_passed") is not False:
        # Only a failed outcome is non-publishable; a later pass overwrites the
        # report and must never inherit a diagnostic snapshot.
        return "skipped_publishable"
    report_digest = report.get("report_digest")
    if not isinstance(report_digest, str) or not report_digest:
        return "skipped_invalid"
    candidate = _select_candidate(
        quarantine_index.get(version) or [], _report_not_before(report)
    )
    if candidate is None:
        return "awaiting_quarantine"
    manifest_path = precommit_report.diagnostic_manifest_path(root, version)
    if _manifest_matches(
        manifest_path, report_digest=report_digest, candidate=candidate
    ):
        return "unchanged"
    written = precommit_report.write_diagnostic_manifest(
        root,
        version=version,
        report_path_value=report_path_value,
        report_digest=report_digest,
        source_v=report.get("source_v"),
        candidate_dir=candidate,
    )
    return "created" if written is not None else "awaiting_quarantine"


def sync_diagnostic_manifests(
    results_dir: str | Path,
    *,
    max_reports: int = MAX_REPORTS_PER_CYCLE,
) -> dict[str, Any]:
    """Materialize missing diagnostic manifests; never raises. Idempotent."""

    root = Path(results_dir)
    summary: dict[str, Any] = {
        "scanned": 0,
        "created": 0,
        "unchanged": 0,
        "awaiting_quarantine": 0,
        "skipped_publishable": 0,
        "skipped_invalid": 0,
        "failed": 0,
        "created_versions": [],
    }
    report_dir = root / precommit_report.REPORT_DIR_NAME
    try:
        if not report_dir.is_dir():
            return summary
        reports = _iter_reports(report_dir)
    except OSError as exc:
        log.warning("diagnostic manifest report scan failed: %s", exc)
        summary["failed"] += 1
        return summary
    if max_reports and len(reports) > int(max_reports):
        # Keep the newest versions; older ones are already materialized or
        # will be caught once the backlog shrinks.
        reports = reports[-int(max_reports) :]
    summary["scanned"] = len(reports)
    quarantine_index = precommit_report.quarantined_candidate_entries(root)
    for version, path in reports:
        try:
            outcome = _sync_report(root, version, path, quarantine_index)
        except Exception:
            log.warning(
                "diagnostic manifest for v%s failed", version, exc_info=True
            )
            summary["failed"] += 1
            continue
        summary[outcome] = int(summary.get(outcome) or 0) + 1
        if outcome == "created":
            summary["created_versions"].append(version)
    return summary


def _shutting_down(shutdown_mgr: Any) -> bool:
    return bool(
        shutdown_mgr is not None
        and getattr(shutdown_mgr, "is_shutting_down", False)
    )


def _resolve_results_dir(results_dir: str | Path | None) -> Path:
    if results_dir is not None:
        return Path(results_dir)
    from evolution_infra import RESULTS_DIR

    return Path(RESULTS_DIR)


async def run_diagnostic_manifest_loop(
    shutdown_mgr: Any = None,
    *,
    results_dir: str | Path | None = None,
) -> None:
    """Process-lifetime loop. Never raises into the lifespan."""

    if not _env_flag("POK_DIAGNOSTIC_MANIFEST_ENABLED", True):
        log.info(
            "diagnostic manifest scan disabled "
            "(POK_DIAGNOSTIC_MANIFEST_ENABLED)"
        )
        return
    interval = _interval_sec()
    try:
        root = _resolve_results_dir(results_dir)
    except Exception as exc:
        log.warning(
            "diagnostic manifest scan disabled: results dir unavailable: %s",
            exc,
        )
        return
    log.info("diagnostic manifest scan started (interval=%.0fs)", interval)
    while not _shutting_down(shutdown_mgr):
        try:
            summary = await asyncio.to_thread(sync_diagnostic_manifests, root)
            if summary.get("created"):
                versions = summary.get("created_versions") or []
                log.info(
                    "diagnostic manifests written: %s",
                    ",".join(f"v{v}" for v in versions),
                )
                try:
                    from system_log import log_system_event

                    log_system_event(
                        "pipeline.diagnostic_manifest_written",
                        "info",
                        (
                            "Wrote non-publishable diagnostic manifests for "
                            f"abandoned precommit failures: {versions}"
                        ),
                        {
                            "created": summary.get("created"),
                            "versions": versions,
                            "scanned": summary.get("scanned"),
                        },
                    )
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("diagnostic manifest scan cycle failed: %s", exc)
        waited = 0.0
        while waited < interval:
            if _shutting_down(shutdown_mgr):
                break
            await asyncio.sleep(1.0)
            waited += 1.0
