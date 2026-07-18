"""Static capability contract for ``national_tcp_policy_v1`` bots.

The active bot ABI has one intentionally narrow boundary:

* the system owns raw TCP, stream decoding, state reconstruction, deadlines,
  tracker updates, legality and wire serialization in ``national_bot.py``;
* the system owns compact immutable metadata in ``precompute.py``;
* candidate code owns only ``policy.py``;
* policy receives one schema-versioned ``decision_context`` and returns only
  typed intents.

This detector never imports candidate modules and never interprets archived
JSON-derived bot code.  It is safe to run during planning and quality gates.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import stat
from typing import Any, Iterable

from bot_namespace import strict_artifact_layout_errors


# Bare-action output tracking now covers refinement-generator ``yield`` and
# ``yield from`` paths in addition to ordinary function returns.  It also
# follows a bounded set of pure local helper/constant forms. Capability
# receipts bind this detector identity, so a candidate checked under an older
# detector cannot be reused without a fresh capability gate.
CAPABILITY_SCHEMA_VERSION = 8
NATIONAL_CAPABILITY_DETECTOR_VERSION = "national-policy-static-v7"
DECISION_CONTEXT_SCHEMA_VERSION = 1
POLICY_ENTRYPOINTS = {
    "get_baseline_decision": ("context",),
    "iter_decisions": ("context", "baseline", "deadline"),
}
CONTEXT_FIELDS = (
    "schema_version",
    "runtime_version",
    "decision_id",
    "cards",
    "hand",
    "betting",
    "history",
    "line",
    "legal",
    "opponent",
    "deadline",
)
INTENT_KINDS = ("pass", "fold", "allin", "raise")
SYSTEM_FILES = ("national_bot.py", "precompute.py")
RETIRED_ABI_FILES = ("main.py", "state.py", "strategy.py")

# A candidate never owns the raw TCP vocabulary.  ``pass`` is included even
# though it is not a wire token: it is a typed intent kind and therefore must
# still be wrapped in ``{"kind": "pass"}``, rather than returned as a bare
# scalar.  The static checker only follows these values to policy entrypoint
# returns; their appearance in public-state enums is valid input handling.
_BARE_ACTION_LITERALS = frozenset({
    "pass",
    "fold",
    "allin",
    "raise",
    "call",
    "check",
})

REQUIRED_CHECKS = (
    "national_policy_module",
    "system_runtime_current",
    "policy_baseline_entrypoint",
    "fast_policy_baseline",
    "policy_refinement_entrypoint",
    "decision_context_v1",
    "typed_intent_v1",
    "socket_owner_action_mapping",
    "raw_tcp_stream_decoder",
    "exact_raise_to_boundary",
    "decision_time_budget_visible",
    "killable_decision_runtime",
    "decision_path_no_external_io",
    "decision_path_no_full_history_scan",
    "decision_path_no_large_runtime_tables",
    "persistent_match_memory",
    "terminal_response_memory",
    "showdown_range_posterior",
    "authoritative_hand_context",
    "policy_consumes_legal_context",
    "policy_consumes_betting_context",
    "incremental_opponent_model",
)

ADVISORY_CHECKS = (
    "incremental_refinement_protocol",
    "budget_scaled_refinement",
    "precompute_lookup_path",
    "precompute_runtime_influence",
    "terminal_response_adaptation",
    "showdown_range_adaptation",
    "donk_line_reachability",
    "delayed_probe_line_reachability",
)

_FORBIDDEN_IMPORT_ROOTS = frozenset({
    "asyncio",
    "builtins",
    "ctypes",
    "ftplib",
    "glob",
    "http",
    "importlib",
    "io",
    "logging",
    "marshal",
    "mmap",
    "multiprocessing",
    "os",
    "pathlib",
    "pickle",
    "requests",
    "shutil",
    "smtplib",
    "socket",
    "sqlite3",
    "ssl",
    "subprocess",
    "sys",
    "tarfile",
    "telnetlib",
    "tempfile",
    "urllib",
    "zipfile",
})
_FORBIDDEN_CALL_LEAVES = frozenset({
    "Popen",
    "__import__",
    "compile",
    "eval",
    "exec",
    "getattr",
    "globals",
    "input",
    "itemgetter",
    "attrgetter",
    "locals",
    "methodcaller",
    "open",
    "vars",
})
_FORBIDDEN_IO_METHOD_LEAVES = frozenset({
    "bind",
    "connect",
    "create_connection",
    "open",
    "read_bytes",
    "read_text",
    "recv",
    "send",
    "sendall",
    "urlopen",
    "write_bytes",
    "write_text",
})
_HISTORY_SCAN_CALLS = frozenset({
    "all",
    "any",
    "dict",
    "dump",
    "dumps",
    "enumerate",
    "filter",
    "iter",
    "list",
    "map",
    "max",
    "min",
    "reversed",
    "set",
    "sorted",
    "sum",
    "tuple",
})
_CONTEXT_DEEP_SCAN_CALLS = frozenset({
    "asdict",
    "deepcopy",
    "dump",
    "dumps",
    "pformat",
    "repr",
    "str",
})
_HISTORY_SCAN_METHODS = frozenset({"copy", "items", "keys", "values"})
_MAX_POLICY_LITERAL_ENTRIES = 4096
# Cap top-level system evaluator invocations in the synchronous baseline.
# The sandbox probe uses the same unit, so static and dynamic gates cannot
# quietly diverge on an 800-call boundary.
BASELINE_EVALUATOR_CALL_CAP = 800
_SYSTEM_EVALUATOR_SYMBOLS = frozenset({
    "precompute.evaluate_five",
    "precompute.best_hand_rank",
    "precompute.evaluate_seven",
    "precompute.compare_hands",
})
_FORBIDDEN_REFLECTION_ATTRIBUTES = frozenset({
    "__builtins__",
    "__dict__",
    "__getattribute__",
    "__globals__",
    "__mro__",
    "__subclasses__",
})
_FORBIDDEN_REFLECTION_NAMES = frozenset({"__builtins__"})
_HISTORY_RECONSTRUCTION_NAMES = frozenset({
    "request",
    "requests",
    "response",
    "responses",
    "current_request_view",
    "hand_runtime",
    "opponent_runtime",
})


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_digest(value: Any) -> str:
    return _digest_bytes(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    )


def _regular_file(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not stat.S_ISLNK(mode)


def _python_sources(root: Path) -> tuple[dict[str, str], list[str]]:
    sources: dict[str, str] = {}
    issues: list[str] = []
    if not root.is_dir() or root.is_symlink():
        return {}, ["bot_directory_missing_or_not_regular"]
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            issues.append("python_path_outside_bot")
            continue
        if not _regular_file(path):
            issues.append(f"python_source_not_regular:{relative}")
            continue
        try:
            sources[relative] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            issues.append(f"python_source_unreadable:{relative}:{type(exc).__name__}")
    return sources, issues


def _parse_sources(sources: dict[str, str]) -> tuple[dict[str, ast.Module], list[str]]:
    trees: dict[str, ast.Module] = {}
    issues: list[str] = []
    for relative, source in sorted(sources.items()):
        try:
            trees[relative] = ast.parse(source, filename=relative, type_comments=True)
        except (SyntaxError, ValueError) as exc:
            issues.append(f"python_source_unparsable:{relative}:{type(exc).__name__}")
    return trees, issues


def _check(
    check_id: str,
    passed: bool,
    *,
    guidance: str,
    summary: str,
    locations: Iterable[str] = (),
    required: bool = True,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = {
        "summary": str(summary),
        "locations": list(dict.fromkeys(map(str, locations))),
    }
    if details:
        evidence.update(details)
    return {
        "check_id": check_id,
        "name": check_id,
        "passed": bool(passed),
        "required": bool(required),
        "skill_layer": _skill_layer(check_id),
        "guidance": guidance,
        "evidence": evidence,
    }


def _skill_layer(check_id: str) -> str:
    if "opponent" in check_id or "response" in check_id or "showdown" in check_id:
        return "opponent_model"
    if "precompute" in check_id:
        return "precompute"
    if check_id in {"donk_line_reachability", "delayed_probe_line_reachability"}:
        return "line_template"
    if check_id in {"typed_intent_v1", "exact_raise_to_boundary"}:
        return "action_intent"
    return "runtime_architecture"


def _function_map(tree: ast.Module | None) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    if tree is None:
        return {}
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _baseline_full_enumeration_locations(tree: ast.Module | None) -> list[str]:
    """Find full-combination calls reachable from the synchronous baseline.

    Full river enumeration is valid only in ``iter_decisions`` where every
    batch rechecks its monotonic deadline.  This AST reachability guard follows
    direct helper and callable-alias paths, rejects the concrete combinator
    primitive and known oversized nested-range pair loops from
    ``get_baseline_decision``, while allowing those operations in refinement.
    A system-owned dynamic phase counter separately limits baseline evaluator
    work; neither gate substitutes for the other.
    """

    functions = _function_map(tree)
    baseline = functions.get("get_baseline_decision")
    if baseline is None:
        return []
    callable_defs: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    # The candidate can hide an expensive helper in a nested function or a
    # class method.  Attribute calls are ambiguous without execution, so keep
    # all same-named definitions and inspect them conservatively.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            callable_defs.setdefault(node.name, []).append(node)

    symbol_aliases: dict[str, str] = {}
    function_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                symbol_aliases[alias.asname or alias.name.split(".", 1)[0]] = (
                    alias.name
                )
        elif isinstance(node, ast.ImportFrom):
            module = str(node.module or "")
            for alias in node.names:
                if alias.name != "*":
                    symbol_aliases[alias.asname or alias.name] = (
                        f"{module}.{alias.name}" if module else alias.name
                    )

    # Resolve straight-line aliases such as
    # ``combo = itertools.combinations`` before walking reachable code.
    # Dynamic reflection is separately forbidden by this detector.
    for _ in range(8):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            symbol = _qualified_symbol(node.value, symbol_aliases, {})
            if not symbol:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in _assigned_names(target):
                    if symbol_aliases.get(name) != symbol:
                        symbol_aliases[name] = symbol
                        changed = True
        if not changed:
            break

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if not isinstance(value, ast.Name) or value.id not in callable_defs:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in _assigned_names(target):
                    function_aliases[name] = value.id

    deck_aliases: set[str] = set()

    def is_deck_source(value: ast.AST) -> bool:
        return bool(
            isinstance(value, ast.Call)
            and _qualified_symbol(value.func, symbol_aliases, {})
            == "precompute.deck_without"
        )

    def is_deck_value(value: ast.AST) -> bool:
        if isinstance(value, ast.Name):
            return value.id in deck_aliases
        if isinstance(value, ast.Subscript):
            return is_deck_value(value.value)
        if isinstance(value, ast.Starred):
            return is_deck_value(value.value)
        if isinstance(value, ast.Call):
            return is_deck_source(value) or any(
                is_deck_value(argument) for argument in value.args
            )
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            return any(is_deck_value(item) for item in value.elts)
        return False

    for _ in range(8):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if not (is_deck_source(node.value) or is_deck_value(node.value)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in _assigned_names(target):
                    if name not in deck_aliases:
                        deck_aliases.add(name)
                        changed = True
        if not changed:
            break

    def range_work_bound(node: ast.AST) -> int | None:
        exact = _range_cardinality(node)
        if exact is not None:
            return exact
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "range"
            and 2 <= len(node.args) <= 3
            and not node.keywords
        ):
            return None
        stop = _constant_int(node.args[1])
        if stop is None:
            return None
        # An expression such as ``range(left + 1, 45)`` has no exact static
        # cardinality, but 45 remains a sound finite upper bound for detecting
        # a nested 45-card pair sweep.  This is intentionally conservative.
        return max(0, abs(stop))

    def nested_range_work(node: ast.For | ast.AsyncFor) -> int | None:
        own_size = range_work_bound(node.iter)
        if own_size is None:
            return None
        largest = own_size
        for child in node.body:
            if not isinstance(child, (ast.For, ast.AsyncFor)):
                continue
            child_size = nested_range_work(child)
            if child_size is not None:
                largest = max(largest, own_size * child_size)
        return largest

    reachable = {id(baseline)}
    frontier = [baseline]
    while frontier:
        function = frontier.pop()
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name):
                callee = function_aliases.get(call.func.id, call.func.id)
            elif isinstance(call.func, ast.Attribute):
                callee = call.func.attr
            else:
                continue
            for candidate in callable_defs.get(callee) or ():
                if id(candidate) not in reachable:
                    reachable.add(id(candidate))
                    frontier.append(candidate)

    locations: list[str] = []
    for function in sorted(
        (
            node
            for nodes in callable_defs.values()
            for node in nodes
            if id(node) in reachable
        ),
        key=lambda node: (node.lineno, node.col_offset, node.name),
    ):
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            symbol = _qualified_symbol(call.func, symbol_aliases, {})
            if symbol == "itertools.combinations" or symbol.endswith(
                ".combinations"
            ):
                locations.append(
                    f"policy.py:{call.lineno}:baseline_full_enumeration"
                )
        for loop in ast.walk(function):
            if not isinstance(loop, (ast.For, ast.AsyncFor)):
                continue
            work = nested_range_work(loop)
            if work is not None and work > BASELINE_EVALUATOR_CALL_CAP:
                locations.append(
                    f"policy.py:{loop.lineno}:baseline_full_enumeration"
                )
        for expression in ast.walk(function):
            if not isinstance(
                expression,
                (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
            ):
                continue
            sizes = [range_work_bound(item.iter) for item in expression.generators]
            if sizes and all(size is not None for size in sizes):
                work = 1
                for size in sizes:
                    work *= int(size)
                if work > BASELINE_EVALUATOR_CALL_CAP:
                    locations.append(
                        f"policy.py:{expression.lineno}:baseline_full_enumeration"
                    )
        for outer in ast.walk(function):
            if not isinstance(outer, (ast.For, ast.AsyncFor)) or not is_deck_value(
                outer.iter
            ):
                continue
            if any(
                isinstance(inner, (ast.For, ast.AsyncFor))
                and inner is not outer
                and is_deck_value(inner.iter)
                for inner in ast.walk(outer)
            ):
                locations.append(
                    f"policy.py:{outer.lineno}:baseline_full_enumeration"
                )
    return list(dict.fromkeys(locations))


def _signature_ok(node: ast.AST | None, expected: tuple[str, ...]) -> bool:
    if not isinstance(node, ast.FunctionDef):
        return False
    args = node.args
    return bool(
        tuple(item.arg for item in args.posonlyargs + args.args) == expected
        and not args.kwonlyargs
        and args.vararg is None
        and args.kwarg is None
        and len(args.defaults) == 0
    )


def _call_leaf(node: ast.Call) -> str:
    target: ast.AST = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _literal_string(
    node: ast.AST | None,
    string_constants: dict[str, str],
) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return string_constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left, string_constants)
        right = _literal_string(node.right, string_constants)
        if left is not None and right is not None:
            return left + right
    return None


def _is_bare_action_literal(value: str | None) -> bool:
    """Return whether a statically-known scalar is a policy/wire action.

    ``raise <amount>`` is a raw TCP token, while the single-word values are
    either raw TCP tokens or the candidate-side ``pass``/``raise`` intent
    names.  All of them are forbidden only when they flow to a public policy
    entrypoint return; input facts such as ``opponent_action == "check"`` are
    deliberately not evidence of candidate wire ownership.
    """

    if value in _BARE_ACTION_LITERALS:
        return True
    prefix = "raise "
    suffix = (
        value[len(prefix):]
        if isinstance(value, str) and value.startswith(prefix)
        else ""
    )
    return bool(suffix) and all("0" <= character <= "9" for character in suffix)


def _bare_action_return_locations(tree: ast.Module | None) -> list[str]:
    """Find bounded, statically-known action scalars at policy outputs.

    A global literal scan cannot distinguish an observed opponent action from
    a candidate output.  This deliberately small flow analysis follows only
    output paths of ``get_baseline_decision`` and ``iter_decisions``.  In
    addition to direct/alias returns and generator yields, it recognizes a
    finite set of pure scalar forms that otherwise make a raw action easy to
    hide: literal format calls, literal tuple/list subscripts, augmented
    string aliases, and acyclic module-local helpers.  A helper is summarized
    only when its return/yield expression is statically resolvable; dynamic
    helpers are left to the runtime typed-intent guard rather than guessed.

    The analysis never executes policy code.  Helper recursion, expression
    combinations, and value sets are all bounded.  A known action on any
    explored path remains evidence even when another branch is unknown.  The
    historical evidence key remains ``bare_action_return_locations`` for
    receipt compatibility; yielded entries are labelled ``bare_action_yield``.
    """

    if tree is None:
        return []

    _MAX_VALUES = 16
    _MAX_COMBINATIONS = 32
    _MAX_HELPER_DEPTH = 4
    _MAX_HELPER_PARAMETERS = 8
    _UNRESOLVED_HELPER_FLOW = "\x00unresolved_helper_output_flow"

    helpers = {
        function.name: function
        for function in tree.body
        if isinstance(function, ast.FunctionDef)
        and function.name not in POLICY_ENTRYPOINTS
    }
    module_aliases: dict[str, frozenset[str]] = {}
    module_sequences: dict[str, tuple[frozenset[str], ...]] = {}

    def bounded_values(values) -> frozenset[str]:
        """Keep analysis work finite without discarding known action evidence."""

        result: set[str] = set()
        actions: set[str] = set()
        unresolved = False
        for value in values:
            if not isinstance(value, str):
                continue
            if value == _UNRESOLVED_HELPER_FLOW:
                unresolved = True
            if _is_bare_action_literal(value):
                actions.add(value)
            if len(result) < _MAX_VALUES:
                result.add(value)
        if unresolved:
            result.add(_UNRESOLVED_HELPER_FLOW)
        return frozenset(result | actions)

    def union_values(*groups: frozenset[str]) -> frozenset[str]:
        return bounded_values(
            value for group in groups for value in group
        )

    def concat_values(
        left: frozenset[str],
        right: frozenset[str],
    ) -> frozenset[str]:
        unresolved = _UNRESOLVED_HELPER_FLOW in left or _UNRESOLVED_HELPER_FLOW in right
        left = left.difference({_UNRESOLVED_HELPER_FLOW})
        right = right.difference({_UNRESOLVED_HELPER_FLOW})
        if not left or not right:
            return (
                frozenset({_UNRESOLVED_HELPER_FLOW})
                if unresolved
                else frozenset()
            )
        if len(left) * len(right) > _MAX_COMBINATIONS:
            values = bounded_values(
                f"{first}{second}"
                for first in sorted(left)[:_MAX_VALUES]
                for second in sorted(right)[:_MAX_VALUES]
            )
        else:
            values = bounded_values(
                f"{first}{second}" for first in left for second in right
            )
        return union_values(
            values,
            frozenset({_UNRESOLVED_HELPER_FLOW}) if unresolved else frozenset(),
        )

    def possible_sequence(
        value: ast.AST | None,
        aliases: dict[str, frozenset[str]],
        sequences: dict[str, tuple[frozenset[str], ...]],
        *,
        helper_depth: int = 0,
        helper_stack: frozenset[str] = frozenset(),
    ) -> tuple[frozenset[str], ...] | None:
        if isinstance(value, ast.Name):
            return sequences.get(value.id)
        if isinstance(value, (ast.Tuple, ast.List)):
            return tuple(
                possible_strings(
                    item,
                    aliases,
                    sequences,
                    helper_depth=helper_depth,
                    helper_stack=helper_stack,
                )
                for item in value.elts
            )
        return None

    def literal_index(value: ast.AST | None) -> int | None:
        if isinstance(value, ast.Constant) and type(value.value) is int:
            return int(value.value)
        if isinstance(value, ast.UnaryOp) and isinstance(value.op, ast.USub):
            child = literal_index(value.operand)
            return -child if child is not None else None
        return None

    def format_values(
        template_values: frozenset[str],
        positional: list[frozenset[str]],
        keywords: dict[str, frozenset[str]],
    ) -> frozenset[str]:
        if not template_values or any(not values for values in positional):
            return frozenset()
        if any(not values for values in keywords.values()):
            return frozenset()
        combinations: list[tuple[str, ...]] = [()]
        for values in [*positional, *keywords.values()]:
            if len(combinations) * len(values) > _MAX_COMBINATIONS:
                return frozenset()
            combinations = [
                args + (value,)
                for args in combinations
                for value in values
            ]
        keyword_names = tuple(keywords)
        rendered: list[str] = []
        for template in template_values:
            for arguments in combinations:
                args = arguments[:len(positional)]
                named = {
                    name: arguments[len(positional) + index]
                    for index, name in enumerate(keyword_names)
                }
                try:
                    rendered.append(template.format(*args, **named))
                except (IndexError, KeyError, ValueError, TypeError, AttributeError):
                    continue
        return bounded_values(rendered)

    def helper_output_values(
        function: ast.FunctionDef,
        call: ast.Call,
        caller_aliases: dict[str, frozenset[str]],
        caller_sequences: dict[str, tuple[frozenset[str], ...]],
        *,
        helper_depth: int,
        helper_stack: frozenset[str],
    ) -> frozenset[str]:
        """Summarize an acyclic, module-local helper without executing it."""

        if (
            function.name in helper_stack
            or helper_depth >= _MAX_HELPER_DEPTH
        ):
            # A cycle/depth overflow is only propagated when this helper value
            # reaches an entrypoint output.  Do not turn an unused recursive
            # utility into evidence, but never certify an unresolved helper as
            # a typed decision once it is returned/yielded publicly.
            return frozenset({_UNRESOLVED_HELPER_FLOW})
        if (
            function.args.vararg is not None
            or function.args.kwarg is not None
            or any(isinstance(argument, ast.Starred) for argument in call.args)
        ):
            return frozenset()
        parameters = [*function.args.posonlyargs, *function.args.args]
        if len(parameters) > _MAX_HELPER_PARAMETERS or len(call.args) > len(parameters):
            return frozenset()
        parameter_names = [parameter.arg for parameter in parameters]
        if any(keyword.arg is None or keyword.arg not in parameter_names for keyword in call.keywords):
            return frozenset()
        if len({keyword.arg for keyword in call.keywords}) != len(call.keywords):
            return frozenset()
        bound_aliases = dict(module_aliases)
        bound_sequences = dict(module_sequences)
        supplied: dict[str, ast.AST] = {}
        for parameter, argument in zip(parameter_names, call.args):
            supplied[parameter] = argument
        for keyword in call.keywords:
            assert keyword.arg is not None
            if keyword.arg in supplied:
                return frozenset()
            supplied[keyword.arg] = keyword.value
        defaults = list(function.args.defaults)
        default_start = len(parameters) - len(defaults)
        for index, parameter in enumerate(parameter_names):
            value = supplied.get(parameter)
            if value is None and index >= default_start:
                value = defaults[index - default_start]
            if value is None:
                bound_aliases.pop(parameter, None)
                bound_sequences.pop(parameter, None)
                continue
            values = possible_strings(
                value,
                caller_aliases,
                caller_sequences,
                helper_depth=helper_depth,
                helper_stack=helper_stack,
            )
            sequence = possible_sequence(
                value,
                caller_aliases,
                caller_sequences,
                helper_depth=helper_depth,
                helper_stack=helper_stack,
            )
            if values:
                bound_aliases[parameter] = values
            else:
                bound_aliases.pop(parameter, None)
            if sequence is not None:
                bound_sequences[parameter] = sequence
            else:
                bound_sequences.pop(parameter, None)
        return scan_helper_block(
            function.body,
            bound_aliases,
            bound_sequences,
            helper_depth=helper_depth + 1,
            helper_stack=helper_stack | {function.name},
        )[2]

    def possible_strings(
        value: ast.AST | None,
        aliases: dict[str, frozenset[str]],
        sequences: dict[str, tuple[frozenset[str], ...]],
        *,
        helper_depth: int = 0,
        helper_stack: frozenset[str] = frozenset(),
    ) -> frozenset[str]:
        """Return bounded statically-known strings that can flow through value."""

        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return frozenset({value.value})
        if isinstance(value, ast.Name):
            return aliases.get(value.id, frozenset())
        if isinstance(value, ast.IfExp):
            return union_values(
                possible_strings(
                    value.body, aliases, sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                ),
                possible_strings(
                    value.orelse, aliases, sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                ),
            )
        if isinstance(value, ast.BoolOp):
            return union_values(*(
                possible_strings(
                    item, aliases, sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                )
                for item in value.values
            ))
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            return union_values(*(
                possible_strings(
                    item, aliases, sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                )
                for item in value.elts
            ))
        if isinstance(value, ast.Subscript):
            sequence = possible_sequence(
                value.value,
                aliases,
                sequences,
                helper_depth=helper_depth,
                helper_stack=helper_stack,
            )
            index = literal_index(value.slice)
            if sequence is None or index is None or not -len(sequence) <= index < len(sequence):
                return frozenset()
            return sequence[index]
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            return concat_values(
                possible_strings(
                    value.left, aliases, sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                ),
                possible_strings(
                    value.right, aliases, sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                ),
            )
        if isinstance(value, ast.JoinedStr):
            chunks: list[frozenset[str]] = []
            for chunk in value.values:
                if isinstance(chunk, ast.Constant) and isinstance(chunk.value, str):
                    chunks.append(frozenset({chunk.value}))
                elif isinstance(chunk, ast.FormattedValue):
                    if chunk.format_spec is not None:
                        return frozenset()
                    values = possible_strings(
                        chunk.value,
                        aliases,
                        sequences,
                        helper_depth=helper_depth,
                        helper_stack=helper_stack,
                    )
                    if chunk.conversion == ord("r"):
                        values = bounded_values(repr(item) for item in values)
                    elif chunk.conversion == ord("a"):
                        values = bounded_values(ascii(item) for item in values)
                    elif chunk.conversion not in {-1, ord("s")}:
                        return frozenset()
                    chunks.append(values)
                else:
                    return frozenset()
            result = frozenset({""})
            for chunk in chunks:
                result = concat_values(result, chunk)
                if not result:
                    return frozenset()
            return result
        if isinstance(value, ast.Call):
            if (
                isinstance(value.func, ast.Attribute)
                and value.func.attr == "format"
                and not any(isinstance(argument, ast.Starred) for argument in value.args)
                and all(keyword.arg is not None for keyword in value.keywords)
            ):
                return format_values(
                    possible_strings(
                        value.func.value,
                        aliases,
                        sequences,
                        helper_depth=helper_depth,
                        helper_stack=helper_stack,
                    ),
                    [
                        possible_strings(
                            argument,
                            aliases,
                            sequences,
                            helper_depth=helper_depth,
                            helper_stack=helper_stack,
                        )
                        for argument in value.args
                    ],
                    {
                        str(keyword.arg): possible_strings(
                            keyword.value,
                            aliases,
                            sequences,
                            helper_depth=helper_depth,
                            helper_stack=helper_stack,
                        )
                        for keyword in value.keywords
                    },
                )
            if isinstance(value.func, ast.Name) and value.func.id in helpers:
                return helper_output_values(
                    helpers[value.func.id],
                    value,
                    aliases,
                    sequences,
                    helper_depth=helper_depth,
                    helper_stack=helper_stack,
                )
        return frozenset()

    def assign_aliases(
        statement: ast.Assign | ast.AnnAssign,
        aliases: dict[str, frozenset[str]],
        sequences: dict[str, tuple[frozenset[str], ...]],
        *,
        helper_depth: int = 0,
        helper_stack: frozenset[str] = frozenset(),
    ) -> None:
        values = possible_strings(
            statement.value,
            aliases,
            sequences,
            helper_depth=helper_depth,
            helper_stack=helper_stack,
        )
        sequence = possible_sequence(
            statement.value,
            aliases,
            sequences,
            helper_depth=helper_depth,
            helper_stack=helper_stack,
        )
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else [statement.target]
        )
        for target in targets:
            for name in _assigned_names(target):
                if values:
                    aliases[name] = values
                else:
                    aliases.pop(name, None)
                if sequence is not None:
                    sequences[name] = sequence
                else:
                    sequences.pop(name, None)

    def augment_aliases(
        statement: ast.AugAssign,
        aliases: dict[str, frozenset[str]],
        sequences: dict[str, tuple[frozenset[str], ...]],
        *,
        helper_depth: int = 0,
        helper_stack: frozenset[str] = frozenset(),
    ) -> None:
        added = possible_strings(
            statement.value,
            aliases,
            sequences,
            helper_depth=helper_depth,
            helper_stack=helper_stack,
        )
        for name in _assigned_names(statement.target):
            if isinstance(statement.op, ast.Add) and aliases.get(name) and added:
                aliases[name] = concat_values(aliases[name], added)
            else:
                aliases.pop(name, None)
            sequences.pop(name, None)

    def merged_states(
        *states: tuple[
            dict[str, frozenset[str]],
            dict[str, tuple[frozenset[str], ...]],
        ],
    ) -> tuple[
        dict[str, frozenset[str]],
        dict[str, tuple[frozenset[str], ...]],
    ]:
        aliases: dict[str, frozenset[str]] = {}
        sequences: dict[str, tuple[frozenset[str], ...]] = {}
        for name in set().union(*(set(state[0]) for state in states)):
            values = union_values(*(state[0].get(name, frozenset()) for state in states))
            if values:
                aliases[name] = values
        for name in set().union(*(set(state[1]) for state in states)):
            candidates = [state[1][name] for state in states if name in state[1]]
            if not candidates or len({len(value) for value in candidates}) != 1:
                continue
            sequences[name] = tuple(
                union_values(*(value[index] for value in candidates))
                for index in range(len(candidates[0]))
            )
        return aliases, sequences

    def yield_values(
        value: ast.AST | None,
        aliases: dict[str, frozenset[str]],
        sequences: dict[str, tuple[frozenset[str], ...]],
        *,
        helper_depth: int = 0,
        helper_stack: frozenset[str] = frozenset(),
    ) -> frozenset[str]:
        if isinstance(value, (ast.Yield, ast.YieldFrom)):
            return possible_strings(
                value.value,
                aliases,
                sequences,
                helper_depth=helper_depth,
                helper_stack=helper_stack,
            )
        if value is None:
            return frozenset()
        return union_values(*(
            yield_values(
                child,
                aliases,
                sequences,
                helper_depth=helper_depth,
                helper_stack=helper_stack,
            )
            for child in ast.iter_child_nodes(value)
        ))

    def scan_helper_block(
        statements: list[ast.stmt],
        aliases: dict[str, frozenset[str]],
        sequences: dict[str, tuple[frozenset[str], ...]],
        *,
        helper_depth: int,
        helper_stack: frozenset[str],
    ) -> tuple[
        dict[str, frozenset[str]],
        dict[str, tuple[frozenset[str], ...]],
        frozenset[str],
    ]:
        """Evaluate only static scalar emissions of one module-local helper."""

        current_aliases = dict(aliases)
        current_sequences = dict(sequences)
        emitted: frozenset[str] = frozenset()
        for statement in statements:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                emitted = union_values(emitted, yield_values(
                    statement.value, current_aliases, current_sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                ))
                assign_aliases(
                    statement,
                    current_aliases,
                    current_sequences,
                    helper_depth=helper_depth,
                    helper_stack=helper_stack,
                )
                continue
            if isinstance(statement, ast.AugAssign):
                emitted = union_values(emitted, yield_values(
                    statement.value, current_aliases, current_sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                ))
                augment_aliases(
                    statement,
                    current_aliases,
                    current_sequences,
                    helper_depth=helper_depth,
                    helper_stack=helper_stack,
                )
                continue
            if isinstance(statement, ast.Return):
                emitted = union_values(
                    emitted,
                    possible_strings(
                        statement.value,
                        current_aliases,
                        current_sequences,
                        helper_depth=helper_depth,
                        helper_stack=helper_stack,
                    ),
                    yield_values(
                        statement.value,
                        current_aliases,
                        current_sequences,
                        helper_depth=helper_depth,
                        helper_stack=helper_stack,
                    ),
                )
                continue
            if isinstance(statement, ast.Expr):
                emitted = union_values(emitted, yield_values(
                    statement.value, current_aliases, current_sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                ))
                continue
            if isinstance(statement, ast.If):
                left = scan_helper_block(
                    statement.body, current_aliases, current_sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                )
                right = scan_helper_block(
                    statement.orelse, current_aliases, current_sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                )
                current_aliases, current_sequences = merged_states(
                    (left[0], left[1]), (right[0], right[1]),
                )
                emitted = union_values(emitted, left[2], right[2])
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
                body = scan_helper_block(
                    statement.body, current_aliases, current_sequences,
                    helper_depth=helper_depth, helper_stack=helper_stack,
                )
                current_aliases, current_sequences = merged_states(
                    (current_aliases, current_sequences), (body[0], body[1]),
                )
                emitted = union_values(emitted, body[2])
                continue
            if isinstance(statement, ast.Try):
                paths = [
                    scan_helper_block(
                        statement.body, current_aliases, current_sequences,
                        helper_depth=helper_depth, helper_stack=helper_stack,
                    ),
                    *(
                        scan_helper_block(
                            handler.body, current_aliases, current_sequences,
                            helper_depth=helper_depth, helper_stack=helper_stack,
                        )
                        for handler in statement.handlers
                    ),
                    scan_helper_block(
                        statement.orelse, current_aliases, current_sequences,
                        helper_depth=helper_depth, helper_stack=helper_stack,
                    ),
                ]
                current_aliases, current_sequences = merged_states(
                    *((path[0], path[1]) for path in paths)
                )
                emitted = union_values(emitted, *(path[2] for path in paths))
                if statement.finalbody:
                    final = scan_helper_block(
                        statement.finalbody, current_aliases, current_sequences,
                        helper_depth=helper_depth, helper_stack=helper_stack,
                    )
                    current_aliases, current_sequences = final[:2]
                    emitted = union_values(emitted, final[2])
                continue
            if isinstance(statement, ast.Match):
                paths = [
                    scan_helper_block(
                        case.body, current_aliases, current_sequences,
                        helper_depth=helper_depth, helper_stack=helper_stack,
                    )
                    for case in statement.cases
                ]
                if paths:
                    current_aliases, current_sequences = merged_states(
                        *((path[0], path[1]) for path in paths)
                    )
                    emitted = union_values(emitted, *(path[2] for path in paths))
        return current_aliases, current_sequences, emitted

    # Module constants remain visible to entrypoints even if their declarations
    # appear after a function definition, so collect only module-level straight
    # assignments before analysing local return flow.
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            assign_aliases(statement, module_aliases, module_sequences)
        elif isinstance(statement, ast.AugAssign):
            augment_aliases(statement, module_aliases, module_sequences)

    locations: list[str] = []

    def record_action_output(
        value: ast.AST | None,
        node: ast.AST,
        aliases: dict[str, frozenset[str]],
        sequences: dict[str, tuple[frozenset[str], ...]],
        *,
        flow: str,
    ) -> None:
        for emitted in sorted(possible_strings(value, aliases, sequences)):
            if emitted == _UNRESOLVED_HELPER_FLOW:
                locations.append(
                    f"policy.py:{node.lineno}:bare_action_{flow}:unresolved_helper"
                )
            elif _is_bare_action_literal(emitted):
                locations.append(
                    f"policy.py:{node.lineno}:bare_action_{flow}:{emitted}"
                )

    def record_yield_output(
        value: ast.AST | None,
        aliases: dict[str, frozenset[str]],
        sequences: dict[str, tuple[frozenset[str], ...]],
    ) -> None:
        if isinstance(value, ast.Yield):
            record_action_output(
                value.value,
                value,
                aliases,
                sequences,
                flow="yield",
            )
            record_yield_output(value.value, aliases, sequences)
            return
        if isinstance(value, ast.YieldFrom):
            record_action_output(
                value.value,
                value,
                aliases,
                sequences,
                flow="yield",
            )
            record_yield_output(value.value, aliases, sequences)
            return
        if value is not None:
            # Yield expressions can be parenthesized or occur as the value of
            # an assignment/return expression.  Walk only the current
            # expression tree (never nested function bodies) so syntactic
            # reshaping cannot hide a public refinement output from this
            # intentionally small flow analysis.
            for child in ast.iter_child_nodes(value):
                record_yield_output(child, aliases, sequences)

    def scan_block(
        statements: list[ast.stmt],
        aliases: dict[str, frozenset[str]],
        sequences: dict[str, tuple[frozenset[str], ...]],
    ) -> tuple[
        dict[str, frozenset[str]],
        dict[str, tuple[frozenset[str], ...]],
    ]:
        current = dict(aliases)
        current_sequences = dict(sequences)
        for statement in statements:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                record_yield_output(statement.value, current, current_sequences)
                assign_aliases(statement, current, current_sequences)
                continue
            if isinstance(statement, ast.AugAssign):
                record_yield_output(statement.value, current, current_sequences)
                augment_aliases(statement, current, current_sequences)
                continue
            if isinstance(statement, ast.Return):
                record_yield_output(statement.value, current, current_sequences)
                record_action_output(
                    statement.value,
                    statement,
                    current,
                    current_sequences,
                    flow="return",
                )
                continue
            if isinstance(statement, ast.Expr):
                record_yield_output(statement.value, current, current_sequences)
                continue
            # Returns nested under a branch remain output paths.  Merge only
            # known literal possibilities for following aliases; unknown branch
            # values cannot turn a standalone input enum into output evidence.
            if isinstance(statement, ast.If):
                record_yield_output(statement.test, current, current_sequences)
                current, current_sequences = merged_states(
                    scan_block(statement.body, current, current_sequences),
                    scan_block(statement.orelse, current, current_sequences),
                )
                continue
            if isinstance(statement, (ast.For, ast.AsyncFor)):
                record_yield_output(statement.iter, current, current_sequences)
                body_aliases = dict(current)
                body_sequences = dict(current_sequences)
                for name in _assigned_names(statement.target):
                    values = possible_strings(statement.iter, current, current_sequences)
                    if values:
                        body_aliases[name] = values
                    else:
                        body_aliases.pop(name, None)
                    sequence = possible_sequence(statement.iter, current, current_sequences)
                    if sequence is not None:
                        body_sequences[name] = sequence
                    else:
                        body_sequences.pop(name, None)
                current, current_sequences = merged_states(
                    (current, current_sequences),
                    scan_block(statement.body, body_aliases, body_sequences),
                    scan_block(statement.orelse, current, current_sequences),
                )
                continue
            if isinstance(statement, ast.While):
                record_yield_output(statement.test, current, current_sequences)
                current, current_sequences = merged_states(
                    (current, current_sequences),
                    scan_block(statement.body, current, current_sequences),
                    scan_block(statement.orelse, current, current_sequences),
                )
                continue
            if isinstance(statement, ast.Try):
                paths = [
                    scan_block(statement.body, current, current_sequences),
                    *(
                        scan_block(handler.body, current, current_sequences)
                        for handler in statement.handlers
                    ),
                    scan_block(statement.orelse, current, current_sequences),
                ]
                current, current_sequences = merged_states(*paths)
                if statement.finalbody:
                    current, current_sequences = scan_block(
                        statement.finalbody, current, current_sequences,
                    )
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                current, current_sequences = scan_block(
                    statement.body, current, current_sequences,
                )
                continue
            if isinstance(statement, ast.Match):
                current, current_sequences = merged_states(
                    (current, current_sequences),
                    *(
                        scan_block(case.body, current, current_sequences)
                        for case in statement.cases
                    ),
                )
        return current, current_sequences

    for function in tree.body:
        if (
            isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
            and function.name in POLICY_ENTRYPOINTS
        ):
            scan_block(function.body, module_aliases, module_sequences)
    return list(dict.fromkeys(locations))


def _constant_int(node: ast.AST | None) -> int | None:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return int(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _constant_int(node.operand)
        if value is None:
            return None
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _constant_int(node.left)
        right = _constant_int(node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.FloorDiv) and right:
                return left // right
        except ArithmeticError:
            return None
    return None


def _qualified_symbol(
    node: ast.AST | None,
    symbol_aliases: dict[str, str],
    string_constants: dict[str, str],
) -> str:
    if isinstance(node, ast.Name):
        if node.id == "__builtins__":
            return "builtins"
        return symbol_aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _qualified_symbol(node.value, symbol_aliases, string_constants)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Subscript):
        parent = _qualified_symbol(node.value, symbol_aliases, string_constants)
        key = _literal_string(node.slice, string_constants)
        if parent in {"builtins", "builtins.__dict__"} and key:
            return f"builtins.{key}"
        return ""
    if (
        isinstance(node, ast.Call)
        and _qualified_symbol(node.func, symbol_aliases, string_constants).split(".")[-1]
        == "getattr"
        and len(node.args) >= 2
    ):
        parent = _qualified_symbol(node.args[0], symbol_aliases, string_constants)
        attribute = _literal_string(node.args[1], string_constants)
        if parent and attribute:
            return f"{parent}.{attribute}"
    return ""


def _context_path(
    node: ast.AST | None,
    context_aliases: dict[str, tuple[str, ...]],
    string_constants: dict[str, str],
) -> tuple[str, ...] | None:
    if isinstance(node, ast.Starred):
        return _context_path(
            node.value,
            context_aliases,
            string_constants,
        )
    if isinstance(node, ast.Name):
        return context_aliases.get(node.id)
    if isinstance(node, ast.Subscript):
        parent = _context_path(node.value, context_aliases, string_constants)
        key = _literal_string(node.slice, string_constants)
        if parent is not None and key is not None:
            return (*parent, key)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
    ):
        parent = _context_path(
            node.func.value,
            context_aliases,
            string_constants,
        )
        key = _literal_string(node.args[0], string_constants)
        if parent is not None and key is not None:
            return (*parent, key)
    if isinstance(node, ast.Call) and len(node.args) >= 1:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "__getitem__":
            parent = _context_path(
                node.func.value,
                context_aliases,
                string_constants,
            )
            key = _literal_string(node.args[0], string_constants)
            if parent is not None and key is not None:
                return (*parent, key)
        if (
            (
                isinstance(node.func, ast.Name)
                and node.func.id == "getitem"
            )
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "getitem"
            )
        ) and len(node.args) >= 2:
            parent = _context_path(
                node.args[0],
                context_aliases,
                string_constants,
            )
            key = _literal_string(node.args[1], string_constants)
            if parent is not None and key is not None:
                return (*parent, key)
    if isinstance(node, ast.Call) and node.args:
        if isinstance(node.func, ast.Name) and node.func.id in {
            "dict",
            "list",
            "tuple",
        }:
            return _context_path(
                node.args[0],
                context_aliases,
                string_constants,
            )
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "copy",
            "deepcopy",
        }:
            return _context_path(
                node.args[0],
                context_aliases,
                string_constants,
            )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "copy"
        and not node.args
    ):
        return _context_path(
            node.func.value,
            context_aliases,
            string_constants,
        )
    if isinstance(node, ast.Dict):
        unpacked = [
            _context_path(value, context_aliases, string_constants)
            for key, value in zip(node.keys, node.values)
            if key is None
        ]
        if len(unpacked) == 1 and unpacked[0] is not None:
            return unpacked[0]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _context_path(node.left, context_aliases, string_constants)
        right = _context_path(node.right, context_aliases, string_constants)
        if left is not None and right is None:
            return left
        if right is not None and left is None:
            return right
        if left is not None and left == right:
            return left
    return None


def _assigned_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [
            name
            for item in node.elts
            for name in _assigned_names(item)
        ]
    return []


def _bounded_history_slice(
    node: ast.AST,
    context_aliases: dict[str, tuple[str, ...]],
    string_constants: dict[str, str],
) -> bool:
    if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
        return False
    parent = _context_path(node.value, context_aliases, string_constants)
    if not parent or parent[0] != "history":
        return False
    lower = _constant_int(node.slice.lower)
    upper = _constant_int(node.slice.upper)
    step = _constant_int(node.slice.step) if node.slice.step is not None else 1
    if not step:
        return False
    if lower is not None and lower < 0 and upper is None:
        return abs(lower) <= 64
    if lower is None and upper is not None and 0 <= upper <= 64:
        return True
    if lower is not None and upper is not None:
        return abs(upper - lower) <= 64
    return False


def _contains_unbounded_history_reference(
    node: ast.AST,
    context_aliases: dict[str, tuple[str, ...]],
    string_constants: dict[str, str],
) -> bool:
    if _bounded_history_slice(node, context_aliases, string_constants):
        return False
    path = _context_path(node, context_aliases, string_constants)
    if path and path[0] == "history":
        return True
    return any(
        _contains_unbounded_history_reference(
            child,
            context_aliases,
            string_constants,
        )
        for child in ast.iter_child_nodes(node)
    )


def _literal_cardinality(node: ast.AST | None) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(
        node.value,
        (str, bytes, bytearray),
    ):
        return len(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        total = len(node.elts)
        for item in node.elts:
            child = _literal_cardinality(item)
            if child:
                total += child
        return total
    if isinstance(node, ast.Dict):
        total = len(node.keys)
        for item in node.values:
            child = _literal_cardinality(item)
            if child:
                total += child
        return total
    return None


def _range_cardinality(node: ast.AST | None) -> int | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    if node.func.id != "range" or node.keywords or not 1 <= len(node.args) <= 3:
        return None
    values = [_constant_int(item) for item in node.args]
    if any(item is None for item in values):
        return None
    if len(values) == 1:
        start, stop, step = 0, int(values[0]), 1
    elif len(values) == 2:
        start, stop, step = int(values[0]), int(values[1]), 1
    else:
        start, stop, step = map(int, values)
    if not step:
        return None
    return len(range(start, stop, step))


def _constructed_size(
    node: ast.AST | None,
    size_aliases: dict[str, int],
) -> int | None:
    literal = _literal_cardinality(node)
    if literal is not None:
        return literal
    if isinstance(node, ast.Name):
        return size_aliases.get(node.id)
    if isinstance(node, ast.BinOp):
        left = _constructed_size(node.left, size_aliases)
        right = _constructed_size(node.right, size_aliases)
        if isinstance(node.op, ast.Add) and left is not None and right is not None:
            return left + right
        if isinstance(node.op, ast.Mult):
            left_count = _constant_int(node.left)
            right_count = _constant_int(node.right)
            if left is not None and right_count is not None:
                return left * right_count
            if right is not None and left_count is not None:
                return right * left_count
    range_size = _range_cardinality(node)
    if range_size is not None:
        return range_size
    if isinstance(node, ast.Call) and node.args:
        leaf = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        if leaf in {"bytearray", "dict", "list", "set", "tuple"}:
            return (
                _constant_int(node.args[0])
                or _constructed_size(node.args[0], size_aliases)
            )
        if leaf == "product":
            sizes = [_constructed_size(item, size_aliases) for item in node.args]
            if sizes and all(size is not None for size in sizes):
                total = 1
                for size in sizes:
                    total *= int(size)
                return total
    return None


def _nested_mutating_loop_size(node: ast.For | ast.AsyncFor) -> int | None:
    own_size = _range_cardinality(node.iter)
    if own_size is None:
        return None
    largest = own_size
    for child in node.body:
        if not isinstance(child, (ast.For, ast.AsyncFor)):
            continue
        child_size = _nested_mutating_loop_size(child)
        if child_size is not None:
            largest = max(largest, own_size * child_size)
    mutates_collection = any(
        isinstance(candidate, ast.Call)
        and isinstance(candidate.func, ast.Attribute)
        and candidate.func.attr in {"add", "append", "extend", "update"}
        for candidate in ast.walk(node)
    )
    return largest if mutates_collection else None


def _policy_static_evidence(tree: ast.Module | None) -> dict[str, Any]:
    if tree is None:
        return {
            "imports": set(),
            "forbidden_imports": [],
            "forbidden_calls": [],
            "loaded_names": set(),
            "string_literals": set(),
            "integer_return_locations": [],
            "bare_action_return_locations": [],
            "raise_dict_locations": [],
            "kind_literals": set(),
            "large_literal_locations": [],
            "context_fields": set(),
            "history_scan_locations": [],
            "baseline_full_enumeration_locations": [],
            "evaluator_alias_locations": [],
        }
    string_constants: dict[str, str] = {}
    symbol_aliases: dict[str, str] = {}
    imports: set[str] = set()
    forbidden_imports: list[str] = []
    forbidden_calls: list[str] = []
    loaded_names: set[str] = set()
    string_literals: set[str] = set()
    integer_return_locations: list[str] = []
    bare_action_return_locations = _bare_action_return_locations(tree)
    raise_dict_locations: list[str] = []
    kind_literals: set[str] = set()
    large_literal_locations: list[str] = []
    context_fields: set[str] = set()
    history_scan_locations: list[str] = []
    evaluator_alias_locations: list[str] = []
    parent_by_id = {
        id(child): parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                symbol_aliases[alias.asname or alias.name.split(".", 1)[0]] = (
                    alias.name
                )
        elif isinstance(node, ast.ImportFrom):
            module = str(node.module or "")
            for alias in node.names:
                if alias.name == "*":
                    continue
                symbol_aliases[alias.asname or alias.name] = (
                    f"{module}.{alias.name}" if module else alias.name
                )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            literal = _literal_string(value, string_constants)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if literal is not None:
                for target in targets:
                    for name in _assigned_names(target):
                        string_constants[name] = literal

    # Resolve callable aliases (including aliases obtained through getattr or
    # builtins.__dict__) before checking calls.  A small fixed point is enough
    # for straight-line alias chains and deliberately errs on the safe side.
    for _ in range(8):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            symbol = _qualified_symbol(value, symbol_aliases, string_constants)
            if not symbol:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in _assigned_names(target):
                    if symbol_aliases.get(name) != symbol:
                        symbol_aliases[name] = symbol
                        changed = True
        if not changed:
            break

    context_aliases: dict[str, tuple[str, ...]] = {"context": ()}
    propagation_functions: dict[
        str,
        list[ast.FunctionDef | ast.AsyncFunctionDef],
    ] = {}
    for candidate in ast.walk(tree):
        if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            propagation_functions.setdefault(candidate.name, []).append(candidate)

    def call_definitions(
        call: ast.Call,
    ) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        if isinstance(call.func, ast.Name):
            return list(propagation_functions.get(call.func.id) or [])
        if isinstance(call.func, ast.Attribute):
            return list(propagation_functions.get(call.func.attr) or [])
        return []

    def call_bindings(
        call: ast.Call,
        callee: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> list[tuple[ast.arg, ast.AST]]:
        positional_parameters = list(callee.args.posonlyargs + callee.args.args)
        parameter_variants = [positional_parameters]
        if (
            isinstance(call.func, ast.Attribute)
            and positional_parameters
            and positional_parameters[0].arg in {"self", "cls"}
        ):
            # Attribute syntax can be either a bound instance call or an
            # explicit unbound class-method call.  Propagate both shapes; a
            # false negative here would permit history to hide in the shifted
            # argument.
            parameter_variants.append(positional_parameters[1:])
        bindings = [
            binding
            for parameters in parameter_variants
            for binding in zip(parameters, call.args)
        ]
        keyword_parameters = [
            *positional_parameters,
            *callee.args.kwonlyargs,
        ]
        by_name = {
            parameter.arg: parameter for parameter in keyword_parameters
        }
        bindings.extend(
            (by_name[keyword.arg], keyword.value)
            for keyword in call.keywords
            if keyword.arg in by_name
        )
        for keyword in call.keywords:
            if keyword.arg is not None or not isinstance(keyword.value, ast.Dict):
                continue
            for key, value in zip(keyword.value.keys, keyword.value.values):
                name = _literal_string(key, string_constants)
                if name in by_name:
                    bindings.append((by_name[name], value))
        return bindings

    for _ in range(12):
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                value = node.value
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
                path = _context_path(value, context_aliases, string_constants)
                if (
                    path is None
                    and isinstance(value, ast.Call)
                ):
                    return_paths = {
                        candidate_path
                        for callee in call_definitions(value)
                        for return_node in ast.walk(callee)
                        if isinstance(return_node, ast.Return)
                        and (
                            candidate_path := _context_path(
                                return_node.value,
                                context_aliases,
                                string_constants,
                            )
                        )
                        is not None
                    }
                    if len(return_paths) == 1:
                        path = next(iter(return_paths))
                if path is not None:
                    for target in targets:
                        for name in _assigned_names(target):
                            if context_aliases.get(name) != path:
                                context_aliases[name] = path
                                changed = True
                if (
                    isinstance(value, (ast.Tuple, ast.List))
                    and len(targets) == 1
                    and isinstance(targets[0], (ast.Tuple, ast.List))
                ):
                    for target_item, value_item in zip(
                        targets[0].elts,
                        value.elts,
                    ):
                        item_path = _context_path(
                            value_item,
                            context_aliases,
                            string_constants,
                        )
                        if item_path is None:
                            continue
                        for name in _assigned_names(target_item):
                            if context_aliases.get(name) != item_path:
                                context_aliases[name] = item_path
                                changed = True
            if isinstance(node, ast.Call):
                for callee in call_definitions(node):
                    for parameter, argument in call_bindings(node, callee):
                        path = _context_path(
                            argument,
                            context_aliases,
                            string_constants,
                        )
                        if (
                            path is not None
                            and context_aliases.get(parameter.arg) != path
                        ):
                            context_aliases[parameter.arg] = path
                            changed = True
        if not changed:
            break

    size_aliases: dict[str, int] = {}
    for _ in range(12):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            size = _constructed_size(node.value, size_aliases)
            if size is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in _assigned_names(target):
                    if size_aliases.get(name) != size:
                        size_aliases[name] = size
                        changed = True
        if not changed:
            break

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                imports.add(root)
                if root in _FORBIDDEN_IMPORT_ROOTS:
                    forbidden_imports.append(f"policy.py:{node.lineno}:{root}")
        elif isinstance(node, ast.ImportFrom):
            root = str(node.module or "").split(".", 1)[0]
            imports.add(root)
            if root in _FORBIDDEN_IMPORT_ROOTS:
                forbidden_imports.append(f"policy.py:{node.lineno}:{root}")
            if root == "precompute":
                for alias in node.names:
                    symbol = f"precompute.{alias.name}"
                    if symbol in _SYSTEM_EVALUATOR_SYMBOLS:
                        evaluator_alias_locations.append(
                            f"policy.py:{node.lineno}:evaluator_alias:{alias.name}"
                        )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            symbol = _qualified_symbol(
                node.value,
                symbol_aliases,
                string_constants,
            )
            if symbol in _SYSTEM_EVALUATOR_SYMBOLS:
                evaluator_alias_locations.append(
                    f"policy.py:{node.lineno}:evaluator_alias"
                )
            size = _constructed_size(node.value, size_aliases)
            if size is not None and size > _MAX_POLICY_LITERAL_ENTRIES:
                large_literal_locations.append(
                    f"policy.py:{node.lineno}:constructed:{size}"
                )
        elif isinstance(node, ast.Call):
            symbol = _qualified_symbol(node.func, symbol_aliases, string_constants)
            leaf = symbol.split(".")[-1] if symbol else _call_leaf(node)
            root = symbol.split(".", 1)[0] if symbol else ""
            call_values = [
                *node.args,
                *(keyword.value for keyword in node.keywords),
            ]
            if (
                leaf in _FORBIDDEN_CALL_LEAVES
                or leaf in _FORBIDDEN_IO_METHOD_LEAVES
                or root in _FORBIDDEN_IMPORT_ROOTS
            ):
                forbidden_calls.append(
                    f"policy.py:{node.lineno}:{symbol or leaf or 'dynamic_call'}"
                )
            path = _context_path(node, context_aliases, string_constants)
            if path:
                context_fields.add(path[0])
            scans_history = leaf in _HISTORY_SCAN_CALLS and any(
                _contains_unbounded_history_reference(
                    argument,
                    context_aliases,
                    string_constants,
                )
                for argument in call_values
            )
            scans_full_context = leaf in _CONTEXT_DEEP_SCAN_CALLS and any(
                _context_path(
                    argument,
                    context_aliases,
                    string_constants,
                )
                == ()
                for argument in call_values
            )
            if scans_history or scans_full_context:
                history_scan_locations.append(
                    f"policy.py:{node.lineno}:{leaf}"
                )
            if (
                isinstance(node.func, ast.Attribute)
                and leaf in _HISTORY_SCAN_METHODS
                and _contains_unbounded_history_reference(
                    node.func.value,
                    context_aliases,
                    string_constants,
                )
            ):
                history_scan_locations.append(
                    f"policy.py:{node.lineno}:{leaf}"
                )
            range_size = _range_cardinality(node)
            if range_size is not None and range_size > _MAX_POLICY_LITERAL_ENTRIES:
                large_literal_locations.append(
                    f"policy.py:{node.lineno}:range:{range_size}"
                )
            if leaf in {"bytearray", "dict", "list", "set", "tuple"} and node.args:
                allocation = _constant_int(node.args[0]) or _range_cardinality(node.args[0])
                if allocation is not None and allocation > _MAX_POLICY_LITERAL_ENTRIES:
                    large_literal_locations.append(
                        f"policy.py:{node.lineno}:{leaf}:{allocation}"
                    )
                elif (
                    allocation is None
                    and isinstance(node.args[0], ast.Call)
                    and isinstance(node.args[0].func, ast.Name)
                    and node.args[0].func.id == "range"
                ):
                    large_literal_locations.append(
                        f"policy.py:{node.lineno}:{leaf}:unbounded_range"
                    )
        elif isinstance(node, ast.Subscript):
            path = _context_path(node, context_aliases, string_constants)
            if path:
                context_fields.add(path[0])
        elif isinstance(node, ast.Attribute):
            if node.attr in _FORBIDDEN_REFLECTION_ATTRIBUTES:
                forbidden_calls.append(
                    f"policy.py:{node.lineno}:reflection:{node.attr}"
                )
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loaded_names.add(node.id)
            if node.id in _FORBIDDEN_REFLECTION_NAMES:
                forbidden_calls.append(
                    f"policy.py:{node.lineno}:reflection:{node.id}"
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            string_literals.add(node.value)
        elif isinstance(node, ast.Return):
            if isinstance(node.value, ast.Constant) and (
                isinstance(node.value.value, int) and not isinstance(node.value.value, bool)
            ):
                integer_return_locations.append(f"policy.py:{node.lineno}")
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            size = _literal_cardinality(node) or 0
            if size > _MAX_POLICY_LITERAL_ENTRIES:
                large_literal_locations.append(f"policy.py:{node.lineno}:{size}")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            left_size = _literal_cardinality(node.left)
            right_size = _literal_cardinality(node.right)
            left_count = _constant_int(node.left)
            right_count = _constant_int(node.right)
            size = (
                left_size * right_count
                if left_size is not None and right_count is not None
                else right_size * left_count
                if right_size is not None and left_count is not None
                else None
            )
            if size is not None and size > _MAX_POLICY_LITERAL_ENTRIES:
                large_literal_locations.append(
                    f"policy.py:{node.lineno}:repeat:{size}"
                )
            elif (
                size is None
                and (
                    left_size is not None
                    and right_count is None
                    or right_size is not None
                    and left_count is None
                )
            ):
                large_literal_locations.append(
                    f"policy.py:{node.lineno}:repeat:unbounded"
                )
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            sizes = [_range_cardinality(item.iter) for item in node.generators]
            if sizes and all(size is not None for size in sizes):
                total = 1
                for size in sizes:
                    total *= int(size)
                if total > _MAX_POLICY_LITERAL_ENTRIES:
                    large_literal_locations.append(
                        f"policy.py:{node.lineno}:comprehension:{total}"
                    )

        if isinstance(node, (ast.For, ast.AsyncFor)) and _contains_unbounded_history_reference(
            node.iter,
            context_aliases,
            string_constants,
        ):
            history_scan_locations.append(f"policy.py:{node.lineno}:for")
        elif isinstance(node, ast.While) and _contains_unbounded_history_reference(
            node.test,
            context_aliases,
            string_constants,
        ):
            history_scan_locations.append(f"policy.py:{node.lineno}:while")
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            if any(
                _contains_unbounded_history_reference(
                    generator.iter,
                    context_aliases,
                    string_constants,
                )
                for generator in node.generators
            ):
                history_scan_locations.append(
                    f"policy.py:{node.lineno}:comprehension"
                )
        if isinstance(node, (ast.For, ast.AsyncFor)):
            loop_size = _nested_mutating_loop_size(node)
            if loop_size is not None and loop_size > _MAX_POLICY_LITERAL_ENTRIES:
                large_literal_locations.append(
                    f"policy.py:{node.lineno}:mutating_loop:{loop_size}"
                )

        if isinstance(node, ast.Dict):
            literal_map: dict[str, ast.AST] = {}
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    literal_map[key.value] = value
            kind = literal_map.get("kind")
            if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
                kind_literals.add(kind.value)
                if kind.value == "raise" and "raise_to" in literal_map:
                    raise_dict_locations.append(f"policy.py:{node.lineno}")

    # A policy may call a system evaluator directly through the precompute
    # module, but it may never retain one as a value.  Defaults, containers,
    # closures, partials, class attributes and getattr all bypass post-import
    # instrumentation if this boundary is not structural.  Checking the
    # reference itself is more complete than attempting to enumerate Python
    # value carriers.
    for node in ast.walk(tree):
        symbol = _qualified_symbol(node, symbol_aliases, string_constants)
        if symbol not in _SYSTEM_EVALUATOR_SYMBOLS:
            continue
        parent = parent_by_id.get(id(node))
        direct_module_call = (
            isinstance(node, ast.Attribute)
            and isinstance(parent, ast.Call)
            and parent.func is node
            and _qualified_symbol(
                node.value,
                symbol_aliases,
                string_constants,
            )
            == "precompute"
        )
        if not direct_module_call:
            evaluator_alias_locations.append(
                f"policy.py:{getattr(node, 'lineno', 0)}:evaluator_alias"
            )

    return {
        "imports": imports,
        "forbidden_imports": forbidden_imports,
        "forbidden_calls": forbidden_calls,
        "loaded_names": loaded_names,
        "string_literals": string_literals,
        "integer_return_locations": integer_return_locations,
        "bare_action_return_locations": bare_action_return_locations,
        "raise_dict_locations": raise_dict_locations,
        "kind_literals": kind_literals,
        "large_literal_locations": list(dict.fromkeys(large_literal_locations)),
        "context_fields": context_fields,
        "history_scan_locations": list(dict.fromkeys(history_scan_locations)),
        "baseline_full_enumeration_locations": (
            _baseline_full_enumeration_locations(tree)
        ),
        "evaluator_alias_locations": list(
            dict.fromkeys(evaluator_alias_locations)
        ),
    }


def _exact_system_runtime(root: Path) -> tuple[bool, list[str], dict[str, str]]:
    issues: list[str] = []
    observed: dict[str, str] = {}
    try:
        from national_native import NATIVE_BOT_TEMPLATE, NATIVE_PRECOMPUTE_TEMPLATE

        expected = {
            "national_bot.py": NATIVE_BOT_TEMPLATE.encode("utf-8"),
            "precompute.py": NATIVE_PRECOMPUTE_TEMPLATE.encode("utf-8"),
        }
    except Exception as exc:
        return False, [f"system_runtime_template_unavailable:{type(exc).__name__}"], observed
    for relative, expected_bytes in expected.items():
        path = root / relative
        if not _regular_file(path):
            issues.append(f"system_runtime_file_missing_or_not_regular:{relative}")
            continue
        try:
            actual = path.read_bytes()
        except OSError as exc:
            issues.append(f"system_runtime_file_unreadable:{relative}:{type(exc).__name__}")
            continue
        observed[relative] = _digest_bytes(actual)
        if actual != expected_bytes:
            issues.append(f"system_runtime_file_drift:{relative}")
    return not issues, issues, observed


def _source_digest(sources: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative, source in sorted(sources.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def evaluate_national_capabilities(bot_dir: str | Path) -> dict[str, Any]:
    root = Path(bot_dir).resolve()
    sources, source_issues = _python_sources(root)
    trees, parse_issues = _parse_sources(sources)
    # Static decision checks must not give a green result to an artifact that
    # the launch/certification path would reject for carrying an unbound model,
    # table, helper, cache, or symlink.  A future system asset remains outside
    # this directory and is admitted by a separate bound asset profile; it is
    # not an exception to the closed candidate layout.
    layout_errors = strict_artifact_layout_errors(root)
    infrastructure_failures = []
    if source_issues or parse_issues:
        infrastructure_failures.append({
            "component": "national_policy_static_parser",
            "failure_class": "candidate_artifact",
            "issues": [*source_issues, *parse_issues],
        })

    retired_present = [name for name in RETIRED_ABI_FILES if name in sources]
    extra_python = sorted(
        set(sources).difference({"national_bot.py", "precompute.py", "policy.py"})
    )
    policy_tree = trees.get("policy.py")
    policy_module_ok = (
        policy_tree is not None
        and not retired_present
        and not extra_python
        and not layout_errors
    )
    functions = _function_map(policy_tree)
    static = _policy_static_evidence(policy_tree)
    runtime_ok, runtime_issues, runtime_hashes = _exact_system_runtime(root)
    if runtime_issues and any("unavailable" in item for item in runtime_issues):
        infrastructure_failures.append({
            "component": "national_runtime_template",
            "failure_class": "internal_infrastructure",
            "issues": runtime_issues,
        })

    baseline_ok = _signature_ok(
        functions.get("get_baseline_decision"),
        POLICY_ENTRYPOINTS["get_baseline_decision"],
    )
    refinement_ok = _signature_ok(
        functions.get("iter_decisions"),
        POLICY_ENTRYPOINTS["iter_decisions"],
    )
    forbidden_kind_literals = sorted(
        static["kind_literals"].difference(INTENT_KINDS)
    )
    typed_ok = bool(
        baseline_ok
        and refinement_ok
        and not static["integer_return_locations"]
        and not static["bare_action_return_locations"]
        and not forbidden_kind_literals
    )
    if "raise" in static["kind_literals"] and not static["raise_dict_locations"]:
        typed_ok = False

    candidate_io_ok = not static["forbidden_imports"] and not static["forbidden_calls"]
    history_identifier_issues = static["loaded_names"].intersection(
        _HISTORY_RECONSTRUCTION_NAMES
    )
    history_ok = not history_identifier_issues and not static[
        "history_scan_locations"
    ]
    table_ok = not static["large_literal_locations"]
    baseline_enumeration_ok = not static[
        "baseline_full_enumeration_locations"
    ]
    baseline_evaluator_alias_ok = not static["evaluator_alias_locations"]
    context_used = bool("context" in static["loaded_names"])
    context_fields = set(static["context_fields"])

    def system_check(check_id: str, summary: str, guidance: str) -> dict[str, Any]:
        return _check(
            check_id,
            runtime_ok,
            guidance=guidance,
            summary=summary if runtime_ok else "; ".join(runtime_issues),
            locations=["national_bot.py", "precompute.py"],
        )

    checks: list[dict[str, Any]] = [
        _check(
            "national_policy_module",
            policy_module_ok,
            guidance=(
                "Provide the exact five executable/identity Bot files only; remove retired "
                "ABI files, helpers, and candidate-owned/unbound assets."
            ),
            summary=(
                "policy.py is the sole active candidate decision module"
                if policy_module_ok
                else (
                    "policy_missing_or_forbidden_python_present:"
                    f"retired={retired_present}:extra={extra_python}:layout={layout_errors}"
                )
            ),
            locations=["policy.py", *retired_present, *extra_python, *layout_errors],
            details={"strict_artifact_layout_errors": layout_errors},
        ),
        _check(
            "system_runtime_current",
            runtime_ok,
            guidance="Regenerate the system-owned runtime; candidate workers may not edit it.",
            summary="exact current system runtime bytes" if runtime_ok else "; ".join(runtime_issues),
            locations=["national_bot.py", "precompute.py"],
            details={"sha256": runtime_hashes},
        ),
        _check(
            "policy_baseline_entrypoint",
            baseline_ok,
            guidance="Define synchronous get_baseline_decision(context) in policy.py.",
            summary="exact baseline signature" if baseline_ok else "baseline signature missing or widened",
            locations=["policy.py:get_baseline_decision"],
        ),
        _check(
            "fast_policy_baseline",
            (
                baseline_ok
                and candidate_io_ok
                and table_ok
                and baseline_enumeration_ok
                and baseline_evaluator_alias_ok
            ),
            guidance=(
                "Keep get_baseline_decision synchronous, I/O-free, free of "
                "oversized construction, and free of full opponent-hole "
                "enumeration or system-evaluator aliases; place complete river "
                "work only in deadline-checked iter_decisions."
            ),
            summary=(
                "bounded synchronous policy baseline"
                if (
                    baseline_ok
                    and candidate_io_ok
                    and table_ok
                    and baseline_enumeration_ok
                    and baseline_evaluator_alias_ok
                )
                else (
                    "baseline is missing, blocking, performs forbidden work, "
                    "reaches full opponent enumeration, or aliases a system evaluator"
                )
            ),
            locations=[
                "policy.py:get_baseline_decision",
                *static["baseline_full_enumeration_locations"],
                *static["evaluator_alias_locations"],
            ],
            details={
                "baseline_full_enumeration_locations": static[
                    "baseline_full_enumeration_locations"
                ],
                "evaluator_alias_locations": static[
                    "evaluator_alias_locations"
                ],
            },
        ),
        _check(
            "policy_refinement_entrypoint",
            refinement_ok,
            guidance="Define synchronous iter_decisions(context, baseline, deadline) in policy.py.",
            summary="exact refinement signature" if refinement_ok else "refinement signature missing or widened",
            locations=["policy.py:iter_decisions"],
        ),
        _check(
            "decision_context_v1",
            runtime_ok and context_used,
            guidance="Consume only the schema-versioned decision_context v1 object.",
            summary=(
                "system constructs decision_context v1 and policy consumes context"
                if runtime_ok and context_used
                else "context provider or consumer missing"
            ),
            locations=["national_bot.py:_decision_context", "policy.py"],
            details={
                "schema_version": DECISION_CONTEXT_SCHEMA_VERSION,
                "top_level_fields": list(CONTEXT_FIELDS),
                "consumed_fields": sorted(context_fields),
            },
        ),
        _check(
            "typed_intent_v1",
            typed_ok,
            guidance=(
                "Return only {'kind': pass|fold|allin} or "
                "{'kind': 'raise', 'raise_to': int}; never return wire strings or integers."
            ),
            summary=(
                "closed typed-intent vocabulary"
                if typed_ok
                else "scalar/direct-wire/unknown intent evidence detected"
            ),
            locations=[
                "policy.py",
                *static["integer_return_locations"],
                *static["bare_action_return_locations"],
                *static["raise_dict_locations"],
            ],
            details={
                "observed_kind_literals": sorted(static["kind_literals"]),
                "forbidden_kind_literals": forbidden_kind_literals,
                "bare_action_return_locations": static[
                    "bare_action_return_locations"
                ],
            },
        ),
        system_check(
            "socket_owner_action_mapping",
            "socket owner maps pass and validates typed intents",
            "Restore the exact system runtime action mapper.",
        ),
        system_check(
            "raw_tcp_stream_decoder",
            "delimiter-free fragmented/sticky TCP stream decoder",
            "Restore the exact system runtime stream decoder.",
        ),
        system_check(
            "exact_raise_to_boundary",
            "raise intent uses exact stage-total raise_to and official 2x boundary",
            "Restore the exact system runtime raise-to validator.",
        ),
        system_check(
            "decision_time_budget_visible",
            "decision_context includes monotonic deadline evidence",
            "Restore system-owned decision timing context.",
        ),
        system_check(
            "killable_decision_runtime",
            "policy runs in the system killable decision worker",
            "Restore the exact killable system decision runtime.",
        ),
        _check(
            "decision_path_no_external_io",
            candidate_io_ok,
            guidance="Remove candidate network, file, subprocess, and dynamic-code I/O.",
            summary="candidate policy is I/O-free" if candidate_io_ok else "forbidden candidate I/O",
            locations=[*static["forbidden_imports"], *static["forbidden_calls"]],
        ),
        _check(
            "decision_path_no_full_history_scan",
            history_ok,
            guidance=(
                "Use bounded decision_context line/opponent snapshots; never iterate, "
                "copy, aggregate, or indirectly rescan full decision_context.history."
            ),
            summary=(
                "no full-history reconstruction or traversal"
                if history_ok
                else "full-history reconstruction or traversal detected"
            ),
            locations=[
                *(f"policy.py:{name}" for name in sorted(history_identifier_issues)),
                *static["history_scan_locations"],
            ],
        ),
        _check(
            "decision_path_no_large_runtime_tables",
            table_ok,
            guidance="Move bounded immutable facts to approved system assets or compact policy helpers.",
            summary="no oversized policy literal" if table_ok else "oversized policy literal",
            locations=static["large_literal_locations"],
        ),
        system_check(
            "persistent_match_memory",
            "connection-scoped incremental opponent tracker",
            "Restore the exact system tracker runtime.",
        ),
        system_check(
            "terminal_response_memory",
            "omitted closer and terminal settlement updates are system-owned",
            "Restore the exact system terminal-response tracker.",
        ),
        system_check(
            "showdown_range_posterior",
            "showdown cards update bounded opponent posterior",
            "Restore the exact system showdown tracker.",
        ),
        system_check(
            "authoritative_hand_context",
            "pot, stacks, street closure, SPR and legality are system reconstructed",
            "Restore the exact system decision_context builder.",
        ),
        _check(
            "policy_consumes_legal_context",
            "legal" in context_fields,
            guidance="Read decision_context.legal before selecting pass/allin/raise intents.",
            summary="policy reads legal context" if "legal" in context_fields else "legal context not read",
            locations=["policy.py"],
        ),
        _check(
            "policy_consumes_betting_context",
            "betting" in context_fields,
            guidance="Read decision_context.betting for pot, stacks, to_call, SPR and raise sizing.",
            summary="policy reads betting context" if "betting" in context_fields else "betting context not read",
            locations=["policy.py"],
        ),
        _check(
            "incremental_opponent_model",
            "opponent" in context_fields,
            guidance=(
                "Consume the bounded decision_context.opponent snapshot and prove "
                "confidence-gated causal influence on a legal typed intent at the wire."
            ),
            summary=(
                "opponent snapshot is consumed; managed causal proof is pending"
                if "opponent" in context_fields
                else "opponent snapshot is not consumed"
            ),
            locations=["policy.py"],
            required=True,
        ),
    ]

    advisory_states = {
        "incremental_refinement_protocol": refinement_ok,
        "budget_scaled_refinement": refinement_ok and "deadline" in static["loaded_names"],
        "precompute_lookup_path": "precompute" in static["imports"],
        "precompute_runtime_influence": "precompute" in static["imports"],
        "terminal_response_adaptation": "opponent" in context_fields,
        "showdown_range_adaptation": "opponent" in context_fields,
        "donk_line_reachability": "line" in context_fields,
        "delayed_probe_line_reachability": "line" in context_fields,
    }
    for check_id in ADVISORY_CHECKS:
        checks.append(_check(
            check_id,
            advisory_states[check_id],
            guidance=f"Add reachable policy evidence for {check_id} when selected as the generation focus.",
            summary="reachable context/entrypoint evidence" if advisory_states[check_id] else "not statically demonstrated",
            locations=["policy.py"],
            required=False,
        ))

    checks_by_id = {item["check_id"]: item for item in checks}
    required_failures = [
        item for item in checks
        if item["required"] and not item["passed"]
    ]
    advisory_warnings = [
        item for item in checks
        if not item["required"] and not item["passed"]
    ]
    probe_subject = {
        "schema_version": 1,
        "orchestrator_version": "national-policy-static-abi-v1",
        "context_schema_version": DECISION_CONTEXT_SCHEMA_VERSION,
        "context_fields": list(CONTEXT_FIELDS),
        "intent_kinds": list(INTENT_KINDS),
    }
    dynamic_runtime_probe = {
        **probe_subject,
        "scenario_digest": _canonical_digest({
            "contexts": ["preflop", "flop", "turn", "river"],
            "transport": ["fragmented", "sticky", "no_newline"],
        }),
        "limits_digest": _canonical_digest({
            "candidate_import": False,
            "max_policy_literal_entries": 4096,
        }),
        "probe_identity_digest": _canonical_digest(probe_subject),
        "artifacts": [],
    }
    conclusive = not infrastructure_failures
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
        "epoch": "national_tcp_policy_v1",
        "bot_dir": str(root),
        "source_digest": _source_digest(sources),
        "conclusive": conclusive,
        "ok": conclusive and not required_failures,
        "outcome": (
            "passed"
            if conclusive and not required_failures
            else "failed"
            if conclusive
            else "infrastructure_failure"
        ),
        "checks": checks,
        "checks_by_id": checks_by_id,
        "required_checks": list(REQUIRED_CHECKS),
        "required_failures": required_failures,
        "advisory_warnings": advisory_warnings,
        "passed_checks": [item["check_id"] for item in checks if item["passed"]],
        "infrastructure_failures": infrastructure_failures,
        "dynamic_runtime_probe": dynamic_runtime_probe,
        "precompute_evidence": {
            "consumed_artifacts": [],
            "system_owned": True,
        },
        "policy_abi": {
            "module": "policy.py",
            "entrypoints": {key: list(value) for key, value in POLICY_ENTRYPOINTS.items()},
            "context_schema_version": DECISION_CONTEXT_SCHEMA_VERSION,
            "context_fields": list(CONTEXT_FIELDS),
            "intent_kinds": list(INTENT_KINDS),
            "raise_field": "raise_to",
            "pass_mapping": "socket_owner_call_or_check",
        },
    }


def national_runtime_feedback_summary(
    capabilities: dict[str, Any] | None,
    *,
    max_items: int = 8,
) -> str:
    """Render bounded, actionable policy-ABI feedback for planning prompts."""

    if not isinstance(capabilities, dict):
        return "National policy capability evidence unavailable (fail closed)."
    failures = list(capabilities.get("required_failures") or [])
    warnings = list(capabilities.get("advisory_warnings") or [])
    lines = [
        "National TCP policy capability contract:",
        f"- epoch: {capabilities.get('epoch', 'unknown')}",
        f"- detector: {capabilities.get('detector_version', 'unknown')}",
        f"- required failures: {len(failures)}",
        f"- advisory gaps: {len(warnings)}",
    ]
    for item in [*failures, *warnings][:max_items]:
        lines.append(
            f"- {item.get('check_id')}: {item.get('guidance') or 'close this policy capability'}"
        )
    return "\n".join(lines)


__all__ = [
    "ADVISORY_CHECKS",
    "CAPABILITY_SCHEMA_VERSION",
    "CONTEXT_FIELDS",
    "DECISION_CONTEXT_SCHEMA_VERSION",
    "INTENT_KINDS",
    "NATIONAL_CAPABILITY_DETECTOR_VERSION",
    "POLICY_ENTRYPOINTS",
    "REQUIRED_CHECKS",
    "evaluate_national_capabilities",
    "national_runtime_feedback_summary",
]
