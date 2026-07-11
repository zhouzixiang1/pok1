import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import elo_daemon
import national_native
import runtime_capacity
import tool_eval
from national_eval import run_national_precommit
from precommit_eval_contract import build_sample_plan
from workflow_profiles import get_workflow_profile


def _write_call_bot(bot_dir: Path):
    bot_dir.mkdir(parents=True)
    (bot_dir / "main.py").write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    print(json.dumps({'response': 0}), flush=True)\n",
        encoding="utf-8",
    )


def _backend_plan(opponents, *, hands=70, matches=8):
    normalized = [
        {
            "name": item["name"],
            "reason": item.get("reason", "precommit"),
            "path": item.get("path", ""),
        }
        for item in opponents
    ]
    return {
        "plan_digest": "test-plan",
        "opponents": normalized,
        "settings": {
            "hands_per_match": hands,
            "matches_per_opponent": matches,
            "parent_min_samples": 4,
            "parent_max_score": 0.40,
            "aggregate_min_samples": 8,
            "aggregate_min_loss_margin": 2,
        },
        "sample_plan": build_sample_plan(normalized, matches),
    }


def _backend_evaluation_contract():
    return {"contract_digest": "test-contract"}


def test_national_primary_profile_selects_national_protocol():
    profile = get_workflow_profile("national_primary")
    assert profile.evaluation_protocol == "national"
    assert profile.rating_protocol == "national"
    assert profile.national_precommit_hands == 70
    assert profile.national_rating_hands == 70
    assert profile.national_rating_matches == 1
    assert profile.national_acceptance_hands == 70
    assert profile.national_acceptance_timeout_sec == 420
    assert profile.eval_wait_min_games == 24
    assert profile.eval_wait_rd_threshold == 110.0
    assert profile.eval_wait_rd_min_games == 12


def test_daemon_dispatches_national_rating_backend(monkeypatch):
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_primary")
    calls = {}

    def fake_national(a, b, pa, pb, config):
        calls["national"] = (a, b, pa, pb, config)
        return (a, b, 1, 0, 0, 1, None, [100])

    def fake_local(*_args):
        raise AssertionError("local rating backend should not run in national_primary")

    monkeypatch.setattr(elo_daemon, "_run_national_rating_match", fake_national)
    monkeypatch.setattr(elo_daemon, "_run_local_json_match", fake_local)

    result = elo_daemon.run_single_match(("A", "B", "/a/main.py", "/b/main.py", 5))

    assert result == ("A", "B", 1, 0, 0, 1, None, [100])
    assert calls["national"][4]["protocol"] == "national"
    assert calls["national"][4]["national_hands"] == 70
    assert calls["national"][4]["national_matches"] == 5


def test_national_acceptance_runs_candidate_pairs_only(monkeypatch):
    import national_acceptance

    calls = []

    def fake_resolve(token):
        path = Path(f"/bots/{token}/main.py")
        return national_acceptance.BotSpec(label=str(token), path=path)

    async def fake_run_pair(bot_a, bot_b, hands, *, strict=True, deck_seed_base=None):
        calls.append((bot_a.label, bot_b.label, hands, strict, deck_seed_base))
        return {
            "bot_a": bot_a.label,
            "bot_b": bot_b.label,
            "hands_requested": hands,
            "hands_played": hands,
            "per_player": {
                bot_a.label: {
                    "earnings": 100,
                    "illegal_actions": 0,
                    "timeouts": 0,
                    "adapter": {},
                },
                bot_b.label: {
                    "earnings": -100,
                    "illegal_actions": 0,
                    "timeouts": 0,
                    "adapter": {},
                },
            },
            "net_chips_a": 100,
            "net_chips_b": -100,
            "net_chips_a_per_hand": 50.0,
            "strict_adapter": strict,
            "deck_seed_base": deck_seed_base,
            "passed_compliance": True,
            "issues": [],
        }

    monkeypatch.setattr(national_acceptance, "resolve_bot", fake_resolve)
    monkeypatch.setattr(national_acceptance, "run_pair", fake_run_pair)

    result = asyncio.run(national_acceptance.run_acceptance_for_candidate(
        "A",
        opponent_tokens=["B", "C"],
        hands=2,
        timeout_sec=5,
    ))

    assert result.passed is True
    assert calls == [
        ("A", "B", 2, True, None),
        ("A", "C", 2, True, None),
    ]
    assert result.report["candidate_only"] is True
    assert result.report["pair_count"] == 2
    assert "C" not in result.report["matrix"]["B"]


def test_national_acceptance_timeout_returns_failure(monkeypatch):
    import national_acceptance

    def fake_resolve(token):
        path = Path(f"/bots/{token}/main.py")
        return national_acceptance.BotSpec(label=str(token), path=path)

    async def slow_run_pair(*_args, **_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(national_acceptance, "resolve_bot", fake_resolve)
    monkeypatch.setattr(national_acceptance, "run_pair", slow_run_pair)

    result = asyncio.run(national_acceptance.run_acceptance_for_candidate(
        "A",
        opponent_tokens=["B"],
        hands=2,
        timeout_sec=0.01,
    ))

    assert result.passed is False
    assert result.issues == ["national_acceptance_timeout: exceeded 0.01s"]
    assert result.summary["passed_compliance"] is False
    assert result.report["timed_out"] is True


def test_daemon_explicit_national_rating_matches_override_pairs(monkeypatch):
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_primary")
    monkeypatch.setenv("POK_NATIONAL_RATING_MATCHES", "2")
    calls = {}

    def fake_national(a, b, pa, pb, config):
        calls["national"] = config
        return (a, b, 1, 0, 0, 1, None, [100])

    monkeypatch.setattr(elo_daemon, "_run_national_rating_match", fake_national)
    monkeypatch.setattr(
        elo_daemon,
        "_run_local_json_match",
        lambda *_args: (_ for _ in ()).throw(AssertionError("local backend should not run")),
    )

    elo_daemon.run_single_match(("A", "B", "/a/main.py", "/b/main.py", 5))

    assert calls["national"]["national_matches"] == 2


def test_daemon_defaults_to_native_national_rating_backend(monkeypatch):
    monkeypatch.delenv("POK_WORKFLOW_PROFILE", raising=False)
    monkeypatch.delenv("POK_RATING_PROTOCOL", raising=False)
    calls = {}

    def fake_national(a, b, pa, pb, config):
        calls["national"] = (a, b, pa, pb, config)
        return (a, b, 1, 0, 0, 1, None, [100])

    def fake_local(*_args):
        raise AssertionError("local backend should not run by default")

    monkeypatch.setattr(elo_daemon, "_run_national_rating_match", fake_national)
    monkeypatch.setattr(elo_daemon, "_run_local_json_match", fake_local)

    result = elo_daemon.run_single_match(("A", "B", "/a/main.py", "/b/main.py", 5))

    assert result == ("A", "B", 1, 0, 0, 1, None, [100])
    assert calls["national"][4]["protocol"] == "national"
    assert calls["national"][4]["national_execution_mode"] == "native_tcp"


def test_native_rating_environment_cannot_shorten_70_hand_samples(monkeypatch):
    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    monkeypatch.setenv("POK_RATING_PROTOCOL", "local_json")
    monkeypatch.setenv("POK_NATIONAL_RATING_HANDS", "3")

    config = elo_daemon._rating_protocol_config(n_pairs=1)

    assert config["national_execution_mode"] == "native_tcp"
    assert config["protocol"] == "national"
    assert config["national_hands"] == 70
    assert config["native_strength_runtime_overlay"]["mode"] == (
        "current_system_wrapper_bilateral"
    )


def test_native_precommit_environment_cannot_shorten_70_hand_contract(monkeypatch):
    profile = get_workflow_profile("national_native")
    monkeypatch.setenv("POK_NATIONAL_PRECOMMIT_HANDS", "3")

    hands, _matches = tool_eval._national_precommit_shape(profile, 8)

    assert hands == 70


def test_daemon_does_not_nest_capacity_around_native_runner(monkeypatch):
    monkeypatch.delenv("POK_WORKFLOW_PROFILE", raising=False)
    monkeypatch.delenv("POK_RATING_PROTOCOL", raising=False)

    def forbidden_outer_lease(*_args, **_kwargs):
        raise AssertionError("native runner must own the only capacity lease")

    monkeypatch.setattr(runtime_capacity, "acquire_match_slots", forbidden_outer_lease)
    monkeypatch.setattr(
        elo_daemon,
        "_run_national_rating_match",
        lambda a, b, *_args: (a, b, 1, 0, 0, 1, None, [100]),
    )

    result = elo_daemon.run_single_match(("A", "B", "/a", "/b", 1))

    assert result[:6] == ("A", "B", 1, 0, 0, 1)


def test_daemon_native_rating_requires_existing_native_entries_for_both_players(monkeypatch):
    calls = []

    async def fake_native_pair(bot_a, bot_b, hands, **kwargs):
        calls.append((bot_a, bot_b, hands, kwargs))
        return {
            "net_chips_a": 100,
            "hands_played": 70,
            "passed_compliance": True,
            "issues": [],
            "wrapper_used": False,
            "runtime_overlay": {
                "enabled": True,
                "both_sides": True,
                "mode": "current_system_wrapper_bilateral",
            },
        }

    monkeypatch.setattr(
        national_native,
        "run_current_runtime_native_strength_pair",
        fake_native_pair,
    )
    monkeypatch.setattr(elo_daemon, "save_match_replay", lambda *_args: None)

    result = elo_daemon._run_national_rating_match(
        "A",
        "B",
        "/bots/A/main.py",
        "/bots/B/main.py",
        {
            "national_hands": 70,
            "national_matches": 1,
            "national_execution_mode": "native_tcp",
            "strict": True,
        },
    )

    assert result == ("A", "B", 1, 0, 0, 1, None, [100])
    assert len(calls) == 1
    assert calls[0][3]["require_native_a"] is True
    assert calls[0][3]["require_native_b"] is True


def test_daemon_national_rating_maps_net_chips(monkeypatch):
    import national_acceptance

    samples = iter([
        {"net_chips_a": 100, "hands_played": 70, "passed_compliance": True, "issues": []},
        {"net_chips_a": -50, "hands_played": 70, "passed_compliance": True, "issues": []},
        {"net_chips_a": 0, "hands_played": 70, "passed_compliance": True, "issues": []},
    ])
    saved = {}

    async def fake_run_pair(*_args, **_kwargs):
        return next(samples)

    monkeypatch.setattr(
        national_acceptance,
        "resolve_bot",
        lambda token: SimpleNamespace(label=Path(token).parent.name or str(token), path=Path(token)),
    )
    monkeypatch.setattr(national_acceptance, "run_pair", fake_run_pair)
    monkeypatch.setattr(elo_daemon, "save_match_replay", lambda *args: saved.setdefault("args", args))

    result = elo_daemon._run_national_rating_match(
        "A",
        "B",
        "/bots/A/main.py",
        "/bots/B/main.py",
        {"national_hands": 70, "national_matches": 3, "strict": True},
    )

    assert result == ("A", "B", 1, 1, 1, 3, None, [100, -50, 0])
    assert saved["args"][2:5] == (1, 1, 1)


def test_daemon_national_rating_blocks_compliance_failures(monkeypatch):
    import national_acceptance

    async def fake_run_pair(*_args, **_kwargs):
        return {
            "net_chips_a": 100,
            "hands_played": 70,
            "passed_compliance": False,
            "issues": ["A: illegal_actions=1"],
        }

    monkeypatch.setattr(
        national_acceptance,
        "resolve_bot",
        lambda token: SimpleNamespace(label=Path(token).parent.name or str(token), path=Path(token)),
    )
    monkeypatch.setattr(national_acceptance, "run_pair", fake_run_pair)
    monkeypatch.setattr(
        elo_daemon,
        "save_match_replay",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("compliance-failed rating rows must not be persisted")
        ),
    )

    result = elo_daemon._run_national_rating_match(
        "A",
        "B",
        "/bots/A/main.py",
        "/bots/B/main.py",
        {"national_hands": 70, "national_matches": 1, "strict": True},
    )

    assert result[:6] == ("A", "B", 0, 0, 0, 0)
    assert result[6].startswith("national_rating_contract:")
    assert result[7] == []


def test_daemon_national_rating_does_not_persist_incomplete_rows(monkeypatch):
    import national_acceptance

    async def fake_run_pair(*_args, **_kwargs):
        return {
            "net_chips_a": 1,
            "hands_played": 69,
            "passed_compliance": True,
            "issues": [],
        }

    monkeypatch.setattr(
        national_acceptance,
        "resolve_bot",
        lambda token: SimpleNamespace(label=str(token), path=Path(token)),
    )
    monkeypatch.setattr(national_acceptance, "run_pair", fake_run_pair)
    monkeypatch.setattr(
        elo_daemon,
        "save_match_replay",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("incomplete rating rows must not reach replay/history")
        ),
    )

    result = elo_daemon._run_national_rating_match(
        "A",
        "B",
        "/bots/A/main.py",
        "/bots/B/main.py",
        {"national_hands": 70, "national_matches": 1, "strict": True},
    )

    assert result[:6] == ("A", "B", 0, 0, 0, 0)
    assert "hands_played=69/70" in result[6]
    assert result[7] == []


def test_native_precommit_excludes_failed_and_incomplete_strength_rows(tmp_path, monkeypatch):
    candidate = tmp_path / "Candidate"
    opponent = tmp_path / "Opponent"
    candidate.mkdir()
    opponent.mkdir()
    results = iter([
        {
            "hands_played": 70,
            "net_chips_a": 1,
            "passed_compliance": False,
            "issues": ["Candidate: illegal_actions=1"],
            "wrapper_used": False,
        },
        {
            "hands_played": 69,
            "net_chips_a": -1,
            "passed_compliance": True,
            "issues": ["hands_played=69/70"],
            "wrapper_used": False,
        },
    ])

    monkeypatch.setattr(
        national_native,
        "resolve_bot",
        lambda token: (Path(token).name, Path(token)),
    )
    monkeypatch.setattr(
        national_native,
        "run_current_runtime_native_strength_pair",
        lambda *_args, **_kwargs: _async_next(results),
    )

    result = asyncio.run(national_native.run_native_precommit(
        candidate,
        [{"name": "Opponent", "path": str(opponent), "reason": "parent"}],
        hands=70,
        matches_per_opponent=2,
        parent_label="Opponent",
    ))

    matchup = result["matchups"][0]
    assert matchup["n_played"] == 0
    assert matchup["net_chips"] == []
    assert matchup["wins"] == matchup["losses"] == matchup["draws"] == 0
    assert all(row["strength_admitted"] is False for row in matchup["repeats"])
    assert result["aggregate_net_chips"] == []
    assert result["paired_bootstrap"]["net_chips_samples"] == 0
    assert result["passed"] is False


async def _async_next(iterator):
    return next(iterator)


def _run_fake_native_precommit(tmp_path, monkeypatch, net_chips):
    candidate = tmp_path / "Candidate"
    opponent = tmp_path / "Opponent"
    candidate.mkdir(exist_ok=True)
    opponent.mkdir(exist_ok=True)
    samples = iter(net_chips)

    async def fake_pair(*_args, **_kwargs):
        net = next(samples)
        return {
            "hands_played": 70,
            "net_chips_a": net,
            "passed_compliance": True,
            "issues": [],
            "wrapper_used": False,
            "runtime_overlay": {
                "enabled": True,
                "both_sides": True,
                "mode": "current_system_wrapper_bilateral",
            },
        }

    monkeypatch.setattr(
        national_native,
        "resolve_bot",
        lambda token: (Path(token).name, Path(token)),
    )
    monkeypatch.setattr(
        national_native,
        "run_current_runtime_native_strength_pair",
        fake_pair,
    )
    return asyncio.run(national_native.run_native_precommit(
        candidate,
        [{"name": "Opponent", "path": str(opponent), "reason": "parent"}],
        hands=70,
        matches_per_opponent=len(net_chips),
        parent_label="Opponent",
    ))


def test_native_precommit_0w_8l_tiny_chip_losses_fail(tmp_path, monkeypatch):
    result = _run_fake_native_precommit(tmp_path, monkeypatch, [-1] * 8)

    assert result["total_wins"] == 0
    assert result["total_losses"] == 8
    assert result["passed"] is False
    assert {row["reason"] for row in result["blockers"]} >= {
        "lost_to_parent",
        "aggregate_native_regression",
    }


def test_native_precommit_9w_7l_ignores_huge_secondary_chip_deficit(tmp_path, monkeypatch):
    result = _run_fake_native_precommit(
        tmp_path,
        monkeypatch,
        [1] * 9 + [-100_000] * 7,
    )

    assert result["total_wins"] == 9
    assert result["total_losses"] == 7
    assert result["paired_bootstrap"]["outcome_gate"]["primary_match_score"] == 9 / 16
    assert result["paired_bootstrap"]["net_chips_mean"] < 0
    assert result["blockers"] == []
    assert result["passed"] is True


def test_tool_backend_excludes_telemetry_only_samples_from_totals_and_contract(
    tmp_path,
    monkeypatch,
):
    candidate = tmp_path / "Candidate"
    parent = tmp_path / "Parent"
    nemesis = tmp_path / "Nemesis"
    for path in (candidate, parent, nemesis):
        path.mkdir()
    opponents = [
        {"name": "Parent", "path": str(parent), "reason": "parent"},
        {"name": "Nemesis", "path": str(nemesis), "reason": "nemesis_probe"},
    ]
    plan = _backend_plan(opponents, hands=70, matches=8)

    async def fake_pair(_candidate, opponent_path, _hands, **_kwargs):
        net = 1 if Path(opponent_path).name == "Parent" else -100_000
        return {
            "hands_played": 70,
            "net_chips_a": net,
            "passed_compliance": True,
            "issues": [],
            "wrapper_used": False,
            "runtime_overlay": {
                "enabled": True,
                "both_sides": True,
                "mode": "current_system_wrapper_bilateral",
            },
        }

    monkeypatch.setattr(
        national_native,
        "resolve_bot",
        lambda token: (Path(token).name, Path(token)),
    )
    monkeypatch.setattr(
        national_native,
        "run_current_runtime_native_strength_pair",
        fake_pair,
    )
    native_result = asyncio.run(national_native.run_native_precommit(
        candidate,
        opponents,
        hands=70,
        matches_per_opponent=8,
        parent_label="Parent",
        sample_plan=plan["sample_plan"],
    ))

    async def fake_native_precommit(*_args, **_kwargs):
        return native_result

    monkeypatch.setattr(national_native, "run_native_precommit", fake_native_precommit)
    monkeypatch.setattr(tool_eval, "_record_gate", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)
    monkeypatch.delenv("POK_OFFICIAL_REQUIRED", raising=False)
    monkeypatch.delenv("POK_OFFICIAL_PRECOMMIT_GATE", raising=False)
    profile = get_workflow_profile("national_native")

    wrapped = asyncio.run(tool_eval._run_national_precommit_backend(
        v=10,
        source_v=9,
        requested_n_games=8,
        candidate_name="Candidate",
        parent_name="Parent",
        candidate_main=candidate,
        code_fingerprint="abc",
        workflow_profile=profile,
        candidate_id="Candidate_from_9",
        opponents=opponents,
        all_opponents=opponents,
        precommit_attempt=1,
        initial_blockers=[],
        started_at=0.0,
        precommit_plan=plan,
        evaluation_contract=_backend_evaluation_contract(),
    ))
    result = json.loads(wrapped["content"][0]["text"])

    assert native_result["total_wins"] == 8
    assert native_result["total_losses"] == 0
    assert native_result["paired_bootstrap"]["net_chips_samples"] == 8
    assert result["expected_net_chips_samples"] == 8
    assert result["total_wins"] == 8
    assert result["total_losses"] == 0
    assert result["strength_order"]["samples"] == 8
    assert "national_outcome_sample_mismatch" not in {
        row["reason"] for row in result["blockers"]
    }
    assert result["passed"] is True


def test_national_precommit_backend_runs_minimal_bots(tmp_path):
    bot_a = tmp_path / "CallA"
    bot_b = tmp_path / "CallB"
    _write_call_bot(bot_a)
    _write_call_bot(bot_b)

    result = asyncio.run(run_national_precommit(
        bot_a,
        [{"name": "CallB", "path": str(bot_b), "reason": "parent"}],
        hands=70,
        matches_per_opponent=1,
        parent_label="CallB",
        deck_seed_base=42,
    ))

    assert result["evaluation_protocol"] == "national"
    assert result["matchups"][0]["hands_per_match"] == 70
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
        hands=70,
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

    opponents = [{"name": "CallB", "path": str(bot_b), "reason": "parent"}]
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
        opponents=opponents,
        all_opponents=[{"name": "CallB", "reason": "parent"}],
        precommit_attempt=1,
        initial_blockers=[],
        started_at=0.0,
        precommit_plan=_backend_plan(opponents),
        evaluation_contract=_backend_evaluation_contract(),
    ))
    result = json.loads(wrapped["content"][0]["text"])

    assert result["evaluation_protocol"] == "national"
    assert result["hands_per_match"] == 70
    assert result["scorecard"]["gates"][0]["name"] == "national_precommit_regression"
    assert recorded["name"] == "precommit_eval"
    assert recorded["stage"] in {"verified", "precommit_failed"}


def test_tool_eval_native_precommit_uses_official_compliance_defaults(tmp_path, monkeypatch):
    import sys
    from types import ModuleType

    bot_a = tmp_path / "CallA"
    bot_b = tmp_path / "CallB"
    _write_call_bot(bot_a)
    _write_call_bot(bot_b)
    profile = get_workflow_profile("national_native")

    calls = []
    native_calls = []

    async def fake_native_precommit(*args, **kwargs):
        native_calls.append(kwargs)
        repeats = [
            {
                "repeat": row["repeat"],
                "deck_seed_base": row["deck_seed_base"],
                "bot_seed_base": row["bot_seed_base"],
            }
            for row in kwargs["sample_plan"]
        ]
        return {
            "evaluation_protocol": "national_native_tcp",
            "candidate": "CallA",
            "opponents": [{"name": "CallB", "path": str(bot_b), "reason": "parent"}],
                "matchups": [{
                    "opponent": "CallB",
                    "repeats": repeats,
                    "wins": 8,
                    "losses": 0,
                    "draws": 0,
                    "net_chips": [100] * 8,
                }],
                "total_wins": 8,
                "total_losses": 0,
                "total_draws": 0,
                "paired_bootstrap": {
                    "protocol": "national_native_tcp",
                    "hands_per_match": 70,
                "matches_per_opponent": 8,
                "net_chips_samples": 8,
                "net_chips_mean": 100.0,
                "aggregate_ci_lower": 25.0,
                "aggregate_ci_upper": 175.0,
                "gate_degraded": False,
            },
            "blockers": [],
            "passed": True,
        }

    fake_national_native = ModuleType("national_native")
    fake_national_native.run_native_precommit = fake_native_precommit
    fake_official_certification = ModuleType("official_certification")
    fake_official_job = ModuleType("official_certification_job")
    fake_official_certification.STATUS_COMPLIANCE_PASS = "official-compliance-pass"
    fake_official_certification.STATUS_CERTIFIED = "official-certified"
    fake_official_certification.STATUS_FAILED = "official-failed"
    fake_official_certification.STATUS_INCONCLUSIVE = "official-inconclusive"
    fake_official_certification.STATUS_PENDING = "official-pending"

    def fake_build_spec(mode, candidate, *, opponent, self_play_rounds, opponent_rounds, target_hands):
        calls.append((mode, candidate, opponent, self_play_rounds, opponent_rounds, target_hands))
        return {
            "mode": mode,
            "candidate": candidate,
            "opponent": opponent,
            "self_play_rounds": self_play_rounds,
            "opponent_rounds": opponent_rounds,
            "target_hands": target_hands,
        }

    def fake_read_status(_candidate):
        return {"status": "local-pass", "mode": None, "issues": []}

    def fake_enqueue_certification(spec, *, reason):
        return {
            "status": "official-pending",
            "mode": spec["mode"],
            "queued": True,
            "reason": reason,
            "summary": {
                "self_play_rounds": spec["self_play_rounds"],
                "opponent_rounds": spec["opponent_rounds"],
                "target_hands": spec["target_hands"],
            },
            "issues": [],
        }

    def fake_official_compliance_verdict(_status):
        return {
            "ok": True,
            "blocking": False,
            "inconclusive": False,
            "classification": "passed_or_pending",
        }

    def fake_select_official_opponent(candidate, active_bots, **_kwargs):
        return {
            "selected": True,
            "candidate": str(candidate),
            "opponent": {
                "bot": bot_b.name,
                "path": str(bot_b),
                "eligible": True,
                "reason": "official_certified",
            },
            "considered": [],
        }

    fake_official_certification.build_spec = fake_build_spec
    fake_official_certification.read_status = fake_read_status
    fake_official_certification.enqueue_certification = fake_enqueue_certification
    fake_official_certification.official_compliance_verdict = fake_official_compliance_verdict
    fake_official_certification.select_official_opponent = fake_select_official_opponent
    fake_official_job.start_or_poll_job = lambda _spec, **_kwargs: {
        "state": "queued",
        "pending": True,
        "job_id": "official-job-test",
        "issues": [],
    }

    monkeypatch.setenv("POK_OFFICIAL_REQUIRED", "1")
    monkeypatch.setenv("POK_OFFICIAL_OPPONENT", str(bot_b))
    monkeypatch.delenv("POK_OFFICIAL_PRECOMMIT_SELF_ROUNDS", raising=False)
    monkeypatch.delenv("POK_OFFICIAL_PRECOMMIT_OPPONENT_ROUNDS", raising=False)
    monkeypatch.delenv("POK_OFFICIAL_PRECOMMIT_TARGET_HANDS", raising=False)
    monkeypatch.setitem(sys.modules, "national_native", fake_national_native)
    monkeypatch.setitem(sys.modules, "official_certification", fake_official_certification)
    monkeypatch.setitem(sys.modules, "official_certification_job", fake_official_job)
    monkeypatch.setattr(tool_eval, "_record_gate", lambda *args, **kwargs: True)
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)

    opponents = [{"name": "CallB", "path": str(bot_b), "reason": "parent"}]
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
        opponents=opponents,
        all_opponents=[{"name": "CallB", "reason": "parent"}],
        precommit_attempt=1,
        initial_blockers=[],
        started_at=0.0,
        precommit_plan=_backend_plan(opponents),
        evaluation_contract=_backend_evaluation_contract(),
    ))
    result = json.loads(wrapped["content"][0]["text"])

    assert calls == [("compliance", str(bot_a), str(bot_b), 1, 1, 10)]
    assert native_calls[0]["matches_per_opponent"] == 8
    assert result["passed"] is True
    assert result["official_platform"]["status"] == "official-pending"
    assert result["official_platform"]["mode"] == "compliance"
    assert result["official_platform"]["summary"] == {
        "self_play_rounds": 1,
        "opponent_rounds": 1,
        "target_hands": 10,
    }
    gate_names = [gate["name"] for gate in result["scorecard"]["gates"]]
    assert "official_platform_compliance" in gate_names
    assert "official_platform_acceptance" not in gate_names
    official_gate = next(
        gate for gate in result["scorecard"]["gates"]
        if gate["name"] == "official_platform_compliance"
    )
    assert official_gate["status"] == "passed"
    assert official_gate["metrics"]["classification"] == "passed_or_pending"


def test_tool_eval_national_backend_blocks_without_samples(tmp_path, monkeypatch):
    bot_a = tmp_path / "CallA"
    _write_call_bot(bot_a)
    profile = get_workflow_profile("national_primary")

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
        precommit_plan=_backend_plan([], hands=70, matches=8),
        evaluation_contract=_backend_evaluation_contract(),
    ))
    result = json.loads(wrapped["content"][0]["text"])

    assert result["passed"] is False
    assert result["paired_bootstrap"]["net_chips_samples"] == 0
    assert result["failure_class"] == "regression"
    assert result["scorecard"]["gates"][0]["status"] == "failed"
    assert {blocker["reason"] for blocker in result["blockers"]} == {"national_no_samples"}
    assert recorded["name"] == "precommit_eval"
    assert recorded["stage"] == "precommit_failed"
