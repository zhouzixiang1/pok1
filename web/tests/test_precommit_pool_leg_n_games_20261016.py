"""Pool-weakest admission-leg sample depth regressions (2026-10-16).

The pool-ratchet admission leg freezes its own match count
(``POK_PRECOMMIT_POOL_WEAKEST_N_GAMES``, default 24) while the parent / v1 /
top legs keep ``PRECOMMIT_DEFAULT_N_GAMES`` (12) — the parent gate semantics
are unchanged.  With the derived pool-leg score threshold
(``_pool_gate_max_score_for_matches``) the pass boundary is W>=14/24:
parity false-admission P(W>=14 | p=0.5, n=24) = 0.2706 (the calibration's
0.271), and the decisive-win power at a true 0.55 edge is P = 0.4539.
"""

from math import comb
from pathlib import Path

import pytest

import precommit_eval_contract as contract
import tool_eval
from bot_namespace import bot_name, parse_bot_version
import pool_ratchet_gate


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


# ── Resolver ────────────────────────────────────────────────────────────


def test_pool_weakest_n_games_default_and_env_override(monkeypatch):
    monkeypatch.delenv("POK_PRECOMMIT_POOL_WEAKEST_N_GAMES", raising=False)
    assert tool_eval.precommit_pool_weakest_n_games() == 24
    assert tool_eval.PRECOMMIT_POOL_WEAKEST_N_GAMES == 24
    monkeypatch.setenv("POK_PRECOMMIT_POOL_WEAKEST_N_GAMES", "16")
    assert tool_eval.precommit_pool_weakest_n_games() == 16
    for bad in ("0", "3", "49", "abc", "-1"):
        monkeypatch.setenv("POK_PRECOMMIT_POOL_WEAKEST_N_GAMES", bad)
        with pytest.raises(ValueError):
            tool_eval.precommit_pool_weakest_n_games()


# ── Threshold derivation ────────────────────────────────────────────────


def test_derived_max_score_for_24_matches_is_13_over_24():
    assert tool_eval._pool_gate_max_score_for_matches(24) == 13 / 24
    # General formula: pass boundary at floor(n/2) + 2 decisive wins.
    assert tool_eval._pool_gate_max_score_for_matches(12) == 7 / 12


def test_threshold_binomial_derivation_w14_of_24():
    """W>=14/24 is the pass boundary: parity false-admission 0.2706 (the
    calibration's 0.271); decisive-win power at a 0.55 edge is 0.4539."""
    def p_ge(k, n, p):
        q = 1 - p
        return sum(comb(n, i) * p**i * q ** (n - i) for i in range(k, n + 1))

    # The smallest W with W/24 strictly greater than the derived threshold.
    threshold = tool_eval._pool_gate_max_score_for_matches(24)
    w_min = next(w for w in range(1, 25) if w / 24 > threshold)
    assert w_min == 14
    assert round(p_ge(14, 24, 0.5), 4) == 0.2706
    assert round(p_ge(14, 24, 0.55), 4) == 0.4539
    # W=13 sits exactly AT the threshold (exact tie -> chip-CI rescue path).
    assert 13 / 24 == threshold


def _pool_settings(n_games=24, max_score=None):
    return {
        "pool_gate": {
            "label": bot_name(11),
            "min_samples": 6,
            "max_score": (
                max_score
                if max_score is not None
                else tool_eval._pool_gate_max_score_for_matches(n_games)
            ),
            "tie_ci_lower_min": 0.0,
        }
    }


def _matchup(wins, losses, draws, net_chips=None):
    return {
        "opponent": bot_name(11),
        "reason": "pool_weakest_gate",
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "net_chips": list(net_chips or []),
    }


def test_pool_gate_boundary_w14_passes_w13_blocks():
    # 14W-10L: score 14/24 > 13/24 -> passes.
    blockers, _ = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(14, 10, 0)], bot_name(11), _pool_settings()
    )
    assert blockers == []
    # 13W-11L without a chip edge: exact tie at 13/24 -> still a regression.
    blockers, summary = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(13, 11, 0, net_chips=[-100] * 24)], bot_name(11),
        _pool_settings(),
    )
    assert [b["reason"] for b in blockers] == ["did_not_beat_pool_weakest"]
    assert summary["per_matchup"][0]["samples"] == 24
    # 12W-12L is below the derived threshold: a plain loss, and positive
    # chips cannot rescue it because it is not an exact tie at 13/24.
    blockers, _ = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(12, 12, 0, net_chips=[500] * 24)], bot_name(11),
        _pool_settings(),
    )
    assert [b["reason"] for b in blockers] == ["did_not_beat_pool_weakest"]
    assert "net-chip CI lower" not in blockers[0]["details"]


def test_pool_gate_exact_tie_at_derived_threshold_keeps_chip_ci_rescue():
    chips_positive = [400, 500, 300, 600, 200, 800] * 4
    blockers, _ = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(13, 11, 0, net_chips=chips_positive)], bot_name(11),
        _pool_settings(),
    )
    assert blockers == []
    blockers, _ = pool_ratchet_gate.pool_gate_blockers(
        [_matchup(13, 11, 0, net_chips=[-v for v in chips_positive])],
        bot_name(11), _pool_settings(),
    )
    assert [b["reason"] for b in blockers] == ["did_not_beat_pool_weakest"]


# ── Plan freeze: per-opponent match counts ──────────────────────────────


def _make_pool_leg_plan(tmp_path, monkeypatch, *, pool_matches=24,
                        default_matches=12, annotate=True):
    opponents = []
    identities = {}
    for version, reason in ((8, "parent"), (11, "pool_weakest_gate")):
        path = tmp_path / bot_name(version)
        path.mkdir()
        (path / "national_bot.py").write_text("# native\n", encoding="utf-8")
        item = {"name": path.name, "reason": reason}
        if annotate and reason == "pool_weakest_gate":
            item["matches"] = pool_matches
        opponents.append(item)
        identities[str(path)] = _published(
            path, artifact_hash=str(version) * 64
        )
    monkeypatch.setattr(
        contract,
        "published_bot_identity",
        lambda path: dict(identities[str(Path(path))]),
    )
    plan = contract.create_precommit_plan(
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
        opponents=opponents,
        hands_per_match=70,
        matches_per_opponent=default_matches,
        path_resolver=lambda item: tmp_path / item["name"],
        require_published_opponents=True,
        deck_seed_base=91_000,
    )
    return plan


def _validate(plan):
    return contract.validate_precommit_plan(
        plan,
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
    )


def test_pool_leg_freezes_24_while_other_legs_keep_12(tmp_path, monkeypatch):
    plan = _make_pool_leg_plan(tmp_path, monkeypatch)

    per_opponent = {}
    for row in plan["sample_plan"]:
        per_opponent.setdefault(row["opponent"], []).append(row)
    assert len(per_opponent[bot_name(8)]) == 12
    assert len(per_opponent[bot_name(11)]) == 24
    assert plan["settings"]["matches_per_opponent"] == 12
    assert plan["settings"]["matches_by_opponent"] == {
        bot_name(8): 12,
        bot_name(11): 24,
    }
    assert {
        item["name"]: item["matches"] for item in plan["opponents"]
    } == {bot_name(8): 12, bot_name(11): 24}
    assert _validate(plan) == []


def test_uniform_plan_without_matches_keeps_one_count(tmp_path, monkeypatch):
    """No per-opponent annotation (legacy-mode shape): every leg keeps the
    plan-wide count — the parent/v1 leg semantics are unchanged."""
    plan = _make_pool_leg_plan(tmp_path, monkeypatch, annotate=False)

    assert plan["settings"]["matches_by_opponent"] == {
        bot_name(8): 12,
        bot_name(11): 12,
    }
    assert len(plan["sample_plan"]) == 24
    assert _validate(plan) == []


def test_matches_by_opponent_tamper_fails_validation(tmp_path, monkeypatch):
    plan = _make_pool_leg_plan(tmp_path, monkeypatch)
    plan["settings"]["matches_by_opponent"][bot_name(11)] = 12
    plan["plan_digest"] = contract.canonical_digest(
        {k: v for k, v in plan.items() if k != "plan_digest"}
    )
    issues = _validate(plan)
    # The per-opponent record is re-proved from the plan's own opponents
    # (which still carry matches=24), so the settings drift is caught
    # precisely by the matches_by_opponent check while the sample plan —
    # consistent with those opponents — still validates.
    assert issues == ["precommit_matches_by_opponent_mismatch"]


def test_native_runner_row_count_proof_uses_per_opponent_sum(
    tmp_path, monkeypatch
):
    """run_native_precommit's fast-fail sample-plan row proof expects
    sum(per-opponent counts), not len(opponents) * plan-wide count."""
    import asyncio
    from pathlib import Path

    import national_native
    from precommit_eval_contract import build_sample_plan

    monkeypatch.setattr(
        national_native,
        "resolve_bot",
        lambda value: (Path(value).name, Path(value)),
    )
    candidate = tmp_path / bot_name(9)
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    opponents = [
        {"name": bot_name(8), "reason": "parent"},
        {
            "name": bot_name(11),
            "reason": "pool_weakest_gate",
            "matches": 24,
        },
    ]
    sample_plan = build_sample_plan(opponents, 12, deck_seed_base=91_000)
    assert len(sample_plan) == 36
    assert sum(1 for row in sample_plan if row["opponent"] == bot_name(8)) == 12
    assert sum(1 for row in sample_plan if row["opponent"] == bot_name(11)) == 24

    # A uniform 12+12-row plan does NOT satisfy the declared 24-deep pool leg:
    # the proof must expect 12 + 24 = 36 rows and reject before any launch.
    uniform_plan = build_sample_plan(
        [{"name": bot_name(8), "reason": "parent"},
         {"name": bot_name(11), "reason": "pool_weakest_gate"}],
        12,
        deck_seed_base=91_000,
    )
    assert len(uniform_plan) == 24

    async def run(plan):
        return await national_native.run_native_precommit(
            candidate,
            opponents,
            hands=70,
            matches_per_opponent=12,
            sample_plan=plan,
        )

    with pytest.raises(ValueError, match="expected 36"):
        asyncio.run(run(uniform_plan))


# ── Gate-spec derivation at the freeze moment ───────────────────────────


def test_build_pool_gate_spec_derives_threshold_from_pool_leg(monkeypatch):
    monkeypatch.delenv("POK_POOL_GATE_MAX_SCORE", raising=False)
    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    opponents = [
        {"name": bot_name(8), "reason": "parent"},
        {
            "name": bot_name(11),
            "reason": "pool_weakest_gate",
            "matches": 24,
        },
    ]
    spec, error = tool_eval._build_pool_gate_spec(opponents, first_strict=False)
    assert error is None
    assert spec["label"] == bot_name(11)
    assert spec["max_score"] == 13 / 24
    assert spec["min_samples"] == 6


def test_explicit_max_score_env_keeps_operator_authority(monkeypatch):
    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_GATE_MAX_SCORE", "0.55")
    opponents = [
        {
            "name": bot_name(11),
            "reason": "pool_weakest_gate",
            "matches": 24,
        },
    ]
    spec, error = tool_eval._build_pool_gate_spec(opponents, first_strict=False)
    assert error is None
    assert spec["max_score"] == 0.55
