"""行为合法性验证模块。

严格按照《非法行为说明.docx》实现全部 13 条规则。
所有非法行为统一按弃牌 (fold) 处理。

game_state 字典结构：
    stage:             'preflop' | 'flop' | 'turn' | 'river'
    actions:           [(action_type, amount_or_None), ...] 当前阶段行动历史
    player_chips:      int   当前玩家剩余筹码
    player_bet:        int   当前玩家本阶段已下注额
    opponent_bet:      int   对手本阶段已下注额
    is_small_blind:    bool  当前玩家是否小盲注
    is_big_blind:      bool  当前玩家是否大盲注
    allin_occurred:    bool  当前阶段是否已有人 allin
    player_action_count: int  当前玩家在本阶段已行动次数
"""
from __future__ import annotations

SMALL_BLIND = 50
BIG_BLIND = 100
MIN_RAISE_PREFLOP = 200
MIN_RAISE_POSTFLOP = 100
RAISE_MULTIPLIER = 2


def validate_action(action_type: str, action_amount: int | None,
                    game_state: dict) -> tuple[bool, str]:
    """验证行为是否合法。

    返回 (is_legal, reason)。reason 为空字符串表示合法。
    """
    # ── 规则 1：任何时刻不允许 bet ─────────────────────────
    if action_type == "bet":
        return False, "bet is never allowed; use raise instead"
    if action_type == "unknown":
        return False, "unrecognized action"

    # fold 永远合法
    if action_type == "fold":
        return True, ""

    stage = game_state["stage"]
    is_sb = game_state["is_small_blind"]
    is_bb = game_state["is_big_blind"]
    actions = game_state["actions"]
    chips = game_state["player_chips"]
    player_bet = game_state["player_bet"]
    action_count = game_state["player_action_count"]
    is_first_in_stage = len(actions) == 0

    # ── call 相关规则 ──────────────────────────────────────
    if action_type == "call":
        # 规则 2：flop/turn/river 第一个行为 call → 非法
        if stage in ("flop", "turn", "river") and is_first_in_stage:
            return False, "call is illegal as first action in flop/turn/river"
        # 规则 3：preflop BB 在 SB call 后 call → 非法
        if stage == "preflop" and is_bb and action_count == 0:
            if actions and actions[-1][0] == "call":
                return False, "BB call is illegal after SB call in preflop"
        return True, ""

    # ── check 相关规则 ─────────────────────────────────────
    if action_type == "check":
        if stage == "preflop":
            # 规则 5：preflop 只有大盲注第一个行为可以 check
            if not (is_bb and action_count == 0):
                return False, "check in preflop only allowed as BB first action"
            return True, ""
        # flop/turn/river
        # 规则 4：非第一个行为出现 check → 非法
        if not is_first_in_stage:
            return False, "check is illegal for non-first action in flop/turn/river"
        return True, ""

    # ── allin 相关规则 ─────────────────────────────────────
    if action_type == "allin":
        # 规则 13：连续两个 allin → 第二个非法
        if game_state["allin_occurred"]:
            return False, "consecutive allin is illegal"
        return True, ""

    # ── raise 相关规则 ─────────────────────────────────────
    if action_type == "raise":
        amount = action_amount  # raise-to-total（加注到的阶段总额）
        needed = amount - player_bet  # 需要额外投入的筹码

        # 规则 11：raise 金额等于全部筹码 → 必须 allin
        if needed == chips:
            return False, "must use allin when raise equals remaining chips"
        # 规则 10：raise 超过持有筹码 → 非法
        if needed > chips:
            return False, "raise amount exceeds player chips"
        # 规则 12：allin 后不能 raise
        if game_state["allin_occurred"]:
            return False, "raise is illegal after allin; only call or fold"

        if stage == "preflop":
            # 规则 6：preflop SB 第一个 raise 必须 ≥ 200
            if is_sb and action_count == 0:
                if amount < MIN_RAISE_PREFLOP:
                    return False, f"preflop SB first raise must be >= {MIN_RAISE_PREFLOP}"
            # 规则 7：preflop BB 第一个 raise
            elif is_bb and action_count == 0:
                if actions:
                    last_action = actions[-1]
                    if last_action[0] == "call":
                        # SB call 后 BB raise 必须 ≥ 200
                        if amount < MIN_RAISE_PREFLOP:
                            return False, f"preflop BB raise must be >= {MIN_RAISE_PREFLOP} after SB call"
                    elif last_action[0] == "raise":
                        # SB raise 后 BB raise 必须 ≥ 2× SB raise-to
                        if amount < last_action[1] * RAISE_MULTIPLIER:
                            return False, f"preflop BB raise must be >= {RAISE_MULTIPLIER}x SB raise ({last_action[1]})"
        else:
            # 规则 9：flop/turn/river 第一个 raise 必须 ≥ 100
            if is_first_in_stage:
                if amount < MIN_RAISE_POSTFLOP:
                    return False, f"first raise in {stage} must be >= {MIN_RAISE_POSTFLOP}"

        # 规则 8：连续 raise 必须 ≥ 2× 上一次 raise-to
        last_raise = _last_raise_amount(actions)
        if last_raise is not None:
            if amount < last_raise * RAISE_MULTIPLIER:
                return False, f"consecutive raise must be >= {RAISE_MULTIPLIER}x previous ({last_raise})"

        return True, ""

    return False, "unrecognized action type"


def _last_raise_amount(actions: list) -> int | None:
    """返回当前阶段上一次 raise 的筹码量（raise-to-total）。"""
    for act in reversed(actions):
        if act[0] == "raise":
            return act[1]
    return None
