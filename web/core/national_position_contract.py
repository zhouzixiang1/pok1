"""Static enforcement of the typed national heads-up position contract."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable


SYSTEM_OWNED_FILES = frozenset({"national_bot.py", "precompute.py"})
RETIRED_POSITION_IDENTIFIERS = frozenset({
    "dealer",
    "dealer_id",
    "is_bb",
    "is_sb",
    "my_is_bb",
    "my_is_sb",
    "next_player",
})
RETIRED_CONTEXT_KEYS = frozenset({
    "dealer",
    "dealer_id",
    "is_bb",
    "is_sb",
    "my_is_bb",
    "my_is_sb",
})
POSITION_VALUES = frozenset({"small_blind", "big_blind"})
_POSTFLOP_FIRST_NAMES = (
    "acts_first_postflop",
    "first_to_act_postflop",
    "postflop_oop",
    "is_oop",
)
_POSTFLOP_IN_POSITION_NAMES = (
    "hero_in_position_postflop",
    "in_position_postflop",
    "postflop_ip",
    "is_ip",
)
_CONTRADICTORY_TEXT = {
    "small blind acts first postflop": (
        "small blind is in position postflop; use "
        "decision_context.hand.acts_first_postflop"
    ),
    "big blind is in position postflop": (
        "big blind acts first postflop; use "
        "decision_context.line.hero_in_position_postflop"
    ),
    "small_blind acts_first_postflop": (
        "acts_first_postflop is true only for big_blind"
    ),
    "big_blind hero_in_position_postflop": (
        "hero_in_position_postflop is true only for small_blind"
    ),
}


def _string_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _context_key_access(node: ast.AST) -> str | None:
    if isinstance(node, ast.Subscript):
        if isinstance(node.value, ast.Name) and node.value.id == "context":
            return _string_key(node.slice)
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "context"
            and node.args
        ):
            return _string_key(node.args[0])
    return None


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> Iterable[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        for item in ast.walk(target):
            if isinstance(item, ast.Name):
                yield item.id.lower()


def _comparison_position_value(node: ast.AST | None) -> str | None:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return None
    if not isinstance(node.ops[0], (ast.Eq, ast.Is)) or len(node.comparators) != 1:
        return None
    values = (node.left, node.comparators[0])
    for value in values:
        literal = _string_key(value)
        if literal in POSITION_VALUES:
            return literal
    return None


def _typed_position_guidance() -> str:
    return (
        "read decision_context.hand.position/acts_first_postflop or the "
        "system-derived decision_context.line position flags; do not reconstruct seats"
    )


class _PositionVisitor(ast.NodeVisitor):
    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.errors: list[str] = []

    def _error(self, node: ast.AST, message: str) -> None:
        self.errors.append(
            f"{self.relative}:{getattr(node, 'lineno', 1)}: {message}"
        )

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.lower() in RETIRED_POSITION_IDENTIFIERS:
            self._error(
                node,
                f"retired position identifier {node.id!r}; {_typed_position_guidance()}",
            )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        key = _context_key_access(node)
        if key in RETIRED_CONTEXT_KEYS:
            self._error(
                node,
                f"retired decision_context key {key!r}; {_typed_position_guidance()}",
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        key = _context_key_access(node)
        if key in RETIRED_CONTEXT_KEYS:
            self._error(
                node,
                f"retired decision_context key {key!r}; {_typed_position_guidance()}",
            )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_position_assignment(node, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._check_position_assignment(node, node.value)
        self.generic_visit(node)

    def _check_position_assignment(
        self,
        node: ast.Assign | ast.AnnAssign,
        value: ast.AST | None,
    ) -> None:
        compared = _comparison_position_value(value)
        if compared is None:
            return
        for name in _assignment_names(node):
            if compared == "small_blind" and any(
                marker in name for marker in _POSTFLOP_FIRST_NAMES
            ):
                self._error(
                    node,
                    f"{name!r} cannot be derived from small_blind; read "
                    "decision_context.hand.acts_first_postflop",
                )
            if compared == "big_blind" and any(
                marker in name for marker in _POSTFLOP_IN_POSITION_NAMES
            ):
                self._error(
                    node,
                    f"{name!r} cannot be derived from big_blind; read "
                    "decision_context.line.hero_in_position_postflop",
                )

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            lowered = " ".join(node.value.lower().split())
            for phrase, guidance in _CONTRADICTORY_TEXT.items():
                if phrase in lowered:
                    self._error(node, f"contradictory position text: {guidance}")
        self.generic_visit(node)


def detect_position_semantics_errors(bot_dir: str | Path) -> list[str]:
    """Reject position reconstruction outside the system-owned context builder.

    Candidate policy reads the closed typed fields.  The authoritative runtime
    states that ``big_blind`` acts first postflop and ``small_blind`` is in
    position; candidate code must not recreate those facts from seat/dealer IDs.
    """

    root = Path(bot_dir)
    errors: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or path.name in SYSTEM_OWNED_FILES:
            continue
        try:
            relative = path.relative_to(root).as_posix()
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=relative, type_comments=True)
        except (OSError, SyntaxError, ValueError) as exc:
            errors.append(
                f"{path.name}:1: position contract could not parse candidate: "
                f"{type(exc).__name__}"
            )
            continue
        visitor = _PositionVisitor(relative)
        visitor.visit(tree)
        errors.extend(visitor.errors)
    return list(dict.fromkeys(errors))[:20]


__all__ = ["detect_position_semantics_errors"]
