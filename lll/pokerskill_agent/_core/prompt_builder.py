"""Pure Python fallback prompt builder for local runs.

This module provides the same API that the compiled extension exposes:
- SYSTEM_PROMPT
- build_preflop_prompt(...)
- build_prompt(...)

It is intentionally lightweight and protocol-safe, used when compiled binaries
are unavailable in local environments (e.g. Windows).
"""

from __future__ import annotations

from typing import Iterable, Optional


SYSTEM_PROMPT = (
    "You are a heads-up no-limit Texas hold'em decision agent. "
    "Return exactly one poker action using the provided tool. "
    "Follow legal actions and betting bounds strictly."
)


def _fmt_actions(legal_actions: Iterable[str]) -> str:
    mapping = {"f": "fold", "k": "check", "c": "call", "b": "bet/raise"}
    parts = [f"{a}({mapping.get(a, a)})" for a in legal_actions]
    return ", ".join(parts) if parts else "(none)"


def _fmt_raise_bounds(raise_min: Optional[float], raise_max: Optional[float]) -> str:
    if raise_min is None or raise_max is None:
        return "N/A"
    return f"[{raise_min:.2f}, {raise_max:.2f}] BB"


def _common_prompt(
    *,
    hand_id: int,
    street: str,
    hero_hole_cards: str,
    board_cards: str,
    pot: float,
    total_pot: float,
    hero_stack: float,
    villain_stack: float,
    hero_position: str,
    legal_actions: list[str],
    raise_min: Optional[float],
    raise_max: Optional[float],
    action_history: list[str],
    use_skills: bool,
) -> str:
    return (
        f"Hand #{hand_id}\n"
        f"Street: {street}\n"
        f"Hero position: {hero_position}\n"
        f"Hero hole cards: {hero_hole_cards}\n"
        f"Board cards: {board_cards or '(none)'}\n"
        f"Pot: {pot:.2f} BB (total_pot={total_pot:.2f} BB)\n"
        f"Stacks: hero={hero_stack:.2f} BB, villain={villain_stack:.2f} BB\n"
        f"Action history: {action_history if action_history else '[]'}\n"
        f"Legal actions: {_fmt_actions(legal_actions)}\n"
        f"Raise bounds (if action=b): {_fmt_raise_bounds(raise_min, raise_max)}\n"
        f"PokerSkill mode: {'ON' if use_skills else 'OFF'}\n\n"
        "Decision requirements:\n"
        "1) Choose only from legal actions.\n"
        "2) If action='b', amount must be within raise bounds.\n"
        "3) Prefer conservative legal action when uncertain.\n"
        "4) Return the decision via tool call."
    )


def build_preflop_prompt(
    *,
    hand_id: int,
    hero_hole_cards: str,
    hero_position: str,
    hero_stack: float,
    villain_stack: float,
    pot: float,
    total_pot: float,
    legal_actions: list[str],
    raise_min: Optional[float],
    raise_max: Optional[float],
    action_history: list[str],
    use_skills: bool = True,
) -> str:
    return _common_prompt(
        hand_id=hand_id,
        street="preflop",
        hero_hole_cards=hero_hole_cards,
        board_cards="",
        pot=pot,
        total_pot=total_pot,
        hero_stack=hero_stack,
        villain_stack=villain_stack,
        hero_position=hero_position,
        legal_actions=legal_actions,
        raise_min=raise_min,
        raise_max=raise_max,
        action_history=action_history,
        use_skills=use_skills,
    )


def build_prompt(
    *,
    hand_id: int,
    street: str,
    hero_hole_cards: str,
    board_cards: str,
    pot: float,
    total_pot: float,
    hero_stack: float,
    villain_stack: float,
    hero_position: str,
    legal_actions: list[str],
    raise_min: Optional[float],
    raise_max: Optional[float],
    action_history: list[str],
    use_skills: bool = True,
) -> str:
    return _common_prompt(
        hand_id=hand_id,
        street=street,
        hero_hole_cards=hero_hole_cards,
        board_cards=board_cards,
        pot=pot,
        total_pot=total_pot,
        hero_stack=hero_stack,
        villain_stack=villain_stack,
        hero_position=hero_position,
        legal_actions=legal_actions,
        raise_min=raise_min,
        raise_max=raise_max,
        action_history=action_history,
        use_skills=use_skills,
    )

