#!/usr/bin/env python3
"""Trace neural-advisor interventions and attach them to hand outcomes.

This is a diagnostic tool for advisor-style neural experiments. It runs a
candidate bot as bot0 against a fixed opponent on common normal/mirrored decks,
then records every decision where the neural advisor changes the rule action.
Each change is joined with that hand's chip delta so we can find leaks by
street, price, or change type instead of tuning thresholds blindly.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import random
import statistics
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
ENGINE = ROOT / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from analyze_advice import _classify_change, _load_bot, _neural_probs  # noqa: E402
from engine.battle import _PersistentBot, _call_bot  # noqa: E402
from judge import judge as judge_func  # noqa: E402
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
            "log": log,
        }
    finally:
        for proc in persistent:
            proc.close()


def _response_from_log(row: dict[str, Any], player_id: int) -> int | None:
    raw = (row.get(str(player_id)) or {}).get("response")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _stage_name(public_cards: list[int]) -> str:
    n = len(public_cards)
    if n >= 5:
        return "river"
    if n == 4:
        return "turn"
    if n >= 3:
        return "flop"
    return "preflop"


def _final_label(action: int) -> str:
    if action == -1:
        return "fold"
    if action == -2:
        return "allin"
    if action == 0:
        return "call"
    return "raise"


def _hand_deltas(log: list[dict[str, Any]]) -> dict[int, float]:
    deltas: dict[int, float] = {}
    for row in log:
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
        if output.get("command") == "finish":
            hand = current_hand
        else:
            hand = max(0, current_hand - 1)
        try:
            deltas[hand] = float(temp_result[0].get("win_chips", 0.0))
        except (TypeError, ValueError, AttributeError):
            deltas[hand] = 0.0
    return deltas


def _analyze_log(
    version_dir: Path,
    log: list[dict[str, Any]],
    seat: int = 0,
    candidate_conf: float = 0.85,
) -> dict[str, Any]:
    main_mod, state_mod, strategy_mod, neural_mod, apply_neural_advice = _load_bot(version_dir)
    requests: list[dict[str, Any]] = []
    hand_deltas = _hand_deltas(log)
    changes: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "decisions": 0,
        "final_changed": 0,
        "counterfactual_candidates": 0,
        "change_types": {},
        "changed_hand_count": 0,
        "changed_hand_delta_sum": 0.0,
        "all_hand_delta_sum": sum(hand_deltas.values()),
    }

    for idx in range(0, max(0, len(log) - 1), 2):
        output = log[idx].get("output") if isinstance(log[idx], dict) else None
        response_row = log[idx + 1] if isinstance(log[idx + 1], dict) else {}
        if not isinstance(output, dict):
            continue
        req = (output.get("content") or {}).get(str(seat))
        if not isinstance(req, dict):
            continue
        req = dict(req)
        requests.append(req)
        if "remaining_hands" not in req and hasattr(state_mod, "infer_remaining_hands_from_requests"):
            req["remaining_hands"] = state_mod.infer_remaining_hands_from_requests(requests)
            requests[-1] = req

        with contextlib.redirect_stderr(io.StringIO()):
            rule_action = strategy_mod.get_action(req, list(requests))
            state = state_mod.reconstruct_state(req)
            base_final = main_mod.sanitize_action(rule_action, state, req["my_chips"])
            advised_raw = rule_action
            if apply_neural_advice is not None:
                advised_raw = apply_neural_advice(req, state, int(rule_action))
            advised_final = main_mod.sanitize_action(advised_raw, state, req["my_chips"])
        summary["decisions"] += 1
        probs, top_label, top_conf = _neural_probs(neural_mod, req, state)
        public_cards = list(req.get("public_cards") or [])
        hand = int(req.get("hand", -1))
        top_name = (
            neural_mod.LABELS[top_label]
            if neural_mod is not None and top_label is not None
            else None
        )
        if (
            top_name is not None
            and float(top_conf) >= candidate_conf
            and top_name != _final_label(int(base_final))
        ):
            summary["counterfactual_candidates"] += 1
            candidates.append(
                {
                    "decision_index": summary["decisions"] - 1,
                    "hand": hand,
                    "hand_delta": float(hand_deltas.get(hand, 0.0)),
                    "stage": _stage_name(public_cards),
                    "public_cards": public_cards,
                    "my_cards": list(req.get("my_cards") or []),
                    "to_call": state.get("to_call"),
                    "pot": state.get("pot"),
                    "my_chips": req.get("my_chips"),
                    "rule_action": int(rule_action),
                    "base_final": int(base_final),
                    "rule_label": _final_label(int(base_final)),
                    "top_label": top_name,
                    "top_conf": float(top_conf),
                    "call_conf": float(probs[1]) if probs is not None and len(probs) > 1 else None,
                }
            )

        if int(base_final) == int(advised_final):
            continue

        kind = _classify_change(int(base_final), int(advised_final))
        summary["final_changed"] += 1
        summary["change_types"][kind] = summary["change_types"].get(kind, 0) + 1
        changes.append(
            {
                "decision_index": summary["decisions"] - 1,
                "hand": hand,
                "hand_delta": float(hand_deltas.get(hand, 0.0)),
                "stage": _stage_name(public_cards),
                "public_cards": public_cards,
                "my_cards": list(req.get("my_cards") or []),
                "to_call": state.get("to_call"),
                "pot": state.get("pot"),
                "my_chips": req.get("my_chips"),
                "rule_action": int(rule_action),
                "base_final": int(base_final),
                "advised_raw": int(advised_raw),
                "advised_final": int(advised_final),
                "actual": _response_from_log(response_row, seat),
                "kind": kind,
                "top_label": top_name,
                "top_conf": float(top_conf),
                "call_conf": float(probs[1]) if probs is not None and len(probs) > 1 else None,
            }
        )

    changed_hands = {row["hand"] for row in changes}
    summary["changed_hand_count"] = len(changed_hands)
    summary["changed_hand_delta_sum"] = sum(float(hand_deltas.get(hand, 0.0)) for hand in changed_hands)
    return {
        "summary": summary,
        "hand_deltas": hand_deltas,
        "changes": changes,
        "candidates": candidates,
    }


def _group_changes(changes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = {}
    for row in changes:
        key = f"{row.get('kind')}|{row.get('stage')}|to_call={row.get('to_call')}"
        grouped.setdefault(key, []).append(float(row.get("hand_delta", 0.0)))
    out: dict[str, dict[str, Any]] = {}
    for key, values in grouped.items():
        out[key] = {
            "n": len(values),
            "mean_hand_delta": statistics.mean(values) if values else 0.0,
            "sum_hand_delta": sum(values),
            "min_hand_delta": min(values) if values else 0.0,
            "max_hand_delta": max(values) if values else 0.0,
        }
    return dict(sorted(out.items(), key=lambda item: (item[1]["sum_hand_delta"], item[0])))


def _group_candidates(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = {}
    for row in candidates:
        key = (
            f"{row.get('rule_label')}->{row.get('top_label')}|"
            f"{row.get('stage')}|to_call={row.get('to_call')}"
        )
        grouped.setdefault(key, []).append(float(row.get("hand_delta", 0.0)))
    out: dict[str, dict[str, Any]] = {}
    for key, values in grouped.items():
        out[key] = {
            "n": len(values),
            "mean_hand_delta": statistics.mean(values) if values else 0.0,
            "sum_hand_delta": sum(values),
            "min_hand_delta": min(values) if values else 0.0,
            "max_hand_delta": max(values) if values else 0.0,
        }
    return dict(sorted(out.items(), key=lambda item: (item[1]["sum_hand_delta"], item[0])))


def _write(output: Path | None, payload: dict[str, Any]) -> None:
    if output is None:
        return
    out = output if output.is_absolute() else ROOT / output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--opponent", required=True)
    parser.add_argument("--games", type=int, default=4)
    parser.add_argument("--candidate-conf", type=float, default=0.85)
    parser.add_argument("--seed-base", type=int)
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument("--seed-stride", type=int, default=1)
    parser.add_argument("--bot-seed-base", type=int)
    parser.add_argument("--bot-seed-stride", type=int, default=10000)
    parser.add_argument("--max-hands", type=int, default=70)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    version_dir = _resolve(args.version)
    version_main = _main_path(version_dir)
    opponent = _main_path(_resolve(args.opponent))
    payload: dict[str, Any] = {
        "mode": "common_deck_advice_trace",
        "games": args.games,
        "seed_base": args.seed_base,
        "seed_offset": args.seed_offset,
        "seed_stride": args.seed_stride,
        "bot_seed_base": args.bot_seed_base,
        "bot_seed_stride": args.bot_seed_stride,
        "max_hands": args.max_hands,
        "version": _rel(version_main),
        "opponent": _rel(opponent),
        "pairs": [],
        "total": {
            "decisions": 0,
            "final_changed": 0,
            "counterfactual_candidates": 0,
            "changed_hand_delta_sum": 0.0,
            "all_hand_delta_sum": 0.0,
            "change_types": {},
        },
        "change_groups": {},
        "candidate_groups": {},
    }
    _write(args.output, payload)

    all_changes: list[dict[str, Any]] = []
    all_candidates: list[dict[str, Any]] = []
    for idx in range(args.games):
        seed = (
            args.seed_base + args.seed_offset + idx * args.seed_stride
            if args.seed_base is not None
            else None
        )
        initdata = _seeded_initdata(seed, args.max_hands) if seed is not None else _fresh_initdata()
        mirror = _mirror_initdata(initdata)
        normal_bot_seeds = match_bot_seeds(args.bot_seed_base, args.bot_seed_stride, idx, "normal")
        mirror_bot_seeds = match_bot_seeds(args.bot_seed_base, args.bot_seed_stride, idx, "mirror")
        row: dict[str, Any] = {
            "idx": idx,
            "seed": seed,
            "dealer": initdata["dealer"],
            "bot_seeds": {},
            "normal": {},
            "mirror": {},
        }
        if normal_bot_seeds is not None:
            row["bot_seeds"]["normal"] = list(normal_bot_seeds)
            row["bot_seeds"]["mirror"] = list(mirror_bot_seeds)
        for name, deck in (("normal", initdata), ("mirror", mirror)):
            bot_seeds = normal_bot_seeds if name == "normal" else mirror_bot_seeds
            match = _play_match(version_main, opponent, deck, bot_seeds)
            analysis = _analyze_log(version_dir, match["log"], seat=0, candidate_conf=args.candidate_conf)
            row[name] = {
                "winner": match["winner"],
                "bot0_chips": match["bot0_chips"],
                "bot1_chips": match["bot1_chips"],
                "summary": analysis["summary"],
                "changes": analysis["changes"],
                "candidates": analysis["candidates"],
            }
            all_changes.extend({**change, "pair": idx, "side": name} for change in analysis["changes"])
            all_candidates.extend(
                {**candidate, "pair": idx, "side": name}
                for candidate in analysis["candidates"]
            )
        payload["pairs"].append(row)

        total = payload["total"]
        for side in ("normal", "mirror"):
            summary = row[side]["summary"]
            total["decisions"] += int(summary["decisions"])
            total["final_changed"] += int(summary["final_changed"])
            total["counterfactual_candidates"] += int(summary["counterfactual_candidates"])
            total["changed_hand_delta_sum"] += float(summary["changed_hand_delta_sum"])
            total["all_hand_delta_sum"] += float(summary["all_hand_delta_sum"])
            for key, value in (summary.get("change_types") or {}).items():
                total["change_types"][key] = total["change_types"].get(key, 0) + int(value)
        payload["change_groups"] = _group_changes(all_changes)
        payload["candidate_groups"] = _group_candidates(all_candidates)
        _write(args.output, payload)
        print(
            f"pair {idx + 1}/{args.games}: changes={payload['total']['final_changed']} "
            f"candidates={payload['total']['counterfactual_candidates']} "
            f"changed_delta={payload['total']['changed_hand_delta_sum']:.1f} "
            f"all_delta={payload['total']['all_hand_delta_sum']:.1f}"
        )


if __name__ == "__main__":
    main()
