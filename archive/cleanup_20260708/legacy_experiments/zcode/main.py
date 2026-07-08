"""JSON (local-engine / Botzone) entry point.

Reads a single JSON line from stdin::

    {"requests": [...], "responses": [...], "data": ...}

Emits a single JSON line on stdout::

    {"response": <int action>, "data": <optional>}

Action integer semantics (matches engine/judge.py):
    >0  raise to this round total (raise-to-total, NOT a delta)
     0  call or check (depending on context)
    -1  fold
    -2  all-in

This module is the Botzone / local battle runner entry. For the national
TCP platform use ``python zcode/national_bot.py`` instead.
"""

from __future__ import annotations

import json
import os
import random
import sys
import traceback

# Allow ``python zcode/main.py`` and ``python -m zcode.main`` invocations.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from zcode.policy import Policy, PolicyConfig, sanitize_action
from zcode.state import reconstruct_state
from zcode.student_infer import advise_action


def _build_policy() -> Policy:
    """Construct a policy. The seed is left random (None) so that the bot
    does not become predictable across hands; persistent-mode subprocesses
    keep one RNG for the whole game which gives natural hand-to-hand
    variation while remaining deterministic per process.
    """
    cfg = PolicyConfig(seed=None)
    return Policy(cfg)


# Reuse a single policy per process (persistent subprocess in battle.py).
_POLICY: Policy | None = None


def _get_policy() -> Policy:
    global _POLICY
    if _POLICY is None:
        _POLICY = _build_policy()
    return _POLICY


def decide_action(payload: dict) -> int:
    """Compute the action integer for one ``payload``."""
    requests = payload.get("requests") or []
    if not requests:
        return 0
    req = requests[-1]
    if not isinstance(req, dict):
        return 0
    st = reconstruct_state(req)
    if st.i_folded or st.my_allin:
        # No decision to make; check/call to acknowledge.
        return 0
    pol = _get_policy()
    raw = pol.decide(st, requests=requests)
    base = sanitize_action(raw, st)
    # Advisory override: ask the behaviour-cloning student (trained on
    # claude_v279) to adjust the action when it strongly disagrees.
    try:
        base = advise_action(req, st, base)
    except Exception:
        pass
    return sanitize_action(base, st)


def _process_one_line(raw_in: str) -> int:
    """Process one JSON line and emit a response. Returns 0 on success."""
    def _try_json(s: str):
        try:
            return json.loads(s)
        except Exception:
            return None
    payload = _try_json(raw_in)
    if payload is None:
        raw_in += sys.stdin.read()
        payload = _try_json(raw_in)
    if payload is None:
        sys.stdout.write(json.dumps({"response": 0}) + "\n")
        sys.stdout.flush()
        return 0
    try:
        action = decide_action(payload)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        action = 0
    sys.stdout.write(json.dumps({"response": int(action)}))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def main() -> int:
    # Persistent subprocess loop: keep reading one JSON line per decision
    # until stdin closes (EOF). This supports both the local engine's
    # persistent mode (one process per game, many decisions) and the
    # single-shot Botzone mode (one line then EOF).
    # Lazily warm up the student model so the first decision doesn't pay
    # the full load cost inside the response deadline.
    try:
        from zcode.student_infer import get_student
        get_student()
    except Exception:
        pass

    while True:
        raw_in = sys.stdin.readline()
        if not raw_in:
            return 0  # EOF
        _process_one_line(raw_in)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
