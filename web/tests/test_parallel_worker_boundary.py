import asyncio
from unittest.mock import AsyncMock, patch


def test_parallel_worker_boundary_ignores_disjoint_sibling_edits(tmp_path, monkeypatch):
    import agent_workers

    next_dir = tmp_path / "national_v11"
    next_dir.mkdir()
    for name in ("constants.py", "opponent.py", "strategy.py"):
        (next_dir / name).write_text(f"# {name} baseline\n", encoding="utf-8")

    tasks = [
        {
            "worker_id": 1,
            "role": "Hyperparameter Tuner",
            "target_files": ["constants.py"],
            "files_allowed": ["constants.py"],
            "worker_prompt": "edit constants",
        },
        {
            "worker_id": 2,
            "role": "Opponent Modeler",
            "target_files": ["opponent.py"],
            "files_allowed": ["opponent.py"],
            "worker_prompt": "edit opponent",
        },
        {
            "worker_id": 3,
            "role": "Algorithmic Logic Architect",
            "target_files": ["strategy.py"],
            "files_allowed": ["strategy.py"],
            "worker_prompt": "edit strategy",
        },
    ]

    class UI:
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

    async def fake_claude_query(*_args, **kwargs):
        role_name = _args[3] if len(_args) > 3 else ""
        if "WORKER 2" in role_name:
            (next_dir / "opponent.py").write_text("# opponent changed\n", encoding="utf-8")
            await asyncio.sleep(0.01)
        elif "WORKER 3" in role_name:
            (next_dir / "strategy.py").write_text("# strategy changed\n", encoding="utf-8")
            await asyncio.sleep(0.01)
        else:
            await asyncio.sleep(0.05)
            (next_dir / "constants.py").write_text("# constants changed\n", encoding="utf-8")

    monkeypatch.setattr(agent_workers, "MAX_WORKER_RETRIES", 1)
    monkeypatch.setattr(agent_workers, "run_claude_query", fake_claude_query)
    monkeypatch.setattr(agent_workers, "verify_code", lambda *_args, **_kwargs: [])

    async def _run():
        with patch("audit_agents._run_worker_cot_check", new_callable=AsyncMock) as cot:
            cot.return_value = {"cot_consistent": True, "focus_areas": []}
            return await agent_workers._execute_workers(
                tasks,
                "{worker_prompt}",
                next_dir,
                11,
                [],
                UI(),
                reviewer_feedback="",
                source_v=10,
            )

    success, snapshots, focus = asyncio.run(_run())

    assert success is True
    assert focus == []
    assert snapshots[(0, "constants.py")] == "# constants.py baseline\n"
    assert snapshots[(1, "opponent.py")] == "# opponent.py baseline\n"
    assert snapshots[(2, "strategy.py")] == "# strategy.py baseline\n"
    assert (next_dir / "constants.py").read_text(encoding="utf-8") == "# constants changed\n"
    assert (next_dir / "opponent.py").read_text(encoding="utf-8") == "# opponent changed\n"
    assert (next_dir / "strategy.py").read_text(encoding="utf-8") == "# strategy changed\n"
