"""Tests for the cross-generation local-optima constraint injection (STEP3).

Validates that prior critic local-optima rejections flow back into the next
generation's Master prompt as a hard constraint, breaking the cycle where the
Master keeps re-proposing an exhausted direction (observed: v82 master
re-proposed constant-tuning after the critic rejected it for exactly that).

Covers tool_planning._load_recent_critic_local_optima and
_build_cross_gen_constraint_block.
"""

import json

import pytest


def _write_failures(path, entries):
    """Append worker_failures.jsonl rows to the (isolated) path."""
    with open(path, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


@pytest.fixture
def failures_file():
    """The monkeypatched WORKER_FAILURES_FILE (isolated to tmp by conftest)."""
    import evolution_infra
    return evolution_infra.WORKER_FAILURES_FILE


class TestLoadRecentCriticLocalOptima:
    def test_filters_by_gen_and_critic_only(self, failures_file):
        """Only critic local_optima entries with gen <= next_v are returned;
        reviewer/worker records, out-of-range (future) gens, and non-True
        warning flags are excluded."""
        import core.tool_planning as tp
        _write_failures(failures_file, [
            {"gen": 81, "worker_id": "critic", "role": "Strategy Critic",
             "error": "Rejected", "timestamp": 1.0,
             "local_optima_warning": True, "local_optima_reason": "constant tuning plateau"},
            {"gen": 82, "worker_id": "critic", "role": "Strategy Critic",
             "error": "Rejected", "timestamp": 2.0,
             "local_optima_warning": True, "local_optima_reason": "v82 constant tweaks"},
            # reviewer reject — no local_optima field, must be skipped
            {"gen": 82, "worker_id": "reviewer", "role": "Code Reviewer",
             "error": "Rejected", "timestamp": 3.0},
            # worker failure — different schema, must be skipped
            {"gen": 80, "worker_id": 1, "role": "Worker", "error": "boom",
             "failure_type": "timeout"},
            # future gen (83 > next_v=82) — must be excluded
            {"gen": 83, "worker_id": "critic", "role": "Strategy Critic",
             "error": "future", "timestamp": 5.0,
             "local_optima_warning": True, "local_optima_reason": "future gen"},
        ])
        result = tp._load_recent_critic_local_optima(next_v=82)
        gens = [g for g, _, _ in result]
        assert gens == [82, 81], f"expected [82, 81], got {gens}"
        reasons = [r for _, r, _ in result]
        assert any("v82 constant tweaks" in r for r in reasons)

    def test_dedup_same_gen_keeps_latest(self, failures_file):
        """retry_workers can reject the same gen repeatedly; keep the latest."""
        import core.tool_planning as tp
        _write_failures(failures_file, [
            {"gen": 82, "worker_id": "critic", "role": "Strategy Critic",
             "error": "first", "timestamp": 1.0,
             "local_optima_warning": True, "local_optima_reason": "first attempt"},
            {"gen": 82, "worker_id": "critic", "role": "Strategy Critic",
             "error": "second", "timestamp": 5.0,
             "local_optima_warning": True, "local_optima_reason": "second attempt"},
        ])
        result = tp._load_recent_critic_local_optima(next_v=82)
        assert len(result) == 1, f"expected dedup to 1, got {len(result)}"
        assert "second attempt" in result[0][1]

    def test_empty_when_no_local_optima(self, failures_file):
        """Normal critic reject (no local_optima flag) -> empty list."""
        import core.tool_planning as tp
        _write_failures(failures_file, [
            {"gen": 82, "worker_id": "critic", "role": "Strategy Critic",
             "error": "normal reject, not local optima", "timestamp": 1.0},
        ])
        assert tp._load_recent_critic_local_optima(next_v=82) == []

    def test_false_warning_is_skipped(self, failures_file):
        """local_optima_warning=False is never written by _record_quality_failure
        (v is not False filter); even if a malformed False row existed, it's skipped."""
        import core.tool_planning as tp
        _write_failures(failures_file, [
            {"gen": 82, "worker_id": "critic", "role": "Strategy Critic",
             "error": "x", "timestamp": 1.0, "local_optima_warning": False},
        ])
        assert tp._load_recent_critic_local_optima(next_v=82) == []


class TestBuildCrossGenConstraintBlock:
    def test_no_inject_when_no_rejection_and_no_pool(self, failures_file, monkeypatch, tmp_path):
        """First-ever gen / clean crossover: no critic rejection + empty pool -> ''."""
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", tmp_path / "nonexistent.md")
        assert tp._build_cross_gen_constraint_block(next_v=82) == ""

    def test_injects_critic_local_optima_reason(self, failures_file, monkeypatch, tmp_path):
        """A prior critic local-optima rejection produces a constraint block
        naming that reason."""
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", tmp_path / "nonexistent.md")
        _write_failures(failures_file, [
            {"gen": 82, "worker_id": "critic", "role": "Strategy Critic",
             "error": "Rejected", "timestamp": 1.0,
             "local_optima_warning": True,
             "local_optima_reason": "constant tuning of sizing ratios is exhausted"},
        ])
        block = tp._build_cross_gen_constraint_block(next_v=82)
        assert block, "expected non-empty block"
        assert tp.CROSS_GEN_MARKER in block
        assert "constant tuning of sizing ratios is exhausted" in block

    def test_wording_not_unconditional_forbidden(self, failures_file, monkeypatch, tmp_path):
        """The block must NOT be an unconditional FORBIDDEN — it must require a
        structural method + H2H evidence (a path forward) and explicitly NOT
        over-generalize (so legitimate opponent-stat sizing isn't refused, which
        is the very reframe the critic asked v82 to do)."""
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", tmp_path / "nonexistent.md")
        _write_failures(failures_file, [
            {"gen": 82, "worker_id": "critic", "role": "Strategy Critic",
             "error": "x", "timestamp": 1.0,
             "local_optima_warning": True, "local_optima_reason": "constant tuning"},
        ])
        block = tp._build_cross_gen_constraint_block(next_v=82)
        assert "STRUCTURAL new method" in block
        assert "H2H evidence" in block
        assert "still permitted and encouraged" in block

    def test_idempotent_marker_guard(self, failures_file, monkeypatch, tmp_path):
        """run_master retries must not stack the constraint block. Simulate the
        caller's CROSS_GEN_MARKER guard over two apply attempts."""
        import core.tool_planning as tp
        monkeypatch.setattr(tp, "EXPERIENCE_FILE", tmp_path / "nonexistent.md")
        _write_failures(failures_file, [
            {"gen": 82, "worker_id": "critic", "role": "Strategy Critic",
             "error": "x", "timestamp": 1.0,
             "local_optima_warning": True, "local_optima_reason": "constant tuning"},
        ])
        perf = ""
        block = tp._build_cross_gen_constraint_block(next_v=82)
        if block and tp.CROSS_GEN_MARKER not in perf:
            perf = perf + block
        # Second "retry" — marker already present, must not append again
        block2 = tp._build_cross_gen_constraint_block(next_v=82)
        if block2 and tp.CROSS_GEN_MARKER not in perf:
            perf = perf + block2
        assert perf.count(tp.CROSS_GEN_MARKER) == 1, \
            f"expected marker once, got {perf.count(tp.CROSS_GEN_MARKER)}"
