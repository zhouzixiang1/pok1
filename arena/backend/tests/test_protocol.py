"""protocol 解析/格式化测试:parse_action 严格匹配 + format_* 协议字节级契约。"""
from __future__ import annotations

from arena.backend.engine.deck import Card
from arena.backend.engine.protocol import (
    format_earn_chips,
    format_flop,
    format_name_query,
    format_oppo_hands,
    format_preflop,
    format_river,
    format_turn,
    parse_action,
)


def test_parse_fixed_actions():
    assert parse_action("call") == ("call", None)
    assert parse_action("check") == ("check", None)
    assert parse_action("fold") == ("fold", None)
    assert parse_action("allin") == ("allin", None)


def test_parse_raise():
    assert parse_action("raise 400") == ("raise", 400)
    assert parse_action("raise 0") == ("raise", 0)
    assert parse_action("raise 12000") == ("raise", 12000)


def test_parse_bet_recognized_but_illegal():
    # bet 被识别为 bet(交 validator 规则1 判非法), 不是 unknown
    assert parse_action("bet 100") == ("bet", None)


def test_parse_strict_single_space():
    # 协议要求关键字与金额间恰一个空格; 多空格/前导/尾随 -> unknown(非法)
    assert parse_action("raise  400") == ("unknown", None)
    assert parse_action(" raise 400") == ("unknown", None)
    assert parse_action("raise 400 ") == ("unknown", None)
    assert parse_action("raise  400 ") == ("unknown", None)


def test_parse_unknown():
    assert parse_action("foo") == ("unknown", None)
    assert parse_action("") == ("unknown", None)
    assert parse_action("raise") == ("unknown", None)  # 缺金额
    assert parse_action("raise abc") == ("unknown", None)


def test_format_name_query():
    assert format_name_query() == "name"


def test_format_preflop():
    cards = [Card(0, 12), Card(1, 0)]  # <0,12>=♠A, <1,0>=♥2
    assert format_preflop(cards, "SMALLBLIND") == "preflop|SMALLBLIND|<0,12><1,0>"
    assert format_preflop(cards, "BIGBLIND") == "preflop|BIGBLIND|<0,12><1,0>"


def test_format_stages():
    flop = [Card(0, 0), Card(1, 1), Card(2, 2)]
    assert format_flop(flop) == "flop|<0,0><1,1><2,2>"
    assert format_turn(Card(3, 3)) == "turn|<3,3>"
    assert format_river(Card(0, 5)) == "river|<0,5>"


def test_format_earn_chips():
    assert format_earn_chips(0) == "earnChips 0"
    assert format_earn_chips(-200) == "earnChips -200"
    assert format_earn_chips(1500) == "earnChips 1500"


def test_format_oppo_hands():
    cards = [Card(0, 12), Card(0, 11)]
    assert format_oppo_hands(cards) == "oppo_hands|<0,12><0,11>"
