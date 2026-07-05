#!/usr/bin/env python3
"""Generate replayable multi-action counterfactual targets.

This is the vector-target companion to counterfactual_rollout_probe.py. At a
single bot0 decision point it enumerates the fixed abstract action labels,
forces each legal final action on the same judge prefix, and records action
values plus deltas against the rule action. The output is meant for offline
value/advantage training, not direct runtime use.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
ENGINE = ROOT / "engine"
for path in (ROOT, TOOLS, ENGINE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from analyze_advice import _load_bot  # noqa: E402
from blueprint_contract import action_from_label as _contract_action_from_label  # noqa: E402
from blueprint_contract import legal_mask as _contract_legal_mask  # noqa: E402
from counterfactual_rollout_probe import _advantage_features  # noqa: E402
from counterfactual_rollout_probe import _branch_bot_seeds, _decision_analysis_seed  # noqa: E402
from counterfactual_rollout_probe import _final_bot0_chips, _forced_branch, _fresh_initdata  # noqa: E402
from counterfactual_rollout_probe import _hand_delta, _main_path, _mirror_initdata, _rel  # noqa: E402
from counterfactual_rollout_probe import _resolve, _seed_analysis_rng, _seeded_initdata  # noqa: E402
from counterfactual_rollout_probe import _stage_name, _stats, _write  # noqa: E402
from engine.battle import _PersistentBot, _call_bot  # noqa: E402
from feature_spec import LABELS, encode_features, label_action  # noqa: E402
from judge import judge as judge_func  # noqa: E402
from seeded_process import SeededPersistentBot, match_bot_seeds  # noqa: E402


STATE_FEATURE_KEYS = ("pot", "to_call", "my_stage_bet", "opponent_stage_bet", "opponent_allin")


def _feature_request(req: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    feature_req = dict(req)
    for key in STATE_FEATURE_KEYS:
        if key in state:
            feature_req[key] = state[key]
    return feature_req


def _request_features(req: dict[str, Any], state: dict[str, Any]) -> list[float]:
    return [float(value) for value in encode_features(_feature_request(req, state), None)]


def _signal(neural_mod: Any, req: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if neural_mod is None:
        return {
            "raw_probs": None,
            "masked_probs": None,
            "top_label": None,
            "top_name": None,
            "top_conf": 0.0,
        }
    try:
        model = neural_mod._model()
        if model is None:
            return {
                "raw_probs": None,
                "masked_probs": None,
                "top_label": None,
                "top_name": None,
                "top_conf": 0.0,
            }
        feature_req = _feature_request(req, state)
        raw_probs = neural_mod._predict(model, neural_mod.encode_features(feature_req, None))
        label, conf, masked_probs = neural_mod._masked_top(raw_probs, neural_mod._legal_mask(req, state))
        return {
            "raw_probs": [float(value) for value in raw_probs],
            "masked_probs": [float(value) for value in masked_probs],
            "top_label": int(label),
            "top_name": str(neural_mod.LABELS[label]),
            "top_conf": float(conf),
        }
    except Exception:
        return {
            "raw_probs": None,
            "masked_probs": None,
            "top_label": None,
            "top_name": None,
            "top_conf": 0.0,
        }


def _legal_mask(neural_mod: Any, req: dict[str, Any], state: dict[str, Any]) -> list[int]:
    try:
        mask = neural_mod._legal_mask(req, state) if neural_mod is not None else None
    except Exception:
        mask = None
    if mask is None:
        mask = _contract_legal_mask(req, state)
    out = [1 if bool(value) else 0 for value in mask]
    if len(out) < len(LABELS):
        out.extend([0] * (len(LABELS) - len(out)))
    return out[: len(LABELS)]


def _candidate_action(neural_mod: Any, label: int, req: dict[str, Any], state: dict[str, Any]) -> int:
    try:
        if neural_mod is not None and hasattr(neural_mod, "_candidate_action"):
            return int(neural_mod._candidate_action(int(label), req, state))
    except Exception:
        pass
    return int(_contract_action_from_label(int(label), req, state))


def _sanitize(main_mod: Any, raw_action: int, req: dict[str, Any], state: dict[str, Any]) -> int:
    return int(main_mod.sanitize_action(int(raw_action), state, int(req.get("my_chips", 0) or 0)))


def _branch_value(
    branch: dict[str, Any],
    hand: int,
    branch_scope: str,
) -> float | None:
    if branch.get("status") != "ok":
        return None
    if branch_scope == "hand":
        return _hand_delta(branch.get("log", []), hand)
    result = branch.get("result")
    return _final_bot0_chips(result) if isinstance(result, dict) else None


def _action_menu(
    main_mod: Any,
    neural_mod: Any,
    req: dict[str, Any],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    mask = _legal_mask(neural_mod, req, state)
    menu: list[dict[str, Any]] = []
    for label, name in enumerate(LABELS):
        raw_action = _candidate_action(neural_mod, label, req, state)
        final_action = _sanitize(main_mod, raw_action, req, state)
        menu.append(
            {
                "label_id": label,
                "label": name,
                "legal": bool(mask[label]),
                "raw_action": int(raw_action),
                "final_action": int(final_action),
            }
        )
    return menu


def _evaluate_menu(
    menu: list[dict[str, Any]],
    prefix_log: list[dict[str, Any]],
    initdata: dict[str, Any],
    bot_paths: list[str],
    bot_requests: list[list[dict[str, Any]]],
    bot_responses: list[list[int]],
    bot_data: list[Any],
    request_data: dict[str, Any],
    hand: int,
    args: argparse.Namespace,
    row_index: int,
    baseline_final_actions: list[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    branch_requests = copy.deepcopy(bot_requests)
    branch_requests[0].append(copy.deepcopy(request_data))
    branch_responses = copy.deepcopy(bot_responses)
    branch_data = copy.deepcopy(bot_data)
    stop_after_hand = int(hand) if args.branch_scope == "hand" else None
    seeds = _branch_bot_seeds(args.bot_seed_base, row_index)

    by_action: dict[int, dict[str, Any]] = {}
    for item in menu:
        if not item["legal"]:
            continue
        final_action = int(item["final_action"])
        if final_action in by_action:
            continue
        branch = _forced_branch(
            prefix_log,
            initdata,
            bot_paths,
            branch_requests,
            branch_responses,
            branch_data,
            final_action,
            args.max_branch_steps,
            stop_after_hand,
            seeds,
        )
        by_action[final_action] = {
            "final_action": final_action,
            "status": branch.get("status"),
            "value": _branch_value(branch, hand, args.branch_scope),
            "bot0_chips": branch.get("bot0_chips"),
            "hand_delta": _hand_delta(branch.get("log", []), hand),
            "branch_bot_seeds": list(seeds) if seeds is not None else None,
        }

    for final_action in baseline_final_actions or []:
        final_action = int(final_action)
        if final_action in by_action:
            continue
        branch = _forced_branch(
            prefix_log,
            initdata,
            bot_paths,
            branch_requests,
            branch_responses,
            branch_data,
            final_action,
            args.max_branch_steps,
            stop_after_hand,
            seeds,
        )
        by_action[final_action] = {
            "final_action": final_action,
            "status": branch.get("status"),
            "value": _branch_value(branch, hand, args.branch_scope),
            "bot0_chips": branch.get("bot0_chips"),
            "hand_delta": _hand_delta(branch.get("log", []), hand),
            "branch_bot_seeds": list(seeds) if seeds is not None else None,
            "baseline_only": True,
        }

    evaluated: list[dict[str, Any]] = []
    for item in menu:
        row = dict(item)
        if not row["legal"]:
            row.update({"status": "illegal", "value": None, "delta_vs_rule": None, "regret_vs_mean": None})
        else:
            row.update(by_action.get(int(row["final_action"]), {"status": "missing", "value": None}))
        evaluated.append(row)
    return evaluated, by_action


def _complete_targets(menu: list[dict[str, Any]], rule_final: int, branch_values: dict[int, dict[str, Any]]) -> dict[str, Any]:
    legal_label_values = [
        float(item["value"])
        for item in menu
        if item.get("legal") and item.get("status") == "ok" and item.get("value") is not None
    ]
    unique_values_by_final: dict[int, float] = {}
    for item in menu:
        if not item.get("legal") or item.get("status") != "ok" or item.get("value") is None:
            continue
        unique_values_by_final.setdefault(int(item.get("final_action", 0)), float(item["value"]))
    unique_values = list(unique_values_by_final.values())
    mean_label_value = sum(legal_label_values) / len(legal_label_values) if legal_label_values else None
    mean_unique_value = sum(unique_values) / len(unique_values) if unique_values else None
    rule_value = None
    rule_branch = branch_values.get(int(rule_final))
    if rule_branch is not None and rule_branch.get("value") is not None:
        rule_value = float(rule_branch["value"])
    else:
        for item in menu:
            if item.get("legal") and int(item.get("final_action", 0)) == int(rule_final) and item.get("value") is not None:
                rule_value = float(item["value"])
                break

    action_values: list[float | None] = []
    delta_vs_rule: list[float | None] = []
    regret_vs_mean: list[float | None] = []
    for item in menu:
        value = item.get("value")
        value_f = float(value) if item.get("legal") and item.get("status") == "ok" and value is not None else None
        action_values.append(value_f)
        item["delta_vs_rule"] = value_f - rule_value if value_f is not None and rule_value is not None else None
        item["regret_vs_mean"] = value_f - mean_unique_value if value_f is not None and mean_unique_value is not None else None
        delta_vs_rule.append(item["delta_vs_rule"])
        regret_vs_mean.append(item["regret_vs_mean"])

    best_label = None
    if legal_label_values:
        candidates = [
            (idx, item)
            for idx, item in enumerate(menu)
            if item.get("legal") and item.get("status") == "ok" and item.get("value") is not None
        ]
        best_label = max(candidates, key=lambda pair: float(pair[1]["value"]))[0] if candidates else None
    return {
        "rule_value": rule_value,
        "mean_legal_value": mean_unique_value,
        "mean_unique_action_value": mean_unique_value,
        "mean_legal_label_value": mean_label_value,
        "best_label_id": best_label,
        "best_label": LABELS[best_label] if best_label is not None else None,
        "action_values": action_values,
        "delta_vs_rule": delta_vs_rule,
        "regret_vs_mean": regret_vs_mean,
    }


def _final_action_breakdown(menu: list[dict[str, Any]]) -> tuple[list[int], dict[str, int], dict[str, list[str]]]:
    counts: dict[str, int] = {}
    labels: dict[str, list[str]] = {}
    for item in menu:
        if not item.get("legal"):
            continue
        final_action = int(item.get("final_action", 0))
        key = str(final_action)
        counts[key] = counts.get(key, 0) + 1
        labels.setdefault(key, []).append(str(item.get("label")))
    return sorted(int(value) for value in counts), counts, labels


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_label_delta: dict[str, list[float]] = {name: [] for name in LABELS}
    by_label_regret: dict[str, list[float]] = {name: [] for name in LABELS}
    best_counts: dict[str, int] = {}
    unique_counts: list[int] = []
    evaluated_branch_counts: list[int] = []
    legal_label_counts: list[int] = []
    off_menu_rule_rows = 0
    ok_rows = 0
    for row in rows:
        if row.get("status") != "ok":
            continue
        ok_rows += 1
        unique_counts.append(int(row.get("unique_final_action_count", 0) or 0))
        evaluated_branch_counts.append(int(row.get("evaluated_branch_count", 0) or 0))
        legal_label_counts.append(sum(int(value) for value in row.get("legal_mask", [])))
        if not row.get("rule_final_in_menu", False):
            off_menu_rule_rows += 1
        best = row.get("best_label")
        if best:
            best_counts[str(best)] = best_counts.get(str(best), 0) + 1
        for idx, name in enumerate(LABELS):
            delta = row.get("delta_vs_rule", [None] * len(LABELS))[idx]
            regret = row.get("regret_vs_mean", [None] * len(LABELS))[idx]
            if delta is not None:
                by_label_delta[name].append(float(delta))
            if regret is not None:
                by_label_regret[name].append(float(regret))
    return {
        "rows": len(rows),
        "ok_rows": ok_rows,
        "failed_rows": len(rows) - ok_rows,
        "mean_unique_final_action_count": sum(unique_counts) / len(unique_counts) if unique_counts else 0.0,
        "mean_evaluated_branch_count": sum(evaluated_branch_counts) / len(evaluated_branch_counts)
        if evaluated_branch_counts
        else 0.0,
        "mean_legal_label_count": sum(legal_label_counts) / len(legal_label_counts) if legal_label_counts else 0.0,
        "off_menu_rule_rows": off_menu_rule_rows,
        "best_label_counts": dict(sorted(best_counts.items())),
        "delta_vs_rule_by_label": {
            label: _stats(values) for label, values in by_label_delta.items() if values
        },
        "regret_vs_mean_by_label": {
            label: _stats(values) for label, values in by_label_regret.items() if values
        },
    }


def _scan_side(
    side_name: str,
    initdata: dict[str, Any],
    deck_seed: int | None,
    version_dir: Path,
    bot0: Path,
    bot1: Path,
    loaded_bot: tuple[Any, Any, Any, Any, Any],
    args: argparse.Namespace,
    start_row_index: int,
    scan_bot_seeds: tuple[int, int] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    main_mod, state_mod, strategy_mod, neural_mod, _apply_neural_advice = loaded_bot
    bot_paths = [str(bot0.resolve()), str(bot1.resolve())]
    result = json.loads(judge_func(json.dumps({"log": [], "initdata": copy.deepcopy(initdata)})))
    game_initdata = copy.deepcopy(result["initdata"])
    log: list[dict[str, Any]] = [{"output": result}]
    bot_requests: list[list[dict[str, Any]]] = [[], []]
    bot_responses: list[list[int]] = [[], []]
    bot_data: list[Any] = [None, None]
    analysis_requests: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    decisions = 0
    persistent = None
    if scan_bot_seeds is not None:
        persistent = [
            SeededPersistentBot(bot_paths[0], scan_bot_seeds[0]),
            SeededPersistentBot(bot_paths[1], scan_bot_seeds[1]),
        ]
    elif args.scan_persistent:
        persistent = [_PersistentBot(bot_paths[0]), _PersistentBot(bot_paths[1])]

    try:
        while result.get("command") == "request":
            if len(rows) + start_row_index >= args.max_rows or decisions >= args.max_scan_decisions:
                break
            content = result.get("content", {})
            if not content:
                break
            player_id = int(next(iter(content.keys())))
            request_data = content[str(player_id)]

            if player_id == 0:
                req = dict(request_data)
                analysis_requests.append(req)
                if "remaining_hands" not in req and hasattr(state_mod, "infer_remaining_hands_from_requests"):
                    req["remaining_hands"] = state_mod.infer_remaining_hands_from_requests(analysis_requests)
                    analysis_requests[-1] = req
                with contextlib.redirect_stderr(io.StringIO()):
                    state = state_mod.reconstruct_state(req)
                stage = _stage_name(list(req.get("public_cards") or []))
                if args.stage == "any" or args.stage == stage:
                    analysis_seed = _decision_analysis_seed(scan_bot_seeds, deck_seed, decisions)
                    _seed_analysis_rng(analysis_seed)
                    with contextlib.redirect_stderr(io.StringIO()):
                        rule_action = int(strategy_mod.get_action(req, list(analysis_requests)))
                        rule_final = _sanitize(main_mod, rule_action, req, state)
                    signal = _signal(neural_mod, req, state)
                    menu = _action_menu(main_mod, neural_mod, req, state)
                    unique_legal = {int(item["final_action"]) for item in menu if item["legal"]}
                    if len(unique_legal) >= int(args.min_unique_actions):
                        row_index = start_row_index + len(rows)
                        evaluated, _by_action = _evaluate_menu(
                            menu,
                            log,
                            game_initdata,
                            bot_paths,
                            bot_requests,
                            bot_responses,
                            bot_data,
                            request_data,
                            int(req.get("hand", -1)),
                            args,
                            row_index,
                            [rule_final],
                        )
                        targets = _complete_targets(evaluated, rule_final, _by_action)
                        status = "ok" if targets["rule_value"] is not None else "missing_rule_branch"
                        unique_actions, final_action_counts, final_action_labels = _final_action_breakdown(evaluated)
                        rule_label_id = label_action(rule_final, _feature_request(req, state), None)
                        advantage_features = _advantage_features(
                            req,
                            state,
                            rule_action,
                            int(signal["top_label"]) if signal.get("top_label") is not None else None,
                            float(signal.get("top_conf") or 0.0),
                            signal.get("raw_probs"),
                        )
                        row = {
                            "row_index": row_index,
                            "side": side_name,
                            "decision_index": decisions,
                            "analysis_seed": analysis_seed,
                            "hand": int(req.get("hand", -1)),
                            "stage": stage,
                            "public_cards": list(req.get("public_cards") or []),
                            "my_cards": list(req.get("my_cards") or []),
                            "to_call": state.get("to_call"),
                            "pot": state.get("pot"),
                            "my_chips": req.get("my_chips"),
                            "rule_action": rule_action,
                            "rule_final": rule_final,
                            "rule_label_id": rule_label_id,
                            "rule_label": LABELS[rule_label_id],
                            "neural_top_label_id": signal.get("top_label"),
                            "neural_top_label": signal.get("top_name"),
                            "neural_top_conf": signal.get("top_conf"),
                            "neural_raw_probs": signal.get("raw_probs"),
                            "neural_masked_probs": signal.get("masked_probs"),
                            "state_features": _request_features(req, state),
                            "advantage_features": advantage_features,
                            "legal_mask": [1 if item["legal"] else 0 for item in evaluated],
                            "unique_final_actions": unique_actions,
                            "unique_final_action_count": len(unique_actions),
                            "evaluated_branch_count": len(_by_action),
                            "rule_final_in_menu": int(rule_final) in set(unique_actions),
                            "rule_branch": _by_action.get(int(rule_final)),
                            "final_action_counts": final_action_counts,
                            "final_action_labels": final_action_labels,
                            "actions": evaluated,
                            "status": status,
                            **targets,
                        }
                        rows.append(row)
                        print(
                            f"{side_name} row {row_index}: hand={row['hand']} {stage} "
                            f"rule={row['rule_final']} best={row['best_label']} "
                            f"rule_value={row['rule_value']}"
                        )

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
            if player_id == 0:
                decisions += 1
    finally:
        if persistent:
            for proc in persistent:
                proc.close()

    return rows, {
        "side": side_name,
        "decisions": decisions,
        "bot0_chips": _final_bot0_chips(result),
        "version": _rel(version_dir),
        "bot_seeds": list(scan_bot_seeds) if scan_bot_seeds is not None else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--opponent", required=True)
    parser.add_argument("--games", type=int, default=1)
    parser.add_argument("--max-rows", type=int, default=12)
    parser.add_argument("--max-scan-decisions", type=int, default=500)
    parser.add_argument("--stage", choices=["any", "preflop", "flop", "turn", "river"], default="any")
    parser.add_argument("--branch-scope", choices=["hand", "match"], default="hand")
    parser.add_argument("--max-branch-steps", type=int, default=5000)
    parser.add_argument("--min-unique-actions", type=int, default=2)
    parser.add_argument("--seed-base", type=int)
    parser.add_argument("--seed-offset", type=int, default=0)
    parser.add_argument("--seed-stride", type=int, default=1)
    parser.add_argument("--bot-seed-base", type=int)
    parser.add_argument("--bot-seed-stride", type=int, default=10000)
    parser.add_argument("--max-hands", type=int, default=70)
    parser.add_argument("--no-scan-persistent", action="store_true")
    parser.add_argument("--no-mirror", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.scan_persistent = not args.no_scan_persistent

    version_dir = _resolve(args.version)
    bot0 = _main_path(version_dir)
    bot1 = _main_path(_resolve(args.opponent))
    loaded_bot = _load_bot(version_dir)
    payload: dict[str, Any] = {
        "mode": "multi_action_counterfactual_probe_v1",
        "target": "legal_mask_action_values_delta_vs_rule_regret_vs_mean",
        "notes": [
            "Each row forks one bot0 decision and enumerates abstract action labels.",
            "Branches share the same judge prefix, deck seed, and branch bot RNG seed.",
            "delta_vs_rule uses the sanitized rule action as the baseline value.",
            "regret_vs_mean subtracts the mean value over successfully evaluated unique final actions.",
            "Duplicate label-to-final-action mappings are recorded in final_action_counts and share a branch value.",
            "If the rule baseline uses an off-menu raise size, it is evaluated as rule_branch but not added to the fixed action vector.",
        ],
        "version": _rel(bot0),
        "opponent": _rel(bot1),
        "labels": list(LABELS),
        "games": args.games,
        "max_rows": args.max_rows,
        "max_scan_decisions": args.max_scan_decisions,
        "seed_base": args.seed_base,
        "seed_offset": args.seed_offset,
        "seed_stride": args.seed_stride,
        "bot_seed_base": args.bot_seed_base,
        "bot_seed_stride": args.bot_seed_stride,
        "max_hands": args.max_hands,
        "branch_scope": args.branch_scope,
        "scan_persistent": args.scan_persistent,
        "filters": {
            "stage": args.stage,
            "min_unique_actions": args.min_unique_actions,
        },
        "matches": [],
        "rows": [],
        "summary": {},
    }
    _write(args.output, payload)

    for idx in range(args.games):
        seed = args.seed_base + args.seed_offset + idx * args.seed_stride if args.seed_base is not None else None
        initdata = _seeded_initdata(seed, args.max_hands) if seed is not None else _fresh_initdata()
        sides = [("normal", initdata)]
        if not args.no_mirror:
            sides.append(("mirror", _mirror_initdata(initdata)))
        for side_name, side_initdata in sides:
            if len(payload["rows"]) >= args.max_rows:
                break
            scan_bot_seeds = match_bot_seeds(args.bot_seed_base, args.bot_seed_stride, idx, side_name)
            rows, match_summary = _scan_side(
                f"g{idx}:{side_name}",
                side_initdata,
                seed,
                version_dir,
                bot0,
                bot1,
                loaded_bot,
                args,
                len(payload["rows"]),
                scan_bot_seeds,
            )
            remaining = args.max_rows - len(payload["rows"])
            payload["matches"].append({"game": idx, "seed": seed, **match_summary})
            payload["rows"].extend(rows[:remaining])
            payload["summary"] = _summarize(payload["rows"])
            _write(args.output, payload)
            print(f"game {idx + 1}/{args.games} {side_name}: rows={len(payload['rows'])}")
        if len(payload["rows"]) >= args.max_rows:
            break

    payload["summary"] = _summarize(payload["rows"])
    _write(args.output, payload)
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
