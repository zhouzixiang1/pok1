"""Protocol-boundary checks for evolved Botzone bots."""

from __future__ import annotations

import ast
from pathlib import Path


_TCP_ACTION_PREFIXES = ("raise ", "fold", "call", "check", "allin", "bet ")


def _literal_text(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip().lower()
    return None


def check_bot_protocol_contract(bot_dir: Path) -> list[str]:
    """Return blocking contract violations for a Botzone-style bot.

    The bot may reason about poker actions in prose/strings, but it must not
    print or return national TCP wire actions directly. Deployment to TCP goes
    through sever/bot_adapter.py.
    """
    bot_dir = Path(bot_dir)
    violations: list[str] = []
    main_py = bot_dir / "main.py"
    if not main_py.exists():
        return []

    for path in sorted(bot_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(bot_dir).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                if not node.args:
                    continue
                text = _literal_text(node.args[0])
                if text and text.startswith(_TCP_ACTION_PREFIXES):
                    violations.append(f"{rel}: print() emits TCP action text {text!r}; output must be JSON response int")
            if isinstance(node, ast.Return):
                text = _literal_text(node.value)
                if text and text.startswith(_TCP_ACTION_PREFIXES):
                    violations.append(f"{rel}: return emits TCP action text {text!r}; use Botzone integer response")
    return violations
