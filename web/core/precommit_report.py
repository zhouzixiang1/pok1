"""Seed-free precommit evaluation reports.

Every precommit outcome (pass or fail) becomes a stable, digest-bound JSON
report under ``RESULTS_DIR/precommit_reports/v<N>.json``.  The canonical file
always holds the latest round; each round also keeps a copy beside it
(``v<N>_round<k>.json``, or a stable event-timestamp label for legacy rows) so
a later repair round cannot erase the earlier round's evidence.  The report is
derived from the candidate ledger's frozen scorecard; it is an evaluation
product, never a publication authority.  Publication still requires the
native precommit gate to pass.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("pok.precommit_report")

SCHEMA_VERSION = 1
KIND = "precommit-evaluation-report-v1"
REPORT_DIR_NAME = "precommit_reports"
DIAGNOSTIC_DIR_NAME = "diagnostics"
ABANDON_TXN_DIR_NAME = "policy_epoch_abandon_transactions"
PRECOMMIT_GATE_NAME = "national_precommit_regression"
_FORBIDDEN_KEY_PARTS = ("seed", "nonce")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact(item)
            for key, item in value.items()
            if not any(part in str(key).lower() for part in _FORBIDDEN_KEY_PARTS)
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _positive_int(value: Any) -> int | None:
    """Return ``value`` only when it is a genuine positive ``int``."""

    return value if type(value) is int and value >= 1 else None


def _scorecard_gates(entry: dict[str, Any]) -> list[dict[str, Any]]:
    scorecard = entry.get("scorecard")
    if not isinstance(scorecard, dict):
        return []
    gates = scorecard.get("gates")
    if not isinstance(gates, list):
        return []
    return [gate for gate in gates if isinstance(gate, dict)]


def _precommit_gate(entry: dict[str, Any]) -> dict[str, Any] | None:
    for gate in _scorecard_gates(entry):
        if gate.get("name") == PRECOMMIT_GATE_NAME:
            return gate
    if entry.get("gate") == "precommit_eval":
        for gate in _scorecard_gates(entry):
            if isinstance(gate.get("metrics"), dict):
                return gate
    return None


def _parse_blocker(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return _redact(raw)
    if not isinstance(raw, str):
        return {"raw": str(raw)}
    try:
        import ast

        parsed = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        return _redact(parsed)
    return {"raw": raw}


def build_precommit_report(
    entry: dict[str, Any], *, generated_at: str | None = None
) -> dict[str, Any] | None:
    """Build one report, or return None when the entry is not precommit."""

    gate = _precommit_gate(entry)
    if gate is None:
        return None
    metrics = gate.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    # Ledger-level metrics (the event's own ``metrics`` dict) carry the round
    # bookkeeping; the gate-level metrics above are the scorecard evidence.
    event_metrics = entry.get("metrics")
    if not isinstance(event_metrics, dict):
        event_metrics = {}
    outcome = metrics.get("outcome_gate")
    if not isinstance(outcome, dict):
        outcome = {}
    raw_matchups = outcome.get("per_matchup")
    if not isinstance(raw_matchups, list):
        raw_matchups = []
    matchups = []
    for row in raw_matchups:
        if not isinstance(row, dict):
            continue
        matchups.append(
            {
                key: row.get(key)
                for key in (
                    "opponent",
                    "wins",
                    "losses",
                    "draws",
                    "samples",
                    "points",
                    "primary_match_score",
                    "win_loss_margin",
                )
                if key in row
            }
        )
    blockers = [_parse_blocker(item) for item in (entry.get("failures") or [])]
    status = str(gate.get("status") or "")
    precommit_passed = status == "passed" and not blockers
    scorecard = entry.get("scorecard")
    primary_score = (
        scorecard.get("primary_score") if isinstance(scorecard, dict) else None
    )
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "version": entry.get("version"),
        "source_v": entry.get("source_v"),
        "stage": entry.get("stage"),
        "candidate_id": entry.get("candidate_id"),
        "run_id": entry.get("run_id"),
        "workflow_profile_id": entry.get("workflow_profile_id"),
        "parent_ids": list(entry.get("parent_ids") or []),
        "generated_at": generated_at
        or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_event_timestamp": entry.get("timestamp"),
        # Round identity of this precommit evaluation.  ``sample_round`` is the
        # frozen plan's seed round (1 = first sample, 2 = the one pre-registered
        # independent-seed repair round); ``precommit_attempt`` distinguishes
        # re-runs of the same round.  Both stay top-level (never inside the
        # ``_redact`` subtrees) and carry no plan seed material.
        "sample_round": _positive_int(
            event_metrics.get("precommit_seed_round")
        ),
        "precommit_attempt": _positive_int(
            event_metrics.get("precommit_attempt")
        ),
        "gate": gate.get("name"),
        "status": status,
        "precommit_passed": precommit_passed,
        "publishable": precommit_passed,
        "primary_score": primary_score,
        "aggregate": _redact(
            {
                "wins": outcome.get("wins"),
                "losses": outcome.get("losses"),
                "draws": outcome.get("draws"),
                "samples": outcome.get("samples"),
                "score": outcome.get("primary_match_score"),
                "win_loss_margin": outcome.get("win_loss_margin"),
                "net_chips_samples": metrics.get("net_chips_samples"),
                "net_chips_mean": metrics.get("net_chips_mean"),
                "net_chips_std": metrics.get("net_chips_std"),
                "net_chips_min": metrics.get("net_chips_min"),
                "net_chips_max": metrics.get("net_chips_max"),
                "net_chips_ci": metrics.get("secondary_net_chip_ci"),
            }
        ),
        "matchups": _redact(matchups),
        "plan": _redact(
            {
                "matches_per_opponent": metrics.get("matches_per_opponent"),
                "parent_min_samples": outcome.get("parent_min_samples"),
                "parent_max_score": outcome.get("parent_max_score"),
                "aggregate_min_samples": outcome.get("aggregate_min_samples"),
                "aggregate_min_loss_margin": outcome.get(
                    "aggregate_min_loss_margin"
                ),
                "gate_basis": outcome.get("gate_basis"),
            }
        ),
        "blockers": blockers,
        "failure_class": entry.get("failure_class"),
        "gate_degraded": metrics.get("gate_degraded"),
        "diagnostic": {
            "non_publishable": not precommit_passed,
            "note": (
                "Non-publishable evaluation evidence; a failed candidate's "
                "artifact is preserved by the canonical abandon transaction."
                if not precommit_passed
                else "Precommit passed; publication remains owned by the native precommit gate."
            ),
        },
    }
    raw = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "surrogateescape")
    report["report_digest"] = hashlib.sha256(raw).hexdigest()
    return report


def report_path(results_dir: str | Path, version: int) -> Path:
    return Path(results_dir) / REPORT_DIR_NAME / f"v{int(version)}.json"


def report_round_path(results_dir: str | Path, version: int, label: str) -> Path:
    """Round/timestamp evidence copy beside the canonical per-version report.

    The canonical ``v<N>.json`` keeps its "latest round wins" meaning; these
    copies preserve earlier rounds.  The name never matches the diagnostic
    scan's ``^v(\\d+)\\.json$`` pattern, so readers are unaffected.
    """

    return Path(results_dir) / REPORT_DIR_NAME / f"v{int(version)}_{label}.json"


def _round_label(report: dict[str, Any]) -> str:
    """File-label for the round copy, or ``""`` when none can be derived.

    ``sample_round`` is the round identity.  ``precommit_attempt`` is NOT one:
    the rework transition clears the checkpoint counter, so both rounds report
    attempt 1.  It is only a disambiguator for same-round re-runs.  Legacy rows
    without round metrics fall back to the stable source event timestamp (never
    ``generated_at``, which is the wall clock of each export run and would emit
    a new file every backfill).
    """

    round_value = _positive_int(report.get("sample_round"))
    if round_value is not None:
        attempt = _positive_int(report.get("precommit_attempt"))
        if attempt is not None:
            return f"round{round_value}_attempt{attempt}"
        return f"round{round_value}"
    timestamp = report.get("source_event_timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return ""
    if not math.isfinite(timestamp) or timestamp <= 0:
        return ""
    try:
        return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(float(timestamp)))
    except (OSError, OverflowError, ValueError):
        return ""


def diagnostic_manifest_path(results_dir: str | Path, version: int) -> Path:
    return (
        Path(results_dir)
        / DIAGNOSTIC_DIR_NAME
        / f"v{int(version)}"
        / "manifest.json"
    )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        try:
            descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            descriptor = None
        if descriptor is not None:
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temp.unlink(missing_ok=True)


def write_precommit_report(
    results_dir: str | Path,
    entry: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> Path | None:
    """Write one report atomically; returns its path or None."""

    report = build_precommit_report(entry, generated_at=generated_at)
    if report is None:
        return None
    version = report.get("version")
    if type(version) is not int:
        return None
    path = report_path(results_dir, version)
    _write_json_atomic(path, report)
    # Conservative round evidence: the canonical report above is authoritative
    # and already durable, so a failing copy can never block the main path.
    try:
        label = _round_label(report)
        if label:
            _write_json_atomic(
                report_round_path(results_dir, version, label), report
            )
    except Exception:
        log.warning("precommit round evidence copy failed", exc_info=True)
    return path


def quarantined_candidate_index(results_dir: str | Path) -> dict[int, Path]:
    """Map every abandoned version to its canonical quarantine candidate dir.

    One directory walk per caller instead of one walk per version.  The first
    sorted candidate receipt for a version wins, exactly matching
    ``_quarantined_candidate``.  Corrupt or foreign receipts are skipped.
    """

    root = Path(results_dir) / ABANDON_TXN_DIR_NAME
    index: dict[int, Path] = {}
    if not root.is_dir():
        return index
    for receipt in sorted(root.glob("*/candidate/policy_epoch_receipt.json")):
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        version = payload.get("version")
        if type(version) is not int:
            continue
        index.setdefault(version, receipt.parent)
    return index


def quarantined_candidate_entries(
    results_dir: str | Path,
) -> dict[int, list[tuple[float, Path]]]:
    """Map version -> *completed* abandon quarantines, newest first.

    A transaction counts only when its terminal ``receipt.json`` proves the
    candidate is in quarantine and the checkpoint was cleared, and the
    quarantined candidate receipt is readable.  ``abandoned_at`` is the
    terminal receipt mtime, which is strictly after the precommit evaluation
    that caused the abandon.  A batch caller (the automatic diagnostic scan)
    uses that ordering to bind a report to the quarantine created *after* it
    instead of an older quarantine left by a previous attempt at the same
    re-allocated version.
    """

    root = Path(results_dir) / ABANDON_TXN_DIR_NAME
    entries: dict[int, list[tuple[float, Path]]] = {}
    if not root.is_dir():
        return entries
    for receipt in sorted(root.glob("*/receipt.json")):
        candidate = receipt.parent / "candidate"
        try:
            terminal = json.loads(receipt.read_text(encoding="utf-8"))
            if (
                terminal.get("candidate_state") != "quarantine"
                or terminal.get("checkpoint_cleared") is not True
            ):
                continue
            payload = json.loads(
                (candidate / "policy_epoch_receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            abandoned_at = receipt.stat().st_mtime
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        version = payload.get("version")
        if type(version) is not int:
            continue
        entries.setdefault(version, []).append((abandoned_at, candidate))
    for rows in entries.values():
        rows.sort(key=lambda item: item[0], reverse=True)
    return entries


def _quarantined_candidate(results_dir: str | Path, version: int) -> Path | None:
    return quarantined_candidate_index(results_dir).get(int(version))


def write_diagnostic_manifest(
    results_dir: str | Path,
    *,
    version: int,
    report_path_value: Path,
    report_digest: str | None,
    source_v: int | None = None,
    candidate_dir: str | Path | None = None,
) -> Path | None:
    """Point at the canonical abandon quarantine for a failed candidate.

    The candidate code is never copied or executed here; the abandon
    transaction already preserves the exact five-file artifact.  The manifest
    makes that non-publishable snapshot discoverable next to its report.

    ``candidate_dir`` lets a batch caller reuse one quarantine index walk; it
    is re-validated against the receipt's version so a stale or foreign path
    can never be bound.
    """

    if candidate_dir is None:
        candidate = _quarantined_candidate(results_dir, version)
    else:
        candidate = Path(candidate_dir)
        try:
            payload = json.loads(
                (candidate / "policy_epoch_receipt.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if payload.get("version") != int(version) or not candidate.is_dir():
            return None
    if candidate is None:
        return None
    root = Path(results_dir)
    try:
        report_ref = str(report_path_value.relative_to(root))
    except ValueError:
        report_ref = str(report_path_value)
    files = sorted(
        item.name for item in candidate.iterdir() if item.is_file()
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "precommit-diagnostic-snapshot-v1",
        "version": int(version),
        "source_v": source_v,
        "non_publishable": True,
        "evaluation_report": report_ref,
        "report_digest": report_digest,
        "candidate_snapshot": str(candidate),
        "candidate_files": files,
        "note": (
            "Quarantined by the canonical abandon transaction; this snapshot "
            "is diagnostic only and can never be published from here."
        ),
    }
    raw = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8", "surrogateescape")
    manifest["manifest_digest"] = hashlib.sha256(raw).hexdigest()
    path = diagnostic_manifest_path(root, version)
    _write_json_atomic(path, manifest)
    return path


def export_precommit_reports(
    results_dir: str | Path,
    *,
    versions: Iterable[int] | None = None,
    generated_at: str | None = None,
) -> list[Path]:
    """Backfill reports from the append-only candidate ledger.

    The last precommit event for each version wins, matching the ledger's
    append order.  Compacted historical rows without a scorecard are skipped.
    """

    root = Path(results_dir)
    ledger = root / "candidates.jsonl"
    if not ledger.is_file():
        return []
    wanted = {int(version) for version in versions} if versions is not None else None
    latest: dict[int, dict[str, Any]] = {}
    with open(ledger, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            version = entry.get("version")
            if type(version) is not int:
                continue
            if wanted is not None and version not in wanted:
                continue
            if _precommit_gate(entry) is None:
                continue
            latest[version] = entry
    paths = []
    quarantine_index = quarantined_candidate_index(root)
    for version in sorted(latest):
        path = write_precommit_report(
            root, latest[version], generated_at=generated_at
        )
        if path is not None:
            paths.append(path)
            report = json.loads(path.read_text(encoding="utf-8"))
            candidate = quarantine_index.get(version)
            if not report.get("publishable") and candidate is not None:
                write_diagnostic_manifest(
                    root,
                    version=version,
                    report_path_value=path,
                    report_digest=report.get("report_digest"),
                    source_v=report.get("source_v"),
                    candidate_dir=candidate,
                )
    return paths
