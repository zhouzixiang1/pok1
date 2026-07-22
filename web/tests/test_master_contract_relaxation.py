"""Regression tests for the three Master contract relaxation fixes (Jul 2026).

① focus_id: master_plan_executable_contract_text no longer leaks focus_id
   literals when selected_focus=None.
② guidance: _proposal_schema_repair_guidance covers six extra error tokens.
③ measurement: _proposal_measurement_contract_valid accepts case-normalized
   input (from _parsed_proposal_measurement .lower() revert).
"""

import pytest

from agent_master import (
    _proposal_schema_repair_guidance,
    _proposal_measurement_contract_valid,
)
from output_schema import (
    master_plan_executable_contract_text,
    RUNTIME_CONTRACT_REQUIRED_SECTIONS_BY_FOCUS,
    NATIONAL_POLICY_FOCUS_ID,
)


# ═══════════════════════════════════════════════════════════════════════════
# ① focus_id: no leak when selected_focus=None; only selected when present
# ═══════════════════════════════════════════════════════════════════════════

def test_focus_id_none_leaks_no_literal():
    """selected_focus=None must not surface any focus_id in the contract text."""
    text = master_plan_executable_contract_text(selected_focus=None)
    for focus_id in RUNTIME_CONTRACT_REQUIRED_SECTIONS_BY_FOCUS:
        assert focus_id not in text, (
            f"focus_id {focus_id!r} leaked when selected_focus=None"
        )


def test_focus_id_present_only_selected():
    """When a focus is selected only that focus_id must appear."""
    selected = {"focus_id": NATIONAL_POLICY_FOCUS_ID}
    text = master_plan_executable_contract_text(selected_focus=selected)
    assert NATIONAL_POLICY_FOCUS_ID in text
    for focus_id in RUNTIME_CONTRACT_REQUIRED_SECTIONS_BY_FOCUS:
        if focus_id != NATIONAL_POLICY_FOCUS_ID:
            assert focus_id not in text, (
                f"unselected focus_id {focus_id!r} leaked"
            )


def test_focus_id_none_backward_compat():
    """Calling the function with no argument (default None) must match the
    explicit None path."""
    text_default = master_plan_executable_contract_text()
    text_explicit = master_plan_executable_contract_text(selected_focus=None)
    assert text_default == text_explicit


# ═══════════════════════════════════════════════════════════════════════════
# ② guidance: six previously uncovered error tokens now get instruction
# ═══════════════════════════════════════════════════════════════════════════

_GUIDANCE_KWARGS = dict(require_snapshot_evidence=False)


def test_guidance_json_object_required():
    guidance = _proposal_schema_repair_guidance(
        ("proposal_json_object_required",), **_GUIDANCE_KWARGS,
    )
    assert guidance and "json" in guidance.lower() or "object" in guidance.lower()


def test_guidance_schema_mismatch():
    guidance = _proposal_schema_repair_guidance(
        ("proposal_schema_mismatch:abc123",), **_GUIDANCE_KWARGS,
    )
    assert guidance and "schema_version" in guidance


def test_guidance_execution_mode_mismatch():
    guidance = _proposal_schema_repair_guidance(
        ("proposal_execution_mode_mismatch:abc123",), **_GUIDANCE_KWARGS,
    )
    assert guidance and "execution_mode" in guidance


def test_guidance_root_scoped_unknown_leaf():
    guidance = _proposal_schema_repair_guidance(
        ("proposal_mechanism_root_scoped_unknown_leaf:opponent.rates:fold_to_raise",),
        **_GUIDANCE_KWARGS,
    )
    assert guidance and "root" in guidance.lower() and "leaf" in guidance.lower()


def test_guidance_mechanism_target_invalid():
    guidance = _proposal_schema_repair_guidance(
        ("proposal_mechanism_target_invalid",), **_GUIDANCE_KWARGS,
    )
    assert guidance and ("dot target" in guidance.lower() or "mechanism_target" in guidance.lower())


def test_guidance_target_files_invalid():
    guidance = _proposal_schema_repair_guidance(
        ("proposal_target_files_invalid",), **_GUIDANCE_KWARGS,
    )
    assert guidance and "policy.py" in guidance


def test_guidance_still_capped_at_four():
    """More than four hints must not produce more than four lines."""
    many = tuple(
        f"proposal_reachable_chain:{i}" for i in range(10)
    )
    guidance = _proposal_schema_repair_guidance(many, require_snapshot_evidence=False)
    lines = [line for line in guidance.split("\n") if line.startswith("- ")]
    assert len(lines) <= 4


# ═══════════════════════════════════════════════════════════════════════════
# ③ measurement: case-normalized input accepted after .lower() revert
# ═══════════════════════════════════════════════════════════════════════════

_UPPERCASE_MEASUREMENT = (
    "TARGET=national_v143; PRIMARY=COMPLETE_70_HAND_WLD; "
    "EXPECTED_DELTA=0.03; SAMPLES=>=30_COMPLETE_MATCHES; "
    "UNCERTAINTY=WILSON_WLD_INTERVAL; SECONDARY=net_chip_ci"
)


def test_measurement_uppercase_now_accepted():
    """After the .lower() revert, uppercase literals are normalized and must
    match the lowercase contract constants."""
    assert _proposal_measurement_contract_valid(
        _UPPERCASE_MEASUREMENT, "frozen_strength_snapshot",
    ) is True


def test_measurement_mixed_case_accepted():
    mixed = (
        "target=national_v143; PRIMARY=complete_70_hand_wld; "
        "expected_delta=0.01; samples=>=30_COMPLETE_MATCHES; "
        "uncertainty=WILSON_WLD_INTERVAL; secondary=net_chip_ci"
    )
    assert _proposal_measurement_contract_valid(
        mixed, "singleton_parent_no_strength",
    ) is True


def test_measurement_canonical_still_accepted():
    """The canonical lowercase form (as in the prompt template) still passes."""
    canonical = (
        "target=national_v143; primary=complete_70_hand_wld; "
        "expected_delta=0.03; samples=>=30_complete_matches; "
        "uncertainty=wilson_wld_interval; secondary=net_chip_ci"
    )
    assert _proposal_measurement_contract_valid(
        canonical, "frozen_strength_snapshot",
    ) is True


def test_measurement_nonsense_still_rejected():
    """Nonsense values are still rejected even after case normalization."""
    assert not _proposal_measurement_contract_valid(
        "target=national_v143; primary=imagination; expected_delta=0.03; "
        "samples=>=30_complete_matches; uncertainty=wilson_wld_interval; "
        "secondary=net_chip_ci",
        "frozen_strength_snapshot",
    )
