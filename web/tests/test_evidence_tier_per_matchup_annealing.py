"""Per-matchup primary-tier annealing for the statistical evidence bar.

Contract change 2026-09-13: the PRIMARY (matchup-row) tier anneals on the
cited matchup's own best row instead of the whole-pool max, so an irrelevant
large row elsewhere in the pool can no longer harden the tier of a matchup
that only has 4-21 games (one root cause of the 253-generation abandonment
chain: the evaluation daemon supplied relevant samples too slowly while an
irrelevant row pinned the primary tier at 30).

Non-matchup (aggregate-class) citations keep the legacy pool-annealed
primary tier, so the change is a pure relaxation on the primary side: every
citation set that passed before still passes.

Invariants pinned here:
- the shared 15-game floor never moves, and re-hardens automatically as the
  pool grows (no persisted state);
- the aggregate tier keeps the pool-wide annealing, including the
  ``selection_snapshot.json#/rows`` container-pointer rule that binds the
  strongest row's ``games`` (unchanged from 2026-08-16/19);
- the proposal validator (``agent_master_validation``) and the plan-audit
  mirror (``evidence_snapshot``) emit byte-identical rejection tokens
  through ONE shared formatter (AGENTS.md: one citation set, one pool-max
  typing rule).
"""

import json
import re
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from bot_namespace import bot_name  # noqa: E402


def _row(games):
    a = games // 2
    return {
        "games": games,
        "a_wins": a,
        "b_wins": games - a,
        "draws": 0,
        "win_rate": round(a / games, 4) if games else 0.0,
    }


def _pair_key(a, b):
    return f"{bot_name(a)} vs {bot_name(b)}"


def _pair_token(a, b):
    return f"{bot_name(a)}_vs_{bot_name(b)}"


def _h2h_ref(a, b):
    return f"snapshot:head_to_head.json#/{_pair_key(a, b)}"


def _bot_ref(v):
    return f"snapshot:bot_stats.json#/{bot_name(v)}"


def _write_snapshot(root, h2h=None, bot_stats=None, selection=None):
    snap = Path(root) / "snap"
    snap.mkdir(parents=True, exist_ok=True)
    if h2h is not None:
        (snap / "head_to_head.json").write_text(
            json.dumps(h2h), encoding="utf-8"
        )
    if bot_stats is not None:
        (snap / "bot_stats.json").write_text(
            json.dumps(bot_stats), encoding="utf-8"
        )
    if selection is not None:
        (snap / "selection_snapshot.json").write_text(
            json.dumps(selection), encoding="utf-8"
        )
    return snap


def test_unrelated_large_row_no_longer_hardens_cited_matchup(tmp_path):
    """The 253-abandon anatomy: pool max 31 (an unrelated pairing) used to
    pin the primary tier at 30 while the cited matchup only had 18 games and
    the best aggregate row had 24 (< 30), so the citation set was rejected
    outright. The cited matchup now anneals on its own best row (tier 15),
    so its 18-row passes primary and the 24-row clears the annealed
    aggregate tier 23. The pool key is stored in the REVERSED direction to
    prove both a-vs-b / b-vs-a count as the same matchup."""
    import agent_master_validation as amv

    snap = _write_snapshot(
        tmp_path,
        h2h={
            _pair_key(1, 9): _row(31),         # irrelevant larger row
            _pair_key(3, 2): _row(18),         # cited matchup, reversed key
        },
        bot_stats={bot_name(2): {"games": 24, "wins": 12}},
    )
    assert amv._effective_evidence_tiers(snap) == (30, 23)
    citations = [
        (_h2h_ref(2, 3), 18),   # cited in the OTHER direction than the key
        (_bot_ref(2), 24),
    ]
    assert amv._snapshot_evidence_two_tier_errors(citations, snap) == []


def test_sparse_matchup_still_rejected_at_shared_floor(tmp_path):
    """A 4-game matchup cannot anneal below the shared 15-game floor: the
    4-game citation is still rejected and the token carries the per-matchup
    numbers (cited / best_available / tier) for self-correction."""
    import agent_master_validation as amv

    snap = _write_snapshot(tmp_path, h2h={_pair_key(2, 3): _row(4)})
    errs = amv._snapshot_evidence_two_tier_errors([(_h2h_ref(2, 3), 4)], snap)
    assert errs == [
        "proposal_cited_sample_too_small"
        f".{_pair_token(2, 3)}"
        ".cited.4.best_available.4.tier.15"
        ".and_aggregate.15"
        ".aggregate_sources.bot_stats.selection_snapshot"
    ]
    for err in errs:
        assert re.fullmatch(r"[a-z0-9_.:-]+", err) is not None
        assert len(err) <= 200


def test_mature_matchup_keeps_absolute_tier(tmp_path):
    """A matchup whose best row already reached 30 stays hardened at 30:
    citing a 20-game row of that matchup is rejected. (The 20-game citation
    deliberately disagrees with the 48-game snapshot row here: tier math is
    graded in isolation — citation ACCURACY is
    validate_h2h_citations_against_snapshot's job.)"""
    import agent_master_validation as amv

    snap = _write_snapshot(tmp_path, h2h={_pair_key(2, 3): _row(48)})
    errs = amv._snapshot_evidence_two_tier_errors([(_h2h_ref(2, 3), 20)], snap)
    assert errs == [
        "proposal_cited_sample_too_small"
        f".{_pair_token(2, 3)}"
        ".cited.20.best_available.48.tier.30"
        ".and_aggregate.36"
        ".aggregate_sources.bot_stats.selection_snapshot"
    ]


def test_aggregate_tier_and_container_pointer_unchanged(tmp_path):
    """The aggregate tier still anneals on the WHOLE-pool max (not the cited
    matchup), and the advertised ``selection_snapshot.json#/rows`` container
    pointer still binds the strongest row's games so it can satisfy the
    aggregate tier."""
    import agent_master_validation as amv

    snap = _write_snapshot(
        tmp_path,
        h2h={_pair_key(1, 2): _row(31), _pair_key(1, 3): _row(24)},
        selection={"rows": [
            {"name": bot_name(1), "games": 24, "wins": 12},
            {"name": bot_name(3), "games": 9, "wins": 4},
        ]},
    )
    # pool max 31 -> aggregate anneals to 23 (75% rule), primary pool tier 30.
    assert amv._effective_evidence_tiers(snap) == (30, 23)
    binding = amv._snapshot_reference_evidence_binding(
        "snapshot:selection_snapshot.json#/rows", snap
    )
    assert binding is not None and binding["games"] == 24
    citations = [
        (_h2h_ref(1, 2), 31),                            # primary: tier 30
        ("snapshot:selection_snapshot.json#/rows", 24),  # aggregate: 24>=23
    ]
    assert amv._snapshot_evidence_two_tier_errors(citations, snap) == []
    # When the pool max is an unrelated 48-game row, the aggregate tier
    # anneals to 36 and an 18-game aggregate citation cannot satisfy it —
    # the aggregate side is NOT per-matchup.
    snap2 = _write_snapshot(
        tmp_path / "second",
        h2h={_pair_key(1, 9): _row(48), _pair_key(2, 3): _row(18)},
    )
    errs = amv._snapshot_evidence_two_tier_errors([(_h2h_ref(2, 3), 18)], snap2)
    assert errs == [
        "proposal_cited_sample_too_small"
        f".{_pair_token(2, 3)}"
        ".cited.18.best_available.18.tier.15"
        ".and_aggregate.36"
        ".aggregate_sources.bot_stats.selection_snapshot"
    ]


def test_validator_and_audit_mirror_tokens_identical(monkeypatch, tmp_path):
    """Both sides must emit the byte-identical rejection token: they share
    the tier math, the matchup classification, and ONE formatter. The audit
    side is exercised through its bindings path (proposal_binding) and its
    alias-fallback path (plan prose naming the matchup)."""
    import agent_master_validation as amv
    import evidence_snapshot as es

    h2h = {_pair_key(2, 3): _row(4), _pair_key(1, 9): _row(48)}
    bot_stats = {bot_name(2): {"games": 20, "wins": 10}}
    snap = _write_snapshot(tmp_path, h2h=h2h, bot_stats=bot_stats)
    citations = [(_h2h_ref(2, 3), 4), (_bot_ref(2), 20)]
    validator_errs = amv._snapshot_evidence_two_tier_errors(citations, snap)

    monkeypatch.setattr(
        es,
        "load_generation_evaluation_snapshot",
        lambda next_v: {
            "available": True,
            "h2h": h2h,
            "bot_stats": bot_stats,
        },
    )
    expected = [
        "proposal_cited_sample_too_small"
        f".{_pair_token(2, 3)}"
        ".cited.4.best_available.4.tier.15"
        ".and_aggregate.36"
        ".aggregate_sources.bot_stats.selection_snapshot"
    ]
    assert validator_errs == expected

    plan_bindings = {
        "proposal_binding": {"snapshot_evidence": [
            {"reference": _h2h_ref(2, 3), "games": 4},
            {"reference": _bot_ref(2), "games": 20},
        ]},
        "worker_prompt": f"{_pair_key(2, 3)} games=4 weakness",
    }
    assert es.statistical_evidence_floor_errors(plan_bindings, 190) == expected

    plan_alias_only = {
        "worker_prompt": f"{_pair_key(2, 3)} games=4 weakness "
        f"(see snapshot:bot_stats.json#/{bot_name(2)})",
        "evidence_refs": [_bot_ref(2)],
    }
    assert es.statistical_evidence_floor_errors(plan_alias_only, 190) == expected


def test_unknown_pool_and_unknown_matchup_keep_absolute_tiers(tmp_path):
    """An unreadable pool (snapshot_dir=None) or an unreadable matchup keeps
    the absolute primary tier — annealing only weakens the bar when the
    relevant sample is observably small, never when it is merely unknown."""
    import agent_master_validation as amv

    citations = [(_h2h_ref(2, 3), 45), (_bot_ref(2), 234)]
    assert amv._snapshot_evidence_two_tier_errors(citations, None) == []
    # A snapshot dir without head_to_head.json: the cited matchup is unknown
    # (best_available 0) and grades at the absolute tier 30.
    snap = _write_snapshot(
        tmp_path,
        bot_stats={bot_name(2): {"games": 234, "wins": 117}},
    )
    assert amv._snapshot_evidence_two_tier_errors(citations, snap) == []
    errs = amv._snapshot_evidence_two_tier_errors([(_h2h_ref(2, 3), 20)], snap)
    assert errs == [
        "proposal_cited_sample_too_small"
        f".{_pair_token(2, 3)}"
        ".cited.20.best_available.0.tier.30"
        ".and_aggregate.200"
        ".aggregate_sources.bot_stats.selection_snapshot"
    ]


def test_floor_rehardens_as_pool_grows(tmp_path):
    """No persisted state: the same matchup hardens from 15 to 30 as its own
    best row grows past 30 (pool-maturity re-hardening)."""
    import agent_master_validation as amv

    small = _write_snapshot(tmp_path / "small", h2h={_pair_key(2, 3): _row(18)})
    grown = _write_snapshot(tmp_path / "grown", h2h={_pair_key(2, 3): _row(48)})
    assert amv._matchup_primary_tier(18) == 15
    assert amv._matchup_primary_tier(48) == 30
    # 18-row passes in the small pool...
    assert amv._snapshot_evidence_two_tier_errors(
        [(_h2h_ref(2, 3), 18), (_bot_ref(2), 18)], small
    ) == []
    # ...and the same numbers fail once the matchup itself matures.
    errs = amv._snapshot_evidence_two_tier_errors(
        [(_h2h_ref(2, 3), 18), (_bot_ref(2), 18)], grown
    )
    assert errs and ".cited.18.best_available.48.tier.30." in errs[0]
