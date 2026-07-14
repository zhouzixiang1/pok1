"""国赛原始 TCP 消息编解码。

官方平台不提供换行符或长度前缀，TCP 包边界也不是消息边界。
因此本模块的 tokenizer 只根据官方 token 语法分割原始字符流。
"""
from __future__ import annotations

import re

try:
    from ..engine.deck import Card, cards_to_str, str_to_cards
except ImportError:  # Standalone ``cd sever`` compatibility.
    from engine.deck import Card, cards_to_str, str_to_cards


# ── 服务器 → 客户端 ───────────────────────

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
        if type(amount) is not int or amount < 0:
            raise ValueError("official raise relay requires a non-negative integer")
        return f"raise {amount}"
    if action_type in ("call", "check", "fold", "allin"):
        return action_type
    raise ValueError(f"unsupported official action relay: {action_type!r}")


# ── 客户端 → 服务器 ───────────────────────

CLIENT_FIXED_ACTIONS = ("allin", "check", "call", "fold")
_CLIENT_RAISE_RE = re.compile(r"raise [0-9]+")


def _could_start_client_action(value: str) -> bool:
    """Return whether ``value`` starts, or is a prefix of, an action token."""
    return any(
        word.startswith(value) or value.startswith(word)
        for word in CLIENT_FIXED_ACTIONS
    ) or (
        "raise ".startswith(value) or value.startswith("raise ")
    )


def take_client_action(
    buffer: str,
    *,
    flush_boundary: bool = False,
) -> tuple[str | None, str]:
    """Take one action from an unframed client character stream.

    A numeric raise at the end of ``buffer`` is deliberately deferred until
    ``flush_boundary`` is true: ``raise 2`` may merely be a fragment of
    ``raise 200``.  A following syntactically possible action is an
    unambiguous lexical boundary, so ``raise 200call`` can be split without
    losing ``call``.

    Whitespace is never stripped.  Invalid spacing and trailing characters
    stay in the remainder so the caller can reject the complete decision.
    """
    if not buffer:
        return None, ""

    for word in CLIENT_FIXED_ACTIONS:
        if not buffer.startswith(word):
            continue
        rest = buffer[len(word):]
        if not rest:
            if flush_boundary:
                return word, ""
            return None, buffer
        if _could_start_client_action(rest):
            return word, rest
        return None, buffer

    match = _CLIENT_RAISE_RE.match(buffer)
    if match is not None:
        token = match.group(0)
        rest = buffer[match.end():]
        if not rest:
            if flush_boundary:
                return token, ""
            return None, buffer
        if _could_start_client_action(rest):
            return token, rest
        return None, buffer

    # A valid but incomplete token needs more bytes. Invalid data is retained
    # verbatim and is rejected by the decision receiver at its boundary.
    return None, buffer


def split_client_actions(
    buffer: str,
    *,
    flush_boundary: bool = False,
) -> tuple[list[str], str]:
    """Split as many official client actions as are proven by ``buffer``."""
    messages: list[str] = []
    while buffer:
        message, rest = take_client_action(
            buffer,
            flush_boundary=flush_boundary,
        )
        if message is None:
            return messages, rest
        messages.append(message)
        buffer = rest
    return messages, ""


def client_action_needs_more(buffer: str) -> bool:
    """Whether ``buffer`` is only a strict prefix of one action token.

    Complete fixed actions and raises with at least one digit are boundary
    candidates and are resolved after a short stream-idle interval. Numeric
    candidates still wait for that interval because more digits may follow.
    """
    if not buffer:
        return True
    if any(word.startswith(buffer) and word != buffer for word in CLIENT_FIXED_ACTIONS):
        return True
    if "raise ".startswith(buffer) and buffer != "raise ":
        return True
    return buffer == "raise "


def parse_action(raw: str) -> tuple[str, int | None]:
    """解析客户端行为字符串。

    返回 (action_type, amount)。raise 时 amount 为加注到的阶段总额。
    """
    # 文档要求行为关键字和筹码量之间有且只有一个空格。
    # 不 strip 原始动作；前后空格、多空格、Tab 都按非法格式处理。
    if _CLIENT_RAISE_RE.fullmatch(raw):
        return ("raise", int(raw.split(" ", 1)[1]))
    if raw == "call":
        return ("call", None)
    if raw == "check":
        return ("check", None)
    if raw == "fold":
        return ("fold", None)
    if raw == "allin":
        return ("allin", None)
    # bet 不允许，但需要识别以便返回非法
    if re.fullmatch(r"bet [0-9]+", raw):
        return ("bet", None)
    return ("unknown", None)


# ── 解析服务器消息（供客户端/测试使用） ───────────

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


_CARD_RE = re.compile(r"<\d+,\d+>")
_SERVER_NUMERIC_RE = (
    re.compile(r"earnChips -?[0-9]+"),
    re.compile(r"raise [0-9]+"),
)


def _take_cards_message(
    buffer: str,
    prefix: str,
    count: int,
) -> tuple[str | None, str]:
    if not buffer.startswith(prefix):
        return None, buffer
    pos = len(prefix)
    for _ in range(count):
        match = _CARD_RE.match(buffer, pos)
        if match is None:
            return None, buffer
        pos = match.end()
    return buffer[:pos], buffer[pos:]


def take_server_message(
    buffer: str,
    *,
    flush_boundary: bool = False,
) -> tuple[str | None, str]:
    """Take one official server token from a raw, delimiter-free stream."""
    if not buffer:
        return None, ""
    if buffer.startswith("name"):
        return "name", buffer[4:]
    for blind in ("SMALLBLIND", "BIGBLIND"):
        message, rest = _take_cards_message(
            buffer,
            f"preflop|{blind}|",
            2,
        )
        if message is not None:
            return message, rest
    for prefix, count in (
        ("flop|", 3),
        ("turn|", 1),
        ("river|", 1),
        ("oppo_hands|", 2),
    ):
        message, rest = _take_cards_message(buffer, prefix, count)
        if message is not None:
            return message, rest
    for pattern in _SERVER_NUMERIC_RE:
        match = pattern.match(buffer)
        if match is None:
            continue
        token = match.group(0)
        rest = buffer[match.end():]
        if rest or flush_boundary:
            return token, rest
        return None, buffer
    for word in CLIENT_FIXED_ACTIONS:
        if buffer.startswith(word):
            return word, buffer[len(word):]
    return None, buffer


def split_server_messages(
    buffer: str,
    *,
    flush_boundary: bool = False,
) -> tuple[list[str], str]:
    """Split official server tokens without relying on TCP packet boundaries."""
    messages: list[str] = []
    while buffer:
        message, rest = take_server_message(
            buffer,
            flush_boundary=flush_boundary,
        )
        if message is None:
            return messages, rest
        messages.append(message)
        buffer = rest
    return messages, ""
