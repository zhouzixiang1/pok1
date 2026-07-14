"""Input validation for PokerSkill Agent."""


class ValidationError(Exception):
    """Raised when game state input is invalid."""
    pass


REQUIRED_FIELDS = [
    "street",
    "hero_hole_cards",
    "hero_position",
    "legal_actions",
    "action_history",
]

VALID_STREETS = {"preflop", "flop", "turn", "river"}
VALID_POSITIONS = {"BTN", "BB"}
VALID_ACTIONS = {"f", "k", "c", "b"}


def validate_game_state(raw: dict) -> dict:
    """Validate and normalize game state input.

    Args:
        raw: Raw JSON-parsed dictionary.

    Returns:
        Normalized state dictionary with defaults applied.

    Raises:
        ValidationError: If required fields are missing or invalid.
    """
    if not isinstance(raw, dict):
        raise ValidationError("Input must be a JSON object")

    for field in REQUIRED_FIELDS:
        if field not in raw:
            raise ValidationError(f"Missing required field: {field}")

    street = raw["street"]
    if street not in VALID_STREETS:
        raise ValidationError(
            f"Invalid street: '{street}'. Must be one of: {sorted(VALID_STREETS)}"
        )

    position = raw["hero_position"]
    if position not in VALID_POSITIONS:
        raise ValidationError(
            f"Invalid hero_position: '{position}'. Must be 'BTN' or 'BB'"
        )

    hole_cards = raw["hero_hole_cards"]
    if not isinstance(hole_cards, str) or len(hole_cards) != 4:
        raise ValidationError(
            f"hero_hole_cards must be a 4-character string (e.g., 'AhKd'), got: '{hole_cards}'"
        )

    board_cards = raw.get("board_cards", "")
    if street != "preflop" and not board_cards:
        raise ValidationError("board_cards is required for postflop streets")

    expected_board_len = {"preflop": 0, "flop": 6, "turn": 8, "river": 10}
    if board_cards and len(board_cards) != expected_board_len.get(street, 0):
        raise ValidationError(
            f"board_cards length {len(board_cards)} doesn't match street '{street}' "
            f"(expected {expected_board_len[street]} chars)"
        )

    legal_actions = raw["legal_actions"]
    if not isinstance(legal_actions, list) or not legal_actions:
        raise ValidationError("legal_actions must be a non-empty list")
    for a in legal_actions:
        if a not in VALID_ACTIONS:
            raise ValidationError(f"Invalid legal action: '{a}'. Must be one of: f, k, c, b")

    action_history = raw["action_history"]
    if not isinstance(action_history, list):
        raise ValidationError("action_history must be a list")

    state = {
        "hand_id": int(raw.get("hand_id", 1)),
        "street": street,
        "hero_hole_cards": hole_cards,
        "board_cards": board_cards,
        "pot": float(raw.get("pot", 2.5)),
        "total_pot": float(raw.get("total_pot", raw.get("pot", 2.5))),
        "hero_stack": float(raw.get("hero_stack", 199.0)),
        "villain_stack": float(raw.get("villain_stack", 199.0)),
        "hero_position": position,
        "legal_actions": legal_actions,
        "raise_min": float(raw["raise_min"]) if raw.get("raise_min") is not None else None,
        "raise_max": float(raw["raise_max"]) if raw.get("raise_max") is not None else None,
        "action_history": action_history,
        "use_skills": bool(raw.get("use_skills", True)),
    }

    return state
