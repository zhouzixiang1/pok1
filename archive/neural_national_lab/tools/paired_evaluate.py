#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import copy
import json
import random
import statistics
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ENGINE = ROOT / "archive" / "botzone_local" / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from archive.botzone_local.engine.battle import _PersistentBot, _call_bot  # noqa: E402
from archive.botzone_local.engine.judge import judge as judge_func  # noqa: E402
from seeded_process import SeededPersistentBot, match_bot_seeds  # noqa: E402


def _resolve(path: str) -> Path:
    p = Path(path)
    return (ROOT / p).resolve() if not p.is_absolute() else p


def _main_path(path: Path) -> Path:
    return path / "main.py" if path.is_dir() else path


def _label(path: Path) -> str:
    return path.parent.name if path.name == "main.py" else path.name


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _stats(values: list[float], hands_per_unit: int) -> dict[str, Any]:
    n = len(values)
    mean = statistics.mean(values) if values else 0.0
    median = statistics.median(values) if values else 0.0
    stddev = statistics.stdev(values) if n >= 2 else 0.0
    stderr = stddev / (n ** 0.5) if n >= 2 else 0.0
    delta = 1.96 * stderr if n >= 2 else 0.0
    scale = 70.0 / max(1, hands_per_unit)
    return {
        "samples": n,
        "mean_net": mean,
        "median_net": median,
        "stddev_net": stddev,
        "stderr_net": stderr,
        "ci95_low": mean - delta if n >= 2 else None,
        "ci95_high": mean + delta if n >= 2 else None,
        "mean_per_70_hands": mean * scale,
        "ci95_low_per_70_hands": (mean - delta) * scale if n >= 2 else None,
        "ci95_high_per_70_hands": (mean + delta) * scale if n >= 2 else None,
        "significant_positive_95": bool(n >= 2 and mean - delta > 0.0),
        "significant_negative_95": bool(n >= 2 and mean + delta < 0.0),
    }


def _fresh_initdata() -> dict[str, Any]:
    result = json.loads(judge_func(json.dumps({"log": []})))
    return copy.deepcopy(result["initdata"])


def _seeded_initdata(seed: int, max_hands: int = 70) -> dict[str, Any]:
    rng = random.Random(int(seed))
    decks = []
    for _ in range(max_hands):
        deck = list(range(52))
        rng.shuffle(deck)
        decks.append(deck)
    return {
        "max_hand": max_hands,
        "dealer": rng.randint(0, 1),
        "decks": decks,
    }


def _mirror_initdata(initdata: dict[str, Any]) -> dict[str, Any]:
    mirrored = {
        "max_hand": initdata["max_hand"],
        "dealer": (initdata["dealer"] + 1) % 2,
        "decks": [],
    }
    for deck in initdata["decks"]:
        mirrored["decks"].append(deck[:-4] + deck[-2:] + deck[-4:-2])
    return mirrored


def _play_match(
    bot0: Path,
    bot1: Path,
    initdata: dict[str, Any],
    bot_seeds: tuple[int, int] | None = None,
) -> dict[str, Any]:
    bot_paths = [str(bot0.resolve()), str(bot1.resolve())]
    if bot_seeds is None:
        persistent = [_PersistentBot(bot_paths[0]), _PersistentBot(bot_paths[1])]
    else:
        persistent = [
            SeededPersistentBot(bot_paths[0], bot_seeds[0]),
            SeededPersistentBot(bot_paths[1], bot_seeds[1]),
        ]
    try:
        result = json.loads(judge_func(json.dumps({"log": [], "initdata": copy.deepcopy(initdata)})))
        game_initdata = copy.deepcopy(result["initdata"])
        log: list[dict[str, Any]] = [{"output": result}]
        bot_requests: list[list[dict[str, Any]]] = [[], []]
        bot_responses: list[list[int]] = [[], []]
        bot_data: list[Any] = [None, None]

        while result.get("command") == "request":
            content = result.get("content", {})
            if not content:
                break
            player_id = int(next(iter(content.keys())))
            request_data = content[str(player_id)]
            response, verdict, _ = _call_bot(
                bot_paths,
                player_id,
                request_data,
                bot_requests,
                bot_responses,
                bot_data=bot_data,
                persistent_procs=persistent,
            )
            log.append({str(player_id): {"response": str(response), "verdict": verdict}, "output": None})
            result = json.loads(judge_func(json.dumps({"log": log, "initdata": game_initdata})))
            log.append({"output": result})
            if result.get("command") == "finish":
                break

        chips = [0.0, 0.0]
        if result.get("command") == "finish":
            final = result.get("display", {}).get("final_result", [])
            if len(final) >= 2:
                chips = [float(final[0]["win_chips"]), float(final[1]["win_chips"])]
        return {
            "winner": 0 if chips[0] > chips[1] else (1 if chips[1] > chips[0] else -1),
            "bot0_chips": chips[0],
            "bot1_chips": chips[1],
        }
    finally:
        for proc in persistent:
            proc.close()


def _write(output: Path | None, payload: dict[str, Any]) -> None:
    if output is None:
        return
    out = output if output.is_absolute() else ROOT / output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_payload(output: Path | None) -> dict[str, Any] | None:
    if output is None:
        return None
    path = output if output.is_absolute() else ROOT / output
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _pair_seed(args: argparse.Namespace, idx: int) -> int | None:
    if args.seed_base is None:
        return None
    return int(args.seed_base) + int(args.seed_offset) + idx * int(args.seed_stride)


def _play_pair(idx: int, paths: list[Path], opponent: Path, args: argparse.Namespace) -> dict[str, Any]:
    seed = _pair_seed(args, idx)
    initdata = _seeded_initdata(seed, args.max_hands) if seed is not None else _fresh_initdata()
    mirror = _mirror_initdata(initdata)
    row: dict[str, Any] = {
        "idx": idx,
        "seed": seed,
        "dealer": initdata["dealer"],
        "bot_seeds": {},
        "net_chips": {},
        "normal": {},
        "mirror": {},
    }
    normal_bot_seeds = match_bot_seeds(args.bot_seed_base, args.bot_seed_stride, idx, "normal")
    mirror_bot_seeds = match_bot_seeds(args.bot_seed_base, args.bot_seed_stride, idx, "mirror")
    if normal_bot_seeds is not None:
        row["bot_seeds"]["normal"] = list(normal_bot_seeds)
        row["bot_seeds"]["mirror"] = list(mirror_bot_seeds)
    for path in paths:
        label = _label(path)
        normal_result = _play_match(path, opponent, initdata, normal_bot_seeds)
        mirror_result = _play_match(path, opponent, mirror, mirror_bot_seeds)
        row["normal"][label] = normal_result
        row["mirror"][label] = mirror_result
        row["net_chips"][label] = normal_result["bot0_chips"] + mirror_result["bot0_chips"]
    return row


def _compatible_resume(existing: dict[str, Any], expected: dict[str, Any]) -> bool:
    keys = (
        "mode",
        "baseline_label",
        "seed_base",
        "seed_offset",
        "seed_stride",
        "bot_seed_base",
        "max_hands",
    )
    for key in keys:
        if existing.get(key) != expected.get(key):
            return False
    if expected.get("bot_seed_base") is not None and existing.get("bot_seed_stride") != expected.get("bot_seed_stride"):
        return False
    return existing.get("entries") == expected.get("entries")


def _existing_pairs(payload: dict[str, Any], games: int) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in payload.get("pairs", []):
        try:
            idx = int(row["idx"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < games:
            out[idx] = row
    return out


def _summarize(payload: dict[str, Any]) -> None:
    samples = {key: [] for key in payload["entries"]}
    deltas = {key: [] for key in payload["entries"] if key != payload["baseline_label"]}
    base = payload["baseline_label"]
    for row in payload["pairs"]:
        for key, value in row["net_chips"].items():
            samples[key].append(float(value))
        for key in deltas:
            deltas[key].append(float(row["net_chips"][key]) - float(row["net_chips"][base]))
    payload["results"] = {
        key: {
            **payload["entries"][key],
            "net_chips": values,
            **_stats(values, 140),
        }
        for key, values in samples.items()
    }
    payload["paired_vs_baseline"] = {
        key: {
            "baseline": base,
            "candidate": key,
            "delta_net_chips": values,
            **_stats(values, 140),
        }
        for key, values in deltas.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", action="append", required=True)
    parser.add_argument("--opponent", required=True)
    parser.add_argument("--games", type=int, default=4)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--seed-base", type=int)
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument("--seed-stride", type=int, default=1)
    parser.add_argument("--bot-seed-base", type=int)
    parser.add_argument("--bot-seed-stride", type=int, default=10000)
    parser.add_argument("--max-hands", type=int, default=70)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    opponent = _main_path(_resolve(args.opponent))
    baseline = _main_path(_resolve(args.baseline))
    paths = [baseline]
    for candidate in args.candidate:
        path = _main_path(_resolve(candidate))
        if path not in paths:
            paths.append(path)

    entries = {
        _label(path): {
            "path": _rel(path),
            "opponent": _rel(opponent),
        }
        for path in paths
    }
    payload: dict[str, Any] = {
        "mode": "common_deck_mirror_pair",
        "games": args.games,
        "workers": args.workers,
        "seed_base": args.seed_base,
        "seed_offset": args.seed_offset,
        "seed_stride": args.seed_stride,
        "bot_seed_base": args.bot_seed_base,
        "bot_seed_stride": args.bot_seed_stride,
        "max_hands": args.max_hands,
        "baseline_label": _label(baseline),
        "entries": entries,
        "pairs": [],
        "results": {},
        "paired_vs_baseline": {},
    }
    existing = _read_payload(args.output) if args.resume else None
    if existing is not None:
        if not _compatible_resume(existing, payload):
            raise SystemExit("existing output is not compatible with requested resume parameters")
        existing_by_idx = _existing_pairs(existing, args.games)
        payload["pairs"] = []
        idx = 0
        while idx in existing_by_idx:
            payload["pairs"].append(existing_by_idx[idx])
            idx += 1
        payload["games"] = args.games
        payload["workers"] = args.workers
        _summarize(payload)
    _write(args.output, payload)

    rows: dict[int, dict[str, Any]] = {}
    next_idx = len(payload["pairs"])

    def _consume_row(row: dict[str, Any]) -> None:
        nonlocal next_idx
        rows[int(row["idx"])] = row
        while next_idx in rows:
            payload["pairs"].append(rows.pop(next_idx))
            _summarize(payload)
            _write(args.output, payload)
            _print_progress(payload, next_idx + 1, args.games)
            next_idx += 1

    pending_indices = [idx for idx in range(args.games) if idx >= next_idx]
    if not pending_indices:
        _print_progress(payload, len(payload["pairs"]), args.games)
        return

    if args.workers <= 1:
        for idx in pending_indices:
            _consume_row(_play_pair(idx, paths, opponent, args))
    else:
        # judge_func and the local battle stack are not thread-safe: concurrent
        # calls can cross-contaminate seeded games. Process workers keep the
        # judge state isolated while preserving multi-core throughput.
        with ProcessPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(_play_pair, idx, paths, opponent, args): idx
                for idx in pending_indices
            }
            for future in as_completed(futures):
                _consume_row(future.result())


def _print_progress(payload: dict[str, Any], completed: int, total: int) -> None:
    paired = payload["paired_vs_baseline"]
    print(f"pair {completed}/{total}")
    for label, result in payload["results"].items():
        print(
            f"  {label}: mean70={result['mean_per_70_hands']:.1f} "
            f"ci70=[{result['ci95_low_per_70_hands']}, {result['ci95_high_per_70_hands']}] "
            f"nets={result['net_chips']}"
        )
    for label, result in paired.items():
        print(
            f"  delta {label}: mean70={result['mean_per_70_hands']:.1f} "
            f"ci70=[{result['ci95_low_per_70_hands']}, {result['ci95_high_per_70_hands']}] "
            f"deltas={result['delta_net_chips']}"
        )


if __name__ == "__main__":
    main()
