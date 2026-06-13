"""
Bot action statistics extraction from replay files.

Authoritative action source: RESPONSE log entries.

A replay JSON (produced by elo_daemon.save_match_replay via engine/battle.mirror_battle)
has the shape:

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
"""

import json
import os
from pathlib import Path


# display.round integer -> street name
_STREET_BY_ROUND = {0: "preflop", 1: "flop", 2: "turn", 3: "river"}
_STREETS = ("preflop", "flop", "turn", "river")


def _empty_street_stats():
    return {"total": 0, "fold": 0, "call": 0, "raise": 0, "check": 0, "allin": 0}


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

    Returns a list of dicts: {"bot": <name>, "street": <str|None>, "action": <class>, "hand": <int|None>}.
    `street` is None only if the preceding request's display.round was missing/unrecognized.
    `hand` is the 0-indexed hand number (from display.matchdata.hand), for total_hands counting.
    `action` is one of fold/call/check/raise/allin (allin is also semantically a raise, but
    here it is its own class; the aggregator double-counts it into the raise key as well).
    """
    if isinstance(replay_json, (str, bytes)):
        replay_json = json.loads(replay_json)

    bot0 = replay_json.get("bot0")
    bot1 = replay_json.get("bot1")
    if not bot0 or not bot1:
        return []
    pid_to_bot = {0: bot0, 1: bot1}

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
                action_class = _classify_response(resp, round_player_bet, int(pid_str))
                if action_class is None:
                    continue
                actions.append({
                    "bot": pid_to_bot[int(pid_str)],
                    "street": street,
                    "action": action_class,
                    "hand": hand,
                })
                break  # a response entry carries exactly one player id
    return actions


# Backward-compat alias for any older import name.
def extract_hands_from_replay(replay_json):
    """DEPRECATED alias. Returns the raw action list (kept for import compatibility)."""
    return extract_actions_from_replay(replay_json)


def _new_zero_totals():
    """Per-bot nested counters: {street: {total,fold,call,raise,check,allin}, plus hands set."""
    return {
        "preflop": _empty_street_stats(),
        "flop": _empty_street_stats(),
        "turn": _empty_street_stats(),
        "river": _empty_street_stats(),
        "_hands": set(),  # distinct hand numbers this bot acted in
    }


def _aggregate_action(totals, bot, action):
    """Increment the per-bot per-street counters for one action.

    `allin` is counted in BOTH the allin key AND the raise key: an all-in is semantically a
    raise/bet, and the readers (tool_planning.py / orchestrator_context.py) report raise as a
    fraction of total actions on a street. Double-counting allin into raise keeps that
    fraction meaningful while still surfacing the dedicated allin frequency.
    """
    street = action["street"]
    cls = action["action"]
    bt = totals[bot]
    # Actions on an unrecognized street are skipped (no bucket to put them in).
    if street not in _STREETS:
        return
    st = bt[street]
    st["total"] += 1
    if cls == "fold":
        st["fold"] += 1
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
    if action.get("hand") is not None:
        bt["_hands"].add(action["hand"])


def _finalize_totals(totals, active_bots):
    """Convert raw counters into the output shape, dropping bots with no actions."""
    result = {}
    for b in active_bots:
        bt = totals.get(b)
        if not bt:
            result[b] = {}
            continue
        out = {}
        any_total = 0
        for street in _STREETS:
            st = bt[street]
            # Emit a fresh dict without internal keys; keep zero streets (readers guard on total>0).
            out[street] = {
                "total": st["total"],
                "fold": st["fold"],
                "call": st["call"],
                "raise": st["raise"],
                "check": st["check"],
                "allin": st["allin"],
            }
            any_total += st["total"]
        if any_total == 0:
            result[b] = {}
            continue
        out["total_hands"] = len(bt["_hands"])
        result[b] = out
    return result


def compute_all_bot_stats(active_bots, replays_dir):
    """Compute aggregate per-street action statistics for ALL active bots in a single pass.

    Output shape (per bot with at least one action):
        {
          "preflop": {"total": N, "fold": n, "call": n, "raise": n, "check": n, "allin": n},
          "flop":    {...},
          "turn":    {...},
          "river":   {...},
          "total_hands": M,   # distinct hands this bot acted in
        }
    Bots with no recorded actions map to an empty dict {}.

    `allin` actions increment BOTH the `allin` and `raise` counters on their street.
    """
    replays_dir = Path(replays_dir)
    if not replays_dir.exists():
        return {b: {} for b in active_bots}

    bot_set = set(active_bots)
    totals = {b: _new_zero_totals() for b in active_bots}

    for entry in os.listdir(replays_dir):
        if not entry.endswith(".json"):
            continue
        filepath = replays_dir / entry
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                replay_json = json.load(f)
        except Exception:
            continue

        actions = extract_actions_from_replay(replay_json)
        for action in actions:
            bot = action["bot"]
            if bot not in bot_set:
                continue
            _aggregate_action(totals, bot, action)

    return _finalize_totals(totals, active_bots)


def compute_bot_action_stats(bot_name, replays_dir):
    """Compute aggregate action statistics for a single bot.

    Delegates to compute_all_bot_stats([bot_name], replays_dir)[bot_name] for API compat.
    """
    return compute_all_bot_stats([bot_name], replays_dir).get(bot_name, {})
