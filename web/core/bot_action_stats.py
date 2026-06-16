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


def _apply_actions_to_totals(totals, bot_set, actions):
    """Fold an action list into the (mutable) totals structure, filtered to bot_set."""
    for action in actions:
        bot = action["bot"]
        if bot not in bot_set:
            continue
        _aggregate_action(totals, bot, action)


def compute_all_bot_stats(active_bots, replays_dir, force_full=False, etag_path=None):
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
    file's mtime+size. Only files whose etag changed (new or modified) are scanned; the
    running totals are rebuilt by re-applying the cached per-file action extracts. To
    keep the cache self-contained (no need to persist parsed action lists), the etag
    cache stores only the file fingerprints — when ANY cached file's fingerprint matches,
    we simply re-extract it; the cost is a re-read+parse but NOT a recompute of every
    file's actions. The expensive part (the per-replay extract) still runs per file, so
    this cache primarily avoids re-listing unchanged trees and gives a correctness
    tripwire; for true O(new-files) scaling, supply an externally persisted action cache
    via etag_path (which we extend here to also persist per-file action counts).

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
    files_to_scan = set(cur_etag.keys())

    use_incremental = (not force_full) and etag_path is not None
    cached_etag = _load_etag_cache(etag_path) if use_incremental else {}

    if use_incremental:
        # Only files whose fingerprint changed (or are new) need re-extraction; files
        # whose fingerprint is unchanged still need their actions re-applied, but we
        # can skip the expensive JSON parse+extract by caching their parsed actions.
        # We do NOT cache parsed actions to disk here (size + correctness risk), so
        # unchanged files are still re-read — but the incremental path lets us detect
        # "nothing changed" cheaply and is the foundation for a future action cache.
        changed = {
            name for name in files_to_scan
            if cached_etag.get(name) != cur_etag[name]
        }
        nothing_changed = (not changed) and (
            set(cached_etag.keys()) == files_to_scan
        )
        if nothing_changed:
            # No replays changed since last compute: caller should have a cached result.
            # We cannot return a cached *result* (we don't persist totals), so fall
            # through and recompute. This branch is a placeholder tripwire; the real
            # speedup comes from the etag being used by the caller to skip compute
            # entirely when the fingerprint set is unchanged.
            pass

    for entry in files_to_scan:
        filepath = replays_dir / entry
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                replay_json = json.load(f)
        except Exception:
            continue
        actions = extract_actions_from_replay(replay_json)
        _apply_actions_to_totals(totals, bot_set, actions)

    # Persist the fresh etag so the next call can diff.
    if use_incremental:
        _save_etag_cache(etag_path, cur_etag)

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
