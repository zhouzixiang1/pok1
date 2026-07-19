"""Recovery regressions for stale architecture-policy candidate identity."""

from __future__ import annotations

from copy import deepcopy
import asyncio
import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _legacy_replan_fixture(tmp_path: Path) -> tuple[dict, Path, Path, dict]:
    from bot_artifact import hash_path
    from bot_namespace import (
        refresh_policy_identity_documents,
        strict_lineage_parent_versions,
    )
    from evolution_infra import copy_bot_tree_for_candidate
    from prepared_baseline_contract import build_prepared_artifact_contract

    source = tmp_path / "bots" / "national_v143"
    candidate = tmp_path / "bots" / "national_v148"
    source.parent.mkdir(parents=True)
    copy_bot_tree_for_candidate(ROOT / "bots" / "national_v143", source)

    expected = tmp_path / "expected" / "national_v148"
    expected.parent.mkdir(parents=True)
    copy_bot_tree_for_candidate(source, expected)
    refresh_policy_identity_documents(
        expected,
        148,
        parent_versions=strict_lineage_parent_versions(148, 143, None),
    )
    prepared_contract = build_prepared_artifact_contract(
        expected,
        source_v=143,
        next_v=148,
    )
    assert prepared_contract["prepared_artifact_hash"] == (
        "93ec85b0ecdbb77d7993f092d6acafd3c7f066e548b5c92b36947265b6bec070"
    )
    assert hashlib.sha256((source / "policy.py").read_bytes()).hexdigest() == (
        "600133ba79b429e85c67300ca189f4d28a6d4947d948bc5ac8b67ef0e4ef86cd"
    )
    assert hashlib.sha256(
        (source / "policy_epoch_receipt.json").read_bytes()
    ).hexdigest() == (
        "eae560f26ab979a59c130e51e1fa2c10b063bcc2c3848476ce69c7f147403a35"
    )

    # This is the exact buggy preimage: the target path contains untouched
    # parent-version identity documents rather than a v148 refresh.
    copy_bot_tree_for_candidate(source, candidate)
    checkpoint = {
        "next_v": 148,
        "source_v": 143,
        "parent2_v": None,
        "stage": "direction_audited",
        "checkpoint_revision": 10,
        "workflow_run_id": "generation:148:identity-replan-test",
        "epoch_binding": {
            "published_parent_identities": [{
                "version": 143,
                "tag_artifact_hash": hash_path(source),
            }],
        },
        "master_plan": {},
        "direction_audit": {"receipt_digest": "d" * 64},
        "gate_results": {},
        "runtime_contract_ledger": None,
        "repair_baseline_artifact_hash": "9" * 64,
        "publication_intent": None,
        "official_job": None,
        "infra_failure": None,
        "audit_context": {
            "selection": {"strategy": "singleton_strict_bootstrap"},
            "master_context": {"context_digest": "m" * 64},
            "protocol_bootstrap": {"receipt_digest": "p" * 64},
            "protocol_bootstrap_prepare": {"receipt_digest": "p" * 64},
            "prepared_artifact_contract": prepared_contract,
            "strict_policy_identity_refresh": {
                "output_artifact_hash": "9" * 64,
            },
            "durable_worker_output": {"artifact_hash": "9" * 64},
            "quality_native_match_timing_plan": {"hands": 70},
            "quality_native_match_timing_plan_digest": "8" * 64,
            "architecture_policy_identity_replan": {
                "source_stage": "quality_failed",
                "identity_errors": [
                    "runtime_architecture_policy_identity_digest_mismatch"
                ],
                "candidate_reset_to_source": True,
                "runtime_contract_ledger_reset": True,
                "previous_runtime_contract_ledger_digest": "7" * 64,
                "directive": "Build a fresh policy-bound Master plan.",
            },
        },
    }
    assert hash_path(candidate) == hash_path(source)
    assert hash_path(candidate) != prepared_contract["prepared_artifact_hash"]
    return checkpoint, source, candidate, prepared_contract


def test_existing_bad_direction_replan_recovers_and_is_idempotent(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import tool_planning
    from bot_artifact import hash_path
    from bot_namespace import (
        policy_identity_document_errors,
        strict_lineage_parent_versions,
    )

    checkpoint, source, candidate, prepared_contract = _legacy_replan_fixture(
        tmp_path
    )
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path / "results")
    writes = []

    def crash_before_checkpoint(*_args, **_kwargs):
        raise SystemExit("simulated crash after directory content-CAS")

    monkeypatch.setattr(tool_planning, "write_pipeline_checkpoint", crash_before_checkpoint)
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_a, **_k: None)

    with pytest.raises(SystemExit, match="simulated crash"):
        tool_planning._recover_persisted_architecture_policy_identity_replan(
            checkpoint,
            candidate,
            source,
        )
    assert hash_path(candidate) == prepared_contract["prepared_artifact_hash"]

    def record_checkpoint(*args, **kwargs):
        writes.append((args, kwargs))
        return True

    monkeypatch.setattr(tool_planning, "write_pipeline_checkpoint", record_checkpoint)
    recovered = _payload(
        tool_planning._recover_persisted_architecture_policy_identity_replan(
            checkpoint,
            candidate,
            source,
        )
    )

    assert recovered["recovered"] is True
    assert recovered["target_identity_refreshed"] is True
    assert recovered["prepared_artifact_hash"] == prepared_contract[
        "prepared_artifact_hash"
    ]
    assert policy_identity_document_errors(
        candidate,
        148,
        parent_versions=strict_lineage_parent_versions(148, 143, None),
    ) == []
    assert len(writes) == 1
    args, kwargs = writes[0]
    assert args[:3] == (148, 143, "direction_audited")
    assert kwargs["expected_checkpoint_revision"] == 10
    assert kwargs["expected_checkpoint_stage"] == "direction_audited"
    assert kwargs["replace_audit_context"] is True
    assert kwargs["clear_repair_baseline_artifact_hash"] is True
    assert kwargs["master_plan"] == {}
    replacement = kwargs["audit_context"]
    assert replacement["prepared_artifact_contract"] == prepared_contract
    assert replacement["architecture_policy_identity_replan"][
        "schema_version"
    ] == 2
    for stale_key in (
        "strict_policy_identity_refresh",
        "durable_worker_output",
        "quality_native_match_timing_plan",
        "quality_native_match_timing_plan_digest",
    ):
        assert stale_key not in replacement

    projected = deepcopy(checkpoint)
    projected["checkpoint_revision"] = 11
    projected["audit_context"] = replacement
    projected["repair_baseline_artifact_hash"] = None
    assert tool_planning._recover_persisted_architecture_policy_identity_replan(
        projected,
        candidate,
        source,
    ) is None


def test_quality_identity_failure_uses_target_refresh_transaction(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import tool_planning
    from bot_artifact import hash_path
    from bot_namespace import (
        refresh_policy_identity_documents,
        strict_lineage_parent_versions,
    )

    checkpoint, source, candidate, prepared_contract = _legacy_replan_fixture(
        tmp_path
    )
    with (candidate / "policy.py").open("a", encoding="utf-8") as writer:
        writer.write("\n# stale-policy Worker output\n")
    refresh_policy_identity_documents(
        candidate,
        148,
        parent_versions=strict_lineage_parent_versions(148, 143, None),
    )
    assert hash_path(candidate) != prepared_contract["prepared_artifact_hash"]
    checkpoint["stage"] = "quality_failed"
    checkpoint["master_plan"] = {"tasks": [{"worker_id": "old-worker"}]}
    checkpoint["gate_results"] = {
        "quality": {
            "national_architecture_transition": {
                "policy_identity_errors": [
                    "runtime_architecture_policy_identity_digest_mismatch"
                ]
            }
        }
    }
    del checkpoint["audit_context"]["architecture_policy_identity_replan"]
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path / "results")
    writes = []
    monkeypatch.setattr(
        tool_planning,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: writes.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_a, **_k: None)

    result = _payload(
        tool_planning._recover_architecture_policy_identity(
            checkpoint,
            candidate,
            source,
        )
    )

    assert result["recovered"] is True
    assert result["prepared_artifact_hash"] == prepared_contract[
        "prepared_artifact_hash"
    ]
    assert hash_path(candidate) == prepared_contract["prepared_artifact_hash"]
    assert writes[0][1]["reset_runtime_contract_ledger"] is True
    assert writes[0][1]["runtime_contract_ledger_reset_reason"] == (
        "architecture_policy_identity_replan"
    )
    assert writes[0][1]["expected_checkpoint_stage"] == "quality_failed"


def test_run_master_routes_persisted_bad_state_through_recovery_first(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import tool_planning

    checkpoint, source, candidate, _prepared_contract = _legacy_replan_fixture(
        tmp_path
    )
    expected = tool_planning._json_tool_result({
        "error": "ARCHITECTURE_POLICY_IDENTITY_REPLAN",
        "recovered": True,
        "next_tool": "run_master",
    })
    calls = []

    async def no_exhausted(*_args, **_kwargs):
        return None

    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *_a: checkpoint)
    monkeypatch.setattr(
        tool_planning,
        "_owned_infrastructure_failure",
        lambda *_a, **_k: (None, None),
    )
    monkeypatch.setattr(
        tool_planning,
        "_execute_exhausted_infrastructure_failure",
        no_exhausted,
    )
    monkeypatch.setattr(
        tool_planning,
        "get_bot_dir",
        lambda version: source if int(version) == 143 else candidate,
    )
    monkeypatch.setattr(
        tool_planning,
        "_recover_persisted_architecture_policy_identity_replan",
        lambda *args: calls.append(args) or expected,
    )

    result = asyncio.run(tool_planning.run_master.handler({
        "source_v": 143,
        "next_v": 148,
    }))

    assert result == expected
    assert calls == [(checkpoint, candidate, source)]


def test_identity_replan_checkpoint_cas_failure_restores_exact_preimage(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import tool_planning
    from bot_artifact import hash_path

    checkpoint, source, candidate, _prepared_contract = _legacy_replan_fixture(
        tmp_path
    )
    original_hash = hash_path(candidate)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path / "results")
    writes = iter([False, True])
    monkeypatch.setattr(
        tool_planning,
        "write_pipeline_checkpoint",
        lambda *_a, **_k: next(writes),
    )
    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *_a: checkpoint)
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_a, **_k: None)

    result = _payload(
        tool_planning._recover_persisted_architecture_policy_identity_replan(
            checkpoint,
            candidate,
            source,
        )
    )

    assert result["error"] == (
        "ARCHITECTURE_POLICY_IDENTITY_REPLAN_CHECKPOINT_CAS_FAILED"
    )
    assert result["candidate_preimage_restored"] is True
    assert hash_path(candidate) == original_hash

    retried = _payload(
        tool_planning._recover_persisted_architecture_policy_identity_replan(
            checkpoint,
            candidate,
            source,
        )
    )
    assert retried["recovered"] is True
    assert hash_path(candidate) == retried["prepared_artifact_hash"]


def test_identity_replan_rejects_forged_legacy_receipt_without_mutation(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import tool_planning
    from bot_artifact import hash_path

    checkpoint, source, candidate, _prepared_contract = _legacy_replan_fixture(
        tmp_path
    )
    checkpoint["audit_context"]["architecture_policy_identity_replan"][
        "untrusted_override"
    ] = True
    original_hash = hash_path(candidate)
    writes = []
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(
        tool_planning,
        "write_pipeline_checkpoint",
        lambda *_a, **_k: writes.append(True) or True,
    )

    result = _payload(
        tool_planning._recover_persisted_architecture_policy_identity_replan(
            checkpoint,
            candidate,
            source,
        )
    )

    assert result["error"] == (
        "ARCHITECTURE_POLICY_IDENTITY_REPLAN_RECOVERY_INVALID"
    )
    assert "identity_replan_receipt_fields_mismatch" in result[
        "validation_errors"
    ]
    assert result["candidate_overwritten"] is False
    assert writes == []
    assert hash_path(candidate) == original_hash


def test_checkpoint_identity_replan_replacement_clears_stale_authority(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    from bot_artifact import canonical_digest

    monkeypatch.setattr(
        evolution_infra,
        "PIPELINE_STATE_FILE",
        tmp_path / "pipeline_state.json",
    )
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "selected",
        audit_context={"selection": {"strategy": "single"}},
    ) is True
    assert evolution_infra.write_pipeline_checkpoint(300, 299, "preparing") is True
    assert evolution_infra.write_pipeline_checkpoint(300, 299, "prepared") is True
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "direction_audited",
        direction_audit={"receipt_digest": "1" * 64},
    ) is True
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "master_planned",
        master_plan={"tasks": [{"worker_id": "old"}]},
    ) is True
    assert evolution_infra.write_pipeline_checkpoint(300, 299, "workers_done") is True
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "quality_failed",
        gate_results={"quality": {"all_passed": False}},
        audit_context={
            "prepared_artifact_contract": {"contract_digest": "a" * 64},
            "strict_policy_identity_refresh": {"receipt_digest": "b" * 64},
            "durable_worker_output": {"artifact_hash": "c" * 64},
        },
        repair_baseline_artifact_hash="d" * 64,
    ) is True
    before = evolution_infra.read_pipeline_checkpoint()
    prepared_replacement = {
        "schema_version": 1,
        "source_v": 299,
        "next_v": 300,
        "prepared_artifact_hash": "4" * 64,
    }
    prepared_replacement["contract_digest"] = canonical_digest(
        prepared_replacement
    )
    replan_replacement = {
        "schema_version": 2,
        "kind": "single-parent-architecture-policy-identity-replan-v2",
        "prepared_artifact_hash": "4" * 64,
        "prepared_artifact_contract_digest": prepared_replacement[
            "contract_digest"
        ],
        "target_identity_refreshed": True,
        "stale_worker_gate_identity_cleared": True,
    }
    replan_replacement["receipt_digest"] = canonical_digest(
        replan_replacement
    )
    replacement = {
        "selection": {"strategy": "single"},
        "prepared_artifact_contract": prepared_replacement,
        "architecture_policy_identity_replan": replan_replacement,
    }

    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "direction_audited",
        master_plan={},
        audit_context=replacement,
        replace_audit_context=True,
        audit_context_replacement_reason="architecture_policy_identity_replan",
        clear_repair_baseline_artifact_hash=True,
        reset_runtime_contract_ledger=True,
        expected_runtime_contract_ledger_digest="",
        runtime_contract_ledger_reset_reason=(
            "architecture_policy_identity_replan"
        ),
        expected_checkpoint_revision=before["checkpoint_revision"],
        expected_checkpoint_stage="quality_failed",
        expected_workflow_run_id=before["workflow_run_id"],
    ) is True
    after = evolution_infra.read_pipeline_checkpoint()

    assert after["stage"] == "direction_audited"
    assert after["master_plan"] == {}
    assert after["gate_results"] == {}
    assert after["audit_context"] == replacement
    assert after["repair_baseline_artifact_hash"] is None

    # The same destructive knobs are not a generic checkpoint escape hatch.
    before_bytes = (tmp_path / "pipeline_state.json").read_bytes()
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "direction_audited",
        audit_context={},
        replace_audit_context=True,
        audit_context_replacement_reason="untrusted",
    ) is False
    assert (tmp_path / "pipeline_state.json").read_bytes() == before_bytes
