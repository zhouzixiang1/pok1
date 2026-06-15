"""Tests for the orchestrator infra-error classifier (rc1_orch).

The `_is_cycle_infra_error` helper is the centralized, testable seam for deciding
whether an orchestrator exception is an LLM-infra failure (short -0.5 backoff) or
a real business/auth failure (longer backoff). We do NOT integration-test the
heavy `_run_one_cycle`; the helper is sufficient for this fix.
"""

from core.orchestrator import _is_cycle_infra_error
from claude_agent_sdk import ProcessError, CLINotFoundError, ClaudeSDKError


def test_processerror_exit143_is_infra():
    assert _is_cycle_infra_error(ProcessError("command failed with exit code 143")) is True


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


def test_keyerror_valueerror_business_is_not_infra():
    assert _is_cycle_infra_error(KeyError("missing config key")) is False
    assert _is_cycle_infra_error(ValueError("bad user input")) is False
