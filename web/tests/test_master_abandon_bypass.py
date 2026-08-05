"""Regression guard for the Master-abandon rate-limit refusal loop.

_abandon_master_generation previously called _do_abandon_generation without
_bypass_rate_limit=True, so the 60-second abandon rate-limit gate refused the
abandon whenever one was attempted in the last minute. The orchestrator then
re-invoked run_master and the Master burned another full analysis+audit
cycle (6+ LLM role calls) on a doomed direction — the gen-64
proposal_mechanism_foreign_targets loop. The Master-exhaustion path is a
system-owned fail-closed path that has already proved the immutable candidate
cannot be retried, so it must bypass the cooldown (the cooldown exists to
protect against LLM-driven abandon spam, which cannot happen from this
deterministic tool path).
"""

import asyncio
import json

from conftest import STRICT_SOURCE_V, STRICT_TARGET_V
import pytest
import tool_planning

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")


def test_abandon_master_generation_bypasses_rate_limit(monkeypatch):
    """_abandon_master_generation forwards _bypass_rate_limit=True so the
    deterministic Master-exhaustion abandon is not refused by the 60s cooldown."""
    import evolution_core
    import tool_bot_management

    # Stub the checkpoint read so the lazy import path inside
    # _abandon_master_generation reaches _do_abandon_generation instead of
    # raising on a missing checkpoint.  expected_abandon_identity is stubbed
    # to return no identity kwargs so the call focuses on the bypass kwarg.
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: {"stub": True})
    monkeypatch.setattr(
        tool_bot_management,
        "expected_abandon_identity",
        lambda _ckpt: {},
    )

    captured = {}

    async def fake_do_abandon(reason, **kwargs):
        captured["reason"] = reason
        captured["bypass"] = kwargs.get("_bypass_rate_limit")
        return {"abandoned": True, "reason": reason}

    monkeypatch.setattr(tool_bot_management, "_do_abandon_generation", fake_do_abandon)

    result = asyncio.run(tool_planning._abandon_master_generation(
        STRICT_TARGET_V,
        STRICT_SOURCE_V,
        error="MASTER_EXHAUSTED",
        fail_count=2,
        reason="master_validation_failed v%s: proposal stuck" % STRICT_TARGET_V,
        event_type="pipeline.master_exhausted",
        event_message="Master exhausted retry budget",
    ))
    payload = json.loads(result["content"][0]["text"])
    assert payload["abandoned"] is True
    assert captured["bypass"] is True, (
        "_abandon_master_generation must pass _bypass_rate_limit=True so the "
        "deterministic Master-exhaustion abandon is not refused by the 60s "
        "rate-limit cooldown (gen-64 loop root cause)"
    )
