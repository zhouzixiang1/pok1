"""validator 协议契约测试:13 条非法行为 + raise 边界(>=2× 精确2×合法,arena 决策1)。

固化《非法行为说明.docx》13 条规则与 raise 边界 oracle,防回归。
"""
from __future__ import annotations

import pytest

from arena.backend.engine.validator import (
    MIN_RAISE_POSTFLOP,
    MIN_RAISE_PREFLOP,
    RAISE_MULTIPLIER,
    validate_action,
)


def gs(
    stage: str = "preflop",
    actions: list | None = None,
    player_chips: int = 20000,
    player_bet: int = 0,
    opponent_bet: int = 0,
    is_sb: bool = False,
    is_bb: bool = False,
    allin_occurred: bool = False,
    player_action_count: int = 0,
) -> dict:
    return {
        "stage": stage,
        "actions": actions or [],
        "player_chips": player_chips,
        "player_bet": player_bet,
        "opponent_bet": opponent_bet,
        "is_small_blind": is_sb,
        "is_big_blind": is_bb,
        "allin_occurred": allin_occurred,
        "player_action_count": player_action_count,
    }


# ── 规则 1: bet 永远非法 ────────────────────────────────────
def test_bet_always_illegal():
    assert not validate_action("bet", 100, gs())[0]
    assert not validate_action("bet", 100, gs(stage="flop"))[0]


def test_unknown_illegal():
    assert not validate_action("unknown", None, gs())[0]


def test_fold_always_legal():
    assert validate_action("fold", None, gs(stage="preflop"))[0]
    assert validate_action("fold", None, gs(stage="river", actions=[("raise", 200)]))[0]


# ── 规则 2: call ─────────────────────────────────────────────
def test_call_first_in_postflop_illegal():
    assert not validate_action("call", None, gs(stage="flop", actions=[]))[0]
    assert not validate_action("call", None, gs(stage="turn", actions=[]))[0]


def test_bb_call_after_sb_call_illegal():
    # preflop BB 在 SB 仅 call 后 call -> 非法(BB 已下大盲无待跟,须 check)
    state = gs(stage="preflop", is_bb=True, player_action_count=0,
               opponent_bet=100, player_bet=100, actions=[("call", None)])
    assert not validate_action("call", None, state)[0]


def test_call_legal_when_owes_chips():
    # postflop 对手 raise 200, 本方 call 合法(跟注)
    assert validate_action("call", None, gs(stage="flop", actions=[("raise", 200)]))[0]


# ── 规则 3: check ────────────────────────────────────────────
def test_bb_check_after_sb_call_legal():
    state = gs(stage="preflop", is_bb=True, player_action_count=0,
               opponent_bet=100, player_bet=100, actions=[("call", None)])
    assert validate_action("check", None, state)[0]


def test_check_after_first_postflop_illegal():
    # postflop 非首动作 check -> 非法(欠0且非首动作须用 call)
    assert not validate_action("check", None, gs(stage="flop", actions=[("check", None)]))[0]


def test_check_first_postflop_legal():
    assert validate_action("check", None, gs(stage="flop", actions=[]))[0]


# ── raise 边界(arena 决策1: >=2×, 精确2×合法)──────────────
def test_sb_first_raise_min_preflop():
    assert not validate_action("raise", 199, gs(stage="preflop", is_sb=True,
                                                player_action_count=0, player_bet=50))[0]
    assert validate_action("raise", 200, gs(stage="preflop", is_sb=True,
                                            player_action_count=0, player_bet=50))[0]


def test_raise_exact_2x_is_legal():
    # 官方 EXE 实测: raise 200 -> raise 400(精确 2×)合法(arena 决策1)
    state = gs(stage="preflop", is_bb=True, player_action_count=0,
               player_bet=100, actions=[("raise", 200)])
    ok, reason = validate_action("raise", 400, state)
    assert ok, reason


def test_raise_below_2x_illegal():
    state = gs(stage="preflop", is_bb=True, player_action_count=0,
               player_bet=100, actions=[("raise", 200)])
    assert not validate_action("raise", 399, state)[0]


def test_postflop_first_raise_min():
    assert not validate_action("raise", 99, gs(stage="flop", player_bet=0, actions=[]))[0]
    assert validate_action("raise", 100, gs(stage="flop", player_bet=0, actions=[]))[0]


def test_consecutive_raise_2x_legal():
    # flop raise 200 -> raise 400 合法
    state = gs(stage="flop", player_bet=0, actions=[("raise", 200)])
    ok, reason = validate_action("raise", 400, state)
    assert ok, reason


# ── raise 筹码约束 ───────────────────────────────────────────
def test_raise_equals_chips_must_allin():
    # raise-to 恰好等于剩余筹码(needed==chips) -> 必须用 allin,raise 非法
    assert not validate_action("raise", 500, gs(stage="flop", player_bet=0, player_chips=500))[0]


def test_raise_exceeds_chips_illegal():
    assert not validate_action("raise", 600, gs(stage="flop", player_bet=0, player_chips=500))[0]


def test_raise_to_must_exceed_player_bet():
    # raise-to <= 本街已下注 -> 非法
    assert not validate_action("raise", 100, gs(stage="flop", player_bet=100, player_chips=20000))[0]


# ── allin 规则 ───────────────────────────────────────────────
def test_consecutive_allin_illegal():
    assert not validate_action("allin", None, gs(stage="flop", allin_occurred=True))[0]


def test_raise_after_allin_illegal():
    assert not validate_action("raise", 200, gs(stage="flop", allin_occurred=True,
                                                player_bet=0, player_chips=500))[0]


def test_first_allin_legal():
    assert validate_action("allin", None, gs(stage="flop", allin_occurred=False))[0]


# ── 常量校验 ─────────────────────────────────────────────────
def test_constants():
    assert MIN_RAISE_PREFLOP == 200
    assert MIN_RAISE_POSTFLOP == 100
    assert RAISE_MULTIPLIER == 2  # arena 决策1: >=2× 精确2×合法
