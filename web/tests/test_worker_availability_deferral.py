import asyncio

import pytest

from bot_namespace import STRICT_ARTIFACT_FILES, strict_artifact_layout_errors
from llm_availability import LLMAvailabilityBlocked, classify_llm_availability


class _UI:
    costs = {}

    def log_history(self, *_args, **_kwargs):
        pass

    def clear_io(self):
        pass

    def set_status(self, *_args, **_kwargs):
        pass

    def log_io(self, *_args, **_kwargs):
        pass


def _billing_blocked(role="worker"):
    issue = classify_llm_availability(
        ["API Error: 403 usage limit for this billing cycle"],
        statuses=[403],
    )
    assert issue is not None
    return LLMAvailabilityBlocked(issue, role=role)


def _write_strict_bot(root):
    root.mkdir()
    payloads = {
        "national_bot.py": "# system runtime\n",
        "precompute.py": "# system precompute\n",
        "policy.py": "value = 1\n",
        "national_runtime_manifest.json": "{}\n",
        "policy_epoch_receipt.json": "{}\n",
    }
    assert frozenset(payloads) == STRICT_ARTIFACT_FILES
    for relative, payload in payloads.items():
        (root / relative).write_text(payload, encoding="utf-8")
    assert strict_artifact_layout_errors(root) == []
    return root


def test_worker_availability_exception_skips_inner_retries_and_rolls_back(
    tmp_path,
    monkeypatch,
):
    import agent_workers

    candidate = _write_strict_bot(tmp_path / "national_v143")
    target = candidate / "policy.py"
    calls = 0
    query_kwargs = []

    async def unavailable(*_args, **kwargs):
        nonlocal calls
        calls += 1
        query_kwargs.append(kwargs)
        target.write_text("partial = True\n", encoding="utf-8")
        raise _billing_blocked()

    monkeypatch.setattr(agent_workers, "MAX_WORKER_RETRIES", 3)
    monkeypatch.setattr(agent_workers, "run_claude_query", unavailable)
    monkeypatch.setattr(agent_workers, "get_logs_dir", lambda _v: tmp_path / "logs")
    monkeypatch.setattr(agent_workers, "verify_code", lambda *_a, **_k: [])

    task = {
        "worker_id": "availability",
        "role": "Algorithmic Logic Architect",
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
        "worker_prompt": "make the assigned typed-policy change",
    }

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(
            agent_workers._execute_workers(
                [task],
                "{worker_prompt}",
                candidate,
                143,
                [],
                _UI(),
                reviewer_feedback="",
                source_v=None,
            )
        )

    assert calls == 1
    assert query_kwargs[0]["allowed_read_dirs"] == [candidate]
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_serial_policy_worker_availability_stops_batch_without_semantic_failure(
    tmp_path,
    monkeypatch,
):
    import agent_workers

    candidate = _write_strict_bot(tmp_path / "national_v143")
    policy = candidate / "policy.py"
    calls = []

    async def unavailable(*args, **_kwargs):
        role = args[3]
        calls.append(role)
        if "blocked" in role:
            policy.write_text("partial = True\n", encoding="utf-8")
            raise _billing_blocked(role)
        raise AssertionError("a later serialized Worker must not start")

    monkeypatch.setattr(agent_workers, "MAX_WORKER_RETRIES", 3)
    monkeypatch.setattr(agent_workers, "run_claude_query", unavailable)
    monkeypatch.setattr(agent_workers, "get_logs_dir", lambda _v: tmp_path / "logs")
    monkeypatch.setattr(agent_workers, "verify_code", lambda *_a, **_k: [])

    tasks = [
        {
            "worker_id": "blocked",
            "role": "blocked",
            "target_files": ["policy.py"],
            "must_change_files": ["policy.py"],
            "worker_prompt": "edit policy",
        },
        {
            "worker_id": "sibling",
            "role": "sibling",
            "target_files": ["policy.py"],
            "must_change_files": ["policy.py"],
            "worker_prompt": "refine policy",
        },
    ]

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(
            agent_workers._execute_workers(
                tasks,
                "{role} {worker_prompt}",
                candidate,
                    143,
                [],
                _UI(),
                reviewer_feedback="",
                source_v=None,
            )
        )

    assert len(calls) == 1
    assert "blocked" in calls[0]
    assert policy.read_text(encoding="utf-8") == "value = 1\n"
