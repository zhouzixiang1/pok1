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

    def test_get_unanalyzed_filters_analyzed(self, history_file):
        """Returns only entries whose IDs have not been marked as analyzed."""
        entries = [
            {"id": "m1", "bot0": "v1", "bot1": "v2"},
            {"id": "m2", "bot0": "v2", "bot1": "v3"},
            {"id": "m3", "bot0": "v1", "bot1": "v3"},
        ]
        with open(history_file, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        be.mark_analyzed("m2")
        result = be.get_unanalyzed_matches(n=10)

        ids = [e["id"] for e in result]
        assert "m1" in ids
        assert "m3" in ids
        assert "m2" not in ids


# ── 3. test_get_unanalyzed_returns_empty_when_all_analyzed ──


class TestGetUnanalyzedEmpty:

    def test_get_unanalyzed_returns_empty_when_all_analyzed(self, history_file):
        """Empty list when every match has been analyzed."""
        entries = [
            {"id": "m1", "bot0": "v1", "bot1": "v2"},
            {"id": "m2", "bot0": "v2", "bot1": "v3"},
        ]
        with open(history_file, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

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

        # Match is still marked as analyzed (don't retry failed LLM calls)
        assert be.is_analyzed(match_id) is True
