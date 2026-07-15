import copy
from pathlib import Path

import pytest

import national_runtime_probe
import national_runtime_probe_worker
import runtime_architecture_policy
from output_schema import RuntimeContract
from strategy_reference_pack import (
    reference_pack_ids,
    validate_reference_selection,
)
from national_native import (
    NATIVE_BOT_TEMPLATE,
    NATIVE_PRECOMPUTE_TEMPLATE,
    NATIONAL_DECISION_RUNTIME_VERSION,
)
from national_runtime_probe_scenarios import DECISION_SCENARIOS


BOOTSTRAP_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "core"
    / "bootstrap_assets"
    / "strict_v1"
    / "policy.py"
)


TYPED_POLICY = '''\
def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    line = context["line"]
    opponent = context["opponent"]
    hand = context["hand"]
    kinds = set(legal["policy_kinds"])
    if (line["can_donk"] or line["can_delayed_probe"]) and "raise" in kinds:
        return {"kind": "raise", "raise_to": legal["min_raise_to"]}
    if opponent.get("confidence", 0.0) > 2.0 and "allin" in kinds:
        return {"kind": "allin"}
    if betting["to_call"] > 0 and "pass" in kinds:
        return {"kind": "pass"}
    if hand["position"] in {"small_blind", "big_blind"} and "pass" in kinds:
        return {"kind": "pass"}
    return {"kind": "fold"}


def iter_decisions(context, baseline, deadline):
    if context["deadline"] and deadline < 0:
        yield {"decision": baseline, "sample_count": 1}
    return
'''

REFINING_POLICY = TYPED_POLICY.replace(
    '''def iter_decisions(context, baseline, deadline):
    if context["deadline"] and deadline < 0:
        yield {"decision": baseline, "sample_count": 1}
    return
''',
    '''def iter_decisions(context, baseline, deadline):
    kinds = set(context["legal"]["policy_kinds"])
    for step in range(8):
        accumulator = 0
        for value in range(100000):
            accumulator = (accumulator + value + step) % 1000003
        if "fold" in kinds and baseline.get("kind") != "fold":
            decision = {"kind": "fold"}
        elif "allin" in kinds and baseline.get("kind") != "allin":
            decision = {"kind": "allin"}
        elif "raise" in kinds:
            decision = {
                "kind": "raise",
                "raise_to": context["legal"]["min_raise_to"],
            }
        else:
            decision = baseline
        if step % 2:
            decision = baseline
        yield {
            "decision": decision,
            "sample_count": accumulator,
            "complete": step == 7,
        }
''',
)

PROFILE_POLICY = TYPED_POLICY.replace(
    '''    if (line["can_donk"] or line["can_delayed_probe"]) and "raise" in kinds:
        return {"kind": "raise", "raise_to": legal["min_raise_to"]}
''',
    '''    if (line["can_donk"] or line["can_delayed_probe"]) and "raise" in kinds:
        aggression = opponent.get("rates", {}).get("aggression", 0.5)
        weight = opponent.get("adaptation_weight", 0.0)
        target = legal["min_raise_to"] + int(100 * aggression * weight)
        return {
            "kind": "raise",
            "raise_to": min(legal["max_raise_to"], target),
        }
''',
)

UNGATED_PROFILE_POLICY = PROFILE_POLICY.replace(
    'target = legal["min_raise_to"] + int(100 * aggression * weight)',
    'target = legal["min_raise_to"] + int(100 * aggression)',
)


def _write_typed_bot(root: Path, policy: str = TYPED_POLICY) -> Path:
    root.mkdir(parents=True)
    (root / "national_bot.py").write_text(NATIVE_BOT_TEMPLATE, encoding="utf-8")
    (root / "precompute.py").write_text(
        NATIVE_PRECOMPUTE_TEMPLATE,
        encoding="utf-8",
    )
    (root / "policy.py").write_text(policy, encoding="utf-8")
    return root


def _worker_spec() -> dict:
    return {
        "schema_version": national_runtime_probe.RUNTIME_PROBE_SCHEMA_VERSION,
        "expected_decision_runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
        "spec_digest": "unit-spec",
        "code_fingerprint": "unit-fingerprint",
    }


def test_scenario_bank_is_raw_delimiter_free_official_wire():
    for scenario in DECISION_SCENARIOS:
        assert "messages" in scenario
        assert "history" not in scenario
        assert "my_cards" not in scenario
        for message in scenario["messages"]:
            assert "\n" not in message
            assert "\r" not in message
            assert not message.startswith("{")
        for intent in scenario["setup_intents"]:
            assert set(intent).issubset({"kind", "raise_to"})
            assert intent["kind"] in {"pass", "fold", "allin", "raise"}


def test_worker_exercises_typed_context_lines_and_persistent_memory(tmp_path):
    bot = _write_typed_bot(tmp_path / "bot")
    result = national_runtime_probe_worker.run(bot, _worker_spec())

    assert result["ok"] is True, result["issues"]
    rows = result["official_transcript_decisions"]
    assert {row["id"] for row in rows} == {
        scenario["id"] for scenario in DECISION_SCENARIOS
    }
    assert all(row["context"]["schema_version"] == 1 for row in rows)
    assert all(
        row["context"]["cards"]["encoding"] == "national_tcp_suit_rank_v1"
        for row in rows
    )
    assert all(row["decision"]["kind"] in {"pass", "fold", "allin", "raise"} for row in rows)
    assert all("\n" not in row["wire"] and "\r" not in row["wire"] for row in rows)
    assert result["line_reachability"]["dimensions"]["donk"]["ok"] is True
    assert result["line_reachability"]["dimensions"]["delayed_probe"]["ok"] is True
    assert result["line_reachability"]["dimensions"]["donk"][
        "socket_validated"
    ] is True
    delayed_row = next(
        row
        for row in rows
        if row["id"] == "turn_delayed_probe_vs_opponent_pfr"
    )
    assert any(
        action.get("inferred") is True
        and action.get("inference_boundary") == "street:turn"
        for action in delayed_row["context"]["history"]["actions"]
    )
    memory = result["persistent_memory"]
    assert memory["terminal_response"]["fold"] == 1
    assert memory["terminal_response"]["call"] == 1
    assert memory["showdown_range"]["samples"] == 1
    assert memory["showdown_range"]["selection_scope"] == "reached_showdown_only"


def test_worker_rejects_non_typed_policy_output(tmp_path):
    policy = TYPED_POLICY.replace(
        'return {"kind": "pass"}',
        "return 0",
    )
    bot = _write_typed_bot(tmp_path / "bot", policy)
    result = national_runtime_probe_worker.run(bot, _worker_spec())

    assert result["ok"] is False
    assert any(
        "typed_intent_not_closed_mapping" in issue
        for issue in result["issues"]
    )


def test_worker_rejects_non_typed_refinement_output(tmp_path):
    policy = TYPED_POLICY.replace(
        '''    if context["deadline"] and deadline < 0:
        yield {"decision": baseline, "sample_count": 1}
    return
''',
        '''    yield 0
    return
''',
    )
    bot = _write_typed_bot(tmp_path / "bot", policy)
    result = national_runtime_probe_worker.run(bot, _worker_spec())

    assert result["failure_class"] == "candidate_contract"
    assert any(
        "candidate_policy_refinement:typed_intent_not_closed_mapping" in issue
        for issue in result["candidate_issues"]
    )


def test_worker_measures_trusted_short_long_refinement(tmp_path):
    bot = _write_typed_bot(tmp_path / "bot", REFINING_POLICY)
    result = national_runtime_probe_worker.run(bot, _worker_spec())

    assert result["ok"] is True, result["issues"]
    scaling = result["budget_scaled_refinement"]
    assert scaling["ok"] is True, scaling
    assert scaling["worker_seed_equal"] is True
    assert scaling["long"]["trusted_steps"] == 8
    assert scaling["long"]["trusted_cpu_ms"] >= 5.0
    assert scaling["changes_sanitized_decision"] is True
    states = runtime_architecture_policy._dynamic_probe_states(result)
    assert states["incremental_refinement_protocol"] is True
    assert states["budget_scaled_refinement"] is True


def test_profile_counterfactual_reaches_socket_validated_wire(tmp_path):
    bot = _write_typed_bot(tmp_path / "bot", PROFILE_POLICY)
    result = national_runtime_probe_worker.run(bot, _worker_spec())

    assert result["ok"] is True, result["issues"]
    action_profile = result["policy_counterfactuals"]["dimensions"][
        "action_profile"
    ]
    assert action_profile["socket_validated"] is True
    assert action_profile["changed"] is True
    assert action_profile["negative_control_stable"] is True
    assert action_profile["causal_passed"] is True
    assert action_profile["left_wire"] != action_profile["right_wire"]
    states = runtime_architecture_policy._dynamic_probe_states(result)
    assert states["incremental_opponent_model"] is True


def test_profile_counterfactual_rejects_raw_signal_without_confidence_gate(
    tmp_path,
):
    bot = _write_typed_bot(tmp_path / "bot", UNGATED_PROFILE_POLICY)
    result = national_runtime_probe_worker.run(bot, _worker_spec())

    assert result["ok"] is True, result["issues"]
    action_profile = result["policy_counterfactuals"]["dimensions"][
        "action_profile"
    ]
    assert action_profile["positive_wire_effect"] is True
    assert action_profile["negative_control_stable"] is False
    assert action_profile["causal_passed"] is False
    states = runtime_architecture_policy._dynamic_probe_states(result)
    assert states["incremental_opponent_model"] is False


def test_checked_in_bootstrap_policy_uses_all_bounded_match_signals_on_wire(
    tmp_path,
):
    bot = _write_typed_bot(
        tmp_path / "bot",
        BOOTSTRAP_POLICY_PATH.read_text(encoding="utf-8"),
    )
    result = national_runtime_probe_worker.run(bot, _worker_spec())

    assert result["ok"] is True, result["issues"]
    dimensions = result["policy_counterfactuals"]["dimensions"]
    assert set(dimensions) == {
        "action_profile",
        "terminal_response",
        "showdown_range",
    }
    for dimension in dimensions.values():
        assert dimension["scenario"] == "flop_donk_vs_opponent_pfr"
        assert dimension["socket_validated"] is True
        assert dimension["changed"] is True
        assert dimension["negative_control_stable"] is True
        assert dimension["causal_passed"] is True
        assert dimension["left_wire"].startswith("raise ")
        assert dimension["right_wire"].startswith("raise ")
        assert dimension["left_wire"] != dimension["right_wire"]

    states = runtime_architecture_policy._dynamic_probe_states(result)
    assert states["incremental_opponent_model"] is True
    assert states["terminal_response_adaptation"] is True
    assert states["showdown_range_adaptation"] is True


def _fake_worker_result(spec: dict) -> dict:
    return {
        "schema_version": national_runtime_probe.RUNTIME_PROBE_SCHEMA_VERSION,
        "orchestrator_version": (
            national_runtime_probe.RUNTIME_PROBE_ORCHESTRATOR_VERSION
        ),
        "worker_version": national_runtime_probe.RUNTIME_PROBE_WORKER_VERSION,
        "scenario_version": national_runtime_probe.RUNTIME_PROBE_SCENARIO_VERSION,
        "scenario_digest": national_runtime_probe.RUNTIME_PROBE_SCENARIO_DIGEST,
        "limits_digest": national_runtime_probe.RUNTIME_PROBE_LIMITS_DIGEST,
        "worker_digest": national_runtime_probe.RUNTIME_PROBE_WORKER_DIGEST,
        "probe_identity_digest": (
            national_runtime_probe.RUNTIME_PROBE_IDENTITY_DIGEST
        ),
        "policy_abi": "decision_context_v1_typed_intent_v1",
        "spec_digest": spec["spec_digest"],
        "code_fingerprint": spec["code_fingerprint"],
        "ok": True,
        "failure_class": "none",
        "issues": [],
        "official_transcript_decisions": [],
        "line_reachability": {"ok": True, "issues": [], "dimensions": {}},
        "persistent_memory": {"ok": True, "issues": []},
        "policy_counterfactuals": {"ok": True, "issues": [], "dimensions": {}},
        "budget_scaled_refinement": {
            "probe_kind": "trusted_multifidelity_2s_vs_8s",
            "ok": False,
            "active": False,
            "system_issues": [],
            "capability_issues": ["not_selected"],
            "short": {},
            "long": {},
        },
    }


def test_orchestrator_requires_repeatability_and_caches_by_artifact(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")
    calls = []

    def run_once(_root, spec, _timeout):
        calls.append(spec["spec_digest"])
        return copy.deepcopy(_fake_worker_result(spec))

    national_runtime_probe.clear_runtime_probe_cache()
    monkeypatch.setattr(national_runtime_probe, "_run_once", run_once)
    first = national_runtime_probe.run_national_runtime_probe(bot)
    cached = national_runtime_probe.run_national_runtime_probe(bot)

    assert first["ok"] is True
    assert first["repeatability_ok"] is True
    assert first["policy_abi"] == "decision_context_v1_typed_intent_v1"
    assert len(calls) == 2
    assert cached["cache_hit"] is True
    assert len(calls) == 2

    (bot / "policy.py").write_text(TYPED_POLICY + "\n# changed\n", encoding="utf-8")
    changed = national_runtime_probe.run_national_runtime_probe(bot)
    assert changed["ok"] is True
    assert len(calls) == 4
    assert changed["code_fingerprint"] != first["code_fingerprint"]


def test_orchestrator_fails_closed_when_worker_identity_is_missing(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")

    def run_once(_root, spec, _timeout):
        result = _fake_worker_result(spec)
        result.pop("worker_digest")
        return result

    national_runtime_probe.clear_runtime_probe_cache()
    monkeypatch.setattr(national_runtime_probe, "_run_once", run_once)
    observed = national_runtime_probe.run_national_runtime_probe(bot)

    assert observed["ok"] is False
    assert "runtime_probe_worker_digest_mismatch" in observed["issues"]


def test_active_probe_sources_contain_no_retired_wrapper_calls():
    core = Path(national_runtime_probe.__file__).parent
    combined = "\n".join(
        (core / name).read_text(encoding="utf-8")
        for name in (
            "national_runtime_probe.py",
            "national_runtime_probe_worker.py",
            "national_runtime_probe_scenarios.py",
        )
    )
    forbidden = (
        "def _" + "request(",
        "._" + "request()",
        "_strategy_" + "action",
        "_action_" + "to_tcp",
        "hand_" + "runtime",
        "opponent_" + "runtime",
    )
    assert not [token for token in forbidden if token in combined]


def _gate_capabilities() -> dict:
    required = set(runtime_architecture_policy.RUNTIME_FLOOR_CHECKS)
    advisory = {
        "incremental_refinement_protocol",
        "budget_scaled_refinement",
        "incremental_opponent_model",
        "terminal_response_adaptation",
        "showdown_range_adaptation",
        "donk_line_reachability",
        "delayed_probe_line_reachability",
    }
    checks = [
        {
            "check_id": check_id,
            "name": check_id,
            "passed": True,
            "required": check_id in required,
            "guidance": check_id,
            "evidence": {},
        }
        for check_id in sorted(required | advisory)
    ]
    return {
        "schema_version": 2,
        "epoch": runtime_architecture_policy.ACTIVE_EPOCH,
        "conclusive": True,
        "ok": True,
        "outcome": "passed",
        "checks": checks,
        "checks_by_id": {item["check_id"]: item for item in checks},
        "required_checks": sorted(required),
        "required_failures": [],
        "advisory_warnings": [],
        "infrastructure_failures": [],
    }


def _passing_gate_probe() -> dict:
    return {
        "schema_version": national_runtime_probe.RUNTIME_PROBE_SCHEMA_VERSION,
        "orchestrator_version": (
            national_runtime_probe.RUNTIME_PROBE_ORCHESTRATOR_VERSION
        ),
        "scenario_digest": national_runtime_probe.RUNTIME_PROBE_SCENARIO_DIGEST,
        "limits_digest": national_runtime_probe.RUNTIME_PROBE_LIMITS_DIGEST,
        "probe_identity_digest": national_runtime_probe.RUNTIME_PROBE_IDENTITY_DIGEST,
        "managed_isolation_digest": "a" * 64,
        "ok": True,
        "failure_class": "none",
        "issues": [],
        "official_transcript_decisions": [],
        "policy_counterfactuals": {
            "dimensions": {
                "action_profile": {
                    "causal_passed": True,
                    "positive_wire_effect": True,
                    "negative_control_stable": True,
                    "socket_validated": True,
                    "left_wire": "raise 300",
                    "right_wire": "raise 400",
                    "negative_left_wire": "raise 350",
                    "negative_right_wire": "raise 350",
                },
                "terminal_response": {
                    "causal_passed": True,
                    "positive_wire_effect": True,
                    "negative_control_stable": True,
                    "socket_validated": True,
                    "left_wire": "raise 310",
                    "right_wire": "raise 410",
                    "negative_left_wire": "raise 350",
                    "negative_right_wire": "raise 350",
                },
                "showdown_range": {
                    "causal_passed": True,
                    "positive_wire_effect": True,
                    "negative_control_stable": True,
                    "socket_validated": True,
                    "left_wire": "raise 320",
                    "right_wire": "raise 420",
                    "negative_left_wire": "raise 350",
                    "negative_right_wire": "raise 350",
                },
            }
        },
        "line_reachability": {
            "dimensions": {
                "donk": {"ok": True, "policy_changed": False},
                "delayed_probe": {"ok": True, "policy_changed": False},
            }
        },
        "budget_scaled_refinement": {"ok": False, "long": {}},
    }


def test_gate_classifies_candidate_and_system_probe_failures(monkeypatch, tmp_path):
    probe = _passing_gate_probe()
    monkeypatch.setattr(
        national_runtime_probe,
        "run_national_runtime_probe",
        lambda *_args, **_kwargs: copy.deepcopy(probe),
    )
    merged, _observed, infrastructure = (
        runtime_architecture_policy._apply_typed_runtime_probe(
            _gate_capabilities(),
            tmp_path,
            runtime_contract_ledger=None,
        )
    )
    assert merged["ok"] is True
    assert infrastructure == []
    assert merged["checks_by_id"]["typed_runtime_probe"]["passed"] is True
    assert (
        merged["checks_by_id"]["terminal_response_adaptation"]["required"]
        is False
    )

    probe.update({
        "ok": False,
        "failure_class": "candidate_contract",
        "issues": ["candidate_policy_baseline:typed_intent_kind_invalid"],
    })
    merged, _observed, infrastructure = (
        runtime_architecture_policy._apply_typed_runtime_probe(
            _gate_capabilities(),
            tmp_path,
            runtime_contract_ledger=None,
        )
    )
    assert merged["conclusive"] is True
    assert merged["ok"] is False
    assert infrastructure == []
    assert merged["checks_by_id"]["typed_runtime_probe"]["passed"] is False

    probe.update({
        "failure_class": "probe_infra",
        "issues": ["official_transcript_runtime_fault"],
    })
    merged, _observed, infrastructure = (
        runtime_architecture_policy._apply_typed_runtime_probe(
            _gate_capabilities(),
            tmp_path,
            runtime_contract_ledger=None,
        )
    )
    assert merged["conclusive"] is False
    assert merged["outcome"] == "infrastructure_failure"
    assert infrastructure[0]["side"] == "system"


def test_dynamic_state_recomputes_wire_causality_instead_of_trusting_flag():
    probe = _passing_gate_probe()
    action = probe["policy_counterfactuals"]["dimensions"]["action_profile"]
    action["right_wire"] = action["left_wire"]

    states = runtime_architecture_policy._dynamic_probe_states(probe)

    assert action["causal_passed"] is True
    assert states["incremental_opponent_model"] is False


def test_opponent_causal_floor_is_hard_and_selected_profile_adds_primary_gate(
    monkeypatch,
    tmp_path,
):
    capabilities = _gate_capabilities()
    monkeypatch.setattr(
        runtime_architecture_policy,
        "_lineage_capabilities",
        lambda _path: copy.deepcopy(capabilities),
    )
    monkeypatch.setattr(
        runtime_architecture_policy,
        "evaluate_national_capabilities",
        lambda _path: copy.deepcopy(capabilities),
    )
    monkeypatch.setattr(
        runtime_architecture_policy,
        "build_architecture_policy",
        lambda *_args, **_kwargs: {
            "effective_baseline_checks": {},
            "baseline_passed_checks": [],
            "selected_focus": {
                "required_checks": ["incremental_opponent_model"]
            },
        },
    )
    failing_probe = _passing_gate_probe()
    failing_probe["policy_counterfactuals"]["dimensions"][
        "action_profile"
    ]["causal_passed"] = False
    failing_probe["policy_counterfactuals"]["dimensions"][
        "terminal_response"
    ]["causal_passed"] = False
    monkeypatch.setattr(
        national_runtime_probe,
        "run_national_runtime_probe",
        lambda *_args, **_kwargs: copy.deepcopy(failing_probe),
    )

    baseline = runtime_architecture_policy.evaluate_architecture_transition(
        tmp_path,
        tmp_path,
    )
    assert baseline["ok"] is False
    assert baseline["unresolved_focus_checks"] == [
        "incremental_opponent_model"
    ]
    assert baseline["blocking_focus_checks"] == []
    assert [
        item["check_id"] for item in baseline["runtime_floor_failures"]
    ] == ["incremental_opponent_model"]

    ledger = runtime_architecture_policy.build_runtime_contract_ledger({
        "tasks": [{
            "worker_id": "terminal-primary",
            "runtime_contract": {
                "state_learning": {
                    "profile_dimensions": ["terminal_response"],
                    "oracle_refs": list(
                        runtime_architecture_policy.STATE_LEARNING_ORACLE_REFS
                    ),
                }
            },
        }]
    })
    selected = runtime_architecture_policy.evaluate_architecture_transition(
        tmp_path,
        tmp_path,
        runtime_contract_ledger=ledger,
    )
    assert selected["ok"] is False
    assert selected["selected_dynamic_checks"] == [
        "terminal_response_adaptation"
    ]
    assert selected["selected_dynamic_failures"] == [
        "terminal_response_adaptation"
    ]


def test_precompute_primary_is_deferred_and_never_selected():
    assert "lead_sizing_geometry_v1" not in reference_pack_ids()
    assert "reusable_precompute" not in {
        item["focus_id"]
        for item in runtime_architecture_policy.architecture_focus_specs()
    }
    errors = validate_reference_selection(
        "lead_sizing_geometry_v1",
        "bounded_precompute_lookup",
    )
    assert errors and "primary_unavailable" in errors[0]
    with pytest.raises(ValueError):
        RuntimeContract.model_validate({
            "state_learning": {
                "work_primitive": "bounded_precompute_lookup",
                "oracle_refs": list(
                    runtime_architecture_policy.STATE_LEARNING_ORACLE_REFS
                ),
            },
            "reference_pack_id": "lead_sizing_geometry_v1",
        })

    states = {
        "incremental_opponent_model": True,
        "incremental_refinement_protocol": False,
        "budget_scaled_refinement": False,
        "precompute_lookup_path": False,
        "precompute_runtime_influence": False,
    }
    selected = runtime_architecture_policy._select_architecture_focus_from_state(
        states
    )
    assert selected["focus_id"] == "deadline_refinement"
