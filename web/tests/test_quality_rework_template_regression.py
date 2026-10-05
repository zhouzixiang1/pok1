"""Regression: quality-rework preservation templates must survive .format(next_v=...).

v511 (2026-10-05) crashed the orchestrator with KeyError('bot_name(next_v)')
because tool_planning_quality_rework.py carried plain-string templates with a
``{bot_name(next_v)}`` placeholder while the contract renderers format them with
only ``next_v`` (str.format treats ``bot_name(next_v)`` as a literal key). This
test scans every string literal in that module for brace placeholders and
asserts each one renders cleanly with the kwargs the renderers actually pass,
so a future template edit cannot reintroduce the crash on a path only reached
deep inside the pipeline.
"""

import ast
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "core" / "tool_planning_quality_rework.py"

# The kwargs the contract renderers pass (tool_planning_quality_contracts.py
# formats preservation/method with at least next_v; source_v appears in sibling
# renderers).
FORMAT_KWARGS = {"next_v": 511, "source_v": 88}


def _string_literals(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node


def _template_literals():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    return [
        (node.lineno, node.value)
        for node in _string_literals(tree)
        if "{" in node.value and "}" in node.value and not node.value.startswith(("Import ", "from "))
    ]


def test_every_braced_literal_formats_with_renderer_kwargs():
    templates = _template_literals()
    assert templates, "expected to find preservation/method templates in the module"
    failures = []
    for lineno, value in templates:
        try:
            value.format(**FORMAT_KWARGS)
        except (KeyError, IndexError, ValueError) as exc:
            failures.append(f"line {lineno}: {exc!r} in {value[:80]!r}")
    assert not failures, "templates not renderable with renderer kwargs:\n" + "\n".join(failures)


def test_no_function_call_placeholders_in_templates():
    for lineno, value in _template_literals():
        assert "{" not in value.replace("{{", "").replace("}}", "") or _no_call_placeholder(
            value
        ), f"line {lineno}: function-call placeholder found in {value[:80]!r}"


def _no_call_placeholder(value: str) -> bool:
    # Disallow {identifier( ... } — str.format can never resolve a call, it
    # treats the whole thing as a literal key (the v511 crash shape).
    import re

    return re.search(r"\{[A-Za-z_][A-Za-z0-9_]*\(", value) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
