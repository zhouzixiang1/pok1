#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import statistics
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ENGINE = ROOT / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from engine.battle import _PersistentBot, _call_bot  # noqa: E402
from judge import judge as judge_func  # noqa: E402


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


def _mirror_initdata(initdata: dict[str, Any]) -> dict[str, Any]:
    mirrored = {
        "max_hand": initdata["max_hand"],
        "dealer": (initdata["dealer"] + 1) % 2,
        "decks": [],
    }
    for deck in initdata["decks"]:
        mirrored["decks"].append(deck[:-4] + deck[-2:] + deck[-4:-2])
    return mirrored


def _play_match(bot0: Path, bot1: Path, initdata: dict[str, Any]) -> dict[str, Any]:
    bot_paths = [str(bot0.resolve()), str(bot1.resolve())]
    persistent = [_PersistentBot(bot_paths[0]), _PersistentBot(bot_paths[1])]
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
        "baseline_label": _label(baseline),
        "entries": entries,
        "pairs": [],
        "results": {},
        "paired_vs_baseline": {},
    }
    _write(args.output, payload)

    for idx in range(args.games):
        initdata = _fresh_initdata()
        mirror = _mirror_initdata(initdata)
        row: dict[str, Any] = {
            "idx": idx,
            "dealer": initdata["dealer"],
            "net_chips": {},
            "normal": {},
            "mirror": {},
        }
        for path in paths:
            label = _label(path)
            normal_result = _play_match(path, opponent, initdata)
            mirror_result = _play_match(path, opponent, mirror)
            row["normal"][label] = normal_result
            row["mirror"][label] = mirror_result
            row["net_chips"][label] = normal_result["bot0_chips"] + mirror_result["bot0_chips"]
        payload["pairs"].append(row)
        _summarize(payload)
        _write(args.output, payload)
        paired = payload["paired_vs_baseline"]
        print(f"pair {idx + 1}/{args.games}")
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
