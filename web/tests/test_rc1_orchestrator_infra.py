"""Tests for the orchestrator infra-error classifier (rc1_orch).

The `_is_cycle_infra_error` helper is the centralized, testable seam for deciding
whether an orchestrator exception is an LLM-infra failure (short -0.5 backoff) or
a real business/auth failure (longer backoff). We do NOT integration-test the
heavy `_run_one_cycle`; the helper is sufficient for this fix.
"""

from orchestrator import (
    _is_cycle_infra_error,
    _is_shutdown_cancel_error,
    _is_crossover_incompatible_result,
    _is_crossover_llm_exhausted_result,
)
from claude_agent_sdk import ProcessError, CLINotFoundError, ClaudeSDKError
import inspect
import orchestrator


def test_processerror_exit143_is_infra():
    assert _is_cycle_infra_error(ProcessError("command failed with exit code 143")) is True


def test_processerror_exit143_during_shutdown_is_not_infra():
    assert _is_cycle_infra_error(
        ProcessError("command failed with exit code 143"),
        is_shutting_down=True,
    ) is False


def test_claude_sdk_error_signature_is_infra():
    assert _is_cycle_infra_error(
        ClaudeSDKError("Missing required field in assistant message: signature")
    ) is True


def test_generic_business_failure_is_not_infra():
    assert _is_cycle_infra_error(Exception("some real business failure")) is False


def test_valueerror_exit143_keyword_is_infra():
    # Keyword fallback catches SDK-wrapped ProcessError/exit-143 even when the
    # exception is not an SDK type.
    assert _is_cycle_infra_error(ValueError("exit code 143 in wrapper")) is True


# ---------------------------------------------------------------------------
# Crossover route result-token detectors (B1, 2026-07-09).
#
# A crossover that exhausts its LLM retries (repeated idle timeouts / SDK
# stream stalls) must be distinguishable from a compatibility rejection and
# from a bare {"success": False}. These pure detectors are the testable seam
# that lets the deterministic router abandon the generation instead of
# re-routing to run_crossover forever (the v126 deadlock).
# ---------------------------------------------------------------------------
def test_crossover_incompatible_detector_matches_token():
    assert _is_crossover_incompatible_result({"error": "CROSSOVER_INCOMPATIBLE"}) is True


def test_crossover_llm_exhausted_detector_matches_token():
    assert _is_crossover_llm_exhausted_result({"error": "CROSSOVER_LLM_EXHAUSTED"}) is True


def test_crossover_detectors_distinguish_each_other_and_bare_failure():
    # Exhausted must NOT match the incompatible detector (distinct abandon reason).
    assert _is_crossover_incompatible_result({"error": "CROSSOVER_LLM_EXHAUSTED"}) is False
    assert _is_crossover_llm_exhausted_result({"error": "CROSSOVER_INCOMPATIBLE"}) is False
    # A bare {"success": False} with no error token must match neither — that is
    # exactly the pre-fix shape that caused the infinite re-route deadlock.
    assert _is_crossover_incompatible_result({"success": False}) is False
    assert _is_crossover_llm_exhausted_result({"success": False}) is False


def test_crossover_detectors_guard_non_dict_input():
    assert _is_crossover_incompatible_result(None) is False
    assert _is_crossover_llm_exhausted_result(None) is False
    assert _is_crossover_incompatible_result("CROSSOVER_LLM_EXHAUSTED") is False
    assert _is_crossover_llm_exhausted_result("CROSSOVER_INCOMPATIBLE") is False


def test_valueerror_exit143_keyword_during_shutdown_is_cancel():
    error = ValueError("exit code 143 in wrapper")
    assert _is_shutdown_cancel_error(error) is True
    assert _is_cycle_infra_error(error, is_shutting_down=True) is False


def test_keyerror_valueerror_business_is_not_infra():
    assert _is_cycle_infra_error(KeyError("missing config key")) is False
    assert _is_cycle_infra_error(ValueError("bad user input")) is False


def test_daemon_dead_prepare_log_escalates_after_repeated_failures():
    source = inspect.getsource(orchestrator.orchestrator_loop)

    assert 'daemon_dead_level = "error" if consecutive_prep_fails >= 3 else "warn"' in source
