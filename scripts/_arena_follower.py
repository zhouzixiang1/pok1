"""共享验收用 bot 客户端:follower(最小 call/check 跟随)+ bettor(每手发 bet 测非法)。

供 scripts/accept_*.py 复用;in-process 连 MatchManager 的 ArenaTCPServer。
"""
from __future__ import annotations

import asyncio


async def follower(host: str, port: int, name: str, *,
                   quit_after_hand: int | None = None, timeout: float = 30.0) -> None:
    reader, writer = await asyncio.open_connection(host, port)
    buf = ""
    hand = 0
    state = {"acted": False}
    is_sb = False
    in_allin = False

    async def recv_line():
        nonlocal buf
        while "\n" not in buf:
            d = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not d:
                return None
            buf += d.decode("utf-8", "replace")
        line, buf = buf.split("\n", 1)
        return line.rstrip("\r")

    def send(msg: str) -> None:
        writer.write((msg + "\n").encode("utf-8"))
        state["acted"] = True

    try:
        while True:
            line = await recv_line()
            if line is None:
                return
            line = line.strip()
            if not line:
                continue
            if line == "name":
                writer.write((name + "\n").encode("utf-8"))
                state["acted"] = False
                continue
            if line.startswith("preflop|"):
                parts = line.split("|")
                is_sb = len(parts) > 1 and parts[1] == "SMALLBLIND"
                in_allin = False
                state["acted"] = False
                if is_sb:
                    send("call")
                continue
            if line.startswith(("flop|", "turn|", "river|")):
                state["acted"] = False
                if not in_allin and not is_sb:
                    send("check")
                continue
            if line.startswith("earnChips"):
                in_allin = False
                hand += 1
                if quit_after_hand is not None and hand >= quit_after_hand:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
                    return
                continue
            if line.startswith("oppo_hands|"):
                continue
            if line == "allin":
                in_allin = True
            if not state["acted"]:
                if line.startswith("raise") or line == "allin":
                    send("call")
                elif line == "check":
                    send("call")
                elif line == "call":
                    send("check")
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def bettor(host: str, port: int, name: str, *, timeout: float = 30.0) -> list[str]:
    """每手首次该动作时发 'bet 100'(规则1 永远非法),记录收到的全部消息。

    返回收到的消息列表(验收"违规方静默":不应含 error/illegal)。
    """
    reader, writer = await asyncio.open_connection(host, port)
    buf = ""
    sent_this_hand = False
    received: list[str] = []

    async def recv_line():
        nonlocal buf
        while "\n" not in buf:
            d = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not d:
                return None
            buf += d.decode("utf-8", "replace")
        line, buf = buf.split("\n", 1)
        return line.rstrip("\r")

    try:
        while True:
            line = await recv_line()
            if line is None:
                break
            line = line.strip()
            if line:
                received.append(line)
            if line == "name":
                writer.write((name + "\n").encode("utf-8"))
                await writer.drain()
                continue
            if line.startswith("earnChips"):
                sent_this_hand = False  # 新一手
                continue
            if not sent_this_hand and (line.startswith("preflop|SMALLBLIND") or
                                        line.startswith("raise") or line == "call" or
                                        line.startswith(("flop|", "turn|", "river|"))):
                # SB 首动 / 对手 call/raise 后本方该动 / postflop 首动 -> 发 bet(非法)
                writer.write(b"bet 100\n")
                await writer.drain()
                sent_this_hand = True
    finally:
        try:
            writer.close()
        except Exception:
            pass
    return received
