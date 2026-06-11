"""Logic tests for replay_analysis.py — replay data summarization for LLM analysis."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from replay_analysis import _num_public_cards_to_street, extract_street_patterns, summarize_replay_for_analysis


# ── _num_public_cards_to_street ──

class TestNumPublicCardsToStreet:

    def test_preflop(self):
        assert _num_public_cards_to_street(0) == "preflop"

    def test_flop(self):
        assert _num_public_cards_to_street(3) == "flop"

    def test_turn(self):
        assert _num_public_cards_to_street(4) == "turn"

    def test_river(self):
        assert _num_public_cards_to_street(5) == "river"

    def test_unknown(self):
        assert _num_public_cards_to_street(2) == "street_2"

    def test_six_cards(self):
        assert _num_public_cards_to_street(6) == "street_6"


# ── extract_street_patterns ──

class TestExtractStreetPatterns:

    def _make_log(self, player_id, action, public_cards=None, pot=0):
        display = {"pot": pot}
        display["last_action"] = {"player_id": player_id, "action": action}
        display["public_cards"] = public_cards or []
        return {"output": {"display": display}}

    def test_empty_games(self):
        result = extract_street_patterns([], 0)
        assert result == ""

    def test_single_fold_preflop(self):
        games = [{"logs": [self._make_log(0, -1)]}]
        result = extract_street_patterns(games, 0)
        assert "Preflop" in result
        assert "fold=100%" in result

    def test_raise_with_pot(self):
        games = [{"logs": [
            self._make_log(0, 500, public_cards=[1, 2, 3], pot=1000),
        ]}]
        result = extract_street_patterns(games, 0)
        assert "Flop" in result
        assert "raise=" in result
        assert "avg_raise=" in result

    def test_ignores_other_player(self):
        games = [{"logs": [self._make_log(1, -1)]}]
        result = extract_street_patterns(games, 0)
        assert result == ""

    def test_call_on_river(self):
        games = [{"logs": [
            self._make_log(1, 0, public_cards=[1, 2, 3, 4, 5]),
        ]}]
        result = extract_street_patterns(games, 1)
        assert "River" in result
        assert "call=100%" in result

    def test_allin_counted(self):
        games = [{"logs": [
            self._make_log(0, -2, public_cards=[]),
        ]}]
        result = extract_street_patterns(games, 0)
        assert "allin=100%" in result

    def test_mixed_actions(self):
        logs = [
            self._make_log(0, -1, []),     # fold preflop
            self._make_log(0, 500, [1, 2, 3], pot=1000),  # raise flop
            self._make_log(0, 0, [1, 2, 3, 4]),            # call turn
        ]
        games = [{"logs": logs}]
        result = extract_street_patterns(games, 0)
        assert "Preflop" in result
        assert "Flop" in result
        assert "Turn" in result


# ── summarize_replay_for_analysis ──

class TestSummarizeReplayForAnalysis:

    def _make_replay(self, bot0, bot1, games):
        return {"bot0": bot0, "bot1": bot1, "games": games}

    def _make_game(self, winner, bot0_chips=0, bot1_chips=0, logs=None, game_idx=0):
        return {
            "game": game_idx,
            "winner": winner,
            "bot0_chips": bot0_chips,
            "bot1_chips": bot1_chips,
            "logs": logs or [],
        }

    def test_unknown_bot_returns_empty(self):
        replay = self._make_replay("A", "B", [])
        assert summarize_replay_for_analysis(replay, "C") == ""

    def test_empty_games_returns_empty(self):
        replay = self._make_replay("A", "B", [])
        assert summarize_replay_for_analysis(replay, "A") == ""

    def test_basic_summary_with_wins(self):
        games = [
            self._make_game(0, bot0_chips=500, game_idx=0),
            self._make_game(0, bot0_chips=300, game_idx=1),
            self._make_game(1, bot0_chips=-400, game_idx=2),
        ]
        replay = self._make_replay("Alice", "Bob", games)
        summary = summarize_replay_for_analysis(replay, "Alice")
        assert "2W/1L" in summary
        assert "Alice vs Bob" in summary

    def test_draws_tracked(self):
        games = [
            self._make_game(0, bot0_chips=100, game_idx=0),
            self._make_game(-1, bot0_chips=0, game_idx=1),
            self._make_game(1, bot0_chips=-200, game_idx=2),
        ]
        replay = self._make_replay("A", "B", games)
        summary = summarize_replay_for_analysis(replay, "A")
        assert "1W/1D/1L" in summary

    def test_bot1_perspective(self):
        games = [self._make_game(1, bot1_chips=700, game_idx=0)]
        replay = self._make_replay("A", "B", games)
        summary = summarize_replay_for_analysis(replay, "B")
        assert "1W/0L" in summary

    def test_chip_delta_stats(self):
        games = [
            self._make_game(0, bot0_chips=1000, game_idx=0),
            self._make_game(1, bot0_chips=-500, game_idx=1),
        ]
        replay = self._make_replay("A", "B", games)
        summary = summarize_replay_for_analysis(replay, "A")
        assert "avg=" in summary
        assert "best=" in summary
        assert "worst=" in summary

    def test_big_losses_reported(self):
        games = [self._make_game(1, bot0_chips=-8000, game_idx=5)]
        replay = self._make_replay("A", "B", games)
        summary = summarize_replay_for_analysis(replay, "A")
        assert "Big losses" in summary

    def test_multi_game_aggregation(self):
        games = [
            self._make_game(0, bot0_chips=200, game_idx=i)
            for i in range(5)
        ]
        replay = self._make_replay("A", "B", games)
        summary = summarize_replay_for_analysis(replay, "A")
        assert "5W/0L" in summary
        assert "out of 5 games" in summary

    def test_actions_with_display_logs(self):
        log = {"output": {"display": {
            "pot": 500,
            "last_action": {"player_id": 0, "action": -1},
            "public_cards": [],
        }}}
        games = [self._make_game(1, bot0_chips=-500, logs=[log], game_idx=0)]
        replay = self._make_replay("A", "B", games)
        summary = summarize_replay_for_analysis(replay, "A")
        assert "fold=" in summary
