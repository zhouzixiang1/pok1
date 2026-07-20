"""TCP 桥(里程碑 4 第三组件,容器内运行)。

**角色**:容器内进程,连接「平台(stdin/stdout JSON)」与「用户 TCP bot(socket 国赛文本)」。
用户 bot 代码零改动,连 ``127.0.0.1:50101``(容器内回环)。

**流程**(见 CONTRACT.md 第三节):

1. 桥启动 → 监听 ``127.0.0.1:50101``。
2. spawn 用户 bot(``python <entry> --host 127.0.0.1 --port 50101 --name <name>``)。
3. 等 bot 连入,收 name 握手(裸队名)。
4. 主循环:
   - 读平台 stdin 一行(JSON request)→ 翻译成国赛文本序列
     (preflop|/flop|/.../转发对手动作/earnChips/oppo_hands)→ 发 bot socket。
   - 读 bot socket 一个动作(token 前缀分帧,复用 transport.pop_client_action)
     → 包成 ``{"response": int}`` → 写平台 stdout。

**卡牌翻译**:
  平台 JSON 整数(0-51)→ 国赛 ``<suit,rank>`` 文本。
  JSON 整数 = ``rank*4 + JUDGE_SUIT``,``JUDGE_SUIT`` 取 ``%4``;
  反向映射 ``JUDGE_TO_TCP_SUIT`` 得到平台 suit,再格式化为 ``<suit,rank>``。

**可独立运行测试**:
  ``python tcp_bridge.py --bot-entry national_bot.py --bot-name Bot``
  加 argparse(--bot-entry/--bot-name/--listen-host/--listen-port)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

logger = logging.getLogger("tcp_bridge")

# 默认监听(容器内回环,CONTRACT.md 第三节)
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 50101

# bot 默认名(环境变量 BOT_NAME 优先)
DEFAULT_BOT_NAME = "Bot"

# 平台内部常量(与 protocol_adapter.py 同源)
INITIAL_CHIPS = 20000
SMALL_BLIND = 50
BIG_BLIND = 100

# ── 卡牌编码(与 protocol_adapter.py 同源,这里独立保留以便容器内零依赖)──
# JSON 整数 = rank*4 + JUDGE_SUIT,JUDGE_SUIT = TCP_TO_JUDGE_SUIT[平台suit]
TCP_TO_JUDGE_SUIT = {0: 2, 1: 0, 2: 1, 3: 3}
JUDGE_TO_TCP_SUIT = {v: k for k, v in TCP_TO_JUDGE_SUIT.items()}


def json_int_to_tcp_card(card_int: int) -> str:
    """JSON 整数卡牌 → 国赛 ``<suit,rank>`` 文本。

    JSON 整数 = rank*4 + JUDGE_SUIT;反向映射得到平台 suit,再格式化。
    """
    judge_suit = card_int % 4
    rank = card_int // 4
    suit = JUDGE_TO_TCP_SUIT[judge_suit]
    return f"<{suit},{rank}>"


def _json_cards_to_tcp_str(cards: list[int]) -> str:
    """JSON 整数列表 → 国赛卡牌串(如 ``<0,12><1,0>``)。"""
    return "".join(json_int_to_tcp_card(c) for c in cards)


def _action_history_to_tcp(action_type: str, amount) -> str:
    """平台动作 → 国赛文本动作(转发给 bot)。

    raise → ``raise <to-total>``;call/check/fold/allin 直名。
    """
    if action_type == "raise":
        return f"raise {int(amount)}"
    if action_type in ("call", "check", "fold", "allin"):
        return action_type
    # 未知动作:转发名字,fallback
    return action_type


# ── bot 文本响应 → JSON 整数 ──────────────────────────────
def tcp_action_to_json_int(raw: str) -> int:
    """国赛文本动作 → JSON int。

    ``fold``→-1, ``call``/``check``→0, ``allin``→-2, ``raise N``→N。
    未知/空 → 0(call/check,由平台按 to_call 决定)。
    """
    line = raw.strip()
    if not line:
        return 0
    parts = line.split()
    head = parts[0]
    if head == "fold":
        return -1
    if head in ("call", "check"):
        return 0
    if head == "allin":
        return -2
    if head in ("raise", "bet") and len(parts) >= 2:
        try:
            return int(parts[1])
        except ValueError:
            return 0
    return 0


# 复用 transport.py 的 pop_client_action(无需重写分帧)
# 容器内路径不同,延迟导入以便独立运行测试。
def _import_pop_action():
    try:
        # 作为 platform 包一部分运行
        from ....server.transport import pop_client_action  # type: ignore
        return pop_client_action
    except Exception:
        try:
            from ..server.transport import pop_client_action  # type: ignore
            return pop_client_action
        except Exception:
            return None


class BridgeState:
    """桥维护的「已发给 bot 的」状态,用来对每个 JSON request 做增量翻译。

    逻辑:每次收到 request,对比上次发送的状态,计算「需要补发」的文本序列:

    1. 新手牌开始(``hand`` 变化,或 history 清空)→ 上一手可能需要 settle。
    2. preflop 消息(含 blind 判定 + 手牌)。
    3. 公共牌增量(0→3 flop, 3→4 turn, 4→5 river)。
    4. 对手动作增量(history 尾部未发的条目)。
    """

    def __init__(self) -> None:
        self.last_hand: int = 0
        self.last_history_len: int = 0
        self.last_public_len: int = 0
        # 上一手结束时,记录双方筹码供 settle 计算用
        self.prev_my_chips: int = INITIAL_CHIPS
        self.prev_opp_chips: int = INITIAL_CHIPS
        # 上一手 my_id(用于 oppo_hands/earnChips 推断)
        self.prev_my_id: int | None = None
        self.prev_hand_cards: list[int] = []
        # 标记上一手是否已 settle(防重复发 earnChips)
        self.hand_settled: bool = True

    def reset(self) -> None:
        self.last_hand = 0
        self.last_history_len = 0
        self.last_public_len = 0
        self.prev_my_chips = INITIAL_CHIPS
        self.prev_opp_chips = INITIAL_CHIPS
        self.prev_my_id = None
        self.prev_hand_cards = []
        self.hand_settled = True


def _blind_of(my_id: int, dealer_id: int) -> str:
    """heads-up:dealer = SB。返回本 bot 的 blind 串。"""
    return "SMALLBLIND" if my_id == dealer_id else "BIGBLIND"


def translate_request(state: BridgeState, req: dict) -> list[str]:
    """把一个 JSON request 增量翻译成国赛文本消息序列(发给 bot)。

    返回需要顺序发给 bot socket 的文本消息列表。每次调用前,
    bot 应处于「等待平台消息」状态(即上一次决策已完整收尾)。

    翻译规则(对照 bots/national_v142/national_bot.py handle()):

    - 新手牌:先发上一手 settle(若未发)→ 再发 ``preflop|<blind>|<cards>``。
    - 公共牌增量:flop/turn/river。
    - 对手动作增量:history 尾部未转发条目,按 action_type 转文本。
    """
    messages: list[str] = []
    hand = int(req.get("hand", 0))
    my_id = int(req.get("my_id", 0))
    dealer_id = int(req.get("dealer_id", 0))
    history = req.get("history", [])
    public_cards = req.get("public_cards", []) or []
    my_cards = req.get("my_cards", []) or []
    my_chips = int(req.get("my_chips", INITIAL_CHIPS))
    opp_chips = int(req.get("opponent_chips", INITIAL_CHIPS))

    # ── 新手牌:hand 变化或 history 清零(preflop 新一手) ──
    is_new_hand = (hand != state.last_hand) or (
        len(history) == 0 and state.last_history_len > 0
    )
    if is_new_hand and hand > 0:
        # 上一手若未 settle,补 settle(基于筹码差异)
        if not state.hand_settled and state.prev_my_id is not None:
            settle_msgs = _settle_messages(state, my_chips, opp_chips)
            messages.extend(settle_msgs)
            state.hand_settled = True

        # 新手牌:发 preflop
        blind = _blind_of(my_id, dealer_id)
        cards_str = _json_cards_to_tcp_str(my_cards)
        messages.append(f"preflop|{blind}|{cards_str}")
        state.last_hand = hand
        state.last_history_len = 0
        state.last_public_len = 0
        state.prev_my_chips = my_chips
        state.prev_opp_chips = opp_chips
        state.prev_my_id = my_id
        state.prev_hand_cards = list(my_cards)
        state.hand_settled = False

    # ── 公共牌增量 ──
    if len(public_cards) > state.last_public_len:
        new_cards = public_cards[state.last_public_len:]
        state.last_public_len = len(public_cards)
        if len(new_cards) == 3:
            messages.append(f"flop|{_json_cards_to_tcp_str(new_cards)}")
        elif len(new_cards) == 1 and len(public_cards) == 4:
            messages.append(f"turn|{_json_cards_to_tcp_str(new_cards)}")
        elif len(new_cards) == 1 and len(public_cards) == 5:
            messages.append(f"river|{_json_cards_to_tcp_str(new_cards)}")
        else:
            # allin runout 可能一帧发多张(3→5)
            # 拆成对应阶段消息,按 public_cards 总数判定阶段
            _emit_stage_incremental(messages, public_cards, new_cards)

    # ── 对手动作增量 ──
    if len(history) > state.last_history_len:
        new_actions = history[state.last_history_len:]
        state.last_history_len = len(history)
        for entry in new_actions:
            player_id = int(entry.get("player_id", 0))
            action_type = entry.get("action_type", "")
            amount = entry.get("action", 0)
            # 只转发对手动作(自己动作已通过 bot 自身决策记录)
            if player_id != my_id:
                messages.append(_action_history_to_tcp(action_type, amount))

    # 更新筹码(供下一手 settle)
    state.prev_my_chips = my_chips
    state.prev_opp_chips = opp_chips
    state.prev_my_id = my_id
    return messages


def _emit_stage_incremental(messages: list[str], public_cards: list,
                            new_cards: list) -> None:
    """处理 allin runout(一帧发多张)的公共牌分阶段消息。

    按 public_cards 总数回推应该属于哪个阶段。
    """
    total = len(public_cards)
    # 当前已处理的牌范围
    start_idx = total - len(new_cards)
    # flop(0-2)/turn(3)/river(4) 边界
    if start_idx == 0 and total >= 3:
        messages.append(f"flop|{_json_cards_to_tcp_str(public_cards[0:3])}")
        start_idx = 3
    if start_idx <= 3 and total >= 4:
        messages.append(f"turn|{_json_cards_to_tcp_str([public_cards[3]])}")
        start_idx = 4
    if start_idx <= 4 and total >= 5:
        messages.append(f"river|{_json_cards_to_tcp_str([public_cards[4]])}")


def _settle_messages(state: BridgeState, my_chips: int,
                     opp_chips: int) -> list[str]:
    """构造上一手 settle 的文本消息(earnChips)。

    **限制说明**:JSON 协议每手 reset 筹码到 INITIAL_CHIPS,且 request 不带
    胜负/底池信息。我们只能用「上一手最后一次决策时的筹码」近似估算 earnings:

        earnChips ≈ prev_my_chips - INITIAL_CHIPS

    这表示「上一手最后投入后剩余的筹码 - 起始」,负数=输了已投入的筹码,
    正数理论上不会出现(reset 前 chips <= INITIAL),所以这里主要是「输了多少」。
    bot 用 earnChips 统计输赢趋势(锦标赛上下文),不影响单手规则正确性。
    若对手 fold 给我方赢,prev_my_chips 反映赢前的状态,会有误差;
    但 bot 的策略基于 my_chips 当前值(每手 reset=20000)做主决策,earnChips
    仅做长程趋势参考,误差可接受。

    JSON→TCP 桥的已知限制:无 oppo_hands(对手手牌)信息可发(协议不传),
    仅发 earnChips。
    """
    messages: list[str] = []
    earn = state.prev_my_chips - INITIAL_CHIPS
    messages.append(f"earnChips {earn}")
    return messages


class TCPBridge:
    """桥主体:管理 socket server + bot 子进程 + stdin/stdout 主循环。"""

    def __init__(self, *, bot_entry: str, bot_name: str,
                 listen_host: str, listen_port: int,
                 bot_cwd: str | None = None,
                 bot_cmd: list[str] | None = None) -> None:
        self.bot_entry = bot_entry
        self.bot_name = bot_name
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.bot_cwd = bot_cwd
        # bot 启动命令(基础部分)。None 时默认 ["python", bot_entry]。
        # 桥自动追加国赛协议参数:--host/--port/--name。
        # 多语言:C++/Java 编译后传 ["./bot_bin"] / ["java","Main"]。
        self.bot_cmd = bot_cmd
        self.state = BridgeState()
        self._bot_reader: asyncio.StreamReader | None = None
        self._bot_writer: asyncio.StreamWriter | None = None
        self._server: asyncio.AbstractServer | None = None
        self._bot_proc: asyncio.subprocess.Process | None = None
        self._bot_connected = asyncio.Event()
        self._pop_action = _import_pop_action()

    async def run(self) -> int:
        """主入口:起 server → spawn bot → 握手 → stdin→socket→stdout 循环。

        返回进程 exit code(0 正常,非 0 异常)。
        """
        # 1. 起监听
        self._server = await asyncio.start_server(
            self._on_bot_connect,
            host=self.listen_host, port=self.listen_port,
        )
        logger.info("bridge listening on %s:%d", self.listen_host, self.listen_port)

        # 2. spawn bot(连回环)
        await self._spawn_bot()

        # 3. 等 bot 连入(最多 30s)
        try:
            await asyncio.wait_for(self._bot_connected.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("bot 连接超时,exit")
            await self._cleanup()
            return 3

        # 4. 握手:发 name 查询,收 name 响应
        await self._do_name_handshake()

        # 5. 主循环:读 stdin → 翻译 → 发 socket → 读响应 → 写 stdout
        try:
            await self._main_loop()
        except (EOFError, asyncio.IncompleteReadError):
            # 平台关闭 stdin → 正常结束
            pass
        finally:
            await self._cleanup()
        return 0

    async def _on_bot_connect(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        """bot socket 接入回调。只接受第一个连接。"""
        if self._bot_writer is not None:
            # 已有连接,拒绝多余的
            writer.close()
            return
        self._bot_reader = reader
        self._bot_writer = writer
        self._bot_connected.set()
        logger.info("bot connected from %s", writer.get_extra_info("peername"))

    async def _spawn_bot(self) -> None:
        """spawn 用户 bot 子进程。

        默认 ``python <bot_entry> --host <listen_host> --port <listen_port> --name <name>``。
        若设置了 ``bot_cmd``(多语言:C++/Java 编译后的命令),用它作基础命令。
        国赛协议参数(--host/--port/--name)统一追加到基础命令后。
        """
        base_cmd = self.bot_cmd if self.bot_cmd else [sys.executable, self.bot_entry]
        cmd = list(base_cmd) + [
            "--host", self.listen_host,
            "--port", str(self.listen_port),
            "--name", self.bot_name,
        ]
        logger.info("spawning bot: %s (cwd=%s)", " ".join(cmd), self.bot_cwd)
        self._bot_proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.bot_cwd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _do_name_handshake(self) -> None:
        """国赛握手:发 ``name`` 查询 → 收 bot 队名响应。

        bot(national_bot.py)响应队名(裸文本)。我们读完即可。
        """
        assert self._bot_writer is not None and self._bot_reader is not None
        self._bot_writer.write(b"name")
        await self._bot_writer.drain()
        # 读一行(队名,national_bot 发裸队名无换行 → 用 idle boundary)
        name = await self._recv_bot_action(timeout=10.0)
        logger.info("bot name handshake: %r", name)

    async def _recv_bot_action(self, timeout: float = 60.0) -> str | None:
        """从 bot socket 读一个动作(复用 transport.pop_client_action 分帧)。

        national_bot 发送裸文本(无换行),用 idle boundary(短超时无新字节)
        判定动作边界。fallback:若无 pop_client_action,用 readline+idle 混合。
        """
        assert self._bot_reader is not None
        deadline = asyncio.get_running_loop().time() + max(0.001, timeout)
        buf = ""
        idle_sec = 0.05  # 短 idle 判定动作边界
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                chunk = await asyncio.wait_for(
                    self._bot_reader.read(4096), timeout=min(remaining, idle_sec))
            except asyncio.TimeoutError:
                # idle 边界:若 buf 非空,判定为动作结束
                if buf:
                    return buf.strip()
                continue
            if not chunk:
                # bot 关闭连接
                return None
            buf += chunk.decode("utf-8", "replace")
            # 尝试用 pop_client_action 切出一个完整动作
            if self._pop_action is not None:
                action, remainder = self._pop_action(buf, terminal=False)
                if action is not None:
                    return action.strip()
            else:
                # 无 pop_action:fallback,有换行就切
                if "\n" in buf:
                    line, _ = buf.split("\n", 1)
                    return line.strip()
                # 否则继续读到 idle

    async def _main_loop(self) -> None:
        """stdin → translate → bot socket → 响应 → stdout。

        每轮:
        - 读 stdin 一行(JSON request)。
        - 翻译成国赛文本序列,顺序发给 bot socket。
        - bot 处理后(若需要决策)会发回一个动作。
        - 读 bot 动作 → 转 JSON int → 写 stdout ``{"response": int}``。
        """
        loop = asyncio.get_running_loop()
        stdin_reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(stdin_reader), sys.stdin)

        while True:
            line = await stdin_reader.readline()
            if not line:
                break  # 平台关闭 stdin
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                logger.error("invalid JSON from platform: %r (%s)", text, exc)
                continue
            reqs = payload.get("requests", [])
            if not reqs:
                continue
            req = dict(reqs[-1])

            # 翻译并发给 bot
            messages = translate_request(self.state, req)
            assert self._bot_writer is not None
            for msg in messages:
                self._bot_writer.write(msg.encode("utf-8"))
                await self._bot_writer.drain()

            # 读 bot 动作 → JSON int → 写 stdout
            action = await self._recv_bot_action(timeout=60.0)
            if action is None:
                # bot 超时/断开 → fold(-1)
                value = -1
            else:
                value = tcp_action_to_json_int(action)
            sys.stdout.write(json.dumps({"response": int(value)}) + "\n")
            sys.stdout.flush()

    async def _cleanup(self) -> None:
        """清理:停 bot 子进程,关 socket server,关 bot 连接。"""
        if self._bot_writer is not None:
            try:
                self._bot_writer.close()
                await asyncio.wait_for(self._bot_writer.wait_closed(), timeout=1.0)
            except (asyncio.TimeoutError, OSError, ConnectionError):
                pass
            self._bot_writer = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        if self._bot_proc is not None and self._bot_proc.returncode is None:
            try:
                self._bot_proc.terminate()
                await asyncio.wait_for(self._bot_proc.wait(), timeout=3.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._bot_proc.kill()
                except ProcessLookupError:
                    pass
            self._bot_proc = None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TCP bridge: JSON<->national TCP")
    p.add_argument("--bot-entry", required=True,
                   help="bot 入口文件(如 national_bot.py)")
    p.add_argument("--bot-name", default=None,
                   help=f"bot 队名(默认 env BOT_NAME 或 {DEFAULT_BOT_NAME!r})")
    p.add_argument("--listen-host", default=DEFAULT_LISTEN_HOST)
    p.add_argument("--listen-port", type=int, default=DEFAULT_LISTEN_PORT)
    p.add_argument("--bot-cwd", default=None, help="bot 工作目录")
    p.add_argument("--bot-cmd", default=None,
                   help="bot 启动命令(JSON 数组,如 '[\"./bot_bin\"]');"
                        "默认 [python, bot_entry]。用于 C++/Java 编译型 bot")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    bot_name = (
        args.bot_name
        or os.environ.get("BOT_NAME")
        or DEFAULT_BOT_NAME
    )
    # 解析 bot_cmd(JSON 数组),None 则用默认(python entry)
    bot_cmd: list[str] | None = None
    if args.bot_cmd:
        try:
            parsed = json.loads(args.bot_cmd)
            if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                bot_cmd = parsed
            else:
                raise ValueError("not a list of strings")
        except (json.JSONDecodeError, ValueError) as exc:
            logging.error("invalid --bot-cmd %r: %s", args.bot_cmd, exc)
            return 2
    logging.basicConfig(
        level=os.environ.get("POK_BRIDGE_LOG_LEVEL", "INFO"),
        format="[bridge %(asctime)s] %(message)s",
    )
    bridge = TCPBridge(
        bot_entry=args.bot_entry,
        bot_name=bot_name,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        bot_cwd=args.bot_cwd,
        bot_cmd=bot_cmd,
    )
    try:
        return asyncio.run(bridge.run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
