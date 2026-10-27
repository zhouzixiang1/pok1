"""Automatic diagnostic-manifest scan for abandoned precommit failures."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import diagnostic_manifest
import precommit_report


def _entry(
    *, version: int = 589, passed: bool = False, timestamp: float = 123.0
) -> dict:
    status = "passed" if passed else "failed"
    return {
        "version": version,
        "source_v": version - 1,
        "stage": "verified" if passed else "precommit_failed",
        "candidate_id": f"national_cloud_v{version}_from_national_cloud_v{version - 1}",
        "run_id": f"{version}#0",
        "workflow_profile_id": "national_native",
        "parent_ids": [f"national_cloud_v{version - 1}"],
        "timestamp": timestamp,
        "gate": "precommit_eval",
        "failure_class": "none" if passed else "national_precommit_regression",
        "failures": (
            []
            if passed
            else ["{'reason': 'did_not_beat_parent', 'details': '4W-8L'}"]
        ),
        "scorecard": {
            "primary_score": 0.4167,
            "gates": [
                {
                    "name": precommit_report.PRECOMMIT_GATE_NAME,
                    "status": status,
                    "metrics": {
                        "outcome_gate": {
                            "wins": 20,
                            "losses": 28,
                            "draws": 0,
                            "samples": 48,
                            "per_matchup": [
                                {
                                    "opponent": f"national_cloud_v{version - 1}",
                                    "wins": 4,
                                    "losses": 8,
                                    "draws": 0,
                                    "samples": 12,
                                }
                            ],
                        },
                        "net_chips_mean": -1889.5,
                    },
                }
            ],
        },
    }


def _write_report(results: Path, entry: dict) -> Path:
    path = precommit_report.write_precommit_report(results, entry)
    assert path is not None
    return path


def _write_quarantine(
    results: Path,
    version: int,
    *,
    name: str = "tx-test",
    completed_at: float | None = None,
) -> Path:
    txn = results / precommit_report.ABANDON_TXN_DIR_NAME / name
    candidate = txn / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "policy_epoch_receipt.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    (candidate / "policy.py").write_text("# candidate\n", encoding="utf-8")
    terminal = txn / "receipt.json"
    terminal.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "national-policy-recorded-abandon-finalize",
                "candidate_state": "quarantine",
                "checkpoint_cleared": True,
            }
        ),
        encoding="utf-8",
    )
    if completed_at is not None:
        os.utime(terminal, (completed_at, completed_at))
    return candidate


def _manifest(results: Path, version: int) -> dict:
    path = precommit_report.diagnostic_manifest_path(results, version)
    assert path.is_file()
    return json.loads(path.read_text(encoding="utf-8"))


def test_no_quarantine_means_no_manifest(tmp_path: Path):
    results = tmp_path / "results"
    _write_report(results, _entry(version=589))

    summary = diagnostic_manifest.sync_diagnostic_manifests(results)

    assert summary["scanned"] == 1
    assert summary["awaiting_quarantine"] == 1
    assert summary["created"] == 0
    assert not precommit_report.diagnostic_manifest_path(
        results, 589
    ).exists()
    assert not (results / precommit_report.DIAGNOSTIC_DIR_NAME).exists()


def test_manifest_written_after_quarantine_and_report_untouched(
    tmp_path: Path,
):
    results = tmp_path / "results"
    report_path = _write_report(results, _entry(version=589))
    report_bytes = report_path.read_bytes()
    report_mtime = report_path.stat().st_mtime_ns
    assert not precommit_report.diagnostic_manifest_path(
        results, 589
    ).exists()

    candidate = _write_quarantine(results, 589)
    summary = diagnostic_manifest.sync_diagnostic_manifests(results)

    assert summary["created"] == 1
    assert summary["created_versions"] == [589]
    manifest = _manifest(results, 589)
    assert manifest["kind"] == "precommit-diagnostic-snapshot-v1"
    assert manifest["version"] == 589
    assert manifest["non_publishable"] is True
    assert manifest["evaluation_report"] == "precommit_reports/v589.json"
    assert manifest["candidate_snapshot"] == str(candidate)
    assert manifest["candidate_files"] == [
        "policy.py",
        "policy_epoch_receipt.json",
    ]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert manifest["report_digest"] == report["report_digest"]

    # The scan never rewrites the evaluation report.
    assert report_path.read_bytes() == report_bytes
    assert report_path.stat().st_mtime_ns == report_mtime


def test_sync_is_idempotent(tmp_path: Path):
    results = tmp_path / "results"
    _write_report(results, _entry(version=589))
    _write_quarantine(results, 589)
    assert diagnostic_manifest.sync_diagnostic_manifests(results)["created"] == 1

    manifest_path = precommit_report.diagnostic_manifest_path(results, 589)
    first_bytes = manifest_path.read_bytes()
    first_mtime = manifest_path.stat().st_mtime_ns

    summary = diagnostic_manifest.sync_diagnostic_manifests(results)

    assert summary["created"] == 0
    assert summary["unchanged"] == 1
    assert manifest_path.read_bytes() == first_bytes
    assert manifest_path.stat().st_mtime_ns == first_mtime


def test_new_report_digest_rewrites_manifest(tmp_path: Path):
    results = tmp_path / "results"
    _write_report(results, _entry(version=589, timestamp=123.0))
    _write_quarantine(results, 589)
    diagnostic_manifest.sync_diagnostic_manifests(results)
    manifest_path = precommit_report.diagnostic_manifest_path(results, 589)
    first_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    report_path = _write_report(results, _entry(version=589, timestamp=789.0))
    assert (
        json.loads(report_path.read_text(encoding="utf-8"))["report_digest"]
        != first_manifest["report_digest"]
    )
    summary = diagnostic_manifest.sync_diagnostic_manifests(results)

    assert summary["created"] == 1
    manifest = _manifest(results, 589)
    assert manifest["report_digest"] == json.loads(
        report_path.read_text(encoding="utf-8")
    )["report_digest"]


def test_quarantine_must_postdate_the_report(tmp_path: Path):
    """A re-allocated version must wait for its own abandon, not an older one."""

    results = tmp_path / "results"
    old_candidate = _write_quarantine(
        results, 590, name="tx-old", completed_at=1_000.0
    )
    _write_report(results, _entry(version=590, timestamp=2_000.0))

    summary = diagnostic_manifest.sync_diagnostic_manifests(results)

    assert summary["awaiting_quarantine"] == 1
    assert summary["created"] == 0
    assert not precommit_report.diagnostic_manifest_path(
        results, 590
    ).exists()

    new_candidate = _write_quarantine(
        results, 590, name="tx-new", completed_at=3_000.0
    )
    summary = diagnostic_manifest.sync_diagnostic_manifests(results)

    assert summary["created"] == 1
    manifest = _manifest(results, 590)
    assert manifest["candidate_snapshot"] == str(new_candidate)
    assert manifest["candidate_snapshot"] != str(old_candidate)


def test_incomplete_abandon_transaction_is_not_bound(tmp_path: Path):
    results = tmp_path / "results"
    _write_report(results, _entry(version=591))
    candidate = _write_quarantine(results, 591)
    terminal = candidate.parent / "receipt.json"
    terminal.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "candidate_state": "absent",
                "checkpoint_cleared": False,
            }
        ),
        encoding="utf-8",
    )

    summary = diagnostic_manifest.sync_diagnostic_manifests(results)

    assert summary["awaiting_quarantine"] == 1
    assert summary["created"] == 0
    assert not precommit_report.diagnostic_manifest_path(
        results, 591
    ).exists()


def test_passing_report_never_gets_a_manifest(tmp_path: Path):
    results = tmp_path / "results"
    _write_report(results, _entry(version=590, passed=True))
    _write_quarantine(results, 590)

    summary = diagnostic_manifest.sync_diagnostic_manifests(results)

    assert summary["skipped_publishable"] == 1
    assert summary["created"] == 0
    assert not precommit_report.diagnostic_manifest_path(
        results, 590
    ).exists()


def test_malformed_and_symlinked_reports_are_ignored(tmp_path: Path):
    results = tmp_path / "results"
    report_dir = results / precommit_report.REPORT_DIR_NAME
    report_dir.mkdir(parents=True)
    (report_dir / "v700.json").write_text("{not json", encoding="utf-8")
    _write_quarantine(results, 701)
    outside = tmp_path / "outside-v701.json"
    outside.write_text(json.dumps(_entry(version=701)), encoding="utf-8")
    (report_dir / "v701.json").symlink_to(outside)

    summary = diagnostic_manifest.sync_diagnostic_manifests(results)

    assert summary["scanned"] == 1
    assert summary["skipped_invalid"] == 1
    assert summary["created"] == 0
    assert not precommit_report.diagnostic_manifest_path(
        results, 701
    ).exists()


def test_round_evidence_copies_do_not_become_versions(tmp_path: Path):
    """``vN_roundK.json`` copies are evidence, never a scannable version."""

    results = tmp_path / "results"
    for round_number in (1, 2):
        entry = _entry(version=589)
        entry["metrics"] = {"precommit_seed_round": round_number}
        _write_report(results, entry)
    _write_quarantine(results, 589)

    summary = diagnostic_manifest.sync_diagnostic_manifests(results)

    assert summary["scanned"] == 1
    assert summary["created"] == 1
    assert summary["skipped_invalid"] == 0
    manifest = _manifest(results, 589)
    assert manifest["evaluation_report"] == "precommit_reports/v589.json"
    report_dir = results / precommit_report.REPORT_DIR_NAME
    assert (report_dir / "v589_round1.json").is_file()
    assert (report_dir / "v589_round2.json").is_file()


def test_loop_disabled_is_a_noop(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("POK_DIAGNOSTIC_MANIFEST_ENABLED", "0")
    _write_report(tmp_path / "results", _entry(version=589))
    _write_quarantine(tmp_path / "results", 589)

    asyncio.run(
        diagnostic_manifest.run_diagnostic_manifest_loop(
            results_dir=tmp_path / "results"
        )
    )

    assert not precommit_report.diagnostic_manifest_path(
        tmp_path / "results", 589
    ).exists()
