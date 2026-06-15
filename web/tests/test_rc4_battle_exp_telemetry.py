"""RC4: typed battle_exp telemetry — tests for the _classify_llm_error seam.

The two best-effort except blocks in battle_experience.py now classify LLM
errors (infra vs business) and emit a typed system event. The classification
helper is the only testable seam (the thread itself is heavy and not
exercised here).
"""

import asyncio

from claude_agent_sdk import ClaudeSDKError

from core.battle_experience import _classify_llm_error


def test_classify_sdk_error_is_infra():
    assert _classify_llm_error(ClaudeSDKError("boom")) == "infra"


def test_classify_timeout_is_infra():
    assert _classify_llm_error(asyncio.TimeoutError()) == "infra"


def test_classify_value_error_is_business():
    assert _classify_llm_error(ValueError("bad json")) == "business"


def test_classify_key_error_is_business():
    assert _classify_llm_error(KeyError("x")) == "business"
