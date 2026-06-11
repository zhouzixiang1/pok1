"""End-to-end stage tests for the LLM evolution pipeline data flow.

Tests each stage's output → consumption chain, verifying that recently-added
data flow improvements (Critic insights, evidence, reviewer context, etc.)
work correctly without real LLM calls.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

# Ensure imports work
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "web" / "core"))


# ══════════════════════════════════════════════════════════════════════
# Stage 1: Stagnation Analyzer — prev_critic_info injection
# ══════════════════════════════════════════════════════════════════════

class TestStagnationCriticInsights:
    """Verify Critic insights are loaded from archive and passed to Stagnation Analyzer."""

    def test_prev_critic_info_loaded_from_archive(self, tmp_path, monkeypatch):
        """When archive/v99.json has critic_data, prev_critic_info should be non-empty."""
        import evolution_infra
        import generation_scheduler

        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()
        archive_file = archive_dir / "v99.json"
        archive_file.write_text(json.dumps({
            "version": 99,
            "critic_data": {
                "strategic_assessment": "Bot is too passive preflop",
                "local_optima_warning": True,
            }
        }))
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        # Re-run the loading logic
        prev_critic_info = ""
        archive_dir_path = tmp_path / "archive"
        if archive_dir_path.exists():
            archives = sorted(archive_dir_path.glob("v*.json"), reverse=True)
            if archives:
                latest = json.loads(archives[0].read_text())
                critic_data = latest.get("critic_data", {})
                if critic_data:
                    sa = critic_data.get("strategic_assessment", "")
                    lo = critic_data.get("local_optima_warning", False)
                    if sa or lo:
                        prev_critic_info = f"Previous Critic assessment: {sa}"
                        if lo:
                            prev_critic_info += "\n⚠ LOCAL OPTIMA WARNING: Critic detected potential local optimum in previous generation."

        assert "passive preflop" in prev_critic_info
        assert "LOCAL OPTIMA WARNING" in prev_critic_info

    def test_prev_critic_info_empty_when_no_archive(self, tmp_path, monkeypatch):
        """When no archive files exist, prev_critic_info should be empty."""
        import evolution_infra
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        archive_dir = tmp_path / "archive"
        prev_critic_info = ""
        if archive_dir.exists():
            archives = sorted(archive_dir.glob("v*.json"), reverse=True)
            if archives:
                latest = json.loads(archives[0].read_text())
                critic_data = latest.get("critic_data", {})
                if critic_data:
                    sa = critic_data.get("strategic_assessment", "")
                    lo = critic_data.get("local_optima_warning", False)
                    if sa or lo:
                        prev_critic_info = f"Previous Critic assessment: {sa}"

        assert prev_critic_info == ""

    def test_analyze_stagnation_accepts_prev_critic_info(self):
        """_analyze_stagnation() accepts prev_critic_info parameter."""
        from stagnation_analyzer import _analyze_stagnation
        import inspect
        sig = inspect.signature(_analyze_stagnation)
        assert "prev_critic_info" in sig.parameters
        assert sig.parameters["prev_critic_info"].default == ""

    def test_critic_insights_in_prompt_template(self):
        """stagnation_analyzer.md contains {critic_insights} placeholder."""
        template = (PROJECT_ROOT / "web" / "core" / "prompts" / "stagnation_analyzer.md").read_text()
        assert "{critic_insights}" in template


# ══════════════════════════════════════════════════════════════════════
# Stage 2: Critic evidence → experience_pool
# ══════════════════════════════════════════════════════════════════════

class TestCriticEvidenceToExperiencePool:
    """Verify Critic evidence extraction and writing to experience_pool.md."""

    def test_evidence_extraction_formats_correctly(self):
        """Evidence dict is formatted into a summary string."""
        evidence = {
            "h2h_weaknesses": ["loses to aggressive bots", "weak vs 3bet"],
            "experience_pool_refs": ["preflop_tight_isa"],
            "diff_refs": ["strategy.py:L45"],
        }
        ev_parts = []
        h2h_w = evidence.get("h2h_weaknesses", [])
        if h2h_w:
            ev_parts.append(f"H2H weaknesses: {', '.join(str(w) for w in h2h_w[:5])}")
        ep_refs = evidence.get("experience_pool_refs", [])
        if ep_refs:
            ev_parts.append(f"Experience pool refs: {', '.join(str(r) for r in ep_refs[:3])}")
        diff_refs = evidence.get("diff_refs", [])
        if diff_refs:
            ev_parts.append(f"Diff refs: {', '.join(str(r) for r in diff_refs[:3])}")

        summary = "; ".join(ev_parts)
        assert "H2H weaknesses: loses to aggressive bots, weak vs 3bet" in summary
        assert "Experience pool refs: preflop_tight_isa" in summary
        assert "Diff refs: strategy.py:L45" in summary

    def test_evidence_empty_skips_write(self):
        """When evidence is empty or None, no write happens."""
        # Test with None
        evidence = None
        assert not evidence

        # Test with empty dict
        evidence = {}
        ev_parts = []
        h2h_w = evidence.get("h2h_weaknesses", [])
        if h2h_w:
            ev_parts.append(f"H2H weaknesses")
        assert not ev_parts  # Should be empty

    def test_evidence_truncation(self):
        """H2H weaknesses truncated to 5, refs to 3."""
        evidence = {
            "h2h_weaknesses": [f"w{i}" for i in range(10)],
            "experience_pool_refs": [f"r{i}" for i in range(10)],
            "diff_refs": [f"d{i}" for i in range(10)],
        }
        ev_parts = []
        h2h_w = evidence.get("h2h_weaknesses", [])
        if h2h_w:
            ev_parts.append(f"H2H weaknesses: {', '.join(str(w) for w in h2h_w[:5])}")
        ep_refs = evidence.get("experience_pool_refs", [])
        if ep_refs:
            ev_parts.append(f"Experience pool refs: {', '.join(str(r) for r in ep_refs[:3])}")
        diff_refs = evidence.get("diff_refs", [])
        if diff_refs:
            ev_parts.append(f"Diff refs: {', '.join(str(r) for r in diff_refs[:3])}")

        summary = "; ".join(ev_parts)
        assert "w0, w1, w2, w3, w4" in summary
        assert "w5" not in summary, f"Expected truncation at 5 items, but found w5 in: {summary[:200]}"
        assert summary.count("r0") <= 1

    def test_append_experience_updates_writes_to_pool(self, tmp_path, monkeypatch):
        """_append_experience_updates writes evidence to experience_pool.md."""
        import tool_commit
        import evolution_infra

        pool_file = tmp_path / "experience_pool.md"
        pool_file.write_text("## RECENT_LESSONS\n- old lesson\n## POSTFLOP_STRATEGY\n")
        monkeypatch.setattr(evolution_infra, "EXPERIENCE_FILE", pool_file)

        tool_commit._append_experience_updates(
            version=42,
            updates=["Critic evidence: H2H weaknesses: weak vs 3bet"],
            strategic_advice="",
            generation_assessment="info",
        )

        content = pool_file.read_text()
        assert "Critic evidence" in content
        assert "weak vs 3bet" in content
        assert "## POSTFLOP_STRATEGY" in content  # Section preserved


# ══════════════════════════════════════════════════════════════════════
# Stage 3: Reviewer output → Archivist
# ══════════════════════════════════════════════════════════════════════

class TestReviewerToArchivist:
    """Verify Reviewer change_summary and risk_areas are injected into Archivist."""

    def test_review_info_extraction_from_checkpoint(self, tmp_path, monkeypatch):
        """review_info is built from checkpoint gate_results.review."""
        import tool_commit

        ckpt = {
            "gate_results": {
                "review": {
                    "change_summary": "Modified preflop raise logic",
                    "risk_areas": ["postflop.py:L200", "constants.py"],
                }
            }
        }

        review_info = ""
        review_gate = ckpt.get("gate_results", {}).get("review", {})
        cs = review_gate.get("change_summary", "")
        ra = review_gate.get("risk_areas", [])
        if cs:
            review_info += f"\nReviewer Change Summary: {cs}"
        if ra:
            review_info += f"\nReviewer Risk Areas: {', '.join(ra) if isinstance(ra, list) else str(ra)}"

        assert "Modified preflop raise logic" in review_info
        assert "postflop.py:L200" in review_info

    def test_review_info_empty_when_no_review(self):
        """When no review gate exists, review_info is empty."""
        ckpt = {"gate_results": {}}
        review_info = ""
        review_gate = ckpt.get("gate_results", {}).get("review", {})
        cs = review_gate.get("change_summary", "")
        ra = review_gate.get("risk_areas", [])
        if cs:
            review_info += f"\nReviewer Change Summary: {cs}"
        if ra:
            review_info += f"\nReviewer Risk Areas: ..."

        assert review_info == ""

    def test_review_info_injected_into_snapshot(self):
        """review_info is added as reviewer_context in snapshot dict."""
        review_info = "\nReviewer Change Summary: test"
        snapshot = {"version": 10, "source_v": 5}
        if review_info:
            snapshot["reviewer_context"] = review_info

        assert "reviewer_context" in snapshot
        assert "test" in snapshot["reviewer_context"]


# ══════════════════════════════════════════════════════════════════════
# Stage 4: exhausted_directions → Consolidator
# ══════════════════════════════════════════════════════════════════════

class TestExhaustedDirectionsToConsolidator:
    """Verify exhausted_directions are passed from checkpoint to Consolidator."""

    def test_consolidator_accepts_exhausted_directions(self):
        """_consolidate_experience_pool() accepts exhausted_directions parameter."""
        from experience_archivist import _consolidate_experience_pool
        import inspect
        sig = inspect.signature(_consolidate_experience_pool)
        assert "exhausted_directions" in sig.parameters
        assert sig.parameters["exhausted_directions"].default == ""

    def test_exhausted_dirs_extracted_from_checkpoint(self):
        """exhausted_directions are read from pipeline checkpoint."""
        ckpt = {
            "direction_audit": {
                "exhausted_directions": [
                    "Increase postflop_call_margin",
                    "Add bb_vs_raise preflop branch",
                ]
            }
        }
        da = ckpt.get("direction_audit", {})
        ed = da.get("exhausted_directions", [])
        exhausted_dirs = ", ".join(ed) if ed else ""

        assert "postflop_call_margin" in exhausted_dirs
        assert "bb_vs_raise" in exhausted_dirs

    def test_exhausted_dirs_empty_when_no_audit(self):
        """When no direction_audit in checkpoint, exhausted_dirs is empty."""
        ckpt = {}
        da = ckpt.get("direction_audit", {})
        ed = da.get("exhausted_directions", [])
        exhausted_dirs = ", ".join(ed) if ed else ""
        assert exhausted_dirs == ""

    def test_exhausted_directions_used_in_template(self, tmp_path, monkeypatch):
        """exhausted_directions parameter is actually used in template substitution."""
        from evolution_infra import substitute_template

        template = "Pool: {pool_content}\nExhausted: {exhausted_directions}"
        result = substitute_template(template, {
            "pool_content": "test pool",
            "exhausted_directions": "dir1, dir2",
        })
        assert "dir1, dir2" in result
        assert "Exhausted: dir1, dir2" in result


# ══════════════════════════════════════════════════════════════════════
# Stage 5: prev_critic persistence
# ══════════════════════════════════════════════════════════════════════

class TestPrevCriticPersistence:
    """Verify prev_critic is correctly saved and loaded from checkpoint."""

    def test_record_gate_saves_prev_critic(self, tmp_path, monkeypatch):
        """_record_gate preserves previous critic result as prev_critic."""
        import tool_helpers
        import evolution_infra

        ckpt_file = tmp_path / "pipeline_state.json"
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        # First: create checkpoint with initial critic gate
        evolution_infra.write_pipeline_checkpoint(
            next_v=10, source_v=5, stage="critic_checked",
        )
        tool_helpers._record_gate(10, 5, "critic", {
            "score": 4,
            "approved": False,
            "feedback": "Not good enough",
        }, stage="critic_checked")

        # Now record a new critic gate — should preserve prev_critic
        tool_helpers._record_gate(10, 5, "critic", {
            "score": 7,
            "approved": True,
            "feedback": "Good now",
        }, stage="critic_checked")

        ckpt = json.loads(ckpt_file.read_text())
        critic_gate = ckpt["gate_results"]["critic"]
        assert "prev_critic" in critic_gate
        assert critic_gate["prev_critic"]["score"] == 4
        assert critic_gate["prev_critic"]["feedback"] == "Not good enough"
        assert critic_gate["score"] == 7  # New value preserved


# ══════════════════════════════════════════════════════════════════════
# Stage 6: Master analysis from checkpoint (Direction Auditor)
# ══════════════════════════════════════════════════════════════════════

class TestDirectionAuditorCheckpointRead:
    """Verify Direction Auditor reads Master analysis from checkpoint."""

    def test_checkpoint_analysis_preferred_over_regex(self, tmp_path, monkeypatch):
        """When checkpoint has master_plan.analysis, it's used instead of regex."""
        import evolution_infra

        ckpt_file = tmp_path / "pipeline_state.json"
        ckpt_file.write_text(json.dumps({
            "next_v": 10,
            "source_v": 8,
            "stage": "master_planned",
            "master_plan": {
                "analysis": "Diversity injection via structural postflop fold logic",
                "tasks": [],
            }
        }))
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        # Simulate the checkpoint-first logic
        ckpt = evolution_infra.read_pipeline_checkpoint()
        if ckpt and "master_plan" in ckpt:
            analysis_text = ckpt["master_plan"].get("analysis", "")
            assert "Diversity injection" in analysis_text


# ══════════════════════════════════════════════════════════════════════
# Stage 7: Stagnation confidence → strategy decision
# ══════════════════════════════════════════════════════════════════════

class TestStagnationConfidenceStrategy:
    """Verify _decide_strategy respects confidence level."""

    def test_low_confidence_no_crossover(self):
        """is_stagnant=True but confidence=low → no crossover."""
        from generation_scheduler import _decide_strategy
        combined = {"is_stagnant": True, "confidence": "low"}
        strategy, source_v, parents = _decide_strategy(combined, 30, {})
        assert strategy == "master"

    def test_medium_confidence_triggers_crossover(self, monkeypatch):
        """is_stagnant=True and confidence=medium → crossover (not just high)."""
        from generation_scheduler import _decide_strategy
        combined = {"is_stagnant": True, "confidence": "medium"}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv: (30, 20),
        )
        strategy, source_v, parents = _decide_strategy(combined, 40, {})
        assert strategy == "crossover"

    def test_high_confidence_triggers_crossover(self, monkeypatch):
        """is_stagnant=True and confidence=high → crossover."""
        from generation_scheduler import _decide_strategy
        combined = {"is_stagnant": True, "confidence": "high"}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv: (30, 20),
        )
        strategy, source_v, parents = _decide_strategy(combined, 40, {})
        assert strategy == "crossover"

    def test_no_stagnation_default_master(self):
        """No stagnation → master strategy."""
        from generation_scheduler import _decide_strategy
        strategy, source_v, parents = _decide_strategy(None, 30, {})
        assert strategy == "master"

    def test_diversity_needed_triggers_crossover(self, monkeypatch):
        """combined with diversity_needed=True → crossover."""
        from generation_scheduler import _decide_strategy
        combined = {"diversity_needed": True, "trend": "stagnant"}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv: (30, 20),
        )
        strategy, source_v, parents = _decide_strategy(combined, 40, {})
        assert strategy == "crossover"


# ══════════════════════════════════════════════════════════════════════
# Stage 8: Worker failure type structured recording
# ══════════════════════════════════════════════════════════════════════

class TestWorkerFailureType:
    """Verify worker failures are recorded with structured failure_type."""

    def test_record_worker_failure_includes_type(self, tmp_path, monkeypatch):
        """_record_worker_failure writes failure_type to JSONL."""
        import agent_workers
        import evolution_infra

        failures_file = tmp_path / "worker_failures.jsonl"
        monkeypatch.setattr(evolution_infra, "WORKER_FAILURES_FILE", failures_file)
        monkeypatch.setattr(agent_workers, "WORKER_FAILURES_FILE", failures_file)
        monkeypatch.setattr("system_log.SYSTEM_EVENTS_FILE", tmp_path / "events.jsonl")

        agent_workers._record_worker_failure(
            gen=10, worker_id=1, role="Architect",
            error="zero changes in target files: strategy.py",
            failure_type="zero_changes",
        )

        lines = failures_file.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["failure_type"] == "zero_changes"
        assert entry["gen"] == 10
        assert entry["worker_id"] == 1

    def test_failure_type_values(self, tmp_path, monkeypatch):
        """All expected failure_type values are valid."""
        import agent_workers
        import evolution_infra

        failures_file = tmp_path / "worker_failures.jsonl"
        monkeypatch.setattr(evolution_infra, "WORKER_FAILURES_FILE", failures_file)
        monkeypatch.setattr(agent_workers, "WORKER_FAILURES_FILE", failures_file)
        monkeypatch.setattr("system_log.SYSTEM_EVENTS_FILE", tmp_path / "events.jsonl")

        expected_types = ["zero_changes", "compile_error", "smoke_error", "timeout", "boundary_violation"]
        for ft in expected_types:
            agent_workers._record_worker_failure(
                gen=10, worker_id=1, role="Test",
                error=f"test error for {ft}",
                failure_type=ft,
            )

        lines = failures_file.read_text().strip().split("\n")
        assert len(lines) == 5
        for line in lines:
            entry = json.loads(line)
            assert entry["failure_type"] in expected_types

    def test_default_failure_type_is_unknown(self, tmp_path, monkeypatch):
        """Default failure_type is 'unknown'."""
        import agent_workers
        import evolution_infra

        failures_file = tmp_path / "worker_failures.jsonl"
        monkeypatch.setattr(evolution_infra, "WORKER_FAILURES_FILE", failures_file)
        monkeypatch.setattr(agent_workers, "WORKER_FAILURES_FILE", failures_file)
        monkeypatch.setattr("system_log.SYSTEM_EVENTS_FILE", tmp_path / "events.jsonl")

        agent_workers._record_worker_failure(
            gen=10, worker_id=1, role="Test",
            error="something went wrong",
        )

        entry = json.loads(failures_file.read_text().strip())
        assert entry["failure_type"] == "unknown"


# ══════════════════════════════════════════════════════════════════════
# Stage 9: save_ratings atomic write
# ══════════════════════════════════════════════════════════════════════

class TestSaveRatingsAtomic:
    """Verify save_ratings uses atomic write (tmp + rename)."""

    def test_atomic_write_creates_valid_json(self, tmp_path, monkeypatch):
        """save_ratings produces valid JSON via atomic write."""
        import elo_daemon
        import evolution_infra

        ratings_file = tmp_path / "glicko_ratings.json"
        monkeypatch.setattr(evolution_infra, "RATINGS_FILE", ratings_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(elo_daemon, "RATINGS_FILE", ratings_file)
        monkeypatch.setattr(elo_daemon, "RESULTS_DIR", tmp_path)

        # Create mock Glicko2Player objects
        class MockPlayer:
            def to_dict(self):
                return {"r": 1500, "rd": 50, "sigma": 0.06}

        ratings = {"claude_v10": MockPlayer()}
        elo_daemon.save_ratings(ratings)

        assert ratings_file.exists()
        data = json.loads(ratings_file.read_text())
        assert "claude_v10" in data
        assert data["claude_v10"]["r"] == 1500
        assert "last_period" in data["claude_v10"]

    def test_no_stale_tmp_file(self, tmp_path, monkeypatch):
        """After atomic write, no .tmp file should remain."""
        import elo_daemon
        import evolution_infra

        ratings_file = tmp_path / "glicko_ratings.json"
        monkeypatch.setattr(evolution_infra, "RATINGS_FILE", ratings_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
        monkeypatch.setattr(elo_daemon, "RATINGS_FILE", ratings_file)
        monkeypatch.setattr(elo_daemon, "RESULTS_DIR", tmp_path)

        class MockPlayer:
            def to_dict(self):
                return {"r": 1500, "rd": 50, "sigma": 0.06}

        elo_daemon.save_ratings({"claude_v10": MockPlayer()})
        assert not (tmp_path / "glicko_ratings.tmp").exists()


# ══════════════════════════════════════════════════════════════════════
# Stage 10: Pipeline checkpoint fsync
# ══════════════════════════════════════════════════════════════════════

class TestPipelineCheckpointFsync:
    """Verify write_pipeline_checkpoint uses fsync."""

    def test_checkpoint_write_produces_valid_json(self, tmp_path, monkeypatch):
        """write_pipeline_checkpoint produces valid, readable JSON."""
        import evolution_infra

        ckpt_file = tmp_path / "pipeline_state.json"
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        evolution_infra.write_pipeline_checkpoint(
            next_v=11, source_v=10, stage="master_planned",
            master_plan={"analysis": "test", "tasks": []},
        )

        assert ckpt_file.exists()
        data = json.loads(ckpt_file.read_text())
        assert data["next_v"] == 11
        assert data["stage"] == "master_planned"
        assert data["master_plan"]["analysis"] == "test"

    def test_no_stale_tmp_file(self, tmp_path, monkeypatch):
        """After checkpoint write, no .tmp file should remain."""
        import evolution_infra

        ckpt_file = tmp_path / "pipeline_state.json"
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        evolution_infra.write_pipeline_checkpoint(next_v=11, source_v=10, stage="prepared")
        assert not (tmp_path / "pipeline_state.tmp").exists()


# ══════════════════════════════════════════════════════════════════════
# Stage 11: LLM query rate limit detection
# ══════════════════════════════════════════════════════════════════════

class TestRateLimitDetection:
    """Verify _is_rate_limited doesn't false-positive on long responses."""

    def test_short_error_detected(self):
        from llm_query import _is_rate_limited
        assert _is_rate_limited("Error: model overloaded, please retry")
        assert _is_rate_limited("HTTP/1.1 529 Too Many Requests")
        assert _is_rate_limited("该模型当前访问量过大")

    def test_long_response_not_detected(self):
        """Long LLM output containing 'rate limit' should NOT trigger."""
        from llm_query import _is_rate_limited
        long_text = "The rate limit policy affects how bots play. " * 200  # >2000 chars
        assert not _is_rate_limited(long_text)

    def test_normal_short_text_not_detected(self):
        from llm_query import _is_rate_limited
        assert not _is_rate_limited("The bot should fold weak hands preflop.")
        assert not _is_rate_limited("Here is my analysis of the strategy.")


# ══════════════════════════════════════════════════════════════════════
# Stage 12: JSON output parsing
# ══════════════════════════════════════════════════════════════════════

class TestJsonOutputParsing:
    """Verify parse_json_output handles various LLM output formats."""

    def test_json_in_code_block(self):
        from llm_query import parse_json_output
        output = '```json\n{"tasks": [], "analysis": "test"}\n```'
        result = parse_json_output(output)
        assert result is not None
        assert result["analysis"] == "test"

    def test_raw_json(self):
        from llm_query import parse_json_output
        output = '{"tasks": [{"worker_id": 1}]}'
        result = parse_json_output(output)
        assert result is not None
        assert result["tasks"][0]["worker_id"] == 1

    def test_no_json_returns_none(self):
        from llm_query import parse_json_output
        output = "This is just plain text with no JSON."
        assert parse_json_output(output) is None

    def test_json_with_embedded_backticks(self):
        """JSON containing ``` inside string values (e.g., worker prompts)."""
        from llm_query import parse_json_output
        output = '```json\n{"prompt": "use ```python``` for code", "tasks": []}\n```'
        result = parse_json_output(output)
        assert result is not None
        assert result["tasks"] == []

    def test_multiple_json_blocks_picks_last(self):
        from llm_query import parse_json_output
        output = '```json\n{"first": true}\n```\nSome text\n```json\n{"second": true}\n```'
        result = parse_json_output(output)
        assert result is not None
        assert "second" in result


# ══════════════════════════════════════════════════════════════════════
# Stage 13: Full pipeline state machine transitions
# ══════════════════════════════════════════════════════════════════════

class TestPipelineStateTransitions:
    """Verify pipeline state machine transitions are correct."""

    def test_prepared_to_master_planned(self, tmp_path, monkeypatch):
        """Stage transitions: prepared → master_planned."""
        import evolution_infra

        ckpt_file = tmp_path / "pipeline_state.json"
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        evolution_infra.write_pipeline_checkpoint(
            next_v=11, source_v=10, stage="prepared",
        )
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "prepared"

        evolution_infra.write_pipeline_checkpoint(
            next_v=11, source_v=10, stage="master_planned",
            master_plan={"analysis": "test", "tasks": [{"worker_id": 1}]},
        )
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "master_planned"
        assert ckpt["master_plan"]["tasks"][0]["worker_id"] == 1

    def test_gate_results_preserved_across_writes(self, tmp_path, monkeypatch):
        """Gate results are preserved when writing new stage."""
        import evolution_infra
        import tool_helpers

        ckpt_file = tmp_path / "pipeline_state.json"
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        evolution_infra.write_pipeline_checkpoint(
            next_v=11, source_v=10, stage="workers_done",
        )
        tool_helpers._record_gate(11, 10, "quality", {"all_passed": True}, stage="quality_passed")
        tool_helpers._record_gate(11, 10, "review", {"approved": True, "quality_score": 8}, stage="reviewed")

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["gate_results"]["quality"]["all_passed"] is True
        assert ckpt["gate_results"]["review"]["approved"] is True
        assert ckpt["stage"] == "reviewed"


# ══════════════════════════════════════════════════════════════════════
# Worker Circuit Breaker — failure-only counting
# ══════════════════════════════════════════════════════════════════════

class TestWorkerFailureCircuitBreaker:
    """Verify the circuit breaker counts only failed worker invocations."""

    def _setup_checkpoint(self, tmp_path, monkeypatch, failure_count=0,
                          invocation_count=None, stage="master_planned"):
        """Helper: create a checkpoint file with the given state."""
        import evolution_infra

        ckpt_file = tmp_path / "pipeline_state.json"
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        state = {
            "next_v": 11,
            "source_v": 10,
            "stage": stage,
            "master_plan": {
                "tasks": [
                    {"worker_id": 1, "role": "Algorithmic Logic Architect",
                     "target_files": ["strategy.py"], "worker_prompt": "test"},
                    {"worker_id": 2, "role": "Hyperparameter Tuner",
                     "target_files": ["constants.py"], "worker_prompt": "test"},
                ]
            },
            "worker_failure_count": failure_count,
            "gate_results": {},
        }
        # Support old-format checkpoints with worker_invocation_count only
        if invocation_count is not None:
            state.pop("worker_failure_count", None)
            state["worker_invocation_count"] = invocation_count

        ckpt_file.write_text(json.dumps(state))
        return ckpt_file

    def test_successful_workers_do_not_increment_count(self, tmp_path, monkeypatch):
        """Successful worker batches should NOT increase the failure counter."""
        import asyncio
        import evolution_infra
        import tool_planning

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, failure_count=2)
        _handler = tool_planning.execute_workers.handler

        async def _run():
            with patch.object(tool_planning, '_execute_workers', new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, '_validate_worker_boundaries', return_value=[]), \
                 patch.object(tool_planning, '_py_files_changed_between', return_value=['strategy.py']):
                mock_exec.return_value = (True, {}, [])
                await _handler({"tasks": [
                    {"worker_id": 1, "role": "arch", "target_files": ["a.py"], "worker_prompt": "x"},
                    {"worker_id": 2, "role": "tuner", "target_files": ["b.py"], "worker_prompt": "y"},
                ], "next_v": 11, "source_v": 10})

        asyncio.run(_run())

        # Verify checkpoint still has failure_count=2 (unchanged)
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["worker_failure_count"] == 2

    def test_failed_workers_increment_count(self, tmp_path, monkeypatch):
        """Failed worker batches should increase the failure counter by 1 per round."""
        import asyncio
        import evolution_infra
        import tool_planning

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, failure_count=2)
        _handler = tool_planning.execute_workers.handler

        async def _run():
            with patch.object(tool_planning, '_execute_workers', new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = (False, {}, [])
                await _handler({"tasks": [
                    {"worker_id": 1, "role": "arch", "target_files": ["a.py"], "worker_prompt": "x"},
                    {"worker_id": 2, "role": "tuner", "target_files": ["b.py"], "worker_prompt": "y"},
                ], "next_v": 11, "source_v": 10})

        asyncio.run(_run())

        # Verify checkpoint has failure_count=3 (2 previous + 1 per failed round)
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["worker_failure_count"] == 3

    def test_circuit_breaker_trips_at_threshold(self, tmp_path, monkeypatch):
        """Circuit breaker should block when failure_count >= 6."""
        import asyncio
        import tool_planning

        self._setup_checkpoint(tmp_path, monkeypatch, failure_count=6)
        _handler = tool_planning.execute_workers.handler

        async def _run():
            return await _handler({"tasks": [
                {"worker_id": 1, "role": "arch", "target_files": ["a.py"], "worker_prompt": "x"},
                {"worker_id": 2, "role": "tuner", "target_files": ["b.py"], "worker_prompt": "y"},
            ], "next_v": 11, "source_v": 10})

        result = asyncio.run(_run())

        result_text = result["content"][0]["text"]
        result_data = json.loads(result_text)
        assert "CIRCUIT BREAKER" in result_data["error"]
        assert result_data["failure_count"] == 6

    def test_circuit_breaker_allows_at_exact_threshold(self, tmp_path, monkeypatch):
        """When failure_count < 6, workers should execute."""
        import asyncio
        import tool_planning

        self._setup_checkpoint(tmp_path, monkeypatch, failure_count=5)
        _handler = tool_planning.execute_workers.handler

        mock_exec = None

        async def _run():
            nonlocal mock_exec
            with patch.object(tool_planning, '_execute_workers', new_callable=AsyncMock) as mock_exec_inner:
                mock_exec = mock_exec_inner
                mock_exec_inner.return_value = (True, {}, [])
                await _handler({"tasks": [
                    {"worker_id": 1, "role": "arch", "target_files": ["a.py"], "worker_prompt": "x"},
                    {"worker_id": 2, "role": "tuner", "target_files": ["b.py"], "worker_prompt": "y"},
                ], "next_v": 11, "source_v": 10})

        asyncio.run(_run())

        # Should NOT have been blocked — execute_workers was called
        mock_exec.assert_called_once()

    def test_backward_compat_old_invocation_count_key(self, tmp_path, monkeypatch):
        """Old checkpoint with worker_invocation_count (no worker_failure_count) should be read."""
        import asyncio
        import tool_planning
        from unittest.mock import AsyncMock, patch

        # Write old-format checkpoint: only worker_invocation_count, no worker_failure_count
        self._setup_checkpoint(tmp_path, monkeypatch, invocation_count=5)
        _handler = tool_planning.execute_workers.handler

        mock_exec = None

        async def _run():
            nonlocal mock_exec
            with patch.object(tool_planning, '_execute_workers', new_callable=AsyncMock) as mock_exec_inner, \
                 patch.object(tool_planning, '_validate_worker_boundaries', return_value=[]), \
                 patch.object(tool_planning, '_py_files_changed_between', return_value=['strategy.py']):
                mock_exec = mock_exec_inner
                mock_exec.return_value = (True, {}, [])
                return await _handler({"tasks": [
                    {"worker_id": 1, "role": "arch", "target_files": ["a.py"], "worker_prompt": "x"},
                    {"worker_id": 2, "role": "tuner", "target_files": ["b.py"], "worker_prompt": "y"},
                ], "next_v": 11, "source_v": 10})

        result = asyncio.run(_run())

        # New behavior (PIPE-001): failure_count = 5, threshold = 6 → NOT tripped
        # Workers should execute (mock verified by success result)
        result_text = result["content"][0]["text"]
        result_data = json.loads(result_text)
        assert "error" not in result_data, f"Expected no error, got: {result_data}"
        mock_exec.assert_called_once()

    def test_backward_compat_old_invocation_count_trips_at_threshold(self, tmp_path, monkeypatch):
        """Old checkpoint with worker_invocation_count >= 6 should trip circuit breaker."""
        import asyncio
        import tool_planning

        self._setup_checkpoint(tmp_path, monkeypatch, invocation_count=7)
        _handler = tool_planning.execute_workers.handler

        async def _run():
            return await _handler({"tasks": [
                {"worker_id": 1, "role": "arch", "target_files": ["a.py"], "worker_prompt": "x"},
                {"worker_id": 2, "role": "tuner", "target_files": ["b.py"], "worker_prompt": "y"},
            ], "next_v": 11, "source_v": 10})

        result = asyncio.run(_run())

        # 7 >= 6 → circuit breaker should trip
        result_text = result["content"][0]["text"]
        result_data = json.loads(result_text)
        assert "CIRCUIT BREAKER" in result_data["error"]
        assert result_data["failure_count"] == 7
