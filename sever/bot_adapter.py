"""Bot 桥接器：将 engine/judge.py 风格的本地 bot 连接到 TCP 竞赛服务器。

用法:
  python bot_adapter.py --bot ../bots/claude_v5 --name test
  python bot_adapter.py --bot ../bots/claude_v5 --name test --host 127.0.0.1 --port 10001

卡牌转换:
  TCP 协议: <suit,rank>, suit 0-3=♠♥♦♣, rank 0-12=2-A
  judge.py: 整数 0-51, number = card // 4 + 2, suit = card % 4 (♥=0,♦=1,♠=2,♣=3)
  转换公式: judge_int = rank * 4 + suit  (直接映射，注意两套 suit 编码不同)

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("bot_adapter")


# ── 卡牌转换 ──

def tcp_card_to_int(suit, rank):
    """TCP <suit,rank> → judge.py 整数。
    注意：两套系统的 suit 编码不同，但 card_int = rank * 4 + suit 在两套中
    数值不同但 eval 一致（因为同局内所有卡牌统一转换）。
    """
    return rank * 4 + suit


# ── Bot 进程管理 ──

class BotProcess:
    """管理 bot 子进程的 stdin/stdout JSON 协议。"""

    def __init__(self, bot_path):
        self.bot_path = bot_path
        # 判断是单文件还是目录
        if os.path.isdir(bot_path):
            self.script = os.path.join(bot_path, "main.py")
        else:
            self.script = bot_path
        self.proc = None
        self._requests = []
        self._responses = []
        self._data = None

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, self.script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        logger.info(f"Bot started: {self.script} (PID {self.proc.pid})")

    def send_and_recv(self, request_dict):
        """发送请求并接收 bot 的回复。"""
        msg = json.dumps(request_dict, ensure_ascii=False)
        try:
            self.proc.stdin.write(msg + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            if not line:
                logger.error("Bot process returned empty output")
                return None
            return json.loads(line.strip())
        except (BrokenPipeError, json.JSONDecodeError) as e:
            logger.error(f"Bot communication error: {e}")
            return None

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ── 适配逻辑 ──

class BotAdapter:
    """将 TCP 协议消息转换为 bot 的 JSON 格式。"""

    def __init__(self, host, port, bot_path, name):
        self.host = host
        self.port = port
        self.name = name
        self.bot = BotProcess(bot_path)
        self.reader = None
        self.writer = None
        self._buf = ""

        # 游戏状态追踪
        self._my_cards = []    # judge.py 整数
        self._public_cards = []  # judge.py 整数
        self._stage = "preflop"
        self._is_sb = False
        self._hand_num = 0
        self._history = []

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
                self.writer.close()

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

        # Name query
        if msg == "name":
            await self._send_line(self.name)
            return

        # Preflop
        if msg.startswith("preflop|"):
            blind_type, cards = parse_preflop(msg)
            self._is_sb = (blind_type == "SMALLBLIND")
            self._my_cards = [tcp_card_to_int(c.suit, c.rank) for c in cards]
            self._public_cards = []
            self._stage = "preflop"
            self._hand_num += 1
            self._history = []

            # preflop: SB 先行动
            if self._is_sb:
                await self._bot_decide()
            return

        # Flop/Turn/River
        if msg.startswith("flop|") or msg.startswith("turn|") or msg.startswith("river|"):
            cards = parse_stage_cards(msg)
            for c in cards:
                self._public_cards.append(tcp_card_to_int(c.suit, c.rank))
            stage_name = msg.split("|")[0]
            self._stage = stage_name
            logger.info(f"Stage: {stage_name}, public: {self._public_cards}")

            # postflop: BB 先行动
            if not self._is_sb:
                await self._bot_decide()
            return

        # earnChips
        if msg.startswith("earnChips"):
            logger.info(f"Earned: {msg.split()[1]}")
            return

        # oppo_hands
        if msg.startswith("oppo_hands|"):
            logger.info(f"Opponent showdown: {msg}")
            return

        # 对手行为 → 需要响应
        action_type, amount = parse_action(msg)
        logger.info(f"Opponent: {action_type}" + (f" {amount}" if amount else ""))

        if action_type in ("fold",):
            return  # 手牌结束
        if action_type == "call" and self._stage == "preflop" and self._is_sb:
            # preflop SB call 后 BB 还需行动，但 SB 不需要
            return
        if action_type == "call":
            # call 结束阶段
            return

        # 需要响应对手行为
        await self._bot_decide()

    async def _bot_decide(self):
        """构建 bot 输入 JSON 并获取决策。"""
        request = {
            "requests": [],
            "responses": [],
        }
        if self.bot.bot._data if hasattr(self.bot, '_data') else None:
            request["data"] = self.bot._data

        # 简化：直接调用 bot 的 send_and_recv
        # 实际需要构建完整的状态 JSON
        # 这里用最简单的方式 — 让 bot 自行维护状态
        result = self.bot.send_and_recv(request)

        if result is None:
            await self._send_line("fold")
            return

        response = result.get("response", 0)
        data = result.get("data")
        if data:
            self.bot._data = data

        # 转换行为
        action_str = self._convert_action(response)
        await self._send_line(action_str)

    def _convert_action(self, action_int):
        """judge.py 整数 → TCP 行为字符串。"""
        if action_int == 0:
            return "call"
        if action_int == -1:
            return "fold"
        if action_int == -2:
            return "allin"
        if action_int > 0:
            return f"raise {action_int}"  # raise-to-total
        return "fold"


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
