import asyncio
import ast
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


def test_smoke_test_ignores_successful_battle_cleanup_broken_pipe(monkeypatch, tmp_path):
    import subprocess

    import code_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "main.py").write_text("print('ok')\n")
    cleanup_noise = "\n".join([
        "Smoke test passed successfully.",
        "Exception ignored while finalizing file <_io.TextIOWrapper name=6 encoding='UTF-8'>:",
        "Traceback (most recent call last):",
        '  File "/home/zzx/project/pok/web/core/engine/battle.py", line 65, in _start',
        "    self.proc = subprocess.Popen(",
        "BrokenPipeError: [Errno 32] Broken pipe",
    ])

    def _fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(_args[0], 0, stdout="Smoke test passed successfully.\n", stderr=cleanup_noise)

    monkeypatch.setattr(code_verification.subprocess, "run", _fake_run)

    assert code_verification.run_smoke_test(str(bot)) == []


def test_smoke_test_keeps_real_traceback_after_cleanup_filter(monkeypatch, tmp_path):
    import subprocess

    import code_verification

    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "main.py").write_text("print('ok')\n")
    stderr = "\n".join([
        "Exception ignored while finalizing file <_io.TextIOWrapper name=6 encoding='UTF-8'>:",
        "Traceback (most recent call last):",
        '  File "/home/zzx/project/pok/web/core/engine/battle.py", line 65, in _start',
        "    self.proc = subprocess.Popen(",
        "BrokenPipeError: [Errno 32] Broken pipe",
        "Traceback (most recent call last):",
        '  File "/tmp/bot/main.py", line 1, in <module>',
        "NameError: boom",
    ])

    def _fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            _args[0], 0, stdout="Smoke test passed successfully.\n", stderr=stderr
        )

    monkeypatch.setattr(code_verification.subprocess, "run", _fake_run)

    errors = code_verification.run_smoke_test(str(bot))
    assert errors
    assert "NameError: boom" in errors[0]


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
    assert "runtime_import" in " ".join(ckpt["gate_results"]["quality"]["failed_gates"])


def test_quality_gate_records_smoke_failure_details(monkeypatch):
    import evolution_infra
    import tool_gates

    evolution_infra.write_pipeline_checkpoint(3, 2, "workers_done")

    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda v: evolution_infra.BOTS_DIR / f"claude_v{v}")
    monkeypatch.setattr(tool_gates, "_py_files_changed_between", lambda _src, _dst: ["main.py"])
    monkeypatch.setattr(tool_gates, "verify_code", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_import_contract_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_smoke_test", lambda _bot_dir: ["smoke test emitted failure output despite exit 0: boom"])
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

    result = asyncio.run(tool_gates.run_quality_gates.handler({"version": 3, "source_v": 2}))
    data = json.loads(result["content"][0]["text"])

    assert data["smoke_ok"] is False
    assert data["smoke_errors"]
    assert data["failed_gates"] == ["smoke_test"]
    ckpt = evolution_infra.read_pipeline_checkpoint()
    quality = ckpt["gate_results"]["quality"]
    assert quality["smoke_ok"] is False
    assert quality["smoke_errors"] == data["smoke_errors"]
    assert quality["failed_gates"] == ["smoke_test"]


def test_log_system_event_is_not_reimported_inside_runtime_functions():
    """Inner imports make log_system_event a local and can crash earlier calls."""
    web_root = Path(__file__).resolve().parents[1]
    targets = [
        web_root / "core" / "orchestrator.py",
        web_root / "core" / "generation_scheduler.py",
    ]

    offenders = []
    for target in targets:
        tree = ast.parse(target.read_text(), filename=str(target))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.ImportFrom)
                    and inner.module == "system_log"
                    and any(alias.name == "log_system_event" for alias in inner.names)
                ):
                    offenders.append(f"{target.name}:{node.name}:L{inner.lineno}")

    assert offenders == []


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


def test_normalize_master_plan_paths_rewrites_only_task_path_refs():
    import tool_planning

    source_v = 224
    next_v = 232
    abs_source = tool_planning.PROJECT_ROOT / "bots" / f"claude_v{source_v}"
    plan = {
        "analysis": (
            "The source path bots/claude_v224/ is discussed here as read-only; "
            "plain claude_v224 and claude_v2244 should stay untouched."
        ),
        "source_v": source_v,
        "tasks": [{
            "target_files": ["bots/claude_v224/strategy.py", "strategy.py"],
            "worker_prompt": (
                "Edit bots/claude_v224/strategy.py, then run "
                "cd bots/claude_v224 && python -m py_compile strategy.py. "
                f"Also test sys.path.insert(0, '{abs_source}'). "
                "Do not rewrite plain claude_v224 or claude_v2244 labels."
            ),
        }],
    }

    normalized, meta = tool_planning._normalize_master_plan_paths(
        plan, source_v=source_v, next_v=next_v
    )

    task = normalized["tasks"][0]
    prompt = task["worker_prompt"]
    assert task["target_files"][0] == "bots/claude_v232/strategy.py"
    assert "bots/claude_v232/strategy.py" in prompt
    assert "cd bots/claude_v232 &&" in prompt
    assert f"'{tool_planning.PROJECT_ROOT / 'bots' / 'claude_v232'}'" in prompt
    assert "bots/claude_v224/" not in json.dumps(normalized["tasks"])
    assert "plain claude_v224" in prompt
    assert "claude_v2244" in prompt
    assert normalized["analysis"] == plan["analysis"]
    assert meta["replacements"] >= 3


def test_run_master_normalizes_parent_paths_before_audit(monkeypatch):
    import audit_agents
    import evolution_infra
    import replay_spotlight
    import tool_planning

    async def _fake_master(*_args, **_kwargs):
        return {
            "analysis": "targeted plan",
            "targeted_failure": "missed turn semi-bluff raise",
            "expected_behavior_change": "raise draws instead of folding",
            "do_not_touch": ["opponent.py"],
            "measurement_plan": "compare target to parent",
            "tasks": [{
                "worker_id": 1,
                "role": "Algorithmic Logic Architect",
                "target_files": ["bots/claude_v224/strategy.py"],
                "worker_prompt": (
                    "Modify bots/claude_v224/strategy.py and run "
                    "python -m py_compile bots/claude_v224/strategy.py"
                ),
            }],
        }

    captured_audit = {}

    async def _fake_audit(plan, _source_v, _ui, next_v=None):
        captured_audit["plan"] = plan
        captured_audit["next_v"] = next_v
        return {"overall_pass": True}

    checkpoint = {
        "next_v": 232,
        "source_v": 224,
        "stage": "direction_audited",
        "audit_attempt": 0,
        "direction_audit": {"repetition_detected": False, "llm_failed": False},
    }
    writes = []

    class _UI:
        def clear_io(self):
            pass

        def log_history(self, *_args, **_kwargs):
            pass

        def get_output(self):
            return ""

    monkeypatch.setattr(tool_planning, "_run_master_analysis", _fake_master)
    monkeypatch.setattr(tool_planning, "_matching_checkpoint", lambda *_a, **_k: checkpoint)
    monkeypatch.setattr(tool_planning, "_get_ui", lambda: _UI())
    monkeypatch.setattr(tool_planning, "_build_cross_gen_constraint_block", lambda _v: "")
    monkeypatch.setattr(tool_planning, "_extract_exhausted_keywords", lambda: [])
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *a, **k: None)
    monkeypatch.setattr(replay_spotlight, "find_critical_hands", lambda **_k: "")
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(audit_agents, "_run_master_plan_audit", _fake_audit)

    def _fake_write(_next_v, _source_v, stage, **kwargs):
        writes.append((stage, kwargs))
        return True

    monkeypatch.setattr(tool_planning, "write_pipeline_checkpoint", _fake_write)

    result = asyncio.run(tool_planning.run_master.handler({"next_v": 232, "source_v": 224}))
    data = json.loads(result["content"][0]["text"])

    audited_task = captured_audit["plan"]["tasks"][0]
    persisted_task = writes[-1][1]["master_plan"]["tasks"][0]
    returned_task = data["plan"]["tasks"][0]
    for task in (audited_task, persisted_task, returned_task):
        text = json.dumps(task)
        assert "bots/claude_v232/strategy.py" in text
        assert "bots/claude_v224/strategy.py" not in text
    assert captured_audit["next_v"] == 232


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
