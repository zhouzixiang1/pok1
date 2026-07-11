"""Evidence-backed architecture contract for national-native bots.

Protocol legality is owned by the validator and official EXE. This module
evaluates whether a candidate uses the long-lived national runtime deliberately:
bounded decision work, reusable pure facts, and connection-level opponent state.
Checks are stable identifiers so policy can compare a candidate with its parent.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any


NATIONAL_CAPABILITY_DETECTOR_VERSION = "4.3.0"
CAPABILITY_SCHEMA_VERSION = 5
DECISION_GRAPH_MAX_DEPTH = 5
MIN_PRECOMPUTE_ENTRIES = 20
MAX_PRECOMPUTE_ENTRIES = 65_536
LARGE_DECISION_COLLECTION_SIZE = 128

_FULL_MATCH_SEQUENCE_NAMES = frozenset({
    "requests",
    "responses",
    "_requests",
    "_responses",
    "showdowns",
    "_showdowns",
    "opponent_showdowns",
})
_ARTIFACT_NAME_MARKERS = (
    "lookup",
    "table",
    "precompute",
    "equity_grid",
    "hand_class",
    "rank_cache",
    "texture_cache",
    "combo_fact",
    "straight_high",
    "five_of_seven",
)
_OPPONENT_RUNTIME_CORE_FIELDS = frozenset({
    "confidence",
    "adaptation_weight",
    "vpip",
    "pfr",
    "allin_rate",
    "postflop_aggr",
    "postflop_check_rate",
    "fold_to_raise",
    "aggression",
    "avg_raise_bb",
    "raise_samples",
    "flop_aggr",
    "turn_aggr",
    "river_aggr",
    "fold_to_jam_rate",
    "fold_to_jam_samples",
    "river_overcall_freq",
    "river_overcall_samples",
    "terminal_response",
    "showdown_range",
})

# Runtime-contract reference cards must be tied to real request reads, rather
# than to string literals which happen to name the same fields.  Native strategy
# entry points conventionally receive this object as ``req``; ``request`` and
# the two explicit long-form variants cover thin forwarding helpers without
# treating arbitrary local dictionaries as authoritative runtime state.
_REQUEST_ROOT_PARAMETER_NAMES = frozenset({
    "req",
    "request",
    "request_data",
    "request_payload",
})
_LIVE_RUNTIME_ROOTS = frozenset({"hand_runtime", "opponent_runtime"})


def _read_python_sources(bot_dir: str | Path) -> dict[str, str]:
    root = Path(bot_dir)
    sources: dict[str, str] = {}
    read_errors = []
    for path in sorted(root.glob("*.py")):
        try:
            sources[path.name] = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            read_errors.append(f"{path.name}:{type(exc).__name__}:{str(exc)[:180]}")
    if read_errors:
        raise OSError("capability source read failed: " + "; ".join(read_errors[:8]))
    return sources


def _parse_sources(sources: dict[str, str]) -> dict[str, ast.Module]:
    trees: dict[str, ast.Module] = {}
    for filename, text in sources.items():
        try:
            trees[filename] = ast.parse(text, filename=filename)
        except SyntaxError:
            continue
    return trees


def _regex(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) is not None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _expr_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            names.add(item.id.lower())
        elif isinstance(item, ast.Attribute):
            names.add(item.attr.lower())
    return names


def _constant_int(node: ast.AST | None) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return int(node.value)
    return None


def _range_size(node: ast.AST) -> int | None:
    if not isinstance(node, ast.Call) or _call_name(node.func).rsplit(".", 1)[-1] != "range":
        return None
    values = [_constant_int(arg) for arg in node.args]
    if not values or any(value is None for value in values):
        return None
    ints = [int(value) for value in values if value is not None]
    if len(ints) == 1:
        start, stop, step = 0, ints[0], 1
    elif len(ints) == 2:
        start, stop, step = ints[0], ints[1], 1
    else:
        start, stop, step = ints[0], ints[1], ints[2]
    if step == 0:
        return None
    return len(range(start, stop, step))


def _comprehension_size(generators: list[ast.comprehension]) -> int | None:
    size = 1
    for generator in generators:
        part = _range_size(generator.iter)
        if part is None:
            return None
        size *= part
        if size > MAX_PRECOMPUTE_ENTRIES:
            return size
    return size


def _static_collection_size(node: ast.AST) -> int | None:
    if isinstance(node, ast.Dict):
        return len(node.keys)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return len(node.elts)
    if isinstance(node, (ast.DictComp, ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        return _comprehension_size(node.generators)
    if isinstance(node, ast.Call) and node.args:
        terminal = _call_name(node.func).rsplit(".", 1)[-1]
        if terminal in {"dict", "list", "tuple", "set", "frozenset"}:
            return _static_collection_size(node.args[0])
    return None


def _line_label(filename: str, node: ast.AST, detail: str) -> str:
    return f"{filename}:L{getattr(node, 'lineno', '?')}:{detail}"


def _target_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for item in node.elts:
            names.update(_target_names(item))
    return names


def _loaded_identifiers(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    names: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load):
            names.add(item.id)
        elif isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Load):
            names.add(item.attr)
    return names


def _string_values(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {
        str(item.value)
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _call_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {
        _call_name(item.func)
        for item in ast.walk(node)
        if isinstance(item, ast.Call) and _call_name(item.func)
    }


class _FunctionNodeCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.nodes: list[ast.AST] = []

    def generic_visit(self, node: ast.AST) -> Any:
        self.nodes.append(node)
        return super().generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        return None

    def visit_Lambda(self, node: ast.Lambda) -> Any:
        return None


def _function_body_nodes(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    collector = _FunctionNodeCollector()
    for statement in node.body:
        collector.visit(statement)
    return collector.nodes


def _literal_string(node: ast.AST | None) -> str | None:
    """Return one literal dictionary key, if this expression has one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return str(node.value)
    # ``ast.Index`` disappeared in Python 3.9, but accepting its legacy shape
    # costs almost nothing and keeps the detector usable on archived workers.
    legacy_value = getattr(node, "value", None)
    if type(node).__name__ == "Index":
        return _literal_string(legacy_value)
    return None


def _source_rooted_expr_paths(
    node: ast.AST | None,
    aliases: dict[str, set[tuple[str, ...]]],
) -> set[tuple[str, ...]]:
    """Trace literal dict access chains back to a request-root alias.

    This deliberately recognizes concrete ``req.get('hand_runtime')`` /
    ``req['hand_runtime']`` style accesses, including aliases such as
    ``hand = req.get(...)``.  It does *not* infer evidence from a bare string
    literal, a variable name, or a comment.  The caller later retains only
    paths which reach an action sink.
    """
    if node is None:
        return set()
    if isinstance(node, ast.Name):
        return set(aliases.get(node.id, set()))
    if isinstance(node, ast.Subscript):
        base_paths = _source_rooted_expr_paths(node.value, aliases)
        key = _literal_string(node.slice)
        if key is None:
            return base_paths
        return {(*path, key) for path in base_paths}
    if isinstance(node, ast.Call):
        # ``mapping.get('field', default)`` is the dominant runtime-request
        # access idiom in native strategies.  Only its receiver and literal
        # key define the source path; the fallback is not treated as a live
        # input for this purpose.
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            base_paths = _source_rooted_expr_paths(node.func.value, aliases)
            key = _literal_string(node.args[0])
            if key is None:
                return base_paths
            return {(*path, key) for path in base_paths}
        # A strategy may cast or combine a live scalar before comparing it at
        # the action sink.  Propagate concrete request paths through call
        # arguments so ``float(terminal.get('confidence', 0.0))`` remains
        # inspectable.  This still requires an actual source-rooted access.
        paths: set[tuple[str, ...]] = set()
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            paths.update(_source_rooted_expr_paths(argument, aliases))
        return paths
    if isinstance(node, ast.Attribute):
        return _source_rooted_expr_paths(node.value, aliases)

    # Arithmetic, comparisons, boolean expressions, conditional expressions,
    # literals nested in containers, and comprehensions can all transport a
    # previously proven request value into an action.  Recursing through their
    # children is safe because only Name aliases established from a request
    # root can create a path.
    paths: set[tuple[str, ...]] = set()
    for child in ast.iter_child_nodes(node):
        paths.update(_source_rooted_expr_paths(child, aliases))
    return paths


def _source_rooted_live_access_paths(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    """Return live request paths that syntactically reach an action sink.

    A field read into a dead local is deliberately omitted.  Reads qualify only
    when they reach a ``return``/``yield`` expression or control a branch which
    returns/yields an action.  This is intentionally a conservative, local
    data-flow approximation: dynamic probes remain the proof that the observed
    field actually changes a final sanitized wire action.
    """
    arguments = (
        [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if hasattr(node.args, "posonlyargs")
        else [*node.args.args, *node.args.kwonlyargs]
    )
    aliases: dict[str, set[tuple[str, ...]]] = {
        argument.arg: {()}
        for argument in arguments
        if argument.arg.lower() in _REQUEST_ROOT_PARAMETER_NAMES
    }
    if not aliases:
        return []

    body_nodes = _function_body_nodes(node)
    assignments: list[tuple[set[str], ast.AST]] = []
    for item in body_nodes:
        if isinstance(item, ast.Assign):
            targets: set[str] = set()
            for target in item.targets:
                targets.update(_target_names(target))
            if targets:
                assignments.append((targets, item.value))
        elif isinstance(item, ast.AnnAssign):
            targets = _target_names(item.target)
            if targets and item.value is not None:
                assignments.append((targets, item.value))
        elif isinstance(item, ast.AugAssign):
            targets = _target_names(item.target)
            if targets:
                assignments.append((targets, item.value))

    # Resolve aliases to a small fixed point.  Assignment order is normally
    # sufficient, while the fixed point also covers simple forwarding aliases.
    for _ in range(max(1, len(assignments) + 1)):
        changed = False
        for targets, value in assignments:
            paths = _source_rooted_expr_paths(value, aliases)
            for target in targets:
                before = len(aliases.get(target, set()))
                aliases.setdefault(target, set()).update(paths)
                changed = changed or len(aliases[target]) != before
        if not changed:
            break

    returned_names: set[str] = set()
    for item in body_nodes:
        if isinstance(item, ast.Return) and item.value is not None:
            returned_names.update(_loaded_identifiers(item.value))
        elif isinstance(item, (ast.Yield, ast.YieldFrom)) and item.value is not None:
            returned_names.update(_loaded_identifiers(item.value))

    sink_paths: set[tuple[str, ...]] = set()

    class _SinkVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.control_paths: list[set[tuple[str, ...]]] = []

        def _active_controls(self) -> set[tuple[str, ...]]:
            result: set[tuple[str, ...]] = set()
            for paths in self.control_paths:
                result.update(paths)
            return result

        def visit_FunctionDef(self, nested: ast.FunctionDef) -> Any:
            # Nested functions are independent decision profiles when reachable;
            # do not let their closures claim evidence for this function.
            return None

        def visit_AsyncFunctionDef(self, nested: ast.AsyncFunctionDef) -> Any:
            return None

        def visit_If(self, item: ast.If) -> Any:
            self.control_paths.append(_source_rooted_expr_paths(item.test, aliases))
            for statement in item.body:
                self.visit(statement)
            for statement in item.orelse:
                self.visit(statement)
            self.control_paths.pop()

        def visit_While(self, item: ast.While) -> Any:
            self.control_paths.append(_source_rooted_expr_paths(item.test, aliases))
            for statement in item.body:
                self.visit(statement)
            for statement in item.orelse:
                self.visit(statement)
            self.control_paths.pop()

        def visit_Assign(self, item: ast.Assign) -> Any:
            # Support the common ``if live_field: action = ...; return action``
            # form without crediting a dead temporary.  Only variables that are
            # later returned/yielded receive active-control provenance.
            active = self._active_controls()
            if active:
                for target in item.targets:
                    for name in _target_names(target):
                        if name in returned_names:
                            aliases.setdefault(name, set()).update(active)
            self.generic_visit(item)

        def visit_AnnAssign(self, item: ast.AnnAssign) -> Any:
            active = self._active_controls()
            if active:
                for name in _target_names(item.target):
                    if name in returned_names:
                        aliases.setdefault(name, set()).update(active)
            self.generic_visit(item)

        def visit_Return(self, item: ast.Return) -> Any:
            sink_paths.update(_source_rooted_expr_paths(item.value, aliases))
            sink_paths.update(self._active_controls())

        def visit_Yield(self, item: ast.Yield) -> Any:
            sink_paths.update(_source_rooted_expr_paths(item.value, aliases))
            sink_paths.update(self._active_controls())

        def visit_YieldFrom(self, item: ast.YieldFrom) -> Any:
            sink_paths.update(_source_rooted_expr_paths(item.value, aliases))
            sink_paths.update(self._active_controls())

    visitor = _SinkVisitor()
    for statement in node.body:
        visitor.visit(statement)

    return sorted({
        ".".join(path)
        for path in sink_paths
        if len(path) >= 2 and path[0] in _LIVE_RUNTIME_ROOTS
    })


def _function_contract_evidence(
    filename: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, Any]:
    """Approximate value-flow into action-affecting sinks inside one function."""
    nodes = _function_body_nodes(node)
    dependencies: dict[str, set[str]] = {}
    runtime_vars: set[str] = set()
    confidence_vars: set[str] = set()
    baseline_vars: set[str] = set()
    clock_vars: set[str] = set()
    deadline_vars: set[str] = set()
    finite_bound_vars: set[str] = set()
    baseline_lines: list[int] = []
    deadline_assignment_lines: list[int] = []

    assignments: list[tuple[set[str], ast.AST, ast.AST]] = []
    for item in nodes:
        targets: set[str] = set()
        value = None
        if isinstance(item, ast.Assign):
            for target in item.targets:
                targets.update(_target_names(target))
            value = item.value
        elif isinstance(item, ast.AnnAssign):
            targets.update(_target_names(item.target))
            value = item.value
        if targets and value is not None:
            assignments.append((targets, value, item))
            deps = _loaded_identifiers(value)
            for target in targets:
                dependencies[target] = set(deps)

    for _ in range(max(1, len(assignments) + 1)):
        changed = False
        for targets, value, assignment in assignments:
            deps = _loaded_identifiers(value)
            strings = {item.lower() for item in _string_values(value)}
            calls = {item.rsplit(".", 1)[-1] for item in _call_names(value)}
            runtime_source = "opponent_runtime" in strings or bool(deps & runtime_vars)
            confidence_source = bool(
                strings.intersection({"confidence", "adaptation_weight"})
                or deps & confidence_vars
            )
            timing_source = bool(calls.intersection({"monotonic", "perf_counter"}))
            for target in targets:
                lowered = target.lower()
                if runtime_source and target not in runtime_vars:
                    runtime_vars.add(target)
                    changed = True
                if confidence_source and target not in confidence_vars:
                    confidence_vars.add(target)
                    changed = True
                if any(marker in lowered for marker in ("baseline", "fallback")):
                    baseline_vars.add(target)
                    baseline_lines.append(getattr(assignment, "lineno", 0))
                if timing_source and target not in clock_vars:
                    clock_vars.add(target)
                    changed = True
                if (
                    "deadline" in lowered
                    and (timing_source or bool(deps & (clock_vars | deadline_vars)))
                    and target not in deadline_vars
                ):
                    deadline_vars.add(target)
                    deadline_assignment_lines.append(getattr(assignment, "lineno", 0))
                    changed = True
                if any(marker in lowered for marker in ("max_samples", "sample_limit", "iteration_limit")):
                    bound = _constant_int(value)
                    if bound is not None and 1 <= bound <= 100_000:
                        finite_bound_vars.add(target)
        if not changed:
            break

    def expanded(names: set[str]) -> set[str]:
        result = set(names)
        for _ in range(max(1, len(dependencies) + 1)):
            before = len(result)
            for name in list(result):
                result.update(dependencies.get(name, set()))
            if len(result) == before:
                break
        return result

    scaled_runtime_vars: set[str] = set()

    def has_confidence_scale(value: ast.AST) -> bool:
        for item in ast.walk(value):
            if not isinstance(item, ast.BinOp) or not isinstance(item.op, ast.Mult):
                continue
            if (
                isinstance(item.left, ast.Constant)
                and isinstance(item.left.value, (int, float))
                and float(item.left.value) == 0.0
            ) or (
                isinstance(item.right, ast.Constant)
                and isinstance(item.right.value, (int, float))
                and float(item.right.value) == 0.0
            ):
                continue
            left_deps = expanded(_loaded_identifiers(item.left))
            right_deps = expanded(_loaded_identifiers(item.right))
            left_strings = {text.lower() for text in _string_values(item.left)}
            right_strings = {text.lower() for text in _string_values(item.right)}
            left_weight = bool(
                left_deps & confidence_vars
                or left_strings.intersection({"confidence", "adaptation_weight"})
            )
            right_weight = bool(
                right_deps & confidence_vars
                or right_strings.intersection({"confidence", "adaptation_weight"})
            )
            if left_weight or right_weight:
                return True
        return False

    for _ in range(max(1, len(assignments) + 1)):
        changed = False
        for targets, value, _assignment in assignments:
            deps = expanded(_loaded_identifiers(value))
            scaled = has_confidence_scale(value) or bool(deps & scaled_runtime_vars)
            if scaled:
                for target in targets:
                    if target not in scaled_runtime_vars:
                        scaled_runtime_vars.add(target)
                        changed = True
        if not changed:
            break

    sink_nodes = [
        item.value
        for item in nodes
        if isinstance(item, ast.Return) and item.value is not None
    ]
    sink_dependencies = set()
    weighted_runtime_locations = []
    fallback_locations = []
    for sink in sink_nodes:
        deps = expanded(_loaded_identifiers(sink))
        strings = {item.lower() for item in _string_values(sink)}
        direct_runtime = "opponent_runtime" in strings
        direct_confidence = bool(strings.intersection({"confidence", "adaptation_weight"}))
        runtime_used = direct_runtime or bool(deps & runtime_vars)
        confidence_used = direct_confidence or bool(deps & confidence_vars)
        scaled = has_confidence_scale(sink) or bool(deps & scaled_runtime_vars)
        if runtime_used and confidence_used and scaled:
            weighted_runtime_locations.append(
                _line_label(filename, sink, "opponent_runtime_weighted_action_dependency")
            )
        if deps & baseline_vars:
            fallback_locations.append(_line_label(filename, sink, "baseline_return_dependency"))
        sink_dependencies.update(deps)

    deadline_check_lines = []
    deadline_check_locations = []
    finite_bound_locations = []
    for item in nodes:
        test = None
        if isinstance(item, ast.Compare):
            test = item
        elif isinstance(item, (ast.If, ast.While)):
            test = item.test
        if test is not None:
            deps = _loaded_identifiers(test)
            calls = {name.rsplit(".", 1)[-1] for name in _call_names(test)}
            if deps & deadline_vars and (
                isinstance(test, ast.Compare)
                or calls.intersection({"monotonic", "perf_counter"})
            ):
                deadline_check_lines.append(getattr(item, "lineno", 0))
                deadline_check_locations.append(_line_label(filename, item, "monotonic_deadline_check"))
            if deps & finite_bound_vars:
                finite_bound_locations.append(_line_label(filename, item, "finite_work_bound"))
        if isinstance(item, (ast.For, ast.comprehension)):
            iterator = item.iter
            size = _range_size(iterator)
            names = _loaded_identifiers(iterator)
            if (size is not None and 1 <= size <= 100_000) or names & finite_bound_vars:
                finite_bound_locations.append(_line_label(filename, item, "finite_iteration_bound"))

    baseline_function = "baseline" in node.name.lower() and any(
        isinstance(item, ast.Return) for item in nodes
    )
    if baseline_function:
        baseline_lines.append(getattr(node, "lineno", 0))
        fallback_locations.append(_line_label(filename, node, "baseline_function_return"))
    baseline_before_deadline = bool(baseline_lines) and bool(deadline_check_lines)
    if baseline_before_deadline and node.name.lower().find("baseline") < 0:
        baseline_before_deadline = min(baseline_lines) < max(deadline_check_lines)

    return {
        "sink_dependencies": sorted(sink_dependencies),
        "source_rooted_live_access_paths": _source_rooted_live_access_paths(node),
        "weighted_runtime_locations": sorted(dict.fromkeys(weighted_runtime_locations)),
        "deadline_assignments": [
            _line_label(filename, node, f"deadline_assignment_line[{line}]")
            for line in sorted(set(deadline_assignment_lines))
        ],
        "deadline_checks": sorted(dict.fromkeys(deadline_check_locations)),
        "finite_work_bounds": sorted(dict.fromkeys(finite_bound_locations)),
        "baseline_locations": [
            _line_label(filename, node, f"baseline_line[{line}]")
            for line in sorted(set(baseline_lines))
        ],
        "fallback_locations": sorted(dict.fromkeys(fallback_locations)),
        "baseline_before_deadline": baseline_before_deadline,
    }


class _FunctionVisitor(ast.NodeVisitor):
    _EXTERNAL_IO_CALLS = {
        "open",
        "input",
        "socket.socket",
        "socket.create_connection",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "os.open",
        "os.popen",
        "os.system",
        "mmap.mmap",
        "pickle.load",
        "requests.get",
        "requests.post",
        "urllib.request.urlopen",
    }
    _FILE_METHODS = {
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "open",
        "unlink",
        "rename",
        "replace",
    }
    _COMBINATORIAL_CALLS = {"combinations", "combinations_with_replacement", "permutations", "product"}

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.external_io: list[str] = []
        self.history_scans: list[str] = []
        self.large_runtime_tables: list[str] = []
        self.calls: list[str] = []
        self.read_names: set[str] = set()
        self.string_literals: set[str] = set()
        self.full_history_aliases: set[str] = set(_FULL_MATCH_SEQUENCE_NAMES)

    def _is_full_history_expr(self, node: ast.AST | None) -> bool:
        if node is None:
            return False
        return bool({name.lower() for name in _expr_names(node)} & self.full_history_aliases)

    def visit_Name(self, node: ast.Name) -> Any:
        if isinstance(node.ctx, ast.Load):
            self.read_names.add(node.id)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, str):
            self.string_literals.add(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        return None

    def visit_Call(self, node: ast.Call) -> Any:
        name = _call_name(node.func)
        if name:
            self.calls.append(name)
        terminal = name.rsplit(".", 1)[-1] if name else ""
        if name in self._EXTERNAL_IO_CALLS or terminal in self._FILE_METHODS:
            self.external_io.append(_line_label(self.filename, node, name or terminal))
        if terminal in self._COMBINATORIAL_CALLS:
            self.large_runtime_tables.append(
                _line_label(self.filename, node, f"combinatorial_call[{terminal}]")
            )
        if any(self._is_full_history_expr(arg) for arg in node.args):
            self.history_scans.append(
                _line_label(self.filename, node, f"full_match_argument[{terminal or name}]")
            )
        self.generic_visit(node)

    def _record_alias(self, targets: list[ast.AST], value: ast.AST | None) -> None:
        if not self._is_full_history_expr(value):
            return
        for target in targets:
            for name in _target_names(target):
                self.full_history_aliases.add(name.lower())

    def visit_Assign(self, node: ast.Assign) -> Any:
        self._record_alias(list(node.targets), node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        self._record_alias([node.target], node.value)
        self.generic_visit(node)

    def _record_loop(self, node: ast.AST, iterator: ast.AST) -> None:
        names = {name.lower() for name in _expr_names(iterator)}
        matched = sorted(names.intersection(self.full_history_aliases))
        if matched or self._is_full_history_expr(iterator):
            self.history_scans.append(
                _line_label(
                    self.filename,
                    node,
                    f"full_match_scan[{','.join(matched) or 'aliased_sequence'}]",
                )
            )
        size = _range_size(iterator)
        if size is not None and size >= LARGE_DECISION_COLLECTION_SIZE:
            self.large_runtime_tables.append(
                _line_label(self.filename, node, f"runtime_range[{size}]")
            )

    def visit_For(self, node: ast.For) -> Any:
        self._record_loop(node, node.iter)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> Any:
        self._record_loop(node, node.iter)
        self.generic_visit(node)

    def _record_collection(self, node: ast.AST, kind: str) -> None:
        size = _static_collection_size(node)
        if size is not None and size >= LARGE_DECISION_COLLECTION_SIZE:
            self.large_runtime_tables.append(
                _line_label(self.filename, node, f"runtime_{kind}[{size}]")
            )

    def visit_Dict(self, node: ast.Dict) -> Any:
        self._record_collection(node, "dict")
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> Any:
        self._record_collection(node, "list")
        self.generic_visit(node)

    def visit_Set(self, node: ast.Set) -> Any:
        self._record_collection(node, "set")
        self.generic_visit(node)

    def visit_DictComp(self, node: ast.DictComp) -> Any:
        self._record_collection(node, "dictcomp")
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> Any:
        self._record_collection(node, "listcomp")
        self.generic_visit(node)

    def visit_SetComp(self, node: ast.SetComp) -> Any:
        self._record_collection(node, "setcomp")
        self.generic_visit(node)


def _likely_decision_function_name(function_name: str) -> bool:
    name = function_name.lower()
    markers = (
        "get_action",
        "decide",
        "decision",
        "choose_action",
        "select_action",
        "baseline_action",
        "refine_action",
        "iter_refinement",
        "strategy_action",
        "strategy_worker",
    )
    return name in {"act", "action"} or any(marker in name for marker in markers)


def _function_profiles(
    trees: dict[str, ast.Module],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    profiles: dict[str, dict[str, Any]] = {}
    by_name: dict[str, list[str]] = {}
    for filename, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            visitor = _FunctionVisitor(filename)
            for statement in node.body:
                visitor.visit(statement)
            contract_evidence = _function_contract_evidence(filename, node)
            key = f"{filename}:{node.name}"
            profiles[key] = {
                "filename": filename,
                "name": node.name,
                "calls": list(dict.fromkeys(visitor.calls)),
                "external_io": visitor.external_io,
                "history_scans": visitor.history_scans,
                "large_runtime_tables": visitor.large_runtime_tables,
                "read_names": sorted(visitor.read_names),
                "string_literals": sorted(visitor.string_literals),
                **contract_evidence,
                "is_decision": _likely_decision_function_name(node.name),
            }
            by_name.setdefault(node.name, []).append(key)
    return profiles, by_name


def _resolve_call_targets(call_name: str, by_name: dict[str, list[str]]) -> list[str]:
    terminal = call_name.rsplit(".", 1)[-1]
    return list(dict.fromkeys([*by_name.get(call_name, []), *by_name.get(terminal, [])]))


def _decision_graph(
    profiles: dict[str, dict[str, Any]],
    by_name: dict[str, list[str]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    roots = sorted(key for key, profile in profiles.items() if profile.get("is_decision"))
    chains: dict[str, list[str]] = {root: [root] for root in roots}
    stack = [(root, [root], 0) for root in roots]
    while stack:
        key, chain, depth = stack.pop()
        if depth >= DECISION_GRAPH_MAX_DEPTH:
            continue
        profile = profiles.get(key) or {}
        for call_name in profile.get("calls") or []:
            for target in _resolve_call_targets(call_name, by_name):
                if target in chain:
                    continue
                next_chain = [*chain, target]
                previous = chains.get(target)
                if previous is None or len(next_chain) < len(previous):
                    chains[target] = next_chain
                    stack.append((target, next_chain, depth + 1))
    return {key: chains[key] for key in sorted(chains)}, {root: [root] for root in roots}


def _format_path(chain: list[str], evidence: str) -> str:
    return "->".join([*chain, evidence])


def _decision_path_risks(
    trees: dict[str, ast.Module],
    profiles: dict[str, dict[str, Any]],
    decision_chains: dict[str, list[str]],
) -> dict[str, Any]:
    risks = {"external_io": [], "history_scans": [], "large_runtime_tables": []}
    for key, chain in decision_chains.items():
        profile = profiles.get(key) or {}
        # national_bot.py is a system-provided socket/runtime owner. Its pipe,
        # process-tree cleanup and wire I/O are required infrastructure, not
        # candidate strategy-path I/O. Candidate modules remain fully checked.
        if profile.get("filename") == "national_bot.py":
            continue
        for risk_name in risks:
            risks[risk_name].extend(
                _format_path(chain, item) for item in profile.get(risk_name) or []
            )
    # Import-time and dormant helper I/O are also forbidden in candidate-owned
    # modules.  Otherwise a module can load an untracked /tmp/results policy at
    # import and expose only a constant to the clean decision call graph.
    for key, profile in profiles.items():
        if profile.get("filename") == "national_bot.py":
            continue
        risks["external_io"].extend(profile.get("external_io") or [])
    for filename, tree in trees.items():
        if filename == "national_bot.py":
            continue
        visitor = _FunctionVisitor(filename)
        for statement in tree.body:
            visitor.visit(statement)
        risks["external_io"].extend(visitor.external_io)
    return {
        "decision_functions": sorted(
            key for key, profile in profiles.items() if profile.get("is_decision")
        ),
        **{name: sorted(dict.fromkeys(items)) for name, items in risks.items()},
    }


def _decision_time_evidence(
    profiles: dict[str, dict[str, Any]],
    decision_chains: dict[str, list[str]],
) -> dict[str, Any]:
    fields = (
        "deadline_assignments",
        "deadline_checks",
        "finite_work_bounds",
        "baseline_locations",
        "fallback_locations",
    )
    evidence = {field: [] for field in fields}
    baseline_before_deadline = False
    for key, chain in decision_chains.items():
        profile = profiles.get(key) or {}
        for field in fields:
            evidence[field].extend(
                _format_path(chain, item) for item in profile.get(field) or []
            )
        baseline_before_deadline = baseline_before_deadline or bool(
            profile.get("baseline_before_deadline")
        )
    evidence = {
        field: sorted(dict.fromkeys(values))
        for field, values in evidence.items()
    }
    passed = bool(
        evidence["deadline_assignments"]
        and evidence["deadline_checks"]
        and evidence["finite_work_bounds"]
        and evidence["baseline_locations"]
        and evidence["fallback_locations"]
        and baseline_before_deadline
    )
    return {
        "passed": passed,
        "baseline_before_deadline": baseline_before_deadline,
        **evidence,
    }


def _assignment_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [target.id for target in targets if isinstance(target, ast.Name)]


def _bounded_lru_cache(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int | None:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        if _call_name(decorator.func).rsplit(".", 1)[-1] != "lru_cache":
            continue
        maxsize: int | None = None
        if decorator.args:
            maxsize = _constant_int(decorator.args[0])
        for keyword in decorator.keywords:
            if keyword.arg == "maxsize":
                maxsize = _constant_int(keyword.value)
        if maxsize is not None and 1 <= maxsize <= MAX_PRECOMPUTE_ENTRIES:
            return maxsize
    return None


def _precompute_evidence(
    trees: dict[str, ast.Module],
    profiles: dict[str, dict[str, Any]],
    decision_chains: dict[str, list[str]],
) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    imports_by_file: dict[str, dict[str, str]] = {}
    for filename, tree in trees.items():
        imported: dict[str, str] = {}
        for item in tree.body:
            if isinstance(item, ast.ImportFrom) and item.module:
                owner_module = item.module.rsplit(".", 1)[-1]
                for alias in item.names:
                    imported[alias.asname or alias.name] = owner_module
        imports_by_file[filename] = imported

    def consumer_locations(owner_file: str, name: str) -> list[str]:
        owner_module = Path(owner_file).stem
        locations: list[str] = []
        for key, chain in decision_chains.items():
            profile = profiles.get(key) or {}
            if name not in set(profile.get("sink_dependencies") or []):
                continue
            consumer_file = str(profile.get("filename") or "")
            bound_to_owner = (
                consumer_file == owner_file
                or imports_by_file.get(consumer_file, {}).get(name) == owner_module
            )
            if bound_to_owner:
                locations.append(_format_path(chain, _line_label(consumer_file, None, name)))
        return sorted(dict.fromkeys(locations))

    module_startup_calls: dict[str, set[str]] = {}
    for filename, tree in trees.items():
        calls: set[str] = set()
        for item in tree.body:
            call: ast.Call | None = None
            if isinstance(item, ast.Expr) and isinstance(item.value, ast.Call):
                call = item.value
            elif isinstance(item, (ast.Assign, ast.AnnAssign)) and isinstance(item.value, ast.Call):
                call = item.value
            if call is not None:
                name = _call_name(call.func).rsplit(".", 1)[-1]
                if name:
                    calls.add(name)
            elif isinstance(item, ast.For):
                loop_size = _range_size(item.iter)
                if loop_size is not None and 1 <= loop_size <= MAX_PRECOMPUTE_ENTRIES:
                    for statement in item.body:
                        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                            name = _call_name(statement.value.func).rsplit(".", 1)[-1]
                            if name:
                                calls.add(name)
        module_startup_calls[filename] = calls
    for filename, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                size = _static_collection_size(value)
                for name in _assignment_names(node):
                    named_artifact = any(marker in name.lower() for marker in _ARTIFACT_NAME_MARKERS)
                    if (
                        named_artifact
                        and size is not None
                        and MIN_PRECOMPUTE_ENTRIES <= size <= MAX_PRECOMPUTE_ENTRIES
                    ):
                        consumers = consumer_locations(filename, name)
                        artifacts.append({
                            "name": name,
                            "kind": "module_lookup",
                            "build_phase": "module_import",
                            "bound_entries": size,
                            "location": _line_label(filename, node, name),
                            "consumer_locations": consumers,
                            "consumed_by_decision": bool(consumers),
                            "built_before_first_decision": True,
                        })
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                maxsize = _bounded_lru_cache(node)
                if maxsize is not None:
                    warmed = node.name in module_startup_calls.get(filename, set())
                    consumers = consumer_locations(filename, node.name)
                    artifacts.append({
                        "name": node.name,
                        "kind": "bounded_lru_cache",
                        "build_phase": "module_import",
                        "bound_entries": maxsize,
                        "location": _line_label(filename, node, node.name),
                        "consumer_locations": consumers,
                        "consumed_by_decision": bool(consumers),
                        "built_before_first_decision": warmed,
                    })
    consumed = [
        artifact
        for artifact in artifacts
        if artifact["consumed_by_decision"] and artifact["built_before_first_decision"]
    ]
    return {
        "passed": bool(consumed),
        "artifacts": artifacts,
        "consumed_artifacts": consumed,
        "total_bound_entries": sum(item["bound_entries"] for item in artifacts),
    }


def _self_mutated_attributes(
    node: ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> set[str]:
    if node is None:
        return set()
    mutated: set[str] = set()
    for item in _function_body_nodes(node):
        if isinstance(item, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            for target in targets:
                for child in ast.walk(target):
                    if (
                        isinstance(child, ast.Attribute)
                        and isinstance(child.value, ast.Name)
                        and child.value.id == "self"
                    ):
                        mutated.add(child.attr)
        if isinstance(item, ast.Call):
            name = _call_name(item.func)
            if name.startswith("self.") and name.rsplit(".", 1)[-1] in {
                "add", "append", "clear", "extend", "pop", "remove", "update"
            }:
                parts = name.split(".")
                if len(parts) >= 3:
                    mutated.add(parts[1])
    return mutated


def _self_loaded_attributes(
    node: ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> set[str]:
    if node is None:
        return set()
    return {
        item.attr
        for item in ast.walk(node)
        if (
            isinstance(item, ast.Attribute)
            and isinstance(item.value, ast.Name)
            and item.value.id == "self"
            and isinstance(item.ctx, ast.Load)
        )
    }


def _opponent_tracker_provider(
    trees: dict[str, ast.Module],
    native: str,
) -> dict[str, bool]:
    tracker = None
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in (trees.get("national_bot.py") or ast.Module(body=[], type_ignores=[])).body:
        if isinstance(node, ast.ClassDef) and node.name == "OpponentTracker":
            tracker = node
            methods = {
                item.name: item
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            break
    snapshot = methods.get("snapshot")
    snapshot_strings = _string_values(snapshot) if snapshot is not None else set()
    snapshot_names = _loaded_identifiers(snapshot) if snapshot is not None else set()
    snapshot_attributes = _self_loaded_attributes(snapshot)

    def mutation_reaches_snapshot(method_name: str) -> bool:
        return bool(
            _self_mutated_attributes(methods.get(method_name))
            & snapshot_attributes
        )
    bounded_recent_state = False
    recent_state_maxlen: int | None = None
    if tracker is not None:
        for item in ast.walk(tracker):
            if not isinstance(item, ast.Call) or _call_name(item.func).rsplit(".", 1)[-1] != "deque":
                continue
            maxlen = None
            for keyword in item.keywords:
                if keyword.arg == "maxlen":
                    maxlen = _constant_int(keyword.value)
            if maxlen is not None and 1 <= maxlen <= 70:
                bounded_recent_state = True
                recent_state_maxlen = maxlen
                break
    adaptation_match = re.search(
        r"(?m)^\s*OPPONENT_ADAPTATION_CAP\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$",
        native,
    )
    adaptation_cap = float(adaptation_match.group(1)) if adaptation_match else None
    return {
        "tracker_defined": tracker is not None,
        "tracker_instantiated": bool(re.search(r"self\._opponent_tracker\s*=\s*OpponentTracker\s*\(", native)),
        "hand_lifecycle": mutation_reaches_snapshot("begin_hand") and ".begin_hand(" in native,
        "street_lifecycle": methods.get("begin_street") is not None and ".begin_street(" in native,
        "action_updates": mutation_reaches_snapshot("observe_action") and ".observe_action(" in native,
        "settlement_updates": mutation_reaches_snapshot("observe_settlement") and ".observe_settlement(" in native,
        "showdown_updates": mutation_reaches_snapshot("observe_showdown") and ".observe_showdown(" in native,
        "showdown_range_posterior": (
            mutation_reaches_snapshot("observe_showdown")
            and methods.get("_showdown_class") is not None
            and {
                "showdown_range",
                "selection_scope",
                "selection_bias_guard",
                "showdown_reach_rate",
                "adaptation_weight",
                "prior_source",
                "bucket_rates",
            }.issubset(snapshot_strings)
        ),
        "terminal_response_updates": (
            mutation_reaches_snapshot("observe_action")
            and {
                "fold_to_jam_rate",
                "fold_to_jam_samples",
                "river_overcall_freq",
                "river_overcall_samples",
                "facing_raise_by_street",
                "facing_allin_by_street",
                "terminal_response",
            }.issubset(snapshot_strings)
        ),
        "suppressed_terminal_repair": (
            "def _infer_suppressed_terminal_opponent_action" in native
            and native.count("._infer_suppressed_terminal_opponent_action(") >= 3
        ),
        "snapshot_injected": bool(
            re.search(
                r"['\"]opponent_runtime['\"]\s*:\s*self\._opponent_tracker\.snapshot\s*\(",
                native,
            )
        ),
        "hand_context_injected": bool(
            re.search(
                r"['\"]hand_runtime['\"]\s*:\s*self\._hand_runtime\s*\(",
                native,
            )
            and "def _hand_runtime" in native
            and all(
                f'"{field}"' in native or f"'{field}'" in native
                for field in ("can_donk", "can_delayed_probe", "preflop_aggressor")
            )
        ),
        "bounded_recent_state": bounded_recent_state,
        "recent_state_maxlen": recent_state_maxlen,
        "confidence_scaled": (
            {"confidence", "adaptation_weight"}.issubset(snapshot_strings)
            and "OPPONENT_ADAPTATION_CAP" in snapshot_names
        ),
        "compatibility_schema": _OPPONENT_RUNTIME_CORE_FIELDS.issubset(snapshot_strings),
        "adaptation_cap": adaptation_cap,
    }


def _incremental_model_evidence(
    sources: dict[str, str],
    trees: dict[str, ast.Module],
    profiles: dict[str, dict[str, Any]],
    decision_chains: dict[str, list[str]],
) -> dict[str, Any]:
    native = sources.get("national_bot.py", "")
    provider = _opponent_tracker_provider(trees, native)
    consumer_locations: list[str] = []
    tracked_fields = (
        "fold_to_raise",
        "fold_to_jam_rate",
        "river_overcall_freq",
        "showdown_range",
        "tightness",
        "bucket_rates",
        "confidence",
        "adaptation_weight",
        "selection_scope",
        "showdown_reach_rate",
        "terminal_response",
        "contexts",
        "hand_runtime",
        # Legacy literal-level hints retained for older persisted capability
        # reports.  New reference-card gates use source_rooted_live_access_paths
        # below, never these strings, because a dead literal is not consumption.
        "street",
        "spr",
        "pot",
        "effective_stack",
        "hero_position",
        "preflop_aggressor",
        "street_open",
        "to_call",
        "pot_odds",
        "can_donk",
        "can_delayed_probe",
    )
    field_locations: dict[str, list[str]] = {field: [] for field in tracked_fields}
    # Keep an evidence key that identifies the reachable decision function or
    # helper chain but intentionally excludes the individual literal/line.
    # A nested access such as terminal_response["confidence"] produces two
    # different literal locations, yet they do belong to the same live
    # decision path.  Policy gates use this normalized representation when a
    # reference card requires a multi-part opponent-runtime path.
    field_function_locations: dict[str, list[str]] = {
        field: [] for field in tracked_fields
    }
    source_rooted_live_access_locations: dict[str, list[str]] = {}
    for key in decision_chains:
        profile = profiles.get(key) or {}
        if profile.get("filename") == "national_bot.py":
            continue
        decision_location = "->".join(decision_chains.get(key) or [key])
        for location in profile.get("weighted_runtime_locations") or []:
            consumer_locations.append(_format_path(decision_chains.get(key) or [key], location))
        literals = {str(item).lower() for item in profile.get("string_literals") or []}
        for field in tracked_fields:
            if field in literals:
                field_locations[field].append(
                    _format_path(
                        decision_chains.get(key) or [key],
                        _line_label(str(profile.get("filename") or ""), None, field),
                    )
                )
                field_function_locations[field].append(decision_location)
        for access_path in profile.get("source_rooted_live_access_paths") or []:
            path = str(access_path)
            if not path:
                continue
            source_rooted_live_access_locations.setdefault(path, []).append(
                _format_path(
                    decision_chains.get(key) or [key],
                    _line_label(
                        str(profile.get("filename") or ""),
                        None,
                        f"source_rooted_live_access[{path}]",
                    ),
                )
            )
    required_provider = (
        "tracker_defined",
        "tracker_instantiated",
        "hand_lifecycle",
        "action_updates",
        "settlement_updates",
        "showdown_updates",
        "showdown_range_posterior",
        "terminal_response_updates",
        "suppressed_terminal_repair",
        "snapshot_injected",
        "hand_context_injected",
        "bounded_recent_state",
        "confidence_scaled",
        "compatibility_schema",
    )
    provider_complete = all(provider[name] for name in required_provider)
    return {
        "provider": provider,
        "provider_complete": provider_complete,
        "consumer_locations": sorted(consumer_locations),
        "decision_field_locations": {
            field: sorted(dict.fromkeys(locations))
            for field, locations in field_locations.items()
        },
        "decision_field_function_locations": {
            field: sorted(dict.fromkeys(locations))
            for field, locations in field_function_locations.items()
        },
        # Exact, source-rooted request paths which reach a return/yield action
        # sink in a reachable strategy helper.  Presence of this key (including
        # an empty mapping) marks the v4.3+ detector evidence: consumers must
        # not silently fall back to the older literal-name heuristic.
        "source_rooted_live_access_paths": {
            path: sorted(dict.fromkeys(locations))
            for path, locations in sorted(source_rooted_live_access_locations.items())
        },
        "consumed_by_decision": bool(consumer_locations),
        "incremental_complete": provider_complete and bool(consumer_locations),
    }


def _check(
    check_id: str,
    passed: bool,
    severity: str,
    skill_layer: str,
    summary: str,
    guidance: str,
    *,
    locations: list[str] | None = None,
    facts: dict[str, Any] | None = None,
    confidence: float = 0.95,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "name": check_id,
        "passed": bool(passed),
        "severity": severity,
        "skill_layer": skill_layer,
        "confidence": round(max(0.0, min(float(confidence), 1.0)), 3),
        "evidence": {
            "summary": summary,
            "locations": list(locations or []),
            "facts": dict(facts or {}),
        },
        "guidance": guidance,
    }


def evaluate_national_capabilities(bot_dir: str | Path) -> dict[str, Any]:
    """Return stable, evidence-backed architecture capabilities for one bot."""

    try:
        sources = _read_python_sources(bot_dir)
    except OSError as exc:
        issue = f"capability_source_read_error:{type(exc).__name__}:{str(exc)[:300]}"
        return {
            "schema_version": CAPABILITY_SCHEMA_VERSION,
            "detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
            "bot_dir": str(Path(bot_dir)),
            "ok": False,
            "conclusive": False,
            "outcome": "infrastructure_failure",
            "infrastructure_failures": [{
                "component": "capability_source_reader",
                "failure_class": "internal_infrastructure",
                "issues": [issue],
            }],
            "required_failures": [],
            "advisory_warnings": [],
            "checks": [],
            "checks_by_id": {},
            "decision_path_risks": {},
            "decision_time_evidence": {},
            "decision_runtime_evidence": {},
            "precompute_evidence": {},
            "incremental_model_evidence": {},
            "dynamic_runtime_probe": {
                "ok": False,
                "failure_class": "not_run",
                "issues": ["capability_source_reader_failed"],
            },
        }
    trees = _parse_sources(sources)
    native = sources.get("national_bot.py", "")
    profiles, by_name = _function_profiles(trees)
    decision_chains, _roots = _decision_graph(profiles, by_name)
    decision_risks = _decision_path_risks(trees, profiles, decision_chains)
    decision_time = _decision_time_evidence(profiles, decision_chains)
    decision_constant_names = {
        "default_hard_deadline_ms": "DEFAULT_DECISION_HARD_DEADLINE_SEC",
        "default_baseline_target_ms": "DEFAULT_DECISION_BASELINE_TARGET_SEC",
        "default_refinement_budget_ms": "DEFAULT_DECISION_REFINEMENT_BUDGET_SEC",
    }
    for evidence_key, constant_name in decision_constant_names.items():
        match = re.search(
            rf"(?m)^\s*{constant_name}\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*$",
            native,
        )
        decision_time[evidence_key] = (
            int(float(match.group(1)) * 1000)
            if match
            else None
        )
    precompute = _precompute_evidence(trees, profiles, decision_chains)
    incremental = _incremental_model_evidence(sources, trees, profiles, decision_chains)
    strategy_function_names = {
        profile.get("name")
        for profile in profiles.values()
        if profile.get("filename") == "strategy.py"
    }
    expected_decision_runtime_version: int | None = None
    try:
        from national_runtime_probe import (
            NATIONAL_DECISION_RUNTIME_VERSION as expected_decision_runtime_version,
            run_national_runtime_probe,
        )

        dynamic_probe = run_national_runtime_probe(
            bot_dir,
            static_artifacts=precompute.get("consumed_artifacts") or [],
        )
    except Exception as exc:
        dynamic_probe = {
            "schema_version": 1,
            "ok": False,
            "failure_class": "internal_infrastructure",
            "issues": [f"runtime_probe_error:{type(exc).__name__}:{str(exc)[:180]}"],
            "artifacts": [],
            "tracker": {"ok": False, "issues": ["runtime_probe_error"]},
            "strategy_influence": {"ok": False, "issues": ["runtime_probe_error"]},
        }
    probe_failure_class = str(dynamic_probe.get("failure_class") or "")
    probe_infrastructure_failure = probe_failure_class in {
        "probe_infra",
        "internal_infrastructure",
    }
    infrastructure_failures = (
        [{
            "component": "national_runtime_probe",
            "failure_class": probe_failure_class,
            "issues": [str(item) for item in (dynamic_probe.get("issues") or [])[:8]],
        }]
        if probe_infrastructure_failure
        else []
    )
    dynamic_artifacts = [
        item for item in dynamic_probe.get("artifacts") or []
        if isinstance(item, dict) and item.get("ok")
    ]
    dynamic_action_influence_artifacts = [
        item for item in dynamic_artifacts
        if item.get("value_affects_final_wire") is True
    ]
    precompute["dynamic_probe_artifacts"] = dynamic_probe.get("artifacts") or []
    precompute["dynamic_probe_passed"] = (
        None if probe_infrastructure_failure else bool(dynamic_artifacts)
    )
    if not probe_infrastructure_failure:
        precompute["passed"] = bool(precompute.get("passed") and dynamic_artifacts)
    precompute["dynamic_action_influence_artifacts"] = dynamic_action_influence_artifacts
    precompute["dynamic_action_influence_passed"] = (
        None
        if probe_infrastructure_failure
        else bool(precompute.get("passed") and dynamic_action_influence_artifacts)
    )
    incremental["dynamic_tracker"] = dynamic_probe.get("tracker") or {}
    incremental["dynamic_strategy_influence"] = dynamic_probe.get("strategy_influence") or {}
    hand_context = dynamic_probe.get("hand_context") or {}
    influence_dimensions = incremental["dynamic_strategy_influence"].get("dimensions") or {}
    decision_field_locations = incremental.get("decision_field_locations") or {}
    terminal_flat_fields_consumed = all(
        decision_field_locations.get(field)
        for field in ("fold_to_raise", "fold_to_jam_rate", "river_overcall_freq")
    )
    terminal_context_fields_consumed = all(
        decision_field_locations.get(field)
        for field in ("terminal_response", "contexts", "confidence", "adaptation_weight")
    )
    terminal_fields_consumed = bool(
        terminal_flat_fields_consumed or terminal_context_fields_consumed
    )
    showdown_fields_consumed = bool(
        decision_field_locations.get("showdown_range")
        and (
            decision_field_locations.get("tightness")
            or decision_field_locations.get("bucket_rates")
        )
        and decision_field_locations.get("confidence")
        and decision_field_locations.get("adaptation_weight")
        and decision_field_locations.get("selection_scope")
    )
    line_fields_consumed = all(
        decision_field_locations.get(field)
        for field in ("hand_runtime", "can_donk", "can_delayed_probe")
    )
    decision_runtime = dynamic_probe.get("decision_runtime") or {}
    baseline_samples_ms = [
        float(value)
        for value in decision_runtime.get("baseline_samples_ms") or []
        if isinstance(value, (int, float))
    ]
    dynamic_baseline_ok = decision_runtime.get("baseline_ok")
    if not isinstance(dynamic_baseline_ok, bool):
        dynamic_baseline_ok = bool(
            baseline_samples_ms and max(baseline_samples_ms) <= 250.0
        )
    refinement_observed = any(
        int(((row.get(label) or {}).get("runtime_metrics") or {}).get("refinement_messages") or 0) > 0
        for row in (dynamic_probe.get("strategy_influence") or {}).get("rows") or []
        if isinstance(row, dict)
        for label in ("baseline", "aggressive", "passive")
    ) or any(
        int((decision_runtime.get("budget_scaling") or {}).get(tier, {}).get("trusted_steps") or 0) > 0
        for tier in ("short", "long")
    )
    if not probe_infrastructure_failure:
        incremental["provider_complete"] = bool(
            incremental.get("provider_complete")
            and incremental["dynamic_tracker"].get("ok")
        )
        incremental["incremental_complete"] = bool(
            incremental.get("provider_complete")
            and incremental.get("consumed_by_decision")
            and (influence_dimensions.get("action_profile") or {}).get("ok")
        )

    checks = [
        _check(
            "official_safe_wire_send",
            "_send_wire_action" in native and "POK_OFFICIAL_ACTION_DELAY" in native,
            "required",
            "native_tcp",
            "official send helper and action throttle are both present",
            "Send EXE actions only through _send_wire_action and preserve the official delay.",
            facts={"send_helper": "_send_wire_action" in native, "throttle": "POK_OFFICIAL_ACTION_DELAY" in native},
        ),
        _check(
            "clean_diagnostics_channel",
            ("--log" in native or "POK_TRACE_DECISIONS" in native)
            and not _regex(native, r"(?m)^\s*print\s*\("),
            "required",
            "telemetry",
            "diagnostics have a non-stdout channel and no top-level print call was found",
            "Write diagnostics to --log or stderr; stdout is reserved for no national protocol payload.",
            confidence=0.9,
        ),
        _check(
            "decision_time_budget_visible",
            decision_time["passed"],
            "advisory",
            "runtime_architecture",
            "decision call graph has a monotonic deadline, finite work bound, baseline-first path, and fallback return",
            "Use an explicit monotonic deadline and finite cap, compute a legal baseline before refinement, and return it on timeout/error.",
            locations=(
                decision_time["deadline_checks"][:3]
                + decision_time["fallback_locations"][:3]
            ),
            facts={
                "deadline_assignment": bool(decision_time["deadline_assignments"]),
                "deadline_check": bool(decision_time["deadline_checks"]),
                "finite_work_bound": bool(decision_time["finite_work_bounds"]),
                "baseline": bool(decision_time["baseline_locations"]),
                "fallback": bool(decision_time["fallback_locations"]),
                "baseline_before_deadline": decision_time["baseline_before_deadline"],
            },
            confidence=0.9,
        ),
        _check(
            "killable_decision_runtime",
            bool(decision_runtime.get("safety_ok", decision_runtime.get("ok"))),
            "advisory",
            "runtime_architecture",
            "a hanging strategy is terminated and the next decision restarts a fresh worker",
            "Run strategy and sanitizer inside the persistent process worker; terminate it at the deadline and restart on the next decision.",
            facts={
                "runtime_version": expected_decision_runtime_version,
                "safety_ok": decision_runtime.get("safety_ok"),
                "safety_issues": decision_runtime.get("safety_issues") or [],
                "fallback_ready_samples_ms": (
                    decision_runtime.get("fallback_ready_samples_ms") or []
                ),
                "timeout_recovery": decision_runtime.get("timeout_recovery") or {},
            },
        ),
        _check(
            "fast_strategy_baseline",
            (
                "get_baseline_action" in strategy_function_names
                and dynamic_baseline_ok
            ),
            "advisory",
            "runtime_architecture",
            "strategy publishes an explicit sanitized baseline within 250ms across the scenario bank",
            "Implement get_baseline_action(req, current_hand_view) as a bounded lookup path; the socket fallback must already exist at t=0.",
            facts={
                "function_present": "get_baseline_action" in strategy_function_names,
                "baseline_ok": decision_runtime.get("baseline_ok"),
                "baseline_issues": decision_runtime.get("baseline_issues") or [],
                "sample_count": len(baseline_samples_ms),
                "max_ms": max(baseline_samples_ms) if baseline_samples_ms else None,
            },
        ),
        _check(
            "incremental_refinement_protocol",
            "iter_refinements" in strategy_function_names and refinement_observed,
            "advisory",
            "runtime_architecture",
            "strategy yields sanitized, decision-id-scoped candidates before the monotonic deadline",
            "Implement iter_refinements(req, current_hand_view, baseline, deadline); reported sample_count is diagnostic, while the system counts yielded batches.",
            facts={
                "function_present": "iter_refinements" in strategy_function_names,
                "refinement_observed": refinement_observed,
            },
        ),
        _check(
            "budget_scaled_refinement",
            bool(decision_runtime.get("refinement_ok")),
            "advisory",
            "runtime_architecture",
            "refinement has trusted iterator/elapsed work, changes a sanitized baseline, and scales or proves finite exhaustion under a longer budget",
            "Yield real candidate batches; candidate sample_count/complete metadata is non-authoritative, and empty or baseline-only yields are not refinement.",
            facts={
                "refinement_issues": decision_runtime.get("refinement_issues") or [],
                "budget_scaling": decision_runtime.get("budget_scaling") or {},
            },
        ),
        _check(
            "decision_path_no_external_io",
            not decision_risks["external_io"],
            "required",
            "runtime_architecture",
            "candidate-owned modules contain no file, network, or subprocess I/O",
            "Build pure tables in memory; do not load policy from files, network, subprocesses, import-time helpers, or dormant code.",
            locations=decision_risks["external_io"][:8],
        ),
        _check(
            "decision_path_no_full_history_scan",
            not decision_risks["history_scans"],
            "advisory",
            "match_memory",
            "decision call graph does not iterate full-match requests/responses/showdowns",
            "Consume opponent_runtime aggregates; current-hand history may remain bounded and local.",
            locations=decision_risks["history_scans"][:8],
        ),
        _check(
            "decision_path_no_large_runtime_tables",
            not decision_risks["large_runtime_tables"],
            "advisory",
            "precompute",
            "decision call graph avoids large collection construction and combinatorial enumeration",
            "Build pure poker facts at import/startup or use a bounded cache consumed by the decision path.",
            locations=decision_risks["large_runtime_tables"][:8],
            confidence=0.9,
        ),
        _check(
            "precompute_lookup_path",
            precompute["passed"],
            "advisory",
            "precompute",
            "a bounded module/startup artifact is proven to be consumed by the decision call graph",
            "Add a bounded immutable lookup or lru_cache and consume it in get_action or a reachable helper.",
            locations=[item["location"] for item in precompute["consumed_artifacts"][:8]],
            facts={
                "artifact_count": len(precompute["artifacts"]),
                "consumed_count": len(precompute["consumed_artifacts"]),
                "dynamic_consumed_count": len(dynamic_artifacts),
                "total_bound_entries": precompute["total_bound_entries"],
            },
            confidence=0.9,
        ),
        _check(
            "precompute_runtime_influence",
            precompute["dynamic_action_influence_passed"],
            "advisory",
            "precompute",
            "a bounded lookup's values change a final sanitized wire action under a trusted same-shape counterfactual",
            "For a selected precompute innovation, consume live hand/opponent inputs and make an inspectable table value alter a legal final action; retain a legal empty-table fallback.",
            locations=[
                str(item.get("location") or "")
                for item in precompute["dynamic_action_influence_artifacts"][:8]
            ],
            facts={
                "dynamic_action_influence_count": len(dynamic_action_influence_artifacts),
                "artifacts": [
                    {
                        "owner_file": item.get("owner_file"),
                        "name": item.get("name"),
                        "scenarios": item.get("action_influence_scenarios") or [],
                    }
                    for item in dynamic_action_influence_artifacts[:8]
                ],
            },
            confidence=0.95,
        ),
        _check(
            "persistent_match_memory",
            incremental["provider_complete"],
            "advisory",
            "match_memory",
            "connection-level tracker has bounded lifecycle, action, settlement, showdown, and confidence state",
            "Keep OpponentTracker alive for the connection and update it from every protocol event.",
            facts=incremental["provider"],
        ),
        _check(
            "terminal_response_memory",
            bool(
                incremental["provider"].get("suppressed_terminal_repair")
                and incremental["provider"].get("terminal_response_updates")
                and incremental["dynamic_tracker"].get("ok")
            ),
            "advisory",
            "match_memory",
            "relayed folds and relayed/inferred terminal calls update street-specific response posteriors across hands",
            "Repair only boundary-proven omitted call/check tokens before clearing street bets, then persist fold-to-raise, fold-to-jam, and river-overcall samples in OpponentTracker.",
            facts={
                "suppressed_terminal_repair": incremental["provider"].get("suppressed_terminal_repair"),
                "terminal_response_updates": incremental["provider"].get("terminal_response_updates"),
                "tracker_issues": incremental["dynamic_tracker"].get("issues") or [],
            },
        ),
        _check(
            "showdown_range_posterior",
            bool(
                incremental["provider"].get("showdown_range_posterior")
                and incremental["dynamic_tracker"].get("ok")
            ),
            "advisory",
            "opponent_model",
            "revealed cards update a bounded prior-smoothed, line-conditioned reached-showdown range posterior",
            "Convert oppo_hands into bounded range buckets/classes with priors, confidence, and explicit reached-showdown selection scope.",
            facts={
                "provider": incremental["provider"].get("showdown_range_posterior"),
                "tracker_issues": incremental["dynamic_tracker"].get("issues") or [],
            },
        ),
        _check(
            "authoritative_hand_context",
            bool(
                incremental["provider"].get("hand_context_injected")
                and hand_context.get("ok")
            ),
            "advisory",
            "line_template",
            "wrapper-owned hand_runtime preserves preflop aggressor and official check/call street semantics for live line flags",
            "Consume req['hand_runtime'] for cross-street spots; do not rediscover donk or delayed-probe eligibility from current-street history.",
            facts={
                "provider": incremental["provider"].get("hand_context_injected"),
                "dynamic_issues": hand_context.get("issues") or [],
            },
        ),
        _check(
            "incremental_opponent_model",
            incremental["incremental_complete"],
            "advisory",
            "opponent_model",
            "opponent_runtime is produced incrementally, injected into requests, and read by the decision graph",
            "Read req['opponent_runtime'] and scale any exploit delta by its confidence/adaptation_weight.",
            locations=incremental["consumer_locations"][:8],
            facts={
                "provider_complete": incremental["provider_complete"],
                "consumed_by_decision": incremental["consumed_by_decision"],
                "dynamic_strategy_influence": bool(
                    (influence_dimensions.get("action_profile") or {}).get("ok")
                ),
            },
        ),
        _check(
            "terminal_response_adaptation",
            bool(
                terminal_fields_consumed
                and (influence_dimensions.get("terminal_response") or {}).get("ok")
            ),
            "advisory",
            "opponent_model",
            "terminal fold/call response profiles produce an observable legal action difference",
            "Consume fold-to-raise, fold-to-jam, and river-overcall evidence with confidence in a reachable decision path.",
            facts={
                "ok": (influence_dimensions.get("terminal_response") or {}).get("ok"),
                "changed_pairs": (influence_dimensions.get("terminal_response") or {}).get("changed_pairs", 0),
                "required_fields_consumed": terminal_fields_consumed,
            },
        ),
        _check(
            "showdown_range_adaptation",
            bool(
                showdown_fields_consumed
                and (influence_dimensions.get("showdown_range") or {}).get("ok")
            ),
            "advisory",
            "opponent_model",
            "showdown-only tight and loose counterfactuals produce an observable legal action difference",
            "Feed opponent_runtime.showdown_range posterior weights into range/EV logic; reading a counter without changing a decision is insufficient.",
            facts={
                "ok": (influence_dimensions.get("showdown_range") or {}).get("ok"),
                "changed_pairs": (influence_dimensions.get("showdown_range") or {}).get("changed_pairs", 0),
                "required_fields_consumed": showdown_fields_consumed,
            },
        ),
        _check(
            "semantic_line_reachability",
            bool(
                line_fields_consumed
                and (influence_dimensions.get("semantic_lines") or {}).get("ok")
            ),
            "advisory",
            "line_template",
            "donk and delayed-probe positive/control pairs both reach distinct sanitized actions",
            "Wire hand_runtime.can_donk and can_delayed_probe into live strategy using the official BB-check/SB-pass-call transcript.",
            facts={
                "ok": (influence_dimensions.get("semantic_lines") or {}).get("ok"),
                "changed_pairs": (influence_dimensions.get("semantic_lines") or {}).get("changed_pairs", 0),
                "required_fields_consumed": line_fields_consumed,
            },
        ),
    ]

    if probe_infrastructure_failure:
        for item in checks:
            if item["check_id"] in {
                "killable_decision_runtime",
                "fast_strategy_baseline",
                "incremental_refinement_protocol",
                "budget_scaled_refinement",
                "precompute_lookup_path",
                "precompute_runtime_influence",
                "persistent_match_memory",
                "terminal_response_memory",
                "showdown_range_posterior",
                "authoritative_hand_context",
                "incremental_opponent_model",
                "terminal_response_adaptation",
                "showdown_range_adaptation",
                "semantic_line_reachability",
            }:
                item["passed"] = None
                item["evidence"]["status"] = "inconclusive_infrastructure"
    required_failures = [
        item for item in checks
        if item["severity"] == "required" and item["passed"] is False
    ]
    advisory_warnings = [
        item for item in checks
        if item["severity"] == "advisory" and item["passed"] is False
    ]
    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
        "bot_dir": str(Path(bot_dir)),
        "ok": not required_failures and not infrastructure_failures,
        "conclusive": not infrastructure_failures,
        "outcome": (
            "infrastructure_failure"
            if infrastructure_failures
            else "candidate_failure"
            if required_failures
            else "passed"
        ),
        "infrastructure_failures": infrastructure_failures,
        "required_failures": required_failures,
        "advisory_warnings": advisory_warnings,
        "checks": checks,
        "checks_by_id": {item["check_id"]: item for item in checks},
        "decision_path_risks": decision_risks,
        "decision_time_evidence": decision_time,
        "decision_runtime_evidence": decision_runtime,
        "precompute_evidence": precompute,
        "incremental_model_evidence": incremental,
        "dynamic_runtime_probe": dynamic_probe,
    }


def _evidence_summary(item: dict[str, Any]) -> str:
    evidence = item.get("evidence") or {}
    summary = str(evidence.get("summary") or "no evidence summary")
    locations = evidence.get("locations") or []
    if locations:
        summary += f"; example={locations[0]}"
    return summary


def _prioritized_risk_examples(items: list[str], limit: int = 3) -> list[str]:
    return sorted(dict.fromkeys(items), key=lambda item: (item.count("->"), item))[:limit]


def national_runtime_feedback_summary(
    bot_dir: str | Path,
    *,
    source_label: str = "source bot",
    max_chars: int = 4000,
) -> str:
    """Return bounded, actionable detector evidence for Master planning."""

    result = evaluate_national_capabilities(bot_dir)
    required = result.get("required_failures") or []
    advisory = result.get("advisory_warnings") or []
    checks = result.get("checks") or []
    risks = result.get("decision_path_risks") or {}
    lines = [
        f"National runtime architecture feedback for {source_label}:",
        f"Detector {result.get('detector_version')} uses AST evidence and call-path consumption; this is not a strength score.",
        "This is a planning signal only; official EXE compliance and native TCP gates remain authoritative for legality.",
    ]
    if required:
        lines.append("Required runtime contract failures:")
        for item in required[:4]:
            lines.append(
                f"- {item.get('check_id')}: {item.get('guidance')} "
                f"(evidence: {_evidence_summary(item)})"
            )
    if advisory:
        lines.append("Architecture improvement opportunities:")
        lines.append(
            "- gap_ids: "
            + ", ".join(str(item.get("check_id")) for item in advisory)
        )
        for item in advisory[:6]:
            lines.append(
                f"- {item.get('check_id')}: {item.get('guidance')} "
                f"(evidence: {_evidence_summary(item)})"
            )
        risk_examples: list[str] = []
        for key, label in (
            ("external_io", "decision_path_external_io"),
            ("history_scans", "decision_path_history_scan"),
            ("large_runtime_tables", "decision_path_runtime_table"),
        ):
            for risk in _prioritized_risk_examples(risks.get(key) or [], limit=3):
                risk_examples.append(f"- {label}: {risk}")
        if risk_examples:
            lines.append("Decision path evidence to route into worker tasks:")
            lines.extend(risk_examples[:6])
    else:
        lines.append("No advisory runtime-architecture gaps detected by the evidence contract.")
    passed = [item for item in checks if item.get("passed")]
    if passed:
        lines.append(
            "Already present: "
            + ", ".join(str(item.get("check_id")) for item in passed[:8])
        )
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text
