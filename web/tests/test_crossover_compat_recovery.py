import asyncio
import json
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _eligible_test_parents(monkeypatch):
    import tool_commit

    monkeypatch.setattr(
        tool_commit,
        "get_active_bots",
        lambda: ["national_v1", "national_v7"],
    )


def _tool_json(result):
    return json.loads(result["content"][0]["text"])


def test_crossover_compatibility_uses_glicko_r_stable_h2h_and_architecture_context(
    tmp_path,
    monkeypatch,
):
    import audit_agents
    import evidence_snapshot
    import evolution_infra

    parent_a = tmp_path / "national_v7"
    parent_b = tmp_path / "national_v1"
    for path in (parent_a, parent_b):
        path.mkdir()
        for name in ("strategy.py", "postflop.py", "constants.py"):
            (path / name).write_text(f"# {path.name} {name}\n", encoding="utf-8")

    monkeypatch.setattr(
        audit_agents,
        "get_bot_dir",
        lambda version: parent_a if int(version) == 7 else parent_b,
    )
    monkeypatch.setattr(audit_agents, "get_logs_dir", lambda _version: tmp_path)
    monkeypatch.setattr(
        evolution_infra,
        "load_ratings",
        lambda: {
            "national_v7": SimpleNamespace(r=1600.0, rd=55.0),
            "national_v1": SimpleNamespace(r=1510.0, rd=70.0),
        },
    )
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_h2h_snapshot",
        lambda _target: {
            "national_v1 vs national_v7": {
                "games": 120,
                "a_wins": 52,
                "b_wins": 68,
                "draws": 0,
            }
        },
    )
    captured = {}

    async def fake_query(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return json.dumps({
            "compatible": True,
            "compatibility_score": 8,
            "conflict_areas": [],
            "suggested_merge_approach": "Preserve parent A runtime and import one strategy helper.",
            "files_to_take_from_a": ["strategy.py"],
            "files_to_take_from_b": ["postflop.py"],
        }), 0.0, {}

    monkeypatch.setattr(audit_agents, "run_claude_query", fake_query)

    result = asyncio.run(audit_agents._run_crossover_compatibility_audit(
        7,
        1,
        SimpleNamespace(),
        target_v=25,
        architecture_context={"selected_focus": "incremental_match_model"},
    ))

    assert result["compatible"] is True
    assert "1600.0 ± 55.0" in captured["prompt"]
    assert "games=120, a_wins=52, b_wins=68" in captured["prompt"]
    assert "incremental_match_model" in captured["prompt"]
    assert "See ratings above" not in captured["prompt"]


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

    async def _compat(_parent_a, _parent_b, _ui, **_kwargs):
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


def test_run_crossover_llm_exhausted_abandons_generation(tmp_path, monkeypatch):
    # B1 (2026-07-09): when the crossover LLM retries are exhausted (repeated
    # idle timeouts / SDK stream stalls) WITHOUT a compatibility rejection,
    # run_crossover must abandon the generation and return a distinct
    # CROSSOVER_LLM_EXHAUSTED token. Previously it returned a bare
    # {"success": False} with no "error", which the orchestrator deterministic
    # router treated as "route done, re-enter loop" and re-routed to
    # run_crossover again — an infinite deadlock (~28 min/cycle, no progress).
    import audit_agents
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

    async def _compat(_parent_a, _parent_b, _ui, **_kwargs):
        # Compatibility passes; the failure is the LLM itself timing out.
        return {"compatible": True, "compatibility_score": 8}

    async def _crossover_returns_false(*_args, **_kwargs):
        # _run_crossover returns False when all MAX_CROSSOVER_RETRIES attempts
        # fail (e.g. each LLM call idle-timed out).
        return False

    fake_state = tmp_path / "pipeline_state.json"
    fake_state.write_text("{}", encoding="utf-8")
    cleared = []

    monkeypatch.setattr(tool_commit, "get_bot_dir", _bot_dir)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(tool_commit, "git_dir_is_committed", lambda _v: False)
    monkeypatch.setattr(tool_commit, "_run_crossover", _crossover_returns_false)
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

    # Must surface a distinct, recognizable error token — NOT a bare
    # {"success": False} — so the orchestrator can abandon instead of looping.
    assert data["error"] == "CROSSOVER_LLM_EXHAUSTED"
    assert data["success"] is False
    assert data["abandoned"] is True
    assert data["abandon_result"]["abandoned_v"] == 25
    assert cleared == [True]
    # The pair must NOT be recorded as incompatible (it wasn't a compatibility
    # failure, it was an LLM transport failure — the same pair may succeed later).
    import crossover_compat
    assert crossover_compat.is_crossover_pair_blocked(7, 1) is False


def test_run_crossover_records_prepare_scope_files(tmp_path, monkeypatch):
    import audit_agents
    import tool_commit

    parent_a_dir = tmp_path / "national_v7"
    parent_b_dir = tmp_path / "national_v1"
    target_dir = tmp_path / "national_v25"
    for path in (parent_a_dir, parent_b_dir):
        path.mkdir()
        (path / "main.py").write_text("# parent\n", encoding="utf-8")
        (path / ".completed").touch()
    target_dir.mkdir()
    (target_dir / "main.py").write_text("# child\n", encoding="utf-8")

    def _bot_dir(version):
        return {
            7: parent_a_dir,
            1: parent_b_dir,
            25: target_dir,
        }[int(version)]

    async def _compat(_parent_a, _parent_b, _ui, **_kwargs):
        return {"compatible": True, "compatibility_score": 8}

    async def _crossover_ok(*_args, **_kwargs):
        return True

    captured = {}

    def _write_checkpoint(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr(tool_commit, "get_bot_dir", _bot_dir)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(tool_commit, "_run_crossover", _crossover_ok)
    monkeypatch.setattr(tool_commit, "_py_files_changed_between", lambda *_a: ["card_utils.py", "state.py"])
    monkeypatch.setattr(tool_commit, "write_pipeline_checkpoint", _write_checkpoint)
    monkeypatch.setattr(audit_agents, "_run_crossover_compatibility_audit", _compat)

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 7,
        "parent_b": 1,
        "target_v": 25,
    }))
    data = _tool_json(result)

    assert data["success"] is True
    assert captured["args"][:3] == (25, 7, "workers_done")
    assert captured["kwargs"]["parent2_v"] == 1
    assert captured["kwargs"]["prepare_scope_files"] == ["card_utils.py", "state.py"]


def test_run_crossover_routes_position_semantics_failure_to_repair(tmp_path, monkeypatch):
    import audit_agents
    import tool_commit

    parent_a_dir = tmp_path / "national_v7"
    parent_b_dir = tmp_path / "national_v1"
    target_dir = tmp_path / "national_v25"
    for path in (parent_a_dir, parent_b_dir):
        path.mkdir()
        (path / "main.py").write_text("# parent\n", encoding="utf-8")
        (path / ".completed").touch()
    target_dir.mkdir()
    (target_dir / "main.py").write_text("# child\n", encoding="utf-8")
    (target_dir / "state.py").write_text(
        "def reconstruct_state(req):\n"
        "    dealer_id = req['dealer_id']\n"
        "    sb = next_player(dealer_id, 1)\n"
        "    bb = next_player(dealer_id, 2)\n"
        "    return {'sb': sb, 'bb': bb}\n",
        encoding="utf-8",
    )

    def _bot_dir(version):
        return {
            7: parent_a_dir,
            1: parent_b_dir,
            25: target_dir,
        }[int(version)]

    async def _compat(_parent_a, _parent_b, _ui, **_kwargs):
        return {"compatible": True, "compatibility_score": 8}

    async def _crossover_ok(*_args, **_kwargs):
        return True

    captured = {}

    def _write_checkpoint(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr(tool_commit, "get_bot_dir", _bot_dir)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(tool_commit, "git_dir_is_committed", lambda _v: False)
    monkeypatch.setattr(tool_commit, "_run_crossover", _crossover_ok)
    monkeypatch.setattr(tool_commit, "_py_files_changed_between", lambda *_a: ["state.py"])
    monkeypatch.setattr(tool_commit, "write_pipeline_checkpoint", _write_checkpoint)
    monkeypatch.setattr(audit_agents, "_run_crossover_compatibility_audit", _compat)

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 7,
        "parent_b": 1,
        "target_v": 25,
    }))
    data = _tool_json(result)

    assert data["success"] is True
    assert data["contract_failed"] is True
    assert data["stage"] == "quality_failed"
    assert captured["args"][:3] == (25, 7, "quality_failed")
    assert captured["kwargs"]["parent2_v"] == 1
    quality = captured["kwargs"]["gate_results"]["quality"]
    assert quality["all_passed"] is False
    assert quality["position_semantics_ok"] is False
    assert any("SB must be dealer_id" in err for err in quality["position_semantics_errors"])
