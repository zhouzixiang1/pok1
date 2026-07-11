"""Regression coverage for the executable Master plan contract."""

from copy import deepcopy
import json
from pathlib import Path

from output_schema import (
    MASTER_PLAN_MAX_TASKS,
    MATCH_MEMORY_ALLOWED_UPDATE_EVENTS,
    MATCH_MEMORY_REQUIRED_UPDATE_EVENTS,
    PRECOMPUTE_BUILD_PHASES,
    RUNTIME_CONTRACT_WORKER_PROMPT_TERMS,
    WORKER_PROMPT_MAX_CHARS,
    WORKER_TASK_MAX_TARGET_FILES,
    MasterPlan,
    master_plan_executable_contract_text,
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
            "consumer": "strategy.get_baseline_action",
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
            "snapshot_field": "opponent_runtime",
            "max_recent_hands": 8,
            "prior_rule": "beta_prior_weight_8",
            "confidence_rule": (
                "global_actions_over_actions_plus_24_and_context_samples_over_samples_plus_8"
            ),
            "adaptation_cap": 0.65,
            "consumer": "strategy.get_baseline_action",
        }
    return {
        "decision": decision_contract,
        "precompute_artifacts": artifacts,
        "match_memory": match_memory,
        "official_feedback_refs": [],
        "forbidden_runtime_work": ["full-history scans in get_action"],
    }


def _v143_regression_plan() -> dict:
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
                    "strategy.py",
                    "precompute.py",
                    "card_utils.py",
                    "simulation.py",
                ],
                "skill_layer": "precompute",
                "files_allowed": ["precompute.py"],
                "worker_prompt": (
                    "Build bounded module-import precompute data in precompute.py and "
                    "consume it from the legal strategy path."
                ),
                "runtime_contract": _runtime_contract(precompute=True),
            },
            {
                "worker_id": 2,
                "role": "Opponent Modeler",
                "target_files": ["national_bot.py", "strategy.py"],
                "skill_layer": "opponent_model",
                "files_allowed": ["national_bot.py", "strategy.py"],
                "worker_prompt": (
                    "Incrementally update the tracker, compute confidence, and pass "
                    "opponent_runtime into strategy.get_baseline_action."
                ),
                "runtime_contract": _runtime_contract(memory=True),
            },
        ],
    }


def test_executable_contract_is_rendered_from_schema_sources():
    text = master_plan_executable_contract_text()

    assert f"tasks: 1..{MASTER_PLAN_MAX_TASKS} items" in text
    assert (
        f"task.target_files: 1..{WORKER_TASK_MAX_TARGET_FILES} files" in text
    )
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


def test_v143_contract_errors_are_rejected_by_schema_and_semantic_gate():
    plan = _v143_regression_plan()

    _unchanged, schema_errors = validate_agent_output("master", plan)
    assert any("target_files" in error and "at most 3" in error for error in schema_errors)
    assert any(
        "national_bot.py is read-only in Master worker tasks" in error
        for error in schema_errors
    )

    semantic_errors, _warnings = _validate_master_plan(plan)
    assert any("too many target_files (4 > 3)" in error for error in semantic_errors)
    assert any("required execution term(s) ['memory']" in error for error in semantic_errors)


def test_corrected_v143_contract_passes_both_validation_layers():
    plan = _v143_regression_plan()
    plan["tasks"][0]["target_files"] = [
        "strategy.py",
        "precompute.py",
        "card_utils.py",
    ]
    plan["tasks"][1]["worker_prompt"] = (
        "Implement incremental match memory, compute confidence, and publish "
        "opponent_runtime to strategy.get_baseline_action."
    )
    plan["tasks"][1]["target_files"] = ["strategy.py"]
    plan["tasks"][1]["files_allowed"] = ["strategy.py"]
    plan["tasks"][1]["read_only_dependencies"] = ["national_bot.py"]

    validated, schema_errors = validate_agent_output("master", deepcopy(plan))
    assert schema_errors == []
    semantic_errors, _warnings = _validate_master_plan(validated)
    assert semantic_errors == []


def test_system_runtime_owner_can_be_declared_read_only_but_not_hidden_writable():
    plan = {
        "analysis": "Consume the existing system provider without editing its wrapper.",
        "targeted_failure": "Opponent profile consumer is missing.",
        "tasks": [{
            "worker_id": 1,
            "role": "Opponent Modeler",
            "target_files": ["strategy.py"],
            "skill_layer": "opponent_model",
            "files_allowed": [],
            "read_only_dependencies": ["national_bot.py"],
            "worker_prompt": (
                "Consume bounded opponent match memory and opponent_runtime with confidence."
            ),
            "runtime_contract": _runtime_contract(memory=True),
        }],
    }

    MasterPlan.model_validate(plan)

    plan["tasks"][0]["read_only_dependencies"] = []
    plan["tasks"][0]["files_allowed"] = ["national_bot.py"]
    try:
        MasterPlan.model_validate(plan)
    except ValueError as exc:
        assert "system-provided national_bot.py" in str(exc)
    else:
        raise AssertionError("hidden writable system provider was accepted")


def test_master_plan_rejects_two_generation_state_learning_primaries():
    prompt = (ROOT / "web/core/prompts/master_prompt.md").read_text(encoding="utf-8")
    start = prompt.index('{\n  "analysis": "Strategic analysis as a single string.')
    end = prompt.index("\n\n- Do NOT include `branch_from`", start)
    plan = json.loads(prompt[start:end])
    second = deepcopy(plan["tasks"][0])
    second["worker_id"] = 2
    second["target_files"] = ["postflop.py"]
    second["files_allowed"] = ["postflop.py"]
    second["runtime_contract"]["state_learning"] = {
        "work_primitive": None,
        "profile_dimensions": [],
        "line_controls": ["delayed_probe"],
        "oracle_refs": [
            "docs/official-raise-boundary-oracle-2026-07-11.md",
            "docs/official-terminal-settlement-oracle-2026-07-11.md",
        ],
    }
    second["runtime_contract"]["reference_pack_id"] = ""
    second["checks_required"] = ["semantic_line_reachability"]
    second["worker_prompt"] += (
        " Consume hand_runtime can_delayed_probe with a positive/control sanitized "
        "action difference and telemetry."
    )
    plan["tasks"].append(second)

    try:
        MasterPlan.model_validate(plan)
    except ValueError as exc:
        assert "exactly one state_learning primary is allowed across the entire generation" in str(exc)
    else:
        raise AssertionError("two generation-level state_learning primaries were accepted")


def test_master_plan_rejects_writable_system_native_entrypoint():
    plan = {
        "analysis": "Attempt to mix strategy work with system wrapper mutation.",
        "targeted_failure": "System provider ownership is not isolated.",
        "tasks": [{
            "worker_id": 1,
            "role": "Opponent Modeler",
            "target_files": ["strategy.py", "national_bot.py"],
            "skill_layer": "opponent_model",
            "architecture_focus_id": "incremental_match_model",
            "files_allowed": ["strategy.py", "national_bot.py"],
            "worker_prompt": (
                "Consume incremental match memory and opponent_runtime with confidence."
            ),
            "runtime_contract": _runtime_contract(memory=True),
        }],
    }

    try:
        MasterPlan.model_validate(plan)
    except ValueError as exc:
        assert "national_bot.py is read-only in Master worker tasks" in str(exc)
    else:
        raise AssertionError("ordinary Master task received writable national_bot.py")


def test_architecture_focus_contract_requirements_are_not_deferred():
    task = {
        "worker_id": 1,
        "role": "Algorithmic Logic Architect",
        "target_files": ["strategy.py"],
        "skill_layer": "telemetry",
        "architecture_focus_id": "decision_path_purity",
        "worker_prompt": "Move telemetry outside the live decision path without other edits.",
    }
    plan = {
        "analysis": "Remove external I/O from the live decision path without strategy changes.",
        "targeted_failure": "Decision-path purity probe finds telemetry I/O.",
        "tasks": [task],
    }

    _unchanged, missing_contract_errors = validate_agent_output("master", plan)
    assert any("runtime_contract is required" in error for error in missing_contract_errors)
    semantic_errors, _warnings = _validate_master_plan(plan)
    assert any("runtime_contract is required" in error for error in semantic_errors)

    task["runtime_contract"] = _runtime_contract(decision=True)
    _unchanged, missing_terms_errors = validate_agent_output("master", plan)
    assert any("required execution term(s)" in error for error in missing_terms_errors)
    semantic_errors, _warnings = _validate_master_plan(plan)
    assert any("required execution term(s)" in error for error in semantic_errors)

    task["worker_prompt"] = (
        "Keep telemetry outside the decision path; enforce a decision budget, compute "
        "the legal baseline before refinement, check the deadline, and use the legal "
        "fallback on error."
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
            "target_files": ["strategy.py"],
            "skill_layer": "telemetry",
            "architecture_focus_id": "future_focus",
            "worker_prompt": "Implement the future architecture focus with bounded telemetry.",
        }],
    }

    _unchanged, schema_errors = validate_agent_output("master", plan)
    assert any("runtime_contract is required" in error for error in schema_errors)
    semantic_errors, _warnings = _validate_master_plan(plan)
    assert any("runtime_contract is required" in error for error in semantic_errors)


def test_compiler_preserves_runtime_terms_across_ten_to_twelve_k_boundary(tmp_path):
    import plan_compiler

    plan = {
        "analysis": "Compile a long but valid deadline-aware implementation brief.",
        "targeted_failure": "Long runtime prompts lost executable contract terms.",
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["strategy.py"],
            "skill_layer": "runtime_architecture",
            "worker_prompt": (
                "Keep the decision budget, legal fallback, fast baseline, and hard deadline. "
                + ("bounded refinement detail " * 430)
            ),
            "runtime_contract": _runtime_contract(decision=True),
        }],
    }
    original_chars = len(plan["tasks"][0]["worker_prompt"])
    assert plan_compiler.HARD_WORKER_PROMPT_CHARS < original_chars <= WORKER_PROMPT_MAX_CHARS
    _validated, schema_errors = validate_agent_output("master", plan)
    assert schema_errors == []

    compiled, meta = plan_compiler.compile_master_plan(
        plan,
        next_v=144,
        target_dir=tmp_path / "national_v144",
        project_root=tmp_path,
    )

    assert meta["compiled"] is True
    compiled_prompt = compiled["tasks"][0]["worker_prompt"].lower()
    for term in ("budget", "fallback", "baseline", "deadline"):
        assert term in compiled_prompt
    semantic_errors, _warnings = _validate_master_plan(compiled)
    assert semantic_errors == []


def test_compiler_preserves_dynamic_focus_terms(tmp_path):
    import plan_compiler

    plan = {
        "analysis": "Externalize a long decision-path purity task without losing focus terms.",
        "targeted_failure": "Compiled prompts lost dynamic architecture focus terms.",
        "architecture_policy": {
            "plan_required_floor_checks": [],
            "selected_focus": {
                "focus_id": "decision_path_purity",
                "accepted_skill_layers": ["telemetry"],
                "suggested_files": ["strategy.py"],
                "required_terms": ["decision path", "telemetry", "I/O"],
            }
        },
        "tasks": [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["strategy.py"],
            "skill_layer": "telemetry",
            "architecture_focus_id": "decision_path_purity",
            "worker_prompt": (
                "Keep telemetry and I/O outside the decision path; preserve the decision "
                "budget, legal fallback, fast baseline, and hard deadline. "
                + ("purity implementation detail " * 400)
            ),
            "runtime_contract": _runtime_contract(decision=True),
        }],
    }
    original_chars = len(plan["tasks"][0]["worker_prompt"])
    assert plan_compiler.HARD_WORKER_PROMPT_CHARS < original_chars <= WORKER_PROMPT_MAX_CHARS
    schema_plan = deepcopy(plan)
    schema_plan.pop("architecture_policy")
    _validated, schema_errors = validate_agent_output("master", schema_plan)
    assert schema_errors == []

    compiled, _meta = plan_compiler.compile_master_plan(
        plan,
        next_v=145,
        target_dir=tmp_path / "national_v145",
        project_root=tmp_path,
    )

    compiled_prompt = compiled["tasks"][0]["worker_prompt"].lower()
    for term in ("decision path", "telemetry", "i/o"):
        assert term in compiled_prompt
    semantic_errors, _warnings = _validate_master_plan(compiled)
    assert semantic_errors == []
