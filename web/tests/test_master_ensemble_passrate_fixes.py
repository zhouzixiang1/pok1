"""Master ensemble pass-rate fixes (2026-09-13, v449-v452 runtime evidence).

The proposal ensemble kept landing at 2/3 valid directions and dying on
``three_distinct_schema_valid_scout_proposals_required``.  Three production
fixes, each covered here:

(a) **Unpin finalize** — a ``target_infeasible_unpinned`` schema repair now
    gets the SAME complete unavailable set at retry dispatch as a collision
    repair (a7a81224 only covered collision-class repairs): every accepted
    direction's change_symbol, every registered pin, and the direction's own
    first-round symbol, rendered through the shared
    ``schema_retry_avoid_claimed_symbol`` prompt branch.  Clean pins are
    untouched.

(b) **Bounded new-error second retry** — a direction whose FIRST retry fails
    validation with an error-class prefix absent from its attempt-1 rejection
    classes may take exactly ONE additional retry (module constant
    ``_ENSEMBLE_NEW_ERROR_RETRY_ROUNDS = 1``; two retries per direction in
    total).  Same-class repetitions never re-retry, and no third round
    exists.  Live v450 shape: attempt-1 ``shared_leaf_requires_full_namespace``
    fixed, retry dies on the opaque ``proposal_contract_invalid``.

(c) **Field detail behind ``proposal_contract_invalid``** — the generic
    fallback token is now followed by up to three concrete field-level codes
    (``_proposal_contract_invalid_detail_hints``), covering the three
    validator checks the primary hints function cannot see: the
    frozen-snapshot measurement binding and the expected/forbidden
    measurement-target bindings.  The repair guidance renders each new code.

Reuse the ensemble fixtures from test_master_proposal_ensemble /
test_master_schema_retry_unpin (cross-test imports are the established
pattern here).
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
from test_master_schema_retry_unpin import (
    _BOT_STATS_REF,
    _H2H_REF,
    _measurement_missing,
    _retargeted_proposal,
    _strip_deadline_literals,
)

NEXT_V = 149

_M = "policy.py:_choose_intent_mechanism"
_C = "policy.py:_choose_intent_compute_memory"
_CF = "policy.py:_choose_intent_counterfactual"
_FREE = "policy.py:_choose_intent"


def _token(symbol: str) -> str:
    return f"schema_retry_avoid_claimed_symbol.{symbol}"


class _RoundHarness:
    """Ensemble harness with per-direction per-round Scout outputs.

    ``attempt1`` / ``retry1`` / ``retry2`` map direction -> raw output; any
    missing entry falls back to a fully valid stock proposal.  The retry
    round is derived from how many times the SAME role has already been
    dispatched (the second schema retry reuses the strict-safe role label).
    """

    def __init__(
        self,
        monkeypatch,
        tmp_path,
        attempt1=None,
        retry1=None,
        retry2=None,
    ):
        import agent_master

        self.agent_master = agent_master
        self.attempt1 = attempt1 or {}
        self.retry1 = retry1 or {}
        self.retry2 = retry2 or {}
        source_dir = tmp_path / "source"
        snapshot_dir = tmp_path / "snapshot"
        _write_source(source_dir)
        _write_strength_snapshot(snapshot_dir)
        self.source_dir = source_dir
        self.snapshot_dir = snapshot_dir
        self.calls = []  # (role_name, prompt)
        self.rendered_inputs = []
        self.log_paths = []
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

        async def fake_query(
            prompt, _ctx, _ui, role_name, log_file=None, *_args, **_kwargs
        ):
            if role_name.startswith("MASTER PROPOSAL CRITIC"):
                self.calls.append((role_name, prompt))
                ids = list(dict.fromkeys(re.findall(
                    r'"proposal_id":"([0-9a-f]{16})"', prompt
                )))
                return _critic_output(self.agent_master, ids), 0.0, {}
            prior_same_role = sum(
                1 for name, _prompt in self.calls if name == role_name
            )
            self.calls.append((role_name, prompt))
            self.log_paths.append(str(log_file))
            direction = role_name.split("MASTER PROPOSAL ", 1)[1].split()[0]
            if "RETRY" not in role_name:
                output = self.attempt1.get(direction)
            elif prior_same_role == 0:
                output = self.retry1.get(direction)
            else:
                output = self.retry2.get(direction)
            if output is None:
                output = _proposal(direction, snapshot=True)
            return output, 0.0, {}

        monkeypatch.setattr(self.agent_master, "get_bot_dir", lambda _v: source_dir)
        monkeypatch.setattr(self.agent_master, "run_claude_query", fake_query)
        monkeypatch.setattr(llm_query, "render_llm_prompt", capturing_render)

    def scout_inputs(self, direction: str) -> list[dict]:
        return [
            inputs
            for inputs in self.rendered_inputs
            if inputs.get("direction") == direction
            and "repair_kind" in inputs
        ]

    def retry_inputs(self, direction: str, kind: str = "schema") -> list[dict]:
        return [
            inputs
            for inputs in self.rendered_inputs
            if inputs.get("direction") == direction
            and inputs.get("repair_kind") == kind
        ]

    def role_count(self, role_name: str) -> int:
        return sum(1 for name, _prompt in self.calls if name == role_name)

    def retry_prompt(self, direction: str, kind: str = "schema") -> str:
        label = "DISTINCTNESS" if kind == "distinctness" else "SCHEMA"
        role = f"MASTER PROPOSAL {direction} {label} RETRY"
        return next(prompt for name, prompt in self.calls if name == role)

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
# Shared invalid-output builders.  Each failure carries a DISTINCT error
# class so the new-error retry decision can be exercised deterministically.
# ═══════════════════════════════════════════════════════════════════════════

def _mutated_raw(direction: str, mutate) -> str:
    payload = json.loads(_raw_proposal(direction, snapshot=True))
    mutate(payload)
    return json.dumps(payload)


def _short_risks(payload: dict) -> None:
    """``proposal_risks_invalid`` (class proposal_risks_invalid)."""
    payload["risks"] = "too risky"


def _short_targeted_failure(payload: dict) -> None:
    """``proposal_required_text_invalid:targeted_failure``
    (class proposal_required_text_invalid)."""
    payload["targeted_failure"] = "too short"


def _forbidden_measurement_target(payload: dict) -> None:
    """A contract-valid measurement naming the PREPARED candidate bot
    (bot_name(NEXT_V)).  The validator rejects it on the forbidden-target
    binding while ``_master_proposal_projection_hints`` returns EMPTY —
    the exact opaque ``proposal_contract_invalid`` death."""
    payload["measurement"] = payload["measurement"].replace(
        f"target={bot_name(STRICT_TARGET_V)}",
        f"target={bot_name(NEXT_V)}",
    )


# ═══════════════════════════════════════════════════════════════════════════
# (a) The unpin retry's avoid set carries every occupied symbol and the
# prompt renders the complete list.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unpin_retry_avoid_set_holds_all_occupied_symbols(monkeypatch, tmp_path):
    """mechanism's attempt-1 is target-infeasible (pin released); both other
    directions accept and claim their symbols.  The unpin repair must name
    its own infeasible symbol AND both accepted claims, and the retry prompt
    must render the complete claimed list so the single attempt can pick a
    genuinely free symbol (v449: the unpin burned itself on a claimed one)."""

    harness = _RoundHarness(
        monkeypatch,
        tmp_path,
        attempt1={
            "mechanism": json.dumps(_strip_deadline_literals(
                json.loads(_raw_proposal("mechanism", snapshot=True))
            )),
        },
        retry1={"mechanism": _retargeted_proposal("mechanism", "_choose_intent")},
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    retry_inputs = harness.retry_inputs("mechanism", "schema")
    assert len(retry_inputs) == 1
    hints = retry_inputs[0]["projection_hints"]
    assert (
        "schema_retry_target_infeasible_unpinned.policy.py:_choose_intent_mechanism"
        in hints
    )
    # Own infeasible symbol plus BOTH other directions' accepted claims.
    assert {item.split(".", 1)[1] for item in hints
            if item.startswith("schema_retry_avoid_claimed_symbol.")} == {
        _M, _CF, _C,
    }
    # The retry prompt renders the complete list through the shared avoid
    # branch and keeps the distinct unpin instruction.
    retry_prompt = harness.retry_prompt("mechanism", "schema")
    assert "already claimed" in retry_prompt
    assert f"{_CF}, {_C}, {_M}" in retry_prompt
    assert "structurally infeasible" in retry_prompt
    assert "NEW change_symbol" in retry_prompt
    symbols = {p["change_symbol"] for p in packet["ordered_proposals"]}
    assert _FREE in symbols
    # Clean-pin paths stay untouched: the accepted directions issued no retry.
    assert harness.retry_inputs("counterfactual") == []
    assert harness.retry_inputs("compute_memory") == []


# ═══════════════════════════════════════════════════════════════════════════
# (b) The bounded new-error second retry.
# ═══════════════════════════════════════════════════════════════════════════

def test_second_retry_constant_and_error_class_extraction():
    """The extra round is a module constant, and the class extractor collapses
    parametric hint suffixes to their stable code prefix."""
    import agent_master_ensemble

    assert agent_master_ensemble._ENSEMBLE_NEW_ERROR_RETRY_ROUNDS == 1
    classes = agent_master_ensemble._proposal_error_classes((
        "proposal_required_text_invalid:targeted_failure",
        "schema_retry_keep_change_symbol.policy.py:_bluff_allowed",
        "proposal_cited_sample_too_small.mechanism.cited.4.tier.5",
        "proposal_contract_invalid",
    ))
    assert classes == {
        "proposal_required_text_invalid",
        "schema_retry_keep_change_symbol",
        "proposal_cited_sample_too_small",
        "proposal_contract_invalid",
    }
    assert agent_master_ensemble._proposal_error_classes(None) == frozenset()


@pytest.mark.asyncio
async def test_new_error_class_after_first_retry_triggers_second_retry(
    monkeypatch, tmp_path
):
    """v450 shape, fixed: attempt-1 fails on the measurement field, the retry
    FIXES that but introduces a new failure class (short risks), and the
    direction gets exactly one more attempt — which succeeds.  3/3 instead of
    the historical 2/3 abandon."""

    harness = _RoundHarness(
        monkeypatch,
        tmp_path,
        attempt1={
            "mechanism": _mutated_raw("mechanism", _measurement_missing),
        },
        retry1={"mechanism": _mutated_raw("mechanism", _short_risks)},
        # retry2 falls back to the fully valid stock proposal (pin kept).
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    assert harness.role_count("MASTER PROPOSAL mechanism SCHEMA RETRY") == 2
    # The second retry mirrors the first repair's clean-pin rule and carries
    # the NEW failure class plus the keep token.
    second_retry_inputs = harness.retry_inputs("mechanism", "schema")
    assert second_retry_inputs[1]["projection_hints"] == [
        "proposal_risks_invalid",
        "schema_retry_keep_change_symbol.policy.py:_choose_intent_mechanism",
    ]
    # Distinct diagnostic logs: first retry unsuffixed, second retry gets the
    # round suffix so the two attempts never overwrite one IO log.
    retry_logs = [
        path for path in harness.log_paths
        if path.endswith("master_proposal_mechanism_schema_retry_io.txt")
        or path.endswith("master_proposal_mechanism_schema_retry2_io.txt")
    ]
    assert len(retry_logs) == 2
    assert any(path.endswith("master_proposal_mechanism_schema_retry_io.txt")
               for path in retry_logs)
    assert any(path.endswith("master_proposal_mechanism_schema_retry2_io.txt")
               for path in retry_logs)


@pytest.mark.asyncio
async def test_repeated_same_error_class_does_not_trigger_second_retry(
    monkeypatch, tmp_path
):
    """The retry repeated the attempt-1 error CLASS (required-text), only on
    a different field: no real progress, so the single-retry behavior stays
    and no second retry is dispatched."""

    harness = _RoundHarness(
        monkeypatch,
        tmp_path,
        attempt1={
            "mechanism": _mutated_raw("mechanism", _measurement_missing),
        },
        retry1={"mechanism": _mutated_raw(
            "mechanism", _short_targeted_failure
        )},
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is False
    assert packet["reason"].endswith("got_2")
    assert harness.role_count("MASTER PROPOSAL mechanism SCHEMA RETRY") == 1
    # The single retry prompt carried the attempt-1 class' (new) guidance.
    retry_prompt = harness.retry_prompt("mechanism", "schema")
    assert "under 20 characters" in retry_prompt
    assert "measurement" in retry_prompt


@pytest.mark.asyncio
async def test_second_retry_fires_at_most_once_never_a_third(
    monkeypatch, tmp_path
):
    """The second retry also fails (this time with a class already seen in
    attempt-1): no third retry exists — the per-direction cap is structural."""

    harness = _RoundHarness(
        monkeypatch,
        tmp_path,
        attempt1={
            "mechanism": _mutated_raw("mechanism", _measurement_missing),
        },
        retry1={"mechanism": _mutated_raw("mechanism", _short_risks)},
        retry2={"mechanism": _mutated_raw(
            "mechanism", _short_targeted_failure
        )},
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is False
    assert packet["reason"].endswith("got_2")
    assert harness.role_count("MASTER PROPOSAL mechanism SCHEMA RETRY") == 2
    # The critics never ran: the ensemble died before ballots.
    assert not any("CRITIC" in name for name, _prompt in harness.calls)


# ═══════════════════════════════════════════════════════════════════════════
# (c) proposal_contract_invalid fallback carries field-level detail.
# ═══════════════════════════════════════════════════════════════════════════

def _ensemble_probe_kwargs(snapshot_dir, source_graph):
    return dict(
        source_graph=source_graph,
        snapshot_dir=snapshot_dir,
        national_policy_only=True,
        require_snapshot_evidence=True,
        evidence_mode="frozen_strength_snapshot",
    )


def test_forbidden_measurement_target_rejects_with_empty_primary_hints(
    tmp_path,
):
    """The gap behind the opaque v450 death: the validator rejects the
    prepared-candidate measurement target while the primary hints function —
    which cannot see the expected/forbidden bindings — returns []."""
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    raw = _mutated_raw("mechanism", _forbidden_measurement_target)
    kwargs = _ensemble_probe_kwargs(snapshot_dir, graph)

    assert agent_master._validated_master_proposal(
        raw,
        "mechanism",
        execution_mode="strategy_implementation",
        forbidden_measurement_target=bot_name(NEXT_V),
        **kwargs,
    ) is None
    assert agent_master._master_proposal_projection_hints(raw, **kwargs) == []
    details = agent_master._proposal_contract_invalid_detail_hints(
        raw,
        **kwargs,
        forbidden_measurement_target=bot_name(NEXT_V),
    )
    assert details == [
        "proposal_measurement_target_not_bound_to_snapshot",
        "proposal_measurement_target_forbidden_match:forbidden="
        + bot_name(NEXT_V),
    ]


def test_snapshot_unbound_measurement_target_gets_field_detail(tmp_path):
    """A contract-valid measurement naming a bot absent from the snapshot
    rows rejects on the frozen-snapshot binding with empty primary hints;
    the detail probe names it."""
    import agent_master

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)
    graph, _digest = agent_master._source_symbol_graph(source_dir)
    raw = _mutated_raw("mechanism", lambda payload: payload.update(
        {"measurement": payload["measurement"].replace(
            f"target={bot_name(STRICT_TARGET_V)}",
            f"target={bot_name(NEXT_V + 200)}",
        )}
    ))
    kwargs = _ensemble_probe_kwargs(snapshot_dir, graph)

    assert agent_master._validated_master_proposal(
        raw,
        "mechanism",
        execution_mode="strategy_implementation",
        forbidden_measurement_target=bot_name(NEXT_V),
        **kwargs,
    ) is None
    assert agent_master._master_proposal_projection_hints(raw, **kwargs) == []
    assert agent_master._proposal_contract_invalid_detail_hints(
        raw, **kwargs
    ) == ["proposal_measurement_target_not_bound_to_snapshot"]


@pytest.mark.asyncio
async def test_contract_invalid_fallback_hints_carry_field_detail_and_render(
    monkeypatch, tmp_path
):
    """End-to-end: the opaque fallback now reads
    ``[proposal_contract_invalid, <field detail...>]``, the repair prompt
    renders an actionable instruction for the field detail, and the pinned
    retry that fixes the target is accepted."""

    harness = _RoundHarness(
        monkeypatch,
        tmp_path,
        attempt1={
            "mechanism": _mutated_raw(
                "mechanism", _forbidden_measurement_target
            ),
        },
        # retry1 falls back to the valid stock proposal (pin kept, target
        # bound to the snapshot).
    )
    packet = await harness.run(tmp_path)

    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    retry_inputs = harness.retry_inputs("mechanism", "schema")
    assert len(retry_inputs) == 1
    hints = retry_inputs[0]["projection_hints"]
    # Generic code first, concrete field-level detail after (cap 3).
    assert hints[:3] == [
        "proposal_contract_invalid",
        "proposal_measurement_target_not_bound_to_snapshot",
        "proposal_measurement_target_forbidden_match:forbidden="
        + bot_name(NEXT_V),
    ]
    retry_prompt = harness.retry_prompt("mechanism", "schema")
    assert "does not exist yet" in retry_prompt
    assert bot_name(NEXT_V) in retry_prompt
    assert "must name a bot that appears inside the cited snapshot rows" in retry_prompt
    assert harness.role_count("MASTER PROPOSAL mechanism SCHEMA RETRY") == 1


def test_guidance_renders_the_new_field_detail_codes():
    import agent_master

    guidance = agent_master._proposal_schema_repair_guidance(
        (
            "proposal_contract_invalid",
            "proposal_measurement_target_not_bound_to_snapshot",
            "proposal_measurement_target_forbidden_match:forbidden="
            + bot_name(NEXT_V),
        ),
        require_snapshot_evidence=True,
    )
    assert "does not exist yet" in guidance
    assert bot_name(NEXT_V) in guidance
    assert "must name a bot that appears inside the cited snapshot rows" in guidance

    required = agent_master._proposal_schema_repair_guidance(
        (
            "proposal_required_text_invalid:targeted_failure",
            "proposal_required_text_invalid:counterfactual",
        ),
        require_snapshot_evidence=False,
    )
    assert "targeted_failure, counterfactual" in required
    assert "under 20 characters" in required


# ═══════════════════════════════════════════════════════════════════════════
# The evidence helpers stay importable for future tests (re-exported fixture
# references keep the sibling-test import graph intact).
# ═══════════════════════════════════════════════════════════════════════════

assert _BOT_STATS_REF and _H2H_REF and STRICT_SOURCE_V is not None
