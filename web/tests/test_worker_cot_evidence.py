import asyncio
import json

import pytest


class _UI:
    def log_history(self, *_args, **_kwargs):
        return None

    def clear_io(self):
        return None

    def set_status(self, *_args, **_kwargs):
        return None


def _task():
    return {
        "worker_id": 1,
        "role": "logic",
        "worker_prompt": "change the strict policy",
        "target_files": ["policy.py"],
    }


def _evidence(audit_agents, *, output="CURRENT_FENCED_OUTPUT"):
    return audit_agents.bind_fenced_worker_output(
        task=_task(),
        worker_id=1,
        next_v=145,
        source_v=143,
        worker_effect_identity={
            "workflow_run_id": "generation:145:workflow-v1",
            "envelope_digest": "a" * 64,
            "effect_id": "worker-effect-1",
            "lease_epoch": 3,
        },
        attempt=2,
        dispatch_receipt_digest="b" * 64,
        output=output,
    )


def test_worker_cot_uses_fenced_output_and_never_reopens_worker_log(
    monkeypatch,
    tmp_path,
):
    import audit_agents

    candidate = tmp_path / "national_v145"
    candidate.mkdir()
    before = "def decide(context):\n    return {'kind': 'pass'}\n"
    after = "def decide(context):\n    return {'kind': 'fold'}\n"
    (candidate / "policy.py").write_text(after, encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "worker_1_io.txt").write_text(
        "FORGED_MUTABLE_LOG_OUTPUT",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit_agents, "get_logs_dir", lambda _version: logs)
    captured = {}

    async def query(prompt, *_args, **_kwargs):
        captured["prompt"] = str(prompt)
        return json.dumps({
            "worker_id": 1,
            "cot_consistent": True,
            "discrepancies": [],
            "logical_contradictions": [],
            "boundary_violations": [],
            "focus_areas": [],
        }), 0.0, {}

    monkeypatch.setattr(audit_agents, "run_claude_query", query)
    result = asyncio.run(audit_agents._run_worker_cot_check(
        _task(),
        0,
        145,
        143,
        candidate,
        {(0, "policy.py"): before},
        _UI(),
        worker_output_evidence=_evidence(audit_agents),
    ))

    assert result["cot_consistent"] is True
    assert "CURRENT_FENCED_OUTPUT" in captured["prompt"]
    assert "FORGED_MUTABLE_LOG_OUTPUT" not in captured["prompt"]


def test_worker_cot_rejects_tampered_fenced_output_before_provider(
    monkeypatch,
    tmp_path,
):
    import audit_agents

    candidate = tmp_path / "national_v145"
    candidate.mkdir()
    before = "def decide(context):\n    return {'kind': 'pass'}\n"
    (candidate / "policy.py").write_text(
        "def decide(context):\n    return {'kind': 'fold'}\n",
        encoding="utf-8",
    )
    evidence = _evidence(audit_agents)
    evidence.payload["output_excerpt"] = "SELF_RESIGNED_FORGERY"

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("provider must not receive forged Worker output")

    monkeypatch.setattr(audit_agents, "run_claude_query", forbidden)
    with pytest.raises(
        audit_agents.WorkerCoTEvidenceError,
        match="excerpt_digest_mismatch|binding_digest_mismatch",
    ):
        asyncio.run(audit_agents._run_worker_cot_check(
            _task(),
            0,
            145,
            143,
            candidate,
            {(0, "policy.py"): before},
            _UI(),
            worker_output_evidence=evidence,
        ))


def test_worker_cot_rejects_plain_self_signed_dict_authority(tmp_path):
    import audit_agents

    candidate = tmp_path / "national_v145"
    candidate.mkdir()
    (candidate / "policy.py").write_text("changed\n", encoding="utf-8")
    forged = dict(_evidence(audit_agents).payload)

    with pytest.raises(
        audit_agents.WorkerCoTEvidenceError,
        match="authority_missing",
    ):
        asyncio.run(audit_agents._run_worker_cot_check(
            _task(),
            0,
            145,
            143,
            candidate,
            {(0, "policy.py"): "before\n"},
            _UI(),
            worker_output_evidence=forged,
        ))


def test_execute_workers_binds_current_provider_result_to_effect_lease(
    monkeypatch,
    tmp_path,
):
    import agent_workers
    import audit_agents

    candidate = tmp_path / "workspace"
    candidate.mkdir()
    policy = candidate / "policy.py"
    policy.write_text(
        "def decide(context):\n    return {'kind': 'pass'}\n",
        encoding="utf-8",
    )
    task = _task()
    captured = {}

    async def worker_query(_prompt, *_args, **_kwargs):
        policy.write_text(
            "def decide(context):\n    return {'kind': 'fold'}\n",
            encoding="utf-8",
        )
        return "CURRENT_PROVIDER_RESULT", 0.0, {}

    async def cot_check(*_args, **kwargs):
        evidence = kwargs.get("worker_output_evidence")
        captured["payload"] = audit_agents._open_fenced_worker_output(
            evidence,
            task=task,
            worker_id=1,
            next_v=145,
            source_v=143,
        )
        return {"cot_consistent": True, "focus_areas": []}

    monkeypatch.setattr(agent_workers, "run_claude_query", worker_query)
    monkeypatch.setattr(audit_agents, "_run_worker_cot_check", cot_check)
    success, _snapshots, _focus = asyncio.run(agent_workers._execute_workers(
        [task],
        "",
        candidate,
        145,
        [],
        _UI(),
        reviewer_feedback="",
        source_v=143,
        worker_effect_identity={
            "workflow_run_id": "generation:145:workflow-v1",
            "envelope_digest": "a" * 64,
            "effect_id": "worker-effect-1",
            "lease_epoch": 3,
        },
    ))

    assert success is True
    assert captured["payload"]["output_sha256"] == __import__(
        "hashlib"
    ).sha256(b"CURRENT_PROVIDER_RESULT").hexdigest()
    assert captured["payload"]["effect_id"] == "worker-effect-1"
    assert captured["payload"]["lease_epoch"] == 3
