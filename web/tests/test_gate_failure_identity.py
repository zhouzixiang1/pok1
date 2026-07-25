"""Write-side identity contract for durable quality-gate failures."""

from __future__ import annotations

import json

import pytest

import checkpoint_schema
import evolution_infra
import tool_gates
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V
from server.routes import _helpers
from system_strict_bootstrap import build_fresh_bootstrap_receipt


RESET_DIGEST = "d" * 64


def _fresh_checkpoint(*, workflow: str) -> dict:
    bootstrap = build_fresh_bootstrap_receipt(
        active_bots=(),
        epoch_reset_receipt_digest=RESET_DIGEST,
    )
    audit_context = {
        "protocol_bootstrap": bootstrap,
        "selection": {"strategy": "fresh_policy_bootstrap"},
    }
    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=STRICT_TARGET_V,
        source_v=STRICT_SOURCE_V,
        audit_context=audit_context,
        published_high_water=STRICT_SOURCE_V,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=None,
    )
    return {
        "checkpoint_schema_version": checkpoint_schema.CHECKPOINT_SCHEMA_VERSION,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": binding,
        "next_v": STRICT_TARGET_V,
        "source_v": STRICT_SOURCE_V,
        "parent2_v": None,
        "stage": "workers_done",
        "workflow_run_id": workflow,
        "checkpoint_revision": 1,
        "audit_context": audit_context,
    }


def test_gate_failure_writer_binds_current_workflow_and_old_workflow_is_hidden(
    monkeypatch, tmp_path
):
    failures_path = tmp_path / "worker_failures.jsonl"
    checkpoint_path = tmp_path / "pipeline_state.json"
    authority = {"checkpoint": _fresh_checkpoint(workflow=f"generation:{STRICT_TARGET_V}:old")}

    monkeypatch.setattr(evolution_infra, "WORKER_FAILURES_FILE", failures_path)
    monkeypatch.setattr(
        tool_gates,
        "read_pipeline_checkpoint",
        lambda: authority["checkpoint"],
    )
    # The unit boundary still exercises the canonical checkpoint envelope; the
    # filesystem/archive validation itself has dedicated reset-receipt tests.
    monkeypatch.setattr(
        checkpoint_schema,
        "live_policy_epoch_reset_receipt_errors",
        lambda *_args, **_kwargs: [],
    )

    old = tool_gates._record_quality_failure(
        STRICT_TARGET_V,
        "national_native_contract",
        "native_tcp",
        "old workflow rejection",
    )
    authority["checkpoint"] = _fresh_checkpoint(
        workflow=f"generation:{STRICT_TARGET_V}:current"
    )
    current = tool_gates._record_quality_failure(
        STRICT_TARGET_V,
        "reviewer",
        "Code Reviewer",
        "current workflow rejection",
    )

    rows = [json.loads(line) for line in failures_path.read_text().splitlines()]
    assert rows == [old, current]
    assert current["gen"] == STRICT_TARGET_V
    assert current["evaluation_epoch"] == "national_tcp_policy_v1"
    assert current["workflow_run_id"] == f"generation:{STRICT_TARGET_V}:current"

    checkpoint_path.write_text(
        json.dumps(authority["checkpoint"]),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        _helpers,
        "_strict_reset_receipt",
        lambda _results: {"receipt_digest": RESET_DIGEST},
    )
    monkeypatch.setattr(_helpers, "_strict_published_active_pool", lambda: [])

    visible = _helpers.read_strict_worker_failures(
        failures_path,
        results_dir=tmp_path,
        checkpoint_path=checkpoint_path,
    )
    assert [row["error"] for row in visible] == ["current workflow rejection"]


@pytest.mark.parametrize(
    ("checkpoint_factory", "generation", "receipt_errors", "expected_issue"),
    [
        (
            lambda: None,
            STRICT_TARGET_V,
            [],
            "checkpoint_missing_or_not_object",
        ),
        (
            lambda: _fresh_checkpoint(workflow=f"generation:{STRICT_TARGET_V}:current"),
            STRICT_TARGET_V,
            ["policy_epoch_reset_receipt_missing_or_unsafe"],
            "policy_epoch_reset_receipt_missing_or_unsafe",
        ),
        (
            lambda: _fresh_checkpoint(workflow=f"generation:{STRICT_TARGET_V}:current"),
            STRICT_TARGET_V + 1,
            [],
            "checkpoint_event_generation_mismatch",
        ),
        (
            lambda: _fresh_checkpoint(workflow=""),
            STRICT_TARGET_V,
            [],
            "checkpoint_event_workflow_run_id_missing",
        ),
    ],
)
def test_gate_failure_writer_fails_before_opening_ledger_without_authority(
    monkeypatch,
    tmp_path,
    checkpoint_factory,
    generation,
    receipt_errors,
    expected_issue,
):
    failures_path = tmp_path / "worker_failures.jsonl"
    monkeypatch.setattr(evolution_infra, "WORKER_FAILURES_FILE", failures_path)
    monkeypatch.setattr(
        tool_gates,
        "read_pipeline_checkpoint",
        checkpoint_factory,
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "live_policy_epoch_reset_receipt_errors",
        lambda *_args, **_kwargs: list(receipt_errors),
    )

    with pytest.raises(checkpoint_schema.CheckpointSchemaError) as raised:
        tool_gates._record_quality_failure(
            generation,
            "reviewer",
            "Code Reviewer",
            "must not be written",
        )

    assert expected_issue in raised.value.errors
    assert not failures_path.exists()


def test_gate_failure_extra_cannot_override_identity(monkeypatch, tmp_path):
    failures_path = tmp_path / "worker_failures.jsonl"
    monkeypatch.setattr(evolution_infra, "WORKER_FAILURES_FILE", failures_path)
    monkeypatch.setattr(
        tool_gates,
        "read_pipeline_checkpoint",
        lambda: _fresh_checkpoint(workflow=f"generation:{STRICT_TARGET_V}:current"),
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "live_policy_epoch_reset_receipt_errors",
        lambda *_args, **_kwargs: [],
    )

    with pytest.raises(checkpoint_schema.CheckpointSchemaError) as raised:
        tool_gates._record_quality_failure(
            STRICT_TARGET_V,
            "reviewer",
            "Code Reviewer",
            "must not be written",
            workflow_run_id=f"generation:{STRICT_TARGET_V}:forged",
        )

    assert any(
        issue.startswith("quality_failure_reserved_identity_override:")
        for issue in raised.value.errors
    )
    assert not failures_path.exists()
