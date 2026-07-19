"""Regression coverage for the executable Master plan contract."""

from copy import deepcopy
import asyncio
import hashlib
import json
from pathlib import Path

from output_schema import (
    MASTER_PLAN_MAX_TASKS,
    MATCH_MEMORY_ALLOWED_UPDATE_EVENTS,
    MATCH_MEMORY_REQUIRED_UPDATE_EVENTS,
    PRECOMPUTE_BUILD_PHASES,
    RUNTIME_CONTRACT_WORKER_PROMPT_TERMS,
    RuntimeContract,
    WORKER_PROMPT_MAX_CHARS,
    WORKER_TASK_MAX_TARGET_FILES,
    MasterPlan,
    master_plan_executable_contract_text,
    runtime_contract_worker_prompt_terms,
    validate_agent_output,
)
from tool_planning import _validate_master_plan


ROOT = Path(__file__).resolve().parents[2]


def _runtime_contract(
    *,
    decision: bool = False,
    precompute: bool = False,
    memory: bool = False,
) -> dict:
    from runtime_architecture_policy import native_policy_runtime_contract

    decision_contract = None
    if decision:
        decision_contract = {
            "clock": "time.monotonic",
            "hard_deadline_ms": 55000,
            "baseline_target_ms": 250,
            "refinement_budget_ms": 54000,
            "baseline_path": "compute the existing deterministic legal action",
            "fallback_action": "return the sanitized legal baseline",
            "refinement_bound": "at most sixty-four refinement samples",
            "max_samples": 64,
        }
    artifacts = []
    if precompute:
        artifacts.append({
            "name": "equity_lookup",
            "owner_file": "precompute.py",
            "build_phase": "module_import",
            "max_build_ms": 500,
            "max_entries": 4096,
            "max_bytes": 262144,
            "key_shape": "tuple[int,int,bool]",
            "consumer": "policy.get_baseline_decision",
            "fallback": "legal_baseline",
        })
    match_memory = None
    if memory:
        match_memory = {
            "tracker_class": "OpponentTracker",
            "owner_file": "national_bot.py",
            "reset_boundary": "tcp_connection",
            "update_events": [
                "hand_start",
                "opponent_action",
                "settlement",
                "showdown",
            ],
            "snapshot_field": "opponent",
            "max_recent_hands": 8,
            "prior_rule": "beta_prior_weight_8",
            "confidence_rule": (
                "global_actions_over_actions_plus_24_and_context_samples_over_samples_plus_8"
            ),
            "adaptation_cap": 0.65,
            "consumer": "policy.get_baseline_decision",
        }
    return {
        "policy_abi": (
            native_policy_runtime_contract()["policy_abi"]
            if decision or precompute
            else None
        ),
        "decision": decision_contract,
        "precompute_artifacts": artifacts,
        "match_memory": match_memory,
        "official_feedback_refs": [],
        "forbidden_runtime_work": ["full-history scans in get_action"],
    }


def _invalid_multifile_v143_plan() -> dict:
    return {
        "analysis": "Replace hot-path table construction and add bounded match tracking.",
        "targeted_failure": "Runtime architecture debt blocks bounded decisions.",
        "expected_behavior_change": "Decisions consume bounded precompute and opponent state.",
        "do_not_touch": ["sever/engine/validator.py"],
        "measurement_plan": "Run capability, decision, and native precommit gates.",
        "tasks": [
            {
                "worker_id": 1,
                "role": "Algorithmic Logic Architect",
                "target_files": [
                    "policy.py",
                    "precompute.py",
                    "card_utils.py",
                    "simulation.py",
                ],
                "skill_layer": "precompute",
                "files_allowed": ["policy.py"],
                "read_only_dependencies": ["precompute.py"],
                "worker_prompt": (
                    "Build bounded module-import precompute data in precompute.py and "
                    "consume it from the legal policy path with decision_context, "
                    "typed intent, raise_to, pass, and precompute."
                ),
                "runtime_contract": _runtime_contract(precompute=True),
            },
            {
                "worker_id": 2,
                "role": "Opponent Modeler",
                "target_files": ["national_bot.py", "policy.py"],
                "skill_layer": "opponent_model",
                "files_allowed": ["national_bot.py", "policy.py"],
                "worker_prompt": (
                    "Incrementally update the tracker, compute confidence, and pass "
                    "opponent evidence into policy.get_baseline_decision."
                ),
                "runtime_contract": _runtime_contract(memory=True),
            },
        ],
    }


def test_executable_contract_is_rendered_from_schema_sources():
    text = master_plan_executable_contract_text()

    assert f"tasks: 1..{MASTER_PLAN_MAX_TASKS} items" in text
    assert 'each task writable scope is exactly ["policy.py"]' in text
    assert f"task.worker_prompt: 20..{WORKER_PROMPT_MAX_CHARS} characters" in text
    assert f'build_phase="{PRECOMPUTE_BUILD_PHASES[0]}"' in text
    for event in MATCH_MEMORY_ALLOWED_UPDATE_EVENTS:
        assert f'"{event}"' in text
    for event in MATCH_MEMORY_REQUIRED_UPDATE_EVENTS:
        assert f'"{event}"' in text
    for section, terms in RUNTIME_CONTRACT_WORKER_PROMPT_TERMS.items():
        assert f"runtime_contract.{section}" in text
        for term in terms:
            assert f'"{term}"' in text

    schema = MasterPlan.model_json_schema()
    assert schema["properties"]["tasks"]["maxItems"] == MASTER_PLAN_MAX_TASKS
    worker_schema = schema["$defs"]["WorkerTask"]
    assert (
        worker_schema["properties"]["target_files"]["maxItems"]
        == WORKER_TASK_MAX_TARGET_FILES
    )
    assert (
        worker_schema["properties"]["worker_prompt"]["maxLength"]
        == WORKER_PROMPT_MAX_CHARS
    )


def test_static_master_json_example_remains_schema_valid():
    prompt = (ROOT / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    example = json.loads(prompt[start:end])

    validated = MasterPlan.model_validate(example)
    assert validated.tasks[0].runtime_contract is not None


def test_system_binding_materializes_card_terms_before_strict_validation():
    import plan_compiler

    prompt = (ROOT / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    plan = json.loads(prompt[start:end])
    plan["tasks"][0]["worker_prompt"] = (
        "Implement the selected structured candidate refinement in policy.py "
        "with the declared tests and no unrelated edits."
    )
    original = deepcopy(plan)

    _unchanged, before_errors = validate_agent_output("master", plan)
    assert any("strategy_reference_pack_worker_terms_missing" in error for error in before_errors)

    bound, meta = plan_compiler.bind_system_owned_worker_contract_terms(plan)

    assert plan == original
    assert meta["bound"] is True
    assert meta["invalid_contract_tasks"] == []
    assert meta["invalid_prompt_tasks"] == []
    assert meta["overflow_tasks"] == []
    contract = RuntimeContract.model_validate(plan["tasks"][0]["runtime_contract"])
    bound_prompt = bound["tasks"][0]["worker_prompt"].lower()
    for term in runtime_contract_worker_prompt_terms(contract):
        assert term.lower() in bound_prompt
    assert plan_compiler.SYSTEM_OWNED_CONTRACT_HEADER.lower() in bound_prompt

    validated, after_errors = validate_agent_output("master", bound)
    assert after_errors == []
    assert validated["tasks"][0]["runtime_contract"]["reference_pack_id"] == (
        "range_weighted_candidate_batch_v1"
    )

    rebound, rebound_meta = plan_compiler.bind_system_owned_worker_contract_terms(bound)
    assert rebound == bound
    assert rebound_meta["bound"] is False
    assert rebound["tasks"][0]["worker_prompt"].count(
        plan_compiler.SYSTEM_OWNED_CONTRACT_HEADER
    ) == 1


def test_system_binding_never_repairs_invalid_contract_or_card_selection():
    import plan_compiler

    prompt = (ROOT / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    valid = json.loads(prompt[start:end])

    invalid_plans = []
    invalid_enum = deepcopy(valid)
    invalid_enum["tasks"][0]["runtime_contract"]["state_learning"]["work_primitive"] = []
    invalid_plans.append(invalid_enum)
    mismatched_card = deepcopy(valid)
    mismatched_card["tasks"][0]["runtime_contract"]["reference_pack_id"] = (
        "lead_sizing_geometry_v1"
    )
    invalid_plans.append(mismatched_card)

    for plan in invalid_plans:
        original_prompt = plan["tasks"][0]["worker_prompt"]
        bound, meta = plan_compiler.bind_system_owned_worker_contract_terms(plan)
        assert meta["bound"] is False
        assert meta["invalid_contract_tasks"]
        assert bound["tasks"][0]["worker_prompt"] == original_prompt
        _unchanged, errors = validate_agent_output("master", bound)
        assert errors


def test_system_binding_never_creates_or_pads_a_missing_worker_brief():
    import plan_compiler

    prompt = (ROOT / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    valid = json.loads(prompt[start:end])

    for invalid_prompt in (None, "too short"):
        plan = deepcopy(valid)
        plan["tasks"][0]["worker_prompt"] = invalid_prompt
        bound, meta = plan_compiler.bind_system_owned_worker_contract_terms(plan)
        assert meta["bound"] is False
        assert meta["invalid_prompt_tasks"]
        assert bound["tasks"][0]["worker_prompt"] == invalid_prompt
        _unchanged, errors = validate_agent_output("master", bound)
        assert errors


def test_system_binding_rejects_unclosed_provider_system_marker():
    import plan_compiler

    prompt = (ROOT / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    plan = json.loads(prompt[start:end])
    original = (
        "Implement the selected structured policy mechanism and its typed checks.\n"
        + plan_compiler.SYSTEM_OWNED_CONTRACT_BEGIN
    )
    plan["tasks"][0]["worker_prompt"] = original

    bound, meta = plan_compiler.bind_system_owned_worker_contract_terms(plan)

    assert bound["tasks"][0]["worker_prompt"] == original
    assert meta["bound"] is False
    assert meta["invalid_prompt_tasks"] == [{
        "worker_id": plan["tasks"][0]["worker_id"],
        "reason": "worker_prompt_reserved_system_marker",
        "reserved_markers": [plan_compiler.SYSTEM_OWNED_CONTRACT_BEGIN],
        "original_chars": len(original),
    }]


def test_system_binding_replaces_one_block_when_policy_adds_focus_terms():
    import plan_compiler

    prompt = (ROOT / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    plan = json.loads(prompt[start:end])

    first, first_meta = plan_compiler.bind_system_owned_worker_contract_terms(plan)
    assert first_meta["bound"] is True
    first["tasks"][0]["architecture_focus_id"] = "national_runtime_v4_state_learning"
    first["architecture_policy"] = {
        "selected_focus": {
            "focus_id": "national_runtime_v4_state_learning",
            "required_terms": ["posterior control", "trusted iterator"],
        }
    }
    second, second_meta = plan_compiler.bind_system_owned_worker_contract_terms(first)
    second_prompt = second["tasks"][0]["worker_prompt"]

    assert second_meta["bound"] is True
    assert second_meta["bound_tasks"][0]["replaced_blocks"] == 1
    assert second_prompt.count(plan_compiler.SYSTEM_OWNED_CONTRACT_BEGIN) == 1
    assert second_prompt.count(plan_compiler.SYSTEM_OWNED_CONTRACT_END) == 1
    assert "posterior control" in second_prompt
    assert "trusted iterator" in second_prompt

    third, third_meta = plan_compiler.bind_system_owned_worker_contract_terms(second)
    assert third == second
    assert third_meta["bound"] is False


def test_system_binding_does_not_truncate_prompt_to_hide_overflow():
    import plan_compiler

    prompt = (ROOT / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    plan = json.loads(prompt[start:end])
    original_prompt = "x" * (WORKER_PROMPT_MAX_CHARS - 10)
    plan["tasks"][0]["worker_prompt"] = original_prompt

    bound, meta = plan_compiler.bind_system_owned_worker_contract_terms(plan)

    assert meta["bound"] is False
    assert meta["overflow_tasks"]
    assert bound["tasks"][0]["worker_prompt"] == original_prompt
    _unchanged, errors = validate_agent_output("master", bound)
    assert any("required execution term" in error or "worker_terms_missing" in error for error in errors)


def test_master_reference_summary_exposes_every_card_literal_contract():
    from strategy_reference_pack import (
        get_reference_card,
        master_reference_summary,
        reference_pack_ids,
        worker_reference_card,
    )

    summary = master_reference_summary().lower()
    assert "required worker literals" in summary
    for reference_id in reference_pack_ids():
        card = get_reference_card(reference_id)
        assert card is not None
        assert reference_id.lower() in summary
        worker_card = worker_reference_card(reference_id).lower()
        for term in card.required_worker_terms:
            assert term.lower() in summary
            assert term.lower() in worker_card


def test_master_reference_summary_filters_closed_axes_and_fails_closed_for_none():
    from strategy_reference_pack import master_reference_summary

    action_profile = master_reference_summary(
        allowed_primaries=("action_profile",),
    ).lower()
    assert "action_profile_confidence_v1" in action_profile
    assert "opponent.terminal_response.fold_to_raise" not in action_profile
    assert "opponent.showdown_range.bucket_rates" not in action_profile
    assert "allowed_primaries=['action_profile']" in action_profile

    unavailable = master_reference_summary(
        allowed_primaries=("not_a_real_primary",),
    ).lower()
    assert "no compatible reference card exists" in unavailable
    assert "action_profile_confidence_v1" not in unavailable


def test_checkpoint_worker_authority_binds_tasks_to_runtime_ledger():
    import tool_planning
    from runtime_architecture_policy import attach_runtime_contract_ledger

    prompt = (ROOT / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    plan = attach_runtime_contract_ledger(json.loads(prompt[start:end]), replace=True)
    checkpoint = {
        "stage": "master_planned",
        "master_plan": deepcopy(plan),
        "runtime_contract_ledger": deepcopy(plan["runtime_contract_ledger"]),
    }

    assert tool_planning._checkpoint_master_task_authority_errors(
        checkpoint,
        checkpoint["master_plan"]["tasks"],
    ) == []

    tampered_tasks = deepcopy(checkpoint["master_plan"]["tasks"])
    tampered_tasks[0]["runtime_contract"]["decision"]["max_samples"] = 32
    errors = tool_planning._checkpoint_master_task_authority_errors(
        checkpoint,
        tampered_tasks,
    )
    assert "master_tasks_runtime_contract_ledger_mismatch" in errors

    tampered_checkpoint = deepcopy(checkpoint)
    tampered_checkpoint["runtime_contract_ledger"]["ledger_digest"] = "0" * 64
    errors = tool_planning._checkpoint_master_task_authority_errors(
        tampered_checkpoint,
        tampered_checkpoint["master_plan"]["tasks"],
    )
    assert any(error.startswith("checkpoint:runtime_contract_ledger_digest_mismatch") for error in errors)


def test_execute_workers_rejects_caller_rewritten_initial_task_before_llm(
    tmp_path,
    monkeypatch,
):
    from prepared_baseline_contract import build_prepared_artifact_contract
    import tool_planning

    source = tmp_path / "national_v143"
    candidate = tmp_path / "national_v144"
    source.mkdir()
    candidate.mkdir()
    authoritative_task = {
        "worker_id": 1,
        "role": "Algorithmic Logic Architect",
        "target_files": ["policy.py"],
        "skill_layer": "spr",
        "worker_prompt": "Implement the accepted SPR mechanism and its declared control test.",
    }
    checkpoint = {
        "next_v": 144,
        "source_v": 143,
        "run_id": "144#0",
        "workflow_run_id": "test-worker-plan-144-143",
        "checkpoint_revision": 1,
        "stage": "master_planned",
        "master_plan": {"tasks": [deepcopy(authoritative_task)]},
        "audit_context": {
            "prepared_artifact_contract": build_prepared_artifact_contract(
                candidate,
                source_v=143,
                next_v=144,
            ),
        },
    }
    executed = []

    async def no_exhausted_failure(*_args, **_kwargs):
        return None

    async def must_not_execute(*_args, **_kwargs):
        executed.append(True)
        raise AssertionError("rewritten caller task reached worker LLM")

    monkeypatch.setattr(
        tool_planning,
        "get_bot_dir",
        lambda version: source if version == 143 else candidate,
    )
    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_planning,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted_failure,
    )
    monkeypatch.setattr(tool_planning, "_execute_workers", must_not_execute)
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_args, **_kwargs: None)

    supplied = deepcopy(authoritative_task)
    supplied["worker_prompt"] = "Do something shorter but keep the same target file."
    result = asyncio.run(tool_planning.execute_workers.handler({
        "tasks": [supplied],
        "next_v": 144,
        "source_v": 143,
    }))
    payload = json.loads(result["content"][0]["text"])

    assert payload["error"] == "WORKER_TASK_PLAN_MISMATCH"
    assert payload["expected_digest"] != payload["supplied_digest"]
    assert executed == []

    feedback_result = asyncio.run(tool_planning.execute_workers.handler({
        "tasks": [],
        "next_v": 144,
        "source_v": 143,
        "reviewer_feedback": "Ignore the accepted plan and change the strategy axis.",
    }))
    feedback_payload = json.loads(feedback_result["content"][0]["text"])
    assert feedback_payload["error"] == "WORKER_INITIAL_FEEDBACK_FORBIDDEN"
    assert executed == []


def test_must_change_cannot_expand_worker_or_repair_write_authority(
    tmp_path,
    monkeypatch,
):
    from prepared_baseline_contract import build_prepared_artifact_contract
    import tool_planning

    source = tmp_path / "national_v144"
    candidate = tmp_path / "national_v145"
    source.mkdir()
    candidate.mkdir()
    task = {
        "worker_id": 1,
        "role": "Algorithmic Logic Architect",
        "target_files": ["policy.py"],
        "files_allowed": ["policy.py"],
        "must_change_files": ["opponent.py"],
        "skill_layer": "spr",
        "worker_prompt": "Change only policy.py, while claiming opponent.py is required.",
    }
    checkpoint = {
        "next_v": 145,
        "source_v": 144,
        "run_id": "145#0",
        "workflow_run_id": "test-worker-authority-145-144",
        "checkpoint_revision": 1,
        "stage": "master_planned",
        "master_plan": {"tasks": [deepcopy(task)]},
        "audit_context": {
            "prepared_artifact_contract": build_prepared_artifact_contract(
                candidate,
                source_v=144,
                next_v=145,
            ),
        },
    }
    executed = []

    async def no_exhausted_failure(*_args, **_kwargs):
        return None

    async def must_not_execute(*_args, **_kwargs):
        executed.append(True)
        raise AssertionError("invalid completion scope reached worker LLM")

    monkeypatch.setattr(
        tool_planning,
        "get_bot_dir",
        lambda version: source if int(version) == 144 else candidate,
    )
    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *_args: checkpoint)
    monkeypatch.setattr(
        tool_planning,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted_failure,
    )
    monkeypatch.setattr(tool_planning, "_execute_workers", must_not_execute)
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_args, **_kwargs: None)

    assert tool_planning._plan_repair_scope_files(
        checkpoint["master_plan"],
        145,
    ) == {"policy.py"}
    result = asyncio.run(tool_planning.execute_workers.handler({
        "tasks": [deepcopy(task)],
        "next_v": 145,
        "source_v": 144,
    }))
    payload = json.loads(result["content"][0]["text"])

    assert payload["error"] == "WORKER_TASK_AUTHORITY_INVALID"
    assert any(
        "must_change_outside_writable_scope:['opponent.py']" in error
        for error in payload["validation_errors"]
    )
    assert executed == []


def _prompt_plan() -> dict:
    prompt = (ROOT / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    return json.loads(prompt[start:end])


def test_v143_multifile_contract_is_rejected_by_both_validation_layers():
    plan = _invalid_multifile_v143_plan()

    _unchanged, schema_errors = validate_agent_output("master", plan)
    assert any("at most 3" in error for error in schema_errors)
    assert any("writable scope must be exactly ['policy.py']" in error for error in schema_errors)

    semantic_errors, _warnings = _validate_master_plan(plan)
    assert any("too many target_files" in error for error in semantic_errors)
    assert any("policy.py" in error and "writable" in error for error in semantic_errors)


def test_current_policy_only_plan_passes_both_validation_layers():
    plan = _prompt_plan()

    validated, schema_errors = validate_agent_output("master", deepcopy(plan))
    assert schema_errors == []
    semantic_errors, _warnings = _validate_master_plan(validated)
    assert semantic_errors == []
    assert validated["tasks"][0]["target_files"] == ["policy.py"]


def test_system_runtime_owner_is_read_only_and_cannot_expand_write_scope():
    plan = _prompt_plan()
    MasterPlan.model_validate(plan)

    task = plan["tasks"][0]
    task["read_only_dependencies"] = ["precompute.py"]
    task["files_allowed"] = ["policy.py", "national_bot.py"]
    try:
        MasterPlan.model_validate(plan)
    except ValueError as exc:
        assert "writable scope must be exactly ['policy.py']" in str(exc)
    else:
        raise AssertionError("system-owned national_bot.py became writable")


def test_master_plan_rejects_two_generation_state_learning_primaries():
    plan = _prompt_plan()
    second = deepcopy(plan["tasks"][0])
    second["worker_id"] = 2
    second["runtime_contract"]["state_learning"] = {
        "work_primitive": None,
        "profile_dimensions": [],
        "line_controls": ["delayed_probe"],
        "oracle_refs": [
            "docs/official-raise-boundary-oracle-2026-07-11.md",
            "docs/official-terminal-settlement-oracle-2026-07-11.md",
            "docs/official-allin-runout-wire-oracle-2026-07-19.md",
        ],
    }
    second["runtime_contract"]["reference_pack_id"] = ""
    second["checks_required"] = ["delayed_probe_line_reachability"]
    second["worker_prompt"] += (
        " Consume decision_context line.can_delayed_probe with a positive/control "
        "typed intent difference and telemetry."
    )
    plan["tasks"].append(second)

    try:
        MasterPlan.model_validate(plan)
    except ValueError as exc:
        assert "exactly one state_learning primary is allowed" in str(exc)
    else:
        raise AssertionError("two generation-level primaries were accepted")


def test_master_plan_rejects_writable_system_native_entrypoint():
    plan = _prompt_plan()
    task = plan["tasks"][0]
    task["target_files"] = ["policy.py", "national_bot.py"]
    task["files_allowed"] = ["policy.py", "national_bot.py"]

    try:
        MasterPlan.model_validate(plan)
    except ValueError as exc:
        assert "writable scope must be exactly ['policy.py']" in str(exc)
    else:
        raise AssertionError("ordinary Master task received writable national_bot.py")


def test_architecture_focus_contract_requirements_are_not_deferred():
    task = {
        "worker_id": 1,
        "role": "Algorithmic Logic Architect",
        "target_files": ["policy.py"],
        "files_allowed": ["policy.py"],
        "skill_layer": "telemetry",
        "architecture_focus_id": "decision_path_purity",
        "worker_prompt": "Move telemetry outside the live decision path without other edits.",
    }
    plan = {
        "analysis": "Remove external I/O from the live decision path without strategy changes.",
        "targeted_failure": "Decision-path purity probe finds telemetry I/O.",
        "measurement_plan": "Verify the typed decision-path purity check and native regression evidence.",
        "tasks": [task],
    }

    _unchanged, missing_contract_errors = validate_agent_output("master", plan)
    assert any("runtime_contract is required" in error for error in missing_contract_errors)
    semantic_errors, _warnings = _validate_master_plan(plan)
    assert any("runtime_contract is required" in error for error in semantic_errors)

    task["runtime_contract"] = _runtime_contract(decision=True)
    _unchanged, missing_terms_errors = validate_agent_output("master", plan)
    assert any("required execution term(s)" in error for error in missing_terms_errors)

    task["worker_prompt"] = (
        "Keep telemetry outside the decision path and I/O boundary; use decision_context "
        "to return a typed intent with raise_to or pass. Enforce a decision budget, "
        "compute the legal baseline, check the deadline, and use the legal fallback."
    )
    validated, schema_errors = validate_agent_output("master", plan)
    assert schema_errors == []
    semantic_errors, _warnings = _validate_master_plan(validated)
    assert semantic_errors == []


def test_unknown_future_focus_requires_contract_in_both_layers():
    plan = {
        "analysis": "Exercise forward-compatible architecture focus validation.",
        "targeted_failure": "A future focus must not bypass the runtime contract gate.",
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py"],
            "files_allowed": ["policy.py"],
            "skill_layer": "telemetry",
            "architecture_focus_id": "future_focus",
            "worker_prompt": "Implement the future architecture focus with bounded telemetry.",
        }],
    }

    _unchanged, schema_errors = validate_agent_output("master", plan)
    assert any("runtime_contract is required" in error for error in schema_errors)
    semantic_errors, _warnings = _validate_master_plan(plan)
    assert any("runtime_contract is required" in error for error in semantic_errors)


def test_explicit_lower_compaction_cap_preserves_runtime_terms(tmp_path):
    import plan_compiler

    plan = {
        "analysis": "Compile a long but valid deadline-aware implementation brief.",
        "targeted_failure": "Long runtime prompts lost executable contract terms.",
        "measurement_plan": "Verify compiled prompt terms and the typed runtime decision checks.",
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py"],
            "files_allowed": ["policy.py"],
            "skill_layer": "runtime_architecture",
            "worker_prompt": (
                "Use decision_context to return typed intent with raise_to or pass; keep "
                "the decision budget, legal fallback, fast baseline, and hard deadline. "
                + ("bounded refinement detail " * 430)
            ),
            "runtime_contract": _runtime_contract(decision=True),
        }],
    }
    original_chars = len(plan["tasks"][0]["worker_prompt"])
    explicit_compaction_cap = 10_000
    assert explicit_compaction_cap < original_chars <= WORKER_PROMPT_MAX_CHARS
    _validated, schema_errors = validate_agent_output("master", plan)
    assert schema_errors == []

    compiled, meta = plan_compiler.compile_master_plan(
        plan,
        next_v=144,
        target_dir=tmp_path / "national_v144",
        project_root=tmp_path,
        hard_prompt_chars=explicit_compaction_cap,
    )

    assert meta["compiled"] is True
    compiled_prompt = compiled["tasks"][0]["worker_prompt"].lower()
    for term in ("budget", "fallback", "baseline", "deadline"):
        assert term in compiled_prompt
    semantic_errors, _warnings = _validate_master_plan(compiled)
    assert semantic_errors == []


def test_explicit_lower_compaction_cap_preserves_dynamic_focus_terms(tmp_path):
    import plan_compiler

    plan = {
        "analysis": "Externalize a long decision-path purity task without losing focus terms.",
        "targeted_failure": "Compiled prompts lost dynamic architecture focus terms.",
        "measurement_plan": "Verify dynamic focus terms and typed runtime counterfactual checks.",
        "architecture_policy": {
            "plan_required_floor_checks": [],
            "selected_focus": {
                "focus_id": "decision_path_purity",
                "accepted_skill_layers": ["telemetry"],
                "suggested_files": ["policy.py"],
                "required_terms": ["decision path", "telemetry", "I/O"],
            }
        },
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py"],
            "files_allowed": ["policy.py"],
            "skill_layer": "telemetry",
            "architecture_focus_id": "decision_path_purity",
            "worker_prompt": (
                "Use decision_context to return typed intent with raise_to or pass; keep "
                "telemetry and I/O outside the decision path; preserve the decision "
                "budget, legal fallback, fast baseline, and hard deadline. "
                + ("purity implementation detail " * 400)
            ),
            "runtime_contract": _runtime_contract(decision=True),
        }],
    }
    original_chars = len(plan["tasks"][0]["worker_prompt"])
    explicit_compaction_cap = 10_000
    assert explicit_compaction_cap < original_chars <= WORKER_PROMPT_MAX_CHARS
    schema_plan = deepcopy(plan)
    schema_plan.pop("architecture_policy")
    _validated, schema_errors = validate_agent_output("master", schema_plan)
    assert schema_errors == []

    compiled, _meta = plan_compiler.compile_master_plan(
        plan,
        next_v=145,
        target_dir=tmp_path / "national_v145",
        project_root=tmp_path,
        hard_prompt_chars=explicit_compaction_cap,
    )

    compiled_prompt = compiled["tasks"][0]["worker_prompt"].lower()
    for term in ("decision path", "telemetry", "i/o"):
        assert term in compiled_prompt
    # This unit fixture supplies only the dynamic focus fragment, not a signed
    # architecture-policy receipt.  Validate the compiled Worker contract
    # independently; full policy identity has dedicated coverage.
    compiled_contract = deepcopy(compiled)
    compiled_contract.pop("architecture_policy")
    semantic_errors, _warnings = _validate_master_plan(compiled_contract)
    assert semantic_errors == []


def _proposal_contract_fixture(agent_master) -> tuple[dict, dict, str]:
    proposal = {
        "schema_version": "master-proposal-v3",
        "direction": "mechanism",
        "targeted_failure": (
            "A reachable parent decision branch ignores the selected bounded state."
        ),
        "structural_change": (
            "Route one deadline-bounded state feature through the existing decision consumer."
        ),
        "counterfactual": (
            "Hold cards, legality, state, and seed fixed while toggling only that feature."
        ),
        "measurement": (
            "target=national_v143; primary=complete_70_hand_wld; "
            "expected_delta=0.03; samples=>=30_complete_matches; "
            "uncertainty=wilson_wld_interval; secondary=net_chip_ci"
        ),
        "why_not_threshold_tuning": (
            "The change adds state flow into a reachable consumer instead of tuning one cutoff."
        ),
        "mechanism_target": "deadline",
        "expected_diff": (
            "The paired deadline intervention changes the selected intent through _choose_intent."
        ),
        "target_files": ["policy.py"],
        "source_symbols": [
            "policy.py:get_baseline_decision",
            "policy.py:_choose_intent",
        ],
        "reachable_chain": [
            "policy.py:get_baseline_decision",
            "policy.py:_choose_intent",
        ],
        "falsifier": {
            "test_name": "fast_policy_baseline",
            "state_learning_primary": "sample_counted_candidate_batch",
            "intervention_target": "deadline",
            "control": "Run the frozen parent with sample_count=1 before the deadline on the same canonical decision state.",
            "intervention": "Enable only the selected bounded state mechanism with a changed deadline.",
            "expected_observation": (
                "The intervention changes the intended action while the control stays fixed."
            ),
        },
        "evidence_refs": [
            "source:policy.py:get_baseline_decision",
            "source:policy.py:_choose_intent",
        ],
        "snapshot_evidence": [],
        "execution_mode": "strategy_implementation",
        "risks": "Sparse paired states can overfit, so the legal fallback remains unchanged.",
    }
    proposal["proposal_id"] = agent_master._proposal_identity(proposal)
    contract = agent_master._selected_proposal_contract(proposal)
    block = agent_master._selected_proposal_worker_block(proposal)
    return proposal, contract, block


def test_system_owned_contract_reserve_bounds_all_closed_terms():
    import output_schema
    import plan_compiler
    import runtime_architecture_policy
    import strategy_reference_pack

    terms = []
    for values in output_schema.RUNTIME_CONTRACT_WORKER_PROMPT_TERMS.values():
        terms.extend(values)
    for values in output_schema.STATE_LEARNING_PRIMARY_PROMPT_TERMS.values():
        terms.extend(values)
    for card in strategy_reference_pack._CARDS:
        terms.extend(card.required_worker_terms)
    for focus in runtime_architecture_policy.architecture_focus_specs():
        terms.extend(focus.get("required_terms") or [])
    terms.extend((
        "f" * 64,
        max(output_schema.MASTER_PROPOSAL_FALSIFIER_TESTS, key=len),
    ))
    unique_terms = tuple(dict.fromkeys(map(str, terms)))
    block = plan_compiler._system_owned_contract_binding_block(unique_terms)

    assert len(block) <= plan_compiler.SYSTEM_OWNED_CONTRACT_MAX_CHARS
    assert plan_compiler.SYSTEM_OWNED_CONTRACT_MAX_CHARS == 2048


def test_compiler_externalizes_long_prompt_without_losing_selected_contract(tmp_path):
    import agent_master
    import plan_compiler

    proposal, contract, selected_block = _proposal_contract_fixture(agent_master)
    falsifier = proposal["falsifier"]["test_name"]
    plan = {
        "proposal_binding": {
            "contract_digest": contract["contract_digest"],
            "target_files": ["policy.py"],
            "falsifier": deepcopy(proposal["falsifier"]),
        },
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py"],
            "files_allowed": ["policy.py"],
            "worker_prompt": (
                ("bounded implementation context before contract. " * 140)
                + "\n\n"
                + selected_block
                + "\n\n"
                + ("bounded implementation context after contract. " * 140)
            ),
        }],
    }
    original_prompt = plan["tasks"][0]["worker_prompt"]
    assert len(original_prompt) > plan_compiler.HARD_WORKER_PROMPT_CHARS

    compiled, meta = plan_compiler.compile_master_plan(
        plan,
        next_v=144,
        target_dir=tmp_path / "national_v144",
        project_root=tmp_path,
        context_chars=len(selected_block) + 2_000,
    )

    assert meta["compiled"] is True
    assert meta["preserved_inline_tasks"] == []
    task = compiled["tasks"][0]
    brief = (tmp_path / task["task_brief_file"]).read_text(encoding="utf-8")
    assert brief.count(selected_block) == 1
    assert f"contract_digest={contract['contract_digest']}" in brief
    assert f'"test_name":"{falsifier}"' in brief
    assert contract["contract_digest"] in task["worker_prompt"]
    assert falsifier in task["worker_prompt"]


def test_compiler_keeps_long_prompt_inline_when_selected_contract_cannot_fit(tmp_path):
    import agent_master
    import plan_compiler

    _proposal, _contract, selected_block = _proposal_contract_fixture(agent_master)
    plan = {
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py"],
            "files_allowed": ["policy.py"],
            "worker_prompt": (
                ("long context before immutable contract. " * 170)
                + "\n\n"
                + selected_block
                + "\n\n"
                + ("long context after immutable contract. " * 170)
            ),
        }],
    }
    original_prompt = plan["tasks"][0]["worker_prompt"]

    compiled, meta = plan_compiler.compile_master_plan(
        plan,
        next_v=144,
        target_dir=tmp_path / "national_v144",
        project_root=tmp_path,
        context_chars=len(selected_block) - 1,
    )

    assert meta["compiled"] is False
    assert meta["preserved_inline_tasks"] == [{
        "worker_id": 1,
        "reason": "selected_proposal_contract_exceeds_context_budget",
        "original_chars": len(original_prompt),
        "selected_contract_chars": len(selected_block),
    }]
    assert compiled["tasks"][0]["worker_prompt"] == original_prompt
    assert selected_block in compiled["tasks"][0]["worker_prompt"]
    assert not (tmp_path / "national_v144" / ".task_context").exists()


def test_system_bootstrap_reuses_canonical_selected_proposal_contract_without_task_brief(
    monkeypatch,
    tmp_path,
):
    """A sealed final Master must verify against its own prompt digest.

    The bootstrap receipt used to reproduce only a subset of the Master
    contract, so adding typed state-learning fields in the canonical Master
    projection made the bootstrap recompute a different digest.  That falsely
    rejected an otherwise valid v143 plan before the deterministic Worker was
    allowed to start.
    """

    import agent_master
    import system_strict_bootstrap
    from tests.test_master_success_return import _valid_proposal_packet

    proposal, _contract, _selected_block = _proposal_contract_fixture(agent_master)
    packet = _valid_proposal_packet(
        agent_master,
        proposal,
        tmp_path / "proposal_invocations",
    )
    selected = packet["ordered_proposals"][0]
    binding = agent_master._selected_proposal_binding(selected, packet)
    selected_contract = agent_master._selected_proposal_contract(selected)
    selected_block = agent_master._selected_proposal_worker_block(selected)
    assert binding["contract_digest"] == selected_contract["contract_digest"]

    # This is the smallest complete graph needed by the selected proposal;
    # the test is about Master/bootstrap contract equality, not file parsing.
    graph = {
        "policy.py:get_baseline_decision": {"_choose_intent"},
        "policy.py:_choose_intent": set(),
    }
    monkeypatch.setattr(
        system_strict_bootstrap,
        "_prepared_graph",
        lambda: (graph, packet["source_code_digest"], []),
    )
    import plan_compiler

    # An explicit generic lower cap can still create a task brief for a
    # non-strict caller, but bootstrap must reject that lossy authority form.
    explicit_compaction_cap = 10_000
    padding = "x" * max(
        1,
        explicit_compaction_cap + 1 - len(selected_block),
    )
    plan = {
        "selected_proposal_id": selected["proposal_id"],
        "proposal_binding": binding,
        "proposal_ensemble": packet,
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py"],
            "files_allowed": ["policy.py"],
            "worker_prompt": (
                padding
                + "\n\nImplement only the typed, bounded selected mechanism.\n\n"
                + selected_block
            ),
        }],
    }

    # The inline selected block remains the only bootstrappable form.
    assert system_strict_bootstrap.validate_selected_proposal_for_blueprint(plan) == []

    compiled, compiler = plan_compiler.compile_master_plan(
        plan,
        next_v=143,
        target_dir=tmp_path / "candidate",
        project_root=tmp_path,
        hard_prompt_chars=explicit_compaction_cap,
    )
    assert compiler["compiled"] is True
    compiled_prompt = compiled["tasks"][0]["worker_prompt"]
    assert plan_compiler.SELECTED_PROPOSAL_BEGIN in compiled_prompt
    assert plan_compiler.SELECTED_PROPOSAL_END in compiled_prompt
    assert f"proposal_id={selected['proposal_id']}" in compiled_prompt
    assert f"contract_digest={binding['contract_digest']}" in compiled_prompt
    assert "system_bootstrap_master_externalized_worker_prompt_forbidden" in (
        system_strict_bootstrap.validate_selected_proposal_for_blueprint(compiled)
    )

    missing_anchor = deepcopy(plan)
    missing_anchor["tasks"][0]["worker_prompt"] = (
        "Read the transient task brief, but no selected proposal identity is present."
    )
    assert "system_bootstrap_worker_selected_proposal_block_missing" in (
        system_strict_bootstrap.validate_selected_proposal_for_blueprint(
            missing_anchor
        )
    )

    drifted = deepcopy(plan)
    drifted["proposal_binding"]["state_learning_primary"] = "showdown_range"
    assert "system_bootstrap_proposal_contract_packet_mismatch" in (
        system_strict_bootstrap.validate_selected_proposal_for_blueprint(drifted)
    )

    # A later canonical typed field must flow through the one binding helper
    # without requiring a parallel bootstrap field inventory.  The prior
    # verifier manually reconstructed its contract and would reject this
    # otherwise coherent extension as a packet mismatch.
    original_contract = agent_master._selected_proposal_contract
    original_binding = agent_master._selected_proposal_binding

    def _future_contract(item):
        unsigned = {
            key: value
            for key, value in original_contract(item).items()
            if key != "contract_digest"
        }
        unsigned["future_typed_primary"] = "policy_contextuality"
        return {
            **unsigned,
            "contract_digest": hashlib.sha256(
                json.dumps(
                    unsigned,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }

    def _future_binding(item, source_packet):
        canonical = original_binding(item, source_packet)
        future_contract = _future_contract(item)
        return {
            **canonical,
            "contract_digest": future_contract["contract_digest"],
            "future_typed_primary": future_contract["future_typed_primary"],
        }

    monkeypatch.setattr(agent_master, "_selected_proposal_contract", _future_contract)
    monkeypatch.setattr(agent_master, "_selected_proposal_binding", _future_binding)
    extended = deepcopy(plan)
    extended_binding = _future_binding(selected, packet)
    extended["proposal_binding"] = extended_binding
    extended["tasks"][0]["worker_prompt"] = extended["tasks"][0]["worker_prompt"].replace(
        binding["contract_digest"],
        extended_binding["contract_digest"],
    )
    assert system_strict_bootstrap.validate_selected_proposal_for_blueprint(extended) == []

    future_drift = deepcopy(extended)
    future_drift["proposal_binding"]["future_typed_primary"] = "tampered"
    assert "system_bootstrap_proposal_contract_packet_mismatch" in (
        system_strict_bootstrap.validate_selected_proposal_for_blueprint(future_drift)
    )


def test_selected_proposal_quality_requires_executed_typed_check(tmp_path):
    import agent_master
    from bot_artifact import canonical_digest
    from tests.test_master_success_return import (
        BOUND_PROPOSAL,
        _valid_proposal_packet,
    )
    import tool_gates

    packet = _valid_proposal_packet(
        agent_master,
        deepcopy(BOUND_PROPOSAL),
        tmp_path / "proposal_invocations",
    )
    selected = packet["ordered_proposals"][0]
    contract = agent_master._selected_proposal_contract(selected)
    binding = agent_master._selected_proposal_binding(selected, packet)
    check_id = selected["falsifier"]["test_name"]
    master_plan = {
        "targeted_failure": selected["targeted_failure"],
        "measurement_plan": selected["measurement"],
        "selected_proposal_id": selected["proposal_id"],
        "proposal_ensemble": packet,
        "proposal_binding": binding,
    }
    passed_row = {
        "check_id": check_id,
        "passed": True,
        "control_action": "pass",
        "intervention_action": "raise",
        "paired_state_digest": "a" * 64,
    }
    transition = {
        "selected_dynamic_checks": [check_id],
        "selected_dynamic_failures": [],
        "candidate_capabilities": {"checks_by_id": {check_id: passed_row}},
    }
    candidate_dir = tmp_path / "candidate"
    candidate_dir.mkdir()
    (candidate_dir / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    return _choose_intent(context)\n\n"
        "def _choose_intent(context):\n"
        "    return {'kind': 'raise', 'raise_to': 400}\n",
        encoding="utf-8",
    )
    baseline_digests = packet["proposal_source_symbol_digests"][
        selected["proposal_id"]
    ]
    diff_rows = [{
        "symbol": symbol,
        "baseline_ast_sha256": baseline_digests[symbol],
        "candidate_ast_sha256": agent_master._source_symbol_ast_digest(
            candidate_dir,
            symbol,
        ),
        "changed": True,
    } for symbol in selected["reachable_chain"]]

    passed = tool_gates._selected_proposal_quality_evidence(
        master_plan,
        transition,
        candidate_dir=candidate_dir,
    )
    assert passed == {
        "required": True,
        "ok": True,
        "check_id": check_id,
        "check_evidence_digest": canonical_digest(passed_row),
        "proposal_contract_digest": contract["contract_digest"],
        "evidence_scope": (
            "reachable_symbol_delta_plus_typed_capability_only;"
            "not_full_counterfactual_or_strength_proof"
        ),
        "reachable_symbol_diff_required": True,
        "reachable_symbol_diff_ok": True,
        "changed_reachable_symbols": selected["reachable_chain"],
        "reachable_symbol_diff_digest": canonical_digest(diff_rows),
        "errors": [],
    }

    drifted_plan = deepcopy(master_plan)
    drifted_plan["proposal_binding"]["measurement"] += "; forged=true"
    drifted = tool_gates._selected_proposal_quality_evidence(
        drifted_plan,
        transition,
        candidate_dir=candidate_dir,
    )
    assert drifted["ok"] is False
    assert "proposal_quality_binding_projection_mismatch" in drifted["errors"]

    unchanged_packet = deepcopy(packet)
    for symbol in selected["reachable_chain"]:
        unchanged_packet["proposal_source_symbol_digests"][
            selected["proposal_id"]
        ][symbol] = agent_master._source_symbol_ast_digest(
            candidate_dir,
            symbol,
        )
    unchanged_plan = deepcopy(master_plan)
    unchanged_plan["proposal_ensemble"] = unchanged_packet
    unchanged_plan["proposal_binding"] = agent_master._selected_proposal_binding(
        selected,
        unchanged_packet,
    )
    unchanged = tool_gates._selected_proposal_quality_evidence(
        unchanged_plan,
        transition,
        candidate_dir=candidate_dir,
    )
    assert unchanged["ok"] is False
    assert unchanged["changed_reachable_symbols"] == []
    assert "proposal_quality_reachable_chain_unchanged" in unchanged["errors"]

    # The proposal prose and measurement remain a hypothesis: without a typed
    # check row they are not executable quality evidence.
    missing = tool_gates._selected_proposal_quality_evidence(
        master_plan,
        {
            "selected_dynamic_checks": [check_id],
            "selected_dynamic_failures": [],
            "candidate_capabilities": {"checks_by_id": {}},
        },
        candidate_dir=candidate_dir,
    )
    assert missing["ok"] is False
    assert missing["check_evidence_digest"] == ""
    assert missing["errors"] == [
        "proposal_quality_selected_check_evidence_missing"
    ]

    missing_ledger = tool_gates._selected_proposal_quality_evidence(
        master_plan,
        {
            "selected_dynamic_checks": [],
            "selected_dynamic_failures": [],
            "candidate_capabilities": {"checks_by_id": {}},
        },
        candidate_dir=candidate_dir,
    )
    assert missing_ledger["required"] is True
    assert missing_ledger["ok"] is False
    assert missing_ledger["errors"] == [
        "proposal_quality_selected_check_not_executed",
        "proposal_quality_selected_check_evidence_missing",
    ]

    failed = tool_gates._selected_proposal_quality_evidence(
        master_plan,
        {
            "selected_dynamic_checks": [check_id],
            "selected_dynamic_failures": [check_id],
            "candidate_capabilities": {
                "checks_by_id": {
                    check_id: {**passed_row, "passed": False},
                }
            },
        },
        candidate_dir=candidate_dir,
    )
    assert failed["ok"] is False
    assert failed["check_evidence_digest"] == ""
    assert failed["errors"] == [
        "proposal_quality_selected_check_failed",
        "proposal_quality_selected_check_evidence_missing",
    ]
