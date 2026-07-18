import copy
import hashlib
import json
from pathlib import Path

import pytest

import national_runtime_probe
import national_runtime_probe_worker
import runtime_architecture_policy
import national_runtime_authority
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
def _fold_locks_match_win(context):
    hand = context.get("hand", {})
    betting = context.get("betting", {})
    line = context.get("line", {})
    opponent = context.get("opponent", {})
    control = hand.get("match_control", {})
    fields = {
        "schema_version", "initial_chips", "small_blind", "big_blind",
        "current_position", "current_exposure", "future_forced_blinds",
        "forced_fold_loss_bound", "hero_net_earned", "fold_locks_win",
    }
    if not isinstance(control, dict) or set(control) != fields:
        return False
    integers = (
        "schema_version", "initial_chips", "small_blind", "big_blind",
        "current_exposure", "future_forced_blinds",
        "forced_fold_loss_bound", "hero_net_earned",
    )
    if any(type(control.get(field)) is not int for field in integers):
        return False
    if type(control.get("fold_locks_win")) is not bool:
        return False
    if control["schema_version"] != 1 or control["initial_chips"] != 20000:
        return False
    if control["small_blind"] != 50 or control["big_blind"] != 100:
        return False
    position = hand.get("position")
    if position not in {"small_blind", "big_blind"}:
        return False
    if control["current_position"] != position or line.get("position") != position:
        return False
    remaining = hand.get("remaining_including_current")
    if type(remaining) is not int or not 1 <= remaining <= 70:
        return False
    pairs, odd = divmod(remaining - 1, 2)
    future = pairs * 150
    if odd:
        future += 100 if position == "small_blind" else 50
    stack = betting.get("hero_stack")
    if type(stack) is not int:
        return False
    exposure = 20000 - stack
    hero_net = opponent.get("match_result", {}).get("hero_net_earned")
    expected = exposure + future
    return bool(
        type(hero_net) is int
        and control["current_exposure"] == exposure
        and control["future_forced_blinds"] == future
        and control["forced_fold_loss_bound"] == expected
        and control["hero_net_earned"] == hero_net
        and control["fold_locks_win"] is (hero_net > expected)
        and control["fold_locks_win"]
    )


def get_baseline_decision(context):
    legal = context["legal"]
    betting = context["betting"]
    line = context["line"]
    opponent = context["opponent"]
    hand = context["hand"]
    kinds = set(legal["policy_kinds"])
    if "fold" in kinds and _fold_locks_match_win(context):
        return {"kind": "fold"}
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
        # The official TCP server obtains both team names before it begins the
        # first hand.  The runtime uses that real protocol event to launch its
        # isolated policy worker, so omitting it would turn a connection-start
        # cost into a fabricated first-decision deadline failure.
        assert scenario["messages"][0] == "name"
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
    assert all(
        row["name_handshake"] == {
            "wire": ("TypedProbeB",),
            "worker_started": True,
            "worker_generation": 1,
        }
        for row in rows
    )
    assert result["line_reachability"]["dimensions"]["donk"]["ok"] is True
    assert result["line_reachability"]["dimensions"]["delayed_probe"]["ok"] is True
    assert result["line_reachability"]["dimensions"]["donk"][
        "socket_validated"
    ] is True
    for line in ("donk", "delayed_probe"):
        evidence = result["line_reachability"]["dimensions"][line]
        assert all(
            type(evidence[field]) is bool
            for field in (
                "positive_refinement_active",
                "negative_refinement_active",
                "mixed_identity_refinement_active",
                "matched_control_refinement_active",
            )
        )
        assert evidence["producer_reachable"] is True
        assert evidence["context_ablation_exact"] is True
        assert evidence["positive_without_ablation_digest"] == (
            evidence["matched_without_ablation_digest"]
        )
        assert evidence["consumer_wire_effect"] is True
        # This generic fixture raises on every enabled line.  Reachability is
        # real, but it must not receive bounded-mixing capability credit.
        assert evidence["bounded_mixing"] is False
        assert evidence["causal_passed"] is False
        assert evidence["positive_wire"] != evidence["matched_control_wire"]
    generic_states = runtime_architecture_policy._dynamic_probe_states(result)
    assert generic_states["donk_line_reachability"] is False
    assert generic_states["delayed_probe_line_reachability"] is False
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
    assert memory["name_handshake"] == {
        "wire": ("TypedProbeB",),
        "worker_generation": 1,
    }
    assert memory["terminal_response"]["fold"] == 1
    assert memory["terminal_response"]["call"] == 1
    assert memory["showdown_range"]["samples"] == 1
    assert memory["showdown_range"]["selection_scope"] == "reached_showdown_only"
    match_control = result["match_control_consumer"]
    assert match_control["ok"] is True
    assert match_control["causal_passed"] is True
    assert match_control["rows"]["strict_win"]["wire"] == "fold"
    assert match_control["rows"]["equality_boundary"]["wire"] != "fold"
    assert match_control["rows"]["malformed_proof"]["wire"] != "fold"
    assert all(
        type(row["refinement_active"]) is bool
        for row in match_control["rows"].values()
    )


def test_worker_rejects_policy_that_drops_match_control_consumer(tmp_path):
    policy = TYPED_POLICY.replace(
        '    if "fold" in kinds and _fold_locks_match_win(context):\n'
        '        return {"kind": "fold"}\n',
        "",
        1,
    )
    bot = _write_typed_bot(tmp_path / "bot", policy)
    result = national_runtime_probe_worker.run(bot, _worker_spec())

    assert result["ok"] is False
    assert result["failure_class"] == "candidate_contract"
    assert any(
        "match_control_consumer_strict_win_wire_mismatch" in issue
        for issue in result["candidate_issues"]
    )


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


def test_worker_rejects_baseline_evaluator_work_above_system_cap(tmp_path):
    policy = (
        "from precompute import evaluate_seven as rank\n\n"
        + TYPED_POLICY.replace(
            "def get_baseline_decision(context):\n",
            "def get_baseline_decision(context):\n"
            "    for _index in range(801):\n"
            "        rank((0, 1, 2, 3, 4, 5, 6))\n",
            1,
        )
    )
    bot = _write_typed_bot(tmp_path / "bot", policy)
    result = national_runtime_probe_worker.run(bot, _worker_spec())

    assert result["failure_class"] == "candidate_contract"
    assert any(
        "baseline_evaluator_call_cap_exceeded:801>800" in issue
        for issue in result["candidate_issues"]
    )


def test_worker_counts_direct_alternate_system_evaluator_at_baseline(tmp_path):
    from national_capability_contract import BASELINE_EVALUATOR_CALL_CAP

    assert (
        national_runtime_probe_worker.BASELINE_EVALUATOR_CALL_CAP
        == BASELINE_EVALUATOR_CALL_CAP
        == 800
    )
    policy = (
        "import precompute\n\n"
        + TYPED_POLICY.replace(
            "def get_baseline_decision(context):\n",
            "def get_baseline_decision(context):\n"
            "    for _index in range(801):\n"
            "        precompute.best_hand_rank((0, 1, 2, 3, 4, 5, 6))\n",
            1,
        )
    )
    bot = _write_typed_bot(tmp_path / "bot", policy)
    result = national_runtime_probe_worker.run(bot, _worker_spec())

    assert result["failure_class"] == "candidate_contract"
    assert any(
        "baseline_evaluator_call_cap_exceeded:801>800" in issue
        for issue in result["candidate_issues"]
    )


def test_worker_rejects_deadline_profile_specific_late_baseline(tmp_path):
    policy = (
        "import time\n\n"
        + TYPED_POLICY.replace(
            "def get_baseline_decision(context):\n",
            "def get_baseline_decision(context):\n"
            "    if context['deadline']['hard_budget_ms'] >= 2000:\n"
            "        until = time.monotonic() + 0.225\n"
            "        while time.monotonic() < until:\n"
            "            pass\n",
            1,
        )
    )
    bot = _write_typed_bot(tmp_path / "bot", policy)
    result = national_runtime_probe_worker.run(bot, _worker_spec())

    assert result["failure_class"] == "candidate_contract"
    assert any(
        "budget_short:river_facing_large_bet:policy_baseline_deadline_missed"
        in issue
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
    assert all(
        type(action_profile[field]) is bool
        for field in (
            "left_refinement_active",
            "right_refinement_active",
            "negative_left_refinement_active",
            "negative_right_refinement_active",
        )
    )
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

    assert dimensions["showdown_range"]["negative_control_kind"] == (
        "selection_bias_guard_removed"
    )

    for line in ("donk", "delayed_probe"):
        evidence = result["line_reachability"]["dimensions"][line]
        assert evidence["mixing_class"] == "structural_air_no_hole_draw"
        assert evidence["mixing_context_exact"] is True
        assert evidence["stable_context_normalization_paths"] == [
            "deadline.hard_monotonic",
            "deadline.refinement_monotonic",
        ]
        assert evidence["mixing_comparison_ignored_paths"] == ["cards"]
        assert evidence["positive_without_cards_digest"] == (
            evidence["mixed_without_cards_digest"]
        )
        assert evidence["bounded_mixing"] is True
        assert evidence["positive_wire"] != evidence["mixed_identity_wire"]
        assert evidence["causal_passed"] is True

    match_control = result["match_control_consumer"]
    assert match_control["ok"] is True
    assert match_control["causal_passed"] is True
    assert match_control["strict_comparison"] == (
        "hero_net_earned > forced_fold_loss_bound"
    )

    states = runtime_architecture_policy._dynamic_probe_states(result)
    assert states["incremental_opponent_model"] is True
    assert states["terminal_response_adaptation"] is True
    assert states["showdown_range_adaptation"] is True
    entrypoints = result["policy_entrypoints"]
    assert entrypoints["ok"] is True
    assert all(
        row["baseline_work"]["evaluator_calls"]
        <= row["baseline_work"]["evaluator_call_cap"]
        for row in entrypoints["rows"]
        if row["baseline_work"]["instrumented"]
    )
    transcript_rows = result["official_transcript_decisions"]
    assert all(
        isinstance(row["runtime"]["socket_fallback_ready_ms"], float)
        and row["runtime"]["socket_fallback_ready_ms"] >= 0.0
        for row in transcript_rows
    )
    assert all(
        isinstance(row["runtime"]["baseline_published_ms"], float)
        and row["runtime"]["baseline_published_ms"]
        <= row["runtime"]["baseline_target_ms"] == 200.0
        for row in transcript_rows
    )


def _fake_worker_result(spec: dict) -> dict:
    line_row = {
        "positive_refinement_active": False,
        "positive_decision": {"kind": "raise", "raise_to": 300},
        "positive_wire": "raise 300",
        "negative_refinement_active": False,
        "negative_decision": {"kind": "pass"},
        "negative_wire": "call",
        "mixed_identity_refinement_active": False,
        "mixed_identity_decision": {"kind": "pass"},
        "mixed_identity_wire": "check",
        "matched_control_refinement_active": False,
        "matched_control_decision": {"kind": "pass"},
        "matched_control_wire": "check",
    }
    counterfactual_row = {
        "left_refinement_active": False,
        "left_decision": {"kind": "raise", "raise_to": 300},
        "left_wire": "raise 300",
        "right_refinement_active": False,
        "right_decision": {"kind": "raise", "raise_to": 400},
        "right_wire": "raise 400",
        "negative_left_refinement_active": False,
        "negative_left_decision": {"kind": "pass"},
        "negative_left_wire": "check",
        "negative_right_refinement_active": False,
        "negative_right_decision": {"kind": "pass"},
        "negative_right_wire": "check",
    }
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
        **national_runtime_probe.runtime_probe_native_template_evidence(),
        "policy_abi": "decision_context_v1_typed_intent_v1",
        "spec_digest": spec["spec_digest"],
        "code_fingerprint": spec["code_fingerprint"],
        "ok": True,
        "failure_class": "none",
        "issues": [],
        "process_returncode": 0,
        "managed_isolation": {
            "policy_sha256": "a" * 64,
            "bpf_sha256": "b" * 64,
            "bpf_size": 1,
            "namespaces": ["user", "net"],
        },
        "official_transcript_decisions": [
            {
                "id": scenario["id"],
                "ok": True,
                "issues": [],
                "decision": {"kind": "pass"},
                "wire": "call",
                "runtime": {
                    "refinement_messages": 0,
                    "trusted_refinement_steps": 0,
                },
            }
            for scenario in DECISION_SCENARIOS
        ],
        "line_reachability": {
            "ok": True,
            "issues": [],
            "system_issues": [],
            "candidate_issues": [],
            "dimensions": {
                "donk": copy.deepcopy(line_row),
                "delayed_probe": copy.deepcopy(line_row),
            },
        },
        "persistent_memory": {"ok": True, "issues": []},
        "policy_counterfactuals": {
            "ok": True,
            "issues": [],
            "system_issues": [],
            "candidate_issues": [],
            "dimensions": {
                "action_profile": copy.deepcopy(counterfactual_row),
                "terminal_response": copy.deepcopy(counterfactual_row),
                "showdown_range": copy.deepcopy(counterfactual_row),
            },
        },
        "match_control_consumer": {
            "ok": True,
            "system_issues": [],
            "candidate_issues": [],
            "rows": {
                "strict_win": {
                    "refinement_active": False,
                    "decision": {"kind": "fold"},
                    "wire": "fold",
                },
                "equality_boundary": {
                    "refinement_active": False,
                    "decision": {"kind": "pass"},
                    "wire": "call",
                },
                "malformed_proof": {
                    "refinement_active": False,
                    "decision": {"kind": "pass"},
                    "wire": "call",
                },
            },
        },
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


def _repeatability_evidence_template(view_digest: str) -> dict:
    return {
        "schema_version": (
            national_runtime_probe.RUNTIME_PROBE_REPEATABILITY_SCHEMA_VERSION
        ),
        "view_contract": (
            national_runtime_probe.RUNTIME_PROBE_REPEATABILITY_VIEW_CONTRACT
        ),
        "view_digest_algorithm": (
            national_runtime_probe.RUNTIME_PROBE_REPEATABILITY_DIGEST_ALGORITHM
        ),
        "repeat_count": 2,
        "view_digest_count": 2,
        "view_digests": [
            {"repeat": 1, "sha256": view_digest},
            {"repeat": 2, "sha256": view_digest},
        ],
        "view_digests_truncated": False,
        "differing_path_count": 0,
        "differing_paths": [],
        "differing_paths_truncated": False,
        "redaction": dict(
            national_runtime_probe.RUNTIME_PROBE_REPEATABILITY_REDACTION
        ),
    }


def _valid_managed_isolation() -> dict:
    """Minimal immutable managed-executor identity for host-only fixtures."""

    return {
        "policy_sha256": "a" * 64,
        "bpf_sha256": "b" * 64,
        "bpf_size": 1,
        "namespaces": ["user", "net"],
    }


def _seal_passing_repeatability_probe(probe: dict) -> dict:
    """Add a self-consistent successful receipt to a complete raw probe."""

    if not isinstance(probe.get("managed_isolation"), dict):
        probe["managed_isolation"] = _valid_managed_isolation()
    probe["managed_isolation_digest"] = national_runtime_probe._canonical_digest(
        probe["managed_isolation"]
    )
    probe["ok"] = True
    probe["repeatability_ok"] = True
    probe["evidence_integrity_ok"] = True
    probe["failure_class"] = "none"
    probe["issues"] = []
    # The view deliberately excludes its own receipt, so the placeholder
    # cannot participate in the digest calculation.
    probe["repeatability"] = _repeatability_evidence_template("0" * 64)
    digest = national_runtime_probe._canonical_digest(
        national_runtime_probe._repeatability_view(probe)
    )
    probe["repeatability"] = _repeatability_evidence_template(digest)
    return probe


def _semantic_repeat_result(spec: dict, *, refinement_active: bool) -> dict:
    result = _fake_worker_result(spec)
    river = {
        "id": "river_facing_large_bet",
        "ok": True,
        "issues": [],
        "context": {"private_test_only": "context-secret"},
        "context_digest": "c" * 64,
        "decision": {"kind": "pass"},
        "wire": "call",
        "setup_wire": ["TypedProbeB"],
        "runtime": {
            "runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
            "socket_fallback_decision": {"kind": "pass"},
            "baseline_published": True,
            "baseline_target_met": True,
            "policy_baseline_decision": {"kind": "pass"},
            "timed_out": False,
            "worker_terminated": False,
            "completed": True,
            "refinement_messages": 1 if refinement_active else 0,
            "trusted_refinement_steps": 1 if refinement_active else 0,
            "trusted_refinement_cpu_ms": 1.0,
            "trusted_refinement_elapsed_ms": 1.0,
            "refinement_iterator_exhausted": False,
        },
    }
    result["official_transcript_decisions"] = [
        river,
        *(
            row
            for row in result["official_transcript_decisions"]
            if row.get("id") != river["id"]
        ),
    ]
    result["policy_entrypoints"] = {
        "ok": True,
        "issues": [],
        "rows": [{
            "scenario": "river_facing_large_bet",
            "decision": {"kind": "pass"},
            "refinement_decisions": [{"kind": "fold"}],
            "baseline_work": {
                "instrumented": True,
                "evaluator_calls": 3,
                "evaluator_call_cap": 800,
                "evaluator_calls_by_name": {"evaluate_seven": 3},
            },
            "ok": True,
            "issue": None,
        }],
    }
    result["budget_scaled_refinement"] = {
        "probe_kind": "trusted_multifidelity_2s_vs_8s",
        "scenario": "river_facing_large_bet",
        "ok": True,
        "active": refinement_active,
        "system_issues": [],
        "candidate_issues": [],
        "capability_issues": [],
        "worker_seed_equal": True,
        "bounded_work": True,
        "scaled_or_exhausted": True,
        "changes_sanitized_decision": True,
        "short": {
            "iterator_exhausted": False,
            "action_changes": 1,
            "refinement_messages": 1 if refinement_active else 0,
            "trusted_refinement_steps": 1 if refinement_active else 0,
            "trusted_cpu_ms": 1.0,
            "trusted_elapsed_ms": 1.0,
            "baseline_published": True,
            "baseline_target_met": True,
            "worker_seed": 20260710,
            "decision": {"kind": "pass"},
            "wire": "call",
        },
        "long": {
            "iterator_exhausted": True,
            "action_changes": 2,
            "refinement_messages": 2 if refinement_active else 0,
            "trusted_refinement_steps": 2 if refinement_active else 0,
            "trusted_cpu_ms": 2.0,
            "trusted_elapsed_ms": 2.0,
            "baseline_published": True,
            "baseline_target_met": True,
            "worker_seed": 20260710,
            "decision": {"kind": "fold"},
            "wire": "fold",
        },
    }
    return result


def test_repeatability_omits_active_refinement_trace_and_action_variance(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")
    runs = [
        _semantic_repeat_result(
            national_runtime_probe.build_runtime_probe_spec(bot),
            refinement_active=True,
        ),
        _semantic_repeat_result(
            national_runtime_probe.build_runtime_probe_spec(bot),
            refinement_active=True,
        ),
    ]
    second = runs[1]
    second["worker_stdout"] = "stdout-secret"
    second["worker_stderr"] = "stderr-secret"
    row = second["official_transcript_decisions"][0]
    row["context"] = {"private_test_only": "different-context-secret"}
    row["decision"] = {"kind": "fold"}
    row["wire"] = "fold"
    row["runtime"].update({
        "refinement_messages": 37,
        "trusted_refinement_steps": 37,
        "trusted_refinement_cpu_ms": 987.654,
        "trusted_refinement_elapsed_ms": 765.432,
        "refinement_iterator_exhausted": True,
    })
    second["policy_entrypoints"]["rows"][0]["refinement_decisions"] = [
        {"kind": "allin"},
        {"kind": "fold"},
    ]
    second["budget_scaled_refinement"]["short"].update({
        "iterator_exhausted": True,
        "action_changes": 11,
        "refinement_messages": 19,
        "trusted_refinement_steps": 19,
        "trusted_cpu_ms": 200.0,
        "trusted_elapsed_ms": 300.0,
        "decision": {"kind": "fold"},
        "wire": "fold",
    })
    second["budget_scaled_refinement"]["long"].update({
        "iterator_exhausted": False,
        "action_changes": 99,
        "refinement_messages": 99,
        "trusted_refinement_steps": 99,
        "trusted_cpu_ms": 900.0,
        "trusted_elapsed_ms": 999.0,
        "decision": {"kind": "allin"},
        "wire": "allin",
    })

    def run_once(_root, _spec, _timeout):
        return copy.deepcopy(runs.pop(0))

    national_runtime_probe.clear_runtime_probe_cache()
    monkeypatch.setattr(national_runtime_probe, "_run_once", run_once)
    observed = national_runtime_probe.run_national_runtime_probe(bot)

    assert observed["ok"] is True
    assert observed["repeatability_ok"] is True
    evidence = observed["repeatability"]
    assert evidence["differing_path_count"] == 0
    assert evidence["view_digest_count"] == 2
    serialized = json.dumps(evidence, sort_keys=True)
    for secret in (
        "stdout-secret",
        "stderr-secret",
        "context-secret",
        "different-context-secret",
    ):
        assert secret not in serialized


def test_repeatability_rejects_baseline_final_action_divergence_with_pointer(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)
    runs = [
        _semantic_repeat_result(spec, refinement_active=False),
        _semantic_repeat_result(spec, refinement_active=False),
    ]
    runs[1]["official_transcript_decisions"][0].update({
        "decision": {"kind": "fold"},
        "wire": "fold",
    })

    national_runtime_probe.clear_runtime_probe_cache()
    monkeypatch.setattr(
        national_runtime_probe,
        "_run_once",
        lambda *_args: copy.deepcopy(runs.pop(0)),
    )
    observed = national_runtime_probe.run_national_runtime_probe(bot)

    assert observed["ok"] is False
    assert "runtime_probe_non_repeatable" in observed["issues"]
    pointers = {
        (item["repeat"], item["json_pointer"])
        for item in observed["repeatability"]["differing_paths"]
    }
    assert (2, "/official_transcript_decisions/0/wire") in pointers
    assert (2, "/official_transcript_decisions/0/decision/kind") in pointers


def test_repeatability_compares_inactive_capability_wire_paths(tmp_path):
    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)
    first = _semantic_repeat_result(spec, refinement_active=False)
    second = _semantic_repeat_result(spec, refinement_active=False)
    for result in (first, second):
        result["policy_counterfactuals"] = {
            "ok": True,
            "issues": [],
            "system_issues": [],
            "candidate_issues": [],
            "dimensions": {
                "action_profile": {
                    "scenario": "flop_donk_vs_opponent_pfr",
                    "left_profile": "aggressive",
                    "right_profile": "passive",
                    "left_decision": {"kind": "raise", "raise_to": 300},
                    "right_decision": {"kind": "raise", "raise_to": 400},
                    "left_wire": "raise 300",
                    "right_wire": "raise 400",
                    "negative_left_decision": {"kind": "raise", "raise_to": 350},
                    "negative_right_decision": {"kind": "raise", "raise_to": 350},
                    "negative_left_wire": "raise 350",
                    "negative_right_wire": "raise 350",
                    "changed": True,
                    "positive_wire_effect": True,
                    "negative_control_stable": True,
                    "negative_control_kind": "authority_weight_removed",
                    "causal_passed": True,
                    "socket_validated": True,
                }
            },
        }
    second["policy_counterfactuals"]["dimensions"]["action_profile"][
        "left_wire"
    ] = "raise 301"

    evidence = national_runtime_probe._repeatability_evidence([
        national_runtime_probe._repeatability_view(first),
        national_runtime_probe._repeatability_view(second),
    ])

    assert evidence["differing_path_count"] >= 1
    assert {
        (item["repeat"], item["json_pointer"])
        for item in evidence["differing_paths"]
    } >= {
        (2, "/policy_counterfactuals/dimensions/action_profile/left_wire")
    }


def test_repeatability_aggregates_second_repeat_failure_and_identity_isolation(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)
    first = _semantic_repeat_result(spec, refinement_active=False)
    second = _semantic_repeat_result(spec, refinement_active=False)
    second.update({
        "ok": False,
        "failure_class": "candidate_contract",
        "issues": ["candidate_policy_baseline:typed_intent_kind_invalid"],
        "worker_digest": "f" * 64,
    })
    second["managed_isolation"] = {}
    runs = [first, second]

    national_runtime_probe.clear_runtime_probe_cache()
    monkeypatch.setattr(
        national_runtime_probe,
        "_run_once",
        lambda *_args: copy.deepcopy(runs.pop(0)),
    )
    observed = national_runtime_probe.run_national_runtime_probe(bot)

    assert observed["ok"] is False
    assert "candidate_policy_baseline:typed_intent_kind_invalid:repeat=2" in (
        observed["issues"]
    )
    assert "runtime_probe_repeat_not_ok:repeat=2" in observed["issues"]
    assert (
        "runtime_probe_repeat_failure_class:candidate_contract:repeat=2"
        in observed["issues"]
    )
    assert "runtime_probe_worker_digest_mismatch:repeat=2" in observed["issues"]
    assert (
        "runtime_probe_repeat_managed_isolation_missing:repeat=2"
        in observed["issues"]
    )
    pointers = {
        (item["repeat"], item["json_pointer"])
        for item in observed["repeatability"]["differing_paths"]
    }
    assert (2, "/identity/worker_digest") in pointers
    assert (2, "/managed_isolation/policy_sha256") in pointers


def test_repeatability_evidence_is_bounded_when_many_views_differ(tmp_path):
    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)
    reference = national_runtime_probe._repeatability_view(
        _semantic_repeat_result(spec, refinement_active=False)
    )
    views = [reference]
    for index in range(
        national_runtime_probe.RUNTIME_PROBE_MAX_REPEAT_VIEW_DIGESTS + 3
    ):
        changed = copy.deepcopy(reference)
        changed["identity"]["code_fingerprint"] = f"{index:064x}"
        views.append(changed)

    evidence = national_runtime_probe._repeatability_evidence(views)

    assert len(evidence["view_digests"]) == (
        national_runtime_probe.RUNTIME_PROBE_MAX_REPEAT_VIEW_DIGESTS
    )
    assert evidence["view_digests_truncated"] is True
    assert len(evidence["differing_paths"]) <= (
        national_runtime_probe.RUNTIME_PROBE_MAX_REPEAT_DIFF_PATHS
    )
    assert evidence["differing_path_count"] >= len(evidence["differing_paths"])


def test_repeatability_does_not_use_budget_activity_for_nonbudget_actions(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)

    cases = (
        (
            "line",
            "/line_reachability/dimensions/donk/positive_wire",
            lambda result: result["line_reachability"]["dimensions"]["donk"].update(
                {"positive_wire": "raise 301"}
            ),
        ),
        (
            "counterfactual",
            "/policy_counterfactuals/dimensions/action_profile/left_wire",
            lambda result: result["policy_counterfactuals"]["dimensions"][
                "action_profile"
            ].update({"left_wire": "raise 301"}),
        ),
        (
            "match_control",
            "/match_control_consumer/rows/strict_win/wire",
            lambda result: result["match_control_consumer"]["rows"][
                "strict_win"
            ].update({"wire": "call"}),
        ),
    )
    for _label, pointer, mutate in cases:
        first = _semantic_repeat_result(spec, refinement_active=True)
        second = _semantic_repeat_result(spec, refinement_active=True)
        mutate(second)
        runs = [first, second]
        national_runtime_probe.clear_runtime_probe_cache()
        monkeypatch.setattr(
            national_runtime_probe,
            "_run_once",
            lambda *_args: copy.deepcopy(runs.pop(0)),
        )

        observed = national_runtime_probe.run_national_runtime_probe(bot)

        assert observed["budget_scaled_refinement"]["active"] is True
        assert observed["repeatability_ok"] is False
        assert (2, pointer) in {
            (item["repeat"], item["json_pointer"])
            for item in observed["repeatability"]["differing_paths"]
        }


def test_repeatability_allows_only_active_nonbudget_row_action_variation(
    tmp_path,
    monkeypatch,
):
    """A row may vary only when that row reports actual refinement work."""

    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)
    first = _semantic_repeat_result(spec, refinement_active=False)
    second = _semantic_repeat_result(spec, refinement_active=False)
    for result, wire in ((first, "raise 300"), (second, "raise 301")):
        result["line_reachability"]["dimensions"]["donk"].update({
            "positive_decision": {"kind": "raise", "raise_to": 300},
            "positive_wire": wire,
            "positive_refinement_active": True,
        })
    runs = [first, second]
    national_runtime_probe.clear_runtime_probe_cache()
    monkeypatch.setattr(
        national_runtime_probe,
        "_run_once",
        lambda *_args: copy.deepcopy(runs.pop(0)),
    )

    observed = national_runtime_probe.run_national_runtime_probe(bot)

    assert observed["ok"] is True, observed["issues"]
    assert observed["repeatability_ok"] is True
    assert observed["repeatability"]["differing_path_count"] == 0


def test_repeatability_rejects_missing_per_scenario_refinement_activity(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)
    first = _semantic_repeat_result(spec, refinement_active=False)
    second = _semantic_repeat_result(spec, refinement_active=False)
    for result in (first, second):
        result["match_control_consumer"] = {
            "ok": True,
            "system_issues": [],
            "candidate_issues": [],
            "rows": {
                "strict_win": {
                    "decision": {"kind": "fold"},
                    "wire": "fold",
                }
            },
        }
    runs = [first, second]
    national_runtime_probe.clear_runtime_probe_cache()
    monkeypatch.setattr(
        national_runtime_probe,
        "_run_once",
        lambda *_args: copy.deepcopy(runs.pop(0)),
    )

    observed = national_runtime_probe.run_national_runtime_probe(bot)

    assert observed["ok"] is False
    assert (
        "runtime_probe_per_scenario_refinement_activity_invalid:"
        "match_control:strict_win:refinement_active:repeat=1"
    ) in observed["issues"]


def test_repeatability_rejects_missing_required_activity_section(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)
    first = _semantic_repeat_result(spec, refinement_active=False)
    second = _semantic_repeat_result(spec, refinement_active=False)
    for result in (first, second):
        result.pop("policy_counterfactuals")
    runs = [first, second]
    national_runtime_probe.clear_runtime_probe_cache()
    monkeypatch.setattr(
        national_runtime_probe,
        "_run_once",
        lambda *_args: copy.deepcopy(runs.pop(0)),
    )

    observed = national_runtime_probe.run_national_runtime_probe(bot)

    assert observed["ok"] is False
    assert (
        "runtime_probe_per_scenario_refinement_section_invalid:"
        "counterfactual:repeat=1"
    ) in observed["issues"]


def test_repeatability_rejects_missing_official_transcript_scenarios(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)
    first = _semantic_repeat_result(spec, refinement_active=False)
    second = _semantic_repeat_result(spec, refinement_active=False)
    for result in (first, second):
        result["official_transcript_decisions"] = []
    runs = [first, second]
    national_runtime_probe.clear_runtime_probe_cache()
    monkeypatch.setattr(
        national_runtime_probe,
        "_run_once",
        lambda *_args: copy.deepcopy(runs.pop(0)),
    )

    observed = national_runtime_probe.run_national_runtime_probe(bot)

    assert observed["ok"] is False
    assert (
        "runtime_probe_official_transcript_id_set_mismatch:repeat=1"
        in observed["issues"]
    )


def test_repeatability_rejects_active_row_missing_wire(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)
    first = _semantic_repeat_result(spec, refinement_active=False)
    second = _semantic_repeat_result(spec, refinement_active=False)
    for result in (first, second):
        row = result["line_reachability"]["dimensions"]["donk"]
        row["positive_refinement_active"] = True
        row.pop("positive_wire")
    runs = [first, second]
    national_runtime_probe.clear_runtime_probe_cache()
    monkeypatch.setattr(
        national_runtime_probe,
        "_run_once",
        lambda *_args: copy.deepcopy(runs.pop(0)),
    )

    observed = national_runtime_probe.run_national_runtime_probe(bot)

    assert observed["ok"] is False
    assert (
        "runtime_probe_per_scenario_refinement_wire_invalid:"
        "line:donk:positive_wire:repeat=1"
    ) in observed["issues"]


def test_repeatability_validator_rejects_missing_malformed_and_tampered_evidence():
    probe = _seal_passing_repeatability_probe(
        _semantic_repeat_result(_worker_spec(), refinement_active=False)
    )
    assert national_runtime_probe.validate_runtime_probe_repeatability_evidence(
        probe
    ) == []

    missing = dict(probe)
    missing.pop("repeatability")
    assert "runtime_probe_repeatability_evidence_missing" in (
        national_runtime_probe.validate_runtime_probe_repeatability_evidence(
            missing
        )
    )

    malformed = copy.deepcopy(probe)
    malformed["repeatability"]["view_digests"] = [
        {"repeat": 1, "sha256": "not-a-digest"}
    ]
    errors = national_runtime_probe.validate_runtime_probe_repeatability_evidence(
        malformed
    )
    assert "runtime_probe_repeatability_view_digests_length_invalid" in errors
    assert "runtime_probe_repeatability_view_digest_invalid" in errors

    tampered = copy.deepcopy(probe)
    tampered["repeatability"].update({
        "differing_path_count": 1,
        "differing_paths": [{
            "repeat": 2,
            "json_pointer": "/official_transcript_decisions/0/wire",
        }],
        "differing_paths_truncated": False,
    })
    assert "runtime_probe_repeatability_pass_has_differences" in (
        national_runtime_probe.validate_runtime_probe_repeatability_evidence(
            tampered
        )
    )

    isolation_tampered = copy.deepcopy(probe)
    isolation_tampered["managed_isolation"]["bpf_size"] = 2
    assert (
        "runtime_probe_repeatability_managed_isolation_digest_mismatch"
        in national_runtime_probe.validate_runtime_probe_repeatability_evidence(
            isolation_tampered
        )
    )

    failed_flag = copy.deepcopy(probe)
    failed_flag["ok"] = False
    assert "runtime_probe_repeatability_not_passed" in (
        national_runtime_probe.validate_runtime_probe_repeatability_evidence(
            failed_flag
        )
    )

    divergent = copy.deepcopy(probe)
    divergent["repeatability"]["view_digests"][1]["sha256"] = "f" * 64
    assert "runtime_probe_repeatability_pass_view_digests_diverge" in (
        national_runtime_probe.validate_runtime_probe_repeatability_evidence(
            divergent
        )
    )


def test_repeatability_identity_change_misses_legacy_cache_entry(tmp_path):
    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)
    identity_payload = national_runtime_probe._runtime_probe_identity_payload(
        national_runtime_probe.RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY,
        national_runtime_probe.RUNTIME_PROBE_NATIVE_TEMPLATE_DIGEST,
    )
    legacy_payload = copy.deepcopy(identity_payload)
    legacy_payload.pop("repeatability")
    legacy_identity = national_runtime_probe._canonical_digest(legacy_payload)
    legacy_spec = copy.deepcopy(spec)
    legacy_spec["probe_identity_digest"] = legacy_identity
    legacy_spec["spec_digest"] = national_runtime_probe._canonical_digest(
        legacy_spec
    )
    legacy_key = national_runtime_probe._canonical_digest({
        "probe_identity_digest": legacy_identity,
        "spec_digest": legacy_spec["spec_digest"],
        "timeout_sec": 1.0,
        "repeats": 2,
    })
    current_key = national_runtime_probe._cache_key(
        spec,
        timeout_sec=1.0,
        repeats=2,
    )

    national_runtime_probe.clear_runtime_probe_cache()
    national_runtime_probe._cache_put(legacy_key, {"ok": True})

    assert legacy_identity != national_runtime_probe.RUNTIME_PROBE_IDENTITY_DIGEST
    assert legacy_key != current_key
    assert national_runtime_probe._cache_get(current_key) is None


def test_runtime_probe_identity_binds_exact_native_template_bytes(tmp_path):
    bot = _write_typed_bot(tmp_path / "bot")
    spec = national_runtime_probe.build_runtime_probe_spec(bot)
    evidence = national_runtime_probe.runtime_probe_native_template_evidence()

    assert {
        "native_runtime_template_identity",
        "native_runtime_template_digest",
    }.issubset(spec)
    assert {
        key: spec[key]
        for key in evidence
    } == evidence
    assert national_runtime_probe.runtime_probe_native_template_evidence_matches(
        spec
    )
    assert (
        national_runtime_probe.RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY["sha256"]
        == hashlib.sha256(NATIVE_BOT_TEMPLATE.encode("utf-8")).hexdigest()
    )
    assert (
        national_runtime_probe.RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY["artifacts"]
        ["precompute.py"]["sha256"]
        == hashlib.sha256(NATIVE_PRECOMPUTE_TEMPLATE.encode("utf-8")).hexdigest()
    )

    changed_identity = copy.deepcopy(
        national_runtime_probe.RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY
    )
    changed_identity["artifacts"]["precompute.py"]["sha256"] = "0" * 64
    changed_identity["artifacts"]["precompute.py"]["size"] += 1
    changed_identity["combined_digest"] = national_runtime_authority._canonical_digest({
        "schema_version": changed_identity["schema_version"],
        "kind": changed_identity["kind"],
        "artifacts": changed_identity["artifacts"],
    })
    changed_digest = national_runtime_probe._canonical_digest(changed_identity)
    changed_probe_identity = national_runtime_probe._canonical_digest(
        national_runtime_probe._runtime_probe_identity_payload(
            changed_identity,
            changed_digest,
        )
    )

    assert changed_probe_identity != national_runtime_probe.RUNTIME_PROBE_IDENTITY_DIGEST


def test_runtime_probe_template_hash_change_misses_cache_without_version_bump(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")
    baseline_spec = national_runtime_probe.build_runtime_probe_spec(bot)
    baseline_key = national_runtime_probe._cache_key(
        baseline_spec,
        timeout_sec=1.0,
        repeats=2,
    )
    baseline_runtime_version = NATIONAL_DECISION_RUNTIME_VERSION
    changed_identity = copy.deepcopy(
        national_runtime_probe.RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY
    )
    changed_identity["artifacts"]["precompute.py"]["sha256"] = "f" * 64
    changed_identity["artifacts"]["precompute.py"]["size"] += 1
    changed_identity["combined_digest"] = national_runtime_authority._canonical_digest({
        "schema_version": changed_identity["schema_version"],
        "kind": changed_identity["kind"],
        "artifacts": changed_identity["artifacts"],
    })
    changed_template_digest = national_runtime_probe._canonical_digest(
        changed_identity
    )
    changed_probe_identity = national_runtime_probe._canonical_digest(
        national_runtime_probe._runtime_probe_identity_payload(
            changed_identity,
            changed_template_digest,
        )
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY",
        changed_identity,
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "RUNTIME_PROBE_NATIVE_TEMPLATE_DIGEST",
        changed_template_digest,
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "RUNTIME_PROBE_IDENTITY_DIGEST",
        changed_probe_identity,
    )

    changed_spec = national_runtime_probe.build_runtime_probe_spec(bot)
    changed_key = national_runtime_probe._cache_key(
        changed_spec,
        timeout_sec=1.0,
        repeats=2,
    )
    national_runtime_probe.clear_runtime_probe_cache()
    national_runtime_probe._cache_put(baseline_key, {"ok": True})

    assert NATIONAL_DECISION_RUNTIME_VERSION == baseline_runtime_version
    assert changed_spec["code_fingerprint"] == baseline_spec["code_fingerprint"]
    assert changed_key != baseline_key
    assert national_runtime_probe._cache_get(changed_key) is None


def test_runtime_probe_fails_closed_when_worker_omits_native_template_binding(
    tmp_path,
    monkeypatch,
):
    bot = _write_typed_bot(tmp_path / "bot")

    def run_once(_root, spec, _timeout):
        result = _fake_worker_result(spec)
        result.pop("native_runtime_template_identity")
        return result

    national_runtime_probe.clear_runtime_probe_cache()
    monkeypatch.setattr(national_runtime_probe, "_run_once", run_once)
    observed = national_runtime_probe.run_national_runtime_probe(bot)

    assert observed["ok"] is False
    assert (
        "runtime_probe_native_runtime_template_identity_mismatch:repeat=1"
        in observed["issues"]
    )


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
    assert "runtime_probe_worker_digest_mismatch:repeat=1" in observed["issues"]


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
    probe = _semantic_repeat_result(_worker_spec(), refinement_active=False)
    for name, row in probe["policy_counterfactuals"]["dimensions"].items():
        row.update({
            "causal_passed": True,
            "positive_wire_effect": True,
            "negative_control_stable": True,
            "negative_control_kind": (
                "selection_bias_guard_removed"
                if name == "showdown_range"
                else "authority_weight_removed"
            ),
            "socket_validated": True,
        })
    for row in probe["line_reachability"]["dimensions"].values():
        row.update({"ok": True, "policy_changed": False})
    return _seal_passing_repeatability_probe(probe)


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

    for field, malformed in (
        ("left_wire", "raise nonsense"),
        ("right_wire", "raise 22\n"),
        ("negative_left_wire", "garbage"),
        ("negative_right_wire", "raise -1"),
    ):
        candidate = _passing_gate_probe()
        candidate["policy_counterfactuals"]["dimensions"]["action_profile"][
            field
        ] = malformed
        assert runtime_architecture_policy._dynamic_probe_states(candidate)[
            "incremental_opponent_model"
        ] is False


def test_dynamic_line_state_recomputes_matched_ablation_wire_causality():
    probe = _passing_gate_probe()
    line = {
        "ok": True,
        "flag": "can_donk",
        "positive": True,
        "negative": False,
        "mixed_identity": True,
        "producer_reachable": True,
        "context_ablation_exact": True,
        "positive_without_ablation_digest": "a" * 64,
        "matched_without_ablation_digest": "a" * 64,
        "positive_without_cards_digest": "b" * 64,
        "mixed_without_cards_digest": "b" * 64,
        "mixing_class": "structural_air_no_hole_draw",
        "mixing_context_exact": True,
        "bounded_mixing": True,
        "stable_context_normalization_paths": [
            "deadline.hard_monotonic",
            "deadline.refinement_monotonic",
        ],
        "mixing_comparison_ignored_paths": ["cards"],
        "ablation_paths": [
            "line.can_donk",
            "line.line_tags:donk_opportunity",
        ],
        "consumer_wire_effect": True,
        "causal_passed": True,
        "socket_validated": True,
        "positive_wire": "raise 223",
        "negative_wire": "check",
        "matched_control_wire": "check",
        "mixed_identity_wire": "check",
    }
    probe["line_reachability"]["dimensions"]["donk"] = line
    assert runtime_architecture_policy._dynamic_probe_states(probe)[
        "donk_line_reachability"
    ] is True

    for field, malformed in (
        ("positive_wire", "raise nonsense"),
        ("negative_wire", "garbage"),
        ("matched_control_wire", "raise 2\r\n"),
        ("mixed_identity_wire", "check "),
    ):
        original = line[field]
        line[field] = malformed
        assert runtime_architecture_policy._dynamic_probe_states(probe)[
            "donk_line_reachability"
        ] is False
        line[field] = original

    line["matched_control_wire"] = line["positive_wire"]
    assert runtime_architecture_policy._dynamic_probe_states(probe)[
        "donk_line_reachability"
    ] is False

    line["matched_without_ablation_digest"] = "a" * 64
    line["mixed_identity_wire"] = "allin"
    assert runtime_architecture_policy._dynamic_probe_states(probe)[
        "donk_line_reachability"
    ] is False

    line["mixed_identity_wire"] = "check"
    line["matched_control_wire"] = "check"
    line["positive"] = False
    assert runtime_architecture_policy._dynamic_probe_states(probe)[
        "donk_line_reachability"
    ] is False

    line["positive"] = True
    line["matched_without_ablation_digest"] = "b" * 64
    assert runtime_architecture_policy._dynamic_probe_states(probe)[
        "donk_line_reachability"
    ] is False


def test_dynamic_showdown_state_binds_selection_guard_negative_control():
    probe = _passing_gate_probe()
    assert runtime_architecture_policy._dynamic_probe_states(probe)[
        "showdown_range_adaptation"
    ] is True

    probe["policy_counterfactuals"]["dimensions"]["showdown_range"][
        "negative_control_kind"
    ] = "authority_weight_removed"
    assert runtime_architecture_policy._dynamic_probe_states(probe)[
        "showdown_range_adaptation"
    ] is False


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
    failing_probe = _seal_passing_repeatability_probe(failing_probe)
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
