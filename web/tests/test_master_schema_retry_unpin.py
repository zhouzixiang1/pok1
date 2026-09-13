"""Schema-retry unpinning by failure class (2026-09-13).

The Master ensemble pins every schema retry to its original change_symbol
(``schema_retry_keep_change_symbol.<symbol>``) unless the pin collides with
another direction's claim (``schema_retry_avoid_claimed_symbol.<symbol>``).
When the attempt-1 rejection itself proves the pinned TARGET's own shape is
infeasible for the proposal contract (the three mechanism-target binding
classes), a pinned retry can only reproduce the same deterministic rejection
and burns the single permitted repair — observed six-in-a-row in live logs.

These tests pin down the third mode:
``schema_retry_target_infeasible_unpinned.<symbol>`` — the pin is released,
the retry MUST pick a new (valid, unclaimed) symbol, and the pre-existing
keep / avoid modes stay byte-for-byte unchanged.

Reuse the ensemble fixtures from test_master_proposal_ensemble (cross-test
imports are an established pattern here, e.g. test_logic_replay_spotlight_native).
"""

import json
import re

import pytest

from bot_namespace import bot_name
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V
from test_master_proposal_ensemble import (
    _UI,
    _critic_output,
    _proposal,
    _raw_proposal,
    _write_source,
    _write_strength_snapshot,
)

NEXT_V = 149

_H2H_REF = (
    "snapshot:head_to_head.json#/"
    f"{bot_name(STRICT_TARGET_V)} vs {bot_name(STRICT_TARGET_V + 1)}"
)
_BOT_STATS_REF = f"snapshot:bot_stats.json#/{bot_name(STRICT_SOURCE_V)}"

# The three target-shape infeasibility classes that must release the pin,
# exercised in the exact suffixed forms the runtime emits.
_PREFIX_VARIANTS = (
    "proposal_mechanism_target_missing_from_executable_fields:"
    "deadline:expected_diff,intervention,structural_change",
    "proposal_mechanism_qualified_target_identifier_continuation:"
    "opponent.rates_fold",
    "proposal_mechanism_root_scoped_unknown_leaf:"
    "opponent.rates:adaptation_weight",
)

# Sibling codes that must NOT release the pin.  The bare
# ``proposal_mechanism_target_invalid`` form (agent_master_validation.py:1690)
# is the scalar mechanism_target spelling check — unrelated to the pinned
# change_symbol's shape — so its repair is re-spelling mechanism_target in
# place, keeping the pin.  A mismatch is repairable in place for the same
# reason.
_KEEP_PIN_CODES = (
    "proposal_mechanism_target_invalid",
    "proposal_mechanism_target_mismatch:expected=deadline:actual=deck.shuffle",
)

_UNPIN_TOKEN = "schema_retry_target_infeasible_unpinned.policy.py:_choose_intent_mechanism"
_KEEP_TOKEN = "schema_retry_keep_change_symbol.policy.py:_choose_intent_mechanism"


def _strip_deadline_literals(payload: dict) -> dict:
    """Trigger the REAL missing-from-executable-fields rejection.

    Removes the required ``deadline`` literal from the three executable
    fields (structural_change / expected_diff / falsifier.intervention) while
    every other falsifier/mechanism check stays valid, so the projection
    hints carry ``proposal_mechanism_target_missing_from_executable_fields``.
    """

    payload["structural_change"] = payload["structural_change"].replace(
        "deadline-bounded", "time-boxed"
    )
    payload["expected_diff"] = payload["expected_diff"].replace(
        "before the deadline", "before the time budget expires"
    )
    payload["falsifier"]["intervention"] = (
        "Run only the proposed mechanism with a changed time budget on that "
        "identical canonical state."
    )
    return payload


def _measurement_missing(payload: dict) -> dict:
    """A plain schema failure that carries NO infeasibility class."""

    payload["measurement_plan"] = payload.pop("measurement")
    return payload


def _marker_invalid_proposal(direction: str) -> str:
    """Attempt-1 output the hints monkeypatch recognizes by its marker."""

    payload = json.loads(_raw_proposal(direction, snapshot=True))
    payload["targeted_failure"] = (
        "UNPIN-MARKER identifies one repeated reachable decision failure."
    )
    return json.dumps(_measurement_missing(payload))


def _retargeted_proposal(direction: str, new_leaf: str) -> str:
    """A valid retry proposal whose change_symbol switched to ``new_leaf``."""

    payload = json.loads(_raw_proposal(direction, snapshot=True))
    payload["source_symbols"] = [
        "policy.py:get_baseline_decision",
        f"policy.py:{new_leaf}",
    ]
    payload["change_symbol"] = f"policy.py:{new_leaf}"
    payload["reachable_chain"] = [
        "policy.py:get_baseline_decision",
        f"policy.py:{new_leaf}",
    ]
    payload["evidence_refs"] = [
        "source:policy.py:get_baseline_decision",
        f"source:policy.py:{new_leaf}",
        _H2H_REF,
        _BOT_STATS_REF,
    ]
    return json.dumps(payload)


class _Harness:
    """Ensemble harness with per-direction attempt-1 / retry outputs."""

    def __init__(self, monkeypatch, tmp_path, attempt1: dict, retry: dict):
        import agent_master

        self.agent_master = agent_master
        source_dir = tmp_path / "source"
        snapshot_dir = tmp_path / "snapshot"
        _write_source(source_dir)
        _write_strength_snapshot(snapshot_dir)
        self.source_dir = source_dir
        self.snapshot_dir = snapshot_dir
        self.calls = []  # (role_name, prompt)
        self.rendered_inputs = []
        # The role contract pins the producer renderer to its registered
        # source file, so the renderer itself must NOT be wrapped.  Capture
        # the retry's raw projection_hints at the render_llm_prompt boundary
        # (propose() imports it from llm_query at call time) and delegate to
        # the real renderer untouched.
        import llm_query

        real_render = llm_query.render_llm_prompt

        def capturing_render(role_name, *, producer, renderer_inputs, **kwargs):
            self.rendered_inputs.append(dict(renderer_inputs))
            return real_render(
                role_name,
                producer=producer,
                renderer_inputs=renderer_inputs,
                **kwargs,
            )

        async def fake_query(prompt, _ctx, _ui, role_name, *_args, **_kwargs):
            self.calls.append((role_name, prompt))
            if role_name.startswith("MASTER PROPOSAL CRITIC"):
                ids = list(dict.fromkeys(re.findall(
                    r'"proposal_id":"([0-9a-f]{16})"', prompt
                )))
                return _critic_output(agent_master, ids), 0.0, {}
            direction = next(
                name
                for name in ("mechanism", "counterfactual", "compute_memory")
                if name in role_name
            )
            if "RETRY" in role_name:
                output = retry[direction]
            else:
                output = attempt1.get(direction)
                if output is None:
                    output = _proposal(direction, snapshot=True)
            return output, 0.0, {}

        monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: source_dir)
        monkeypatch.setattr(agent_master, "run_claude_query", fake_query)
        monkeypatch.setattr(llm_query, "render_llm_prompt", capturing_render)

    def retry_inputs(self, direction: str) -> list[dict]:
        return [
            inputs
            for inputs in self.rendered_inputs
            if inputs.get("direction") == direction
            and inputs.get("repair_kind") == "schema"
        ]

    def retry_prompt(self, direction: str) -> str:
        role = f"MASTER PROPOSAL {direction} SCHEMA RETRY"
        return next(
            prompt for name, prompt in self.calls if name == role
        )

    async def run(self, tmp_path):
        return json.loads(await self.agent_master._run_master_proposal_ensemble(
            "frozen planning context",
            source_v=STRICT_TARGET_V,
            next_v=NEXT_V,
            ui=_UI(),
            log_dir=tmp_path,
            allowed_evidence_snapshot_dir=str(self.snapshot_dir),
        ))


# ═══════════════════════════════════════════════════════════════════════════
# Prompt renderer: the new token must disclose its constraint, and the
# pre-existing keep/avoid branches must stay unchanged.
# ═══════════════════════════════════════════════════════════════════════════

def test_guidance_renders_target_infeasible_unpinned_token():
    import agent_master

    guidance = agent_master._proposal_schema_repair_guidance(
        (_UNPIN_TOKEN,),
        require_snapshot_evidence=False,
    )
    assert "policy.py:_choose_intent_mechanism" in guidance
    assert "structurally infeasible" in guidance
    assert "NEW change_symbol" in guidance
    assert "claimed by another direction" in guidance


def test_guidance_keep_and_avoid_branches_are_unchanged():
    import agent_master

    keep = agent_master._proposal_schema_repair_guidance(
        ("schema_retry_keep_change_symbol.policy.py:_bluff_allowed",),
        require_snapshot_evidence=False,
    )
    assert "must not switch" in keep
    assert "structurally infeasible" not in keep

    avoid = agent_master._proposal_schema_repair_guidance(
        ("schema_retry_avoid_claimed_symbol.policy.py:_bluff_allowed",),
        require_snapshot_evidence=False,
    )
    assert "already claimed" in avoid
    assert "structurally infeasible" not in avoid
    assert "must not switch" not in avoid


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end ensemble behavior.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_infeasible_target_first_round_releases_pin_and_accepts_switch(
    monkeypatch, tmp_path
):
    """A real missing-from-executable-fields attempt-1 rejection unpins the
    retry; the switched-symbol retry is accepted instead of dropped."""

    import agent_master

    attempt1 = json.dumps(_strip_deadline_literals(
        json.loads(_raw_proposal("mechanism", snapshot=True))
    ))
    harness = _Harness(
        monkeypatch,
        tmp_path,
        attempt1={"mechanism": attempt1},
        retry={"mechanism": _retargeted_proposal("mechanism", "_choose_intent")},
    )

    # Sanity: the real projection hints for THIS direction's attempt-1 output
    # carry the infeasibility class (no monkeypatching in this test).
    graph, _digest = agent_master._source_symbol_graph(harness.source_dir)
    hints = agent_master._master_proposal_projection_hints(
        attempt1,
        source_graph=graph,
        snapshot_dir=harness.snapshot_dir,
        national_policy_only=True,
        require_snapshot_evidence=True,
        evidence_mode="frozen_strength_snapshot",
    )
    assert any(
        hint.startswith("proposal_mechanism_target_missing_from_executable_fields")
        for hint in hints
    )

    packet = await harness.run(tmp_path)

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    retry_inputs = harness.retry_inputs("mechanism")
    assert len(retry_inputs) == 1
    assert _UNPIN_TOKEN in retry_inputs[0]["projection_hints"]
    assert not any(
        hint.startswith("schema_retry_keep_change_symbol")
        for hint in retry_inputs[0]["projection_hints"]
    )
    retry_prompt = harness.retry_prompt("mechanism")
    assert "structurally infeasible" in retry_prompt
    assert "policy.py:_choose_intent_mechanism" in retry_prompt
    assert "NEW change_symbol" in retry_prompt
    # The switched symbol survived into the accepted packet.
    symbols = {p["change_symbol"] for p in packet["ordered_proposals"]}
    assert "policy.py:_choose_intent" in symbols


@pytest.mark.asyncio
async def test_plain_schema_failure_still_pins_and_drops_symbol_switch(
    monkeypatch, tmp_path
):
    """Attempt-1 with only a non-infeasible error keeps the pin: a
    symbol-switching retry is still dropped (pre-existing behavior)."""

    attempt1 = json.dumps(_measurement_missing(
        json.loads(_raw_proposal("mechanism", snapshot=True))
    ))
    harness = _Harness(
        monkeypatch,
        tmp_path,
        attempt1={"mechanism": attempt1},
        retry={"mechanism": _retargeted_proposal("mechanism", "_choose_intent")},
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is False
    assert packet["reason"].endswith("got_2")
    retry_inputs = harness.retry_inputs("mechanism")
    assert _KEEP_TOKEN in retry_inputs[0]["projection_hints"]
    retry_prompt = harness.retry_prompt("mechanism")
    assert "must not switch" in retry_prompt
    assert "structurally infeasible" not in retry_prompt


@pytest.mark.asyncio
async def test_pin_collision_token_and_behavior_unchanged(monkeypatch, tmp_path):
    """pin_collides keeps its original token and avoid-downgrade exactly."""

    # mechanism's attempt-1 is VALID and claims policy.py:_choose_intent.
    claimed = _retargeted_proposal("mechanism", "_choose_intent")
    # counterfactual's attempt-1 is schema-invalid but extracts the SAME
    # symbol -> pin collides with the accepted claim.
    colliding = json.dumps(_measurement_missing(
        json.loads(_raw_proposal("counterfactual", snapshot=True))
    ))
    payload = json.loads(colliding)
    payload["source_symbols"] = [
        "policy.py:get_baseline_decision",
        "policy.py:_choose_intent",
    ]
    payload["change_symbol"] = "policy.py:_choose_intent"
    payload["reachable_chain"] = [
        "policy.py:get_baseline_decision",
        "policy.py:_choose_intent",
    ]
    payload["evidence_refs"] = [
        "source:policy.py:get_baseline_decision",
        "source:policy.py:_choose_intent",
        _H2H_REF,
        _BOT_STATS_REF,
    ]
    colliding = json.dumps(payload)
    harness = _Harness(
        monkeypatch,
        tmp_path,
        attempt1={"mechanism": claimed, "counterfactual": colliding},
        retry={"counterfactual": _proposal("counterfactual", snapshot=True)},
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    retry_inputs = harness.retry_inputs("counterfactual")
    assert len(retry_inputs) == 1
    assert (
        "schema_retry_avoid_claimed_symbol.policy.py:_choose_intent"
        in retry_inputs[0]["projection_hints"]
    )
    assert not any(
        hint.startswith("schema_retry_target_infeasible_unpinned")
        for hint in retry_inputs[0]["projection_hints"]
    )
    assert not any(
        hint.startswith("schema_retry_keep_change_symbol")
        for hint in retry_inputs[0]["projection_hints"]
    )
    retry_prompt = harness.retry_prompt("counterfactual")
    assert "already claimed" in retry_prompt


@pytest.mark.asyncio
async def test_unpinned_retry_reusing_original_symbol_is_rejected(
    monkeypatch, tmp_path
):
    """Unpinning is not a free pass: the infeasible original symbol sits in
    the avoid set, so reusing it (even in a fully valid proposal) rejects."""

    attempt1 = json.dumps(_strip_deadline_literals(
        json.loads(_raw_proposal("mechanism", snapshot=True))
    ))
    harness = _Harness(
        monkeypatch,
        tmp_path,
        attempt1={"mechanism": attempt1},
        # The stock fixture is fully valid and keeps _choose_intent_mechanism.
        retry={"mechanism": _proposal("mechanism", snapshot=True)},
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is False
    assert packet["reason"].endswith("got_2")
    assert _UNPIN_TOKEN in harness.retry_inputs("mechanism")[0]["projection_hints"]


@pytest.mark.asyncio
async def test_unpinned_retry_switching_into_claimed_symbol_is_rejected(
    monkeypatch, tmp_path
):
    """The new symbol must also dodge symbols claimed by other directions."""

    attempt1 = json.dumps(_strip_deadline_literals(
        json.loads(_raw_proposal("mechanism", snapshot=True))
    ))
    harness = _Harness(
        monkeypatch,
        tmp_path,
        attempt1={"mechanism": attempt1},
        # counterfactual's accepted attempt-1 already claims this leaf.
        retry={
            "mechanism": _retargeted_proposal(
                "mechanism", "_choose_intent_counterfactual"
            )
        },
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is False
    assert packet["reason"].endswith("got_2")
    assert _UNPIN_TOKEN in harness.retry_inputs("mechanism")[0]["projection_hints"]


@pytest.mark.asyncio
async def test_unpin_decision_uses_only_that_directions_own_rejection(
    monkeypatch, tmp_path
):
    """Two directions fail attempt-1 with different classes: the infeasible
    one unpins, the plain-schema one stays pinned — no hint leakage across
    directions."""

    harness = _Harness(
        monkeypatch,
        tmp_path,
        attempt1={
            "mechanism": json.dumps(_strip_deadline_literals(
                json.loads(_raw_proposal("mechanism", snapshot=True))
            )),
            "counterfactual": json.dumps(_measurement_missing(
                json.loads(_raw_proposal("counterfactual", snapshot=True))
            )),
        },
        retry={
            "mechanism": _retargeted_proposal("mechanism", "_choose_intent"),
            # keep-mode retry must KEEP its symbol to be accepted.
            "counterfactual": _proposal("counterfactual", snapshot=True),
        },
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    mechanism_retry = harness.retry_inputs("mechanism")[0]
    counterfactual_retry = harness.retry_inputs("counterfactual")[0]
    assert _UNPIN_TOKEN in mechanism_retry["projection_hints"]
    assert not any(
        hint.startswith("schema_retry_keep_change_symbol")
        for hint in mechanism_retry["projection_hints"]
    )
    assert (
        "schema_retry_keep_change_symbol.policy.py:_choose_intent_counterfactual"
        in counterfactual_retry["projection_hints"]
    )
    assert not any(
        hint.startswith("schema_retry_target_infeasible_unpinned")
        for hint in counterfactual_retry["projection_hints"]
    )
    assert harness.retry_inputs("compute_memory") == []


# ═══════════════════════════════════════════════════════════════════════════
# Each of the three infeasibility prefixes (exact-prefix matching, including
# the colon-suffixed wire forms) releases the pin; sibling stem codes and the
# bare scalar-spelling code do not.  The attempt-1 hints are controlled
# through the marker so every prefix is exercised deterministically against
# the same ensemble flow.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("prefix", _PREFIX_VARIANTS)
@pytest.mark.asyncio
async def test_each_target_infeasible_prefix_releases_the_pin(
    monkeypatch, tmp_path, prefix
):
    import agent_master

    real_hints = agent_master._master_proposal_projection_hints

    def marker_hints(output, *args, **kwargs):
        if "UNPIN-MARKER" in str(output):
            return [prefix]
        return real_hints(output, *args, **kwargs)

    harness = _Harness(
        monkeypatch,
        tmp_path,
        attempt1={"mechanism": _marker_invalid_proposal("mechanism")},
        retry={"mechanism": _retargeted_proposal("mechanism", "_choose_intent")},
    )
    monkeypatch.setattr(
        agent_master, "_master_proposal_projection_hints", marker_hints
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    retry_inputs = harness.retry_inputs("mechanism")
    assert prefix in retry_inputs[0]["projection_hints"]
    assert _UNPIN_TOKEN in retry_inputs[0]["projection_hints"]
    assert "structurally infeasible" in harness.retry_prompt("mechanism")
    symbols = {p["change_symbol"] for p in packet["ordered_proposals"]}
    assert "policy.py:_choose_intent" in symbols


@pytest.mark.parametrize("code", _KEEP_PIN_CODES)
@pytest.mark.asyncio
async def test_scalar_target_code_does_not_release_the_pin(
    monkeypatch, tmp_path, code
):
    """Repairable-in-place target codes keep the pin: a symbol-switching
    retry is still dropped (the repair is re-spelling the target literal,
    not switching change_symbol)."""

    import agent_master

    real_hints = agent_master._master_proposal_projection_hints

    def marker_hints(output, *args, **kwargs):
        if "UNPIN-MARKER" in str(output):
            return [code]
        return real_hints(output, *args, **kwargs)

    harness = _Harness(
        monkeypatch,
        tmp_path,
        attempt1={"mechanism": _marker_invalid_proposal("mechanism")},
        retry={"mechanism": _retargeted_proposal("mechanism", "_choose_intent")},
    )
    monkeypatch.setattr(
        agent_master, "_master_proposal_projection_hints", marker_hints
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is False
    assert packet["reason"].endswith("got_2")
    retry_inputs = harness.retry_inputs("mechanism")
    assert _KEEP_TOKEN in retry_inputs[0]["projection_hints"]
    assert not any(
        hint.startswith("schema_retry_target_infeasible_unpinned")
        for hint in retry_inputs[0]["projection_hints"]
    )
    retry_prompt = harness.retry_prompt("mechanism")
    assert "must not switch" in retry_prompt
    assert "structurally infeasible" not in retry_prompt
