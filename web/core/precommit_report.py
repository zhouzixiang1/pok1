"""Seed-free precommit evaluation reports.

Every precommit outcome (pass or fail) becomes a stable, digest-bound JSON
report under ``RESULTS_DIR/precommit_reports/v<N>.json``.  The report is
derived from the candidate ledger's frozen scorecard; it is an evaluation
product, never a publication authority.  Publication still requires the
native precommit gate to pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

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
    return path


def _quarantined_candidate(results_dir: str | Path, version: int) -> Path | None:
    root = Path(results_dir) / ABANDON_TXN_DIR_NAME
    if not root.is_dir():
        return None
    for receipt in sorted(root.glob("*/candidate/policy_epoch_receipt.json")):
        try:
            payload = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if payload.get("version") == version:
            return receipt.parent
    return None


def write_diagnostic_manifest(
    results_dir: str | Path,
    *,
    version: int,
    report_path_value: Path,
    report_digest: str | None,
    source_v: int | None = None,
) -> Path | None:
    """Point at the canonical abandon quarantine for a failed candidate.

    The candidate code is never copied or executed here; the abandon
    transaction already preserves the exact five-file artifact.  The manifest
    makes that non-publishable snapshot discoverable next to its report.
    """

    candidate = _quarantined_candidate(results_dir, version)
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
    for version in sorted(latest):
        path = write_precommit_report(
            root, latest[version], generated_at=generated_at
        )
        if path is not None:
            paths.append(path)
            report = json.loads(path.read_text(encoding="utf-8"))
            if not report.get("publishable"):
                write_diagnostic_manifest(
                    root,
                    version=version,
                    report_path_value=path,
                    report_digest=report.get("report_digest"),
                    source_v=report.get("source_v"),
                )
    return paths
