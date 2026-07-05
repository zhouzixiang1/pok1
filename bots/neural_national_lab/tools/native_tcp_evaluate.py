#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
WEB_CORE = ROOT / "web" / "core"
if str(WEB_CORE) not in sys.path:
    sys.path.insert(0, str(WEB_CORE))

from national_native import run_native_tcp_pair  # noqa: E402


def _resolve(path: str) -> Path:
    raw = Path(path)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


def _seeds(args: argparse.Namespace) -> list[int | None]:
    if args.seeds:
        return [int(seed) for seed in args.seeds.split(",") if seed.strip()]
    if args.seed_base is None:
        return [None for _ in range(args.matches)]
    return [int(args.seed_base) + idx * int(args.seed_stride) for idx in range(args.matches)]


def _bot_seed(args: argparse.Namespace, match_idx: int, opponent_idx: int) -> int | None:
    if args.bot_seed_base is None:
        return None
    return int(args.bot_seed_base) + match_idx * int(args.bot_seed_stride) + opponent_idx * 100_000


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_opponent: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_opponent.setdefault(row["opponent"], {"rows": []})["rows"].append(row)
    for opponent, payload in by_opponent.items():
        subset = payload.pop("rows")
        values = [int(row["net_chips"]) for row in subset]
        hands = sum(int(row["hands_played"]) for row in subset)
        payload.update({
            "matches": len(subset),
            "hands": hands,
            "compliant_matches": sum(1 for row in subset if row["passed_compliance"]),
            "total_net_chips": sum(values),
            "mean_net_chips": round(statistics.mean(values), 3) if values else 0.0,
            "median_net_chips": statistics.median(values) if values else 0,
            "mean_per_hand": round(sum(values) / max(1, hands), 3),
            "wins": sum(1 for value in values if value > 0),
            "losses": sum(1 for value in values if value < 0),
            "draws": sum(1 for value in values if value == 0),
            "samples": values,
            "issues": [row for row in subset if row["issues"]],
            "candidate_illegal_total": sum(row["candidate_illegal"] for row in subset),
            "candidate_timeouts_total": sum(row["candidate_timeouts"] for row in subset),
            "opponent_illegal_total": sum(row["opponent_illegal"] for row in subset),
            "opponent_timeouts_total": sum(row["opponent_timeouts"] for row in subset),
            "adapter_actions_candidate_total": sum(row["adapter_actions_candidate"] for row in subset),
            "adapter_actions_opponent_total": sum(row["adapter_actions_opponent"] for row in subset),
        })
    combined = [int(row["net_chips"]) for row in rows]
    combined_hands = sum(int(row["hands_played"]) for row in rows)
    return {
        "combined": {
            "matches": len(rows),
            "hands": combined_hands,
            "compliant_matches": sum(1 for row in rows if row["passed_compliance"]),
            "total_net_chips": sum(combined),
            "mean_per_hand": round(sum(combined) / max(1, combined_hands), 3),
            "wins": sum(1 for value in combined if value > 0),
            "losses": sum(1 for value in combined if value < 0),
            "draws": sum(1 for value in combined if value == 0),
        },
        "opponents": by_opponent,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    candidate = _resolve(args.candidate)
    opponents = [_resolve(item) for item in args.opponent]
    seed_values = _seeds(args)
    semaphore = asyncio.Semaphore(max(1, int(args.workers)))
    started = time.time()

    async def one(opponent_idx: int, opponent: Path, match_idx: int, deck_seed: int | None) -> dict[str, Any]:
        async with semaphore:
            result = await run_native_tcp_pair(
                candidate,
                opponent,
                int(args.hands),
                require_native_a=True,
                require_native_b=not args.allow_generated_opponent_entry,
                deck_seed_base=deck_seed,
                bot_seed_base=_bot_seed(args, match_idx, opponent_idx),
                timeout_sec=float(args.timeout_sec),
            )
            bot_a = result["bot_a"]
            bot_b = result["bot_b"]
            return {
                "candidate": bot_a,
                "opponent": bot_b,
                "opponent_path": str(opponent),
                "match_idx": match_idx,
                "deck_seed_base": deck_seed,
                "bot_seed_base": result.get("bot_seed_base"),
                "hands_played": int(result["hands_played"]),
                "net_chips": int(result["net_chips_a"]),
                "net_chips_per_hand": result["net_chips_a_per_hand"],
                "passed_compliance": bool(result["passed_compliance"]),
                "issues": result["issues"],
                "candidate_illegal": result["per_player"][bot_a]["illegal_actions"],
                "candidate_timeouts": result["per_player"][bot_a]["timeouts"],
                "opponent_illegal": result["per_player"][bot_b]["illegal_actions"],
                "opponent_timeouts": result["per_player"][bot_b]["timeouts"],
                "adapter_actions_candidate": result["per_player"][bot_a]["adapter"]["actions_sent"],
                "adapter_actions_opponent": result["per_player"][bot_b]["adapter"]["actions_sent"],
                "candidate_native": result["per_player"][bot_a]["native"],
                "opponent_native": result["per_player"][bot_b]["native"],
            }

    tasks = [
        one(opponent_idx, opponent, match_idx, deck_seed)
        for opponent_idx, opponent in enumerate(opponents)
        for match_idx, deck_seed in enumerate(seed_values)
    ]
    rows = []
    for coro in asyncio.as_completed(tasks):
        row = await coro
        rows.append(row)
        if args.print_rows:
            print(json.dumps(row, ensure_ascii=False), flush=True)
    payload = {
        "execution_mode": "native_tcp",
        "candidate_path": str(candidate),
        "opponent_paths": [str(path) for path in opponents],
        "hands_per_match": int(args.hands),
        "seeds": seed_values,
        "workers": int(args.workers),
        "requires_native_opponents": not args.allow_generated_opponent_entry,
        "bot_seed_base": args.bot_seed_base,
        "elapsed_sec": round(time.time() - started, 3),
        "rows": sorted(rows, key=lambda row: (row["opponent"], row["match_idx"])),
    }
    payload.update(_summary(payload["rows"]))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate neural lab bots through native national TCP matches.")
    parser.add_argument("--candidate", required=True, help="Candidate bot directory containing native national_bot.py.")
    parser.add_argument("--opponent", action="append", required=True, help="Opponent bot directory. Repeat for multiple opponents.")
    parser.add_argument("--hands", type=int, default=10, help="Hands per match, capped by the native runner at 70.")
    parser.add_argument("--matches", type=int, default=10, help="Number of matches per opponent when --seeds is not provided.")
    parser.add_argument("--seed-base", type=int, default=None, help="Deck seed base for deterministic decks.")
    parser.add_argument("--seed-stride", type=int, default=1)
    parser.add_argument("--seeds", default="", help="Comma-separated deck seeds. Overrides --matches and --seed-base.")
    parser.add_argument("--bot-seed-base", type=int, default=None, help="Seed Python random in each native bot process.")
    parser.add_argument("--bot-seed-stride", type=int, default=10)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    parser.add_argument("--allow-generated-opponent-entry", action="store_true", help="Allow template native entry for legacy opponents. Off by default.")
    parser.add_argument("--print-rows", action="store_true")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    args = parser.parse_args()

    payload = asyncio.run(_run(args))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.output:
        output = _resolve(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
