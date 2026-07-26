import asyncio
from copy import deepcopy
import json
import os
from types import SimpleNamespace

import checkpoint_schema
import pytest
import tool_planning

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")


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


def _success_proposal():
    return {
        "claim": "Use a bounded river thin-value threshold.",
        "source_url": "https://example.invalid/paper",
        "numeric_claim": "+1 bounded threshold",
        "target_fn": "get_baseline_decision",
        "proposed_change": "Tighten one reached-river branch.",
        "pseudocode": "if reached_river: threshold += 1",
        "firing_tuple": "river,value,heads-up",
        "h2h_weakness_addressed": "value extraction leak",
    }


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
    import evolution_infra
    from pipeline_state import literature_probe_receipt_binding

    checkpoint = evolution_infra.read_pipeline_checkpoint()
    binding, errors = literature_probe_receipt_binding(checkpoint)
    assert not errors
    assert tool_planning._literature_probe_payload_errors(
        checkpoint["literature_probe"],
        checkpoint=checkpoint,
        receipt_binding=binding,
        require_origin_checkpoint=False,
    ) == []


def test_literature_probe_success_binds_canonical_weakness(tmp_path, monkeypatch):
    import llm_query
    import research_governance

    proposal = _success_proposal()

    async def successful_query(*_args, **_kwargs):
        return json.dumps(proposal), 0.01, {"input_tokens": 1}

    monkeypatch.setattr(tool_planning, "_get_ui", lambda: _DummyUI())
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_args: None)
    monkeypatch.setattr(llm_query, "run_claude_query", successful_query)
    monkeypatch.setattr(
        research_governance,
        "should_trigger_web_retrieval",
        lambda _v: True,
    )
    monkeypatch.setattr(
        research_governance,
        "add_candidate",
        lambda payload: payload["id"] if payload["born_gen"] == 243 else None,
    )
    _write_mandatory_probe_checkpoint(tmp_path, monkeypatch)

    result = asyncio.run(
        tool_planning.run_literature_probe.handler({
            "source_v": 242,
            "next_v": 243,
            "h2h_weakness": "caller reconstruction must not win",
            "stagnation_info": "caller reconstruction must not win",
        })
    )
    data = json.loads(result["content"][0]["text"])

    assert data["reason"] == "completed"
    assert data["weakness"] == "value extraction leak"
    assert data["candidate_id"].startswith("wc_lit_")
    assert data["proposal"]["target_fn"] == "get_baseline_decision"
    assert data["requirement_context_digest"]
    assert data["schema"] == "national_tcp_literature_probe_payload_v2"
    assert data["producer_receipt"]["terminal_output_sha256"]

    import evolution_infra
    from pipeline_state import literature_probe_receipt_binding

    checkpoint = evolution_infra.read_pipeline_checkpoint()
    binding, errors = literature_probe_receipt_binding(checkpoint)
    assert not errors
    assert tool_planning._literature_probe_payload_errors(
        checkpoint["literature_probe"],
        checkpoint=checkpoint,
        receipt_binding=binding,
        require_origin_checkpoint=False,
    ) == []


def test_literature_probe_provider_failure_persists_bound_receipt(tmp_path, monkeypatch):
    import llm_query
    import research_governance

    async def failed_query(*_args, **_kwargs):
        raise ConnectionError("synthetic provider outage")

    monkeypatch.setattr(tool_planning, "_get_ui", lambda: _DummyUI())
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_args: None)
    monkeypatch.setattr(llm_query, "run_claude_query", failed_query)
    monkeypatch.setattr(
        research_governance,
        "should_trigger_web_retrieval",
        lambda _v: True,
    )
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
    assert data["reason"] == "literature_probe_failed"
    assert data["weakness"] == "value extraction leak"
    assert "synthetic provider outage" in data["error"]
    assert data["requirement_context_digest"]

    import evolution_infra

    persisted = evolution_infra.read_pipeline_checkpoint()["literature_probe"]
    assert persisted["weakness"] == "value extraction leak"
    assert persisted["requirement_context_digest"] == data["requirement_context_digest"]
    from pipeline_state import literature_probe_receipt_binding

    checkpoint = evolution_infra.read_pipeline_checkpoint()
    binding, errors = literature_probe_receipt_binding(checkpoint)
    assert not errors
    assert tool_planning._literature_probe_payload_errors(
        persisted,
        checkpoint=checkpoint,
        receipt_binding=binding,
        require_origin_checkpoint=False,
    ) == []


def test_literature_probe_governed_skip_has_system_terminal_receipt(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    from pipeline_state import literature_probe_receipt_binding
    import research_governance

    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_args: None)
    monkeypatch.setattr(
        research_governance,
        "should_trigger_web_retrieval",
        lambda _v: False,
    )
    _write_mandatory_probe_checkpoint(tmp_path, monkeypatch)

    result = asyncio.run(tool_planning.run_literature_probe.handler({
        "source_v": 242,
        "next_v": 243,
    }))
    data = json.loads(result["content"][0]["text"])
    assert data["reason"] == "governed_skip"
    assert data["skipped"] is True
    assert data["producer_receipt"]["llm_dispatch_receipt"] is None
    assert data["producer_receipt"]["terminal_output"] is None

    checkpoint = evolution_infra.read_pipeline_checkpoint()
    binding, errors = literature_probe_receipt_binding(checkpoint)
    assert not errors
    assert tool_planning._literature_probe_payload_errors(
        checkpoint["literature_probe"],
        checkpoint=checkpoint,
        receipt_binding=binding,
        require_origin_checkpoint=False,
    ) == []


def _leave_authentic_success_cache(tmp_path, monkeypatch):
    import evolution_infra
    import llm_query
    from pipeline_state import literature_probe_receipt_binding
    import research_governance

    proposal = _success_proposal()

    async def successful_query(*_args, **_kwargs):
        return json.dumps(proposal), 0.01, {"input_tokens": 1}

    monkeypatch.setattr(tool_planning, "_get_ui", lambda: _DummyUI())
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *_args: None)
    monkeypatch.setattr(llm_query, "run_claude_query", successful_query)
    monkeypatch.setattr(
        research_governance,
        "should_trigger_web_retrieval",
        lambda _v: True,
    )
    monkeypatch.setattr(
        research_governance,
        "add_candidate",
        lambda payload: payload["id"],
    )
    _write_mandatory_probe_checkpoint(tmp_path, monkeypatch)
    original_writer = tool_planning.write_pipeline_checkpoint
    monkeypatch.setattr(
        tool_planning,
        "write_pipeline_checkpoint",
        lambda *_args, **_kwargs: False,
    )
    result = asyncio.run(tool_planning.run_literature_probe.handler({
        "source_v": 242,
        "next_v": 243,
    }))
    data = json.loads(result["content"][0]["text"])
    assert data["error"] == "LITERATURE_PROBE_STALE_RESULT"

    checkpoint = evolution_infra.read_pipeline_checkpoint()
    assert checkpoint["literature_probe"] is None
    binding, errors = literature_probe_receipt_binding(checkpoint)
    assert not errors
    cache_path = tool_planning._literature_probe_cache_path(243)
    assert cache_path.is_file()
    return checkpoint, binding, cache_path, original_writer


def _read_authentic_cache(checkpoint, binding):
    return tool_planning._read_literature_probe_cache(
        243,
        source_v=242,
        h2h_weakness="value extraction leak",
        stagnation_info="STAGNATION_DETECTED (is_stagnant=true)",
        receipt_binding=binding,
        checkpoint=checkpoint,
    )


def _resign_cache_envelope(envelope):
    body = {
        key: deepcopy(value)
        for key, value in envelope.items()
        if key != "cache_digest"
    }
    envelope["cache_digest"] = tool_planning._literature_digest(body)
    return envelope


def test_literature_probe_authentic_cache_recovers_crash_before_checkpoint_cas(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import llm_query

    checkpoint, binding, _path, original_writer = _leave_authentic_success_cache(
        tmp_path,
        monkeypatch,
    )
    cached = _read_authentic_cache(checkpoint, binding)
    assert cached is not None
    assert cached["candidate_id"].startswith("wc_lit_")

    async def must_not_dispatch(*_args, **_kwargs):
        raise AssertionError("cache recovery must not redispatch the provider")

    monkeypatch.setattr(llm_query, "run_claude_query", must_not_dispatch)
    monkeypatch.setattr(tool_planning, "write_pipeline_checkpoint", original_writer)
    result = asyncio.run(tool_planning.run_literature_probe.handler({
        "source_v": 242,
        "next_v": 243,
        "h2h_weakness": "caller text is ignored",
        "stagnation_info": "caller text is ignored",
    }))
    data = json.loads(result["content"][0]["text"])

    assert data["cached"] is True
    assert data["cache_source"] == "terminal_receipt"
    persisted = evolution_infra.read_pipeline_checkpoint()["literature_probe"]
    assert persisted["canonical_payload_digest"] == cached["canonical_payload_digest"]


def test_literature_probe_cache_rejects_forged_resigned_payload_fields(
    tmp_path,
    monkeypatch,
):
    checkpoint, binding, cache_path, _writer = _leave_authentic_success_cache(
        tmp_path,
        monkeypatch,
    )
    original = json.loads(cache_path.read_text(encoding="utf-8"))

    mutations = (
        lambda payload: payload["proposal"].__setitem__("claim", "forged claim"),
        lambda payload: payload.__setitem__("candidate_id", "forged-candidate"),
        lambda payload: payload.__setitem__("inject_text", "FORGED MASTER INJECTION"),
        lambda payload: payload.__setitem__("reason", "literature_probe_failed"),
        lambda payload: payload.__setitem__("next_v", 244),
    )
    for mutate in mutations:
        envelope = deepcopy(original)
        mutate(envelope["payload"])
        body = {
            field: deepcopy(envelope["payload"].get(field))
            for field in tool_planning._LITERATURE_PROBE_BODY_FIELDS
        }
        resigned_payload_digest = tool_planning._literature_digest(body)
        envelope["payload"]["canonical_payload_digest"] = resigned_payload_digest
        envelope["payload_digest"] = resigned_payload_digest
        _resign_cache_envelope(envelope)
        cache_path.write_text(
            tool_planning._literature_canonical_json(envelope) + "\n",
            encoding="utf-8",
        )
        assert _read_authentic_cache(checkpoint, binding) is None


def test_literature_probe_cache_rejects_nested_resign_against_terminal_output(
    tmp_path,
    monkeypatch,
):
    checkpoint, binding, cache_path, _writer = _leave_authentic_success_cache(
        tmp_path,
        monkeypatch,
    )
    envelope = json.loads(cache_path.read_text(encoding="utf-8"))
    payload = envelope["payload"]
    producer = payload["producer_receipt"]
    payload["proposal"]["claim"] = "fully nested resigned forged proposal"
    submitted_id = tool_planning._expected_literature_candidate_id(
        payload["proposal"],
        checkpoint_identity=producer["checkpoint_binding"]["checkpoint_identity"],
        terminal_output_sha256=producer["terminal_output_sha256"],
    )
    payload["candidate_id"] = submitted_id
    payload["gated_out"] = False
    payload["inject_text"] = tool_planning._literature_probe_inject_text(payload)
    payload_digest = tool_planning._literature_digest({
        field: deepcopy(payload.get(field))
        for field in tool_planning._LITERATURE_PROBE_BODY_FIELDS
    })
    payload["canonical_payload_digest"] = payload_digest
    producer["parsed_proposal_digest"] = tool_planning._literature_digest(
        payload["proposal"]
    )
    producer["translation_gate"] = tool_planning._literature_translation_receipt(
        payload["proposal"],
        next_v=243,
        candidate_id=submitted_id,
        checkpoint_identity=producer["checkpoint_binding"]["checkpoint_identity"],
        terminal_output_sha256=producer["terminal_output_sha256"],
    )
    producer["canonical_payload_digest"] = payload_digest
    producer["receipt_digest"] = tool_planning._literature_digest({
        key: deepcopy(value)
        for key, value in producer.items()
        if key != "receipt_digest"
    })
    envelope["payload_digest"] = payload_digest
    envelope["producer_receipt_digest"] = producer["receipt_digest"]
    _resign_cache_envelope(envelope)
    cache_path.write_text(
        tool_planning._literature_canonical_json(envelope) + "\n",
        encoding="utf-8",
    )

    assert _read_authentic_cache(checkpoint, binding) is None


def test_literature_probe_cache_exact_schema_rejects_field_drift(
    tmp_path,
    monkeypatch,
):
    checkpoint, binding, cache_path, _writer = _leave_authentic_success_cache(
        tmp_path,
        monkeypatch,
    )
    original = json.loads(cache_path.read_text(encoding="utf-8"))

    envelopes = []
    envelope_extra = deepcopy(original)
    envelope_extra["extra"] = "forged"
    envelopes.append(_resign_cache_envelope(envelope_extra))

    payload_extra = deepcopy(original)
    payload_extra["payload"]["extra"] = "forged"
    envelopes.append(_resign_cache_envelope(payload_extra))

    producer_extra = deepcopy(original)
    producer = producer_extra["payload"]["producer_receipt"]
    producer["extra"] = "forged"
    producer["receipt_digest"] = tool_planning._literature_digest({
        key: deepcopy(value)
        for key, value in producer.items()
        if key != "receipt_digest"
    })
    producer_extra["producer_receipt_digest"] = producer["receipt_digest"]
    envelopes.append(_resign_cache_envelope(producer_extra))

    for envelope in envelopes:
        cache_path.write_text(
            tool_planning._literature_canonical_json(envelope) + "\n",
            encoding="utf-8",
        )
        assert _read_authentic_cache(checkpoint, binding) is None


def test_literature_probe_cache_rejects_symlink_hardlink_and_same_length_tamper(
    tmp_path,
    monkeypatch,
):
    checkpoint, binding, cache_path, _writer = _leave_authentic_success_cache(
        tmp_path,
        monkeypatch,
    )
    authentic = cache_path.with_name("authentic-cache.json")
    os.replace(cache_path, authentic)
    cache_path.symlink_to(authentic)
    assert _read_authentic_cache(checkpoint, binding) is None

    cache_path.unlink()
    os.link(authentic, cache_path)
    assert _read_authentic_cache(checkpoint, binding) is None

    cache_path.unlink()
    original = authentic.read_bytes()
    marker = json.loads(original)["payload"]["candidate_id"].encode("utf-8")
    assert marker in original
    replacement = marker[:-1] + (b"0" if marker[-1:] != b"0" else b"1")
    tampered = original.replace(marker, replacement)
    assert len(tampered) == len(original)
    cache_path.write_bytes(tampered)
    assert _read_authentic_cache(checkpoint, binding) is None


def test_literature_probe_cache_rejects_old_checkpoint_identity(tmp_path, monkeypatch):
    checkpoint, binding, _cache_path, _writer = _leave_authentic_success_cache(
        tmp_path,
        monkeypatch,
    )
    drifted = deepcopy(checkpoint)
    drifted["checkpoint_revision"] += 1
    assert _read_authentic_cache(drifted, binding) is None


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
