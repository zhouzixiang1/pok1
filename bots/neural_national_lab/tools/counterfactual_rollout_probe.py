#!/usr/bin/env python3
"""Probe single-decision counterfactual action value.

The previous advisor experiments joined neural interventions to whole-hand
outcomes. That is noisy: a later mistake or lucky showdown can dominate the
label. This tool instead forks the local judge log at one bot0 decision, forces
the rule action on one branch and the neural-advised action on another, then
continues both branches on the same deck prefix.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
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


def _resolve(path: str) -> Path:
    p = Path(path)
    return (ROOT / p).resolve() if not p.is_absolute() else p


def _main_path(path: Path) -> Path:
    return path / "main.py" if path.is_dir() else path


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


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


def _label_matches_kind(label_name: str | None, kind: str) -> bool:
    if kind == "any":
        return True
    if label_name is None:
        return False
    if kind == "to_raise":
        return label_name.startswith("raise")
    if kind == "to_call" or kind == "fold_to_call":
        return label_name == "call"
    if kind == "to_fold":
        return label_name == "fold"
    if kind == "to_allin":
        return label_name == "allin"
    return True


def _advisor_signal(neural_mod, req: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    probs, top_label, top_conf = _neural_probs(neural_mod, req, state)
    top_name = (
        neural_mod.LABELS[top_label]
        if neural_mod is not None and top_label is not None
        else None
    )
    signal: dict[str, Any] = {
        "probs": probs,
        "top_label": top_label,
        "top_name": top_name,
        "top_conf": float(top_conf),
        "call_conf": float(probs[1]) if probs is not None and len(probs) > 1 else None,
        "raise_conf": float(probs[2]) if probs is not None and len(probs) > 2 else None,
    }
    if neural_mod is None:
        return signal
    try:
        model = neural_mod._model()
        if model is None:
            return signal
        feature_req = dict(req)
        for key in ("pot", "to_call", "my_stage_bet", "opponent_stage_bet", "opponent_allin"):
            if key in state:
                feature_req[key] = state[key]
        raw_probs = neural_mod._predict(model, neural_mod.encode_features(feature_req, None))
        label, conf, masked_probs = neural_mod._masked_top(raw_probs, neural_mod._legal_mask(req, state))
        signal.update(
            {
                "probs": masked_probs,
                "top_label": label,
                "top_name": neural_mod.LABELS[label],
                "top_conf": float(conf),
                "call_conf": float(masked_probs[1]) if len(masked_probs) > 1 else None,
                "raise_conf": float(max(masked_probs[2:5])) if len(masked_probs) > 4 else None,
            }
        )
    except Exception:
        return signal
    return signal


def _cheap_candidate_possible(signal: dict[str, Any], stage: str, args: argparse.Namespace) -> bool:
    if args.stage != "any" and stage != args.stage:
        return False
    if float(signal.get("top_conf") or 0.0) < args.min_conf:
        return False
    return _label_matches_kind(signal.get("top_name"), args.kind)


def _final_bot0_chips(result: dict[str, Any]) -> float | None:
    if result.get("command") != "finish":
        return None
    final = result.get("display", {}).get("final_result", [])
    if not isinstance(final, list) or len(final) < 1:
        return None
    try:
        return float(final[0]["win_chips"])
    except (KeyError, TypeError, ValueError):
        return None


def _hand_delta(log: list[dict[str, Any]], hand: int) -> float | None:
    for row in reversed(log):
        output = row.get("output") if isinstance(row, dict) else None
        if not isinstance(output, dict):
            continue
        display = output.get("display") or {}
        temp_result = display.get("temp_result")
        matchdata = display.get("matchdata") or {}
        if not isinstance(temp_result, list) or len(temp_result) < 1:
            continue
        try:
            current_hand = int(matchdata.get("hand", 0))
        except (TypeError, ValueError):
            current_hand = 0
        ended_hand = current_hand if output.get("command") == "finish" else current_hand - 1
        if ended_hand != hand:
            continue
        try:
            return float(temp_result[0].get("win_chips", 0.0))
        except (TypeError, ValueError, AttributeError):
            return None
    return None


def _write(output: Path | None, payload: dict[str, Any]) -> None:
    if output is None:
        return
    out = output if output.is_absolute() else ROOT / output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _stats(values: list[float]) -> dict[str, Any]:
    n = len(values)
    mean = statistics.mean(values) if values else 0.0
    median = statistics.median(values) if values else 0.0
    stddev = statistics.stdev(values) if n >= 2 else 0.0
    stderr = stddev / (n ** 0.5) if n >= 2 else 0.0
    margin = 1.96 * stderr if n >= 2 else 0.0
    return {
        "samples": n,
        "mean": mean,
        "median": median,
        "stddev": stddev,
        "stderr": stderr,
        "ci95_low": mean - margin if n >= 2 else None,
        "ci95_high": mean + margin if n >= 2 else None,
        "significant_positive_95": bool(n >= 2 and mean - margin > 0.0),
        "significant_negative_95": bool(n >= 2 and mean + margin < 0.0),
    }


def _pot_band(value: Any) -> str:
    try:
        pot = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if pot < 300:
        return "pot_lt_300"
    if pot < 700:
        return "pot_300_699"
    if pot < 1500:
        return "pot_700_1499"
    if pot < 3000:
        return "pot_1500_2999"
    return "pot_ge_3000"


def _summarize(probes: list[dict[str, Any]]) -> dict[str, Any]:
    deltas = [float(row["primary_delta"]) for row in probes if row.get("status") == "ok" and row.get("primary_delta") is not None]
    match_deltas = [float(row["match_delta"]) for row in probes if row.get("status") == "ok" and row.get("match_delta") is not None]
    hand_deltas = [float(row["hand_delta"]) for row in probes if row.get("status") == "ok" and row.get("hand_delta") is not None]
    by_kind: dict[str, list[float]] = {}
    by_stage: dict[str, list[float]] = {}
    by_stage_kind: dict[str, list[float]] = {}
    by_pot_band: dict[str, list[float]] = {}
    for row in probes:
        if row.get("status") != "ok":
            continue
        delta = float(row["primary_delta"])
        by_kind.setdefault(str(row.get("kind")), []).append(delta)
        by_stage.setdefault(str(row.get("stage")), []).append(delta)
        by_stage_kind.setdefault(f"{row.get('stage')}|{row.get('kind')}", []).append(delta)
        by_pot_band.setdefault(_pot_band(row.get("pot")), []).append(delta)
    return {
        "ok_probes": len(deltas),
        "failed_probes": len(probes) - len(deltas),
        "primary_delta": _stats(deltas),
        "match_delta": _stats(match_deltas),
        "hand_delta": _stats(hand_deltas),
        "by_kind": {key: _stats(values) for key, values in sorted(by_kind.items())},
        "by_stage": {key: _stats(values) for key, values in sorted(by_stage.items())},
        "by_stage_kind": {key: _stats(values) for key, values in sorted(by_stage_kind.items())},
        "by_pot_band": {key: _stats(values) for key, values in sorted(by_pot_band.items())},
    }


def _continue_after_response(
    log: list[dict[str, Any]],
    initdata: dict[str, Any],
    bot_paths: list[str],
    bot_requests: list[list[dict[str, Any]]],
    bot_responses: list[list[int]],
    bot_data: list[Any],
    max_steps: int,
    stop_after_hand: int | None = None,
) -> dict[str, Any]:
    try:
        result = json.loads(judge_func(json.dumps({"log": log, "initdata": initdata})))
        log.append({"output": result})
    except Exception as exc:
        return {"status": "judge_error", "error": str(exc), "log": log, "result": None}

    if stop_after_hand is not None and _hand_delta(log, stop_after_hand) is not None:
        return {"status": "ok", "log": log, "result": result}

    steps = 0
    while result.get("command") == "request":
        if steps >= max_steps:
            return {"status": "step_limit", "log": log, "result": result}
        content = result.get("content", {})
        if not content:
            return {"status": "empty_request", "log": log, "result": result}
        player_id = int(next(iter(content.keys())))
        request_data = content[str(player_id)]
        response, verdict, _ = _call_bot(
            bot_paths,
            player_id,
            request_data,
            bot_requests,
            bot_responses,
            bot_data=bot_data,
            persistent_procs=None,
        )
        log.append({str(player_id): {"response": str(response), "verdict": verdict}, "output": None})
        try:
            result = json.loads(judge_func(json.dumps({"log": log, "initdata": initdata})))
        except Exception as exc:
            return {"status": "judge_error", "error": str(exc), "log": log, "result": None}
        log.append({"output": result})
        steps += 1
        if stop_after_hand is not None and _hand_delta(log, stop_after_hand) is not None:
            return {"status": "ok", "log": log, "result": result}

    return {"status": "ok", "log": log, "result": result}


def _forced_branch(
    prefix_log: list[dict[str, Any]],
    initdata: dict[str, Any],
    bot_paths: list[str],
    bot_requests: list[list[dict[str, Any]]],
    bot_responses: list[list[int]],
    bot_data: list[Any],
    forced_action: int,
    max_steps: int,
    stop_after_hand: int | None,
) -> dict[str, Any]:
    log = copy.deepcopy(prefix_log)
    requests = copy.deepcopy(bot_requests)
    responses = copy.deepcopy(bot_responses)
    data = copy.deepcopy(bot_data)
    responses[0].append(int(forced_action))
    log.append({"0": {"response": str(int(forced_action)), "verdict": "OK"}, "output": None})
    branch = _continue_after_response(
        log,
        initdata,
        bot_paths,
        requests,
        responses,
        data,
        max_steps,
        stop_after_hand=stop_after_hand,
    )
    result = branch.get("result")
    branch["bot0_chips"] = _final_bot0_chips(result) if isinstance(result, dict) else None
    return branch


def _candidate_passes(kind: str, stage: str, args: argparse.Namespace) -> bool:
    if args.kind != "any" and kind != args.kind:
        return False
    if args.stage != "any" and stage != args.stage:
        return False
    return True


def _probe_side(
    side_name: str,
    initdata: dict[str, Any],
    version_dir: Path,
    bot0: Path,
    bot1: Path,
    loaded_bot: tuple[Any, Any, Any, Any, Any],
    args: argparse.Namespace,
    start_probe_index: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    main_mod, state_mod, strategy_mod, neural_mod, apply_neural_advice = loaded_bot
    bot_paths = [str(bot0.resolve()), str(bot1.resolve())]
    result = json.loads(judge_func(json.dumps({"log": [], "initdata": copy.deepcopy(initdata)})))
    game_initdata = copy.deepcopy(result["initdata"])
    log: list[dict[str, Any]] = [{"output": result}]
    bot_requests: list[list[dict[str, Any]]] = [[], []]
    bot_responses: list[list[int]] = [[], []]
    bot_data: list[Any] = [None, None]
    analysis_requests: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    decisions = 0
    persistent = [_PersistentBot(bot_paths[0]), _PersistentBot(bot_paths[1])] if args.scan_persistent else None

    try:
        while result.get("command") == "request":
            if len(probes) + start_probe_index >= args.max_probes:
                break
            if decisions >= args.max_scan_decisions:
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
                public_cards = list(req.get("public_cards") or [])
                stage = _stage_name(public_cards)
                signal = _advisor_signal(neural_mod, req, state)

                if _cheap_candidate_possible(signal, stage, args):
                    with contextlib.redirect_stderr(io.StringIO()):
                        rule_action = strategy_mod.get_action(req, list(analysis_requests))
                        base_final = main_mod.sanitize_action(rule_action, state, req["my_chips"])
                        advised_raw = rule_action
                        if apply_neural_advice is not None:
                            advised_raw = apply_neural_advice(req, state, int(rule_action))
                        advised_final = main_mod.sanitize_action(advised_raw, state, req["my_chips"])
                    kind = _classify_change(int(base_final), int(advised_final))

                    if (
                        len(probes) + start_probe_index < args.max_probes
                        and int(base_final) != int(advised_final)
                        and _candidate_passes(kind, stage, args)
                    ):
                        branch_requests = copy.deepcopy(bot_requests)
                        branch_requests[0].append(copy.deepcopy(request_data))
                        branch_responses = copy.deepcopy(bot_responses)
                        branch_data = copy.deepcopy(bot_data)
                        stop_after_hand = int(req.get("hand", -1)) if args.branch_scope == "hand" else None
                        baseline = _forced_branch(
                            log,
                            game_initdata,
                            bot_paths,
                            branch_requests,
                            branch_responses,
                            branch_data,
                            int(base_final),
                            args.max_branch_steps,
                            stop_after_hand,
                        )
                        candidate = _forced_branch(
                            log,
                            game_initdata,
                            bot_paths,
                            branch_requests,
                            branch_responses,
                            branch_data,
                            int(advised_final),
                            args.max_branch_steps,
                            stop_after_hand,
                        )
                        baseline_chips = baseline.get("bot0_chips")
                        candidate_chips = candidate.get("bot0_chips")
                        hand = int(req.get("hand", -1))
                        baseline_hand = _hand_delta(baseline.get("log", []), hand)
                        candidate_hand = _hand_delta(candidate.get("log", []), hand)
                        match_delta = (
                            float(candidate_chips) - float(baseline_chips)
                            if baseline_chips is not None and candidate_chips is not None
                            else None
                        )
                        hand_delta = (
                            float(candidate_hand) - float(baseline_hand)
                            if baseline_hand is not None and candidate_hand is not None
                            else None
                        )
                        if args.branch_scope == "hand":
                            status = "ok" if hand_delta is not None else "branch_failed"
                            primary_delta = hand_delta
                        else:
                            status = "ok" if match_delta is not None else "branch_failed"
                            primary_delta = match_delta
                        probe = {
                            "probe_index": start_probe_index + len(probes),
                            "side": side_name,
                            "decision_index": decisions,
                            "hand": hand,
                            "stage": stage,
                            "kind": kind,
                            "public_cards": public_cards,
                            "my_cards": list(req.get("my_cards") or []),
                            "to_call": state.get("to_call"),
                            "pot": state.get("pot"),
                            "my_chips": req.get("my_chips"),
                            "rule_action": int(rule_action),
                            "base_final": int(base_final),
                            "base_label": _final_label(int(base_final)),
                            "advised_raw": int(advised_raw),
                            "advised_final": int(advised_final),
                            "advised_label": _final_label(int(advised_final)),
                            "top_label": signal.get("top_name"),
                            "top_conf": float(signal.get("top_conf") or 0.0),
                            "call_conf": signal.get("call_conf"),
                            "raise_conf": signal.get("raise_conf"),
                            "status": status,
                            "baseline_status": baseline.get("status"),
                            "candidate_status": candidate.get("status"),
                            "baseline_chips": baseline_chips,
                            "candidate_chips": candidate_chips,
                            "match_delta": match_delta,
                            "baseline_hand_delta": baseline_hand,
                            "candidate_hand_delta": candidate_hand,
                            "hand_delta": hand_delta,
                            "primary_delta": primary_delta,
                        }
                        probes.append(probe)
                        print(
                            f"{side_name} probe {probe['probe_index']}: {kind} {stage} "
                            f"primary_delta={probe['primary_delta']} match_delta={probe['match_delta']} "
                            f"hand_delta={probe['hand_delta']}"
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

    final_chips = _final_bot0_chips(result)
    return probes, {
        "side": side_name,
        "decisions": decisions,
        "bot0_chips": final_chips,
        "version": _rel(version_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--opponent", required=True)
    parser.add_argument("--games", type=int, default=2)
    parser.add_argument("--max-probes", type=int, default=24)
    parser.add_argument("--max-scan-decisions", type=int, default=500)
    parser.add_argument("--min-conf", type=float, default=0.0)
    parser.add_argument("--kind", choices=["any", "to_raise", "to_call", "to_fold", "to_allin", "fold_to_call"], default="to_raise")
    parser.add_argument("--stage", choices=["any", "preflop", "flop", "turn", "river"], default="any")
    parser.add_argument("--branch-scope", choices=["hand", "match"], default="hand")
    parser.add_argument("--max-branch-steps", type=int, default=5000)
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
        "mode": "single_decision_counterfactual_rollout",
        "target": "forced_action_primary_delta",
        "notes": [
            "Branches force one bot0 action with verdict OK, then continue the same local judge match.",
            "hand scope stops after the forked hand settles and uses hand_delta as primary_delta.",
            "match scope continues the full local judge match and uses match_delta as primary_delta.",
        ],
        "version": _rel(bot0),
        "opponent": _rel(bot1),
        "games": args.games,
        "max_probes": args.max_probes,
        "max_scan_decisions": args.max_scan_decisions,
        "branch_scope": args.branch_scope,
        "scan_persistent": args.scan_persistent,
        "filters": {
            "kind": args.kind,
            "stage": args.stage,
            "min_conf": args.min_conf,
        },
        "matches": [],
        "probes": [],
        "summary": {},
    }
    _write(args.output, payload)

    for idx in range(args.games):
        initdata = _fresh_initdata()
        sides = [("normal", initdata)]
        if not args.no_mirror:
            sides.append(("mirror", _mirror_initdata(initdata)))
        for side_name, side_initdata in sides:
            if len(payload["probes"]) >= args.max_probes:
                break
            probes, match_summary = _probe_side(
                f"g{idx}:{side_name}",
                side_initdata,
                version_dir,
                bot0,
                bot1,
                loaded_bot,
                args,
                len(payload["probes"]),
            )
            payload["matches"].append({"game": idx, **match_summary})
            payload["probes"].extend(probes)
            payload["summary"] = _summarize(payload["probes"])
            _write(args.output, payload)
            print(
                f"game {idx + 1}/{args.games} {side_name}: probes={len(payload['probes'])} "
                f"mean={payload['summary'].get('primary_delta', {}).get('mean', 0.0):.1f}"
            )
        if len(payload["probes"]) >= args.max_probes:
            break

    payload["summary"] = _summarize(payload["probes"])
    _write(args.output, payload)
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
