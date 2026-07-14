"""Fail-closed isolation between national-native and Botzone-era facilities."""

from __future__ import annotations

import asyncio
import ast
import builtins
import inspect
import sys
from types import SimpleNamespace

import pytest


def test_unknown_workflow_profile_fails_closed(monkeypatch):
    import workflow_profiles

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native_typo")

    with pytest.raises(
        workflow_profiles.WorkflowProfileConfigurationError,
        match="unknown workflow profile",
    ):
        workflow_profiles.get_workflow_profile()


def test_unknown_workflow_profile_cannot_reenter_adapter_control_paths(monkeypatch):
    import evaluation_contract
    import pipeline_state
    import tool_helpers
    import workflow_profiles

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native_typo")

    for resolver in (
        tool_helpers._active_workflow_profile_info,
        pipeline_state._active_workflow_profile_info,
        evaluation_contract._active_national_execution_mode,
    ):
        with pytest.raises(workflow_profiles.WorkflowProfileConfigurationError):
            resolver()


def test_unknown_rating_protocol_fails_closed(monkeypatch):
    import elo_daemon

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    monkeypatch.setenv("POK_RATING_PROTOCOL", "nationl")

    with pytest.raises(ValueError, match="invalid POK_RATING_PROTOCOL"):
        elo_daemon._rating_protocol_config(n_pairs=1)


def test_national_native_fix_registry_reports_without_mutating(
    monkeypatch,
    tmp_path,
):
    import fix_injection
    import system_log

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    bot_dir = tmp_path / "national_v999"
    bot_dir.mkdir()
    (bot_dir / "national_bot.py").write_text("def main():\n    pass\n", encoding="utf-8")
    (bot_dir / "state.py").write_text(
        "def state(last_raise_to, my_round_bet):\n"
        "    min_raise_action = max(0, 2 * last_raise_to - my_round_bet)\n"
        "    return min_raise_action\n",
        encoding="utf-8",
    )
    before = {
        path.name: path.read_bytes()
        for path in bot_dir.iterdir()
        if path.is_file()
    }

    applied, reported = fix_injection.apply_known_fixes(bot_dir)

    assert applied == []
    assert "BOT-002a" in reported
    assert {
        path.name: path.read_bytes()
        for path in bot_dir.iterdir()
        if path.is_file()
    } == before

    events = []
    monkeypatch.setattr(system_log, "log_system_event", lambda *args: events.append(args))
    fix_injection.log_fix_application(applied, reported, bot_dir, source_v=998)

    assert events[0][0] == "pipeline.fix_injection_report_only"
    assert events[0][3]["mutation_enabled"] is False
    assert events[0][3]["report_only"] is True


def test_legacy_fix_registry_can_still_repair_archived_regressions(
    monkeypatch,
    tmp_path,
):
    import fix_injection

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "default")
    bot_dir = tmp_path / "legacy_bot"
    bot_dir.mkdir()
    target = bot_dir / "constants.py"
    target.write_text("TOTAL_HANDS = 50\n", encoding="utf-8")

    applied, _reported = fix_injection.apply_known_fixes(bot_dir)

    assert "BOT-004" in applied
    assert target.read_text(encoding="utf-8") == "TOTAL_HANDS = 70\n"


def test_national_native_post_cleanup_skips_engine_battle_facilities(
    monkeypatch,
):
    import evolution_infra
    import exploitability_prober
    import generation_scheduler
    import qd_async_eval

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    monkeypatch.setattr(evolution_infra, "git_has_tag", lambda _version: True)
    monkeypatch.setattr(evolution_infra, "get_active_bots", lambda: [])

    legacy_calls = []
    monkeypatch.setattr(
        exploitability_prober,
        "run_exploitability_probes",
        lambda *_args, **_kwargs: legacy_calls.append("exploitability"),
    )
    monkeypatch.setattr(
        qd_async_eval,
        "launch_qd_eval",
        lambda *_args, **_kwargs: legacy_calls.append("qd"),
    )
    fingerprints = []
    monkeypatch.setattr(
        generation_scheduler,
        "_record_committed_bot_fingerprint",
        lambda version, source_v: fingerprints.append((version, source_v)),
    )
    events = []
    monkeypatch.setattr(
        generation_scheduler,
        "log_system_event",
        lambda *args: events.append(args),
    )

    ctx = generation_scheduler.GenerationContext(
        current_v=150,
        next_v=151,
        strategy="master",
        source_v=150,
    )
    asyncio.run(generation_scheduler.post_generation_cleanup(None, None, ctx))

    assert legacy_calls == []
    assert fingerprints == []
    skipped = {event[0]: event[3] for event in events if len(event) >= 4}
    expected_reason = "national_native_legacy_engine_facility_disabled"
    assert skipped["pipeline.exploitability_probe_skipped"]["reason"] == expected_reason
    assert skipped["pipeline.qd_eval_skipped"]["reason"] == expected_reason
    assert skipped["pipeline.behavior_fingerprint_skipped"]["reason"] == expected_reason


def test_national_native_disables_botzone_behavior_advisories(monkeypatch):
    import generation_scheduler
    import tool_commit

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")

    assert generation_scheduler._legacy_engine_post_commit_facilities_enabled() is False
    assert tool_commit._legacy_behavior_advisory_enabled() is False


def test_legacy_profile_keeps_botzone_behavior_advisories(monkeypatch):
    import generation_scheduler
    import tool_commit

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "default")

    assert generation_scheduler._legacy_engine_post_commit_facilities_enabled() is True
    assert tool_commit._legacy_behavior_advisory_enabled() is True


def test_native_daemon_has_no_top_level_botzone_battle_import():
    import elo_daemon

    tree = ast.parse(inspect.getsource(elo_daemon))
    top_level_imports = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    }
    assert "engine.battle" not in top_level_imports
    local_source = inspect.getsource(elo_daemon._run_local_json_match)
    assert "from engine.battle import mirror_battle" in local_source


def test_native_inline_eval_returns_before_legacy_battle_import():
    import tool_eval

    source = inspect.getsource(tool_eval.run_inline_eval.handler)
    native_pos = source.find('national_execution_mode", "adapter") == "native_tcp"')
    native_return_pos = source.find('source": "inline_native_diagnostic"')
    legacy_import_pos = source.find("from engine.battle import mirror_battle")

    assert 0 <= native_pos < native_return_pos < legacy_import_pos


def test_native_decision_backend_cannot_import_or_call_botzone_tester(
    monkeypatch,
    tmp_path,
):
    import tool_gates

    expected = {
        "pass_rate": 1.0,
        "passed": 3,
        "total": 3,
        "critical_failures": [],
        "coverage_only_count": 0,
        "external_scenario_sidecars_loaded": False,
    }
    monkeypatch.setitem(
        sys.modules,
        "national_decision_tester",
        SimpleNamespace(run_national_decision_tests=lambda _path: expected),
    )
    monkeypatch.setattr(
        tool_gates,
        "run_decision_test_details",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("legacy decision backend executed")
        ),
    )
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "decision_tester":
            raise AssertionError("legacy decision_tester imported")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    detail, meta = tool_gates._run_workflow_decision_tests(
        tmp_path,
        native_tcp_mode=True,
        extra_scenarios=[{"id": "legacy-sidecar-must-be-ignored"}],
    )

    assert detail is expected
    assert meta == {
        "assertion_backed_count": 3,
        "coverage_only_count": 0,
        "external_scenario_sidecars_loaded": False,
    }


def test_legacy_profile_keeps_post_commit_facilities_available(monkeypatch):
    import generation_scheduler

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "default")

    assert generation_scheduler._legacy_engine_post_commit_facilities_enabled() is True
