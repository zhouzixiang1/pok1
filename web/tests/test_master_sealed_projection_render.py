"""Block-49 regression: sealed projection bytes must survive the worker budget.

Failure this pins (workflow dwfrun-0828fda5 + independent critique, every line
reproduced): v666/v668/v671/v672 died ``sealed_evidence_projection_mismatch``
because the worker-budget trim rewrote ``resolved_projection`` to [:400] INSIDE
the sealed proposal (recomputing projection_sha256 and proposal_id), while the
audit re-derives the canonical [:1600] prefix
(evidence_snapshot.py:1721). Producer and validator held a static 1600-vs-400
contradiction since 7683710b (2026-10-04); the models' numbers were proven
byte-correct (node_sha256/games matched the frozen snapshot 8/8) — only the
prefix length disagreed.

Fix (plan A): compaction happens only on the RENDER copy of the worker block;
the sealed dictionary is never touched, so identity, digests, and the audit's
canonical comparison all stay consistent, while the v328-v333 13k worker cap
keeps its teeth at the same trigger.
"""

import hashlib
import json

import pytest

import agent_master_proposal_packet as packet
from output_schema import WORKER_PROMPT_MAX_CHARS, WORKER_PROMPT_MIN_CHARS
from plan_compiler import SYSTEM_OWNED_CONTRACT_MAX_CHARS


def _binding(projection_len: int) -> dict:
    projection = "".join(
        str(index % 10) for index in range(projection_len)
    )
    return {
        "reference": "snapshot:selection_snapshot.json#/rows",
        "games": 1401,
        "a_wins": 700,
        "b_wins": 600,
        "draws": 101,
        "node_sha256": "a" * 64,
        "resolved_projection": projection,
        "projection_sha256": hashlib.sha256(projection.encode("utf-8")).hexdigest(),
        "projection_truncated": projection_len > 1600,
    }


def _proposal(*, bindings: int = 1, projection_len: int = 1600,
              pad_symbols: int = 0) -> dict:
    falsifier = {
        "test_name": "incremental_opponent_model",
        "state_learning_primary": "action_profile",
        "intervention_target": "opponent.rates",
        "control": "Hold opponent.rates at its bounded prior for the identical paired state.",
        "intervention": "Apply the proposed profile-driven margin on that identical state.",
        "expected_observation": "One preflop socket intent changes while the control does not.",
    }
    symbols = ["policy.evaluate"]
    if pad_symbols:
        symbols.append("x" * pad_symbols)
    return {
        "proposal_id": "p" * 16,
        "targeted_failure": "Repeated preflop jam against tight profiles.",
        "structural_change": "One confidence-gated margin operand on the raise path.",
        "counterfactual": "Same state, rates frozen vs profiled, one intent differs.",
        "measurement": "target=preflop_jam; primary=incremental_opponent_model;",
        "expected_diff": "Tight profiles face fewer jams.",
        "target_files": ["bots/national_cloud_v700/policy.py"],
        "source_symbols": symbols,
        "change_symbol": "policy._raise_intent",
        "reachable_chain": ["policy.get_baseline_decision", "policy._raise_intent"],
        "falsifier": falsifier,
        "mechanism_target": "opponent.rates",
        "evidence_refs": ["snapshot:selection_snapshot.json#/rows"],
        "snapshot_evidence": [_binding(projection_len) for _ in range(bindings)],
        "execution_mode": "strategy_implementation",
        "why_not_threshold_tuning": "Adds a new operand class, not a constant move.",
        "risks": "Sparse profiles stay neutral by the confidence cap.",
    }


def _sealed_snapshot_bytes(proposal: dict) -> str:
    return json.dumps(
        proposal["snapshot_evidence"], ensure_ascii=False, sort_keys=True
    )


def _budget_limit() -> int:
    # binding_chars above this leave the provider less than its minimum room
    # (mirror of _worker_budget_fits).
    return (
        WORKER_PROMPT_MAX_CHARS
        - 2
        - SYSTEM_OWNED_CONTRACT_MAX_CHARS
        - WORKER_PROMPT_MIN_CHARS
    )


def test_small_proposal_renders_canonical_projection_untouched():
    proposal = _proposal(bindings=3, projection_len=1600)
    block = packet._worker_block_for_proposal(proposal)
    assert len(block) <= _budget_limit()
    assert '"projection_render_compacted":true' not in block
    assert _binding(1600)["resolved_projection"] in block


def test_oversized_block_compacts_render_and_seal_stays_canonical():
    """The v666-shape packet: bindings too big for the worker cap. Old design
    rewrote the SEALED bytes (400) and the audit died. New design compacts
    only the rendered copy; sealed stays canonical and bindability passes.

    pad_symbols=3600 makes the overflow PROJECTION-driven (full render 11220
    breaches the 10930 room cap, the compacted render 7725 fits), which is the
    exact shape the fix must rescue. A pad so large that compaction cannot
    rescue is a DIFFERENT case, pinned by
    test_compaction_that_does_not_buy_room_keeps_canonical_render.
    """

    proposal = _proposal(bindings=3, projection_len=1600, pad_symbols=3600)
    full = packet._selected_proposal_worker_block(proposal, compact_evidence=False)
    compacted = packet._selected_proposal_worker_block(proposal, compact_evidence=True)
    assert len(full) > _budget_limit()          # would breach the worker cap
    assert len(compacted) <= _budget_limit()    # compaction buys real room
    sealed_before = _sealed_snapshot_bytes(proposal)

    block = packet._worker_block_for_proposal(proposal)
    assert block == compacted
    assert '"projection_render_compacted":true' in block
    # the 1600-char canonical projection must NOT appear (render is compacted)
    assert _binding(1600)["resolved_projection"] not in block
    # …while the SEALED dictionary is byte-identical to what the audit reads
    assert _sealed_snapshot_bytes(proposal) == sealed_before
    binding = proposal["snapshot_evidence"][0]
    assert len(binding["resolved_projection"]) == 1600
    assert "projection_render_compacted" not in binding
    assert packet._proposal_worker_bindability_error(proposal) is None


def test_trim_never_mutates_sealed_projection():
    """Direct regression on the removed trim branch: prose may be capped, the
    snapshot_evidence subtree must not gain or lose a byte."""

    proposal = _proposal(bindings=2, projection_len=1600)
    # Force a bindability breach through model-authored prose only.
    proposal["counterfactual"] = "y" * 6000
    proposal["expected_diff"] = "z" * 3000
    sealed_before = _sealed_snapshot_bytes(proposal)
    assert packet._proposal_worker_bindability_error(proposal) is not None

    trimmed = packet._trim_proposal_prose_to_worker_budget(proposal)
    assert trimmed is not proposal
    assert _sealed_snapshot_bytes(trimmed) == sealed_before
    for binding in trimmed["snapshot_evidence"]:
        assert "projection_render_compacted" not in binding
        assert len(binding["resolved_projection"]) == 1600


def test_budget_and_bind_use_the_same_render():
    """plan_compiler identity + the no_binding_budget gate both measure the
    block that ACTUALLY gets appended; a divergence would re-open a silent
    truncation class. Pin the shared seam."""

    # 3600: projection-driven overflow that compaction DOES rescue (see the
    # oversized-block test) — so the bound prompt must carry the compacted
    # render, and the budget measurement agrees with the appended block.
    proposal = _proposal(bindings=3, projection_len=1600, pad_symbols=3600)
    compilation = packet._selected_proposal_compilation_contract(proposal)
    block = packet._worker_block_for_proposal(proposal)
    assert (
        compilation["reserved_selected_contract_chars"] == len(block)
    )

    data = {
        "tasks": [
            {
                "worker_id": "w1",
                "role": "worker",
                "target_files": ["bots/national_cloud_v700/policy.py"],
                "worker_prompt": "Edit the named symbol to add the margin. " * 20,
            }
        ]
    }
    bound = packet._bind_selected_proposal_workers(data, proposal)
    prompt = bound["tasks"][0]["worker_prompt"]
    assert block in prompt
    assert '"projection_render_compacted":true' in prompt
    # sealed bytes untouched by binding either
    assert _sealed_snapshot_bytes(proposal) == _sealed_snapshot_bytes(
        json.loads(json.dumps(proposal))
    )


def test_compaction_that_does_not_buy_room_keeps_canonical_render():
    """A giant NON-compactable field must not be papered over: the block keeps
    the canonical projection and the bindability error guides the schema
    repair instead (no silent quality loss)."""

    proposal = _proposal(bindings=1, projection_len=1600, pad_symbols=12000)
    full = packet._selected_proposal_worker_block(proposal, compact_evidence=False)
    compacted = packet._selected_proposal_worker_block(proposal, compact_evidence=True)
    assert len(compacted) > _budget_limit()  # 1200 pad can't be compacted away
    block = packet._worker_block_for_proposal(proposal)
    assert block == full
    assert packet._proposal_worker_bindability_error(proposal) is not None
