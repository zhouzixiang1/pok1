import asyncio
import json
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _eligible_test_parents(monkeypatch):
    import checkpoint_schema
    import tool_commit

    def resolve(label, **_kwargs):
        version = int(str(label).rsplit("_v", 1)[1])
        return SimpleNamespace(
            eligible=True,
            version=version,
            issues=(),
            runtime_manifest={"epoch": "national_tcp_policy_v1"},
            epoch_receipt={"epoch": "national_tcp_policy_v1", "version": version},
            publication_identity={"published": True, "version": version},
            certificate_digest="a" * 64,
        )

    monkeypatch.setattr(checkpoint_schema, "resolve_national_bot_spec", resolve)

    monkeypatch.setattr(
        tool_commit,
        "get_active_bots",
        lambda: ["national_v143", "national_v149"],
    )
    monkeypatch.setattr(
        tool_commit,
        "read_pipeline_checkpoint",
        lambda: {
            "next_v": 167,
            "source_v": 149,
            "parent2_v": 143,
            "stage": "selected",
        },
    )


def _tool_json(result):
    return json.loads(result["content"][0]["text"])


def test_run_crossover_rejects_parent_b_not_selected_by_checkpoint(monkeypatch):
    import tool_commit

    called = []

    async def must_not_synthesize(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("identity mismatch reached crossover synthesis")

    monkeypatch.setattr(tool_commit, "_run_crossover", must_not_synthesize)

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 149,
        "parent_b": 144,
        "target_v": 167,
    }))
    data = _tool_json(result)

    assert data["error"] == "CROSSOVER_CHECKPOINT_IDENTITY_MISMATCH"
    assert data["requested"]["parent2_v"] == 144
    assert data["checkpoint"]["parent2_v"] == 143
    assert called == []


def test_run_crossover_requires_scheduler_checkpoint(monkeypatch):
    import tool_commit

    monkeypatch.setattr(tool_commit, "read_pipeline_checkpoint", lambda: None)
    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 149,
        "parent_b": 143,
        "target_v": 167,
    }))
    data = _tool_json(result)

    assert data["error"] == "CROSSOVER_CHECKPOINT_MISSING"
    assert data["success"] is False


def test_run_crossover_cannot_rerun_after_prepared(monkeypatch):
    import tool_commit

    monkeypatch.setattr(
        tool_commit,
        "read_pipeline_checkpoint",
        lambda: {
            "next_v": 167,
            "source_v": 149,
            "parent2_v": 143,
            "stage": "prepared",
        },
    )

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 149,
        "parent_b": 143,
        "target_v": 167,
    }))
    data = _tool_json(result)

    assert data["error"] == "CROSSOVER_STAGE_BLOCKED"
    assert data["stage"] == "prepared"


def test_run_crossover_parent_capability_exception_uses_bounded_infrastructure(
    tmp_path,
    monkeypatch,
):
    import national_capability_contract
    import national_position_contract
    import tool_commit
    import workflow_profiles

    parent_a = tmp_path / "national_v149"
    parent_b = tmp_path / "national_v143"
    target = tmp_path / "national_v167"
    for root in (parent_a, parent_b):
        root.mkdir()
        (root / "policy.py").write_text("# parent\n", encoding="utf-8")
        (root / "national_bot.py").write_text("# native\n", encoding="utf-8")
        (root / ".completed").touch()

    def bot_dir(version):
        return {149: parent_a, 143: parent_b, 167: target}[int(version)]

    async def must_not_synthesize(*_args, **_kwargs):
        raise AssertionError("inconclusive parent capability reached synthesis")

    captured = {}

    async def record(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return tool_commit._json_tool_result({
            "error": "CROSSOVER_INFRASTRUCTURE_INCONCLUSIVE",
            "failure_class": "infrastructure",
            "success": False,
        })

    monkeypatch.setattr(tool_commit, "get_bot_dir", bot_dir)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(tool_commit, "git_dir_is_committed", lambda _v: False)
    monkeypatch.setattr(tool_commit, "_run_crossover", must_not_synthesize)
    monkeypatch.setattr(tool_commit, "_record_crossover_infrastructure", record)
    monkeypatch.setattr(
        workflow_profiles,
        "get_workflow_profile",
        lambda: SimpleNamespace(national_execution_mode="native_tcp"),
    )
    monkeypatch.setattr(
        national_position_contract,
        "detect_position_semantics_errors",
        lambda _path: [],
    )
    monkeypatch.setattr(
        national_capability_contract,
        "evaluate_national_capabilities",
        lambda _path: (_ for _ in ()).throw(RuntimeError("probe unavailable")),
    )

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 149,
        "parent_b": 143,
        "target_v": 167,
    }))
    data = _tool_json(result)

    assert data["error"] == "CROSSOVER_INFRASTRUCTURE_INCONCLUSIVE"
    assert captured["kwargs"]["component"] == "crossover_parent_capability_policy"
    assert captured["kwargs"]["metadata"]["pre_synthesis"] is True


def test_crossover_compatibility_uses_glicko_r_stable_h2h_and_architecture_context(
    tmp_path,
    monkeypatch,
):
    import audit_agents
    import evidence_snapshot
    import evolution_infra

    parent_a = tmp_path / "national_v149"
    parent_b = tmp_path / "national_v143"
    for path in (parent_a, parent_b):
        path.mkdir()
        (path / "policy.py").write_text(
            f"# {path.name} policy.py\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        audit_agents,
        "get_bot_dir",
        lambda version: parent_a if int(version) == 149 else parent_b,
    )
    monkeypatch.setattr(audit_agents, "get_logs_dir", lambda _version: tmp_path)
    monkeypatch.setattr(
        evolution_infra,
        "load_ratings",
        lambda: (_ for _ in ()).throw(
            AssertionError("compatibility audit must not reopen live ratings")
        ),
    )
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_evaluation_snapshot",
        lambda _target: {
            "available": True,
            "ratings": {
                "national_v149": {"r": 1600.0, "rd": 55.0},
                "national_v143": {"r": 1510.0, "rd": 70.0},
            },
            "h2h": {
                "national_v143 vs national_v149": {
                    "games": 120,
                    "a_wins": 52,
                    "b_wins": 68,
                    "draws": 0,
                }
            },
        },
    )
    captured = {}

    async def fake_query(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return json.dumps({
            "compatible": True,
            "compatibility_score": 8,
            "conflict_areas": [],
            "suggested_merge_approach": "Preserve the runtime and compose one policy component.",
            "files_to_take_from_a": ["policy.py"],
            "files_to_take_from_b": ["policy.py"],
        }), 0.0, {}

    monkeypatch.setattr(audit_agents, "run_claude_query", fake_query)

    result = asyncio.run(audit_agents._run_crossover_compatibility_audit(
        149,
        143,
        SimpleNamespace(),
        target_v=167,
        architecture_context={"selected_focus": "incremental_match_model"},
    ))

    assert result["compatible"] is True
    assert "1600.0 ± 55.0" in captured["prompt"]
    assert "games=120, a_wins=52, b_wins=68" in captured["prompt"]
    assert "incremental_match_model" in captured["prompt"]
    assert "See ratings above" not in captured["prompt"]


def test_run_crossover_llm_incompatibility_is_advisory_only(tmp_path, monkeypatch):
    import audit_agents
    import evolution_core
    import tool_bot_management
    import tool_commit

    parent_a_dir = tmp_path / "national_v149"
    parent_b_dir = tmp_path / "national_v143"
    target_dir = tmp_path / "national_v167"
    for path in (parent_a_dir, parent_b_dir):
        path.mkdir()
        (path / "policy.py").write_text("# parent\n", encoding="utf-8")
        (path / ".completed").touch()

    def _bot_dir(version):
        return {
            149: parent_a_dir,
            143: parent_b_dir,
            167: target_dir,
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
    synthesis = {}

    async def _crossover_runs(_a, _b, _target, _ui, **kwargs):
        synthesis.update(kwargs)
        target_dir.mkdir()
        (target_dir / "policy.py").write_text("# child\n", encoding="utf-8")
        return True

    monkeypatch.setattr(tool_commit, "_run_crossover", _crossover_runs)
    monkeypatch.setattr(audit_agents, "_run_crossover_compatibility_audit", _compat)
    import national_position_contract
    monkeypatch.setattr(
        national_position_contract,
        "detect_position_semantics_errors",
        lambda _path: [],
    )
    monkeypatch.setattr(tool_commit, "write_pipeline_checkpoint", lambda *_a, **_k: True)

    tool_bot_management._LAST_ABANDON_TS[0] = 0.0
    tool_bot_management._LAST_ABANDON_TS[1] = ""
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", fake_state)
    monkeypatch.setattr(
        tool_bot_management,
        "read_pipeline_checkpoint",
        lambda: {
            "next_v": 167,
            "source_v": 149,
            "parent2_v": 143,
            "run_id": "167#0",
            "workflow_run_id": "test-crossover-167-149",
            "checkpoint_revision": 1,
            "stage": "selected",
        },
    )
    monkeypatch.setattr(
        tool_bot_management,
        "clear_pipeline_checkpoint",
        lambda **_kwargs: cleared.append(True) or True,
    )
    monkeypatch.setattr(tool_bot_management, "get_bot_dir", _bot_dir)
    monkeypatch.setattr(tool_bot_management, "git_dir_is_committed", lambda _v: False)

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 149,
        "parent_b": 143,
        "target_v": 167,
    }))
    data = _tool_json(result)

    assert data["success"] is True
    assert synthesis["compatibility"]["compatible"] is False
    assert synthesis["compatibility"]["compatibility_score"] == 3
    assert cleared == []


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

    parent_a_dir = tmp_path / "national_v149"
    parent_b_dir = tmp_path / "national_v143"
    target_dir = tmp_path / "national_v167"
    for path in (parent_a_dir, parent_b_dir):
        path.mkdir()
        (path / "policy.py").write_text("# parent\n", encoding="utf-8")
        (path / ".completed").touch()

    def _bot_dir(version):
        return {
            149: parent_a_dir,
            143: parent_b_dir,
            167: target_dir,
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
        lambda: {
            "next_v": 167,
            "source_v": 149,
            "parent2_v": 143,
            "run_id": "167#0",
            "workflow_run_id": "test-crossover-exhausted-167-149",
            "checkpoint_revision": 1,
            "stage": "selected",
        },
    )
    monkeypatch.setattr(
        tool_bot_management,
        "clear_pipeline_checkpoint",
        lambda **_kwargs: cleared.append(True) or True,
    )
    monkeypatch.setattr(tool_bot_management, "get_bot_dir", _bot_dir)
    monkeypatch.setattr(tool_bot_management, "git_dir_is_committed", lambda _v: False)

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 149,
        "parent_b": 143,
        "target_v": 167,
    }))
    data = _tool_json(result)

    # Must surface a distinct, recognizable error token — NOT a bare
    # {"success": False} — so the orchestrator can abandon instead of looping.
    assert data["error"] == "CROSSOVER_LLM_EXHAUSTED"
    assert data["success"] is False
    assert data["abandoned"] is True
    assert data["abandon_result"]["abandoned_v"] == 167
    assert cleared == [True]


def test_run_crossover_concurrent_synthesis_is_retryable_without_checkpoint_mutation(
    tmp_path, monkeypatch
):
    import audit_agents
    import evolution_core
    import tool_bot_management
    import tool_commit

    parent_a = tmp_path / "national_v149"
    parent_b = tmp_path / "national_v143"
    target = tmp_path / "national_v167"
    for path in (parent_a, parent_b):
        path.mkdir()
        (path / "policy.py").write_text("# parent\n", encoding="utf-8")
        (path / ".completed").touch()

    def bot_dir(version):
        return {149: parent_a, 143: parent_b, 167: target}[int(version)]

    async def compatible(*_args, **_kwargs):
        return {"compatible": True, "compatibility_score": 8}

    async def busy(*_args, **_kwargs):
        return {
            "success": False,
            "outcome": "concurrent_effect_in_progress",
            "failure_class": "concurrency",
            "issue": "active_provider_lease:test",
        }

    checkpoint = {
        "next_v": 167,
        "source_v": 149,
        "parent2_v": 143,
        "run_id": "167#0",
        "workflow_run_id": "test-crossover-concurrent-167-149",
        "checkpoint_revision": 1,
        "stage": "selected",
    }
    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text("{}", encoding="utf-8")
    before = state_file.read_bytes()

    monkeypatch.setattr(tool_commit, "get_bot_dir", bot_dir)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(tool_commit, "git_dir_is_committed", lambda _v: False)
    monkeypatch.setattr(tool_commit, "_run_crossover", busy)
    monkeypatch.setattr(audit_agents, "_run_crossover_compatibility_audit", compatible)
    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", state_file)
    monkeypatch.setattr(
        tool_bot_management,
        "read_pipeline_checkpoint",
        lambda: checkpoint,
    )
    monkeypatch.setattr(tool_bot_management, "get_bot_dir", bot_dir)
    monkeypatch.setattr(tool_bot_management, "git_dir_is_committed", lambda _v: False)
    monkeypatch.setattr(
        tool_bot_management,
        "clear_pipeline_checkpoint",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("concurrent synthesis abandoned the generation")
        ),
    )
    monkeypatch.setattr(
        tool_commit,
        "write_pipeline_checkpoint",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("concurrent synthesis mutated the checkpoint")
        ),
    )

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 149,
        "parent_b": 143,
        "target_v": 167,
    }))
    data = _tool_json(result)

    assert data["error"] == "CROSSOVER_SYNTHESIS_IN_PROGRESS"
    assert data["retryable"] is True
    assert data["failure_class"] == "concurrency"
    assert state_file.read_bytes() == before


def test_run_crossover_records_prepare_scope_files(tmp_path, monkeypatch):
    import audit_agents
    import tool_commit

    parent_a_dir = tmp_path / "national_v149"
    parent_b_dir = tmp_path / "national_v143"
    target_dir = tmp_path / "national_v167"
    for path in (parent_a_dir, parent_b_dir):
        path.mkdir()
        (path / "policy.py").write_text("# parent\n", encoding="utf-8")
        (path / ".completed").touch()
    target_dir.mkdir()
    (target_dir / "policy.py").write_text("# child\n", encoding="utf-8")

    def _bot_dir(version):
        return {
            149: parent_a_dir,
            143: parent_b_dir,
            167: target_dir,
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
    monkeypatch.setattr(tool_commit, "_py_files_changed_between", lambda *_a: ["card_utils.py", "policy.py"])
    monkeypatch.setattr(tool_commit, "write_pipeline_checkpoint", _write_checkpoint)
    monkeypatch.setattr(audit_agents, "_run_crossover_compatibility_audit", _compat)

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 149,
        "parent_b": 143,
        "target_v": 167,
    }))
    data = _tool_json(result)

    assert data["success"] is True
    assert data["stage"] == "prepared"
    assert data["next_tool"] == "run_direction_audit"
    assert "only the recombination baseline" in data["directive"]
    assert captured["args"][:3] == (167, 149, "prepared")
    assert captured["kwargs"]["parent2_v"] == 143
    assert captured["kwargs"]["prepare_scope_files"] == ["card_utils.py", "policy.py"]
    assert "master_plan" not in captured["kwargs"]
    assert captured["kwargs"]["audit_context"]["crossover"]["baseline_prepared"] is True


def test_run_crossover_infrastructure_resume_reuses_preserved_child_without_llm(
    tmp_path,
    monkeypatch,
):
    import audit_agents
    import evidence_snapshot
    import national_capability_contract
    import national_position_contract
    import prepared_baseline_contract
    import runtime_architecture_policy
    import tool_commit
    import workflow_profiles
    from bot_artifact import hash_path
    from pipeline_infrastructure import build_infrastructure_failure

    parent_a_dir = tmp_path / "national_v149"
    parent_b_dir = tmp_path / "national_v143"
    target_dir = tmp_path / "national_v167"
    for path in (parent_a_dir, parent_b_dir):
        path.mkdir()
        (path / "policy.py").write_text("# parent\n", encoding="utf-8")
        (path / "national_bot.py").write_text("# native\n", encoding="utf-8")
        (path / ".completed").touch()
    target_dir.mkdir()
    (target_dir / "policy.py").write_text("# preserved crossover child\n", encoding="utf-8")
    (target_dir / "national_bot.py").write_text("# native\n", encoding="utf-8")

    def bot_dir(version):
        return {149: parent_a_dir, 143: parent_b_dir, 167: target_dir}[int(version)]

    capabilities = {
        "detector_version": "test",
        "checks": [],
        "checks_by_id": {},
        "required_failures": [],
        "infrastructure_failures": [],
        "outcome": "passed",
    }
    source_policy = {"policy_digest": "s" * 64, "selected_focus": None}
    prepared_policy = {"policy_digest": "p" * 64, "selected_focus": None}
    transition = {
        "ok": True,
        "outcome": "passed",
        "evaluation_phase": "preplan",
        "source_capabilities": capabilities,
        "candidate_capabilities": capabilities,
        "runtime_floor_failures": [],
        "regressions": [],
    }
    overlay = build_infrastructure_failure(
        None,
        component="national_runtime_probe",
        code="crossover_preplan_probe_inconclusive",
        owner_tool="run_crossover",
        resume_stage="crossover_running",
        attempt_key="same-candidate",
        issues=["probe unavailable"],
        metadata={
            "parent2_v": 143,
            "candidate_fingerprint": hash_path(target_dir),
            "source_fingerprint": hash_path(parent_a_dir),
            "parent2_fingerprint": hash_path(parent_b_dir),
        },
    )
    checkpoint = {
        "next_v": 167,
        "source_v": 149,
        "parent2_v": 143,
        "stage": "crossover_running",
        "infra_failure": overlay,
        "audit_context": {
            "crossover": {
                "compatibility": {"compatible": True, "compatibility_score": 8},
            },
        },
    }
    writes = []

    async def no_exhausted(*_args, **_kwargs):
        return None

    async def must_not_synthesize(*_args, **_kwargs):
        raise AssertionError("preserved child was sent through crossover LLM again")

    async def must_not_reaudit(*_args, **_kwargs):
        raise AssertionError("preserved compatibility receipt was ignored")

    def fake_policy(_source, **kwargs):
        return prepared_policy if kwargs.get("prepared_capability_snapshot") else source_policy

    monkeypatch.setattr(tool_commit, "get_bot_dir", bot_dir)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(tool_commit, "git_dir_is_committed", lambda _v: False)
    monkeypatch.setattr(tool_commit, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tool_commit, "_execute_exhausted_infrastructure_failure", no_exhausted)
    monkeypatch.setattr(tool_commit, "_run_crossover", must_not_synthesize)
    monkeypatch.setattr(tool_commit, "_py_files_changed_between", lambda *_a: ["policy.py"])
    monkeypatch.setattr(
        tool_commit,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        workflow_profiles,
        "get_workflow_profile",
        lambda: SimpleNamespace(national_execution_mode="native_tcp"),
    )
    monkeypatch.setattr(
        national_position_contract,
        "detect_position_semantics_errors",
        lambda _path: [],
    )
    monkeypatch.setattr(
        national_capability_contract,
        "evaluate_national_capabilities",
        lambda _path: capabilities,
    )
    monkeypatch.setattr(
        runtime_architecture_policy,
        "build_architecture_policy",
        fake_policy,
    )
    monkeypatch.setattr(
        runtime_architecture_policy,
        "evaluate_architecture_transition",
        lambda *_a, **_k: transition,
    )
    monkeypatch.setattr(
        runtime_architecture_policy,
        "build_prepared_capability_snapshot",
        lambda *_a, **_k: {"snapshot_digest": "c" * 64},
    )
    monkeypatch.setattr(
        prepared_baseline_contract,
        "build_prepared_baseline_contract",
        lambda *_a, **_k: {"contract_digest": "b" * 64},
    )
    monkeypatch.setattr(
        evidence_snapshot,
        "load_generation_snapshot_identity",
        lambda _v: {
            "available": True,
            "manifest_digest": "m" * 64,
            "sha256": "h" * 64,
        },
    )
    monkeypatch.setattr(
        audit_agents,
        "_run_crossover_compatibility_audit",
        must_not_reaudit,
    )

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 149,
        "parent_b": 143,
        "target_v": 167,
    }))
    data = _tool_json(result)

    assert data["success"] is True
    assert data["stage"] == "prepared"
    assert [call[0][2] for call in writes] == ["prepared"]
    assert writes[0][1]["clear_infra_failure"] is True
    assert writes[0][1]["infra_failure_owner"] == "run_crossover"
    assert writes[0][1]["audit_context"]["prepared_baseline_contract"] == {
        "contract_digest": "b" * 64,
    }
    assert (
        writes[0][1]["audit_context"]["crossover"]["prepared_architecture_policy"]
        == prepared_policy
    )


def test_run_crossover_infrastructure_resume_abandons_drifted_child_without_llm(
    tmp_path,
    monkeypatch,
):
    import national_position_contract
    import tool_bot_management
    import tool_commit
    from bot_artifact import hash_path
    from pipeline_infrastructure import build_infrastructure_failure

    parent_a_dir = tmp_path / "national_v149"
    parent_b_dir = tmp_path / "national_v143"
    target_dir = tmp_path / "national_v167"
    for path in (parent_a_dir, parent_b_dir):
        path.mkdir()
        (path / "policy.py").write_text("# parent\n", encoding="utf-8")
        (path / ".completed").touch()
    target_dir.mkdir()
    (target_dir / "policy.py").write_text("# preserved child\n", encoding="utf-8")

    captured_candidate = hash_path(target_dir)
    captured_source = hash_path(parent_a_dir)
    overlay = build_infrastructure_failure(
        None,
        component="national_runtime_probe",
        code="crossover_preplan_probe_inconclusive",
        owner_tool="run_crossover",
        resume_stage="crossover_running",
        attempt_key="preserved-child",
        issues=["probe unavailable"],
        metadata={
            "parent2_v": 143,
            "candidate_fingerprint": captured_candidate,
            "source_fingerprint": captured_source,
            "parent2_fingerprint": hash_path(parent_b_dir),
        },
    )
    checkpoint = {
        "next_v": 167,
        "source_v": 149,
        "parent2_v": 143,
        "stage": "crossover_running",
        "infra_failure": overlay,
    }
    # Simulate an edit while the infrastructure retry is paused.  The retry
    # must not treat the earlier provenance/runtime evidence as applying.
    (target_dir / "policy.py").write_text("# drifted child\n", encoding="utf-8")

    def bot_dir(version):
        return {149: parent_a_dir, 143: parent_b_dir, 167: target_dir}[int(version)]

    async def no_exhausted(*_args, **_kwargs):
        return None

    async def must_not_synthesize(*_args, **_kwargs):
        raise AssertionError("drifted preserved child reached crossover synthesis")

    async def abandon(*, reason):
        return {"abandoned": True, "reason": reason, "abandoned_v": 167}

    monkeypatch.setattr(tool_commit, "get_bot_dir", bot_dir)
    monkeypatch.setattr(tool_commit, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(tool_commit, "git_dir_is_committed", lambda _v: False)
    monkeypatch.setattr(
        tool_commit,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted,
    )
    monkeypatch.setattr(tool_commit, "_run_crossover", must_not_synthesize)
    monkeypatch.setattr(
        national_position_contract,
        "detect_position_semantics_errors",
        lambda _path: [],
    )
    monkeypatch.setattr(tool_bot_management, "_do_abandon_generation", abandon)

    result = asyncio.run(tool_commit.run_crossover.handler({
        "parent_a": 149,
        "parent_b": 143,
        "target_v": 167,
    }))
    data = _tool_json(result)

    assert data["error"] == "CROSSOVER_PRESERVED_CHILD_DRIFT"
    assert data["success"] is False
    assert data["abandoned"] is True
    assert data["failure_class"] == "integrity"


def test_crossover_infrastructure_helper_persists_owned_retry_overlay(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import tool_commit

    checkpoint_path = tmp_path / "pipeline_state.json"
    source = tmp_path / "national_v149"
    child = tmp_path / "national_v167"
    source.mkdir()
    child.mkdir()
    (source / "policy.py").write_text("SOURCE = True\n", encoding="utf-8")
    (child / "policy.py").write_text("CHILD = True\n", encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", checkpoint_path)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    assert evolution_infra.write_pipeline_checkpoint(
        167,
        149,
        "selected",
        parent2_v=143,
    )
    assert evolution_infra.write_pipeline_checkpoint(
        167,
        149,
        "crossover_running",
        parent2_v=143,
    )
    monkeypatch.setattr(
        tool_commit,
        "get_bot_dir",
        lambda version: source if int(version) == 149 else child,
    )

    result = asyncio.run(tool_commit._record_crossover_infrastructure(
        167,
        149,
        143,
        component="national_runtime_probe",
        code="crossover_preplan_probe_inconclusive",
        issues=["probe unavailable"],
        architecture_policy={"policy_digest": "p" * 64},
    ))
    data = _tool_json(result)
    checkpoint = evolution_infra.read_pipeline_checkpoint()

    assert data["failure_class"] == "infrastructure"
    assert data["action"] == "retry_same_tool"
    assert data["success"] is False
    assert checkpoint["stage"] == "crossover_running"
    assert checkpoint["infra_failure"]["owner_tool"] == "run_crossover"
    assert checkpoint["infra_failure"]["resume_stage"] == "crossover_running"


def test_crossover_infrastructure_budget_is_monotonic_across_components(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import tool_bot_management
    import tool_commit

    checkpoint_path = tmp_path / "pipeline_state.json"
    source = tmp_path / "national_v149"
    parent_b = tmp_path / "national_v143"
    child = tmp_path / "national_v167"
    for root, text in (
        (source, "SOURCE = True\n"),
        (parent_b, "PARENT_B = True\n"),
        (child, "CHILD = True\n"),
    ):
        root.mkdir()
        (root / "policy.py").write_text(text, encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", checkpoint_path)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    assert evolution_infra.write_pipeline_checkpoint(
        167,
        149,
        "selected",
        parent2_v=143,
    )
    assert evolution_infra.write_pipeline_checkpoint(
        167,
        149,
        "crossover_running",
        parent2_v=143,
    )
    monkeypatch.setattr(
        tool_commit,
        "get_bot_dir",
        lambda version: {149: source, 143: parent_b, 167: child}[int(version)],
    )
    abandon_calls = []

    async def abandon(*, reason):
        abandon_calls.append(reason)
        return {"abandoned": True, "reason": reason, "abandoned_v": 167}

    monkeypatch.setattr(tool_bot_management, "_do_abandon_generation", abandon)

    attempts = []
    for component in (
        "national_runtime_probe",
        "prepared_baseline_contract",
        "national_runtime_probe",
    ):
        result = asyncio.run(tool_commit._record_crossover_infrastructure(
            167,
            149,
            143,
            component=component,
            code=f"{component}_failed",
            issues=["temporarily unavailable"],
            architecture_policy={"policy_digest": "a" * 64},
        ))
        data = _tool_json(result)
        attempts.append(data["infra_failure"]["attempt"])

    assert attempts == [1, 2, 3]
    assert data["infra_failure"]["exhausted"] is True
    assert data["infra_failure"]["metadata"]["crossover_generation_attempt"] == 3
    assert data["abandoned"] is True
    assert abandon_calls == [
        "infrastructure_exhausted:national_runtime_probe",
    ]
