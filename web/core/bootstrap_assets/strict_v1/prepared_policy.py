"""Fresh strict preparation baseline with no optional search.

The first Worker replaces this file's materialized ``policy.py`` with the
content-bound anytime policy.  Preparation still consumes the system-owned
169-class table so its legal fallback is informed and fast.
"""

from __future__ import annotations

import time

import precompute


def _hole_ids(context):
    cards = (context.get("cards", {}) or {}).get("hole", ())
    if len(cards) != 2:
        return ()
    try:
        result = tuple(
            precompute.card_id(int(card["suit"]), int(card["rank"]))
            for card in cards
        )
    except (KeyError, TypeError, ValueError):
        return ()
    return result if result[0] != result[1] else ()


def get_baseline_decision(context):
    """Return a safe typed baseline without reconstructing protocol state."""

    legal = context.get("legal", {}) or {}
    betting = context.get("betting", {}) or {}
    kinds = set(legal.get("policy_kinds", ()))
    try:
        to_call = max(0, int(betting.get("to_call", 0)))
        pot = max(1, int(betting.get("pot", 1)))
    except (TypeError, ValueError):
        to_call, pot = 0, 1
    hole = _hole_ids(context)
    equity = precompute.preflop_equity(*hole) if hole else 0.35
    pot_odds = to_call / float(pot + to_call)
    if to_call and equity + 0.02 < pot_odds and "fold" in kinds:
        return {"kind": "fold"}
    if "pass" in kinds:
        return {"kind": "pass"}
    if "fold" in kinds:
        return {"kind": "fold"}
    return {"kind": "allin"}


def iter_decisions(context, baseline, deadline):
    """Preparation has no search; it still honors the absolute deadline ABI."""

    del context, baseline
    if float(deadline) > time.monotonic() and False:
        yield {"kind": "fold"}
    return
