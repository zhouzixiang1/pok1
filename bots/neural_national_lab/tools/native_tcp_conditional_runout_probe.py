#!/usr/bin/env python3
"""Replicate one native TCP counterfactual under conditional board runouts.

All cards dealt before the selected decision remain identical to the source
row. Undealt cards in that hand are reshuffled per replicate, and the rule and
forced branches share the resulting deck. This estimates conditional action
risk without changing the national TCP bot contract or using an adapter.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[3]
WEB_CORE = ROOT / "web" / "core"
TOOLS = Path(__file__).resolve().parent
for path in (WEB_CORE, TOOLS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import national_native  # noqa: E402
try:  # national_native owned the game runtime before the shared-runtime split.
    import national_game_runtime  # type: ignore[import-not-found]  # noqa: E402
except ModuleNotFoundError as exc:  # Keep archived/older checkouts usable.
    if exc.name != "national_game_runtime":
        raise
    national_game_runtime = None
from native_tcp_counterfactual_probe import (  # noqa: E402
    _force_confirmed,
    _resolve,
    _run_pair,
    _settlement_map,
    _trace_rows,
)


def _runtime_deck_binding() -> tuple[Any, type]:
    """Return the module whose ``Deck`` name is resolved by the game runtime."""
    for owner in (national_game_runtime, national_native):
        if owner is None:
            continue
        deck_class = getattr(owner, "Deck", None)
        if isinstance(deck_class, type):
            return owner, deck_class
    raise RuntimeError("national TCP game runtime does not expose a Deck binding")


def _read_row(path: Path, index: int) -> dict[str, Any]:
    if index < 0:
        raise ValueError("row index must be non-negative")
    with path.open("r", encoding="utf-8") as handle:
        for row_index, line in enumerate(handle):
            if row_index == index:
                return json.loads(line)
    raise ValueError(f"row index {index} is outside {path}")


def _forced_action(row: dict[str, Any], label: str) -> int:
    matches = [
        probe for probe in row.get("probes") or []
        if probe.get("status") == "ok"
        and probe.get("force_confirmed") is True
        and str(probe.get("forced_label")) == label
    ]
    if len(matches) != 1:
        raise ValueError(
            f"source row has {len(matches)} valid alternatives for {label}"
        )
    return int(matches[0]["forced_action"])


def _known_card_count(row: dict[str, Any]) -> int:
    request = row.get("request") or {}
    public_cards = request.get("public_cards") or []
    count = 4 + len(public_cards)
    if count not in {4, 7, 8, 9}:
        raise ValueError(f"unsupported public-card prefix length: {count}")
    return count


def _conditional_deck_factory(
    deck_class: type,
    *,
    target_seed: int,
    known_cards: int,
    runout_seed: int,
) -> Callable[..., Any]:
    def factory(seed=None):
        deck = deck_class(seed=seed)
        if seed is None or int(seed) != int(target_seed):
            return deck
        prefix = list(deck.cards[:known_cards])
        suffix = list(deck.cards[known_cards:])
        random.Random(runout_seed).shuffle(suffix)
        deck.cards = prefix + suffix
        return deck

    return factory


def _decision_trace(
    result: dict[str, Any],
    *,
    hand: int,
    decision_index: int,
) -> dict[str, Any] | None:
    label = str(result.get("bot_a"))
    for decision in _trace_rows(result, label):
        if int(decision.get("hand", 0) or 0) != hand:
            continue
        if int(decision.get("hand_decision_index", -1) or 0) != decision_index:
            continue
        return decision
    return None


def _context_matches_source(
    decision: dict[str, Any] | None,
    source: dict[str, Any],
    *,
    rule_action: int,
) -> bool:
    if decision is None:
        return False
    return bool(
        decision.get("request") == source.get("request")
        and decision.get("state") == source.get("state")
        and int(decision.get("sanitized_action", 0) or 0) == rule_action
    )


async def _run_with_deck(
    candidate: Path,
    opponent: Path,
    *,
    hands: int,
    deck_seed_base: int,
    bot_seed_base: int,
    timeout_sec: float,
    deck_factory: Callable[..., Any],
    force: dict[str, int] | None,
) -> dict[str, Any]:
    deck_owner, original_deck = _runtime_deck_binding()
    deck_owner.Deck = deck_factory
    try:
        return await _run_pair(
            candidate,
            opponent,
            hands=hands,
            deck_seed_base=deck_seed_base,
            bot_seed_base=bot_seed_base,
            timeout_sec=timeout_sec,
            force=force,
        )
    finally:
        deck_owner.Deck = original_deck


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _bootstrap_ci(
    values: list[float], *, samples: int, seed: int
) -> dict[str, float]:
    if not values:
        return {"lower": 0.0, "mean": 0.0, "upper": 0.0}
    rng = random.Random(seed)
    means = [
        statistics.fmean(values[rng.randrange(len(values))] for _ in values)
        for _ in range(max(1, samples))
    ]
    return {
        "lower": _percentile(means, 0.025),
        "mean": statistics.fmean(values),
        "upper": _percentile(means, 0.975),
    }


def _metric_summary(
    values: list[float], *, bootstrap_samples: int, bootstrap_seed: int
) -> dict[str, Any]:
    return {
        "valid_replicates": len(values),
        "mean": statistics.fmean(values) if values else 0.0,
        "median": statistics.median(values) if values else 0.0,
        "stdev": statistics.pstdev(values) if values else 0.0,
        "p05": _percentile(values, 0.05),
        "p20": _percentile(values, 0.20),
        "positive_rate": (
            sum(value > 0 for value in values) / len(values) if values else 0.0
        ),
        "bootstrap_mean_ci": _bootstrap_ci(
            values, samples=bootstrap_samples, seed=bootstrap_seed
        ),
    }


def _long_horizon_deltas(
    *, hand_delta: float | None,
    baseline_match_value: float | None,
    forced_match_value: float | None,
) -> tuple[float | None, float | None]:
    if (
        hand_delta is None
        or baseline_match_value is None
        or forced_match_value is None
    ):
        return None, None
    match_delta = float(forced_match_value) - float(baseline_match_value)
    return match_delta, match_delta - float(hand_delta)


async def _collect(args: argparse.Namespace) -> dict[str, Any]:
    source = _read_row(args.source, args.row_index)
    candidate = _resolve(args.candidate)
    opponent = _resolve(args.opponent)
    hand = int(source["hand"])
    decision_index = int(source["hand_decision_index"])
    deck_seed_base = int(source["deck_seed_base"])
    bot_seed_base = int(source["bot_seed_base"])
    rule_action = int(source["rule_final"])
    forced_action = _forced_action(source, args.alternative_label)
    known_cards = _known_card_count(source)
    _, original_deck = _runtime_deck_binding()
    target_deck_seed = deck_seed_base + hand
    through_match = bool(getattr(args, "through_match", False))
    run_hands = max(
        hand,
        int((source.get("request") or {}).get("max_hand", hand) or hand),
    ) if through_match else hand
    fixed_rule_value = (
        float(source["rule_value"])
        if args.reuse_terminal_fold and rule_action == -1 else None
    )
    fixed_rule_match_value = None
    if through_match and fixed_rule_value is not None:
        raw_match_value = source.get("baseline_match_net_chips")
        if raw_match_value is not None:
            fixed_rule_match_value = float(raw_match_value)
    reuse_terminal_baseline = bool(
        fixed_rule_value is not None
        and (not through_match or fixed_rule_match_value is not None)
    )
    rows = []
    for replicate in range(args.replicates):
        runout_seed = int(args.runout_seed_base) + replicate
        deck_factory = _conditional_deck_factory(
            original_deck,
            target_seed=target_deck_seed,
            known_cards=known_cards,
            runout_seed=runout_seed,
        )
        baseline = None
        baseline_value = fixed_rule_value if reuse_terminal_baseline else None
        baseline_match_value = (
            fixed_rule_match_value if reuse_terminal_baseline else None
        )
        baseline_ok = reuse_terminal_baseline
        if baseline_value is None:
            baseline = await _run_with_deck(
                candidate,
                opponent,
                hands=run_hands,
                deck_seed_base=deck_seed_base,
                bot_seed_base=bot_seed_base,
                timeout_sec=args.timeout_sec,
                deck_factory=deck_factory,
                force=None,
            )
            baseline_decision = _decision_trace(
                baseline, hand=hand, decision_index=decision_index
            )
            baseline_settlement = _settlement_map(baseline, 0).get(hand)
            baseline_settlements = _settlement_map(baseline, 0)
            baseline_ok = bool(
                baseline.get("passed_compliance")
                and baseline_decision is not None
                and int(baseline_decision.get("final_action", 0) or 0)
                == rule_action
                and _context_matches_source(
                    baseline_decision, source, rule_action=rule_action
                )
                and baseline_settlement is not None
            )
            baseline_value = (
                float(baseline_settlement) if baseline_settlement is not None else None
            )
            if through_match and baseline_ok and len(baseline_settlements) == run_hands:
                baseline_match_value = float(sum(baseline_settlements.values()))

        forced = await _run_with_deck(
            candidate,
            opponent,
            hands=run_hands,
            deck_seed_base=deck_seed_base,
            bot_seed_base=bot_seed_base,
            timeout_sec=args.timeout_sec,
            deck_factory=deck_factory,
            force={
                "hand": hand,
                "decision": decision_index,
                "action": forced_action,
            },
        )
        forced_settlements = _settlement_map(forced, 0)
        forced_settlement = forced_settlements.get(hand)
        forced_decision = _decision_trace(
            forced, hand=hand, decision_index=decision_index
        )
        context_match = _context_matches_source(
            forced_decision, source, rule_action=rule_action
        )
        forced_ok = bool(
            forced.get("passed_compliance")
            and forced_settlement is not None
            and context_match
            and _force_confirmed(
                forced,
                str(forced.get("bot_a")),
                hand=hand,
                decision_index=decision_index,
                action=forced_action,
            )
        )
        delta = (
            float(forced_settlement) - float(baseline_value)
            if baseline_ok and forced_ok and baseline_value is not None else None
        )
        forced_match_value = None
        if through_match and forced_ok and len(forced_settlements) == run_hands:
            forced_match_value = float(sum(forced_settlements.values()))
        match_delta, tail_delta = _long_horizon_deltas(
            hand_delta=delta,
            baseline_match_value=baseline_match_value,
            forced_match_value=forced_match_value,
        )
        rows.append({
            "replicate": replicate,
            "runout_seed": runout_seed,
            "baseline_reused_terminal_fold": reuse_terminal_baseline,
            "baseline_value": baseline_value,
            "forced_value": forced_settlement,
            "delta_vs_rule": delta,
            "baseline_match_value": baseline_match_value,
            "forced_match_value": forced_match_value,
            "match_delta_vs_rule": match_delta,
            "tail_delta_vs_rule": tail_delta,
            "baseline_ok": baseline_ok,
            "forced_ok": forced_ok,
            "context_match": context_match,
            "forced_issues": list(forced.get("issues") or []),
            "baseline_issues": (
                list(baseline.get("issues") or []) if baseline is not None else []
            ),
        })

    deltas = [float(row["delta_vs_rule"]) for row in rows if row["delta_vs_rule"] is not None]
    match_deltas = [
        float(row["match_delta_vs_rule"])
        for row in rows if row["match_delta_vs_rule"] is not None
    ]
    tail_deltas = [
        float(row["tail_delta_vs_rule"])
        for row in rows if row["tail_delta_vs_rule"] is not None
    ]
    hand_summary = _metric_summary(
        deltas,
        bootstrap_samples=args.bootstrap_samples,
        bootstrap_seed=args.bootstrap_seed,
    )
    match_summary = _metric_summary(
        match_deltas,
        bootstrap_samples=args.bootstrap_samples,
        bootstrap_seed=args.bootstrap_seed + 1,
    )
    tail_summary = _metric_summary(
        tail_deltas,
        bootstrap_samples=args.bootstrap_samples,
        bootstrap_seed=args.bootstrap_seed + 2,
    )
    return {
        "format": "native_tcp_conditional_runout_v2",
        "execution_mode": "native_tcp",
        "adapter_used": False,
        "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source": {
            "path": str(args.source.resolve()),
            "sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
            "row_index": args.row_index,
            "opponent": source.get("opponent"),
            "hand": hand,
            "stage": source.get("stage"),
            "hand_decision_index": decision_index,
            "deck_seed_base": deck_seed_base,
            "bot_seed_base": bot_seed_base,
            "rule_action": rule_action,
            "forced_action": forced_action,
            "forced_label": args.alternative_label,
            "known_card_count": known_cards,
            "run_hands": run_hands,
            "through_match": through_match,
        },
        "candidate": str(candidate),
        "opponent": str(opponent),
        "rows": rows,
        "summary": {
            "requested_replicates": args.replicates,
            "valid_replicates": len(deltas),
            "mean_delta": hand_summary["mean"],
            "median_delta": hand_summary["median"],
            "stdev_delta": hand_summary["stdev"],
            "p05_delta": hand_summary["p05"],
            "p20_delta": hand_summary["p20"],
            "positive_rate": hand_summary["positive_rate"],
            "catastrophe_threshold": args.catastrophe_threshold,
            "catastrophe_rate": (
                sum(value <= -args.catastrophe_threshold for value in deltas)
                / len(deltas) if deltas else 0.0
            ),
            "bootstrap_mean_ci": hand_summary["bootstrap_mean_ci"],
            "through_match": through_match,
            "metrics": {
                "hand_delta_vs_rule": hand_summary,
                "tail_delta_vs_rule": tail_summary if through_match else None,
                "match_delta_vs_rule": match_summary if through_match else None,
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--opponent", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--row-index", required=True, type=int)
    parser.add_argument("--alternative-label", required=True)
    parser.add_argument("--replicates", type=int, default=16)
    parser.add_argument("--runout-seed-base", type=int, default=2026071100)
    parser.add_argument("--catastrophe-threshold", type=float, default=5000.0)
    parser.add_argument("--bootstrap-samples", type=int, default=5000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260711)
    parser.add_argument("--timeout-sec", type=float, default=120.0)
    parser.add_argument("--reuse-terminal-fold", action="store_true")
    parser.add_argument(
        "--through-match",
        action="store_true",
        help="Continue both branches through max_hand and report tail/match deltas.",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.replicates <= 0 or args.bootstrap_samples <= 0:
        raise SystemExit("replicates and bootstrap samples must be positive")
    if args.catastrophe_threshold <= 0 or args.timeout_sec <= 0:
        raise SystemExit("thresholds and timeout must be positive")
    try:
        payload = asyncio.run(_collect(args))
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"invalid conditional-runout request: {exc}") from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(payload["summary"], sort_keys=True))
    complete = payload["summary"]["valid_replicates"] == args.replicates
    if args.through_match:
        match_summary = payload["summary"]["metrics"]["match_delta_vs_rule"]
        complete = bool(
            complete
            and match_summary
            and match_summary["valid_replicates"] == args.replicates
        )
    return 0 if complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
