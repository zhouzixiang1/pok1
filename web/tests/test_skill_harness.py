from decision_tester import audit_action_grounding
from skill_library import describe_skill_layers, scenario_skill_metadata, valid_skill_layers


def test_skill_library_describes_known_layers():
    layers = valid_skill_layers()
    assert "protocol" in layers
    assert "spr" in layers
    assert "bb_vs_limp" in layers
    assert "bb_vs_open" in layers
    text = describe_skill_layers(["protocol"])
    assert "Botzone JSON contract" in text


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
