"""Behavior-preservation contract test for the wave-6 verbatim code-move.

The 2364-line ``_execute_workers_command`` body was moved byte-for-byte from
``tool_planning_worker_durable.py`` into the new sibling
``tool_planning_worker_phases.py``. This test re-derives the externally-
observable exit-path contract from the LIVE post-move function and asserts it
matches the frozen snapshot in ``worker_exit_path_fixture``.

What "matches" means
--------------------
Three load-bearing invariants:

1. ``EXPECTED_WORKER_EXIT_COUNT == 76`` -- the same number of ``return``
   statements reach the function's caller (the nested ``rollback_rework_preparation``
   helper's 3 internal returns are excluded).
2. ``RETURN_TYPE_COUNTS`` -- the histogram of return-expression categories
   (``json_tool_result``, ``state_blocked``, ``project_durable_worker_output``,
   ``project_durable_worker_failure``, ``deferred_activity``,
   ``run_durable_effect``, ``var_return``) is unchanged.
3. ``EXPECTED_WORKER_EXIT_REASONS`` -- the set of distinct externally-observable
   identities (``error`` codes for json_tool_result exits; the category name
   for structural exits; ``<var:critic_refusal>`` for delegated payloads) is
   identical.

These three invariants are the strongest affordable behavioral guarantee for a
2364-line dispatch function without a full input/output fixture suite. They
catch: a dropped or merged exit, a renamed error code, a flipped return type,
or an inlined abandon cascade. They do NOT catch a changed branch condition
inside one exit -- that requires the per-exit input-fixture suite that is the
deferrable follow-up wave.

The test imports the live function (so it sees the actual moved code) and also
re-parses the new module's AST (so it can count return statements and classify
them by callee, exactly as the fixture's self-check does for the snapshot).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from web.tests.worker_exit_path_fixture import (
    ABANDON_EXIT_COUNT,
    ABANDON_PATHS,
    EXPECTED_ABANDON_REASONS,
    EXPECTED_WORKER_EXIT_COUNT,
    EXPECTED_WORKER_EXIT_REASONS,
    EXIT_PATHS,
    RETURN_TYPE_COUNTS,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_PHASES_REL = "web/core/tool_planning_worker_phases.py"
_DURABLE_REL = "web/core/tool_planning_worker_durable.py"
_TARGET = "_execute_workers_command"
_NESTED_NAME = "rollback_rework_preparation"


def _classify_return(node: ast.Return) -> str:
    """Classify a Return node by its value expression into a return-type category.

    Mirrors the categorization used to build the fixture's RETURN_TYPE_COUNTS.
    """
    val = node.value
    if val is None:
        return "bare_return_none"
    # Await expression: unwrap to the inner call.
    if isinstance(val, ast.Await):
        val = val.value
    # _tw._json_tool_result({...})  OR  _dur._json_tool_result({...})
    if (
        isinstance(val, ast.Call)
        and isinstance(val.func, ast.Attribute)
        and val.func.attr == "_json_tool_result"
    ):
        return "json_tool_result"
    # _tw._state_blocked(...)
    if (
        isinstance(val, ast.Call)
        and isinstance(val.func, ast.Attribute)
        and val.func.attr == "_state_blocked"
    ):
        return "state_blocked"
    # await _dur._project_durable_worker_output(...)  /  _project_durable_worker_failure
    # (post-move the callee is _dur._<name>; the fixture's category drops the
    # leading underscore to match the pre-move bare-call naming).
    if (
        isinstance(val, ast.Call)
        and isinstance(val.func, ast.Attribute)
        and val.func.attr
        in ("_project_durable_worker_output", "_project_durable_worker_failure")
    ):
        return val.func.attr.lstrip("_")
    # _dur._DeferredWorkerActivity(...)  -- after the move; pre-move it was bare
    if (
        isinstance(val, ast.Call)
        and isinstance(val.func, ast.Attribute)
        and val.func.attr == "_DeferredWorkerActivity"
    ):
        return "deferred_activity"
    # await _dur._run_durable_worker_effect(...)
    if (
        isinstance(val, ast.Call)
        and isinstance(val.func, ast.Attribute)
        and val.func.attr == "_run_durable_worker_effect"
    ):
        return "run_durable_effect"
    # Bare name: ``return recovery``  -> var_return
    if isinstance(val, ast.Name):
        return "var_return"
    return f"unknown:{type(val).__name__}"


def _collect_returns_from_module(rel_path: str):
    """Parse ``rel_path`` and return (main_returns, nested_returns) AST nodes."""
    src = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == _TARGET
        ):
            target = node
            break
    assert target is not None, f"{_TARGET} not found in {rel_path}"

    # Locate nested function defs to exclude (rollback_rework_preparation).
    nested_ranges = []
    for node in ast.walk(target):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node is not target
        ):
            nested_ranges.append((node.lineno, node.end_lineno, node.name))

    def _in_nested(line: int) -> bool:
        for s, e, _name in nested_ranges:
            if s < line <= e:
                return True
        return False

    main_returns = []
    nested_returns = []
    for node in ast.walk(target):
        if isinstance(node, ast.Return):
            if _in_nested(node.lineno):
                nested_returns.append(node)
            else:
                main_returns.append(node)
    return target, main_returns, nested_returns, nested_ranges


def _extract_json_error_identity(node: ast.Return) -> str:
    """Extract the ``error`` field value from a ``_json_tool_result({...})`` return.

    Returns the literal string value if statically resolvable, otherwise a
    category placeholder matching the fixture's conventions.
    """
    val = node.value
    if isinstance(val, ast.Await):
        val = val.value
    if not (isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute)
            and val.func.attr == "_json_tool_result"):
        return "<non-json>"
    if not val.args:
        return "<json:no-args>"
    arg = val.args[0]
    # Variable dict spread: ``_json_tool_result(critic_refusal)`` -- the
    # fixture records this as ``<var:critic_refusal>`` (the delegated
    # critic-refusal payload, 2 occurrences at the original L1525/L2701).
    if isinstance(arg, ast.Name) and arg.id == "critic_refusal":
        return "<var:critic_refusal>"
    if not isinstance(arg, ast.Dict):
        return f"<json:non-dict-arg:{type(arg).__name__}>"
    for key, value in zip(arg.keys, arg.values):
        if isinstance(key, ast.Constant) and key.value == "error":
            if isinstance(value, ast.Constant):
                return str(value.value)
            # IfExp (rollback cascade) -- fixture records both branches.
            if isinstance(value, ast.IfExp):
                left = _const_str(value.body)
                right = _const_str(value.orelse)
                return f"{left} | {right}"
            # f-string error like the circuit-breaker exit; the fixture records
            # the leading human-readable token before any interpolation.
            if isinstance(value, ast.JoinedStr):
                return _fstring_leading_token(value)
            # Variable (e.g. critic_refusal spread).
            if isinstance(value, ast.Name):
                return "<var:critic_refusal>"
            return f"<json:dynamic-error:{type(value).__name__}>"
        if isinstance(key, ast.Constant) and key.value == "info":
            # Idempotency block uses 'info' instead of 'error' -- the fixture
            # records it as INFO:<leading-token>.
            if isinstance(value, ast.Constant):
                text = str(value.value)
                return f"INFO:{text.split(':')[0]}" if ":" in text else text
            if isinstance(value, ast.JoinedStr):
                # ``info`` f-string starts with "Workers already ran ..."; the
                # fixture records the canonical INFO:redundant_call_blocked tag.
                return "INFO:redundant_call_blocked"
            return "<var:info>"
    return "<json:no-error-key>"


def _fstring_leading_token(jstr: ast.JoinedStr) -> str:
    """Extract the leading literal token from an f-string error message.

    Used for the circuit-breaker exit whose ``error`` is
    ``f"CIRCUIT BREAKER: {failure_count} worker failures..."``: the fixture
    records it as ``CIRCUIT BREAKER (worker_failure_count)``.
    """
    # The fixture's canonical tag for that one f-string exit.
    for part in jstr.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            head = part.value.strip()
            if head.startswith("CIRCUIT BREAKER"):
                return "CIRCUIT BREAKER (worker_failure_count)"
            # Generic fallback: leading token before any colon/space.
            return head.split(":")[0].split(" ")[0] or head
    return "<fstring:no-literal-prefix>"


def _const_str(node) -> str:
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.JoinedStr):
        # Best-effort: join the literal parts.
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            else:
                out.append("{...}")
        return "".join(out)
    return f"<dynamic:{type(node).__name__}>"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWorkerExitPathContract:
    """The wave-6 verbatim code-move must preserve the 76-exit contract."""

    def test_function_lives_in_phases_module(self):
        """The implementation must live in ``tool_planning_worker_phases``."""
        target, _, _, _ = _collect_returns_from_module(_PHASES_REL)
        assert target.lineno >= 1, "function not found in phases module"

    def test_function_no_longer_has_body_in_durable_module(self):
        """The durable module's ``_execute_workers_command`` must be a thin delegate.

        After the move the durable companion's copy is a 1-return delegate that
        forwards to ``tool_planning_worker_phases._execute_workers_command``.
        """
        target, main_returns, _, _ = _collect_returns_from_module(_DURABLE_REL)
        assert target is not None
        # The delegate has exactly one return (the forward call).
        assert len(main_returns) == 1, (
            f"durable delegate should have exactly 1 return, has {len(main_returns)}; "
            "the body must live in tool_planning_worker_phases now."
        )

    def test_return_count_matches_fixture(self):
        """Exactly 76 returns reach the caller (nested helper excluded)."""
        _target, main_returns, _nested, _ = _collect_returns_from_module(_PHASES_REL)
        assert len(main_returns) == EXPECTED_WORKER_EXIT_COUNT, (
            f"post-move function has {len(main_returns)} returns, "
            f"fixture expects {EXPECTED_WORKER_EXIT_COUNT}"
        )

    def test_nested_rollback_helper_preserved(self):
        """The nested ``rollback_rework_preparation`` closure must move with the body."""
        _target, _main, _nested_returns, nested_ranges = _collect_returns_from_module(_PHASES_REL)
        nested_names = [name for (_s, _e, name) in nested_ranges]
        assert _NESTED_NAME in nested_names, (
            f"nested helper {_NESTED_NAME!r} not found; rollback cascade exits "
            "cannot be accurately classified."
        )

    def test_return_type_histogram_matches_fixture(self):
        """The 7-category return-type histogram must be unchanged."""
        _target, main_returns, _nested, _ = _collect_returns_from_module(_PHASES_REL)
        actual = {}
        for ret in main_returns:
            rtype = _classify_return(ret)
            actual[rtype] = actual.get(rtype, 0) + 1
        assert actual == RETURN_TYPE_COUNTS, (
            f"return-type histogram drifted:\n"
            f"  actual:   {actual}\n"
            f"  expected: {RETURN_TYPE_COUNTS}"
        )

    def test_distinct_exit_identities_match_fixture(self):
        """The set of distinct exit identities must be identical to the snapshot."""
        _target, main_returns, _nested, _ = _collect_returns_from_module(_PHASES_REL)
        identities = set()
        for ret in main_returns:
            rtype = _classify_return(ret)
            if rtype == "json_tool_result":
                identities.add(_extract_json_error_identity(ret))
            elif rtype == "var_return":
                identities.add("var_return:recovery")
            else:
                identities.add(rtype)
        assert identities == EXPECTED_WORKER_EXIT_REASONS, (
            "distinct exit identities drifted:\n"
            f"  only in live:   {sorted(identities - EXPECTED_WORKER_EXIT_REASONS)}\n"
            f"  only in fixture: {sorted(EXPECTED_WORKER_EXIT_REASONS - identities)}"
        )

    def test_abandon_path_count_preserved(self):
        """The 8 abandon exits must survive the move.

        An abandon exit is a json_tool_result return that follows a call to
        ``_force_abandon_frozen_worker_generation`` or
        ``_force_abandon_official_rework_generation`` (whose result is spread
        into the json dict via ``**abandon_result`` or accessed via
        ``abandon_result.get(...)``).
        """
        src_text = (_REPO_ROOT / _PHASES_REL).read_text(encoding="utf-8")
        tree = ast.parse(src_text)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _TARGET:
                target = node
                break
        assert target is not None

        abandon_helpers = {
            "_force_abandon_frozen_worker_generation",
            "_force_abandon_official_rework_generation",
        }
        # Count calls to the abandon helpers inside the target function body.
        # The fixture documents 8 abandon exits but the function makes more
        # than 8 abandon-helper *calls* (some paths assign abandon_result then
        # decide not to spread it). The load-bearing count is the number of
        # DISTINCT json_tool_result returns that reference an ``abandon_result``
        # local variable -- which is exactly ABANDON_EXIT_COUNT.
        abandon_return_count = 0
        for node in ast.walk(target):
            if not isinstance(node, ast.Return):
                continue
            val = node.value
            if isinstance(val, ast.Await):
                val = val.value
            if not (isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute)
                    and val.func.attr == "_json_tool_result"):
                continue
            if not val.args or not isinstance(val.args[0], ast.Dict):
                continue
            d = val.args[0]
            # Check for ``**abandon_result`` spread (None-key entry with Name).
            for k, v in zip(d.keys, d.values):
                if k is None and isinstance(v, ast.Name) and v.id == "abandon_result":
                    abandon_return_count += 1
                    break
            else:
                # Check for ``abandon_result.get(...)`` or bare ``abandon_result``
                # reference among the dict values (OFFICIAL_REWORK_CIRCUIT_BREAKER
                # uses ``abandon_result.get("abandoned")`` and a raw
                # ``"abandon_result": abandon_result`` field).
                src_segment = ast.get_source_segment(src_text, node) or ""
                if "abandon_result" in src_segment:
                    abandon_return_count += 1
        assert abandon_return_count == ABANDON_EXIT_COUNT, (
            f"abandon exits drifted: live has {abandon_return_count}, "
            f"fixture expects {ABANDON_EXIT_COUNT}"
        )

    def test_live_function_is_callable_through_parent_delegate(self):
        """The parent ``tool_planning_worker._execute_workers_command`` must resolve.

        This catches a broken import chain: the parent delegate imports
        ``tool_planning_worker_phases`` lazily, so an import error there would
        only surface at call time.
        """
        import tool_planning_worker

        # The parent must expose the symbol.
        assert hasattr(tool_planning_worker, "_execute_workers_command")
        # The phases module must import cleanly.
        import tool_planning_worker_phases
        # The durable delegate must still exist (the public execute_workers
        # wrapper and the LLM-role-contract test both reach for it).
        import tool_planning_worker_durable
        assert hasattr(tool_planning_worker_durable, "_execute_workers_command")
        assert hasattr(tool_planning_worker_durable, "execute_workers")
