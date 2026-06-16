"""Replay analysis: extract structured statistics from match replay JSON.

Pure data transformation — no LLM calls. Used by the match analyst agent
to summarize replay data before sending to the LLM.

Phase 2 additions (trace-first behavior fingerprints, for later Phase 3
FAMOU/MAP-Elites nemesis selection and population diversity):
  - extract_behavior_fingerprint: structured per-street action frequencies +
    aggression/call-down/VPIP features from a replay's game logs.
  - fingerprint_distance: normalized distance ∈ [0,1] between two fingerprints
    (cosine over the per-street action-frequency vector + scalar feature diffs).
extract_street_patterns (the string-summary used by summarize_replay_for_analysis)
is left UNCHANGED.
"""

import json
import math
from collections import defaultdict


def _num_public_cards_to_street(n):
    """Map community-card count to street name."""
    return {0: "preflop", 3: "flop", 4: "turn", 5: "river"}.get(n, f"street_{n}")


def extract_street_patterns(games, bot_idx):
    """Extract per-street action frequencies from a list of game dicts.

    Returns a dict mapping street name → action counts, plus a compact text summary.
    Used by summarize_replay_for_analysis() to detect street-specific weaknesses.
    """
    streets = {s: defaultdict(int) for s in ("preflop", "flop", "turn", "river")}

    for g in games:
        for log in g.get("logs", []):
            out = log.get("output")
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

            # Determine street from number of community cards present BEFORE this action
            n_community = len(display.get("public_cards", []))
            street = _num_public_cards_to_street(n_community)
            if street not in streets:
                continue

            act_val = action_info.get("action", 0)
            if act_val == -1:
                streets[street]["fold"] += 1
            elif act_val == -2:
                streets[street]["allin"] += 1
            elif act_val > 0:
                streets[street]["raise"] += 1
                # Track raise size relative to pot (pot available from display)
                pot = display.get("pot", 0)
                if pot > 0:
                    streets[street]["raise_size_sum"] += act_val
                    streets[street]["raise_size_pot_sum"] += act_val / pot
                    streets[street]["raise_size_count"] += 1
            elif act_val == 0:
                streets[street]["call"] += 1
            # Other values (e.g. timeout) are ignored

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

STREETS = ("preflop", "flop", "turn", "river")
# Canonical action categories used in per-street frequency vectors.
_FP_ACTIONS = ("fold", "raise", "call", "allin")


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
    fp = _empty_fingerprint()
    # Raw per-street action counters.
    counts = {s: defaultdict(float) for s in STREETS}
    raise_size_sum = {s: 0.0 for s in STREETS}
    raise_size_pot_sum = {s: 0.0 for s in STREETS}
    raise_size_count = {s: 0 for s in STREETS}

    global_raise = 0
    global_allin = 0
    global_call = 0
    preflop_raise = 0
    preflop_call = 0
    preflop_total = 0
    river_call = 0
    river_allin = 0
    river_total = 0

    for g in games:
        for log in g.get("logs", []):
            out = log.get("output")
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

            n_community = len(display.get("public_cards", []))
            street = _num_public_cards_to_street(n_community)
            if street not in counts:
                continue

            act_val = action_info.get("action", 0)
            fp["total_actions"] += 1
            if act_val == -1:
                counts[street]["fold"] += 1.0
            elif act_val == -2:
                counts[street]["allin"] += 1.0
                global_allin += 1
                if street == "preflop":
                    preflop_raise += 1  # all-in is a voluntary preflop investment
                elif street == "river":
                    river_allin += 1
            elif act_val > 0:
                counts[street]["raise"] += 1.0
                global_raise += 1
                pot = display.get("pot", 0)
                if pot and pot > 0:
                    raise_size_sum[street] += float(act_val)
                    raise_size_pot_sum[street] += float(act_val) / float(pot)
                    raise_size_count[street] += 1
                if street == "preflop":
                    preflop_raise += 1
            elif act_val == 0:
                counts[street]["call"] += 1.0
                global_call += 1
                if street == "preflop":
                    preflop_call += 1
                elif street == "river":
                    river_call += 1
            # Tally per-street denominator for VPIP/call-down.
            if street == "preflop":
                preflop_total += 1
            elif street == "river":
                river_total += 1

    # Normalize per-street frequencies to sum 1.
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
        fp["aggression_factor"] = (global_raise + global_allin) / (global_call + 1)
    if preflop_total > 0:
        fp["vpip"] = (preflop_raise + preflop_call) / preflop_total
    if river_total > 0:
        fp["call_down_rate"] = (river_call + river_allin) / river_total

    return fp


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


def summarize_replay_for_analysis(replay_data, bot_name):
    """Extract structured statistics from replay JSON for LLM analysis.

    Compresses ~253 game logs into a compact ~500 token summary covering
    win rates, chip distribution, fold frequency, key action patterns,
    and per-street behaviour breakdown.
    """
    bot_idx = None
    opp_idx = None
    if replay_data.get("bot0") == bot_name:
        bot_idx, opp_idx = 0, 1
    elif replay_data.get("bot1") == bot_name:
        bot_idx, opp_idx = 1, 0
    if bot_idx is None:
        return ""

    games = replay_data.get("games", [])
    total_games = len(games)
    if total_games == 0:
        return ""

    wins = sum(1 for g in games if g.get("winner") == bot_idx)
    chip_deltas = [g.get(f"bot{bot_idx}_chips", 0.0) for g in games]

    lines = []
    draws = total_games - wins - sum(1 for g in games if g.get("winner") == opp_idx)
    losses = total_games - wins - draws
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

    for g in games:
        game_chip = g.get(f"bot{bot_idx}_chips", 0.0)
        logs = g.get("logs", [])

        for log in logs:
            out = log.get("output")
            if not out or not isinstance(out, dict):
                continue

            # Count from request content (bot's own actions)
            content = out.get("content", {})
            if isinstance(content, dict):
                player_data = content.get(str(bot_idx), {})
                if isinstance(player_data, dict):
                    history = player_data.get("history", [])
                    continue

            # Count from display data
            display = out.get("display")
            if display and isinstance(display, dict):
                action = display.get("last_action")
                if action and isinstance(action, dict):
                    pid = action.get("player_id")
                    if pid == bot_idx:
                        act_val = action.get("action", 0)
                        if act_val == -1:
                            fold_count += 1
                        elif act_val == -2:
                            allin_count += 1
                        elif act_val > 0:
                            raise_count += 1
                        else:
                            call_count += 1

        if game_chip < -5000:
            big_pot_losses.append((g.get("game", "?"), game_chip))

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
