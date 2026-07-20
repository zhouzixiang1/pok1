"""MatchRunner 规则逻辑测试(纯 mock,不依赖 docker)。

用 ``MockRunner`` 返回固定动作序列,验证 MatchRunner 正确判定:

1. 两 bot 都 fold → 一方赢盲注(SB preflop fold → BB 赢 50)。
2. 一方 raise 一方 call 到 river showdown → evaluator 判胜。
3. 超时(mock runner 抛 TimeoutError)→ fold。
4. 完整 70 手跑完无异常 → 返回结构正确。
5. 非法 raise → fold。

**不依赖 pytest-asyncio**:测试函数是同步的,内部用 ``asyncio.run()`` 跑异步
``run_match``。这样不引入新依赖(保持现有 103 测试套件零影响)。

MockRunner 实现与 DockerRunner 同接口(start_session/send/stop_session/cleanup_all),
但 send 按每个 session 的预置响应队列返回。
"""
from __future__ import annotations

import asyncio
import json

from arena.backend.engine.deck import Card
from arena.backend.platform.runtime.match_runner import (
    EVT_ACTION, EVT_HAND_START, EVT_MATCH_END, EVT_SETTLE, MatchRunner,
)


def run(coro):
    """跑一个协程并返回结果(测试用,避免 pytest-asyncio 依赖)。"""
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Mock Runner(与 DockerRunner 同接口,不发 docker)─────────────

class MockRunner:
    """伪 DockerRunner:按 session_id 维护响应队列。

    用法::

        runner = MockRunner()
        runner.script("sess-a", [-1, 0, -2])   # sess-a 第1/2/3次决策返回 fold/call/allin
        runner.script("sess-b", [0, 0])
    """

    def __init__(self) -> None:
        self._scripts: dict[str, list[int]] = {}
        self._cursors: dict[str, int] = {}
        self._calls: dict[str, list[str]] = {}  # 记录每次 send 收到的 request(调试)
        self._error_on: dict[str, Exception] = {}  # 触发异常(测超时)
        self._session_counter = 0
        self._first_session = "sess-a"
        self._second_session = "sess-b"

    def script(self, session_id: str, responses: list[int]) -> None:
        self._scripts[session_id] = list(responses)
        self._cursors[session_id] = 0

    def raise_on_send(self, session_id: str, exc: Exception) -> None:
        """让该 session 的 send 永远抛 exc(测超时/断开)。"""
        self._error_on[session_id] = exc

    async def start_session(self, image: str, name_hint: str = "") -> str:
        self._session_counter += 1
        if self._session_counter == 1:
            self._first_session = f"sess-{name_hint or 'a'}"
            self._scripts.setdefault(self._first_session, [])
            self._cursors.setdefault(self._first_session, 0)
            self._calls.setdefault(self._first_session, [])
            return self._first_session
        self._second_session = f"sess-{name_hint or 'b'}"
        self._scripts.setdefault(self._second_session, [])
        self._cursors.setdefault(self._second_session, 0)
        self._calls.setdefault(self._second_session, [])
        return self._second_session

    async def send(self, session_id: str, request: str,
                   timeout: float = 60.0) -> str:
        self._calls.setdefault(session_id, []).append(request)
        if session_id in self._error_on:
            raise self._error_on[session_id]
        script = self._scripts.get(session_id, [])
        cursor = self._cursors.get(session_id, 0)
        if cursor >= len(script):
            # 脚本耗尽:默认 check/call(0),防止卡死
            value = 0
        else:
            value = script[cursor]
            self._cursors[session_id] = cursor + 1
        return json.dumps({"response": int(value)})

    async def stop_session(self, session_id: str) -> None:
        return None

    async def cleanup_all(self) -> None:
        return None


# ── 确定性 deck_factory:控制发牌让 showdown 可预测 ────────────────

def _deck_with(cards_in_order: list[Card]):
    """造一个 deck_factory:每次返回固定顺序发牌的 Deck。

    cards_in_order 顺序:[sb_c1, sb_c2, bb_c1, bb_c2,
                        flop1, flop2, flop3, turn, river, ...]
    """
    class _FakeDeck:
        def __init__(self, hand_num, sb_idx, bb_idx):
            self.cards = list(cards_in_order)
        def deal(self, n):
            dealt = self.cards[:n]
            self.cards = self.cards[n:]
            return dealt
    return lambda hand_num, sb_idx, bb_idx: _FakeDeck(hand_num, sb_idx, bb_idx)


def _card(suit: int, rank: int) -> Card:
    return Card(suit, rank)


# ── 测试 1:两 bot 都 fold → 一方赢盲注 ──────────────────────────

def test_both_fold_bb_wins_blinds():
    """preflop SB 先 fold → BB 直接赢,earnings = +50(SB 投的盲注)。

    SB chips: 20000 - 50(SB) = 19950,fold → BB 拿走 150 底池,
    SB final = 19950(已 fold 不再投),earn = -50。
    BB final = 19900(BB 投100) + 150(底池) = 20050,earn = +50。
    """
    runner = MockRunner()
    match = MatchRunner(runner, hands_per_match=1, action_timeout=2.0,
                        deck_factory=_deck_with([
                            _card(0, 12), _card(1, 11),   # SB: ♠A ♥K
                            _card(2, 10), _card(3, 9),    # BB: ♦Q ♣J
                        ]))
    # 手1:player0=SB,player1=BB。SB 先决策 → fold(-1)。
    # MockRunner 第一个 start_session 是 player0(SB,name_hint=BotA)
    runner.script("sess-BotA", [-1])   # SB fold
    runner.script("sess-BotB", [])     # BB 不决策(SB fold 即结束)

    result = run(match.run_match(
        image_a="img-a", image_b="img-b",
        name_a="BotA", name_b="BotB"))

    assert result["hands_played"] == 1
    # SB(player0)输 50,BB(player1)赢 50
    assert result["earnings"] == [-50, 50]
    assert result["winner"] == 1  # BB 赢
    # settle 事件应有 1 个,winner_idx=1
    settles = [e for e in result["events"] if e["type"] == EVT_SETTLE]
    assert len(settles) == 1
    assert settles[0]["winner_idx"] == 1
    assert settles[0]["is_showdown"] is False


# ── 测试 2:raise + call 到 river showdown → evaluator 判胜 ──────

def test_raise_call_showdown_evaluator_wins():
    """SB raise 到 200(preflop 合法最小),BB call,后续每条街 BB bet + SB call 到 river。

    牌局设计:SB 拿 AA(对A,强),BB 拿 KK(对K,弱),公共牌散乱不强化任何一方。
    → SB 一对 A 击败 BB 一对 K。

    **平台规则关键点**(validator 规则 4):postflop 不能 check-check,
    只能 bet+call 结束一条街。所以每条街 BB 先 bet 100,SB call。
    """
    deck_cards = [
        _card(0, 12), _card(1, 12),   # SB: ♠A ♥A
        _card(0, 11), _card(1, 11),   # BB: ♠K ♥K
        # flop: ♦2 ♣4 ♦6(不接 SB/BB 手牌)
        _card(2, 0), _card(3, 2), _card(2, 4),
        # turn
        _card(3, 6),   # ♣8
        # river
        _card(2, 8),   # ♦10
    ]
    runner = MockRunner()
    match = MatchRunner(runner, hands_per_match=1, action_timeout=2.0,
                        deck_factory=_deck_with(deck_cards))
    # 手1:player0=SB 先决策(preflop)。
    # preflop: SB raise 200(合法), BB call(0, to_call=100>0 → call)
    # flop/turn/river: BB 先(postflop BB 先)→ raise 100(合法最小); SB call
    runner.script("sess-BotA", [200, 0, 0, 0])      # SB: preflop raise, flop call, turn call, river call
    runner.script("sess-BotB", [0, 100, 100, 100])  # BB: preflop call, flop bet, turn bet, river bet

    result = run(match.run_match(
        image_a="img-a", image_b="img-b",
        name_a="BotA", name_b="BotB"))

    assert result["hands_played"] == 1
    settles = [e for e in result["events"] if e["type"] == EVT_SETTLE]
    assert len(settles) == 1
    assert settles[0]["is_showdown"] is True
    # SB(player0,AA)赢 BB(player1,KK)
    assert settles[0]["winner_idx"] == 0
    assert result["winner"] == 0
    # SB 赢 BB 投入的筹码,零和
    assert result["earnings"][0] > 0
    assert result["earnings"][1] < 0
    assert result["earnings"][0] == -result["earnings"][1]


# ── 测试 3:超时 → fold ──────────────────────────────────────

def test_timeout_results_in_fold():
    """MockRunner 让 SB 的 send 抛 TimeoutError → SB 判 fold,BB 赢盲注。"""
    runner = MockRunner()
    match = MatchRunner(runner, hands_per_match=1, action_timeout=0.1,
                        deck_factory=_deck_with([
                            _card(0, 12), _card(1, 11),
                            _card(2, 10), _card(3, 9),
                        ]))
    # SB(player0)send 永远超时
    runner.raise_on_send("sess-BotA", asyncio.TimeoutError())
    runner.script("sess-BotB", [])

    result = run(match.run_match(
        image_a="img-a", image_b="img-b",
        name_a="BotA", name_b="BotB"))

    assert result["hands_played"] == 1
    # SB 超时 fold → BB 赢,与 test1 相同
    assert result["earnings"] == [-50, 50]
    assert result["winner"] == 1
    # 应有 timeout action 事件
    timeouts = [e for e in result["events"]
                if e["type"] == EVT_ACTION and e["action"] == "timeout"]
    assert len(timeouts) == 1
    assert timeouts[0]["player_idx"] == 0


# ── 测试 4:完整 70 手跑完无异常 ────────────────────────────────

def test_full_70_hands_runs_clean():
    """70 手完整跑完:每手先决策方都 fold(快速结束),验证结构正确。

    SB/BB 交替(奇数手 p0=SB,偶数手 p1=SB),每手 preflop SB 先决策 → fold,
    BB 直接赢盲注。双方各 35 手当 SB(fold 输 50)/当 BB(赢 50)→ 净 0,平局。

    让两个 bot 都脚本「只要被问就 fold」,preflop 只有 SB 被问(BB 不会有机会)。
    """
    runner = MockRunner()
    match = MatchRunner(runner, hands_per_match=70, action_timeout=1.0)

    # 两个 bot 都「被问就 fold」;每手 preflop SB 先被问 → fold 结束。
    runner.script("sess-BotA", [-1] * 70)
    runner.script("sess-BotB", [-1] * 70)

    result = run(match.run_match(
        image_a="img-a", image_b="img-b",
        name_a="BotA", name_b="BotB"))

    assert result["hands_played"] == 70
    # SB/BB 交替:奇数手 p0=SB fold(-50),偶数手 p1=SB fold(-50)。
    # 各 35 手当 SB 输 50,各 35 手当 BB 赢 50 → 净 0,平局。
    assert result["earnings"] == [0, 0]
    assert result["winner"] is None  # 平局(earnings 相等)
    # match_end 事件
    ends = [e for e in result["events"] if e["type"] == EVT_MATCH_END]
    assert len(ends) == 1
    assert ends[0]["hands_played"] == 70
    # 70 个 settle
    settles = [e for e in result["events"] if e["type"] == EVT_SETTLE]
    assert len(settles) == 70


# ── 测试 5:非法 raise → fold ───────────────────────────────────

def test_illegal_raise_becomes_fold():
    """preflop SB 首次 raise < 200(非法)→ 判 fold,BB 赢盲注。"""
    runner = MockRunner()
    match = MatchRunner(runner, hands_per_match=1, action_timeout=2.0,
                        deck_factory=_deck_with([
                            _card(0, 12), _card(1, 11),
                            _card(2, 10), _card(3, 9),
                        ]))
    # SB raise 101(preflop SB 首次 raise 必须 ≥ 200 → 非法)
    runner.script("sess-BotA", [101])
    runner.script("sess-BotB", [])

    result = run(match.run_match(
        image_a="img-a", image_b="img-b",
        name_a="BotA", name_b="BotB"))

    assert result["hands_played"] == 1
    # SB 非法 → fold,与超时同结果
    assert result["earnings"] == [-50, 50]
    assert result["winner"] == 1
    illegal = [e for e in result["events"]
               if e["type"] == EVT_ACTION and e["action"] == "illegal"]
    assert len(illegal) == 1
    assert "preflop SB first raise" in illegal[0]["reason"]


# ── 测试 6:同步 event_sink 回调 ────────────────────────────────

def test_event_sink_invoked():
    """同步 event_sink 被触发,收到所有事件。"""
    sink_events: list[dict] = []

    def sink(event: dict) -> None:
        sink_events.append(event)

    runner = MockRunner()
    match = MatchRunner(runner, hands_per_match=1, action_timeout=2.0,
                        event_sink=sink,
                        deck_factory=_deck_with([
                            _card(0, 12), _card(1, 11),
                            _card(2, 10), _card(3, 9),
                        ]))
    runner.script("sess-BotA", [-1])
    runner.script("sess-BotB", [])

    result = run(match.run_match(
        image_a="img-a", image_b="img-b",
        name_a="BotA", name_b="BotB"))

    # sink 收到的事件应与 result["events"] 一致
    assert len(sink_events) == len(result["events"])
    types = [e["type"] for e in sink_events]
    assert EVT_HAND_START in types
    assert EVT_ACTION in types
    assert EVT_SETTLE in types
    assert EVT_MATCH_END in types


def test_async_event_sink_invoked():
    """异步 event_sink 也正常工作。"""
    sink_events: list[dict] = []

    async def sink(event: dict) -> None:
        await asyncio.sleep(0)
        sink_events.append(event)

    runner = MockRunner()
    match = MatchRunner(runner, hands_per_match=1, action_timeout=2.0,
                        event_sink=sink,
                        deck_factory=_deck_with([
                            _card(0, 12), _card(1, 11),
                            _card(2, 10), _card(3, 9),
                        ]))
    runner.script("sess-BotA", [-1])
    runner.script("sess-BotB", [])

    run(match.run_match(image_a="a", image_b="b", name_a="A", name_b="B"))
    assert len(sink_events) > 0


# ── 测试 7:allin + call → 自动发剩余牌直接 showdown ──────────

def test_allin_call_runs_out_board():
    """SB allin,BB call → 自动发完 board 直接 showdown。

    设计:SB 强牌(AA),BB 弱牌(72o)。SB allin(-2),BB call(0)。
    全部筹码进 pot。SB(AA)击溃 BB(72o)。
    """
    deck_cards = [
        _card(0, 12), _card(1, 12),   # SB: ♠A ♥A(对A)
        _card(0, 5), _card(1, 0),     # BB: ♠7 ♥2(72o,弱)
        _card(2, 0), _card(3, 2), _card(2, 4),  # flop ♦2 ♣4 ♦6
        _card(3, 6),   # turn ♣8
        _card(2, 8),   # river ♦10
    ]
    runner = MockRunner()
    match = MatchRunner(runner, hands_per_match=1, action_timeout=2.0,
                        deck_factory=_deck_with(deck_cards))
    # preflop SB allin(-2),BB call(0)
    runner.script("sess-BotA", [-2])   # SB allin
    runner.script("sess-BotB", [0])    # BB call

    result = run(match.run_match(
        image_a="img-a", image_b="img-b",
        name_a="BotA", name_b="BotB"))

    assert result["hands_played"] == 1
    settles = [e for e in result["events"] if e["type"] == EVT_SETTLE]
    assert len(settles) == 1
    assert settles[0]["is_showdown"] is True
    # SB(AA)赢,earnings ±20000(全筹码)
    assert settles[0]["winner_idx"] == 0
    assert result["earnings"] == [20000, -20000]


# ── 桥协议翻译单元测试(tcp_bridge 的纯函数)─────────────────────

from arena.backend.platform.runtime.tcp_bridge import (  # noqa: E402
    BridgeState, json_int_to_tcp_card, tcp_action_to_json_int, translate_request,
)
from arena.backend.engine.protocol_adapter import card_to_json_int
from arena.backend.engine.deck import Card as _C2


def test_bridge_card_translation_roundtrip():
    """平台 Card → JSON int → 国赛 <suit,rank> 文本 roundtrip 保持一致性。

    对照 protocol_adapter 的 card_to_json_int:同一张平台 Card,
    桥反向翻译得到的 <suit,rank> 必须与原 Card 的 (suit, rank) 一致。
    """
    for suit in range(4):
        for rank in range(13):
            card = _C2(suit, rank)
            j = card_to_json_int(card)
            tcp = json_int_to_tcp_card(j)
            # tcp 形如 <suit,rank>
            assert tcp == f"<{suit},{rank}>", f"{card} → json {j} → tcp {tcp}"


def test_bridge_action_text_to_json():
    """国赛文本动作 → JSON int(对照 bots/national_v142/national_bot.py)。"""
    assert tcp_action_to_json_int("fold") == -1
    assert tcp_action_to_json_int("call") == 0
    assert tcp_action_to_json_int("check") == 0
    assert tcp_action_to_json_int("allin") == -2
    assert tcp_action_to_json_int("raise 400") == 400
    assert tcp_action_to_json_int("raise 0") == 0
    # 未知 → 0(call/check,平台按 to_call 决定)
    assert tcp_action_to_json_int("garbage") == 0
    assert tcp_action_to_json_int("") == 0


def test_bridge_translate_new_hand_sb():
    """新手牌 SB 视角:发 preflop|SMALLBLIND|<cards>。"""
    state = BridgeState()
    req = {
        "my_id": 0, "dealer_id": 0,   # my_id==dealer_id → SB
        "my_cards": [card_to_json_int(_C2(0, 12)), card_to_json_int(_C2(1, 11))],
        "public_cards": [], "history": [],
        "hand": 1, "max_hand": 70,
        "my_chips": 19950, "opponent_chips": 19900,
    }
    msgs = translate_request(state, req)
    assert msgs[0].startswith("preflop|SMALLBLIND|")
    # 卡牌翻译:<0,12><1,11>(原平台 suit/rank)
    assert "<0,12>" in msgs[0] and "<1,11>" in msgs[0]


def test_bridge_translate_bb_after_sb_call():
    """BB 视角:preflop + 转发对手 call(增量翻译)。"""
    state = BridgeState()
    req = {
        "my_id": 1, "dealer_id": 0,   # BB
        "my_cards": [card_to_json_int(_C2(2, 10))],
        "public_cards": [],
        "history": [{"round": 0, "player_id": 0,
                     "action": 0, "action_type": "call"}],
        "hand": 1, "max_hand": 70,
        "my_chips": 19900, "opponent_chips": 19950,
    }
    msgs = translate_request(state, req)
    # 先 preflop(BB),再对手 call
    assert any(m.startswith("preflop|BIGBLIND|") for m in msgs)
    assert "call" in msgs


def test_bridge_translate_flop_increment():
    """flop 公共牌增量翻译:flop|<3 cards>。"""
    state = BridgeState()
    # 第一手先发 preflop
    req1 = {
        "my_id": 0, "dealer_id": 0,
        "my_cards": [card_to_json_int(_C2(0, 12)), card_to_json_int(_C2(0, 11))],
        "public_cards": [],
        "history": [{"round": 0, "player_id": 0, "action": 0, "action_type": "call"},
                    {"round": 0, "player_id": 1, "action": 0, "action_type": "check"}],
        "hand": 1, "max_hand": 70,
        "my_chips": 20000, "opponent_chips": 20000,
    }
    translate_request(state, req1)
    # flop:public_cards 从 0 → 3
    flop = [_C2(2, 0), _C2(3, 2), _C2(2, 4)]
    req2 = {
        "my_id": 0, "dealer_id": 0,
        "my_cards": req1["my_cards"],
        "public_cards": [card_to_json_int(c) for c in flop],
        "history": req1["history"],
        "hand": 1, "max_hand": 70,
        "my_chips": 20000, "opponent_chips": 20000,
    }
    msgs = translate_request(state, req2)
    # 应只发 flop(对手 check 已在 req1 发过,history 无新条目)
    flop_msgs = [m for m in msgs if m.startswith("flop|")]
    assert len(flop_msgs) == 1
    assert flop_msgs[0] == "flop|<2,0><3,2><2,4>"


def test_bridge_translate_raise_action():
    """对手 raise N → 转发 'raise N'(N 是 stage 总额)。"""
    state = BridgeState()
    req = {
        "my_id": 1, "dealer_id": 0,
        "my_cards": [card_to_json_int(_C2(0, 5))],
        "public_cards": [],
        "history": [{"round": 0, "player_id": 0,
                     "action": 200, "action_type": "raise"}],
        "hand": 1, "max_hand": 70,
        "my_chips": 19900, "opponent_chips": 19800,
    }
    msgs = translate_request(state, req)
    assert "raise 200" in msgs
