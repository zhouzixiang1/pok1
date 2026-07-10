#!/usr/bin/env python3
"""Collect native TCP single-decision counterfactual action-value rows.

The probe runs a trace-capable native bot once as the baseline, then replays
the same deck/bot seed while forcing one alternative final action at one
decision. Labels are hand-scoped deltas versus the baseline action on that
same hand. This stays inside the national TCP protocol and does not use the
legacy national adapter.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
WEB_CORE = ROOT / "web" / "core"
TOOLS = Path(__file__).resolve().parent
for path in (WEB_CORE, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cross_hand_sequence import (  # noqa: E402
    CROSS_HAND_SEQUENCE_SCHEMA,
    server_sequences_by_hand,
)
from feature_spec import LABELS, encode_features, label_action  # noqa: E402
from national_native import run_native_tcp_pair  # noqa: E402


OPPONENT_PROFILE_KEYS = (
    "confidence",
    "actions_total_norm",
    "fold_rate",
    "call_rate",
    "check_rate",
    "raise_rate",
    "allin_rate",
    "aggression",
    "preflop_actions_norm",
    "preflop_raise_rate",
    "postflop_actions_norm",
    "postflop_raise_rate",
)
OPPONENT_ACTION_LABELS = ("fold", "check", "call", "raise", "allin")


def _resolve(path: str) -> Path:
    raw = Path(path)
    return raw if raw.is_absolute() else (ROOT / raw).resolve()


async def _run_pair(
    candidate: Path,
    opponent: Path,
    *,
    hands: int,
    deck_seed_base: int | None,
    bot_seed_base: int | None,
    timeout_sec: float,
    force: dict[str, int | None] | None = None,
    capture_events: bool = False,
) -> dict[str, Any]:
    force = force or {}
    candidate_env = {
        "POK_TRACE_DECISIONS": "1",
        "POK_FORCE_HAND": force.get("hand"),
        "POK_FORCE_DECISION": force.get("decision"),
        "POK_FORCE_ACTION": force.get("action"),
    }
    clean_opponent_env = {
        "POK_TRACE_DECISIONS": None,
        "POK_FORCE_HAND": None,
        "POK_FORCE_DECISION": None,
        "POK_FORCE_ACTION": None,
    }
    return await run_native_tcp_pair(
        candidate,
        opponent,
        hands,
        require_native_a=True,
        require_native_b=True,
        deck_seed_base=deck_seed_base,
        bot_seed_base=bot_seed_base,
        timeout_sec=timeout_sec,
        bot_a_env_overrides=candidate_env,
        bot_b_env_overrides=clean_opponent_env,
        capture_events=capture_events,
    )


def _stage_name(row: dict[str, Any]) -> str:
    req = row.get("request") or {}
    n = len(req.get("public_cards") or [])
    if n >= 5:
        return "river"
    if n == 4:
        return "turn"
    if n >= 3:
        return "flop"
    return "preflop"


def _settlement_map(result: dict[str, Any], player_idx: int) -> dict[int, int]:
    out: dict[int, int] = {}
    for item in result.get("settlements", []):
        earnings = item.get("earnings")
        if not isinstance(earnings, list) or len(earnings) <= player_idx:
            continue
        out[int(item.get("hand", 0) or 0)] = int(earnings[player_idx])
    return out


def _trace_rows(result: dict[str, Any], label: str) -> list[dict[str, Any]]:
    pdata = result.get("per_player", {}).get(label, {})
    native = pdata.get("native", {}) if isinstance(pdata, dict) else {}
    rows = native.get("decision_trace", [])
    return [row for row in rows if isinstance(row, dict) and row.get("type") == "decision"]


def _label_id(action: int, req: dict[str, Any]) -> int:
    try:
        return int(label_action(int(action), req, None))
    except Exception:
        if action == -1:
            return 0
        if action == -2:
            return 5
        return 1 if action == 0 else 3


def _raise_total(req: dict[str, Any], state: dict[str, Any], ratio: float) -> int | None:
    my_bet = int(state.get("my_round_bet", req.get("my_stage_bet", 0)) or 0)
    to_call = max(0, int(state.get("to_call", 0) or 0))
    pot = max(1, int(state.get("pot", req.get("pot", 150)) or 150))
    min_raise_action = max(1, int(state.get("min_raise_action", state.get("round_raise", 100)) or 100))
    my_chips = max(0, int(req.get("my_chips", 0) or 0))
    delta = max(min_raise_action, int(to_call + (pot + to_call) * float(ratio)))
    if delta <= to_call or delta >= my_chips:
        return None
    return my_bet + delta


def _legal_alternatives(row: dict[str, Any]) -> list[int]:
    req = row.get("request") or {}
    state = row.get("state") or {}
    final = int(row.get("final_action", 0) or 0)
    to_call = max(0, int(state.get("to_call", req.get("to_call", 0)) or 0))
    opponent_allin = bool(state.get("opponent_allin") or req.get("opponent_allin"))
    actions: list[int] = []
    if to_call > 0 and final != -1:
        actions.append(-1)
    if final != 0:
        actions.append(0)
    if not opponent_allin:
        for ratio in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0):
            raise_action = _raise_total(req, state, ratio)
            if raise_action is not None and raise_action != final:
                actions.append(int(raise_action))
    if not opponent_allin and final != -2:
        actions.append(-2)
    final_label = _label_id(final, req)
    by_label: dict[int, int] = {}
    for action in actions:
        label = _label_id(action, req)
        if action == final or label == final_label:
            continue
        by_label.setdefault(label, action)
    return [by_label[label] for label in sorted(by_label)]


def _rotate_alternatives(
    alternatives: list[int], max_alternatives: int, rotation: int
) -> list[int]:
    limit = max(1, int(max_alternatives))
    if len(alternatives) <= limit:
        return alternatives
    count = len(alternatives)
    offset = int(rotation) % count
    stride = max(1, count // limit)
    selected: list[int] = []
    cursor = offset
    while len(selected) < limit:
        action = alternatives[cursor % count]
        if action not in selected:
            selected.append(action)
        cursor += stride
        if cursor % count == offset:
            cursor += 1
    return selected


def _filter_alternatives(
    alternatives: list[int],
    req: dict[str, Any],
    allowed_labels: set[int],
) -> list[int]:
    if not allowed_labels:
        return alternatives
    return [action for action in alternatives if _label_id(int(action), req) in allowed_labels]


def _passes_decision_filters(
    args: argparse.Namespace,
    *,
    stage: str,
    req: dict[str, Any],
    state: dict[str, Any],
    profile: dict[str, Any],
    rule_label: int,
) -> bool:
    if args.allowed_rule_label_ids and rule_label not in args.allowed_rule_label_ids:
        return False
    actions_total = int(profile.get("actions_total", 0) or 0)
    if actions_total < int(args.min_opponent_actions):
        return False
    if int(args.max_opponent_actions) > 0 and actions_total > int(args.max_opponent_actions):
        return False
    if args.max_opponent_raise_rate is not None:
        if _clip01(profile.get("raise_rate", 0.0)) > float(args.max_opponent_raise_rate):
            return False
    if args.initial_sb_only:
        if stage != "preflop":
            return False
        try:
            my_id = int(req.get("my_id", 0) or 0)
            dealer_id = int(req.get("dealer_id", 0) or 0)
        except (TypeError, ValueError):
            return False
        if my_id != dealer_id:
            return False
        if req.get("history"):
            return False
        to_call = float(state.get("to_call", req.get("to_call", 0.0)) or 0.0)
        if to_call > float(args.initial_sb_max_to_call):
            return False
    return True


def _empty_targets() -> list[float | None]:
    return [None for _ in LABELS]


def _target_mask(targets: list[float | None]) -> list[int]:
    return [1 if value is not None else 0 for value in targets]


def _clip01(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        x = float(default)
    if x != x:
        x = float(default)
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _opponent_profile_features(req: dict[str, Any]) -> list[float]:
    profile = req.get("opponent_profile") or {}
    if not isinstance(profile, dict):
        profile = {}
    return [_clip01(profile.get(key, 0.0)) for key in OPPONENT_PROFILE_KEYS]


def _row_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values: list[float] = []
    by_label: dict[str, list[float]] = defaultdict(list)
    statuses = Counter(str(row.get("status")) for row in rows)
    for row in rows:
        for idx, value in enumerate(row.get("delta_vs_rule", [])):
            if value is None:
                continue
            if idx == int(row.get("rule_label_id", -1)):
                continue
            values.append(float(value))
            by_label[LABELS[idx]].append(float(value))
    return {
        "rows": len(rows),
        "target_samples": len(values),
        "mean_delta": round(statistics.mean(values), 3) if values else 0.0,
        "median_delta": statistics.median(values) if values else 0.0,
        "positive": sum(1 for value in values if value > 0),
        "negative": sum(1 for value in values if value < 0),
        "zero": sum(1 for value in values if value == 0),
        "status_counts": dict(sorted(statuses.items())),
        "by_label": {
            label: {
                "samples": len(label_values),
                "mean_delta": round(statistics.mean(label_values), 3) if label_values else 0.0,
                "positive": sum(1 for value in label_values if value > 0),
                "negative": sum(1 for value in label_values if value < 0),
                "zero": sum(1 for value in label_values if value == 0),
            }
            for label, label_values in sorted(by_label.items())
        },
    }


def _force_confirmed(
    result: dict[str, Any], label: str, *, hand: int, decision_index: int, action: int
) -> bool:
    for row in _trace_rows(result, label):
        if int(row.get("hand", 0) or 0) != hand:
            continue
        if int(row.get("hand_decision_index", -1) or 0) != decision_index:
            continue
        return bool(row.get("forced")) and int(row.get("final_action", 0) or 0) == action
    return False


def _sample_decisions(
    eligible: list[dict[str, Any]], limit: int, sampling: str
) -> list[dict[str, Any]]:
    limit = max(1, int(limit))
    if len(eligible) <= limit:
        return eligible
    if sampling == "first":
        return eligible[:limit]
    n = len(eligible)
    indices = [min(n - 1, ((2 * idx + 1) * n) // (2 * limit)) for idx in range(limit)]
    return [eligible[idx] for idx in indices]


def _update_best(values: list[float | None], label_id: int, value: float) -> bool:
    current = values[label_id]
    if current is None or float(value) > float(current):
        values[label_id] = float(value)
        return True
    return False


def _event_matches_decision(event: dict[str, Any], decision: dict[str, Any]) -> bool:
    action = str(event.get("action", ""))
    final_action = int(decision.get("final_action", 0) or 0)
    if final_action == -1:
        return action == "fold"
    if final_action == -2:
        return action == "allin"
    if final_action == 0:
        return action in {"check", "call"}
    return action == "raise" and int(event.get("amount", 0) or 0) == final_action


def _behavior_response_rows(
    baseline: dict[str, Any], trace: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build leakage-free hero-action -> immediate opponent-response labels."""
    events = baseline.get("events") or []
    cross_hand_sequences = server_sequences_by_hand(
        events, opponent_player_idx=1
    )
    rows: list[dict[str, Any]] = []
    cursor = 0
    for decision in trace:
        hand = int(decision.get("hand", 0) or 0)
        stage = _stage_name(decision)
        action_event_index = None
        for idx in range(cursor, len(events)):
            event = events[idx]
            if event.get("type") != "action" or int(event.get("player_idx", -1)) != 0:
                continue
            if int(event.get("hand", 0) or 0) != hand or str(event.get("stage")) != stage:
                continue
            if _event_matches_decision(event, decision):
                action_event_index = idx
                cursor = idx + 1
                break
        if action_event_index is None:
            continue
        response = None
        for event in events[action_event_index + 1:]:
            if int(event.get("hand", 0) or 0) != hand:
                break
            if event.get("type") in {"stage", "settle"}:
                break
            if event.get("type") == "action":
                if int(event.get("player_idx", -1)) == 1 and str(event.get("stage")) == stage:
                    response = event
                break
        if response is None:
            continue
        response_action = str(response.get("action", ""))
        if response_action not in OPPONENT_ACTION_LABELS:
            continue
        req = decision.get("request") or {}
        state = decision.get("state") or {}
        profile = req.get("opponent_profile") or {}
        if not isinstance(profile, dict):
            profile = {}
        final_action = int(decision.get("final_action", 0) or 0)
        amount = float(response.get("amount", 0) or 0)
        pot = max(1.0, float(state.get("pot", req.get("pot", 150)) or 150))
        features = [float(value) for value in encode_features(req, None)]
        rows.append({
            "status": "ok",
            "source": "baseline_native_action_response",
            "opponent": baseline["bot_b"],
            "deck_seed_base": baseline.get("deck_seed_base"),
            "bot_seed_base": baseline.get("bot_seed_base"),
            "hand": hand,
            "stage": stage,
            "hand_decision_index": int(decision.get("hand_decision_index", 0) or 0),
            "decision_serial": int(decision.get("decision_serial", 0) or 0),
            "hero_action": final_action,
            "hero_action_label_id": _label_id(final_action, req),
            "opponent_action": response_action,
            "opponent_action_label_id": OPPONENT_ACTION_LABELS.index(response_action),
            "opponent_action_amount": amount,
            "opponent_action_amount_norm": min(1.0, max(0.0, amount / 20000.0)),
            "opponent_action_pot_ratio": min(4.0, max(0.0, amount / pot)),
            "state_features": features,
            "opponent_profile_features": _opponent_profile_features(req),
            "cross_hand_sequence_schema": CROSS_HAND_SEQUENCE_SCHEMA,
            "cross_hand_sequence": cross_hand_sequences.get(hand, []),
            "request": req,
            "state": state,
        })
    return rows


async def _collect(args: argparse.Namespace) -> dict[str, Any]:
    candidate = _resolve(args.candidate)
    opponent = _resolve(args.opponent)
    baseline = await _run_pair(
        candidate,
        opponent,
        hands=int(args.hands),
        deck_seed_base=args.seed_base,
        bot_seed_base=args.bot_seed_base,
        timeout_sec=float(args.timeout_sec),
        capture_events=True,
    )
    candidate_label = baseline["bot_a"]
    trace = _trace_rows(baseline, candidate_label)
    settlements = _settlement_map(baseline, 0)
    cross_hand_sequences = server_sequences_by_hand(
        baseline.get("events") or [], opponent_player_idx=1
    )
    behavior_rows = (
        _behavior_response_rows(baseline, trace)
        if baseline.get("passed_compliance")
        else []
    )
    rows: list[dict[str, Any]] = []
    probe_details: list[dict[str, Any]] = []
    stage_filter = None if args.stage == "any" else str(args.stage)
    eligible: list[dict[str, Any]] = []

    for decision in trace if baseline.get("passed_compliance") else []:
        stage = _stage_name(decision)
        if stage_filter and stage != stage_filter:
            continue
        hand = int(decision.get("hand", 0) or 0)
        if hand < int(args.min_hand):
            continue
        if hand <= 0 or hand not in settlements:
            continue
        alternatives = _legal_alternatives(decision)
        req = decision.get("request") or {}
        profile = req.get("opponent_profile") or {}
        if not isinstance(profile, dict):
            profile = {}
        final_action = int(decision.get("final_action", 0) or 0)
        rule_label = _label_id(final_action, req)
        state = decision.get("state") or {}
        if not _passes_decision_filters(
            args,
            stage=stage,
            req=req,
            state=state,
            profile=profile,
            rule_label=rule_label,
        ):
            continue
        alternatives = _filter_alternatives(alternatives, req, args.allowed_alternative_label_ids)
        rotation = (
            int(args.seed_base or 0) * 131
            + hand * 17
            + int(decision.get("hand_decision_index", 0) or 0)
        )
        alternatives = _rotate_alternatives(
            alternatives, int(args.max_alternatives), rotation
        )
        if not alternatives:
            continue
        eligible.append({
            "decision": decision,
            "stage": stage,
            "hand": hand,
            "request": req,
            "profile": profile,
            "final_action": final_action,
            "rule_label": rule_label,
            "state": state,
            "alternatives": alternatives,
            "alternative_rotation": rotation,
        })

    selected_rows = _sample_decisions(
        eligible, int(args.max_decisions), str(args.decision_sampling)
    )
    semaphore = asyncio.Semaphore(max(1, min(4, int(args.probe_workers))))

    async def run_forced(selected_index: int, alt: int):
        item = selected_rows[selected_index]
        decision = item["decision"]
        try:
            async with semaphore:
                forced = await _run_pair(
                    candidate,
                    opponent,
                    hands=int(args.hands),
                    deck_seed_base=args.seed_base,
                    bot_seed_base=args.bot_seed_base,
                    timeout_sec=float(args.timeout_sec),
                    force={
                        "hand": int(item["hand"]),
                        "decision": int(decision.get("hand_decision_index", 0) or 0),
                        "action": int(alt),
                    },
                )
            return selected_index, int(alt), forced, None
        except Exception as exc:
            return selected_index, int(alt), None, f"{type(exc).__name__}: {exc}"

    forced_results: dict[int, dict[int, tuple[dict[str, Any] | None, str | None]]] = defaultdict(dict)
    jobs = [
        run_forced(selected_index, int(alt))
        for selected_index, item in enumerate(selected_rows)
        for alt in item["alternatives"]
    ]
    if jobs:
        for selected_index, alt, forced, error in await asyncio.gather(*jobs):
            forced_results[selected_index][alt] = (forced, error)

    for selected_index, item in enumerate(selected_rows):
        decision = item["decision"]
        stage = item["stage"]
        hand = int(item["hand"])
        req = item["request"]
        profile = item["profile"]
        final_action = int(item["final_action"])
        rule_label = int(item["rule_label"])
        state = item["state"]
        alternatives = item["alternatives"]
        targets = _empty_targets()
        targets[rule_label] = 0.0
        match_targets = _empty_targets()
        match_targets[rule_label] = 0.0
        tail_targets = _empty_targets()
        tail_targets[rule_label] = 0.0
        action_values = _empty_targets()
        action_values[rule_label] = float(settlements[hand])
        baseline_match_value = int(baseline.get("net_chips_a", 0) or 0)
        match_action_values = _empty_targets()
        match_action_values[rule_label] = float(baseline_match_value)
        legal_mask = [0 for _ in LABELS]
        legal_mask[rule_label] = 1
        probe_records: list[dict[str, Any]] = []
        for alt in alternatives:
            alt_label = _label_id(int(alt), req)
            legal_mask[alt_label] = 1
            forced, run_error = forced_results[selected_index].get(
                int(alt), (None, "missing forced result")
            )
            forced_value = None
            forced_match_value = None
            delta = match_delta = tail_delta = None
            force_confirmed = False
            issues: list[Any] = []
            illegal_actions = None
            if forced is None:
                status = "run_error"
                issues = [run_error or "unknown forced-run error"]
            else:
                forced_label = forced["bot_a"]
                forced_settlements = _settlement_map(forced, 0)
                forced_value = forced_settlements.get(hand)
                forced_match_value = int(forced.get("net_chips_a", 0) or 0)
                force_confirmed = _force_confirmed(
                    forced,
                    forced_label,
                    hand=hand,
                    decision_index=int(decision.get("hand_decision_index", 0) or 0),
                    action=int(alt),
                )
                issues = list(forced.get("issues", []))
                illegal_actions = forced.get("per_player", {}).get(
                    forced_label, {}
                ).get("illegal_actions")
            if forced is None:
                pass
            elif forced_value is None:
                status = "missing_forced_settlement"
            elif not force_confirmed:
                status = "force_not_confirmed"
            elif not forced.get("passed_compliance"):
                status = "forced_issues"
            else:
                delta = int(forced_value) - int(settlements[hand])
                match_delta = int(forced_match_value) - baseline_match_value
                tail_delta = match_delta - delta
                status = "ok"
                if _update_best(targets, alt_label, float(delta)):
                    action_values[alt_label] = float(forced_value)
                if _update_best(match_targets, alt_label, float(match_delta)):
                    match_action_values[alt_label] = float(forced_match_value)
                _update_best(tail_targets, alt_label, float(tail_delta))
            probe_records.append({
                "forced_action": int(alt),
                "forced_label": LABELS[alt_label],
                "forced_hand_earnings": forced_value,
                "delta_vs_rule": delta,
                "match_delta_vs_rule": match_delta,
                "tail_delta_vs_rule": tail_delta,
                "forced_match_net_chips": forced_match_value,
                "force_confirmed": force_confirmed,
                "status": status,
                "issues": issues,
                "illegal_actions": illegal_actions,
            })
        if sum(_target_mask(targets)) <= 1:
            continue
        features = [float(value) for value in encode_features(req, None)]
        opponent_profile_features = _opponent_profile_features(req)
        native_context_features = features + opponent_profile_features
        train_targets = [0.0 if value is None else float(value) for value in targets]
        row = {
            "status": "ok" if all(record["status"] == "ok" for record in probe_records) else "partial",
            "opponent": baseline["bot_b"],
            "deck_seed_base": args.seed_base,
            "bot_seed_base": args.bot_seed_base,
            "hand": hand,
            "stage": stage,
            "hand_decision_index": int(decision.get("hand_decision_index", 0) or 0),
            "decision_serial": int(decision.get("decision_serial", 0) or 0),
            "rule_final": final_action,
            "rule_label_id": rule_label,
            "rule_label": LABELS[rule_label],
            "rule_value": int(settlements[hand]),
            "state_features": features,
            "opponent_profile_features": opponent_profile_features,
            "cross_hand_sequence_schema": CROSS_HAND_SEQUENCE_SCHEMA,
            "cross_hand_sequence": cross_hand_sequences.get(hand, []),
            "native_context_features": native_context_features,
            "opponent_profile": profile,
            "delta_vs_rule": targets,
            "match_delta_vs_rule": match_targets,
            "tail_delta_vs_rule": tail_targets,
            "action_values": action_values,
            "match_action_values": match_action_values,
            "target_mask": _target_mask(targets),
            "target_masks": {
                "delta_vs_rule": _target_mask(targets),
                "match_delta_vs_rule": _target_mask(match_targets),
                "tail_delta_vs_rule": _target_mask(tail_targets),
            },
            "legal_mask": legal_mask,
            "features": features,
            "targets": train_targets,
            "raw_targets": train_targets,
            "feature_set": "state",
            "target": "delta_vs_rule",
            "available_targets": ["delta_vs_rule", "match_delta_vs_rule", "tail_delta_vs_rule"],
            "weight": max(0.05, min(5.0, max(abs(value) for value in train_targets) / 1000.0)),
            "request": req,
            "state": state,
            "probes": probe_records,
            "valid_probe_count": sum(record["status"] == "ok" for record in probe_records),
            "invalid_probe_count": sum(record["status"] != "ok" for record in probe_records),
            "baseline_match_net_chips": baseline_match_value,
            "alternative_rotation": int(item["alternative_rotation"]),
        }
        rows.append(row)
        probe_details.extend(probe_records)

    return {
        "execution_mode": "native_tcp_counterfactual",
        "candidate_path": str(candidate),
        "opponent_path": str(opponent),
        "hands": int(args.hands),
        "deck_seed_base": args.seed_base,
        "bot_seed_base": args.bot_seed_base,
        "filters": {
            "stage": args.stage,
            "min_hand": int(args.min_hand),
            "min_opponent_actions": int(args.min_opponent_actions),
            "max_opponent_actions": int(args.max_opponent_actions),
            "max_opponent_raise_rate": args.max_opponent_raise_rate,
            "rule_labels": list(args.rule_label),
            "alternative_labels": list(args.alternative_label),
            "initial_sb_only": bool(args.initial_sb_only),
            "initial_sb_max_to_call": float(args.initial_sb_max_to_call),
        },
        "trace_decisions": len(trace),
        "eligible_decisions": len(eligible),
        "selected_decisions": len(selected_rows),
        "decision_sampling": str(args.decision_sampling),
        "probe_workers": max(1, min(4, int(args.probe_workers))),
        "baseline_net_chips": baseline.get("net_chips_a"),
        "baseline_passed_compliance": baseline.get("passed_compliance"),
        "baseline_issues": baseline.get("issues", []),
        "behavior_rows": behavior_rows,
        "behavior_summary": {
            "rows": len(behavior_rows),
            "by_action": dict(sorted(Counter(row["opponent_action"] for row in behavior_rows).items())),
        },
        "rows": rows,
        "summary": _row_summary(rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Native TCP single-decision counterfactual probe.")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--opponent", required=True)
    parser.add_argument("--hands", type=int, default=10)
    parser.add_argument("--seed-base", type=int, default=None)
    parser.add_argument("--bot-seed-base", type=int, default=None)
    parser.add_argument("--stage", choices=["any", "preflop", "flop", "turn", "river"], default="any")
    parser.add_argument("--min-hand", type=int, default=1)
    parser.add_argument("--min-opponent-actions", type=int, default=0)
    parser.add_argument("--max-opponent-actions", type=int, default=0)
    parser.add_argument("--max-opponent-raise-rate", type=float, default=None)
    parser.add_argument("--initial-sb-only", action="store_true")
    parser.add_argument("--initial-sb-max-to-call", type=float, default=60.0)
    parser.add_argument("--max-decisions", type=int, default=8)
    parser.add_argument("--max-alternatives", type=int, default=2)
    parser.add_argument(
        "--probe-workers",
        type=int,
        default=1,
        help="Concurrent forced native matches within this probe (maximum 4).",
    )
    parser.add_argument(
        "--decision-sampling",
        choices=("first", "uniform"),
        default="uniform",
        help="Select the first eligible decisions or spread them across the hand window.",
    )
    parser.add_argument(
        "--rule-label",
        action="append",
        default=[],
        choices=list(LABELS),
        help="Limit baseline decisions by their final rule label. Repeatable; default accepts all labels.",
    )
    parser.add_argument(
        "--alternative-label",
        action="append",
        default=[],
        choices=list(LABELS),
        help="Limit forced alternatives to these labels. Repeatable; default probes all generated alternatives.",
    )
    parser.add_argument("--timeout-sec", type=float, default=90.0)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jsonl-output", type=Path)
    parser.add_argument("--behavior-jsonl-output", type=Path)
    args = parser.parse_args()
    args.probe_workers = max(1, min(4, int(args.probe_workers)))
    args.allowed_rule_label_ids = {int(LABELS.index(label)) for label in args.rule_label}
    args.allowed_alternative_label_ids = {int(LABELS.index(label)) for label in args.alternative_label}

    payload = asyncio.run(_collect(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.jsonl_output:
        args.jsonl_output.parent.mkdir(parents=True, exist_ok=True)
        args.jsonl_output.write_text(
            "\n".join(json.dumps(row, separators=(",", ":")) for row in payload["rows"]) + "\n",
            encoding="utf-8",
        )
    if args.behavior_jsonl_output:
        args.behavior_jsonl_output.parent.mkdir(parents=True, exist_ok=True)
        args.behavior_jsonl_output.write_text(
            "\n".join(
                json.dumps(row, separators=(",", ":"))
                for row in payload["behavior_rows"]
            ) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
