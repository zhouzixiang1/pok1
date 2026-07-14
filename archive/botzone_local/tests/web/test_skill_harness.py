import pytest

from decision_tester import audit_action_grounding
from skill_library import describe_skill_layers, scenario_skill_metadata, valid_skill_layers


def test_skill_library_describes_known_layers():
    layers = valid_skill_layers()
    assert "protocol" in layers
    assert "spr" in layers
    assert "bb_vs_limp" in layers
    assert "bb_vs_open" in layers
    assert "runtime_architecture" in layers
    assert "precompute" in layers
    assert "match_memory" in layers
    text = describe_skill_layers(["protocol"])
    assert "Botzone JSON contract" in text


def test_national_native_runtime_layers_are_schema_valid():
    from output_schema import WorkerTask
    from workflow_profiles import get_workflow_profile, profile_summary

    profile = get_workflow_profile("national_native")
    for layer in ("runtime_architecture", "precompute", "match_memory"):
        assert layer in profile.focus_skill_layers
        runtime_contract = {
            "decision": {
                "clock": "time.monotonic",
                "hard_deadline_ms": 55_000,
                "baseline_target_ms": 250,
                "refinement_budget_ms": 54_000,
                "baseline_path": "existing deterministic legal action",
                "fallback_action": "use existing legal fallback",
                "refinement_bound": "no full-history scan; max 64 samples",
                "max_samples": 64,
            },
            "precompute_artifacts": [{
                "name": "preflop_bucket_table",
                "owner_file": "strategy.py",
                "build_phase": "module_import",
                "max_build_ms": 500,
                "max_entries": 169,
                "max_bytes": 65536,
                "key_shape": "tuple[int,int,bool]",
                "consumer": "strategy.get_action",
                "fallback": "legal_baseline",
            }],
            "match_memory": {
                "tracker_class": "OpponentTracker",
                "owner_file": "national_bot.py",
                "reset_boundary": "tcp_connection",
                "update_events": ["hand_start", "opponent_action", "settlement", "showdown"],
                "snapshot_field": "opponent_runtime",
                "max_recent_hands": 8,
                "prior_rule": "beta_prior_weight_8",
                "confidence_rule": (
                    "global_actions_over_actions_plus_24_and_context_samples_over_samples_plus_8"
                ),
                "adaptation_cap": 0.65,
                "consumer": "strategy.get_action",
            },
            "official_feedback_refs": [],
            "forbidden_runtime_work": ["file_io_in_decision"],
        }
        task = WorkerTask(
            worker_id=1,
            role="Algorithmic Logic Architect",
            target_files=["strategy.py"],
            files_allowed=["strategy.py"],
            read_only_dependencies=["national_bot.py"],
            skill_layer=layer,
            worker_prompt=(
                "Implement a bounded decision budget with a legal fallback, fast baseline, "
                "and hard deadline; consume bounded precompute lookup data and incremental "
                "match memory with confidence through opponent_runtime."
            ),
            runtime_contract=runtime_contract,
        )
        assert task.skill_layer == layer
        assert layer in profile_summary(profile)


def test_runtime_layers_require_structured_runtime_contract():
    from pydantic import ValidationError
    from output_schema import WorkerTask

    with pytest.raises(ValidationError):
        WorkerTask(
            worker_id=1,
            role="Algorithmic Logic Architect",
            target_files=["national_bot.py"],
            skill_layer="runtime_architecture",
            worker_prompt="Implement a focused national runtime architecture change with checks.",
        )


def test_scenario_skill_metadata_reports_missing_fields():
    meta = scenario_skill_metadata({"id": "x", "skill_layer": "spr", "street": "river"})

    assert meta["skill_layer"] == "spr"
    assert "pot" in meta["missing_required_fields"]
    assert "to_call" in meta["missing_required_fields"]


def test_action_grounding_rejects_illegal_raise_bounds():
    scenario = {
        "legal_actions": ["call", "raise"],
        "raise_min": 200,
        "raise_max": 1000,
        "national_legal_expected": True,
    }

    failures = audit_action_grounding(150, "raise", scenario)

    assert failures[0] == "National legality expectation failed"
    assert "below raise_min" in failures[1]


def test_action_grounding_allows_legacy_scenario_without_metadata():
    assert audit_action_grounding(0, "call", {}) == []


def test_preflop_workflow_skill_layers_are_schema_valid():
    from output_schema import WorkerTask
    from workflow_profiles import get_workflow_profile

    profile = get_workflow_profile("preflop_range")
    for index, layer in enumerate(profile.focus_skill_layers, start=1):
        task = WorkerTask(
            worker_id=index,
            role="Algorithmic Logic Architect",
            target_files=["strategy.py"],
            skill_layer=layer,
            worker_prompt="Implement a focused preflop range change with tests.",
        )
        assert task.skill_layer == layer
