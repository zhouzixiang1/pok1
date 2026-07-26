"""Bare-action AST static-checks engine for national_capability_contract.

Extracted as a cohesive business cluster; national_capability_contract.py
retains thin delegate shells.
"""
from __future__ import annotations

import ast

import national_capability_contract as _ncc  # noqa: E402  (cross-refs)


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
        and function.name not in _ncc.POLICY_ENTRYPOINTS
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
            if _ncc._is_bare_action_literal(value):
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
            for name in _ncc._assigned_names(target):
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
        for name in _ncc._assigned_names(statement.target):
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
            elif _ncc._is_bare_action_literal(emitted):
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
                for name in _ncc._assigned_names(statement.target):
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
            and function.name in _ncc.POLICY_ENTRYPOINTS
        ):
            scan_block(function.body, module_aliases, module_sequences)
    return list(dict.fromkeys(locations))


def _constant_int(node: ast.AST | None) -> int | None:
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return int(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _ncc._constant_int(node.operand)
        if value is None:
            return None
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left = _ncc._constant_int(node.left)
        right = _ncc._constant_int(node.right)
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
        parent = _ncc._qualified_symbol(node.value, symbol_aliases, string_constants)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Subscript):
        parent = _ncc._qualified_symbol(node.value, symbol_aliases, string_constants)
        key = _ncc._literal_string(node.slice, string_constants)
        if parent in {"builtins", "builtins.__dict__"} and key:
            return f"builtins.{key}"
        return ""
    if (
        isinstance(node, ast.Call)
        and _ncc._qualified_symbol(node.func, symbol_aliases, string_constants).split(".")[-1]
        == "getattr"
        and len(node.args) >= 2
    ):
        parent = _ncc._qualified_symbol(node.args[0], symbol_aliases, string_constants)
        attribute = _ncc._literal_string(node.args[1], string_constants)
        if parent and attribute:
            return f"{parent}.{attribute}"
    return ""


def _context_path(
    node: ast.AST | None,
    context_aliases: dict[str, tuple[str, ...]],
    string_constants: dict[str, str],
) -> tuple[str, ...] | None:
    if isinstance(node, ast.Starred):
        return _ncc._context_path(
            node.value,
            context_aliases,
            string_constants,
        )
    if isinstance(node, ast.Name):
        return context_aliases.get(node.id)
    if isinstance(node, ast.Subscript):
        parent = _ncc._context_path(node.value, context_aliases, string_constants)
        key = _ncc._literal_string(node.slice, string_constants)
        if parent is not None and key is not None:
            return (*parent, key)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
    ):
        parent = _ncc._context_path(
            node.func.value,
            context_aliases,
            string_constants,
        )
        key = _ncc._literal_string(node.args[0], string_constants)
        if parent is not None and key is not None:
            return (*parent, key)
    if isinstance(node, ast.Call) and len(node.args) >= 1:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "__getitem__":
            parent = _ncc._context_path(
                node.func.value,
                context_aliases,
                string_constants,
            )
            key = _ncc._literal_string(node.args[0], string_constants)
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
            parent = _ncc._context_path(
                node.args[0],
                context_aliases,
                string_constants,
            )
            key = _ncc._literal_string(node.args[1], string_constants)
            if parent is not None and key is not None:
                return (*parent, key)
    if isinstance(node, ast.Call) and node.args:
        if isinstance(node.func, ast.Name) and node.func.id in {
            "dict",
            "list",
            "tuple",
        }:
            return _ncc._context_path(
                node.args[0],
                context_aliases,
                string_constants,
            )
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "copy",
            "deepcopy",
        }:
            return _ncc._context_path(
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
        return _ncc._context_path(
            node.func.value,
            context_aliases,
            string_constants,
        )
    if isinstance(node, ast.Dict):
        unpacked = [
            _ncc._context_path(value, context_aliases, string_constants)
            for key, value in zip(node.keys, node.values)
            if key is None
        ]
        if len(unpacked) == 1 and unpacked[0] is not None:
            return unpacked[0]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _ncc._context_path(node.left, context_aliases, string_constants)
        right = _ncc._context_path(node.right, context_aliases, string_constants)
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
            for name in _ncc._assigned_names(item)
        ]
    return []


def _bounded_history_slice(
    node: ast.AST,
    context_aliases: dict[str, tuple[str, ...]],
    string_constants: dict[str, str],
) -> bool:
    if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
        return False
    parent = _ncc._context_path(node.value, context_aliases, string_constants)
    if not parent or parent[0] != "history":
        return False
    lower = _ncc._constant_int(node.slice.lower)
    upper = _ncc._constant_int(node.slice.upper)
    step = _ncc._constant_int(node.slice.step) if node.slice.step is not None else 1
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
    if _ncc._bounded_history_slice(node, context_aliases, string_constants):
        return False
    path = _ncc._context_path(node, context_aliases, string_constants)
    if path and path[0] == "history":
        return True
    return any(
        _ncc._contains_unbounded_history_reference(
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
            child = _ncc._literal_cardinality(item)
            if child:
                total += child
        return total
    if isinstance(node, ast.Dict):
        total = len(node.keys)
        for item in node.values:
            child = _ncc._literal_cardinality(item)
            if child:
                total += child
        return total
    return None


def _range_cardinality(node: ast.AST | None) -> int | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    if node.func.id != "range" or node.keywords or not 1 <= len(node.args) <= 3:
        return None
    values = [_ncc._constant_int(item) for item in node.args]
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
    literal = _ncc._literal_cardinality(node)
    if literal is not None:
        return literal
    if isinstance(node, ast.Name):
        return size_aliases.get(node.id)
    if isinstance(node, ast.BinOp):
        left = _ncc._constructed_size(node.left, size_aliases)
        right = _ncc._constructed_size(node.right, size_aliases)
        if isinstance(node.op, ast.Add) and left is not None and right is not None:
            return left + right
        if isinstance(node.op, ast.Mult):
            left_count = _ncc._constant_int(node.left)
            right_count = _ncc._constant_int(node.right)
            if left is not None and right_count is not None:
                return left * right_count
            if right is not None and left_count is not None:
                return right * left_count
    range_size = _ncc._range_cardinality(node)
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
                _ncc._constant_int(node.args[0])
                or _ncc._constructed_size(node.args[0], size_aliases)
            )
        if leaf == "product":
            sizes = [_ncc._constructed_size(item, size_aliases) for item in node.args]
            if sizes and all(size is not None for size in sizes):
                total = 1
                for size in sizes:
                    total *= int(size)
                return total
    return None


def _nested_mutating_loop_size(node: ast.For | ast.AsyncFor) -> int | None:
    own_size = _ncc._range_cardinality(node.iter)
    if own_size is None:
        return None
    largest = own_size
    for child in node.body:
        if not isinstance(child, (ast.For, ast.AsyncFor)):
            continue
        child_size = _ncc._nested_mutating_loop_size(child)
        if child_size is not None:
            largest = max(largest, own_size * child_size)
    mutates_collection = any(
        isinstance(candidate, ast.Call)
        and isinstance(candidate.func, ast.Attribute)
        and candidate.func.attr in {"add", "append", "extend", "update"}
        for candidate in ast.walk(node)
    )
    return largest if mutates_collection else None
