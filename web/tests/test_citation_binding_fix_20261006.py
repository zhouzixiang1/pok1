"""Citation-binding fix (2026-10-06): the counterfactual rejection chain.

Live forensics (v518 20:45/20:49, v519 21:09 — always the counterfactual
direction): the scout wrote TWO snapshot-shaped ``evidence_refs`` whose
pointers named real frozen rows (byte-identical games/a_wins/b_wins/draws),
yet the gate reported ``proposal_evidence_ref_invalid`` +
``proposal_snapshot_evidence_required`` +
``proposal_cited_sample_too_small.matchup.none.refs_written.2.cited.0...``.
The written spelling merged the canonical pointer prefix with the
repo-relative snapshot path the prompt also renders (``h2h_relpath``)::

    snapshot:web/core/results/v519/evidence_snapshot/head_to_head.json#/A vs B

``_snapshot_reference_like`` counted it (refs_written.2), but
``_validated_snapshot_reference`` normalized that merge only WITHOUT the
``snapshot:`` prefix (2026-10-04 audit P3 closed just the unprefixed half),
so the prefixed spelling stayed dead: a pointer that names a real row was
never counted as a bound citation (cited.0).

These tests pin the three fix surfaces:

* extraction — the prefixed repo-relative merge binds like the unprefixed
  one (same canonical reference, same typed games, participates in the
  two-tier grading) and the normalization never widens the read scope;
* repair hint — the ``refs_written`` branch carries a verbatim, BINDABLE
  pointer form plus the structured ``snapshot_evidence`` block shape;
* scout prompt — the output contract and the exact-citable-rows section
  teach the bare-filename pointer form with a verbatim copyable example
  (the old ``snapshot:relative/file.json#/verified/json/pointer`` teaching
  invited exactly the failing merge).
"""

from pathlib import Path

import pytest

from bot_namespace import bot_name

from test_evidence_snapshot import _patch_h2h_paths

NEXT_V = 521
SOURCE_V = 123
OPPONENT_V = 74

_PAIR_KEY = f"{bot_name(11)} vs {bot_name(SOURCE_V)}"
_AGGREGATE_GAMES = 268


# ═══════════════════════════════════════════════════════════════════════════
# Extraction: the prefixed repo-relative merge must bind
# ═══════════════════════════════════════════════════════════════════════════

def _plain_snapshot_dir(tmp_path: Path) -> Path:
    """A minimal frozen-snapshot directory shaped like the live one.

    Only the two files the binding/tier helpers read are needed; the
    directory's last component is ``evidence_snapshot`` so the repo-relative
    tail alignment in ``_repo_relative_snapshot_path_relative`` behaves
    exactly as against the live runtime snapshot.
    """
    snapshot_dir = tmp_path / "web" / "core" / "results" / f"v{NEXT_V}" / "evidence_snapshot"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "head_to_head.json").write_text(
        __import__("json").dumps({
            _PAIR_KEY: {
                "games": 23, "a_wins": 10, "b_wins": 13, "draws": 0,
                "win_rate": 0.4348,
            },
        }),
        encoding="utf-8",
    )
    (snapshot_dir / "selection_snapshot.json").write_text(
        __import__("json").dumps({
            "rows": [
                {"bot": bot_name(SOURCE_V), "games": _AGGREGATE_GAMES},
                {"bot": bot_name(OPPONENT_V), "games": 240},
            ],
        }),
        encoding="utf-8",
    )
    return snapshot_dir


def _repo_rel(filename: str) -> str:
    return f"web/core/results/v{NEXT_V}/evidence_snapshot/{filename}"


def test_prefixed_repo_relative_pointer_binds_as_citation(tmp_path):
    """The exact live spelling (v518 retry / v519 first attempt) must bind.

    ``snapshot:<repo-relative path>#<locator>`` resolves onto the canonical
    bare-filename pointer with the row's typed games, exactly like the same
    path written without the prefix (the 2026-10-04 case).
    """
    import agent_master_validation as amv

    snapshot_dir = _plain_snapshot_dir(tmp_path)
    live_spelling = f"snapshot:{_repo_rel('head_to_head.json')}#/{_PAIR_KEY}"

    binding = amv._snapshot_reference_evidence_binding(
        live_spelling, snapshot_dir
    )
    assert binding is not None, (
        "the prefixed repo-relative merge must resolve to the same snapshot "
        "node as the unprefixed spelling; a pointer that names a real row "
        "may not stay uncounted (live refs_written.2.cited.0)"
    )
    assert binding["reference"] == (
        f"snapshot:head_to_head.json#/{_PAIR_KEY}"
    ), "the binding must normalize onto the canonical bare-filename pointer"
    assert binding["games"] == 23
    assert binding["a_wins"] == 10
    assert binding["b_wins"] == 13
    assert binding["draws"] == 0

    # The unprefixed merge (already fixed 2026-10-04) and the canonical
    # spelling keep working — no regression.
    assert amv._snapshot_reference_evidence_binding(
        f"{_repo_rel('head_to_head.json')}#/{_PAIR_KEY}", snapshot_dir
    ) is not None
    assert amv._snapshot_reference_evidence_binding(
        f"snapshot:head_to_head.json#/{_PAIR_KEY}", snapshot_dir
    ) is not None


def test_bound_prefixed_citations_pass_two_tier_grading(tmp_path):
    """Once bound, the live citation set clears the two-tier bar.

    With the pair's best row at 23 games the per-matchup primary tier
    anneals to max(15, 3*23//4) = 17 (23 >= 17 passes) and the aggregate
    pointer carries 268 >= 200 — the v519 counterfactual attempt passes
    once its pointers are counted.
    """
    import agent_master_validation as amv

    snapshot_dir = _plain_snapshot_dir(tmp_path)
    aggregate_spelling = (
        f"snapshot:{_repo_rel('selection_snapshot.json')}#/rows/0"
    )
    aggregate_binding = amv._snapshot_reference_evidence_binding(
        aggregate_spelling, snapshot_dir
    )
    assert aggregate_binding is not None
    assert aggregate_binding["games"] == _AGGREGATE_GAMES

    citations = [
        (f"snapshot:head_to_head.json#/{_PAIR_KEY}", 23),
        (aggregate_binding["reference"], _AGGREGATE_GAMES),
    ]
    assert amv._snapshot_evidence_two_tier_errors(
        citations, snapshot_dir
    ) == [], "the exact live citation set must clear the annealed tiers"


def test_prefixed_merge_normalization_never_widens_scope(tmp_path):
    """Only a tail-aligned repo path maps; everything else stays dead.

    A ``snapshot:``-prefixed path whose directory tail does not align with
    the snapshot directory, an absolute path, a traversal, and a non-json
    filename all keep failing — the normalization cannot turn an arbitrary
    filesystem path into a readable snapshot node.
    """
    import agent_master_validation as amv

    snapshot_dir = _plain_snapshot_dir(tmp_path)
    for dead in (
        f"snapshot:other/dir/head_to_head.json#/{_PAIR_KEY}",
        f"snapshot:{snapshot_dir.parent / 'head_to_head.json'}#/{_PAIR_KEY}",
        f"snapshot:{_repo_rel('../head_to_head.json')}#/{_PAIR_KEY}",
        f"snapshot:{_repo_rel('notes.txt')}#/{_PAIR_KEY}",
        # A locator missing the leading slash is malformed, merge or not.
        f"snapshot:{_repo_rel('head_to_head.json')}{_PAIR_KEY}",
    ):
        assert (
            amv._snapshot_reference_evidence_binding(dead, snapshot_dir)
            is None
        ), f"non-aligned spelling must stay unbound: {dead}"


# ═══════════════════════════════════════════════════════════════════════════
# Repair hint: verbatim bindable pointer + structured block shape
# ═══════════════════════════════════════════════════════════════════════════

def _live_rejection_token() -> str:
    return (
        "proposal_cited_sample_too_small.matchup.none.refs_written.2."
        "cited.0.best_available.30.tier.30.and_aggregate.200."
        "aggregate_sources.bot_stats.selection_snapshot"
    )


def test_repair_hint_carries_verbatim_pointer_and_block_shape():
    """The refs_written branch must teach a copyable, BINDING pointer form.

    The pre-fix hint's example (``snapshot:head_to_head.json#/rows``) did
    not itself resolve (head_to_head.json has no ``rows`` key), and no
    structured block shape was shown. The hint must now carry the matchup
    and aggregate pointer forms, the derived ``snapshot_evidence`` field
    list, and the do-not-merge warning for the repo-relative path.
    """
    import agent_master_validation as amv

    guidance = amv._proposal_schema_repair_guidance(
        (_live_rejection_token(),),
        require_snapshot_evidence=True,
    )
    assert "snapshot:head_to_head.json#/" in guidance
    assert "snapshot:selection_snapshot.json#/rows" in guidance
    assert "snapshot_evidence" in guidance
    for field in ("games", "a_wins", "b_wins", "draws"):
        assert field in guidance
    assert "web/core/results" in guidance, (
        "the hint must name the repo-relative path form the model wrongly "
        "merged into the pointer"
    )
    # The aggregate example must itself be a bindable pointer shape (the
    # matchup example uses a placeholder row key, so bind the aggregate one).
    assert "snapshot:head_to_head.json#/rows" not in guidance.replace(
        "snapshot:selection_snapshot.json#/rows", ""
    ), "no non-binding head_to_head.json#/rows example may remain"


def test_hint_aggregate_example_pointer_binds(tmp_path):
    """``snapshot:selection_snapshot.json#/rows`` — the shape the hint and the
    prompt teach — resolves against a snapshot that has that container."""
    import agent_master_validation as amv

    snapshot_dir = _plain_snapshot_dir(tmp_path)
    binding = amv._snapshot_reference_evidence_binding(
        "snapshot:selection_snapshot.json#/rows", snapshot_dir
    )
    assert binding is not None and binding["games"] == _AGGREGATE_GAMES


# ═══════════════════════════════════════════════════════════════════════════
# Scout prompt: teach the bare-filename pointer with a verbatim example
# ═══════════════════════════════════════════════════════════════════════════

def _build_frozen_snapshot(monkeypatch, tmp_path):
    import evidence_snapshot

    payload = {
        f"{bot_name(SOURCE_V)} vs {bot_name(OPPONENT_V)}": {
            "games": 34, "a_wins": 12, "b_wins": 20, "draws": 2,
            "win_rate": 0.3824,
        },
        _PAIR_KEY: {
            "games": 44, "a_wins": 25, "b_wins": 17, "draws": 2,
            "win_rate": 0.5909,
        },
    }
    bot_stats_rows = {
        bot_name(SOURCE_V): {
            "games": 430, "wins": 210, "losses": 210, "draws": 10,
            "win_rate": 0.5,
        },
        bot_name(OPPONENT_V): {
            "games": 452, "wins": 220, "losses": 222, "draws": 10,
            "win_rate": 0.49,
        },
        bot_name(11): {"games": 460, "wins": 230, "losses": 228, "draws": 2},
    }
    _patch_h2h_paths(
        monkeypatch, tmp_path, payload, bot_stats_rows=bot_stats_rows
    )
    snapshot = evidence_snapshot.ensure_generation_h2h_snapshot(NEXT_V)
    assert snapshot.get("available") is True
    return evidence_snapshot, Path(snapshot["h2h_path"]).parent


def _scout_inputs(**overrides):
    inputs = {
        "planning_context": "frozen planning context",
        "direction": "counterfactual",
        "directive": "one falsifiable counterfactual/control",
        "source_v": SOURCE_V,
        "next_v": NEXT_V,
        "protocol_bootstrap_prepared_only": False,
        "singleton_no_strength": False,
        "source_symbol_index": "SYSTEM-VERIFIED SOURCE CALL INDEX",
        "repair_kind": "",
        "projection_hints": [],
        "allowed_primaries": [],
        "invocation_id": "3" * 32,
    }
    inputs.update(overrides)
    return inputs


def test_counterfactual_scout_prompt_teaches_bindable_pointer_form(
    monkeypatch, tmp_path
):
    """The counterfactual prompt must teach the exact pointer form.

    The old contract fragment ``snapshot:relative/file.json#/verified/
    json/pointer`` invited the model to write a relative path after the
    prefix — the exact merge that died live. The prompt must instead carry
    the bare-filename form, a verbatim matchup-pointer example built from
    the strongest citable row, and the derived ``snapshot_evidence`` block
    shape; the example pointer must itself bind against the frozen snapshot.
    """
    import agent_master

    _evidence_snapshot, snapshot_dir = _build_frozen_snapshot(
        monkeypatch, tmp_path
    )
    prompt = agent_master._render_master_proposal_provider_prompt(
        _scout_inputs()
    ).text

    assert "EXACT CITABLE SNAPSHOT ROWS" in prompt
    assert "snapshot:relative/file.json#" not in prompt, (
        "the ambiguous relative-path teaching must be gone"
    )
    assert "snapshot:head_to_head.json#/" in prompt
    # The verbatim example uses the strongest citable row (44 games).
    assert f"snapshot:head_to_head.json#/{_PAIR_KEY}" in prompt
    # The structured block the system derives from the pointer is named.
    assert "snapshot_evidence" in prompt
    for field in ("games", "a_wins", "b_wins", "draws"):
        assert field in prompt
    assert "web/core/results" in prompt, (
        "the prompt must warn against prepending the repo-relative path"
    )

    # The taught example is not decorative: it binds against the same frozen
    # snapshot the gate validates with.
    import agent_master_validation as amv

    binding = amv._snapshot_reference_evidence_binding(
        f"snapshot:head_to_head.json#/{_PAIR_KEY}", snapshot_dir
    )
    assert binding is not None and binding["games"] == 44


def test_repair_dispatch_prompt_carries_the_pointer_hint(
    monkeypatch, tmp_path
):
    """A schema-repair scout dispatch (the v518 live path) renders the
    refs_written guidance with the same verbatim pointer teaching."""
    import agent_master

    _build_frozen_snapshot(monkeypatch, tmp_path)
    prompt = agent_master._render_master_proposal_provider_prompt(
        _scout_inputs(
            repair_kind="schema",
            projection_hints=[_live_rejection_token()],
        )
    ).text

    assert "snapshot:relative/file.json#" not in prompt
    assert "snapshot:head_to_head.json#/" in prompt
    assert "snapshot_evidence" in prompt
    assert "web/core/results" in prompt
