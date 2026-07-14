#!/usr/bin/env python3
"""Build solver-inspired labels for high-value poker bot tuning samples.

This script is intentionally offline and read-only for source match data.  It
does not call the reference projects directly; instead it turns their useful
ideas into compact action abstractions: check/call, half-pot, pot, and all-in.
The output is meant to justify small Botzone-safe rules in bot candidates.
"""

from __future__ import print_function

import argparse
import collections
import csv
import datetime
import itertools
import json
import os
import sys


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUTPUT_DIR = os.path.join(PROJECT_DIR, "temp", "bot20_analysis")
DEFAULT_SUMMARY = os.path.join(DEFAULT_OUTPUT_DIR, "summary.json")
DEFAULT_BOTZONE_RUN = os.path.join(PROJECT_DIR, "botzone_runs", "20260429_001114_bot20_v0")
DEFAULT_BOT20_ANCHOR = os.path.join(
    PROJECT_DIR, "ladder_results", "bot20_anchor_20260428_212727", "summary.json"
)
DEFAULT_BOT26_CORE = os.path.join(
    PROJECT_DIR, "temp", "bot20_analysis", "bot26_core_validation_50", "summary.json"
)

STAGE_ALIASES = {
    "preflop": "preflop",
    "pre_flop": "preflop",
    "pre-flop": "preflop",
    "preflop": "preflop",
    "flop": "flop",
    "turn": "turn",
    "river": "river",
}
STAGE_BY_ID = {"0": "preflop", "1": "flop", "2": "turn", "3": "river"}
HAND_CLASS_NAMES = [
    "high_card",
    "pair",
    "two_pair",
    "trips",
    "straight",
    "flush",
    "full_house",
    "quads",
    "straight_flush",
]


def now_text():
    return datetime.datetime.now().isoformat(timespec="seconds")


def ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)


def relpath(path):
    try:
        return os.path.relpath(path, PROJECT_DIR)
    except ValueError:
        return path


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    ensure_dir(os.path.dirname(path))
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


def write_text(path, text):
    ensure_dir(os.path.dirname(path))
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, path)


def read_csv(path):
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def safe_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def safe_float(value, default=0.0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_json_text(value, default=None):
    if default is None:
        default = {}
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def norm_stage(stage, stage_id=None):
    raw = str(stage or "").strip()
    if raw:
        key = raw.lower().replace(" ", "_")
        if key in STAGE_ALIASES:
            return STAGE_ALIASES[key]
    sid = str(stage_id or "").strip()
    return STAGE_BY_ID.get(sid, raw.lower() or "unknown")


def card_rank(card):
    return card // 4 + 2


def card_suit(card):
    return card % 4


def evaluate_5(cards):
    ranks = sorted([card_rank(c) for c in cards], reverse=True)
    suits = [card_suit(c) for c in cards]
    counts = {}
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
    groups = sorted(((count, rank) for rank, count in counts.items()), reverse=True)
    unique = sorted(set(ranks), reverse=True)

    is_flush = len(set(suits)) == 1
    # Judge-compatible: no A-2-3-4-5 wheel straight.
    is_straight = len(unique) == 5 and unique[0] - unique[4] == 4
    straight_high = unique[0] if is_straight else 0

    if is_flush and is_straight:
        return (8, straight_high)
    if groups[0][0] == 4:
        quad = groups[0][1]
        kicker = max(rank for rank in ranks if rank != quad)
        return (7, quad, kicker)
    if groups[0][0] == 3 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if is_flush:
        return (5,) + tuple(ranks)
    if is_straight:
        return (4, straight_high)
    if groups[0][0] == 3:
        trips = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != trips), reverse=True)
        return (3, trips) + tuple(kickers)
    if groups[0][0] == 2 and groups[1][0] == 2:
        high_pair = max(groups[0][1], groups[1][1])
        low_pair = min(groups[0][1], groups[1][1])
        kicker = max(rank for rank in ranks if rank not in (high_pair, low_pair))
        return (2, high_pair, low_pair, kicker)
    if groups[0][0] == 2:
        pair = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != pair), reverse=True)
        return (1, pair) + tuple(kickers)
    return (0,) + tuple(ranks)


def evaluate_best(cards):
    if len(cards) < 5:
        return (0,)
    best = None
    for combo in itertools.combinations(cards, 5):
        score = evaluate_5(combo)
        if best is None or score > best:
            best = score
    return best or (0,)


def hand_class_name(score):
    cls = score[0] if score else 0
    if cls < 0 or cls >= len(HAND_CLASS_NAMES):
        return "unknown"
    return HAND_CLASS_NAMES[cls]


def preflop_strength(cards):
    if len(cards) != 2:
        return 0.0
    r1, r2 = sorted([card_rank(cards[0]), card_rank(cards[1])], reverse=True)
    suited = card_suit(cards[0]) == card_suit(cards[1])
    gap = abs(r1 - r2)
    if r1 == r2:
        return min(0.92, 0.52 + (r1 - 2) * 0.035)
    score = 0.22 + (r1 + r2) / 32.0
    if suited:
        score += 0.045
    if gap == 1:
        score += 0.030
    elif gap == 2:
        score += 0.015
    elif gap >= 5:
        score -= 0.030
    if r1 == 14:
        score += 0.040
    return max(0.0, min(0.90, score))


def board_texture(public_cards):
    info = {
        "paired": False,
        "flush_pressure": 0.0,
        "straight_pressure": 0.0,
        "dynamic": False,
        "high_card": 0,
    }
    if len(public_cards) < 3:
        return info
    ranks = [card_rank(c) for c in public_cards]
    suits = [card_suit(c) for c in public_cards]
    info["paired"] = len(set(ranks)) < len(ranks)
    info["high_card"] = max(ranks)

    suit_counts = collections.Counter(suits)
    max_suit = max(suit_counts.values())
    if max_suit >= 4:
        info["flush_pressure"] = 1.0
    elif max_suit == 3:
        info["flush_pressure"] = 0.75
    elif max_suit == 2 and len(public_cards) >= 4:
        info["flush_pressure"] = 0.35

    expanded = set(ranks)
    best_straight = 0.0
    for start in range(2, 11):
        window = set(range(start, start + 5))
        present_cards = expanded & window
        present = len(present_cards)
        if present >= 4:
            best_straight = max(best_straight, 1.0)
        elif present == 3:
            best_straight = max(best_straight, 0.65)
        elif present == 2 and max(present_cards, default=start) - min(present_cards, default=start) <= 3:
            best_straight = max(best_straight, 0.28)
    info["straight_pressure"] = best_straight
    info["dynamic"] = info["flush_pressure"] >= 0.75 or info["straight_pressure"] >= 0.65
    return info


def pair_shape(hole_cards, public_cards, score):
    if not score or score[0] != 1:
        return "none"
    pair_rank = score[1]
    hole_ranks = [card_rank(c) for c in hole_cards]
    board_ranks = [card_rank(c) for c in public_cards]
    board_unique = sorted(set(board_ranks), reverse=True)
    if pair_rank not in hole_ranks:
        return "board_pair"
    if hole_ranks[0] == hole_ranks[1]:
        if board_ranks and pair_rank > max(board_ranks):
            return "overpair"
        return "underpair" if any(rank > pair_rank for rank in board_unique) else "pocket_pair"
    if board_unique and pair_rank == board_unique[0]:
        kicker = max([rank for rank in hole_ranks if rank != pair_rank] or [0])
        return "top_pair_weak_kicker" if kicker <= 9 else "top_pair"
    if len(board_unique) >= 2 and pair_rank == board_unique[1]:
        return "middle_pair"
    return "bottom_pair"


def action_bucket(action, pot, to_call, actor_chips):
    if action == -2 or action >= max(12000, actor_chips - 200):
        return "all-in"
    if action <= 0:
        return "check/call" if action == 0 else "fold"
    base = max(1, pot + max(0, to_call))
    ratio = float(action) / float(base)
    if ratio < 0.70:
        return "0.5pot"
    if ratio < 1.35:
        return "1pot"
    return "all-in"


def to_call_from_row(row, req, actor_id):
    round_bets = parse_json_text(row.get("round_player_bet_before"), [])
    if isinstance(round_bets, list) and actor_id < len(round_bets):
        current = safe_int(round_bets[actor_id], 0)
        return max(0, max([safe_int(x, 0) for x in round_bets]) - current)
    history = req.get("history") or []
    if history:
        last = history[-1]
        if last.get("player_id") != actor_id and last.get("action_type") in ("raise", "allin"):
            return max(1, safe_int(last.get("action"), 0))
    return 0


def opponent_bucket(name):
    lower = str(name or "").lower()
    if "k40k" in lower:
        return "k40k"
    if "allin" in lower or "all-in" in lower:
        return "allin_v5"
    if "bot_19" in lower or "bot19" in lower:
        return "bot19"
    if "bot_21" in lower or "bot21" in lower:
        return "bot21"
    return "other"


def opponent_label(row):
    opponent = row.get("opponent") or "unknown"
    version = row.get("opponent_version")
    if version not in (None, ""):
        return "{} v{}".format(opponent, version)
    return opponent


def summarize_counts(records):
    rows = {}
    for item in records:
        opp = item["opponent"]
        row = rows.setdefault(opp, {
            "opponent": opp,
            "matches": 0,
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "chip_sum": 0,
            "catastrophic_matches": 0,
        })
        row["matches"] += 1
        chip = safe_int(item.get("chip_delta"), 0)
        row["chip_sum"] += chip
        if chip > 0:
            row["wins"] += 1
        elif chip < 0:
            row["losses"] += 1
        else:
            row["draws"] += 1
        if chip <= -10000:
            row["catastrophic_matches"] += 1
    result = []
    for row in rows.values():
        row["avg_chip"] = round(row["chip_sum"] / float(row["matches"]), 2) if row["matches"] else 0.0
        result.append(row)
    return sorted(result, key=lambda x: (opponent_bucket(x["opponent"]), x["avg_chip"]))


def load_botzone_rows(run_dir, bot_name):
    rows_by_match = {}
    rows = read_csv(os.path.join(run_dir, "matches.csv"))
    for row in rows:
        if str(row.get("my_bot") or "").lower() != bot_name.lower():
            continue
        match_id = row.get("match_id")
        if not match_id:
            continue
        row = dict(row)
        row["_run_dir"] = run_dir
        rows_by_match[match_id] = row
    return rows_by_match


def label_decision(row, hand_delta, meta, args):
    actor_id = safe_int(row.get("actor_id", row.get("player")), -1)
    req = parse_json_text(row.get("request_json"), {})
    stage = norm_stage(row.get("stage") or row.get("round"), None)
    action = safe_int(row.get("response_action", row.get("action")), 0)
    pot = max(1, safe_int(row.get("pot_before"), 1))
    my_chips = safe_int(row.get("my_chips_before"), req.get("my_chips", 0))
    to_call = to_call_from_row(row, req, actor_id)
    public_cards = req.get("public_cards") or parse_json_text(row.get("public_cards"), [])
    my_cards = req.get("my_cards") or parse_json_text(row.get("my_cards"), [])
    score = evaluate_best(my_cards + public_cards) if len(my_cards + public_cards) >= 5 else (0,)
    hand_name = hand_class_name(score) if len(my_cards + public_cards) >= 5 else "pre_showdown"
    cls = score[0] if score else 0
    texture = board_texture(public_cards)
    pair_type = pair_shape(my_cards, public_cards, score)
    bucket = action_bucket(action, pot, to_call, my_chips)
    big_pot = pot + max(0, to_call) >= args.big_pot
    facing_big = to_call >= max(1800, int(pot * 0.55)) or bucket == "all-in"
    active_big = action > 0 and (action >= max(1800, int(pot * 0.65)) or bucket in ("1pot", "all-in"))
    labels = []
    reasons = []

    if stage == "preflop" and (bucket == "all-in" or to_call >= 8000 or action >= 8000):
        strength = preflop_strength(my_cards)
        if strength < 0.62:
            labels.append("force_fold")
            reasons.append("preflop all-in accident with non-premium strength {:.3f}".format(strength))
        else:
            labels.append("allow_allin")
            reasons.append("preflop all-in candidate with premium strength {:.3f}".format(strength))

    if stage in ("flop", "turn", "river") and facing_big and cls <= 1 and not texture["dynamic"]:
        labels.append("force_fold" if stage in ("turn", "river") and hand_delta <= args.huge_hand_loss else "call_only")
        reasons.append("{} big-bet pressure with {}".format(stage, hand_name))

    if stage in ("turn", "river") and facing_big and cls <= 2:
        labels.append("call_only")
        reasons.append("solver abstraction prefers no re-raise with {} in big pot".format(hand_name))

    if stage in ("turn", "river") and active_big and cls <= 2:
        labels.append("check_control" if to_call == 0 else "call_only")
        reasons.append("large {} action with non-nut {}".format(stage, hand_name))

    if stage == "river" and action > 0 and cls <= 2:
        if big_pot or texture["dynamic"] or texture["paired"]:
            labels.append("check_control")
            reasons.append("thin river value on risky or large-pot board")
        else:
            labels.append("small_value")
            reasons.append("river thin value should use smaller sizing")

    if stage in ("turn", "river") and to_call == 0 and cls >= 3:
        if cls >= 5 or (cls == 3 and not texture["dynamic"]):
            labels.append("large_value")
            reasons.append("strong value class supports pressure")
        else:
            labels.append("small_value")
            reasons.append("medium made hand supports controlled value")

    if not labels:
        return None

    primary_order = ["force_fold", "call_only", "check_control", "small_value", "large_value", "allow_allin"]
    primary = sorted(set(labels), key=lambda item: primary_order.index(item) if item in primary_order else 99)[0]
    return {
        "primary_label": primary,
        "labels": sorted(set(labels), key=lambda item: primary_order.index(item) if item in primary_order else 99),
        "reasons": reasons[:4],
        "opponent": meta["opponent"],
        "opponent_bucket": opponent_bucket(meta["opponent"]),
        "match_id": meta["match_id"],
        "hand": safe_int(row.get("hand"), -1),
        "hand_delta": hand_delta,
        "stage": stage,
        "action": action,
        "action_bucket": bucket,
        "pot": pot,
        "to_call": to_call,
        "hand_class": hand_name,
        "hand_class_id": cls,
        "pair_type": pair_type,
        "dynamic_board": texture["dynamic"],
        "paired_board": texture["paired"],
        "flush_pressure": texture["flush_pressure"],
        "straight_pressure": texture["straight_pressure"],
        "my_cards": my_cards,
        "public_cards": public_cards,
    }


def collect_botzone_samples(args):
    run_dirs = []
    if args.botzone_run:
        run_dirs.append(os.path.abspath(args.botzone_run))
    else:
        root = os.path.join(args.project_dir, "botzone_runs")
        run_dirs = [
            os.path.join(root, name)
            for name in sorted(os.listdir(root))
            if os.path.isfile(os.path.join(root, name, "matches.csv"))
        ] if os.path.isdir(root) else []

    target_rows = {}
    for run_dir in run_dirs:
        target_rows.update(load_botzone_rows(run_dir, args.bot_name))

    opponent_rows = []
    samples = []
    for match_id, match_row in target_rows.items():
        opponent = opponent_label(match_row)
        bucket = opponent_bucket(opponent)
        if args.focus_only and bucket == "other":
            continue
        opponent_rows.append({
            "opponent": opponent,
            "chip_delta": safe_int(match_row.get("chip_delta"), 0),
            "result": match_row.get("result"),
        })

        match_dir = os.path.join(match_row["_run_dir"], "matches", match_id)
        hands = {
            str(row.get("hand")): safe_int(row.get("test_delta"), 0)
            for row in read_csv(os.path.join(match_dir, "hands.csv"))
        }
        decisions_path = os.path.join(match_dir, "decisions.csv")
        for row in read_csv(decisions_path):
            actor_id = safe_int(row.get("actor_id", row.get("player")), -1)
            if actor_id != 0:
                continue
            hand_delta = hands.get(str(row.get("hand")), 0)
            labeled = label_decision(row, hand_delta, {
                "match_id": match_id,
                "opponent": opponent,
            }, args)
            if labeled is None:
                continue
            if bucket == "other" and hand_delta > args.huge_hand_loss:
                continue
            labeled["decisions_csv"] = relpath(decisions_path)
            samples.append(labeled)

    label_order = {"force_fold": 0, "call_only": 1, "check_control": 2, "small_value": 3, "large_value": 4, "allow_allin": 5}
    samples.sort(key=lambda item: (
        item["opponent_bucket"],
        label_order.get(item["primary_label"], 99),
        item["hand_delta"],
        item["match_id"],
        item["hand"],
    ))
    return {
        "run_dirs": [relpath(path) for path in run_dirs],
        "target_matches": len(target_rows),
        "opponent_summary": summarize_counts(opponent_rows),
        "samples": samples,
    }


def load_anchor_focus(summary_path, opponents):
    data = read_json(summary_path, {})
    result = []
    for row in data.get("results", []):
        opponent = row.get("opponent")
        if opponent not in opponents:
            continue
        result.append({
            "opponent": opponent,
            "anchor": row.get("anchor"),
            "anchor_pair_wins": row.get("anchor_pair_wins"),
            "opponent_pair_wins": row.get("opponent_pair_wins"),
            "pair_draws": row.get("pair_draws"),
            "anchor_pair_win_rate": row.get("anchor_pair_win_rate"),
            "anchor_avg_chip_diff": row.get("anchor_avg_chip_diff"),
            "source": relpath(summary_path),
        })
    return result


def top_counter(counter, limit):
    return [{"key": key, "count": count} for key, count in counter.most_common(limit)]


def build_summary(botzone, args):
    samples = botzone["samples"]
    label_counter = collections.Counter(item["primary_label"] for item in samples)
    cluster_counter = collections.Counter(
        "{}|{}|{}|{}".format(
            item["opponent_bucket"],
            item["stage"],
            item["hand_class"],
            item["primary_label"],
        )
        for item in samples
    )
    action_counter = collections.Counter(
        "{}|{}|{}".format(item["stage"], item["action_bucket"], item["primary_label"])
        for item in samples
    )
    local_reference = {
        "bot20_anchor": load_anchor_focus(args.bot20_anchor_summary, ["bot_19", "bot_21"]),
        "bot26_core": load_anchor_focus(args.bot26_core_summary, ["bot_19", "bot_21"]),
    }
    return {
        "generated_at": now_text(),
        "project_dir": args.project_dir,
        "config": {
            "bot_name": args.bot_name,
            "botzone_run": relpath(args.botzone_run) if args.botzone_run else None,
            "huge_hand_loss": args.huge_hand_loss,
            "big_pot": args.big_pot,
            "focus_only": args.focus_only,
        },
        "ref_usage": {
            "TexasHoldemSolverJava": "Used as an offline abstraction model: check/call, 0.5pot, 1pot, all-in labels.",
            "SKPokerEval": "Used only as evaluator design reference; labels use judge-compatible no-wheel evaluation.",
        },
        "botzone": botzone,
        "aggregates": {
            "label_counts": dict(label_counter),
            "clusters": top_counter(cluster_counter, 40),
            "action_abstractions": top_counter(action_counter, 40),
        },
        "local_reference": local_reference,
    }


def format_table(headers, rows):
    lines = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def build_report(summary):
    lines = []
    lines.append("# Ref Strategy Labels")
    lines.append("")
    lines.append("- Generated: `{}`".format(summary["generated_at"]))
    lines.append("- Ref usage: TexasHoldemSolverJava for action abstraction; SKPokerEval for evaluator-test design only.")
    lines.append("- Judge compatibility: labels use no-wheel straight evaluation.")
    lines.append("")

    lines.append("## Botzone Focus Opponents")
    rows = []
    for row in summary["botzone"]["opponent_summary"]:
        if opponent_bucket(row["opponent"]) == "other":
            continue
        rows.append([
            row["opponent"],
            "{}-{}-{}".format(row["wins"], row["losses"], row["draws"]),
            row["avg_chip"],
            row["catastrophic_matches"],
        ])
    lines.append(format_table(["opponent", "W-L-D", "avg_chip", "cat_loss"], rows) if rows else "No focus opponent rows.")
    lines.append("")

    lines.append("## Label Counts")
    rows = [[key, value] for key, value in sorted(summary["aggregates"]["label_counts"].items())]
    lines.append(format_table(["label", "count"], rows) if rows else "No labels.")
    lines.append("")

    lines.append("## High-Value Clusters")
    rows = [[item["key"], item["count"]] for item in summary["aggregates"]["clusters"][:16]]
    lines.append(format_table(["opponent|stage|class|label", "count"], rows) if rows else "No clusters.")
    lines.append("")

    lines.append("## Traceable Samples")
    rows = []
    for item in summary["botzone"]["samples"][:24]:
        rows.append([
            item["primary_label"],
            item["opponent"],
            item["match_id"],
            item["hand"],
            item["hand_delta"],
            item["stage"],
            item["action_bucket"],
            item["hand_class"],
            item["pot"],
        ])
    lines.append(format_table(["label", "opponent", "match", "hand", "delta", "stage", "action", "class", "pot"], rows) if rows else "No samples.")
    lines.append("")

    lines.append("## Local Bot19/Bot21 Reference")
    rows = []
    for section, items in summary["local_reference"].items():
        for item in items:
            rows.append([
                section,
                item["opponent"],
                "{}-{}-{}".format(item["anchor_pair_wins"], item["opponent_pair_wins"], item["pair_draws"]),
                item["anchor_avg_chip_diff"],
                item["source"],
            ])
    lines.append(format_table(["source", "opponent", "pair", "avg_chip", "file"], rows) if rows else "No local reference rows.")
    lines.append("")
    return "\n".join(lines)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Create solver-inspired strategy labels from match data.")
    parser.add_argument("--project-dir", default=PROJECT_DIR)
    parser.add_argument("--summary", default=DEFAULT_SUMMARY)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--botzone-run", default=DEFAULT_BOTZONE_RUN)
    parser.add_argument("--bot-name", default="bot20")
    parser.add_argument("--huge-hand-loss", type=int, default=-3000)
    parser.add_argument("--big-pot", type=int, default=3000)
    parser.add_argument("--focus-only", action="store_true", default=True)
    parser.add_argument("--bot20-anchor-summary", default=DEFAULT_BOT20_ANCHOR)
    parser.add_argument("--bot26-core-summary", default=DEFAULT_BOT26_CORE)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    args.project_dir = os.path.abspath(args.project_dir)
    if args.botzone_run:
        args.botzone_run = os.path.abspath(args.botzone_run)
    output_dir = os.path.abspath(args.output_dir)
    ensure_dir(output_dir)

    botzone = collect_botzone_samples(args)
    summary = build_summary(botzone, args)

    json_path = os.path.join(output_dir, "ref_strategy_labels.json")
    report_path = os.path.join(output_dir, "ref_strategy_labels.md")
    write_json(json_path, summary)
    write_text(report_path, build_report(summary))

    print("summary={}".format(relpath(json_path)))
    print("report={}".format(relpath(report_path)))
    print("samples={}".format(len(summary["botzone"]["samples"])))
    print("labels={}".format(summary["aggregates"]["label_counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
