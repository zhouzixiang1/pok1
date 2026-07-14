"""Protocol-boundary checks for legacy Botzone JSON bot entries."""

from __future__ import annotations

import ast
from pathlib import Path


_TCP_ACTION_PREFIXES = ("raise ", "fold", "call", "check", "allin", "bet ")
_ACTION_RETURN_ENTRYPOINTS = {
    "act",
    "choose_action",
    "decide",
    "decide_action",
    "get_action",
    "main",
    "make_decision",
    "respond",
}


def _literal_text(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip().lower()
    return None


def _is_stderr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "stderr"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _print_targets_stdout(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg == "file" and _is_stderr(keyword.value):
            return False
    return True


def _parent_links(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]


def _enclosing_function(node: ast.AST) -> str | None:
    cur = node
    while hasattr(cur, "parent"):
        cur = cur.parent  # type: ignore[attr-defined]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
    return None


def _return_can_escape_as_bot_action(rel: str, function_name: str | None) -> bool:
    """True when a literal TCP-action return is plausibly the bot response.

    Evolved bots commonly use string labels inside private helper functions
    (for example preflop bucket labels such as "call"/"raise") and later map
    them to Botzone integer responses. Those internal labels are legal. The
    protected contract should only block direct TCP text at response-producing
    boundaries.
    """
    if function_name is None:
        return rel == "main.py"
    return function_name in _ACTION_RETURN_ENTRYPOINTS


def check_bot_protocol_contract(bot_dir: Path) -> list[str]:
    """Return blocking contract violations for legacy JSON bot files.

    In national_native mode, ``national_bot.py`` is the direct TCP entrypoint
    and is validated by ``national_native.check_native_contract`` instead. This
    legacy contract still protects ``main.py`` and strategy modules from
    accidentally turning the JSON/local entry into TCP stdout text.
    """
    bot_dir = Path(bot_dir)
    violations: list[str] = []
    main_py = bot_dir / "main.py"
    if not main_py.exists():
        return []

    for path in sorted(bot_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.name == "national_bot.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        _parent_links(tree)
        rel = path.relative_to(bot_dir).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                if not node.args:
                    continue
                if not _print_targets_stdout(node):
                    continue
                text = _literal_text(node.args[0])
                if text and text.startswith(_TCP_ACTION_PREFIXES):
                    violations.append(f"{rel}: print() emits TCP action text {text!r}; output must be JSON response int")
            if isinstance(node, ast.Return):
                text = _literal_text(node.value)
                function_name = _enclosing_function(node)
                if (
                    text
                    and text.startswith(_TCP_ACTION_PREFIXES)
                    and _return_can_escape_as_bot_action(rel, function_name)
                ):
                    location = f"{function_name}()" if function_name else "module"
                    violations.append(
                        f"{rel}: {location} return emits TCP action text {text!r}; "
                        "use Botzone integer response"
                    )
    return violations
