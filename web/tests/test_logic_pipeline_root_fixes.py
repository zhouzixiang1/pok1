import asyncio
import ast
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _legacy_default_workflow(monkeypatch):
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "default")


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
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda **_kwargs: [])
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
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda **_kwargs: [])
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


def test_quality_gate_blocks_unreachable_new_function(monkeypatch, tmp_path):
    import evolution_infra
    import tool_gates

    source = tmp_path / "claude_v3"
    child = tmp_path / "claude_v4"
    source.mkdir()
    child.mkdir()
    (source / "postflop.py").write_text("def existing():\n    return 1\n")
    (child / "postflop.py").write_text(
        "def existing():\n    return 1\n\n"
        "def _new_helper():\n    return 2\n"
    )

    evolution_infra.write_pipeline_checkpoint(4, 3, "workers_done")

    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
    monkeypatch.setattr(tool_gates, "_py_files_changed_between", lambda _src, _dst: ["postflop.py"])
    monkeypatch.setattr(tool_gates, "verify_code", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_import_contract_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_smoke_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda **_kwargs: [])
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

    result = asyncio.run(tool_gates.run_quality_gates.handler({"version": 4, "source_v": 3}))
    data = json.loads(result["content"][0]["text"])

    assert data["reachability_ok"] is False
    assert any("reachability" in gate for gate in data["failed_gates"])
    ckpt = evolution_infra.read_pipeline_checkpoint()
    assert ckpt["stage"] == "quality_failed"
    assert ckpt["gate_results"]["quality"]["reachability_ok"] is False


def test_quality_gate_allows_unreachable_verify_helper(monkeypatch, tmp_path):
    import evolution_infra
    import tool_gates

    source = tmp_path / "claude_v6"
    child = tmp_path / "claude_v7"
    source.mkdir()
    child.mkdir()
    (source / "state.py").write_text("def existing():\n    return 1\n")
    (child / "state.py").write_text(
        "def existing():\n    return 1\n\n"
        "def _verify_preflop_shove_defense():\n"
        "    assert existing() == 1\n"
    )

    evolution_infra.write_pipeline_checkpoint(7, 6, "workers_done")

    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
    monkeypatch.setattr(tool_gates, "_py_files_changed_between", lambda _src, _dst: ["state.py"])
    monkeypatch.setattr(tool_gates, "verify_code", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_import_contract_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_smoke_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda **_kwargs: [])
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

    result = asyncio.run(tool_gates.run_quality_gates.handler({"version": 7, "source_v": 6}))
    data = json.loads(result["content"][0]["text"])

    assert data["all_passed"] is True
    assert data["reachability_ok"] is True
    assert data["reachability_warnings"] == []
    ckpt = evolution_infra.read_pipeline_checkpoint()
    assert ckpt["stage"] == "quality_passed"
    assert ckpt["gate_results"]["quality"]["reachability_ok"] is True


def test_quality_gate_reruns_when_cached_code_fingerprint_is_stale(monkeypatch, tmp_path):
    import evolution_infra
    import tool_gates
    import tool_helpers

    source = tmp_path / "claude_v4"
    child = tmp_path / "claude_v5"
    source.mkdir()
    child.mkdir()
    (source / "main.py").write_text("def act():\n    return 0\n")
    (child / "main.py").write_text("def act():\n    return 1\n")

    evolution_infra.write_pipeline_checkpoint(5, 4, "workers_done")
    tool_helpers._record_gate(
        5,
        4,
        "quality",
        {"all_passed": True, "critical_scenarios_passed": True, "code_fingerprint": "stale"},
        stage="quality_passed",
    )

    calls = {"verify": 0}

    def _verify(_bot_dir):
        calls["verify"] += 1
        return []

    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
    monkeypatch.setattr(tool_gates, "_py_files_changed_between", lambda _src, _dst: ["main.py"])
    monkeypatch.setattr(tool_gates, "verify_code", _verify)
    monkeypatch.setattr(tool_gates, "run_import_contract_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_smoke_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda **_kwargs: [])
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

    result = asyncio.run(tool_gates.run_quality_gates.handler({"version": 5, "source_v": 4}))
    data = json.loads(result["content"][0]["text"])

    assert calls["verify"] == 1
    assert data.get("idempotent_cache") is not True
    assert data["all_passed"] is True
    assert data["code_fingerprint"] != "stale"


def test_quality_gate_skips_dynamic_llm_when_heuristics_sufficient(monkeypatch, tmp_path):
    import evolution_infra
    import tool_gates
    import decision_tester

    source = tmp_path / "claude_v8"
    child = tmp_path / "claude_v9"
    source.mkdir()
    child.mkdir()
    (source / "strategy.py").write_text("def act():\n    return 0\n")
    (child / "strategy.py").write_text("def act():\n    if True:\n        return 1\n    return 0\n")

    evolution_infra.write_pipeline_checkpoint(9, 8, "workers_done")

    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
    monkeypatch.setattr(tool_gates, "_py_files_changed_between", lambda _src, _dst: ["strategy.py"])
    monkeypatch.setattr(tool_gates, "verify_code", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_import_contract_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_smoke_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda **_kwargs: [])
    monkeypatch.setattr(tool_gates, "check_code_size", lambda *_a, **_k: (10, []))
    monkeypatch.setattr(tool_gates, "verify_fixes", lambda _bot_dir: {"mandatory": {"ok": True}})
    monkeypatch.setattr(tool_gates, "DYNAMIC_TEST_HEURISTIC_SUFFICIENT", 1)
    monkeypatch.setattr(tool_gates, "DYNAMIC_TEST_LLM_ENABLED", True)

    scenario = {
        "id": "dyn_branch_unit",
        "description": "branch coverage",
        "severity": "advisory",
        "input": {"requests": [], "responses": []},
        "forbidden_actions": [],
    }
    saved = []
    monkeypatch.setattr(decision_tester, "SCENARIOS_FILE", tmp_path / "dynamic.json")
    (tmp_path / "dynamic.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(decision_tester, "generate_scenarios_from_diff", lambda *_a, **_k: [scenario])
    monkeypatch.setattr(decision_tester, "load_dynamic_scenarios", lambda: saved)
    monkeypatch.setattr(decision_tester, "save_dynamic_scenarios", lambda rows: saved.extend(rows))

    import audit_agents

    async def _should_not_generate(*_a, **_k):
        raise AssertionError("LLM dynamic test generation should be skipped")

    monkeypatch.setattr(audit_agents, "_generate_dynamic_tests", _should_not_generate)

    captured = {}

    def _decision(_bot_dir, *, extra_scenarios=None):
        captured["extra_scenarios"] = extra_scenarios
        return {
            "pass_rate": 1.0,
            "passed": 1,
            "total": 1,
            "critical_passed": 1,
            "critical_total": 1,
            "critical_failures": [],
            "failures": [],
            "scenarios": [],
        }

    monkeypatch.setattr(tool_gates, "run_decision_test_details", _decision)

    ledger_events = []

    def _append_candidate_event(event_type, **kwargs):
        ledger_events.append({"event_type": event_type, **kwargs})

    monkeypatch.setattr(tool_gates, "append_candidate_event", _append_candidate_event)

    result = asyncio.run(tool_gates.run_quality_gates.handler({"version": 9, "source_v": 8}))
    data = json.loads(result["content"][0]["text"])

    assert data["all_passed"] is True
    assert data["dynamic_test_generation"]["heuristic_count"] == 1
    assert data["dynamic_test_generation"]["llm_status"] == "skipped_heuristic_sufficient"
    assert data["dynamic_test_generation"]["combined_count"] == 1
    assert captured["extra_scenarios"] == [scenario]
    assert [e["event_type"] for e in ledger_events] == ["quality_started", "quality_finished"]
    assert ledger_events[-1]["stage"] == "quality_passed"
    assert ledger_events[-1]["scorecard"].name == "quality"


def test_quality_gate_skips_dynamic_llm_when_disabled(monkeypatch, tmp_path):
    import evolution_infra
    import tool_gates
    import decision_tester

    source = tmp_path / "claude_v18"
    child = tmp_path / "claude_v19"
    source.mkdir()
    child.mkdir()
    (source / "strategy.py").write_text("def act():\n    return 0\n")
    (child / "strategy.py").write_text("def act():\n    return 1\n")

    evolution_infra.write_pipeline_checkpoint(19, 18, "workers_done")

    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
    monkeypatch.setattr(tool_gates, "_py_files_changed_between", lambda _src, _dst: ["strategy.py"])
    monkeypatch.setattr(tool_gates, "verify_code", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_import_contract_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_smoke_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda **_kwargs: [])
    monkeypatch.setattr(tool_gates, "check_code_size", lambda *_a, **_k: (10, []))
    monkeypatch.setattr(tool_gates, "verify_fixes", lambda _bot_dir: {"mandatory": {"ok": True}})
    monkeypatch.setattr(tool_gates, "DYNAMIC_TEST_LLM_ENABLED", False)

    scenario = {
        "id": "dyn_profile_unit",
        "description": "profile coverage",
        "severity": "advisory",
        "input": {"requests": [], "responses": []},
        "forbidden_actions": [],
    }
    monkeypatch.setattr(decision_tester, "SCENARIOS_FILE", tmp_path / "dynamic_disabled.json")
    (tmp_path / "dynamic_disabled.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(decision_tester, "generate_scenarios_from_diff", lambda *_a, **_k: [scenario])
    monkeypatch.setattr(decision_tester, "load_dynamic_scenarios", lambda: [])
    monkeypatch.setattr(decision_tester, "save_dynamic_scenarios", lambda _rows: None)

    import audit_agents

    async def _should_not_generate(*_a, **_k):
        raise AssertionError("LLM dynamic test generation is disabled by default")

    monkeypatch.setattr(audit_agents, "_generate_dynamic_tests", _should_not_generate)

    captured = {}

    def _decision(_bot_dir, *, extra_scenarios=None):
        captured["extra_scenarios"] = extra_scenarios
        return {
            "pass_rate": 1.0,
            "passed": 1,
            "total": 1,
            "critical_passed": 1,
            "critical_total": 1,
            "critical_failures": [],
            "failures": [],
            "scenarios": [],
        }

    monkeypatch.setattr(tool_gates, "run_decision_test_details", _decision)
    monkeypatch.setattr(tool_gates, "append_candidate_event", lambda *_a, **_k: None)

    result = asyncio.run(tool_gates.run_quality_gates.handler({"version": 19, "source_v": 18}))
    data = json.loads(result["content"][0]["text"])

    assert data["all_passed"] is True
    assert data["dynamic_test_generation"]["llm_enabled"] is False
    assert data["dynamic_test_generation"]["llm_status"] == "skipped_disabled"
    assert data["dynamic_test_generation"]["combined_count"] == 1
    assert captured["extra_scenarios"] == [scenario]


def test_quality_gate_records_placement_shadow_review_scorecard(monkeypatch, tmp_path):
    import evolution_infra
    import tool_gates
    import code_verification

    source = tmp_path / "claude_v10"
    child = tmp_path / "claude_v11"
    source.mkdir()
    child.mkdir()
    (source / "main.py").write_text("def act():\n    return 0\n")
    (child / "main.py").write_text("def act():\n    return 1\n")

    evolution_infra.write_pipeline_checkpoint(11, 10, "workers_done")

    monkeypatch.setattr(tool_gates, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
    monkeypatch.setattr(tool_gates, "_py_files_changed_between", lambda _src, _dst: ["main.py"])
    monkeypatch.setattr(tool_gates, "verify_code", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_import_contract_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_smoke_test", lambda _bot_dir: [])
    monkeypatch.setattr(tool_gates, "run_national_protocol_tests", lambda **_kwargs: [])
    monkeypatch.setattr(tool_gates, "check_code_size", lambda *_a, **_k: (10, []))
    monkeypatch.setattr(tool_gates, "verify_fixes", lambda _bot_dir: {"mandatory": {"ok": True}})
    monkeypatch.setattr(code_verification, "detect_placement_shadow_warnings", lambda _bot_dir: [
        "strategy.py:L10: placement_shadow (review) - advisory call-site",
    ])
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
    monkeypatch.setattr(tool_gates, "append_candidate_event", None)

    result = asyncio.run(tool_gates.run_quality_gates.handler({"version": 11, "source_v": 10}))
    data = json.loads(result["content"][0]["text"])

    assert data["all_passed"] is True
    gates = {g["name"]: g for g in data["scorecard"]["gates"]}
    review_gate = gates["placement_shadow_review"]
    assert review_gate["status"] == "failed"
    assert review_gate["blocking"] is False
    assert review_gate["metrics"]["review_count"] == 1
    assert all(g["blocking"] is False for g in gates.values() if g["status"] == "failed")


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
    abs_source = tool_planning.PROJECT_ROOT / "bots" / f"national_v{source_v}"
    plan = {
        "analysis": (
            "The source path bots/national_v224/ is discussed here as read-only; "
            "plain national_v224 and national_v2244 should stay untouched."
        ),
        "source_v": source_v,
        "tasks": [{
            "target_files": ["bots/national_v224/strategy.py", "strategy.py"],
            "worker_prompt": (
                "Edit bots/national_v224/strategy.py, then run "
                "cd bots/national_v224 && python -m py_compile strategy.py. "
                f"Also test sys.path.insert(0, '{abs_source}'). "
                "Do not rewrite plain national_v224 or national_v2244 labels."
            ),
        }],
    }

    normalized, meta = tool_planning._normalize_master_plan_paths(
        plan, source_v=source_v, next_v=next_v
    )

    task = normalized["tasks"][0]
    prompt = task["worker_prompt"]
    assert task["target_files"][0] == "bots/national_v232/strategy.py"
    assert "bots/national_v232/strategy.py" in prompt
    assert "cd bots/national_v232 &&" in prompt
    assert f"'{tool_planning.PROJECT_ROOT / 'bots' / 'national_v232'}'" in prompt
    assert "bots/national_v224/" not in json.dumps(normalized["tasks"])
    assert "plain national_v224" in prompt
    assert "national_v2244" in prompt
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
                "target_files": ["bots/national_v224/strategy.py"],
                "worker_prompt": (
                    "Modify bots/national_v224/strategy.py and run "
                    "python -m py_compile bots/national_v224/strategy.py"
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
        assert "bots/national_v232/strategy.py" in text
        assert "bots/national_v224/strategy.py" not in text
    assert captured_audit["next_v"] == 232


def test_run_master_hard_validates_before_master_audit(monkeypatch):
    import audit_agents
    import evolution_infra
    import replay_spotlight
    import tool_planning

    async def _fake_master(*_args, **_kwargs):
        return {
            "analysis": "targeted plan",
            "targeted_failure": "one leak",
            "expected_behavior_change": "one changed decision",
            "do_not_touch": ["opponent.py"],
            "measurement_plan": "compare target to parent",
            "branch_from": 199,
            "tasks": [{
                "worker_id": 1,
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py"],
                "worker_prompt": "Change strategy.py in the target bot.",
            }],
        }

    async def _audit_should_not_run(*_args, **_kwargs):
        raise AssertionError("LLM master plan audit should not run after hard validation failure")

    checkpoint = {
        "next_v": 233,
        "source_v": 224,
        "stage": "direction_audited",
        "audit_attempt": 0,
        "direction_audit": {"repetition_detected": False, "llm_failed": False},
    }

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
    monkeypatch.setattr(tool_planning, "_bump_master_fail_count", lambda *_a, **_k: 1)
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *a, **k: None)
    monkeypatch.setattr(replay_spotlight, "find_critical_hands", lambda **_k: "")
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(audit_agents, "_run_master_plan_audit", _audit_should_not_run)

    result = asyncio.run(tool_planning.run_master.handler({"next_v": 233, "source_v": 224}))
    data = json.loads(result["content"][0]["text"])

    assert data["error"] == "MASTER_VALIDATION_FAILED"
    assert "branch_from" in "; ".join(data["validation_errors"])


def test_master_checkpoint_heartbeat_touches_active_stage(monkeypatch):
    import evolution_infra
    import tool_planning

    calls = []

    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: {
        "next_v": 268,
        "source_v": 250,
        "stage": "direction_audited",
        "audit_attempt": 1,
    })

    def _fake_write(next_v, source_v, stage, **kwargs):
        calls.append((next_v, source_v, stage, kwargs))
        return True

    monkeypatch.setattr(tool_planning, "write_pipeline_checkpoint", _fake_write)
    monkeypatch.setattr(tool_planning, "log_system_event", lambda *a, **k: None)

    assert tool_planning._touch_master_checkpoint(
        268,
        250,
        phase="master_plan_audit_start",
        audit_attempt=2,
        audit_context={"master_audit_rejection": {"overall_pass": False}},
    ) is True

    assert calls == [(
        268,
        250,
        "direction_audited",
        {
            "audit_attempt": 2,
            "audit_context": {"master_audit_rejection": {"overall_pass": False}},
            "touch_stage_timestamp": True,
        },
    )]


def test_illegal_stage_regression_is_not_written():
    import evolution_infra

    assert evolution_infra.write_pipeline_checkpoint(10, 9, "workers_done") is True
    assert evolution_infra.write_pipeline_checkpoint(10, 9, "direction_audited") is False
    assert evolution_infra.read_pipeline_checkpoint()["stage"] == "workers_done"


def test_master_planned_can_replan_to_direction_audited():
    import evolution_infra

    assert evolution_infra.write_pipeline_checkpoint(
        10,
        9,
        "master_planned",
        master_plan={"tasks": [{"worker_prompt": "stale"}]},
    ) is True
    assert evolution_infra.write_pipeline_checkpoint(
        10,
        9,
        "direction_audited",
        master_plan={},
        audit_attempt=1,
    ) is True
    ckpt = evolution_infra.read_pipeline_checkpoint()
    assert ckpt["stage"] == "direction_audited"
    assert ckpt["master_plan"] == {}
    assert ckpt["audit_attempt"] == 1


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
        if args[:3] == ("tag", "-l", "national-bot-v202"):
            return "national-bot-v202\n"
        if args[:3] == ("rev-list", "-n", "1"):
            return "abc123\n"
        if args[:3] == ("show", "-s", "--format=%B"):
            return "evolve: v201 -> v202\n\nparent: national_v201\nstrategy: master\n"
        raise AssertionError(args)

    monkeypatch.setattr(evolution_infra, "_git", fake_git)

    assert evolution_infra.git_get_parent(202) == 201


def test_get_bot_info_handles_parent_and_oversized_triples(tmp_path, monkeypatch):
    import tool_status

    bot_dir = tmp_path / "national_v202"
    bot_dir.mkdir()
    (bot_dir / "main.py").write_text("print('ok')\n")

    monkeypatch.setattr(tool_status, "get_bot_dir", lambda v: tmp_path / f"national_v{v}")
    monkeypatch.setattr(tool_status, "load_ratings", lambda: {})
    monkeypatch.setattr(tool_status, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(tool_status, "git_get_parent", lambda _v: "national_v201")
    monkeypatch.setattr(tool_status, "check_code_size", lambda *_a, **_k: (
        2501, [("strategy.py", 2501, 2500)]
    ))

    result = asyncio.run(tool_status.get_bot_info.handler({"version": 202}))
    data = json.loads(result["content"][0]["text"])

    assert data["parent_v"] == 201
    assert data["oversized_files"] == {"strategy.py": {"lines": 2501, "limit": 2500}}


def test_get_status_uses_abandoned_floor_for_next_v(tmp_path, monkeypatch):
    import evolution_core
    import tool_status

    (tmp_path / "national_v256").mkdir()
    monkeypatch.setattr(tool_status, "get_active_bots", lambda: [])
    monkeypatch.setattr(tool_status, "find_current_v", lambda: 254)
    monkeypatch.setattr(tool_status, "find_max_committed_v", lambda: 254)
    monkeypatch.setattr(tool_status, "find_abandoned_version_floor", lambda: 255)
    monkeypatch.setattr(
        tool_status,
        "compute_next_generation_v",
        lambda current_v, max_committed_v, abandoned_floor: max(
            current_v, max_committed_v, abandoned_floor
        ) + 1,
    )
    monkeypatch.setattr(tool_status, "get_bot_dir", lambda v: tmp_path / f"national_v{v}")
    monkeypatch.setattr(tool_status, "load_ratings", lambda: {})
    monkeypatch.setattr(tool_status, "load_daemon_stats", lambda: {"total_games": 0})
    monkeypatch.setattr(tool_status, "read_locked_json", lambda *_a, **_k: {})
    monkeypatch.setattr(tool_status, "load_strength_scores", lambda: {})
    monkeypatch.setattr(tool_status, "load_h2h_avg_winrates", lambda: {})
    monkeypatch.setattr(evolution_core, "_load_recent_failures", lambda _n: [])

    result = asyncio.run(tool_status.get_status.handler({}))
    data = json.loads(result["content"][0]["text"])

    assert data["current_v"] == 254
    assert data["abandoned_floor"] == 255
    assert data["next_v"] == 256
    assert data["incomplete_next_v"] == 256


def test_orchestrator_context_uses_abandoned_floor_for_next_v(tmp_path, monkeypatch):
    import evolution_core
    import orchestrator_context

    incomplete = tmp_path / "national_v256"
    incomplete.mkdir()
    monkeypatch.setattr(evolution_core, "get_active_bots", lambda: [])
    monkeypatch.setattr(evolution_core, "load_ratings", lambda: {})
    monkeypatch.setattr(evolution_core, "find_current_v", lambda: 254)
    monkeypatch.setattr(evolution_core, "find_max_committed_v", lambda: 254)
    monkeypatch.setattr(evolution_core, "find_abandoned_version_floor", lambda: 255)
    monkeypatch.setattr(
        evolution_core,
        "compute_next_generation_v",
        lambda current_v, max_committed_v, abandoned_floor: max(
            current_v, max_committed_v, abandoned_floor
        ) + 1,
    )
    monkeypatch.setattr(evolution_core, "get_bot_dir", lambda v: tmp_path / f"national_v{v}")
    monkeypatch.setattr(orchestrator_context, "_get_time_budget_info", lambda: "")

    text = orchestrator_context._build_context(one_gen=False, dry_run=False, gen_ctx=None)

    assert "Next generation will be: v256" in text
    assert "national_v256 directory exists but is NOT completed" in text
    assert "Next generation will be: v255" not in text


def test_cleanup_incomplete_preserves_git_tracked_dirs(tmp_path, monkeypatch):
    import tool_bot_management as tbm

    bots_dir = tmp_path / "bots"
    tracked = bots_dir / "national_v100"
    scratch = bots_dir / "national_v101"
    tracked.mkdir(parents=True)
    scratch.mkdir()
    (tracked / "main.py").write_text("x=1\n")
    (scratch / "main.py").write_text("x=1\n")

    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(tbm, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(tbm, "RESULTS_DIR", results)
    monkeypatch.setattr(tbm, "git_has_tag", lambda _v: False)
    monkeypatch.setattr(tbm, "git_dir_is_committed", lambda v: v == 100)
    monkeypatch.setattr(tbm, "log_system_event", lambda *_a, **_k: None)

    result = asyncio.run(tbm.cleanup_incomplete.handler({}))
    data = json.loads(result["content"][0]["text"])

    assert tracked.exists()
    assert not scratch.exists()
    assert data["cleaned"] == ["national_v101"]
    assert data["preserved_git_tracked"] == ["national_v100"]


def test_battle_scheduler_status_peeks_pending_claimed_completed(tmp_path, monkeypatch):
    import battle_scheduler

    jobs = tmp_path / "battle_jobs.jsonl"
    claimed = tmp_path / "battle_jobs.claimed"
    results = tmp_path / "battle_results.jsonl"
    monkeypatch.setattr(battle_scheduler, "BATTLE_JOBS_FILE", jobs)
    monkeypatch.setattr(battle_scheduler, "BATTLE_CLAIMED_FILE", claimed)
    monkeypatch.setattr(battle_scheduler, "BATTLE_RESULTS_FILE", results)

    jobs.write_text(json.dumps({"job_id": "pending"}) + "\n")
    claimed.write_text(json.dumps({"job_id": "claimed"}) + "\n")
    results.write_text(json.dumps({"job_id": "done", "total": 16}) + "\n")

    status = battle_scheduler.get_job_status(["pending", "claimed", "done", "missing"])

    assert status["pending"] == ["pending"]
    assert status["claimed"] == ["claimed"]
    assert status["completed"] == ["done"]
    assert status["missing"] == ["missing"]


def test_precommit_scheduler_job_details_include_state_age_and_matchup():
    import tool_eval
    from types import SimpleNamespace

    jobs_by_id = {
        "j1": SimpleNamespace(submitted_at=100.0, timeout_sec=960, n_pairs=8, bot_b_name="claude_v241"),
        "j2": SimpleNamespace(submitted_at=90.0, timeout_sec=960, n_pairs=8, bot_b_name="claude_v237"),
    }
    job_id_to_opponent = {
        "j1": {"name": "claude_v241", "reason": "parent"},
        "j2": {"name": "claude_v237", "reason": "top_strength"},
    }

    details = tool_eval._precommit_scheduler_job_details(
        ["j1", "j2"],
        job_id_to_opponent,
        jobs_by_id,
        {"claimed": ["j1"], "completed": ["j2"], "pending": [], "missing": []},
        {"j2": {"wins_a": 10, "wins_b": 6, "draws": 0, "total": 16, "error": None}},
        now=115.0,
    )

    assert details[0] == {
        "job_id": "j1",
        "opponent": "claude_v241",
        "reason": "parent",
        "state": "claimed",
        "age_sec": 15.0,
        "timeout_sec": 960,
        "n_games": 8,
    }
    assert details[1]["opponent"] == "claude_v237"
    assert details[1]["state"] == "collected"
    assert details[1]["age_sec"] == 25.0
    assert details[1]["wins"] == 10
    assert details[1]["losses"] == 6
    assert details[1]["total"] == 16


def test_scheduler_stall_reason_treats_claimed_jobs_as_running():
    import tool_eval

    claimed_rounds = tool_eval._claimed_job_stall_rounds(
        n_games=8,
        poll_interval=5.0,
        per_game_timeout=960,
        poll_budget=1500,
    )
    assert claimed_rounds > 120
    assert tool_eval._scheduler_stall_reason(
        collected_count=0,
        submitted_count=4,
        rounds_since_progress=24,
        pending_stall_rounds=0,
        missing_stall_rounds=0,
        pending_count=0,
        claimed_count=4,
        completed_count=0,
        scheduler_stall_rounds=24,
        claimed_job_stall_rounds=claimed_rounds,
    ) == ""
    assert tool_eval._scheduler_stall_reason(
        collected_count=0,
        submitted_count=4,
        rounds_since_progress=claimed_rounds,
        pending_stall_rounds=0,
        missing_stall_rounds=0,
        pending_count=0,
        claimed_count=4,
        completed_count=0,
        scheduler_stall_rounds=24,
        claimed_job_stall_rounds=claimed_rounds,
    ) == "claimed_jobs_exceeded_grace"
    assert tool_eval._scheduler_stall_reason(
        collected_count=0,
        submitted_count=4,
        rounds_since_progress=24,
        pending_stall_rounds=24,
        missing_stall_rounds=0,
        pending_count=4,
        claimed_count=0,
        completed_count=0,
        scheduler_stall_rounds=24,
        claimed_job_stall_rounds=120,
    ) == "jobs_never_claimed"


def test_subagent_guard_allows_readonly_parent_probe_but_blocks_writes():
    import evolution_infra
    import llm_query

    project_root = evolution_infra.PROJECT_ROOT
    allowed = str(project_root / "bots" / "claude_v234")
    readonly_ls = "ls -d bots/claude_v224 bots/claude_v206 2>&1"
    readonly_python = (
        "python -c \"from pathlib import Path; "
        "print(Path('bots/claude_v221/strategy.py').read_text()[:10])\""
    )
    readonly_python_assignment = """python -c "
import json
h2h = json.load(open('web/core/results/head_to_head.json'))
for opp, wr in [('claude_v206', 0.44)]:
    if wr > 0.10:
        print(opp)
"
"""
    readonly_python_heredoc = """python3 << 'PYEOF'
import json
h2h = json.load(open('web/core/results/head_to_head.json'))
if 0.55 > 0.10:
    print('readonly')
PYEOF
"""
    readonly_wc = "wc -l web/core/experience_pool.md 2>/dev/null"
    readonly_tag = "git tag -l 'bot-v2*' | tail -10"
    readonly_git_status = "git status --short --branch && git diff -- bots/claude_v224/main.py"
    readonly_git_log = "git -C . log --oneline -5 && git show --stat HEAD && git rev-parse HEAD && git ls-files bots/claude_v224"
    readonly_dispatch_probe = """# Verify dispatch is in opponent_allin block
grep -n "_allin_polarized_equity_fold" bots/claude_v260/strategy.py"""
    write_redirect = "echo x > bots/claude_v224/strategy.py"
    mixed_allowed_and_protected_write = (
        "python -c \"from pathlib import Path; "
        "Path('bots/claude_v234/notes.txt').write_text('ok'); "
        "Path('web/core/tool_gates.py').write_text('bad')\""
    )
    write_heredoc_redirect = """python3 << 'PYEOF' > bots/claude_v224/notes.txt
print('x')
PYEOF
"""
    write_python = (
        "python -c \"from pathlib import Path; "
        "Path('bots/claude_v221/strategy.py').write_text('x')\""
    )
    write_tag = "git tag bot-v999"
    write_git_reset = "git reset --hard HEAD"
    write_git_switch = "git switch main"
    write_git_clean = "git clean -fd"
    write_git_merge = "git merge feature"
    write_git_rebase = "git rebase main"
    write_patch = "patch -p1 < /tmp/change.patch"
    copy_parent_into_allowed = (
        "mkdir -p bots/claude_v234 && "
        "cp bots/claude_v240/*.py bots/claude_v234/ && "
        "ls bots/claude_v234/ 2>&1"
    )
    copy_parent_multiline_with_comment = """mkdir -p bots/claude_v234
# Copy parent code into the allowed child bot directory.
cp bots/claude_v240/*.py bots/claude_v234/
ls bots/claude_v234/
wc -l bots/claude_v234/strategy.py
"""
    copy_parent_files_into_allowed = (
        "cp bots/claude_v240/main.py bots/claude_v240/strategy.py "
        "bots/claude_v234/"
    )
    copy_into_other_bot = "cp bots/claude_v240/*.py bots/claude_v235/"
    mkdir_other_bot = "mkdir -p bots/claude_v235"
    tee_protected = "printf x | tee web/core/tmp.txt"
    redirect_allowed = "echo x > bots/claude_v234/notes.txt"
    rm_allowed = "rm bots/claude_v234/tmp.py"
    rm_relative_bare = "rm -rf __pycache__"
    rm_relative_after_cd = "cd bots/claude_v234 && rm -rf __pycache__ && python -B -c \"import strategy\" 2>&1"
    rm_relative_after_grouped_cd = (
        "(cd bots/claude_v234 && rm -rf __pycache__) && echo cleaned && ls bots/claude_v234/"
    )
    rm_allowed_and_parent_pycache = (
        "rm -rf bots/claude_v234/__pycache__ bots/claude_v240/__pycache__ 2>&1"
    )
    redirect_relative_after_cd = "cd bots/claude_v234 && echo x > notes.txt"
    rm_relative_other_bot_after_cd = "cd bots/claude_v240 && rm -rf __pycache__"
    rm_relative_other_bot_after_grouped_cd = (
        "(cd bots/claude_v234 && rm -rf ../claude_v240/__pycache__) && echo cleaned"
    )
    rm_other_bot = "rm bots/claude_v240/tmp.py"

    assert llm_query._subagent_is_outside_allowed(readonly_ls, allowed) is True
    assert llm_query._subagent_bash_is_mutation(readonly_ls) is False
    assert llm_query._subagent_bash_is_mutation(readonly_python) is False
    assert llm_query._subagent_bash_is_mutation(readonly_python_assignment) is False
    assert llm_query._subagent_bash_is_mutation(readonly_python_heredoc) is False
    assert llm_query._subagent_bash_is_mutation(readonly_wc) is False
    assert llm_query._subagent_bash_is_mutation(readonly_tag) is False
    assert llm_query._subagent_bash_is_mutation(readonly_git_status) is False
    assert llm_query._subagent_bash_is_mutation(readonly_git_log) is False
    assert llm_query._subagent_bash_is_mutation(readonly_dispatch_probe) is False
    assert llm_query._subagent_bash_mutation_detector(readonly_ls) is None
    assert llm_query._subagent_bash_mutation_detector(readonly_python) is None
    assert llm_query._subagent_bash_mutation_detector(readonly_python_assignment) is None
    assert llm_query._subagent_bash_mutation_detector(readonly_python_heredoc) is None
    assert llm_query._subagent_bash_mutation_detector(readonly_tag) is None
    assert llm_query._subagent_bash_mutation_detector(readonly_git_status) is None
    assert llm_query._subagent_bash_mutation_detector(readonly_git_log) is None
    assert llm_query._subagent_bash_mutation_detector(readonly_dispatch_probe) is None
    assert llm_query._subagent_bash_is_mutation(write_redirect) is True
    assert llm_query._subagent_bash_is_mutation(mixed_allowed_and_protected_write) is True
    assert llm_query._subagent_is_outside_allowed(mixed_allowed_and_protected_write, allowed) is True
    assert llm_query._subagent_bash_is_mutation(write_heredoc_redirect) is True
    assert llm_query._subagent_bash_is_mutation(write_python) is True
    assert llm_query._subagent_bash_is_mutation(write_tag) is True
    assert llm_query._subagent_bash_is_mutation(write_git_reset) is True
    assert llm_query._subagent_bash_is_mutation(write_git_switch) is True
    assert llm_query._subagent_bash_is_mutation(write_git_clean) is True
    assert llm_query._subagent_bash_is_mutation(write_git_merge) is True
    assert llm_query._subagent_bash_is_mutation(write_git_rebase) is True
    assert llm_query._subagent_bash_is_mutation(write_patch) is True
    assert llm_query._subagent_bash_mutation_detector(write_redirect).startswith("write_redirect:")
    assert llm_query._subagent_bash_mutation_detector(write_heredoc_redirect).startswith("write_redirect:")
    assert llm_query._subagent_bash_mutation_detector(write_python) == "python_write_pattern:.write_text("
    assert llm_query._subagent_bash_mutation_detector(write_tag) == "git_tag_mutation"
    assert llm_query._subagent_bash_mutation_detector(write_git_reset) == "git_command:reset"
    assert llm_query._subagent_bash_mutation_detector(write_patch) == "bash_pattern:patch"
    assert llm_query._subagent_bash_is_mutation(copy_parent_into_allowed) is True
    assert llm_query._subagent_bash_write_scope_violation(copy_parent_into_allowed, allowed) is None
    assert llm_query._subagent_bash_write_scope_violation(copy_parent_multiline_with_comment, allowed) is None
    assert llm_query._subagent_bash_write_scope_violation(copy_parent_files_into_allowed, allowed) is None
    assert llm_query._subagent_bash_write_scope_violation(redirect_allowed, allowed) is None
    assert llm_query._subagent_bash_write_scope_violation(rm_allowed, allowed) is None
    assert llm_query._subagent_bash_write_scope_violation(rm_relative_after_cd, allowed) is None
    assert llm_query._subagent_bash_write_scope_violation(rm_relative_after_grouped_cd, allowed) is None
    assert (
        llm_query._subagent_bash_write_scope_violation(
            rm_allowed_and_parent_pycache,
            allowed,
        )
        == "rm:bots/claude_v240/__pycache__"
    )
    assert llm_query._subagent_bash_write_scope_violation(redirect_relative_after_cd, allowed) is None
    assert llm_query._subagent_bash_write_scope_violation(rm_relative_bare, allowed).startswith("rm:")
    assert llm_query._subagent_bash_write_scope_violation(
        rm_relative_other_bot_after_cd, allowed
    ).startswith("rm:")
    assert llm_query._subagent_bash_write_scope_violation(
        rm_relative_other_bot_after_grouped_cd, allowed
    ).startswith("rm:")
    scoped_strategy = project_root / "bots" / "claude_v234" / "strategy.py"
    file_scope = {"files": [str(scoped_strategy)]}
    python_open_allowed = (
        f"python3 -c \"lines = open('{scoped_strategy}').readlines()\\n"
        f"with open('{scoped_strategy}', 'w') as f:\\n"
        "    f.writelines(lines)\""
    )
    python_open_relative_allowed = (
        "cd bots/claude_v234 && "
        "python3 -c \"with open('strategy.py', 'w') as f: f.write('x')\""
    )
    python_open_other_file = (
        f"python3 -c \"with open('{project_root / 'bots' / 'claude_v234' / 'opponent.py'}', 'w') as f: "
        "f.write('x')\""
    )
    python_path_heredoc_allowed = (
        f"python3 - <<'PY'\n"
        f"from pathlib import Path\n"
        f"Path('{scoped_strategy}').write_text('x')\n"
        f"PY"
    )
    python_path_heredoc_relative_allowed = (
        "cd bots/claude_v234 && python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "Path('strategy.py').write_text('x')\n"
        "PY"
    )
    python_path_var_heredoc_relative_allowed = (
        "cd bots/claude_v234 && python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "p = Path('strategy.py')\n"
        "p.write_text('x')\n"
        "PY"
    )
    python_path_heredoc_other_file = (
        "cd bots/claude_v234 && python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "Path('opponent.py').write_text('x')\n"
        "PY"
    )
    python_path_var_heredoc_other_file = (
        "cd bots/claude_v234 && python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "p = Path('opponent.py')\n"
        "p.write_text('x')\n"
        "PY"
    )
    national_new_helper = project_root / "bots" / "national_v114" / "river_thin_value.py"
    national_strategy = project_root / "bots" / "national_v114" / "strategy.py"
    national_file_scope = {"files": [str(national_new_helper), str(national_strategy)]}
    python_path_heredoc_multi_target_allowed = (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "Path('bots/national_v114/river_thin_value.py').write_text('helper')\n"
        "Path('bots/national_v114/strategy.py').write_text('strategy')\n"
        "PY"
    )
    assert llm_query._subagent_is_outside_allowed(
        str(scoped_strategy),
        file_scope,
    ) is False
    assert llm_query._subagent_is_outside_allowed(
        "file:" + str(scoped_strategy),
        file_scope,
    ) is False
    assert llm_query._subagent_is_outside_allowed(
        scoped_strategy.as_uri(),
        file_scope,
    ) is False
    assert llm_query._subagent_bash_write_scope_violation(
        "sed -i 's/a/b/' bots/claude_v234/strategy.py",
        file_scope,
    ) is None
    assert llm_query._subagent_bash_write_scope_violation(
        "sed -i 's/a/b/' file:" + str(scoped_strategy),
        file_scope,
    ) is None
    assert llm_query._subagent_bash_write_scope_violation(
        "cd bots/claude_v234 && sed -i 's/a/b/' strategy.py",
        file_scope,
    ) is None
    assert llm_query._subagent_bash_write_scope_violation(
        python_open_allowed,
        file_scope,
    ) is None
    assert llm_query._subagent_bash_write_scope_violation(
        python_open_relative_allowed,
        file_scope,
    ) is None
    assert llm_query._subagent_bash_write_scope_violation(
        python_path_heredoc_allowed,
        file_scope,
    ) is None
    assert llm_query._subagent_bash_write_scope_violation(
        python_path_heredoc_relative_allowed,
        file_scope,
    ) is None
    assert llm_query._subagent_bash_write_scope_violation(
        python_path_var_heredoc_relative_allowed,
        file_scope,
    ) is None
    assert llm_query._subagent_bash_write_scope_violation(
        python_open_other_file,
        file_scope,
    ).startswith("python_open_write:")
    assert llm_query._subagent_bash_write_scope_violation(
        python_path_heredoc_other_file,
        file_scope,
    ).startswith("python_path_write_text:")
    assert llm_query._subagent_bash_write_scope_violation(
        python_path_var_heredoc_other_file,
        file_scope,
    ).startswith("python_path_write_text:")
    assert llm_query._subagent_bash_write_scope_violation(
        python_path_heredoc_multi_target_allowed,
        national_file_scope,
    ) is None
    assert llm_query._subagent_bash_write_scope_violation(
        "echo x > bots/claude_v234/notes.txt",
        file_scope,
    ).startswith("write_redirect:")
    assert llm_query._subagent_bash_write_scope_violation(
        "cd bots/claude_v234 && echo x > notes.txt",
        file_scope,
    ).startswith("write_redirect:")
    assert llm_query._subagent_is_outside_allowed(
        "Path('bots/claude_v234/notes.txt').write_text('x')",
        file_scope,
    ) is True
    assert llm_query._subagent_is_outside_allowed(
        "file:" + str(project_root / "bots" / "claude_v235" / "strategy.py"),
        file_scope,
    ) is True
    assert llm_query._subagent_bash_write_scope_violation(copy_into_other_bot, allowed).startswith("cp_dest:")
    assert llm_query._subagent_bash_write_scope_violation(mkdir_other_bot, allowed).startswith("mkdir:")
    assert llm_query._subagent_bash_write_scope_violation(tee_protected, allowed).startswith("tee:")
    assert llm_query._subagent_bash_write_scope_violation(rm_other_bot, allowed).startswith("rm:")
    assert llm_query._subagent_bash_write_scope_violation(write_git_reset, allowed) == "git_command:reset"
    assert llm_query._subagent_readonly_mutation_violation(
        "Bash", {"command": readonly_git_status}
    ) is None
    assert llm_query._subagent_readonly_mutation_violation(
        "Bash", {"command": copy_parent_into_allowed}
    ) == "bash_pattern:mkdir"
    assert llm_query._subagent_readonly_mutation_violation(
        "Bash", {"command": write_redirect}
    ).startswith("write_redirect:")
    assert llm_query._subagent_readonly_mutation_violation(
        "Edit", {"file_path": "bots/claude_v234/main.py"}
    ) == "Edit_not_allowed"


def test_subagent_write_guard_uses_structured_scope_for_decisions():
    """The hook's human-readable scope label must not be used as a path."""
    import asyncio
    import evolution_infra
    import llm_query

    project_root = evolution_infra.PROJECT_ROOT

    async def _run():
        hooks = llm_query._make_subagent_write_guard(
            str(project_root / "bots" / "claude_v266")
        )
        handler = hooks["PreToolUse"][0].hooks[0]
        allowed = await handler({
            "tool_name": "Bash",
            "tool_input": {
                "command": (
                    "ls bots/ | grep claude_v266 || echo missing\n"
                    "mkdir -p bots/claude_v266\n"
                    "cp bots/claude_v209/*.py bots/claude_v266/\n"
                    "ls bots/claude_v266/"
                ),
            },
        }, "tool-use-1", {})
        denied = await handler({
            "tool_name": "Bash",
            "tool_input": {"command": "mkdir -p bots/claude_v267"},
        }, "tool-use-2", {})
        edit_allowed = await handler({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(project_root / "bots" / "claude_v266" / "strategy.py")
            },
        }, "tool-use-3", {})
        edit_file_uri_allowed = await handler({
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "file:" + str(project_root / "bots" / "claude_v266" / "strategy.py")
            },
        }, "tool-use-4", {})
        file_scope_hooks = llm_query._make_subagent_write_guard({
            "files": [str(project_root / "bots" / "claude_v266" / "strategy.py")]
        })
        file_scope_handler = file_scope_hooks["PreToolUse"][0].hooks[0]
        denied_cache_cleanup = await file_scope_handler({
            "tool_name": "Bash",
            "tool_input": {
                "command": "rm -rf bots/claude_v266/__pycache__ && diff -rq bots/claude_v265 bots/claude_v266"
            },
        }, "tool-use-5", {})
        return allowed, denied, edit_allowed, edit_file_uri_allowed, denied_cache_cleanup

    allowed, denied, edit_allowed, edit_file_uri_allowed, denied_cache_cleanup = asyncio.run(_run())
    assert allowed == {}
    assert edit_allowed == {}
    assert edit_file_uri_allowed == {}
    decision = denied["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "bots/claude_v267" in decision["permissionDecisionReason"]
    cache_decision = denied_cache_cleanup["hookSpecificOutput"]
    assert cache_decision["permissionDecision"] == "deny"
    assert "Do not delete `__pycache__`" in cache_decision["permissionDecisionReason"]
    assert "diff --exclude=__pycache__" in cache_decision["permissionDecisionReason"]
    assert "only delete caches inside" not in cache_decision["permissionDecisionReason"]


def test_commit_bot_blocks_missing_code_fingerprints(tmp_path, monkeypatch):
    import tool_commit

    bot_dir = tmp_path / "bots" / "claude_v444"
    bot_dir.mkdir(parents=True)
    (bot_dir / "main.py").write_text("# candidate\n")
    ckpt = {
        "next_v": 444,
        "source_v": 443,
        "stage": "verified",
        "gate_results": {
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
            "review": {"approved": True},
            "critic": {"approved": True, "score": 7},
            "precommit_eval": {"passed": True},
        },
    }
    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _v: bot_dir)
    monkeypatch.setattr(tool_commit, "_matching_checkpoint", lambda _v, _source_v: ckpt)
    monkeypatch.setattr(tool_commit, "log_system_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        tool_commit,
        "git_commit_bot",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("git_commit_bot must not run")),
    )

    result = asyncio.run(tool_commit.commit_bot.handler({
        "version": 444,
        "source_v": 443,
        "strategy": "test",
        "review_approved": True,
    }))
    data = json.loads(result["content"][0]["text"])

    assert data["error"].startswith("COMMIT BLOCKED")
    assert "quality_code_fingerprint" in data["missing_gates"]
    assert "precommit_code_fingerprint" in data["missing_gates"]


def test_orchestrator_guard_allows_readonly_redirection_but_blocks_writes():
    import orchestrator_context

    readonly = (
        "git status --short --branch | head -30 && echo \"---\" && "
        "ls -d bots/claude_v221 bots/claude_v206 2>&1"
    )
    readonly_tag_probe = (
        "git status --short --branch | head -20 && echo \"---TAGS---\" && "
        "git tag -l 'bot-v2*' | tail -10 && echo \"---PARENT DIRS---\" && "
        "ls -d bots/claude_v206 bots/claude_v221 bots/claude_v235 2>&1"
    )
    readonly_python = "python -c \"print(open('bots/claude_v221/main.py').read()[:10])\""
    readonly_python_comparison = """python3 << 'PYEOF'
for wr in [0.20, 0.55]:
    if wr > 0.10:
        print(wr)
PYEOF
"""
    readonly_pipeline_tmp_extract = """cat web/core/results/pipeline_state.json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
plan = d.get('master_plan') or d.get('plan') or {}
print(json.dumps(plan, default=str))
" > /tmp/v242_plan.json 2>/dev/null
wc -c /tmp/v242_plan.json"""
    readonly_worker_probe = """diff -rq bots/claude_v246/ bots/claude_v247/ --exclude='__pycache__' --exclude='.completed' 2>/dev/null
echo "=== line counts strategy.py / postflop.py ==="
wc -l bots/claude_v247/strategy.py bots/claude_v247/postflop.py bots/claude_v247/reachability_test.py 2>/dev/null
echo "=== confirm new fn present, old fn gone (v247) ==="
grep -n "_paired_board_marginal_allin_fold\\|_marginal_made_river_fold_gate" bots/claude_v247/postflop.py bots/claude_v247/strategy.py 2>/dev/null | head -40"""
    readonly_functional_probe = """echo "=== Confirm only strategy.py differs (functional) ==="
diff bots/claude_v246/strategy.py bots/claude_v247/strategy.py | grep -E '^[<>]' | grep -vE '^[<>][[:space:]]*#' | grep -vE '^[<>][[:space:]]*$'
echo "=== (empty above = zero functional changes) ==="
ls -la bots/claude_v246/reachability_test.py bots/claude_v247/reachability_test.py 2>&1"""
    write_redirect = "echo x > bots/claude_v221/main.py"
    write_heredoc_redirect = """python3 << 'PYEOF' > bots/claude_v221/tmp.txt
print('x')
PYEOF
"""
    write_python = (
        "python -c \"from pathlib import Path; "
        "Path('bots/claude_v221/main.py').write_text('x')\""
    )

    assert orchestrator_context._orchestrator_bash_is_mutation(readonly) is False
    assert orchestrator_context._orchestrator_bash_is_mutation(readonly_tag_probe) is False
    assert orchestrator_context._orchestrator_bash_is_mutation("git tag --list 'bot-v23*' | sort -V") is False
    assert orchestrator_context._orchestrator_bash_is_mutation("git tag --sort=-creatordate | head -5") is False
    assert orchestrator_context._orchestrator_bash_is_mutation(readonly_python) is False
    assert orchestrator_context._orchestrator_bash_is_mutation(readonly_python_comparison) is False
    assert orchestrator_context._orchestrator_bash_is_mutation(readonly_pipeline_tmp_extract) is False
    assert orchestrator_context._orchestrator_bash_is_mutation(readonly_worker_probe) is False
    assert orchestrator_context._orchestrator_bash_is_mutation(readonly_functional_probe) is False
    assert orchestrator_context._orchestrator_bash_is_mutation(write_redirect) is True
    assert orchestrator_context._orchestrator_bash_is_mutation(write_heredoc_redirect) is True
    assert orchestrator_context._orchestrator_bash_is_mutation(write_python) is True
    assert orchestrator_context._orchestrator_bash_is_mutation("echo confirm; rm bots/claude_v221/main.py") is True
    assert orchestrator_context._orchestrator_bash_is_mutation("git tag bot-v999") is True
    assert orchestrator_context._orchestrator_bash_is_mutation("git tag -a bot-v999 -m x") is True


def test_orchestrator_prompt_delegates_code_change_check_to_quality_gate():
    prompt = (Path(__file__).resolve().parents[1] / "core/prompts/orchestrator.md").read_text()

    assert "run_quality_gates` owns the byte-for-byte" in prompt
    assert "blocking `code_changed` gate" in prompt
    assert "diff -rq bots/claude_v{source_v}/" not in prompt


def test_post_generation_fingerprint_uses_committed_next_v(monkeypatch):
    import behavior_diversity
    import generation_scheduler

    saved = []
    events = []

    def fake_compute(bot_name):
        return ("fingerprint-for", bot_name)

    monkeypatch.setattr(behavior_diversity, "compute_decision_fingerprint", fake_compute)
    monkeypatch.setattr(
        behavior_diversity,
        "save_fingerprint",
        lambda bot_name, fp: saved.append((bot_name, fp)),
    )
    monkeypatch.setattr(
        generation_scheduler,
        "log_system_event",
        lambda *args: events.append(args),
    )

    bot_name = generation_scheduler._save_committed_bot_fingerprint(237)

    assert bot_name == "national_v237"
    assert saved == [("national_v237", ("fingerprint-for", "national_v237"))]
    assert events
    assert events[0][0] == "pipeline.fingerprint_saved"
    assert events[0][3]["version"] == 237
    assert events[0][3]["bot"] == "national_v237"


def test_archivist_housekeeping_commit_stages_only_curated_paths(monkeypatch):
    import tool_commit

    calls = []
    staged_after_add = []

    def fake_git(*args, **_kwargs):
        calls.append(args)
        if args[:3] == ("diff", "--cached", "--name-only") and "--" not in args:
            return "\n".join(staged_after_add) + ("\n" if staged_after_add else "")
        if args[:3] == ("status", "--porcelain", "--"):
            path = args[-1]
            if path == "web/core/experience_pool.md":
                return " M web/core/experience_pool.md\n"
            if path == "bots/national_v204":
                return " D bots/national_v204/main.py\n"
            return ""
        if args[:3] == ("diff", "--cached", "--name-only") and args[-1] == "web/core/experience_pool.md":
            return "web/core/experience_pool.md\n"
        if args[:3] == ("diff", "--cached", "--name-only") and args[-1] == "bots/national_v204":
            return "bots/national_v204/main.py\n"
        if args[:1] == ("add",):
            if args[-1] == "web/core/experience_pool.md":
                staged_after_add.append("web/core/experience_pool.md")
            if args[-1] == "bots/national_v204":
                staged_after_add.append("bots/national_v204/main.py")
            return ""
        if args[:1] == ("commit",):
            return ""
        if args[:3] == ("rev-parse", "--short", "HEAD"):
            return "abc123\n"
        raise AssertionError(args)

    monkeypatch.setattr(tool_commit, "_git", fake_git)
    monkeypatch.setattr(tool_commit, "_git_ensure_main_branch", lambda: None)
    monkeypatch.setattr(tool_commit, "log_system_event", lambda *_a, **_k: None)

    result = tool_commit._archive_housekeeping_commit(
        234,
        {"reaped": True, "culled": "national_v204"},
        experience_touched=True,
        preexisting_dirty=set(),
    )

    assert result["committed"] is True
    assert result["staged_files"] == [
        "web/core/experience_pool.md",
        "bots/national_v204/main.py",
    ]
    assert ("add", "--", "web/core/experience_pool.md") in calls
    assert ("add", "-u", "--", "bots/national_v204") in calls
    assert any(call[:3] == ("commit", "-m", "chore: archive v234 evolution housekeeping") for call in calls)
    assert any(
        call[-3:] == ("--", "web/core/experience_pool.md", "bots/national_v204/main.py")
        for call in calls
    )
    assert not any(call[:2] == ("add", "-A") for call in calls)


def test_archivist_housekeeping_skips_if_unexpected_files_are_staged(monkeypatch):
    import tool_commit

    calls = []
    staged_after_add = []

    def fake_git(*args, **_kwargs):
        calls.append(args)
        if args[:3] == ("diff", "--cached", "--name-only") and "--" not in args:
            if not staged_after_add:
                return ""
            return "web/core/experience_pool.md\nunrelated.py\n"
        if args[:3] == ("status", "--porcelain", "--"):
            return " M web/core/experience_pool.md\n" if args[-1] == "web/core/experience_pool.md" else ""
        if args[:3] == ("diff", "--cached", "--name-only") and args[-1] == "web/core/experience_pool.md":
            return "web/core/experience_pool.md\n"
        if args[:1] == ("add",):
            staged_after_add.append(args[-1])
            return ""
        if args[:1] == ("restore",):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(tool_commit, "_git", fake_git)
    monkeypatch.setattr(tool_commit, "_git_ensure_main_branch", lambda: None)
    monkeypatch.setattr(tool_commit, "log_system_event", lambda *_a, **_k: None)

    result = tool_commit._archive_housekeeping_commit(
        235,
        None,
        experience_touched=True,
        preexisting_dirty=set(),
    )

    assert result["committed"] is False
    assert result["reason"] == "unexpected_staged_files"
    assert result["unexpected_staged"] == ["unrelated.py"]
    assert ("restore", "--staged", "--", "web/core/experience_pool.md") in calls
    assert not any(call[:1] == ("commit",) for call in calls)


def test_git_push_refs_reports_failure(monkeypatch):
    import evolution_infra

    calls = []

    def fake_git(*args, **_kwargs):
        calls.append(args)
        if args == ("push", "origin", "main"):
            raise RuntimeError("push failed")
        return ""

    monkeypatch.setattr(evolution_infra, "_git", fake_git)

    assert evolution_infra.git_push_refs("main", "national-bot-v999") is False
    assert ("push", "origin", "main") in calls
    assert ("push", "origin", "national-bot-v999") in calls


def test_git_push_refs_reconciles_unrelated_remote_main(monkeypatch):
    import evaluation_contract
    import evolution_infra

    calls = []
    first_main_push = {"done": False}

    def fake_git(*args, **_kwargs):
        calls.append(args)
        if args == ("push", "origin", "main") and not first_main_push["done"]:
            first_main_push["done"] = True
            raise RuntimeError("fetch first")
        if args[:3] == ("push", "origin", "main"):
            return ""
        if args[:3] == ("push", "origin", "national-bot-v999"):
            return ""
        if args == ("fetch", "origin", "--prune", "--tags"):
            return ""
        if args == ("rev-list", "--left-right", "--count", "HEAD...origin/main"):
            return "1\t1\n"
        if args == ("merge-base", "HEAD", "origin/main"):
            return "base123\n"
        if args == ("rev-parse", "--short=12", "origin/main"):
            return "remote456\n"
        if args == ("rev-parse", "--short=12", "HEAD"):
            return "local123\n"
        if args[:3] == ("merge", "--no-ff", "origin/main"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: {"next_v": 999, "source_v": 998})
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args: ["docs/notes.md", "bots/neural_national_lab/data/run.json"],
    )

    assert evolution_infra.git_push_refs("main", "national-bot-v999") is True
    assert any(call[:3] == ("merge", "--no-ff", "origin/main") for call in calls)
    assert calls.count(("push", "origin", "main")) == 2


def test_git_push_refs_blocks_remote_contract_change(monkeypatch):
    import evaluation_contract
    import evolution_infra

    calls = []

    def fake_git(*args, **_kwargs):
        calls.append(args)
        if args == ("push", "origin", "main"):
            raise RuntimeError("fetch first")
        if args == ("push", "origin", "national-bot-v999"):
            return ""
        if args == ("fetch", "origin", "--prune", "--tags"):
            return ""
        if args == ("rev-list", "--left-right", "--count", "HEAD...origin/main"):
            return "1\t1\n"
        if args == ("merge-base", "HEAD", "origin/main"):
            return "base123\n"
        if args == ("rev-parse", "--short=12", "origin/main"):
            return "remote456\n"
        if args == ("rev-parse", "--short=12", "HEAD"):
            return "local123\n"
        raise AssertionError(args)

    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: {"next_v": 999, "source_v": 998})
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args: ["web/core/orchestrator.py"],
    )

    assert evolution_infra.git_push_refs("main", "national-bot-v999") is False
    assert not any(call[:1] == ("merge",) for call in calls)


def test_git_commit_bot_refuses_preexisting_blocking_staged_files(monkeypatch):
    import bot_artifact
    import evolution_infra

    calls = []

    def fake_git(*args, **_kwargs):
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main\n"
        if args == ("diff", "--cached", "--name-only"):
            return "web/core/tool_gates.py\n"
        raise AssertionError(args)

    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    monkeypatch.setattr(bot_artifact, "hash_path", lambda _path: "candidate-hash")
    monkeypatch.setattr(evolution_infra, "_require_national_epoch_registry_for_commit", lambda: None)

    with __import__("pytest").raises(RuntimeError, match="pre-existing blocking staged"):
        evolution_infra.git_commit_bot(
            999,
            998,
            "test",
            official_certificate={
                "certificate_digest": "cert-digest",
                "candidate_hash": "candidate-hash",
                "policy_id": "official-full-v2",
            },
        )

    assert not any(call[:1] == ("add",) for call in calls)
    assert not any(call[:1] == ("commit",) for call in calls)


def test_git_commit_bot_preserves_unrelated_staged_files(monkeypatch):
    import bot_artifact
    import evolution_infra

    calls = []
    staged = ["docs/user-notes.md"]

    def fake_git(*args, **_kwargs):
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main\n"
        if args == ("diff", "--cached", "--name-only"):
            return "\n".join(staged) + ("\n" if staged else "")
        if args == ("add", "--", "bots/national_v999"):
            staged.append("bots/national_v999/main.py")
            return ""
        if args[:2] == ("commit", "-m"):
            assert args[-2:] == ("--", "bots/national_v999")
            return ""
        if args == ("rev-parse", "HEAD"):
            return "abc123456789\n"
        if args == ("rev-parse", "--short=12", "HEAD"):
            return "abc123456789\n"
        if args == ("tag", "-d", "national-bot-v999"):
            return ""
        if args[:3] == ("tag", "national-bot-v999", "-m"):
            assert "official-certificate: cert-digest" in args[3]
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    monkeypatch.setattr(bot_artifact, "hash_path", lambda _path: "candidate-hash")
    monkeypatch.setattr(evolution_infra, "_require_national_epoch_registry_for_commit", lambda: None)
    monkeypatch.setattr(
        evolution_infra,
        "_advance_national_epoch_high_water",
        lambda _version: type("Mutation", (), {"created_tags": ()})(),
    )

    push_ok = evolution_infra.git_commit_bot(
        999,
        998,
        "test",
        official_certificate={
            "certificate_digest": "cert-digest",
            "candidate_hash": "candidate-hash",
            "policy_id": "official-full-v2",
        },
    )

    assert push_ok is False
    assert ("add", "--", "bots/national_v999") in calls
    assert any(call[:1] == ("commit",) for call in calls)
    assert not any("docs/user-notes.md" in call for call in calls if call[:1] == ("commit",))


def test_git_commit_bot_binds_official_certificate_to_commit_and_tag(monkeypatch):
    import bot_artifact
    import evolution_infra

    calls = []
    staged = []

    def fake_git(*args, **_kwargs):
        calls.append(args)
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main\n"
        if args == ("diff", "--cached", "--name-only"):
            return "\n".join(staged) + ("\n" if staged else "")
        if args == ("add", "--", "bots/national_v999"):
            staged.append("bots/national_v999/main.py")
            return ""
        if args[:2] == ("commit", "-m"):
            assert "official-certificate: cert-digest" in args[2]
            assert "official-candidate-hash: candidate-hash" in args[2]
            return ""
        if args in {
            ("rev-parse", "HEAD"),
            ("rev-parse", "--short=12", "HEAD"),
        }:
            return "abc123456789\n"
        if args == ("tag", "-d", "national-bot-v999"):
            return ""
        if args[:3] == ("tag", "national-bot-v999", "-m"):
            assert "official-certificate: cert-digest" in args[3]
            assert "official-policy: official-full-v2" in args[3]
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    monkeypatch.setattr(bot_artifact, "hash_path", lambda _path: "candidate-hash")
    monkeypatch.setattr(evolution_infra, "_require_national_epoch_registry_for_commit", lambda: None)
    monkeypatch.setattr(
        evolution_infra,
        "_advance_national_epoch_high_water",
        lambda version: calls.append(("advance-high-water", version))
        or type("Mutation", (), {"created_tags": (f"national-high-water-v{version}",)})(),
    )

    evolution_infra.git_commit_bot(
        999,
        998,
        "test",
        official_certificate={
            "certificate_digest": "cert-digest",
            "candidate_hash": "candidate-hash",
            "policy_id": "official-full-v2",
        },
    )

    assert any(call[:3] == ("tag", "national-bot-v999", "-m") for call in calls)
    commit_index = next(index for index, call in enumerate(calls) if call[:1] == ("commit",))
    high_water_index = calls.index(("advance-high-water", 999))
    tag_index = next(index for index, call in enumerate(calls) if call[:3] == ("tag", "national-bot-v999", "-m"))
    assert commit_index < high_water_index < tag_index
