"""重开验收(验收 9):第一场跑完后 close_clients re-arm 回 listening,
第二对 bot 自动开赛。serve_loop --max-matches 2, 验证 2 场全 completed。

用法: python scripts/accept_rearm.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from arena.backend.server.match_manager import MatchManager
from _arena_follower import follower


async def main() -> None:
    rd = Path("/tmp/arena-rearm-accept")
    if rd.exists():
        shutil.rmtree(rd)
    m = MatchManager(
        records_dir=str(rd), hands_per_match=3,
        connect_timeout_sec=8, name_timeout_sec=8, action_timeout_sec=8,
    )
    serve_task = asyncio.create_task(m.serve_loop("127.0.0.1", 50106, max_matches=2))
    await asyncio.sleep(1.0)

    a1 = asyncio.create_task(follower("127.0.0.1", 50106, "A1"))
    b1 = asyncio.create_task(follower("127.0.0.1", 50106, "B1"))
    # 等第一场结束(matches_played>=1)
    while m._matches_played < 1:
        await asyncio.sleep(0.2)
    # re-arm:serve close_clients 后回 listening,等第二对
    await asyncio.sleep(0.6)
    a2 = asyncio.create_task(follower("127.0.0.1", 50106, "A2"))
    b2 = asyncio.create_task(follower("127.0.0.1", 50106, "B2"))

    await serve_task
    for t in (a1, b1, a2, b2):
        t.cancel()

    matches = m.list_matches()
    print("matches:", [(x["match_id"], x["reason"], x["hands_played"]) for x in matches])
    assert len(matches) == 2, f"应跑 2 场, 实际 {len(matches)}"
    assert all(x["reason"] == "completed" for x in matches), "应全部 completed"
    names_sets = [{x["names"][0], x["names"][1]} for x in matches]
    assert {"A1", "B1"} in names_sets and {"A2", "B2"} in names_sets, "两对 bot 各一场"
    print(f"✓ 重开验收通过: {len(matches)} 场全部 completed, re-arm 正确")


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), timeout=60))
