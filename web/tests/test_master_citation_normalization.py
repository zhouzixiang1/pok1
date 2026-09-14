"""Deterministic pre-audit citation normalization (v451/v485 fix).

The final Master plan is normalized against the SAME frozen generation
evidence snapshot the plan audit validates against, between plan acceptance
and ``master_plan_audit_start``.  These tests pin the normalization rules:
normalize resolvable, tier-legal matchup citations and resolvable aggregate
pointers; leave everything else untouched so the audit keeps failing closed.
"""

import json

from bot_namespace import bot_name

from tests.test_evidence_snapshot import _patch_h2h_paths

import evidence_snapshot


# Mature matchup row: games=45 >= per-matchup tier 30, so its citations are
# normalization-eligible.  bot_stats games=220 gives the aggregate pointer a
# resolvable row at the absolute 200-game aggregate tier.
def _mature_payload():
    key = f"{bot_name(17)} vs {bot_name(20)}"
    return {
        key: {
            "games": 45,
            "a_wins": 29,
            "b_wins": 16,
            "draws": 0,
            "win_rate": 0.6444,
        }
    }


_MATURE_BOT_STATS_ROWS = {
    bot_name(17): {"games": 220, "wins": 130, "win_rate": 0.59},
}


def _mature_fixture(monkeypatch, tmp_path):
    live = _patch_h2h_paths(
        monkeypatch,
        tmp_path,
        _mature_payload(),
        bot_stats_rows=_MATURE_BOT_STATS_ROWS,
    )
    evidence_snapshot.ensure_generation_h2h_snapshot(24)
    return live


def _report_entry(report, **fields):
    matches = [
        entry
        for entry in report["normalizations"]
        if all(entry.get(k) == v for k, v in fields.items())
    ]
    assert matches, f"no normalization entry matching {fields} in {report}"
    return matches[0]


def test_mismatched_matchup_numbers_are_normalized_and_audit_passes(
    monkeypatch, tmp_path
):
    """(a) Wrong numbers on a real, tier-legal matchup row are rewritten.

    The live files are poisoned after snapshot creation to prove the
    normalization reads the SAME frozen snapshot the audit reads, never the
    live results.
    """
    live = _mature_fixture(monkeypatch, tmp_path)
    key = f"{bot_name(17)} vs {bot_name(20)}"
    live.write_text(
        json.dumps({
            key: {
                "games": 99,
                "a_wins": 90,
                "b_wins": 9,
                "draws": 0,
                "win_rate": 0.9,
            }
        }),
        encoding="utf-8",
    )

    plan = {
        "analysis": (
            f"{key}: games=50, a_wins=31, b_wins=19, draws=1. "
            f"Corroboration snapshot:bot_stats.json#/{bot_name(17)} "
            "games=220."
        ),
    }
    pre_errors = evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    )
    assert "snapshot has games=45" in "; ".join(pre_errors)

    report = evidence_snapshot.normalize_master_plan_citations(plan, 24)

    assert plan["analysis"] == (
        f"{key}: games=45, a_wins=29, b_wins=16, draws=0. "
        f"Corroboration snapshot:bot_stats.json#/{bot_name(17)} "
        "games=220."
    )
    assert report["available"] is True
    assert report["total"] == 4
    _report_entry(
        report,
        kind="h2h_matchup",
        field="games",
        **{"from": 50, "to": 45},
    )
    _report_entry(
        report,
        kind="h2h_matchup",
        field="a_wins",
        **{"from": 31, "to": 29},
    )
    _report_entry(
        report,
        kind="h2h_matchup",
        field="b_wins",
        **{"from": 19, "to": 16},
    )
    _report_entry(
        report,
        kind="h2h_matchup",
        field="draws",
        **{"from": 1, "to": 0},
    )
    # The audit validators run on the SAME plan object and both pass now.
    assert evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    ) == []
    assert evidence_snapshot.statistical_evidence_floor_errors(plan, 24) == []


def test_mismatched_wl_record_is_normalized_in_both_perspectives(
    monkeypatch, tmp_path
):
    """(a) W/L citations are rewritten with the alias perspective mapping."""
    _mature_fixture(monkeypatch, tmp_path)
    key = f"{bot_name(17)} vs {bot_name(20)}"

    direct = {
        "analysis": (
            f"{key}: 31W/19L. Corroboration "
            f"snapshot:bot_stats.json#/{bot_name(17)} games=220."
        ),
    }
    report = evidence_snapshot.normalize_master_plan_citations(direct, 24)
    assert direct["analysis"] == (
        f"{key}: 29W/16L. Corroboration "
        f"snapshot:bot_stats.json#/{bot_name(17)} games=220."
    )
    _report_entry(
        report,
        kind="h2h_matchup",
        field="wins(W)",
        **{"from": 31, "to": 29},
    )
    _report_entry(
        report,
        kind="h2h_matchup",
        field="losses(L)",
        **{"from": 19, "to": 16},
    )
    assert evidence_snapshot.validate_h2h_citations_against_snapshot(
        direct, 24
    ) == []

    # Reversed alias: W/L stay from the cited perspective (v20 viewpoint),
    # so the rewritten record is 16W/29L, not the raw a_wins/b_wins order.
    reversed_plan = {
        "analysis": (
            f"{bot_name(20)} vs {bot_name(17)}: 19W/31L. Corroboration "
            f"snapshot:bot_stats.json#/{bot_name(17)} games=220."
        ),
    }
    evidence_snapshot.normalize_master_plan_citations(reversed_plan, 24)
    assert reversed_plan["analysis"] == (
        f"{bot_name(20)} vs {bot_name(17)}: 16W/29L. Corroboration "
        f"snapshot:bot_stats.json#/{bot_name(17)} games=220."
    )
    assert evidence_snapshot.validate_h2h_citations_against_snapshot(
        reversed_plan, 24
    ) == []


def test_unresolvable_or_subtier_citations_are_untouched_and_audit_rejects(
    monkeypatch, tmp_path
):
    """(b) Citations that resolve to no row (or a sub-tier row) stay put.

    The normalization never invents snapshot facts; the audit outcome is
    byte-identical with and without normalization, and the sub-tier row's
    citation is still rejected by the accuracy validator exactly as before.
    """
    key = f"{bot_name(17)} vs {bot_name(20)}"
    # Sub-tier matchup: best row 4 games < its per-matchup tier (15), so its
    # citations must never be normalized into a passing plan.
    _patch_h2h_paths(
        monkeypatch,
        tmp_path,
        _mature_payload()
        | {
            f"{bot_name(5)} vs {bot_name(8)}": {
                "games": 4,
                "a_wins": 1,
                "b_wins": 3,
                "draws": 0,
                "win_rate": 0.25,
            },
        },
    )
    evidence_snapshot.ensure_generation_h2h_snapshot(24)

    unknown_matchup_plan = {
        "analysis": (
            f"{bot_name(18)} vs {bot_name(19)}: games=50, a_wins=31, "
            "b_wins=19."
        ),
    }
    before = dict(unknown_matchup_plan)
    report = evidence_snapshot.normalize_master_plan_citations(
        unknown_matchup_plan, 24
    )
    assert report["total"] == 0
    assert report["normalizations"] == []
    assert unknown_matchup_plan["analysis"] == before["analysis"]
    # The audit behaves exactly as it would without normalization: an
    # unknown matchup is graded by the audit's own rules, untouched here.
    assert (
        evidence_snapshot.validate_h2h_citations_against_snapshot(
            unknown_matchup_plan, 24
        )
        == evidence_snapshot.validate_h2h_citations_against_snapshot(
            before, 24
        )
    )

    subtier_plan = {
        "analysis": f"{bot_name(5)} vs {bot_name(8)}: games=6, a_wins=2, b_wins=4.",
    }
    pre_errors = evidence_snapshot.validate_h2h_citations_against_snapshot(
        subtier_plan, 24
    )
    assert "cited games=6" in "; ".join(pre_errors)
    report = evidence_snapshot.normalize_master_plan_citations(subtier_plan, 24)
    assert report["total"] == 0
    assert subtier_plan["analysis"] == (
        f"{bot_name(5)} vs {bot_name(8)}: games=6, a_wins=2, b_wins=4."
    )
    post_errors = evidence_snapshot.validate_h2h_citations_against_snapshot(
        subtier_plan, 24
    )
    assert post_errors == pre_errors
    assert "snapshot has games=4" in "; ".join(post_errors)


def test_correct_citations_are_untouched_with_empty_report(
    monkeypatch, tmp_path
):
    """(c) Snapshot-exact numbers trigger no rewrite and no report entry."""
    _mature_fixture(monkeypatch, tmp_path)
    key = f"{bot_name(17)} vs {bot_name(20)}"
    plan = {
        "analysis": (
            f"{key}: games=45, a_wins=29, b_wins=16, draws=0. "
            f"Corroboration snapshot:bot_stats.json#/{bot_name(17)} "
            "games=220."
        ),
    }
    original = plan["analysis"]
    report = evidence_snapshot.normalize_master_plan_citations(plan, 24)
    assert report["total"] == 0
    assert report["normalizations"] == []
    assert report["available"] is True
    assert plan["analysis"] == original
    assert evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    ) == []
    assert evidence_snapshot.statistical_evidence_floor_errors(plan, 24) == []


def test_aggregate_pointer_games_are_normalized_when_resolvable(
    monkeypatch, tmp_path
):
    """(d) Resolvable aggregate pointers get snapshot games; others stay put."""
    _mature_fixture(monkeypatch, tmp_path)

    plan = {
        "analysis": (
            f"Corroboration snapshot:bot_stats.json#/{bot_name(17)} "
            "games=555, wins=130. Also "
            "snapshot:selection_snapshot.json#/rows games=77. Unknown "
            f"pointer snapshot:bot_stats.json#/{bot_name(99)} games=555."
        ),
    }
    report = evidence_snapshot.normalize_master_plan_citations(plan, 24)

    # bot_stats pointer resolves (games=220) and the selection container
    # binds its strongest row's games (also 220); the unresolvable pointer
    # is left untouched.
    assert plan["analysis"] == (
        f"Corroboration snapshot:bot_stats.json#/{bot_name(17)} "
        "games=220, wins=130. Also "
        "snapshot:selection_snapshot.json#/rows games=220. Unknown "
        f"pointer snapshot:bot_stats.json#/{bot_name(99)} games=555."
    )
    assert report["total"] == 2
    bot_stats_entry = _report_entry(
        report,
        kind="aggregate_pointer",
        field="games",
        **{"from": 555, "to": 220},
    )
    assert (
        f"snapshot:bot_stats.json#/{bot_name(17)}"
        in bot_stats_entry["pointer"]
    )
    _report_entry(
        report,
        kind="aggregate_pointer",
        field="games",
        **{"from": 77, "to": 220},
    )
    assert evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    ) == []


def test_normalization_without_a_readable_snapshot_is_inert(monkeypatch):
    """No frozen snapshot for the generation: nothing is read or rewritten."""
    import evolution_infra

    monkeypatch.setattr(
        evolution_infra, "RESULTS_DIR", evolution_infra.RESULTS_DIR.parent / "nope"
    )
    plan = {"analysis": "any text"}
    report = evidence_snapshot.normalize_master_plan_citations(plan, 424242)
    assert report["available"] is False
    assert report["total"] == 0
    assert plan["analysis"] == "any text"


def test_report_is_capped_but_total_counts_every_rewrite(
    monkeypatch, tmp_path
):
    """The report list is bounded; the total counts all applied rewrites."""
    _mature_fixture(monkeypatch, tmp_path)
    key = f"{bot_name(17)} vs {bot_name(20)}"
    plan = {
        f"field_{index}": f"{key}: games=50, a_wins=31, b_wins=19."
        for index in range(22)
    }
    report = evidence_snapshot.normalize_master_plan_citations(plan, 24)
    assert report["total"] == 22 * 3
    assert len(report["normalizations"]) == 64
    assert all(
        value.endswith("games=45, a_wins=29, b_wins=16.")
        for value in plan.values()
    )
