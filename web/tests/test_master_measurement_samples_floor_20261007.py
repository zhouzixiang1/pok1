"""Bare sample counts >= 30 are floor-equivalent measurement spellings.

The proposal measurement contract's ``samples`` slot is a strength-sample
FLOOR declaration, not a plan: downstream precommit/elo_daemon recompute
strength from real matches and never read the field (docstring on
``_parsed_proposal_measurement``).  A model writing ``samples=40_complete_matches``
declares a count that EXCEEDS the 30 floor, yet the floor regex rejected it
(233 historical ``proposal_measurement_contract_invalid`` hint mentions),
burning the one schema-retry budget each scout has — the retry then cannot
fix the next error (the documented fix-one-break-next oscillation).

Equivalence widening only: counts below the floor, leading zeros, and
non-integer prose still fail.
"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

av = importlib.import_module("agent_master_validation")


def _valid_samples(value: str) -> bool:
    return av._proposal_samples_floor_matches(value)


def test_bare_counts_at_or_above_floor_pass():
    assert _valid_samples("40_complete_matches")
    assert _valid_samples("99_complete_matches")
    assert _valid_samples("100_complete_matches")
    assert _valid_samples("40")
    assert _valid_samples("30")


def test_counts_below_floor_still_fail():
    assert not _valid_samples("29_complete_matches")
    assert not _valid_samples("3_complete_matches")
    assert not _valid_samples("29")
    assert not _valid_samples("03_complete_matches")
    assert not _valid_samples("0_complete_matches")


def test_existing_floor_spellings_unchanged():
    assert _valid_samples(">=30_complete_matches")
    assert _valid_samples(">= 30 complete matches")
    assert _valid_samples("≥30")
    assert _valid_samples("at_least_30_complete_matches")
    assert _valid_samples("at least 30")
    assert _valid_samples("30_complete_matches")
    assert not _valid_samples(">=29_complete_matches")
    assert not _valid_samples(">30_complete_matches")
    assert not _valid_samples("some matches")


def test_v527_real_measurement_string_passes_frozen_mode():
    # The exact string that killed v527's mechanism scout on 2026-10-07
    # (only difference from a passing string: samples=40_complete_matches).
    measurement = (
        "target=national_cloud_v185; primary=complete_70_hand_wld; "
        "expected_delta=0.03; samples=40_complete_matches; "
        "uncertainty=wilson_wld_interval; secondary=net_chip_ci"
    )
    assert av._proposal_measurement_contract_valid(
        measurement, "frozen_strength_snapshot"
    )


def test_fresh_strict_control_mode_still_requires_canonical_measurement():
    # fresh_strict_control_no_strength compares against the canonical string
    # wholesale, not the floor regex; a bare-count spelling must still fail.
    measurement = (
        "target=national_cloud_v1; primary=complete_70_hand_wld; "
        "expected_delta=0.03; samples=40_complete_matches; "
        "uncertainty=wilson_wld_interval; secondary=net_chip_ci"
    )
    assert not av._proposal_measurement_contract_valid(
        measurement, "fresh_strict_control_no_strength"
    )
