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
# binding objects with int leaves.  Those structures are SEALED at acceptance
# (proposal_id / scout role_result_digest are computed over exactly these
# bytes), so the normalizer never rewrites them — a post-acceptance rewrite
# deterministically failed the v488 quality gate with
# proposal_identity_mismatch / proposal_invocation_result_mismatch for every
# proposal, and the re-signed digest can never be recomputed.
#
# 2026-10-04 audit_scope: the audit no longer flattens the sealed structures
# into its citable-text view at all.  Both renderers (_flatten_text /
# _flatten_marked) skip the whole proposal_ensemble / proposal_binding
# subtree, so the window/alias attribution that used to bind a sealed
# ``games: 259`` leaf onto a pair row (the false v485/v486/v489 rejections)
# can no longer fire: the audit grades only the plan's self-authored text,
# and the sealed statistical authority is reconciled byte-level against the
# frozen snapshot by validate_sealed_proposal_evidence_precision.

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
    to last), stale counts, FAKE digests.  Under the pre-audit_scope shared
    flatten the binding's own ``games`` leaf rendered BEFORE its
    ``reference`` alias line, so the pair window starting at the alias never
    saw it — the audit only ever flagged what followed the alias.  Under
    audit_scope the sealed subtree never renders at all; only the precision
    validator grades these bytes."""
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
    keys with ``games`` FIRST, so the pre-audit_scope flattened ``games:
    259`` line landed inside the preceding h2h binding's pair window (the
    audit attributed it to the pair row — the exact false v485/v486/v489
    rejection).  The projection is a long truncated container dump like the
    real binding, so after the pointer's reference line it swallowed the
    pointer's own aggregate window and nothing beyond the binding was
    attributed.  The digests are deliberately fake: this binding is NOT
    system-derived, so the sealed-evidence precision validator must flag
    it."""
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


def test_structured_sealed_binding_objects_are_never_rewritten(
    monkeypatch, tmp_path
):
    """Sealed snapshot_evidence leaves stay byte-exact and OUT of the audit.

    Pre-audit_scope, the real v486 packet bindings rendered (alphabetical
    JSON order) so the selection binding's ``games: 259`` line followed the
    h2h binding's ``reference`` alias line: the pair window reached it
    before its own ``snapshot:`` reference truncated the window, and the
    audit attributed it to the pair row (a false rejection — every sealed
    number was system-derived and snapshot-exact).  The normalizer still
    never rewrites them (v488), and the shared citable-text view now
    excludes the sealed subtrees entirely, so the audit outcome is
    byte-identical with and without normalization: no prose-citation
    rejection.  The stale/fake-digest sealed bindings are instead flagged by
    validate_sealed_proposal_evidence_precision (fail-closed moved to the
    byte-level reconciliation, not away).
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
    pre_bytes = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    marked_text = evidence_snapshot._flatten_marked(plan, [], 0)[0]
    assert marked_text == evidence_snapshot._flatten_text(plan)
    # The shared citable-text view renders the sealed subtrees EMPTY: no
    # binding leaves, no aliases from sealed reference lines.
    assert "259" not in marked_text
    assert "snapshot:selection_snapshot.json#/rows" not in marked_text
    assert stale_h2h["node_sha256"] not in marked_text

    pre_errors = evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    )
    # The audit no longer rejects the plan over sealed bytes: nothing in the
    # plan's self-authored text cites a wrong number.
    assert pre_errors == []

    report = evidence_snapshot.normalize_master_plan_citations(plan, 24)

    # Nothing was rewritten: the sealed structures stay byte-identical.
    assert json.dumps(plan, sort_keys=True, separators=(",", ":")) == pre_bytes
    assert report["total"] == 0
    assert report["normalizations"] == []
    for binding_list in (
        plan["proposal_binding"]["snapshot_evidence"],
        plan["proposal_ensemble"]["proposals"][0]["snapshot_evidence"],
    ):
        h2h_binding, selection_binding = binding_list
        assert selection_binding["games"] == 259
        assert h2h_binding["games"] == 30
        assert h2h_binding["a_wins"] == 11
        assert h2h_binding["b_wins"] == 19
        # Digest/projection bytes untouched.
        assert h2h_binding["node_sha256"] == "a" * 64
        assert h2h_binding["projection_sha256"] == "b" * 64
        assert selection_binding["node_sha256"] == "c" * 64
    assert evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    ) == pre_errors
    # Fail-closed moved to the byte-level sealed-evidence reconciliation:
    # these hand-shaped bindings (fake digests, stale counts) ARE flagged.
    precision = evidence_snapshot.validate_sealed_proposal_evidence_precision(
        plan, 24
    )
    joined_precision = "; ".join(precision)
    assert "sealed_evidence_node_digest_mismatch" in joined_precision
    assert f"snapshot:head_to_head.json#/{key}" in joined_precision
    assert "snapshot:selection_snapshot.json#/rows" in joined_precision


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


def test_sealed_proposal_structures_are_never_rewritten(monkeypatch, tmp_path):
    """v488 regression: the seam must never mutate sealed proposal structures.

    At plan acceptance the ensemble's ``proposal_id`` (sha256 over the
    substantive contract, which includes ``snapshot_evidence``) and each
    scout invocation's ``role_result_digest`` (canonical digest over the
    original proposal bytes) are sealed.  The quality gate
    (``_selected_proposal_quality_evidence``) later re-feeds
    ``master_plan['proposal_ensemble']`` to ``_parse_valid_proposal_packet``,
    which re-derives both identities from the live bytes — so ANY
    post-acceptance rewrite of ``proposal_ensemble`` / ``proposal_binding``
    deterministically fails ``proposal_identity_mismatch`` and
    ``proposal_invocation_result_mismatch`` for every proposal (v488: three
    proposals, six gate errors, quality gate dead after four ``games``
    leaves were rewritten).  ``role_result_digest`` seals the scout output,
    so a rewritten packet can never be re-signed; the only safe behavior is
    to leave the sealed bytes byte-exact.  The audit no longer grades those
    bytes as prose citations (exclusion view); their statistical authority
    is reconciled byte-level by the sealed-evidence precision validator.
    """
    import copy

    from agent_master_validation import _proposal_identity

    key = _v486_style_fixture(monkeypatch, tmp_path)

    def _ordered_proposal(direction):
        return {
            "direction": direction,
            "schema_version": "master-proposal-v4",
            "change_symbol": f"policy.py:_choose_intent_{direction}",
            "snapshot_evidence": [
                _stale_h2h_binding(key),
                _stale_selection_binding(),
            ],
        }

    plan = {
        "analysis": (
            f"{key}: games=30, a_wins=11, b_wins=19. Corroboration "
            "snapshot:selection_snapshot.json#/rows (games=259)."
        ),
        "selected_proposal_id": "da60ede64cd3e9a5",
        "proposal_binding": {
            "selected_proposal_id": "da60ede64cd3e9a5",
            "snapshot_evidence": [
                _stale_h2h_binding(key),
                _stale_selection_binding(),
            ],
        },
        "proposal_ensemble": {
            "ordered_proposals": [
                _ordered_proposal("mechanism"),
                _ordered_proposal("compute_memory"),
            ],
        },
    }
    pre_ensemble_bytes = json.dumps(
        plan["proposal_ensemble"], sort_keys=True, separators=(",", ":")
    )
    pre_binding_bytes = json.dumps(
        plan["proposal_binding"], sort_keys=True, separators=(",", ":")
    )
    pre_ids = [
        _proposal_identity(item)
        for item in plan["proposal_ensemble"]["ordered_proposals"]
    ]

    report = evidence_snapshot.normalize_master_plan_citations(plan, 24)

    # The sealed structures stay byte-identical, so the gate-rederived
    # proposal identities (and the scout-sealed role_result_digestes over
    # these same bytes) still match.
    assert json.dumps(
        plan["proposal_ensemble"], sort_keys=True, separators=(",", ":")
    ) == pre_ensemble_bytes
    assert json.dumps(
        plan["proposal_binding"], sort_keys=True, separators=(",", ":")
    ) == pre_binding_bytes
    assert [
        _proposal_identity(item)
        for item in plan["proposal_ensemble"]["ordered_proposals"]
    ] == pre_ids
    # The stale citation numbers inside the sealed structures stay put —
    # including the shallow-shared binding copy.
    assert plan["proposal_binding"]["snapshot_evidence"][1]["games"] == 259
    assert all(
        item["snapshot_evidence"][1]["games"] == 259
        for item in plan["proposal_ensemble"]["ordered_proposals"]
    )
    assert not [
        entry
        for entry in report["normalizations"]
        if str(entry.get("path", "")).startswith(
            ("proposal_ensemble.", "proposal_binding")
        )
    ]
    # The audit no longer rejects the plan over the sealed bytes: only the
    # plan's own text is graded, and that text was normalized.
    post_errors = evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    )
    assert post_errors == []
    # The sealed bytes themselves are still fail-closed: the fake-digest
    # stale bindings are flagged by the byte-level precision validator.
    precision = evidence_snapshot.validate_sealed_proposal_evidence_precision(
        plan, 24
    )
    assert "; ".join(precision).count("sealed_evidence_node_digest_mismatch") >= 3

    # The seam stays live OUTSIDE the sealed structures: the prose citation
    # in the very same plan is still normalized to the snapshot row values.
    assert plan["analysis"] == (
        f"{key}: games=28, a_wins=11, b_wins=17. Corroboration "
        "snapshot:selection_snapshot.json#/rows (games=220)."
    )


def test_five_repeated_stale_objects_are_all_left_byte_exact(
    monkeypatch, tmp_path
):
    """The same stale binding pair repeated 5 times (packet proposals plus
    the derived proposal_binding) is left byte-exact at every occurrence
    (v488: the ensemble/binding bytes back the sealed proposal identities
    the quality gate re-derives).  The prose-citation audit passes (sealed
    subtrees are excluded from its citable-text view); the byte-level
    sealed-evidence precision validator still flags the underlying defect
    once per distinct reference (deduplicated — five copies of the same
    wrong binding are one evidence defect)."""
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
    pre_errors = evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    )
    assert pre_errors == []
    pre_bytes = json.dumps(plan, sort_keys=True, separators=(",", ":"))

    report = evidence_snapshot.normalize_master_plan_citations(plan, 24)

    all_selections = [p["snapshot_evidence"][1] for p in proposals] + [
        plan["proposal_binding"]["snapshot_evidence"][1]
    ]
    assert len(all_selections) == 5
    for binding in all_selections:
        assert binding["games"] == 259
    assert report["total"] == 0
    assert report["normalizations"] == []
    assert json.dumps(plan, sort_keys=True, separators=(",", ":")) == pre_bytes
    post_errors = evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    )
    assert post_errors == pre_errors
    precision = evidence_snapshot.validate_sealed_proposal_evidence_precision(
        plan, 24
    )
    joined = "; ".join(precision)
    # Five copies, two distinct references: each wrong binding is flagged
    # once per distinct reference+defect, not five noisy duplicates.
    assert joined.count("snapshot:head_to_head.json#/" + key) >= 1
    assert joined.count("snapshot:selection_snapshot.json#/rows") >= 1
    assert "sealed_evidence" in joined


# ---------------------------------------------------------------------------
# Sealed-evidence precision (audit_scope, 2026-10-04)
# ---------------------------------------------------------------------------
# The sealed statistical authority is the system re-derived
# ``snapshot_evidence`` binding the scout acceptance point creates
# (``_snapshot_reference_evidence_binding`` reads the exact frozen snapshot
# directory the audit bundle loads).  These tests pin
# ``validate_sealed_proposal_evidence_precision``: bindings produced by the
# real producer against the real frozen snapshot reconcile byte-level
# (pointer resolution + node_sha256 + typed scalars + projection) and the
# audit passes on the exclusion view; a tampered/re-sealed binding fails
# BOTH the precision validator and the quality-gate identity re-derivation.

def _snapshot_dir_for(monkeypatch_unused, next_v: int = 24):
    identity = evidence_snapshot.load_generation_snapshot_identity(next_v)
    assert identity.get("available"), identity
    from pathlib import Path

    return Path(identity["manifest_path"]).parent


def _system_derived_pair(key: str, snapshot_dir):
    from agent_master_validation import _snapshot_reference_evidence_binding

    h2h = _snapshot_reference_evidence_binding(
        f"snapshot:head_to_head.json#/{key}", snapshot_dir
    )
    aggregate = _snapshot_reference_evidence_binding(
        "snapshot:bot_stats.json#/national_cloud_v1", snapshot_dir
    )
    assert h2h is not None and aggregate is not None
    assert h2h["games"] == 28 and aggregate["games"] == 220
    return h2h, aggregate


def test_sealed_precision_accepts_system_derived_bindings(
    monkeypatch, tmp_path
):
    """Bindings the real producer derived from the real frozen snapshot pass
    every audit leg and the quality-gate identity re-derivation."""
    import copy

    from agent_master_validation import _proposal_identity
    from bot_artifact import canonical_digest

    key = _v486_style_fixture(monkeypatch, tmp_path)
    snapshot_dir = _snapshot_dir_for(None)
    h2h_binding, aggregate_binding = _system_derived_pair(key, snapshot_dir)

    proposal = {
        "schema_version": "master-proposal-v4",
        "change_symbol": "policy.py:_choose_intent_mechanism",
        "snapshot_evidence": [
            copy.deepcopy(h2h_binding),
            copy.deepcopy(aggregate_binding),
        ],
    }
    proposal["proposal_id"] = _proposal_identity(proposal)
    plan = {
        # No textual aggregate pointer in the plan's own text: the aggregate
        # corroboration leg must be satisfied by the STRUCTURED citation
        # reference inside the sealed binding (has_aggregate union).
        "analysis": (
            f"{key}: games=28, a_wins=11, b_wins=17, draws=0, "
            "win_rate=0.3929 (confirmed weakness)."
        ),
        "selected_proposal_id": proposal["proposal_id"],
        "proposal_binding": {
            "selected_proposal_id": proposal["proposal_id"],
            "snapshot_evidence": [
                copy.deepcopy(h2h_binding),
                copy.deepcopy(aggregate_binding),
            ],
        },
        "proposal_ensemble": {"ordered_proposals": [proposal]},
    }

    # One shared citable-text view: marked render == plain render, sealed
    # subtree empty, prose intact.
    marked_text = evidence_snapshot._flatten_marked(plan, [], 0)[0]
    assert marked_text == evidence_snapshot._flatten_text(plan)
    assert "snapshot:bot_stats.json" not in marked_text
    assert h2h_binding["node_sha256"] not in marked_text

    # Audit legs: prose accuracy, statistical floor (aggregate leg via the
    # structured citation reference), sealed byte-level precision.
    assert evidence_snapshot.validate_h2h_citations_against_snapshot(
        plan, 24
    ) == []
    assert evidence_snapshot.statistical_evidence_floor_errors(plan, 24) == []
    assert evidence_snapshot.validate_sealed_proposal_evidence_precision(
        plan, 24
    ) == []

    # Normalization is a no-op on snapshot-exact prose and never touches the
    # sealed bytes.
    pre_bytes = json.dumps(plan, sort_keys=True, separators=(",", ":"))
    report = evidence_snapshot.normalize_master_plan_citations(plan, 24)
    assert report["total"] == 0
    assert json.dumps(plan, sort_keys=True, separators=(",", ":")) == pre_bytes

    # Quality-gate identity re-derivation over the live ensemble bytes still
    # matches (the sealed packet was never rewritten).
    live = plan["proposal_ensemble"]["ordered_proposals"][0]
    assert _proposal_identity(live) == live["proposal_id"]
    assert canonical_digest(live)  # digest is computable from live bytes


def test_reseal_rewrite_fails_identity_and_precision(monkeypatch, tmp_path):
    """The v488 temptation — rewrite a stale sealed number to satisfy the
    audit — fails both gates: the quality-gate identity re-derivation
    (proposal_id) and the byte-level sealed-evidence precision check.  The
    only passing state is byte-exact sealed bytes plus the audit exclusion
    view."""
    import copy

    from agent_master_validation import _proposal_identity

    key = _v486_style_fixture(monkeypatch, tmp_path)
    snapshot_dir = _snapshot_dir_for(None)
    h2h_binding, aggregate_binding = _system_derived_pair(key, snapshot_dir)
    proposal = {
        "schema_version": "master-proposal-v4",
        "change_symbol": "policy.py:_choose_intent_mechanism",
        "snapshot_evidence": [h2h_binding, aggregate_binding],
    }
    proposal["proposal_id"] = _proposal_identity(proposal)
    sealed_role_digest_target = copy.deepcopy(proposal)
    plan = {
        "analysis": f"{key}: games=28, a_wins=11, b_wins=17, draws=0.",
        "selected_proposal_id": proposal["proposal_id"],
        "proposal_binding": {
            "selected_proposal_id": proposal["proposal_id"],
            "snapshot_evidence": [copy.deepcopy(h2h_binding), aggregate_binding],
        },
        "proposal_ensemble": {"ordered_proposals": [proposal]},
    }
    assert evidence_snapshot.validate_sealed_proposal_evidence_precision(
        plan, 24
    ) == []

    # Re-seal: rewrite one sealed games leaf (exactly the v488 normalization
    # shape — 28 -> 30 inside proposal_ensemble).
    rewritten = copy.deepcopy(plan)
    rewritten["proposal_ensemble"]["ordered_proposals"][0][
        "snapshot_evidence"
    ][0]["games"] = 30

    # Quality gate identity: the re-derived id no longer matches the sealed
    # proposal_id, and the scout role_result_digest (canonical digest over
    # the original proposal bytes) can never be recomputed.
    live = rewritten["proposal_ensemble"]["ordered_proposals"][0]
    assert _proposal_identity(live) != live["proposal_id"]
    from bot_artifact import canonical_digest

    assert canonical_digest(live) != canonical_digest(sealed_role_digest_target)
    # Precision: the tampered scalar no longer reconciles with the frozen
    # snapshot row (node games=28).
    precision = evidence_snapshot.validate_sealed_proposal_evidence_precision(
        rewritten, 24
    )
    joined = "; ".join(precision)
    assert "sealed_evidence_scalar_mismatch" in joined
    assert "games" in joined
    assert f"snapshot:head_to_head.json#/{key}" in joined


def test_sealed_precision_flags_projection_and_digest_tampering(
    monkeypatch, tmp_path
):
    """Distinct precision defects surface distinct tokens: a rewritten
    resolved_projection breaks the projection/digest checks even when the
    typed scalars still match the snapshot row."""
    import copy

    key = _v486_style_fixture(monkeypatch, tmp_path)
    snapshot_dir = _snapshot_dir_for(None)
    h2h_binding, aggregate_binding = _system_derived_pair(key, snapshot_dir)
    tampered = copy.deepcopy(h2h_binding)
    tampered["resolved_projection"] = tampered["resolved_projection"].replace(
        '"games":28', '"games":99'
    )
    plan = {
        "analysis": f"{key}: games=28, a_wins=11, b_wins=17, draws=0.",
        "proposal_binding": {
            "selected_proposal_id": "abc",
            "snapshot_evidence": [tampered, aggregate_binding],
        },
    }
    precision = evidence_snapshot.validate_sealed_proposal_evidence_precision(
        plan, 24
    )
    joined = "; ".join(precision)
    assert "sealed_evidence_projection_mismatch" in joined
    # The projection digest no longer matches the tampered projection bytes.
    assert "sealed_evidence_projection_digest_mismatch" in joined


def test_sealed_precision_rejects_unresolvable_reference(monkeypatch, tmp_path):
    """A sealed reference that cannot resolve against the frozen bundle is a
    precision error (fail-closed), never silently ignored."""
    key = _v486_style_fixture(monkeypatch, tmp_path)
    plan = {
        "analysis": f"{key}: games=28, a_wins=11, b_wins=17, draws=0.",
        "proposal_binding": {
            "selected_proposal_id": "abc",
            "snapshot_evidence": [
                {
                    "reference": (
                        "snapshot:head_to_head.json#/"
                        "national_cloud_v404 vs national_cloud_v405"
                    ),
                    "node_sha256": "0" * 64,
                    "resolved_projection": "{}",
                    "projection_sha256": "1" * 64,
                    "projection_truncated": False,
                }
            ],
        },
    }
    precision = evidence_snapshot.validate_sealed_proposal_evidence_precision(
        plan, 24
    )
    joined = "; ".join(precision)
    assert "sealed_evidence_node_unresolvable" in joined
    assert "national_cloud_v404" in joined


def test_sealed_precision_without_readable_snapshot_is_inert(monkeypatch):
    """No frozen snapshot: the precision validator returns no errors and the
    wide-except posture at the dispatch site is unchanged."""
    import evolution_infra

    monkeypatch.setattr(
        evolution_infra, "RESULTS_DIR", evolution_infra.RESULTS_DIR.parent / "nope"
    )
    plan = {
        "proposal_binding": {
            "snapshot_evidence": [
                {
                    "reference": "snapshot:bot_stats.json#/x",
                    "node_sha256": "0" * 64,
                    "resolved_projection": "{}",
                    "projection_sha256": "1" * 64,
                    "projection_truncated": False,
                }
            ]
        }
    }
    assert evidence_snapshot.validate_sealed_proposal_evidence_precision(
        plan, 424242
    ) == []


def test_aggregate_leg_accepts_structured_citation_references(
    monkeypatch, tmp_path
):
    """Keep-regression for the has_aggregate union: when the plan's own text
    cites no aggregate pointer but the sealed ``proposal_binding`` bindings
    carry one (the normal case once sealed subtrees leave the citable-text
    view), the aggregate corroboration leg is satisfied by the STRUCTURED
    citation reference — the same ``snapshot:(bot_stats|selection_snapshot)
    .json`` pointer rule the text regex applies."""
    import copy

    key = _v486_style_fixture(monkeypatch, tmp_path)
    snapshot_dir = _snapshot_dir_for(None)
    h2h_binding, aggregate_binding = _system_derived_pair(key, snapshot_dir)
    plan = {
        "analysis": f"{key}: games=28, a_wins=11, b_wins=17, draws=0.",
        "proposal_binding": {
            "selected_proposal_id": "abc",
            "snapshot_evidence": [h2h_binding, aggregate_binding],
        },
    }
    assert "snapshot:bot_stats.json" not in evidence_snapshot._flatten_text(plan)
    assert evidence_snapshot.statistical_evidence_floor_errors(plan, 24) == []
