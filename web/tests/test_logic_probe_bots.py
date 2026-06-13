"""Smoke + behavior tests for the 4 exploitability probe bots.

Probe bots reconstruct judge state from a `req` dict:
    {my_id, dealer_id, history:[{round, player_id, action_type, action}],
     public_cards, my_chips}

These tests are intentionally lightweight — the probe bots are ADVISORY inputs
to exploitability scoring, not part of the rated pipeline. They verify the
bots (a) import and return a valid action type, and (b) the check_raiser
rewrite actually generates preflop raises (the confirmed bug: the old code
called/checked on every preflop path and folded when to_call>=my_chips).
"""

import sys
from pathlib import Path

import pytest

PROBE_DIR = Path(__file__).resolve().parent.parent / "core" / "probe_bots"
sys.path.insert(0, str(PROBE_DIR))

import check_raiser  # noqa: E402
import min_bettor  # noqa: E402
import overbettor  # noqa: E402
import always_caller  # noqa: E402


def _req(my_id, dealer_id, history, public_cards=None, my_chips=20000):
    return {
        "my_id": my_id,
        "dealer_id": dealer_id,
        "history": history,
        "public_cards": public_cards or [],
        "my_chips": my_chips,
    }


def _act(action):
    """All legal engine/judge action ints: -2 allin, -1 fold, 0 call/check, >0 raise-to."""
    return isinstance(action, int) and (action == -2 or action == -1 or action >= 0)


# ── 1. All four probes return a valid action (smoke) ──

class TestProbeSmoke:
    def test_check_raiser_valid_action(self):
        # SB first to act preflop
        r = _req(0, 0, history=[])
        assert _act(check_raiser.get_action(r))

    def test_min_bettor_valid_action(self):
        r = _req(0, 0, history=[])
        assert _act(min_bettor.get_action(r))

    def test_overbettor_valid_action(self):
        r = _req(0, 0, history=[])
        assert _act(overbettor.get_action(r))

    def test_always_caller_valid_action(self):
        r = _req(0, 0, history=[])
        assert _act(always_caller.get_action(r))


# ── 2. check_raiser now raises preflop (the rewrite's whole point) ──

class TestCheckRaiserPreflopAggression:
    def test_bb_facing_limb_opens_for_raise(self):
        """BB (my_id=1, dealer=0) facing a SB limp should RAISE, not call.

        Old behavior: returned 0 (call) on every preflop path. The rewrite
        open-raises when to_call==0 (BB facing a limp).
        """
        # SB (pid 0) completed/limped preflop
        history = [{"round": 0, "player_id": 0, "action_type": "call", "action": 0}]
        r = _req(1, 0, history=history)  # BB to act
        action = check_raiser.get_action(r)
        assert action > 0, f"check_raiser should open-raise preflop vs a limp, got {action}"

    def test_facing_allin_pushes_not_folds(self):
        """Facing an all-in raise bigger than stack -> all-in, never fold.

        Old behavior: returned -1 (fold) when to_call>=my_chips. The rewrite
        returns -2 (call it off) since a probe must exercise the axis.
        """
        # Opponent (pid 0) shoves 20000 preflop; bot is BB with 20000
        history = [{"round": 0, "player_id": 0, "action_type": "allin", "action": -2}]
        r = _req(1, 0, history=history, my_chips=20000)
        action = check_raiser.get_action(r)
        # Must not fold; either calls (0) or all-ins (-2). The rewrite favors -2.
        assert action != -1, f"check_raiser must not fold vs all-in, got {action}"


# ── 3. always_caller never folds ──

class TestAlwaysCallerNeverFolds:
    def test_calls_simple_scenario(self):
        r = _req(0, 0, history=[])
        assert always_caller.get_action(r) != -1

    def test_allin_when_cant_afford_call(self):
        """Facing a bet larger than stack -> all-in, never fold."""
        # Opponent bets 20000; bot has only 500
        history = [{"round": 0, "player_id": 0, "action_type": "raise", "action": 20000}]
        r = _req(1, 0, history=history, my_chips=500)
        action = always_caller.get_action(r)
        assert action == -2, f"always_caller should all-in when can't afford call, got {action}"
