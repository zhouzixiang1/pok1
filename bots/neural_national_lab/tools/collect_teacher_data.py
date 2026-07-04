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

from engine.battle import battle, mirror_battle  # noqa: E402
from blueprint_contract import CONTRACT_VERSION, legal_mask, state_from_request  # noqa: E402
from feature_spec import LABELS, encode_features, label_action  # noqa: E402


def _extract(game_log: dict[str, Any], teacher: str, opponent: str) -> list[dict]:
    rows = game_log.get("logs") or []
    samples = []
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
        except (TypeError, ValueError):
            continue
        display = out.get("display") or {}
        label = label_action(action, req, display)
        state = state_from_request(req, display)
        mask = legal_mask(req, state)
        if 0 <= label < len(mask):
            mask[label] = 1
        samples.append({
            "features": encode_features(req, display),
            "label": label,
            "legal_mask": mask,
            "weight": 1.0,
            "action": action,
            "meta": {
                "teacher": teacher,
                "opponent": opponent,
                "label_name": LABELS[label],
                "hand": req.get("hand"),
                "contract": CONTRACT_VERSION,
            },
        })
    return samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--teacher", required=True, type=Path)
    parser.add_argument("--opponent", action="append", required=True, type=Path)
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--mode", choices=["battle", "mirror"], default="battle")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    teacher = (ROOT / args.teacher).resolve() if not args.teacher.is_absolute() else args.teacher
    samples = []
    for opp_arg in args.opponent:
        opponent = (ROOT / opp_arg).resolve() if not opp_arg.is_absolute() else opp_arg
        if args.mode == "mirror":
            _, _, _, logs, _ = mirror_battle(str(teacher), str(opponent), n_games=args.games, save_log=True)
        else:
            _, _, _, logs = battle(str(teacher), str(opponent), n_games=args.games, save_log=True)
        pair = []
        for game_log in logs:
            pair.extend(_extract(game_log, teacher.parent.name, opponent.parent.name))
        samples.extend(pair)
        print(f"{teacher.parent.name} vs {opponent.parent.name}: {len(pair)} samples", file=sys.stderr)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample, separators=(",", ":")) + "\n")
    counts = {name: 0 for name in LABELS}
    for sample in samples:
        counts[LABELS[int(sample["label"])]] += 1
    print(f"wrote {len(samples)} samples to {args.output}", file=sys.stderr)
    print(f"label counts: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
