"""Bridge module: exposes generate_prompt() from compiled Cython core."""

from .prompt_builder import build_prompt, build_preflop_prompt, SYSTEM_PROMPT


def generate_prompt(state: dict) -> dict:
    """Generate a structured HUNL poker prompt from game state.

    Args:
        state: Validated game state dictionary.

    Returns:
        {"system_prompt": str, "user_prompt": str}
    """
    if state["street"] == "preflop":
        user_prompt = build_preflop_prompt(
            hand_id=state["hand_id"],
            hero_hole_cards=state["hero_hole_cards"],
            hero_position=state["hero_position"],
            hero_stack=state["hero_stack"],
            villain_stack=state["villain_stack"],
            pot=state["pot"],
            total_pot=state["total_pot"],
            legal_actions=state["legal_actions"],
            raise_min=state.get("raise_min"),
            raise_max=state.get("raise_max"),
            action_history=state["action_history"],
            use_skills=state.get("use_skills", True),
        )
    else:
        user_prompt = build_prompt(
            hand_id=state["hand_id"],
            street=state["street"],
            hero_hole_cards=state["hero_hole_cards"],
            board_cards=state["board_cards"],
            pot=state["pot"],
            total_pot=state["total_pot"],
            hero_stack=state["hero_stack"],
            villain_stack=state["villain_stack"],
            hero_position=state["hero_position"],
            legal_actions=state["legal_actions"],
            raise_min=state.get("raise_min"),
            raise_max=state.get("raise_max"),
            action_history=state["action_history"],
            use_skills=state.get("use_skills", True),
        )

    return {
        "system_prompt": SYSTEM_PROMPT,
        "user_prompt": user_prompt,
    }
