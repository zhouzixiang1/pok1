"""Fail-closed reuse rules for the system-owned native TCP template."""

import copy

import national_runtime_probe
import pipeline_state
import tool_helpers
import tool_commit
from web.tests.runtime_probe_fixtures import passing_runtime_probe


def _precompute_only_runtime_identity():
    """Return a structurally valid identity differing only in precompute bytes."""

    import national_runtime_authority

    identity = copy.deepcopy(
        national_runtime_probe.RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY
    )
    precompute = identity["artifacts"]["precompute.py"]
    precompute["sha256"] = "f" * 64
    precompute["size"] += 1
    identity["combined_digest"] = national_runtime_authority._canonical_digest({
        "schema_version": identity["schema_version"],
        "kind": identity["kind"],
        "artifacts": identity["artifacts"],
    })
    return identity


def _install_precompute_only_runtime_identity(monkeypatch):
    """Simulate a source reload after only system precompute changed."""

    identity = _precompute_only_runtime_identity()
    template_digest = national_runtime_probe._canonical_digest(identity)
    probe_identity = national_runtime_probe._canonical_digest(
        national_runtime_probe._runtime_probe_identity_payload(
            identity,
            template_digest,
        )
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY",
        identity,
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "RUNTIME_PROBE_NATIVE_TEMPLATE_DIGEST",
        template_digest,
    )
    monkeypatch.setattr(
        national_runtime_probe,
        "RUNTIME_PROBE_IDENTITY_DIGEST",
        probe_identity,
    )
    return identity


def _native_quality_gate():
    probe = passing_runtime_probe()
    return {
        "all_passed": True,
        "critical_scenarios_passed": True,
        "workflow_profile_id": "national_native",
        "national_execution_mode": "native_tcp",
        "national_native_contract_ok": True,
        "national_capability_contract": {"dynamic_runtime_probe": probe},
        **national_runtime_probe.runtime_probe_native_template_evidence(),
    }


def test_runtime_probe_evidence_does_not_expose_mutable_runtime_authority():
    expected = copy.deepcopy(
        national_runtime_probe.RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY
    )
    evidence = national_runtime_probe.runtime_probe_native_template_evidence()
    evidence["native_runtime_template_identity"]["artifacts"]["precompute.py"][
        "sha256"
    ] = "0" * 64

    assert national_runtime_probe.RUNTIME_PROBE_NATIVE_TEMPLATE_IDENTITY == expected
    assert (
        national_runtime_probe.runtime_probe_native_template_evidence()[
            "native_runtime_template_identity"
        ]
        == expected
    )


def test_native_quality_reuse_requires_exact_template_evidence(monkeypatch):
    monkeypatch.setattr(
        tool_helpers,
        "_active_workflow_profile_info",
        lambda: ("national_native", "native_tcp"),
    )
    gate = _native_quality_gate()
    checkpoint = {"gate_results": {"quality": gate}}

    assert tool_helpers._quality_gate_ok(checkpoint) is True

    missing = copy.deepcopy(gate)
    missing.pop("native_runtime_template_identity")
    assert tool_helpers._quality_gate_ok(
        {"gate_results": {"quality": missing}}
    ) is False

    stale = copy.deepcopy(gate)
    stale["native_runtime_template_digest"] = "0" * 64
    assert tool_helpers._quality_gate_ok(
        {"gate_results": {"quality": stale}}
    ) is False

    _install_precompute_only_runtime_identity(monkeypatch)
    assert tool_helpers._quality_gate_ok(checkpoint) is False


def test_native_quality_reuse_rejects_tampered_repeatability_receipt(monkeypatch):
    monkeypatch.setattr(
        tool_helpers,
        "_active_workflow_profile_info",
        lambda: ("national_native", "native_tcp"),
    )
    monkeypatch.setattr(
        pipeline_state,
        "_active_workflow_profile_info",
        lambda: ("national_native", "native_tcp"),
    )
    quality = _native_quality_gate()
    malformed = copy.deepcopy(quality)
    malformed["national_capability_contract"]["dynamic_runtime_probe"][
        "repeatability"
    ]["view_contract"] = "tampered"

    assert tool_helpers._quality_gate_ok(
        {"gate_results": {"quality": malformed}}
    ) is False
    assert pipeline_state._quality_gate_matches_active_workflow(
        {"quality": malformed}
    ) is False

    raw_row_tampered = _native_quality_gate()
    raw_row_tampered["national_capability_contract"]["dynamic_runtime_probe"][
        "line_reachability"
    ]["dimensions"]["donk"].pop("positive_wire")
    assert tool_helpers._quality_gate_ok(
        {"gate_results": {"quality": raw_row_tampered}}
    ) is False
    assert pipeline_state._quality_gate_matches_active_workflow(
        {"quality": raw_row_tampered}
    ) is False

    nested_failure = _native_quality_gate()
    nested_failure["national_capability_contract"]["dynamic_runtime_probe"][
        "official_transcript_decisions"
    ][0].update({"ok": False, "issues": ["synthetic failure"]})
    assert tool_helpers._quality_gate_ok(
        {"gate_results": {"quality": nested_failure}}
    ) is False
    assert pipeline_state._quality_gate_matches_active_workflow(
        {"quality": nested_failure}
    ) is False


def test_pipeline_refreshes_native_quality_and_precommit_on_template_drift(
    monkeypatch,
):
    monkeypatch.setattr(
        pipeline_state,
        "_active_workflow_profile_info",
        lambda: ("national_native", "native_tcp"),
    )
    quality = _native_quality_gate()
    precommit = {
        "passed": True,
        "workflow_profile_id": "national_native",
        "national_execution_mode": "native_tcp",
        **national_runtime_probe.runtime_probe_native_template_evidence(),
    }

    assert pipeline_state._quality_gate_matches_active_workflow(
        {"quality": quality}
    ) is True
    assert pipeline_state._precommit_gate_matches_active_workflow(
        {"precommit_eval": precommit}
    ) is True

    _install_precompute_only_runtime_identity(monkeypatch)
    assert pipeline_state._quality_gate_matches_active_workflow(
        {"quality": quality}
    ) is False
    assert pipeline_state._precommit_gate_matches_active_workflow(
        {"precommit_eval": precommit}
    ) is False


def _commit_checkpoint():
    evidence = national_runtime_probe.runtime_probe_native_template_evidence()
    probe = passing_runtime_probe()
    probe.update(evidence)
    quality = {
        "version": 144,
        "source_v": 143,
        "all_passed": True,
        "critical_scenarios_passed": True,
        "workflow_profile_id": "national_native",
        "national_execution_mode": "native_tcp",
        "national_native_contract_ok": True,
        "code_fingerprint": "b" * 64,
        "runtime_contract_ledger_digest": "c" * 64,
        "runtime_probe_schema_version": (
            national_runtime_probe.RUNTIME_PROBE_SCHEMA_VERSION
        ),
        "runtime_probe_orchestrator_version": (
            national_runtime_probe.RUNTIME_PROBE_ORCHESTRATOR_VERSION
        ),
        "runtime_probe_scenario_digest": (
            national_runtime_probe.RUNTIME_PROBE_SCENARIO_DIGEST
        ),
        "runtime_probe_limits_digest": (
            national_runtime_probe.RUNTIME_PROBE_LIMITS_DIGEST
        ),
        "runtime_probe_identity_digest": (
            national_runtime_probe.RUNTIME_PROBE_IDENTITY_DIGEST
        ),
        "runtime_probe_managed_isolation_digest": probe[
            "managed_isolation_digest"
        ],
        "national_capability_contract": {"dynamic_runtime_probe": probe},
        **evidence,
    }
    precommit = {
        "version": 144,
        "source_v": 143,
        "passed": True,
        "workflow_profile_id": "national_native",
        "national_execution_mode": "native_tcp",
        "code_fingerprint": "b" * 64,
        "precommit_eval_contract": {"contract_digest": "d" * 64},
        "precommit_eval_contract_digest": "d" * 64,
        **evidence,
    }
    return {
        "next_v": 144,
        "source_v": 143,
        "workflow_profile_id": "national_native",
        "national_execution_mode": "native_tcp",
        "runtime_contract_ledger": {"stub": True},
        "master_plan": {"runtime_contract_ledger": {"stub": True}},
        "audit_context": {"precommit_eval_plan": {"stub": True}},
        "gate_results": {
            "quality": quality,
            "review": {
                "version": 144,
                "source_v": 143,
                "approved": True,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
            },
            "critic": {
                "version": 144,
                "source_v": 143,
                "approved": True,
                "llm_invoked": True,
                "critic_llm_executed": True,
                "schema_valid": True,
            },
            "precommit_eval": precommit,
        },
    }


def test_commit_ledger_rejects_precommit_from_another_native_template(
    monkeypatch,
    tmp_path,
):
    import national_native
    import national_position_contract
    import precommit_eval_contract
    import runtime_architecture_policy
    import tool_gates

    monkeypatch.setattr(tool_gates, "_bot_code_fingerprint", lambda _path: "b" * 64)
    monkeypatch.setattr(
        runtime_architecture_policy,
        "runtime_contract_ledger_digest",
        lambda _ledger: "c" * 64,
    )
    monkeypatch.setattr(
        runtime_architecture_policy,
        "validate_runtime_contract_ledger",
        lambda _ledger: [],
    )
    monkeypatch.setattr(
        precommit_eval_contract,
        "validate_precommit_plan",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        precommit_eval_contract,
        "validate_evaluation_contract",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(national_native, "check_native_contract", lambda *_a, **_k: [])
    monkeypatch.setattr(
        national_position_contract,
        "detect_position_semantics_errors",
        lambda *_args, **_kwargs: [],
    )
    checkpoint = _commit_checkpoint()

    accepted = tool_commit.validate_commit_gate_ledger(
        144,
        143,
        checkpoint,
        bot_dir=tmp_path,
    )
    assert accepted["ok"] is True

    malformed = copy.deepcopy(checkpoint)
    malformed_probe = malformed["gate_results"]["quality"][
        "national_capability_contract"
    ]["dynamic_runtime_probe"]
    malformed_probe["repeatability"]["redaction"] = {"candidate_source": "leak"}
    rejected_repeatability = tool_commit.validate_commit_gate_ledger(
        144,
        143,
        malformed,
        bot_dir=tmp_path,
    )
    assert rejected_repeatability["ok"] is False
    assert any(
        item.get("gate") == "runtime_probe_repeatability"
        for item in rejected_repeatability["failed_gates"]
    )

    _install_precompute_only_runtime_identity(monkeypatch)
    rejected = tool_commit.validate_commit_gate_ledger(
        144,
        143,
        checkpoint,
        bot_dir=tmp_path,
    )
    assert rejected["ok"] is False
    assert any(
        item.get("gate") == "precommit_native_runtime_identity"
        for item in rejected["failed_gates"]
    )
