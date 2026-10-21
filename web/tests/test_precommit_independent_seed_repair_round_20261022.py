"""One pre-registered independent-seed precommit repair round (2026-10-22).

Context: v589 precommit (12/24/12 = 48 native 70-hand matches, 20W-28L,
parent 4-8) was abandoned because ``POK_MAX_PRECOMMIT_REWORK_ROUNDS=0``.  The
old rework path replayed the checkpoint-frozen plan, so a same-seed rerun was
provably zero-gain (v558/v560/v568/v571/v573).

Contract under test:

* A Workers repair round (checkpoint ``precommit_rework_count >= 1``) makes
  the next precommit rebuild the frozen plan with a FRESH one-time nonce;
  opponents, per-opponent sample depths, evaluator identity and every gate
  threshold are carried over verbatim.
* Exactly two independent samples are allowed per generation (round 1 +
  round 1 repair).  No retry-until-pass: a third round is refused.
* Infrastructure retries keep the SAME plan (a harness stall is not a policy
  result and never buys a fresh sample).
* Both attempts' complete scorecards are persisted in the checkpoint
  attempt history (and the append-only candidate ledger).
* Passing still requires the unchanged native precommit gate to pass.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import pool_ratchet_gate
import precommit_eval_contract as contract
import strength_order
import tool_eval
import national_runtime_probe
from bot_namespace import bot_name, parse_bot_version
from web.tests.runtime_probe_fixtures import passing_runtime_probe


def _bn(version: int) -> str:
    return bot_name(version)


def _published(path: Path, *, artifact_hash: str = "a" * 64) -> dict:
    return {
        "published": True,
        "artifact_hash": artifact_hash,
        "tag": f"national-cloud-bot-v{parse_bot_version(str(path.name))}",
        "tag_type": "tag",
        "tag_object": "b" * 40,
        "commit_oid": "c" * 40,
        "completion_tree_oid": "d" * 40,
        "main_tree_oid": "d" * 40,
        "issues": [],
    }


def _install_bots(tmp_path, monkeypatch, versions=(9, 8, 5, 4)):
    bots = tmp_path / "bots"
    bots.mkdir()
    identities = {}
    for version in versions:
        path = bots / _bn(version)
        path.mkdir()
        (path / "national_bot.py").write_text("# native\n", encoding="utf-8")
        identities[str(path)] = _published(
            path, artifact_hash=str(version) * 64
        )
    monkeypatch.setattr(
        contract,
        "published_bot_identity",
        lambda path: dict(identities[str(Path(path))]),
    )
    return bots


def _nonce_sequence(monkeypatch, nonces):
    pending = list(nonces)

    def fake_nonce():
        if not pending:
            raise AssertionError("unexpected extra seed nonce draw")
        return pending.pop(0)

    monkeypatch.setattr(
        pool_ratchet_gate, "generate_deck_seed_nonce", fake_nonce
    )


def _make_round1_plan(tmp_path, monkeypatch, *, nonce="a" * 32):
    _nonce_sequence(monkeypatch, [nonce])
    opponents = [
        {"name": _bn(8), "reason": "parent", "matches": 12},
        {"name": _bn(5), "reason": "top_strength"},
    ]
    return contract.create_precommit_plan(
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
        opponents=opponents,
        hands_per_match=70,
        matches_per_opponent=12,
        path_resolver=lambda item: tmp_path / "bots" / item["name"],
        require_published_opponents=True,
        seed_round=1,
    )


def _validate(plan):
    return contract.validate_precommit_plan(
        plan,
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
    )


def _seed_free(value):
    """Strip seed-bearing fields (the plan is never pasted into prompts)."""

    if isinstance(value, dict):
        return {
            key: _seed_free(item)
            for key, item in value.items()
            if key not in {"deck_seed_nonce", "deck_seed_base", "bot_seed_base"}
        }
    if isinstance(value, list):
        return [_seed_free(item) for item in value]
    return value


# ── Plan rebuild: fresh seed, identical gates ───────────────────────────


def test_repair_round_rebuilds_with_fresh_nonce_and_identical_gates(
    tmp_path, monkeypatch
):
    bots = _install_bots(tmp_path, monkeypatch)
    plan1 = _make_round1_plan(tmp_path, monkeypatch)
    assert plan1["settings"]["precommit_seed_round"] == 1
    assert contract.plan_seed_round(plan1) == 1
    assert _validate(plan1) == []

    _nonce_sequence(monkeypatch, ["b" * 32])
    plan2 = contract.rebuild_precommit_plan_for_seed_round(
        plan1,
        seed_round=2,
        path_resolver=lambda item: bots / item["name"],
    )

    assert plan2["settings"]["precommit_seed_round"] == 2
    assert plan2["settings"]["deck_seed_nonce"] == "b" * 32
    assert plan2["settings"]["deck_seed_nonce"] != plan1["settings"]["deck_seed_nonce"]
    assert plan2["settings"]["deck_seed_base"] != plan1["settings"]["deck_seed_base"]
    assert plan2["plan_digest"] != plan1["plan_digest"]
    # The repair sample must not replay the failed deck schedule.
    assert plan2["sample_plan"] != plan1["sample_plan"]
    assert _validate(plan2) == []

    # Opponents, per-opponent depths, evaluator identity and every gate
    # threshold are carried over verbatim (fixed alpha, no lowering).
    assert plan2["opponents"] == plan1["opponents"]
    assert plan2["settings"]["matches_by_opponent"] == plan1["settings"][
        "matches_by_opponent"
    ]
    assert plan2["evaluator_identity"] == plan1["evaluator_identity"]
    for key in (
        "parent_min_samples",
        "parent_max_score",
        "aggregate_min_samples",
        "aggregate_min_loss_margin",
        "gate_profile_id",
        "strength_evidence_required",
        "pool_gate",
        "publish_mode",
    ):
        assert plan2["settings"][key] == plan1["settings"][key]
    assert strength_order.PRECOMMIT_AGGREGATE_MIN_SAMPLES == 8
    assert strength_order.PRECOMMIT_AGGREGATE_MIN_LOSS_MARGIN == 2
    assert strength_order.PRECOMMIT_PARENT_MAX_SCORE == 0.50


def test_seed_round_is_validated_and_tamper_evident(tmp_path, monkeypatch):
    _install_bots(tmp_path, monkeypatch)
    plan = _make_round1_plan(tmp_path, monkeypatch)

    forged = dict(plan)
    forged["settings"] = {
        **plan["settings"],
        contract.PRECOMMIT_SEED_ROUND_SETTINGS_KEY: 0,
    }
    forged["plan_digest"] = contract.canonical_digest(
        {key: value for key, value in forged.items() if key != "plan_digest"}
    )
    assert "precommit_seed_round_invalid" in _validate(forged)

    # Legacy plans without the key are round 1 and stay valid.
    legacy = dict(plan)
    legacy["settings"] = {
        key: value
        for key, value in plan["settings"].items()
        if key != contract.PRECOMMIT_SEED_ROUND_SETTINGS_KEY
    }
    legacy["plan_digest"] = contract.canonical_digest(
        {key: value for key, value in legacy.items() if key != "plan_digest"}
    )
    assert _validate(legacy) == []
    assert contract.plan_seed_round(legacy) == 1


# ── Run-time rotation trigger ───────────────────────────────────────────


def _checkpoint_with_plan(plan, *, rework_count, stage="critic_checked"):
    return {
        "next_v": 9,
        "source_v": 8,
        "stage": stage,
        "checkpoint_revision": 5,
        "precommit_attempt": 1,
        "precommit_rework_count": rework_count,
        "workflow_run_id": "generation:9:seed-round",
        "audit_context": {"precommit_eval_plan": plan},
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "national_native_contract_ok": True,
                "national_capability_contract": {
                    "dynamic_runtime_probe": passing_runtime_probe(),
                },
                **national_runtime_probe.runtime_probe_native_template_evidence(),
            },
            "review": {
                "approved": True,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
            },
            "critic": {
                "approved": True,
                "llm_invoked": True,
                "critic_llm_executed": True,
                "schema_valid": True,
            },
        },
    }


def _install_handler_harness(monkeypatch, checkpoint, bots, *, backend_result):
    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
        national_precommit_hands=70,
        national_precommit_matches=12,
    )
    backend_calls = []
    writes = []

    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda version: bots / _bn(version))
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(
        tool_eval, "_prepare_official_profile_refresh", lambda *_: {"ok": True}
    )
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)
    monkeypatch.setattr(tool_eval, "log_system_event", lambda *a, **k: None)
    monkeypatch.setattr(
        "tool_gates._bot_code_fingerprint", lambda _path: "candidate-code"
    )

    def fake_write(_v, _source_v, stage, **kwargs):
        writes.append((stage, kwargs))
        checkpoint["stage"] = stage
        if kwargs.get("audit_context"):
            checkpoint["audit_context"].update(kwargs["audit_context"])
        if kwargs.get("precommit_attempt") is not None:
            checkpoint["precommit_attempt"] = kwargs["precommit_attempt"]
        return True

    async def fake_backend(**kwargs):
        backend_calls.append(kwargs)
        return tool_eval._json_tool_result(backend_result)

    monkeypatch.setattr(tool_eval, "write_pipeline_checkpoint", fake_write)
    monkeypatch.setattr(tool_eval, "_run_national_precommit_backend", fake_backend)
    return backend_calls, writes


def test_rework_rotates_the_plan_once_and_the_next_attempt_uses_it(
    tmp_path, monkeypatch
):
    bots = _install_bots(tmp_path, monkeypatch)
    plan1 = _make_round1_plan(tmp_path, monkeypatch)
    checkpoint = _checkpoint_with_plan(plan1, rework_count=1)
    backend_calls, writes = _install_handler_harness(
        monkeypatch,
        checkpoint,
        bots,
        backend_result={"passed": True, "failure_class": "passed"},
    )
    _nonce_sequence(monkeypatch, ["c" * 32])

    asyncio.run(
        tool_eval.run_precommit_eval.handler(
            {"version": 9, "source_v": 8, "n_games": 12}
        )
    )

    plan2 = backend_calls[0]["precommit_plan"]
    assert plan2["settings"]["precommit_seed_round"] == 2
    assert plan2["settings"]["deck_seed_nonce"] == "c" * 32
    assert plan2["plan_digest"] != plan1["plan_digest"]
    assert plan2["sample_plan"] != plan1["sample_plan"]
    assert plan2["opponents"] == plan1["opponents"]

    # The rotated plan is durably persisted with an exact CAS fence.
    rotation_writes = [
        (stage, kwargs)
        for stage, kwargs in writes
        if (kwargs.get("audit_context") or {}).get("precommit_eval_plan")
    ]
    assert len(rotation_writes) == 1
    stage, kwargs = rotation_writes[0]
    assert stage == "critic_checked"
    assert kwargs["audit_context"]["precommit_eval_plan"] == plan2
    assert kwargs["expected_checkpoint_revision"] == 5
    assert kwargs["expected_checkpoint_stage"] == "critic_checked"
    assert kwargs["expected_workflow_run_id"] == "generation:9:seed-round"
    # The frozen checkpoint authority now carries the rotated plan.
    assert checkpoint["audit_context"]["precommit_eval_plan"] == plan2


def test_second_repair_round_is_refused_no_retry_until_pass(
    tmp_path, monkeypatch
):
    bots = _install_bots(tmp_path, monkeypatch)
    plan1 = _make_round1_plan(tmp_path, monkeypatch)
    checkpoint = _checkpoint_with_plan(plan1, rework_count=2)
    backend_calls, writes = _install_handler_harness(
        monkeypatch,
        checkpoint,
        bots,
        backend_result={"passed": True},
    )
    _nonce_sequence(monkeypatch, ["d" * 32])

    result = asyncio.run(
        tool_eval.run_precommit_eval.handler(
            {"version": 9, "source_v": 8, "n_games": 12}
        )
    )
    payload = json.loads(result["content"][0]["text"])

    assert "PRECOMMIT INDEPENDENT REPAIR ROUND EXHAUSTED" in payload["error"]
    assert payload["passed"] is False
    assert payload["intent"]["kind"] == "abandon"
    assert payload["blockers"][0]["reason"] == "precommit_repair_round_exhausted"
    assert backend_calls == []
    assert [w for w in writes if (w[1].get("audit_context") or {}).get("precommit_eval_plan")] == []
    assert tool_eval.PRECOMMIT_SEED_ROUNDS_MAX == 2
    assert tool_eval.PRECOMMIT_SEED_REPAIR_ROUNDS_MAX == 1


def test_infrastructure_retry_keeps_the_frozen_plan(tmp_path, monkeypatch):
    bots = _install_bots(tmp_path, monkeypatch)
    plan1 = _make_round1_plan(tmp_path, monkeypatch)
    checkpoint = _checkpoint_with_plan(plan1, rework_count=0)
    backend_calls, writes = _install_handler_harness(
        monkeypatch,
        checkpoint,
        bots,
        backend_result={"passed": False, "failure_class": "infrastructure"},
    )
    _nonce_sequence(monkeypatch, [])

    asyncio.run(
        tool_eval.run_precommit_eval.handler(
            {"version": 9, "source_v": 8, "n_games": 12}
        )
    )

    assert backend_calls[0]["precommit_plan"] == plan1
    assert [w for w in writes if (w[1].get("audit_context") or {}).get("precommit_eval_plan")] == []


# ── Two complete scorecards persisted ───────────────────────────────────


def _fake_result(seed_round, *, wins, losses, passed):
    return {
        "passed": passed,
        "failure_class": "passed" if passed else "regression",
        "total_wins": wins,
        "total_losses": losses,
        "total_draws": 0,
        "matches_by_opponent": {_bn(8): 12, _bn(5): 12},
        "blockers": (
            []
            if passed
            else [{"reason": "did_not_beat_parent", "opponent": _bn(8), "details": "x"}]
        ),
        "scorecard": {
            "name": "precommit_eval",
            "primary_score": wins / (wins + losses),
            "gates": [{"name": "national_precommit_regression",
                       "status": "passed" if passed else "failed"}],
        },
        "precommit_seed_round": seed_round,
    }


def test_both_attempts_persist_complete_scorecards(monkeypatch):
    checkpoint = {
        "stage": "precommit_failed",
        "checkpoint_revision": 9,
        "workflow_run_id": "generation:9:history",
        "audit_context": {},
    }
    writes = []

    def fake_write(_v, _s, stage, **kwargs):
        writes.append((stage, kwargs))
        if kwargs.get("audit_context"):
            checkpoint["audit_context"].update(kwargs["audit_context"])
        return True

    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(tool_eval, "write_pipeline_checkpoint", fake_write)
    plan1 = {"plan_digest": "p1", "settings": {"deck_seed_base": 1,
                                               "precommit_seed_round": 1}}
    plan2 = {"plan_digest": "p2", "settings": {"deck_seed_base": 2,
                                               "precommit_seed_round": 2}}

    assert tool_eval._record_precommit_attempt_history(
        9,
        8,
        result=_fake_result(1, wins=4, losses=8, passed=False),
        precommit_plan=plan1,
        precommit_attempt=1,
    )
    assert tool_eval._record_precommit_attempt_history(
        9,
        8,
        result=_fake_result(2, wins=7, losses=5, passed=True),
        precommit_plan=plan2,
        precommit_attempt=2,
    )

    records = writes[-1][1]["audit_context"][
        tool_eval.PRECOMMIT_ATTEMPT_HISTORY_KEY
    ]
    assert [record["seed_round"] for record in records] == [1, 2]
    assert [record["attempt"] for record in records] == [1, 2]
    assert records[0]["plan_digest"] == "p1"
    assert records[1]["plan_digest"] == "p2"
    assert records[0]["scorecard"]["primary_score"] == pytest.approx(4 / 12)
    assert records[1]["scorecard"]["primary_score"] == pytest.approx(7 / 12)
    assert records[0]["passed"] is False and records[1]["passed"] is True
    assert records[0]["blockers"][0]["reason"] == "did_not_beat_parent"
    # The write is fenced by the exact checkpoint CAS identity.
    assert writes[-1][1]["expected_checkpoint_revision"] == 9
    assert writes[-1][1]["expected_checkpoint_stage"] == "precommit_failed"
    assert writes[-1][1]["expected_workflow_run_id"] == "generation:9:history"


def test_attempt_history_is_round_keyed_and_bounded(monkeypatch):
    checkpoint = {
        "stage": "precommit_failed",
        "checkpoint_revision": 1,
        "workflow_run_id": "generation:9:bounded",
        "audit_context": {},
    }
    writes = []

    def fake_write(_v, _s, stage, **kwargs):
        writes.append((stage, kwargs))
        if kwargs.get("audit_context"):
            checkpoint["audit_context"].update(kwargs["audit_context"])
        return True

    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(tool_eval, "write_pipeline_checkpoint", fake_write)

    for attempt in range(1, 6):
        plan = {
            "plan_digest": f"p{attempt}",
            "settings": {"precommit_seed_round": attempt},
        }
        tool_eval._record_precommit_attempt_history(
            9,
            8,
            result=_fake_result(attempt, wins=attempt, losses=1, passed=False),
            precommit_plan=plan,
            precommit_attempt=attempt,
        )
    records = checkpoint["audit_context"][
        tool_eval.PRECOMMIT_ATTEMPT_HISTORY_KEY
    ]
    # Bounded, newest-last, one record per seed round.
    assert len(records) == tool_eval.PRECOMMIT_ATTEMPT_HISTORY_MAX
    assert [record["seed_round"] for record in records] == [2, 3, 4, 5]


# ── Passing is still the unchanged native precommit gate ────────────────


def test_rotated_plan_keeps_the_unchanged_gate_verdict():
    matchup = {
        "opponent": _bn(8),
        "reason": "parent",
        "wins": 4,
        "losses": 8,
        "draws": 0,
        "net_chips": [-100] * 12,
    }
    blockers, _summary = strength_order.precommit_outcome_blockers(
        [matchup], parent_label=_bn(8)
    )
    reasons = [blocker["reason"] for blocker in blockers]
    assert "did_not_beat_parent" in reasons
    assert "aggregate_native_regression" in reasons

    matchup["wins"], matchup["losses"] = 7, 5
    matchup["net_chips"] = [100] * 12
    blockers, _summary = strength_order.precommit_outcome_blockers(
        [matchup], parent_label=_bn(8)
    )
    assert blockers == []


# ── Task B: parent-leg sample depth knob ────────────────────────────────


def test_parent_leg_depth_env_resolution(monkeypatch):
    monkeypatch.delenv("POK_PRECOMMIT_PARENT_N_GAMES", raising=False)
    # Unset means "inherit the plan-wide count" (historical behavior).
    assert tool_eval.precommit_parent_n_games() is None
    monkeypatch.setenv("POK_PRECOMMIT_PARENT_N_GAMES", "24")
    assert tool_eval.precommit_parent_n_games() == 24
    for bad in ("0", "5", "49", "abc", "-1"):
        monkeypatch.setenv("POK_PRECOMMIT_PARENT_N_GAMES", bad)
        with pytest.raises(ValueError):
            tool_eval.precommit_parent_n_games()


def test_parent_leg_depth_is_frozen_per_opponent(tmp_path, monkeypatch):
    _install_bots(tmp_path, monkeypatch)
    opponents = [
        {"name": _bn(8), "reason": "parent", "matches": 24},
        {"name": _bn(5), "reason": "top_strength"},
    ]
    plan = contract.create_precommit_plan(
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
        opponents=opponents,
        hands_per_match=70,
        matches_per_opponent=12,
        path_resolver=lambda item: tmp_path / "bots" / item["name"],
        require_published_opponents=True,
        deck_seed_base=91_000,
    )
    assert plan["settings"]["matches_by_opponent"] == {
        _bn(8): 24,
        _bn(5): 12,
    }
    assert sum(1 for row in plan["sample_plan"] if row["opponent"] == _bn(8)) == 24
    assert sum(1 for row in plan["sample_plan"] if row["opponent"] == _bn(5)) == 12
    assert _validate(plan) == []
    # No threshold is lowered by a deeper sample.
    assert plan["settings"]["parent_max_score"] == 0.50
    assert plan["settings"]["aggregate_min_samples"] == 8
    assert plan["settings"]["aggregate_min_loss_margin"] == 2
