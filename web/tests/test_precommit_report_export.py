from __future__ import annotations

import hashlib
import json
from pathlib import Path

import candidate_store
import precommit_report


def _entry(*, version: int = 589, status: str = "failed") -> dict:
    return {
        "version": version,
        "source_v": 585,
        "stage": "precommit_failed" if status == "failed" else "verified",
        "candidate_id": f"national_cloud_v{version}_from_national_cloud_v585",
        "run_id": f"{version}#0",
        "workflow_profile_id": "national_native",
        "parent_ids": ["national_cloud_v585"],
        "timestamp": 123.0,
        "gate": "precommit_eval",
        "failure_class": (
            "national_precommit_regression" if status == "failed" else "none"
        ),
        "failures": (
            [
                "{'reason': 'did_not_beat_parent', 'opponent': "
                "'national_cloud_v585', 'details': '4W-8L'}"
            ]
            if status == "failed"
            else []
        ),
        "scorecard": {
            "name": "precommit_eval",
            "primary_score": 0.4167,
            "gates": [
                {
                    "name": "national_precommit_regression",
                    "status": status,
                    "blocking": True,
                    "metrics": {
                        "protocol": "national_native_tcp",
                        "hands_per_match": 70,
                        "matches_per_opponent": 12,
                        "deck_seed_base": 123456,
                        "outcome_gate": {
                            "wins": 20,
                            "losses": 28,
                            "draws": 0,
                            "samples": 48,
                            "points": 20.0,
                            "primary_match_score": 0.4167,
                            "win_loss_margin": -8,
                            "per_matchup": [
                                {
                                    "opponent": "national_cloud_v585",
                                    "wins": 4,
                                    "losses": 8,
                                    "draws": 0,
                                    "samples": 12,
                                    "points": 4.0,
                                    "primary_match_score": 0.3333,
                                    "win_loss_margin": -4,
                                },
                                {
                                    "opponent": "national_cloud_v173",
                                    "wins": 12,
                                    "losses": 12,
                                    "draws": 0,
                                    "samples": 24,
                                    "points": 12.0,
                                    "primary_match_score": 0.5,
                                    "win_loss_margin": 0,
                                },
                            ],
                            "parent_min_samples": 6,
                            "parent_max_score": 0.5,
                            "aggregate_min_samples": 8,
                            "aggregate_min_loss_margin": 2,
                            "gate_basis": "complete_70_hand_wld_plus_tie_chip_ci",
                        },
                        "net_chips_samples": 48,
                        "net_chips_mean": -1889.5,
                        "net_chips_std": 14994.6,
                        "net_chips_min": -18593,
                        "net_chips_max": 18463,
                        "secondary_net_chip_ci": [-5962.4, 2501.0],
                        "gate_degraded": False,
                    },
                    "failures": [],
                }
            ],
        },
    }


def test_build_report_extracts_scorecard_and_redacts():
    report = precommit_report.build_precommit_report(_entry())
    assert report is not None
    assert report["version"] == 589
    assert report["aggregate"]["wins"] == 20
    assert report["aggregate"]["losses"] == 28
    assert report["matchups"][0]["opponent"] == "national_cloud_v585"
    assert report["blockers"][0]["reason"] == "did_not_beat_parent"
    assert report["precommit_passed"] is False
    assert report["publishable"] is False
    assert report["diagnostic"]["non_publishable"] is True
    assert "seed" not in json.dumps(report).lower()
    payload = {key: value for key, value in report.items() if key != "report_digest"}
    expected = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", "surrogateescape")
    ).hexdigest()
    assert report["report_digest"] == expected


def test_export_and_append_hook_write_reports(tmp_path: Path):
    ledger = tmp_path / "candidates.jsonl"
    quality = {"version": 589, "stage": "quality_passed", "gate": "quality"}
    ledger.write_text(
        json.dumps(quality) + "\n" + json.dumps(_entry()) + "\n",
        encoding="utf-8",
    )
    quarantine = (
        tmp_path
        / "policy_epoch_abandon_transactions"
        / "tx-test"
        / "candidate"
    )
    quarantine.mkdir(parents=True)
    (quarantine / "policy_epoch_receipt.json").write_text(
        json.dumps({"version": 589}), encoding="utf-8"
    )
    (quarantine / "policy.py").write_text("# candidate\n", encoding="utf-8")
    paths = precommit_report.export_precommit_reports(tmp_path)
    assert [path.name for path in paths] == ["v589.json"]
    report = json.loads(paths[0].read_text(encoding="utf-8"))
    assert report["version"] == 589
    manifest = json.loads(
        (tmp_path / "diagnostics" / "v589" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["non_publishable"] is True
    assert manifest["candidate_snapshot"].endswith("candidate")
    assert manifest["candidate_files"] == [
        "policy.py",
        "policy_epoch_receipt.json",
    ]
    assert manifest["manifest_digest"]

    entry = _entry(version=590)
    candidate_store.append_candidate_event(
        "precommit_failed",
        version=590,
        source_v=585,
        candidate_id=entry["candidate_id"],
        run_id=entry["run_id"],
        workflow_profile_id=entry["workflow_profile_id"],
        parent_ids=entry["parent_ids"],
        stage=entry["stage"],
        gate=entry["gate"],
        scorecard=entry["scorecard"],
        metrics={"passed": False, "net_chips_mean": -1889.5},
        failures=entry["failures"],
        failure_class=entry["failure_class"],
        path=ledger,
    )
    hook_report = tmp_path / "precommit_reports" / "v590.json"
    assert hook_report.is_file()
    assert json.loads(hook_report.read_text(encoding="utf-8"))["version"] == 590
