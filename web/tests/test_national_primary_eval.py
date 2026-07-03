import asyncio
import json
from pathlib import Path

import tool_eval
from national_eval import run_national_precommit
from workflow_profiles import get_workflow_profile


def _write_call_bot(bot_dir: Path):
    bot_dir.mkdir(parents=True)
    (bot_dir / "main.py").write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    print(json.dumps({'response': 0}), flush=True)\n",
        encoding="utf-8",
    )


def test_national_primary_profile_selects_national_protocol():
    profile = get_workflow_profile("national_primary")
    assert profile.evaluation_protocol == "national"
    assert profile.national_precommit_hands == 70
    assert profile.national_acceptance_hands == 70


def test_national_precommit_backend_runs_minimal_bots(tmp_path):
    bot_a = tmp_path / "CallA"
    bot_b = tmp_path / "CallB"
    _write_call_bot(bot_a)
    _write_call_bot(bot_b)

    result = asyncio.run(run_national_precommit(
        bot_a,
        [{"name": "CallB", "path": str(bot_b), "reason": "parent"}],
        hands=2,
        matches_per_opponent=1,
        parent_label="CallB",
        deck_seed_base=42,
        parent_loss_threshold=-999999,
        aggregate_loss_threshold=-999999,
    ))

    assert result["evaluation_protocol"] == "national"
    assert result["matchups"][0]["hands_per_match"] == 2
    assert result["paired_bootstrap"]["protocol"] == "national"
    assert result["paired_bootstrap"]["net_chips_samples"] == 1
    assert result["blockers"] == []
    assert result["passed"] is True


def test_national_precommit_blocks_without_opponents(tmp_path):
    bot_a = tmp_path / "CallA"
    _write_call_bot(bot_a)

    result = asyncio.run(run_national_precommit(
        bot_a,
        [],
        hands=2,
        matches_per_opponent=1,
        deck_seed_base=42,
    ))

    assert result["passed"] is False
    assert result["paired_bootstrap"]["net_chips_samples"] == 0
    assert {blocker["reason"] for blocker in result["blockers"]} >= {
        "national_no_opponents",
        "national_no_samples",
    }


def test_tool_eval_national_backend_returns_precommit_shape(tmp_path, monkeypatch):
    bot_a = tmp_path / "CallA"
    bot_b = tmp_path / "CallB"
    _write_call_bot(bot_a)
    _write_call_bot(bot_b)
    profile = get_workflow_profile("national_primary")
    profile.national_precommit_hands = 2

    recorded = {}

    def fake_record_gate(version, source_v, name, payload, stage=None, reviewer_feedback=None):
        recorded["version"] = version
        recorded["source_v"] = source_v
        recorded["name"] = name
        recorded["payload"] = payload
        recorded["stage"] = stage
        recorded["reviewer_feedback"] = reviewer_feedback
        return True

    monkeypatch.setattr(tool_eval, "_record_gate", fake_record_gate)
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)

    wrapped = asyncio.run(tool_eval._run_national_precommit_backend(
        v=10,
        source_v=9,
        requested_n_games=8,
        candidate_name="CallA",
        parent_name="CallB",
        candidate_main=bot_a,
        code_fingerprint="abc",
        workflow_profile=profile,
        candidate_id="CallA_from_9",
        opponents=[{"name": "CallB", "path": str(bot_b), "reason": "parent"}],
        all_opponents=[{"name": "CallB", "reason": "parent"}],
        precommit_attempt=1,
        initial_blockers=[],
        started_at=0.0,
    ))
    result = json.loads(wrapped["content"][0]["text"])

    assert result["evaluation_protocol"] == "national"
    assert result["hands_per_match"] == 2
    assert result["scorecard"]["gates"][0]["name"] == "national_precommit_regression"
    assert recorded["name"] == "precommit_eval"
    assert recorded["stage"] in {"verified", "precommit_failed"}


def test_tool_eval_national_backend_blocks_without_samples(tmp_path, monkeypatch):
    bot_a = tmp_path / "CallA"
    _write_call_bot(bot_a)
    profile = get_workflow_profile("national_primary")
    profile.national_precommit_hands = 2

    recorded = {}

    def fake_record_gate(version, source_v, name, payload, stage=None, reviewer_feedback=None):
        recorded["name"] = name
        recorded["payload"] = payload
        recorded["stage"] = stage
        return True

    monkeypatch.setattr(tool_eval, "_record_gate", fake_record_gate)
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)

    wrapped = asyncio.run(tool_eval._run_national_precommit_backend(
        v=10,
        source_v=9,
        requested_n_games=8,
        candidate_name="CallA",
        parent_name="CallB",
        candidate_main=bot_a,
        code_fingerprint="abc",
        workflow_profile=profile,
        candidate_id="CallA_from_9",
        opponents=[],
        all_opponents=[],
        precommit_attempt=1,
        initial_blockers=[],
        started_at=0.0,
    ))
    result = json.loads(wrapped["content"][0]["text"])

    assert result["passed"] is False
    assert result["paired_bootstrap"]["net_chips_samples"] == 0
    assert result["failure_class"] == "regression"
    assert result["scorecard"]["gates"][0]["status"] == "failed"
    assert {blocker["reason"] for blocker in result["blockers"]} == {"national_no_samples"}
    assert recorded["name"] == "precommit_eval"
    assert recorded["stage"] == "precommit_failed"
