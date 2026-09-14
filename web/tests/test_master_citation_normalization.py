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


# ---------------------------------------------------------------------------
# Real v485/v486 rejection shapes (results/v486/logs/master_io.txt)
# ---------------------------------------------------------------------------
# The audited plan carries the proposal packet (proposal_ensemble) and its
# derived proposal_binding, whose snapshot_evidence lists contain structured
# binding objects with int leaves.  The audit flattens them to ``games: 30``
# lines and attributes them to the pair via cross-object windows — numbers
# the string-leaf-only normalizer never touched (v486 fired 0 times).

def _v486_style_fixture(monkeypatch, tmp_path):
    """Frozen snapshot shaped like the real v485/v486 rejection: the pair row
    moved to games=28 while stale bindings still say 30, and the aggregate
    selection container binds 220 while the stale binding says 259."""
    key = f"{bot_name(1)} vs {bot_name(105)}"
    _patch_h2h_paths(
        monkeypatch,
        tmp_path,
        {
            key: {
                "games": 28,
                "a_wins": 11,
                "b_wins": 17,
                "draws": 0,
                "win_rate": 0.3929,
            },
        },
        bot_stats_rows={bot_name(1): {"games": 220, "wins": 130, "win_rate": 0.59}},
    )
    evidence_snapshot.ensure_generation_h2h_snapshot(24)
    return key


def _sorted_binding(raw):
    """Real packets serialize with ``json.dumps(..., sort_keys=True)``
    (``_parse_valid_proposal_packet``), so in-memory binding key order is
    alphabetical — ``reference`` is second-to-last and the long
    ``resolved_projection`` line directly follows it."""
    return json.loads(json.dumps(raw, sort_keys=True))


def _stale_h2h_binding(key):
    """Binding-shaped object exactly as the packet carries it after the JSON
    round-trip: alphabetically ordered keys (a_wins first, reference second
    to last), stale counts.  With this order the binding's own ``games``
    leaf is rendered BEFORE its ``reference`` alias line, so the pair window
    starting at the alias never sees it — the audit only ever flags what
    follows the alias."""
    return _sorted_binding({
        "a_wins": 11,
        "b_wins": 19,
        "draws": 0,
        "games": 30,
        "node_sha256": "a" * 64,
        "projection_sha256": "b" * 64,
        "projection_truncated": False,
        "reference": f"snapshot:head_to_head.json#/{key}",
        "resolved_projection": json.dumps(
            {
                "a_wins": 11,
                "b_wins": 19,
                "draws": 0,
                "games": 30,
                "win_rate": 0.6444,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    })


def _stale_selection_binding():
    """Real serialized shape from the v486 packet: alphabetically ordered
    keys with ``games`` FIRST, so the flattened ``games: 259`` line lands
    inside the preceding h2h binding's pair window (the audit attributes it
    to the pair row — the exact v485/v486 rejection).  The projection is a
    long truncated container dump like the real binding, so after the
    pointer's reference line it swallows the pointer's own aggregate window
    and nothing beyond the binding is attributed."""
    projection = json.dumps(
        [
            {"confidence": "confirmed_weakness", "games": 220, "pad": "x" * 40}
            for _index in range(20)
        ],
        separators=(",", ":"),
    )
    assert len(projection) > 400
    return _sorted_binding({
        "games": 259,
        "node_sha256": "c" * 64,
        "resolved_projection": projection,
        "projection_sha256": "d" * 64,
        "projection_truncated": True,
        "reference": "snapshot:selection_snapshot.json#/rows",
    })


def test_structured_binding_objects_are_normalized_with_audit_attribution(
    monkeypatch, tmp_path
):
    """Structured int leaves inside snapshot_evidence are rewritten.

    The real v486 packet bindings render (alphabetical JSON order) so the
    selection binding's ``games: 259`` line follows the h2h binding's
    ``reference`` alias line: the pair window reaches it before its own
    ``snapshot:`` reference truncates the window, and the audit attributes
    it to the pair row.  The normalizer follows that exact attribution.
    Digest and projection bytes are never touched.
    """
    import copy

    key = _v486_style_fixture(monkeypatch, tmp_path)
    stale_h2h = _stale_h2h_binding(key)
    stale_selection = _stale_selection_binding()
    plan = {
        "proposal_binding": {
            "selected_proposal_id": "abc",
            "snapshot_evidence": [stale_h2h, stale_selection],
        },
        "proposal_ensemble": {
            "proposals": [
                {
                    "schema_version": "master-proposal-v4",
                    "snapshot_evidence": [
                        copy.deepcopy(stale_h2h),
                        copy.deepcopy(stale_selection),
                    ],
                }
            ]
        },
    }
    marked_text = evidence_snapshot._flatten_marked(plan, [], 0)[0]
    assert marked_text == evidence_snapshot._flatten_text(plan)

    pre_errors = evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    )
    joined = "; ".join(pre_errors)
    # One error per packet copy, exactly like the real v486 rejection: the
    # pair window flags the selection binding's games leaf, not the h2h
    # binding's own counts (those render before their alias line).
    assert joined.count("cited games=259") == 2
    assert "snapshot has games=28" in joined

    report = evidence_snapshot.normalize_master_plan_citations(plan, 24)

    for binding_list in (
        plan["proposal_binding"]["snapshot_evidence"],
        plan["proposal_ensemble"]["proposals"][0]["snapshot_evidence"],
    ):
        h2h_binding, selection_binding = binding_list
        # The selection binding's games leaf is reached by the h2h binding's
        # pair window (its serialized key order puts ``games`` before its own
        # snapshot: reference), so the audit's attribution gives it the pair
        # row — exactly the real v485/v486 rejection.
        assert selection_binding["games"] == 28
        # The h2h binding's own counts render before their alias line: the
        # audit never attributed them, so normalization never touches them.
        assert h2h_binding["games"] == 30
        assert h2h_binding["a_wins"] == 11
        assert h2h_binding["b_wins"] == 19
        # Digest/projection bytes untouched.
        assert h2h_binding["node_sha256"] == "a" * 64
        assert h2h_binding["projection_sha256"] == "b" * 64
        assert selection_binding["node_sha256"] == "c" * 64
    assert report["total"] == 2
    selection_entry = _report_entry(
        report,
        kind="h2h_matchup",
        field="games",
        **{"from": 259, "to": 28},
    )
    assert "snapshot_evidence[1]" in selection_entry["path"]
    assert evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    ) == []


def test_mixed_prose_window_follows_pair_attribution(monkeypatch, tmp_path):
    """Numbers before a truncating pointer belong to the pair row; the number
    after the pointer belongs to the aggregate pointer's row."""
    key = _v486_style_fixture(monkeypatch, tmp_path)
    plan = {
        "analysis": (
            f"{key}: games=30, a_wins=11, b_wins=19, draws=0, "
            "win_rate=0.3667 (confirmed weakness), corroborated by the "
            "aggregate row snapshot:selection_snapshot.json#/rows "
            "(games=259)."
        ),
    }
    pre_errors = evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    )
    joined = "; ".join(pre_errors)
    assert "cited games=30" in joined
    assert "snapshot has games=28" in joined

    report = evidence_snapshot.normalize_master_plan_citations(plan, 24)

    assert plan["analysis"] == (
        f"{key}: games=28, a_wins=11, b_wins=17, draws=0, "
        "win_rate=0.3667 (confirmed weakness), corroborated by the "
        "aggregate row snapshot:selection_snapshot.json#/rows "
        "(games=220)."
    )
    _report_entry(
        report,
        kind="h2h_matchup",
        field="games",
        **{"from": 30, "to": 28},
    )
    _report_entry(
        report,
        kind="h2h_matchup",
        field="b_wins",
        **{"from": 19, "to": 17},
    )
    _report_entry(
        report,
        kind="aggregate_pointer",
        field="games",
        **{"from": 259, "to": 220},
    )
    assert evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    ) == []


def test_five_repeated_stale_objects_are_all_rewritten(
    monkeypatch, tmp_path
):
    """The same stale binding pair repeated 5 times (packet proposals plus
    the derived proposal_binding) is rewritten at every occurrence."""
    key = _v486_style_fixture(monkeypatch, tmp_path)
    proposals = [
        {
            "schema_version": "master-proposal-v4",
            "snapshot_evidence": [
                _stale_h2h_binding(key),
                _stale_selection_binding(),
            ],
        }
        for _index in range(4)
    ]
    plan = {
        "proposal_binding": {
            "selected_proposal_id": "abc",
            "snapshot_evidence": [
                _stale_h2h_binding(key),
                _stale_selection_binding(),
            ],
        },
        "proposal_ensemble": {"proposals": proposals},
    }
    pre_joined = "; ".join(
        evidence_snapshot.validate_h2h_citations_against_snapshot(plan, 24)
    )
    assert pre_joined.count("cited games=259") == 5

    report = evidence_snapshot.normalize_master_plan_citations(plan, 24)

    all_selections = [p["snapshot_evidence"][1] for p in proposals] + [
        plan["proposal_binding"]["snapshot_evidence"][1]
    ]
    assert len(all_selections) == 5
    for binding in all_selections:
        assert binding["games"] == 28
    assert report["total"] == 5
    assert evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    ) == []
