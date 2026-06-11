"""Logic tests for commentary.py — deterministic match commentary generation."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from commentary import _card_name, _action_name, generate_match_commentary


# ── _card_name ──

class TestCardName:
    """Card encoding: number = card // 4 + 2 (2-14), suit = card % 4 (0=h,1=d,2=s,3=c)."""

    def test_ace_of_hearts(self):
        assert _card_name(48) == "A♥"  # 48//4+2=14, 48%4=0

    def test_two_of_hearts(self):
        assert _card_name(0) == "2♥"  # 0//4+2=2, 0%4=0

    def test_ace_of_clubs(self):
        assert _card_name(51) == "A♣"  # 51//4+2=14, 51%4=3

    def test_king_of_spades(self):
        assert _card_name(46) == "K♠"  # 46//4+2=13, 46%4=2

    def test_ten_of_diamonds(self):
        assert _card_name(33) == "T♦"  # 33//4+2=10, 33%4=1

    def test_none_input(self):
        assert _card_name(None) == "?"


# ── _action_name ──

class TestActionName:

    def test_call(self):
        assert _action_name(0) == "calls"

    def test_fold(self):
        assert _action_name(-1) == "folds"

    def test_all_in(self):
        assert _action_name(-2) == "ALL IN"

    def test_raise(self):
        assert _action_name(500) == "raises to 500"

    def test_none(self):
        assert _action_name(None) == "act"

    def test_raise_one(self):
        assert _action_name(1) == "raises to 1"


# ── generate_match_commentary ──

class TestGenerateMatchCommentary:

    def _make_log(self, pot=0, last_action=None, public_cards=None,
                  round_num=None, player_cards=None):
        """Helper to build a single log entry dict."""
        display = {"pot": pot}
        if last_action is not None:
            display["last_action"] = last_action
        if public_cards is not None:
            display["public_cards"] = public_cards
        if round_num is not None:
            display["round"] = round_num
        if player_cards is not None:
            display["player_cards"] = player_cards
        return {"output": {"display": display}}

    def test_empty_games(self):
        result = generate_match_commentary({"games": [], "bot0": "A", "bot1": "B"})
        assert result == {}

    def test_no_games_key(self):
        result = generate_match_commentary({"bot0": "A", "bot1": "B"})
        assert result == {}

    def test_single_game_no_logs(self):
        replay = {
            "bot0": "Alice", "bot1": "Bob",
            "games": [{"game": 0, "winner": 0, "bot0_chips": 500, "logs": []}],
        }
        result = generate_match_commentary(replay)
        assert "0" in result
        assert "Alice wins" in result["0"]

    def test_big_win_triggers_big_annotation(self):
        replay = {
            "bot0": "Alice", "bot1": "Bob",
            "games": [{"game": 1, "winner": 0, "bot0_chips": 15000, "logs": []}],
        }
        result = generate_match_commentary(replay)
        assert "wins big" in result["1"]

    def test_draw_game(self):
        """Draws (winner=-1) are excluded from the result annotation unless
        there was an all-in or big pot. A quiet draw yields 'No notable events'."""
        replay = {
            "bot0": "Alice", "bot1": "Bob",
            "games": [{"game": 2, "winner": -1, "bot0_chips": 0, "logs": []}],
        }
        result = generate_match_commentary(replay)
        # No all-in and no big win, so the draw result line is skipped
        assert result["2"] == "No notable events"

    def test_draw_with_allin(self):
        """Draws with an all-in event still get the all-in showdown annotation."""
        log = self._make_log(
            pot=5000,
            last_action={"player_id": 0, "action": -2},
        )
        replay = {
            "bot0": "Alice", "bot1": "Bob",
            "games": [{"game": 9, "winner": -1, "bot0_chips": 0, "logs": [log]}],
        }
        result = generate_match_commentary(replay)
        assert "Draw" in result["9"]

    def test_all_in_event(self):
        log = self._make_log(
            pot=10000,
            last_action={"player_id": 0, "action": -2},
        )
        replay = {
            "bot0": "Alice", "bot1": "Bob",
            "games": [{"game": 3, "winner": 0, "bot0_chips": 8000, "logs": [log]}],
        }
        result = generate_match_commentary(replay)
        assert "ALL IN" in result["3"]

    def test_raise_with_big_pot(self):
        log = self._make_log(
            pot=6000,
            last_action={"player_id": 1, "action": 3000},
        )
        replay = {
            "bot0": "Alice", "bot1": "Bob",
            "games": [{"game": 4, "winner": 1, "bot0_chips": -6000, "logs": [log]}],
        }
        result = generate_match_commentary(replay)
        assert "raises to 3000" in result["4"]

    def test_showdown_detection(self):
        """Showdown when round=4 and all-in present → shows 'all-in showdown' text."""
        log_action = self._make_log(
            pot=8000,
            last_action={"player_id": 0, "action": -2},
        )
        log_showdown = self._make_log(round_num=4, pot=8000)
        replay = {
            "bot0": "Alice", "bot1": "Bob",
            "games": [{
                "game": 5, "winner": 1, "bot0_chips": -5000,
                "logs": [log_action, log_showdown],
            }],
        }
        result = generate_match_commentary(replay)
        assert "all-in showdown" in result["5"]

    def test_player_cards_displayed(self):
        log = self._make_log(
            player_cards=[[48, 44], [50, 46]],  # A♥ K♥, A♠ K♠
            pot=200,
        )
        replay = {
            "bot0": "Alice", "bot1": "Bob",
            "games": [{"game": 6, "winner": 0, "bot0_chips": 200, "logs": [log]}],
        }
        result = generate_match_commentary(replay)
        assert "A♥" in result["6"]
        assert "A♠" in result["6"]

    def test_no_notable_events(self):
        """Game with no meaningful display data."""
        replay = {
            "bot0": "X", "bot1": "Y",
            "games": [{"game": 7, "logs": [{"output": {}}]}],
        }
        result = generate_match_commentary(replay)
        assert result["7"] == "No notable events"
