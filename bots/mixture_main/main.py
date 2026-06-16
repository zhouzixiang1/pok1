"""Phase 4: PSRO MixtureBot — opponent-side (bot1) meta-strategy dispatcher.

This bot is a STANDARD engine subprocess bot (one process per game, line-
delimited JSON, exactly like bots/claude_v{N}/main.py). The engine has NO idea
it is a mixture — it just sees another subprocess on the bot1 side. This
preserves the 2-player Popen contract of engine/battle.py (ZERO engine changes,
ZERO bot0-contract changes) — the hard gate of Phase 4.

DESIGN (Plan (c) from the Phase 4 spec — see why (a)/(b) were rejected there):
  - On every NEW hand (detected via request["hand"]), roll the PSRO meta
    distribution to pick ONE sub-bot to play that entire hand.
  - Each DECISION line within the hand is dispatched to that sub-bot via a fresh
    subprocess.run, passing the FULL requests/responses payload (the sub-bot
    reconstructs its state from history — it is stateless across hands, relying
    on reconstruct_state(requests), NOT on a persistent `data` field).

WHY fresh-subprocess-per-decision (slow but correct):
  - Sub-bots are different bot directories (bots/claude_v{N}/) that each import
    a `state.py`, `strategy.py`, etc. Importing their modules in-process would
    cause module-name collisions + global state pollution across sub-bots. The
    only safe way to mix them is isolated subprocesses.
  - The sub-bot needs the FULL request history (it calls
    infer_remaining_hands_from_requests(requests)), so we forward the complete
    payload each time — not just the last request.
  - MixtureBot is the OPPONENT side (bot1) and runs only when PSRO is enabled
    (feature flag, default OFF), so the per-decision subprocess cost is bounded
    (n_games * n_opponents * 70 hands * decisions_per_hand per generation).

CORRECTNESS:
  - Sub-bots were verified to NOT depend on a persistent `data` field (grep
    across bots/claude_v*/main.py: zero hits). They are stateless across hands.
  - Within a single hand, the same sub-bot plays every decision (we pin it per
    hand), so the sub-bot sees a consistent self-history for that hand.

CONFIG: reads mixture_config.json (beside this main.py), shape:
  {
    "strategy_weights": {"claude_v50": 0.4, "claude_v47": 0.35, ...},
    "bot_paths":        {"claude_v50": "/abs/path/main.py", ...}
  }
Missing/invalid config -> SAFE FOLD (-1) on every decision (never crashes the
match; the engine treats MixtureBot as just a fold-bot, which is a harmless
degradation rather than a protocol violation).
"""

import json
import os
import random
import subprocess
import sys


_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mixture_config.json")
_SUBPROCESS_TIMEOUT_SEC = 60  # matches engine/battle.py per-decision timeout


def _load_config():
    """Load mixture_config.json. Returns (weights_dict, paths_dict) or (None, None)."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None, None
    weights = cfg.get("strategy_weights") if isinstance(cfg, dict) else None
    paths = cfg.get("bot_paths") if isinstance(cfg, dict) else None
    if not isinstance(weights, dict) or not isinstance(paths, dict):
        return None, None
    # Keep only entries with a resolvable main.py path + positive weight.
    clean_w = {}
    clean_p = {}
    for bot, w in weights.items():
        try:
            wf = float(w)
        except (TypeError, ValueError):
            continue
        if wf <= 0.0:
            continue
        p = paths.get(bot)
        if not p or not os.path.isfile(p):
            continue
        clean_w[bot] = wf
        clean_p[bot] = p
    if not clean_w:
        return None, None
    return clean_w, clean_p


def _weighted_choice(weights, rng):
    """Sample one key from {key: weight}. Assumes weights non-empty, all > 0."""
    total = sum(weights.values())
    r = rng.random() * total
    cum = 0.0
    last = None
    for k, w in weights.items():
        cum += w
        last = k
        if r <= cum:
            return k
    return last


def _dispatch_to_sub(sub_main_path, payload):
    """Call a sub-bot's main.py with the full payload, return (action, data).

    Returns (-1, None) on ANY failure (subprocess error, timeout, bad JSON) —
    fold is always a legal engine action, so a sub-bot hiccup degrades to a
    single fold rather than breaking the match.

    The sub-bot's returned `data` field is passed through unchanged so the
    engine's _PersistentBot persistent-state semantics are preserved (the engine
    round-trips `data` on persistent procs). Sub-bots are stateless across hands
    in practice, so this is a no-op for correctness but keeps the protocol whole.
    """
    try:
        proc = subprocess.run(
            [sys.executable, sub_main_path],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
        )
        if proc.returncode != 0:
            return -1, None
        result = json.loads(proc.stdout.strip())
        action = int(result.get("response", -1))
        data = result.get("data")
        return action, data
    except Exception:
        return -1, None


def main():
    """Read decision lines from stdin (one JSON payload per decision), dispatch
    each to the per-hand-selected sub-bot, print one JSON response per line.

    Engine protocol (engine/battle.py _PersistentBot.call): each line is a full
    payload {"requests":[...], "responses":[...], "data":?}. We read requests[-1]
    to get the current hand index, switch sub-bot on hand change, then forward
    the payload to the chosen sub-bot and emit its response.
    """
    weights, paths = _load_config()
    rng = random.Random()

    if weights is None:
        # No valid config -> safe fold on every decision (never crash the match).
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)  # consume to keep stdin drained
            except Exception:
                pass
            sys.stdout.write(json.dumps({"response": -1}) + "\n")
            sys.stdout.flush()
        return

    current_hand = None
    current_sub = None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            # Malformed input line: emit a safe fold.
            sys.stdout.write(json.dumps({"response": -1}) + "\n")
            sys.stdout.flush()
            continue

        requests = payload.get("requests") if isinstance(payload, dict) else None
        # Determine the current hand from the last request. Fall back to 0.
        hand = None
        if isinstance(requests, list) and requests:
            last_req = requests[-1]
            if isinstance(last_req, dict):
                hand = last_req.get("hand")
        if hand is None:
            hand = current_hand if current_hand is not None else 0

        # New hand -> roll the meta distribution to pick this hand's sub-bot.
        if hand != current_hand:
            current_hand = hand
            current_sub = _weighted_choice(weights, rng)

        sub_main = paths.get(current_sub)
        if not sub_main:
            sys.stdout.write(json.dumps({"response": -1}) + "\n")
            sys.stdout.flush()
            continue

        action, data = _dispatch_to_sub(sub_main, payload)
        out = {"response": int(action)}
        if data is not None:
            out["data"] = data
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
