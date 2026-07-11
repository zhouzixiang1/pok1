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
            "prior_rule": "Beta prior with eight pseudo-observations",
            "confidence_rule": "actions divided by actions plus twenty-four",
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
        "worker_prompt" in error and "['memory']" in error
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

    validated, schema_errors = validate_agent_output("master", deepcopy(plan))
    assert schema_errors == []
    semantic_errors, _warnings = _validate_master_plan(validated)
    assert semantic_errors == []


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
