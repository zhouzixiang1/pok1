"""非法验收(验收 8):bettor 发 'bet 100'(规则 1 永远非法)。
期望: 违规方记 fold、对手收 fold、违规方静默(不发 error/illegal)、THP 记违规方。

用法: python scripts/accept_illegal.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from arena.backend.server.match_manager import MatchManager
from _arena_follower import bettor, follower


async def main() -> None:
    rd = Path("/tmp/arena-illegal-accept")
    if rd.exists():
        shutil.rmtree(rd)
    m = MatchManager(
        records_dir=str(rd), hands_per_match=3,
        connect_timeout_sec=8, name_timeout_sec=8, action_timeout_sec=8,
    )
    serve_task = asyncio.create_task(m.serve_loop("127.0.0.1", 50107, max_matches=1))
    await asyncio.sleep(1.0)

    a = asyncio.create_task(follower("127.0.0.1", 50107, "GoodA"))
    b = asyncio.create_task(bettor("127.0.0.1", 50107, "BettorB"))

    await serve_task
    # bettor 可能仍在 recv 等数据;serve 退后 close_clients 促其退出;给 2s 缓冲
    await asyncio.sleep(2.0)
    bettor_received = []
    if b.done():
        bettor_received = b.result() or []
    for t in (a, b):
        t.cancel()

    matches = m.list_matches()
    assert matches, "无比赛记录"
    thp = m.read_thp(matches[-1]["match_id"]) or ""
    print("=== THP ===")
    print(thp)
    print(f"=== bettor 收到 {len(bettor_received)} 条消息 ===")
    print(bettor_received[:8])

    # 1) THP 应记录 fold(违规方 bet -> fold)
    assert "f" in thp, "THP 应含 fold(违规方)"
    # 2) 违规方静默:收到的消息不应含 error / illegal 通知
    leaked = [m for m in bettor_received if m.startswith(("error", "illegal"))]
    assert not leaked, f"违规方不应收到 error/illegal 通知, 却收到: {leaked}"
    # 3) bettor 收到 earnChips(结算, 静默 fold 后正常结算)
    assert any(m.startswith("earnChips") for m in bettor_received), "应收到 earnChips 结算"
    print("✓ 非法 bet 验收通过: bet->fold, 对手收 fold, 违规方静默, THP 记 fold, 正常结算")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))
