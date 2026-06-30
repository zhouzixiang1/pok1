import asyncio
import json
from pathlib import Path


def test_import_contract_catches_missing_symbol_that_py_compile_misses(tmp_path):
    from code_verification import run_import_contract_test, verify_code

    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "main.py").write_text("import strategy\n")
    (bot / "strategy.py").write_text("from opponent import missing_symbol\n")
    (bot / "opponent.py").write_text("def other_symbol():\n    return 1\n")

    assert verify_code(str(bot)) == []
    errors = run_import_contract_test(str(bot))
    assert errors
    assert errors[0]["module"] in {"main", "strategy"}
    assert "missing_symbol" in errors[0]["traceback"]


def test_smoke_test_fails_before_battle_on_import_contract_error(tmp_path):
    from code_verification import run_smoke_test

    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "main.py").write_text("import strategy\n")
    (bot / "strategy.py").write_text("from opponent import missing_symbol\n")
    (bot / "opponent.py").write_text("def other_symbol():\n    return 1\n")

    errors = run_smoke_test(str(bot))
    assert errors
    assert "missing_symbol" in errors[0]


def test_quality_gate_records_runtime_import_failure_as_quality_failed(monkeypatch):
    import evolution_infra
    import tool_gates

    evolution_infra.write_pipeline_checkpoint(2, 1, "workers_done")

    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda v: evolution_infra.BOTS_DIR / f"claude_v{v}")
    monkeypatch.setattr(tool_gates, "_py_files_changed_between", lambda _src, _dst: ["main.py"])
    monkeypatch.setattr(tool_gates, "verify_code", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_import_contract_test", lambda _bot_dir: [{
        "module": "strategy",
        "exception": "ImportError",
        "message": "cannot import name 'missing_symbol'",
        "traceback": "ImportError: missing_symbol",
    }])
    monkeypatch.setattr(tool_gates, "run_smoke_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda: [])
    monkeypatch.setattr(tool_gates, "check_code_size", lambda *_a, **_k: (10, []))
    monkeypatch.setattr(tool_gates, "verify_fixes", lambda _bot_dir: {"mandatory": {"ok": True}})
    monkeypatch.setattr(tool_gates, "run_decision_test_details", lambda *_a, **_k: {
        "pass_rate": 1.0,
        "passed": 1,
        "total": 1,
        "critical_passed": 1,
        "critical_total": 1,
        "critical_failures": [],
        "failures": [],
        "scenarios": [],
    })

    import audit_agents

    async def _no_dynamic_tests(*_a, **_k):
        return []

    monkeypatch.setattr(audit_agents, "_generate_dynamic_tests", _no_dynamic_tests)

    result = asyncio.run(tool_gates.run_quality_gates.handler({"version": 2, "source_v": 1}))
    data = json.loads(result["content"][0]["text"])

    assert data["import_ok"] is False
    assert "runtime_import" in " ".join(data["failed_gates"])
    ckpt = evolution_infra.read_pipeline_checkpoint()
    assert ckpt["stage"] == "quality_failed"
    assert ckpt["gate_results"]["quality"]["import_ok"] is False


def test_run_master_blocks_after_crossover_checkpoint_without_analysis(monkeypatch):
    import tool_planning

    called = []

    async def _should_not_call(*_a, **_k):
        called.append(True)
        return {"tasks": []}

    monkeypatch.setattr(tool_planning, "_run_master_analysis", _should_not_call)
    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda _v, _sv: {
        "next_v": 230,
        "source_v": 195,
        "stage": "workers_done",
        "master_plan": None,
        "parent2_v": 219,
    })

    result = asyncio.run(tool_planning.run_master.handler({"next_v": 230, "source_v": 195}))
    data = json.loads(result["content"][0]["text"])

    assert data["error"] == "CROSSOVER_ALREADY_DONE"
    assert called == []


def test_run_master_blocks_after_crossover_checkpoint_with_synthetic_plan(monkeypatch):
    import tool_planning

    called = []

    async def _should_not_call(*_a, **_k):
        called.append(True)
        return {"tasks": []}

    monkeypatch.setattr(tool_planning, "_run_master_analysis", _should_not_call)
    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda _v, _sv: {
        "next_v": 230,
        "source_v": 195,
        "stage": "workers_done",
        "master_plan": {"strategy": "crossover", "tasks": []},
        "parent2_v": 219,
    })

    result = asyncio.run(tool_planning.run_master.handler({"next_v": 230, "source_v": 195}))
    data = json.loads(result["content"][0]["text"])

    assert data["error"] == "CROSSOVER_ALREADY_DONE"
    assert "plan" not in data
    assert called == []


def test_illegal_stage_regression_is_not_written():
    import evolution_infra

    assert evolution_infra.write_pipeline_checkpoint(10, 9, "workers_done") is True
    assert evolution_infra.write_pipeline_checkpoint(10, 9, "direction_audited") is False
    assert evolution_infra.read_pipeline_checkpoint()["stage"] == "workers_done"


def test_fix_application_event_includes_target_version(monkeypatch):
    import fix_injection
    import system_log

    events = []
    monkeypatch.setattr(system_log, "log_system_event", lambda *event: events.append(event))

    fix_injection.log_fix_application(["BOT-001a"], [], Path("/tmp/claude_v231"), 224)

    assert events
    assert events[0][0] == "pipeline.fixes_applied"
    assert events[0][3]["target_v"] == 231


def test_git_get_parent_reads_annotated_tag_target_commit(monkeypatch):
    import evolution_infra

    def fake_git(*args, **_kwargs):
        if args[:3] == ("tag", "-l", "bot-v202"):
            return "bot-v202\n"
        if args[:3] == ("rev-list", "-n", "1"):
            return "abc123\n"
        if args[:3] == ("show", "-s", "--format=%B"):
            return "evolve: v201 -> v202\n\nparent: claude_v201\nstrategy: master\n"
        raise AssertionError(args)

    monkeypatch.setattr(evolution_infra, "_git", fake_git)

    assert evolution_infra.git_get_parent(202) == 201


def test_get_bot_info_handles_parent_and_oversized_triples(tmp_path, monkeypatch):
    import tool_status

    bot_dir = tmp_path / "claude_v202"
    bot_dir.mkdir()
    (bot_dir / "main.py").write_text("print('ok')\n")

    monkeypatch.setattr(tool_status, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
    monkeypatch.setattr(tool_status, "load_ratings", lambda: {})
    monkeypatch.setattr(tool_status, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(tool_status, "git_get_parent", lambda _v: "claude_v201")
    monkeypatch.setattr(tool_status, "check_code_size", lambda *_a, **_k: (
        2501, [("strategy.py", 2501, 2500)]
    ))

    result = asyncio.run(tool_status.get_bot_info.handler({"version": 202}))
    data = json.loads(result["content"][0]["text"])

    assert data["parent_v"] == 201
    assert data["oversized_files"] == {"strategy.py": {"lines": 2501, "limit": 2500}}
