"""Static contract checks for national heads-up position semantics."""

from __future__ import annotations

import re
from pathlib import Path


POSITION_SEMANTICS_PATTERNS = {
    "dealer==bb": "dealer is SB in heads-up; BB is 1 - dealer_id",
    "dealer == bb": "dealer is SB in heads-up; BB is 1 - dealer_id",
    "dealer is bb": "dealer is SB in heads-up; BB is 1 - dealer_id",
    "dealer=bb": "dealer is SB in heads-up; BB is 1 - dealer_id",
    "sb acts first every street": "SB acts first preflop only; BB acts first postflop",
    "sb first postflop": "BB acts first postflop",
    "bb postflop in-position": "SB is in position postflop; BB acts first",
    "flop_sb_act_first": "decision templates must use BB-first postflop semantics",
}

SB_FROM_DEALER_NEXT_PLAYER_RE = re.compile(
    r"\b(?P<var>sb|[a-z_][a-z0-9_]*_sb)\s*=\s*next_player\(\s*"
    r"(?P<dealer>[a-z_][a-z0-9_]*)\s*,\s*1\s*\)"
)
BB_FROM_DEALER_NEXT_PLAYER_RE = re.compile(
    r"\b(?P<var>bb|[a-z_][a-z0-9_]*_bb)\s*=\s*next_player\(\s*"
    r"(?P<dealer>[a-z_][a-z0-9_]*)\s*,\s*2\s*\)"
)
PY_DEF_RE = re.compile(r"^(?P<indent>\s*)def\s+(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)\s*\(")
POSTFLOP_OOP_NAME_TOKENS = ("postflop", "flop", "turn", "river")


def _is_postflop_oop_helper(name: str) -> bool:
    lowered = name.lower()
    return "oop" in lowered and any(token in lowered for token in POSTFLOP_OOP_NAME_TOKENS)


def detect_position_semantics_errors(bot_dir: str | Path) -> list[str]:
    """Detect old heads-up position assumptions in candidate bot code.

    Authoritative convention: ``dealer_id`` is SB, BB is ``1 - dealer_id`` in
    heads-up, BB acts first on flop/turn/river, and SB is in position postflop.
    """

    errors: list[str] = []
    root = Path(bot_dir)
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        current_postflop_oop_func = ""
        current_func_indent = -1
        for lineno, line in enumerate(lines, 1):
            def_match = PY_DEF_RE.match(line)
            if def_match:
                current_postflop_oop_func = (
                    def_match.group("name").lower()
                    if _is_postflop_oop_helper(def_match.group("name")) else ""
                )
                current_func_indent = len(def_match.group("indent")) if current_postflop_oop_func else -1
            elif current_postflop_oop_func and line.strip():
                line_indent = len(line) - len(line.lstrip())
                stripped = line.lstrip()
                if line_indent <= current_func_indent and not stripped.startswith(")"):
                    current_postflop_oop_func = ""
                    current_func_indent = -1

            rel = path.relative_to(root)
            lowered = line.lower()
            for pattern, explanation in POSITION_SEMANTICS_PATTERNS.items():
                if pattern in lowered:
                    errors.append(f"{rel}:{lineno}: {explanation} ({pattern})")
            if current_postflop_oop_func and "my_is_sb" in lowered and "do not" not in lowered:
                errors.append(
                    f"{rel}:{lineno}: postflop OOP helper {current_postflop_oop_func} "
                    "must key on my_is_bb/BB, not my_is_sb/SB"
                )

            sb_match = SB_FROM_DEALER_NEXT_PLAYER_RE.search(lowered)
            if sb_match:
                dealer_var = sb_match.group("dealer")
                if "dealer" in dealer_var:
                    if sb_match.group("var") == "sb" and dealer_var == "dealer_id":
                        errors.append(f"{rel}:{lineno}: SB must be dealer_id, not next_player(dealer_id, 1)")
                    else:
                        errors.append(
                            f"{rel}:{lineno}: {sb_match.group('var')} must be {dealer_var}, "
                            f"not next_player({dealer_var}, 1)"
                        )

            bb_match = BB_FROM_DEALER_NEXT_PLAYER_RE.search(lowered)
            if bb_match:
                dealer_var = bb_match.group("dealer")
                if "dealer" in dealer_var:
                    if bb_match.group("var") == "bb" and dealer_var == "dealer_id":
                        errors.append(f"{rel}:{lineno}: BB must be 1 - dealer_id, not next_player(dealer_id, 2)")
                    else:
                        errors.append(
                            f"{rel}:{lineno}: {bb_match.group('var')} must be 1 - {dealer_var}, "
                            f"not next_player({dealer_var}, 2)"
                        )
    return errors[:20]
