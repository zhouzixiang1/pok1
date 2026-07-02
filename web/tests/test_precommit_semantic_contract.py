import pytest

from tool_eval import run_precommit_eval as _run_precommit_eval_tool


run_precommit_eval = _run_precommit_eval_tool.handler


def _parse_tool_result(result):
    import json

    return json.loads(result["content"][0]["text"])


def _patch_precommit_harness(monkeypatch, tmp_path, mirror_result):
    import sys
    from unittest.mock import MagicMock

    bot_root = tmp_path / "bots"
    for name in ("claude_v99", "claude_v98"):
        bot_dir = bot_root / name
        bot_dir.mkdir(parents=True)
        (bot_dir / "main.py").write_text("# fake bot\n", encoding="utf-8")

    monkeypatch.setattr("tool_eval._bot_main", lambda name: bot_root / name / "main.py")
    monkeypatch.setattr("tool_eval._select_precommit_opponents", lambda _v, _sv: [
        {"name": "claude_v98", "reason": "parent"}
    ])
    monkeypatch.setattr("tool_eval.is_daemon_scheduler_capable", lambda: False)
    monkeypatch.setattr("tool_eval._matching_checkpoint", lambda _v, _sv: {
        "version": 99,
        "source_v": 98,
        "master_plan": {"tasks": []},
        "gate_results": {
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
            "review": {"approved": True},
            "critic": {"approved": True, "score": 7},
        },
    })
    monkeypatch.setattr("tool_eval._get_ui", lambda: MagicMock())
    monkeypatch.setattr("tool_eval._record_gate", lambda *a, **k: True)
    monkeypatch.setattr("tool_eval.PRECOMMIT_SEQUENTIAL_EARLY_STOP", False)

    import engine.battle  # noqa: F401 - ensure module is present in sys.modules

    battle_module = sys.modules["engine.battle"]
    monkeypatch.setattr(
        battle_module,
        "mirror_battle",
        lambda *a, **k: mirror_result,
    )


@pytest.mark.asyncio
async def test_semantic_block_without_evidence_is_downgraded(monkeypatch, tmp_path):
    import audit_agents
    import tool_eval

    _patch_precommit_harness(monkeypatch, tmp_path, ([3, 0], 0, 3, [500, 400, 300]))

    async def _unsupported_block(*_args, **_kwargs):
        return {
            "recommended_action": "block",
            "confidence": "medium",
            "regression_semantics": "marginal",
            "block_evidence": [],
        }

    events = []
    monkeypatch.setattr(audit_agents, "_run_precommit_semantic", _unsupported_block)
    monkeypatch.setattr(tool_eval, "log_system_event", lambda *args, **kwargs: events.append(args))

    result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 3})
    data = _parse_tool_result(result)

    assert not any(b.get("reason") == "semantic_regression" for b in data["blockers"])
    assert any(event[0] == "pipeline.precommit_semantic_block_downgraded" for event in events)


@pytest.mark.asyncio
async def test_semantic_block_with_high_confidence_evidence_blocks(monkeypatch, tmp_path):
    import audit_agents

    _patch_precommit_harness(monkeypatch, tmp_path, ([3, 0], 0, 3, [500, 400, 300]))

    async def _supported_block(*_args, **_kwargs):
        return {
            "recommended_action": "block",
            "confidence": "high",
            "regression_semantics": "clear_regression",
            "block_evidence": ["claude_v98 river all-in loss field: net_chips=-900"],
        }

    monkeypatch.setattr(audit_agents, "_run_precommit_semantic", _supported_block)

    result = await run_precommit_eval({"version": 99, "source_v": 98, "n_games": 3})
    data = _parse_tool_result(result)

    assert data["passed"] is False
    semantic = [b for b in data["blockers"] if b.get("reason") == "semantic_regression"]
    assert semantic
    assert semantic[0]["evidence"] == ["claude_v98 river all-in loss field: net_chips=-900"]
