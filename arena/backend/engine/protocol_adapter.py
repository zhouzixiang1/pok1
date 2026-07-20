"""协议适配层:统一 GameEngine 对外通信的双协议支持(里程碑 4 核心)。

平台引擎用统一的 ``Card`` 对象和游戏状态跑德扑规则(零协议耦合)。
发给每个 bot 时,按它的协议编码:

- **BotZoneJsonAdapter**:发 BotZone 风格 JSON(stdin/stdout),新平台 Docker bot 用。
  每次决策发送**累积请求历史**(bot 的 main.py 读 requests[] 数组)。
- **TextProtocolAdapter**:发国赛 TCP 文本协议,现有 TCP 通道 + 容器内 TCP bot 用。

两个 bot 永不直接通信,都只跟平台通信 → JSON bot 和 TCP bot 可同场混战。

卡牌编码(关键,易错):
  平台 Card(suit 0-3=♠♥♦♣, rank 0-12=2-A)
  JSON 整数 0-51:card = rank*4 + JUDGE_SUIT,JUDGE_SUIT = TCP_TO_JUDGE_SUIT[suit]
  TCP_TO_JUDGE_SUIT = {0:2, 1:0, 2:1, 3:3}(同 bots/national_v142/national_bot.py:31)
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .deck import Card

# 平台 suit → bot 内部 judge suit(bots/national_v142/national_bot.py:31 同源)
TCP_TO_JUDGE_SUIT = {0: 2, 1: 0, 2: 1, 3: 3}
# 反向:judge suit → 平台 suit
JUDGE_TO_TCP_SUIT = {v: k for k, v in TCP_TO_JUDGE_SUIT.items()}

SMALL_BLIND = 50
BIG_BLIND = 100


def card_to_json_int(card) -> int:
    """平台 Card → JSON 整数(rank*4 + judge_suit)。"""
    return card.rank * 4 + TCP_TO_JUDGE_SUIT[card.suit]


def json_int_to_card_pair(card_int: int) -> tuple[int, int]:
    """JSON 整数 → (平台 suit, rank)。"""
    judge_suit = card_int % 4
    rank = card_int // 4
    return JUDGE_TO_TCP_SUIT[judge_suit], rank


# 动作整数语义(bots/national_v142/main.py sanitize_action)
ACT_ALLIN = -2
ACT_FOLD = -1
ACT_CALL_CHECK = 0  # 0:有 to_call 则 call,否则 check


def action_to_json_int(action_type: str, amount) -> int:
    """平台动作 → JSON 整数。"""
    if action_type == "allin":
        return ACT_ALLIN
    if action_type == "fold":
        return ACT_FOLD
    if action_type in ("call", "check"):
        return ACT_CALL_CHECK
    if action_type == "raise":
        return int(amount)
    raise ValueError(f"未知动作 {action_type}")


def json_int_to_action(value: int, *, to_call: int) -> tuple[str, int | None]:
    """JSON 整数 → 平台动作。

    to_call: 当前需跟注金额(>0 表示有待跟注 → 0 应解读为 call;否则 check)。
    """
    if value == ACT_ALLIN:
        return ("allin", None)
    if value == ACT_FOLD:
        return ("fold", None)
    if value == ACT_CALL_CHECK:
        return ("call", None) if to_call > 0 else ("check", None)
    if value > 0:
        return ("raise", value)
    raise ValueError(f"非法 JSON 动作值 {value}")


class ProtocolAdapter:
    """协议适配器基类。子类实现 build_request / parse_response + 状态维护。

    生命周期由 GameEngine 驱动:on_* 更新内部状态,build_request 构造请求。
    每个对局的两个 bot 各用一个 adapter 实例(状态独立)。
    """

    def on_hand_start(self, hand_num: int, sb_idx: int, bb_idx: int,
                      dealer_id: int) -> None:
        """新手开始。dealer_id = sb_idx(heads-up dealer = SB)。"""
        raise NotImplementedError

    def on_hole_cards(self, player_idx: int, cards: list) -> None:
        raise NotImplementedError

    def on_community_cards(self, cards: list) -> None:
        """累积公共牌(flop 追加3,turn追加1,river追加1)。"""
        raise NotImplementedError

    def on_opponent_action(self, player_idx: int, action_type: str,
                           amount, round_idx: int) -> None:
        """对手动作记入 history。"""
        raise NotImplementedError

    def on_self_action(self, player_idx: int, action_type: str,
                       amount, round_idx: int) -> None:
        """自己上一个动作(更新历史,供下次请求)。"""
        raise NotImplementedError

    def build_request(self, *, my_id: int, my_chips: int,
                      opponent_chips: int) -> str:
        """构造本次决策请求(返回协议字符串)。"""
        raise NotImplementedError

    def parse_response(self, response_str: str, *,
                       to_call: int) -> tuple[str, int | None]:
        """解析 bot 响应。to_call 决定 0 的 call/check 解读。"""
        raise NotImplementedError


class BotZoneJsonAdapter(ProtocolAdapter):
    """BotZone JSON-over-stdin 协议适配器。

    维护本对局(本 bot 视角)的累积请求历史,每次 build_request 发送完整数组。
    bot 每次都收到从手开始到当前的全部请求,据此重建状态决策。
    """

    def __init__(self, *, max_hand: int = 70) -> None:
        self.max_hand = max_hand
        self._reset_all()

    def _reset_all(self) -> None:
        self._requests_history: list[dict] = []  # 累积所有请求(跨手)
        self._current_hand: int = 0
        self._dealer_id: int = 0
        self._my_cards: dict[int, list] = {}  # player_idx → cards
        self._community: list = []
        self._current_history: list[dict] = []  # 本手动作历史
        self._current_round: int = 0
        self._my_id: int | None = None  # 本 adapter 对应的 bot 座位

    # ── 状态更新 ────────────────────────────────────────────

    def on_hand_start(self, hand_num: int, sb_idx: int, bb_idx: int,
                      dealer_id: int) -> None:
        self._current_hand = hand_num
        self._dealer_id = dealer_id
        self._community = []
        self._current_history = []
        self._current_round = 0  # preflop

    def on_hole_cards(self, player_idx: int, cards: list) -> None:
        self._my_cards[player_idx] = list(cards)

    def on_community_cards(self, cards: list) -> None:
        self._community = list(cards)
        # 公共牌数量决定 round:0=preflop,3=flop,4=turn,5=river
        n = len(cards)
        self._current_round = 0 if n == 0 else 1 if n == 3 else 2 if n == 4 else 3

    def _record_action(self, player_idx: int, action_type: str,
                       amount, round_idx: int) -> None:
        self._current_history.append({
            "round": round_idx,
            "player_id": player_idx,
            "action": _action_amount_int(action_type, amount),
            "action_type": action_type,
        })

    def on_opponent_action(self, player_idx: int, action_type: str,
                           amount, round_idx: int) -> None:
        self._record_action(player_idx, action_type, amount, round_idx)

    def on_self_action(self, player_idx: int, action_type: str,
                       amount, round_idx: int) -> None:
        self._record_action(player_idx, action_type, amount, round_idx)

    # ── 构造请求 ────────────────────────────────────────────

    def build_request(self, *, my_id: int, my_chips: int,
                      opponent_chips: int) -> str:
        """构造本次决策请求(累积历史 + 当前快照)。"""
        self._my_id = my_id
        my_cards = self._my_cards.get(my_id, [])
        req = {
            "my_id": my_id,
            "dealer_id": self._dealer_id,
            "my_cards": [card_to_json_int(c) for c in my_cards],
            "public_cards": [card_to_json_int(c) for c in self._community],
            "history": list(self._current_history),
            "hand": self._current_hand,
            "max_hand": self.max_hand,
            "my_chips": my_chips,
            "opponent_chips": opponent_chips,
            "small_blind": SMALL_BLIND,
            "big_blind": BIG_BLIND,
        }
        self._requests_history.append(req)
        return json.dumps({"requests": self._requests_history}, ensure_ascii=False)

    def parse_response(self, response_str: str, *,
                       to_call: int) -> tuple[str, int | None]:
        payload = json.loads(response_str)
        value = int(payload["response"])
        return json_int_to_action(value, to_call=to_call)

    # 测试/调试用
    @property
    def requests_count(self) -> int:
        return len(self._requests_history)


def _action_amount_int(action_type: str, amount) -> int:
    """history 条目的 action 字段(与 action_type 配对的数值)。"""
    if action_type in ("fold",):
        return -1
    if action_type in ("call", "check"):
        return 0
    if action_type == "allin":
        return -2
    if action_type == "raise":
        return int(amount)
    return 0
