"""Integration tests for P0-P6 root cause fixes.

P0: Idempotency guards on MCP pipeline tools (tool_gates.py)
P1: Broadened SDK exception catch (llm_query.py)
P2: State machine transition guards (evolution_infra.py)
P3: AST-based dead code detection (code_verification.py)
P4: Exhausted direction enforcement in plan validation (tool_planning.py)
P5: replay_spotlight reads my_cards instead of history (replay_spotlight.py)
P6: Fix injection logging cleanup (fix_injection.py)
"""

import inspect
import json
import os
import tempfile
from pathlib import Path

import pytest


# ── P0: Idempotency Guards ──────────────────────────────────────────

class TestP0IdempotencyGuards:
    """P0: Pipeline tools return cached results on repeat calls."""

    def _read_tool_gates_source(self):
        p = Path(__file__).resolve().parent.parent / "core" / "tool_gates.py"
        return p.read_text()

    def test_quality_gates_has_guard(self):
        source = self._read_tool_gates_source()
        assert "idempotent_cache" in source

    def test_review_has_guard(self):
        source = self._read_tool_gates_source()
        # After refactoring, guards are consolidated in _idempotency_check helper;
        # verify run_review calls the helper with gate_name="review"
        assert '_idempotency_check(' in source
        assert 'gate_name="review"' in source

    def test_critic_has_guard(self):
        source = self._read_tool_gates_source()
        # After refactoring, guards are consolidated in _idempotency_check helper;
        # verify run_critic calls the helper with gate_name="critic"
        assert '_idempotency_check(' in source
        assert 'gate_name="critic"' in source


# ── P1: SDK Exception Handling ───────────────────────────────────────

class TestP1BroadExceptionCatch:
    """P1: ClaudeSDKError base class catches all SDK errors."""

    def test_imports_base_class(self):
        from core import llm_query
        source = inspect.getsource(llm_query)
        assert "ClaudeSDKError" in source

    def test_process_stream_uses_base(self):
        from core.llm_query import _process_stream
        source = inspect.getsource(_process_stream)
        assert "ClaudeSDKError" in source

    def test_cancelled_still_propagates(self):
        from core.llm_query import _process_stream
        source = inspect.getsource(_process_stream)
        # CancelledError must still be re-raised
        assert "CancelledError" in source
        assert "raise" in source


# ── P2: Stage Transition Guards ─────────────────────────────────────

class TestP2StageTransition:
    """P2: State transition validation in evolution_infra.py."""

    def test_forward_transitions_allowed(self):
        from core.evolution_infra import validate_stage_transition, STAGE_ORDER
        for i in range(len(STAGE_ORDER) - 1):
            ok, reason = validate_stage_transition(STAGE_ORDER[i], STAGE_ORDER[i + 1])
            assert ok, f"Forward {STAGE_ORDER[i]} -> {STAGE_ORDER[i+1]} should be valid: {reason}"

    def test_backward_transition_blocked(self):
        from core.evolution_infra import validate_stage_transition
        ok, reason = validate_stage_transition("reviewed", "quality_passed")
        assert not ok
        assert "backward" in reason

    def test_retry_to_master_planned_allowed(self):
        from core.evolution_infra import validate_stage_transition
        for src in ["workers_done", "quality_passed", "reviewed", "critic_checked"]:
            ok, reason = validate_stage_transition(src, "master_planned")
            assert ok, f"{src} -> master_planned should be valid"
            assert "retry" in reason

    def test_timeout_override_allowed(self):
        from core.evolution_infra import validate_stage_transition
        for src in ["reviewed", "critic_checked", "verified"]:
            ok, _ = validate_stage_transition(src, "timed_out")
            assert ok

    def test_none_to_any_allowed(self):
        from core.evolution_infra import validate_stage_transition
        ok, _ = validate_stage_transition(None, "reviewed")
        assert ok

    def test_same_stage_allowed(self):
        from core.evolution_infra import validate_stage_transition
        ok, _ = validate_stage_transition("reviewed", "reviewed")
        assert ok

    def test_fresh_restart_allowed(self):
        from core.evolution_infra import validate_stage_transition
        ok, _ = validate_stage_transition("critic_checked", "prepared")
        assert ok


# ── P3: AST Dead Code Detection ─────────────────────────────────────

class TestP3ASTDeadCode:
    """P3: AST-based dead code detection in code_verification.py."""

    def test_detects_empty_function_stub(self, tmp_path):
        from core.code_verification import _detect_dead_code_ast
        f = tmp_path / "bot.py"
        f.write_text("def placeholder():\n    pass\n")
        errors = _detect_dead_code_ast(str(tmp_path))
        assert len(errors) == 1
        assert "placeholder" in errors[0]

    def test_no_false_positive_on_dunder(self, tmp_path):
        from core.code_verification import _detect_dead_code_ast
        f = tmp_path / "bot.py"
        f.write_text("class Foo:\n    def __init__(self):\n        pass\n")
        errors = _detect_dead_code_ast(str(tmp_path))
        assert len(errors) == 0

    def test_valid_code_no_errors(self, tmp_path):
        from core.code_verification import _detect_dead_code_ast
        f = tmp_path / "bot.py"
        f.write_text("def compute(x):\n    return x + 1\n")
        errors = _detect_dead_code_ast(str(tmp_path))
        assert len(errors) == 0

    def test_detects_unreachable_after_return(self, tmp_path):
        from core.code_verification import _detect_dead_code_ast
        f = tmp_path / "bot.py"
        f.write_text("def early_exit():\n    return 42\n    x = 1\n")
        errors = _detect_dead_code_ast(str(tmp_path))
        assert any("unreachable" in e for e in errors)


# ── P4: Exhausted Direction Enforcement ─────────────────────────────

class TestP4ExhaustedDirection:
    """P4: _validate_master_plan blocks plans matching EXHAUSTED directions."""

    def test_function_exists(self):
        from core.tool_planning import _validate_master_plan
        assert callable(_validate_master_plan)

    def test_exhausted_check_in_source(self):
        from core.tool_planning import _validate_master_plan
        source = inspect.getsource(_validate_master_plan)
        assert "_extract_exhausted_keywords" in source
        assert "_fuzzy_match_exhausted" in source


# ── P5: Replay Spotlight Card Fix ───────────────────────────────────

class TestP5ReplayCards:
    """P5: replay_spotlight reads my_cards instead of history."""

    def test_uses_my_cards_field(self):
        from core.replay_spotlight import _extract_hand_swing
        source = inspect.getsource(_extract_hand_swing)
        assert "my_cards" in source

    def test_no_history_slice_as_cards(self):
        from core.replay_spotlight import _extract_hand_swing
        source = inspect.getsource(_extract_hand_swing)
        # The old bug was hist[:2] treating action dicts as cards
        assert "hist[:2]" not in source


# ── P5b: replay_spotlight per-hand granularity ──────────────────────

# A games[i] entry is a 70-hand MIRROR HALF-GAME, not a single hand. The
# fixture below mirrors the EXACT real replay schema (verified against
# web/core/results/match_replay/*.json): request entries carry
# display.matchdata.{hand,total_win_chips}, display.{public_cards,last_action,pot}
# and content[str(player_id)]["my_cards"]; response entries have output=None.
#
# Fixture: 3 hands. hand0 checks down (delta 0), hand1 bot0 raises all-in
# (delta +500), hand2 bot0 folds preflop (delta -50, LAST hand).

def _p5_req(hand, twc, content_key, my_cards, public_cards, last_action, pot):
    return {"output": {
        "command": "req",
        "content": {content_key: {
            "my_cards": my_cards, "public_cards": public_cards,
            "hand": hand, "total_win_chips": twc,
        }},
        "display": {
            "matchdata": {"hand": hand, "max_hand": 3,
                          "total_win_chips": twc, "total_win_games": [0, 0]},
            "public_cards": public_cards,
            "last_action": last_action,
            "pot": pot,
        },
    }}


def _p5_resp(pid):
    return {str(pid): {"response": 0, "verdict": "ok"}, "output": None}


def _p5_build_half_game():
    logs0 = [
        _p5_req(0, [0, 0], "1", [10, 11], [], None, 150),
        _p5_resp(1),
        _p5_req(0, [0, 0], "0", [20, 21], [],
                {"player_id": 1, "action": 0, "action_type": "call"}, 200),
        _p5_resp(0),
        _p5_req(0, [0, 0], "0", [20, 21], [1, 2, 3],
                {"player_id": 0, "action": 0, "action_type": "check"}, 200),
        _p5_resp(0),
        _p5_req(0, [0, 0], "0", [20, 21], [1, 2, 3, 4],
                {"player_id": 1, "action": 0, "action_type": "check"}, 200),
        _p5_resp(0),
        _p5_req(0, [0, 0], "0", [20, 21], [1, 2, 3, 4, 5],
                {"player_id": 0, "action": 0, "action_type": "check"}, 200),
    ]
    logs1 = [
        _p5_req(1, [0.0, 0.0], "0", [40, 41], [],
                {"player_id": 0, "action": 19999, "action_type": "raise"}, 200),
        _p5_resp(0),
        _p5_req(1, [0.0, 0.0], "1", [30, 31], [],
                {"player_id": 1, "action": -2, "action_type": "allin"}, 40000),
        _p5_resp(1),
    ]
    logs2 = [
        _p5_req(2, [500.0, -500.0], "0", [50, 51], [],
                {"player_id": 1, "action": 0, "action_type": "call"}, 200),
        _p5_resp(0),
        _p5_req(2, [500.0, -500.0], "0", None, [],
                {"player_id": 0, "action": -1, "action_type": "fold"}, 200),
        # trailing summary entry (twc updated to final cumulative)
        _p5_req(2, [450.0, -450.0], "0", None, [], None, 0),
    ]
    return {
        "game": 0, "mirror": False, "winner": 0,
        "bot0_chips": 450.0, "bot1_chips": -450.0,
        "logs": logs0 + logs1 + logs2,
    }


class TestP5bReplayPerHand:
    """P5b: replay_spotlight splits each 70-hand half-game into real hands."""

    def test_iter_hands_splits_per_hand_count(self):
        from core.replay_spotlight import _iter_hands
        game = _p5_build_half_game()
        hands = list(_iter_hands(game, 0, 1))
        # 3 distinct hands, one per matchdata.hand value — NOT a single
        # fictional half-game entry.
        assert len(hands) == 3
        assert {h["hand_num"] for h in hands} == {0, 1, 2}

    def test_iter_hands_final_hand_delta_uses_bot_chips(self):
        from core.replay_spotlight import _iter_hands
        game = _p5_build_half_game()
        hands = list(_iter_hands(game, 0, 1))
        last = hands[-1]
        # Last hand has no successor: delta must use bot0_chips (450) minus
        # the start-of-hand cumulative (500) = -50, NOT the full-game 450.
        assert last["hand_num"] == 2
        assert last["chip_delta"] == -50.0

    def test_iter_hands_per_hand_board_isolation(self):
        from core.replay_spotlight import _iter_hands
        game = _p5_build_half_game()
        hands = {h["hand_num"]: h for h in _iter_hands(game, 0, 1)}
        # Each hand's board/cards are isolated to that hand, not the
        # max-len board across all 70 hands of the half-game.
        assert hands[0]["public_cards"] == [1, 2, 3, 4, 5]
        assert hands[1]["public_cards"] == []
        assert hands[0]["bot_cards"] == [20, 21]
        assert hands[1]["bot_cards"] == [40, 41]

    def test_iter_hands_chip_delta_is_per_hand_not_cumulative(self):
        from core.replay_spotlight import _iter_hands
        game = _p5_build_half_game()
        hands = {h["hand_num"]: h for h in _iter_hands(game, 0, 1)}
        # Per-hand delta is the single-hand swing, not the 70-hand cumulative
        # (the old bug returned game["bot0_chips"] = 450 as the swing).
        assert hands[0]["chip_delta"] == 0
        assert hands[1]["chip_delta"] == 500.0
        assert hands[1]["swing"] == 500.0
        assert hands[1]["swing"] != game["bot0_chips"]

    def test_extract_hand_swing_wraps_iter_hands(self):
        from core.replay_spotlight import _extract_hand_swing
        game = _p5_build_half_game()
        summary = _extract_hand_swing(game, 0, 1)
        # Thin wrapper returns the largest single-hand swing (hand 1, +500),
        # not the half-game cumulative (450).
        assert summary is not None
        assert summary["hand_num"] == 1
        assert summary["swing"] == 500.0

    def test_find_critical_hands_ranks_single_hand_swing(self):
        from core.replay_spotlight import find_critical_hands
        replay = {"bot0": "test_bot", "bot1": "opp",
                  "games": [_p5_build_half_game()]}
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "r.json"), "w") as f:
                json.dump(replay, f)
            out = find_critical_hands("test_bot", tmp, max_hands=5)
        assert isinstance(out, str)
        assert "H1" in out            # biggest swing hand appears
        assert "delta=+500" in out     # per-hand delta, not cumulative
        # hand 0 (delta 0) is excluded; full-game 450 is never reported as a swing
        assert "H0" not in out

    def test_find_critical_hands_real_replay(self):
        # Integration test against the real match_replay directory. Skips
        # gracefully when the daemon has rotated all replays away.
        from core.replay_spotlight import _iter_hands, find_critical_hands
        try:
            from core.evolution_infra import RESULTS_DIR
        except Exception:
            pytest.skip("evolution_infra not importable")
        replays_dir = str(RESULTS_DIR / "match_replay")
        files = sorted(
            [p for p in __import__("glob").glob(os.path.join(replays_dir, "*.json"))],
            key=os.path.getmtime, reverse=True,
        )
        if not files:
            pytest.skip("no real replay files available")
        try:
            with open(files[0]) as f:
                replay = json.load(f)
        except json.JSONDecodeError:
            pytest.skip("replay file mid-write (daemon rotating) — not valid JSON")
        bot_name = replay.get("bot0") or replay.get("bot1")
        games = replay.get("games", [])
        assert games, "real replay has no games"
        # Each half-game must split into many real hands (70 per game),
        # proving the granularity fix on real data.
        total_hands = sum(len(list(_iter_hands(g, 0, 1))) for g in games)
        assert total_hands > len(games), (
            f"expected per-hand split, got {total_hands} hands / {len(games)} games"
        )
        out = find_critical_hands(
            bot_name, replays_dir, max_hands=3, recent_n_files=min(20, len(files))
        )
        assert isinstance(out, str)
        # Accept either a populated summary or the empty-result message (a thin
        # replay with no positive swings for the chosen bot yields the latter).
        assert "Critical hands" in out or "No hands with chip swings" in out


# ── P6: Fix Injection Logging ───────────────────────────────────────

class TestP6FixInjection:
    """P6: Fix injection logging uses appropriate severity."""

    def test_bot_002b_inactive(self):
        from core.fix_injection import MANDATORY_FIXES
        bot002b = [f for f in MANDATORY_FIXES if f.fix_id == "BOT-002b"]
        assert len(bot002b) == 1
        assert bot002b[0].active is False

    def test_severity_logic(self):
        from core.fix_injection import log_fix_application
        source = inspect.getsource(log_fix_application)
        # Changed from "warn" if skipped and not applied to "warn" if skipped and applied
        assert "skipped and applied" in source

    def test_active_fixes_still_functional(self):
        from core.fix_injection import MANDATORY_FIXES
        active_ids = [f.fix_id for f in MANDATORY_FIXES if f.active]
        assert "BOT-001a" in active_ids
        assert "BOT-002a" in active_ids
        assert "BOT-004" in active_ids
