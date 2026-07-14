import ast
from copy import deepcopy
import json
from pathlib import Path

import pytest

from output_schema import (
    LEGACY_CONSUMER_MIGRATION_CHECKS,
    LEGACY_CONSUMER_MIGRATION_FILES,
    LEGACY_CONSUMER_MIGRATION_FOCUS_ID,
    MasterPlan,
    RuntimeContract,
    STATE_LEARNING_PRIMARY_CHECKS,
)
from plan_compiler import bind_system_owned_legacy_consumer_migration
from pipeline_state import validate_stage_transition
from runtime_architecture_policy import (
    ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
    OFFICIAL_FULL_POLICY_ID,
    OFFICIAL_ORACLE_DOC_DIGESTS,
    PREPARED_CAPABILITY_SNAPSHOT_SCHEMA_VERSION,
    RUNTIME_ARCHITECTURE_POLICY_VERSION,
    RUNTIME_CORRECTNESS_FLOOR_CHECKS,
    RUNTIME_FLOOR_CHECKS,
    STATE_LEARNING_INNOVATION_CHECKS,
    attach_runtime_contract_ledger,
    architecture_policy_prompt,
    build_architecture_policy,
    build_prepared_capability_snapshot,
    crossover_architecture_policy_prompt,
    evaluate_architecture_transition,
    prepared_capability_snapshot_digest,
    validate_prepared_capability_snapshot,
    validate_runtime_contract_ledger,
    validate_plan_architecture_focus,
    validate_runtime_contract_implementation,
)
import tool_planning
from national_native import NATIVE_BOT_TEMPLATE
from national_capability_contract import (
    _decision_graph,
    _function_profiles,
    _incremental_model_evidence,
)


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
    hand_runtime = req.get('hand_runtime', {})
    if hand_runtime.get('can_donk'):
        return 600
    if hand_runtime.get('can_delayed_probe'):
        return 500
    if profile.get('fold_to_jam_samples', 0) >= 10:
        pressure = profile.get('fold_to_raise', 0.0) + profile.get('fold_to_jam_rate', 0.0) - profile.get('river_overcall_freq', 0.0)
        return -2 if pressure > 0.5 else 0
    revealed = profile.get('showdown_range', {})
    if (revealed.get('selection_scope') == 'reached_showdown_only'
            and revealed.get('confidence', 0.0) > 0.0
            and revealed.get('adaptation_weight', 0.0) > 0.0
            and (revealed.get('tightness', 0.0) > 0.30
                 or revealed.get('bucket_rates', {}).get('premium', 0.0) > 0.2)):
        return -1
    adaptation = profile.get('adaptation_weight', 0.0)
    threshold = 0.4 if equity_lookup(60) else 0.6
    adjusted = adaptation * (1.0 if profile.get('vpip', 0.0) > 0.5 else 0.2)
    deadline = time.monotonic() + 0.1
    for sample in range(64):
        if time.monotonic() >= deadline:
            break
        adjusted += sample * 0
    return 400 if adjusted > threshold else 0
def get_action(req, current_view):
    return _choose(req)
def get_baseline_action(req, current_view):
    return _choose(req)
def iter_refinements(req, requests, baseline, deadline):
    refined = -1 if baseline == 0 and req.get('to_call', 0) > 0 else baseline
    for step in range(1, 9):
        if time.monotonic() >= deadline:
            return
        work_checksum = 0
        for outer in range(100):
            for unit in range(100):
                for lane in range(4):
                    work_checksum = (
                        work_checksum * 33 + outer + unit + lane
                    ) & 0xffffffff
        if work_checksum < 0:
            return
        yield {'action': refined, 'sample_count': step * 8, 'confidence': step / 8.0, 'complete': False}
"""
    else:
        strategy = """
def get_action(req, requests):
    total = 0
    for item in requests: total += 1
    return total
"""
    (root / "strategy.py").write_text(strategy, encoding="utf-8")
    for helper in ("opponent.py", "simulation.py", "donk_probe.py"):
        (root / helper).write_text(
            f"# {helper} is part of the strategy consumer ABI.\n",
            encoding="utf-8",
        )
    return root


def _capability_result(state: dict[str, bool]) -> dict:
    checks = [
        {
            "check_id": check_id,
            "passed": bool(passed),
            "guidance": f"repair {check_id}",
            "evidence": {"locations": [f"strategy.py:{check_id}"]},
        }
        for check_id, passed in sorted(state.items())
    ]
    return {
        "detector_version": "prepared-baseline-test-detector",
        "checks": checks,
        "checks_by_id": {item["check_id"]: item for item in checks},
        "required_failures": [],
        "infrastructure_failures": [],
        "outcome": "passed",
    }


def test_policy_selects_system_owned_legacy_consumer_migration_first(tmp_path):
    source = _write_bot(tmp_path / "national_v1", complete=False)

    policy = build_architecture_policy(source)

    assert policy["policy_version"] == RUNTIME_ARCHITECTURE_POLICY_VERSION
    assert policy["official_policy_id"] == OFFICIAL_FULL_POLICY_ID
    assert policy["official_oracle_digests"] == OFFICIAL_ORACLE_DOC_DIGESTS
    assert policy["selected_focus"]["focus_id"] == (
        LEGACY_CONSUMER_MIGRATION_FOCUS_ID
    )
    assert set(policy["selected_focus"]["required_checks"]) == set(
        RUNTIME_FLOOR_CHECKS
    )
    assert policy["legacy_consumer_migration_failures"] == list(
        LEGACY_CONSUMER_MIGRATION_CHECKS
    )
    bundle = policy["legacy_consumer_migration_bundle"]
    assert bundle["required_checks"] == list(LEGACY_CONSUMER_MIGRATION_CHECKS)
    assert bundle["consumer_files"] == list(LEGACY_CONSUMER_MIGRATION_FILES)
    assert len(bundle["bundle_digest"]) == 64
    assert len(policy["source_capability_digest"]) == 64
    rendered = architecture_policy_prompt(policy)
    assert f"official_policy_id={OFFICIAL_FULL_POLICY_ID}" in rendered
    for path, digest in OFFICIAL_ORACLE_DOC_DIGESTS.items():
        assert f"{path}:{digest}" in rendered
    assert f"migration_bundle_digest={bundle['bundle_digest']}" in rendered


def test_fast_baseline_is_a_universal_correctness_floor():
    assert "fast_strategy_baseline" in RUNTIME_CORRECTNESS_FLOOR_CHECKS
    assert "fast_strategy_baseline" not in STATE_LEARNING_INNOVATION_CHECKS


def test_semantic_line_aggregate_is_compatibility_only_not_a_primary():
    assert STATE_LEARNING_PRIMARY_CHECKS["donk"] == (
        "donk_line_reachability",
    )
    assert STATE_LEARNING_PRIMARY_CHECKS["delayed_probe"] == (
        "delayed_probe_line_reachability",
    )
    assert "semantic_line_reachability" not in RUNTIME_FLOOR_CHECKS
    assert "semantic_line_reachability" not in STATE_LEARNING_INNOVATION_CHECKS


def test_prepared_capability_snapshot_is_serializable_and_digest_bound(tmp_path):
    parent = tmp_path / "national_v1"
    prepared = tmp_path / "national_v2"
    parent.mkdir()
    prepared.mkdir()
    parent_capabilities = _capability_result({
        "official_safe_wire_send": True,
        "precompute_lookup_path": False,
    })
    prepared_capabilities = _capability_result({
        "official_safe_wire_send": True,
        "precompute_lookup_path": True,
    })

    snapshot = build_prepared_capability_snapshot(
        parent,
        prepared,
        parent_capabilities=parent_capabilities,
        prepared_capabilities=prepared_capabilities,
    )

    assert snapshot["schema_version"] == PREPARED_CAPABILITY_SNAPSHOT_SCHEMA_VERSION
    assert snapshot["acquired_checks"] == ["precompute_lookup_path"]
    assert snapshot["protected_passed_checks"] == [
        "official_safe_wire_send",
        "precompute_lookup_path",
    ]
    assert len(json.dumps(snapshot, sort_keys=True)) > 0
    assert validate_prepared_capability_snapshot(
        snapshot,
        parent_bot_dir=parent,
        prepared_bot_dir=prepared,
        parent_capabilities=parent_capabilities,
        prepared_capabilities=prepared_capabilities,
    ) == []
    assert prepared_capability_snapshot_digest(snapshot) == snapshot["snapshot_digest"]

    tampered = {**snapshot, "acquired_checks": []}
    errors = validate_prepared_capability_snapshot(tampered)
    assert "prepared_capability_snapshot_acquired_checks_mismatch" in "\n".join(errors)
    assert "prepared_capability_snapshot_digest_mismatch" in errors
    assert prepared_capability_snapshot_digest(tampered) == ""


def test_prepared_child_acquired_capability_is_a_final_regression_baseline(
    tmp_path, monkeypatch
):
    import runtime_architecture_policy as policy_module

    parent = tmp_path / "national_v1"
    prepared = tmp_path / "national_v2"
    parent.mkdir()
    prepared.mkdir()
    parent_state = {check_id: True for check_id in RUNTIME_FLOOR_CHECKS}
    parent_state.update({
        "official_safe_wire_send": True,
        "precompute_lookup_path": False,
    })
    prepared_state = {**parent_state, "precompute_lookup_path": True}
    final_state = {**prepared_state, "precompute_lookup_path": False}
    parent_capabilities = _capability_result(parent_state)
    prepared_capabilities = _capability_result(prepared_state)
    final_capabilities = _capability_result(final_state)
    snapshot = build_prepared_capability_snapshot(
        parent,
        prepared,
        parent_capabilities=parent_capabilities,
        prepared_capabilities=prepared_capabilities,
    )
    policy = build_architecture_policy(
        parent,
        source_capabilities=parent_capabilities,
        prepared_capability_snapshot=snapshot,
    )
    responses = {
        parent.resolve(): parent_capabilities,
        prepared.resolve(): final_capabilities,
    }
    monkeypatch.setattr(
        policy_module,
        "evaluate_national_capabilities",
        lambda path: responses[Path(path).resolve()],
    )

    transition = evaluate_architecture_transition(
        parent,
        prepared,
        expected_policy=policy,
    )

    assert "precompute_lookup_path" in policy["baseline_passed_checks"]
    assert policy["prepared_capability_snapshot_digest"] == snapshot["snapshot_digest"]
    assert transition["ok"] is False
    regression = next(
        item
        for item in transition["regressions"]
        if item["check_id"] == "precompute_lookup_path"
    )
    assert regression["baseline_origin"] == "prepared_child"


def test_prepared_child_closed_floor_debt_is_not_reassigned_to_master(tmp_path):
    parent = tmp_path / "national_v1"
    prepared = tmp_path / "national_v2"
    parent.mkdir()
    prepared.mkdir()
    parent_state = {check_id: True for check_id in RUNTIME_FLOOR_CHECKS}
    parent_state["fast_strategy_baseline"] = False
    parent_state["decision_path_no_full_history_scan"] = False
    prepared_state = {check_id: True for check_id in RUNTIME_FLOOR_CHECKS}
    parent_capabilities = _capability_result(parent_state)
    prepared_capabilities = _capability_result(prepared_state)
    snapshot = build_prepared_capability_snapshot(
        parent,
        prepared,
        parent_capabilities=parent_capabilities,
        prepared_capabilities=prepared_capabilities,
    )

    parent_policy = build_architecture_policy(
        parent,
        source_capabilities=parent_capabilities,
    )
    prepared_policy = build_architecture_policy(
        parent,
        source_capabilities=parent_capabilities,
        prepared_capability_snapshot=snapshot,
    )

    assert set(parent_policy["plan_required_floor_checks"]) == {
        "fast_strategy_baseline",
        "decision_path_no_full_history_scan",
    }
    assert prepared_policy["source_floor_failures"] == [
        "fast_strategy_baseline",
        "decision_path_no_full_history_scan",
    ]
    assert prepared_policy["baseline_floor_failures"] == []
    assert prepared_policy["plan_required_floor_checks"] == []
    assert prepared_policy["effective_baseline_bot"] == prepared.name
    assert prepared_policy["effective_baseline_checks"]["fast_strategy_baseline"] is True


def test_single_parent_policy_keeps_source_as_effective_baseline(tmp_path):
    parent = tmp_path / "national_v1"
    parent.mkdir()
    state = {check_id: True for check_id in RUNTIME_FLOOR_CHECKS}
    state["fast_strategy_baseline"] = False
    capabilities = _capability_result(state)

    policy = build_architecture_policy(parent, source_capabilities=capabilities)

    assert policy["prepared_capability_snapshot"] is None
    assert policy["prepared_capability_snapshot_digest"] is None
    assert policy["effective_baseline_bot"] == parent.name
    assert policy["effective_baseline_checks"] == policy["source_checks"]
    assert policy["baseline_passed_checks"] == sorted(
        check_id for check_id, passed in policy["source_checks"].items() if passed
    )
    assert policy["plan_required_floor_checks"] == ["fast_strategy_baseline"]


def test_policy_fails_closed_when_pinned_official_oracle_content_drifts(
    tmp_path, monkeypatch
):
    import runtime_architecture_policy as policy_module

    source = _write_bot(tmp_path / "national_v1", complete=False)
    monkeypatch.setattr(
        policy_module,
        "OFFICIAL_ORACLE_DOC_DIGESTS",
        {
            **OFFICIAL_ORACLE_DOC_DIGESTS,
            "docs/official-raise-boundary-oracle-2026-07-11.md": "0" * 64,
        },
    )

    with pytest.raises(RuntimeError, match="official oracle document digest mismatch"):
        build_architecture_policy(source)


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
    assert "persistent_match_memory" in failed["unresolved_focus_checks"]
    assert "decision_path_no_full_history_scan" in failed["unresolved_focus_checks"]
    assert passed["ok"] is True
    assert lost["ok"] is False
    assert any(item["check_id"] == "official_safe_wire_send" for item in lost["regressions"])


def test_preplan_transition_defers_only_master_owned_source_debt(tmp_path, monkeypatch):
    import runtime_architecture_policy as policy_module

    source = tmp_path / "national_v1"
    candidate = tmp_path / "national_v2"
    source.mkdir()
    candidate.mkdir()

    provider_checks = {
        "decision_time_budget_visible",
        "killable_decision_runtime",
        "persistent_match_memory",
        "terminal_response_memory",
        "showdown_range_posterior",
        "authoritative_hand_context",
    }
    plan_checks = {
        "fast_strategy_baseline",
        "decision_path_no_full_history_scan",
        "decision_path_no_large_runtime_tables",
    }

    def capabilities(*, candidate_side: bool, lose_parent_capability: bool = False):
        state = {
            check_id: bool(candidate_side and check_id in provider_checks)
            for check_id in provider_checks | plan_checks
        }
        state.update({
            check_id: True for check_id in LEGACY_CONSUMER_MIGRATION_CHECKS
        })
        state["official_safe_wire_send"] = not lose_parent_capability
        checks = [
            {
                "check_id": check_id,
                "passed": passed,
                "guidance": f"repair {check_id}",
                "evidence": {"locations": [f"strategy.py:{check_id}"]},
            }
            for check_id, passed in sorted(state.items())
        ]
        return {
            "detector_version": "test-detector",
            "checks": checks,
            "checks_by_id": {item["check_id"]: item for item in checks},
            "required_failures": [],
            "infrastructure_failures": [],
            "outcome": "passed",
        }

    source_capabilities = capabilities(candidate_side=False)
    candidate_capabilities = capabilities(candidate_side=True)
    responses = {
        source.resolve(): source_capabilities,
        candidate.resolve(): candidate_capabilities,
    }
    monkeypatch.setattr(
        policy_module,
        "evaluate_national_capabilities",
        lambda path: responses[Path(path).resolve()],
    )
    policy = build_architecture_policy(
        source,
        source_capabilities=source_capabilities,
    )

    final = evaluate_architecture_transition(
        source,
        candidate,
        expected_policy=policy,
    )
    preplan = evaluate_architecture_transition(
        source,
        candidate,
        expected_policy=policy,
        evaluation_phase=ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
    )

    assert final["ok"] is False
    assert {item["check_id"] for item in final["runtime_floor_failures"]} == plan_checks
    assert preplan["ok"] is True
    assert preplan["runtime_floor_failures"] == []
    assert set(preplan["deferred_runtime_floor_checks"]) == plan_checks
    assert {
        item["check_id"] for item in preplan["deferred_runtime_floor_failures"]
    } == plan_checks
    assert set(preplan["full_unresolved_focus_checks"]) == plan_checks
    assert set(preplan["deferred_unresolved_focus_checks"]) == plan_checks
    assert preplan["unresolved_focus_checks"] == []

    provider_failure = capabilities(candidate_side=True)
    for item in provider_failure["checks"]:
        if item["check_id"] == "killable_decision_runtime":
            item["passed"] = False
    provider_failure["checks_by_id"] = {
        item["check_id"]: item for item in provider_failure["checks"]
    }
    responses[candidate.resolve()] = provider_failure
    blocked = evaluate_architecture_transition(
        source,
        candidate,
        expected_policy=policy,
        evaluation_phase=ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
    )
    assert blocked["ok"] is False
    assert "killable_decision_runtime" in blocked["unresolved_focus_checks"]

    regressed = capabilities(candidate_side=True, lose_parent_capability=True)
    responses[candidate.resolve()] = regressed
    lost = evaluate_architecture_transition(
        source,
        candidate,
        expected_policy=policy,
        evaluation_phase=ARCHITECTURE_TRANSITION_PHASE_PREPLAN,
    )
    assert lost["ok"] is False
    assert any(
        item["check_id"] == "official_safe_wire_send"
        for item in lost["regressions"]
    )


def test_crossover_policy_prompt_keeps_master_contract_deferred(tmp_path):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    policy = build_architecture_policy(source)

    rendered = crossover_architecture_policy_prompt(policy)

    assert "prepares a recombination baseline" in rendered
    assert "plan_required_floor_checks are deliberately deferred" in rendered
    assert "direction audit, literature probe, Master, and Workers" in rendered
    assert "do not emit or simulate downstream planning objects" in rendered
    assert "traceable Parent B component" in rendered
    assert "crossover makes no independent strategic innovation" in rendered
    assert "required_worker_prompt_terms" not in rendered
    assert "exactly one task MUST" not in rendered


def test_transition_rejects_unknown_evaluation_phase(tmp_path):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    candidate = _write_bot(tmp_path / "national_v2", complete=False)

    with pytest.raises(ValueError, match="unknown architecture transition evaluation phase"):
        evaluate_architecture_transition(
            source,
            candidate,
            evaluation_phase="crossover_magic",
        )


def test_unselected_precompute_dimension_is_shadow_not_a_universal_floor(tmp_path):
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

    assert transition["unresolved_focus_checks"] == []
    assert transition["runtime_floor_failures"] == []
    assert transition["ok"] is True
    shadows = {
        item["check_id"]: item["passed"]
        for item in transition["strategy_shadow_checks"]
    }
    assert shadows["precompute_lookup_path"] is False


def test_policy_identity_detects_stale_source_contract(tmp_path):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    candidate = _write_bot(tmp_path / "national_v2", complete=True)
    policy = build_architecture_policy(source)
    policy["source_capability_digest"] = "stale"

    result = evaluate_architecture_transition(source, candidate, expected_policy=policy)

    assert result["ok"] is False
    assert any("source_capability_digest_mismatch" in item for item in result["policy_identity_errors"])


def test_policy_digest_binds_strategy_reference_pack_registry(tmp_path):
    import runtime_architecture_policy as policy_module

    source = _write_bot(tmp_path / "national_v1", complete=False)
    policy = build_architecture_policy(source)
    original_digest = policy["policy_digest"]
    policy["strategy_reference_pack_digest"] = "0" * 64

    assert policy_module._policy_contract_digest(policy) != original_digest
    errors = policy_module._policy_identity_errors(
        policy,
        build_architecture_policy(source),
    )
    assert any("strategy_reference_pack_digest_mismatch" in error for error in errors)
    assert any("expected_content_digest_mismatch" in error for error in errors)


@pytest.mark.parametrize("field", ["required_checks", "suggested_files", "accepted_skill_layers"])
def test_policy_identity_detects_focus_contract_tampering(tmp_path, field):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    candidate = _write_bot(tmp_path / "national_v2", complete=True)
    policy = build_architecture_policy(source)
    policy["selected_focus"][field] = []

    result = evaluate_architecture_transition(source, candidate, expected_policy=policy)

    assert result["ok"] is False
    assert any("expected_content_digest_mismatch" in item for item in result["policy_identity_errors"])


def test_system_compiler_restores_migration_when_weak_plan_omits_it(tmp_path):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    policy = build_architecture_policy(source)
    weak_plan = {
        "analysis": "The weak planner emitted a generic one-file task.",
        "targeted_failure": "generic strategy issue",
        "tasks": [
            {
                "worker_id": 1,
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py"],
                "skill_layer": "line_template",
                "checks_required": [
                    "precompute_runtime_influence",
                    "semantic_line_reachability",
                ],
                "prohibited_files": ["opponent.py"],
                "instruction": "Implement ordinary river state_learning now.",
                "worker_prompt": (
                    "Implement an ordinary river state_learning primary beside "
                    "the migration and tune its thresholds."
                ),
            },
            {
                "worker_id": 2,
                "role": "Algorithmic Logic Architect",
                "target_files": ["postflop.py"],
                "skill_layer": "line_template",
                "worker_prompt": (
                    "Implement a second ordinary river adaptation in this generation."
                ),
            },
        ],
    }

    assert validate_plan_architecture_focus(weak_plan, policy)
    bound, meta = bind_system_owned_legacy_consumer_migration(
        weak_plan,
        policy=policy,
    )
    task = bound["tasks"][0]

    assert meta["bound"] is True
    assert len(bound["tasks"]) == 1
    assert meta["dropped_worker_ids"] == [2]
    assert task["architecture_focus_id"] == LEGACY_CONSUMER_MIGRATION_FOCUS_ID
    assert task["target_files"] == list(LEGACY_CONSUMER_MIGRATION_FILES[:3])
    assert task["files_allowed"] == [LEGACY_CONSUMER_MIGRATION_FILES[3]]
    assert set(LEGACY_CONSUMER_MIGRATION_FILES) == {
        *task["target_files"],
        *task["files_allowed"],
    }
    assert set(LEGACY_CONSUMER_MIGRATION_CHECKS).issubset(
        task["checks_required"]
    )
    assert set(task["checks_required"]) == {
        *policy["plan_required_floor_checks"],
        *LEGACY_CONSUMER_MIGRATION_CHECKS,
    }
    assert task["prohibited_files"] == []
    assert task["runtime_contract"]["state_learning"] is None
    assert task["runtime_contract"]["legacy_consumer_migration"] is not None
    assert "instruction" not in task
    assert "river" not in task["worker_prompt"].lower()
    assert "ordinary" not in task["worker_prompt"].lower()
    assert validate_plan_architecture_focus(bound, policy) == []
    MasterPlan.model_validate(bound)

    mixed = deepcopy(bound)
    mixed["tasks"].append({
        "worker_id": 2,
        "role": "Algorithmic Logic Architect",
        "target_files": ["postflop.py"],
        "skill_layer": "line_template",
        "worker_prompt": "Implement an ordinary river adaptation in parallel.",
    })
    with pytest.raises(ValueError, match="exactly one total worker task"):
        MasterPlan.model_validate(mixed)
    assert any(
        "exactly one total worker task" in error
        for error in validate_plan_architecture_focus(mixed, policy)
    )

    missing_file = deepcopy(bound)
    missing_file["tasks"][0]["files_allowed"] = []
    with pytest.raises(ValueError, match="requires writable target_files/files_allowed"):
        MasterPlan.model_validate(missing_file)
    assert any(
        "must have writable scope" in error
        for error in validate_plan_architecture_focus(missing_file, policy)
    )

    extra_file = deepcopy(bound)
    extra_file["tasks"][0]["files_allowed"].append("postflop.py")
    with pytest.raises(ValueError, match="unexpected files"):
        MasterPlan.model_validate(extra_file)
    assert any(
        "writable scope is exact" in error
        for error in validate_plan_architecture_focus(extra_file, policy)
    )

    extra_check = deepcopy(bound)
    extra_check["tasks"][0]["checks_required"].append(
        "precompute_runtime_influence"
    )
    with pytest.raises(ValueError, match="ordinary innovation or aggregate checks"):
        MasterPlan.model_validate(extra_check)
    assert any(
        "unexpected ordinary/aggregate checks" in error
        for error in validate_plan_architecture_focus(extra_check, policy)
    )
    assert any(
        "ordinary innovation or aggregate checks" in error
        for error in tool_planning._runtime_contract_errors(
            extra_check["tasks"][0],
            0,
            "runtime_architecture",
        )
    )
    rebound, rebound_meta = bind_system_owned_legacy_consumer_migration(
        bound,
        policy=policy,
    )
    assert rebound_meta["bound"] is True
    assert rebound == bound

    prompt = architecture_policy_prompt(policy)
    assert f"selected_focus={LEGACY_CONSUMER_MIGRATION_FOCUS_ID}" in prompt
    assert "required_worker_prompt_terms=" in prompt
    for term in policy["selected_focus"]["required_terms"]:
        assert term in prompt
    assert "system plan compiler discards other tasks" in prompt


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
            "prior_rule": "beta_prior_weight_8",
            "confidence_rule": (
                "global_actions_over_actions_plus_24_and_context_samples_over_samples_plus_8"
            ),
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
        "run_id": "2#0",
        "workflow_run_id": "test-architecture-recovery-2-1",
        "checkpoint_revision": 1,
        "stage": "quality_failed",
        "master_plan": {
            "architecture_policy": policy,
            "tasks": [{
                "architecture_focus_id": LEGACY_CONSUMER_MIGRATION_FOCUS_ID,
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
    assert repaired_contract["precompute_artifacts"] == []
    assert repaired_contract["state_learning"] is None
    assert repaired_contract["legacy_consumer_migration"][
        "required_checks"
    ] == list(LEGACY_CONSUMER_MIGRATION_CHECKS)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["repair_blocker"] == "runtime_architecture"
    assert task["architecture_focus_id"] == LEGACY_CONSUMER_MIGRATION_FOCUS_ID
    assert task["skill_layer"] == "runtime_architecture"
    assert task["must_change_files"] == ["strategy.py"]
    assert set(task["checks_required"]) == {
        *policy["plan_required_floor_checks"],
        *LEGACY_CONSUMER_MIGRATION_CHECKS,
    }
    assert "national_bot.py" not in task["files_allowed"]
    assert task["read_only_dependencies"] == ["national_bot.py"]
    assert set(LEGACY_CONSUMER_MIGRATION_FILES) == {
        *task["target_files"],
        *task["files_allowed"],
    }
    assert "label, comment, or telemetry field" in task["worker_prompt"]
    assert "opponent_runtime" in task["worker_prompt"]
    assert tool_planning._task_quality_recheck_blockers(task) == {
        "runtime_architecture",
        "reachability",
        "position_semantics",
    }
    RuntimeContract.model_validate(task["runtime_contract"])
    assert task["runtime_contract"] == repaired_contract
    failures = tool_planning._quality_failure_items(ckpt)
    assert any("runtime_architecture_focus:persistent_match_memory" in item for item in failures)
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
    assert validated.state_learning is None
    assert validated.legacy_consumer_migration is not None
    assert set(LEGACY_CONSUMER_MIGRATION_FILES) == {
        *task["target_files"],
        *task["files_allowed"],
    }


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


def test_system_provider_owner_is_read_only_without_expanding_write_scope():
    task = {
        "architecture_focus_id": "incremental_match_model",
        "skill_layer": "opponent_model",
        "target_files": ["strategy.py"],
        "files_allowed": [],
        "read_only_dependencies": ["national_bot.py"],
        "runtime_contract": _match_memory_contract(),
        "worker_prompt": "Consume opponent_runtime match memory with confidence.",
    }

    assert tool_planning._runtime_contract_errors(task, 0, "opponent_model") == []

    task["read_only_dependencies"] = []
    task["files_allowed"] = ["national_bot.py"]
    errors = tool_planning._runtime_contract_errors(task, 0, "opponent_model")
    assert any("system-provided national_bot.py" in error for error in errors)


def test_repair_contract_prefers_candidate_consumed_precompute_artifact():
    capabilities = {
        "precompute_evidence": {
            "consumed_artifacts": [{
                "name": "EQUITY_FACTS",
                "location": "strategy.py:L4:EQUITY_FACTS",
                "build_phase": "module_import",
                "bound_entries": 128,
                "consumer_locations": [
                    "strategy.py:get_baseline_action->strategy.py:L4:EQUITY_FACTS"
                ],
            }],
        },
        "dynamic_runtime_probe": {
            "artifacts": [{
                "owner_file": "strategy.py",
                "name": "EQUITY_FACTS",
                "entries": 128,
                "deep_bytes": 8192,
                "import_elapsed_ms": 12.5,
                "observed_key_shape": "int",
            }],
        },
    }

    contract = tool_planning._architecture_default_runtime_contract(
        "national_runtime_v4_state_learning",
        "precompute",
        "strategy.py",
        required_checks=["precompute_lookup_path"],
        candidate_capabilities=capabilities,
    )

    artifact = contract["precompute_artifacts"][0]
    assert artifact["name"] == "EQUITY_FACTS"
    assert artifact["owner_file"] == "strategy.py"
    assert artifact["consumer"] == "strategy.get_baseline_action"
    assert artifact["max_entries"] == 128
    assert artifact["key_shape"] == "int"


def test_missing_candidate_precompute_never_synthesizes_generic_lookup_primary():
    contract = tool_planning._architecture_default_runtime_contract(
        "national_runtime_v4_state_learning",
        "precompute",
        "strategy.py",
        required_checks=["precompute_lookup_path"],
        candidate_capabilities={},
    )

    assert contract["precompute_artifacts"] == []
    assert contract["state_learning"]["work_primitive"] == "sample_counted_candidate_batch"
    assert contract["reference_pack_id"] == "range_weighted_candidate_batch_v1"


def test_v4_contract_allows_only_one_typed_primary_innovation():
    state = {
        "work_primitive": "sample_counted_candidate_batch",
        "profile_dimensions": ["terminal_response"],
        "line_controls": [],
        "oracle_refs": [
            "docs/official-raise-boundary-oracle-2026-07-11.md",
            "docs/official-terminal-settlement-oracle-2026-07-11.md",
        ],
    }

    with pytest.raises(ValueError, match="exactly one primary innovation"):
        RuntimeContract.model_validate({"state_learning": state})


def test_focus_validator_rejects_second_generation_primary(tmp_path):
    source = _write_bot(tmp_path / "national_v1", complete=False)
    state = {check_id: True for check_id in RUNTIME_FLOOR_CHECKS}
    state.update({
        check_id: False for check_id in STATE_LEARNING_INNOVATION_CHECKS
    })
    policy = build_architecture_policy(
        source,
        source_capabilities=_capability_result(state),
    )
    assert policy["selected_focus"]["focus_id"] == (
        "national_runtime_v4_state_learning"
    )
    oracle_refs = [
        "docs/official-raise-boundary-oracle-2026-07-11.md",
        "docs/official-terminal-settlement-oracle-2026-07-11.md",
    ]
    tasks = [
        {
            "worker_id": 1,
            "architecture_focus_id": "national_runtime_v4_state_learning",
            "skill_layer": "line_template",
            "target_files": ["strategy.py"],
            "checks_required": ["donk_line_reachability"],
            "runtime_contract": {
                "state_learning": {
                    "work_primitive": None,
                    "profile_dimensions": [],
                    "line_controls": ["donk"],
                    "oracle_refs": oracle_refs,
                },
            },
            "worker_prompt": "Use can_donk positive/control sanitized action telemetry.",
        },
        {
            "worker_id": 2,
            "architecture_focus_id": "",
            "skill_layer": "line_template",
            "target_files": ["postflop.py"],
            "checks_required": ["delayed_probe_line_reachability"],
            "runtime_contract": {
                "state_learning": {
                    "work_primitive": None,
                    "profile_dimensions": [],
                    "line_controls": ["delayed_probe"],
                    "oracle_refs": oracle_refs,
                },
            },
            "worker_prompt": (
                "Use can_delayed_probe positive/control sanitized action telemetry."
            ),
        },
    ]

    errors = validate_plan_architecture_focus({"tasks": tasks}, policy)

    assert any("exactly one state_learning primary across the entire generation" in error for error in errors)
    assert any("state_learning may appear only" in error for error in errors)


def test_focus_validator_makes_native_entrypoint_read_only_except_explicit_official_protocol_repair():
    policy = {"selected_focus": None, "plan_required_floor_checks": []}
    ordinary = {
        "worker_id": 1,
        "role": "Opponent Modeler",
        "task_kind": "strategy_change",
        "target_files": ["strategy.py", "national_bot.py"],
        "files_allowed": [],
    }
    errors = validate_plan_architecture_focus({"tasks": [ordinary]}, policy)
    assert any("national_bot.py is read-only" in error for error in errors)

    explicit_official = {
        "worker_id": "auto_official_full_repair",
        "role": "Protocol Integration Architect",
        "task_kind": "official_repair",
        "repair_blocker": "official_full",
        "target_files": ["national_bot.py"],
        "must_change_files": ["national_bot.py"],
        "files_allowed": [],
    }
    assert validate_plan_architecture_focus(
        {"tasks": [explicit_official]}, policy
    ) == []

    spoofed_state_learning = {
        **explicit_official,
        "architecture_focus_id": "national_runtime_v4_state_learning",
        "runtime_contract": {
            "state_learning": {
                "work_primitive": None,
                "profile_dimensions": ["terminal_response"],
                "line_controls": [],
                "oracle_refs": [
                    "docs/official-raise-boundary-oracle-2026-07-11.md",
                    "docs/official-terminal-settlement-oracle-2026-07-11.md",
                ],
            },
        },
    }
    spoofed_errors = validate_plan_architecture_focus(
        {"tasks": [spoofed_state_learning]}, policy
    )
    assert any("national_bot.py is read-only" in error for error in spoofed_errors)


def test_match_memory_prior_and_confidence_rules_are_closed_literals():
    contract = _match_memory_contract()
    contract["match_memory"]["prior_rule"] = "some prose about a prior"

    with pytest.raises(ValueError, match="prior_rule"):
        RuntimeContract.model_validate(contract)


def test_non_primary_strategy_dimensions_remain_shadow_in_implementation_gate():
    oracle_refs = [
        "docs/official-raise-boundary-oracle-2026-07-11.md",
        "docs/official-terminal-settlement-oracle-2026-07-11.md",
    ]
    plan = {"tasks": [{
        "runtime_contract": {
            "state_learning": {
                "work_primitive": None,
                "profile_dimensions": ["terminal_response"],
                "line_controls": [],
                "oracle_refs": oracle_refs,
            },
        },
    }]}
    capabilities = {
        "checks": [
            {"check_id": "terminal_response_adaptation", "passed": True},
            {"check_id": "showdown_range_adaptation", "passed": False},
            {"check_id": "semantic_line_reachability", "passed": False},
            {"check_id": "budget_scaled_refinement", "passed": False},
            {"check_id": "precompute_lookup_path", "passed": False},
        ],
    }

    assert validate_runtime_contract_implementation(plan, capabilities) == []


def test_selected_donk_control_does_not_require_delayed_probe_control():
    plan = {"tasks": [{
        "runtime_contract": {
            "state_learning": {
                "work_primitive": None,
                "profile_dimensions": [],
                "line_controls": ["donk"],
                "oracle_refs": [
                    "docs/official-raise-boundary-oracle-2026-07-11.md",
                    "docs/official-terminal-settlement-oracle-2026-07-11.md",
                ],
            },
        },
    }]}
    capabilities = {
        "checks": [
            {"check_id": "donk_line_reachability", "passed": True},
            {"check_id": "delayed_probe_line_reachability", "passed": False},
            {"check_id": "semantic_line_reachability", "passed": False},
        ],
        "incremental_model_evidence": {
            "decision_field_locations": {
                "hand_runtime": ["strategy.py:get_action"],
                "can_donk": ["strategy.py:get_action"],
                "can_delayed_probe": [],
            },
            "source_rooted_live_access_paths": {
                "hand_runtime.can_donk": ["strategy.py:get_action"],
            },
        },
        "dynamic_runtime_probe": {
            "failure_class": "none",
            "repeatability_ok": True,
            "evidence_integrity_ok": True,
            "migration_evidence_repeatability": {
                "schema_version": 1,
                "candidate_fingerprint_unchanged": True,
                "run_count": 2,
                "runs_eligible": True,
                "dimensions": {
                    "donk": {
                        "stable": True,
                        "authority_tier": "baseline",
                        "evidence_present": True,
                        "observations_identical": True,
                        "observation_digests": ["same", "same"],
                        "evidence": [{
                            "dimension": "donk",
                            "scenario_id": "flop_donk_vs_opponent_pfr",
                            "tier": "baseline",
                            "left_label": "positive",
                            "right_label": "negative",
                            "left_wire": "raise 600",
                            "right_wire": "check",
                            "control_kind": "same_scenario_flag_false",
                            "flag": "can_donk",
                        }],
                    },
                },
            },
            "strategy_influence": {
                "dimensions": {
                    "semantic_lines": {
                        "ok": False,
                        "rows": [
                            {
                                "dimension": "donk",
                                "scenario_id": "flop_donk_vs_opponent_pfr",
                                "control_kind": "same_scenario_flag_false",
                                "flag": "can_donk",
                                "tiers": {
                                    "baseline": {
                                        "positive": {"wire": "raise 600"},
                                        "negative": {"wire": "check"},
                                        "changed": True,
                                    },
                                    "short": {"changed": False},
                                    "long": {"changed": False},
                                },
                            },
                            {
                                "dimension": "delayed_probe",
                                "scenario_id": "turn_delayed_probe_vs_opponent_pfr",
                                "control_kind": "same_scenario_flag_false",
                                "flag": "can_delayed_probe",
                                "tiers": {
                                    "baseline": {"changed": False},
                                    "short": {"changed": False},
                                    "long": {"changed": False},
                                },
                            },
                        ],
                    },
                },
            },
        },
    }

    assert validate_runtime_contract_implementation(plan, capabilities) == []


@pytest.mark.parametrize("missing_check", LEGACY_CONSUMER_MIGRATION_CHECKS)
def test_each_legacy_consumer_keeps_system_migration_focus_active(
    tmp_path,
    missing_check,
):
    source = tmp_path / f"source_{missing_check}"
    source.mkdir()
    state = {check_id: True for check_id in RUNTIME_FLOOR_CHECKS}
    state.update({
        check_id: False for check_id in STATE_LEARNING_INNOVATION_CHECKS
    })
    state[missing_check] = False

    policy = build_architecture_policy(
        source,
        source_capabilities=_capability_result(state),
    )

    assert policy["selected_focus"]["focus_id"] == (
        LEGACY_CONSUMER_MIGRATION_FOCUS_ID
    )
    assert policy["legacy_consumer_migration_failures"] == [missing_check]
    assert policy["legacy_consumer_migration_bundle"]["required_checks"] == list(
        LEGACY_CONSUMER_MIGRATION_CHECKS
    )


def test_all_legacy_consumers_must_pass_before_one_primary_focus_resumes(tmp_path):
    source = tmp_path / "source_complete_migration"
    source.mkdir()
    state = {check_id: True for check_id in RUNTIME_FLOOR_CHECKS}
    state.update({
        check_id: False for check_id in STATE_LEARNING_INNOVATION_CHECKS
    })

    policy = build_architecture_policy(
        source,
        source_capabilities=_capability_result(state),
    )

    assert policy["legacy_consumer_migration_failures"] == []
    assert policy["legacy_consumer_migration_bundle"] is None
    assert policy["selected_focus"]["focus_id"] == (
        "national_runtime_v4_state_learning"
    )


def test_donk_passed_but_delayed_probe_missing_remains_final_blocking(
    tmp_path,
    monkeypatch,
):
    import runtime_architecture_policy as policy_module

    source = tmp_path / "national_v1"
    candidate = tmp_path / "national_v2"
    source.mkdir()
    candidate.mkdir()
    source_state = {check_id: True for check_id in RUNTIME_FLOOR_CHECKS}
    source_state["delayed_probe_line_reachability"] = False
    candidate_state = dict(source_state)
    source_capabilities = _capability_result(source_state)
    candidate_capabilities = _capability_result(candidate_state)
    responses = {
        source.resolve(): source_capabilities,
        candidate.resolve(): candidate_capabilities,
    }
    monkeypatch.setattr(
        policy_module,
        "evaluate_national_capabilities",
        lambda path: responses[Path(path).resolve()],
    )
    policy = build_architecture_policy(
        source,
        source_capabilities=source_capabilities,
    )

    transition = evaluate_architecture_transition(
        source,
        candidate,
        expected_policy=policy,
    )

    assert transition["ok"] is False
    assert transition["legacy_consumer_migration_failures"] == [
        "delayed_probe_line_reachability"
    ]
    assert {
        item["check_id"] for item in transition["runtime_floor_failures"]
    } == {"delayed_probe_line_reachability"}
    assert "donk_line_reachability" not in transition[
        "legacy_consumer_migration_failures"
    ]


def _source_rooted_incremental_evidence(strategy_source: str) -> dict:
    """Exercise AST evidence against real strategy source, without a sandbox run."""
    trees = {
        "strategy.py": ast.parse(strategy_source, filename="strategy.py"),
        "national_bot.py": ast.parse("", filename="national_bot.py"),
    }
    profiles, by_name = _function_profiles(trees)
    decision_chains, _roots = _decision_graph(profiles, by_name)
    return _incremental_model_evidence(
        {"strategy.py": strategy_source, "national_bot.py": ""},
        trees,
        profiles,
        decision_chains,
    )


def _lead_sizing_contract() -> dict:
    return {
        "decision": None,
        "precompute_artifacts": [{
            "name": "LEAD_TABLE",
            "owner_file": "strategy.py",
            "build_phase": "module_import",
            "max_build_ms": 500,
            "max_entries": 128,
            "max_bytes": 262_144,
            "key_shape": "int",
            "consumer": "strategy.get_baseline_action",
            "fallback": "legal_baseline",
        }],
        "match_memory": None,
        "state_learning": {
            "work_primitive": "bounded_precompute_lookup",
            "profile_dimensions": [],
            "line_controls": [],
            "oracle_refs": [
                "docs/official-raise-boundary-oracle-2026-07-11.md",
                "docs/official-terminal-settlement-oracle-2026-07-11.md",
            ],
        },
        "reference_pack_id": "lead_sizing_geometry_v1",
        "official_feedback_refs": [],
        "forbidden_runtime_work": [],
    }


def test_reference_card_requires_source_rooted_live_paths_not_literals():
    live_source = """
def _live_lead(req):
    hand = req.get('hand_runtime', {})
    terminal = req.get('opponent_runtime', {}).get('terminal_response', {})
    if (
        hand.get('street') == 'flop'
        and hand.get('spr', 99.0) < 5.0
        and hand.get('pot', 0) >= 400
        and hand.get('effective_stack', 0) >= 500
        and hand.get('hero_position') == 'bb'
        and hand.get('preflop_aggressor') == 'opponent'
        and hand.get('street_open')
        and terminal.get('confidence', 0.0) >= 0.20
    ):
        return 600
    return 0
def get_action(req, current_view):
    return _live_lead(req)
def get_baseline_action(req, current_view):
    return _live_lead(req)
"""
    dead_literal_source = """
def _dead_lead(req):
    hand = req.get('hand_runtime', {})
    terminal = req.get('opponent_runtime', {}).get('terminal_response', {})
    required = (
        'street', 'spr', 'pot', 'effective_stack', 'hero_position',
        'preflop_aggressor', 'street_open', 'terminal_response', 'confidence',
    )
    street = hand.get('street')
    spr = hand.get('spr')
    pot = hand.get('pot')
    stack = hand.get('effective_stack')
    position = hand.get('hero_position')
    aggressor = hand.get('preflop_aggressor')
    open_street = hand.get('street_open')
    confidence = terminal.get('confidence')
    return 0
def get_action(req, current_view):
    return _dead_lead(req)
def get_baseline_action(req, current_view):
    return _dead_lead(req)
"""
    live = _source_rooted_incremental_evidence(live_source)
    dead = _source_rooted_incremental_evidence(dead_literal_source)
    expected_paths = {
        "hand_runtime.street",
        "hand_runtime.spr",
        "hand_runtime.pot",
        "hand_runtime.effective_stack",
        "hand_runtime.hero_position",
        "hand_runtime.preflop_aggressor",
        "hand_runtime.street_open",
        "opponent_runtime.terminal_response.confidence",
    }

    assert expected_paths.issubset(live["source_rooted_live_access_paths"])
    assert dead["source_rooted_live_access_paths"] == {}
    # The old literal evidence *does* see the words in the dead helper.  This
    # guards the regression that previously let a weak worker satisfy a card by
    # parking a tuple of field names beside an unrelated fixed table lookup.
    assert dead["decision_field_locations"]["street"]
    assert dead["decision_field_function_locations"]["confidence"]

    base_capabilities = {
        "checks": [
            {"check_id": "precompute_lookup_path", "passed": True},
            {"check_id": "precompute_runtime_influence", "passed": True},
        ],
        "precompute_evidence": {"consumed_artifacts": []},
        "decision_path_risks": {},
    }
    plan = {"tasks": [{"runtime_contract": _lead_sizing_contract()}]}
    live_errors = validate_runtime_contract_implementation(
        plan,
        {**base_capabilities, "incremental_model_evidence": live},
    )
    dead_errors = validate_runtime_contract_implementation(
        plan,
        {**base_capabilities, "incremental_model_evidence": dead},
    )

    assert not any("lacks source-rooted" in error for error in live_errors)
    assert any("source-rooted live hand_runtime" in error for error in dead_errors)
    assert any("source-rooted confidence-scaled" in error for error in dead_errors)


def test_sample_primary_binds_max_samples_to_trusted_probe_steps_only():
    contract = {
        "decision": {
            "clock": "time.monotonic",
            "hard_deadline_ms": 55_000,
            "baseline_target_ms": 250,
            "refinement_budget_ms": 54_000,
            "baseline_path": "compute the legal strategy baseline first",
            "fallback_action": "return the sanitized legal fallback",
            "refinement_bound": "at most eight trusted iterator steps",
            "max_samples": 8,
        },
        "state_learning": {
            "work_primitive": "sample_counted_candidate_batch",
            "profile_dimensions": [],
            "line_controls": [],
            "oracle_refs": [
                "docs/official-raise-boundary-oracle-2026-07-11.md",
                "docs/official-terminal-settlement-oracle-2026-07-11.md",
            ],
        },
        "reference_pack_id": "range_weighted_candidate_batch_v1",
    }
    capabilities = {
        "checks": [
            {"check_id": "decision_time_budget_visible", "passed": True},
            {"check_id": "fast_strategy_baseline", "passed": True},
            {"check_id": "incremental_refinement_protocol", "passed": True},
            {"check_id": "budget_scaled_refinement", "passed": True},
        ],
        "decision_time_evidence": {
            "default_hard_deadline_ms": 55_000,
            "default_baseline_target_ms": 250,
            "default_refinement_budget_ms": 54_000,
        },
        "decision_runtime_evidence": {
            "budget_scaling": {
                "long": {
                    "trusted_steps": 8,
                    "reported_sample_count": 100_000,
                },
            },
        },
        "decision_path_risks": {},
        "incremental_model_evidence": {
            "decision_field_locations": {
                "street": ["strategy.py:get_baseline_action"],
                "pot": ["strategy.py:get_baseline_action"],
                "to_call": ["strategy.py:get_baseline_action"],
                "pot_odds": ["strategy.py:get_baseline_action"],
                "spr": ["strategy.py:get_baseline_action"],
                "terminal_response": ["strategy.py:get_baseline_action"],
                "confidence": ["strategy.py:get_baseline_action"],
            },
        },
    }
    plan = {"tasks": [{"runtime_contract": contract}]}

    assert validate_runtime_contract_implementation(plan, capabilities) == []

    capabilities["incremental_model_evidence"]["decision_field_locations"]["spr"] = []
    errors = validate_runtime_contract_implementation(plan, capabilities)
    assert any("range_weighted_candidate_batch_v1 lacks live hand_runtime" in error for error in errors)
    capabilities["incremental_model_evidence"]["decision_field_locations"]["spr"] = [
        "strategy.py:get_baseline_action"
    ]

    capabilities["incremental_model_evidence"]["decision_field_locations"]["confidence"] = [
        "strategy.py:unrelated_helper"
    ]
    errors = validate_runtime_contract_implementation(plan, capabilities)
    assert any("confidence-scaled terminal/showdown" in error for error in errors)
    capabilities["incremental_model_evidence"]["decision_field_locations"]["confidence"] = [
        "strategy.py:get_baseline_action"
    ]

    capabilities["decision_runtime_evidence"]["budget_scaling"]["long"][
        "trusted_steps"
    ] = 9
    errors = validate_runtime_contract_implementation(plan, capabilities)
    assert any("trusted_steps=9" in error and "max_samples=8" in error for error in errors)


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


def test_checkpoint_ledger_reset_requires_authorized_transition_and_digest(
    tmp_path,
    monkeypatch,
):
    import json
    import evolution_infra

    checkpoint_path = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", checkpoint_path)
    plan = attach_runtime_contract_ledger(
        {
            "tasks": [{
                "worker_id": "runtime",
                "skill_layer": "opponent_model",
                "architecture_focus_id": "incremental_match_model",
                "runtime_contract": _match_memory_contract(),
            }],
        },
        replace=True,
    )
    ledger_digest = plan["runtime_contract_ledger"]["ledger_digest"]

    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "master_planned",
        master_plan=plan,
    ) is True
    legacy_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    legacy_checkpoint.pop("runtime_contract_ledger")
    checkpoint_path.write_text(json.dumps(legacy_checkpoint), encoding="utf-8")
    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "direction_audited",
        master_plan={},
    ) is False
    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "direction_audited",
        master_plan={},
        reset_runtime_contract_ledger=True,
        expected_runtime_contract_ledger_digest=ledger_digest,
    ) is False
    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "direction_audited",
        master_plan={},
        reset_runtime_contract_ledger=True,
        expected_runtime_contract_ledger_digest="wrong-digest",
        runtime_contract_ledger_reset_reason="master_plan_rejected_replan",
    ) is False
    assert evolution_infra.write_pipeline_checkpoint(
        2,
        1,
        "direction_audited",
        master_plan={},
        reset_runtime_contract_ledger=True,
        expected_runtime_contract_ledger_digest=ledger_digest,
        runtime_contract_ledger_reset_reason="master_plan_rejected_replan",
    ) is True

    checkpoint = evolution_infra.read_pipeline_checkpoint()
    assert checkpoint["stage"] == "direction_audited"
    assert checkpoint["master_plan"] == {}
    assert checkpoint["runtime_contract_ledger"] is None


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


def test_single_parent_policy_identity_recovery_resets_candidate_and_replans(tmp_path, monkeypatch):
    source = tmp_path / "national_v1"
    candidate = tmp_path / "national_v2"
    source.mkdir()
    candidate.mkdir()
    (source / "strategy.py").write_text("SOURCE = True\n", encoding="utf-8")
    (candidate / "strategy.py").write_text("STALE = True\n", encoding="utf-8")
    (candidate / "new_stale.py").write_text("STALE = True\n", encoding="utf-8")
    master_plan = attach_runtime_contract_ledger(
        {
            "tasks": [{
                "worker_id": "runtime",
                "skill_layer": "opponent_model",
                "architecture_focus_id": "incremental_match_model",
                "runtime_contract": _match_memory_contract(),
            }],
        },
        replace=True,
    )
    ckpt = {
        "next_v": 2,
        "source_v": 1,
        "parent2_v": None,
        "stage": "quality_failed",
        "master_plan": master_plan,
        "runtime_contract_ledger": master_plan["runtime_contract_ledger"],
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
    assert writes[0][1]["reset_runtime_contract_ledger"] is True
    assert writes[0][1]["expected_runtime_contract_ledger_digest"] == (
        master_plan["runtime_contract_ledger"]["ledger_digest"]
    )
    assert writes[0][1]["runtime_contract_ledger_reset_reason"] == (
        "architecture_policy_identity_replan"
    )
    payload = result["content"][0]["text"]
    assert "ARCHITECTURE_POLICY_IDENTITY_REPLAN" in payload
    assert validate_stage_transition("quality_failed", "direction_audited")[0] is True


def test_policy_identity_recovery_never_reports_replan_when_write_fails(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "national_v1"
    candidate = tmp_path / "national_v2"
    source.mkdir()
    candidate.mkdir()
    (source / "strategy.py").write_text("SOURCE = True\n", encoding="utf-8")
    (candidate / "strategy.py").write_text("STALE = True\n", encoding="utf-8")
    ckpt = {
        "next_v": 2,
        "source_v": 1,
        "run_id": "2#0",
        "workflow_run_id": "test-architecture-recovery-2-1",
        "checkpoint_revision": 1,
        "stage": "quality_failed",
        "gate_results": {
            "quality": {
                "national_architecture_transition": {
                    "policy_identity_errors": ["architecture_policy_contract_digest_mismatch"],
                },
            },
        },
    }
    monkeypatch.setattr(tool_planning, "write_pipeline_checkpoint", lambda *_a, **_k: False)

    with pytest.raises(RuntimeError, match="checkpoint rejected"):
        tool_planning._recover_architecture_policy_identity(ckpt, candidate, source)

    assert (candidate / "strategy.py").read_text(encoding="utf-8") == "SOURCE = True\n"


def test_crossover_policy_identity_recovery_never_forges_single_parent_lineage(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "national_v1"
    candidate = tmp_path / "national_v2"
    source.mkdir()
    candidate.mkdir()
    (source / "strategy.py").write_text("SOURCE = True\n", encoding="utf-8")
    (candidate / "strategy.py").write_text("CROSSOVER = True\n", encoding="utf-8")
    ckpt = {
        "next_v": 2,
        "source_v": 1,
        "parent2_v": 7,
        "stage": "quality_failed",
        "gate_results": {
            "quality": {
                "national_architecture_transition": {
                    "policy_identity_errors": [
                        "architecture_policy_contract_digest_mismatch"
                    ],
                },
            },
        },
    }
    writes = []
    monkeypatch.setattr(
        tool_planning,
        "write_pipeline_checkpoint",
        lambda *_args, **_kwargs: writes.append(True) or True,
    )
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_a, **_k: None)

    result = tool_planning._recover_architecture_policy_identity(
        ckpt,
        candidate,
        source,
    )
    payload = json.loads(result["content"][0]["text"])

    assert payload["error"] == "CROSSOVER_ARCHITECTURE_POLICY_IDENTITY_STALE"
    assert payload["candidate_reset_to_source"] is False
    assert payload["next_tool"] == "abandon_generation"
    assert (candidate / "strategy.py").read_text(encoding="utf-8") == "CROSSOVER = True\n"
    assert writes == []


def test_execute_workers_reports_architecture_recovery_failure_not_replan(
    tmp_path,
    monkeypatch,
):
    import asyncio
    import json

    source = tmp_path / "national_v1"
    candidate = tmp_path / "national_v2"
    source.mkdir()
    candidate.mkdir()
    (source / "strategy.py").write_text("SOURCE = True\n", encoding="utf-8")
    (candidate / "strategy.py").write_text("STALE = True\n", encoding="utf-8")
    ckpt = {
        "next_v": 2,
        "source_v": 1,
        "run_id": "2#0",
        "workflow_run_id": "test-execute-architecture-recovery-2-1",
        "checkpoint_revision": 1,
        "stage": "quality_failed",
        "gate_results": {
            "quality": {
                "national_architecture_transition": {
                    "policy_identity_errors": ["architecture_policy_contract_digest_mismatch"],
                },
            },
        },
    }

    async def no_exhausted_failure(*_args, **_kwargs):
        return None

    monkeypatch.setattr(tool_planning, "get_bot_dir", lambda version: source if version == 1 else candidate)
    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *_args: ckpt)
    monkeypatch.setattr(
        tool_planning,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted_failure,
    )
    monkeypatch.setattr(tool_planning, "write_pipeline_checkpoint", lambda *_a, **_k: False)
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_a, **_k: None)

    result = asyncio.run(tool_planning.execute_workers.handler({
        "next_v": 2,
        "source_v": 1,
    }))
    payload = json.loads(result["content"][0]["text"])

    assert payload["error"] == "ARCHITECTURE_POLICY_IDENTITY_RECOVERY_FAILED"
    assert "next_tool" not in payload
    assert "run_master" not in payload["directive"]
