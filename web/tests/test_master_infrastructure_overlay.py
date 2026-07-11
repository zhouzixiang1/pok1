import asyncio
import json

import evolution_infra
import tool_planning


def test_master_source_probe_retries_same_tool_then_abandons(tmp_path, monkeypatch):
    state_file = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_file)
    state_file.write_text(json.dumps({
        "next_v": 2,
        "source_v": 1,
        "stage": "direction_audited",
        "master_plan": None,
        "reviewer_feedback": "",
        "generation_attempt": 0,
        "audit_attempt": 0,
        "gate_results": {},
    }))
    bots = tmp_path / "bots"
    source = bots / "national_v1"
    candidate = bots / "national_v2"
    source.mkdir(parents=True)
    candidate.mkdir(parents=True)
    (source / "national_bot.py").write_text("SOURCE = True\n")
    monkeypatch.setattr(
        tool_planning,
        "get_bot_dir",
        lambda version: bots / f"national_v{version}",
    )
    monkeypatch.setattr(
        tool_planning,
        "_build_generation_architecture_policy",
        lambda _source_v: {
            "outcome": "infrastructure_failure",
            "policy": None,
            "capabilities": None,
            "infrastructure_failures": [{
                "component": "national_runtime_probe",
                "failure_class": "probe_infra",
                "issues": ["sandbox launch failed"],
            }],
        },
    )
    calls = {"master": 0, "abandon": 0}

    async def should_not_run_master(*_a, **_k):
        calls["master"] += 1
        raise AssertionError("Master LLM must not run without source capability evidence")

    monkeypatch.setattr(tool_planning, "_run_master_analysis", should_not_run_master)
    import national_runtime_probe
    monkeypatch.setattr(national_runtime_probe, "_bot_code_fingerprint", lambda _path: "source-fp")
    import tool_bot_management

    async def fake_abandon(*_a, **_k):
        calls["abandon"] += 1
        return {"abandoned": True, "reason": "test probe exhaustion"}

    monkeypatch.setattr(tool_bot_management, "_do_abandon_generation", fake_abandon)

    actions = []
    for _ in range(3):
        result = asyncio.run(tool_planning.run_master.handler({"source_v": 1, "next_v": 2}))
        payload = json.loads(result["content"][0]["text"])
        actions.append(payload["action"])

    checkpoint = evolution_infra.read_pipeline_checkpoint()
    assert actions == ["retry_same_tool", "retry_same_tool", "abandon_generation"]
    assert calls == {"master": 0, "abandon": 1}
    assert checkpoint["stage"] == "direction_audited"
    assert checkpoint["master_plan"] is None
    assert checkpoint["infra_failure"]["owner_tool"] == "run_master"
    assert checkpoint["infra_failure"]["attempt"] == 3
    assert checkpoint["infra_failure"]["exhausted"] is True
