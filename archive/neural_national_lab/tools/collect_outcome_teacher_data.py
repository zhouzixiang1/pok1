#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from blueprint_contract import CONTRACT_VERSION, legal_mask, state_from_request  # noqa: E402
from archive.botzone_local.engine.battle import battle, mirror_battle  # noqa: E402
from feature_spec import LABELS, encode_features, label_action  # noqa: E402


def _resolve(path: Path) -> Path:
    return (ROOT / path).resolve() if not path.is_absolute() else path


def _label(path: Path) -> str:
    return path.parent.name if path.name == "main.py" else path.name


def _hand_deltas(rows: list[dict[str, Any]]) -> dict[int, float]:
    deltas: dict[int, float] = {}
    for row in rows:
        output = row.get("output") if isinstance(row, dict) else None
        if not isinstance(output, dict):
            continue
        display = output.get("display") or {}
        temp_result = display.get("temp_result")
        if not isinstance(temp_result, list) or len(temp_result) < 2:
            continue
        matchdata = display.get("matchdata") or {}
        try:
            current_hand = int(matchdata.get("hand", 0))
        except (TypeError, ValueError):
            current_hand = 0
        hand = current_hand if output.get("command") == "finish" else max(0, current_hand - 1)
        try:
            deltas[hand] = float(temp_result[0].get("win_chips", 0.0))
        except (TypeError, ValueError, AttributeError):
            deltas[hand] = 0.0
    return deltas


def _weight(delta: float, positive_scale: float, negative_scale: float, min_weight: float, max_weight: float) -> float:
    if delta > 0:
        value = 1.0 + min(max_weight - 1.0, delta / max(1.0, positive_scale))
    elif delta < 0:
        value = negative_scale / (1.0 + abs(delta) / max(1.0, positive_scale))
    else:
        value = 0.7
    return max(min_weight, min(max_weight, value))


def _extract(
    game_log: dict[str, Any],
    teacher: str,
    opponent: str,
    *,
    positive_scale: float,
    negative_scale: float,
    min_weight: float,
    max_weight: float,
    min_abs_delta: float,
) -> list[dict[str, Any]]:
    rows = game_log.get("logs") or []
    deltas = _hand_deltas(rows)
    samples: list[dict[str, Any]] = []
    for idx in range(0, max(0, len(rows) - 1), 2):
        out = rows[idx].get("output") if isinstance(rows[idx], dict) else None
        resp_row = rows[idx + 1] if isinstance(rows[idx + 1], dict) else {}
        if not isinstance(out, dict):
            continue
        req = (out.get("content") or {}).get("0")
        resp = resp_row.get("0")
        if not req or not resp:
            continue
        try:
            action = int(resp.get("response", -1))
            hand = int(req.get("hand", -1))
        except (TypeError, ValueError):
            continue
        hand_delta = float(deltas.get(hand, 0.0))
        if abs(hand_delta) < min_abs_delta:
            continue
        display = out.get("display") or {}
        label = label_action(action, req, display)
        state = state_from_request(req, display)
        mask = legal_mask(req, state)
        if 0 <= label < len(mask):
            mask[label] = 1
        weight = _weight(hand_delta, positive_scale, negative_scale, min_weight, max_weight)
        samples.append(
            {
                "features": encode_features(req, display),
                "label": label,
                "legal_mask": mask,
                "weight": weight,
                "action": action,
                "meta": {
                    "teacher": teacher,
                    "opponent": opponent,
                    "label_name": LABELS[label],
                    "hand": hand,
                    "hand_delta": hand_delta,
                    "contract": CONTRACT_VERSION,
                    "target": "teacher_action_outcome_weighted",
                },
            }
        )
    return samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--teacher", action="append", required=True, type=Path)
    parser.add_argument("--opponent", action="append", required=True, type=Path)
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--mode", choices=["battle", "mirror"], default="mirror")
    parser.add_argument("--positive-scale", type=float, default=600.0)
    parser.add_argument("--negative-scale", type=float, default=0.30)
    parser.add_argument("--min-weight", type=float, default=0.08)
    parser.add_argument("--max-weight", type=float, default=3.0)
    parser.add_argument("--min-abs-delta", type=float, default=0.0)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    samples: list[dict[str, Any]] = []
    pair_counts: dict[str, int] = {}
    for teacher_arg in args.teacher:
        teacher = _resolve(teacher_arg)
        teacher_main = teacher / "main.py" if teacher.is_dir() else teacher
        for opponent_arg in args.opponent:
            opponent = _resolve(opponent_arg)
            opponent_main = opponent / "main.py" if opponent.is_dir() else opponent
            if teacher_main.resolve() == opponent_main.resolve():
                continue
            if args.mode == "mirror":
                _, _, _, logs, _ = mirror_battle(str(teacher_main), str(opponent_main), n_games=args.games, save_log=True)
            else:
                _, _, _, logs = battle(str(teacher_main), str(opponent_main), n_games=args.games, save_log=True)
            pair = []
            for game_log in logs:
                pair.extend(
                    _extract(
                        game_log,
                        _label(teacher_main),
                        _label(opponent_main),
                        positive_scale=args.positive_scale,
                        negative_scale=args.negative_scale,
                        min_weight=args.min_weight,
                        max_weight=args.max_weight,
                        min_abs_delta=args.min_abs_delta,
                    )
                )
            key = f"{_label(teacher_main)} vs {_label(opponent_main)}"
            pair_counts[key] = len(pair)
            samples.extend(pair)
            print(f"{key}: {len(pair)} weighted samples", file=sys.stderr)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample, separators=(",", ":")) + "\n")
    counts = {name: 0 for name in LABELS}
    weighted = {name: 0.0 for name in LABELS}
    for sample in samples:
        label = int(sample["label"])
        counts[LABELS[label]] += 1
        weighted[LABELS[label]] += float(sample["weight"])
    summary = {
        "samples": len(samples),
        "pair_counts": pair_counts,
        "label_counts": counts,
        "weighted_label_counts": weighted,
        "contract": CONTRACT_VERSION,
    }
    args.output.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
