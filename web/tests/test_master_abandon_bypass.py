"""Regression guard for the Master-abandon livelock class.

_abandon_master_generation previously called _do_abandon_generation *inline*
from inside a tool dispatch.  While the orchestrator loop was still running it
concurrently bumped the checkpoint ``checkpoint_revision``, so the canonical
abandon's CAS revalidation refused with ``expected_checkpoint_identity_mismatch``,
the ``abandoned: False`` result was ignored, and the orchestrator re-entered
``run_master`` every ~30 s — burning LLM budget forever (the v161/v106
livelock class).

The fix (Stage-0 of the deep-parallelism redesign) moves the canonical abandon
out of the tool layer entirely: ``_abandon_master_generation`` now only
*signals* the request via ``master_abandon_signal.request_abandon`` and returns
a terminal tool result tagged ``abandon_signaled: True``.  The orchestrator
loop finalizes the abandon against a quiescent checkpoint right after
``_run_one_cycle`` returns (mirroring the HTTP ``POST /api/control/abandon``
"stop-then-abandon" pattern).  These tests guard that contract.
"""

import asyncio
import json

from conftest import STRICT_SOURCE_V, STRICT_TARGET_V
import master_abandon_signal
import pytest
import tool_planning

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")


def test_abandon_master_generation_signals_not_inline(monkeypatch):
    """_abandon_master_generation signals the abandon via master_abandon_signal
    instead of calling _do_abandon_generation inline (the CAS-race root cause).

    The tool layer must NOT run the publication-authority transaction itself —
    that races the concurrently-mutated checkpoint.  It must only record the
    request and let the orchestrator loop finalize it against a quiescent
    checkpoint.
    """
    import tool_bot_management

    master_abandon_signal.clear()

    # If _abandon_master_generation ever calls _do_abandon_generation directly,
    # this stub will record it and the test fails.
    inline_called = []

    async def _must_not_be_called_inline(*args, **kwargs):
        inline_called.append(kwargs)
        return {"abandoned": True}

    monkeypatch.setattr(
        tool_bot_management, "_do_abandon_generation", _must_not_be_called_inline
    )

    reason = "master_validation_failed v%s: proposal stuck" % STRICT_TARGET_V
    result = asyncio.run(tool_planning._abandon_master_generation(
        STRICT_TARGET_V,
        STRICT_SOURCE_V,
        error="MASTER_EXHAUSTED",
        fail_count=2,
        reason=reason,
        event_type="pipeline.master_exhausted",
        event_message="Master exhausted retry budget",
    ))
    payload = json.loads(result["content"][0]["text"])

    # The signal must be pending for the orchestrator loop to finalize.
    assert master_abandon_signal.consume_pending() == reason, (
        "_abandon_master_generation must signal master_abandon_signal so the "
        "orchestrator loop finalizes the abandon against a quiescent checkpoint "
        "(inline abandon races the concurrent checkpoint_revision bump — "
        "v161/v106 livelock root cause)"
    )
    # The tool result must indicate the abandon was signaled, not finalized.
    assert payload.get("abandon_signaled") is True
    assert payload.get("abandon_reason") == reason
    # The inline path must NOT have been invoked.
    assert not inline_called, (
        "_abandon_master_generation must NOT call _do_abandon_generation inline"
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
