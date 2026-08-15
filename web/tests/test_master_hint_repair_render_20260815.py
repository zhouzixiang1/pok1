"""Repair-hint rendering must never crash the master proposal provider prompt.

v186 regression (2026-08-15): a proposal whose selected-contract worker
binding exceeded the worker prompt budget produced the hint
``proposal_worker_binding_cannot_fit_minimum_prompt:{...full compilation
JSON...}``. That hint entered the schema-repair prompt's projection_hints and
the renderer raised ``ValueError("Master proposal projection hints are
invalid")`` before ANY provider call — reclassified as
``master_llm_unavailable`` infrastructure failure, burning the whole 6-attempt
infra retry budget on a guaranteed-to-repeat local crash (~$3-4 per 10-minute
attempt, trending worse each retry).

Two-layer fix under test:
1. source: the bindability hint now carries the budget numbers in a compact,
   charset-safe form instead of the full compilation JSON;
2. renderer: invalid/oversized hints are sanitized (stable leading code +
   digest) instead of raising.
"""

import re
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import agent_master_proposal_packet as packet  # noqa: E402
import agent_master_prompts as prompts  # noqa: E402

_HINT_CHARSET = re.compile(r"[a-z0-9_:.-]+")


def test_bindability_error_is_compact_and_charset_safe(monkeypatch):
    compilation = {
        "proposal_id": "p1",
        "falsifier_test_name": "falsifies_thesis",
        "mechanism_target": "action_profile",
        "change_symbol": "policy.py:decide_river",
        "state_learning_primary": "action_profile",
        "intervention_target": "opponent.rates",
        "required_primary_checks": [],
        "reserved_selected_contract_chars": 11330,
        "separator_chars": 2,
        "reserved_runtime_contract_max_chars": 2048,
        "global_cap_chars": 13000,
        "max_provider_chars": 13000 - 11330 - 2 - 2048,  # -380
        "character_metric": "python_unicode_code_points",
    }
    monkeypatch.setattr(
        packet, "_selected_proposal_compilation_contract", lambda proposal: compilation
    )

    hint = packet._proposal_worker_bindability_error({"proposal_id": "p1"})
    assert hint is not None
    assert len(hint) <= 160
    assert _HINT_CHARSET.fullmatch(hint) is not None
    # The actionable budget numbers survive (this is the repair guidance the
    # model needs to shrink its binding).
    assert "binding_chars.11330" in hint
    assert "shrink_binding_by.400" in hint  # 20 - (-380)

    # Fits: no error at all.
    fits = dict(compilation, max_provider_chars=100)
    monkeypatch.setattr(
        packet, "_selected_proposal_compilation_contract", lambda proposal: fits
    )
    assert packet._proposal_worker_bindability_error({"proposal_id": "p1"}) is None


def test_sanitize_projection_hint_passthrough_and_reduction():
    keep = "proposal_snapshot_evidence_too_many"
    assert prompts._sanitize_projection_hint(keep) == keep
    assert prompts._sanitize_projection_hint(f"  {keep}  ") == keep

    # The exact v186 shape: code + full JSON payload.
    leaked = (
        "proposal_worker_binding_cannot_fit_minimum_prompt:"
        '{"proposal_id":"p1","reserved_selected_contract_chars":11330,'
        '"max_provider_chars":-380,"global_cap_chars":13000}'
    )
    out = prompts._sanitize_projection_hint(leaked)
    assert out != leaked
    assert len(out) <= 160
    assert _HINT_CHARSET.fullmatch(out) is not None
    assert out.startswith("proposal_worker_binding_cannot_fit_minimum_prompt")
    # Distinct payloads stay distinct (digest suffix).
    other = leaked.replace("11330", "9999")
    assert prompts._sanitize_projection_hint(other) != out


def test_repair_render_survives_oversized_hints():
    # Renderer-level defense: even a hint that escaped the source-layer fix
    # must not abort the render (the pre-fix behavior burned the infra retry
    # budget as master_llm_unavailable).
    long_json_hint = (
        "proposal_worker_binding_cannot_fit_minimum_prompt:"
        '{"reserved_selected_contract_chars":11330,"max_provider_chars":-380,'
        '"reserved_runtime_contract_max_chars":2048,"global_cap_chars":13000,'
        '"character_metric":"python_unicode_code_points"}'
    )
    rendered = prompts._render_master_proposal_provider_prompt({
        "planning_context": "Frozen facts.",
        "direction": "mechanism",
        "directive": "one structural mechanism",
        "source_v": 1,
        "next_v": 2,
        "protocol_bootstrap_prepared_only": False,
        "singleton_no_strength": False,
        "source_symbol_index": "policy.py:get_baseline_decision",
        "repair_kind": "schema",
        "projection_hints": [
            "proposal_snapshot_evidence_too_many",
            long_json_hint,
        ],
        "allowed_primaries": [],
        "invocation_id": "3" * 32,
    })
    text = rendered.text
    # The render COMPLETED (pre-fix: ValueError before any provider call) and
    # the leaked JSON payload never reaches the provider prompt. Hints are
    # translated into targeted repair guidance rather than echoed verbatim,
    # so we assert on the payload's absence, not the code's presence.
    assert text
    assert '"reserved_selected_contract_chars"' not in text
    assert "python_unicode_code_points" not in text
