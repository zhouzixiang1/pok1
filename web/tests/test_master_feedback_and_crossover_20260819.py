"""Regression tests for the 2026-08-19 adversarial-audit fix batch.

85 generations (v189..v273) produced zero publications because the Master
proposal-packet gates rejected proposals through hint strings that the single
permitted repair prompt never translated.  These tests pin the four classes
that made the retry blind or trapped:

1. the aggregate list pointer ``selection_snapshot.json#/rows`` must bind the
   strongest row's ``games`` so the advertised aggregate source can actually
   satisfy the two-tier statistical evidence bar;
2. one unknown leaf inside a root-scoped list must not cascade into false
   bare-shared-leaf errors for the legal leaves in the same list;
3. the OpponentTracker's real runtime scalar leaves (samples, confidence,
   adaptation_weight, ...) are legal root-scoped children;
4. a schema-retry pin that collides with a symbol another direction already
   claimed becomes an avoid instruction instead of an unsatisfiable keep-pin.
"""

import json

import pytest

from bot_namespace import bot_name
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V


class _UI:
    def clear_io(self):
        pass

    def log_history(self, *_args, **_kwargs):
        pass


# ═══════════════════════════════════════════════════════════════════════════
# ① snapshot pointer binding: aggregate list containers carry games
# ═══════════════════════════════════════════════════════════════════════════

def test_rows_pointer_binds_strongest_row_games(tmp_path):
    from agent_master_validation import _snapshot_reference_evidence_binding

    (tmp_path / "selection_snapshot.json").write_text(json.dumps({
        "rows": [
            {"name": bot_name(1), "games": 120},
            {"name": bot_name(11), "games": 250},
            {"name": "not-a-dict-entry"},
        ],
    }), encoding="utf-8")
    binding = _snapshot_reference_evidence_binding(
        "snapshot:selection_snapshot.json#/rows", tmp_path
    )
    assert binding is not None
    assert binding["games"] == 250


def test_two_tier_bar_accepts_rows_aggregate(tmp_path):
    from agent_master_validation import _snapshot_evidence_two_tier_errors

    (tmp_path / "bot_stats.json").write_text(json.dumps({
        bot_name(STRICT_SOURCE_V): {"games": 314, "wins": 160},
    }), encoding="utf-8")
    # pool max 314 >= absolute tiers (30/200): an h2h primary row plus the
    # /rows aggregate container passes; a sub-200 aggregate row still fails.
    # Contract change 2026-09-13: the tier check takes (reference, games)
    # citations — the aggregate container pointer keeps binding the strongest
    # row's games, and this tmp_path has no head_to_head.json so the h2h
    # citation grades at the unknown-matchup absolute tier 30 (33 >= 30).
    h2h_ref = f"snapshot:head_to_head.json#/{bot_name(STRICT_SOURCE_V)} vs {bot_name(STRICT_TARGET_V)}"
    assert _snapshot_evidence_two_tier_errors(
        [(h2h_ref, 33), ("snapshot:selection_snapshot.json#/rows", 250)],
        tmp_path,
    ) == []
    assert _snapshot_evidence_two_tier_errors(
        [(h2h_ref, 33), ("snapshot:selection_snapshot.json#/rows", 120)],
        tmp_path,
    ) != []


# ═══════════════════════════════════════════════════════════════════════════
# ② + ③ mechanism prose: no cascade from unknown leaves; runtime scalars legal
# ═══════════════════════════════════════════════════════════════════════════

def _mechanism_errors(root: str, structural_change: str):
    from agent_master_proposal_primaries import _proposal_mechanism_target_errors

    primary = {
        "opponent.rates": "incremental_opponent_model",
        "opponent.terminal_response": "terminal_response_adaptation",
        "opponent.showdown_range": "showdown_range_adaptation",
    }[root]
    proposal = {
        "mechanism_target": root,
        "structural_change": structural_change,
        "expected_diff": structural_change,
    }
    falsifier = {
        "test_name": primary,
        "intervention_target": root,
        "intervention": structural_change,
    }
    return _proposal_mechanism_target_errors(proposal, falsifier)


def test_unknown_leaf_does_not_cascade_into_shared_leaf_errors():
    errors = _mechanism_errors(
        "opponent.rates",
        "Adapt only opponent.rates (fold_to_raise, nonsense_field) inside the "
        "bounded consumer.",
    )
    assert (
        "proposal_mechanism_root_scoped_unknown_leaf:opponent.rates:nonsense_field"
        in errors
    )
    assert not [
        e for e in errors
        if e.startswith("proposal_mechanism_shared_leaf_requires_full_namespace")
    ]


def test_terminal_response_runtime_scalars_are_legal_leaves():
    errors = _mechanism_errors(
        "opponent.terminal_response",
        "Gate the adaptation on opponent.terminal_response (confidence, "
        "adaptation_weight, samples, fold_to_raise) inside the consumer.",
    )
    assert not [
        e for e in errors
        if e.startswith("proposal_mechanism_root_scoped_unknown_leaf")
        or e.startswith("proposal_mechanism_shared_leaf_requires_full_namespace")
    ]


def test_showdown_range_runtime_scalars_are_legal_leaves():
    errors = _mechanism_errors(
        "opponent.showdown_range",
        "Weight the prior by opponent.showdown_range (confidence, "
        "adaptation_weight, tightness, showdown_reach_rate) only.",
    )
    assert not [
        e for e in errors
        if e.startswith("proposal_mechanism_root_scoped_unknown_leaf")
        or e.startswith("proposal_mechanism_shared_leaf_requires_full_namespace")
    ]


# ═══════════════════════════════════════════════════════════════════════════
# ④ ensemble retry pinning: colliding pins downgrade to avoid instructions
# ═══════════════════════════════════════════════════════════════════════════

def _ensemble_harness(monkeypatch, tmp_path, attempt_outputs, retry_outputs):
    """Drive _run_master_proposal_ensemble with scripted per-role outputs.

    ``attempt_outputs`` maps direction -> first-attempt raw proposal text;
    ``retry_outputs`` maps direction -> schema-retry raw proposal text.
    Returns (packet_or_none, [(role_name, prompt), ...]).
    """
    import agent_master
    from tests.test_master_proposal_ensemble import (
        _critic_output,
        _write_source,
        _write_strength_snapshot,
    )

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)
    calls = []

    async def fake_query(prompt, _ctx, _ui, role_name, *_args, **_kwargs):
        calls.append((role_name, prompt))
        if role_name.startswith("MASTER PROPOSAL CRITIC"):
            ids = list(dict.fromkeys(
                __import__("re").findall(
                    r'"proposal_id":"([0-9a-f]{16})"', prompt
                )
            ))
            return _critic_output(agent_master, ids), 0.0, {}
        if "SCHEMA RETRY" in role_name:
            for direction, text in retry_outputs.items():
                if direction in role_name:
                    return text, 0.0, {}
        for direction, text in attempt_outputs.items():
            if direction in role_name:
                return text, 0.0, {}
        raise AssertionError(f"unexpected role {role_name}")

    monkeypatch.setattr(agent_master, "get_bot_dir", lambda _v: source_dir)
    monkeypatch.setattr(agent_master, "run_claude_query", fake_query)

    async def run():
        return await agent_master._run_master_proposal_ensemble(
            "frozen planning context",
            source_v=STRICT_TARGET_V,
            next_v=273,
            ui=_UI(),
            log_dir=tmp_path,
            allowed_evidence_snapshot_dir=str(snapshot_dir),
        )

    return run, calls


def _invalid_with_symbol(direction: str, symbol: str) -> str:
    """A schema-invalid proposal whose raw change_symbol is extractable."""

    from tests.test_master_proposal_ensemble import _proposal, _raw_proposal

    payload = json.loads(_raw_proposal(direction, snapshot=True))
    payload["change_symbol"] = symbol
    payload["source_symbols"] = [
        "policy.py:get_baseline_decision",
        symbol,
    ]
    payload["reachable_chain"] = ["policy.py:get_baseline_decision", symbol]
    payload["evidence_refs"] = [
        "source:policy.py:get_baseline_decision",
        f"source:{symbol}",
        "snapshot:head_to_head.json#/"
        f"{bot_name(STRICT_TARGET_V)} vs {bot_name(STRICT_TARGET_V + 1)}",
        f"snapshot:bot_stats.json#/{bot_name(STRICT_SOURCE_V)}",
    ]
    # Invalid: target_files must be exactly ["policy.py"].
    payload["target_files"] = ["policy.py", "helper.py"]
    return json.dumps(payload)


def _valid_with_symbol(direction: str, symbol: str) -> str:
    """A schema-valid retry that keeps the given change_symbol."""

    payload = json.loads(_invalid_with_symbol(direction, symbol))
    payload["target_files"] = ["policy.py"]
    return json.dumps(payload)


def _valid(direction: str) -> str:
    from tests.test_master_proposal_ensemble import _proposal

    return _proposal(direction, snapshot=True)


@pytest.mark.asyncio
async def test_colliding_pins_downgrade_to_avoid_instructions(
    monkeypatch, tmp_path
):
    run, calls = _ensemble_harness(
        monkeypatch,
        tmp_path,
        attempt_outputs={
            "mechanism": _invalid_with_symbol(
                "mechanism", "policy.py:_choose_intent"
            ),
            "counterfactual": _invalid_with_symbol(
                "counterfactual", "policy.py:_choose_intent"
            ),
            "compute_memory": _valid("compute_memory"),
        },
        retry_outputs={
            "mechanism": _valid_with_symbol(
                "mechanism", "policy.py:_choose_intent"
            ),
            "counterfactual": _valid("counterfactual"),
        },
    )
    packet = json.loads(await run())
    assert packet["valid"] is True
    assert packet["proposal_count"] == 3
    mechanism_retry = next(
        p for r, p in calls if "mechanism" in r and "SCHEMA RETRY" in r
    )
    counterfactual_retry = next(
        p for r, p in calls if "counterfactual" in r and "SCHEMA RETRY" in r
    )
    assert "Keep change_symbol exactly policy.py:_choose_intent" in (
        mechanism_retry
    )
    assert "already claimed by another direction" in counterfactual_retry
    assert "policy.py:_choose_intent" in counterfactual_retry


@pytest.mark.asyncio
async def test_pin_against_accepted_symbol_downgrades_to_avoid(
    monkeypatch, tmp_path
):
    run, calls = _ensemble_harness(
        monkeypatch,
        tmp_path,
        attempt_outputs={
            # mechanism's accepted proposal claims _choose_intent_mechanism.
            "mechanism": _valid("mechanism"),
            "counterfactual": _invalid_with_symbol(
                "counterfactual", "policy.py:_choose_intent_mechanism"
            ),
            "compute_memory": _invalid_with_symbol(
                "compute_memory", "policy.py:_choose_intent"
            ),
        },
        retry_outputs={
            "counterfactual": _valid("counterfactual"),
            "compute_memory": _valid_with_symbol(
                "compute_memory", "policy.py:_choose_intent"
            ),
        },
    )
    packet = json.loads(await run())
    assert packet["valid"] is True
    counterfactual_retry = next(
        p for r, p in calls if "counterfactual" in r and "SCHEMA RETRY" in r
    )
    compute_retry = next(
        p for r, p in calls if "compute_memory" in r and "SCHEMA RETRY" in r
    )
    assert "already claimed" in counterfactual_retry
    assert "Keep change_symbol exactly policy.py:_choose_intent" in compute_retry


@pytest.mark.asyncio
async def test_schema_retry_switching_pin_is_rejected(monkeypatch, tmp_path):
    run, _calls = _ensemble_harness(
        monkeypatch,
        tmp_path,
        attempt_outputs={
            "mechanism": _invalid_with_symbol(
                "mechanism", "policy.py:_choose_intent"
            ),
            "counterfactual": _valid("counterfactual"),
            "compute_memory": _valid("compute_memory"),
        },
        # The retry silently switches its target family: it must be rejected.
        retry_outputs={"mechanism": _valid("mechanism")},
    )
    packet = json.loads(await run())
    assert packet["valid"] is False
    assert "three_distinct_schema_valid_scout_proposals_required" in json.dumps(
        packet
    )


# ═══════════════════════════════════════════════════════════════════════════
# Extra snapshot citations: keep the strongest three instead of hard-reject
# ═══════════════════════════════════════════════════════════════════════════

def test_extra_snapshot_refs_keep_strongest_three(tmp_path):
    from agent_master_validation import _validated_master_proposal
    from tests.test_master_proposal_ensemble import (
        _proposal,
        _write_source,
        _write_strength_snapshot,
    )

    source_dir = tmp_path / "source"
    snapshot_dir = tmp_path / "snapshot"
    _write_source(source_dir)
    _write_strength_snapshot(snapshot_dir)
    extra_h2h_key = (
        f"{bot_name(STRICT_TARGET_V)} vs {bot_name(STRICT_TARGET_V + 2)}"
    )
    h2h = json.loads((snapshot_dir / "head_to_head.json").read_text(encoding="utf-8"))
    h2h[extra_h2h_key] = {
        "games": 40,
        "a_wins": 18,
        "b_wins": 20,
        "draws": 2,
        "win_rate": 0.45,
    }
    (snapshot_dir / "head_to_head.json").write_text(
        json.dumps(h2h), encoding="utf-8"
    )
    stats = json.loads((snapshot_dir / "bot_stats.json").read_text(encoding="utf-8"))
    stats[bot_name(STRICT_TARGET_V)] = {
        "games": 120, "wins": 50, "losses": 65, "draws": 5,
    }
    (snapshot_dir / "bot_stats.json").write_text(
        json.dumps(stats), encoding="utf-8"
    )

    payload = json.loads(_proposal("mechanism", snapshot=True).split(
        "```json\n", 1
    )[1].rsplit("\n```", 1)[0])
    payload["evidence_refs"].extend([
        f"snapshot:head_to_head.json#/{extra_h2h_key}",
        f"snapshot:bot_stats.json#/{bot_name(STRICT_TARGET_V)}",
    ])
    graph, _digest = __import__("agent_master")._source_symbol_graph(source_dir)
    proposal = _validated_master_proposal(
        json.dumps(payload),
        "mechanism",
        source_graph=graph,
        snapshot_dir=snapshot_dir,
        national_policy_only=True,
        require_snapshot_evidence=True,
        evidence_mode="frozen_strength_snapshot",
    )
    assert proposal is not None
    snap_refs = [
        ref for ref in proposal["evidence_refs"] if str(ref).startswith("snapshot:")
    ]
    assert len(snap_refs) == 3
    games = sorted(
        int(binding["games"])
        for binding in proposal["snapshot_evidence"]
        if isinstance(binding.get("games"), int)
    )
    assert games == [40, 120, 250]
