"""Logic tests for bot_action_stats.py — replay parsing and stat computation."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from bot_action_stats import extract_hands_from_replay, _classify_hand_strength, compute_bot_action_stats


# ── extract_hands_from_replay ──

class TestExtractHandsFromReplay:

    def _make_replay(self, players=None, hands=None):
        return {
            "players": players or ["Alice", "Bob"],
            "hands": hands or [],
        }

    def _make_hand(self, hole_cards=None, preflop=None, flop=None, turn=None,
                   river=None, showdown=None, settlement=None):
        hand = {"hole_cards": hole_cards or {}}
        for stage in ("preflop", "flop", "turn", "river", "showdown"):
            val = locals().get(stage)
            if val is not None:
                hand[stage] = val
        if settlement is not None:
            hand["settlement"] = settlement
        return hand

    def test_empty_players(self):
        result = extract_hands_from_replay({"players": []})
        assert result == []

    def test_empty_hands(self):
        result = extract_hands_from_replay(self._make_replay())
        assert result == []

    def test_no_players_key(self):
        result = extract_hands_from_replay({"hands": []})
        assert result == []

    def test_json_string_input(self):
        replay = self._make_replay(
            players=["A", "B"],
            hands=[self._make_hand(
                hole_cards={"A": [0, 1], "B": [2, 3]},
                preflop={"actions": [{"player": "A", "action": "fold", "amount": 0}]},
            )],
        )
        result = extract_hands_from_replay(json.dumps(replay))
        assert len(result) == 1
        assert result[0]["actions"][0]["action"] == "fold"

    def test_single_hand_preflop_only(self):
        hand = self._make_hand(
            hole_cards={"Alice": [48, 44], "Bob": [50, 46]},
            preflop={"actions": [
                {"player": "Alice", "action": "raise", "amount": 200},
                {"player": "Bob", "action": "call", "amount": 200},
            ], "pot": 400},
        )
        result = extract_hands_from_replay(self._make_replay(hands=[hand]))
        assert len(result) == 1
        assert result[0]["stages"] == ["preflop"]
        assert result[0]["pot"] == 400
        assert len(result[0]["actions"]) == 2
        assert result[0]["hole_cards"]["Alice"] == [48, 44]

    def test_full_hand_with_showdown(self):
        hand = self._make_hand(
            hole_cards={"Alice": [48, 44], "Bob": [50, 46]},
            preflop={"actions": [
                {"player": "Alice", "action": "raise", "amount": 200},
            ], "pot": 200},
            flop={"actions": [], "pot": 400, "community": [0, 4, 8]},
            turn={"actions": [], "pot": 600, "community": [12]},
            river={"actions": [], "pot": 800, "community": [16]},
            showdown={"winner": "Alice", "win_amount": 800},
        )
        result = extract_hands_from_replay(self._make_replay(hands=[hand]))
        h = result[0]
        assert h["stages"] == ["preflop", "flop", "turn", "river", "showdown"]
        assert h["showdown"] is True
        assert h["winner"] == "Alice"
        assert h["win_amount"] == 800
        assert h["community"]["flop"] == [0, 4, 8]

    def test_settlement_winner_fallback(self):
        hand = self._make_hand(
            hole_cards={"Alice": [0, 1], "Bob": [2, 3]},
            preflop={"actions": [
                {"player": "Alice", "action": "raise", "amount": 200},
                {"player": "Bob", "action": "fold", "amount": 0},
            ]},
            settlement={"Alice": 200, "Bob": -200},
        )
        result = extract_hands_from_replay(self._make_replay(hands=[hand]))
        h = result[0]
        assert h["winner"] == "Alice"
        assert h["win_amount"] == 200
        assert h["pot"] == 200  # sum of positive settlement values

    def test_multiple_hands(self):
        hands = [
            self._make_hand(
                hole_cards={"Alice": [0, 1], "Bob": [2, 3]},
                preflop={"actions": [{"player": "Alice", "action": "fold", "amount": 0}]},
            ),
            self._make_hand(
                hole_cards={"Alice": [4, 5], "Bob": [6, 7]},
                preflop={"actions": [{"player": "Bob", "action": "fold", "amount": 0}]},
            ),
        ]
        result = extract_hands_from_replay(self._make_replay(hands=hands))
        assert len(result) == 2


# ── _classify_hand_strength ──

class TestClassifyHandStrength:

    def test_no_showdown(self):
        hand = {"showdown": False}
        assert _classify_hand_strength(hand, "Alice") == "unknown"

    def test_won_at_showdown(self):
        hand = {"showdown": True, "winner": "Alice"}
        assert _classify_hand_strength(hand, "Alice") == "made"

    def test_lost_at_showdown(self):
        hand = {"showdown": True, "winner": "Bob"}
        assert _classify_hand_strength(hand, "Alice") == "air"

    def test_showdown_no_winner(self):
        hand = {"showdown": True, "winner": None}
        assert _classify_hand_strength(hand, "Alice") == "air"


# ── compute_bot_action_stats ──

class TestComputeBotActionStats:

    def _make_replay_with_hand(self, actions, players=None, extra_hand_data=None):
        """Build a replay dict with a single hand containing given preflop actions."""
        ps = players or ["Alice", "Bob"]
        hand = {
            "hole_cards": {p: [i, i + 1] for i, p in enumerate(ps)},
            "preflop": {"actions": actions, "pot": 200},
        }
        if extra_hand_data:
            hand.update(extra_hand_data)
        return {"players": ps, "hands": [hand]}

    def test_nonexistent_dir(self):
        result = compute_bot_action_stats("Alice", "/nonexistent/path")
        assert result == {}

    def test_empty_dir(self, tmp_path):
        result = compute_bot_action_stats("Alice", str(tmp_path))
        assert result["total_hands"] == 0

    def test_basic_vpip_pfr(self, tmp_path):
        replay = self._make_replay_with_hand([
            {"player": "Alice", "action": "raise", "amount": 200},
            {"player": "Bob", "action": "call", "amount": 200},
        ])
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["total_hands"] == 1
        assert stats["vpip"] == 1.0
        assert stats["pfr"] == 1.0

    def test_vpip_call_only(self, tmp_path):
        replay = self._make_replay_with_hand([
            {"player": "Bob", "action": "raise", "amount": 200},
            {"player": "Alice", "action": "call", "amount": 200},
        ])
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["vpip"] == 1.0
        assert stats["pfr"] == 0.0

    def test_fold_to_3bet(self, tmp_path):
        replay = self._make_replay_with_hand([
            {"player": "Alice", "action": "raise", "amount": 200},
            {"player": "Bob", "action": "raise", "amount": 600},
            {"player": "Alice", "action": "fold", "amount": 0},
        ])
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["fold_to_3bet"] == 1.0

    def test_aggression_freq(self, tmp_path):
        replay = self._make_replay_with_hand([
            {"player": "Alice", "action": "raise", "amount": 200},
            {"player": "Bob", "action": "call", "amount": 200},
        ])
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["aggression_freq"] == 1.0  # 1 raise, 0 passive

    def test_multiple_replay_files(self, tmp_path):
        for i in range(3):
            replay = self._make_replay_with_hand([
                {"player": "Alice", "action": "raise", "amount": 200},
                {"player": "Bob", "action": "call", "amount": 200},
            ])
            (tmp_path / f"r{i}.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["total_hands"] == 3

    def test_corrupted_json_skipped(self, tmp_path):
        (tmp_path / "bad.json").write_text("not json")
        replay = self._make_replay_with_hand([
            {"player": "Alice", "action": "call", "amount": 200},
        ])
        (tmp_path / "good.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["total_hands"] == 1

    def test_bot_not_in_hand(self, tmp_path):
        replay = {"players": ["Bob", "Carol"], "hands": [{
            "hole_cards": {"Bob": [0, 1], "Carol": [2, 3]},
            "preflop": {"actions": [{"player": "Bob", "action": "fold", "amount": 0}]},
        }]}
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["total_hands"] == 1  # hand counted but no bot actions
        assert stats["vpip"] == 0.0

    def test_showdown_win_rate(self, tmp_path):
        replay = {
            "players": ["Alice", "Bob"],
            "hands": [{
                "hole_cards": {"Alice": [0, 1], "Bob": [2, 3]},
                "preflop": {"actions": [
                    {"player": "Alice", "action": "call", "amount": 100},
                ]},
                "showdown": {"winner": "Alice", "win_amount": 200},
            }],
        }
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["showdown_win"] == 1.0

    def test_wtsd(self, tmp_path):
        """WTSD = went to showdown / saw flop."""
        replay = {
            "players": ["Alice", "Bob"],
            "hands": [{
                "hole_cards": {"Alice": [0, 1], "Bob": [2, 3]},
                "preflop": {"actions": [
                    {"player": "Alice", "action": "call", "amount": 100},
                ]},
                "flop": {"actions": [
                    {"player": "Alice", "action": "check", "amount": 0},
                ], "community": [4, 8, 12]},
                "showdown": {"winner": "Bob"},
            }],
        }
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["wtsd"] == 1.0

    def test_stats_keys_complete(self, tmp_path):
        replay = self._make_replay_with_hand([
            {"player": "Alice", "action": "raise", "amount": 200},
            {"player": "Bob", "action": "fold", "amount": 0},
        ])
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        expected_keys = {
            "total_hands", "vpip", "pfr", "fold_to_3bet", "flop_cbet",
            "turn_barrel", "river_value_bet", "river_bluff", "fold_to_river_bet",
            "showdown_win", "avg_won_pot", "avg_lost_pot", "wtsd", "aggression_freq",
        }
        assert expected_keys == set(stats.keys())
