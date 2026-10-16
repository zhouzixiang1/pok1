"""Precommit plan leak-surface guard regressions (2026-10-11).

The frozen precommit plan now carries the deck-seed nonce, so the normal
result payload / checkpoint gate_results must NOT embed the plan.  The two
remaining legitimate carriers are the PRECOMMIT CONTRACT DRIFT pause payload
and the checkpoint audit_context copy that commit re-proves.  The rework
feedback extractor must never surface seed/nonce fields.
"""

import json
from pathlib import Path

import pytest
from types import SimpleNamespace

import national_native
import national_runtime_probe
import precommit_eval_contract as contract
import tool_eval
import tool_helpers
from bot_namespace import bot_name, parse_bot_version
from web.tests.runtime_probe_fixtures import passing_runtime_probe

# The pool-weakest admission-leg match count frozen by the harness (handler
# request n_games=6 is the plan-wide default; the pool leg gets the deeper
# calibrated default from POK_PRECOMMIT_POOL_WEAKEST_N_GAMES).
from tool_eval import PRECOMMIT_POOL_WEAKEST_N_GAMES  # noqa: E402

PRECOMMIT_POOL_WEAKEST_TEST_N_GAMES = PRECOMMIT_POOL_WEAKEST_N_GAMES


def _published(path: Path) -> dict:
    return {
        "published": True,
        "artifact_hash": str(path.name)[-1] * 64,
        "tag": f"national-cloud-bot-v{parse_bot_version(str(path.name))}",
        "tag_type": "tag",
        "tag_object": "b" * 40,
        "commit_oid": "c" * 40,
        "completion_tree_oid": "d" * 40,
        "main_tree_oid": "d" * 40,
        "issues": [],
    }


def _harness(monkeypatch, tmp_path, *, weakest_wins=14, weakest_losses=0):
    bots = tmp_path / "bots"
    for version in (9, 8, 11):
        bot_dir = bots / bot_name(version)
        bot_dir.mkdir(parents=True)
        (bot_dir / "national_bot.py").write_text("# native\n", encoding="utf-8")

    checkpoint = {
        "next_v": 9,
        "source_v": 8,
        "stage": "critic_checked",
        "audit_context": {},
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "workflow_profile_id": "national_native",
                "national_execution_mode": "native_tcp",
                "national_native_contract_ok": True,
                "code_fingerprint": "candidate-code",
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
    profile = SimpleNamespace(
        profile_id="national_native",
        evaluation_protocol="national",
        national_execution_mode="native_tcp",
        national_precommit_hands=70,
        national_precommit_matches=6,
    )
    writes = []

    monkeypatch.delenv("POK_NATIONAL_PRECOMMIT_MATCHES", raising=False)
    monkeypatch.delenv("POK_PRECOMMIT_POOL_WEAKEST_N_GAMES", raising=False)
    monkeypatch.delenv("POK_POOL_GATE_MAX_SCORE", raising=False)
    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setattr(tool_eval, "get_workflow_profile", lambda: profile)
    monkeypatch.setattr(tool_eval, "get_bot_dir", lambda v: bots / bot_name(v))
    monkeypatch.setattr(tool_eval, "_matching_checkpoint", lambda *_: checkpoint)
    monkeypatch.setattr(
        tool_eval, "_prepare_official_profile_refresh", lambda *_: {"ok": True}
    )
    monkeypatch.setattr(tool_eval, "append_candidate_event", None)
    monkeypatch.setattr(tool_eval, "candidate_observability_identity", None)
    monkeypatch.setattr("tool_gates._bot_code_fingerprint", lambda _p: "candidate-code")
    monkeypatch.setattr(
        tool_eval,
        "_select_precommit_opponents",
        lambda *_a, **_k: [
            {"name": bot_name(8), "reason": "parent"},
            {"name": bot_name(11), "reason": "pool_weakest_gate"},
        ],
    )
    monkeypatch.setattr(
        contract,
        "published_bot_identity",
        lambda path: _published(Path(path)),
    )

    def fake_write(_v, _source_v, stage, **kwargs):
        writes.append({"stage": stage, **kwargs})
        checkpoint["stage"] = stage
        if kwargs.get("audit_context"):
            checkpoint["audit_context"].update(kwargs["audit_context"])
        if kwargs.get("gate_results"):
            checkpoint.setdefault("gate_results", {}).update(kwargs["gate_results"])
        if kwargs.get("precommit_attempt") is not None:
            checkpoint["precommit_attempt"] = kwargs["precommit_attempt"]
        return True

    # _record_gate lives in tool_helpers and resolves both the checkpoint
    # matcher and the checkpoint writer in ITS OWN namespace, so the fake
    # write must be pinned on both modules for the real gate write to flow.
    monkeypatch.setattr(tool_eval, "write_pipeline_checkpoint", fake_write)
    monkeypatch.setattr(tool_helpers, "write_pipeline_checkpoint", fake_write)
    monkeypatch.setattr(tool_helpers, "_matching_checkpoint", lambda *_: checkpoint)

    async def fake_native(candidate, opponents, *, hands, matches_per_opponent,
                          sample_plan, **_kwargs):
        matchups = []
        total_wins = total_losses = total_draws = 0
        gate_chips = []
        plan_by_opponent = {}
        for row in sample_plan:
            plan_by_opponent.setdefault(row["opponent"], []).append(row)
        for index, item in enumerate(opponents):
            name = item["name"]
            # Per-opponent frozen counts (2026-10-16 pool-leg calibration):
            # the pool-weakest leg freezes its own deeper ``matches``.
            item_matches = int(item.get("matches") or matches_per_opponent)
            if name == bot_name(11):
                wins, losses = weakest_wins, weakest_losses
            else:
                wins, losses = item_matches, 0
            draws = item_matches - wins - losses
            chips = []
            repeats = []
            for row in plan_by_opponent.get(name, []):
                sign = 1 if len(chips) < wins else (-1 if len(chips) < wins + losses else 0)
                chips.append(sign * (100 + 50 * len(chips)))
                repeats.append({
                    "repeat": row["repeat"],
                    "deck_seed_base": row["deck_seed_base"],
                    "bot_seed_base": row["bot_seed_base"],
                    "local_runtime_budget": {
                        "timing_plan_digest": row[
                            "native_match_timing_plan_digest"
                        ],
                    },
                })
            matchups.append({
                "opponent": name,
                "reason": item.get("reason"),
                "wins": wins,
                "losses": losses,
                "draws": draws,
                "net_chips": chips,
                "repeats": repeats,
            })
            total_wins += wins
            total_losses += losses
            total_draws += draws
            gate_chips.extend(chips)
        return {
            "evaluation_protocol": "national",
            "candidate": bot_name(9),
            "opponents": opponents,
            "matchups": matchups,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "total_draws": total_draws,
            "paired_bootstrap": {
                "protocol": "national",
                "hands_per_match": hands,
                "matches_per_opponent": matches_per_opponent,
                "net_chips_samples": len(gate_chips),
                "strength_net_chips_samples": len(gate_chips),
                "net_chips_mean": 123.0,
            },
            "blockers": [],
            "passed": True,
        }

    monkeypatch.setattr(national_native, "run_native_precommit", fake_native)
    return checkpoint, writes


def _tool_payload(result):
    return json.loads(result["content"][0]["text"])


async def _run(monkeypatch, tmp_path, **kwargs):
    _checkpoint, writes = _harness(monkeypatch, tmp_path, **kwargs)
    result = await tool_eval.run_precommit_eval.handler(
        {"version": 9, "source_v": 8, "n_games": 6}
    )
    return _tool_payload(result), writes


@pytest.mark.asyncio
async def test_passing_result_omits_plan_and_freezes_pool_gate(
    monkeypatch, tmp_path
):
    payload, writes = await _run(monkeypatch, tmp_path)

    assert payload["passed"] is True

    frozen_plan = _audit_context_plan(writes)
    gate = frozen_plan["settings"]["pool_gate"]
    assert gate["label"] == bot_name(11)
    assert gate["min_samples"] == 6
    # Pool-leg threshold derivation (2026-10-16): the pool-weakest leg froze
    # its own 24-match depth, so the code-default score threshold scales to
    # 13/24 — pass boundary W>=14, not bare majority.
    assert gate["max_score"] == tool_eval._pool_gate_max_score_for_matches(
        PRECOMMIT_POOL_WEAKEST_TEST_N_GAMES
    )
    assert gate["tie_ci_lower_min"] == 0.0
    assert frozen_plan["settings"]["seed_derivation"] == "nonce_sha256_v1"
    assert frozen_plan["settings"]["publish_mode"] == "pool_ratchet"
    assert frozen_plan["settings"]["deck_seed_nonce"]
    # The pool leg froze 24 matches while the parent leg kept the requested 6.
    assert frozen_plan["settings"]["matches_by_opponent"] == {
        bot_name(8): 6,
        bot_name(11): PRECOMMIT_POOL_WEAKEST_TEST_N_GAMES,
    }
    # Receipts record the per-opponent games that actually ran.
    assert payload["matches_by_opponent"] == {
        bot_name(8): 6,
        bot_name(11): PRECOMMIT_POOL_WEAKEST_TEST_N_GAMES,
    }
    assert payload["expected_net_chips_samples"] == (
        6 + PRECOMMIT_POOL_WEAKEST_TEST_N_GAMES
    )

    # Leak guard: neither the result payload nor the checkpoint gate_results
    # copy carries the frozen plan (nonce surface).
    assert "precommit_eval_plan" not in payload
    gate_writes = [
        write for write in writes if write.get("gate_results", {}).get("precommit_eval")
    ]
    assert gate_writes, "expected the verified gate write"
    recorded_gate = gate_writes[-1]["gate_results"]["precommit_eval"]
    assert "precommit_eval_plan" not in recorded_gate
    assert "deck_seed_nonce" not in json.dumps(recorded_gate)
    # The audit_context copy (commit authority) still carries the full plan.
    assert frozen_plan["plan_digest"]


@pytest.mark.asyncio
async def test_pool_weakest_gate_blocks_publication_from_frozen_settings(
    monkeypatch, tmp_path
):
    payload, writes = await _run(
        monkeypatch, tmp_path, weakest_wins=0, weakest_losses=2
    )

    assert payload["passed"] is False
    reasons = [
        item.get("reason") for item in payload["blockers"]
        if isinstance(item, dict)
    ]
    assert "did_not_beat_pool_weakest" in reasons
    assert payload["first_strict_control_gate"] is None
    # The failed stage write still records the gate result without the plan.
    gate_writes = [
        write for write in writes if write.get("gate_results", {}).get("precommit_eval")
    ]
    assert "precommit_eval_plan" not in gate_writes[-1]["gate_results"]["precommit_eval"]


def _audit_context_plan(writes):
    for write in writes:
        plan = (write.get("audit_context") or {}).get("precommit_eval_plan")
        if plan:
            return plan
    raise AssertionError("audit_context plan write not found")


@pytest.mark.asyncio
async def test_contract_drift_pause_still_carries_the_plan(monkeypatch, tmp_path):
    """The drift branch keeps its diagnostic copy of the frozen plan."""
    _checkpoint, writes = _harness(monkeypatch, tmp_path)
    checkpoint = tool_eval._matching_checkpoint(9, 8)
    # Freeze a stored plan, then tamper its digest: the next invocation must
    # pause with PRECOMMIT CONTRACT DRIFT and embed the stored plan.
    result = await tool_eval.run_precommit_eval.handler(
        {"version": 9, "source_v": 8, "n_games": 6}
    )
    stored = dict(checkpoint["audit_context"]["precommit_eval_plan"])
    stored["plan_digest"] = "0" * 64
    checkpoint["audit_context"]["precommit_eval_plan"] = stored
    # Reset the recorded verified gate so the idempotency guard does not
    # short-circuit before the drift check.
    checkpoint["stage"] = "critic_checked"
    checkpoint["gate_results"].pop("precommit_eval", None)

    result = await tool_eval.run_precommit_eval.handler(
        {"version": 9, "source_v": 8, "n_games": 6}
    )
    payload = _tool_payload(result)

    assert payload["error"].startswith("PRECOMMIT CONTRACT DRIFT")
    assert payload["precommit_eval_plan"] == stored


def test_rework_feedback_extractor_never_surfaces_seed_fields(monkeypatch, tmp_path):
    from tool_planning_quality_repair_targets import _precommit_failure_items

    gate = {
        "directive": "National precommit FAILED — rework.",
        "blockers": [{"reason": "did_not_beat_pool_weakest", "details": "0W-2L"}],
        "failures": ["national_precommit_regression"],
        "matchups": [
            {"opponent": bot_name(11), "reason": "pool_weakest_gate",
             "wins": 0, "losses": 2, "net_chips": [-120, -80]},
        ],
    }
    items = _precommit_failure_items({
        "gate_results": {"precommit_eval": gate},
        "reviewer_feedback": "",
    })
    joined = "\n".join(items)
    assert "did_not_beat_pool_weakest" in joined
    assert "deck_seed" not in joined
    assert "nonce" not in joined
    assert "precommit_eval_plan" not in joined


def test_pool_weakest_leg_floor_is_gate_minimum(monkeypatch):
    # Red-team 2026-10-16 config hole: a pool leg below the gate's own
    # POOL_GATE_MIN_SAMPLES never engages the blocker branch (silent no-op).
    import tool_eval

    monkeypatch.setenv("POK_PRECOMMIT_POOL_WEAKEST_N_GAMES", "5")
    with pytest.raises(ValueError, match="pool_weakest_n_games_invalid"):
        tool_eval.precommit_pool_weakest_n_games()
    monkeypatch.setenv("POK_PRECOMMIT_POOL_WEAKEST_N_GAMES", "6")
    assert tool_eval.precommit_pool_weakest_n_games() == 6
