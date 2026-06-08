"""THP 棋谱记录器。

按照《中国计算机博弈锦标赛棋（牌）谱标准说明书》德州扑克部分生成棋谱文件。

THP 格式示例：
  STATE:7:r223c/cr646r1170c/cc/cr2340r7898c:6cQh|Ah6s/7h9d5h/8d/As:0|0:A|B;

文件命名：THP-{teamA} vs {teamB}-{winner}胜-{yyyymmddHHMM}-{event}.txt
最后一行：{[THP][teamA][teamB][result][datetime location][event]}

卡牌格式：{rank}{suit}
  rank: 23456789TJQKA (A=1点/T=10/J=11/Q=12/K=13)
  suit: h=红桃 s=黑桃 d=方块 c=梅花
  注意：THP 的 A 是"1点"（即 rank=12 → A），T=10（rank=9 → T）

动作格式：
  r{amount} = raise/bet（不区分）
  c = check/call（不区分）
  f = fold

阶段分隔：/
手牌格式：BB手牌|SB手牌
公共牌：flop/turn/river 用 / 分隔
筹码：A赢筹码|B赢筹码（正=赢，负=输，平=0）
"""
from __future__ import annotations

import os
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── 卡牌转换 ──
# TCP 协议: suit 0=♠,1=♥,2=♦,3=♣; rank 0=2..12=A
# THP 格式: suit s=♠,h=♥,d=♦,c=♣; rank 23456789TJQKA

_TCP_SUIT_TO_THP = {0: 's', 1: 'h', 2: 'd', 3: 'c'}

_RANK_CHARS = list("23456789TJQKA")  # index 0='2', ..., 12='A'


def tcp_card_to_thp(suit: int, rank: int) -> str:
    """TCP (suit, rank) → THP 牌面字符串，如 'Ah', 'Ts'。"""
    return _RANK_CHARS[rank] + _TCP_SUIT_TO_THP[suit]


@dataclass
class HandRecord:
    """单手牌的记录。"""
    hand_num: int = 0
    sb_idx: int = -1          # 0 或 1
    bb_idx: int = -1

    # 手牌：按 player index 存储
    player_cards: dict = field(default_factory=dict)  # idx → [(suit,rank), ...]

    # 公共牌
    flop_cards: list = field(default_factory=list)    # [(suit,rank), ...]
    turn_card: Optional[tuple] = None                  # (suit, rank) or None
    river_card: Optional[tuple] = None                 # (suit, rank) or None

    # 各阶段动作：[preflop_actions, flop_actions, turn_actions, river_actions]
    # 每个 actions 是 [(player_idx, action_type, amount_or_none), ...]
    stage_actions: list = field(default_factory=lambda: [[], [], [], []])

    # 结算
    earnings: list = field(default_factory=lambda: [0, 0])  # [player0_earn, player1_earn]


class THPRecorder:
    """记录一场完整 70 局比赛的棋谱。"""

    def __init__(self, team_a_name: str = "A", team_b_name: str = "B"):
        self.team_names = [team_a_name, team_b_name]  # player 0, player 1
        self.records: list[HandRecord] = []
        self._current: Optional[HandRecord] = None
        self._stage_idx = 0  # 0=preflop, 1=flop, 2=turn, 3=river

    def on_hand_start(self, hand_num: int, sb_idx: int, bb_idx: int):
        """新手牌开始。"""
        self._current = HandRecord(
            hand_num=hand_num,
            sb_idx=sb_idx,
            bb_idx=bb_idx,
        )
        self._stage_idx = 0

    def on_hand_cards(self, player_idx: int, cards: list):
        """记录手牌。cards: list of (suit, rank) tuples。"""
        if self._current:
            self._current.player_cards[player_idx] = cards

    def on_stage_cards(self, stage: str, cards: list):
        """记录公共牌。stage: 'flop'/'turn'/'river'。cards: [(suit,rank), ...]。"""
        if not self._current:
            return
        if stage == "flop":
            self._current.flop_cards = list(cards)
            self._stage_idx = 1
        elif stage == "turn":
            self._current.turn_card = cards[0] if cards else None
            self._stage_idx = 2
        elif stage == "river":
            self._current.river_card = cards[0] if cards else None
            self._stage_idx = 3

    def on_action(self, player_idx: int, action_type: str, amount=None):
        """记录一个动作。action_type: 'call'/'check'/'fold'/'raise'/'allin'。"""
        if self._current:
            self._current.stage_actions[self._stage_idx].append(
                (player_idx, action_type, amount)
            )

    def on_settle(self, earnings: list):
        """记录结算。earnings: [player0_earn, player1_earn]。"""
        if self._current:
            self._current.earnings = list(earnings)
            self.records.append(self._current)
            self._current = None

    # ── 格式化输出 ──

    def _format_card(self, card_tuple) -> str:
        """(suit, rank) → THP 字符串。"""
        suit, rank = card_tuple
        return tcp_card_to_thp(suit, rank)

    def _format_cards(self, cards: list) -> str:
        """多张牌连续格式化。"""
        return "".join(self._format_card(c) for c in cards)

    def _format_action(self, player_idx: int, action_type: str, amount) -> str:
        """格式化单个动作。"""
        if action_type in ("raise", "allin"):
            # r 后跟筹码量（raise-to-total 取增量作为下注量）
            # THP 规范中 r 表示"向筹码池中加注筹码量"，即增量
            # 但实际上规范写 r223 表示 raise 223（金额就那样）
            # allin 的 amount 是全部剩余筹码
            return f"r{amount}" if amount is not None else "r0"
        elif action_type in ("call", "check"):
            return "c"
        elif action_type == "fold":
            return "f"
        return "c"  # 默认

    def _format_stage_actions(self, actions: list) -> str:
        """格式化一个阶段的所有动作。"""
        return "".join(
            self._format_action(idx, atype, amt)
            for idx, atype, amt in actions
        )

    def format_hand(self, rec: HandRecord) -> str:
        """格式化一手牌的完整 THP 记录行。"""
        bb_idx = rec.bb_idx
        sb_idx = rec.sb_idx

        # STATE:N
        parts = [f"STATE:{rec.hand_num}"]

        # 各阶段动作，用 / 分隔
        stage_strs = []
        for stage_actions in rec.stage_actions:
            stage_strs.append(self._format_stage_actions(stage_actions))
        parts.append("/".join(stage_strs))

        # 手牌 + 公共牌
        # BB手牌|SB手牌/flop/turn/river
        bb_cards = self._format_cards(rec.player_cards.get(bb_idx, []))
        sb_cards = self._format_cards(rec.player_cards.get(sb_idx, []))
        hand_str = f"{bb_cards}|{sb_cards}"

        # 公共牌
        community_parts = []
        if rec.flop_cards:
            community_parts.append(self._format_cards(rec.flop_cards))
        if rec.turn_card is not None:
            community_parts.append(self._format_card(rec.turn_card))
        if rec.river_card is not None:
            community_parts.append(self._format_card(rec.river_card))

        if community_parts:
            hand_str += "/" + "/".join(community_parts)
        parts.append(hand_str)

        # 筹码：player0赢筹码|player1赢筹码
        parts.append(f"{rec.earnings[0]}|{rec.earnings[1]}")

        # 参赛者：player0_name|player1_name
        parts.append(f"{self.team_names[0]}|{self.team_names[1]}")

        return ":".join(parts) + ";"

    def format_footer(self, event_name: str = "", location: str = "") -> str:
        """格式化文件末尾的总成绩行。"""
        total = [0, 0]
        for rec in self.records:
            total[0] += rec.earnings[0]
            total[1] += rec.earnings[1]

        # 确定胜者描述
        if total[0] > total[1]:
            result = f"{self.team_names[0]}赢得{total[0]}个筹码"
        elif total[1] > total[0]:
            result = f"{self.team_names[1]}赢得{total[1]}个筹码"
        else:
            result = f"平局，双方各{total[0]}个筹码"

        now = datetime.now()
        dt_str = now.strftime("%Y.%m.%d %H:%M")
        if location:
            dt_str += f" {location}"

        event_str = event_name or "CCGC"

        return (
            f"{{[THP][{self.team_names[0]}][{self.team_names[1]}]"
            f"[{result}][{dt_str}][{event_str}]}}"
        )

    def export_string(self, event_name: str = "", location: str = "") -> str:
        """导出完整棋谱文本。"""
        lines = []
        for rec in self.records:
            lines.append(self.format_hand(rec))
        lines.append(self.format_footer(event_name, location))
        return "\n".join(lines) + "\n"

    def export_file(self, filepath: str, event_name: str = "", location: str = ""):
        """导出棋谱到文件。"""
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
        content = self.export_string(event_name, location)
        with open(filepath, "w", encoding="gb2312", errors="replace") as f:
            f.write(content)
        logger.info(f"THP record exported to {filepath} ({len(self.records)} hands)")
