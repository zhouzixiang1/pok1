from enum import IntEnum
from itertools import combinations
import random
import json


class Suit(IntEnum):
    HEART = 0  # 红桃
    DIAMOND = 1  # 方块
    SPADE = 2  # 黑桃
    CLUB = 3  # 梅花


class HandType(IntEnum):
    HIGH_CARD = 1  # 高牌
    PAIR = 2  # 一对
    TWO_PAIR = 3  # 两对
    THREE_OF_A_KIND = 4  # 三条
    STRAIGHT = 5  # 顺子
    FLUSH = 6  # 同花
    FULL_HOUSE = 7  # 葫芦
    FOUR_OF_A_KIND = 8  # 四条
    STRAIGHT_FLUSH = 9  # 同花顺


class Card:
    def __init__(self, suit, number):
        self.suit = suit
        self.number = number

    @staticmethod
    def from_int(i):
        return Card(Suit(i % 4), i // 4 + 2)

    def to_int(self):
        return (self.number - 2) * 4 + self.suit.value

    def __lt__(self, other):
        return self.number < other.number


def _is_wheel(cards):
    """检查是否为 wheel 顺子 A-2-3-4-5 (排序后 A=14,5,4,3,2)"""
    return [c.number for c in cards] == [14, 5, 4, 3, 2]


def hand_type_of_cards(cards):
    cards.sort(reverse=True)  # 大牌在先

    # 1. 同花顺 (含 wheel 同花顺 A-2-3-4-5)
    if all(card.suit == cards[0].suit for card in cards) and (
        all(cards[i].number == cards[i + 1].number + 1 for i in range(4)) or
        _is_wheel(cards)
    ):
        return HandType.STRAIGHT_FLUSH
    # 2. 四条
    if cards[0].number == cards[1].number == cards[2].number == cards[3].number or \
        cards[1].number == cards[2].number == cards[3].number == cards[4].number:
        return HandType.FOUR_OF_A_KIND
    # 3. 葫芦
    if cards[0].number == cards[1].number == cards[2].number and cards[3].number == cards[4].number or \
        cards[2].number == cards[3].number == cards[4].number and cards[0].number == cards[1].number:
        return HandType.FULL_HOUSE
    # 4. 同花
    if all(card.suit == cards[0].suit for card in cards):
        return HandType.FLUSH
    # 5. 顺子 (含 wheel 顺子 A-2-3-4-5)
    if all(cards[i].number == cards[i + 1].number + 1 for i in range(4)) or _is_wheel(cards):
        return HandType.STRAIGHT
    # 6. 三条
    if cards[0].number == cards[1].number == cards[2].number or \
        cards[1].number == cards[2].number == cards[3].number or \
        cards[2].number == cards[3].number == cards[4].number:
        return HandType.THREE_OF_A_KIND
    # 7. 两对
    if cards[0].number == cards[1].number and cards[2].number == cards[3].number or \
        cards[0].number == cards[1].number and cards[3].number == cards[4].number or \
        cards[1].number == cards[2].number and cards[3].number == cards[4].number:
        return HandType.TWO_PAIR
    # 8. 一对
    if cards[0].number == cards[1].number or \
        cards[1].number == cards[2].number or \
        cards[2].number == cards[3].number or \
        cards[3].number == cards[4].number:
        return HandType.PAIR
    # 9. 高牌
    return HandType.HIGH_CARD


def compare_cards_for_hand_type(cards1, cards2, hand_type):
    cards1.sort(reverse=True)
    cards2.sort(reverse=True)

    if hand_type == HandType.STRAIGHT_FLUSH:
        h1 = 5 if _is_wheel(cards1) else cards1[0].number
        h2 = 5 if _is_wheel(cards2) else cards2[0].number
        return h1 - h2
    if hand_type == HandType.FOUR_OF_A_KIND:
        if cards1[1].number != cards2[1].number:
            return cards1[1].number - cards2[1].number
        high1 = cards1[4 if cards1[0].number == cards1[1].number else 0].number
        high2 = cards2[4 if cards2[0].number == cards2[1].number else 0].number
        return high1 - high2
    if hand_type == HandType.FULL_HOUSE:
        if cards1[2].number != cards2[2].number:
            return cards1[2].number - cards2[2].number
        pair1 = cards1[4 if cards1[0].number == cards1[2].number else 0].number
        pair2 = cards2[4 if cards2[0].number == cards2[2].number else 0].number
        return pair1 - pair2
    if hand_type == HandType.FLUSH:
        for i in range(5):
            if cards1[i].number != cards2[i].number:
                return cards1[i].number - cards2[i].number
        return 0
    if hand_type == HandType.STRAIGHT:
        h1 = 5 if _is_wheel(cards1) else cards1[0].number
        h2 = 5 if _is_wheel(cards2) else cards2[0].number
        return h1 - h2
    if hand_type == HandType.THREE_OF_A_KIND:
        if cards1[2].number != cards2[2].number:
            return cards1[2].number - cards2[2].number
        cards1 = [card for card in cards1 if card.number != cards1[2].number]
        cards2 = [card for card in cards2 if card.number != cards2[2].number]
        for i in range(2):
            if cards1[i].number != cards2[i].number:
                return cards1[i].number - cards2[i].number
        return 0
    if hand_type == HandType.TWO_PAIR:
        if cards1[1].number != cards2[1].number:
            return cards1[1].number - cards2[1].number
        if cards1[3].number != cards2[3].number:
            return cards1[3].number - cards2[3].number

        def get_single(cards):
            if cards[0].number == cards[1].number and cards[2].number == cards[3].number:
                return cards[4].number
            elif cards[0].number == cards[1].number and cards[3].number == cards[4].number:
                return cards[2].number
            else:
                return cards[0].number

        return get_single(cards1) - get_single(cards2)
    if hand_type == HandType.PAIR:

        def get_pair(cards):
            if cards[0].number == cards[1].number:
                return cards[0].number, cards[2:]
            elif cards[1].number == cards[2].number:
                return cards[1].number, cards[:1] + cards[3:]
            elif cards[2].number == cards[3].number:
                return cards[2].number, cards[:2] + cards[4:]
            else:
                return cards[3].number, cards[:3]

        pair1, cards1 = get_pair(cards1)
        pair2, cards2 = get_pair(cards2)
        if pair1 != pair2:
            return pair1 - pair2
    # else: HandType.HIGH_CARD
    for i in range(len(cards1)):
        if cards1[i].number != cards2[i].number:
            return cards1[i].number - cards2[i].number
    return 0


def find_max_hand_type(full_cards):
    if len(full_cards) < 5:
        return HandType.HIGH_CARD, full_cards[:]
    max_hand_type, max_cards = None, None
    for cards in combinations(full_cards, 5):
        cards = list(cards)
        hand_type = hand_type_of_cards(cards)
        if max_hand_type is None or max_hand_type < hand_type:
            max_hand_type, max_cards = hand_type, cards[:]
        elif hand_type == max_hand_type and \
            compare_cards_for_hand_type(max_cards, cards, hand_type) < 0:
            max_cards = cards[:]
    return max_hand_type, max_cards


def compare_full_cards(full_cards1, full_cards2):
    hand_type1, cards1 = find_max_hand_type(full_cards1)
    hand_type2, cards2 = find_max_hand_type(full_cards2)
    if hand_type1 != hand_type2:
        return hand_type1.value - hand_type2.value
    return compare_cards_for_hand_type(cards1, cards2, hand_type1)


class Holdem:
    PRE_FLOP = 0  # 手牌
    FLOP = 1  # 公牌
    TURN = 2  # 转牌
    RIVER = 3  # 河牌

    CALL = 0  # 跟注/过牌
    FOLD = -1  # 弃牌
    ALLIN = -2  # 全下

    def __init__(self, player_chips, dealer_idx=0, small_blind=50, big_blind=100):
        self.num_players = len(player_chips)
        self.player_chips = player_chips
        self.dealer_idx = dealer_idx
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.player_cards = [[] for _ in range(self.num_players)]  # 玩家手牌
        self.public_cards = []  # 公共牌
        self.pot = 0  # 奖池
        self.round = Holdem.PRE_FLOP  # 当前轮次
        self.round_idx = dealer_idx  # 当前玩家位置
        self.round_bet = 0  # 本轮最大下注（阶段总额）
        self.last_raise_to = 0  # 上次加注到的总额（协议文档：raise-to-total 语义）
        self.round_action_left = self.num_players + 2  # 本轮剩余的最低玩家表态次数
        self.round_player_bet = [0 for _ in range(self.num_players)]  # 当前玩家下注 (-1=弃牌)
        self.allin_occurred = False  # 本轮是否有人全下
        self.history = []  # 玩家操作历史记录
        self.chips_before_hand = player_chips[:]  # 记录手牌开始时筹码（边池计算用）
        self.deck = [Card(suit, number) for number in range(2, 15) for suit in Suit]
        random.shuffle(self.deck)

    def set_deck_array(self, deck_array):
        self.deck = [Card.from_int(card_int) for card_int in deck_array]

    def _next_player(self):
        self.round_idx = (self.round_idx + 1) % self.num_players
        while self.round_player_bet[self.round_idx] < 0:
            self.round_idx = (self.round_idx + 1) % self.num_players

    def _next_round(self):
        if self.round == Holdem.PRE_FLOP:
            self.round = Holdem.FLOP
            self.public_cards = [self.deck.pop() for _ in range(3)]
        elif self.round == Holdem.FLOP:
            self.round = Holdem.TURN
            self.public_cards.append(self.deck.pop())
        elif self.round == Holdem.TURN:
            self.round = Holdem.RIVER
            self.public_cards.append(self.deck.pop())
        else:
            players_left = [i for i, bet in enumerate(self.round_player_bet) if bet != Holdem.FOLD]
            players_win = []
            for idx in players_left:
                if len(players_win) == 0:
                    players_win.append(idx)
                else:
                    cards1 = self.player_cards[idx] + self.public_cards
                    cards2 = self.player_cards[players_win[0]] + self.public_cards
                    compare_result = compare_full_cards(cards1, cards2)
                    if compare_result > 0:
                        players_win = [idx]
                    elif compare_result == 0:
                        players_win.append(idx)
            return players_win

        # reset round
        self.round_idx = self.dealer_idx
        self.round_bet = 0
        self.allin_occurred = False
        self.last_raise_to = self.big_blind // 2  # 翻后首个 raise 至少到 big_blind
        self.round_action_left = sum(1 for bet in self.round_player_bet if bet >= 0)
        self.round_player_bet = [bet if bet < 0 else 0 for bet in self.round_player_bet]
        if self.round_action_left > 0:
            self._next_player()
        else:
            return self._next_round()

    def _actions_in_current_round(self):
        """当前轮次中已有多少次行为（用于判断是否是首次行为）"""
        return sum(1 for h in self.history if h["round"] == self.round)

    def _is_preflop_first_action(self, player_idx):
        """Preflop 阶段判断是否是该玩家的首次行为"""
        my_actions = sum(1 for h in self.history
                         if h["round"] == Holdem.PRE_FLOP and h["player_id"] == player_idx
                         and h["action_type"] != "fold")
        return my_actions == 0

    def player_action(self, bet):
        """
        bet: 跟注或过牌(0)/加注到指定金额(>0)/弃牌(-1)/全下(-2)
        >0 表示加注到的阶段总额（raise-to-total），与协议文档一致。
        返回获胜玩家id列表, 或空列表表示下一玩家, 或None表示下一阶段
        非法行为自动转为弃牌（与竞赛平台规则一致）
        """
        if bet == Holdem.FOLD:  # fold, 弃牌
            self.round_player_bet[self.round_idx] = Holdem.FOLD
            players_left = [i for i, bet in enumerate(self.round_player_bet) if bet != Holdem.FOLD]
            action_type = "fold"
            if len(players_left) == 1:
                self.history.append({
                    "player_id": self.round_idx,
                    "action": Holdem.FOLD,
                    "action_type": action_type
                })
                return players_left  # 唯一剩下的玩家获胜
        elif bet == Holdem.ALLIN:  # all in, 全下
            allin_total = self.player_chips[self.round_idx] + self.round_player_bet[self.round_idx]
            # 非法: 连续两个 allin
            if self.allin_occurred:
                return self.player_action(Holdem.FOLD)
            self.allin_occurred = True
            self.pot += self.player_chips[self.round_idx]
            self.round_player_bet[self.round_idx] = allin_total
            self.round_bet = max(self.round_bet, allin_total)
            self.player_chips[self.round_idx] = 0
            action_type = "allin"
        elif bet == Holdem.CALL:  # call/check, 下注或跟注
            round_actions = self._actions_in_current_round()
            inc = self.round_bet - self.round_player_bet[self.round_idx]  # 下注增量
            if inc > 0:  # call 场景（有注要跟）
                # 非法: 翻牌后第一个行为不能是 call（必须 check 或 raise）
                if self.round != Holdem.PRE_FLOP and round_actions == 0:
                    return self.player_action(Holdem.FOLD)
                # 非法: preflop BB 在 SB call 后不能再 call
                if self.round == Holdem.PRE_FLOP:
                    non_dealer = (self.dealer_idx + 1) % self.num_players
                    is_bb = self.round_idx == non_dealer  # dealer=SB, non-dealer=BB
                    if is_bb:
                        sb_actions = [h for h in self.history
                                      if h["round"] == Holdem.PRE_FLOP and h["player_id"] == self.dealer_idx]
                        if sb_actions and sb_actions[-1]["action_type"] == "call" and round_actions == 1:
                            return self.player_action(Holdem.FOLD)
                # 筹码不够 call 时，允许全下所有剩余筹码
                actual_call = min(inc, self.player_chips[self.round_idx])
                self.pot += actual_call
                self.player_chips[self.round_idx] -= actual_call
                self.round_player_bet[self.round_idx] += actual_call
                if self.player_chips[self.round_idx] == 0:
                    self.allin_occurred = True
                    action_type = "allin"
                else:
                    action_type = "call"
            else:  # inc <= 0 → check
                # 规则 4：flop/turn/river 非第一个行为 check → 非法（按 fold 处理）
                # 但 check-check（对手已 check 且下注匹配）是合法的轮结束
                # inc<=0 且 round_actions>0 = check-check，不应判非法
                self.round_player_bet[self.round_idx] = self.round_bet
                action_type = "check"
        elif bet > 0:  # raise, 加注到指定金额（raise-to-total）
            raise_to = bet  # bet 表示加注到的阶段总额
            current_bet = self.round_player_bet[self.round_idx]
            additional = raise_to - current_bet  # 反推增量

            # 非法: allin 后不能 raise
            if self.allin_occurred:
                return self.player_action(Holdem.FOLD)
            # 非法: raise 必须超过当前阶段最大注
            if raise_to <= self.round_bet:
                return self.player_action(Holdem.FOLD)
            # 非法: raise 到的金额等于全部筹码时必须用 allin
            if self.player_chips[self.round_idx] == additional:
                return self.player_action(Holdem.FOLD)
            # 非法: 筹码不足
            if self.player_chips[self.round_idx] < additional:
                return self.player_action(Holdem.FOLD)
            # 非法: 加注到的金额不满足最低加注规则
            # 区分首次加注（允许恰好 2x baseline）和再加注（严格 >2x 玩家加注）
            if self.round == Holdem.PRE_FLOP:
                baseline = self.big_blind      # 100, deal_cards_and_blind 设置
            else:
                baseline = self.big_blind // 2  # 50, _next_round 设置
            if self.last_raise_to > baseline:
                # 再加注: 必须严格 > 2x 上一次加注到的金额
                if raise_to <= self.last_raise_to * 2:
                    return self.player_action(Holdem.FOLD)
            else:
                # 首次加注或盲注: 允许恰好 2x baseline
                if raise_to < self.last_raise_to * 2:
                    return self.player_action(Holdem.FOLD)
            self.last_raise_to = raise_to
            self.round_player_bet[self.round_idx] = raise_to
            self.round_bet = max(self.round_bet, raise_to)
            self.pot += additional
            self.player_chips[self.round_idx] -= additional
            action_type = "raise"
        else:
            return self.player_action(Holdem.FOLD)

        self.round_action_left -= 1
        self.history.append({
            "round": self.round,
            "player_id": self.round_idx,
            "action": bet,
            "action_type": action_type
        })

        if self.round_action_left <= 0:
            round_bet_left = [b for b in self.round_player_bet if b != Holdem.FOLD]
            # allin 后双方都行动过，直接进入下一阶段
            if self.allin_occurred and len(round_bet_left) > 1:
                return self._next_round()
            if round_bet_left.count(self.round_bet) == len(round_bet_left):
                return self._next_round()  # 下一阶段或返回获胜玩家
        self._next_player()
        return []  # 下一玩家

    def deal_cards_and_blind(self):
        """发牌并下盲注 (标准单挑: dealer=SB, non-dealer=BB)"""
        for player_card in self.player_cards:
            player_card.append(self.deck.pop())
            player_card.append(self.deck.pop())
        self.player_action(self.small_blind)  # dealer 放 SB
        result = self.player_action(self.big_blind)  # non-dealer 放 BB
        self.history.clear()  # 盲注不算在历史里面
        self.last_raise_to = self.big_blind  # 翻前初始 raise-to 基准
        return result

    def get_player_cards(self, player_idx, with_public=True):
        if with_public:
            return self.player_cards[player_idx] + self.public_cards
        return self.player_cards[player_idx]

    def get_player_final_chips(self, players_win):
        # 边池计算：一方全下时，正确分配主池和超额投入
        contrib = [self.chips_before_hand[i] - self.player_chips[i] for i in range(self.num_players)]
        min_contrib = min(contrib)
        main_pot = 2 * min_contrib
        side_pot = self.pot - main_pot  # 超额投入
        per_winner = main_pot // len(players_win)
        results = []
        for i, chips in enumerate(self.player_chips):
            if i in players_win:
                # 赢家获得 main_pot 份额 + 如果是超额投入方也获得 side_pot
                win = per_winner
                if contrib[i] > min_contrib:
                    win += side_pot
                results.append(chips + win)
            elif side_pot > 0 and contrib[i] > min_contrib:
                # 超额投入的输家退回多余部分
                results.append(chips + side_pot)
            else:
                results.append(chips)
        return results


def get_display_data(game, matchdata, temp_result=None, error=None, last_game=None):
    data = {
        "matchdata": matchdata,
        "round": game.round,
        "round_idx": game.round_idx,
        "round_bet": game.round_bet,
        "round_raise": game.last_raise_to,
        "round_player_bet": game.round_player_bet,
        "pot": game.pot,
        "player_chips": game.player_chips,
        "public_cards": [card.to_int() for card in game.public_cards],
        "player_cards": [[card.to_int() for card in cards] for cards in game.player_cards],
    }
    if last_game:
        data["last_public_cards"] = [card.to_int() for card in last_game.public_cards]
        data["last_action"] = last_game.history[-1] if len(last_game.history) > 0 else None
    if temp_result:
        data["temp_result"] = temp_result
    if error is not None:
        data["error"] = error
    return data


def make_request_json(game, matchdata, temp_result=None, error=None, last_game=None, initdata=None):
    data = {
        "command": "request",
        "content": {
            str(game.round_idx): {
                "num_players": game.num_players,
                "dealer_id": game.dealer_idx,
                "my_id": game.round_idx,
                "my_chips": game.player_chips[game.round_idx],
                "my_cards": [card.to_int() for card in game.player_cards[game.round_idx]],
                "public_cards": [card.to_int() for card in game.public_cards],
                "history": game.history,
                **matchdata
            },
        },
        "display": get_display_data(game, matchdata, temp_result, error, last_game),
    }
    if initdata is not None:
        data["initdata"] = initdata
    return json.dumps(data)


def make_finish_json(game, matchdata, temp_result, error=None):
    total_win_chips = matchdata["total_win_chips"]
    player_scores = [int(win_chips * 2 / game.big_blind) / 2 for win_chips in total_win_chips]
    display_data = get_display_data(game, matchdata, temp_result, error)
    display_data["final_result"] = []
    for player_idx, win_chips in enumerate(total_win_chips):
        display_data["final_result"].append({
            "win_chips": win_chips,
            "win_games": matchdata["total_win_games"][player_idx]
        })
    return json.dumps({
        "command": "finish",
        "content": {str(player_idx): score
                    for player_idx, score in enumerate(player_scores)},
        "display": display_data,
    })


def judge(input_json):
    N_PLAYERS = 2  # 玩家数
    DEFAULT_N_HANDS = 70  # 默认多少手牌一次比赛
    INITIAL_CHIPS = 20000  # 每手牌初始筹码数

    inputs = json.loads(input_json)
    if len(inputs["log"]) == 0:
        random.seed()
        dealer_idx = random.randint(0, N_PLAYERS - 1)
        initdata = inputs.get("initdata", "")
        initdata = initdata if initdata else {}
        initdata.setdefault("max_hand", DEFAULT_N_HANDS)
        if "decks" not in initdata or not initdata["decks"]:
            initdata["decks"] = []
            for _ in range(initdata["max_hand"]):
                initdata["decks"].append([i for i in range(52)])
                random.shuffle(initdata["decks"][-1])
        if "dealer" not in initdata:
            initdata["dealer"] = dealer_idx
        matchdata = {
            "hand": 0,
            "max_hand": initdata["max_hand"],
            "total_win_chips": [0] * N_PLAYERS,
            "total_win_games": [0] * N_PLAYERS,
        }
        game = Holdem([INITIAL_CHIPS] * N_PLAYERS, initdata["dealer"])
        game.set_deck_array(initdata["decks"][0])
        game.deal_cards_and_blind()
        return make_request_json(game, matchdata, initdata=initdata)
    else:
        initdata = inputs["initdata"]
        # 从上次裁判的log中找到当前比赛信息
        matchdata = inputs["log"][-2]["output"]["display"]["matchdata"]
        # 找到当前局的起始裁判输出序号
        hand_start_judge_log_idx = len(inputs["log"])
        for judge_log in inputs["log"][-2::-2]:
            md = judge_log["output"]["display"]["matchdata"]
            if md["hand"] < matchdata["hand"]: break
            hand_start_judge_log_idx -= 2
        # 初始化当前局
        dealer_idx = (initdata["dealer"] + matchdata["hand"]) % N_PLAYERS
        game = Holdem([INITIAL_CHIPS] * N_PLAYERS, dealer_idx)
        game.set_deck_array(initdata["decks"][matchdata["hand"]])
        game.deal_cards_and_blind()

    # 根据本局游戏bot的回复，恢复游戏
    result = None
    for bot_log in inputs["log"][hand_start_judge_log_idx + 1::2]:
        bot_log = bot_log[str(game.round_idx)]
        if bot_log["verdict"] != "OK":  # 崩溃视为弃牌
            err = f"INVALID_INPUT_VERDICT_{bot_log['verdict']}"
            result = game.player_action(Holdem.FOLD)
        else:
            try:
                bot_input, err = bot_log["response"], None
                result = game.player_action(int(bot_input))
            except ValueError as e:  # 崩溃或错误回复视为弃牌
                result, err = game.player_action(Holdem.FOLD), str(e)
        if result: break

    last_game = game
    if result:  # 一手牌结束
        player_chips = game.get_player_final_chips(result)
        mean_chips = sum(player_chips) / len(player_chips)
        result = []
        for player_idx, chips in enumerate(player_chips):
            player_cards = game.get_player_cards(player_idx)
            max_hand_type, max_cards = find_max_hand_type(player_cards)
            result.append({
                "win_chips": chips - mean_chips,
                "max_hand_type": max_hand_type.value,
                "max_cards": [card.to_int() for card in max_cards],
            })
            matchdata["total_win_chips"][player_idx] += chips - mean_chips
            if chips > mean_chips:
                matchdata["total_win_games"][player_idx] += 1
        if matchdata["hand"] < matchdata["max_hand"] - 1:  # 初始化新局
            matchdata["hand"] += 1
            dealer_idx = (initdata["dealer"] + matchdata["hand"]) % N_PLAYERS
            game = Holdem([INITIAL_CHIPS] * N_PLAYERS, dealer_idx)
            game.set_deck_array(initdata["decks"][matchdata["hand"]])
            game.deal_cards_and_blind()
        else:
            return make_finish_json(game, matchdata, result, err)

    return make_request_json(game, matchdata, result, err, last_game)


if __name__ == "__main__":
    print(judge(input()))