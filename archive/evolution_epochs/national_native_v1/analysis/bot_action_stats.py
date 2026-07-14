"""
Bot action statistics extraction from replay files.

Authoritative action source: RESPONSE log entries.

A replay JSON produced by the native TCP rating daemon has the shape:

    {
      "bot0": <name>, "bot1": <name>,
      "games": [ {"game": int, "mirror": bool, "winner": int,
                   "bot0_chips": float, "bot1_chips": float,
                   "logs": [ <log entries> ]}, ... ]
    }

Each `logs` list interleaves two entry kinds:
  * REQUEST entries: {"output": {"command": "request", "content": {"<pid>": {...}},
                                   "display": {"round": 0|1|2|3, "round_player_bet": [b0, b1],
                                                "last_action": {...}, "matchdata": {"hand": int}, ...}}}
  * RESPONSE entries: {"<pid>": {"response": "<int>", "verdict": "OK"}, "output": null}

The RESPONSE entry carries the action the bot ACTUALLY took in reply to the immediately
preceding REQUEST entry for that same player id. Decoding the response int follows the
engine/judge action codes:
    -1  -> fold
    -2  -> allin
     0  -> call-or-check (disambiguated via the preceding request's display.round_player_bet:
           matched bets => check, unmatched bets => call)
    >0  -> raise-to-total

`display.last_action.action_type` (the request-side mirror) is NOT authoritative: it echoes
the PREVIOUS player's action, so it misattributes hand-ending folds (those entries omit
`round`, landing them in an unknown street) and cannot classify the opening action of any
hand (no previous action exists). It is used here only as an auxiliary cross-check.

Player id maps stably to bot names: replay["bot0"] -> player id 0, replay["bot1"] ->
player id 1. mirror_battle swaps the CARDS/deck, never the bot paths, so this mapping
holds for both the normal and mirror halves of every game.

Pure Python, no external dependencies beyond json / os / pathlib.

=== SCHEMA (Phase 0 prerequisites A+B+D) ===

`compute_all_bot_stats` now returns a PER-OPPONENT breakdown:

    {
      <bot>: {
        <opponent>: {
          "preflop": {"total", "fold", "call", "raise", "check", "allin",
                      "fold_to_bet", "cbet", "barrel"},
          "flop":    {...}, "turn": {...}, "river": {...},
          "total_hands": M,
        },
        ... (one entry per opponent the bot faced)
      }
    }

Bots with no recorded actions map to an empty dict {}.

New per-street metrics (computed from `round_player_bet` + the acting bot's own RESPONSE,
NEVER from `last_action` which misattributes actions):
  * fold_to_bet: a FOLD taken while facing a bet (my_bet < opp_bet on that street).
  * cbet:        a RAISE on the flop by the preflop raiser (continuation bet). The preflop
                 raise is tracked per-hand; a flop raise by the same player counts as cbet.
                 (allin-on-flop-by-PFR also counts, since allin is a raise.)
  * barrel:      a RAISE on a street where the SAME player raised the PREVIOUS street in the
                 SAME hand (multi-street barrel). Flop raise by PFR is cbet, not barrel;
                 barrel starts counting from turn onward.

`aggression_factor` (raise_count / call_count) is NOT stored per-action; it is derivable
from the raise/call counters. It is surfaced by `get_global_stats` for convenience.

=== BACKWARD COMPATIBILITY ===

`get_global_stats(stats, bot)` collapses the per-opponent dimension back into the
legacy flat shape `{street: {total, fold, call, raise, check, allin, ...}, total_hands}`
plus a derived `aggression_factor` per street. Downstream readers
(elo_daemon save_cycle, tool_planning Master-prompt injection, orchestrator_context)
MUST go through `get_global_stats` (or the stats file must be written in the legacy
flat shape via `get_global_stats`). The on-disk `bot_action_stats.json` keeps the legacy
flat shape to avoid breaking any reader that loads it directly.
"""

import json
import os
from pathlib import Path


# display.round integer -> street name
_STREET_BY_ROUND = {0: "preflop", 1: "flop", 2: "turn", 3: "river"}
_STREETS = ("preflop", "flop", "turn", "river")
_STREET_INDEX = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}

# Etag / incremental-computation cache filename (written next to replays by default,
# but the exact path is controlled by compute_all_bot_stats).
_ETAG_FILENAME = ".stats_etag.json"
# Full native replay extraction is intentionally asynchronous and can overlap
# several daemon saves.  A scan remains useful only within this small,
# explicitly reported same-identity lag window.
MAX_ACTION_STATS_CYCLE_LAG = 5


def _empty_street_stats():
    return {
        "total": 0, "fold": 0, "call": 0, "raise": 0, "check": 0, "allin": 0,
        # Phase 0 (A) conditional features:
        "fold_to_bet": 0,  # folds taken while facing a bet (my_bet < opp_bet)
        "cbet": 0,         # flop raise by the preflop raiser (continuation bet)
        "barrel": 0,       # raise on a street following a self-raise on the prior street
    }


def _classify_response(resp_int, round_player_bet, player_id):
    """Classify a response int into one of fold/call/check/raise/allin.

    `round_player_bet` is the [bet_player0, bet_player1] list from the request the bot is
    answering; it disambiguates response=0 (call when bets differ, check when matched).
    Returns None if the value cannot be parsed.
    """
    if resp_int is None:
        return None
    if resp_int == -1:
        return "fold"
    if resp_int == -2:
        return "allin"
    if resp_int > 0:
        return "raise"
    # resp_int == 0 -> call or check
    if not isinstance(round_player_bet, (list, tuple)) or len(round_player_bet) < 2:
        # No bet info available: cannot disambiguate; treat as call (the action committed
        # chips to match / stay in). This branch is rarely hit in real replays.
        return "call"
    my_bet = round_player_bet[player_id]
    opp_bet = round_player_bet[1 - player_id]
    if my_bet == opp_bet:
        return "check"
    return "call"


def _int_response(resp):
    """Parse a response value (string or int) into an int, or None."""
    try:
        return int(resp)
    except (TypeError, ValueError):
        return None


def extract_actions_from_replay(replay_json):
    """Extract every bot action from a single replay JSON.

    Returns a list of dicts, one per RESPONSE entry:

        {
          "bot": <name>,                 # acting bot name
          "opponent": <name>,            # the other bot in this pair (Phase 0 B)
          "street": <str|None>,          # preflop/flop/turn/river, or None if unknown
          "action": <class>,             # fold/call/check/raise/allin
          "hand": <int|None>,            # 0-indexed hand number (matchdata.hand)
          # Phase 0 (A) conditional-feature flags (booleans):
          "fold_to_bet": <bool>,         # True if fold while my_bet < opp_bet
          "cbet": <bool>,                # True if flop raise by the preflop raiser
          "barrel": <bool>,              # True if raise follows a self-raise last street
        }

    `street` is None only if the preceding request's display.round was missing/unrecognized.
    `action` is one of fold/call/check/raise/allin (allin is also semantically a raise, but
    here it is its own class; the aggregator double-counts it into the raise key as well).

    Conditional features are derived purely from `round_player_bet` + the acting bot's own
    RESPONSE, plus a per-hand state machine tracking which streets each player raised on.
    `display.last_action` is intentionally NOT consulted (it echoes the previous player's
    action and mislabels opening actions / hand-ending folds).
    """
    if isinstance(replay_json, (str, bytes)):
        replay_json = json.loads(replay_json)

    bot0 = replay_json.get("bot0")
    bot1 = replay_json.get("bot1")
    if not bot0 or not bot1:
        return []
    pid_to_bot = {0: bot0, 1: bot1}
    # opponent name for each pid (Phase 0 B)
    pid_to_opp = {0: bot1, 1: bot0}

    games = replay_json.get("games", [])
    if not games:
        return []

    actions = []
    for game in games:
        logs = game.get("logs", [])
        if not isinstance(logs, list):
            continue
        # Single forward pass: remember the most-recent request addressed to each
        # player id, so each response finds its matching request in O(1). The old
        # backward `while j >= 0` scan was O(R*L) per game (~50K comparisons per
        # 70-hand half-game), which blocked the daemon save cycle at 2000 replays.
        last_request = {}  # pid_str -> (street, round_player_bet, hand)

        # Per-hand state machine (Phase 0 A) for conditional features. Reset whenever
        # the hand number changes or the street rolls backward. We track per-player:
        #   raised_street: frozenset of street indices this player raised on this hand
        #                  (used for cbet + barrel detection)
        #   cur_hand: last seen hand number (to detect hand boundaries when matchdata.hand
        #             is present; otherwise we reset on street roll-back to preflop)
        raised_streets = {0: set(), 1: set()}
        cur_hand = None

        for entry in logs:
            out = entry.get("output")
            if isinstance(out, dict) and out.get("command") == "request":
                content = out.get("content", {})
                disp_raw = out.get("display")
                disp = disp_raw if isinstance(disp_raw, dict) else {}
                street = _STREET_BY_ROUND.get(disp.get("round"))
                round_player_bet = disp.get("round_player_bet")
                matchdata = disp.get("matchdata", {})
                hand = matchdata.get("hand") if isinstance(matchdata, dict) else None
                # Hand-boundary reset (when hand number advances).
                if hand is not None and hand != cur_hand:
                    cur_hand = hand
                    raised_streets = {0: set(), 1: set()}
                for pid_str in ("0", "1"):
                    if pid_str in content:
                        last_request[pid_str] = (street, round_player_bet, hand)
                continue
            if out is not None:
                continue  # not a response entry
            # RESPONSE entry: output is None, keyed by the acting player id.
            for pid_str in ("0", "1"):
                if pid_str not in entry:
                    continue
                resp = _int_response(entry[pid_str].get("response"))
                req = last_request.get(pid_str)
                if req is None:
                    continue  # no preceding request for this pid
                street, round_player_bet, hand = req
                pid = int(pid_str)
                action_class = _classify_response(resp, round_player_bet, pid)
                if action_class is None:
                    break  # a response entry carries exactly one player id

                # --- Phase 0 (A): conditional-feature derivation ---
                fold_to_bet = False
                cbet = False
                barrel = False

                is_raise = action_class in ("raise", "allin")

                # fold_to_bet: fold while facing a bet (my_bet < opp_bet).
                if action_class == "fold":
                    if (isinstance(round_player_bet, (list, tuple))
                            and len(round_player_bet) >= 2
                            and round_player_bet[pid] is not None
                            and round_player_bet[1 - pid] is not None
                            and round_player_bet[pid] < round_player_bet[1 - pid]):
                        fold_to_bet = True

                street_idx = _STREET_INDEX.get(street) if street else None

                if is_raise and street_idx is not None:
                    prev_raised = raised_streets[pid]
                    if street_idx == 1:
                        # Flop raise. cbet iff this player raised preflop this hand.
                        if 0 in prev_raised:
                            cbet = True
                    elif street_idx >= 2:
                        # Turn/river raise. Barrel iff this player raised the immediately
                        # preceding street this hand.
                        if (street_idx - 1) in prev_raised:
                            barrel = True
                    # Record this raise for downstream streets (cbet/barrel chain).
                    raised_streets[pid].add(street_idx)

                actions.append({
                    "bot": pid_to_bot[pid],
                    "opponent": pid_to_opp[pid],
                    "street": street,
                    "action": action_class,
                    "hand": hand,
                    "fold_to_bet": fold_to_bet,
                    "cbet": cbet,
                    "barrel": barrel,
                })
                break  # a response entry carries exactly one player id
    return actions


# Backward-compat alias for any older import name.
def extract_hands_from_replay(replay_json):
    """DEPRECATED alias. Returns the raw action list (kept for import compatibility)."""
    return extract_actions_from_replay(replay_json)


def _native_tracker_rows(replay_json):
    """Yield opponent snapshots carried by national-native runtime telemetry."""
    bot0 = str(replay_json.get("bot0") or "")
    bot1 = str(replay_json.get("bot1") or "")
    if not bot0 or not bot1:
        return
    replay_id = str(replay_json.get("id") or "")
    for match_index, game in enumerate(replay_json.get("games") or []):
        if not isinstance(game, dict) or game.get("execution_mode") != "native_tcp":
            continue
        per_player = game.get("per_player") or {}
        if not isinstance(per_player, dict) or len(per_player) != 2:
            continue
        game_a = str(game.get("bot_a") or "")
        game_b = str(game.get("bot_b") or "")
        label_to_bot = {game_a: bot0, game_b: bot1}
        labels = list(per_player)
        for observer_label, pdata in per_player.items():
            if not isinstance(pdata, dict):
                continue
            latest = (
                ((((pdata.get("runtime_telemetry") or {}).get("bot_log") or {})
                  .get("opponent_tracker") or {}).get("latest"))
            )
            if not isinstance(latest, dict):
                continue
            other_labels = [label for label in labels if label != observer_label]
            if len(other_labels) != 1:
                continue
            observer = label_to_bot.get(str(observer_label), str(observer_label))
            tracked = label_to_bot.get(str(other_labels[0]), str(other_labels[0]))
            yield {
                "bot": tracked,
                "opponent": observer,
                "snapshot": latest,
                "replay_id": replay_id,
                "match_index": match_index,
            }


def _new_native_tracker_accumulator():
    return {
        "hands_started": 0,
        "hands_completed": 0,
        "showdowns": 0,
        "raw_street_actions": {street: {} for street in _STREETS},
        "semantic_street_actions": {street: {} for street in _STREETS},
        "facing_raise": {},
        "facing_allin": {},
        "facing_raise_by_street": {street: {} for street in _STREETS},
        "facing_allin_by_street": {street: {} for street in _STREETS},
        "showdown_bucket_counts": {},
        "showdown_class_counts": {},
        "showdown_range_samples": 0,
        "latest_contexts": {},
        "_latest_context_key": None,
    }


def _merge_counter(target, source):
    for key, value in (source or {}).items():
        try:
            target[str(key)] = int(target.get(str(key), 0)) + int(value or 0)
        except (TypeError, ValueError):
            continue


def _apply_native_tracker_rows(totals, bot_set, replay_json=None, *, rows=None):
    applied = False
    source_rows = rows if rows is not None else (_native_tracker_rows(replay_json) or [])
    for row in source_rows:
        bot = row["bot"]
        opponent = row["opponent"]
        if bot not in bot_set or opponent not in bot_set or bot == opponent:
            continue
        snapshot = row["snapshot"]
        bucket = _ensure_opponent_bucket(totals[bot], opponent)
        accumulator = bucket.setdefault(
            "_native_tracker",
            _new_native_tracker_accumulator(),
        )
        for field in ("hands_started", "hands_completed", "showdowns"):
            try:
                accumulator[field] += int(snapshot.get(field, 0) or 0)
            except (TypeError, ValueError):
                pass
        raw_actions = snapshot.get("raw_street_actions") or {}
        semantic_actions = snapshot.get("semantic_street_actions") or {}
        for street in _STREETS:
            raw = raw_actions.get(street) or {}
            _merge_counter(accumulator["raw_street_actions"][street], raw)
            _merge_counter(
                accumulator["semantic_street_actions"][street],
                semantic_actions.get(street) or {},
            )
            stats = bucket[street]
            for action in ("fold", "call", "check", "raise", "allin"):
                try:
                    count = int(raw.get(action, 0) or 0)
                except (TypeError, ValueError):
                    count = 0
                stats["total"] += count
                if action == "allin":
                    stats["allin"] += count
                    stats["raise"] += count
                else:
                    stats[action] += count
        terminal = snapshot.get("terminal_response") or {}
        _merge_counter(accumulator["facing_raise"], terminal.get("facing_raise") or {})
        _merge_counter(accumulator["facing_allin"], terminal.get("facing_allin") or {})
        for street in _STREETS:
            _merge_counter(
                accumulator["facing_raise_by_street"][street],
                (terminal.get("facing_raise_by_street") or {}).get(street) or {},
            )
            _merge_counter(
                accumulator["facing_allin_by_street"][street],
                (terminal.get("facing_allin_by_street") or {}).get(street) or {},
            )
        showdown = snapshot.get("showdown_range") or {}
        try:
            accumulator["showdown_range_samples"] += int(showdown.get("samples", 0) or 0)
        except (TypeError, ValueError):
            pass
        _merge_counter(
            accumulator["showdown_bucket_counts"],
            showdown.get("bucket_counts") or {},
        )
        _merge_counter(
            accumulator["showdown_class_counts"],
            showdown.get("class_counts") or {},
        )
        context_key = (str(row["replay_id"]), int(row["match_index"]))
        if (
            accumulator.get("_latest_context_key") is None
            or context_key > tuple(accumulator["_latest_context_key"])
        ):
            accumulator["_latest_context_key"] = context_key
            accumulator["latest_contexts"] = snapshot.get("contexts") or {}
        hand_prefix = (row["replay_id"], row["match_index"])
        for hand in range(max(0, int(snapshot.get("hands_started", 0) or 0))):
            hand_id = (*hand_prefix, hand)
            bucket["_hands"].add(hand_id)
            totals[bot]["_hands"].add(hand_id)
        applied = True
    return applied


def _finalize_native_tracker(accumulator):
    if not isinstance(accumulator, dict):
        return None
    facing_raise = accumulator.get("facing_raise") or {}
    facing_allin = accumulator.get("facing_allin") or {}
    raise_opps = int(facing_raise.get("opportunities", 0) or 0)
    jam_opps = int(facing_allin.get("opportunities", 0) or 0)
    river_raise = (accumulator.get("facing_raise_by_street") or {}).get("river") or {}
    river_allin = (accumulator.get("facing_allin_by_street") or {}).get("river") or {}
    river_opps = int(river_raise.get("opportunities", 0) or 0) + int(
        river_allin.get("opportunities", 0) or 0
    )
    river_calls = int(river_raise.get("call", 0) or 0) + int(
        river_allin.get("call", 0) or 0
    )
    showdown_samples = int(accumulator.get("showdown_range_samples", 0) or 0)
    bucket_counts = dict(accumulator.get("showdown_bucket_counts") or {})
    return {
        "source": "national_native_opponent_tracker",
        "hands_started": int(accumulator.get("hands_started", 0) or 0),
        "hands_completed": int(accumulator.get("hands_completed", 0) or 0),
        "showdowns": int(accumulator.get("showdowns", 0) or 0),
        "raw_street_actions": accumulator.get("raw_street_actions") or {},
        "semantic_street_actions": accumulator.get("semantic_street_actions") or {},
        "terminal_response": {
            "facing_raise": dict(facing_raise),
            "facing_allin": dict(facing_allin),
            "facing_raise_by_street": accumulator.get("facing_raise_by_street") or {},
            "facing_allin_by_street": accumulator.get("facing_allin_by_street") or {},
            "fold_to_raise": (
                int(facing_raise.get("fold", 0) or 0) / raise_opps
                if raise_opps else None
            ),
            "fold_to_jam": (
                int(facing_allin.get("fold", 0) or 0) / jam_opps
                if jam_opps else None
            ),
            "river_overcall": river_calls / river_opps if river_opps else None,
            "river_overcall_samples": river_opps,
        },
        "showdown_range": {
            "samples": showdown_samples,
            "bucket_counts": bucket_counts,
            "bucket_rates": {
                key: (int(value) / showdown_samples if showdown_samples else 0.0)
                for key, value in bucket_counts.items()
            },
            "class_counts": dict(accumulator.get("showdown_class_counts") or {}),
        },
        "latest_contexts": accumulator.get("latest_contexts") or {},
    }


def _new_zero_totals():
    """Per-bot nested counters keyed by opponent then street.

    Returns a mutable container:
        {
          <opponent_name>: {
              "preflop": <street_stats>, ..., "river": <street_stats>,
              "_hands": set(),  # distinct hand numbers vs THIS opponent
          },
          "_hands": set(),  # distinct hand numbers vs ALL opponents (for global view)
        }
    """
    return {
        "_hands": set(),  # global (cross-opponent) hand set
    }


def _ensure_opponent_bucket(bt, opponent):
    """Get-or-create the per-opponent street-counters dict inside a bot's totals."""
    bucket = bt.get(opponent)
    if bucket is None:
        bucket = {
            "preflop": _empty_street_stats(),
            "flop": _empty_street_stats(),
            "turn": _empty_street_stats(),
            "river": _empty_street_stats(),
            "_hands": set(),
        }
        bt[opponent] = bucket
    return bucket


def _aggregate_action(totals, bot, action):
    """Increment the per-bot per-opponent per-street counters for one action.

    `allin` is counted in BOTH the allin key AND the raise key: an all-in is semantically a
    raise/bet, and the readers (tool_planning.py / orchestrator_context.py) report raise as a
    fraction of total actions on a street. Double-counting allin into raise keeps that
    fraction meaningful while still surfacing the dedicated allin frequency.

    Phase 0 (A) conditional flags (fold_to_bet / cbet / barrel) are incremented on the
    same street counters when the action's flag is True.
    Phase 0 (B): counters are nested under the opponent dimension.
    """
    street = action["street"]
    cls = action["action"]
    opponent = action.get("opponent")
    if not opponent:
        # Defensive: actions without a resolvable opponent (shouldn't happen given replay
        # shape) are dropped from the per-opponent breakdown.
        return
    bt = totals[bot]
    bucket = _ensure_opponent_bucket(bt, opponent)
    # Actions on an unrecognized street are skipped (no bucket to put them in).
    if street not in _STREETS:
        # Still record the hand so totals reflect participation even if street was unknown.
        if action.get("hand") is not None:
            bucket["_hands"].add(action["hand"])
            bt["_hands"].add(action["hand"])
        return
    st = bucket[street]
    st["total"] += 1
    if cls == "fold":
        st["fold"] += 1
        if action.get("fold_to_bet"):
            st["fold_to_bet"] += 1
    elif cls == "call":
        st["call"] += 1
    elif cls == "check":
        st["check"] += 1
    elif cls == "raise":
        st["raise"] += 1
    elif cls == "allin":
        # Counted in BOTH allin and raise (see docstring above).
        st["allin"] += 1
        st["raise"] += 1
    # cbet / barrel flags can ride on raise or allin (allin is a raise).
    if action.get("cbet"):
        st["cbet"] += 1
    if action.get("barrel"):
        st["barrel"] += 1
    if action.get("hand") is not None:
        bucket["_hands"].add(action["hand"])
        bt["_hands"].add(action["hand"])


def _finalize_totals(totals, active_bots):
    """Convert raw counters into the output shape (per-opponent breakdown).

    Drops opponents/bots with no actions. Output:
        {
          <bot>: {
            <opponent>: {"preflop": {...}, ..., "river": {...}, "total_hands": M},
            ...
          }
        }
    A bot present in `active_bots` but with no actions maps to {}.
    """
    result = {}
    for b in active_bots:
        bt = totals.get(b)
        if not bt:
            result[b] = {}
            continue
        out = {}
        any_total = 0
        for opponent, bucket in bt.items():
            if opponent == "_hands":
                continue  # internal global hand set, emitted separately below
            opp_out = {}
            opp_total = 0
            for street in _STREETS:
                st = bucket[street]
                opp_out[street] = {
                    "total": st["total"],
                    "fold": st["fold"],
                    "call": st["call"],
                    "raise": st["raise"],
                    "check": st["check"],
                    "allin": st["allin"],
                    "fold_to_bet": st["fold_to_bet"],
                    "cbet": st["cbet"],
                    "barrel": st["barrel"],
                }
                opp_total += st["total"]
            if opp_total == 0:
                continue  # skip opponents with zero actions
            opp_out["total_hands"] = len(bucket["_hands"])
            native_tracker = _finalize_native_tracker(
                bucket.get("_native_tracker")
            )
            if native_tracker is not None:
                opp_out["opponent_tracker"] = native_tracker
            out[opponent] = opp_out
            any_total += opp_total
        if any_total == 0:
            result[b] = {}
            continue
        result[b] = out
    return result


# ── Backward-compatibility view (Phase 0 B) ──

def get_global_stats(stats, bot):
    """Collapse the per-opponent breakdown for `bot` into the legacy flat shape.

    Input `stats` is the value returned by `compute_all_bot_stats` (a {bot: {opponent:
    {street: {...}, total_hands}}} mapping). This function sums every opponent the bot
    faced into a single per-street aggregate, restoring the pre-Phase-0 shape that
    downstream readers (elo_daemon save_cycle, tool_planning Master-prompt injection,
    orchestrator_context) depend on:

        {
          "preflop": {"total", "fold", "call", "raise", "check", "allin",
                      "fold_to_bet", "cbet", "barrel"},
          "flop": {...}, "turn": {...}, "river": {...},
          "total_hands": M,            # distinct hands vs ALL opponents (set union)
          "aggression_factor": {       # derived: raise_count / call_count per street
              "preflop": <float|None>, "flop": ..., "turn": ..., "river": ...
          },
        }

    Bots absent from `stats` (or with no actions) yield {}.
    """
    per_opp = stats.get(bot) if isinstance(stats, dict) else None
    if not per_opp:
        return {}

    agg = {street: _empty_street_stats() for street in _STREETS}
    # total_hands is a distinct-hand count per opponent. Each replay file is a
    # distinct pair and hand numbers reset per file, so cross-opponent collisions
    # are rare; summing per-opponent totals is a close approximation and matches
    # pre-Phase-0 behavior (which also summed across all files).
    total_hands = 0
    opponent_trackers = {}
    for opponent, opp_stats in per_opp.items():
        for street in _STREETS:
            st = opp_stats.get(street)
            if not st:
                continue
            dst = agg[street]
            for k in ("total", "fold", "call", "raise", "check", "allin",
                      "fold_to_bet", "cbet", "barrel"):
                dst[k] += st.get(k, 0)
        total_hands += opp_stats.get("total_hands", 0)
        tracker = opp_stats.get("opponent_tracker")
        if isinstance(tracker, dict):
            opponent_trackers[opponent] = tracker

    out = {}
    any_total = 0
    af = {}
    for street in _STREETS:
        st = agg[street]
        any_total += st["total"]
        out[street] = dict(st)  # shallow copy of the flat street stats
        calls = st["call"]
        raises = st["raise"]
        af[street] = (raises / calls) if calls > 0 else None
    if any_total == 0:
        return {}
    out["total_hands"] = total_hands
    out["aggression_factor"] = af
    if opponent_trackers:
        out["opponent_trackers"] = opponent_trackers
    return out


# ── Incremental computation (Phase 0 D) ──

def _replay_etag(replays_dir):
    """Build {filename: "mtime:size"} for every *.json in the replay dir.

    This is the etag used to detect which replays changed since the last compute pass.
    mtime+size is sufficient because replay files are write-once (elo_daemon creates a
    fresh JSON per match and never edits it). A file is 'new or changed' iff its etag
    string differs from the cached value.
    """
    etag = {}
    try:
        for entry in os.listdir(replays_dir):
            if not entry.endswith(".json"):
                continue
            # Exclude the etag cache itself (it lives in the replay dir and is
            # rewritten every compute pass — including it would make the fingerprint
            # set never converge, permanently invalidating incremental mode) and
            # other dotfiles.
            if entry == _ETAG_FILENAME or entry.startswith("."):
                continue
            fp = replays_dir / entry
            try:
                st = fp.stat()
            except OSError:
                continue
            etag[entry] = f"{int(st.st_mtime)}:{st.st_size}"
    except OSError:
        pass
    return etag


def _load_etag_cache(etag_path):
    if not etag_path.exists():
        return {}
    try:
        with open(etag_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data["files"]
    except Exception:
        pass
    return {}


def _save_etag_cache(etag_path, files_etag):
    try:
        with open(etag_path, "w", encoding="utf-8") as f:
            json.dump({"files": files_etag}, f)
    except Exception:
        pass


def _load_native_contribution_cache(etag_path):
    """Load compact per-replay tracker rows used to rebuild totals in O(files)."""
    if not etag_path.exists():
        return {}
    try:
        with open(etag_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if int(data.get("schema_version", 0) or 0) != 2:
            return {}
        rows = data.get("native_contributions")
        return rows if isinstance(rows, dict) else {}
    except Exception:
        return {}


def _save_native_contribution_cache(etag_path, files_etag, contributions):
    """Durably cache bounded tracker snapshots, never complete replay payloads."""
    temporary = None
    try:
        etag_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 2,
            "files": files_etag,
            "native_contributions": contributions,
        }
        temporary = etag_path.with_name(f".{etag_path.name}.tmp-{os.getpid()}")
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, etag_path)
    except Exception:
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except Exception:
            pass


def _apply_actions_to_totals(totals, bot_set, actions):
    """Fold an action list into the (mutable) totals structure, filtered to bot_set."""
    for action in actions:
        bot = action["bot"]
        if bot not in bot_set:
            continue
        _aggregate_action(totals, bot, action)


def compute_all_bot_stats(
    active_bots,
    replays_dir,
    force_full=False,
    etag_path=None,
    *,
    allowed_replay_ids=None,
):
    """Compute aggregate per-opponent per-street action statistics for ALL active bots.

    Output shape (per bot with at least one action) — see module docstring:

        {
          <bot>: {
            <opponent>: {
              "preflop": {"total","fold","call","raise","check","allin",
                          "fold_to_bet","cbet","barrel"},
              "flop": {...}, "turn": {...}, "river": {...},
              "total_hands": M,
            },
            ...
          }
        }

    Bots with no recorded actions map to an empty dict {}.

    `force_full=True` bypasses the incremental cache and rescans every replay from
    scratch (use this for periodic consistency checks / self-healing).

    Incremental mode (`force_full=False`, default): an etag cache records each replay
    file's mtime+size and the replay's compact native tracker contribution. Unchanged
    native replays are not opened or parsed; their small counter snapshots are reapplied
    to rebuild deterministic totals. New/changed replays are parsed once and replace
    their cache row. Legacy JSON action logs remain a correctness fallback and are not
    stored wholesale in this cache.

    `etag_path` overrides the cache file location (defaults to
    `<replays_dir>/.stats_etag.json`). Set to None explicitly to disable caching.

    The on-disk `bot_action_stats.json` that consumers read is written by callers;
    callers SHOULD pass it through `get_global_stats` to get the legacy flat shape.
    """
    replays_dir = Path(replays_dir)
    if not replays_dir.exists():
        return {b: {} for b in active_bots}

    bot_set = set(active_bots)
    totals = {b: _new_zero_totals() for b in active_bots}

    # Determine the etag cache path.
    if etag_path is None:
        etag_path = replays_dir / _ETAG_FILENAME
    else:
        etag_path = Path(etag_path)

    # Build the current file fingerprint map.
    cur_etag = _replay_etag(replays_dir)
    if allowed_replay_ids is not None:
        allowed = {str(value) for value in allowed_replay_ids}
        cur_etag = {
            filename: etag
            for filename, etag in cur_etag.items()
            if filename in allowed
        }
    files_to_scan = set(cur_etag.keys())

    use_incremental = (not force_full) and etag_path is not None
    cached_etag = _load_etag_cache(etag_path) if use_incremental else {}
    cached_native = (
        _load_native_contribution_cache(etag_path) if use_incremental else {}
    )
    next_native_cache = {}

    if use_incremental:
        # Only files whose fingerprint changed (or are new) need JSON parsing.
        changed = {
            name for name in files_to_scan
            if cached_etag.get(name) != cur_etag[name]
        }
        nothing_changed = (not changed) and (
            set(cached_etag.keys()) == files_to_scan
        )
        if nothing_changed:
            pass

    # A set is intentionally used above for membership math, but replay order
    # must be stable because native tracker snapshots expose one latest context.
    for entry in sorted(files_to_scan):
        cached = cached_native.get(entry)
        if (
            isinstance(cached, dict)
            and cached.get("etag") == cur_etag.get(entry)
            and isinstance(cached.get("rows"), list)
            and _apply_native_tracker_rows(
                totals,
                bot_set,
                rows=cached["rows"],
            )
        ):
            next_native_cache[entry] = cached
            continue
        filepath = replays_dir / entry
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                replay_json = json.load(f)
        except Exception:
            continue
        native_rows = list(_native_tracker_rows(replay_json) or [])
        if native_rows:
            _apply_native_tracker_rows(
                totals,
                bot_set,
                rows=native_rows,
            )
            next_native_cache[entry] = {
                "etag": cur_etag[entry],
                "rows": native_rows,
            }
        else:
            actions = extract_actions_from_replay(replay_json)
            _apply_actions_to_totals(totals, bot_set, actions)

    # Persist the fresh etag so the next call can diff.
    if use_incremental:
        _save_native_contribution_cache(
            etag_path,
            cur_etag,
            next_native_cache,
        )

    return _finalize_totals(totals, active_bots)


def compute_bot_action_stats(bot_name, replays_dir):
    """Compute aggregate action statistics for a single bot, FLATTENED across opponents.

    Returns the LEGACY flat shape (identical to `get_global_stats`):

        {
          "preflop": {"total","fold","call","raise","check","allin",
                      "fold_to_bet","cbet","barrel"},
          "flop": {...}, "turn": {...}, "river": {...},
          "total_hands": M,
          "aggression_factor": {"preflop": <float|None>, ...},
        }

    This keeps single-bot callers (and existing tests) on the pre-Phase-0 flat shape.
    For the per-opponent breakdown use `compute_all_bot_stats([bot], dir)[bot]` directly.
    """
    per_opp = compute_all_bot_stats([bot_name], replays_dir).get(bot_name, {})
    return get_global_stats({bot_name: per_opp}, bot_name)
