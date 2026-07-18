"""Complete, sealed dynamic-probe fixtures for native workflow tests.

These are intentionally test-only receipts.  They mirror the bounded public
shape emitted by the managed worker so state-machine tests cannot bypass the
same repeatability admission that production quality/commit/formal paths use.
"""

from __future__ import annotations

import copy
from typing import Any


def _line_row() -> dict[str, Any]:
    return {
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


def _counterfactual_row() -> dict[str, Any]:
    return {
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


def seal_passing_runtime_probe(probe: dict[str, Any]) -> dict[str, Any]:
    """Bind a complete test probe to its first bounded repeatability view."""

    import national_runtime_probe

    result = copy.deepcopy(probe)
    isolation = result.get("managed_isolation")
    if not isinstance(isolation, dict) or not isolation:
        isolation = {
            "policy_sha256": "a" * 64,
            "bpf_sha256": "b" * 64,
            "bpf_size": 1,
            "namespaces": ["user", "net"],
        }
        result["managed_isolation"] = isolation
    result["managed_isolation_digest"] = national_runtime_probe._canonical_digest(
        isolation
    )
    result.update({
        "ok": True,
        "repeatability_ok": True,
        "evidence_integrity_ok": True,
        "failure_class": "none",
        "issues": [],
    })
    placeholder = {
        "schema_version": national_runtime_probe.RUNTIME_PROBE_REPEATABILITY_SCHEMA_VERSION,
        "view_contract": national_runtime_probe.RUNTIME_PROBE_REPEATABILITY_VIEW_CONTRACT,
        "view_digest_algorithm": national_runtime_probe.RUNTIME_PROBE_REPEATABILITY_DIGEST_ALGORITHM,
        "repeat_count": 2,
        "view_digest_count": 2,
        "view_digests": [
            {"repeat": 1, "sha256": "0" * 64},
            {"repeat": 2, "sha256": "0" * 64},
        ],
        "view_digests_truncated": False,
        "differing_path_count": 0,
        "differing_paths": [],
        "differing_paths_truncated": False,
        "redaction": dict(national_runtime_probe.RUNTIME_PROBE_REPEATABILITY_REDACTION),
    }
    result["repeatability"] = placeholder
    digest = national_runtime_probe._canonical_digest(
        national_runtime_probe._repeatability_view(result)
    )
    result["repeatability"] = {
        **placeholder,
        "view_digests": [
            {"repeat": 1, "sha256": digest},
            {"repeat": 2, "sha256": digest},
        ],
    }
    return result


def passing_runtime_probe() -> dict[str, Any]:
    """Return a complete passing raw-probe receipt for state-machine tests."""

    import national_runtime_probe
    from national_runtime_probe_scenarios import DECISION_SCENARIOS

    line = _line_row()
    counter = _counterfactual_row()
    probe = {
        "schema_version": national_runtime_probe.RUNTIME_PROBE_SCHEMA_VERSION,
        "orchestrator_version": national_runtime_probe.RUNTIME_PROBE_ORCHESTRATOR_VERSION,
        "worker_version": national_runtime_probe.RUNTIME_PROBE_WORKER_VERSION,
        "scenario_version": national_runtime_probe.RUNTIME_PROBE_SCENARIO_VERSION,
        "scenario_digest": national_runtime_probe.RUNTIME_PROBE_SCENARIO_DIGEST,
        "limits_digest": national_runtime_probe.RUNTIME_PROBE_LIMITS_DIGEST,
        "worker_digest": national_runtime_probe.RUNTIME_PROBE_WORKER_DIGEST,
        "probe_identity_digest": national_runtime_probe.RUNTIME_PROBE_IDENTITY_DIGEST,
        "policy_abi": "decision_context_v1_typed_intent_v1",
        "spec_digest": "test-runtime-probe-spec",
        "code_fingerprint": "a" * 64,
        "process_returncode": 0,
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
                "donk": copy.deepcopy(line),
                "delayed_probe": copy.deepcopy(line),
            },
        },
        "persistent_memory": {"ok": True, "issues": []},
        "policy_entrypoints": {"ok": True, "issues": [], "rows": []},
        "policy_counterfactuals": {
            "ok": True,
            "issues": [],
            "system_issues": [],
            "candidate_issues": [],
            "dimensions": {
                "action_profile": copy.deepcopy(counter),
                "terminal_response": copy.deepcopy(counter),
                "showdown_range": copy.deepcopy(counter),
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
            "ok": True,
            "active": False,
            "system_issues": [],
            "candidate_issues": [],
            "capability_issues": [],
            "worker_seed_equal": True,
            "bounded_work": True,
            "scaled_or_exhausted": True,
            "changes_sanitized_decision": True,
            "short": {
                "baseline_published": True,
                "baseline_target_met": True,
                "worker_seed": 20260710,
                "refinement_messages": 0,
                "trusted_refinement_steps": 0,
                "decision": {"kind": "pass"},
                "wire": "call",
            },
            "long": {
                "baseline_published": True,
                "baseline_target_met": True,
                "worker_seed": 20260710,
                "refinement_messages": 0,
                "trusted_refinement_steps": 0,
                "decision": {"kind": "pass"},
                "wire": "call",
            },
        },
        **national_runtime_probe.runtime_probe_native_template_evidence(),
    }
    return seal_passing_runtime_probe(probe)
