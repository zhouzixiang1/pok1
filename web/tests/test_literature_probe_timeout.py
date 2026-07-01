import asyncio
import json

from core import tool_planning


class _DummyUI:
    def clear_io(self):
        pass

    def get_output(self):
        return ""


def test_literature_probe_timeout_returns_continue_payload(monkeypatch):
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
    assert any(event[0] == "pipeline.literature_probe_timeout" for event in events)
