"""Replay analysis: extract structured statistics from match replay JSON.

Pure data transformation — no LLM calls. Used by the match analyst agent
to summarize replay data before sending to the LLM.

Phase 2 additions (trace-first behavior fingerprints, for later Phase 3
FAMOU/MAP-Elites nemesis selection and population diversity):
  - extract_behavior_fingerprint: structured per-street action frequencies +
    aggression/call-down/VPIP features from a replay's game logs.
  - fingerprint_distance: normalized distance in [0,1] between two fingerprints
    (cosine over the per-street action-frequency vector + scalar feature diffs).

Both legacy Botzone replay logs and national/native replay summaries feed the
same action/chip extraction helpers so analyst prompts and behavior fingerprints
do not silently collapse to all-draw, zero-action defaults when the replay schema
changes.
"""

import hashlib
import json
import math
from collections import defaultdict

STREETS = ("preflop", "flop", "turn", "river")
# Canonical action categories used in per-street frequency vectors.
_FP_ACTIONS = ("fold", "raise", "call", "allin")


def _num_public_cards_to_street(n):
    """Map community-card count to street name."""
    return {0: "preflop", 3: "flop", 4: "turn", 5: "river"}.get(n, f"street_{n}")


def _as_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_street(street):
    if street is None:
        return None
    value = str(street).strip().lower()
    return value if value in STREETS else None


def _action_category(action):
    """Map legacy integer and national string actions to canonical categories."""
    if isinstance(action, str):
        value = action.strip().lower()
        if value == "fold":
            return "fold"
        if value in {"raise", "bet"}:
            return "raise"
        if value in {"call", "check"}:
            return "call"
        if value == "allin":
            return "allin"
        return None

    try:
        value = float(action)
    except (TypeError, ValueError):
        return None
    if value == -1:
        return "fold"
    if value == -2:
        return "allin"
    if value > 0:
        return "raise"
    return "call"


def _iter_bot_actions(games, bot_idx):
    """Yield canonical action records for ``bot_idx`` across supported schemas."""
    for game in games:
        logs = game.get("logs") or []
        for log in logs:
            out = log.get("output") if isinstance(log, dict) else None
            if not out or not isinstance(out, dict):
                continue
            display = out.get("display")
            if not display or not isinstance(display, dict):
                continue
            action_info = display.get("last_action")
            if not action_info or not isinstance(action_info, dict):
                continue
            if action_info.get("player_id") != bot_idx:
                continue

            street = _num_public_cards_to_street(len(display.get("public_cards", [])))
            street = _normalize_street(street)
            category = _action_category(action_info.get("action", 0))
            if not street or not category:
                continue
            yield {
                "street": street,
                "category": category,
                "amount": _as_float(action_info.get("action")),
                "pot": _as_float(display.get("pot"), 0.0),
            }

        events = game.get("events_tail") or []
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("type") != "action":
                continue
            if event.get("player_idx") != bot_idx:
                continue
            street = _normalize_street(event.get("stage"))
            category = _action_category(event.get("action"))
            if not street or not category:
                continue
            yield {
                "street": street,
                "category": category,
                "amount": _as_float(event.get("amount")),
                "pot": _as_float(event.get("pot"), 0.0),
            }


def extract_street_patterns(games, bot_idx):
    """Extract per-street action frequencies from a list of game dicts.

    Returns a dict mapping street name → action counts, plus a compact text summary.
    Used by summarize_replay_for_analysis() to detect street-specific weaknesses.
    """
    streets = {s: defaultdict(int) for s in ("preflop", "flop", "turn", "river")}

    for action in _iter_bot_actions(games, bot_idx):
        street = action["street"]
        category = action["category"]
        streets[street][category] += 1
        if category == "raise":
            amount = action.get("amount")
            pot = action.get("pot") or 0
            if amount is not None and pot > 0:
                streets[street]["raise_size_sum"] += amount
                streets[street]["raise_size_pot_sum"] += amount / pot
                streets[street]["raise_size_count"] += 1

    # Build compact text lines
    lines = []
    for street in ("preflop", "flop", "turn", "river"):
        s = streets[street]
        total = s["fold"] + s["raise"] + s["call"] + s["allin"]
        if total == 0:
            continue
        parts = [
            f"fold={s['fold']*100//total}%",
            f"raise={s['raise']*100//total}%",
            f"call={s['call']*100//total}%",
        ]
        if s["allin"] > 0:
            parts.append(f"allin={s['allin']*100//total}%")
        if s.get("raise_size_count", 0) > 0:
            avg_ratio = s["raise_size_pot_sum"] / s["raise_size_count"]
            parts.append(f"avg_raise={avg_ratio:.1f}x_pot")
        lines.append(f"  {street.capitalize()}: {', '.join(parts)}")

    return "\n".join(lines) if lines else ""


# ──────────────────────────────────────────────
# Phase 2: structured behavior fingerprints (trace-first)
# ──────────────────────────────────────────────
def _empty_fingerprint():
    """Return a zero/None fingerprint for the empty-games edge case."""
    return {
        "per_street_freq": {s: {"fold": 0.0, "raise": 0.0, "call": 0.0, "allin": 0.0} for s in STREETS},
        "per_street_avg_raise_x_pot": {s: None for s in STREETS},
        "aggression_factor": None,
        "vpip": None,
        "fold_to_bet_rate": None,
        "bluff_success_rate": None,
        "call_down_rate": None,
        "total_actions": 0,
    }


def _new_fingerprint_accumulator():
    """A fresh, JSON-serializable raw-counter accumulator for behavior stats.

    All fields are additive int/float so accumulators from different replay
    files can be summed to reproduce a single-pass total. The finalized
    fingerprint (normalized ratios) is derived from these raw counters by
    _fingerprint_from_accumulator() — do NOT persist the normalized ratios,
    they are non-additive.
    """
    return {
        "counts": {s: {a: 0.0 for a in _FP_ACTIONS} for s in STREETS},
        "raise_size_sum": {s: 0.0 for s in STREETS},
        "raise_size_pot_sum": {s: 0.0 for s in STREETS},
        "raise_size_count": {s: 0 for s in STREETS},
        "global_raise": 0, "global_allin": 0, "global_call": 0,
        "preflop_raise": 0, "preflop_call": 0, "preflop_total": 0,
        "river_call": 0, "river_allin": 0, "river_total": 0,
        "total_actions": 0,
    }


def _accumulate_fingerprint_counts(games, bot_idx, acc):
    """Fold the action counts for ``bot_idx`` from ``games`` into ``acc`` in place.

    This is the streaming core: it walks supported replay action structures once
    and adds to the mutable accumulator ``acc`` (a _new_fingerprint_accumulator()
    dict), WITHOUT retaining the games. Memory peak = size of one game set, not
    the full replay history. Counts are additive so calling this across multiple
    files then normalizing yields the same fingerprint as a single big pass.
    """
    counts = acc["counts"]
    raise_size_sum = acc["raise_size_sum"]
    raise_size_pot_sum = acc["raise_size_pot_sum"]
    raise_size_count = acc["raise_size_count"]
    for action in _iter_bot_actions(games, bot_idx):
        street = action["street"]
        category = action["category"]
        acc["total_actions"] += 1
        counts[street][category] += 1.0

        if category == "allin":
            acc["global_allin"] += 1
            if street == "preflop":
                acc["preflop_raise"] += 1  # all-in is a voluntary preflop investment
            elif street == "river":
                acc["river_allin"] += 1
        elif category == "raise":
            acc["global_raise"] += 1
            amount = action.get("amount")
            pot = action.get("pot") or 0
            if amount is not None and pot > 0:
                raise_size_sum[street] += float(amount)
                raise_size_pot_sum[street] += float(amount) / float(pot)
                raise_size_count[street] += 1
            if street == "preflop":
                acc["preflop_raise"] += 1
        elif category == "call":
            acc["global_call"] += 1
            if street == "preflop":
                acc["preflop_call"] += 1
            elif street == "river":
                acc["river_call"] += 1

        # Tally per-street denominator for VPIP/call-down.
        if street == "preflop":
            acc["preflop_total"] += 1
        elif street == "river":
            acc["river_total"] += 1


def _fingerprint_from_accumulator(acc):
    """Derive the finalized (normalized) behavior fingerprint from a raw accumulator.

    This is the non-additive normalization step: per-street freqs sum to 1,
    aggression_factor / vpip / call_down_rate are ratios. Must be called AFTER
    all files have been accumulated. acc is consumed read-only.
    """
    fp = _empty_fingerprint()
    counts = acc["counts"]
    raise_size_pot_sum = acc["raise_size_pot_sum"]
    raise_size_count = acc["raise_size_count"]
    fp["total_actions"] = acc["total_actions"]

    for s in STREETS:
        c = counts[s]
        total = sum(c[a] for a in _FP_ACTIONS)
        if total > 0:
            fp["per_street_freq"][s] = {a: c[a] / total for a in _FP_ACTIONS}
        if raise_size_count[s] > 0:
            fp["per_street_avg_raise_x_pot"][s] = (
                raise_size_pot_sum[s] / raise_size_count[s]
            )
    if fp["total_actions"] > 0:
        fp["aggression_factor"] = (acc["global_raise"] + acc["global_allin"]) / (acc["global_call"] + 1)
    if acc["preflop_total"] > 0:
        fp["vpip"] = (acc["preflop_raise"] + acc["preflop_call"]) / acc["preflop_total"]
    if acc["river_total"] > 0:
        fp["call_down_rate"] = (acc["river_call"] + acc["river_allin"]) / acc["river_total"]
    return fp


def extract_behavior_fingerprint(games, bot_idx):
    """Build a structured behavior fingerprint for ``bot_idx`` from game logs.

    Iterates the replay log structure (same ``output.display.last_action`` /
    ``public_cards`` convention as extract_street_patterns) and returns a dict
    of numerical features suitable for fingerprint_distance:

      per_street_freq[street] -> {fold, raise, call, allin}  (normalized to sum 1)
      per_street_avg_raise_x_pot[street] -> float|None       (raise size / pot)
      aggression_factor -> (raise+allin) / (call+1)          (global, +1 avoids div0)
      vpip -> preflop voluntary-in rate ((raise+call) / preflop_actions)
      fold_to_bet_rate -> None (requires reliable prior-opponent action context;
          left None in v1 to avoid miscounting)
      bluff_success_rate -> None (requires cross-game raise→fold tracking; v1 None)
      call_down_rate -> river (call+allin) / river_actions   (float|None)
      total_actions -> int

    Empty/missing actions yield None for the aggregate scalars and a zeroed
    per-street frequency map. fingerprint_distance treats None scalars as
    "ignore this feature" rather than as zero distance.
    """
    acc = _new_fingerprint_accumulator()
    _accumulate_fingerprint_counts(games, bot_idx, acc)
    return _fingerprint_from_accumulator(acc)


def _fp_freq_vector(fp):
    """Flatten per_street_freq into a fixed-order 16-dim list (4 streets × 4 actions)."""
    vec = []
    for s in STREETS:
        block = fp.get("per_street_freq", {}).get(s, {})
        for a in _FP_ACTIONS:
            vec.append(float(block.get(a, 0.0) or 0.0))
    return vec


def _cosine_distance(a, b):
    """Cosine distance (1 - cosine similarity) ∈ [0, 2], clamped to [0, 1].

    Two all-zero vectors are identical ⇒ distance 0 (not 1). A single zero
    vector against a non-zero vector ⇒ 1.0 (maximally different, conservative).
    """
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 and nb == 0.0:
        return 0.0
    if na == 0.0 or nb == 0.0:
        return 1.0
    dot = sum(x * y for x, y in zip(a, b))
    sim = dot / (na * nb)
    # Clamp to [0, 1]; frequencies are non-negative so sim ≥ 0.
    return max(0.0, min(1.0, 1.0 - sim))


def _scalar_distance(a, b, scale=1.0):
    """Normalized absolute difference of two scalars; None on either side skips."""
    if a is None or b is None:
        return None
    return min(1.0, abs(float(a) - float(b)) / float(scale)) if scale else 0.0


def fingerprint_distance(fp1, fp2):
    """Behavior-fingerprint distance ∈ [0, 1] for nemesis / diversity metrics.

    Combines (weights in parens):
      - cosine distance over the 16-dim per-street action-frequency vector (0.6)
      - normalized |aggression_factor| difference, scale 4.0 (0.2)
      - normalized |call_down_rate| difference, scale 1.0 (0.2)

    None scalar features (e.g. empty-games fingerprints) are dropped and their
    weight is redistributed onto the remaining components so a partial
    fingerprint still yields a comparable distance. Identical fingerprints ⇒ 0.0;
    a zero/empty fingerprint vs anything ⇒ 1.0.

    The Jaccard option from the design is unsuitable for continuous frequency
    vectors, so cosine is used (per the design decision).
    """
    # Frequency vector always computable (zeros for empty).
    freq_d = _cosine_distance(_fp_freq_vector(fp1), _fp_freq_vector(fp2))

    agg_d = _scalar_distance(fp1.get("aggression_factor"), fp2.get("aggression_factor"), scale=4.0)
    cd_d = _scalar_distance(fp1.get("call_down_rate"), fp2.get("call_down_rate"), scale=1.0)

    components = [(freq_d, 0.6)]
    if agg_d is not None:
        components.append((agg_d, 0.2))
    if cd_d is not None:
        components.append((cd_d, 0.2))

    total_w = sum(w for _, w in components)
    if total_w <= 0.0:
        return 1.0
    return sum(d * w for d, w in components) / total_w


def _bot_chip_delta(game, bot_idx, bot_name):
    """Return this bot's chip delta for one replay game/match, if available."""
    legacy_key = f"bot{bot_idx}_chips"
    if legacy_key in game:
        return _as_float(game.get(legacy_key))

    per_player = game.get("per_player")
    if isinstance(per_player, dict):
        pdata = per_player.get(bot_name)
        if isinstance(pdata, dict) and "earnings" in pdata:
            return _as_float(pdata.get("earnings"))

    if game.get("bot_a") == bot_name and "net_chips_a" in game:
        return _as_float(game.get("net_chips_a"))
    if game.get("bot_b") == bot_name and "net_chips_b" in game:
        return _as_float(game.get("net_chips_b"))

    # National replay rows conventionally mirror top-level bot0/bot1 as bot_a/b.
    native_key = "net_chips_a" if bot_idx == 0 else "net_chips_b"
    if native_key in game:
        return _as_float(game.get(native_key))

    return None


def _bot_game_outcome(game, bot_idx, opp_idx, chip_delta):
    """Return 1 win, -1 loss, 0 draw for ``bot_idx`` in one game/match."""
    winner = game.get("winner")
    if winner is not None:
        if winner == bot_idx:
            return 1
        if winner == opp_idx:
            return -1
        return 0

    if chip_delta is None:
        return 0
    if chip_delta > 0:
        return 1
    if chip_delta < 0:
        return -1
    return 0


def _game_label(game, fallback_idx):
    for key in ("game", "repeat", "hand", "match"):
        if key in game:
            return game[key]
    return fallback_idx


def _replay_bot_indices(replay_data, bot_name):
    bot_idx = None
    opp_idx = None
    if replay_data.get("bot0") == bot_name:
        bot_idx, opp_idx = 0, 1
    elif replay_data.get("bot1") == bot_name:
        bot_idx, opp_idx = 1, 0
    return bot_idx, opp_idx


def _replay_game_rows(games, bot_idx, opp_idx, bot_name):
    rows = []
    for idx, game in enumerate(games):
        chip_delta = _bot_chip_delta(game, bot_idx, bot_name)
        outcome = _bot_game_outcome(game, bot_idx, opp_idx, chip_delta)
        rows.append({
            "game": game,
            "delta": chip_delta if chip_delta is not None else 0.0,
            "outcome": outcome,
            "label": _game_label(game, idx),
        })
    return rows


def extract_replay_evidence_for_analysis(replay_data, bot_name, match_id=""):
    """Return a compact deterministic evidence row for one bot in one replay.

    This is the non-LLM layer for battle memory. It intentionally mirrors the
    summary signal used by summarize_replay_for_analysis(), but returns
    structured counters with a stable evidence_id so Master/Worker prompts can
    cite observations instead of relying on free-form Markdown.
    """
    bot_idx, opp_idx = _replay_bot_indices(replay_data, bot_name)
    if bot_idx is None:
        return None
    games = replay_data.get("games", [])
    total_games = len(games)
    if total_games == 0:
        return None

    opponent_name = replay_data.get("bot1" if bot_idx == 0 else "bot0", "")
    game_rows = _replay_game_rows(games, bot_idx, opp_idx, bot_name)
    wins = sum(1 for row in game_rows if row["outcome"] > 0)
    losses = sum(1 for row in game_rows if row["outcome"] < 0)
    draws = total_games - wins - losses
    chip_deltas = [row["delta"] for row in game_rows]

    action_counts = {"fold": 0, "raise": 0, "call": 0, "allin": 0}
    street_counts = {
        street: {"fold": 0, "raise": 0, "call": 0, "allin": 0, "total": 0}
        for street in STREETS
    }
    raise_size_pot_sum = {street: 0.0 for street in STREETS}
    raise_size_count = {street: 0 for street in STREETS}
    for action in _iter_bot_actions(games, bot_idx):
        street = action["street"]
        category = action["category"]
        if category not in action_counts or street not in street_counts:
            continue
        action_counts[category] += 1
        street_counts[street][category] += 1
        street_counts[street]["total"] += 1
        if category == "raise":
            amount = action.get("amount")
            pot = action.get("pot") or 0
            if amount is not None and pot > 0:
                raise_size_pot_sum[street] += float(amount) / float(pot)
                raise_size_count[street] += 1

    for street in STREETS:
        if raise_size_count[street] > 0:
            street_counts[street]["avg_raise_x_pot"] = (
                raise_size_pot_sum[street] / raise_size_count[street]
            )
        else:
            street_counts[street]["avg_raise_x_pot"] = None

    total_actions = sum(action_counts.values())
    big_pot_losses = [row for row in game_rows if row["delta"] < -5000]
    avg_delta = sum(chip_deltas) / len(chip_deltas)
    spot_tags = []
    if avg_delta < -250:
        spot_tags.append("negative_chip_ev")
    if avg_delta > 250:
        spot_tags.append("positive_chip_ev")
    if big_pot_losses:
        spot_tags.append("big_pot_losses")
    if total_actions > 0:
        fold_rate = action_counts["fold"] / total_actions
        raise_rate = (action_counts["raise"] + action_counts["allin"]) / total_actions
        if fold_rate >= 0.45:
            spot_tags.append("high_fold_rate")
        if raise_rate <= 0.12:
            spot_tags.append("low_aggression")
        if raise_rate >= 0.35:
            spot_tags.append("high_aggression")
    for street in STREETS:
        total = street_counts[street]["total"]
        if total >= 3 and street_counts[street]["fold"] / total >= 0.5:
            spot_tags.append(f"{street}_fold_heavy")
        if total >= 3 and (street_counts[street]["raise"] + street_counts[street]["allin"]) / total >= 0.4:
            spot_tags.append(f"{street}_aggressive")

    evidence_seed = {
        "match_id": match_id,
        "bot": bot_name,
        "opponent": opponent_name,
        "sample_n": total_games,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "avg_delta": round(avg_delta, 3),
        "actions": action_counts,
    }
    evidence_id = "ev_" + hashlib.sha256(
        json.dumps(evidence_seed, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]

    return {
        "evidence_id": evidence_id,
        "match_id": match_id,
        "bot": bot_name,
        "opponent": opponent_name,
        "sample_n": total_games,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "win_rate": wins / total_games if total_games else None,
        "avg_delta": avg_delta,
        "best_delta": max(chip_deltas),
        "worst_delta": min(chip_deltas),
        "big_pot_loss_count": len(big_pot_losses),
        "actions": action_counts,
        "total_actions": total_actions,
        "street_actions": street_counts,
        "spot_tags": sorted(set(spot_tags)),
    }


def summarize_replay_for_analysis(replay_data, bot_name):
    """Extract structured statistics from replay JSON for LLM analysis.

    Compresses ~253 game logs into a compact ~500 token summary covering
    win rates, chip distribution, fold frequency, key action patterns,
    and per-street behaviour breakdown.
    """
    bot_idx, opp_idx = _replay_bot_indices(replay_data, bot_name)
    if bot_idx is None:
        return ""

    games = replay_data.get("games", [])
    total_games = len(games)
    if total_games == 0:
        return ""

    game_rows = _replay_game_rows(games, bot_idx, opp_idx, bot_name)

    wins = sum(1 for row in game_rows if row["outcome"] > 0)
    losses = sum(1 for row in game_rows if row["outcome"] < 0)
    draws = total_games - wins - losses
    chip_deltas = [row["delta"] for row in game_rows]

    lines = []
    result_str = f"{wins}W/{draws}D/{losses}L" if draws else f"{wins}W/{losses}L"
    lines.append(f"Match: {replay_data['bot0']} vs {replay_data['bot1']}, "
                 f"Result: {result_str} out of {total_games} games")
    lines.append(f"Chip delta: avg={sum(chip_deltas)/len(chip_deltas):.0f}, "
                 f"best={max(chip_deltas):.0f}, worst={min(chip_deltas):.0f}")

    # Per-game action analysis
    fold_count = 0
    raise_count = 0
    call_count = 0
    allin_count = 0
    big_pot_losses = []  # games where bot lost big pots

    for action in _iter_bot_actions(games, bot_idx):
        category = action["category"]
        if category == "fold":
            fold_count += 1
        elif category == "allin":
            allin_count += 1
        elif category == "raise":
            raise_count += 1
        elif category == "call":
            call_count += 1

    for row in game_rows:
        if row["delta"] < -5000:
            big_pot_losses.append((row["label"], row["delta"]))

    total_actions = fold_count + raise_count + call_count + allin_count
    if total_actions > 0:
        lines.append(f"Actions: fold={fold_count}({fold_count*100//total_actions}%), "
                     f"call={call_count}({call_count*100//total_actions}%), "
                     f"raise={raise_count}({raise_count*100//total_actions}%), "
                     f"allin={allin_count}({allin_count*100//total_actions}%)")

    if big_pot_losses:
        lines.append(f"Big losses (>-5000): {len(big_pot_losses)} games")
        for gid, delta in big_pot_losses[:3]:
            lines.append(f"  Game {gid}: {delta:.0f} chips")

    # Per-street action breakdown (StratFormer-style opponent modelling insight)
    street_summary = extract_street_patterns(games, bot_idx)
    if street_summary:
        lines.append("Per-street actions (bot):")
        lines.append(street_summary)

    return "\n".join(lines)
