"""断线判负验收(验收 7):bot 跑 N 手后断开,serve 应连续 2 手判该方 forfeit
+ match_end reason=disconnected(arena 决策 3:仅 TCP 真断开累计,60s 超时只 fold)。

in-process 起 MatchManager(不含 web),client A 跟随、client B 跑 3 手后主动断开。
用法: python scripts/accept_disconnect.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arena.backend.server.match_manager import MatchManager


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
                # fold / 未识别:不响应
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main() -> None:
    rd = Path("/tmp/arena-disc-accept")
    if rd.exists():
        shutil.rmtree(rd)
    m = MatchManager(
        records_dir=str(rd),
        hands_per_match=1000,
        connect_timeout_sec=8,
        name_timeout_sec=8,
        action_timeout_sec=8,
    )
    serve_task = asyncio.create_task(m.serve_loop("127.0.0.1", 50105, max_matches=1))
    await asyncio.sleep(1.0)
    a = asyncio.create_task(follower("127.0.0.1", 50105, "StayA"))
    b = asyncio.create_task(follower("127.0.0.1", 50105, "QuitB", quit_after_hand=3))
    await serve_task
    for t in (a, b):
        t.cancel()
    matches = m.list_matches()
    print("matches:", matches)
    assert matches, "无比赛记录"
    last = matches[-1]
    assert last["reason"] == "disconnected", f"forfeit 未触发, reason={last['reason']}"
    print(f"✓ 断线 forfeit 验收通过: loser_idx={last['loser_idx']}, reason={last['reason']}, "
          f"hands_played={last['hands_played']}")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))
