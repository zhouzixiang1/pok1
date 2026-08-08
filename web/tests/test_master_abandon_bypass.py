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



def test_invalid_literature_probe_branch_uses_canonical_abandon_not_directive():
    """Static regression for the v106 loop: when the literature-probe receipt is
    present but invalid (terminal, non-repairable), run_master_impl must call
    _abandon_master_generation directly — NOT return a LITERATURE_PROBE_RECEIPT_INVALID
    JSON directive. The directive path loops forever because the MCP
    abandon_generation tool is blocked by the direction_audited route guard
    (allowed_tools is run_literature_probe/run_master only), so the abandon
    never executes and no retry-budget breaker fires.

    This test guards the source against regression to the directive form.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "core" / "tool_planning_master_dispatch.py").read_text(encoding="utf-8")
    # The invalid-receipt (_probe_present) branch must drive the canonical
    # abandon, not a JSON tool-result directive.
    assert "_tp._abandon_master_generation(" in src, (
        "run_master_impl must call _abandon_master_generation for the terminal "
        "invalid-literature-probe case"
    )
    # The LITERATURE_PROBE_RECEIPT_INVALID error must NOT be returned as a
    # JSON directive next_tool=abandon_generation (the looping form).
    import re
    directive_matches = re.findall(
        r'LITERATURE_PROBE_RECEIPT_INVALID', src)
    # It may still appear in the abandon call's error= kwarg, but must NOT be
    # inside a _json_tool_result({"next_tool": "abandon_generation", ...}).
    assert '"next_tool": "abandon_generation"' not in src or src.count(
        '_abandon_master_generation'
    ) >= 1, "abandon directive form must be replaced by canonical abandon call"
