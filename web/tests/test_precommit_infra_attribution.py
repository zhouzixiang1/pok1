"""Regression guard for the precommit native-TCP infra misattribution bug.

The precommit gate (``run_precommit_eval``) previously attributed ANY
non-passed result to ``failure_class="regression"`` (candidate rework) even
when every blocker was infrastructure-class — a native run killed mid-match
by a startup-watchdog kill, transport stall, launch-latency spike, or a runner
exception.  The bot runtime is system-owned and byte-identical across every
candidate and baseline opponent, so such a stall can never be a candidate
policy defect.  The Slice-2b consumer then rejected the candidate permanently
with zero retries (the same class of bug as the smoke-gate single-shot
abandon that stranded v30-v62 at ``workers_done``).

Two fixes:
1. ``failure_classification.INFRA_BLOCKER_REASONS`` now includes the native
   runner's actual blocker vocabulary.
2. ``tool_eval.run_precommit_eval`` consults ``classify_precommit_gate`` and
   emits ``failure_class="infrastructure"`` + ``retry_same_tool`` when all
   blockers are infra-class, so the consumer's bounded infra retry budget
   absorbs the transient stall instead of rejecting.

These tests pin the classification layer (the mechanism behind fix #2) so the
infra vocabulary cannot silently drift again.
"""

from failure_classification import (
    INFRA_BLOCKER_REASONS,
    classify_precommit_gate,
    is_infra_blocker,
)


def test_native_runner_blockers_are_infra_class():
    """The native TCP runner's blocker vocabulary is infrastructure-class."""
    native_reasons = [
        "native_incomplete_match",
        "native_precommit_sample_shortfall",
        "native_no_samples",
        "native_precommit_exception",
        "national_precommit_exception",
    ]
    for reason in native_reasons:
        assert reason in INFRA_BLOCKER_REASONS, (
            f"{reason} must be in INFRA_BLOCKER_REASONS so a transient native "
            f"stall is not misattributed as a candidate regression"
        )
        assert is_infra_blocker(reason), f"{reason} must classify as infra"


def test_legacy_infra_blockers_still_classified():
    """The original four infra reasons are unchanged."""
    for reason in ("match_timeout", "incomplete_or_timeout", "scheduler_error", "match_exception"):
        assert is_infra_blocker(reason)


def test_classify_precommit_gate_all_infra_returns_infra_timeout():
    """A gate whose blockers are all infra-class classifies as infra_timeout
    (retryable), NOT regression."""
    gate = {
        "passed": False,
        "blockers": [
            {"reason": "native_incomplete_match", "details": "..."},
            {"reason": "native_no_samples", "details": "..."},
        ],
    }
    assert classify_precommit_gate(gate) == "infra_timeout"


def test_classify_precommit_gate_native_exception_is_infra():
    """A single native_precommit_exception blocker classifies as infra_timeout."""
    gate = {
        "passed": False,
        "blockers": [{"reason": "native_precommit_exception", "details": "TimeoutError"}],
    }
    assert classify_precommit_gate(gate) == "infra_timeout"


def test_classify_precommit_gate_real_regression_still_regression():
    """Regression guard: a genuine strategy regression (e.g.
    aggregate_native_regression) stays ``regression`` (candidate rework)."""
    gate = {
        "passed": False,
        "blockers": [
            {"reason": "aggregate_native_regression", "details": "..."},
        ],
    }
    assert classify_precommit_gate(gate) == "regression"


def test_classify_precommit_gate_mixed_blockers_is_regression():
    """A mix of infra + regression blockers is a regression (the all() rule):
    a real regression is not masked by infra noise."""
    gate = {
        "passed": False,
        "blockers": [
            {"reason": "native_incomplete_match"},
            {"reason": "aggregate_native_regression"},
        ],
    }
    assert classify_precommit_gate(gate) == "regression"


def test_classify_precommit_gate_passed_is_passed():
    """A passed gate classifies as passed regardless of blockers."""
    gate = {"passed": True, "blockers": []}
    assert classify_precommit_gate(gate) == "passed"


def test_classify_precommit_gate_no_blockers_failed_unknown():
    """A failed gate with no blockers is ambiguous (failed_unknown)."""
    assert classify_precommit_gate({"passed": False, "blockers": []}) == "failed_unknown"
    assert classify_precommit_gate(None) == "not_run"
