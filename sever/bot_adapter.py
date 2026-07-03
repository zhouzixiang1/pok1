"""Bot 桥接器：将 engine/judge.py 风格的本地 bot 连接到 TCP 竞赛服务器。

用法:
  python bot_adapter.py --bot ../bots/claude_v5 --name BotA
  python bot_adapter.py --bot ../bots/claude_v5 --name BotA --host 127.0.0.1 --port 10001

卡牌转换:
  TCP 协议: <suit,rank>, suit 0-3=♠♥♦♣, rank 0-12=2-A
  judge.py: 整数 0-51, number = card // 4 + 2, suit = card % 4 (♥=0,♦=1,♠=2,♣=3)
  转换公式: judge_int = rank * 4 + _TCP_TO_JUDGE_SUIT[tcp_suit]

行为转换:
  judge.py 输出:  0=call, -1=fold, -2=allin, >0=raise-to-total
  TCP 协议:       call, fold, allin, raise <amount>
"""
import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from server.protocol import parse_preflop, parse_stage_cards, parse_action
from engine.deck import Card
from engine.validator import SMALL_BLIND, BIG_BLIND

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("bot_adapter")


# ── 卡牌转换 ──

# TCP suit → judge.py suit 映射
# TCP: 0=♠, 1=♥, 2=♦, 3=♣
# judge.py: 0=♥, 1=♦, 2=♠, 3=♣
_TCP_TO_JUDGE_SUIT = {0: 2, 1: 0, 2: 1, 3: 3}
_JUDGE_TO_TCP_SUIT = {v: k for k, v in _TCP_TO_JUDGE_SUIT.items()}


def tcp_card_to_int(card):
    """TCP Card 对象 → judge.py 整数。suit 经映射转换。"""
    judge_suit = _TCP_TO_JUDGE_SUIT[card.suit]
    return card.rank * 4 + judge_suit


def int_to_tcp_card_str(card_int):
    """judge.py 整数 → TCP 协议字符串 '<suit,rank>'。suit 经反向映射。"""
    rank = card_int // 4
    judge_suit = card_int % 4
    tcp_suit = _JUDGE_TO_TCP_SUIT[judge_suit]
    return f"<{tcp_suit},{rank}>"


# ── Bot 进程管理 ──

class BotProcess:
    """管理 bot 子进程的 stdin/stdout JSON 协议。

    bot 使用 line-delimited JSON：每次发送一行 JSON，读取一行 JSON。
    bot 的 main() 通常是 `for line in sys.stdin` 循环。
    """

    def __init__(self, bot_path):
        self.bot_path = bot_path
        if os.path.isdir(bot_path):
            self.script = os.path.join(bot_path, "main.py")
        else:
            self.script = bot_path
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, self.script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        logger.info(f"Bot started: {self.script} (PID {self.proc.pid})")

    def send_and_recv(self, payload, timeout=60):
        """发送 payload dict，接收 response dict。带超时保护。"""
        msg = json.dumps(payload, ensure_ascii=False)
        try:
            self.proc.stdin.write(msg + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ConnectionResetError):
            logger.error("Bot stdin write failed")
            return None

        import threading
        result = [None]
        def _read():
            try:
                result[0] = self.proc.stdout.readline()
            except Exception:
                pass

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            logger.error(f"Bot timeout after {timeout}s in send_and_recv")
            return None
        if not result[0]:
            logger.error("Bot returned empty output")
            return None
        try:
            return json.loads(result[0].strip())
        except json.JSONDecodeError as e:
            logger.error(f"Bot JSON decode error: {e}")
            return None

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ── 适配逻辑 ──

class BotAdapter:
    """将 TCP 协议消息转换为 bot 的 JSON 格式。"""

    TOTAL_HANDS = 70
    INITIAL_CHIPS = 20000

    def __init__(self, host, port, bot_path, name):
        self.host = host
        self.port = port
        self.name = name
        self.bot = BotProcess(bot_path)
        self.reader = None
        self.writer = None
        self._buf = ""

        # 游戏状态追踪（每次新手牌重置）
        self._my_cards = []       # judge.py 整数
        self._public_cards = []   # judge.py 整数
        self._is_sb = False
        self._hand_num = 0
        self._history = []        # judge.py 格式的历史
        self._stage = "preflop"
        self._my_id = 0           # 对 JSON bot 的稳定视角：自己始终是 0
        self._opponent_id = 1
        self._dealer_id = 0
        self._my_action_count = 0  # 本阶段已行动次数
        self._my_chips = self.INITIAL_CHIPS  # 当前剩余筹码
        self._my_stage_bet = 0    # 本阶段已下注额
        self._opponent_chips = self.INITIAL_CHIPS
        self._opponent_stage_bet = 0
        self._pot = 0
        self._in_allin_runout = False  # allin+call 后只接收公共牌/结算，不再行动

        # 持久化状态
        self._bot_data = None     # bot 返回的 data（跨决策持久化）
        self._bot_requests = []   # 与 engine/battle.py 一致：70 手牌内累积
        self._bot_responses = []
        self._total_win_chips = [0, 0]
        self._total_win_games = [0, 0]
        self._last_earned = 0
        self._showdown_observations = []
        self.telemetry = {
            "bot_failures": 0,
            "invalid_actions": 0,
            "actions_sent": 0,
            "clamped_raises": 0,
            "allin_conversions": 0,
            "would_be_illegal_raise": 0,
            "postflop_pass_conversions": 0,
        }

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        logger.info(f"Connected to {self.host}:{self.port}")

    async def run(self):
        self.bot.start()
        try:
            while True:
                msg = await self._recv_line()
                if msg is None:
                    logger.info("Server disconnected")
                    break
                await self._handle(msg)
        finally:
            self.bot.close()
            if self.writer:
                try:
                    self.writer.close()
                except Exception:
                    pass

    async def _recv_line(self):
        while "\n" not in self._buf:
            data = await self.reader.read(4096)
            if not data:
                return None
            self._buf += data.decode("utf-8")
        line, self._buf = self._buf.split("\n", 1)
        return line.strip()

    async def _send_line(self, msg):
        logger.info(f">> {msg}")
        self.writer.write((msg + "\n").encode("utf-8"))
        await self.writer.drain()

    async def _handle(self, msg):
        logger.info(f"<< {msg}")

        # ── Name query ──
        if msg == "name":
            await self._send_line(self.name)
            return

        # ── Preflop：新手牌开始 ──
        if msg.startswith("preflop|"):
            blind_type, cards = parse_preflop(msg)
            self._is_sb = (blind_type == "SMALLBLIND")
            self._my_cards = [tcp_card_to_int(c) for c in cards]
            self._public_cards = []
            self._stage = "preflop"
            self._hand_num += 1
            self._history = []
            self._my_action_count = 0
            self._my_chips = self.INITIAL_CHIPS  # 一局一复位
            self._opponent_chips = self.INITIAL_CHIPS
            self._pot = SMALL_BLIND + BIG_BLIND
            self._in_allin_runout = False
            if self._is_sb:
                self._my_chips -= SMALL_BLIND
                self._my_stage_bet = SMALL_BLIND
                self._opponent_chips -= BIG_BLIND
                self._opponent_stage_bet = BIG_BLIND
            else:
                self._my_chips -= BIG_BLIND
                self._my_stage_bet = BIG_BLIND
                self._opponent_chips -= SMALL_BLIND
                self._opponent_stage_bet = SMALL_BLIND

            # 对每个 bot 使用稳定视角：自己始终是 player 0，对手始终是 1。
            # dealer_id 仍表示 SB，因此它会随盲位在 0/1 间切换。
            self._my_id = 0
            self._opponent_id = 1
            self._dealer_id = 0 if self._is_sb else 1

            # preflop: SB 先行动
            if self._is_sb:
                await self._bot_decide()
            return

        # ── Flop/Turn/River ──
        if msg.startswith("flop|") or msg.startswith("turn|") or msg.startswith("river|"):
            cards = parse_stage_cards(msg)
            for c in cards:
                self._public_cards.append(tcp_card_to_int(c))
            self._stage = msg.split("|")[0]
            self._my_action_count = 0  # 新阶段重置
            self._my_stage_bet = 0     # 新阶段下注归零
            self._opponent_stage_bet = 0
            logger.info(f"Stage: {self._stage}, public count: {len(self._public_cards)}")

            if self._in_allin_runout:
                return

            # postflop: BB 先行动
            if not self._is_sb:
                await self._bot_decide()
            return

        # ── earnChips ──
        if msg.startswith("earnChips"):
            earned = int(msg.split()[1])
            self._last_earned = earned
            self._total_win_chips[self._my_id] += earned
            self._total_win_chips[self._opponent_id] -= earned
            if earned > 0:
                self._total_win_games[self._my_id] += 1
            elif earned < 0:
                self._total_win_games[self._opponent_id] += 1
            self._in_allin_runout = False
            logger.info(f"Hand {self._hand_num} earned: {earned}, "
                        f"total: {self._total_win_chips[self._my_id]}")
            return

        # ── oppo_hands（showdown 对手手牌）──
        if msg.startswith("oppo_hands|"):
            cards = parse_stage_cards(msg)
            self._showdown_observations.append({
                "hand": self._hand_num - 1,
                "opponent_cards": [tcp_card_to_int(c) for c in cards],
                "my_cards": list(self._my_cards),
                "public_cards": list(self._public_cards),
                "history": list(self._history),
                "earned": self._last_earned,
            })
            logger.info(f"Opponent showdown: {msg}")
            return

        # ── 对手行为 → 判断是否需要响应 ──
        action_type, amount = parse_action(msg)

        committed = self._apply_opponent_action(action_type, amount)
        self._record_opponent_action(action_type, amount, committed=committed)
        if action_type == "call" and (
            self._current_round_has_allin()
            or self._my_chips == 0
            or self._opponent_chips == 0
        ):
            self._in_allin_runout = True

        # 判断是否需要响应
        need_respond = self._should_respond(action_type)
        if need_respond:
            await self._bot_decide()
        # 否则等待下一阶段消息或 earnChips

    def _should_respond(self, action_type):
        """判断收到对手行为后是否需要响应。

        TCP 协议规则：如果服务器发送了对手行为消息给我们，
        说明服务器在等我们的响应（除非该行为结束了阶段/牌局，
        此时服务器会紧接着发送阶段牌或 earnChips）。

        但我们收到消息时不知道后续是什么，所以需要根据规则判断。

        规则：
        - fold → 不需要（牌局结束）
        - raise / allin → 一定需要
        - preflop call from SB when we're BB and haven't acted → 需要
        - preflop call 其他情况 → 不需要（阶段结束）
        - postflop call → 不需要（阶段结束）
        - preflop check → 不需要（BB check 结束 preflop）
        - postflop check when we haven't acted → 需要（对手先 check，本方用 call 结束阶段）
        - postflop check when we have acted → 不需要（对手 call 后 check？不可能）
        """
        if action_type == "fold":
            return False

        if action_type in ("raise", "allin"):
            return True

        if action_type == "call":
            # preflop: SB call 后 BB 需行动
            if self._stage == "preflop" and not self._is_sb and self._my_action_count == 0:
                return True
            # 其他 call 都结束阶段
            return False

        if action_type == "check":
            # preflop: BB check 结束 preflop
            if self._stage == "preflop":
                return False
            # postflop: 对手先 check，我们需要响应
            if self._my_action_count == 0:
                return True
            return False

        return False

    def _current_round_has_allin(self):
        """当前阶段是否已有任意一方 allin。"""
        stage_map = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}
        round_num = stage_map.get(self._stage, 0)
        return any(
            h["round"] == round_num and h["action_type"] == "allin"
            for h in self._history
        )

    def _apply_opponent_action(self, action_type, amount):
        """Update reconstructed opponent stack, street bet, and pot.

        The TCP protocol forwards ``allin`` without an amount. Because each hand
        resets to 20000 chips and every prior action is visible, the adapter can
        reconstruct the exact all-in total from opponent_chips/stage_bet instead
        of guessing from history.
        """
        committed = 0
        if action_type == "call":
            diff = max(0, self._my_stage_bet - self._opponent_stage_bet)
            committed = min(diff, self._opponent_chips)
        elif action_type == "raise" and amount is not None:
            diff = max(0, amount - self._opponent_stage_bet)
            committed = min(diff, self._opponent_chips)
        elif action_type == "allin":
            committed = self._opponent_chips

        if committed > 0:
            self._opponent_chips -= committed
            self._opponent_stage_bet += committed
            self._pot += committed
        return committed

    def _betting_snapshot(self):
        """Return adapter-only betting fields for bots that can use them."""
        to_call = max(0, self._opponent_stage_bet - self._my_stage_bet)
        opponent_allin = self._opponent_chips == 0 and self._opponent_stage_bet > 0
        return {
            "opponent_chips": self._opponent_chips,
            "my_stage_bet": self._my_stage_bet,
            "opponent_stage_bet": self._opponent_stage_bet,
            "pot": self._pot,
            "to_call": to_call,
            "opponent_allin": opponent_allin,
        }

    def _record_opponent_action(self, action_type, amount, committed=0):
        """将对手行为记录到 judge.py 格式的 history。"""
        stage_map = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}
        round_num = stage_map.get(self._stage, 0)

        # opponent 的 player_id
        opp_id = self._opponent_id

        if action_type == "call":
            action_val = 0
            action_name = "call"
        elif action_type == "check":
            action_val = 0
            action_name = "check"
        elif action_type == "fold":
            action_val = -1
            action_name = "fold"
        elif action_type == "allin":
            action_val = -2
            action_name = "allin"
        elif action_type == "raise":
            action_val = amount  # raise-to-total
            action_name = "raise"
        else:
            return

        entry = {
            "round": round_num,
            "player_id": opp_id,
            "action": action_val,
            "action_type": action_name,
        }
        if action_name in ("call", "raise", "allin"):
            entry["stage_bet"] = self._opponent_stage_bet
            entry["committed"] = committed
            entry["chips_after"] = self._opponent_chips
        self._history.append(entry)

    def _record_my_action(self, action_type, amount):
        """将自己的行为记录到 history。"""
        stage_map = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}
        round_num = stage_map.get(self._stage, 0)

        if action_type == "call":
            action_val = 0
            action_name = "call"
        elif action_type == "check":
            action_val = 0
            action_name = "check"
        elif action_type == "fold":
            action_val = -1
            action_name = "fold"
        elif action_type == "allin":
            action_val = -2
            action_name = "allin"
        elif action_type == "raise":
            action_val = amount
            action_name = "raise"
        else:
            return

        entry = {
            "round": round_num,
            "player_id": self._my_id,
            "action": action_val,
            "action_type": action_name,
        }
        if action_name in ("call", "raise", "allin"):
            entry["stage_bet"] = self._my_stage_bet
            entry["chips_after"] = self._my_chips
        self._history.append(entry)

    async def _bot_decide(self):
        """构建完整的 judge.py 格式请求，发送给 bot，转换回复为 TCP 协议。"""
        # 构建当前请求（judge.py 的 content[player_id] 格式）
        request = {
            "num_players": 2,
            "dealer_id": self._dealer_id,
            "my_id": self._my_id,
            "my_chips": self._my_chips,
            "my_cards": self._my_cards,
            "public_cards": self._public_cards,
            "history": list(self._history),
            "hand": self._hand_num - 1,
            "max_hand": self.TOTAL_HANDS,
            "total_win_chips": list(self._total_win_chips),
            "total_win_games": list(self._total_win_games),
            "opponent_showdowns": list(self._showdown_observations),
            **self._betting_snapshot(),
        }

        # 构建完整 payload。与 engine/battle.py 对齐：先追加当前 request，
        # 再把整场累计 requests/responses/data 发给 bot。
        self._bot_requests.append(request)
        payload = {
            "requests": list(self._bot_requests),
            "responses": list(self._bot_responses),
        }
        if self._bot_data is not None:
            payload["data"] = self._bot_data

        result = self.bot.send_and_recv(payload)

        if result is None:
            self.telemetry["bot_failures"] += 1
            logger.warning("Bot returned None, folding")
            self._bot_responses.append(-1)
            await self._send_line("fold")
            self.telemetry["actions_sent"] += 1
            self._record_my_action("fold", None)
            return

        response = result.get("response", 0)
        try:
            response_for_history = int(response)
        except (TypeError, ValueError):
            response_for_history = -1
        self._bot_responses.append(response_for_history)
        data = result.get("data")
        if data is not None:
            self._bot_data = data

        # 转换行为
        action_str, tcp_type, tcp_amount = self._convert_action(response)
        await self._send_line(action_str)
        self.telemetry["actions_sent"] += 1
        self._update_chips(tcp_type, tcp_amount)
        self._record_my_action(tcp_type, tcp_amount)
        if tcp_type == "call" and (
            self._current_round_has_allin()
            or self._my_chips == 0
            or self._opponent_chips == 0
        ):
            self._in_allin_runout = True
        self._my_action_count += 1

    def _update_chips(self, action_type, amount):
        """根据发出的动作更新筹码追踪。

        call: 补齐到对手下注额（或剩余全部）
        raise to X: 扣减 X - my_stage_bet
        allin: 筹码归零
        fold/check: 不扣减
        """
        if action_type == "call":
            diff = max(0, self._opponent_stage_bet - self._my_stage_bet)
            actual = min(diff, self._my_chips)
            self._my_chips -= actual
            self._my_stage_bet += actual
            self._pot += actual
        elif action_type == "raise":
            needed = amount - self._my_stage_bet
            self._my_chips -= needed
            self._my_stage_bet = amount
            self._pot += needed
        elif action_type == "allin":
            committed = self._my_chips
            self._my_stage_bet += committed
            self._pot += committed
            self._my_chips = 0

    def _clamp_raise(self, raise_to):
        """Client-side raise validation: clamp to legal minimum.

        Rules:
        - Preflop first raise: >= 200
        - Postflop first raise: >= 100
        - Re-raise: > 2 * last_raise_to (strictly greater)
        - If raise needs all chips, should use allin instead (server rule 11)
        """
        MIN_RAISE_PREFLOP = 200
        MIN_RAISE_POSTFLOP = 100

        stage_map = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}
        round_num = stage_map.get(self._stage, 0)

        # Find last raise in current stage
        last_raise_to = None
        for h in reversed(self._history):
            if h["round"] != round_num:
                break
            if h["action_type"] == "raise":
                last_raise_to = h["action"]
                break

        # Determine minimum legal raise-to
        if last_raise_to is not None:
            # Re-raise: must be > 2 * last_raise_to
            min_raise = last_raise_to * 2 + 1
        elif self._stage == "preflop":
            min_raise = MIN_RAISE_PREFLOP
        else:
            min_raise = MIN_RAISE_POSTFLOP

        # Clamp. The adapter keeps the bot alive for compatibility, but records
        # every correction so national acceptance can reject strategy that only
        # works because the bridge rewrote an illegal raise.
        if raise_to < min_raise:
            self.telemetry["clamped_raises"] += 1
            self.telemetry["would_be_illegal_raise"] += 1
            raise_to = min_raise

        return raise_to

    def _convert_action(self, action):
        """judge.py 整数 → (TCP 字符串, action_type, amount)。

        judge.py 中 0 = call 或 check（不区分）。
        TCP 协议区分 call 和 check：
          - 有对手下注需要跟 → call
          - 无需跟注 → check
        判断依据：history 中对手在本阶段是否有 raise/allin。
        """
        # 类型保护：非整数输入按 fold 处理
        try:
            action_int = int(action)
        except (TypeError, ValueError):
            self.telemetry["invalid_actions"] += 1
            logger.warning(f"Invalid action type: {action!r}, folding")
            return "fold", "fold", None
        if action_int == -1:
            return "fold", "fold", None
        if action_int == -2:
            return "allin", "allin", None
        if action_int > 0:
            # Client-side raise validation: clamp to legal minimum
            action_int = self._clamp_raise(action_int)
            needed = action_int - self._my_stage_bet
            if needed >= self._my_chips:
                self.telemetry["allin_conversions"] += 1
                return "allin", "allin", None
            return f"raise {action_int}", "raise", action_int
        if action_int == 0:
            # 判断是 call 还是 check
            stage_map = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}
            round_num = stage_map.get(self._stage, 0)
            # 检查对手在本阶段是否有 raise 或 allin
            opp_id = self._opponent_id
            opp_raised = any(
                h["round"] == round_num and h["player_id"] == opp_id
                and h["action_type"] in ("raise", "allin")
                for h in self._history
            )
            # preflop: 如果是对手第一个 call（SB call），BB 也是 check
            # 简单规则：如果对手在本阶段有 raise/allin → call，否则 → check
            if opp_raised:
                return "call", "call", None
            # preflop SB call 匹配 BB 盲注 → call
            if self._stage == "preflop" and self._is_sb:
                return "call", "call", None
            # preflop BB 面对 SB call → check
            if self._stage == "preflop" and not self._is_sb:
                return "check", "check", None
            if self._stage != "preflop":
                opp_acted = any(
                    h["round"] == round_num and h["player_id"] == opp_id
                    for h in self._history
                )
                if opp_acted:
                    self.telemetry["postflop_pass_conversions"] += 1
                    return "call", "call", None
            # postflop 首个行动且没有对手行为 → check
            return "check", "check", None
        self.telemetry["invalid_actions"] += 1
        return "fold", "fold", None


async def main_async():
    parser = argparse.ArgumentParser(description="Bot 桥接器")
    parser.add_argument("--bot", required=True, help="Bot 目录或主文件路径")
    parser.add_argument("--name", default="Bot", help="Bot 名称")
    parser.add_argument("--host", default="127.0.0.1", help="服务器地址")
    parser.add_argument("--port", type=int, default=10001, help="服务器端口")
    args = parser.parse_args()

    adapter = BotAdapter(args.host, args.port, args.bot, args.name)
    await adapter.connect()
    await adapter.run()


if __name__ == "__main__":
    asyncio.run(main_async())
