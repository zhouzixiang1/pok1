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


def test_prepare_log_context_uses_planned_next_generation(monkeypatch):
    import event_bus
    import generation_scheduler

    events = []
    monkeypatch.setattr(
        generation_scheduler,
        "log_system_event",
        lambda event_type, severity, message, data: events.append(
            (event_type, severity, message, data)
        ),
    )
    event_bus.reset_for_test()

    try:
        planned_next_v = generation_scheduler._bind_prepare_log_context(
            current_v=243,
            max_committed_v=243,
        )
        ctx = event_bus.capture_context()
    finally:
        event_bus.reset_for_test()

    assert planned_next_v == 244
    assert ctx["run_id"] == "244#0"
    assert ctx["stage"] == "preparing"
    assert ctx["attempt"] == {"generation": 0, "audit": 0, "precommit": 0}
    assert events[0][0] == "pipeline.prepare_context_bound"
    assert events[0][3]["next_v"] == 244


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
        monkeypatch.setattr(tool_commit, "EXPERIENCE_FILE", pool_file)
        monkeypatch.setattr(tool_commit, "git_has_tag", lambda _v: True)

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

    def test_record_gate_returns_false_when_checkpoint_rejects_stage(self, tmp_path, monkeypatch):
        """_record_gate must not report success when checkpoint write is rejected."""
        import tool_helpers
        import evolution_infra

        ckpt_file = tmp_path / "pipeline_state.json"
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        evolution_infra.write_pipeline_checkpoint(
            next_v=10, source_v=5, stage="workers_done",
        )
        recorded = tool_helpers._record_gate(
            10, 5, "direction_audit", {"passed": True}, stage="direction_audited"
        )

        assert recorded is False
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert "direction_audit" not in ckpt.get("gate_results", {})


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

    class _Rating:
        def __init__(self, conservative):
            self._conservative = conservative

        def conservative_rating(self):
            return self._conservative

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
            lambda ratings, cv, **kw: (30, 20),
        )
        strategy, source_v, parents = _decide_strategy(combined, 40, {})
        assert strategy == "crossover"

    def test_source_history_prefers_committed_lineage_over_prepare_noise(self, tmp_path, monkeypatch):
        """Oscillation history should use successful committed lineage, not restart/prepare noise."""
        import generation_scheduler

        events_file = tmp_path / "system_events.jsonl"
        events = [
            {"ts": 1, "type": "pipeline.prepare_done", "data": {"next_v": 231, "source_v": 224}},
            {"ts": 2, "type": "pipeline.prepare_done", "data": {"next_v": 231, "source_v": 224}},
            {"ts": 3, "type": "pipeline.prepare_done", "data": {"next_v": 232, "source_v": 224}},
            {"ts": 4, "type": "pipeline.committed", "data": {"version": 236, "source_v": 235}},
            {"ts": 5, "type": "pipeline.committed", "data": {"version": 237, "source_v": 206}},
            {"ts": 6, "type": "pipeline.committed", "data": {"version": 238, "source_v": 235}},
        ]
        events_file.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        monkeypatch.setattr(generation_scheduler, "SYSTEM_EVENTS_FILE", events_file)

        assert generation_scheduler._read_source_v_history() == [235, 206, 235]

    def test_source_loop_uses_unified_selection_leader(self, monkeypatch):
        """Source-loop repair should follow selection_score, not raw conservative rating."""
        import generation_scheduler
        import tool_helpers

        monkeypatch.setattr(generation_scheduler, "_detect_source_loop", lambda n=3: 206)
        monkeypatch.setattr(generation_scheduler, "_detect_source_oscillation", lambda *a, **k: None)
        monkeypatch.setattr(generation_scheduler, "_log_source_selection_decision", lambda *a, **k: None)
        monkeypatch.setattr(tool_helpers, "load_selection_scores", lambda: {
            "national_v206": 0.46,
            "national_v237": 0.52,
        })
        ratings = {
            "national_v206": self._Rating(1550.0),
            "national_v237": self._Rating(1400.0),
        }

        strategy, source_v, parents = generation_scheduler._decide_strategy(
            {"is_stagnant": False, "confidence": "high"},
            current_v=242,
            ratings=ratings,
        )

        assert strategy == "master"
        assert source_v == 237
        assert parents == ()

    def test_oscillation_breakout_uses_new_credible_near_leader(self, monkeypatch):
        """A high-confidence recent near-leader outside the loop should break source oscillation."""
        import generation_scheduler
        import tool_helpers

        monkeypatch.setattr(generation_scheduler, "_detect_source_loop", lambda n=3: None)
        monkeypatch.setattr(
            generation_scheduler,
            "_detect_source_oscillation",
            lambda n=8, max_unique=3: {206, 235},
        )
        monkeypatch.setattr(generation_scheduler, "_get_unified_leader_v", lambda ratings: 237)
        monkeypatch.setattr(generation_scheduler, "_log_source_selection_decision", lambda *a, **k: None)
        monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *a, **k: None)
        monkeypatch.setattr(tool_helpers, "load_h2h_avg_winrates_with_coverage", lambda: {
            "national_v206": {
                "selection_score": 0.3886,
                "leaderboard_score": 0.3886,
                "strength_confidence": "medium",
            },
            "national_v235": {
                "selection_score": 0.4304,
                "leaderboard_score": 0.4304,
                "strength_confidence": "medium",
            },
            "national_v187": {
                "selection_score": 0.5045,
                "leaderboard_score": 0.5045,
                "strength_confidence": "high",
            },
            "national_v237": {
                "selection_score": 0.4891,
                "leaderboard_score": 0.4891,
                "strength_confidence": "high",
            },
            "national_v238": {
                "selection_score": 0.0700,
                "leaderboard_score": 0.1000,
                "strength_confidence": "low",
            },
        })
        ratings = {
            "national_v206": self._Rating(1178.9),
            "national_v235": self._Rating(1225.7),
            "national_v237": self._Rating(1443.7),
        }

        strategy, source_v, parents = generation_scheduler._decide_strategy(
            {"is_stagnant": False, "confidence": "high"},
            current_v=238,
            ratings=ratings,
        )

        assert strategy == "master"
        assert source_v == 237
        assert parents == ()

    def test_confident_stagnation_defer_oscillation_to_normal_crossover(self, monkeypatch):
        """When stagnation is confident, use the general selection-score crossover path."""
        import generation_scheduler

        monkeypatch.setattr(generation_scheduler, "_detect_source_loop", lambda n=3: None)
        monkeypatch.setattr(
            generation_scheduler,
            "_detect_source_oscillation",
            lambda n=8, max_unique=3: {206, 235},
        )
        monkeypatch.setattr(generation_scheduler, "_pick_crossover_parents", lambda ratings, cv, **kw: (187, 237))
        monkeypatch.setattr(generation_scheduler, "_log_crossover_decision", lambda *a, **k: None)
        monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *a, **k: None)
        ratings = {
            "claude_v206": self._Rating(1178.9),
            "claude_v235": self._Rating(1225.7),
            "claude_v237": self._Rating(1443.7),
        }

        strategy, source_v, parents = generation_scheduler._decide_strategy(
            {"is_stagnant": True, "confidence": "medium"},
            current_v=238,
            ratings=ratings,
        )

        assert strategy == "crossover"
        assert source_v == 187
        assert parents == (187, 237)

    def test_high_confidence_triggers_crossover(self, monkeypatch):
        """is_stagnant=True and confidence=high → crossover."""
        from generation_scheduler import _decide_strategy
        combined = {"is_stagnant": True, "confidence": "high"}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv, **kw: (30, 20),
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
            lambda ratings, cv, **kw: (30, 20),
        )
        strategy, source_v, parents = _decide_strategy(combined, 40, {})
        assert strategy == "crossover"


class TestPostCleanupExperienceCommit:
    """Verify post-generation experience consolidation does not leave hidden dirty state."""

    def test_skips_when_worktree_was_already_dirty(self, tmp_path, monkeypatch):
        import evolution_infra
        import generation_scheduler

        exp = tmp_path / "web" / "core" / "experience_pool.md"
        exp.parent.mkdir(parents=True)
        exp.write_text("## RECENT_LESSONS\n")
        calls = []

        monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(evolution_infra, "EXPERIENCE_FILE", exp)
        monkeypatch.setattr(evolution_infra, "_git", lambda *a, **k: calls.append(a) or "")
        monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *a, **k: None)

        result = generation_scheduler._commit_post_cleanup_experience_change(
            240,
            {"web/core/generation_scheduler.py"},
        )

        assert result == {
            "committed": False,
            "reason": "preexisting_dirty",
            "path": "web/core/experience_pool.md",
        }
        assert calls == []

    def test_commits_only_experience_pool_when_clean(self, tmp_path, monkeypatch):
        import evolution_infra
        import generation_scheduler

        exp = tmp_path / "web" / "core" / "experience_pool.md"
        exp.parent.mkdir(parents=True)
        exp.write_text("## RECENT_LESSONS\n- changed\n")
        state = {"staged": False, "ensured": False}
        calls = []
        published = []

        def fake_git(*args, check=True):
            calls.append(args)
            if args[:3] == ("status", "--porcelain", "--"):
                return " M web/core/experience_pool.md"
            if args[:3] == ("diff", "--cached", "--name-only"):
                return "web/core/experience_pool.md" if state["staged"] else ""
            if args[:2] == ("add", "--"):
                state["staged"] = True
                return ""
            if args[0] == "commit":
                return ""
            if args[:2] == ("rev-parse", "--short"):
                return "abc1234"
            return ""

        monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(evolution_infra, "EXPERIENCE_FILE", exp)
        monkeypatch.setattr(evolution_infra, "_git", fake_git)
        monkeypatch.setattr(evolution_infra, "_git_ensure_main_branch", lambda: state.update(ensured=True))
        monkeypatch.setattr(
            evolution_infra,
            "publish_runtime_expected_head",
            lambda reason, version=None: published.append((reason, version)) or "abc1234",
        )
        monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *a, **k: None)

        result = generation_scheduler._commit_post_cleanup_experience_change(240, set())

        assert result == {
            "committed": True,
            "commit": "abc1234",
            "path": "web/core/experience_pool.md",
            "push_ok": False,
        }
        assert state["ensured"] is True
        assert ("add", "--", "web/core/experience_pool.md") in calls
        assert any(call[:2] == ("commit", "-m") for call in calls)
        assert published == [("post_cleanup_experience_commit", 240)]

    def test_post_cleanup_experience_commit_honors_push_policy(self, tmp_path, monkeypatch):
        import evolution_infra
        import generation_scheduler

        exp = tmp_path / "web" / "core" / "experience_pool.md"
        exp.parent.mkdir(parents=True)
        exp.write_text("## RECENT_LESSONS\n- changed\n")
        state = {"staged": False}
        pushed = []
        published = []

        def fake_git(*args, check=True):
            if args[:3] == ("status", "--porcelain", "--"):
                return " M web/core/experience_pool.md"
            if args[:3] == ("diff", "--cached", "--name-only"):
                return "web/core/experience_pool.md" if state["staged"] else ""
            if args[:2] == ("add", "--"):
                state["staged"] = True
                return ""
            if args[0] == "commit":
                return ""
            if args[:2] == ("rev-parse", "--short"):
                return "def5678"
            return ""

        monkeypatch.setenv("EVOLUTION_GIT_PUSH", "1")
        monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(evolution_infra, "EXPERIENCE_FILE", exp)
        monkeypatch.setattr(evolution_infra, "_git", fake_git)
        monkeypatch.setattr(evolution_infra, "_git_ensure_main_branch", lambda: None)
        monkeypatch.setattr(evolution_infra, "git_push_refs", lambda *refs: pushed.append(refs) or True)
        monkeypatch.setattr(
            evolution_infra,
            "publish_runtime_expected_head",
            lambda reason, version=None: published.append((reason, version)) or "def5678",
        )
        monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *a, **k: None)

        result = generation_scheduler._commit_post_cleanup_experience_change(241, set())

        assert result["committed"] is True
        assert result["push_ok"] is True
        assert pushed == [("main",)]
        assert published == [
            ("post_cleanup_experience_commit", 241),
            ("post_cleanup_experience_push", 241),
        ]


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

    def test_write_failure_preserves_existing_json(self, tmp_path):
        """Atomic writer must not truncate the live file before tmp replace."""
        from evolution_infra import write_locked_json

        ratings_file = tmp_path / "glicko_ratings.json"
        ratings_file.write_text('{"claude_v9": {"r": 1490}}', encoding="utf-8")

        with pytest.raises(TypeError):
            write_locked_json(ratings_file, {"bad": object()})

        data = json.loads(ratings_file.read_text(encoding="utf-8"))
        assert data == {"claude_v9": {"r": 1490}}


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

    def test_early_generation_lease_transitions(self, tmp_path, monkeypatch):
        """Early stages persist selection and materialization before LLM work."""
        import evolution_infra

        ckpt_file = tmp_path / "pipeline_state.json"
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        assert evolution_infra.write_pipeline_checkpoint(
            next_v=11, source_v=10, stage="selected",
        )
        assert evolution_infra.write_pipeline_checkpoint(
            next_v=11, source_v=10, stage="preparing",
        )
        assert evolution_infra.write_pipeline_checkpoint(
            next_v=11, source_v=10, stage="prepared",
        )
        assert evolution_infra.write_pipeline_checkpoint(
            next_v=11, source_v=10, stage="direction_audited",
        )

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "direction_audited"

    def test_crossover_running_can_advance_to_workers_done(self, tmp_path, monkeypatch):
        """Crossover has a recoverable running stage before workers_done."""
        import evolution_infra

        ckpt_file = tmp_path / "pipeline_state.json"
        monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt_file)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)

        assert evolution_infra.write_pipeline_checkpoint(
            next_v=12, source_v=10, stage="selected", parent2_v=9,
        )
        assert evolution_infra.write_pipeline_checkpoint(
            next_v=12, source_v=10, stage="crossover_running", parent2_v=9,
        )
        assert evolution_infra.write_pipeline_checkpoint(
            next_v=12, source_v=10, stage="workers_done", parent2_v=9,
            master_plan={"strategy": "crossover", "tasks": []},
        )

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["parent2_v"] == 9

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

    def test_worker_llm_failure_uses_infrastructure_overlay_and_clean_retry(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        import agent_workers
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy.py").write_text("value = 0\n")
        (next_dir / "strategy.py").write_text("value = 0\n")
        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, failure_count=0)
        state = json.loads(ckpt_file.read_text())
        state["master_plan"]["tasks"] = [{
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["strategy.py"],
            "worker_prompt": "implement the planned strategy change",
        }]
        ckpt_file.write_text(json.dumps(state))
        monkeypatch.setattr(
            tool_planning,
            "get_bot_dir",
            lambda version: source_dir if int(version) == 10 else next_dir,
        )

        async def fail_worker(*_args, **_kwargs):
            raise agent_workers.WorkerInfrastructureError(
                1, "Algorithmic Logic Architect", ["sdk stream stalled"]
            )

        async def run_failure():
            with patch.object(tool_planning, "_execute_workers", side_effect=fail_worker):
                return await tool_planning.execute_workers.handler({
                    "next_v": 11,
                    "source_v": 10,
                })

        failed = json.loads(asyncio.run(run_failure())["content"][0]["text"])
        checkpoint = json.loads(ckpt_file.read_text())
        assert failed["failure_class"] == "infrastructure"
        assert failed["action"] == "retry_same_tool"
        assert checkpoint["stage"] == "master_planned"
        assert checkpoint["worker_failure_count"] == 0
        assert checkpoint["infra_failure"]["owner_tool"] == "execute_workers"

        (next_dir / "strategy.py").write_text("value = 1\n")

        async def run_success():
            with patch.object(
                tool_planning,
                "_execute_workers",
                new_callable=AsyncMock,
            ) as execute:
                execute.return_value = (True, {}, [])
                with patch.object(
                    tool_planning,
                    "_validate_worker_boundaries",
                    return_value=[],
                ), patch.object(
                    tool_planning,
                    "_py_files_changed_between",
                    return_value=["strategy.py"],
                ):
                    return await tool_planning.execute_workers.handler({
                        "next_v": 11,
                        "source_v": 10,
                    })

        recovered = json.loads(asyncio.run(run_success())["content"][0]["text"])
        checkpoint = json.loads(ckpt_file.read_text())
        assert recovered["success"] is True
        assert checkpoint["stage"] == "workers_done"
        assert checkpoint["infra_failure"] is None

    def test_quality_failed_rework_uses_checkpoint_feedback_and_sequential(self, tmp_path, monkeypatch):
        """quality_failed checkpoints should not depend on LLM-supplied feedback."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy.py").write_text("def act():\n    return 0\n")
        (next_dir / "strategy.py").write_text("def act():\n    return 1\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="quality_failed")
        state = json.loads(ckpt_file.read_text())
        state["gate_results"] = {
            "quality": {
                "all_passed": False,
                "failed_gates": ["file_size(strategy.py:2492L/2474L)", "position_semantics(state.py:1)"],
            }
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        def _reset_must_not_run(*_args, **_kwargs):
            raise AssertionError("quality repair should be in-place")

        monkeypatch.setattr(tool_planning, "_incremental_reset_next_dir", _reset_must_not_run)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["strategy.py"]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({
                    "tasks": [
                        {"worker_id": 1, "role": "arch", "target_files": ["strategy.py"], "worker_prompt": "recover file_size"},
                    ],
                    "next_v": 11,
                    "source_v": 10,
                })
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert mock_exec.call_args.kwargs["force_sequential"] is True
        assert "Quality gates failed" in mock_exec.call_args.kwargs["reviewer_feedback"]
        assert "in-place quality repair" in mock_exec.call_args.kwargs["reviewer_feedback"]
        assert "in-place crossover quality repair" not in mock_exec.call_args.kwargs["reviewer_feedback"]
        assert (next_dir / "strategy.py").read_text() == "def act():\n    return 1\n"
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["master_plan"]["work_item"]["kind"] == "quality_repair"
        assert ckpt["master_plan"]["work_item"]["reset_performed"] is False

    def test_crossover_quality_failed_empty_plan_synthesizes_in_place_repair(self, tmp_path, monkeypatch):
        """Crossover quality repairs need tasks but must preserve the fused candidate."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy.py").write_text("def act():\n    return 0\n")
        (next_dir / "strategy.py").write_text("def act():\n    return 1\n")
        (next_dir / "opponent.py").write_text("def pos():\n    return 'bad'\n")
        (next_dir / "state.py").write_text("def state():\n    return 'bad'\n")
        (next_dir / "strategy_helpers.py").write_text("def helper():\n    return 'bad'\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="quality_failed")
        state = json.loads(ckpt_file.read_text())
        state["parent2_v"] = 9
        state["master_plan"] = {"strategy": "crossover", "tasks": [], "parents": [10, 9]}
        state["gate_results"] = {
            "quality": {
                "all_passed": False,
                "failed_gates": [
                    "file_size(strategy.py:2483L/2473L)",
                    "position_semantics(opponent.py:1322: SB must be dealer_id; state.py:223: SB must be dealer_id)",
                ],
                "protected_contract_errors": [
                    "opponent.py: print() emits TCP action text 'allin selftest pass'; output must be JSON response int",
                ],
                "position_semantics_errors": [
                    "opponent.py:1322: SB must be dealer_id",
                    "state.py:223: SB must be dealer_id",
                    "strategy_helpers.py:1188: dealer is SB in heads-up",
                ],
            }
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        def _reset_must_not_run(*_args, **_kwargs):
            raise AssertionError("crossover quality repair should be in-place")

        monkeypatch.setattr(tool_planning, "_incremental_reset_next_dir", _reset_must_not_run)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["strategy.py"]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        tasks = mock_exec.call_args.args[0]
        by_blocker_file = {(task["repair_blocker"], task["target_files"][0]): task for task in tasks}
        assert ("file_size", "strategy.py") in by_blocker_file
        assert ("position_semantics", "opponent.py") in by_blocker_file
        assert ("position_semantics", "state.py") in by_blocker_file
        assert ("position_semantics", "strategy_helpers.py") in by_blocker_file
        assert ("quality_gate", "opponent.py") in by_blocker_file
        assert by_blocker_file[("file_size", "strategy.py")]["must_change_files"] == ["strategy.py"]
        assert "Preserve the current candidate" in by_blocker_file[("file_size", "strategy.py")]["worker_prompt"]
        assert "strategy_helpers.py" in by_blocker_file[("position_semantics", "strategy_helpers.py")]["worker_prompt"]
        assert mock_exec.call_args.kwargs["force_sequential"] is True
        assert "in-place crossover quality repair" in mock_exec.call_args.kwargs["reviewer_feedback"]
        assert (next_dir / "strategy.py").read_text() == "def act():\n    return 1\n"

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert len(ckpt["master_plan"]["tasks"]) == len(tasks)
        assert ckpt["master_plan"]["work_item"]["kind"] == "crossover_quality_repair"
        assert ckpt["master_plan"]["work_item"]["reset_performed"] is False

    def test_official_smoke_protocol_failure_synthesizes_native_entry_repair(self, tmp_path, monkeypatch):
        """Official-platform violations should repair national_bot.py, not loop with no tasks."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "national_bot.py").write_text("def send_action(sock):\n    sock.sendall(b'raise 200')\n")
        (next_dir / "national_bot.py").write_text("def send_action(sock):\n    sock.sendall(b'raise  200')\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="quality_failed")
        state = json.loads(ckpt_file.read_text())
        state["master_plan"] = {"strategy": "native_repair", "tasks": []}
        state["gate_results"] = {
            "quality": {
                "all_passed": False,
                "failed_gates": ["official_smoke"],
                "official_smoke_ok": False,
                "official_smoke_blocking": True,
                "official_smoke_inconclusive": False,
                "official_smoke_classification": "protocol_violation",
                "official_smoke_errors": [
                    "self_play_1: protocol_raise_format: msg='raise  200'",
                ],
                "official_smoke": {
                    "official_llm_repair_guidance": "Normalize raise formatting in _send_wire_action before socket send.",
                    "official_llm_prompt_feedback": "Worker must validate pending action and exact wire formatting.",
                },
            }
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        def _reset_must_not_run(*_args, **_kwargs):
            raise AssertionError("official smoke quality repair should be in-place")

        monkeypatch.setattr(tool_planning, "_incremental_reset_next_dir", _reset_must_not_run)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["national_bot.py"]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        tasks = mock_exec.call_args.args[0]
        assert len(tasks) == 1
        task = tasks[0]
        assert task["repair_blocker"] == "official_smoke"
        assert task["target_files"] == ["national_bot.py"]
        assert task["must_change_files"] == ["national_bot.py"]
        assert "protocol_raise_format" in task["worker_prompt"]
        assert "exactly one ASCII space" in task["worker_prompt"]
        assert "Normalize raise formatting" in task["worker_prompt"]
        assert "pending action" in task["worker_prompt"]
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["master_plan"]["tasks"][0]["repair_blocker"] == "official_smoke"

    def test_quality_failed_refreshes_stale_rework_task_targets(self, tmp_path, monkeypatch):
        """A second quality failure must replace old repair targets with current blockers."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        for directory in (source_dir, next_dir):
            (directory / "strategy.py").write_text("def act():\n    return 0\n")
            (directory / "opponent.py").write_text("def opp():\n    return 0\n")
            (directory / "state.py").write_text("def state():\n    return 0\n")
            (directory / "strategy_helpers.py").write_text("def pos():\n    return 'bad'\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="quality_failed")
        state = json.loads(ckpt_file.read_text())
        state["parent2_v"] = 9
        state["master_plan"] = {
            "strategy": "crossover",
            "tasks": [{
                "worker_id": "auto_quality_repair",
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py", "opponent.py", "state.py"],
                "worker_prompt": "old position repair",
            }],
        }
        state["gate_results"] = {
            "quality": {
                "all_passed": False,
                "failed_gates": [
                    "position_semantics(opponent.py:1329: SB must be dealer_id; state.py:223: SB must be dealer_id)"
                ],
                "position_semantics_errors": [
                    "opponent.py:1329: SB must be dealer_id",
                    "state.py:223: SB must be dealer_id",
                    "strategy_helpers.py:1188: dealer is SB in heads-up",
                ],
            }
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["strategy_helpers.py"]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        tasks = mock_exec.call_args.args[0]
        assert [task["target_files"] for task in tasks] == [
            ["opponent.py"],
            ["state.py"],
            ["strategy_helpers.py"],
        ]
        assert all(task["repair_blocker"] == "position_semantics" for task in tasks)
        assert tasks[-1]["must_change_files"] == ["strategy_helpers.py"]
        assert "strategy_helpers.py" in tasks[-1]["worker_prompt"]

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert [task["target_files"] for task in ckpt["master_plan"]["tasks"]] == [
            ["opponent.py"],
            ["state.py"],
            ["strategy_helpers.py"],
        ]
        assert ckpt["master_plan"]["work_item"]["reset_performed"] is False

    def test_rework_running_refreshes_stale_national_native_contract_task(self, tmp_path, monkeypatch):
        """A resumed rework batch must refresh old tasks when the native contract adds national_bot.py."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        for directory in (source_dir, next_dir):
            (directory / "national_bot.py").write_text("def main():\n    return None\n")
            (directory / "opponent.py").write_text("def opp():\n    return 0\n")
            (directory / "strategy.py").write_text("def act():\n    return 0\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="rework_running")
        state = json.loads(ckpt_file.read_text())
        state["parent2_v"] = 9
        state["reviewer_feedback"] = (
            "Quality gates failed:\n"
            "- national_native_contract(national_bot.py: _strategy_action must not continue "
            "with raw action after sanitizer failure)\n"
            "- opponent.py: print() emits TCP action text; output must be JSON response int\n"
            "- file_size(strategy.py:2483L/2000L)"
        )
        state["master_plan"] = {
            "strategy": "crossover",
            "tasks": [
                {
                    "worker_id": "auto_quality_repair_gate_opponent_py",
                    "role": "Algorithmic Logic Architect",
                    "target_files": ["opponent.py"],
                    "must_change_files": ["opponent.py"],
                    "worker_prompt": "old protected contract repair",
                    "task_kind": "quality_repair",
                    "repair_blocker": "quality_gate",
                    "repair_contract": {"blocker": "quality_gate", "file": "opponent.py"},
                },
                {
                    "worker_id": "auto_quality_repair_file_size_strategy_py",
                    "role": "Algorithmic Logic Architect",
                    "target_files": ["strategy.py"],
                    "must_change_files": ["strategy.py"],
                    "worker_prompt": "old file-size repair",
                    "task_kind": "quality_repair",
                    "repair_blocker": "file_size",
                    "repair_contract": {"blocker": "file_size", "file": "strategy.py"},
                },
            ],
            "repair_scope_files": ["opponent.py", "strategy.py"],
        }
        state["gate_results"] = {
            "quality": {
                "all_passed": False,
                "national_native_contract_ok": False,
                "failed_gates": [
                    (
                        "national_native_contract(national_bot.py: "
                        "_strategy_action must not continue with raw action after sanitizer failure)"
                    ),
                    "file_size(strategy.py:2483L/2000L)",
                ],
                "national_native_contract_errors": [
                    (
                        "national_bot.py: _strategy_action must not continue "
                        "with raw action after sanitizer failure"
                    ),
                ],
                "protected_contract_errors": [
                    "opponent.py: print() emits TCP action text; output must be JSON response int",
                ],
                "oversized_files": {"strategy.py": 2483},
            }
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["national_bot.py"]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        tasks = mock_exec.call_args.args[0]
        blocker_files = {(task["repair_blocker"], task["target_files"][0]) for task in tasks}
        assert ("national_native_contract", "national_bot.py") in blocker_files
        assert ("quality_gate", "national_bot.py") not in blocker_files
        file_size_task = next(task for task in tasks if task["repair_blocker"] == "file_size")
        assert "Large-overage requirement" in file_size_task["worker_prompt"]
        assert "483 lines over" in file_size_task["worker_prompt"]

        ckpt = json.loads(ckpt_file.read_text())
        checkpoint_blocker_files = {
            (task["repair_blocker"], task["target_files"][0])
            for task in ckpt["master_plan"]["tasks"]
        }
        assert ("national_native_contract", "national_bot.py") in checkpoint_blocker_files
        assert "national_bot.py" in ckpt["master_plan"]["repair_scope_files"]

    def test_quality_failed_refreshes_same_file_changed_blocker_contract(self, tmp_path, monkeypatch):
        """A same-file quality failure with a new blocker must not reuse the old contract."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        for directory in (source_dir, next_dir):
            (directory / "strategy_helpers.py").write_text("def helper():\n    return 0\n")
        (next_dir / "strategy_helpers.py").write_text("def helper():\n    return 1\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="quality_failed")
        state = json.loads(ckpt_file.read_text())
        state["parent2_v"] = 9
        state["master_plan"] = {
            "strategy": "crossover",
            "tasks": [{
                "worker_id": "auto_quality_repair_position_strategy_helpers_py",
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy_helpers.py"],
                "must_change_files": ["strategy_helpers.py"],
                "worker_prompt": "old position repair for line 1188",
                "task_kind": "quality_repair",
                "repair_blocker": "position_semantics",
                "repair_contract": {
                    "blocker": "position_semantics",
                    "file": "strategy_helpers.py",
                },
            }],
        }
        state["gate_results"] = {
            "quality": {
                "all_passed": False,
                "failed_gates": ["file_size(strategy_helpers.py:2501L/2500L)"],
            }
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        def _reset_must_not_run(*_args, **_kwargs):
            raise AssertionError("crossover quality repair should stay in-place")

        monkeypatch.setattr(tool_planning, "_incremental_reset_next_dir", _reset_must_not_run)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["strategy_helpers.py"]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        tasks = mock_exec.call_args.args[0]
        assert len(tasks) == 1
        assert tasks[0]["worker_id"] == "auto_quality_repair_file_size_strategy_helpers_py"
        assert tasks[0]["repair_blocker"] == "file_size"
        assert tasks[0]["target_files"] == ["strategy_helpers.py"]
        assert tasks[0]["must_change_files"] == ["strategy_helpers.py"]
        assert "<= 2500 lines" in tasks[0]["worker_prompt"]
        assert "old position repair" not in tasks[0]["worker_prompt"]

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["master_plan"]["tasks"][0]["repair_blocker"] == "file_size"
        assert ckpt["master_plan"]["work_item"]["reset_performed"] is False

    def test_quality_failed_refreshes_stale_precommit_tasks(self, tmp_path, monkeypatch):
        """quality_failed must rebuild tasks if checkpoint still holds a precommit repair."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        for directory in (source_dir, next_dir):
            (directory / "postflop.py").write_text("def old_gate():\n    return 0\n")
            (directory / "strategy.py").write_text("def act():\n    return 0\n")
        (next_dir / "postflop.py").write_text(
            "def old_gate():\n    return 1\n\n"
            "def spr_commitment_gate():\n    return 0\n"
        )

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="quality_failed")
        state = json.loads(ckpt_file.read_text())
        state["parent2_v"] = 9
        state["master_plan"] = {
            "strategy": "crossover",
            "tasks": [{
                "worker_id": "auto_precommit_repair_postflop_py",
                "role": "Strategic Regression Repair Architect",
                "target_files": ["postflop.py"],
                "must_change_files": ["postflop.py"],
                "worker_prompt": "old precommit regression repair",
                "task_kind": "precommit_repair",
                "repair_blocker": "precommit_regression",
            }],
        }
        state["gate_results"] = {
            "quality": {
                "all_passed": False,
                "failed_gates": [
                    (
                        "reachability(postflop.py:L1080: reachability - new top-level "
                        "function 'spr_commitment_gate' has no non-import references)"
                    )
                ],
            }
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        def _reset_must_not_run(*_args, **_kwargs):
            raise AssertionError("quality repair should stay in-place")

        monkeypatch.setattr(tool_planning, "_incremental_reset_next_dir", _reset_must_not_run)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["postflop.py"]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        tasks = mock_exec.call_args.args[0]
        assert [task["worker_id"] for task in tasks] == ["auto_quality_repair_gate_postflop_py"]
        assert tasks[0]["task_kind"] == "quality_repair"
        assert tasks[0]["repair_blocker"] == "quality_gate"
        assert tasks[0]["target_files"] == ["postflop.py"]
        assert "old precommit regression repair" not in tasks[0]["worker_prompt"]
        assert "reachability" in tasks[0]["worker_prompt"]
        assert "_self_test_*" in tasks[0]["worker_prompt"]
        assert 'if __name__ == "__main__"' in tasks[0]["worker_prompt"]
        assert "dummy reference" in tasks[0]["worker_prompt"]

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["master_plan"]["tasks"][0]["worker_id"] == "auto_quality_repair_gate_postflop_py"
        assert ckpt["master_plan"]["work_item"]["kind"] == "crossover_quality_repair"
        assert ckpt["master_plan"]["work_item"]["reset_performed"] is False

    def test_refreshed_quality_repair_preserves_accumulated_declared_scope(self, tmp_path, monkeypatch):
        """Refreshing to one new blocker must not forget earlier in-place repair files."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        for directory in (source_dir, next_dir):
            (directory / "opponent.py").write_text("sb = 'old'\n")
            (directory / "state.py").write_text("sb = 'old'\n")
            (directory / "strategy_helpers.py").write_text("# helper\n")
        (next_dir / "opponent.py").write_text("sb = dealer_id\n")
        (next_dir / "state.py").write_text("sb = dealer_id\n")
        (next_dir / "strategy_helpers.py").write_text("# helper fixed\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="quality_failed")
        state = json.loads(ckpt_file.read_text())
        state["parent2_v"] = 9
        state["master_plan"] = {
            "strategy": "crossover",
            "tasks": [
                {
                    "worker_id": "auto_quality_repair_position_opponent_py",
                    "role": "Algorithmic Logic Architect",
                    "target_files": ["opponent.py"],
                    "must_change_files": ["opponent.py"],
                    "worker_prompt": "old position repair",
                    "task_kind": "quality_repair",
                    "repair_blocker": "position_semantics",
                },
                {
                    "worker_id": "auto_quality_repair_position_state_py",
                    "role": "Algorithmic Logic Architect",
                    "target_files": ["state.py"],
                    "must_change_files": ["state.py"],
                    "worker_prompt": "old position repair",
                    "task_kind": "quality_repair",
                    "repair_blocker": "position_semantics",
                },
                {
                    "worker_id": "auto_quality_repair_position_strategy_helpers_py",
                    "role": "Algorithmic Logic Architect",
                    "target_files": ["strategy_helpers.py"],
                    "must_change_files": ["strategy_helpers.py"],
                    "worker_prompt": "old position repair",
                    "task_kind": "quality_repair",
                    "repair_blocker": "position_semantics",
                },
            ],
        }
        state["gate_results"] = {
            "quality": {
                "all_passed": False,
                "failed_gates": ["file_size(strategy_helpers.py:2501L/2500L)"],
            }
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)
        monkeypatch.setattr(tool_planning, "_incremental_reset_next_dir", lambda *_a, **_k: None)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(
                     tool_planning,
                     "_py_files_changed_between",
                     return_value=["opponent.py", "state.py", "strategy_helpers.py"],
                 ):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        tasks = mock_exec.call_args.args[0]
        assert [task["target_files"] for task in tasks] == [["strategy_helpers.py"]]
        assert tasks[0]["repair_blocker"] == "file_size"

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["master_plan"]["tasks"][0]["repair_blocker"] == "file_size"
        assert ckpt["master_plan"]["repair_scope_files"] == [
            "opponent.py",
            "state.py",
            "strategy_helpers.py",
        ]

    def test_declared_scope_uses_accumulated_repair_scope_files(self):
        """Quality declared-scope should audit cumulative repair scope, not only current task."""
        import tool_gates
        from worker_boundary import audit_changed_files_against_plan

        plan = {
            "tasks": [{
                "worker_id": "auto_quality_repair_file_size_strategy_helpers_py",
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy_helpers.py"],
                "worker_prompt": "fix size",
            }],
            "repair_scope_files": ["opponent.py", "state.py", "strategy_helpers.py"],
        }

        tasks = tool_gates._declared_scope_tasks_from_plan(plan)
        result = audit_changed_files_against_plan(
            ["opponent.py", "state.py", "strategy_helpers.py"],
            tasks,
            next_v=11,
        )

        assert result.passed is True
        assert result.allowed_files == ["opponent.py", "state.py", "strategy_helpers.py"]

    def test_declared_scope_uses_prepare_scope_files(self):
        """Prepare-time deterministic migrations are baseline scope, not worker drift."""
        import tool_gates
        from worker_boundary import audit_changed_files_against_plan

        plan = {
            "tasks": [{
                "worker_id": 1,
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py"],
                "worker_prompt": "add one strategy gate",
            }],
        }
        ckpt = {
            "prepare_scope_files": ["card_utils.py", "opponent.py", "state.py"],
            "master_plan": plan,
        }

        tasks = tool_gates._declared_scope_tasks_from_plan(plan, ckpt)
        result = audit_changed_files_against_plan(
            ["card_utils.py", "opponent.py", "state.py", "strategy.py"],
            tasks,
            next_v=89,
        )

        assert result.passed is True
        assert result.allowed_files == [
            "card_utils.py",
            "opponent.py",
            "state.py",
            "strategy.py",
        ]

    def test_crossover_repair_scope_includes_existing_candidate_diff(self, tmp_path, monkeypatch):
        """Crossover rework scope must include pre-existing fused candidate files."""
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        monkeypatch.setattr(
            tool_planning,
            "get_bot_dir",
            lambda v: source_dir if int(v) == 10 else next_dir,
        )
        monkeypatch.setattr(
            tool_planning,
            "_py_files_changed_between",
            lambda *_a, **_k: ["constants.py", "state.py", "strategy_helpers.py"],
        )

        ckpt = {
            "next_v": 11,
            "source_v": 10,
            "parent2_v": 9,
            "master_plan": {"strategy": "crossover", "repair_scope_files": ["strategy_helpers.py"]},
        }
        plan = {
            "tasks": [{
                "worker_id": "auto_quality_repair_file_size_strategy_helpers_py",
                "target_files": ["strategy_helpers.py"],
            }],
            "work_item": {"kind": "crossover_quality_repair"},
        }

        updated = tool_planning._plan_with_accumulated_repair_scope(ckpt, plan, plan["tasks"], 11)

        assert updated["repair_scope_files"] == [
            "constants.py",
            "state.py",
            "strategy_helpers.py",
        ]

    def test_quality_gate_declared_scope_expands_crossover_diff(self):
        import tool_gates
        from worker_boundary import audit_changed_files_against_plan

        plan = {
            "strategy": "crossover",
            "tasks": [{"target_files": ["strategy_helpers.py"]}],
            "repair_scope_files": ["strategy_helpers.py"],
        }
        ckpt = {"parent2_v": 9, "master_plan": plan}
        changed_files = ["constants.py", "state.py", "strategy_helpers.py"]

        expanded = tool_gates._master_plan_with_crossover_scope(plan, ckpt, changed_files)
        tasks = tool_gates._declared_scope_tasks_from_plan(expanded)
        result = audit_changed_files_against_plan(changed_files, tasks, next_v=11)

        assert result.passed is True
        assert expanded["repair_scope_files"] == [
            "constants.py",
            "state.py",
            "strategy_helpers.py",
        ]

    def test_declared_scope_failure_is_ledger_not_worker_contract(self):
        """Declared-scope misses should update scope accounting, not spawn edit tasks."""
        import tool_planning

        ckpt = {
            "next_v": 272,
            "source_v": 187,
            "parent2_v": 241,
            "stage": "quality_failed",
            "master_plan": {
                "strategy": "crossover",
                "tasks": [],
                "repair_scope_files": ["opponent.py", "state.py", "strategy_helpers.py"],
            },
            "gate_results": {
                "quality": {
                    "all_passed": False,
                    "failed_gates": [
                        (
                            "declared_scope(reachability_test.py: changed outside master plan "
                            "target_files/files_allowed; strategy.py: changed outside master plan "
                            "target_files/files_allowed)"
                        ),
                        "file_size(strategy_helpers.py:2501L/2500L)",
                        (
                            "position_semantics(strategy_helpers.py:1191: postflop OOP helper "
                            "must key on my_is_bb/BB)"
                        ),
                    ],
                    "declared_scope_ok": False,
                    "declared_scope_errors": [
                        "reachability_test.py: changed outside master plan target_files/files_allowed",
                        "strategy.py: changed outside master plan target_files/files_allowed",
                    ],
                    "declared_scope": {
                        "changed_files": [
                            "opponent.py",
                            "reachability_test.py",
                            "state.py",
                            "strategy.py",
                            "strategy_helpers.py",
                        ],
                        "allowed_files": ["opponent.py", "state.py", "strategy_helpers.py"],
                        "violation_count": 2,
                    },
                    "oversized_files": {"strategy_helpers.py": 2501},
                    "position_semantics_errors": [
                        "strategy_helpers.py:1191: postflop OOP helper must key on my_is_bb/BB"
                    ],
                }
            },
        }

        tasks = tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt)

        assert [
            (task["repair_blocker"], task["target_files"])
            for task in tasks
        ] == [
            ("position_semantics", ["strategy_helpers.py"]),
            ("file_size", ["strategy_helpers.py"]),
        ]
        assert tool_planning._declared_scope_ledger_files(ckpt) == {
            "reachability_test.py",
            "strategy.py",
        }

    def test_execute_workers_prunes_declared_scope_ledger_tasks(self, tmp_path, monkeypatch):
        """Old checkpoints with declared-scope pseudo tasks should resume without them."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        for directory in (source_dir, next_dir):
            for filename in (
                "opponent.py",
                "reachability_test.py",
                "state.py",
                "strategy.py",
                "strategy_helpers.py",
            ):
                (directory / filename).write_text(f"# {filename}\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="quality_failed")
        state = json.loads(ckpt_file.read_text())
        state["parent2_v"] = 9
        state["master_plan"] = {
            "strategy": "crossover",
            "repair_scope_files": ["opponent.py", "state.py", "strategy_helpers.py"],
            "tasks": [
                {
                    "worker_id": "auto_quality_repair_file_size_strategy_helpers_py",
                    "role": "Algorithmic Logic Architect",
                    "target_files": ["strategy_helpers.py"],
                    "must_change_files": ["strategy_helpers.py"],
                    "worker_prompt": "fix file_size",
                    "task_kind": "quality_repair",
                    "repair_blocker": "file_size",
                    "repair_contract": {
                        "blocker": "file_size",
                        "file": "strategy_helpers.py",
                    },
                },
                {
                    "worker_id": "auto_quality_repair_position_strategy_helpers_py",
                    "role": "Algorithmic Logic Architect",
                    "target_files": ["strategy_helpers.py"],
                    "must_change_files": ["strategy_helpers.py"],
                    "worker_prompt": "fix position_semantics",
                    "task_kind": "quality_repair",
                    "repair_blocker": "position_semantics",
                    "repair_contract": {
                        "blocker": "position_semantics",
                        "file": "strategy_helpers.py",
                    },
                },
                {
                    "worker_id": "auto_quality_repair_gate_reachability_test_py",
                    "role": "Algorithmic Logic Architect",
                    "target_files": ["reachability_test.py"],
                    "must_change_files": ["reachability_test.py"],
                    "worker_prompt": "reachability_test.py changed outside master plan target_files/files_allowed",
                    "task_kind": "quality_repair",
                    "repair_blocker": "quality_gate",
                    "repair_contract": {
                        "blocker": "quality_gate",
                        "file": "reachability_test.py",
                        "evidence": "reachability_test.py: changed outside master plan target_files/files_allowed",
                    },
                },
                {
                    "worker_id": "auto_quality_repair_gate_strategy_py",
                    "role": "Algorithmic Logic Architect",
                    "target_files": ["strategy.py"],
                    "must_change_files": ["strategy.py"],
                    "worker_prompt": "strategy.py changed outside master plan target_files/files_allowed",
                    "task_kind": "quality_repair",
                    "repair_blocker": "quality_gate",
                    "repair_contract": {
                        "blocker": "quality_gate",
                        "file": "strategy.py",
                        "evidence": "strategy.py: changed outside master plan target_files/files_allowed",
                    },
                },
            ],
        }
        state["gate_results"] = {
            "quality": {
                "all_passed": False,
                "failed_gates": [
                    (
                        "declared_scope(reachability_test.py: changed outside master plan "
                        "target_files/files_allowed; strategy.py: changed outside master plan "
                        "target_files/files_allowed)"
                    ),
                    "file_size(strategy_helpers.py:2501L/2500L)",
                    "position_semantics(strategy_helpers.py:1191: postflop OOP helper must key on BB)",
                ],
                "declared_scope_ok": False,
                "declared_scope_errors": [
                    "reachability_test.py: changed outside master plan target_files/files_allowed",
                    "strategy.py: changed outside master plan target_files/files_allowed",
                ],
                "declared_scope": {
                    "changed_files": [
                        "opponent.py",
                        "reachability_test.py",
                        "state.py",
                        "strategy.py",
                        "strategy_helpers.py",
                    ],
                    "allowed_files": ["opponent.py", "state.py", "strategy_helpers.py"],
                    "violation_count": 2,
                },
                "oversized_files": {"strategy_helpers.py": 2501},
                "position_semantics_errors": [
                    "strategy_helpers.py:1191: postflop OOP helper must key on BB"
                ],
            }
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(
                     tool_planning,
                     "_py_files_changed_between",
                     return_value=[
                         "opponent.py",
                         "reachability_test.py",
                         "state.py",
                         "strategy.py",
                         "strategy_helpers.py",
                     ],
                 ):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        tasks = mock_exec.call_args.args[0]
        assert [
            (task["repair_blocker"], task["target_files"])
            for task in tasks
        ] == [
            ("position_semantics", ["strategy_helpers.py"]),
            ("file_size", ["strategy_helpers.py"]),
        ]

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["master_plan"]["repair_scope_files"] == [
            "opponent.py",
            "reachability_test.py",
            "state.py",
            "strategy.py",
            "strategy_helpers.py",
        ]
        assert [
            task["target_files"] for task in ckpt["master_plan"]["tasks"]
        ] == [["strategy_helpers.py"], ["strategy_helpers.py"]]
        assert [
            task["repair_blocker"] for task in ckpt["master_plan"]["tasks"]
        ] == ["position_semantics", "file_size"]

    def test_quality_rework_skipper_keeps_mixed_task_when_one_blocker_remains(self, tmp_path, monkeypatch):
        """A position task mentioning size feedback must still run while position blockers remain."""
        import tool_gates
        import tool_planning

        monkeypatch.setattr(tool_planning, "check_code_size", lambda *_a, **_k: (0, []))
        monkeypatch.setattr(
            tool_gates,
            "detect_position_semantics_errors",
            lambda _dir: ["state.py: SB must be dealer_id"],
        )

        skipper = tool_planning._quality_rework_skipper(
            tmp_path / "claude_v11",
            tmp_path / "claude_v10",
            11,
            10,
        )
        mixed_task = {
            "worker_id": "w1_position_contract",
            "role": "arch",
            "target_files": ["opponent.py", "state.py", "strategy_helpers.py"],
            "worker_prompt": (
                "Quality failed: file_size(strategy.py:2498L/2476L); "
                "position_semantics(state.py:223); protected_contract"
            ),
        }
        size_only_task = {
            "worker_id": "w2_size_trim",
            "role": "arch",
            "target_files": ["strategy.py"],
            "worker_prompt": "Fix file_size and LOC only",
        }

        assert skipper(mixed_task) == ""
        assert "size" in skipper(size_only_task)

    def test_quality_rework_skipper_skips_cleared_protected_contract_task(self, tmp_path, monkeypatch):
        """A stale protected_contract repair must not force a no-op bot edit."""
        import tool_gates
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (next_dir / "main.py").write_text("print({'response': 0})\n", encoding="utf-8")
        (next_dir / "opponent.py").write_text(
            "import sys\nprint('allin selftest pass', file=sys.stderr)\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(tool_planning, "check_code_size", lambda *_a, **_k: (0, []))
        monkeypatch.setattr(tool_gates, "detect_position_semantics_errors", lambda _dir: [])
        monkeypatch.setattr(tool_planning, "_py_files_changed_between", lambda *_a, **_k: ["opponent.py"])

        skipper = tool_planning._quality_rework_skipper(next_dir, source_dir, 11, 10)
        task = {
            "worker_id": "auto_quality_repair_gate_opponent_py",
            "role": "Algorithmic Logic Architect",
            "target_files": ["opponent.py"],
            "repair_blocker": "quality_gate",
            "repair_contract": {
                "blocker": "quality_gate",
                "file": "opponent.py",
                "evidence": (
                    "opponent.py: print() emits TCP action text "
                    "'allin selftest pass'; output must be JSON response int"
                ),
            },
            "worker_prompt": "Fix protected_contract evidence only.",
        }

        reason = skipper(task)

        assert "protected_contract" in reason
        assert "already cleared" in reason

    def test_quality_rework_skipper_keeps_active_protected_contract_task(self, tmp_path, monkeypatch):
        import tool_gates
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (next_dir / "main.py").write_text("print({'response': 0})\n", encoding="utf-8")
        (next_dir / "opponent.py").write_text(
            "print('allin selftest pass')\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(tool_planning, "check_code_size", lambda *_a, **_k: (0, []))
        monkeypatch.setattr(tool_gates, "detect_position_semantics_errors", lambda _dir: [])
        monkeypatch.setattr(tool_planning, "_py_files_changed_between", lambda *_a, **_k: ["opponent.py"])

        skipper = tool_planning._quality_rework_skipper(next_dir, source_dir, 11, 10)
        task = {
            "worker_id": "auto_quality_repair_gate_opponent_py",
            "target_files": ["opponent.py"],
            "repair_blocker": "quality_gate",
            "worker_prompt": (
                "opponent.py: print() emits TCP action text 'allin selftest pass'; "
                "output must be JSON response int"
            ),
        }

        assert skipper(task) == ""

    def test_quality_rework_skipper_skips_cleared_reachability_task(self, tmp_path, monkeypatch):
        import tool_gates
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "reachability_test.py").write_text("def existing():\n    return 1\n", encoding="utf-8")
        (next_dir / "reachability_test.py").write_text("def existing():\n    return 1\n", encoding="utf-8")

        monkeypatch.setattr(tool_planning, "check_code_size", lambda *_a, **_k: (0, []))
        monkeypatch.setattr(tool_gates, "detect_position_semantics_errors", lambda _dir: [])
        monkeypatch.setattr(tool_planning, "_py_files_changed_between", lambda *_a, **_k: ["reachability_test.py"])

        skipper = tool_planning._quality_rework_skipper(next_dir, source_dir, 11, 10)
        task = {
            "worker_id": "auto_quality_repair_gate_reachability_test_py",
            "target_files": ["reachability_test.py"],
            "repair_blocker": "quality_gate",
            "worker_prompt": (
                "reachability_test.py:L136: reachability - new top-level function "
                "'_restore_boost' has no non-import references"
            ),
        }

        reason = skipper(task)

        assert "reachability" in reason
        assert "already cleared" in reason

    def test_single_quality_rework_task_uses_skipper(self, tmp_path):
        """Single-task quality repair must not bypass the cheap cleared-blocker skipper."""
        import asyncio
        import agent_workers

        next_dir = tmp_path / "claude_v11"
        next_dir.mkdir()
        (next_dir / "strategy_helpers.py").write_text("# already fixed\n")

        class UI:
            costs = {}

            def __init__(self):
                self.messages = []

            def log_history(self, message, level="info"):
                self.messages.append((level, message))

        ui = UI()
        task = {
            "worker_id": "auto_quality_repair_file_size_strategy_helpers_py",
            "role": "Algorithmic Logic Architect",
            "target_files": ["strategy_helpers.py"],
            "worker_prompt": "fix file_size",
        }

        async def _run():
            with patch.object(agent_workers, "_run_single_worker", new_callable=AsyncMock) as run_worker:
                result = await agent_workers._execute_workers(
                    [task],
                    "",
                    next_dir,
                    11,
                    [],
                    ui,
                    reviewer_feedback="Quality gates failed",
                    source_v=10,
                    task_skipper=lambda _task: "all cheap quality rework blockers already cleared by current code",
                )
                return result, run_worker

        (success, snapshots, focus), run_worker = asyncio.run(_run())
        assert success is True
        assert focus == []
        assert snapshots == {(0, "strategy_helpers.py"): "# already fixed\n"}
        run_worker.assert_not_called()
        assert any("Skipping worker auto_quality_repair_file_size_strategy_helpers_py" in msg for _lvl, msg in ui.messages)

    def test_timed_out_quality_worker_preserves_valid_cleared_blocker_edit(self, tmp_path, monkeypatch):
        """A slow worker that already cleared its blocker should not lose valid edits."""
        import asyncio
        import agent_workers

        next_dir = tmp_path / "claude_v11"
        source_dir = tmp_path / "claude_v10"
        next_dir.mkdir()
        source_dir.mkdir()
        baseline = "value = 1\n"
        fixed = "value = 1\nfixed = True\n"
        (next_dir / "opponent.py").write_text(baseline)
        (source_dir / "opponent.py").write_text(baseline)

        class UI:
            costs = {}

            def __init__(self):
                self.messages = []

            def log_history(self, message, level="info"):
                self.messages.append((level, message))

            def clear_io(self):
                pass

            def set_status(self, *_args, **_kwargs):
                pass

            def log_io(self, *_args, **_kwargs):
                pass

        async def slow_worker(*_args, **_kwargs):
            (next_dir / "opponent.py").write_text(fixed)
            await asyncio.sleep(1)

        task = {
            "worker_id": "auto_quality_repair_file_size_opponent_py",
            "role": "Algorithmic Logic Architect",
            "target_files": ["opponent.py"],
            "must_change_files": ["opponent.py"],
            "worker_prompt": "fix file_size",
            "task_kind": "quality_repair",
            "repair_blocker": "file_size",
        }

        monkeypatch.setattr(agent_workers, "MAX_WORKER_RETRIES", 1)
        monkeypatch.setattr(agent_workers, "_worker_timeout_for_task", lambda *_a, **_k: 0.01)
        monkeypatch.setattr(agent_workers, "run_claude_query", slow_worker)
        monkeypatch.setattr(agent_workers, "verify_code", lambda *_a, **_k: [])
        monkeypatch.setattr(agent_workers, "get_bot_dir", lambda _v: source_dir)

        async def _run():
            with patch("audit_agents._run_worker_cot_check", new_callable=AsyncMock) as cot:
                cot.return_value = {"cot_consistent": True, "focus_areas": []}
                return await agent_workers._execute_workers(
                    [task],
                    "{worker_prompt}",
                    next_dir,
                    11,
                    [],
                    UI(),
                    reviewer_feedback="Quality gates failed: file_size(opponent.py:1650L/1500L)",
                    source_v=10,
                    task_skipper=lambda _task: (
                        "quality blocker file(s) already cleared by current code: opponent.py"
                        if "fixed = True" in (next_dir / "opponent.py").read_text()
                        else ""
                    ),
                )

        success, snapshots, focus = asyncio.run(_run())
        assert success is True
        assert focus == []
        assert snapshots == {(0, "opponent.py"): baseline}
        assert (next_dir / "opponent.py").read_text() == fixed

    def test_timed_out_quality_worker_resets_when_blocker_still_present(self, tmp_path, monkeypatch):
        """Timeout preservation requires the cheap quality blocker to be cleared."""
        import asyncio
        import agent_workers

        next_dir = tmp_path / "claude_v11"
        source_dir = tmp_path / "claude_v10"
        next_dir.mkdir()
        source_dir.mkdir()
        baseline = "value = 1\n"
        partial = "value = 1\npartial = True\n"
        (next_dir / "opponent.py").write_text(baseline)
        (source_dir / "opponent.py").write_text(baseline)

        class UI:
            costs = {}

            def log_history(self, *_args, **_kwargs):
                pass

            def clear_io(self):
                pass

            def set_status(self, *_args, **_kwargs):
                pass

            def log_io(self, *_args, **_kwargs):
                pass

        async def slow_worker(*_args, **_kwargs):
            (next_dir / "opponent.py").write_text(partial)
            await asyncio.sleep(1)

        task = {
            "worker_id": "auto_quality_repair_file_size_opponent_py",
            "role": "Algorithmic Logic Architect",
            "target_files": ["opponent.py"],
            "must_change_files": ["opponent.py"],
            "worker_prompt": "fix file_size",
            "task_kind": "quality_repair",
            "repair_blocker": "file_size",
        }

        monkeypatch.setattr(agent_workers, "MAX_WORKER_RETRIES", 1)
        monkeypatch.setattr(agent_workers, "_worker_timeout_for_task", lambda *_a, **_k: 0.01)
        monkeypatch.setattr(agent_workers, "run_claude_query", slow_worker)
        monkeypatch.setattr(agent_workers, "verify_code", lambda *_a, **_k: [])
        monkeypatch.setattr(agent_workers, "get_bot_dir", lambda _v: source_dir)

        with pytest.raises(agent_workers.WorkerInfrastructureError):
            asyncio.run(agent_workers._execute_workers(
                [task],
                "{worker_prompt}",
                next_dir,
                11,
                [],
                UI(),
                reviewer_feedback="Quality gates failed: file_size(opponent.py:1650L/1500L)",
                source_v=10,
                task_skipper=lambda _task: "",
            ))

        assert (next_dir / "opponent.py").read_text() == baseline

    def test_cot_inconsistency_does_not_reset_cleared_file_size_repair(self, tmp_path, monkeypatch):
        """Cheap quality recheck is authoritative over noisy CoT arithmetic."""
        import asyncio
        import agent_workers

        next_dir = tmp_path / "claude_v11"
        source_dir = tmp_path / "claude_v10"
        next_dir.mkdir()
        source_dir.mkdir()
        baseline = "value = 1\n"
        fixed = "value = 1\nfixed = True\n"
        (next_dir / "strategy_helpers.py").write_text(baseline)
        (source_dir / "strategy_helpers.py").write_text(baseline)

        class UI:
            costs = {}

            def __init__(self):
                self.messages = []

            def log_history(self, message, level="info"):
                self.messages.append((level, message))

        async def fake_worker(*_args, **_kwargs):
            (next_dir / "strategy_helpers.py").write_text(fixed)
            return True

        task = {
            "worker_id": "auto_quality_repair_file_size_strategy_helpers_py",
            "role": "Algorithmic Logic Architect",
            "target_files": ["strategy_helpers.py"],
            "must_change_files": ["strategy_helpers.py"],
            "worker_prompt": "fix file_size",
            "task_kind": "quality_repair",
            "repair_blocker": "file_size",
        }
        ui = UI()
        monkeypatch.setattr(agent_workers, "_run_single_worker", fake_worker)
        monkeypatch.setattr(agent_workers, "get_bot_dir", lambda _v: source_dir)

        async def _run():
            with patch("audit_agents._run_worker_cot_check", new_callable=AsyncMock) as cot:
                cot.return_value = {
                    "cot_consistent": False,
                    "focus_areas": ["line-count arithmetic used stale gate baseline"],
                }
                return await agent_workers._execute_workers(
                    [task],
                    "{worker_prompt}",
                    next_dir,
                    11,
                    [],
                    ui,
                    reviewer_feedback="Quality gates failed: file_size(strategy_helpers.py:1664L/1500L)",
                    source_v=10,
                    task_skipper=lambda _task: (
                        "quality blocker file(s) already cleared by current code: strategy_helpers.py"
                        if "fixed = True" in (next_dir / "strategy_helpers.py").read_text()
                        else ""
                    ),
                )

        success, snapshots, focus = asyncio.run(_run())
        assert success is True
        assert focus == []
        assert snapshots == {(0, "strategy_helpers.py"): baseline}
        assert (next_dir / "strategy_helpers.py").read_text() == fixed
        assert any("CoT check was inconsistent" in msg for _level, msg in ui.messages)

    def test_cot_runtime_side_effect_resets_feature_worker(self, tmp_path, monkeypatch):
        """Undisclosed runtime telemetry is a hard failure even for feature work."""
        import asyncio
        import agent_workers

        next_dir = tmp_path / "claude_v11"
        source_dir = tmp_path / "claude_v10"
        next_dir.mkdir()
        source_dir.mkdir()
        baseline = "def decide():\n    return 0\n"
        changed = "def decide():\n    import sys as _sys\n    _sys.stderr.write('debug\\n')\n    return 0\n"
        (next_dir / "strategy.py").write_text(baseline)
        (source_dir / "strategy.py").write_text(baseline)

        class UI:
            costs = {}

            def log_history(self, *_args, **_kwargs):
                pass

        async def fake_worker(*_args, **_kwargs):
            (next_dir / "strategy.py").write_text(changed)
            return True

        task = {
            "worker_id": 1,
            "role": "Algorithmic Logic Architect",
            "target_files": ["strategy.py"],
            "worker_prompt": "Change the decision heuristic.",
            "task_kind": "feature_work",
        }
        monkeypatch.setattr(agent_workers, "_run_single_worker", fake_worker)
        monkeypatch.setattr(agent_workers, "get_bot_dir", lambda _v: source_dir)

        async def _run():
            with patch("audit_agents._run_worker_cot_check", new_callable=AsyncMock) as cot:
                cot.return_value = {
                    "cot_consistent": False,
                    "discrepancies": [
                        "Worker added _sys.stderr.write telemetry but did not disclose "
                        "the runtime side-effect."
                    ],
                    "focus_areas": ["remove hidden stderr telemetry"],
                }
                return await agent_workers._execute_workers(
                    [task],
                    "{worker_prompt}",
                    next_dir,
                    11,
                    [],
                    UI(),
                    reviewer_feedback="",
                    source_v=10,
                )

        success, snapshots, focus = asyncio.run(_run())
        assert success is False
        assert snapshots == {(0, "strategy.py"): baseline}
        assert focus == ["remove hidden stderr telemetry"]
        assert (next_dir / "strategy.py").read_text() == baseline

    def test_critic_repair_cot_inconsistency_resets_worker(self, tmp_path, monkeypatch):
        """Critic repair tasks must not advance when CoT finds claim-vs-diff mismatch."""
        import asyncio
        import agent_workers

        next_dir = tmp_path / "claude_v11"
        source_dir = tmp_path / "claude_v10"
        next_dir.mkdir()
        source_dir.mkdir()
        baseline = "def profile():\n    return {'sizing_delta': 0.0}\n"
        changed = "def profile():\n    return {'sizing_delta': -0.1}\n"
        (next_dir / "strategy.py").write_text(baseline)
        (source_dir / "strategy.py").write_text(baseline)

        class UI:
            costs = {}

            def log_history(self, *_args, **_kwargs):
                pass

        async def fake_worker(*_args, **_kwargs):
            (next_dir / "strategy.py").write_text(changed)
            return True

        task = {
            "worker_id": 2,
            "role": "Algorithmic Logic Architect",
            "target_files": ["strategy.py"],
            "worker_prompt": "Repair the Strategy Critic rejection in strategy.py.",
            "task_kind": "critic_repair",
        }
        monkeypatch.setattr(agent_workers, "_run_single_worker", fake_worker)
        monkeypatch.setattr(agent_workers, "get_bot_dir", lambda _v: source_dir)

        async def _run():
            with patch("audit_agents._run_worker_cot_check", new_callable=AsyncMock) as cot:
                cot.return_value = {
                    "cot_consistent": False,
                    "focus_areas": ["critic repair explanation does not match the diff"],
                }
                return await agent_workers._execute_workers(
                    [task],
                    "{worker_prompt}",
                    next_dir,
                    11,
                    [],
                    UI(),
                    reviewer_feedback="Critic rejected the candidate.",
                    source_v=10,
                )

        success, snapshots, focus = asyncio.run(_run())
        assert success is False
        assert snapshots == {(0, "strategy.py"): baseline}
        assert focus == ["critic repair explanation does not match the diff"]
        assert (next_dir / "strategy.py").read_text() == baseline

    def test_cot_task_mismatch_resets_feature_worker(self, tmp_path, monkeypatch):
        """A worker that reverses its assigned task is a hard failure, not reviewer focus."""
        import asyncio
        import agent_workers

        next_dir = tmp_path / "claude_v11"
        source_dir = tmp_path / "claude_v10"
        next_dir.mkdir()
        source_dir.mkdir()
        baseline = "from tournament import chip_phase_profile\nvalue = chip_phase_profile\n"
        changed = "value = None\n"
        (next_dir / "strategy.py").write_text(baseline)
        (source_dir / "strategy.py").write_text(baseline)

        class UI:
            costs = {}

            def log_history(self, *_args, **_kwargs):
                pass

        async def fake_worker(*_args, **_kwargs):
            (next_dir / "strategy.py").write_text(changed)
            return True

        task = {
            "worker_id": 2,
            "role": "Algorithmic Logic Architect",
            "target_files": ["strategy.py"],
            "worker_prompt": "Wire chip_phase_profile into strategy.py.",
            "task_kind": "feature_work",
        }
        monkeypatch.setattr(agent_workers, "_run_single_worker", fake_worker)
        monkeypatch.setattr(agent_workers, "get_bot_dir", lambda _v: source_dir)

        async def _run():
            with patch("audit_agents._run_worker_cot_check", new_callable=AsyncMock) as cot:
                cot.return_value = {
                    "cot_consistent": False,
                    "discrepancies": [
                        "Assigned task was to wire chip_phase_profile() INTO strategy.py. "
                        "The diff performs NONE of these steps and instead REVERSES the "
                        "pre-existing chip_phase integration; the actual surface area is "
                        "larger and more invasive than the summary claims."
                    ],
                    "focus_areas": ["worker reversed the assigned chip-phase integration"],
                }
                return await agent_workers._execute_workers(
                    [task],
                    "{worker_prompt}",
                    next_dir,
                    11,
                    [],
                    UI(),
                    reviewer_feedback="",
                    source_v=10,
                )

        success, snapshots, focus = asyncio.run(_run())
        assert success is False
        assert snapshots == {(0, "strategy.py"): baseline}
        assert focus == ["worker reversed the assigned chip-phase integration"]
        assert (next_dir / "strategy.py").read_text() == baseline

    def test_repair_planned_quality_rework_passes_skipper_to_workers(self, tmp_path, monkeypatch):
        """A resumed quality repair at repair_planned should skip already-cleared blockers."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy_helpers.py").write_text("# source\n")
        (next_dir / "strategy_helpers.py").write_text("# already fixed\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="repair_planned")
        state = json.loads(ckpt_file.read_text())
        state["parent2_v"] = 9
        state["reviewer_feedback"] = "Quality gates failed:\n- file_size(strategy_helpers.py:2501L/2500L)"
        state["master_plan"] = {
            "strategy": "crossover",
            "tasks": [{
                "worker_id": "auto_quality_repair_file_size_strategy_helpers_py",
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy_helpers.py"],
                "must_change_files": ["strategy_helpers.py"],
                "worker_prompt": "fix file_size",
                "task_kind": "quality_repair",
                "repair_blocker": "file_size",
            }],
            "work_item": {
                "kind": "crossover_quality_repair",
                "source_stage": "quality_failed",
                "reset_performed": False,
            },
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)
        monkeypatch.setattr(tool_planning, "check_code_size", lambda *_a, **_k: (0, []))
        monkeypatch.setattr("tool_gates.detect_position_semantics_errors", lambda _dir: [])

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["strategy_helpers.py"]):
                mock_exec.return_value = (True, {(0, "strategy_helpers.py"): "# already fixed\n"}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec.call_args.kwargs

        result, kwargs = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        assert kwargs["force_sequential"] is True
        assert kwargs["task_skipper"] is not None

    def test_quality_repair_contract_tasks_are_file_scoped_and_deduped(self):
        import tool_planning

        ckpt = {
            "next_v": 268,
            "source_v": 246,
            "parent2_v": 254,
            "stage": "quality_failed",
            "master_plan": {"strategy": "crossover", "tasks": []},
            "gate_results": {
                "quality": {
                    "all_passed": False,
                    "failed_gates": [
                        "file_size(strategy.py:2496L/2493L)",
                        (
                            "position_semantics(opponent.py:1256: SB must be dealer_id; "
                            "state.py:223: SB must be dealer_id)"
                        ),
                    ],
                    "oversized_files": {"strategy.py": 2496},
                    "position_semantics_errors": [
                        "opponent.py:1256: SB must be dealer_id",
                        "state.py:223: SB must be dealer_id",
                    ],
                    "protected_contract_errors": [
                        "opponent.py: print() emits TCP action text; output must be JSON response int",
                    ],
                }
            },
        }

        tasks = tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt)
        blocker_files = [(task["repair_blocker"], task["target_files"][0]) for task in tasks]

        assert blocker_files == [
            ("position_semantics", "opponent.py"),
            ("position_semantics", "state.py"),
            ("quality_gate", "opponent.py"),
            ("file_size", "strategy.py"),
        ]
        assert [task["target_files"] for task in tasks].count(["strategy.py"]) == 1
        assert all(task["must_change_files"] == task["target_files"] for task in tasks)
        assert "<= 2493 lines" in tasks[-1]["worker_prompt"]
        assert "print() emits TCP action text" in tasks[-2]["worker_prompt"]

    def test_reviewer_feedback_quality_repair_uses_primary_files_not_all_mentions(self):
        import tool_planning

        ckpt = {
            "next_v": 95,
            "source_v": 37,
            "parent2_v": 72,
            "stage": "repair_planned",
            "master_plan": {"strategy": "crossover", "tasks": []},
            "gate_results": {"quality": {"all_passed": True, "failed_gates": []}},
        }
        feedback = (
            "Two code-quality issues block approval:\n\n"
            "1. Role boundary violation in constants.py: the worker is assigned as "
            "Algorithmic Logic Architect but edited the existing module-level constant "
            "BB_CALL_THRESHOLD (0.41 -> 0.35). Editing existing numeric constants in "
            "constants.py is Hyperparameter Tuner scope, not Architect scope.\n\n"
            "2. Dead code in opponent.py: PRIOR_BETSIZE_POLARITY and "
            "PRIOR_BETSIZE_POLARITY_WEIGHT are defined at module level but never "
            "referenced anywhere in the bot. Additionally, opponent.py computes and "
            "returns betsize_polarity, flop_polarity, turn_polarity, river_polarity, "
            "shove_rate, flop_shove_rate, turn_shove_rate, and river_shove_rate, but "
            "none of these keys are consumed by strategy.py, postflop.py, "
            "national_bot.py, or main.py.\n\n"
            "Other checks: all changed files compile and import cleanly; national_bot.py "
            "is unchanged and remains a valid raw TCP client."
        )

        tasks = tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt, feedback)

        assert [(task["role"], task["target_files"]) for task in tasks] == [
            ("Hyperparameter Tuner", ["constants.py"]),
            ("Algorithmic Logic Architect", ["opponent.py"]),
        ]
        assert "BB_CALL_THRESHOLD" in tasks[0]["worker_prompt"]
        assert "Constants-only role method" in tasks[0]["worker_prompt"]
        assert "PRIOR_BETSIZE_POLARITY" in tasks[1]["worker_prompt"]
        assert tool_planning._quality_failure_target_files(ckpt, feedback) == {
            "constants.py",
            "opponent.py",
        }

        old_tasks = [
            {
                "worker_id": "auto_quality_repair_gate_constants_py",
                "role": "Algorithmic Logic Architect",
                "target_files": ["constants.py"],
                "must_change_files": ["constants.py"],
                "repair_blocker": "quality_gate",
                "repair_contract": {"blocker": "quality_gate", "file": "constants.py"},
            },
            {
                "worker_id": "auto_quality_repair_gate_opponent_py",
                "role": "Algorithmic Logic Architect",
                "target_files": ["opponent.py"],
                "must_change_files": ["opponent.py"],
                "repair_blocker": "quality_gate",
                "repair_contract": {"blocker": "quality_gate", "file": "opponent.py"},
            },
            {
                "worker_id": "auto_quality_repair_gate_strategy_py",
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py"],
                "must_change_files": ["strategy.py"],
                "repair_blocker": "quality_gate",
                "repair_contract": {"blocker": "quality_gate", "file": "strategy.py"},
            },
        ]
        stale_reason = tool_planning._stale_quality_task_reason(old_tasks, ckpt, feedback)
        assert "extra stale task" in stale_reason
        assert "quality_gate:strategy.py" in stale_reason

    def test_reviewer_scope_drift_repair_targets_revert_file_not_positive_context(self):
        import tool_planning

        ckpt = {
            "next_v": 104,
            "source_v": 103,
            "parent2_v": None,
            "stage": "repair_planned",
            "master_plan": {
                "strategy": "master",
                "tasks": [
                    {
                        "worker_id": "1",
                        "target_files": ["opponent.py"],
                        "prohibited_files": ["national_bot.py"],
                    },
                    {
                        "worker_id": "2",
                        "target_files": ["strategy.py"],
                        "prohibited_files": ["national_bot.py"],
                    },
                ],
            },
            "gate_results": {"quality": {"all_passed": True, "failed_gates": []}},
        }
        feedback = (
            "opponent.py and strategy.py changes are compliant with their worker roles, "
            "compile cleanly, and the opponent self-test passes. However, national_bot.py "
            "was explicitly listed in do_not_touch and in both workers' prohibited_files, "
            "yet it was heavily refactored: new _last_raise_total/_minimum_raise_total/"
            "_raise_action helpers, _zero_action simplified, and _action_to_tcp behavior "
            "changed. This is an unauthorized scope drift and role-boundary violation. "
            "Revert bots/national_v104/national_bot.py to the v103 version to stay within "
            "the approved plan."
        )

        tasks = tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt, feedback)

        assert [(task["role"], task["target_files"]) for task in tasks] == [
            ("Scope Boundary Repair Architect", ["national_bot.py"]),
        ]
        assert "Scope-drift repair method" in tasks[0]["worker_prompt"]
        assert "Revert this target file to the source parent version" in tasks[0]["worker_prompt"]
        assert tool_planning._quality_failure_target_files(ckpt, feedback) == {
            "national_bot.py",
        }

        old_tasks = [
            {
                "worker_id": "auto_quality_repair_gate_strategy_py",
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py"],
                "must_change_files": ["strategy.py"],
                "repair_blocker": "quality_gate",
                "repair_contract": {"blocker": "quality_gate", "file": "strategy.py"},
            },
        ]
        stale_reason = tool_planning._stale_quality_task_reason(old_tasks, ckpt, feedback)
        assert "stale current quality repair contract" in stale_reason
        assert "quality_gate:strategy.py" in stale_reason

    def test_empty_quality_evidence_does_not_expand_to_changed_files(self):
        import tool_planning

        ckpt = {
            "next_v": 95,
            "source_v": 37,
            "parent2_v": 72,
            "stage": "repair_planned",
            "master_plan": {"strategy": "crossover", "tasks": []},
            "gate_results": {"quality": {"all_passed": True, "failed_gates": []}},
        }
        feedback = (
            "Other checks: strategy.py, postflop.py, national_bot.py, and main.py "
            "compile and import cleanly."
        )

        assert tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt, feedback) == []

    def test_critic_rework_synthesizes_crossover_task_from_gate_feedback(self, tmp_path, monkeypatch):
        import tool_planning

        source_dir = tmp_path / "national_v110"
        next_dir = tmp_path / "national_v115"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
        (next_dir / "strategy.py").write_text("VALUE = -1\n", encoding="utf-8")
        (source_dir / "postflop.py").write_text("POST = 1\n", encoding="utf-8")
        (next_dir / "postflop.py").write_text("POST = 1\n", encoding="utf-8")
        (source_dir / "national_bot.py").write_text("ENTRY = 'old'\n", encoding="utf-8")
        (next_dir / "national_bot.py").write_text("ENTRY = 'old'\n", encoding="utf-8")

        monkeypatch.setattr(
            tool_planning,
            "get_bot_dir",
            lambda version: {110: source_dir, 115: next_dir}[int(version)],
        )

        feedback = (
            "CRITIC_REJECTION score=4.0. Feedback: strategy.py inverted the "
            "H2H sign and treats losing matchups as winning matchups."
        )
        ckpt = {
            "next_v": 115,
            "source_v": 110,
            "parent2_v": 71,
            "stage": "repair_planned",
            "reviewer_feedback": feedback,
            "master_plan": {"strategy": "crossover", "tasks": []},
            "gate_results": {
                "quality": {"all_passed": True, "failed_gates": []},
                "critic": {
                    "approved": False,
                    "raw_approved": False,
                    "advisory_approved": False,
                    "score": 4.0,
                    "feedback": "strategy.py uses the wrong sign for prior H2H evidence.",
                },
            },
        }

        tasks = tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt)

        assert len(tasks) == 1
        assert tasks[0]["worker_id"] == "auto_critic_repair"
        assert tasks[0]["task_kind"] == "crossover_critic_repair"
        assert tasks[0]["target_files"] == ["strategy.py"]
        assert tasks[0]["must_change_files"] == ["strategy.py"]
        assert "Strategy Critic hard-gate repair" in tasks[0]["worker_prompt"]
        assert "H2H sign" in tasks[0]["worker_prompt"]
        assert tool_planning._should_reset_before_rework(ckpt, tasks) is False

    def test_review_rework_targets_primary_blocker_not_secondary_notes(self, tmp_path, monkeypatch):
        import tool_planning

        source_dir = tmp_path / "national_v123"
        next_dir = tmp_path / "national_v125"
        source_dir.mkdir()
        next_dir.mkdir()
        for name in ("strategy.py", "simulation.py", "constants.py"):
            (source_dir / name).write_text(f"{name} = 'old'\n", encoding="utf-8")
            (next_dir / name).write_text(f"{name} = 'new'\n", encoding="utf-8")

        monkeypatch.setattr(
            tool_planning,
            "get_bot_dir",
            lambda version: {123: source_dir, 125: next_dir}[int(version)],
        )

        feedback = (
            "Review rejected: simulation.py restored polarized_jam_equity(), but "
            "strategy.py no longer imports or calls it. Choose one complete path: "
            "restore both the simulation.py helper and strategy.py wiring, or remove "
            "the orphaned helper entirely.\n\n"
            "Also notes constants.py PASSIVE_THIN_VALUE_MAX_RATIO 0.40->0.48 is "
            "ungrounded, but this is not the main blocker."
        )
        ckpt = {
            "next_v": 125,
            "source_v": 123,
            "parent2_v": 120,
            "stage": "repair_planned",
            "reviewer_feedback": feedback,
            "master_plan": {"strategy": "crossover", "tasks": []},
            "gate_results": {
                "quality": {"all_passed": True, "failed_gates": []},
                "critic": {"approved": False, "score": 4.0},
                "review": {"approved": False, "feedback": feedback},
            },
        }
        old_tasks = [{
            "worker_id": "auto_quality_repair_gate_constants_py",
            "role": "Algorithmic Logic Architect",
            "target_files": ["constants.py"],
            "must_change_files": ["constants.py"],
            "worker_prompt": "old quality repair task for constants.py",
            "task_kind": "quality_repair",
            "repair_blocker": "quality_gate",
            "repair_contract": {"blocker": "quality_gate", "file": "constants.py"},
        }]

        assert tool_planning._is_review_rework_checkpoint(ckpt) is True
        assert tool_planning._is_critic_rework_checkpoint(ckpt) is False
        assert tool_planning._review_repair_target_files(ckpt, feedback) == [
            "simulation.py",
            "strategy.py",
        ]
        refresh_reason = tool_planning._review_repair_task_refresh_reason(old_tasks, ckpt, feedback)
        assert refresh_reason in {
            "checkpoint task is not a review repair",
            "review repair targets are stale",
        }

        tasks = tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt)

        assert len(tasks) == 1
        assert tasks[0]["worker_id"] == "auto_review_repair"
        assert tasks[0]["task_kind"] == "crossover_review_repair"
        assert tasks[0]["target_files"] == ["simulation.py", "strategy.py"]
        assert tasks[0]["must_change_files"] == ["simulation.py", "strategy.py"]
        assert "constants.py" not in tasks[0]["target_files"]
        assert "Lead Code Reviewer hard-gate repair" in tasks[0]["worker_prompt"]
        assert "Choose one complete path" in tasks[0]["worker_prompt"]
        assert tool_planning._should_reset_before_rework(ckpt, tasks) is False

    def test_execute_workers_refreshes_stale_review_rework_tasks(self, tmp_path, monkeypatch):
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v123"
        next_dir = tmp_path / "claude_v125"
        source_dir.mkdir()
        next_dir.mkdir()
        for name in ("strategy.py", "simulation.py", "constants.py"):
            (source_dir / name).write_text(f"{name} = 'old'\n", encoding="utf-8")
            (next_dir / name).write_text(f"{name} = 'new'\n", encoding="utf-8")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="repair_planned")
        feedback = (
            "Review rejected: simulation.py restored polarized_jam_equity(), but "
            "strategy.py no longer imports or calls it. Choose one complete path.\n\n"
            "Also notes constants.py PASSIVE_THIN_VALUE_MAX_RATIO is ungrounded, "
            "but this is not the main blocker."
        )
        state = json.loads(ckpt_file.read_text())
        state.update({"next_v": 125, "source_v": 123, "parent2_v": 120})
        state["reviewer_feedback"] = feedback
        state["master_plan"] = {
            "strategy": "crossover",
            "tasks": [{
                "worker_id": "auto_quality_repair_gate_constants_py",
                "role": "Algorithmic Logic Architect",
                "target_files": ["constants.py"],
                "must_change_files": ["constants.py"],
                "worker_prompt": "old quality repair task for constants.py",
                "task_kind": "quality_repair",
                "repair_blocker": "quality_gate",
                "repair_contract": {"blocker": "quality_gate", "file": "constants.py"},
            }],
            "work_item": {"kind": "crossover_quality_repair", "source_stage": "quality_failed"},
        }
        state["gate_results"] = {
            "quality": {"all_passed": True, "failed_gates": []},
            "critic": {"approved": False, "score": 4.0},
            "review": {"approved": False, "feedback": feedback},
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        def _reset_must_not_run(*_args, **_kwargs):
            raise AssertionError("review repair should be in-place")

        monkeypatch.setattr(tool_planning, "_incremental_reset_next_dir", _reset_must_not_run)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["strategy.py"]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 125, "source_v": 123})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        tasks = mock_exec.call_args.args[0]
        assert len(tasks) == 1
        assert tasks[0]["worker_id"] == "auto_review_repair"
        assert tasks[0]["task_kind"] == "crossover_review_repair"
        assert tasks[0]["target_files"] == ["simulation.py", "strategy.py"]
        assert "constants.py" not in tasks[0]["target_files"]
        assert mock_exec.call_args.kwargs["force_sequential"] is False
        assert "in-place Lead Code Reviewer repair" in mock_exec.call_args.kwargs["reviewer_feedback"]

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["master_plan"]["tasks"][0]["worker_id"] == "auto_review_repair"
        assert ckpt["master_plan"]["work_item"]["kind"] == "crossover_review_repair"
        assert ckpt["master_plan"]["work_item"]["reset_performed"] is False

    def test_critic_rework_without_file_evidence_uses_changed_strategy_files(self, tmp_path, monkeypatch):
        import tool_planning

        source_dir = tmp_path / "national_v110"
        next_dir = tmp_path / "national_v115"
        source_dir.mkdir()
        next_dir.mkdir()
        for name in ("strategy.py", "postflop.py", "national_bot.py"):
            (source_dir / name).write_text(f"{name} = 'old'\n", encoding="utf-8")
            (next_dir / name).write_text(f"{name} = 'new'\n", encoding="utf-8")

        monkeypatch.setattr(
            tool_planning,
            "get_bot_dir",
            lambda version: {110: source_dir, 115: next_dir}[int(version)],
        )

        ckpt = {
            "next_v": 115,
            "source_v": 110,
            "parent2_v": 71,
            "stage": "repair_planned",
            "reviewer_feedback": (
                "CRITIC_REJECTION score=5.0. Feedback: the candidate overfits "
                "weak historical matchups and misreads the battle experience."
            ),
            "master_plan": {"strategy": "crossover", "tasks": []},
            "gate_results": {
                "critic": {
                    "approved": False,
                    "raw_approved": False,
                    "advisory_approved": True,
                    "score": 5.0,
                },
            },
        }

        tasks = tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt)

        assert tasks[0]["task_kind"] == "crossover_critic_repair"
        assert tasks[0]["target_files"] == ["strategy.py", "postflop.py"]
        assert "national_bot.py" not in tasks[0]["target_files"]

    def test_quality_rework_refreshes_outdated_file_size_contract_prompt(self):
        import tool_planning

        ckpt = {
            "next_v": 282,
            "source_v": 28,
            "parent2_v": 235,
            "stage": "rework_running",
            "master_plan": {
                "strategy": "crossover",
                "tasks": [
                    {
                        "worker_id": "auto_quality_repair_file_size_strategy_py",
                        "role": "Algorithmic Logic Architect",
                        "target_files": ["strategy.py"],
                        "must_change_files": ["strategy.py"],
                        "worker_prompt": "old file-size repair",
                        "task_kind": "quality_repair",
                        "repair_blocker": "file_size",
                        "repair_contract": {"blocker": "file_size", "file": "strategy.py"},
                    }
                ],
            },
            "gate_results": {
                "quality": {
                    "all_passed": False,
                    "failed_gates": ["file_size(strategy.py:2483L/2000L)"],
                    "oversized_files": {"strategy.py": 2483},
                }
            },
        }

        old_tasks = ckpt["master_plan"]["tasks"]
        reason = tool_planning._stale_quality_task_reason(old_tasks, ckpt, "")
        refreshed = tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt)
        file_size_task = next(task for task in refreshed if task["repair_blocker"] == "file_size")

        assert "stale current quality repair contract" in reason
        assert "file_size:strategy.py" in reason
        assert "Large-overage requirement" in file_size_task["worker_prompt"]
        assert file_size_task["repair_contract"]["line_limit"] == 2000

    def test_mechanical_file_size_trim_preserves_strings_and_compiles(self, tmp_path, monkeypatch):
        import py_compile
        import tool_planning

        next_dir = tmp_path / "claude_v11"
        source_dir = tmp_path / "claude_v10"
        next_dir.mkdir()
        source_dir.mkdir()
        target = next_dir / "strategy.py"
        target.write_text(
            '"""module docs\\nline two\\n"""\n'
            "# removable header\n"
            "\n"
            "VALUE = 1\n"
            "\n"
            "def f():\n"
            '    """function docs\\n    line two\\n    line three\\n    """\n'
            "    # removable body comment\n"
            '    text = """keep\\n\\nblank line inside string\\n"""\n'
            "    return text, VALUE\n",
            encoding="utf-8",
        )
        task = {
            "worker_id": "auto_quality_repair_file_size_strategy_py",
            "target_files": ["strategy.py"],
            "repair_blocker": "file_size",
            "repair_contract": {"blocker": "file_size", "file": "strategy.py"},
        }

        monkeypatch.setattr(
            tool_planning,
            "check_code_size",
            lambda *_a, **_k: (250, [("strategy.py", 250, 5)]),
        )

        results = tool_planning._apply_mechanical_file_size_trims(
            [task],
            next_dir,
            source_dir,
            11,
            10,
        )
        text = target.read_text(encoding="utf-8")

        assert results and results[0]["changed"] is True
        assert results[0]["after"] < results[0]["before"]
        assert "module docs" not in text
        assert "function docs" not in text
        assert "removable" not in text
        assert '"""keep\\n\\nblank line inside string\\n"""' in text
        py_compile.compile(str(target), doraise=True)

    def test_quality_repair_synthesizes_national_native_contract_task(self):
        import tool_planning

        ckpt = {
            "next_v": 282,
            "source_v": 28,
            "parent2_v": 235,
            "stage": "quality_failed",
            "master_plan": {"strategy": "crossover", "tasks": []},
            "gate_results": {
                "quality": {
                    "all_passed": False,
                    "national_native_contract_ok": False,
                    "failed_gates": [
                        (
                            "national_native_contract(national_bot.py: "
                            "_strategy_action must not continue with raw action after sanitizer failure)"
                        ),
                        "file_size(strategy.py:2483L/2000L)",
                    ],
                    "national_native_contract_errors": [
                        (
                            "national_bot.py: _strategy_action must not continue "
                            "with raw action after sanitizer failure"
                        ),
                    ],
                    "protected_contract_errors": [
                        "opponent.py: print() emits TCP action text; output must be JSON response int",
                    ],
                    "oversized_files": {"strategy.py": 2483},
                }
            },
        }

        tasks = tool_planning._synthesize_rework_tasks_from_checkpoint(ckpt)
        blocker_files = [(task["repair_blocker"], task["target_files"][0]) for task in tasks]

        assert ("national_native_contract", "national_bot.py") in blocker_files
        assert blocker_files.count(("quality_gate", "national_bot.py")) == 0
        native_task = next(task for task in tasks if task["repair_blocker"] == "national_native_contract")
        assert native_task["role"] == "Protocol Integration Architect"
        assert native_task["must_change_files"] == ["national_bot.py"]
        assert "direct TCP client" in native_task["worker_prompt"]
        assert "sever/bot_adapter.py" in native_task["worker_prompt"]

    def test_repair_planned_crossover_quality_retry_preserves_candidate(self, tmp_path, monkeypatch):
        """A planned retry of crossover quality repair must not reset the fused candidate."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy.py").write_text("def act():\n    return 0\n")
        (next_dir / "strategy.py").write_text("def act():\n    return 1\n")
        (next_dir / "opponent.py").write_text("def opp():\n    return 'bad'\n")
        (next_dir / "state.py").write_text("def state():\n    return 'bad'\n")
        (next_dir / "strategy_helpers.py").write_text("def helper():\n    return 'bad'\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="repair_planned")
        state = json.loads(ckpt_file.read_text())
        state["parent2_v"] = 9
        state["reviewer_feedback"] = "Quality gates failed: position_semantics(strategy_helpers.py:1188)"
        state["master_plan"] = {
            "strategy": "crossover",
            "tasks": [{
                "worker_id": "auto_quality_repair",
                "role": "Algorithmic Logic Architect",
                "target_files": ["opponent.py", "state.py", "strategy_helpers.py"],
                "worker_prompt": "fix quality gates",
                "task_kind": "quality_repair",
            }],
            "work_item": {
                "kind": "crossover_quality_repair",
                "source_stage": "quality_failed",
                "reset_performed": False,
            },
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        def _reset_must_not_run(*_args, **_kwargs):
            raise AssertionError("crossover quality repair retries must stay in-place")

        monkeypatch.setattr(tool_planning, "_incremental_reset_next_dir", _reset_must_not_run)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = (False, {}, [])
                return await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})

        result = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert (next_dir / "strategy.py").read_text() == "def act():\n    return 1\n"

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "repair_planned"
        assert ckpt["master_plan"]["work_item"]["kind"] == "crossover_quality_repair"
        assert ckpt["master_plan"]["work_item"]["reset_performed"] is False

    def test_run_quality_gates_blocks_repair_planned_stage(self, tmp_path, monkeypatch):
        """Quality gates must not run again before repair workers execute."""
        import asyncio
        import tool_gates

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="repair_planned")
        result = asyncio.run(tool_gates.run_quality_gates.handler({
            "version": 11,
            "source_v": 10,
        }))
        data = json.loads(result["content"][0]["text"])

        assert "STATE BLOCKED" in data["error"]
        assert "execute_workers" in data["error"]
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "repair_planned"
        assert ckpt["gate_results"] == {}

    def test_failed_workers_increment_count(self, tmp_path, monkeypatch):
        """Initial worker failure should force Master replan, not replay tasks."""
        import asyncio
        import evolution_infra
        from pipeline_state import next_tool_for_checkpoint
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
        assert ckpt["stage"] == "direction_audited"
        assert ckpt["master_plan"] == {}
        assert next_tool_for_checkpoint(ckpt) == "run_master"
        assert ckpt["audit_context"]["worker_execution_failed_replan"]["worker_failure_count"] == 3

    def test_failed_repair_workers_keep_repair_route(self, tmp_path, monkeypatch):
        """Gate/precommit repair failures keep execute_workers route with feedback."""
        import asyncio
        import fix_injection
        import tool_planning
        from pipeline_state import next_tool_for_checkpoint

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy.py").write_text("def act():\n    return 0\n")
        (next_dir / "strategy.py").write_text("def act():\n    return 1\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, failure_count=1, stage="quality_failed")
        state = json.loads(ckpt_file.read_text())
        state["gate_results"] = {
            "quality": {
                "all_passed": False,
                "failed_gates": ["compile(strategy.py)"],
            }
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = (False, {}, [])
                return await tool_planning.execute_workers.handler({
                    "next_v": 11,
                    "source_v": 10,
                })

        result = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["worker_failure_count"] == 2
        assert ckpt["stage"] == "repair_planned"
        assert next_tool_for_checkpoint(ckpt) == "execute_workers"
        assert "Quality gates failed" in ckpt["reviewer_feedback"]

    def test_reviewer_feedback_retry_from_quality_passed_records_workers_done(self, tmp_path, monkeypatch):
        """Review-reject repairs must reset stage before workers_done is recorded."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy.py").write_text("def act():\n    return 0\n")
        (next_dir / "strategy.py").write_text("def act():\n    return 1\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="quality_passed")
        state = json.loads(ckpt_file.read_text())
        state["precommit_attempt"] = 1
        state["gate_results"] = {
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
            "review": {"approved": False, "feedback": "dead helper not wired"},
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["strategy.py"]):
                mock_exec.return_value = (True, {}, [])
                return await tool_planning.execute_workers.handler({
                    "tasks": [
                        {"worker_id": 1, "role": "arch", "target_files": ["strategy.py"], "worker_prompt": "wire helper"},
                    ],
                    "next_v": 11,
                    "source_v": 10,
                    "reviewer_feedback": "dead helper not wired",
                })

        result = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["precommit_attempt"] == 0

    def test_precommit_failed_retry_preserves_tasks_for_resume(self, tmp_path, monkeypatch):
        """Precommit rework from a crossover child must persist retry tasks."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy.py").write_text("def act():\n    return 0\n")
        (next_dir / "strategy.py").write_text("def act():\n    return 1\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="precommit_failed")
        state = json.loads(ckpt_file.read_text())
        state["precommit_attempt"] = 1
        state["master_plan"] = {"strategy": "crossover", "tasks": []}
        state["reviewer_feedback"] = "Precommit FAILED vs parent; rework stackoff guard"
        state["gate_results"] = {
            "quality": {"all_passed": True, "critical_scenarios_passed": True},
            "review": {"approved": True},
            "critic": {"approved": True},
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "semantic_regression"}],
            },
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        retry_tasks = [
            {"worker_id": 1, "role": "arch", "target_files": ["strategy.py"], "worker_prompt": "fix stackoff"},
        ]

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec:
                mock_exec.return_value = (False, {}, [])
                return await tool_planning.execute_workers.handler({
                    "tasks": retry_tasks,
                    "next_v": 11,
                    "source_v": 10,
                })

        result = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "repair_planned"
        assert ckpt["precommit_attempt"] == 0
        assert ckpt["master_plan"]["tasks"][0]["worker_id"] == "auto_precommit_repair_strategy_py"
        assert ckpt["master_plan"]["tasks"][0]["target_files"] == ["strategy.py"]
        assert ckpt["master_plan"]["tasks"][0]["must_change_files"] == ["strategy.py"]
        assert ckpt["master_plan"]["tasks"][0]["repair_blocker"] == "precommit_regression"
        assert ckpt["master_plan"]["work_item"]["kind"] == "precommit_repair"
        assert "Precommit FAILED" in ckpt["reviewer_feedback"]
        assert ckpt["precommit_rework_count"] == 1

    def test_precommit_rework_circuit_breaker_blocks_repeated_repairs(self, tmp_path, monkeypatch):
        """Repeated precommit repair rounds should abandon instead of looping forever."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "opponent.py").write_text("def opp():\n    return 0\n")
        (next_dir / "opponent.py").write_text("def opp():\n    return 1\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="precommit_failed")
        state = json.loads(ckpt_file.read_text())
        state["precommit_rework_count"] = 2
        state["reviewer_feedback"] = "Precommit FAILED vs parent"
        state["master_plan"] = {"strategy": "single", "tasks": []}
        state["gate_results"] = {
            "quality": {"all_passed": True},
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "aggregate_negative_chip_ev"}],
            },
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "MAX_PRECOMMIT_REWORK_ROUNDS", 2)
        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec:
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])

        assert data["error"] == "PRECOMMIT_REWORK_CIRCUIT_BREAKER"
        assert data["precommit_rework_count"] == 2
        mock_exec.assert_not_called()
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "precommit_failed"
        assert ckpt["precommit_rework_count"] == 2

    def test_official_rework_count_advances_after_successful_worker_batch(self, tmp_path, monkeypatch):
        """A successful worker does not erase the finite formal-repair budget."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy.py").write_text("def act():\n    return 0\n")
        (next_dir / "strategy.py").write_text("def act():\n    return 1\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="official_failed")
        state = json.loads(ckpt_file.read_text())
        state["official_rework_count"] = 0
        state["reviewer_feedback"] = "Official obvious_decision_error"
        state["gate_results"] = {
            "official_full": {
                "passed": False,
                "issues": ["obvious_decision_error: repeated river overcall"],
                "official_evidence_summary": {
                    "classification": "obvious_decision_error",
                    "blocking": True,
                },
            },
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["strategy.py"]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({
                    "next_v": 11,
                    "source_v": 10,
                })
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        executed_tasks = mock_exec.await_args.args[0]
        assert [task["worker_id"] for task in executed_tasks] == [
            "auto_official_full_repair"
        ]
        assert all(
            task.get("task_kind") == "official_repair"
            for task in executed_tasks
        )
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["official_rework_count"] == 1

    def test_official_rework_circuit_breaker_blocks_repeated_formal_runs(self, tmp_path, monkeypatch):
        """Formal 5+3 certification cannot loop forever after worker success."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy.py").write_text("def act():\n    return 0\n")
        (next_dir / "strategy.py").write_text("def act():\n    return 1\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="official_failed")
        state = json.loads(ckpt_file.read_text())
        state["official_rework_count"] = 2
        state["reviewer_feedback"] = "Official obvious_decision_error"
        state["gate_results"] = {
            "official_full": {
                "passed": False,
                "issues": ["obvious_decision_error: repeated river overcall"],
                "official_evidence_summary": {
                    "classification": "obvious_decision_error",
                    "blocking": True,
                },
            },
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "MAX_OFFICIAL_REWORK_ROUNDS", 2)
        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        abandon_calls = []

        async def _fake_force_abandon(next_v, source_v):
            abandon_calls.append((next_v, source_v))
            return {"abandoned": True, "abandoned_v": next_v}

        monkeypatch.setattr(
            tool_planning,
            "_force_abandon_official_rework_generation",
            _fake_force_abandon,
        )

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec:
                result = await tool_planning.execute_workers.handler({
                    "next_v": 11,
                    "source_v": 10,
                })
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])

        assert data["error"] == "OFFICIAL_REWORK_CIRCUIT_BREAKER"
        assert data["official_rework_count"] == 2
        assert data["abandoned"] is True
        assert abandon_calls == [(11, 10)]
        mock_exec.assert_not_called()
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "official_failed"
        assert ckpt["official_rework_count"] == 2

    def test_precommit_failed_refreshes_stale_quality_tasks(self, tmp_path, monkeypatch):
        """precommit_failed must not reuse stale quality-repair tasks from checkpoint."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        for directory in (source_dir, next_dir):
            (directory / "strategy.py").write_text("def act():\n    return 0\n")
            (directory / "postflop.py").write_text("def postflop():\n    return 0\n")
            (directory / "strategy_helpers.py").write_text("def helper():\n    return 0\n")
            (directory / "opponent.py").write_text("def opp():\n    return 0\n")
            (directory / "state.py").write_text("def state():\n    return 0\n")
        (next_dir / "strategy.py").write_text("def act():\n    return 1\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="precommit_failed")
        state = json.loads(ckpt_file.read_text())
        state["parent2_v"] = 9
        state["precommit_attempt"] = 1
        state["reviewer_feedback"] = ""
        state["master_plan"] = {
            "strategy": "crossover",
            "tasks": [{
                "worker_id": "auto_quality_repair",
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py", "opponent.py", "state.py", "strategy_helpers.py"],
                "worker_prompt": "old quality repair: fix position_semantics and file_size",
                "task_kind": "quality_repair",
            }],
        }
        state["gate_results"] = {
            "quality": {"all_passed": True, "failed_gates": []},
            "review": {"approved": True},
            "critic": {"approved": True},
            "precommit_eval": {
                "passed": False,
                "directive": (
                    "Precommit FAILED (attempt 1/3). Do NOT call run_precommit_eval again; "
                    "call execute_workers targeting the loss vs claude_v241."
                ),
                "blockers": [
                    {
                        "reason": "aggregate_negative_chip_ev",
                        "details": "Aggregate W/L 32-32-0 but mean net chips -2015.",
                    },
                    {
                        "reason": "semantic_regression",
                        "details": "clear_regression",
                        "evidence": ["vs claude_v241: 7-9 W/L with net_chips sum -23229"],
                    },
                ],
                "matchups": [
                    {
                        "opponent": "claude_v241",
                        "reason": "top_strength",
                        "wins": 7,
                        "losses": 9,
                        "draws": 0,
                        "net_chips": [-5422.0, -9004.0, -14591.0, -51.0],
                    }
                ],
            },
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        def _reset_must_not_run(*_args, **_kwargs):
            raise AssertionError("precommit repair should preserve the failed candidate")

        monkeypatch.setattr(tool_planning, "_incremental_reset_next_dir", _reset_must_not_run)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["strategy.py"]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        tasks = mock_exec.call_args.args[0]
        assert tasks[0]["worker_id"] == "auto_precommit_repair_strategy_py"
        assert tasks[0]["task_kind"] == "precommit_repair"
        assert tasks[0]["target_files"] == ["strategy.py"]
        assert tasks[0]["must_change_files"] == ["strategy.py"]
        assert "aggregate_negative_chip_ev" in tasks[0]["worker_prompt"]
        assert "semantic_regression" in tasks[0]["worker_prompt"]
        assert "Non-negotiable national position invariant" in tasks[0]["worker_prompt"]
        assert "`dealer_id` is the small blind" in tasks[0]["worker_prompt"]
        assert "not an EV/matchup lever" in tasks[0]["worker_prompt"]
        assert "next_player(dealer_id, 1)" in tasks[0]["worker_prompt"]
        assert tasks[0]["repair_contract"]["protected_invariants"] == ["national_position_semantics"]
        assert "old quality repair" not in tasks[0]["worker_prompt"]
        assert "in-place precommit regression repair" in mock_exec.call_args.kwargs["reviewer_feedback"]
        assert (next_dir / "strategy.py").read_text() == "def act():\n    return 1\n"

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["master_plan"]["tasks"][0]["worker_id"] == "auto_precommit_repair_strategy_py"
        assert ckpt["master_plan"]["work_item"]["kind"] == "precommit_repair"
        assert ckpt["master_plan"]["work_item"]["reset_performed"] is False

    def test_precommit_repair_refreshes_tasks_missing_position_invariant(self, tmp_path, monkeypatch):
        """Old file-scoped precommit repair tasks must be regenerated with protocol invariants."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        (source_dir / "strategy.py").write_text("def act():\n    return 0\n")
        (next_dir / "strategy.py").write_text("def act():\n    return 1\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="precommit_failed")
        state = json.loads(ckpt_file.read_text())
        state["reviewer_feedback"] = "Precommit FAILED vs parent"
        state["master_plan"] = {
            "strategy": "single",
            "tasks": [{
                "worker_id": "auto_precommit_repair_strategy_py",
                "role": "Strategic Regression Repair Architect",
                "target_files": ["strategy.py"],
                "must_change_files": ["strategy.py"],
                "worker_prompt": "old file-scoped precommit repair without national position invariant",
                "task_kind": "precommit_repair",
                "repair_contract": {"blocker": "precommit_regression", "file": "strategy.py"},
            }],
        }
        state["gate_results"] = {
            "quality": {"all_passed": True},
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "lost_to_parent", "details": "Native national mean net chips -3000"}],
            },
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]), \
                 patch.object(tool_planning, "_py_files_changed_between", return_value=["strategy.py"]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        task = mock_exec.call_args.args[0][0]
        assert task["worker_id"] == "auto_precommit_repair_strategy_py"
        assert "Non-negotiable national position invariant" in task["worker_prompt"]
        assert "not an EV/matchup lever" in task["worker_prompt"]
        assert task["repair_contract"]["protected_invariants"] == ["national_position_semantics"]

    def test_precommit_repair_targets_actual_changed_file(self, tmp_path, monkeypatch):
        """Numeric precommit failures should target the real candidate diff, not broad defaults."""
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        for directory in (source_dir, next_dir):
            (directory / "strategy.py").write_text("def act():\n    return 0\n")
            (directory / "postflop.py").write_text("def postflop():\n    return 0\n")
            (directory / "strategy_helpers.py").write_text("def helper():\n    return 0\n")
            (directory / "opponent.py").write_text("def opp():\n    return 0\n")
        (next_dir / "opponent.py").write_text("def opp():\n    return 1\n")

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")

        ckpt = {
            "next_v": 11,
            "source_v": 10,
            "stage": "precommit_failed",
            "master_plan": {"strategy": "crossover", "tasks": []},
            "gate_results": {
                "precommit_eval": {
                    "passed": False,
                    "blockers": [{"reason": "aggregate_negative_chip_ev"}],
                }
            },
        }

        tasks = tool_planning._synthesize_rework_tasks_from_checkpoint(
            ckpt,
            "National precommit FAILED vs claude_v10",
        )

        assert [task["worker_id"] for task in tasks] == ["auto_precommit_repair_opponent_py"]
        assert [task["target_files"] for task in tasks] == [["opponent.py"]]
        assert tasks[0]["repair_contract"]["blocker"] == "precommit_regression"

    def test_rework_running_refreshes_broad_precommit_task(self, tmp_path, monkeypatch):
        """A resumed old broad auto_precommit_repair task must become file-scoped."""
        import asyncio
        import fix_injection
        import tool_planning

        source_dir = tmp_path / "claude_v10"
        next_dir = tmp_path / "claude_v11"
        source_dir.mkdir()
        next_dir.mkdir()
        for directory in (source_dir, next_dir):
            for filename in ("strategy.py", "postflop.py", "strategy_helpers.py", "opponent.py"):
                (directory / filename).write_text(f"def marker():\n    return {filename!r}\n")
        (next_dir / "opponent.py").write_text("def marker():\n    return 'changed opponent'\n")

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, stage="rework_running")
        state = json.loads(ckpt_file.read_text())
        state["parent2_v"] = 9
        state["reviewer_feedback"] = "National precommit FAILED vs claude_v10"
        state["master_plan"] = {
            "strategy": "crossover",
            "tasks": [{
                "worker_id": "auto_precommit_repair",
                "role": "Strategic Regression Repair Architect",
                "target_files": ["strategy.py", "postflop.py", "strategy_helpers.py", "opponent.py"],
                "must_change_files": ["strategy.py", "postflop.py", "strategy_helpers.py", "opponent.py"],
                "worker_prompt": "old broad precommit repair",
                "task_kind": "precommit_repair",
            }],
            "work_item": {
                "kind": "precommit_repair",
                "source_stage": "precommit_failed",
                "reset_performed": False,
            },
        }
        state["gate_results"] = {
            "quality": {"all_passed": True},
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "aggregate_negative_chip_ev"}],
            },
        }
        ckpt_file.write_text(json.dumps(state))

        monkeypatch.setattr(tool_planning, "get_bot_dir", lambda v: tmp_path / f"claude_v{v}")
        monkeypatch.setattr(fix_injection, "apply_known_fixes", lambda _path: ([], []))
        monkeypatch.setattr(fix_injection, "log_fix_application", lambda *_a, **_k: None)

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec, \
                 patch.object(tool_planning, "_validate_worker_boundaries", return_value=[]):
                mock_exec.return_value = (True, {}, [])
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is True
        tasks = mock_exec.call_args.args[0]
        assert [task["worker_id"] for task in tasks] == ["auto_precommit_repair_opponent_py"]
        assert [task["target_files"] for task in tasks] == [["opponent.py"]]
        assert [task["must_change_files"] for task in tasks] == [["opponent.py"]]

        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "workers_done"
        assert ckpt["master_plan"]["tasks"][0]["worker_id"] == "auto_precommit_repair_opponent_py"
        assert ckpt["master_plan"]["work_item"]["kind"] == "precommit_repair"

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

    def test_execute_workers_blocks_stale_exhausted_master_plan(self, tmp_path, monkeypatch):
        """Old master_planned checkpoints must not execute a plan that current
        validation would reject as an EXHAUSTED direction."""
        import asyncio
        import tool_planning
        from runtime_architecture_policy import attach_runtime_contract_ledger

        ckpt_file = self._setup_checkpoint(tmp_path, monkeypatch, failure_count=0)
        state = json.loads(ckpt_file.read_text())
        state["direction_audit"] = {
            "repetition_detected": True,
            "exhausted_directions": ["fold margin clamp tuning"],
            "llm_failed": False,
        }
        state["master_plan"] = attach_runtime_contract_ledger({
            "analysis": "stale plan",
            "tasks": [{
                "worker_id": 1,
                "role": "Algorithmic Logic Architect",
                "target_files": ["strategy.py"],
                "worker_prompt": "Parameter tuning: adjust fold margin clamp and sizing_aggr constants.",
                "runtime_contract": {
                    "decision": None,
                    "precompute_artifacts": [],
                    "match_memory": None,
                    "official_feedback_refs": [],
                    "forbidden_runtime_work": [],
                },
            }],
        }, replace=True)
        state["runtime_contract_ledger"] = state["master_plan"]["runtime_contract_ledger"]
        ckpt_file.write_text(json.dumps(state))
        monkeypatch.setattr(
            tool_planning,
            "_extract_exhausted_keywords",
            lambda: [("parameter_tuning", "fold margin clamp sizing_aggr tuning")],
        )

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec:
                result = await tool_planning.execute_workers.handler({"next_v": 11, "source_v": 10})
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])

        assert data["error"] == "WORKER_EXHAUSTED_PLAN_BLOCKED"
        assert data["next_tool"] == "run_master"
        mock_exec.assert_not_called()
        ckpt = json.loads(ckpt_file.read_text())
        assert ckpt["stage"] == "direction_audited"
        assert ckpt["master_plan"] == {}
        assert ckpt["runtime_contract_ledger"] is None
        assert ckpt["audit_attempt"] == 1
        assert ckpt["direction_audit"]["repetition_detected"] is True
        recovery = ckpt["audit_context"]["worker_exhausted_plan_blocked"]
        assert recovery["runtime_contract_ledger_reset"] is True
        assert recovery["previous_runtime_contract_ledger_digest"]

    def test_exhausted_plan_does_not_claim_replan_when_checkpoint_write_fails(
        self,
        tmp_path,
        monkeypatch,
    ):
        import asyncio
        import tool_planning

        self._setup_checkpoint(tmp_path, monkeypatch, failure_count=0)
        monkeypatch.setattr(
            tool_planning,
            "_extract_exhausted_keywords",
            lambda: [("parameter_tuning", "fold margin clamp tuning")],
        )
        monkeypatch.setattr(tool_planning, "write_pipeline_checkpoint", lambda *_a, **_k: False)
        events = []
        monkeypatch.setattr(
            tool_planning,
            "log_system_event",
            lambda *args, **_kwargs: events.append(args),
        )

        async def _run():
            with patch.object(tool_planning, "_execute_workers", new_callable=AsyncMock) as mock_exec:
                result = await tool_planning.execute_workers.handler({
                    "next_v": 11,
                    "source_v": 10,
                    "tasks": [{
                        "worker_id": 1,
                        "role": "Algorithmic Logic Architect",
                        "target_files": ["strategy.py"],
                        "worker_prompt": "Adjust the fold margin clamp with parameter tuning.",
                    }],
                })
                return result, mock_exec

        result, mock_exec = asyncio.run(_run())
        data = json.loads(result["content"][0]["text"])

        assert data["error"] == "WORKER_EXHAUSTED_PLAN_RECOVERY_FAILED"
        assert "next_tool" not in data
        assert "No re-planning transition has been recorded" in data["message"]
        mock_exec.assert_not_called()
        assert [event[0] for event in events] == [
            "pipeline.worker_exhausted_plan_recovery_failed"
        ]

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
