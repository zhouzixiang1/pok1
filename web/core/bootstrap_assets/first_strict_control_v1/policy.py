"""Non-rating passive control for the first national policy publication."""

from __future__ import annotations


def get_baseline_decision(context):
    legal = context.get("legal", {})
    betting = context.get("betting", {})
    kinds = set(legal.get("policy_kinds", ()))
    if "pass" in kinds:
        return {"kind": "pass"}
    if betting.get("to_call", 0) and "fold" in kinds:
        return {"kind": "fold"}
    if "allin" in kinds:
        return {"kind": "allin"}
    return {"kind": "fold"}


def iter_decisions(context, baseline, deadline):
    if context.get("deadline") and deadline < 0:
        yield baseline
    return
