"""Raw official-wire transcripts for the typed national policy runtime probe.

The probe never fabricates the retired request/response JSON state.  Each
scenario starts from an empty ``NativeNationalBot`` and reaches one decision by
feeding the same delimiter-free messages the official EXE sends.  Setup
decisions are typed policy intents; the final decision is produced by the
candidate's real ``policy.py`` through the system-owned runtime.
"""

from __future__ import annotations

import hashlib
import json


RUNTIME_PROBE_SCENARIO_VERSION = 7


DECISION_SCENARIOS = (
    {
        "id": "preflop_sb_premium",
        "messages": (
            "preflop|SMALLBLIND|<0,12><1,12>",
        ),
        "setup_intents": (),
        "expected": {
            "street": "preflop",
            "position": "small_blind",
            "acts_first_postflop": False,
            "to_call": 50,
        },
    },
    {
        "id": "preflop_bb_facing_raise",
        "messages": (
            "preflop|BIGBLIND|<0,11><1,9>",
            "raise 300",
        ),
        "setup_intents": (),
        "expected": {
            "street": "preflop",
            "position": "big_blind",
            "acts_first_postflop": True,
            "to_call": 200,
        },
    },
    {
        "id": "flop_top_pair_facing_bet",
        "messages": (
            "preflop|SMALLBLIND|<0,12><1,8>",
            "call",
            "flop|<1,12><2,6><3,3>",
            "raise 350",
        ),
        "setup_intents": (
            {"kind": "raise", "raise_to": 300},
        ),
        "expected": {
            "street": "flop",
            "position": "small_blind",
            "acts_first_postflop": False,
            "to_call": 350,
        },
    },
    {
        "id": "flop_donk_vs_opponent_pfr",
        "mixing_class": "structural_air_no_hole_draw",
        "messages": (
            # This fixed Q9/A43 identity has no hole-backed draw and lands in
            # the policy's deterministic 10% structural-air mix.  It proves a
            # donk can reach wire without reviving a 100% air-bluff pattern.
            "preflop|BIGBLIND|<2,7><0,10>",
            "raise 300",
            "flop|<0,2><1,12><2,1>",
        ),
        "setup_intents": (
            {"kind": "pass"},
        ),
        "expected": {
            "street": "flop",
            "position": "big_blind",
            "acts_first_postflop": True,
            "to_call": 0,
            "preflop_aggressor": "opponent",
            "street_open": True,
            "can_donk": True,
            "can_delayed_probe": False,
        },
    },
    {
        "id": "flop_donk_control_hero_pfr",
        "messages": (
            "preflop|BIGBLIND|<2,7><0,10>",
            "call",
            "call",
            "flop|<0,2><1,12><2,1>",
        ),
        "setup_intents": (
            {"kind": "raise", "raise_to": 300},
        ),
        "expected": {
            "street": "flop",
            "position": "big_blind",
            "acts_first_postflop": True,
            "to_call": 0,
            "preflop_aggressor": "hero",
            "street_open": True,
            "can_donk": False,
            "can_delayed_probe": False,
        },
    },
    {
        "id": "flop_donk_mixed_check_identity",
        "mixing_class": "structural_air_no_hole_draw",
        "messages": (
            "preflop|BIGBLIND|<0,11><1,10>",
            "raise 300",
            "flop|<2,8><3,5><0,2>",
        ),
        "setup_intents": (
            {"kind": "pass"},
        ),
        "expected": {
            "street": "flop",
            "position": "big_blind",
            "acts_first_postflop": True,
            "to_call": 0,
            "preflop_aggressor": "opponent",
            "street_open": True,
            "can_donk": True,
            "can_delayed_probe": False,
        },
    },
    {
        "id": "turn_responding_to_check",
        "messages": (
            "preflop|SMALLBLIND|<0,12><1,7>",
            "call",
            "flop|<1,12><2,6><3,3>",
            "check",
            "turn|<0,1>",
            "check",
        ),
        "setup_intents": (
            {"kind": "raise", "raise_to": 300},
            {"kind": "pass"},
        ),
        "expected": {
            "street": "turn",
            "position": "small_blind",
            "acts_first_postflop": False,
            "to_call": 0,
            "responding_to_check": True,
            "pass_wire_kind": "call",
        },
    },
    {
        "id": "river_facing_large_bet",
        "messages": (
            "preflop|SMALLBLIND|<0,9><1,5>",
            "call",
            "flop|<1,12><2,6><3,3>",
            "check",
            "turn|<0,1>",
            "raise 3200",
            "river|<2,7>",
            "raise 4200",
        ),
        "setup_intents": (
            {"kind": "raise", "raise_to": 300},
            {"kind": "pass"},
            {"kind": "pass"},
        ),
        "expected": {
            "street": "river",
            "position": "small_blind",
            "acts_first_postflop": False,
            "to_call": 4200,
        },
    },
    {
        "id": "turn_delayed_probe_vs_opponent_pfr",
        "mixing_class": "structural_air_no_hole_draw",
        "messages": (
            # QJ on T764 has no hole-backed four-card straight or flush draw;
            # this fixed identity is selected by the bounded structural mix.
            "preflop|BIGBLIND|<0,9><0,10>",
            "raise 300",
            "flop|<2,8><3,5><0,2>",
            # The official EXE may suppress the opponent's street-closing
            # pass/call and jump directly to the next street.
            "turn|<1,4>",
        ),
        "setup_intents": (
            {"kind": "pass"},
            {"kind": "pass"},
        ),
        "expected": {
            "street": "turn",
            "position": "big_blind",
            "acts_first_postflop": True,
            "to_call": 0,
            "preflop_aggressor": "opponent",
            "street_open": True,
            "can_donk": False,
            "can_delayed_probe": True,
            "previous_street_checked_through": True,
            "previous_street_opponent_checked_back": True,
            "inferred_boundary": "street:turn",
        },
    },
    {
        "id": "turn_delayed_probe_control_hero_pfr",
        "messages": (
            "preflop|BIGBLIND|<0,9><0,10>",
            "call",
            "call",
            "flop|<2,8><3,5><0,2>",
            # Same omitted closer boundary as the positive control above.
            "turn|<1,4>",
        ),
        "setup_intents": (
            {"kind": "raise", "raise_to": 300},
            {"kind": "pass"},
        ),
        "expected": {
            "street": "turn",
            "position": "big_blind",
            "acts_first_postflop": True,
            "to_call": 0,
            "preflop_aggressor": "hero",
            "street_open": True,
            "can_donk": False,
            "can_delayed_probe": False,
            "previous_street_checked_through": True,
            "previous_street_opponent_checked_back": True,
            "inferred_boundary": "street:turn",
        },
    },
    {
        "id": "turn_delayed_probe_mixed_check_identity",
        "mixing_class": "structural_air_no_hole_draw",
        "messages": (
            "preflop|BIGBLIND|<0,11><1,10>",
            "raise 300",
            "flop|<2,8><3,5><0,2>",
            "turn|<1,4>",
        ),
        "setup_intents": (
            {"kind": "pass"},
            {"kind": "pass"},
        ),
        "expected": {
            "street": "turn",
            "position": "big_blind",
            "acts_first_postflop": True,
            "to_call": 0,
            "preflop_aggressor": "opponent",
            "street_open": True,
            "can_donk": False,
            "can_delayed_probe": True,
            "previous_street_checked_through": True,
            "previous_street_opponent_checked_back": True,
            "inferred_boundary": "street:turn",
        },
    },
)


LINE_SCENARIO_PAIRS = (
    {
        "dimension": "donk",
        "positive": "flop_donk_vs_opponent_pfr",
        "negative": "flop_donk_control_hero_pfr",
        "mixed_identity": "flop_donk_mixed_check_identity",
        "flag": "can_donk",
    },
    {
        "dimension": "delayed_probe",
        "positive": "turn_delayed_probe_vs_opponent_pfr",
        "negative": "turn_delayed_probe_control_hero_pfr",
        "mixed_identity": "turn_delayed_probe_mixed_check_identity",
        "flag": "can_delayed_probe",
    },
)


def scenario_bank_digest() -> str:
    payload = {
        "version": RUNTIME_PROBE_SCENARIO_VERSION,
        "scenarios": DECISION_SCENARIOS,
        "line_pairs": LINE_SCENARIO_PAIRS,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


RUNTIME_PROBE_SCENARIO_DIGEST = scenario_bank_digest()
