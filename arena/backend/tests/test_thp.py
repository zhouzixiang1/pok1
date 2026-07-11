"""THP 棋谱格式测试:卡牌字母编码 + STATE 结构 + raise 记总额(arena 决策2) + gb2312。"""
from __future__ import annotations

import os
import tempfile

from arena.backend.engine.thp_recorder import THPRecorder, tcp_card_to_thp


def test_tcp_card_to_thp_mapping():
    # suit 0=♠s 1=♥h 2=♦d 3=♣c; rank 0=2 .. 8=T .. 9=J .. 11=K .. 12=A
    assert tcp_card_to_thp(0, 12) == "As"
    assert tcp_card_to_thp(1, 12) == "Ah"
    assert tcp_card_to_thp(2, 12) == "Ad"
    assert tcp_card_to_thp(3, 12) == "Ac"
    assert tcp_card_to_thp(0, 0) == "2s"
    assert tcp_card_to_thp(0, 8) == "Ts"   # rank 8 = 10 = T
    assert tcp_card_to_thp(0, 9) == "Js"
    assert tcp_card_to_thp(0, 10) == "Qs"
    assert tcp_card_to_thp(0, 11) == "Ks"


def test_format_hand_structure():
    rec = THPRecorder("BotA", "BotB")
    rec.on_hand_start(1, sb_idx=0, bb_idx=1)
    rec.on_hand_cards(0, [(0, 12), (1, 11)])   # SB: As Kh
    rec.on_hand_cards(1, [(2, 0), (3, 1)])     # BB: 2d 3c
    rec.on_action(0, "call")   # preflop SB limp(须在 on_stage_cards flop 前)
    rec.on_action(1, "check")  # preflop BB check
    rec.on_stage_cards("flop", [(0, 0), (1, 1), (2, 2)])  # 2s 3h 4d
    rec.on_settle([100, -100])
    line = rec.format_hand(rec.records[0])
    assert line.startswith("STATE:1:") and line.endswith(";")
    parts = line.rstrip(";").split(":")
    # STATE:N 含冒号 -> split 后 parts[0]="STATE", parts[1]=N
    assert parts[0] == "STATE"
    assert parts[1] == "1"
    assert parts[2] == "cc///"       # preflop: cc(SB call + BB check), flop/turn/river 空
    assert "2d3c|AsKh" in parts[3]   # 手牌段 BB|SB
    assert "2s3h4d" in parts[3]      # flop
    assert parts[4] == "-100|100"    # earnings BB|SB
    assert parts[5] == "BotB|BotA"   # names BB|SB


def test_format_hand_raise_records_total():
    # recorder 记传入的 amount; arena game.py 传 raise-to-total 总额(决策2, 非增量)
    rec = THPRecorder("A", "B")
    rec.on_hand_start(2, sb_idx=0, bb_idx=1)
    rec.on_hand_cards(0, [(0, 0), (0, 1)])
    rec.on_hand_cards(1, [(1, 0), (1, 1)])
    rec.on_action(0, "raise", 400)   # raise-to-total 400
    rec.on_action(1, "call")
    rec.on_settle([0, 0])
    line = rec.format_hand(rec.records[0])
    assert "r400" in line            # 记总额(若记增量会不同)


def test_export_file_gb2312_with_chinese():
    rec = THPRecorder("BotA", "BotB")
    rec.on_hand_start(1, sb_idx=0, bb_idx=1)
    rec.on_hand_cards(0, [(0, 0), (0, 1)])
    rec.on_hand_cards(1, [(1, 0), (1, 1)])
    rec.on_settle([100, -100])
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "test.thp")
        rec.export_file(path, event_name="测试赛事")
        # 用 gb2312 读回(THP 标准), 中文赛事名可读
        with open(path, "r", encoding="gb2312") as f:
            content = f.read()
        assert "STATE:1" in content
        assert "测试赛事" in content
