"""Logic tests for bot_action_stats.py — action extraction & stat computation.

Fixtures mirror the REAL replay shape produced by elo_daemon.save_match_replay /
engine/battle.mirror_battle:

    {
      "bot0": <name>, "bot1": <name>,
      "games": [{"game": 0, "mirror": False, "winner": 0,
                  "bot0_chips": 1.0, "bot1_chips": -1.0,
                  "logs": [ <interleaved request/response entries> ]}]
    }

Each request entry:
    {"output": {"command": "request",
                 "content": {"<pid>": {<player-facing state>}},
                 "display": {"round": 0..3, "round_player_bet": [b0, b1],
                              "matchdata": {"hand": int}, "last_action": {...}}}}
Each response entry:
    {"<pid>": {"response": "<int>", "verdict": "OK"}, "output": null}
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from bot_action_stats import (
    extract_actions_from_replay,
    extract_hands_from_replay,  # deprecated alias
    compute_bot_action_stats,
    compute_all_bot_stats,
)


# ── helpers to build request/response log entries in the real shape ──

def _request(pid, round_num, round_player_bet, hand=0, last_action=None, public_cards=None):
    """Build a request entry addressed to player `pid`."""
    content = {
        str(pid): {
            "my_id": pid,
            "public_cards": public_cards or [],
        }
    }
    display = {
        "round": round_num,
        "round_player_bet": list(round_player_bet),
        "matchdata": {"hand": hand, "max_hand": 70},
        "public_cards": public_cards or [],
    }
    if last_action is not None:
        display["last_action"] = last_action
    return {"output": {"command": "request", "content": content, "display": display}}


def _response(pid, resp_int):
    """Build a response entry: bot action reply to the preceding request."""
    return {str(pid): {"response": str(resp_int), "verdict": "OK"}, "output": None}


def _make_replay(bot0, bot1, logs_per_game, mirror=False):
    """Wrap a list of log entries into a single-game replay dict."""
    return {
        "bot0": bot0,
        "bot1": bot1,
        "bot0_wins": 1,
        "bot1_wins": 0,
        "draws": 0,
        "games": [{
            "game": 0,
            "mirror": mirror,
            "winner": 0,
            "bot0_chips": 100.0,
            "bot1_chips": -100.0,
            "logs": logs_per_game,
        }],
    }


def _hand0_fold_to_3bet():
    """bot0 raises, bot1 3-bets (raise), bot0 folds — all preflop."""
    logs = [
        # Preflop opening, bot0 (SB=50) to act, BB=100 -> rpb [50,100]
        _request(0, round_num=0, round_player_bet=[50, 100], hand=0),
        _response(0, 200),   # bot0 raises to 200
        # bot1 (BB) to act; bot0 committed 200 -> rpb [200,100]
        _request(1, round_num=0, round_player_bet=[200, 100], hand=0),
        _response(1, 600),   # bot1 re-raises to 600
        # bot0 faces 3bet; rpb [200,600]
        _request(0, round_num=0, round_player_bet=[200, 600], hand=0),
        _response(0, -1),    # bot0 folds
    ]
    return logs


# ── extract_actions_from_replay ──

class TestExtractActionsFromReplay:

    def test_empty_no_bots(self):
        assert extract_actions_from_replay({}) == []

    def test_empty_no_games(self):
        assert extract_actions_from_replay({"bot0": "A", "bot1": "B"}) == []

    def test_json_string_input(self):
        replay = _make_replay("A", "B", [_request(0, 0, [50, 100]), _response(0, -1)])
        result = extract_actions_from_replay(json.dumps(replay))
        assert len(result) == 1
        assert result[0]["bot"] == "A"
        assert result[0]["action"] == "fold"
        assert result[0]["street"] == "preflop"

    def test_raise_call_check_fold_classification(self):
        """preflop raise + call (gap->call); flop check (matched)."""
        logs = [
            # bot0 SB(50) opens to 200 vs BB(100): rpb [50,100] gap -> raise
            _request(0, 0, [50, 100], hand=0),
            _response(0, 200),
            # bot1 BB faces 200; rpb [200,100] gap -> call (resp 0)
            _request(1, 0, [200, 100], hand=0),
            _response(1, 0),
            # flop, bot1 first, rpb [0,0] matched -> check (resp 0)
            _request(1, 1, [0, 0], hand=0),
            _response(1, 0),
            # bot0 flop, rpb [0,0] matched -> check
            _request(0, 1, [0, 0], hand=0),
            _response(0, 0),
        ]
        actions = extract_actions_from_replay(_make_replay("Alice", "Bob", logs))
        by_bot_act = {(a["bot"], a["street"], a["action"]) for a in actions}
        assert ("Alice", "preflop", "raise") in by_bot_act
        assert ("Bob", "preflop", "call") in by_bot_act
        assert ("Bob", "flop", "check") in by_bot_act
        assert ("Alice", "flop", "check") in by_bot_act

    def test_allin_encoded_as_minus_two(self):
        logs = [
            _request(0, 0, [50, 100], hand=0),
            _response(0, -2),  # allin
        ]
        actions = extract_actions_from_replay(_make_replay("A", "B", logs))
        assert actions == [{"bot": "A", "street": "preflop", "action": "allin", "hand": 0}]

    def test_player_id_stably_maps_bots(self):
        """bot0 -> player 0, bot1 -> player 1 (mirror half included)."""
        logs = [
            _request(0, 0, [50, 100], hand=0),
            _response(0, 200),
            _request(1, 0, [200, 100], hand=0),
            _response(1, -1),
        ]
        replay = _make_replay("claude_vA", "claude_vB", logs, mirror=False)
        actions = extract_actions_from_replay(replay)
        bots = {a["bot"] for a in actions}
        assert bots == {"claude_vA", "claude_vB"}
        raiser = [a for a in actions if a["action"] == "raise"][0]
        assert raiser["bot"] == "claude_vA"  # player 0 = bot0

    def test_street_from_display_round(self):
        for rnd, street in [(0, "preflop"), (1, "flop"), (2, "turn"), (3, "river")]:
            logs = [_request(0, rnd, [100, 100]), _response(0, 0)]
            actions = extract_actions_from_replay(_make_replay("A", "B", logs))
            assert actions[0]["street"] == street

    def test_hand_number_captured(self):
        logs = [
            _request(0, 0, [50, 100], hand=5),
            _response(0, 200),
        ]
        actions = extract_actions_from_replay(_make_replay("A", "B", logs))
        assert actions[0]["hand"] == 5

    def test_deprecated_alias_works(self):
        replay = _make_replay("A", "B", [_request(0, 0, [50, 100]), _response(0, -1)])
        assert extract_hands_from_replay(replay) == extract_actions_from_replay(replay)


# ── compute_bot_action_stats / compute_all_bot_stats ──
class TestComputeBotActionStats:

    def test_nonexistent_dir(self):
        assert compute_bot_action_stats("A", "/nonexistent/path") == {}

    def test_empty_dir(self, tmp_path):
        result = compute_bot_action_stats("A", str(tmp_path))
        assert result == {}

    def test_basic_per_street_shape(self, tmp_path):
        replay = _make_replay("Alice", "Bob", _hand0_fold_to_3bet())
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        # Required top-level keys
        assert set(stats.keys()) == {"preflop", "flop", "turn", "river", "total_hands"}
        # Per-street shape
        assert set(stats["preflop"].keys()) == {"total", "fold", "call", "raise", "check", "allin"}
        # bot0 acted: raise200 then fold -> preflop total=2, raise=1, fold=1
        assert stats["preflop"]["total"] == 2
        assert stats["preflop"]["raise"] == 1
        assert stats["preflop"]["fold"] == 1
        assert stats["total_hands"] == 1  # one distinct hand

    def test_allin_double_counted_into_raise(self, tmp_path):
        """allin increments BOTH allin and raise keys (documented semantics)."""
        logs = [
            _request(0, 0, [50, 100], hand=0),
            _response(0, -2),  # allin
        ]
        replay = _make_replay("Alice", "Bob", logs)
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["preflop"]["allin"] == 1
        assert stats["preflop"]["raise"] == 1  # allin also counted as raise
        assert stats["preflop"]["total"] == 1

    def test_call_vs_check_disambiguation(self, tmp_path):
        """response=0 with matched bets -> check; with bet gap -> call."""
        logs = [
            # bot0 SB(50) completes: rpb [50,100] gap -> call
            _request(0, 0, [50, 100], hand=0),
            _response(0, 0),
            # bot1 BB faces completed SB: rpb [100,100] matched -> check
            _request(1, 0, [100, 100], hand=0),
            _response(1, 0),
            # flop, bot1 first, rpb [0,0] matched -> check
            _request(1, 1, [0, 0], hand=0),
            _response(1, 0),
        ]
        replay = _make_replay("Alice", "Bob", logs)
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        alice = compute_bot_action_stats("Alice", str(tmp_path))
        bob = compute_bot_action_stats("Bob", str(tmp_path))
        assert alice["preflop"]["call"] == 1
        assert alice["preflop"]["check"] == 0
        assert bob["preflop"]["check"] == 1
        assert bob["preflop"]["call"] == 0
        assert bob["flop"]["check"] == 1

    def test_street_separation_across_streets(self, tmp_path):
        """One action on each street for bot0, all classified as check (matched bets)."""
        logs = [
            _request(0, 0, [100, 100], hand=0), _response(0, 0),   # preflop check
            _request(0, 1, [0, 0], hand=0), _response(0, 0),      # flop check
            _request(0, 2, [0, 0], hand=0), _response(0, 0),      # turn check
            _request(0, 3, [0, 0], hand=0), _response(0, 0),      # river check
        ]
        replay = _make_replay("Alice", "Bob", logs)
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        for street in ("preflop", "flop", "turn", "river"):
            assert stats[street]["total"] == 1
            assert stats[street]["check"] == 1

    def test_corrupted_json_skipped(self, tmp_path):
        (tmp_path / "bad.json").write_text("not json")
        replay = _make_replay("Alice", "Bob", _hand0_fold_to_3bet())
        (tmp_path / "good.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["preflop"]["total"] == 2

    def test_bot_not_present_returns_empty(self, tmp_path):
        replay = _make_replay("Alice", "Bob", _hand0_fold_to_3bet())
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Carol", str(tmp_path))
        assert stats == {}

    def test_multiple_files_and_hands_count(self, tmp_path):
        """Two files, two distinct hand numbers -> total_hands=2."""
        for h in (0, 1):
            logs = [
                _request(0, 0, [50, 100], hand=h),
                _response(0, 200),
                _request(1, 0, [200, 100], hand=h),
                _response(1, -1),
            ]
            replay = _make_replay("Alice", "Bob", logs)
            (tmp_path / f"r{h}.json").write_text(json.dumps(replay))
        stats = compute_bot_action_stats("Alice", str(tmp_path))
        assert stats["preflop"]["raise"] == 2
        assert stats["total_hands"] == 2

    def test_compute_all_bot_stats_single_pass(self, tmp_path):
        """compute_all_bot_stats reads each file once and returns both bots."""
        replay = _make_replay("Alice", "Bob", _hand0_fold_to_3bet())
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        all_stats = compute_all_bot_stats(["Alice", "Bob", "Carol"], str(tmp_path))
        assert set(all_stats.keys()) == {"Alice", "Bob", "Carol"}
        assert all_stats["Alice"]["preflop"]["raise"] == 1
        assert all_stats["Bob"]["preflop"]["raise"] == 1  # the 3bet
        assert all_stats["Carol"] == {}  # not in any replay

    def test_compute_bot_action_stats_delegates_to_all(self, tmp_path):
        replay = _make_replay("Alice", "Bob", _hand0_fold_to_3bet())
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        single = compute_bot_action_stats("Alice", str(tmp_path))
        allv = compute_all_bot_stats(["Alice"], str(tmp_path))["Alice"]
        assert single == allv

    def test_mirror_half_player_mapping_stable(self, tmp_path):
        """A mirror-half game still maps bot0->player0, bot1->player1."""
        logs = [
            _request(0, 0, [50, 100], hand=0),
            _response(0, 200),
            _request(1, 0, [200, 100], hand=0),
            _response(1, -1),
        ]
        replay = _make_replay("claude_v1", "claude_v2", logs, mirror=True)
        (tmp_path / "r1.json").write_text(json.dumps(replay))
        v1 = compute_bot_action_stats("claude_v1", str(tmp_path))
        v2 = compute_bot_action_stats("claude_v2", str(tmp_path))
        assert v1["preflop"]["raise"] == 1
        assert v2["preflop"]["fold"] == 1
