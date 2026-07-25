import hashlib
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "web" / "core"))

from bot_namespace import bot_name, bot_tag, high_water_tag


def _patch_checkpoint(monkeypatch, checkpoint):
    import orchestrator

    monkeypatch.setattr(
        orchestrator,
        "_read_checkpoint_for_repeated_tool_guard",
        lambda: dict(checkpoint),
    )
    return orchestrator


def _strict_checkpoint(checkpoint):
    """Bind compact route fixtures to a synthetic strict published parent."""

    target = checkpoint["next_v"]
    source = checkpoint["source_v"]
    identity = {
        "version": source,
        "bot": bot_name(source),
        "role": "parent_source",
        "epoch": "national_tcp_policy_v1",
        "runtime_manifest_digest": "1" * 64,
        "epoch_receipt_digest": "2" * 64,
        "publication_identity_digest": "3" * 64,
        "certificate_digest": "4" * 64,
        "completion_tag": bot_tag(source),
        "completion_tag_object_oid": "5" * 40,
        "high_water_tag": high_water_tag(source),
        "high_water_tag_object_oid": "6" * 40,
        "publication_commit_oid": "7" * 40,
        "completion_tree_oid": "8" * 40,
        "tag_artifact_hash": "9" * 64,
    }
    binding = {
        "schema_version": 2,
        "epoch": "national_tcp_policy_v1",
        "mode": "published_strict_parent",
        "next_v": target,
        "source_v": source,
        "parent2_v": None,
        "parent_versions": [source],
        "source_artifact_inherited": True,
        "parent_authority": "strict_published_parent_resolution",
        "published_parent_identities": [identity],
        "protocol_bootstrap_receipt_digest": None,
        "policy_epoch_reset_receipt_digest": None,
        "published_high_water": source,
        "abandoned_receipt_floor": 0,
        "abandoned_receipt_head_digest": None,
        "allocation_floor": source,
    }
    encoded = json.dumps(
        binding,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **checkpoint,
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {
            **binding,
            "binding_digest": hashlib.sha256(encoded).hexdigest(),
        },
        "workflow_run_id": f"generation:{target}:repeated-tool-test",
        "checkpoint_revision": 1,
    }


def test_legacy_checkpoint_cannot_authorize_repeated_pipeline_tool(monkeypatch):
    orchestrator = _patch_checkpoint(
        monkeypatch,
        {
            "next_v": 155,
            "source_v": 142,
            "stage": "direction_audited",
            "master_plan": None,
            "audit_attempt": 1,
        },
    )

    assert orchestrator._classify_allowed_repeated_pipeline_tool("run_master", {}) is None


def test_repeated_run_master_after_validation_failure_is_corrective(monkeypatch):
    orchestrator = _patch_checkpoint(
        monkeypatch,
        _strict_checkpoint({
            "next_v": 164,
            "source_v": 163,
            "stage": "direction_audited",
            "master_plan": None,
            "audit_attempt": 1,
            "audit_context": {
                "master_validation": {
                    "errors": ["RUNTIME_CONTRACT_INVALID"],
                },
            },
        }),
    )

    result = orchestrator._classify_allowed_repeated_pipeline_tool("run_master", {})

    assert result["reason"] == "corrective_master_replan"
    assert result["audit_attempt"] == 1


def test_repeated_run_master_without_failure_context_stays_redundant(monkeypatch):
    orchestrator = _patch_checkpoint(
        monkeypatch,
        _strict_checkpoint({
            "next_v": 164,
            "source_v": 163,
            "stage": "direction_audited",
            "master_plan": None,
            "audit_attempt": 0,
        }),
    )

    assert orchestrator._classify_allowed_repeated_pipeline_tool("run_master", {}) is None


def test_repeated_execute_workers_on_quality_failed_route_is_corrective(monkeypatch):
    orchestrator = _patch_checkpoint(
        monkeypatch,
        _strict_checkpoint({
            "next_v": 165,
            "source_v": 164,
            "stage": "quality_failed",
            "master_plan": {"tasks": []},
            "gate_results": {"quality": {"passed": False}},
        }),
    )

    result = orchestrator._classify_allowed_repeated_pipeline_tool("execute_workers", {})

    assert result["reason"] == "corrective_gate_reentry"
    assert result["stage"] == "quality_failed"


def test_second_quality_gate_without_repair_history_stays_redundant(monkeypatch):
    orchestrator = _patch_checkpoint(
        monkeypatch,
        _strict_checkpoint({
            "next_v": 165,
            "source_v": 164,
            "stage": "workers_done",
            "master_plan": {"tasks": []},
            "gate_results": {},
        }),
    )

    assert orchestrator._classify_allowed_repeated_pipeline_tool("run_quality_gates", {}) is None


def test_second_quality_gate_after_repair_is_corrective(monkeypatch):
    orchestrator = _patch_checkpoint(
        monkeypatch,
        _strict_checkpoint({
            "next_v": 165,
            "source_v": 164,
            "stage": "workers_done",
            "master_plan": {"tasks": []},
            "reviewer_feedback": "quality failed: fix exact blocker",
            "gate_results": {"quality": {"passed": False}},
        }),
    )

    result = orchestrator._classify_allowed_repeated_pipeline_tool("run_quality_gates", {})

    assert result["reason"] == "corrective_gate_reentry"
    assert result["stage"] == "workers_done"
