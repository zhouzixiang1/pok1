import asyncio
import json


def _tool_json(result):
    return json.loads(result["content"][0]["text"])


def test_crossover_incompatibility_cache_roundtrip():
    import crossover_compat

    record = crossover_compat.record_incompatible_crossover(
        7,
        1,
        target_v=25,
        compatibility={
            "compatible": False,
            "compatibility_score": 3,
            "conflict_areas": ["postflop import mismatch"],
            "suggested_merge_approach": "Select different parents.",
        },
    )

    assert record["blocked"] is True
    assert record["pair"] == [1, 7]
    assert crossover_compat.is_crossover_pair_blocked(7, 1) is True
    assert crossover_compat.is_crossover_pair_blocked(1, 7) is True
    assert crossover_compat.is_crossover_pair_blocked(7, 4) is False


def test_crossover_parent_selection_skips_blocked_pair(monkeypatch):
    import crossover_compat
    import generation_scheduler

    active = ["national_v7", "national_v1", "national_v4"]
    strength = {
        "national_v7": 0.90,
        "national_v1": 0.80,
        "national_v4": 0.70,
    }
    crossover_compat.record_incompatible_crossover(
        7,
        1,
        target_v=25,
        compatibility={"compatible": False, "compatibility_score": 3},
    )

    monkeypatch.setattr("evolution_infra.get_active_bots", lambda: active)
    monkeypatch.setattr("tool_helpers.load_selection_scores", lambda: strength)
    monkeypatch.setattr("candidate_store.count_candidate_children", lambda *_a, **_k: 0)
    monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *a, **k: None)

    assert generation_scheduler._pick_crossover_parents({}, current_v=7) == (7, 4)


def test_crossover_parent_selection_prefers_next_gap_pair_over_adjacent_fallback(monkeypatch):
    import crossover_compat
    import generation_scheduler

    active = ["national_v7", "national_v6", "national_v3"]
    strength = {
        "national_v7": 0.90,
        "national_v6": 0.80,
        "national_v3": 0.70,
    }
    crossover_compat.record_incompatible_crossover(
        7,
        3,
        target_v=25,
        compatibility={"compatible": False, "compatibility_score": 3},
    )

    monkeypatch.setattr("evolution_infra.get_active_bots", lambda: active)
    monkeypatch.setattr("tool_helpers.load_selection_scores", lambda: strength)
    monkeypatch.setattr("candidate_store.count_candidate_children", lambda *_a, **_k: 0)
    monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *a, **k: None)

    assert generation_scheduler._pick_crossover_parents({}, current_v=7) == (6, 3)


def test_run_crossover_incompatible_pair_records_and_abandons(tmp_path, monkeypatch):
    import audit_agents
    import crossover_compat
    import evolution_core
    import tool_bot_management
    import tool_commit

    parent_a_dir = tmp_path / "national_v7"
    parent_b_dir = tmp_path / "national_v1"
    target_dir = tmp_path / "national_v25"
    for path in (parent_a_dir, parent_b_dir):
        path.mkdir()
        (path / "main.py").write_text("# parent\n", encoding="utf-8")
        (path / ".completed").touch()

    def _bot_dir(version):
        return {
            7: parent_a_dir,
            1: parent_b_dir,
            25: target_dir,
        }[int(version)]

    async def _compat(_parent_a, _parent_b, _ui):
        return {
            "compatible": False,
            "compatibility_score": 3,
            "conflict_areas": ["postflop import mismatch", "constants mismatch"],
            "suggested_merge_approach": "Select different parents.",
        }

    fake_state = tmp_path / "pipeline_state.json"
    fake_state.write_text("{}", encoding="utf-8")
    cleared = []

    monkeypatch.setattr(tool_commit, "get_bot_dir", _bot_dir)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(tool_commit, "git_dir_is_committed", lambda _v: False)
    async def _crossover_must_not_run(*_args, **_kwargs):
        raise AssertionError("crossover synthesis must not run")

    monkeypatch.setattr(tool_commit, "_run_crossover", _crossover_must_not_run)
    monkeypatch.setattr(audit_agents, "_run_crossover_compatibility_audit", _compat)

    tool_bot_management._LAST_ABANDON_TS[0] = 0.0
    tool_bot_management._LAST_ABANDON_TS[1] = ""
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
    monkeypatch.setattr(
        tool_bot_management,
        "read_pipeline_checkpoint",
        lambda: {"next_v": 25, "source_v": 7, "parent2_v": 1, "stage": "selected"},
    )
    monkeypatch.setattr(tool_bot_management, "clear_pipeline_checkpoint", lambda: cleared.append(True))
    monkeypatch.setattr(tool_bot_management, "get_bot_dir", _bot_dir)
    monkeypatch.setattr(tool_bot_management, "git_dir_is_committed", lambda _v: False)

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 7,
        "parent_b": 1,
        "target_v": 25,
    }))
    data = _tool_json(result)

    assert data["error"] == "CROSSOVER_INCOMPATIBLE"
    assert data["success"] is False
    assert data["abandoned"] is True
    assert data["abandon_result"]["abandoned_v"] == 25
    assert cleared == [True]
    assert crossover_compat.is_crossover_pair_blocked(7, 1) is True
