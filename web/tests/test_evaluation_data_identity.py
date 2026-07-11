import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import evaluation_data_identity as identity


def test_empty_results_initialize_content_bound_manifest(tmp_path):
    results = tmp_path / "results"

    manifest = identity.ensure_evaluation_data_identity(results)
    loaded = json.loads((results / identity.MANIFEST_NAME).read_text(encoding="utf-8"))

    assert manifest == loaded
    assert manifest["base_identity"]["authority"] == "rating_daemon_only"
    assert manifest["base_identity"]["official_exe_strength_weight"] == 0
    assert len(manifest["manifest_digest"]) == 64


def test_existing_unidentified_ratings_fail_closed(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "glicko_ratings.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(identity.EvaluationDataIdentityError, match="no evaluation identity"):
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

    migrated = identity.archive_and_initialize(results, reason="test evaluator migration")

    archive = Path(migrated["archive_dir"])
    assert (archive / "glicko_ratings.json").is_file()
    assert (archive / "head_to_head.json").is_file()
    assert (archive / "migration.json").is_file()
    assert (results / identity.MANIFEST_NAME).is_file()
    assert not (results / "glicko_ratings.json").exists()


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
