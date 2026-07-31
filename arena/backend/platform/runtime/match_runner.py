"""平台对战引擎(里程碑 4 第四组件)。

用两个 ``DockerRunner`` session + 两个 ``BotZoneJsonAdapter``,驱动一场完整的
heads-up 德州扑克对战(70 手)。规则逻辑借鉴 ``engine/game.py`` 的
``_run_hand`` / ``_betting_round`` / ``_settle_fold`` / ``_showdown``,
但通信通道改为 DockerRunner + JSON 协议(不 import / 改造现有 game.py)。

**关键设计**(对照 CONTRACT.md 数据流):

- 每个 bot 一个 ``BotZoneJsonAdapter`` 实例(状态独立,累积请求历史)。
- 每个事件(hand_start/hole_cards/community/action/settle)→ 两个 adapter
  都调对应 ``on_*`` 更新内部状态。adapter 之间不直接通信。
- 每个 decision point → 决策方的 adapter ``build_request`` → ``runner.send``
  → adapter ``parse_response`` → 回到规则循环。
- 超时 / 非法 / 断开 → fold(对齐 game.py:302-337)。
- SB/BB 交替,70 手,每手筹码复位 20000,盲注 50/100。
- 事件流(hand_start/action/settle/match_end)通过 ``event_sink`` 回调
  (里程碑 5 用来写 DB + SSE)。

复用现有 engine: ``deck.Deck`` 发牌、``evaluator.compare_hands`` 判胜、
``validator.validate_action`` 校验合法性、``protocol_adapter.BotZoneJsonAdapter``
做协议翻译。不重写德扑规则。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ...engine.deck import Card, Deck
from ...engine.evaluator import best_hand, compare_hands, hand_name
from ...engine.protocol_adapter import BotZoneJsonAdapter
from ...engine.validator import (
    BIG_BLIND, SMALL_BLIND, validate_action,
)
from .docker_runner import DEFAULT_ACTION_TIMEOUT, DockerRunner

logger = logging.getLogger(__name__)

# 对战配置(与 engine/game.py 对齐)
HANDS_PER_MATCH = 70
INITIAL_CHIPS = 20000

# 事件类型(供 event_sink 消费方,DB + SSE 用)
EVT_HAND_START = "hand_start"
EVT_ACTION = "action"
EVT_SETTLE = "settle"
EVT_MATCH_END = "match_end"
EVT_CARDS_DEALT = "cards_dealt"
EVT_STAGE = "stage"

# 可调用 event_sink:EventSink = Callable[[dict], Awaitable[None] | None]
EventSink = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class _Player:
    """单方玩家状态(每手复位)。"""
    idx: int
    name: str = ""
    image: str = ""
    session_id: str = ""
    adapter: BotZoneJsonAdapter | None = None
    chips: int = INITIAL_CHIPS
    hand_cards: list[Card] = field(default_factory=list)
    blind_type: str = ""        # SMALLBLIND / BIGBLIND
    folded: bool = False


@dataclass
class _BettingResult:
    """下注轮结果(对齐 game.py BettingResult)。"""
    folded: bool = False
    winner_idx: int = -1
    pot: int = 0
    community: list[Card] = field(default_factory=list)
    allin_settled: bool = False


class MatchRunner:
    """驱动一场完整 heads-up 对战(70 手)。

    用法::

        runner = DockerRunner()
        match = MatchRunner(runner, hands_per_match=70, action_timeout=60)
        result = await match.run_match(
            image_a="arena-bot-1:v1", image_b="arena-bot-2:v1",
            name_a="BotA", name_b="BotB",
        )
        # result = {"winner", "earnings", "hands_played", "events"}
        await runner.cleanup_all()

    event_sink: 可选 ``Callable[[dict], None | Awaitable]``,每个事件回调。
    deck_factory: 可选 ``Callable[[hand_num, sb_idx, bb_idx], Deck]``,
        测试用确定性发牌。默认随机 Deck()。
    """

    def __init__(
        self,
        runner: DockerRunner,
        *,
        hands_per_match: int = HANDS_PER_MATCH,
        action_timeout: float = DEFAULT_ACTION_TIMEOUT,
        event_sink: EventSink | None = None,
        deck_factory: Callable[[int, int, int], Deck] | None = None,
    ) -> None:
        self.runner = runner
        self.hands_per_match = hands_per_match
        self.action_timeout = float(action_timeout)
        self.event_sink = event_sink
        self.deck_factory = deck_factory
        self.players: list[_Player] = [_Player(idx=0), _Player(idx=1)]
        self.hand_num = 0
        self.total_earnings = [0, 0]
        self.events: list[dict[str, Any]] = []
        # bot 容器崩溃 / stdout 关闭等不可恢复通信错误 → 本手结束后终止对局
        self._abort_match = False
        self._abort_reason = ""

    # ── 主入口 ─────────────────────────────────────────────

    async def run_match(
        self,
        image_a: str,
        image_b: str,
        name_a: str,
        name_b: str,
    ) -> dict[str, Any]:
        """运行完整对战。返回结果 dict。

        返回结构::

            {
                "winner": 0 | 1 | None,    # None = 平局
                "earnings": [int, int],     # 累计 earnings
                "hands_played": int,
                "events": list[dict],       # 所有事件
                "names": [name_a, name_b],
            }
        """
        # 起两个 session
        self.players[0].image = image_a
        self.players[0].name = name_a
        self.players[0].adapter = BotZoneJsonAdapter(max_hand=self.hands_per_match)
        self.players[1].image = image_b
        self.players[1].name = name_b
        self.players[1].adapter = BotZoneJsonAdapter(max_hand=self.hands_per_match)

        try:
            self.players[0].session_id = await self.runner.start_session(
                image_a, name_hint=name_a)
            self.players[1].session_id = await self.runner.start_session(
                image_b, name_hint=name_b)

            for hand_num in range(1, self.hands_per_match + 1):
                self.hand_num = hand_num
                result = await self._run_hand(hand_num)
                if result is None:
                    break
                self.total_earnings[0] += result.earnings[0]
                self.total_earnings[1] += result.earnings[1]
                if self._abort_match:
                    logger.warning(
                        "abort match after hand %d: %s",
                        hand_num, self._abort_reason or "bot session dead")
                    break
        finally:
            # 对战结束停 session(即使异常也清理)
            for p in self.players:
                if p.session_id:
                    try:
                        await self.runner.stop_session(p.session_id)
                    except Exception:
                        logger.exception("stop session %s failed", p.session_id)
                    p.session_id = ""

        winner = (0 if self.total_earnings[0] > self.total_earnings[1]
                  else 1 if self.total_earnings[1] > self.total_earnings[0]
                  else None)
        await self._emit(EVT_MATCH_END, {
            "total_earnings": list(self.total_earnings),
            "names": [p.name for p in self.players],
            "hands_played": self.hand_num,
            "winner": winner,
            "aborted": self._abort_match,
            "abort_reason": self._abort_reason or None,
        })
        return {
            "winner": winner,
            "earnings": list(self.total_earnings),
            "hands_played": self.hand_num,
            "events": self.events,
            "names": [name_a, name_b],
            "aborted": self._abort_match,
            "abort_reason": self._abort_reason or None,
        }

    # ── 单手流程(借鉴 game.py _run_hand)─────────────────────

    def _new_deck(self, hand_num: int, sb_idx: int, bb_idx: int) -> Deck:
        if self.deck_factory is not None:
            return self.deck_factory(hand_num, sb_idx, bb_idx)
        return Deck()

    async def _run_hand(self, hand_num: int):
        """运行一手牌完整流程。"""
        sb_idx = (hand_num - 1) % 2  # 奇数局 player0=SB, 偶数局 player1=SB
        bb_idx = 1 - sb_idx
        deck = self._new_deck(hand_num, sb_idx, bb_idx)

        # 一局一复位
        for p in self.players:
            p.chips = INITIAL_CHIPS
            p.hand_cards = []
            p.blind_type = ""
            p.folded = False

        sb = self.players[sb_idx]
        bb = self.players[bb_idx]

        # 发手牌
        sb.hand_cards = deck.deal(2)
        bb.hand_cards = deck.deal(2)
        sb.blind_type = "SMALLBLIND"
        bb.blind_type = "BIGBLIND"

        # 下盲注
        sb.chips -= SMALL_BLIND
        bb.chips -= BIG_BLIND
        pot = SMALL_BLIND + BIG_BLIND

        # 通知 adapter:新手牌 + 手牌
        for p in self.players:
            assert p.adapter is not None
            p.adapter.on_hand_start(hand_num, sb_idx, bb_idx, dealer_id=sb_idx)
            p.adapter.on_hole_cards(p.idx, list(p.hand_cards))

        await self._emit(EVT_HAND_START, {
            "hand": hand_num, "sb_idx": sb_idx, "bb_idx": bb_idx,
            "names": [p.name for p in self.players],
            "player_chips": [p.chips for p in self.players],
            "pot": pot,
        })
        await self._emit(EVT_CARDS_DEALT, {
            "hand": hand_num,
            "hole_cards": [
                [c.to_str() for c in self.players[0].hand_cards],
                [c.to_str() for c in self.players[1].hand_cards],
            ],
        })

        community: list[Card] = []

        # 各阶段定义:(name, first_idx, second_idx, first_bet, second_bet)
        stages = [
            ("preflop", sb_idx, bb_idx, SMALL_BLIND, BIG_BLIND),
            ("flop", bb_idx, sb_idx, 0, 0),
            ("turn", bb_idx, sb_idx, 0, 0),
            ("river", bb_idx, sb_idx, 0, 0),
        ]

        for i, (stage_name, first, second, fb, sb_bet) in enumerate(stages):
            # 非首阶段:发公共牌 + 通知 adapter
            if stage_name == "flop":
                flop_cards = deck.deal(3)
                community.extend(flop_cards)
                await self._emit_stage("flop", flop_cards)
                for p in self.players:
                    assert p.adapter is not None
                    p.adapter.on_community_cards(list(community))
            elif stage_name == "turn":
                turn_card = deck.deal(1)
                community.extend(turn_card)
                await self._emit_stage("turn", turn_card)
                for p in self.players:
                    assert p.adapter is not None
                    p.adapter.on_community_cards(list(community))
            elif stage_name == "river":
                river_card = deck.deal(1)
                community.extend(river_card)
                await self._emit_stage("river", river_card)
                for p in self.players:
                    assert p.adapter is not None
                    p.adapter.on_community_cards(list(community))

            result = await self._betting_round(
                stage=stage_name, first_idx=first, second_idx=second,
                first_bet=fb, second_bet=sb_bet,
                pot=pot, community=community, deck=deck,
            )

            if result.folded:
                return await self._settle_fold(
                    result.winner_idx, result.pot, community)

            pot = result.pot
            community = result.community

            if result.allin_settled:
                # allin+call 后自动发剩余公共牌,直接 showdown
                stages_done = i + 1
                if stages_done < 2:
                    flop_cards = deck.deal(3)
                    community.extend(flop_cards)
                    await self._emit_stage("flop", flop_cards)
                    for p in self.players:
                        assert p.adapter is not None
                        p.adapter.on_community_cards(list(community))
                    stages_done = 2
                if stages_done < 3:
                    turn_card = deck.deal(1)
                    community.extend(turn_card)
                    await self._emit_stage("turn", turn_card)
                    for p in self.players:
                        assert p.adapter is not None
                        p.adapter.on_community_cards(list(community))
                    stages_done = 3
                if stages_done < 4:
                    river_card = deck.deal(1)
                    community.extend(river_card)
                    await self._emit_stage("river", river_card)
                    for p in self.players:
                        assert p.adapter is not None
                        p.adapter.on_community_cards(list(community))
                return await self._showdown(sb_idx, bb_idx, community, pot)

        return await self._showdown(sb_idx, bb_idx, community, pot)

    async def _betting_round(self, stage, first_idx, second_idx,
                              first_bet, second_bet,
                              pot, community, deck) -> _BettingResult:
        """一轮下注(借鉴 game.py _betting_round)。

        与 TCP 版的区别:不发文本给 bot,而是在每个事件调用 adapter.on_*;
        决策点用 adapter.build_request + runner.send + parse_response。
        """
        bets = {first_idx: first_bet, second_idx: second_bet}
        action_counts = {first_idx: 0, second_idx: 0}
        actions: list[tuple] = []  # 本阶段行动历史 [(action_type, amount), ...]
        allin_occurred = False

        current_idx = first_idx
        waiting_idx = second_idx

        for _ in range(100):  # 安全上限
            current = self.players[current_idx]
            waiting = self.players[waiting_idx]
            assert current.adapter is not None and waiting.adapter is not None

            is_sb = (current.blind_type == "SMALLBLIND")
            is_bb = (current.blind_type == "BIGBLIND")
            available = current.chips

            # 与 game.py 一致的 game_state(供 validator.validate_action)
            game_state = {
                "stage": stage,
                "actions": actions,
                "player_chips": available,
                "player_bet": bets[current_idx],
                "opponent_bet": bets[waiting_idx],
                "is_small_blind": is_sb,
                "is_big_blind": is_bb,
                "allin_occurred": allin_occurred,
                "player_action_count": action_counts[current_idx],
            }

            # 决策点:问 bot
            to_call = max(0, bets[waiting_idx] - bets[current_idx])
            action_type, action_amount, decision_timing = await self._ask_bot(
                current_idx=current_idx, waiting_idx=waiting_idx,
                stage=stage, pot=pot, bets=bets, to_call=to_call,
                game_state=game_state,
            )

            # 超时 → fold
            if action_type == "timeout":
                logger.info("[H%d] %s: %s timeout → fold",
                            self.hand_num, stage, current.name)
                current.folded = True
                waiting.adapter.on_opponent_action(
                    current.idx, "fold", None, _round_idx(stage))
                await self._emit(EVT_ACTION, {
                    "player_idx": current_idx, "action": "timeout",
                    "stage": stage, "hand": self.hand_num,
                    **decision_timing,
                })
                return _BettingResult(folded=True, winner_idx=waiting_idx,
                                      pot=pot, community=community)

            # 非法 → fold
            if action_type == "illegal":
                logger.info("[H%d] %s: %s illegal (%s) → fold",
                            self.hand_num, stage, current.name,
                            decision_timing.get("reason", ""))
                current.folded = True
                waiting.adapter.on_opponent_action(
                    current.idx, "fold", None, _round_idx(stage))
                current.adapter.on_self_action(
                    current.idx, "fold", None, _round_idx(stage))
                await self._emit(EVT_ACTION, {
                    "player_idx": current_idx, "action": "illegal",
                    "reason": decision_timing.get("reason", ""),
                    "stage": stage, "hand": self.hand_num,
                    **{k: v for k, v in decision_timing.items() if k != "reason"},
                })
                return _BettingResult(folded=True, winner_idx=waiting_idx,
                                      pot=pot, community=community)

            action_counts[current_idx] += 1

            # ── fold ──
            if action_type == "fold":
                current.folded = True
                waiting.adapter.on_opponent_action(
                    current.idx, "fold", None, _round_idx(stage))
                current.adapter.on_self_action(
                    current.idx, "fold", None, _round_idx(stage))
                await self._emit(EVT_ACTION, {
                    "player_idx": current_idx, "action": "fold",
                    "stage": stage, "hand": self.hand_num,
                    **decision_timing,
                })
                return _BettingResult(folded=True, winner_idx=waiting_idx,
                                      pot=pot, community=community)

            # ── call ──
            if action_type == "call":
                diff = bets[waiting_idx] - bets[current_idx]
                actual = min(diff, available)
                current.chips -= actual
                bets[current_idx] += actual
                pot += actual
                actions.append(("call", None))
                waiting.adapter.on_opponent_action(
                    current.idx, "call", None, _round_idx(stage))
                current.adapter.on_self_action(
                    current.idx, "call", None, _round_idx(stage))
                await self._emit(EVT_ACTION, {
                    "player_idx": current_idx, "action": "call",
                    "amount": actual, "stage": stage, "hand": self.hand_num,
                    "pot": pot, **decision_timing,
                })
                if current.chips == 0:
                    allin_occurred = True
                if allin_occurred:
                    return _BettingResult(pot=pot, community=community,
                                          allin_settled=True)
                if action_counts[waiting_idx] > 0:
                    break
                current_idx, waiting_idx = waiting_idx, current_idx
                continue

            # ── check ──
            if action_type == "check":
                actions.append(("check", None))
                waiting.adapter.on_opponent_action(
                    current.idx, "check", None, _round_idx(stage))
                current.adapter.on_self_action(
                    current.idx, "check", None, _round_idx(stage))
                await self._emit(EVT_ACTION, {
                    "player_idx": current_idx, "action": "check",
                    "stage": stage, "hand": self.hand_num,
                    **decision_timing,
                })
                # preflop BB check 且 SB 已 call → 阶段结束
                if stage == "preflop" and is_bb and action_counts[current_idx] == 1:
                    if len(actions) >= 2 and actions[-2][0] == "call":
                        break
                current_idx, waiting_idx = waiting_idx, current_idx
                continue

            # ── raise ──
            if action_type == "raise":
                amount = action_amount  # raise-to-total
                needed = amount - bets[current_idx]
                current.chips -= needed
                bets[current_idx] = amount
                pot += needed
                actions.append(("raise", amount))
                waiting.adapter.on_opponent_action(
                    current.idx, "raise", amount, _round_idx(stage))
                current.adapter.on_self_action(
                    current.idx, "raise", amount, _round_idx(stage))
                await self._emit(EVT_ACTION, {
                    "player_idx": current_idx, "action": "raise",
                    "amount": amount, "needed": needed,
                    "stage": stage, "hand": self.hand_num, "pot": pot,
                    **decision_timing,
                })
                current_idx, waiting_idx = waiting_idx, current_idx
                continue

            # ── allin ──
            if action_type == "allin":
                all_in_amount = available
                current.chips = 0
                bets[current_idx] += all_in_amount
                pot += all_in_amount
                allin_occurred = True
                actions.append(("allin", all_in_amount))
                waiting.adapter.on_opponent_action(
                    current.idx, "allin", None, _round_idx(stage))
                current.adapter.on_self_action(
                    current.idx, "allin", None, _round_idx(stage))
                await self._emit(EVT_ACTION, {
                    "player_idx": current_idx, "action": "allin",
                    "amount": all_in_amount,
                    "stage": stage, "hand": self.hand_num, "pot": pot,
                    **decision_timing,
                })
                current_idx, waiting_idx = waiting_idx, current_idx
                continue

            # 未知 → fold
            current.folded = True
            waiting.adapter.on_opponent_action(
                current.idx, "fold", None, _round_idx(stage))
            current.adapter.on_self_action(
                current.idx, "fold", None, _round_idx(stage))
            return _BettingResult(folded=True, winner_idx=waiting_idx,
                                  pot=pot, community=community)

        return _BettingResult(pot=pot, community=community)

    async def _ask_bot(self, *, current_idx, waiting_idx, stage, pot, bets,
                       to_call, game_state) -> tuple[str, int | None, dict]:
        """问 bot 一次决策。返回 (action_type, amount, decision_timing)。

        超时 → ("timeout", None, {...})。
        非法 → ("illegal", None, {"reason": str, ...})。
        """
        current = self.players[current_idx]
        waiting = self.players[waiting_idx]
        assert current.adapter is not None
        # build_request:adapter 累积历史 + 当前快照
        request = current.adapter.build_request(
            my_id=current.idx,
            my_chips=current.chips,
            opponent_chips=waiting.chips,
        )
        t0 = time.perf_counter()
        try:
            response_str = await self.runner.send(
                current.session_id, request, timeout=self.action_timeout)
        except (asyncio.TimeoutError, RuntimeError, OSError) as exc:
            # 完整异常 message 含 docker_runner 的 stderr 尾部(容器崩溃诊断)
            exc_msg = str(exc)
            logger.info("[H%d] %s: bot 通信异常 %s → fold (%s)",
                        self.hand_num, current.name, type(exc).__name__,
                        exc_msg[:200])
            # RuntimeError / OSError 通常表示容器已死,继续打满 70 手无意义
            if isinstance(exc, (RuntimeError, OSError)):
                self._abort_match = True
                # reason 携带异常 message(含 stderr),便于排查偶发崩溃
                self._abort_reason = f"{current.name}:{type(exc).__name__}"
                if exc_msg:
                    self._abort_reason += f": {exc_msg}"[:300]
            return ("timeout", None, {
                "decision_wait_sec": round(time.perf_counter() - t0, 6),
                "timeout_budget_sec": self.action_timeout,
                "error": type(exc).__name__,
            })
        decision_wait_sec = round(time.perf_counter() - t0, 6)
        decision_timing = {
            "decision_wait_sec": decision_wait_sec,
            "timeout_budget_sec": self.action_timeout,
        }

        # parse_response:to_call 决定 0 的 call/check 解读
        try:
            action_type, amount = current.adapter.parse_response(
                response_str, to_call=to_call)
        except (ValueError, KeyError) as exc:
            logger.info("[H%d] %s: response 解析失败 %s → illegal",
                        self.hand_num, current.name, exc)
            decision_timing["reason"] = f"parse_error: {exc}"
            decision_timing["raw"] = response_str
            return ("illegal", None, decision_timing)

        # 校验合法性
        is_legal, reason = validate_action(action_type, amount, game_state)
        if not is_legal:
            decision_timing["reason"] = reason
            decision_timing["raw_action"] = f"{action_type} {amount}"
            return ("illegal", None, decision_timing)

        return (action_type, amount, decision_timing)

    # ── 结算(借鉴 game.py _settle_fold / _showdown)─────────

    async def _settle_fold(self, winner_idx, pot, community):
        """弃牌结算。"""
        loser_idx = 1 - winner_idx
        winner_final = self.players[winner_idx].chips + pot
        loser_final = self.players[loser_idx].chips
        earnings = [0, 0]
        earnings[winner_idx] = winner_final - INITIAL_CHIPS
        earnings[loser_idx] = loser_final - INITIAL_CHIPS

        await self._emit(EVT_SETTLE, {
            "hand": self.hand_num, "is_showdown": False,
            "winner_idx": winner_idx, "pot": pot,
            "earnings": list(earnings),
            "player_chips": [p.chips for p in self.players],
            "reason": f"{self.players[loser_idx].name} folded",
        })
        logger.info("[H%d] %s wins %d (fold)",
                    self.hand_num, self.players[winner_idx].name,
                    earnings[winner_idx])
        return _HandResult(winner_idx=winner_idx, pot=pot,
                           is_showdown=False, earnings=tuple(earnings))

    async def _showdown(self, sb_idx, bb_idx, community, pot):
        """比牌结算(借鉴 game.py _showdown)。"""
        sb = self.players[sb_idx]
        bb = self.players[bb_idx]
        sb_all = sb.hand_cards + community
        bb_all = bb.hand_cards + community
        cmp = compare_hands(sb_all, bb_all)

        if cmp > 0:
            sb_final = sb.chips + pot
            bb_final = bb.chips
        elif cmp < 0:
            sb_final = sb.chips
            bb_final = bb.chips + pot
        else:
            half = pot // 2
            sb_final = sb.chips + half
            bb_final = bb.chips + pot - half

        earnings = [0, 0]
        earnings[sb_idx] = sb_final - INITIAL_CHIPS
        earnings[bb_idx] = bb_final - INITIAL_CHIPS

        sb_rank, _ = best_hand(sb_all)
        bb_rank, _ = best_hand(bb_all)
        winner_idx = sb_idx if cmp >= 0 else bb_idx

        await self._emit(EVT_SETTLE, {
            "hand": self.hand_num, "is_showdown": True,
            "winner_idx": winner_idx if cmp != 0 else None,
            "pot": pot, "earnings": list(earnings),
            "sb_idx": sb_idx, "bb_idx": bb_idx,
            "sb_cards": [c.to_str() for c in sb.hand_cards],
            "bb_cards": [c.to_str() for c in bb.hand_cards],
            "community": [c.to_str() for c in community],
            "sb_hand": hand_name(sb_rank),
            "bb_hand": hand_name(bb_rank),
            "player_chips": [p.chips for p in self.players],
        })
        logger.info("[H%d] Showdown: SB(%s)=%s BB(%s)=%s pot=%d",
                    self.hand_num, sb.name, hand_name(sb_rank),
                    bb.name, hand_name(bb_rank), pot)
        return _HandResult(
            winner_idx=winner_idx if cmp != 0 else None,
            pot=pot, is_showdown=True, earnings=tuple(earnings),
        )

    # ── 事件分发 ───────────────────────────────────────────

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """发事件到 event_sink + 本地 events 列表。"""
        event = {"type": event_type, **data}
        if event_type in (EVT_ACTION, EVT_SETTLE, EVT_STAGE, EVT_HAND_START):
            event["player_chips"] = [p.chips for p in self.players]
        self.events.append(event)
        if self.event_sink is not None:
            result = self.event_sink(event)
            if asyncio.iscoroutine(result):
                await result

    async def _emit_stage(self, stage: str, cards: list[Card]) -> None:
        await self._emit(EVT_STAGE, {
            "stage": stage,
            "cards": [c.to_str() for c in cards],
            "hand": self.hand_num,
        })


class _HandResult:
    """一手结算结果(对齐 game.py HandResult)。"""

    def __init__(self, winner_idx, pot, is_showdown, earnings):
        self.winner_idx = winner_idx
        self.pot = pot
        self.is_showdown = is_showdown
        self.earnings = tuple(earnings)


# ── 辅助 ─────────────────────────────────────────────────

_ROUND_IDX = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}


def _round_idx(stage: str) -> int:
    """stage 名 → round 编号(preflop=0,flop=1,turn=2,river=3)。"""
    return _ROUND_IDX.get(stage, 0)
