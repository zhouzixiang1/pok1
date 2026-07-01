import asyncio

import pytest

from core import llm_query


class _DummyUI:
    def __init__(self):
        self.costs = []

    def log_history(self, *_args, **_kwargs):
        pass

    def log_io(self, *_args, **_kwargs):
        pass

    def update_cost(self, role_name, cost_usd, usage):
        self.costs.append((role_name, cost_usd, usage))


async def _no_wait(*_args, **_kwargs):
    return None


def test_run_claude_query_emits_role_start_and_done(monkeypatch, tmp_path):
    events = []

    async def fake_stream(full_prompt, options, log_file_path, ui, role_name):
        assert "base prompt" in full_prompt
        assert "context body" in full_prompt
        return ["hello"], 0.125, {"input_tokens": 10, "output_tokens": 3}

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    context_file = tmp_path / "ctx.txt"
    context_file.write_text("context body", encoding="utf-8")
    log_file = tmp_path / "v243" / "logs" / "master_io.txt"
    log_file.parent.mkdir(parents=True)

    output, cost, usage = asyncio.run(
        llm_query.run_claude_query(
            "base prompt",
            [str(context_file)],
            _DummyUI(),
            "master",
            str(log_file),
            tools=["Read"],
        )
    )

    assert output == "hello"
    assert cost == 0.125
    assert usage["input_tokens"] == 10

    categories = [event[0] for event in events]
    assert "pipeline.llm_role_start" in categories
    assert "pipeline.llm_role_done" in categories

    start = next(fields for category, _sev, _msg, fields in events
                 if category == "pipeline.llm_role_start")
    done = next(fields for category, _sev, _msg, fields in events
                if category == "pipeline.llm_role_done")

    assert start["role"] == "master"
    assert start["context_file_count"] == 1
    assert start["tools"] == ["Read"]
    assert start["log_file"] == str(log_file)
    assert done["cost_usd"] == 0.125
    assert done["output_chars"] == len("hello")
    assert done["input_tokens"] == 10
    assert done["output_tokens"] == 3


def test_run_claude_query_emits_role_failed(monkeypatch, tmp_path):
    events = []

    async def fake_stream(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )
    monkeypatch.setattr(llm_query.asyncio, "sleep", _no_wait)

    log_file = tmp_path / "reviewer_io.txt"

    with pytest.raises(RuntimeError):
        asyncio.run(
            llm_query.run_claude_query(
                "prompt",
                [],
                _DummyUI(),
                "reviewer",
                str(log_file),
            )
        )

    failed = [event for event in events if event[0] == "pipeline.llm_role_failed"]
    assert len(failed) == 1
    _category, severity, message, fields = failed[0]
    assert severity == "error"
    assert "reviewer" in message
    assert fields["role"] == "reviewer"
    assert fields["exception_type"] == "RuntimeError"
    assert "boom" in fields["error"]
