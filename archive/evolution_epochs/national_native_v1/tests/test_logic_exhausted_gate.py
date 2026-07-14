"""ARCHIVED national_native_v1 EXHAUSTED-direction gate tests.

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

    def test_validate_master_plan_blocks_positive_exhausted_intent(self, param_pool, monkeypatch):
        import core.tool_planning as tp

        monkeypatch.setattr(tp, "EXPERIENCE_FILE", param_pool)
        plan = {
            "analysis": "try the stale axis",
            "targeted_failure": "plateau",
            "expected_behavior_change": "small fold improvement",
            "do_not_touch": [],
            "measurement_plan": "quality gates",
            "tasks": [{
                "worker_id": 1,
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py"],
                "worker_prompt": "Parameter tuning: adjust fold margin clamp and sizing_aggr constants.",
            }],
        }

        errors, warnings = tp._validate_master_plan(plan, next_v=120)

        assert warnings == []
        assert any("EXHAUSTED_DIRECTION_REPEATED" in error for error in errors)

    def test_validate_master_plan_ignores_noisy_worker_prompt_when_structured_intent_is_novel(
        self, tmp_path, monkeypatch
    ):
        """v58 regression: code skeleton terms in worker_prompt must not make a
        novel BB-OOP probe plan look like a stale fold/SPR/bluff axis."""
        import core.tool_planning as tp

        pool = tmp_path / "experience_pool.md"
        pool.write_text(
            "## OPPONENT_MODELING\n"
            "- Archetype-axis ports saturate to `standard`, reappear without WR-lift. "
            + HARD_GATE_MARKER + "\n"
            "## POSTFLOP_STRATEGY\n"
            "- Polarized-jam call/fold: downstream fold gates alone are insufficient "
            "if SPR_COMMITMENT_PROBE / POLARIZED_JAM_CALL_OVERRIDE fire <1% @>=30g. "
            + HARD_GATE_MARKER + "\n"
            "- Fold-side GENERIC nudges FORBIDDEN & dead (underbettor floors, value-tier ceilings). "
            + HARD_GATE_MARKER + "\n"
            "## BLUFF_CALIBRATION\n"
            "- Board-texture bluff-raise (offense axis) retired as caution unless >=100g WR revives it. "
            + HARD_GATE_MARKER + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", pool)
        plan = {
            "analysis": "second attempt after a rejected stack-off plan",
            "targeted_failure": "Missing BB-OOP turn/river probe line template after PFR check-back.",
            "expected_behavior_change": "Add a bounded value extraction line in to_call==0 spots.",
            "measurement_plan": "quality gates",
            "tasks": [{
                "worker_id": 1,
                "role": "Algorithmic Logic Architect",
                "target_files": ["donk_probe.py", "strategy.py"],
                "behavior_hypothesis": (
                    "BB-OOP probe fires on turn/river after PFR check-back, extracting "
                    "value from medium hands and semi-bluffing draws at controlled frequency."
                ),
                "expected_diff_shape": (
                    "Add _BB_PROBE_* parameters, should_bb_oop_probe_bet(), and one "
                    "strategy.py wiring block after the existing probe block."
                ),
                "worker_prompt": (
                    "## Task: Add BB-OOP turn/river probe bet line template\n"
                    "Do NOT revive the polarized-jam commitment axis. Add constants "
                    "_BB_PROBE_MAX_WETNESS and _BB_PROBE_VALUE_FREQ_CAP; use board_texture, "
                    "tier, value, semi_bluff, fold_to_raise, and telemetry reason=fired."
                ),
            }],
        }

        errors, warnings = tp._validate_master_plan(plan, next_v=58)

        assert warnings == []
        assert not any("EXHAUSTED_DIRECTION_REPEATED" in error for error in errors)

    def test_validate_master_plan_can_warn_for_repair_mode(self, param_pool, monkeypatch):
        import core.tool_planning as tp

        monkeypatch.setattr(tp, "EXPERIENCE_FILE", param_pool)
        plan = {
            "analysis": "repair context",
            "targeted_failure": "quality blocker",
            "expected_behavior_change": "compile fix",
            "do_not_touch": [],
            "measurement_plan": "quality gates",
            "tasks": [{
                "worker_id": 1,
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py"],
                "worker_prompt": "Parameter tuning: adjust fold margin clamp and sizing_aggr constants.",
            }],
        }

        errors, warnings = tp._validate_master_plan(
            plan,
            next_v=120,
            exhausted_policy="warn",
        )

        assert errors == []
        assert any("EXHAUSTED direction" in warning for warning in warnings)


class TestExtractExhaustedBlock:
    """agent_workers._extract_exhausted_block feeds the worker-prompt constraint.

    Tiering (per-section state machine): EXHAUSTED entries inside ## RECENT_LESSONS
    become hard <forbidden_directions>; entries in any other section become advisory
    <advisory_directions> so old exhaustion expires naturally instead of permanently
    blacklisting directions.
    """

    def test_block_includes_both_variants(self, exhausted_pool, monkeypatch):
        import core.agent_workers as aw
        monkeypatch.setattr(aw, "EXPERIENCE_FILE", exhausted_pool)
        block = aw._extract_exhausted_block()
        assert block, "expected a non-empty constraint block"
        # Both EXHAUSTED markers in this fixture live in PARAMETER_TUNING and
        # POSTFLOP_STRATEGY sections (NOT RECENT_LESSONS), so they are tiered as
        # ADVISORY (historical caution), not a hard forbidden_directions ban.
        assert "<advisory_directions>" in block
        # Both real lessons must still appear, with the marker fully stripped.
        assert "fold margin" in block
        assert "should_fold_postflop" in block

    def test_recent_section_emits_hard_block(self, tmp_path, monkeypatch):
        """EXHAUSTED entries inside ## RECENT_LESSONS produce a hard forbidden block."""
        import core.agent_workers as aw
        f = tmp_path / "experience_pool.md"
        f.write_text(
            "## PARAMETER_TUNING\n"
            "- old constant tuning " + HARD_GATE_MARKER + "\n"
            "## RECENT_LESSONS\n"
            "- new fold logic is exhausted " + POSSIBLY_MARKER + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(aw, "EXPERIENCE_FILE", f)
        block = aw._extract_exhausted_block()
        assert "<forbidden_directions>" in block
        assert "<advisory_directions>" in block
        # RECENT line in forbidden block, old line in advisory block
        assert "new fold logic" in block
        assert "old constant tuning" in block
        # forbidden must come before advisory
        assert block.index("<forbidden_directions>") < block.index("<advisory_directions>")

    def test_recent_single_generation_downgraded_to_advisory(self, tmp_path, monkeypatch):
        """A RECENT_LESSONS EXHAUSTED entry that references only the CURRENT or
        PREVIOUS generation is downgraded to advisory — NOT a hard ban.

        Rationale (agent_workers._extract_exhausted_block): RECENT_LESSONS can
        contain a just-created single-generation mechanism marked [POSSIBLY
        EXHAUSTED] before the consolidator has 3+ consecutive-generation evidence.
        Banning such a direction hard would make workers auto-reject Master-
        authorized new directions (the audit's EXHAUSTED over-annotation P0).
        Only multi-generation RECENT evidence earns a hard <forbidden_directions>.
        """
        import core.agent_workers as aw

        f = tmp_path / "experience_pool.md"
        f.write_text(
            "## RECENT_LESSONS\n"
            "- v110 new barrel-continuation probe is exhausted " + POSSIBLY_MARKER + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(aw, "EXPERIENCE_FILE", f)
        # current_gen=111 → only_gen=110 >= 111-1 → downgrade_recent=True
        monkeypatch.setattr(aw, "find_current_v", lambda: 111)

        block = aw._extract_exhausted_block()

        # Single-generation recent entry → advisory, NOT hard ban.
        assert "<advisory_directions>" in block
        assert "<forbidden_directions>" not in block, (
            "single-generation RECENT entry must NOT be a hard ban"
        )
        # The advisory block text must explain the single-generation semantics.
        assert "single-generation" in block or "historical cautions" in block

    def test_recent_multi_generation_stays_hard(self, tmp_path, monkeypatch):
        """A RECENT_LESSONS EXHAUSTED entry referencing multiple (older) generations
        retains the hard ban — this is the earned-exhaustion case the tiering
        preserves."""
        import core.agent_workers as aw

        f = tmp_path / "experience_pool.md"
        f.write_text(
            "## RECENT_LESSONS\n"
            "- v105-v109 constant defensive-guard tuning exhausted " + POSSIBLY_MARKER + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(aw, "EXPERIENCE_FILE", f)
        monkeypatch.setattr(aw, "find_current_v", lambda: 111)

        block = aw._extract_exhausted_block()

        # Multi-generation (v105..v109, all < current-1) → hard ban retained.
        assert "<forbidden_directions>" in block
        assert "<advisory_directions>" not in block

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
        # swept into any constraint block.
        assert "Avoided the EXHAUSTED" not in block

    def test_empty_when_no_markers(self, tmp_path, monkeypatch):
        import core.agent_workers as aw
        f = tmp_path / "experience_pool.md"
        f.write_text("## GENERAL\n- No exhausted entries here.\n", encoding="utf-8")
        monkeypatch.setattr(aw, "EXPERIENCE_FILE", f)
        assert aw._extract_exhausted_block() == ""
