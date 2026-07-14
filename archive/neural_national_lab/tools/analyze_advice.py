#!/usr/bin/env python3
"""Measure how often a neural advisor changes a version's rule action."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archive.botzone_local.engine.battle import battle  # noqa: E402


MODULE_PREFIXES = {
    "main",
    "strategy",
    "state",
    "neural_policy",
    "neural_features",
    "card_utils",
    "constants",
    "opponent",
    "simulation",
    "postflop",
    "tournament",
    "strategy_helpers",
    "line_reading",
    "overbet",
    "donk_probe",
    "passive_exploit",
    "reachability_test",
}


def _resolve(path: str) -> Path:
    p = Path(path)
    return (ROOT / p).resolve() if not p.is_absolute() else p


def _forget_bot_modules() -> None:
    for name in list(sys.modules):
        if name.split(".", 1)[0] in MODULE_PREFIXES:
            del sys.modules[name]


def _load_bot(version_dir: Path):
    _forget_bot_modules()
    sys.path.insert(0, str(version_dir))
    try:
        main_mod = importlib.import_module("main")
        state_mod = importlib.import_module("state")
        strategy_mod = importlib.import_module("strategy")
        try:
            neural_mod = importlib.import_module("neural_policy")
            apply_neural_advice = getattr(neural_mod, "apply_neural_advice")
        except Exception:
            neural_mod = None
            apply_neural_advice = None
    finally:
        try:
            sys.path.remove(str(version_dir))
        except ValueError:
            pass
    return main_mod, state_mod, strategy_mod, neural_mod, apply_neural_advice


def _response_from_log(row: dict[str, Any]) -> int | None:
    raw = (row.get("0") or {}).get("response")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _classify_change(base_final: int, advised_final: int) -> str:
    if base_final == advised_final:
        return "same"
    if advised_final == -1:
        return "to_fold"
    if advised_final == -2:
        return "to_allin"
    if advised_final == 0 and base_final == -1:
        return "fold_to_call"
    if advised_final == 0:
        return "to_call"
    if advised_final > 0:
        return "to_raise"
    return "other"


def _neural_probs(neural_mod, req: dict[str, Any], state: dict[str, Any]) -> tuple[list[float] | None, int | None, float]:
    if neural_mod is None:
        return None, None, 0.0
    try:
        feature_req = dict(req)
        for key in ("pot", "to_call", "my_stage_bet", "opponent_stage_bet", "opponent_allin"):
            if key in state:
                feature_req[key] = state[key]
        model = neural_mod._model()
        if model is None:
            return None, None, 0.0
        probs = neural_mod._predict(model, neural_mod.encode_features(feature_req, None))
        top_label = max(range(len(probs)), key=lambda i: probs[i])
        return probs, top_label, float(probs[top_label])
    except Exception:
        return None, None, 0.0


def _analyze_logs(version_dir: Path, logs: list[dict[str, Any]], max_examples: int) -> dict[str, Any]:
    main_mod, state_mod, strategy_mod, neural_mod, apply_neural_advice = _load_bot(version_dir)
    requests: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "decisions": 0,
        "raw_changed": 0,
        "final_changed": 0,
        "actual_mismatch": 0,
        "change_types": {},
        "top_labels": {},
        "rule_final": {},
        "rule_fold_top_call": 0,
        "rule_fold_top_call_conf_ge_070": 0,
        "rule_fold_top_call_conf_ge_080": 0,
        "rule_fold_top_call_conf_ge_090": 0,
        "max_call_conf_on_rule_fold": 0.0,
        "examples": [],
        "fold_call_candidates": [],
    }

    for idx in range(0, max(0, len(logs) - 1), 2):
        output = logs[idx].get("output") if isinstance(logs[idx], dict) else None
        response_row = logs[idx + 1] if isinstance(logs[idx + 1], dict) else {}
        if not isinstance(output, dict):
            continue
        req = (output.get("content") or {}).get("0")
        if not isinstance(req, dict):
            continue
        req = dict(req)
        requests.append(req)
        if "remaining_hands" not in req and hasattr(state_mod, "infer_remaining_hands_from_requests"):
            req["remaining_hands"] = state_mod.infer_remaining_hands_from_requests(requests)
            requests[-1] = req

        rule_action = strategy_mod.get_action(req, list(requests))
        state = state_mod.reconstruct_state(req)
        base_final = main_mod.sanitize_action(rule_action, state, req["my_chips"])
        summary["decisions"] += 1
        summary["rule_final"][str(int(base_final))] = summary["rule_final"].get(str(int(base_final)), 0) + 1

        probs, top_label, top_conf = _neural_probs(neural_mod, req, state)
        if probs is not None and top_label is not None:
            label_name = neural_mod.LABELS[top_label]
            summary["top_labels"][label_name] = summary["top_labels"].get(label_name, 0) + 1
            if int(base_final) == -1 and label_name == "call":
                call_conf = float(probs[1])
                summary["rule_fold_top_call"] += 1
                summary["max_call_conf_on_rule_fold"] = max(summary["max_call_conf_on_rule_fold"], call_conf)
                if call_conf >= 0.70:
                    summary["rule_fold_top_call_conf_ge_070"] += 1
                if call_conf >= 0.80:
                    summary["rule_fold_top_call_conf_ge_080"] += 1
                if call_conf >= 0.90:
                    summary["rule_fold_top_call_conf_ge_090"] += 1
                if len(summary["fold_call_candidates"]) < max_examples:
                    summary["fold_call_candidates"].append(
                        {
                            "decision_index": summary["decisions"] - 1,
                            "hand": req.get("hand"),
                            "to_call": state.get("to_call"),
                            "pot": state.get("pot"),
                            "call_conf": call_conf,
                            "top_conf": top_conf,
                        }
                    )

        advised_raw = rule_action
        if apply_neural_advice is not None:
            advised_raw = apply_neural_advice(req, state, int(rule_action))
        advised_final = main_mod.sanitize_action(advised_raw, state, req["my_chips"])
        actual = _response_from_log(response_row)

        if int(rule_action) != int(advised_raw):
            summary["raw_changed"] += 1
        if int(base_final) != int(advised_final):
            summary["final_changed"] += 1
            kind = _classify_change(int(base_final), int(advised_final))
            summary["change_types"][kind] = summary["change_types"].get(kind, 0) + 1
            if len(summary["examples"]) < max_examples:
                summary["examples"].append(
                    {
                        "decision_index": summary["decisions"] - 1,
                        "hand": req.get("hand"),
                        "public_cards": list(req.get("public_cards") or []),
                        "to_call": state.get("to_call"),
                        "pot": state.get("pot"),
                        "rule_action": int(rule_action),
                        "base_final": int(base_final),
                        "advised_raw": int(advised_raw),
                        "advised_final": int(advised_final),
                        "actual": actual,
                        "kind": kind,
                        "top_label": neural_mod.LABELS[top_label] if neural_mod is not None and top_label is not None else None,
                        "top_conf": top_conf,
                    }
                )
        if actual is not None and int(actual) != int(advised_final):
            summary["actual_mismatch"] += 1
    return summary


def _merge_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total: dict[str, Any] = {
        "decisions": 0,
        "raw_changed": 0,
        "final_changed": 0,
        "actual_mismatch": 0,
        "change_types": {},
        "top_labels": {},
        "rule_final": {},
        "rule_fold_top_call": 0,
        "rule_fold_top_call_conf_ge_070": 0,
        "rule_fold_top_call_conf_ge_080": 0,
        "rule_fold_top_call_conf_ge_090": 0,
        "max_call_conf_on_rule_fold": 0.0,
    }
    for row in rows:
        for key in (
            "decisions",
            "raw_changed",
            "final_changed",
            "actual_mismatch",
            "rule_fold_top_call",
            "rule_fold_top_call_conf_ge_070",
            "rule_fold_top_call_conf_ge_080",
            "rule_fold_top_call_conf_ge_090",
        ):
            total[key] += int(row.get(key, 0))
        total["max_call_conf_on_rule_fold"] = max(
            total["max_call_conf_on_rule_fold"], float(row.get("max_call_conf_on_rule_fold", 0.0))
        )
        for dict_key in ("change_types", "top_labels", "rule_final"):
            for key, value in (row.get(dict_key) or {}).items():
                total[dict_key][key] = total[dict_key].get(key, 0) + int(value)
    decisions = max(1, total["decisions"])
    total["raw_change_rate"] = total["raw_changed"] / decisions
    total["final_change_rate"] = total["final_changed"] / decisions
    return total


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
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--examples", type=int, default=12)
    args = parser.parse_args()

    version = _resolve(args.version)
    version_dir = version if version.is_dir() else version.parent
    version_main = version / "main.py" if version.is_dir() else version
    opponent = _resolve(args.opponent)
    opponent_main = opponent / "main.py" if opponent.is_dir() else opponent

    rows: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        "version": str(version_main.relative_to(ROOT)),
        "opponent": str(opponent_main.relative_to(ROOT)),
        "games": args.games,
        "rows": rows,
        "total": {},
    }
    _write(args.output, payload)

    for game_idx in range(args.games):
        wins, draws, played, all_logs = battle(str(version_main), str(opponent_main), n_games=1, save_log=True)
        for game_log in all_logs:
            row = _analyze_logs(version_dir, game_log.get("logs") or [], args.examples)
            row.update(
                {
                    "idx": game_idx,
                    "winner": game_log.get("winner"),
                    "bot0_chips": game_log.get("bot0_chips"),
                    "bot1_chips": game_log.get("bot1_chips"),
                    "wins": wins,
                    "draws": draws,
                    "played": played,
                }
            )
            rows.append(row)
        payload["total"] = _merge_totals(rows)
        _write(args.output, payload)
        print(
            f"{payload['version']} vs {payload['opponent']} game {game_idx + 1}/{args.games}: "
            f"final_changed={payload['total']['final_changed']}/"
            f"{payload['total']['decisions']} "
            f"types={payload['total']['change_types']} "
            f"top={payload['total']['top_labels']}"
        )


if __name__ == "__main__":
    main()
