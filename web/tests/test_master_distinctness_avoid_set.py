"""Collision-retry avoid-set completeness (2026-09-13, v446 defect).

A collision-class retry (distinctness repair, or a schema repair whose pin
collides and is downgraded to an avoid instruction) used to carry an avoid
set naming ONLY the symbol its own first round collided with.  Symbols
claimed by other accepted directions — especially directions processed after
the colliding one — and pins registered by other schema repairs were invisible
to the retry prompt AND to the avoid check, so the single permitted retry
re-collided and the direction died (live v446: the counterfactual distinctness
retry picked ``policy.py:_size_conditioned_fold_rate`` already claimed by
compute_memory; ensemble fell to 2/3; the generation was abandoned).

The fix finalizes the avoid set at retry-dispatch time (after the WHOLE first
round) from one shared source — every accepted direction's change_symbol,
every registered ``retry_pinned_symbols`` value, and the direction's own
first-round symbol — renders that exact list into the retry prompt via one
``schema_retry_avoid_claimed_symbol.<symbol>`` token per symbol, and the
attempt-2 hard validation rejects exactly that list.

Reuse the ensemble fixtures/harness from the sibling Master tests
(cross-test imports are the established pattern here).
"""

import json
import logging

import pytest

from test_master_proposal_ensemble import (
    _proposal,
    _raw_proposal,
)
from test_master_schema_retry_unpin import (
    _BOT_STATS_REF,
    _H2H_REF,
    _Harness,
    _measurement_missing,
    _retargeted_proposal,
    _strip_deadline_literals,
)

_M = "policy.py:_choose_intent_mechanism"
_C = "policy.py:_choose_intent_compute_memory"
_CF = "policy.py:_choose_intent_counterfactual"
_PIN = "policy.py:_choose_intent"


def _token(symbol: str) -> str:
    return f"schema_retry_avoid_claimed_symbol.{symbol}"


def _invalid_with_symbol(direction: str, leaf: str) -> str:
    """A schema-invalid attempt-1 whose change_symbol still resolves (so the
    schema retry gets pinned to ``leaf``)."""

    payload = json.loads(_raw_proposal(direction, snapshot=True))
    payload["source_symbols"] = [
        "policy.py:get_baseline_decision",
        f"policy.py:{leaf}",
    ]
    payload["change_symbol"] = f"policy.py:{leaf}"
    payload["reachable_chain"] = [
        "policy.py:get_baseline_decision",
        f"policy.py:{leaf}",
    ]
    payload["evidence_refs"] = [
        "source:policy.py:get_baseline_decision",
        f"source:policy.py:{leaf}",
        _H2H_REF,
        _BOT_STATS_REF,
    ]
    return json.dumps(_measurement_missing(payload))


class _AvoidHarness(_Harness):
    """Ensemble harness whose retry inspectors are repair-kind aware."""

    def retry_inputs(self, direction: str, kind: str = "schema") -> list[dict]:
        return [
            inputs
            for inputs in self.rendered_inputs
            if inputs.get("direction") == direction
            and inputs.get("repair_kind") == kind
        ]

    def retry_prompt(self, direction: str, kind: str = "schema") -> str:
        label = "DISTINCTNESS" if kind == "distinctness" else "SCHEMA"
        role = f"MASTER PROPOSAL {direction} {label} RETRY"
        return next(prompt for name, prompt in self.calls if name == role)


# ═══════════════════════════════════════════════════════════════════════════
# (a) The distinctness retry's avoid set covers every unavailable symbol,
# including symbols claimed by directions accepted AFTER the colliding
# direction's own construction point (the exact v446 shape).
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_distinctness_retry_avoids_all_claims_including_later_directions(
    monkeypatch, tmp_path
):
    # mechanism (processed first) accepts _M; counterfactual collides on _M
    # (distinctness repair constructed BEFORE compute_memory runs);
    # compute_memory (processed last) accepts _C.
    harness = _AvoidHarness(
        monkeypatch,
        tmp_path,
        attempt1={
            "mechanism": _proposal("mechanism", snapshot=True),
            "counterfactual": _retargeted_proposal("counterfactual", "_choose_intent_mechanism"),
            "compute_memory": _proposal("compute_memory", snapshot=True),
        },
        retry={"counterfactual": _proposal("counterfactual", snapshot=True)},
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    retry_inputs = harness.retry_inputs("counterfactual", "distinctness")
    assert len(retry_inputs) == 1
    hints = retry_inputs[0]["projection_hints"]
    # _M: the first-round collision (own symbol AND mechanism's claim).
    assert _token(_M) in hints
    # _C: claimed by compute_memory AFTER counterfactual's repair was
    # constructed — only dispatch-time finalization can see it.
    assert _token(_C) in hints
    # The free retry target is not part of the avoid set.
    assert _token(_CF) not in hints
    # The rendered retry prompt names the complete claimed list.
    retry_prompt = harness.retry_prompt("counterfactual", "distinctness")
    assert "MUST NOT be any of" in retry_prompt
    assert f"{_M}, {_C}" in retry_prompt
    assert "single permitted distinctness repair" in retry_prompt


@pytest.mark.asyncio
async def test_distinctness_avoid_set_includes_registered_pins(
    monkeypatch, tmp_path
):
    # mechanism's attempt-1 is schema-invalid -> schema retry PINNED to _M;
    # counterfactual accepts _C; compute_memory then collides on _C (it runs
    # last) -> compute_memory carries the distinctness repair.
    harness = _AvoidHarness(
        monkeypatch,
        tmp_path,
        attempt1={
            "mechanism": _invalid_with_symbol("mechanism", "_choose_intent_mechanism"),
            "counterfactual": _retargeted_proposal(
                "counterfactual", "_choose_intent_compute_memory"
            ),
            "compute_memory": _proposal("compute_memory", snapshot=True),
        },
        retry={
            "mechanism": _proposal("mechanism", snapshot=True),
            # The stock fixture reuses _C, which is correctly unavailable; the
            # retry must retarget to a free symbol (_choose_intent).
            "compute_memory": _retargeted_proposal("compute_memory", "_choose_intent"),
        },
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    retry_inputs = harness.retry_inputs("compute_memory", "distinctness")
    assert len(retry_inputs) == 1
    hints = retry_inputs[0]["projection_hints"]
    # The other direction's REGISTERED PIN is unavailable even though it is
    # not (yet) an accepted claim.
    assert _token(_M) in hints
    # The colliding claim and the direction's own first-round symbol.
    assert _token(_C) in hints
    # counterfactual accepted in round 1 — it gets no repair at all.
    assert harness.retry_inputs("counterfactual", "distinctness") == []
    # The clean-pin schema retry path is unchanged alongside the fix.
    mechanism_retry = harness.retry_inputs("mechanism", "schema")
    assert (
        "schema_retry_keep_change_symbol.policy.py:_choose_intent_mechanism"
        in mechanism_retry[0]["projection_hints"]
    )
    assert not any(
        hint.startswith("schema_retry_avoid_claimed_symbol")
        for hint in mechanism_retry[0]["projection_hints"]
    )


# ═══════════════════════════════════════════════════════════════════════════
# (b) Same-source hard validation: a retry that lands on ANY symbol of the
# finalized avoid set — including another direction's registered pin, which
# the old single-symbol avoid set and the accepted-claims check both missed —
# is rejected by the avoid check itself.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_distinctness_retry_reusing_registered_pin_is_hard_rejected(
    monkeypatch, tmp_path, caplog
):
    # mechanism accepts _M; counterfactual collides on _M; compute_memory is
    # schema-invalid and PINNED to _C.  The distinctness retry (processed
    # before compute_memory's retry) deliberately picks the PIN symbol _C.
    harness = _AvoidHarness(
        monkeypatch,
        tmp_path,
        attempt1={
            "mechanism": _proposal("mechanism", snapshot=True),
            "counterfactual": _retargeted_proposal(
                "counterfactual", "_choose_intent_mechanism"
            ),
            "compute_memory": _invalid_with_symbol(
                "compute_memory", "_choose_intent_compute_memory"
            ),
        },
        retry={
            "counterfactual": _retargeted_proposal(
                "counterfactual", "_choose_intent_compute_memory"
            ),
            "compute_memory": _proposal("compute_memory", snapshot=True),
        },
    )
    with caplog.at_level(logging.WARNING, logger="pok.master"):
        packet = await harness.run(tmp_path)

    assert packet["valid"] is False
    assert packet["reason"].endswith("got_2")
    # Rejected by the AVOID check reading the finalized set (not merely by
    # the accepted-claims check): pre-fix this retry was admitted and the
    # pinned direction died instead.
    assert (
        "distinctness retry reused the conflicting symbol "
        "policy.py:_choose_intent_compute_memory" in caplog.text
    )
    # The pinned direction's non-collision schema retry still ran and kept
    # its pin (it is the survivor of the collision).
    pinned_retry = harness.retry_inputs("compute_memory", "schema")
    assert len(pinned_retry) == 1
    assert (
        "schema_retry_keep_change_symbol.policy.py:_choose_intent_compute_memory"
        in pinned_retry[0]["projection_hints"]
    )


@pytest.mark.asyncio
async def test_distinctness_retry_reusing_accepted_claim_is_hard_rejected(
    monkeypatch, tmp_path, caplog
):
    harness = _AvoidHarness(
        monkeypatch,
        tmp_path,
        attempt1={
            "mechanism": _proposal("mechanism", snapshot=True),
            "counterfactual": _retargeted_proposal(
                "counterfactual", "_choose_intent_mechanism"
            ),
            "compute_memory": _proposal("compute_memory", snapshot=True),
        },
        retry={
            # Picks compute_memory's ACCEPTED claim (the v446 collision).
            "counterfactual": _retargeted_proposal(
                "counterfactual", "_choose_intent_compute_memory"
            ),
        },
    )
    with caplog.at_level(logging.WARNING, logger="pok.master"):
        packet = await harness.run(tmp_path)

    assert packet["valid"] is False
    assert packet["reason"].endswith("got_2")
    assert (
        "distinctness retry reused the conflicting symbol "
        "policy.py:_choose_intent_compute_memory" in caplog.text
    )


# ═══════════════════════════════════════════════════════════════════════════
# (d) The schema pin-collision downgrade is a collision-class repair too: its
# avoid set and rendered guidance cover every unavailable symbol, not only
# the downgraded pin.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_pin_collision_schema_retry_avoid_set_covers_all_claims(
    monkeypatch, tmp_path
):
    harness = _AvoidHarness(
        monkeypatch,
        tmp_path,
        attempt1={
            # mechanism accepts _PIN; counterfactual's schema-invalid
            # attempt-1 extracts the SAME symbol -> pin collides.
            "mechanism": _retargeted_proposal("mechanism", "_choose_intent"),
            "counterfactual": _invalid_with_symbol("counterfactual", "_choose_intent"),
            "compute_memory": _proposal("compute_memory", snapshot=True),
        },
        retry={"counterfactual": _proposal("counterfactual", snapshot=True)},
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    retry_inputs = harness.retry_inputs("counterfactual", "schema")
    assert len(retry_inputs) == 1
    hints = retry_inputs[0]["projection_hints"]
    assert _token(_PIN) in hints
    # compute_memory's accepted claim is named too, not only the pin.
    assert _token(_C) in hints
    assert not any(
        hint.startswith("schema_retry_keep_change_symbol")
        for hint in hints
    )
    assert not any(
        hint.startswith("schema_retry_target_infeasible_unpinned")
        for hint in hints
    )
    retry_prompt = harness.retry_prompt("counterfactual", "schema")
    assert "already claimed" in retry_prompt
    assert f"{_PIN}, {_C}" in retry_prompt


def test_guidance_renders_every_claimed_symbol_not_only_the_first():
    import agent_master

    guidance = agent_master._proposal_schema_repair_guidance(
        (
            _token("policy.py:_bluff_allowed"),
            _token("policy.py:_size_conditioned_fold_rate"),
        ),
        require_snapshot_evidence=False,
    )
    assert "policy.py:_bluff_allowed" in guidance
    assert "policy.py:_size_conditioned_fold_rate" in guidance
    assert "already claimed" in guidance


# ═══════════════════════════════════════════════════════════════════════════
# (c) Non-collision paths keep their construction-time behavior: the clean
# ensemble issues no retry at all, and the target-infeasible unpin keeps its
# single-symbol avoid set with no claimed-symbol tokens injected.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_clean_ensemble_issues_no_repair_and_no_avoid_tokens(
    monkeypatch, tmp_path
):
    harness = _AvoidHarness(
        monkeypatch,
        tmp_path,
        attempt1={},
        retry={},
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    assert not any("RETRY" in name for name, _prompt in harness.calls)
    # Scout renderer inputs only (critic inputs carry a different contract).
    scout_inputs = [
        inputs for inputs in harness.rendered_inputs if "repair_kind" in inputs
    ]
    assert len(scout_inputs) == 3
    assert all(inputs.get("repair_kind") == "" for inputs in scout_inputs)
    assert not any(
        hint.startswith("schema_retry_avoid_claimed_symbol")
        for inputs in harness.rendered_inputs
        for hint in inputs.get("projection_hints") or ()
    )


@pytest.mark.asyncio
async def test_infeasible_unpin_retry_keeps_own_symbol_only_avoid(
    monkeypatch, tmp_path
):
    attempt1 = json.dumps(_strip_deadline_literals(
        json.loads(_raw_proposal("mechanism", snapshot=True))
    ))
    harness = _AvoidHarness(
        monkeypatch,
        tmp_path,
        attempt1={"mechanism": attempt1},
        retry={"mechanism": _retargeted_proposal("mechanism", "_choose_intent")},
    )
    packet = await harness.run(tmp_path)

    # Byte-for-byte the pre-fix unpin behavior: no claimed-symbol tokens are
    # injected into a NON-collision repair, and the switched symbol survives.
    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    retry_inputs = harness.retry_inputs("mechanism", "schema")
    assert len(retry_inputs) == 1
    hints = retry_inputs[0]["projection_hints"]
    assert (
        "schema_retry_target_infeasible_unpinned.policy.py:_choose_intent_mechanism"
        in hints
    )
    assert not any(
        hint.startswith("schema_retry_avoid_claimed_symbol")
        for hint in hints
    )
    retry_prompt = harness.retry_prompt("mechanism", "schema")
    assert "MUST NOT be any of" not in retry_prompt
    symbols = {p["change_symbol"] for p in packet["ordered_proposals"]}
    assert "policy.py:_choose_intent" in symbols
