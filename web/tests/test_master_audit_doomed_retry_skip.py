"""Doomed Master-audit retry skip (2026-09-13).

When a deterministic audit rejection is ONLY the statistical evidence floor
(every contradiction is the shared ``proposal_cited_sample_too_small``
token) and NO row anywhere in the pool could satisfy either tier, a
corrective re-plan is mathematically unable to fix the citation dimension —
it would burn the second Master run and fail identically. The dispatch loop
then skips the retry and routes straight through the existing
MASTER_AUDIT_REJECTED abandon path, appending the
``evidence_floor_unsatisfiable`` token to the rejection reason (and to the
audit error list) and emitting an operator event that asks the rating daemon
for more samples.

As long as ANY pool row could satisfy the tiers (or the rejection is mixed
/ non-statistical / from the LLM auditor / the pool is unreadable), the
skip must NOT fire and the corrective retry runs normally.
"""

import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from bot_namespace import bot_name  # noqa: E402


def _row(games):
    a = games // 2
    return {"games": games, "a_wins": a, "b_wins": games - a, "draws": 0}


def _pair_key(a, b):
    return f"{bot_name(a)} vs {bot_name(b)}"


def _floor_token(matchup_token, cited, best, tier, aggregate):
    return (
        "proposal_cited_sample_too_small"
        f".{matchup_token}"
        f".cited.{cited}.best_available.{best}.tier.{tier}"
        f".and_aggregate.{aggregate}"
        ".aggregate_sources.bot_stats.selection_snapshot"
    )


def _bundle(h2h=None, bot_stats=None, selection=None):
    bundle = {"available": True}
    if h2h is not None:
        bundle["h2h"] = h2h
    if bot_stats is not None:
        bundle["bot_stats"] = bot_stats
    if selection is not None:
        bundle["selection"] = selection
    return bundle


def _audit(*contradictions):
    return {
        "deterministic_h2h_snapshot_check": True,
        "contradictions": list(contradictions),
        "feedback": "deterministic h2h floor rejection",
        "overall_pass": False,
    }


def test_doomed_pool_skips_retry_and_marks_reason(monkeypatch):
    """All rows below the shared 15-game floor: no H2H pairing can reach a
    per-matchup tier, no aggregate row reaches the pool-annealed tiers —
    the retry is unsatisfiable, so the skip fires and the MASTER_AUDIT
    reason carries the evidence_floor_unsatisfiable token with the numbers."""
    import evidence_snapshot as es
    import tool_planning_master_dispatch as tmd

    monkeypatch.setattr(
        es,
        "load_generation_evaluation_snapshot",
        lambda next_v: _bundle(
            h2h={_pair_key(1, 2): _row(4)},
            bot_stats={bot_name(1): {"games": 9, "wins": 4}},
        ),
    )
    audit = _audit(
        _floor_token("national_cloud_v1_vs_national_cloud_v2", 4, 4, 15, 15)
    )
    report = tmd._master_audit_doomed_retry_report(audit, 190)
    assert report is not None
    assert report["doomed"] is True
    assert report["primary_satisfiable"] is False
    assert report["aggregate_satisfiable"] is False
    assert report["best_matchup_games"] == 4
    assert report["best_non_matchup_games"] == 9
    assert report["primary_tier"] == 15
    assert report["aggregate_tier"] == 15

    reason = tmd._doomed_evidence_floor_abandon_reason(
        190, report, audit["feedback"]
    )
    assert "evidence_floor_unsatisfiable" in reason
    # The numbers the operator event needs (tier + best available).
    assert "primary tier >= 15" in reason
    assert "best matchup games 4" in reason
    assert "rating daemon" in reason


def test_floor_boundary_pool_is_not_doomed(monkeypatch):
    """A pairing at exactly the shared 15-game floor satisfies its own
    per-matchup tier (15 >= 15) and the annealed aggregate tier — the retry
    can succeed, so no skip. (Aggregate-doomed-with-primary-satisfiable is
    structurally impossible: any row that satisfies a primary tier also
    lifts the pool max to at least the annealed aggregate tier.)"""
    import evidence_snapshot as es
    import tool_planning_master_dispatch as tmd

    monkeypatch.setattr(
        es,
        "load_generation_evaluation_snapshot",
        lambda next_v: _bundle(
            h2h={_pair_key(1, 2): _row(15)},
            bot_stats={bot_name(1): {"games": 12, "wins": 6}},
        ),
    )
    audit = _audit(
        _floor_token("national_cloud_v1_vs_national_cloud_v2", 4, 15, 15, 15)
    )
    assert tmd._master_audit_doomed_retry_report(audit, 190) is None


def test_satisfiable_pool_keeps_retry(monkeypatch):
    """A 48-game pairing exists and clears its own tier (and the annealed
    aggregate tier 36): the retry can succeed, so no skip."""
    import evidence_snapshot as es
    import tool_planning_master_dispatch as tmd

    monkeypatch.setattr(
        es,
        "load_generation_evaluation_snapshot",
        lambda next_v: _bundle(
            h2h={_pair_key(1, 2): _row(4), _pair_key(3, 4): _row(48)},
            bot_stats={bot_name(1): {"games": 40, "wins": 20}},
        ),
    )
    audit = _audit(
        _floor_token("national_cloud_v1_vs_national_cloud_v2", 4, 4, 15, 36)
    )
    assert tmd._master_audit_doomed_retry_report(audit, 190) is None


def test_mixed_rejection_never_skips(monkeypatch):
    """A citation-accuracy error next to the floor error means the model can
    still fix the numbers — no skip even when the pool is doomed."""
    import evidence_snapshot as es
    import tool_planning_master_dispatch as tmd

    monkeypatch.setattr(
        es,
        "load_generation_evaluation_snapshot",
        lambda next_v: _bundle(h2h={_pair_key(1, 2): _row(4)}),
    )
    audit = _audit(
        _floor_token("national_cloud_v1_vs_national_cloud_v2", 4, 4, 15, 15),
        f"(key {_pair_key(1, 2)}) cited a_wins mismatch vs snapshot",
    )
    assert tmd._master_audit_doomed_retry_report(audit, 190) is None


def test_llm_audit_rejection_never_skips(monkeypatch):
    """A rejection without the deterministic h2h flag (the LLM auditor's own
    verdict) never triggers the skip."""
    import evidence_snapshot as es
    import tool_planning_master_dispatch as tmd

    monkeypatch.setattr(
        es,
        "load_generation_evaluation_snapshot",
        lambda next_v: _bundle(h2h={_pair_key(1, 2): _row(4)}),
    )
    audit = _audit(
        _floor_token("national_cloud_v1_vs_national_cloud_v2", 4, 4, 15, 15)
    )
    audit["deterministic_h2h_snapshot_check"] = False
    assert tmd._master_audit_doomed_retry_report(audit, 190) is None


def test_unreadable_pool_never_skips(monkeypatch):
    """UNKNOWN pool is never treated as unsatisfiable (mirrors the absolute
    -tier-on-unknown rule): no skip."""
    import evidence_snapshot as es
    import tool_planning_master_dispatch as tmd

    monkeypatch.setattr(
        es,
        "load_generation_evaluation_snapshot",
        lambda next_v: {"available": False, "reason": "snapshot_read_failed"},
    )
    audit = _audit(
        _floor_token("national_cloud_v1_vs_national_cloud_v2", 4, 4, 15, 15)
    )
    assert tmd._master_audit_doomed_retry_report(audit, 190) is None


def test_dispatch_loop_wiring_skips_before_replan():
    """Static regression: the audit loop consults the doomed-retry guard
    AFTER the exhausted-retries abandon and BEFORE re-entering Master, and
    the skip path drives the canonical MASTER_AUDIT_REJECTED abandon (no
    new stage/checkpoint fields), appending the token to the audit's error
    list and emitting the daemon-supply event."""
    src = (CORE_DIR / "tool_planning_master_dispatch.py").read_text(
        encoding="utf-8"
    )
    guard_call = "_master_audit_doomed_retry_report(\n                audit_result, next_v\n            )"
    assert guard_call in src, "audit loop must consult the doomed-retry guard"
    assert "pipeline.master_audit_evidence_floor_unsatisfiable" in src
    assert (
        'audit_result.setdefault("contradictions", []).append(' in src
    ), "the skip must append evidence_floor_unsatisfiable to the error list"
    assert '"evidence_floor_unsatisfiable"' in src
    # Guard sits after the exhausted-retries abandon and before the re-plan.
    exhausted = src.index('event_type="pipeline.master_audit_exhausted_abandon"')
    replan = src.index("# Re-plan with rejection feedback")
    guard = src.index(guard_call)
    assert exhausted < guard < replan
    # The skip must reuse the existing MASTER_AUDIT_REJECTED terminal error
    # (exhausted path + doomed path), not invent a new stage/error code.
    assert src.count('error="MASTER_AUDIT_REJECTED"') >= 2


def test_repair_guidance_maps_underscore_token_to_snapshot_rows(monkeypatch):
    """The rejection token's charset-safe ``a_vs_b`` matchup form must map
    back onto the snapshot rows so h2h_citation_repair_guidance can inject
    the exact replacement facts."""
    import evidence_snapshot as es

    h2h = {
        _pair_key(2, 3): {"games": 18, "a_wins": 9, "b_wins": 9,
                          "draws": 0, "win_rate": 0.5},
    }
    monkeypatch.setattr(es, "load_generation_h2h_snapshot", lambda next_v: h2h)
    token = _floor_token("national_cloud_v2_vs_national_cloud_v3", 4, 18, 15, 36)
    guidance = es.h2h_citation_repair_guidance(190, [token], source_v=2)
    assert _pair_key(2, 3) in guidance
    assert "games=18" in guidance
