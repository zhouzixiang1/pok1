import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import evaluation_data_identity as identity


def test_empty_results_initialize_content_bound_manifest(tmp_path):
    results = tmp_path / "results"

    manifest = identity.ensure_evaluation_data_identity(results)
    loaded = json.loads((results / identity.MANIFEST_NAME).read_text(encoding="utf-8"))

    assert manifest == loaded
    assert manifest["schema_version"] == identity.IDENTITY_SCHEMA_VERSION == 2
    assert manifest["base_identity"]["schema_version"] == identity.IDENTITY_SCHEMA_VERSION
    assert manifest["base_identity"]["profile_id"] == identity.PROFILE_ID
    assert manifest["base_identity"]["authority"] == "rating_daemon_only"
    assert manifest["base_identity"]["official_exe_strength_weight"] == 0
    assert len(manifest["identity_instance_id"]) == 32
    assert len(manifest["manifest_digest"]) == 64


def test_existing_unidentified_ratings_fail_closed(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "glicko_ratings.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(identity.EvaluationDataIdentityError, match="no evaluation identity"):
        identity.ensure_evaluation_data_identity(results)


def test_existing_unidentified_daemon_stats_fail_closed(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "elo_daemon_stats.json").write_text(
        '{"total_games": 120}\n', encoding="utf-8"
    )

    with pytest.raises(identity.EvaluationDataIdentityError, match="no evaluation identity"):
        identity.ensure_evaluation_data_identity(results)


def test_corrupt_identity_manifest_digest_fails_closed(tmp_path):
    results = tmp_path / "results"
    identity.ensure_evaluation_data_identity(results)
    manifest_path = results / identity.MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["manifest_digest"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(identity.EvaluationDataIdentityError, match="digest mismatch"):
        identity.ensure_evaluation_data_identity(results)


def test_digest_valid_but_base_mismatched_identity_fails_closed(tmp_path):
    results = tmp_path / "results"
    identity.ensure_evaluation_data_identity(results)
    manifest_path = results / identity.MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["base_identity"]["profile_id"] = "wrong-rating-authority"
    payload["manifest_digest"] = identity.canonical_digest({
        key: value for key, value in payload.items() if key != "manifest_digest"
    })
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(identity.EvaluationDataIdentityError, match="identity changed"):
        identity.ensure_evaluation_data_identity(results)


def test_runtime_profile_drift_requires_explicit_rotation(tmp_path):
    results = tmp_path / "results"
    identity.ensure_evaluation_data_identity(
        results,
        runtime_profile={"protocol": "national", "national_matches": 5},
    )

    with pytest.raises(identity.EvaluationDataIdentityError, match="runtime profile changed"):
        identity.ensure_evaluation_data_identity(
            results,
            runtime_profile={"protocol": "national", "national_matches": 6},
        )


def test_archive_preserves_old_authoritative_data_before_fresh_manifest(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "glicko_ratings.json").write_text('{"national_v1": {}}\n', encoding="utf-8")
    (results / "head_to_head.json").write_text("{}\n", encoding="utf-8")
    (results / "elo_daemon_stats.json").write_text(
        '{"total_games": 120}\n', encoding="utf-8"
    )
    (results / "daemon_stats.json").write_text(
        '{"total_games": 80}\n', encoding="utf-8"
    )
    cycle_payload = results / "evaluation_cycles" / "legacy-cycle"
    cycle_payload.mkdir(parents=True)
    (cycle_payload / "glicko_ratings.json").write_text("{}\n", encoding="utf-8")

    migrated = identity.archive_and_initialize(results, reason="test evaluator migration")

    archive = Path(migrated["archive_dir"])
    assert (archive / "glicko_ratings.json").is_file()
    assert (archive / "head_to_head.json").is_file()
    assert (archive / "elo_daemon_stats.json").is_file()
    assert (archive / "daemon_stats.json").is_file()
    assert (
        archive / "evaluation_cycles" / "legacy-cycle" / "glicko_ratings.json"
    ).is_file()
    assert (archive / "migration.json").is_file()
    assert (results / identity.MANIFEST_NAME).is_file()
    assert not (results / "glicko_ratings.json").exists()
    assert not (results / "elo_daemon_stats.json").exists()
    assert not (results / "daemon_stats.json").exists()


def test_archive_rotates_identity_bound_generation_snapshots(tmp_path, monkeypatch):
    import evidence_snapshot
    import evolution_infra

    results = tmp_path / "web" / "core" / "results"
    old_snapshot = results / "v143" / identity.GENERATION_EVIDENCE_DIR
    old_snapshot.mkdir(parents=True)
    (old_snapshot / "head_to_head.json").write_text('{"old": {}}', encoding="utf-8")
    (old_snapshot / "manifest.json").write_text(
        json.dumps({"available": True, "next_v": 143}),
        encoding="utf-8",
    )
    unrelated_log = results / "v143" / "logs" / "master_io.txt"
    unrelated_log.parent.mkdir(parents=True)
    unrelated_log.write_text("historical audit log", encoding="utf-8")
    (results / "head_to_head.json").write_text("{}\n", encoding="utf-8")

    migrated = identity.archive_and_initialize(
        results,
        reason="test evaluator and generation evidence migration",
    )

    archive = Path(migrated["archive_dir"])
    archived_snapshot = (
        archive / "generation_snapshots" / "v143" / identity.GENERATION_EVIDENCE_DIR
    )
    assert archived_snapshot.is_dir()
    assert (archived_snapshot / "manifest.json").is_file()
    assert "v143/evidence_snapshot" in migrated["moved"]
    assert not old_snapshot.exists()
    assert unrelated_log.read_text(encoding="utf-8") == "historical audit log"

    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "H2H_FILE", results / "head_to_head.json")
    from evaluation_bundle import publish_evaluation_cycle_manifest

    (results / "head_to_head.json").write_text("{}", encoding="utf-8")
    (results / "bot_stats.json").write_text("{}", encoding="utf-8")
    (results / "glicko_ratings.json").write_text("{}", encoding="utf-8")
    (results / "elo_daemon_stats.json").write_text(
        json.dumps({"total_games": 0, "pairs": {}}), encoding="utf-8"
    )
    (results / "selection_snapshot.json").write_text(
        json.dumps({
            "schema_version": 1,
            "save_num": 1,
            "daemon_run_id": "test-run",
            "active_bots": [],
            "rows": [],
            "rating_history_tail": [],
        }),
        encoding="utf-8",
    )
    (results / "match_history.jsonl").write_text("", encoding="utf-8")
    (results / "rating_history.jsonl").write_text("", encoding="utf-8")
    publish_evaluation_cycle_manifest(
        save_num=1,
        daemon_run_id="test-run",
        active_bots=[],
        results_dir=results,
        _test_only_allow_unleased=True,
    )
    recreated = evidence_snapshot.ensure_generation_h2h_snapshot(143)

    assert recreated["available"] is True
    assert recreated["reused"] is False
    assert recreated["schema_version"] == evidence_snapshot.SNAPSHOT_SCHEMA_VERSION
    assert recreated["evaluation_identity_digest"] == migrated["manifest"]["manifest_digest"]


def test_archive_rejects_unsafe_generation_snapshot_before_moving_ratings(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    ratings = results / "glicko_ratings.json"
    ratings.write_text('{"national_v1": {}}\n', encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (results / "v143").symlink_to(outside, target_is_directory=True)

    with pytest.raises(identity.EvaluationDataIdentityError, match="unsafe generation results"):
        identity.archive_and_initialize(results, reason="unsafe migration must fail closed")

    assert ratings.is_file()
    assert not (results / "archive").exists()


def test_archive_preflights_evaluator_imports_before_moving_ratings(tmp_path, monkeypatch):
    results = tmp_path / "results"
    results.mkdir()
    ratings = results / "glicko_ratings.json"
    ratings.write_text('{"national_v1": {}}\n', encoding="utf-8")

    def broken_identity():
        raise ModuleNotFoundError("sever")

    monkeypatch.setattr(identity, "base_evaluation_identity", broken_identity)

    with pytest.raises(ModuleNotFoundError, match="sever"):
        identity.archive_and_initialize(results, reason="preflight failure")

    assert ratings.is_file()
    assert not (results / "archive").exists()


def test_evaluation_identity_cli_imports_repo_packages_from_any_cwd(tmp_path):
    root = Path(__file__).resolve().parents[2]
    results = tmp_path / "results"
    script = root / "scripts" / "evaluation_data_identity.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--results-dir",
            str(results),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["base_identity"]["profile_id"].startswith(
        "national-native-rating-authority-v2"
    )
    assert (results / identity.MANIFEST_NAME).is_file()


def test_inline_native_eval_is_diagnostic_only(tmp_path, monkeypatch):
    import daemon_management
    import evolution_infra
    import national_native
    import tool_eval

    candidate = tmp_path / "bots" / "national_v143"
    opponent = tmp_path / "bots" / "national_v142"
    for path in (candidate, opponent):
        path.mkdir(parents=True)
        (path / "national_bot.py").write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda version: candidate if version == 143 else opponent)
    monkeypatch.setattr(tool_eval, "get_active_bots", lambda: ["national_v143", "national_v142"])
    monkeypatch.setattr(tool_eval, "load_ratings", lambda: {})
    monkeypatch.setattr(daemon_management, "daemon_proc", None)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(identity, "ROOT", Path(__file__).resolve().parents[2])

    class Result:
        def model_dump(self):
            return {"passed": True, "report": {"execution_mode": "native_tcp"}}

    async def fake_acceptance(*_args, **_kwargs):
        return Result()

    monkeypatch.setattr(national_native, "run_native_acceptance_for_candidate", fake_acceptance)
    monkeypatch.setattr(
        "workflow_profiles.get_workflow_profile",
        lambda: SimpleNamespace(national_execution_mode="native_tcp"),
    )

    response = asyncio.run(tool_eval.run_inline_eval.handler({"version": 143, "n_games": 5}))
    payload = json.loads(response["content"][0]["text"])

    assert payload["authoritative"] is False
    assert payload["ratings_updated"] is False
    assert payload["h2h_updated"] is False
    assert not (evolution_infra.RESULTS_DIR / "glicko_ratings.json").exists()
    assert not (evolution_infra.RESULTS_DIR / "head_to_head.json").exists()
