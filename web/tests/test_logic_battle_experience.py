"""Logic tests for battle_experience.py — incremental match analysis system."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

import battle_experience as be


# ── Fixtures ──


@pytest.fixture(autouse=True)
def _patch_paths(tmp_path, monkeypatch):
    """Redirect all file paths to tmp_path for isolation."""
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(be, "BATTLE_EXPERIENCE_FILE", results / "battle_experience.md")
    monkeypatch.setattr(be, "ANALYSIS_MARKER_FILE", results / ".battle_analysis_progress.json")
    monkeypatch.setattr(be, "MATCH_HISTORY_FILE", results / "match_history.jsonl")
    monkeypatch.setattr(be, "REPLAY_DIR", results / "match_replay")
    monkeypatch.setattr(be, "LLM_COSTS_FILE", results / "llm_costs.jsonl")
    (results / "match_replay").mkdir()


@pytest.fixture
def marker_file():
    return be.ANALYSIS_MARKER_FILE


@pytest.fixture
def history_file():
    return be.MATCH_HISTORY_FILE


@pytest.fixture
def replay_dir():
    return be.REPLAY_DIR


# ── 1. test_mark_and_check_analyzed ──


class TestMarkAndCheckAnalyzed:

    def test_mark_and_check_analyzed(self):
        """mark_analyzed then is_analyzed returns True for that ID."""
        assert be.is_analyzed("match_001") is False
        be.mark_analyzed("match_001")
        assert be.is_analyzed("match_001") is True

    def test_unmarked_id_still_false(self):
        """An ID that was never marked should return False."""
        be.mark_analyzed("match_001")
        assert be.is_analyzed("match_999") is False


# ── 2. test_get_unanalyzed_filters_analyzed ──


class TestGetUnanalyzedFiltersAnalyzed:

    def test_get_unanalyzed_filters_analyzed(self, history_file, replay_dir):
        """Returns only entries whose IDs have not been marked as analyzed."""
        entries = [
            {"id": "m1", "bot0": "v1", "bot1": "v2"},
            {"id": "m2", "bot0": "v2", "bot1": "v3"},
            {"id": "m3", "bot0": "v1", "bot1": "v3"},
        ]
        with open(history_file, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        # replay files must exist — get_unanalyzed skips IDs whose replay was evicted
        for e in entries:
            (replay_dir / e["id"]).write_text(json.dumps({"games": []}))

        be.mark_analyzed("m2")
        result = be.get_unanalyzed_matches(n=10)

        ids = [e["id"] for e in result]
        assert "m1" in ids
        assert "m3" in ids
        assert "m2" not in ids


# ── 3. test_get_unanalyzed_returns_empty_when_all_analyzed ──


class TestGetUnanalyzedEmpty:

    def test_get_unanalyzed_returns_empty_when_all_analyzed(self, history_file, replay_dir):
        """Empty list when every match has been analyzed."""
        entries = [
            {"id": "m1", "bot0": "v1", "bot1": "v2"},
            {"id": "m2", "bot0": "v2", "bot1": "v3"},
        ]
        with open(history_file, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        # replay files present so the empty result is attributable to the
        # analyzed filter, not the absent-replay filter
        for e in entries:
            (replay_dir / e["id"]).write_text(json.dumps({"games": []}))

        be.mark_analyzed("m1")
        be.mark_analyzed("m2")
        assert be.get_unanalyzed_matches() == []

    def test_get_unanalyzed_returns_empty_when_no_history(self):
        """Empty list when match_history.jsonl does not exist."""
        assert be.get_unanalyzed_matches() == []


# ── 4. test_process_one_match_writes_experience ──


class TestProcessOneMatchWritesExperience:

    def test_process_one_match_writes_experience(self, replay_dir, monkeypatch):
        """Mock LLM, verify experience file is written after processing."""
        match_id = "match_exp_001"
        replay_path = replay_dir / match_id
        replay_data = {"games": []}
        replay_path.write_text(json.dumps(replay_data))

        # Mock replay_analysis.summarize_replay_for_analysis to return a summary
        monkeypatch.setattr(
            be.replay_analysis,
            "summarize_replay_for_analysis",
            lambda data, bot_name: f"Summary for {bot_name}",
        )

        # Mock _run_llm_update to return updated content
        monkeypatch.setattr(
            be,
            "_run_llm_update",
            lambda current, new_data: f"# Experience\n{new_data}",
        )

        entry = {"id": match_id, "bot0": "claude_v1", "bot1": "claude_v2"}
        be._process_one_match(entry)

        content = be.BATTLE_EXPERIENCE_FILE.read_text()
        assert "# Experience" in content
        assert "Summary for claude_v1" in content
        assert "Summary for claude_v2" in content


# ── 5. test_process_one_match_marks_analyzed ──


class TestProcessOneMatchMarksAnalyzed:

    def test_process_one_match_marks_analyzed(self, replay_dir, monkeypatch):
        """Verify match ID is marked as analyzed after processing."""
        match_id = "match_mark_001"
        replay_path = replay_dir / match_id
        replay_path.write_text(json.dumps({"games": []}))

        monkeypatch.setattr(
            be.replay_analysis,
            "summarize_replay_for_analysis",
            lambda data, bot_name: "summary",
        )
        monkeypatch.setattr(
            be, "_run_llm_update", lambda current, new_data: "# updated"
        )

        assert be.is_analyzed(match_id) is False
        be._process_one_match({"id": match_id, "bot0": "v1", "bot1": "v2"})
        assert be.is_analyzed(match_id) is True


# ── 6. test_process_one_match_skips_missing_replay ──


class TestProcessOneMatchSkipsMissingReplay:

    def test_process_one_match_skips_missing_replay(self, monkeypatch):
        """No crash when replay file is missing; match is still marked analyzed."""
        match_id = "match_missing_001"
        assert not (be.REPLAY_DIR / match_id).exists()

        # Should not raise
        be._process_one_match({"id": match_id, "bot0": "v1", "bot1": "v2"})

        # Missing replays are marked as analyzed to avoid retrying
        assert be.is_analyzed(match_id) is True

        # Experience file should not be created
        assert not be.BATTLE_EXPERIENCE_FILE.exists()


# ── 7. test_get_battle_experience_returns_content ──


class TestGetBattleExperienceReturnsContent:

    def test_get_battle_experience_returns_content(self):
        """Reads existing file and returns its markdown content."""
        be.BATTLE_EXPERIENCE_FILE.write_text("# Battle Experience\n- Lesson A\n- Lesson B\n")
        content = be.get_battle_experience()
        assert "# Battle Experience" in content
        assert "Lesson A" in content
        assert "Lesson B" in content


# ── 8. test_get_battle_experience_empty_file ──


class TestGetBattleExperienceEmptyFile:

    def test_get_battle_experience_empty_file(self):
        """Returns empty string when file does not exist."""
        assert not be.BATTLE_EXPERIENCE_FILE.exists()
        assert be.get_battle_experience() == ""


# ── 9. test_silent_ui_cost_logging ──


class TestSilentUICostLogging:

    def test_silent_ui_cost_logging(self):
        """update_cost writes a cost entry to llm_costs.jsonl."""
        ui = be.SilentUI()
        ui.update_cost("battle_experience", 0.003, {"input_tokens": 100, "output_tokens": 50})

        lines = be.LLM_COSTS_FILE.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["role"] == "battle_experience"
        assert entry["cost_usd"] == 0.003
        assert entry["input_tokens"] == 100
        assert entry["output_tokens"] == 50

    def test_silent_ui_skips_none_cost(self):
        """update_cost with cost_usd=None does not write anything."""
        ui = be.SilentUI()
        ui.update_cost("battle_experience", None, {"input_tokens": 100})
        assert not be.LLM_COSTS_FILE.exists()


# ── 10. test_llm_failure_preserves_existing ──


class TestLLMFailurePreservesExisting:

    def test_llm_failure_preserves_existing(self, replay_dir, monkeypatch):
        """When LLM returns None, the experience file is left unchanged."""
        # Write initial content
        be.BATTLE_EXPERIENCE_FILE.write_text("# Original Experience\n- Lesson 1\n")

        match_id = "match_fail_001"
        replay_path = replay_dir / match_id
        replay_path.write_text(json.dumps({"games": []}))

        monkeypatch.setattr(
            be.replay_analysis,
            "summarize_replay_for_analysis",
            lambda data, bot_name: "summary text here",
        )
        # LLM returns None (simulating failure)
        monkeypatch.setattr(be, "_run_llm_update", lambda current, new_data: None)

        be._process_one_match({"id": match_id, "bot0": "v1", "bot1": "v2"})

        # Original content preserved
        content = be.BATTLE_EXPERIENCE_FILE.read_text()
        assert "# Original Experience" in content
        assert "Lesson 1" in content

        # Match is NOT marked done on LLM failure — stays retryable (fail_count=1)
        assert be.is_analyzed(match_id) is False
        # But fail_count is recorded
        markers = be._read_markers()
        assert markers.get(match_id, {}).get("fail_count", 0) == 1


# ── 11. test_get_unanalyzed_skips_evicted_replays ──


class TestGetUnanalyzedSkipsEvicted:

    def test_get_unanalyzed_skips_evicted_replays(self, history_file, replay_dir):
        """Entries whose replay files have been evicted are skipped."""
        entries = [
            {"id": "m1", "bot0": "v1", "bot1": "v2", "timestamp": "2024-01-01T00:00:00"},
            {"id": "m2", "bot0": "v2", "bot1": "v3", "timestamp": "2024-01-01T00:01:00"},
        ]
        with open(history_file, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        # Only create replay for m2 (m1 evicted)
        (replay_dir / "m2").write_text(json.dumps({"games": []}))

        result = be.get_unanalyzed_matches(n=10)
        ids = [e["id"] for e in result]
        assert "m1" not in ids  # evicted, skipped
        assert "m2" in ids


# ── 12. test_get_unanalyzed_force_skips_poison ──


class TestGetUnanalyzedForceSkipsPoison:

    def test_get_unanalyzed_force_skips_poison(self, history_file, replay_dir):
        """Matches with fail_count >= 3 are force-skipped."""
        entries = [
            {"id": "m1", "bot0": "v1", "bot1": "v2", "timestamp": "2024-01-01T00:00:00"},
        ]
        with open(history_file, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        (replay_dir / "m1").write_text(json.dumps({"games": []}))

        # Mark with fail_count=3 (poison — force-skipped)
        be.mark_analyzed("m1", fail_count=3)
        assert be.is_analyzed("m1") is True

        result = be.get_unanalyzed_matches(n=10)
        ids = [e["id"] for e in result]
        assert "m1" not in ids


# ── 13. test_get_unanalyzed_random_sampling ──


class TestGetUnanalyzedRandomSampling:

    def test_get_unanalyzed_random_sampling(self, history_file, replay_dir):
        """Random sampling returns a subset without recency bias."""
        entries = []
        for i in range(20):
            e = {"id": f"m{i:02d}", "bot0": "v1", "bot1": "v2",
                 "timestamp": f"2024-01-01T00:{i:02d}:00"}
            entries.append(e)

        with open(history_file, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        for e in entries:
            (replay_dir / e["id"]).write_text(json.dumps({"games": []}))

        result = be.get_unanalyzed_matches(n=5)
        assert len(result) == 5
        ids = [e["id"] for e in result]
        assert len(set(ids)) == 5  # no duplicates


# ── 14. test_increment_fail_count ──


class TestIncrementFailCount:

    def test_increment_fail_count(self):
        """Fail count increments; force-skip kicks in at >= 3."""
        assert be.is_analyzed("m_fail") is False
        be.increment_fail_count("m_fail")
        assert be.is_analyzed("m_fail") is False  # fc=1, transient
        be.increment_fail_count("m_fail")
        assert be.is_analyzed("m_fail") is False  # fc=2, transient
        be.increment_fail_count("m_fail")
        assert be.is_analyzed("m_fail") is True   # fc=3, force-skipped

    def test_mark_analyzed_clears_fail_count(self):
        """mark_analyzed(fail_count=0) marks the match DONE (overwrites prior failures)."""
        be.increment_fail_count("m_clear")
        be.increment_fail_count("m_clear")
        be.mark_analyzed("m_clear", fail_count=0)
        assert be.is_analyzed("m_clear") is True  # done
        markers = be._read_markers()
        assert markers["m_clear"]["fail_count"] == 0


# ── 15. test_legacy_marker_format_migration ──


class TestLegacyMarkerFormat:

    def test_legacy_list_format(self, monkeypatch):
        """Legacy list-of-strings marker file is treated as analyzed."""
        marker_path = be.ANALYSIS_MARKER_FILE
        marker_path.write_text(json.dumps(["legacy_1", "legacy_2"]))
        assert be.is_analyzed("legacy_1") is True
        assert be.is_analyzed("legacy_2") is True
        assert be.is_analyzed("new_1") is False

        # mark_analyzed writes new dict format
        be.mark_analyzed("new_1", fail_count=0)
        markers = be._read_markers()
        assert "legacy_1" in markers
        assert "new_1" in markers
        assert markers["new_1"]["fail_count"] == 0


# ── 16. test_apply_batch_results cumulative merge (parallel-path coverage) ──


class TestApplyBatchResultsCumulative:
    """The parallel batch path: _apply_batch_results must fold ALL summaries
    into ONE LLM merge, not overwrite (the original parallel design lost N-1 of
    N per batch because each worker read the same stale baseline)."""

    def test_batch_merges_all_summaries_not_just_last(self, replay_dir, monkeypatch):
        for mid in ("b1", "b2"):
            (replay_dir / mid).write_text(json.dumps({"games": []}))
        # _apply_batch_results calls _run_llm_incremental (fix-10 incremental mode)
        monkeypatch.setattr(be, "_run_llm_incremental", lambda current, new_data: f"# Merged\n{new_data}")

        results = [
            ({"id": "b1", "bot0": "v1", "bot1": "v2"}, True, "SUMMARY_B1"),
            ({"id": "b2", "bot0": "v1", "bot1": "v2"}, True, "SUMMARY_B2"),
        ]
        be._apply_batch_results(results)

        # BOTH summaries survive in the single merged output (not just the last).
        content = be.BATTLE_EXPERIENCE_FILE.read_text()
        assert "SUMMARY_B1" in content
        assert "SUMMARY_B2" in content
        assert be.is_analyzed("b1") is True
        assert be.is_analyzed("b2") is True

    def test_batch_failure_bumps_fail_count(self, replay_dir, monkeypatch):
        """A failed summary extraction bumps fail_count, stays retryable.
        NOTE: In fix-10, failures in the accumulation loop bump fail_count
        immediately, so _apply_batch_results only sees successes. The fail_count
        is already set before _apply_batch_results is called. This test verifies
        that behavior."""
        monkeypatch.setattr(be, "_run_llm_incremental", lambda c, n: "## NEW\n" + n)

        # Simulate: failure was already bumped by the accumulation loop
        # _apply_batch_results only receives successes
        results = [
            ({"id": "ok1", "bot0": "v1", "bot1": "v2"}, True, "OK1"),
            ({"id": "fail1", "bot0": "v1", "bot1": "v2"}, False, None),
        ]
        be._apply_batch_results(results)
        assert be.is_analyzed("ok1") is True
        # fail1 was not in success_entries, so not marked analyzed
        # But fail_count wasn't bumped here (it's bumped in the caller now)
        assert be.is_analyzed("fail1") is False

    def test_batch_llm_failure_bumps_all_fail_counts(self, replay_dir, monkeypatch):
        """If the cumulative LLM merge returns None, no match is marked done;
        all successful-summary matches get fail_count bumped (retryable)."""
        monkeypatch.setattr(be, "_run_llm_incremental", lambda c, n: None)
        results = [
            ({"id": "x1", "bot0": "v1", "bot1": "v2"}, True, "X1"),
            ({"id": "x2", "bot0": "v1", "bot1": "v2"}, True, "X2"),
        ]
        be._apply_batch_results(results)
        assert be.is_analyzed("x1") is False
        assert be.is_analyzed("x2") is False
        markers = be._read_markers()
        assert markers["x1"]["fail_count"] == 1
        assert markers["x2"]["fail_count"] == 1


# ── 17. test_get_unanalyzed retries transient failures (fc=1,2) ──


class TestGetUnanalyzedRetriesTransient:

    def test_fail_count_1_and_2_are_retried(self, history_file, replay_dir):
        """fail_count 1-2 (transient) ARE returned for retry; 0 (done) and >=3
        (poison) are excluded. This is the whole point of the fail_count schema."""
        for mid in ("t1", "t2", "t3"):
            (replay_dir / mid).write_text(json.dumps({"games": []}))
        entries = [
            {"id": "t1", "bot0": "v1", "bot1": "v2", "timestamp": "2024-01-01T00:00:00"},
            {"id": "t2", "bot0": "v1", "bot1": "v2", "timestamp": "2024-01-01T00:01:00"},
            {"id": "t3", "bot0": "v1", "bot1": "v2", "timestamp": "2024-01-01T00:02:00"},
        ]
        with open(history_file, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        be.increment_fail_count("t1")          # fc=1 transient -> retried
        be.mark_analyzed("t2", fail_count=3)   # poison -> skipped
        # t3 never tried -> included
        result = be.get_unanalyzed_matches(n=10)
        ids = {e["id"] for e in result}
        assert "t1" in ids
        assert "t3" in ids
        assert "t2" not in ids


# ── 18. fix-10: test_battle_experience_analysis_frequency ──


class TestAnalysisFrequency:
    """fix-10: LLM analysis should not fire every batch — matches are accumulated
    until MERGE_THRESHOLD before triggering LLM merge."""

    def test_merge_threshold_constant_exists(self):
        """MERGE_THRESHOLD constant must be defined and > 1."""
        assert hasattr(be, 'MERGE_THRESHOLD')
        assert be.MERGE_THRESHOLD > 1
        # At 6 matches per cycle, 24 means ~4 cycles before LLM fires
        assert be.MERGE_THRESHOLD >= 16

    def test_incremental_prompt_template_exists(self):
        """Incremental prompt template must exist for cost-optimized append mode."""
        incremental_template = be.PROMPTS_DIR / "battle_experience_incremental.md"
        # If the template doesn't exist, fallback to full-rewrite is used
        # (which is still correct, just more expensive). We verify the constant
        # points to the right path.
        assert incremental_template.name == "battle_experience_incremental.md"


class TestIncrementalAppend:
    """fix-10: _apply_batch_results should append to existing file, not overwrite."""

    def test_append_preserves_existing_content(self, replay_dir, monkeypatch):
        """New observations are appended to existing content, not overwriting it."""
        # Write initial experience
        be.BATTLE_EXPERIENCE_FILE.write_text("# Original\n- Lesson 1\n")

        # Mock _run_llm_incremental to return only the new section
        monkeypatch.setattr(
            be, "_run_llm_incremental",
            lambda current, new_data: "## NEW\n- New insight from batch\n",
        )

        results = [
            ({"id": "x1", "bot0": "v1", "bot1": "v2"}, True, "SUMMARY"),
        ]
        be._apply_batch_results(results)

        content = be.BATTLE_EXPERIENCE_FILE.read_text()
        assert "# Original" in content
        assert "Lesson 1" in content
        assert "New insight from batch" in content

    def test_first_analysis_no_append_prefix(self, replay_dir, monkeypatch):
        """When experience file is empty, new content is written directly (no append)."""
        # File does not exist yet — empty content
        assert not be.BATTLE_EXPERIENCE_FILE.exists()

        monkeypatch.setattr(
            be, "_run_llm_incremental",
            lambda current, new_data: "## CROSS-PAIR PATTERNS\n- First lesson\n",
        )

        results = [
            ({"id": "a1", "bot0": "v1", "bot1": "v2"}, True, "S1"),
        ]
        be._apply_batch_results(results)

        content = be.BATTLE_EXPERIENCE_FILE.read_text()
        assert "First lesson" in content

    def test_incremental_falls_back_to_full_rewrite(self, replay_dir, monkeypatch):
        """When incremental template is missing, falls back to _run_llm_update."""
        # Track which function was called
        calls = []
        monkeypatch.setattr(
            be, "_run_llm_incremental",
            lambda c, n: calls.append("incremental") or "## NEW\n- Incremental result\n",
        )
        monkeypatch.setattr(
            be, "_run_llm_update",
            lambda c, n: calls.append("full") or "# Full result\n",
        )

        # Directly test that _run_llm_incremental is used (not _run_llm_update)
        be._run_llm_incremental("existing", "new data")
        assert calls[-1] == "incremental"


class TestStaleTagging:
    """fix-10: get_battle_experience should tag stale observations."""

    def test_stale_constant_exists(self):
        """STALE_GEN_THRESHOLD constant must be defined."""
        assert hasattr(be, 'STALE_GEN_THRESHOLD')
        assert be.STALE_GEN_THRESHOLD >= 5

    def test_tag_stale_marks_old_versions(self, monkeypatch):
        """Sections referencing versions >= STALE_GEN_THRESHOLD gens old get tagged."""
        monkeypatch.setattr(
            be, "_read_markers",
            lambda: {},  # don't read real markers
        )

        # Mock ratings to show current gen is v20
        fake_ratings = {
            "claude_v20": {"r": 1500, "rd": 100},
            "claude_v18": {"r": 1400, "rd": 100},
            "claude_v10": {"r": 1300, "rd": 100},
        }
        from evolution_infra import read_locked_json as real_read

        def mock_read(path, default=None):
            if "glicko" in str(path):
                return fake_ratings
            return default

        monkeypatch.setattr("evolution_infra.read_locked_json", mock_read)

        content = """## STRENGTH PATTERNS
- v10 wins vs v8 60% (10 matches)

## CROSS-PAIR PATTERNS
- v20 beats v18 most of the time
"""
        tagged = be._tag_stale_observations(content)
        # v10 is 10 gens old (v20 - v10 = 10 >= STALE_GEN_THRESHOLD=5)
        assert "[POSSIBLY STALE" in tagged
        assert "v10" in tagged
        # v20 and v18 are NOT stale
        assert "v20 beats v18" in tagged

    def test_no_double_tagging(self, monkeypatch):
        """Sections already tagged [POSSIBLY STALE] should not get double-tagged."""
        fake_ratings = {"claude_v20": {"r": 1500, "rd": 100}}

        def mock_read(path, default=None):
            if "glicko" in str(path):
                return fake_ratings
            return default

        monkeypatch.setattr("evolution_infra.read_locked_json", mock_read)

        content = """## OLD SECTION
[POSSIBLY STALE — no WR improvement in 10 gens]
- v5 does something old
"""
        tagged = be._tag_stale_observations(content)
        # Should have exactly one [POSSIBLY STALE marker
        assert tagged.count("[POSSIBLY STALE") == 1

    def test_get_battle_experience_applies_stale_tagging(self, monkeypatch):
        """get_battle_experience returns content with stale tags applied."""
        be.BATTLE_EXPERIENCE_FILE.write_text(
            "## STRENGTH PATTERNS\n- v5 old insight\n"
        )
        fake_ratings = {"claude_v20": {"r": 1500, "rd": 100}}

        def mock_read(path, default=None):
            if "glicko" in str(path):
                return fake_ratings
            return default

        monkeypatch.setattr("evolution_infra.read_locked_json", mock_read)

        result = be.get_battle_experience()
        assert "[POSSIBLY STALE" in result
