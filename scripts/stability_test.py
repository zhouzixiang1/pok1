"""平台稳定性压力测试:10 个 national bot round-robin × N 轮,PARALLEL 路并行
(多 serve 端口同时跑多对)。验证平台跨大量对战不崩/不漏/THP 完整/无死锁。

用法:
  python scripts/stability_test.py                 # 默认 45 对 × 5 轮 = 225 场 × 70 手, 4 路并行
  python scripts/stability_test.py --rounds 1 --hands 10 --parallel 2   # 快速冒烟
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from arena.backend.server.match_manager import MatchManager

BOTS = [
    "national_v115", "national_v117", "national_v119", "national_v120",
    "national_v121", "national_v122", "national_v123", "national_v135",
    "national_v141", "national_v142",
]
BOPY = "/home/zzx/anaconda3/envs/pytorch/bin/python3"
BOT_DIR = ROOT / "bots"
BASE_PORT = 50110


async def run_one(ai: int, bi: int, round_i: int, port: int,
                  records_dir: Path, hands: int, log: list, wid: int) -> None:
    """跑一场 BOTS[ai] vs BOTS[bi](单 serve max_matches=1 + 2 bot subprocess)。

    每场写 ``match_dir/``:botA.log/botB.log(native_bot wire RECV/SEND)、
    botA.stdout/botB.stdout(进程输出/错误)、events.jsonl(serve 事件流)、
    result.json(结果摘要)、+ 该场 THP/index(MatchManager records)。
    """
    match_dir = records_dir / f"{BOTS[ai]}_vs_{BOTS[bi]}_r{round_i}"
    match_dir.mkdir(parents=True, exist_ok=True)
    mm = MatchManager(
        records_dir=str(match_dir), hands_per_match=hands,
        connect_timeout_sec=20, name_timeout_sec=20, action_timeout_sec=60,
    )
    serve_task = asyncio.create_task(mm.serve_loop("127.0.0.1", port, max_matches=1))
    await asyncio.sleep(0.6)  # 等 listening
    env = {**os.environ, "POK_OFFICIAL_ACTION_DELAY": "0"}
    name_a = f"{BOTS[ai]}_r{round_i}A"
    name_b = f"{BOTS[bi]}_r{round_i}B"
    t0 = time.perf_counter()
    opened_files: list = []
    procs: list[asyncio.subprocess.Process] = []
    status = "no_record"
    try:
        for name, v, tag in [(name_a, ai, "A"), (name_b, bi, "B")]:
            log_path = match_dir / f"bot{tag}.log"
            outf = open(match_dir / f"bot{tag}.stdout", "w")
            opened_files.append(outf)
            p = await asyncio.create_subprocess_exec(
                BOPY, str(BOT_DIR / BOTS[v] / "national_bot.py"),
                "--host", "127.0.0.1", "--port", str(port), "--name", name,
                "--log", str(log_path),
                stdout=outf, stderr=asyncio.subprocess.STDOUT, env=env,
            )
            procs.append(p)
        try:
            await asyncio.wait_for(serve_task, timeout=hands * 5 + 90)
        except asyncio.TimeoutError:
            status = "serve_timeout"
    finally:
        for p in procs:
            try:
                p.kill()
                await asyncio.wait_for(p.wait(), timeout=5)
            except Exception:
                pass
        if not serve_task.done():
            serve_task.cancel()
            try:
                await serve_task
            except Exception:
                pass
        for f in opened_files:
            try:
                f.close()
            except Exception:
                pass

    elapsed = time.perf_counter() - t0
    matches = mm.list_matches()

    # 持久化 serve 事件流(hand_start/stage/action/settle/match_end 等)
    try:
        events = list(getattr(mm, "_event_log", []))
        with open(match_dir / "events.jsonl", "w") as ef:
            for e in events:
                ef.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass

    entry = {"a": BOTS[ai], "b": BOTS[bi], "round": round_i, "sec": round(elapsed, 1)}
    if matches:
        m = matches[-1]
        thp = mm.read_thp(m["match_id"]) or ""
        thp_ok = thp.count("STATE:") >= m["hands_played"]
        entry.update({"status": m["reason"], "hands": m["hands_played"],
                      "earnings": m["total_earnings"], "thp_ok": thp_ok,
                      "match_id": m["match_id"]})
        try:
            (match_dir / "result.json").write_text(
                json.dumps(m, ensure_ascii=False, indent=2))
            if thp:
                (match_dir / "final.thp").write_text(thp, encoding="gb2312", errors="replace")
        except Exception:
            pass
    else:
        entry["status"] = status
    log.append(entry)
    print(f"[w{wid}] {BOTS[ai]} vs {BOTS[bi]} r{round_i} -> {entry['status']} "
          f"({entry.get('hands', '-')}手, {entry['sec']}s) 日志:{match_dir.name}", flush=True)


async def worker(wid: int, jobs: list, records_root: Path, hands: int,
                 log: list, lock: asyncio.Lock) -> None:
    port = BASE_PORT + wid
    rd = records_root / f"worker{wid}"
    rd.mkdir(parents=True, exist_ok=True)
    for i, (ai, bi, ri) in enumerate(jobs):
        await run_one(ai, bi, ri, port, rd, hands, log, wid)
        async with lock:
            done = len(log)
        if (i + 1) % 5 == 0:
            print(f"  [w{wid}] 进度 {i+1}/{len(jobs)} (总完成 {done})", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--hands", type=int, default=70)
    ap.add_argument("--parallel", type=int, default=4)
    args = ap.parse_args()

    pairs = [(a, b, r) for r in range(args.rounds)
             for a, b in itertools.combinations(range(len(BOTS)), 2)]
    print(f"=== 稳定性测试: {len(pairs)} 场 × {args.hands} 手, "
          f"{args.parallel} 路并行 ===", flush=True)

    records_root = Path("/tmp/stability-records")
    shutil.rmtree(records_root, ignore_errors=True)
    records_root.mkdir(parents=True)
    log: list = []
    lock = asyncio.Lock()
    chunks: list[list] = [[] for _ in range(args.parallel)]
    for i, p in enumerate(pairs):
        chunks[i % args.parallel].append(p)

    t0 = time.perf_counter()
    await asyncio.gather(*[
        worker(i, chunks[i], records_root, args.hands, log, lock)
        for i in range(args.parallel)
    ])
    elapsed = time.perf_counter() - t0

    c = Counter(r.get("status") for r in log)
    total_hands = sum(r.get("hands", 0) for r in log)
    thp_ok = sum(1 for r in log if r.get("thp_ok"))
    thp_fail = [r for r in log if r.get("status") == "completed" and not r.get("thp_ok")]
    bad = [r for r in log if r.get("status") != "completed"]

    print(f"\n=== 完成 {len(log)}/{len(pairs)} 场, 耗时 {elapsed:.0f}s ({elapsed/60:.1f}min) ===", flush=True)
    print(f"状态分布: {dict(c)}", flush=True)
    print(f"总手数: {total_hands}, THP 完整: {thp_ok}/{len(log)}", flush=True)
    if thp_fail:
        print(f"!! THP 不完整 {len(thp_fail)} 场: {thp_fail[:3]}", flush=True)
    if bad:
        print(f"!! 非 completed {len(bad)} 场: {bad[:5]}", flush=True)

    report = {
        "total_pairs": len(pairs), "done": len(log), "elapsed_sec": round(elapsed, 1),
        "status_counts": dict(c), "total_hands": total_hands, "thp_ok": thp_ok,
        "rounds": args.rounds, "hands_per_match": args.hands, "parallel": args.parallel,
        "bots": BOTS, "matches": log,
    }
    out = ROOT / "STABILITY_REPORT.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"报告写出: {out}", flush=True)

    ok = c.get("completed", 0) == len(pairs) and thp_ok == len(pairs) and not bad
    print(f"\n=== 稳定性: {'✓ 全部通过(' + str(len(pairs)) + '场完成 + THP完整 + 无崩溃/断线/死锁)' if ok else '⚠ 有异常见上'} ===",
          flush=True)


if __name__ == "__main__":
    asyncio.run(main())
