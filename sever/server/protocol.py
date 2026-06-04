"""消息编解码模块。

严格按照《通信协议.docx》和《自对弈平台使用及通信协议补充说明.docx》格式。
所有消息以换行符 \\n 结尾，行分隔协议。
"""
from __future__ import annotations
import re
from engine.deck import Card, cards_to_str, str_to_cards


# ── 服务器 → 客户端 ────────────────────────────────────────

def format_name_query() -> str:
    return "name"


def format_preflop(cards: list[Card], blind_type: str) -> str:
    """格式化 preflop 消息。blind_type = 'SMALLBLIND' | 'BIGBLIND'"""
    return f"preflop|{blind_type}|{cards_to_str(cards)}"


def format_flop(cards: list[Card]) -> str:
    return f"flop|{cards_to_str(cards)}"


def format_turn(card: Card) -> str:
    return f"turn|{card.to_str()}"


def format_river(card: Card) -> str:
    return f"river|{card.to_str()}"


def format_earn_chips(amount: int) -> str:
    return f"earnChips {amount}"


def format_oppo_hands(cards: list[Card]) -> str:
    return f"oppo_hands|{cards_to_str(cards)}"


def format_opponent_action(action_type: str, amount: int | None = None) -> str:
    """格式化对手行为，转发给另一方。"""
    if action_type == "raise":
        return f"raise {amount}"
    if action_type in ("call", "check", "fold", "allin"):
        return action_type
    return action_type  # fallback


# ── 客户端 → 服务器 ────────────────────────────────────────

def parse_action(raw: str) -> tuple[str, int | None]:
    """解析客户端行为字符串。

    返回 (action_type, amount)。raise 时 amount 为加注到的阶段总额。
    """
    raw = raw.strip()
    # raise <amount>
    if raw.startswith("raise ") or raw.startswith("raise\t"):
        parts = raw.split(None, 1)
        if len(parts) == 2:
            try:
                return ("raise", int(parts[1]))
            except ValueError:
                return ("unknown", None)
        return ("unknown", None)
    if raw == "call":
        return ("call", None)
    if raw == "check":
        return ("check", None)
    if raw == "fold":
        return ("fold", None)
    if raw == "allin":
        return ("allin", None)
    # bet 不允许，但需要识别以便返回非法
    if raw.startswith("bet ") or raw.startswith("bet\t"):
        return ("bet", None)
    return ("unknown", None)


# ── 解析服务器消息（供客户端/测试使用）─────────────────────

def parse_preflop(msg: str) -> tuple[str, list[Card]]:
    """解析 preflop 消息，返回 (blind_type, cards)。"""
    parts = msg.split("|")
    blind_type = parts[1].strip()
    cards = str_to_cards(parts[2].strip())
    return blind_type, cards


def parse_stage_cards(msg: str) -> list[Card]:
    """解析 flop/turn/river 消息中的公共牌。"""
    parts = msg.split("|")
    return str_to_cards(parts[1].strip())
