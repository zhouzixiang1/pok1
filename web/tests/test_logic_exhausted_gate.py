"""Round-trip tests for the EXHAUSTED-direction gate marker handling (p1-5b).

Covers the round-trip closure bug: experience_pool.md can contain both
"[POSSIBLY EXHAUSTED]" and the LLM-escalated "[EXHAUSTED — hard gate]"
(em-dash + suffix). Both extractors must detect and clean BOTH marker
variants, otherwise the exhausted-direction hard gate silently no-ops.
"""

import pytest


# Canonical markers that must be tolerated by BOTH extractors.
POSSIBLY_MARKER = "[POSSIBLY EXHAUSTED]"
# Em-dash U+2014 + LLM-appended "— hard gate" suffix (real-world variant
# observed in experience_pool.md line 22).
HARD_GATE_MARKER = "[EXHAUSTED — hard gate]"


@pytest.fixture
def exhausted_pool(tmp_path):
    """Write an experience_pool.md fixture with both EXHAUSTED marker variants."""
    content = (
        "## PARAMETER_TUNING\n"
        "- fold margin, call clamp, EQR and sizing_aggr tuning are exhausted "
        "across v55-v63 with no sustained gain. "
        + HARD_GATE_MARKER
        + "\n"
        "## POSTFLOP_STRATEGY\n"
        "- should_fold_postflop was refactored to ~4 clean exits. Adding more "
        "defensive fold gates is redundant — consolidate first. "
        + POSSIBLY_MARKER
        + "\n"
        "## RECENT_LESSONS\n"
        "- Avoided the EXHAUSTED constant-tuning gate via structural additions.\n"
    )
    f = tmp_path / "experience_pool.md"
    f.write_text(content, encoding="utf-8")
    return f


class TestExtractExhaustedKeywords:
    """tool_planning._extract_exhausted_keywords feeds the HARD reject gate."""

    def test_finds_hard_gate_variant(self, exhausted_pool, monkeypatch):
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", exhausted_pool)
        kws = tp._extract_exhausted_keywords()
        sections = [s for s, _ in kws]
        phrases = [p for _, p in kws]
        # The [EXHAUSTED — hard gate] entry must be detected (this is the bug:
        # the old literal "[POSSIBLY EXHAUSTED]" check missed it entirely).
        assert "parameter_tuning" in sections
        assert any("fold margin" in p for p in phrases)

    def test_finds_possibly_variant(self, exhausted_pool, monkeypatch):
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", exhausted_pool)
        kws = tp._extract_exhausted_keywords()
        sections = [s for s, _ in kws]
        assert "postflop_strategy" in sections

    def test_no_marker_residue_in_phrase(self, exhausted_pool, monkeypatch):
        """The marker (incl. the '— hard gate' suffix) must be fully stripped."""
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", exhausted_pool)
        kws = tp._extract_exhausted_keywords()
        phrases = [p for _, p in kws]
        for p in phrases:
            assert "exhausted" not in p, f"marker residue left in phrase: {p!r}"
            assert "hard gate" not in p, f"suffix residue left in phrase: {p!r}"

    def test_does_not_match_prose_exhausted(self, exhausted_pool, monkeypatch):
        """Bare 'EXHAUSTED' in RECENT_LESSONS prose is NOT a marker."""
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", exhausted_pool)
        kws = tp._extract_exhausted_keywords()
        sections = [s for s, _ in kws]
        assert "recent_lessons" not in sections

    def test_hard_gate_engages_fuzzy_matching(self, exhausted_pool, monkeypatch):
        """Round-trip: a worker prompt about constant tuning must be flagged."""
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", exhausted_pool)
        kws = tp._extract_exhausted_keywords()
        assert kws, "expected non-empty keywords (gate would be disabled)"
        # Phrase is clause-trimmed to "fold margin, call clamp, eqr and
        # sizing_aggr tuning" — a worker prompt reusing these tokens must match.
        prompt = "Adjust fold margin clamp and sizing_aggr constants."
        assert tp._fuzzy_match_exhausted(prompt.lower(), kws) is True

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", tmp_path / "nope.md")
        assert tp._extract_exhausted_keywords() == []

    def test_recent_lessons_section_excluded(self, tmp_path, monkeypatch):
        """A [POSSIBLY EXHAUSTED]-tagged line inside RECENT_LESSONS must NOT be
        extracted — RECENT_LESSONS holds free-form critic commentary (e.g. a
        1188-char v82 review dump), not a direction. Extracted verbatim it
        becomes a parasitic keyword matching almost any plan."""
        import core.tool_planning as tp
        pool = tmp_path / "experience_pool.md"
        pool.write_text(
            "## PARAMETER_TUNING\n"
            "- constant tuning is exhausted " + POSSIBLY_MARKER + "\n"
            "## RECENT_LESSONS\n"
            "- v82 critic dump: constant tuning, value sizing, strong tier, "
            "structural refactor all noise at <100g, opponent stat targeting "
            "needed " + POSSIBLY_MARKER + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", pool)
        kws = tp._extract_exhausted_keywords()
        sections = [s for s, _ in kws]
        assert "parameter_tuning" in sections
        assert "recent_lessons" not in sections, \
            f"RECENT_LESSONS parasitic entry leaked: {kws}"

    def test_overlong_phrase_excluded(self, tmp_path, monkeypatch):
        """An EXHAUSTED-tagged paragraph (>300 chars) is a critic-review dump,
        not a direction — skip it (defense against future parasitic entries)."""
        import core.tool_planning as tp
        pool = tmp_path / "experience_pool.md"
        long_phrase = " ".join(["word"] * 110)  # well over 500 chars
        pool.write_text(
            "## PARAMETER_TUNING\n"
            f"- {long_phrase} " + POSSIBLY_MARKER + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", pool)
        assert tp._extract_exhausted_keywords() == []


class TestHardGateDirectionToken:
    """The HARD gate (_validate_master_plan, require_direction_token=True)
    requires a direction-characteristic token so a legitimate novel plan sharing
    generic words isn't falsely rejected."""

    @pytest.fixture
    def param_pool(self, tmp_path):
        f = tmp_path / "experience_pool.md"
        f.write_text(
            "## PARAMETER_TUNING\n"
            "- fold margin, call clamp, EQR and sizing_aggr tuning are exhausted "
            "across v55-v63. " + HARD_GATE_MARKER + "\n",
            encoding="utf-8",
        )
        return f

    def test_true_positive_with_direction_token(self, param_pool, monkeypatch):
        """A real constant-tuning plan mentions parameter/tuning -> HARD gate matches."""
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", param_pool)
        kws = tp._extract_exhausted_keywords()
        prompt = "Parameter tuning: adjust fold margin clamp and sizing_aggr constants."
        assert tp._fuzzy_match_exhausted(prompt.lower(), kws, require_direction_token=True) is True

    def test_false_positive_blocked_by_direction_token(self, param_pool, monkeypatch):
        """A plan sharing >=2 distinctive generic tokens (clamp, aggr) but NO
        direction token is blocked by the HARD gate. Without require_direction_token
        it would falsely match — this is exactly the false-positive class STEP2
        fixes (v82 Task0/Task1 legitimate opponent-stat sizing was flagged because
        it shared generic words with the long PARAMETER_TUNING prose)."""
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", param_pool)
        kws = tp._extract_exhausted_keywords()
        # Shares clamp + aggr with the PARAMETER_TUNING phrase, but no
        # parameter/tuning/mechanism/... direction token.
        prompt = "Tighten the value clamp using sizing_aggr for strong hands."
        # default path (execute_workers soft warning) DOES match — recall preserved
        assert tp._fuzzy_match_exhausted(prompt.lower(), kws) is True
        # HARD gate (_validate_master_plan) does NOT — direction token absent
        assert tp._fuzzy_match_exhausted(prompt.lower(), kws, require_direction_token=True) is False


class TestExtractExhaustedBlock:
    """agent_workers._extract_exhausted_block feeds the worker-prompt constraint."""

    def test_block_includes_both_variants(self, exhausted_pool, monkeypatch):
        import core.agent_workers as aw
        monkeypatch.setattr(aw, "EXPERIENCE_FILE", exhausted_pool)
        block = aw._extract_exhausted_block()
        assert block, "expected a non-empty forbidden_directions block"
        assert "<forbidden_directions>" in block
        # Both real lessons must appear, with the marker fully stripped.
        assert "fold margin" in block
        assert "should_fold_postflop" in block

    def test_block_no_marker_residue(self, exhausted_pool, monkeypatch):
        """No '— hard gate]' residue should leak into the constraint block."""
        import core.agent_workers as aw
        monkeypatch.setattr(aw, "EXPERIENCE_FILE", exhausted_pool)
        block = aw._extract_exhausted_block()
        assert "EXHAUSTED]" not in block, f"marker residue in block: {block!r}"
        assert "hard gate]" not in block

    def test_block_excludes_prose_exhausted(self, exhausted_pool, monkeypatch):
        import core.agent_workers as aw
        monkeypatch.setattr(aw, "EXPERIENCE_FILE", exhausted_pool)
        block = aw._extract_exhausted_block()
        # The RECENT_LESSONS line has bare 'EXHAUSTED' (no bracket) — must not be
        # swept into the forbidden-directions block.
        assert "Avoided the EXHAUSTED" not in block

    def test_empty_when_no_markers(self, tmp_path, monkeypatch):
        import core.agent_workers as aw
        f = tmp_path / "experience_pool.md"
        f.write_text("## GENERAL\n- No exhausted entries here.\n", encoding="utf-8")
        monkeypatch.setattr(aw, "EXPERIENCE_FILE", f)
        assert aw._extract_exhausted_block() == ""
