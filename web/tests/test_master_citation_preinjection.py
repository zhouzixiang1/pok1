"""Pre-injection of exact citable snapshot rows into Master prompts (2026-09-13).

Two consecutive generations died at the plan audit because the final Master
plan cited H2H games/wins numbers that disagreed with the frozen snapshot
(hallucinated recall — a corrective retry that injected the right numbers
still re-failed), with the aggregate corroboration leg as the secondary
rejection (the model did not know which aggregate pointer clears the
pool-annealed aggregate tier).

These tests pin down:

* ``evidence_snapshot.exact_citable_rows_preinjection`` renders the source
  parent's exact H2H rows (both directions) plus the aggregate pointers that
  satisfy the CURRENT tier, bounded, fail-open when the snapshot is missing;
* BOTH prompt renderers (final Master + proposal scout) embed the section
  with the exact frozen numbers, and still render without it when no
  snapshot exists;
* ``h2h_citation_repair_guidance`` additionally lists qualifying aggregate
  pointers (with the tier number) for ``proposal_cited_sample_too_small``
  rejections, while plain H2H accuracy rejections keep the old guidance.

Reuses the evaluation-snapshot fixture (``_patch_h2h_paths``) so the frozen
snapshot is built by the real publication path, never hand-written.
"""

import re

from bot_namespace import bot_name

from test_evidence_snapshot import _patch_h2h_paths

NEXT_V = 124
SOURCE_V = 123
OPPONENT_V = 74

# The published bundle canonicalizes H2H pair keys (lexicographic single
# direction per pair), so "both directions" coverage uses two different
# pairs: one canonical row with the source first, one with the source second.
_SOURCE_KEY = f"{bot_name(SOURCE_V)} vs {bot_name(OPPONENT_V)}"
_SOURCE_SECOND_KEY = f"{bot_name(11)} vs {bot_name(SOURCE_V)}"
_FOREIGN_KEY = f"{bot_name(50)} vs {bot_name(51)}"


def _build_frozen_snapshot(
    monkeypatch,
    tmp_path,
    *,
    extra_source_opponents: int = 0,
    cold_pool: bool = False,
):
    """Create one frozen generation snapshot with tier-qualifying aggregates.

    ``cold_pool`` plants only sub-200 aggregate rows (pool max 160) so the
    aggregate tier anneals to 120 — the regime where the model cannot know
    which pointer passes without this table.
    """

    import evidence_snapshot

    payload = {
        _SOURCE_KEY: {
            "games": 34, "a_wins": 12, "b_wins": 20, "draws": 2,
            "win_rate": 0.3824,
        },
        # Source sits on the B side of this canonical pair: the table must
        # still list it with the source-perspective annotation.
        _SOURCE_SECOND_KEY: {
            "games": 44, "a_wins": 25, "b_wins": 17, "draws": 2,
            "win_rate": 0.5909,
        },
        _FOREIGN_KEY: {
            "games": 40, "a_wins": 20, "b_wins": 18, "draws": 2,
            "win_rate": 0.525,
        },
    }
    if cold_pool:
        # Pool max 160 -> aggregate tier anneals to max(15, 3*160//4) = 120.
        bot_stats_rows = {
            bot_name(SOURCE_V): {
                "games": 150, "wins": 75, "losses": 73, "draws": 2,
            },
            bot_name(OPPONENT_V): {
                "games": 160, "wins": 80, "losses": 78, "draws": 2,
            },
            bot_name(50): {"games": 60, "wins": 30, "losses": 28, "draws": 2},
        }
    else:
        bot_stats_rows = {
            bot_name(SOURCE_V): {
                "games": 430, "wins": 210, "losses": 210, "draws": 10,
                "win_rate": 0.5,
            },
            bot_name(OPPONENT_V): {
                "games": 452, "wins": 220, "losses": 222, "draws": 10,
                "win_rate": 0.49,
            },
            bot_name(11): {
                "games": 460, "wins": 230, "losses": 228, "draws": 2,
            },
            # Below the 200 aggregate tier: must never be advertised.
            bot_name(50): {"games": 60, "wins": 30, "losses": 28, "draws": 2},
        }
    for index in range(extra_source_opponents):
        opponent_v = 200 + index
        games = 100 - 2 * index  # even so a_wins + b_wins + draws == games
        payload[f"{bot_name(SOURCE_V)} vs {bot_name(opponent_v)}"] = {
            "games": games,
            "a_wins": games // 2,
            "b_wins": games // 2,
            "draws": 0,
            "win_rate": 0.5,
        }
        bot_stats_rows[bot_name(opponent_v)] = {
            "games": 300 + index,
            "wins": 150 + index,
            "losses": 150,
            "draws": 0,
        }
    _patch_h2h_paths(
        monkeypatch,
        tmp_path,
        payload,
        bot_stats_rows=bot_stats_rows,
    )
    snapshot = evidence_snapshot.ensure_generation_h2h_snapshot(NEXT_V)
    assert snapshot.get("available") is True
    return evidence_snapshot


# ═══════════════════════════════════════════════════════════════════════════
# exact_citable_rows_preinjection
# ═══════════════════════════════════════════════════════════════════════════

def test_preinjection_lists_exact_source_h2h_and_aggregate_numbers(
    monkeypatch, tmp_path
):
    evidence_snapshot = _build_frozen_snapshot(monkeypatch, tmp_path)

    text = evidence_snapshot.exact_citable_rows_preinjection(
        NEXT_V, source_v=SOURCE_V
    )

    assert "EXACT CITABLE SNAPSHOT ROWS" in text
    # Source-parent H2H rows in both seat directions, exact numbers, ranked
    # by games descending (the 44-game source-second row leads).
    assert (
        f"canonical_citation: {_SOURCE_KEY}: games=34, a_wins=12, "
        "b_wins=20, draws=2, win_rate=0.3824"
    ) in text
    assert (
        f"canonical_citation: {_SOURCE_SECOND_KEY}: games=44, a_wins=25, "
        "b_wins=17, draws=2, win_rate=0.5909"
    ) in text
    assert "v123 perspective: 12W/20L" in text
    assert "v123 perspective: 17W/25L" in text
    # A non-source matchup row must not leak into the table.
    assert _FOREIGN_KEY not in text
    # Aggregate pointers meeting the current (absolute, pool max >= 200)
    # tier, with the exact bound games and the tier number.
    assert "games >= 200" in text
    assert "snapshot:selection_snapshot.json#/rows — games=460" in text
    assert (
        f"snapshot:bot_stats.json#/{bot_name(OPPONENT_V)} — games=452" in text
    )
    assert (
        f"snapshot:bot_stats.json#/{bot_name(SOURCE_V)} — games=430" in text
    )
    # The below-tier per-bot row is never advertised as corroboration.
    assert f"snapshot:bot_stats.json#/{bot_name(50)}" not in text
    # Closing hard instruction: copy, never recall.
    assert "ONLY as printed" in text
    assert "verbatim" in text


def test_preinjection_bounds_rows_pointers_and_total_chars(
    monkeypatch, tmp_path
):
    evidence_snapshot = _build_frozen_snapshot(
        monkeypatch, tmp_path, extra_source_opponents=15
    )

    # Bounded totals even with a generous character budget.
    text = evidence_snapshot.exact_citable_rows_preinjection(
        NEXT_V, source_v=SOURCE_V, max_chars=100_000
    )
    h2h_lines = re.findall(r"^- canonical_citation: .+$", text, re.MULTILINE)
    assert len(h2h_lines) <= 12
    pointer_lines = re.findall(r"^- snapshot: .+ — games=\d+$", text, re.MULTILINE)
    assert len(pointer_lines) <= 6

    # Default budget: the section itself never exceeds the ~2500-char cap.
    bounded = evidence_snapshot.exact_citable_rows_preinjection(
        NEXT_V, source_v=SOURCE_V
    )
    assert 0 < len(bounded) <= 2500
    # Deterministic trimming drops trailing H2H rows first; the aggregate
    # pointers and the closing instruction always survive.
    assert "snapshot:selection_snapshot.json#/rows" in bounded
    assert "verbatim" in bounded


def test_preinjection_missing_snapshot_returns_empty(monkeypatch, tmp_path):
    import evolution_infra
    import evidence_snapshot

    results = tmp_path / "web" / "core" / "results"
    results.mkdir(parents=True)
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)

    assert (
        evidence_snapshot.exact_citable_rows_preinjection(
            NEXT_V, source_v=SOURCE_V
        )
        == ""
    )


def test_preinjection_annealed_tier_uses_shared_pool_math(
    monkeypatch, tmp_path
):
    """Cold pool: the advertised aggregate tier is the annealed one.

    Pool max is 160 (the strongest selection row), so the shared annealing
    rule lowers the aggregate tier to max(15, 3*160//4) = 120 and the
    150-game bot_stats row qualifies — the exact regime where the model
    cannot know which pointer passes without this table.
    """

    evidence_snapshot = _build_frozen_snapshot(
        monkeypatch, tmp_path, cold_pool=True
    )

    text = evidence_snapshot.exact_citable_rows_preinjection(
        NEXT_V, source_v=SOURCE_V
    )

    assert "games >= 120" in text
    assert "games >= 200" not in text
    assert (
        f"snapshot:bot_stats.json#/{bot_name(SOURCE_V)} — games=150" in text
    )
    assert "snapshot:selection_snapshot.json#/rows — games=160" in text
    # The 60-game row stays below even the annealed tier.
    assert f"snapshot:bot_stats.json#/{bot_name(50)}" not in text

# ═══════════════════════════════════════════════════════════════════════════
# Prompt renderers embed the section (final Master + proposal scout)
# ═══════════════════════════════════════════════════════════════════════════

def _final_inputs(**overrides):
    inputs = {
        "template_values": {
            "source_v": str(SOURCE_V),
            "next_v": str(NEXT_V),
            "h2h_snapshot_contract": "snapshot contract",
        },
        "master_context": "frozen master context",
        "proposal_ensemble": "ensemble",
        "source_v": SOURCE_V,
        "next_v": NEXT_V,
        "invocation_id": "",
        "schema_repair_suffix": "",
        "final_output_guard": "# SYSTEM-OWNED FINAL EMISSION GATE\nno-op",
    }
    inputs.update(overrides)
    return inputs


def _scout_inputs(**overrides):
    inputs = {
        "planning_context": "frozen planning context",
        "direction": "mechanism",
        "directive": "one structural mechanism",
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


def test_final_master_prompt_contains_preinjected_numbers(monkeypatch, tmp_path):
    _build_frozen_snapshot(monkeypatch, tmp_path)
    import agent_master

    prompt = agent_master._render_master_final_provider_prompt(
        _final_inputs()
    ).text

    assert "EXACT CITABLE SNAPSHOT ROWS" in prompt
    assert "games=34, a_wins=12, b_wins=20, draws=2" in prompt
    assert "snapshot:selection_snapshot.json#/rows — games=460" in prompt
    # The deterministic repair suffix stays at the actual end of the prompt.
    assert "frozen master context" in prompt


def test_proposal_scout_prompt_contains_preinjected_numbers(
    monkeypatch, tmp_path
):
    _build_frozen_snapshot(monkeypatch, tmp_path)
    import agent_master

    prompt = agent_master._render_master_proposal_provider_prompt(
        _scout_inputs()
    ).text

    assert "EXACT CITABLE SNAPSHOT ROWS" in prompt
    assert "games=34, a_wins=12, b_wins=20, draws=2" in prompt
    assert f"snapshot:bot_stats.json#/{bot_name(OPPONENT_V)} — games=452" in prompt


def test_no_strength_bootstrap_scout_prompt_has_no_preinjection(
    monkeypatch, tmp_path
):
    _build_frozen_snapshot(monkeypatch, tmp_path)
    import agent_master

    prompt = agent_master._render_master_proposal_provider_prompt(
        _scout_inputs(
            protocol_bootstrap_prepared_only=True,
            singleton_no_strength=False,
        )
    ).text

    assert "EXACT CITABLE SNAPSHOT ROWS" not in prompt


def test_prompts_still_render_without_snapshot(monkeypatch, tmp_path):
    import agent_master
    import evolution_infra

    results = tmp_path / "web" / "core" / "results"
    results.mkdir(parents=True)
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)

    final_prompt = agent_master._render_master_final_provider_prompt(
        _final_inputs()
    ).text
    scout_prompt = agent_master._render_master_proposal_provider_prompt(
        _scout_inputs()
    ).text

    assert "EXACT CITABLE SNAPSHOT ROWS" not in final_prompt
    assert "frozen master context" in final_prompt
    assert "SCOUT TOOL/CHAIN SCOPE" in scout_prompt
    assert "EXACT CITABLE SNAPSHOT ROWS" not in scout_prompt


# ═══════════════════════════════════════════════════════════════════════════
# h2h_citation_repair_guidance: aggregate leg (proposal_cited_sample_too_small)
# ═══════════════════════════════════════════════════════════════════════════

def test_repair_guidance_appends_aggregate_pointers_for_too_small(
    monkeypatch, tmp_path
):
    evidence_snapshot = _build_frozen_snapshot(monkeypatch, tmp_path)

    token = (
        f"proposal_cited_sample_too_small.{bot_name(OPPONENT_V)}_vs_"
        f"{bot_name(SOURCE_V)}.cited.21.best_available.34.tier.30."
        "and_aggregate.200.aggregate_sources.bot_stats.selection_snapshot"
    )
    guidance = evidence_snapshot.h2h_citation_repair_guidance(
        NEXT_V, [token], source_v=SOURCE_V
    )

    # H2H leg: the cited matchup's exact rows still lead the guidance.
    assert (
        f"canonical_citation: {_SOURCE_KEY}: games=34, a_wins=12, "
        "b_wins=20, draws=2"
    ) in guidance
    # Aggregate leg: qualifying pointers with the exact tier number.
    assert "Aggregate corroboration leg" in guidance
    assert "games >= 200" in guidance
    assert "snapshot:selection_snapshot.json#/rows — games=460" in guidance
    assert (
        f"snapshot:bot_stats.json#/{bot_name(OPPONENT_V)} — games=452"
        in guidance
    )


def test_repair_guidance_aggregate_only_without_h2h_rows(
    monkeypatch, tmp_path
):
    """A too-small rejection whose matchup has no mapped row still gets the
    aggregate pointers (the aggregate leg does not depend on H2H rows)."""

    evidence_snapshot = _build_frozen_snapshot(monkeypatch, tmp_path)

    token = (
        "proposal_cited_sample_too_small.matchup.none.cited.21."
        "best_available.34.tier.30.and_aggregate.200.aggregate_sources."
        "bot_stats.selection_snapshot"
    )
    guidance = evidence_snapshot.h2h_citation_repair_guidance(
        NEXT_V, [token], source_v=SOURCE_V
    )

    assert "Aggregate corroboration leg" in guidance
    assert "snapshot:bot_stats.json#/" in guidance


def test_repair_guidance_plain_accuracy_errors_gain_no_aggregate_block(
    monkeypatch, tmp_path
):
    """H2H-leg (accuracy) rejections keep the legacy guidance exactly: no
    aggregate block is appended without the two-tier token (regression
    guard for the pre-existing repair-guidance contract)."""

    evidence_snapshot = _build_frozen_snapshot(monkeypatch, tmp_path)

    errors = [
        f"{_SOURCE_SECOND_KEY} cited games=10, snapshot has games=44 "
        f"(key {_SOURCE_SECOND_KEY})"
    ]
    guidance = evidence_snapshot.h2h_citation_repair_guidance(
        NEXT_V, errors, source_v=SOURCE_V
    )

    assert (
        f"canonical_citation: {_SOURCE_SECOND_KEY}: games=44, a_wins=25, "
        "b_wins=17, draws=2, win_rate=0.5909"
    ) in guidance
    assert "v123 perspective: 17W/25L" in guidance
    assert "Do not replace them with live H2H" in guidance
    assert "Aggregate corroboration leg" not in guidance
    assert "snapshot:selection_snapshot.json" not in guidance
