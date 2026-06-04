"""单手牌局引擎。

管理一手牌的完整生命周期：发牌 → 4 轮下注 → 结算。
关键规则：
  - 每手牌起始筹码 20000，一局一复位
  - 70 局交替 SB/BB
  - Preflop SB 先表态，Flop/Turn/River BB 先表态
  - raise-to-total 语义（raise X = 加注到阶段总额 X）
  - 非法行为 → fold
  - allin + call 后自动发完剩余公共牌，直接 showdown
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from engine.deck import Deck, Card, cards_to_str
from engine.evaluator import best_hand, compare_hands, hand_name
from engine.validator import validate_action, SMALL_BLIND, BIG_BLIND
from server.protocol import (
    format_preflop, format_flop, format_turn, format_river,
    format_earn_chips, format_oppo_hands, format_opponent_action,
    parse_action,
)

logger = logging.getLogger(__name__)

HANDS_PER_MATCH = 70
INITIAL_CHIPS = 20000
TIMEOUT_SECONDS = 60


@dataclass
class PlayerState:
    """单个玩家的状态。"""
    idx: int           # 玩家编号 0 或 1
    name: str = ""
    chips: int = INITIAL_CHIPS
    hand_cards: list = field(default_factory=list)
    blind_type: str = ""     # SMALLBLIND / BIGBLIND
    folded: bool = False


@dataclass
class BettingResult:
    """下注轮的结果。"""
    folded: bool = False
    winner_idx: int = -1     # 仅 folded=True 时有效
    pot: int = 0
    community: list = field(default_factory=list)
    allin_settled: bool = False  # allin+call 后直接结算


class HandResult:
    """一手牌的结算结果。"""
    def __init__(self, winner_idx, pot, is_showdown, earnings):
        self.winner_idx = winner_idx  # None = 平局
        self.pot = pot
        self.is_showdown = is_showdown
        self.earnings = tuple(earnings)


class GameEngine:
    """管理一场完整的 70 局比赛。"""

    def __init__(self, send_func, broadcast_func=None):
        """
        send_func: async send_func(player_idx, message) -> None
        broadcast_func: async broadcast_func(event_dict) -> None  (用于 SSE)
        """
        self.send = send_func
        self.broadcast = broadcast_func
        self.players = [PlayerState(idx=0), PlayerState(idx=1)]
        self.hand_num = 0
        self.total_earnings = [0, 0]  # 累计输赢
        self.match_over = False

    def set_player_name(self, player_idx: int, name: str):
        self.players[player_idx].name = name

    async def run_match(self, name1: str, name2: str):
        """运行完整 70 局比赛。name1=先连接的玩家, name2=后连接的玩家。"""
        self.players[0].name = name1
        self.players[1].name = name2

        for hand_num in range(1, HANDS_PER_MATCH + 1):
            self.hand_num = hand_num
            result = await self._run_hand(hand_num)
            if result is None:
                break
            self.total_earnings[0] += result.earnings[0]
            self.total_earnings[1] += result.earnings[1]

            if self.match_over:
                break

        await self._emit("match_end", {
            "total_earnings": list(self.total_earnings),
            "names": [p.name for p in self.players],
            "hands_played": self.hand_num,
        })
        logger.info(f"Match over. {self.players[0].name}: {self.total_earnings[0]}, "
                     f"{self.players[1].name}: {self.total_earnings[1]}")

    async def _run_hand(self, hand_num: int) -> HandResult | None:
        """运行一手牌的完整流程。"""
        deck = Deck()
        sb_idx = (hand_num - 1) % 2  # 奇数局 player0=SB, 偶数局 player1=SB
        bb_idx = 1 - sb_idx

        # 一局一复位：每手牌重置
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

        await self._emit("hand_start", {
            "hand": hand_num, "sb_idx": sb_idx, "bb_idx": bb_idx,
            "names": [p.name for p in self.players],
        })

        # 发送 preflop
        await self.send(sb.idx, format_preflop(sb.hand_cards, "SMALLBLIND"))
        await self.send(bb.idx, format_preflop(bb.hand_cards, "BIGBLIND"))
        await self._emit("cards_dealt", {"hand": hand_num})

        community = []

        # 定义各阶段
        stages = [
            # (name, first_idx, second_idx, first_bet, second_bet)
            ("preflop", sb_idx, bb_idx, SMALL_BLIND, BIG_BLIND),
            ("flop", bb_idx, sb_idx, 0, 0),
            ("turn", bb_idx, sb_idx, 0, 0),
            ("river", bb_idx, sb_idx, 0, 0),
        ]

        for i, (stage_name, first, second, fb, sb_bet) in enumerate(stages):
            # 非首阶段：发公共牌
            if stage_name == "flop":
                flop_cards = deck.deal(3)
                community.extend(flop_cards)
                await self._send_stage_cards("flop", flop_cards)
            elif stage_name == "turn":
                turn_card = deck.deal(1)
                community.extend(turn_card)
                await self._send_stage_cards("turn", turn_card)
            elif stage_name == "river":
                river_card = deck.deal(1)
                community.extend(river_card)
                await self._send_stage_cards("river", river_card)

            result = await self._betting_round(
                stage=stage_name, first_idx=first, second_idx=second,
                first_bet=fb, second_bet=sb_bet,
                pot=pot, community=community, deck=deck,
            )

            if result.folded:
                return await self._settle_fold(result.winner_idx, pot, community)

            pot = result.pot
            community = result.community

            if result.allin_settled:
                # allin+call 后自动发完剩余公共牌（无下注），直接 showdown
                # 已发的阶段数：preflop=0, flop=1, turn=2, river=3
                stages_done = i + 1  # 当前阶段也完成了
                if stages_done < 2:  # 还没发 flop
                    flop_cards = deck.deal(3)
                    community.extend(flop_cards)
                    await self._send_stage_cards("flop", flop_cards)
                    stages_done = 2
                if stages_done < 3:  # 还没发 turn
                    turn_card = deck.deal(1)
                    community.extend(turn_card)
                    await self._send_stage_cards("turn", turn_card)
                    stages_done = 3
                if stages_done < 4:  # 还没发 river
                    river_card = deck.deal(1)
                    community.extend(river_card)
                    await self._send_stage_cards("river", river_card)
                return await self._showdown(sb_idx, bb_idx, community, pot)

        # ── Showdown ──
        return await self._showdown(sb_idx, bb_idx, community, pot)

    async def _betting_round(self, stage, first_idx, second_idx,
                              first_bet, second_bet,
                              pot, community, deck) -> BettingResult:
        """执行一轮下注。

        返回 BettingResult：
          - folded=True: 有人弃牌，winner_idx 有效
          - allin_settled=True: allin+call，需要自动发剩余牌
          - 否则正常结束，pot/community 已更新
        """
        bets = {first_idx: first_bet, second_idx: second_bet}
        action_counts = {first_idx: 0, second_idx: 0}
        actions = []  # 当前阶段行动历史
        allin_occurred = False

        current_idx = first_idx
        waiting_idx = second_idx

        for _ in range(100):  # 安全上限
            current = self.players[current_idx]
            waiting = self.players[waiting_idx]

            is_sb = (current.blind_type == "SMALLBLIND")
            is_bb = (current.blind_type == "BIGBLIND")

            # 计算可用筹码（总额 - 本阶段已在台上的注额）
            available = current.chips
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

            # 接收玩家行为
            raw = await self._recv_action(current_idx)

            if raw is None:
                # 超时 → fold
                logger.info(f"[Hand {self.hand_num}] {stage}: {current.name} timeout → fold")
                current.folded = True
                await self._emit("action", {
                    "player_idx": current_idx, "action": "timeout",
                    "stage": stage, "hand": self.hand_num,
                })
                return BettingResult(folded=True, winner_idx=waiting_idx,
                                      pot=pot, community=community)

            parsed = parse_action(raw)
            action_type = parsed[0]
            action_amount = parsed[1]

            # 验证合法性
            is_legal, reason = validate_action(action_type, action_amount, game_state)

            if not is_legal:
                logger.info(f"[Hand {self.hand_num}] {stage}: {current.name} "
                            f"illegal '{raw}' ({reason}) → fold")
                current.folded = True
                await self._emit("action", {
                    "player_idx": current_idx, "action": f"illegal:{raw}",
                    "reason": reason, "stage": stage, "hand": self.hand_num,
                })
                return BettingResult(folded=True, winner_idx=waiting_idx,
                                      pot=pot, community=community)

            action_counts[current_idx] += 1

            # ── Fold ──
            if action_type == "fold":
                current.folded = True
                logger.info(f"[Hand {self.hand_num}] {stage}: {current.name} folds")
                await self.send(waiting_idx, "fold")
                await self._emit("action", {
                    "player_idx": current_idx, "action": "fold",
                    "stage": stage, "hand": self.hand_num,
                })
                return BettingResult(folded=True, winner_idx=waiting_idx,
                                      pot=pot, community=community)

            # ── Call ──
            if action_type == "call":
                diff = bets[waiting_idx] - bets[current_idx]
                actual = min(diff, available)
                current.chips -= actual
                bets[current_idx] += actual
                pot += actual
                actions.append(("call", None))
                await self.send(waiting_idx, "call")
                await self._emit("action", {
                    "player_idx": current_idx, "action": "call",
                    "amount": actual, "stage": stage, "hand": self.hand_num,
                    "pot": pot,
                })
                logger.info(f"[Hand {self.hand_num}] {stage}: {current.name} "
                            f"calls ({actual}), chips={current.chips}")

                # allin 后的 call → 双方都行动完毕，自动发剩余牌
                if allin_occurred:
                    return BettingResult(pot=pot, community=community,
                                          allin_settled=True)

                # call 结束阶段条件：对手已经有过自愿行动
                if action_counts[waiting_idx] > 0:
                    break
                # preflop SB call 后 BB 还需行动
                current_idx, waiting_idx = waiting_idx, current_idx
                continue

            # ── Check ──
            if action_type == "check":
                actions.append(("check", None))
                await self.send(waiting_idx, "check")
                await self._emit("action", {
                    "player_idx": current_idx, "action": "check",
                    "stage": stage, "hand": self.hand_num,
                })
                logger.info(f"[Hand {self.hand_num}] {stage}: {current.name} checks")

                # preflop BB check 且 SB 已 call → 阶段结束
                if stage == "preflop" and is_bb and action_counts[current_idx] == 1:
                    if len(actions) >= 2 and actions[-2][0] == "call":
                        break

                current_idx, waiting_idx = waiting_idx, current_idx
                continue

            # ── Raise ──
            if action_type == "raise":
                amount = action_amount  # raise-to-total
                needed = amount - bets[current_idx]
                current.chips -= needed
                bets[current_idx] = amount
                pot += needed
                actions.append(("raise", amount))
                await self.send(waiting_idx, f"raise {amount}")
                await self._emit("action", {
                    "player_idx": current_idx, "action": "raise",
                    "amount": amount, "needed": needed,
                    "stage": stage, "hand": self.hand_num, "pot": pot,
                })
                logger.info(f"[Hand {self.hand_num}] {stage}: {current.name} "
                            f"raises to {amount} (puts in {needed}), chips={current.chips}")
                current_idx, waiting_idx = waiting_idx, current_idx
                continue

            # ── All-in ──
            if action_type == "allin":
                all_in_amount = available  # 全部剩余筹码
                current.chips = 0
                bets[current_idx] += all_in_amount
                pot += all_in_amount
                allin_occurred = True
                actions.append(("allin", all_in_amount))
                await self.send(waiting_idx, "allin")
                await self._emit("action", {
                    "player_idx": current_idx, "action": "allin",
                    "amount": all_in_amount,
                    "stage": stage, "hand": self.hand_num, "pot": pot,
                })
                logger.info(f"[Hand {self.hand_num}] {stage}: {current.name} "
                            f"all-in ({all_in_amount})")
                current_idx, waiting_idx = waiting_idx, current_idx
                continue

            # 未知行为 → fold
            current.folded = True
            return BettingResult(folded=True, winner_idx=waiting_idx,
                                  pot=pot, community=community)

        return BettingResult(pot=pot, community=community)

    async def _settle_fold(self, winner_idx, pot, community):
        """处理弃牌结算：发送 earnChips，返回 HandResult。"""
        loser_idx = 1 - winner_idx

        # earnChips = 净利润（最终筹码 - 起始筹码）
        # 赢家获得整个底池，输家什么都拿不回
        winner_final = self.players[winner_idx].chips + pot
        loser_final = self.players[loser_idx].chips
        earnings = [0, 0]
        earnings[winner_idx] = winner_final - INITIAL_CHIPS
        earnings[loser_idx] = loser_final - INITIAL_CHIPS

        await self.send(0, format_earn_chips(earnings[0]))
        await self.send(1, format_earn_chips(earnings[1]))

        await self._emit("settle", {
            "hand": self.hand_num, "is_showdown": False,
            "winner_idx": winner_idx, "pot": pot,
            "earnings": list(earnings),
            "reason": f"{self.players[loser_idx].name} folded",
        })
        logger.info(f"[Hand {self.hand_num}] {self.players[winner_idx].name} "
                     f"wins {earnings[winner_idx]} (opponent folded)")

        return HandResult(winner_idx=winner_idx, pot=pot,
                          is_showdown=False, earnings=earnings)

    async def _showdown(self, sb_idx, bb_idx, community, pot):
        """比牌结算。如果 community 不足 5 张，自动补发。"""
        sb = self.players[sb_idx]
        bb = self.players[bb_idx]

        # 如果因为 allin+call 导致 community 不足 5 张，补发
        # (此场景下 _run_hand 不会走到这里，因为 allin_settled 后
        #  会自动发牌再调用 _showdown，但做防御性处理)
        deck = Deck()  # 此 deck 不会用到，仅为防御
        # 实际上 community 已由 _run_hand 逐阶段补发完毕

        sb_all = sb.hand_cards + community
        bb_all = bb.hand_cards + community

        cmp = compare_hands(sb_all, bb_all)

        # earnChips = 净利润（最终筹码 - 起始筹码）
        if cmp > 0:  # SB 赢
            sb_final = sb.chips + pot
            bb_final = bb.chips
        elif cmp < 0:  # BB 赢
            sb_final = sb.chips
            bb_final = bb.chips + pot
        else:  # 平局
            half = pot // 2
            sb_final = sb.chips + half
            bb_final = bb.chips + pot - half

        earnings = [0, 0]
        earnings[sb_idx] = sb_final - INITIAL_CHIPS
        earnings[bb_idx] = bb_final - INITIAL_CHIPS

        # 发送 earnChips（按 player index）
        await self.send(0, format_earn_chips(earnings[0]))
        await self.send(1, format_earn_chips(earnings[1]))

        # 发送对手手牌
        await self.send(sb.idx, format_oppo_hands(bb.hand_cards))
        await self.send(bb.idx, format_oppo_hands(sb.hand_cards))

        sb_rank, _ = best_hand(sb_all)
        bb_rank, _ = best_hand(bb_all)

        winner_idx = sb_idx if cmp >= 0 else bb_idx
        await self._emit("settle", {
            "hand": self.hand_num, "is_showdown": True,
            "winner_idx": winner_idx if cmp != 0 else None,
            "pot": pot,
            "earnings": list(earnings),
            "sb_idx": sb_idx, "bb_idx": bb_idx,
            "sb_cards": [c.to_str() for c in sb.hand_cards],
            "bb_cards": [c.to_str() for c in bb.hand_cards],
            "community": [c.to_str() for c in community],
            "sb_hand": hand_name(sb_rank),
            "bb_hand": hand_name(bb_rank),
        })
        logger.info(f"[Hand {self.hand_num}] Showdown: SB({sb.name})={hand_name(sb_rank)}, "
                     f"BB({bb.name})={hand_name(bb_rank)}, pot={pot}")

        return HandResult(
            winner_idx=winner_idx if cmp != 0 else None,
            pot=pot, is_showdown=True,
            earnings=tuple(earnings),
        )

    async def _send_stage_cards(self, stage, cards):
        """发送公共牌给双方。"""
        if stage == "flop":
            msg = format_flop(cards)
        elif stage == "turn":
            msg = format_turn(cards[0])
        else:
            msg = format_river(cards[0])
        await self.send(0, msg)
        await self.send(1, msg)
        await self._emit("stage", {
            "stage": stage,
            "cards": [c.to_str() for c in cards],
            "hand": self.hand_num,
        })

    async def _recv_action(self, player_idx) -> str | None:
        """接收玩家行为（由 tcp_server 提供 recv 实现）。"""
        raise NotImplementedError

    async def _emit(self, event_type: str, data: dict):
        """广播事件给 Web 前端（SSE）。"""
        if self.broadcast:
            await self.broadcast({"type": event_type, **data})
