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


def test_worker_executes_declared_nested_binary_target(tmp_path, monkeypatch):
    import agent_workers

    next_dir = tmp_path / "national_v11"
    table = next_dir / "tables" / "equity.bin"
    table.parent.mkdir(parents=True)
    table.write_bytes(b"before\xff")
    task = {
        "worker_id": 1,
        "role": "Precompute Artifact Engineer",
        "target_files": ["tables/equity.bin"],
        "files_allowed": ["tables/equity.bin"],
        "worker_prompt": "update the packed equity table",
    }

    async def fake_claude_query(*_args, **_kwargs):
        table.write_bytes(b"after\xfe")

    monkeypatch.setattr(agent_workers, "MAX_WORKER_RETRIES", 1)
    monkeypatch.setattr(agent_workers, "run_claude_query", fake_claude_query)
    monkeypatch.setattr(agent_workers, "verify_code", lambda *_args, **_kwargs: [])

    class UI:
        costs = {}

        def log_history(self, *_args, **_kwargs): pass
        def clear_io(self): pass
        def set_status(self, *_args, **_kwargs): pass
        def log_io(self, *_args, **_kwargs): pass

    async def _run():
        with patch("audit_agents._run_worker_cot_check", new_callable=AsyncMock) as cot:
            cot.return_value = {"cot_consistent": True, "focus_areas": []}
            return await agent_workers._execute_workers(
                [task], "{worker_prompt}", next_dir, 11, [], UI(),
                reviewer_feedback="", source_v=10,
            )

    success, snapshots, _focus = asyncio.run(_run())

    assert success is True
    assert snapshots[(0, "tables/equity.bin")] == b"before\xff"
    assert table.read_bytes() == b"after\xfe"


def test_worker_rejects_and_rolls_back_undeclared_binary_write(tmp_path, monkeypatch):
    import agent_workers

    next_dir = tmp_path / "national_v11"
    table = next_dir / "tables" / "equity.bin"
    rogue = next_dir / "tables" / "rogue.bin"
    table.parent.mkdir(parents=True)
    table.write_bytes(b"before\xff")
    task = {
        "worker_id": 1,
        "role": "Precompute Artifact Engineer",
        "target_files": ["tables/equity.bin"],
        "files_allowed": ["tables/equity.bin"],
        "worker_prompt": "update only the declared packed equity table",
    }

    async def fake_claude_query(*_args, **_kwargs):
        table.write_bytes(b"after\xfe")
        rogue.write_bytes(b"undeclared\x80")

    monkeypatch.setattr(agent_workers, "MAX_WORKER_RETRIES", 1)
    monkeypatch.setattr(agent_workers, "run_claude_query", fake_claude_query)
    monkeypatch.setattr(agent_workers, "verify_code", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(agent_workers, "_record_worker_failure", lambda *_args, **_kwargs: None)

    class UI:
        costs = {}

        def log_history(self, *_args, **_kwargs): pass
        def clear_io(self): pass
        def set_status(self, *_args, **_kwargs): pass
        def log_io(self, *_args, **_kwargs): pass

    success, _snapshots, _focus = asyncio.run(agent_workers._execute_workers(
        [task], "{worker_prompt}", next_dir, 11, [], UI(),
        reviewer_feedback="", source_v=10,
    ))

    assert success is False
    assert table.read_bytes() == b"before\xff"
    assert not rogue.exists()


def test_parallel_rollback_preserves_new_nested_binary_sibling(tmp_path):
    import agent_workers
    from worker_boundary import diff_snapshot, snapshot_python_files

    next_dir = tmp_path / "national_v11"
    next_dir.mkdir()
    before = snapshot_python_files(next_dir)
    sibling = next_dir / "tables" / "sibling.bin"
    rogue = next_dir / "rogue" / "undeclared.bin"
    sibling.parent.mkdir(parents=True)
    rogue.parent.mkdir(parents=True)
    sibling.write_bytes(b"sibling\xff")
    rogue.write_bytes(b"rogue\xfe")

    agent_workers._restore_worker_changes(
        next_dir,
        before,
        ignored_files={"tables/sibling.bin"},
    )

    assert sibling.read_bytes() == b"sibling\xff"
    assert not rogue.exists()
    assert diff_snapshot(next_dir, before) == ["tables", "tables/sibling.bin"]


def test_cross_files_allowed_forces_sequential_workers(tmp_path, monkeypatch):
    import agent_workers

    next_dir = tmp_path / "national_v11"
    next_dir.mkdir()
    (next_dir / "strategy.py").write_text("strategy-v1", encoding="utf-8")
    (next_dir / "helper.py").write_text("helper-v1", encoding="utf-8")
    tasks = [
        {
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["strategy.py"],
            "files_allowed": ["helper.py"],
            "worker_prompt": "edit strategy with helper context",
        },
        {
            "worker_id": 2,
            "role": "Opponent Modeler",
            "target_files": ["helper.py"],
            "files_allowed": [],
            "worker_prompt": "edit helper",
        },
    ]
    active = 0
    peak_active = 0

    async def fake_claude_query(*args, **_kwargs):
        nonlocal active, peak_active
        active += 1
        peak_active = max(peak_active, active)
        role_name = args[3]
        await asyncio.sleep(0.01)
        if "WORKER 1" in role_name:
            (next_dir / "strategy.py").write_text("strategy-v2", encoding="utf-8")
        else:
            (next_dir / "helper.py").write_text("helper-v2", encoding="utf-8")
        active -= 1

    monkeypatch.setattr(agent_workers, "MAX_WORKER_RETRIES", 1)
    monkeypatch.setattr(agent_workers, "run_claude_query", fake_claude_query)
    monkeypatch.setattr(agent_workers, "verify_code", lambda *_args, **_kwargs: [])

    class UI:
        costs = {}

        def log_history(self, *_args, **_kwargs): pass
        def clear_io(self): pass
        def set_status(self, *_args, **_kwargs): pass
        def log_io(self, *_args, **_kwargs): pass

    async def _run():
        with patch("audit_agents._run_worker_cot_check", new_callable=AsyncMock) as cot:
            cot.return_value = {"cot_consistent": True, "focus_areas": []}
            return await agent_workers._execute_workers(
                tasks, "{worker_prompt}", next_dir, 11, [], UI(),
                reviewer_feedback="", source_v=10,
            )

    success, _snapshots, _focus = asyncio.run(_run())

    assert success is True
    assert peak_active == 1
    assert (next_dir / "strategy.py").read_text(encoding="utf-8") == "strategy-v2"
    assert (next_dir / "helper.py").read_text(encoding="utf-8") == "helper-v2"


def test_worker_cot_check_receives_binary_size_and_digest_evidence(
    tmp_path, monkeypatch
):
    import audit_agents

    next_dir = tmp_path / "national_v11"
    table = next_dir / "tables" / "equity.bin"
    table.parent.mkdir(parents=True)
    table.write_bytes(b"after\xfe\x00")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "worker_1_io.txt").write_text(
        "Updated the packed equity table.", encoding="utf-8"
    )
    captured = {}

    async def fake_query(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return (
            "```json\n"
            '{"worker_id":1,"cot_consistent":true,"discrepancies":[],'
            '"logical_contradictions":[],"boundary_violations":[],'
            '"focus_areas":[]}\n```',
            0,
            0,
        )

    monkeypatch.setattr(audit_agents, "get_logs_dir", lambda _version: logs)
    monkeypatch.setattr(audit_agents, "run_claude_query", fake_query)

    result = asyncio.run(audit_agents._run_worker_cot_check(
        {
            "worker_id": 1,
            "role": "Precompute Artifact Engineer",
            "target_files": ["tables/equity.bin"],
            "worker_prompt": "update the packed equity table",
        },
        0,
        11,
        10,
        next_dir,
        {(0, "tables/equity.bin"): b"before\xff\x00"},
        object(),
    ))

    assert result["cot_consistent"] is True
    assert "binary artifact" in captured["prompt"]
    assert "sha256=" in captured["prompt"]
    assert "--- before/tables/equity.bin (binary metadata)" in captured["prompt"]
    assert "before\ufffd" not in captured["prompt"]
