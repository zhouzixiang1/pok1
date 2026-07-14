import asyncio
import json
from types import SimpleNamespace

import checkpoint_schema
import pytest
from core import tool_planning


@pytest.fixture(autouse=True)
def _strict_parent_authority(monkeypatch):
    def resolve(label, **_kwargs):
        version = int(str(label).rsplit("_v", 1)[1])
        return SimpleNamespace(
            eligible=True,
            version=version,
            issues=(),
            runtime_manifest={"epoch": "national_tcp_policy_v1"},
            epoch_receipt={"epoch": "national_tcp_policy_v1", "version": version},
            publication_identity={"published": True, "version": version},
            certificate_digest="a" * 64,
        )

    monkeypatch.setattr(checkpoint_schema, "resolve_national_bot_spec", resolve)


class _DummyUI:
    def clear_io(self):
        pass

    def get_output(self):
        return ""


def _write_mandatory_probe_checkpoint(tmp_path, monkeypatch, *, next_v=243, source_v=242):
    import evolution_infra
    from master_context_contract import build_master_context

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path / "results")
    assert evolution_infra.write_pipeline_checkpoint(
        next_v,
        source_v,
        "prepared",
        audit_context={
            "master_context": build_master_context(
                next_v=next_v,
                source_v=source_v,
                stagnation_info="STAGNATION_DETECTED (is_stagnant=true)",
                match_analysis="value extraction leak",
            ),
        },
    )
    assert evolution_infra.write_pipeline_checkpoint(
        next_v,
        source_v,
        "direction_audited",
        direction_audit={
            "repetition_detected": True,
            "suggested_direction": "value extraction leak",
        },
    )


def test_literature_probe_timeout_returns_bound_continue_payload(tmp_path, monkeypatch):
    events = []

    async def slow_query(*_args, **_kwargs):
        await asyncio.sleep(1)
        return "{}", None, None

    monkeypatch.setattr(tool_planning, "LITERATURE_PROBE_TIMEOUT", 0.01)
    monkeypatch.setattr(tool_planning, "_get_ui", lambda: _DummyUI())
    monkeypatch.setattr(
        tool_planning,
        "log_system_event",
        lambda *event: events.append(event),
    )

    import llm_query
    import research_governance

    monkeypatch.setattr(llm_query, "run_claude_query", slow_query)
    monkeypatch.setattr(research_governance, "should_trigger_web_retrieval", lambda _v: True)
    _write_mandatory_probe_checkpoint(tmp_path, monkeypatch)

    result = asyncio.run(
        tool_planning.run_literature_probe.handler({
            "source_v": 242,
            "next_v": 243,
            "h2h_weakness": "value extraction leak",
            "stagnation_info": "stagnant",
        })
    )
    data = json.loads(result["content"][0]["text"])

    assert data["skipped"] is True
    assert data["reason"] == "literature_probe_timeout"
    assert data["next_v"] == 243
    assert "Proceed with run_master" in data["inject_text"]
    for field in (
        "master_context_digest",
        "direction_audit_digest",
        "requirement_context",
        "requirement_context_digest",
    ):
        assert data[field]
    assert any(event[0] == "pipeline.literature_probe_timeout" for event in events)


def test_literature_probe_rejects_weak_model_out_of_order_call(tmp_path, monkeypatch):
    import evolution_infra
    from master_context_contract import build_master_context

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    assert evolution_infra.write_pipeline_checkpoint(
        243,
        242,
        "prepared",
        audit_context={
            "master_context": build_master_context(
                next_v=243,
                source_v=242,
                stagnation_info="STAGNATION_DETECTED (is_stagnant=true)",
            ),
        },
    )

    result = asyncio.run(
        tool_planning.run_literature_probe.handler({"source_v": 242, "next_v": 243})
    )
    data = json.loads(result["content"][0]["text"])

    assert data["error"] == "LITERATURE_PROBE_WRONG_STAGE"
    assert data["checkpoint_stage"] == "prepared"


def test_direction_audit_rejects_late_weak_model_call_without_overwrite(tmp_path, monkeypatch):
    import evolution_infra

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    assert evolution_infra.write_pipeline_checkpoint(243, 242, "prepared")
    assert evolution_infra.write_pipeline_checkpoint(
        243,
        242,
        "direction_audited",
        direction_audit={"repetition_detected": False, "confidence": "high"},
    )
    assert evolution_infra.write_pipeline_checkpoint(
        243,
        242,
        "master_planned",
        master_plan={"analysis": "owned plan", "tasks": []},
    )
    calls = []

    async def _must_not_run(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("late direction audit must not run")

    monkeypatch.setattr(tool_planning, "_run_direction_audit", _must_not_run)
    result = asyncio.run(
        tool_planning.run_direction_audit.handler({"source_v": 242, "next_v": 243})
    )
    data = json.loads(result["content"][0]["text"])

    assert data["error"] == "DIRECTION_AUDIT_WRONG_STAGE"
    assert data["checkpoint_stage"] == "master_planned"
    assert calls == []
    checkpoint = evolution_infra.read_pipeline_checkpoint()
    assert checkpoint["stage"] == "master_planned"
    assert checkpoint["direction_audit"] == {"repetition_detected": False, "confidence": "high"}
