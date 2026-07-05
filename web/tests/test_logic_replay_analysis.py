"""Logic tests for replay_analysis.py — replay data summarization for LLM analysis."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from replay_analysis import (
    _num_public_cards_to_street,
    extract_behavior_fingerprint,
    extract_replay_evidence_for_analysis,
    extract_street_patterns,
    summarize_replay_for_analysis,
)


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

    def test_national_events_feed_behavior_fingerprint(self):
        games = [{
            "events_tail": [
                {
                    "type": "action",
                    "player_idx": 1,
                    "action": "raise",
                    "stage": "preflop",
                    "amount": 300,
                    "pot": 450,
                },
                {"type": "action", "player_idx": 1, "action": "call", "stage": "river"},
                {"type": "action", "player_idx": 0, "action": "fold", "stage": "river"},
            ],
        }]
        fp = extract_behavior_fingerprint(games, 1)
        assert fp["total_actions"] == 2
        assert fp["per_street_freq"]["preflop"]["raise"] == 1.0
        assert fp["per_street_freq"]["river"]["call"] == 1.0
        assert fp["vpip"] == 1.0
        assert fp["call_down_rate"] == 1.0


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

    def test_national_native_summary_uses_net_chips(self):
        games = [
            {
                "bot_a": "A",
                "bot_b": "B",
                "repeat": 0,
                "net_chips_a": 13470,
                "net_chips_b": -13470,
                "per_player": {
                    "A": {"earnings": 13470},
                    "B": {"earnings": -13470},
                },
            },
            {
                "bot_a": "A",
                "bot_b": "B",
                "repeat": 1,
                "net_chips_a": -16668,
                "net_chips_b": 16668,
                "per_player": {
                    "A": {"earnings": -16668},
                    "B": {"earnings": 16668},
                },
            },
        ]
        replay = self._make_replay("A", "B", games)
        summary = summarize_replay_for_analysis(replay, "B")
        assert "1W/1L" in summary
        assert "0W/2D/0L" not in summary
        assert "best=16668" in summary
        assert "worst=-13470" in summary

    def test_national_native_events_feed_action_summary(self):
        games = [{
            "bot_a": "A",
            "bot_b": "B",
            "net_chips_a": -1000,
            "net_chips_b": 1000,
            "events_tail": [
                {
                    "type": "action",
                    "player_idx": 1,
                    "action": "raise",
                    "stage": "preflop",
                    "amount": 300,
                    "pot": 450,
                },
                {
                    "type": "action",
                    "player_idx": 1,
                    "action": "check",
                    "stage": "flop",
                    "pot": 600,
                },
                {
                    "type": "action",
                    "player_idx": 1,
                    "action": "fold",
                    "stage": "turn",
                    "pot": 1200,
                },
            ],
        }]
        replay = self._make_replay("A", "B", games)
        summary = summarize_replay_for_analysis(replay, "B")
        assert "Actions:" in summary
        assert "fold=1" in summary
        assert "call=1" in summary
        assert "raise=1" in summary
        assert "Per-street actions" in summary
        assert "Preflop" in summary
        assert "Flop" in summary
        assert "Turn" in summary

    def test_extract_replay_evidence_for_analysis(self):
        games = [{
            "bot_a": "A",
            "bot_b": "B",
            "repeat": 0,
            "net_chips_a": -6000,
            "net_chips_b": 6000,
            "events_tail": [
                {"type": "action", "player_idx": 0, "action": "fold", "stage": "flop", "pot": 800},
                {"type": "action", "player_idx": 0, "action": "raise", "stage": "river", "amount": 1800, "pot": 1200},
            ],
        }]
        replay = self._make_replay("A", "B", games)

        evidence = extract_replay_evidence_for_analysis(replay, "A", match_id="m-evidence")

        assert evidence["evidence_id"].startswith("ev_")
        assert evidence["match_id"] == "m-evidence"
        assert evidence["bot"] == "A"
        assert evidence["opponent"] == "B"
        assert evidence["sample_n"] == 1
        assert evidence["avg_delta"] == -6000
        assert evidence["actions"]["fold"] == 1
        assert evidence["actions"]["raise"] == 1
        assert "big_pot_losses" in evidence["spot_tags"]
