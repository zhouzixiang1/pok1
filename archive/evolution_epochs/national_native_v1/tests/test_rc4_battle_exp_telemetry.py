"""Archived RC4 telemetry tests for the retired background bridge.

The two best-effort except blocks in battle_experience.py now classify LLM
errors (infra vs business) and emit a typed system event. The classification
helper is the only testable seam (the thread itself is heavy and not
exercised here).
"""

import asyncio
import logging

from claude_agent_sdk import ClaudeSDKError

from core.battle_experience import (
    _classify_llm_error,
    _llm_error_event_severity,
    _llm_error_event_type,
    _log_llm_failure,
)


def test_classify_sdk_error_is_infra():
    assert _classify_llm_error(ClaudeSDKError("boom")) == "infra"


def test_classify_timeout_is_infra():
    assert _classify_llm_error(asyncio.TimeoutError()) == "infra"


def test_classify_value_error_is_business():
    assert _classify_llm_error(ValueError("bad json")) == "business"


def test_classify_key_error_is_business():
    assert _classify_llm_error(KeyError("x")) == "business"


def test_classify_success_error_result_is_low_priority():
    exc = Exception("Claude Code returned an error result: success")

    assert _classify_llm_error(exc) == "sdk_success_result"
    assert _llm_error_event_type("sdk_success_result") == "battle_exp.sdk_success_result"
    assert _llm_error_event_severity("sdk_success_result") == "info"


def test_success_error_result_logs_at_info(caplog):
    with caplog.at_level(logging.INFO, logger="pok.battle_exp"):
        _log_llm_failure(
            "Sync LLM call failed (%s): %s",
            "sdk_success_result",
            Exception("Claude Code returned an error result: success"),
        )

    records = [r for r in caplog.records if r.name == "pok.battle_exp"]
    assert records
    assert records[-1].levelno == logging.INFO
    assert "sdk_success_result" in records[-1].getMessage()
