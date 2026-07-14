"""Execution-order checks for Workers sharing strict ``policy.py`` ownership."""

import asyncio
from unittest.mock import AsyncMock, patch


class _UI:
    costs = {}

    def __init__(self):
        self.messages = []

    def log_history(self, message, level="info"):
        self.messages.append((level, message))

    def clear_io(self):
        pass

    def set_status(self, *_args, **_kwargs):
        pass

    def log_io(self, *_args, **_kwargs):
        pass


def test_multiple_policy_workers_are_serialized_with_per_worker_snapshots(
    tmp_path, monkeypatch
):
    import agent_workers

    next_dir = tmp_path / "national_v143"
    next_dir.mkdir()
    policy = next_dir / "policy.py"
    policy.write_text("policy-v1", encoding="utf-8")
    tasks = [
        {
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["policy.py"],
            "files_allowed": [],
            "worker_prompt": "edit policy",
        },
        {
            "worker_id": 2,
            "role": "Opponent Modeler",
            "target_files": ["policy.py"],
            "files_allowed": [],
            "worker_prompt": "refine policy",
        },
    ]
    active = 0
    peak_active = 0

    async def fake_worker(task, idx, _template, candidate, *_args, **_kwargs):
        nonlocal active, peak_active
        assert task["target_files"] == ["policy.py"]
        active += 1
        peak_active = max(peak_active, active)
        await asyncio.sleep(0.01)
        (candidate / "policy.py").write_text(
            f"policy-v{idx + 2}", encoding="utf-8"
        )
        active -= 1
        return True

    monkeypatch.setattr(agent_workers, "_run_single_worker", fake_worker)
    ui = _UI()

    async def _run():
        with patch("audit_agents._run_worker_cot_check", new_callable=AsyncMock) as cot:
            cot.return_value = {"cot_consistent": True, "focus_areas": []}
            return await agent_workers._execute_workers(
                tasks,
                "{worker_prompt}",
                next_dir,
                143,
                [],
                ui,
                reviewer_feedback="",
                source_v=142,
            )

    success, snapshots, focus = asyncio.run(_run())

    assert success is True
    assert focus == []
    assert peak_active == 1
    assert snapshots[(0, "policy.py")] == "policy-v1"
    assert snapshots[(1, "policy.py")] == "policy-v2"
    assert policy.read_text(encoding="utf-8") == "policy-v3"
    assert any("SEQUENTIALLY" in message for _level, message in ui.messages)
