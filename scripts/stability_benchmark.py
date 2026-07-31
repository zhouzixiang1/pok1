#!/usr/bin/env python3
"""对战稳定性压测:跑大量对战,量化崩溃率 + 捕获崩溃诊断。

用法:
  .venv/bin/python scripts/stability_benchmark.py            # 默认配置
  .venv/bin/python scripts/stability_benchmark.py --matches 30 --concurrency 2

指标:
  - 完成率(completed / 总场次)
  - 中止率 + 中止模式分类(TimeoutError / RuntimeError / OSError)
  - 中止手数分布(崩溃发生在第几手)
  - 单场耗时分布
  - 每次崩溃的完整 reason(含容器 stderr,用于排查)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

os.environ.setdefault("POK_PLATFORM_RATE_LIMIT", "0")
os.environ.setdefault("POK_PLATFORM_MAX_CONCURRENT_MATCHES", "8")

from arena.backend.platform.store import Store  # noqa: E402
from arena.backend.platform.runtime.bot_manager import BotManager  # noqa: E402
from arena.backend.platform.runtime.docker_runner import DockerRunner  # noqa: E402
from arena.backend.platform.runtime.orchestrator import MatchOrchestrator  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(message)s")


@dataclass
class MatchOutcome:
    idx: int
    bot_a: str
    bot_b: str
    status: str = ""
    hands: int = 0
    duration: float = 0.0
    reason: str = ""
    error: str = ""


async def run_one(orch: MatchOrchestrator, store: Store, idx: int,
                  bot_a_id: int, bot_b_id: int,
                  name_a: str, name_b: str) -> MatchOutcome:
    """跑一场对战,返回结果。并发满时自动重试。"""
    oc = MatchOutcome(idx=idx, bot_a=name_a, bot_b=name_b)
    t0 = time.time()
    try:
        # challenge 可能因并发满被拒,重试到成功(最多 5 分钟)
        mid = None
        challenge_deadline = time.time() + 300
        while time.time() < challenge_deadline:
            try:
                mid = await orch.challenge(
                    challenger_bot_id=bot_a_id, opponent_bot_id=bot_b_id,
                    owner_user_id=1)
                break
            except ValueError as exc:
                if "并发" in str(exc) or "已满" in str(exc):
                    await asyncio.sleep(2)  # 等并发空位
                    continue
                raise
        if mid is None:
            oc.error = "challenge 重试超时(并发一直满)"
        else:
            # 轮询(单场最多 4 分钟)
            deadline = time.time() + 240
            m = None
            while time.time() < deadline:
                await asyncio.sleep(2)
                m = store.get_match(mid)
                if m and m["status"] in ("completed", "aborted"):
                    break
            if m is None:
                oc.error = "no match row"
            else:
                oc.status = m["status"]
                oc.hands = m["hands_played"]
                oc.reason = m["reason"]
    except Exception as exc:
        oc.error = f"{type(exc).__name__}: {exc}"
    finally:
        oc.duration = time.time() - t0
    # 标记符号显示进度
    sym = "✅" if oc.status == "completed" else "❌"
    hands_str = f"{oc.hands}/70"
    print(f"  [{oc.idx:2d}] {sym} {oc.bot_a}v{oc.bot_b} "
          f"{oc.status} {hands_str} {oc.duration:.0f}s "
          f"{oc.reason[:50] if oc.reason else oc.error[:50]}",
          flush=True)
    return oc


async def main_async(args: argparse.Namespace) -> int:
    db_path = args.db_path or "arena_platform.db"
    store = Store(db_path)
    mgr = BotManager(store)
    sys_uid = store.get_user_by_username("system")["id"]
    # 确保内置 bot 注册 + 镜像预构建(避免懒构建干扰稳定性数据)
    mgr.register_builtin_bots(sys_uid)
    # 预构建所有内置 bot 镜像(预热)
    builtin = store.list_bots(owner_id=sys_uid, active_only=True,
                              include_builtin=True)
    print(f"预热镜像({len(builtin)} 个内置 bot)...", flush=True)
    for b in builtin:
        if b["protocol"] == "tcp" and not b["docker_image"]:
            try:
                mgr.build_builtin_image(b["id"])
                print(f"  built {b['name']}", flush=True)
            except Exception as exc:
                print(f"  !! {b['name']} 构建失败: {exc}", flush=True)
    # 重新取(含镜像)
    builtin = store.list_bots(owner_id=sys_uid, active_only=True,
                              include_builtin=True)
    bot_pool = [(b["id"], b["name"]) for b in builtin if b["protocol"] == "tcp"]
    if len(bot_pool) < 2:
        print("内置 bot 不足 2 个,无法测试", file=sys.stderr)
        return 1
    print(f"内置 bot 池({len(bot_pool)}): {[n for _, n in bot_pool]}", flush=True)

    runner = DockerRunner()
    # orchestrator 并发上限设得比 worker 数大,保证 worker 的 challenge 不被拒
    # (worker 串行消费,实际并发 = concurrency)。
    orch = MatchOrchestrator(store, runner, bot_manager=mgr,
                             hands_per_match=70, action_timeout=60.0,
                             max_concurrent=args.concurrency + 2)
    total = args.matches
    print(f"\n开始稳定性压测:{total} 场,并发 {args.concurrency}\n", flush=True)

    # 生成对战组合(轮换,覆盖不同 bot 配对)
    pairs = []
    for i in range(total):
        a = bot_pool[i % len(bot_pool)]
        b = bot_pool[(i + 1) % len(bot_pool)]
        if a[0] == b[0]:  # 避免自打自
            b = bot_pool[(i + 2) % len(bot_pool)]
        pairs.append((a, b))

    outcomes: list[MatchOutcome] = []
    t_start = time.time()
    # 生产者-消费者:challenge 在并发满时是同步拒绝(ValueError),不是排队,
    # 所以用 N 个 worker 从队列取任务,每个跑完再取下一个(避免 gather herd)。
    # orchestrator.max_concurrent 设得比 worker 数大,保证 worker 不被拒。
    queue: asyncio.Queue[tuple[int, tuple, tuple]] = asyncio.Queue()
    for i, (a, b) in enumerate(pairs):
        queue.put_nowait((i + 1, a, b))

    async def worker():
        results: list[MatchOutcome] = []
        while not queue.empty():
            try:
                idx, a, b = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            results.append(await run_one(orch, store, idx, a[0], b[0], a[1], b[1]))
        return results

    workers = await asyncio.gather(*[worker() for _ in range(args.concurrency)])
    for w in workers:
        outcomes.extend(w)
    # 按场次序号排序(便于阅读)
    outcomes.sort(key=lambda o: o.idx)
    await runner.cleanup_all()
    total_dur = time.time() - t_start

    # ══════════════════════════════════════════════════════════
    # 统计
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("稳定性压测结果")
    print("=" * 60)
    completed = [o for o in outcomes if o.status == "completed"]
    aborted = [o for o in outcomes if o.status == "aborted"]
    errored = [o for o in outcomes if o.status not in ("completed", "aborted")]

    print(f"总场次:     {len(outcomes)}")
    print(f"完成:       {len(completed)} ({len(completed)*100//len(outcomes)}%)")
    print(f"中止:       {len(aborted)} ({len(aborted)*100//len(outcomes)}%)")
    print(f"异常:       {len(errored)}")
    print(f"总耗时:     {total_dur:.0f}s")

    if completed:
        durs = [o.duration for o in completed]
        hands_all = [o.hands for o in completed]
        print(f"\n[完成场次]")
        print(f"  耗时:   中位 {statistics.median(durs):.0f}s, "
              f"均值 {statistics.mean(durs):.0f}s, "
              f"范围 {min(durs):.0f}-{max(durs):.0f}s")
        print(f"  手数:   全部 {min(hands_all)}-{max(hands_all)} "
              f"({'正常' if all(h == 70 for h in hands_all) else '有不完整!'})")

    if aborted:
        print(f"\n[中止场次] ({len(aborted)} 场)")
        # 崩溃模式分类(从 reason 提取异常类型)
        modes = Counter()
        abort_hands = []
        for o in aborted:
            abort_hands.append(o.hands)
            r = o.reason
            if "TimeoutError" in r:
                modes["TimeoutError(响应超时)"] += 1
            elif "RuntimeError" in r:
                modes["RuntimeError(容器崩溃/stdout关闭)"] += 1
            elif "OSError" in r:
                modes["OSError(IO错误)"] += 1
            else:
                modes[r.split(":")[0] if ":" in r else "未知"] += 1
        print(f"  崩溃手数: {sorted(abort_hands)}")
        print(f"  崩溃模式:")
        for mode, cnt in modes.most_common():
            print(f"    {mode}: {cnt}")
        print(f"  详情:")
        for o in aborted:
            print(f"    [{o.idx}] {o.bot_a}v{o.bot_b} hand={o.hands} "
                  f"{o.reason[:120]}")

    if errored:
        print(f"\n[异常场次]")
        for o in errored:
            print(f"    [{o.idx}] {o.error[:120]}")

    print("=" * 60)
    # 退出码:有中止/异常返回 1(供 CI 判定)
    return 0 if not aborted and not errored else 1


def main() -> int:
    p = argparse.ArgumentParser(description="对战稳定性压测")
    p.add_argument("--matches", type=int, default=20, help="总场次(默认 20)")
    p.add_argument("--concurrency", type=int, default=2, help="并发(默认 2)")
    p.add_argument("--db-path", default=None, help="db 路径(默认 arena_platform.db)")
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
