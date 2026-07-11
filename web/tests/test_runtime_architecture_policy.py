from pathlib import Path

import pytest

from output_schema import RuntimeContract
from pipeline_state import validate_stage_transition
from runtime_architecture_policy import (
    RUNTIME_ARCHITECTURE_POLICY_VERSION,
    attach_runtime_contract_ledger,
    architecture_policy_prompt,
    build_architecture_policy,
    evaluate_architecture_transition,
    validate_runtime_contract_ledger,
    validate_plan_architecture_focus,
    validate_runtime_contract_implementation,
)
import tool_planning
from national_native import NATIVE_BOT_TEMPLATE


def _write_bot(root: Path, *, complete: bool, lose_wire: bool = False) -> Path:
    root.mkdir(parents=True)
    native = """
import argparse
from collections import deque
POK_OFFICIAL_ACTION_DELAY = 0.30
OPPONENT_ADAPTATION_CAP = 0.65
DEFAULT_DECISION_HARD_DEADLINE_SEC = 55.0
DEFAULT_DECISION_BASELINE_TARGET_SEC = 0.25
DEFAULT_DECISION_REFINEMENT_BUDGET_SEC = 54.0
class OpponentTracker:
    def __init__(self):
        self.recent = deque(maxlen=8)
        self.hands = 0
        self.completed = 0
        self.actions = 0
        self.streets = 0
        self.settlements = 0
        self.showdowns = 0
    def begin_hand(self, hand): self.hands += 1
    def begin_street(self): self.streets += 1
    def observe_action(self, action): self.actions += 1
    def observe_settlement(self, earned):
        self.settlements += 1
        self.completed += 1
        self.recent.append({'hand': self.completed})
    def observe_showdown(self, cards): self.showdowns += 1
    def snapshot(self):
        confidence = self.actions / (self.actions + 24.0)
        return {
            'confidence': confidence,
            'adaptation_weight': min(OPPONENT_ADAPTATION_CAP, confidence * OPPONENT_ADAPTATION_CAP),
            'vpip': self.hands / (self.hands + 8.0), 'pfr': 0.28, 'allin_rate': 0.08,
            'postflop_aggr': 0.36, 'postflop_check_rate': 0.42,
            'fold_to_raise': 0.35, 'aggression': 0.32,
            'avg_raise_bb': 3.0, 'raise_samples': self.settlements,
            'flop_aggr': 0.36, 'turn_aggr': 0.36,
            'river_aggr': min(1.0, 0.36 + (self.showdowns + self.streets) * 0.01),
            'hands_completed': self.completed,
            'recent_hands': list(self.recent),
        }
class NativeNationalBot:
    def __init__(self): self._opponent_tracker = OpponentTracker()
    def _send_wire_action(self, action): pass
    def _request(self): return {'opponent_runtime': self._opponent_tracker.snapshot()}
    def handle(self, msg):
        if msg.startswith('preflop'): self._opponent_tracker.begin_hand(1)
        elif msg.startswith(('flop', 'turn', 'river')): self._opponent_tracker.begin_street()
        elif msg.startswith('earnChips'): self._opponent_tracker.observe_settlement(0)
        elif msg.startswith('oppo_hands'): self._opponent_tracker.observe_showdown([])
        else: self._opponent_tracker.observe_action(msg)
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log')
"""
    if complete:
        native = NATIVE_BOT_TEMPLATE
    if lose_wire:
        native = native.replace("POK_OFFICIAL_ACTION_DELAY", "BROKEN_OFFICIAL_ACTION_DELAY")
    (root / "national_bot.py").write_text(native, encoding="utf-8")
    (root / "main.py").write_text(
        "def sanitize_action(action, state, chips): return int(action)\n",
        encoding="utf-8",
    )
    (root / "state.py").write_text(
        "def reconstruct_state(req): return req\n"
        "def infer_remaining_hands_from_requests(requests): return 1\n",
        encoding="utf-8",
    )
    if complete:
        strategy = """
import time
EQUITY_LOOKUP_TABLE = {key: key / 100 for key in range(128)}
def equity_lookup(key): return EQUITY_LOOKUP_TABLE.get(key, key / 100)
def _choose(req):
    profile = req.get('opponent_runtime', {})
    baseline = 0
    adaptation = profile.get('adaptation_weight', 0.0)
    threshold = 0.4 if equity_lookup(60) else 0.6
    adjusted = baseline + (adaptation * 1.0)
    deadline = time.monotonic() + 0.1
    for sample in range(64):
        if time.monotonic() >= deadline:
            break
        baseline += sample * 0
    return 400 if adjusted > threshold else 0
def get_action(req, current_view):
    return _choose(req)
def get_baseline_action(req, current_view):
    return _choose(req)
def iter_refinements(req, requests, baseline, deadline):
    if time.monotonic() < deadline:
        yield baseline
"""
    else:
        strategy = """
def get_action(req, requests):
    total = 0
    for item in requests: total += 1
    return total
"""
    (root / "strategy.py").write_text(strategy, encoding="utf-8")
    return root


def test_policy_selects_one_coherent_parent_debt(tmp_path):
    source = _write_bot(tmp_path / "national_v1", complete=False)

    policy = build_architecture_policy(source)

    assert policy["policy_version"] == RUNTIME_ARCHITECTURE_POLICY_VERSION
    assert policy["selected_focus"]["focus_id"] == "national_runtime_v3_migration"
    assert policy["selected_focus"]["required_checks"] == [
        "killable_decision_runtime",
        "fast_strategy_baseline",
        "incremental_refinement_protocol",
        "decision_path_no_full_history_scan",
        "decision_path_no_large_runtime_tables",
        "precompute_lookup_path",
        "persistent_match_memory",
        "incremental_opponent_model",
    ]
    assert len(policy["source_capability_digest"]) == 64


def test_transition_probe_infrastructure_never_synthesizes_candidate_repairs(tmp_path, monkeypatch):
    import runtime_architecture_policy as policy_module

    source = _write_bot(tmp_path / "national_v1", complete=True)
    candidate = _write_bot(tmp_path / "national_v2", complete=True)
    conclusive = policy_module.evaluate_national_capabilities(source)
    inconclusive = {
        **conclusive,
        "bot_dir": str(candidate),
        "ok": False,
        "conclusive": False,
        "outcome": "infrastructure_failure",
        "infrastructure_failures": [{
            "component": "national_runtime_probe",
            "failure_class": "probe_infra",
            "issues": ["sandbox launch failed"],
        }],
    }
    monkeypatch.setattr(
        policy_module,
        "evaluate_national_capabilities",
        lambda path: inconclusive if Path(path).name == candidate.name else conclusive,
    )

    transition = evaluate_architecture_transition(source, candidate)

    assert transition["ok"] is False
    assert transition["conclusive"] is False
    assert transition["outcome"] == "infrastructure_failure"
    assert transition["failure_class"] == "infrastructure"
    assert transition["regressions"] == []
    assert transition["runtime_floor_failures"] == []
    assert transition["unresolved_focus_checks"] == []
    assert transition["candidate_failures"] == []
    assert transition["infrastructure_failures"][0]["side"] == "candidate"


def test_transition_requires_focus_closure_and_no_parent_regression(tmp_path):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    incomplete = _write_bot(tmp_path / "national_v2", complete=False)
    complete = _write_bot(tmp_path / "national_v3", complete=True)
    regressed = _write_bot(tmp_path / "national_v4", complete=True, lose_wire=True)
    policy = build_architecture_policy(source)

    failed = evaluate_architecture_transition(source, incomplete, expected_policy=policy)
    passed = evaluate_architecture_transition(source, complete, expected_policy=policy)
    lost = evaluate_architecture_transition(source, regressed, expected_policy=policy)

    assert failed["ok"] is False
    assert "incremental_opponent_model" in failed["unresolved_focus_checks"]
    assert "decision_path_no_full_history_scan" in failed["unresolved_focus_checks"]
    assert passed["ok"] is True
    assert lost["ok"] is False
    assert any(item["check_id"] == "official_safe_wire_send" for item in lost["regressions"])


def test_selected_focus_closure_cannot_bypass_runtime_floor(tmp_path):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    candidate = _write_bot(tmp_path / "national_v2", complete=True)
    strategy_path = candidate / "strategy.py"
    strategy_path.write_text(
            strategy_path.read_text(encoding="utf-8").replace(
                "EQUITY_LOOKUP_TABLE = {key: key / 100 for key in range(128)}",
                "EQUITY_LOOKUP_TABLE = {}",
        ),
        encoding="utf-8",
    )
    policy = build_architecture_policy(source)

    transition = evaluate_architecture_transition(source, candidate, expected_policy=policy)

    assert transition["unresolved_focus_checks"] == ["precompute_lookup_path"]
    assert transition["ok"] is False
    assert [item["check_id"] for item in transition["runtime_floor_failures"]] == [
        "precompute_lookup_path"
    ]


def test_policy_identity_detects_stale_source_contract(tmp_path):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    candidate = _write_bot(tmp_path / "national_v2", complete=True)
    policy = build_architecture_policy(source)
    policy["source_capability_digest"] = "stale"

    result = evaluate_architecture_transition(source, candidate, expected_policy=policy)

    assert result["ok"] is False
    assert any("source_capability_digest_mismatch" in item for item in result["policy_identity_errors"])


@pytest.mark.parametrize("field", ["required_checks", "suggested_files", "accepted_skill_layers"])
def test_policy_identity_detects_focus_contract_tampering(tmp_path, field):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    candidate = _write_bot(tmp_path / "national_v2", complete=True)
    policy = build_architecture_policy(source)
    policy["selected_focus"][field] = []

    result = evaluate_architecture_transition(source, candidate, expected_policy=policy)

    assert result["ok"] is False
    assert any("expected_content_digest_mismatch" in item for item in result["policy_identity_errors"])


def test_plan_must_cover_system_selected_focus(tmp_path):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    policy = build_architecture_policy(source)
    task = {
        "architecture_focus_id": "national_runtime_v3_migration",
        "skill_layer": "runtime_architecture",
        "target_files": ["strategy.py", "precompute.py", "opponent.py"],
        "checks_required": list(policy["plan_required_floor_checks"]),
        "worker_prompt": (
            "Implement get_baseline_action and iter_refinements, consume precompute facts, "
            "and use opponent_runtime incrementally with context-specific confidence."
        ),
    }

    assert validate_plan_architecture_focus({"tasks": [task]}, policy) == []
    task["checks_required"] = []
    floor_errors = validate_plan_architecture_focus({"tasks": [task]}, policy)
    assert any("Runtime floor check" in error for error in floor_errors)
    task["checks_required"] = list(policy["plan_required_floor_checks"])
    task["architecture_focus_id"] = "wrong"
    errors = validate_plan_architecture_focus({"tasks": [task]}, policy)
    assert any("mandatory" in error for error in errors)

    prompt = architecture_policy_prompt(policy)
    assert "selected_focus=national_runtime_v3_migration" in prompt
    assert "label is not proof" in prompt


def _match_memory_contract():
    return {
        "decision": None,
        "precompute_artifacts": [],
        "match_memory": {
            "tracker_class": "OpponentTracker",
            "owner_file": "national_bot.py",
            "reset_boundary": "tcp_connection",
            "update_events": [
                "hand_start",
                "street_start",
                "opponent_action",
                "settlement",
                "showdown",
            ],
            "snapshot_field": "opponent_runtime",
            "max_recent_hands": 8,
            "prior_rule": "Beta-style prior with weight 8",
            "confidence_rule": "actions / (actions + 24)",
            "adaptation_cap": 0.65,
            "consumer": "strategy.get_baseline_action",
        },
        "official_feedback_refs": [],
        "forbidden_runtime_work": ["full-match history scan in get_action"],
    }


def test_quality_failure_routes_one_structured_architecture_repair(tmp_path, monkeypatch):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    candidate = _write_bot(tmp_path / "national_v2", complete=False)
    policy = build_architecture_policy(source)
    transition = evaluate_architecture_transition(source, candidate, expected_policy=policy)
    inherited = _match_memory_contract()
    ckpt = {
        "next_v": 2,
        "source_v": 1,
        "stage": "quality_failed",
        "master_plan": {
            "architecture_policy": policy,
            "tasks": [{
                "architecture_focus_id": "national_runtime_v3_migration",
                "skill_layer": "runtime_architecture",
                "runtime_contract": inherited,
            }],
        },
        "gate_results": {
            "quality": {
                "all_passed": False,
                "failed_gates": ["national_capability_contract(architecture focus)"],
                "national_architecture_transition": transition,
            }
        },
    }
    monkeypatch.setattr(tool_planning, "get_bot_dir", lambda _v: candidate)

    contracts = tool_planning._quality_repair_contracts(ckpt)
    tasks = tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt)

    assert len(contracts) == 1
    assert contracts[0]["blocker"] == "runtime_architecture"
    assert contracts[0]["file"] == "strategy.py"
    repaired_contract = contracts[0]["runtime_contract"]
    assert repaired_contract["match_memory"] == inherited["match_memory"]
    assert repaired_contract["decision"] is not None
    assert repaired_contract["precompute_artifacts"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["repair_blocker"] == "runtime_architecture"
    assert task["architecture_focus_id"] == "national_runtime_v3_migration"
    assert task["skill_layer"] == "runtime_architecture"
    assert task["must_change_files"] == ["strategy.py"]
    assert "national_bot.py" in task["files_allowed"]
    assert "label, comment, or telemetry field" in task["worker_prompt"]
    assert "opponent_runtime" in task["worker_prompt"]
    assert tool_planning._task_quality_recheck_blockers(task) == {"runtime_architecture"}
    RuntimeContract.model_validate(task["runtime_contract"])
    assert task["runtime_contract"] == repaired_contract
    failures = tool_planning._quality_failure_items(ckpt)
    assert any("runtime_architecture_focus:incremental_opponent_model" in item for item in failures)
    assert any("runtime_architecture_focus:decision_path_no_full_history_scan" in item for item in failures)


def test_crossover_architecture_repair_gets_valid_default_runtime_contract(tmp_path, monkeypatch):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    candidate = _write_bot(tmp_path / "national_v2", complete=False)
    policy = build_architecture_policy(source)
    transition = evaluate_architecture_transition(source, candidate, expected_policy=policy)
    ckpt = {
        "next_v": 2,
        "source_v": 1,
        "parent2_v": 9,
        "stage": "quality_failed",
        "master_plan": {"strategy": "crossover", "architecture_policy": policy, "tasks": []},
        "gate_results": {
            "quality": {
                "all_passed": False,
                "failed_gates": ["national_capability_contract(architecture focus)"],
                "national_architecture_transition": transition,
            }
        },
    }
    monkeypatch.setattr(tool_planning, "get_bot_dir", lambda _v: candidate)

    task = tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt)[0]

    validated = RuntimeContract.model_validate(task["runtime_contract"])
    assert validated.match_memory is not None
    assert validated.match_memory.reset_boundary == "tcp_connection"
    assert validated.match_memory.consumer == "strategy.get_baseline_action"


def test_policy_identity_drift_is_not_routed_to_bot_code_worker(tmp_path, monkeypatch):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    candidate = _write_bot(tmp_path / "national_v2", complete=True)
    policy = build_architecture_policy(source)
    policy["source_capability_digest"] = "stale"
    transition = evaluate_architecture_transition(source, candidate, expected_policy=policy)
    ckpt = {
        "next_v": 2,
        "source_v": 1,
        "stage": "quality_failed",
        "master_plan": {"architecture_policy": policy, "tasks": []},
        "gate_results": {
            "quality": {
                "all_passed": False,
                "failed_gates": ["national_capability_contract(architecture policy identity)"],
                "national_architecture_transition": transition,
            }
        },
    }
    monkeypatch.setattr(tool_planning, "get_bot_dir", lambda _v: candidate)

    assert tool_planning._quality_repair_contracts(ckpt) == []


def test_runtime_architecture_repair_task_refreshes_when_contract_is_missing(tmp_path, monkeypatch):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    candidate = _write_bot(tmp_path / "national_v2", complete=False)
    policy = build_architecture_policy(source)
    transition = evaluate_architecture_transition(source, candidate, expected_policy=policy)
    ckpt = {
        "next_v": 2,
        "source_v": 1,
        "stage": "quality_failed",
        "master_plan": {"architecture_policy": policy, "tasks": []},
        "gate_results": {
            "quality": {
                "all_passed": False,
                "failed_gates": ["national_capability_contract(architecture focus)"],
                "national_architecture_transition": transition,
            }
        },
    }
    monkeypatch.setattr(tool_planning, "get_bot_dir", lambda _v: candidate)
    current = tool_planning._quality_repair_contracts(ckpt)[0]
    stale_task = {
        "worker_id": "old",
        "target_files": current["files"],
        "must_change_files": [current["file"]],
        "repair_blocker": "runtime_architecture",
        "repair_contract": {"blocker": "runtime_architecture", "file": current["file"]},
        "architecture_focus_id": current["focus_id"],
        "skill_layer": current["skill_layer"],
        "checks_required": current["required_checks"],
        "worker_prompt": "runtime architecture repair without its executable contract",
    }

    reason = tool_planning._quality_task_contract_refresh_reason(stale_task, current)

    assert reason.endswith("runtime_contract_changed")


def test_runtime_contract_owner_must_be_in_worker_scope():
    task = {
        "architecture_focus_id": "incremental_match_model",
        "skill_layer": "opponent_model",
        "target_files": ["strategy.py"],
        "files_allowed": [],
        "runtime_contract": _match_memory_contract(),
        "worker_prompt": "Consume opponent_runtime memory with confidence in the decision path.",
    }

    errors = tool_planning._runtime_contract_errors(task, 0, "opponent_model")

    assert any("national_bot.py" in error and "outside" in error for error in errors)


def test_runtime_contract_is_checked_against_candidate_evidence(tmp_path):
    candidate = _write_bot(tmp_path / "national_v2", complete=True)
    capabilities = evaluate_architecture_transition(candidate, candidate)["candidate_capabilities"]
    plan = {
        "tasks": [{
            "runtime_contract": _match_memory_contract(),
        }],
    }

    assert validate_runtime_contract_implementation(plan, capabilities) == []

    plan["tasks"][0]["runtime_contract"]["match_memory"]["adaptation_cap"] = 0.5
    errors = validate_runtime_contract_implementation(plan, capabilities)
    assert any("adaptation cap" in error and "0.65" in error for error in errors)


def test_declared_precompute_and_budget_must_match_live_artifacts(tmp_path):
    candidate = _write_bot(tmp_path / "national_v2", complete=True)
    capabilities = evaluate_architecture_transition(candidate, candidate)["candidate_capabilities"]
    contract = {
        "decision": {
            "clock": "time.monotonic",
            "hard_deadline_ms": 55_000,
            "baseline_target_ms": 250,
            "refinement_budget_ms": 54_000,
            "baseline_path": "strategy.get_action baseline",
            "fallback_action": "return sanitized baseline",
            "refinement_bound": "64 samples and deadline",
            "max_samples": 64,
        },
        "precompute_artifacts": [{
            "name": "EQUITY_LOOKUP_TABLE",
            "owner_file": "strategy.py",
            "build_phase": "module_import",
            "max_build_ms": 500,
            "max_entries": 1024,
            "max_bytes": 65536,
            "key_shape": "int",
            "consumer": "strategy.get_baseline_action",
            "fallback": "legal_baseline",
        }],
        "match_memory": None,
        "official_feedback_refs": [],
        "forbidden_runtime_work": [],
    }
    plan = {"tasks": [{"runtime_contract": contract}]}

    assert validate_runtime_contract_implementation(plan, capabilities) == []

    contract["decision"]["hard_deadline_ms"] = 54_500
    contract["precompute_artifacts"][0]["max_entries"] = 64
    errors = validate_runtime_contract_implementation(plan, capabilities)
    assert any(
        "hard_deadline_ms=54500" in error and "implementation default is 55000" in error
        for error in errors
    )
    assert any(
        "above declared max_entries=64" in error
        or "dynamic_precompute_entries:128>declared:64" in error
        for error in errors
    )


def test_runtime_contract_ledger_survives_unrelated_repair_task_replacement(tmp_path):
    candidate = _write_bot(tmp_path / "national_v2", complete=True)
    capabilities = evaluate_architecture_transition(candidate, candidate)["candidate_capabilities"]
    original = {
        "tasks": [{
            "worker_id": 1,
            "skill_layer": "runtime_architecture",
            "architecture_focus_id": "deadline_refinement",
            "runtime_contract": {
                "decision": {
                    "clock": "time.monotonic",
                    "hard_deadline_ms": 54_500,
                    "baseline_target_ms": 250,
                    "refinement_budget_ms": 54_000,
                    "baseline_path": "strategy.get_action baseline",
                    "fallback_action": "return sanitized baseline",
                    "refinement_bound": "64 samples and deadline",
                    "max_samples": 64,
                },
                "precompute_artifacts": [],
                "match_memory": None,
                "official_feedback_refs": [],
                "forbidden_runtime_work": [],
            },
        }],
    }
    original = attach_runtime_contract_ledger(original, replace=True)
    repaired = tool_planning._checkpoint_plan_with_tasks(
        {"master_plan": original},
        [{"worker_id": "file-size", "target_files": ["strategy.py"]}],
        replace_existing_tasks=True,
    )

    assert repaired["runtime_contract_ledger"] == original["runtime_contract_ledger"]
    errors = validate_runtime_contract_implementation(repaired, capabilities)
    assert any(
        "hard_deadline_ms=54500" in error and "implementation default is 55000" in error
        for error in errors
    )


def test_runtime_contract_ledger_tampering_fails_closed(tmp_path):
    candidate = _write_bot(tmp_path / "national_v2", complete=True)
    capabilities = evaluate_architecture_transition(candidate, candidate)["candidate_capabilities"]
    plan = attach_runtime_contract_ledger(
        {
            "tasks": [{
                "worker_id": 1,
                "skill_layer": "opponent_model",
                "architecture_focus_id": "incremental_match_model",
                "runtime_contract": _match_memory_contract(),
            }],
        },
        replace=True,
    )
    plan["runtime_contract_ledger"]["entries"][0]["runtime_contract"]["match_memory"][
        "adaptation_cap"
    ] = 0.5

    ledger_errors = validate_runtime_contract_ledger(plan["runtime_contract_ledger"])
    errors = validate_runtime_contract_implementation(plan, capabilities)

    assert any("digest_mismatch" in error for error in ledger_errors)
    assert any("runtime_contract_ledger" in error and "digest_mismatch" in error for error in errors)


def test_architecture_regression_skipper_uses_actual_check_files(tmp_path, monkeypatch):
    source = _write_bot(tmp_path / "national_v1", complete=True)
    candidate = _write_bot(tmp_path / "national_v2", complete=True, lose_wire=True)
    policy = build_architecture_policy(source)
    monkeypatch.setattr(tool_planning, "check_code_size", lambda *_a, **_k: (0, []))
    skipper = tool_planning._quality_rework_skipper(
        candidate,
        source,
        2,
        1,
        expected_architecture_policy=policy,
    )
    task = {
        "worker_id": "wire-regression",
        "target_files": ["national_bot.py"],
        "must_change_files": ["national_bot.py"],
        "repair_blocker": "runtime_architecture",
        "repair_contract": {
            "blocker": "runtime_architecture",
            "file": "national_bot.py",
        },
        "worker_prompt": "Restore architecture_regression:official_safe_wire_send.",
    }

    assert skipper(task) == ""


@pytest.mark.parametrize("parent2_v", [None, 9])
def test_policy_identity_recovery_resets_candidate_and_replans(tmp_path, monkeypatch, parent2_v):
    source = tmp_path / "national_v1"
    candidate = tmp_path / "national_v2"
    source.mkdir()
    candidate.mkdir()
    (source / "strategy.py").write_text("SOURCE = True\n", encoding="utf-8")
    (candidate / "strategy.py").write_text("STALE = True\n", encoding="utf-8")
    (candidate / "new_stale.py").write_text("STALE = True\n", encoding="utf-8")
    ckpt = {
        "next_v": 2,
        "source_v": 1,
        "parent2_v": parent2_v,
        "stage": "quality_failed",
        "direction_audit": {"suggested_direction": "runtime"},
        "gate_results": {
            "quality": {
                "national_architecture_transition": {
                    "ok": False,
                    "policy_identity_errors": ["architecture_policy_contract_digest_mismatch"],
                }
            }
        },
    }
    writes = []
    monkeypatch.setattr(
        tool_planning,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_a, **_k: None)

    result = tool_planning._recover_architecture_policy_identity(ckpt, candidate, source)

    assert (candidate / "strategy.py").read_text(encoding="utf-8") == "SOURCE = True\n"
    assert not (candidate / "new_stale.py").exists()
    assert writes[0][0][2] == "direction_audited"
    assert writes[0][1]["master_plan"] == {}
    assert writes[0][1]["clear_reviewer_feedback"] is True
    payload = result["content"][0]["text"]
    assert "ARCHITECTURE_POLICY_IDENTITY_REPLAN" in payload
    assert validate_stage_transition("quality_failed", "direction_audited")[0] is True
