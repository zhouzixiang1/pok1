"""Deterministic provenance gate for the pure crossover preparation stage."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
import difflib
import hashlib
from pathlib import Path
from typing import Any

from bot_artifact import artifact_manifest


_PROTECTED_CROSSOVER_FILES = frozenset({
    "national_bot.py",
    "precompute.py",
    "national_runtime_manifest.json",
    "policy_epoch_receipt.json",
})
_MAX_GLUE_STATEMENTS_PER_REPLACEMENT = 4

# No trusted system-owned non-Python crossover output currently exists.  A
# future exception must be path- and digest-specific instead of weakening the
# artifact class globally.
_TRUSTED_SYSTEM_NON_PYTHON_ARTIFACTS: dict[str, frozenset[str]] = {}


@dataclass(frozen=True)
class _ArtifactEntry:
    kind: str
    content: bytes | None = None


@dataclass(frozen=True)
class _GlueAuthority:
    """Parent-B symbols and exact attribute/call chains available to glue."""

    direct_symbols: frozenset[str] = frozenset()
    callable_symbols: frozenset[str] = frozenset()
    module_symbols: frozenset[str] = frozenset()
    attribute_chains: frozenset[tuple[str, ...]] = frozenset()
    call_chains: frozenset[tuple[str, ...]] = frozenset()

    def without(self, shadowed: set[str]) -> "_GlueAuthority":
        if not shadowed:
            return self
        return _GlueAuthority(
            direct_symbols=frozenset(self.direct_symbols.difference(shadowed)),
            callable_symbols=frozenset(self.callable_symbols.difference(shadowed)),
            module_symbols=frozenset(self.module_symbols.difference(shadowed)),
            attribute_chains=frozenset(
                chain for chain in self.attribute_chains if chain[0] not in shadowed
            ),
            call_chains=frozenset(
                chain for chain in self.call_chains if chain[0] not in shadowed
            ),
        )


@dataclass(frozen=True)
class _ComponentSlot:
    """One consumable top-level component occurrence from one parent."""

    source: str
    raw_index: int
    raw_node: ast.stmt
    fingerprints: frozenset[str]


def _artifact_snapshot(root: str | Path) -> dict[str, _ArtifactEntry]:
    """Read the complete cache-filtered bot artifact manifest and file bytes."""
    root = Path(root)
    manifest = artifact_manifest(root)
    snapshot: dict[str, _ArtifactEntry] = {}
    for item in manifest.get("entries") or []:
        relative = str(item.get("path") or "")
        kind = str(item.get("type") or "")
        if relative == ".":
            continue
        if kind == "directory":
            snapshot[relative] = _ArtifactEntry("directory")
            continue
        if kind != "file":
            raise ValueError(f"unsupported crossover artifact entry: {item!r}")
        content = (root / relative).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != int(item.get("size") or 0) or digest != item.get("sha256"):
            raise ValueError(
                f"crossover artifact changed while snapshotting: {relative}"
            )
        snapshot[relative] = _ArtifactEntry("file", content)
    return snapshot


def python_source_snapshot(root: str | Path) -> dict[str, _ArtifactEntry]:
    """Backward-compatible name for the complete bot artifact snapshot."""
    return _artifact_snapshot(root)


def _coerce_baseline_entry(value: Any) -> _ArtifactEntry:
    """Accept pre-upgrade in-memory Python snapshots without dropping entries."""
    if isinstance(value, _ArtifactEntry):
        return value
    if isinstance(value, bytes):
        return _ArtifactEntry("file", value)
    if isinstance(value, str):
        return _ArtifactEntry("file", value.encode("utf-8"))
    raise TypeError(f"unsupported crossover baseline entry: {type(value).__name__}")


def _trusted_system_non_python(path: str, entry: _ArtifactEntry) -> bool:
    if entry.kind != "file" or entry.content is None:
        return False
    allowed = _TRUSTED_SYSTEM_NON_PYTHON_ARTIFACTS.get(path, frozenset())
    return hashlib.sha256(entry.content).hexdigest() in allowed


def _artifact_issue(path: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"path": path, "reason": reason, **extra}


def _apply_system_normalizations(path: str, source: str) -> str:
    """Return exact bytes: strict policy provenance has no mutation allowance.

    Protocol invariants live in the system runtime and candidate strategy
    changes belong to an explicit Worker effect.  Silently normalizing copied
    source would make provenance describe bytes that were never reviewed.
    """
    del path
    return source


def _ast_fingerprint(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _module_fingerprint(tree: ast.Module) -> str:
    return _ast_fingerprint(tree)


def _parse_variants(path: str, source: str) -> list[ast.Module]:
    variants = [ast.parse(source)]
    normalized = _apply_system_normalizations(path, source)
    if normalized != source:
        normalized_tree = ast.parse(normalized)
        if _module_fingerprint(normalized_tree) != _module_fingerprint(variants[0]):
            variants.append(normalized_tree)
    return variants


def _component_slots(
    source: str,
    variants: list[ast.Module],
) -> list[_ComponentSlot]:
    """Build one shared raw/normalized allowance per physical parent node.

    System normalization is a trusted alternative representation of a raw
    component, not a second copy budget.  Production normalizations preserve
    top-level order and cardinality, so only same-position, same-kind,
    same-binding nodes are paired.  A cardinality-changing normalization is
    still accepted as an exact whole module, but cannot donate unbound pieces
    to a composed child.
    """
    raw_nodes = list(variants[0].body)
    alternatives = [
        {_ast_fingerprint(node)}
        for node in raw_nodes
    ]
    for variant in variants[1:]:
        if len(variant.body) != len(raw_nodes):
            continue
        for index, (raw_node, normalized_node) in enumerate(
            zip(raw_nodes, variant.body)
        ):
            if type(raw_node) is not type(normalized_node):
                continue
            if _component_keys(raw_node) != _component_keys(normalized_node):
                continue
            alternatives[index].add(_ast_fingerprint(normalized_node))
    return [
        _ComponentSlot(
            source=source,
            raw_index=index,
            raw_node=node,
            fingerprints=frozenset(alternatives[index]),
        )
        for index, node in enumerate(raw_nodes)
    ]


def _allocate_exact_component_slots(
    child_fingerprints: list[str],
    slots: list[_ComponentSlot],
) -> dict[int, int]:
    """Maximum-match child components to single-use parent occurrences."""
    candidates: dict[int, list[int]] = {}
    for child_index, fingerprint in enumerate(child_fingerprints):
        matching = [
            slot_index
            for slot_index, slot in enumerate(slots)
            if fingerprint in slot.fingerprints
        ]
        if not matching:
            continue
        matching.sort(key=lambda slot_index: (
            len(slots[slot_index].fingerprints),
            0 if slots[slot_index].source == "parent_a" else 1,
            0
            if _ast_fingerprint(slots[slot_index].raw_node) == fingerprint
            else 1,
            slots[slot_index].raw_index,
        ))
        candidates[child_index] = matching

    slot_to_child: dict[int, int] = {}
    child_to_slot: dict[int, int] = {}

    def assign(child_index: int, seen_slots: set[int]) -> bool:
        for slot_index in candidates.get(child_index, []):
            if slot_index in seen_slots:
                continue
            seen_slots.add(slot_index)
            displaced = slot_to_child.get(slot_index)
            if displaced is None or assign(displaced, seen_slots):
                slot_to_child[slot_index] = child_index
                child_to_slot[child_index] = slot_index
                return True
        return False

    for child_index in sorted(candidates, key=lambda index: (
        len(candidates[index]),
        index,
    )):
        assign(child_index, set())
    return child_to_slot


def _bound_target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        result: set[str] = set()
        for item in target.elts:
            result.update(_bound_target_names(item))
        return result
    return set()


class _ModuleNameCollector(ast.NodeVisitor):
    """Collect names bound by one module-level component, skipping inner scopes."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.names.add(alias.asname or alias.name)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.names.add(node.id)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(str(node.name))
        self.generic_visit(node)


def _module_bound_names(node: ast.AST) -> set[str]:
    collector = _ModuleNameCollector()
    collector.visit(node)
    return collector.names


def _component_keys(node: ast.AST) -> set[tuple[str, str]]:
    if isinstance(node, ast.FunctionDef):
        return {("function", node.name)}
    if isinstance(node, ast.AsyncFunctionDef):
        return {("async_function", node.name)}
    if isinstance(node, ast.ClassDef):
        return {("class", node.name)}
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return {("import", name) for name in _module_bound_names(node)}
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        return {("assignment", name) for name in _module_bound_names(node)}
    return {("symbol", name) for name in _module_bound_names(node)}


def _direct_binding_kinds(node: ast.AST) -> tuple[set[str], set[str], set[str]]:
    """Return direct symbols, directly callable symbols, and module imports."""
    direct: set[str] = set()
    callable_names: set[str] = set()
    modules: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            modules.add(alias.asname or alias.name.split(".", 1)[0])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                continue
            name = alias.asname or alias.name
            direct.add(name)
            # Explicit ``from x import y`` is an intentional direct binding;
            # the crossover may invoke it without inventing a module attribute.
            callable_names.add(name)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        direct.add(node.name)
        callable_names.add(node.name)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            direct.update(_bound_target_names(target))
    elif isinstance(node, ast.AnnAssign):
        direct.update(_bound_target_names(node.target))
    return direct, callable_names, modules


def _expression_chain(value: ast.AST) -> tuple[str, ...] | None:
    if isinstance(value, ast.Name):
        return (value.id,)
    if isinstance(value, ast.Attribute):
        prefix = _expression_chain(value.value)
        if prefix is not None:
            return (*prefix, value.attr)
    return None


def _authority_for_child(
    parent_b_variants: list[ast.Module],
    child_tree: ast.Module,
    parent_b_component_fingerprints: set[str],
    parent_b_child_indices: set[int],
) -> _GlueAuthority:
    """Bind glue only to complete Parent-B components present in the child."""
    child_nodes = list(child_tree.body)
    exact_b_nodes = [
        node
        for index, node in enumerate(child_nodes)
        if index in parent_b_child_indices
    ]
    candidate_direct: set[str] = set()
    candidate_callable: set[str] = set()
    candidate_modules: set[str] = set()
    for node in exact_b_nodes:
        direct, callable_names, modules = _direct_binding_kinds(node)
        candidate_direct.update(direct)
        candidate_callable.update(callable_names)
        candidate_modules.update(modules)

    # A Parent-A or novel binding with the same name shadows the copied B
    # component.  Every child top-level component that binds an authorized name
    # must itself be a complete Parent-B component.
    binding_components: dict[str, list[str]] = {}
    for node in child_nodes:
        fingerprint = _ast_fingerprint(node)
        for name in _module_bound_names(node):
            binding_components.setdefault(name, []).append(fingerprint)
    candidates = candidate_direct | candidate_modules
    unshadowed = {
        name for name in candidates
        if binding_components.get(name)
        and all(
            fingerprint in parent_b_component_fingerprints
            for fingerprint in binding_components[name]
        )
    }
    direct = candidate_direct.intersection(unshadowed)
    modules = candidate_modules.intersection(unshadowed)
    callable_names = candidate_callable.intersection(unshadowed)

    roots = direct | modules
    attribute_chains: set[tuple[str, ...]] = set()
    call_chains: set[tuple[str, ...]] = set()
    for tree in parent_b_variants:
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                chain = _expression_chain(node)
                if chain is not None and chain[0] in roots:
                    attribute_chains.add(chain)
            elif isinstance(node, ast.Call):
                chain = _expression_chain(node.func)
                if chain is not None and chain[0] in roots:
                    call_chains.add(chain)

    # A Parent-B assignment is callable only if Parent B itself called that
    # bound name.  Functions/classes and explicit from-imports were already
    # admitted above.
    callable_names.update(
        chain[0]
        for chain in call_chains
        if len(chain) == 1 and chain[0] in direct
    )
    return _GlueAuthority(
        direct_symbols=frozenset(direct),
        callable_symbols=frozenset(callable_names),
        module_symbols=frozenset(modules),
        attribute_chains=frozenset(attribute_chains),
        call_chains=frozenset(call_chains),
    )


class _FunctionLocalCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.globals: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.names.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.names.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.add(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_Global(self, node: ast.Global) -> None:
        self.globals.update(node.names)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.names.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name != "*":
                self.names.add(alias.asname or alias.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(str(node.name))
        self.generic_visit(node)


def _function_parameter_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    result = {
        argument.arg
        for argument in [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
    }
    if node.args.vararg is not None:
        result.add(node.args.vararg.arg)
    if node.args.kwarg is not None:
        result.add(node.args.kwarg.arg)
    return result


def _function_local_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    collector = _FunctionLocalCollector()
    for statement in node.body:
        collector.visit(statement)
    return (
        collector.names
        | _function_parameter_names(node)
    ).difference(collector.globals)


def _function_header_fingerprint(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    payload = (
        type(node).__name__,
        node.name,
        _ast_fingerprint(node.args),
        tuple(_ast_fingerprint(item) for item in node.decorator_list),
        _ast_fingerprint(node.returns) if node.returns is not None else "",
        node.type_comment or "",
        tuple(
            _ast_fingerprint(item)
            for item in (getattr(node, "type_params", None) or [])
        ),
    )
    return repr(payload)


class _ContextAttributeCollector(ast.NodeVisitor):
    """Collect exact attribute chains used by one Parent-A function scope."""

    def __init__(self, roots: set[str]) -> None:
        self.roots = roots
        self.chains: set[tuple[str, ...]] = set()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        chain = _expression_chain(node)
        if chain is not None and chain[0] in self.roots:
            self.chains.add(chain)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return


def _function_context_attribute_chains(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    context_inputs: set[str],
) -> frozenset[tuple[str, ...]]:
    collector = _ContextAttributeCollector(context_inputs)
    for statement in node.body:
        collector.visit(statement)
    return frozenset(collector.chains)


def _attribute_allowed(
    value: ast.Attribute,
    *,
    authority: _GlueAuthority,
    trusted_locals: set[str],
    context_inputs: set[str],
    context_attribute_chains: frozenset[tuple[str, ...]],
    allow_context: bool,
) -> bool:
    chain = _expression_chain(value)
    if chain is None:
        return False
    root = chain[0]
    if allow_context and root in context_inputs:
        return chain in context_attribute_chains
    return chain in authority.attribute_chains


def _argument_allowed(
    value: ast.AST,
    *,
    authority: _GlueAuthority,
    trusted_locals: set[str],
    context_inputs: set[str],
    context_attribute_chains: frozenset[tuple[str, ...]],
) -> bool:
    if isinstance(value, ast.Name):
        return (
            value.id in context_inputs
            or value.id in authority.direct_symbols
            or value.id in trusted_locals
        )
    if isinstance(value, ast.Attribute):
        return _attribute_allowed(
            value,
            authority=authority,
            trusted_locals=trusted_locals,
            context_inputs=context_inputs,
            context_attribute_chains=context_attribute_chains,
            allow_context=True,
        )
    if isinstance(value, ast.Call):
        return _call_allowed(
            value,
            authority=authority,
            trusted_locals=trusted_locals,
            context_inputs=context_inputs,
            context_attribute_chains=context_attribute_chains,
        )
    # Constants, operators, containers, comprehensions, lambdas, and unpacking
    # can encode an independent policy and are not integration glue.
    return False


def _call_allowed(
    value: ast.Call,
    *,
    authority: _GlueAuthority,
    trusted_locals: set[str],
    context_inputs: set[str],
    context_attribute_chains: frozenset[tuple[str, ...]],
) -> bool:
    if isinstance(value.func, ast.Name):
        if value.func.id not in authority.callable_symbols:
            return False
    elif isinstance(value.func, ast.Attribute):
        chain = _expression_chain(value.func)
        if chain is None:
            return False
        if chain not in authority.call_chains:
            return False
    else:
        return False
    if any(isinstance(argument, ast.Starred) for argument in value.args):
        return False
    if any(keyword.arg is None for keyword in value.keywords):
        return False
    return all(
        _argument_allowed(
            argument,
            authority=authority,
            trusted_locals=trusted_locals,
            context_inputs=context_inputs,
            context_attribute_chains=context_attribute_chains,
        )
        for argument in value.args
    ) and all(
        _argument_allowed(
            keyword.value,
            authority=authority,
            trusted_locals=trusted_locals,
            context_inputs=context_inputs,
            context_attribute_chains=context_attribute_chains,
        )
        for keyword in value.keywords
    )


def _value_allowed(
    value: ast.AST,
    *,
    authority: _GlueAuthority,
    trusted_locals: set[str],
    context_inputs: set[str],
    context_attribute_chains: frozenset[tuple[str, ...]],
) -> bool:
    if isinstance(value, ast.Call):
        return _call_allowed(
            value,
            authority=authority,
            trusted_locals=trusted_locals,
            context_inputs=context_inputs,
            context_attribute_chains=context_attribute_chains,
        )
    if isinstance(value, ast.Name):
        # A copied B constant/marker is evidence, not a replacement decision.
        # Only an actual B-call result may flow directly to the return sink.
        return value.id in trusted_locals
    if isinstance(value, ast.Attribute):
        return _attribute_allowed(
            value,
            authority=authority,
            trusted_locals=trusted_locals,
            context_inputs=context_inputs,
            context_attribute_chains=context_attribute_chains,
            allow_context=False,
        )
    return False


def _glue_statement_allowed(
    statement: ast.stmt,
    *,
    authority: _GlueAuthority,
    trusted_locals: set[str],
    context_inputs: set[str],
    context_attribute_chains: frozenset[tuple[str, ...]],
) -> tuple[bool, set[str], bool]:
    if isinstance(statement, ast.Assign):
        if (
            len(statement.targets) != 1
            or not isinstance(statement.targets[0], ast.Name)
            or not isinstance(statement.value, ast.Call)
        ):
            return False, set(), False
        allowed = _call_allowed(
            statement.value,
            authority=authority,
            trusted_locals=trusted_locals,
            context_inputs=context_inputs,
            context_attribute_chains=context_attribute_chains,
        )
        return allowed, ({statement.targets[0].id} if allowed else set()), False
    if isinstance(statement, ast.Return) and statement.value is not None:
        allowed = _value_allowed(
            statement.value,
            authority=authority,
            trusted_locals=trusted_locals,
            context_inputs=context_inputs,
            context_attribute_chains=context_attribute_chains,
        )
        return allowed, set(), allowed
    # Bare calls have no data-flow sink.  Repeating a stochastic or side-effect
    # Parent-B call would create a new policy even though every individual call
    # is provenance-valid, so thin glue never admits call expressions.
    return False, set(), False


def _function_glue_replacement_allowed(
    parent_a_node: ast.FunctionDef | ast.AsyncFunctionDef,
    child_node: ast.FunctionDef | ast.AsyncFunctionDef,
    authority: _GlueAuthority,
) -> bool:
    """Allow only a same-function terminal return replaced by B-rooted glue."""
    if _function_header_fingerprint(parent_a_node) != _function_header_fingerprint(child_node):
        return False
    a_fingerprints = [_ast_fingerprint(item) for item in parent_a_node.body]
    child_fingerprints = [_ast_fingerprint(item) for item in child_node.body]
    preserved_child_indices: set[int] = set()
    removed_a_indices: set[int] = set()
    glue_child_indices: set[int] = set()
    for tag, a_start, a_end, c_start, c_end in difflib.SequenceMatcher(
        a=a_fingerprints,
        b=child_fingerprints,
        autojunk=False,
    ).get_opcodes():
        if tag == "equal":
            preserved_child_indices.update(range(c_start, c_end))
        if tag in {"replace", "delete"}:
            removed_a_indices.update(range(a_start, a_end))
        if tag in {"replace", "insert"}:
            glue_child_indices.update(range(c_start, c_end))
    if not glue_child_indices:
        return False
    if len(glue_child_indices) > _MAX_GLUE_STATEMENTS_PER_REPLACEMENT:
        return False
    ordered_glue_indices = sorted(glue_child_indices)
    if (
        ordered_glue_indices
        != list(range(ordered_glue_indices[0], len(child_node.body)))
        or not isinstance(child_node.body[-1], ast.Return)
    ):
        # Integration glue is a small terminal dispatch suffix.  Interleaving
        # it with preserved Parent-A state transitions makes provenance and
        # result consumption ambiguous.
        return False
    # Crossover glue may replace terminal dispatch, but may not erase Parent A
    # predicates, state preparation, loops, assignments, or side effects.
    if any(
        not isinstance(parent_a_node.body[index], ast.Return)
        for index in removed_a_indices
    ):
        return False

    child_locals = _function_local_names(child_node)
    context_inputs = (
        _function_parameter_names(parent_a_node)
        | _function_local_names(parent_a_node)
    )
    context_attribute_chains = _function_context_attribute_chains(
        parent_a_node,
        context_inputs,
    )
    available = authority.without(child_locals)
    trusted_locals: set[str] = set()
    active_local: str | None = None
    glue_call_counts: Counter[tuple[str, ...]] = Counter()
    consumed_parent_b = False
    for index, statement in enumerate(child_node.body):
        if index in preserved_child_indices:
            # A preserved A statement is not new glue and cannot manufacture a
            # B-result local merely because a same-named B symbol now exists.
            continue
        if index not in glue_child_indices:
            return False
        statement_call_chains: list[tuple[str, ...]] = []
        for call in (
            item for item in ast.walk(statement) if isinstance(item, ast.Call)
        ):
            chain = _expression_chain(call.func)
            if chain is None:
                return False
            statement_call_chains.append(chain)
        glue_call_counts.update(statement_call_chains)
        if any(count > 1 for count in glue_call_counts.values()):
            # Repetition changes stochastic sampling and side-effect frequency;
            # it is strategy synthesis, not a thin connection between parents.
            return False
        active_uses = sum(
            1
            for item in ast.walk(statement)
            if (
                isinstance(item, ast.Name)
                and isinstance(item.ctx, ast.Load)
                and item.id == active_local
            )
        )
        if active_local is not None and active_uses != 1:
            return False
        allowed, newly_trusted, consumed = _glue_statement_allowed(
            statement,
            authority=available,
            trusted_locals=trusted_locals,
            context_inputs=context_inputs,
            context_attribute_chains=context_attribute_chains,
        )
        if not allowed:
            return False
        if newly_trusted.intersection(trusted_locals | context_inputs):
            return False
        if active_local is not None:
            trusted_locals.remove(active_local)
            active_local = None
        if newly_trusted:
            active_local = next(iter(newly_trusted))
            trusted_locals.add(active_local)
        consumed_parent_b = consumed_parent_b or consumed
    if active_local is not None:
        # Every produced value must flow once into the immediately following
        # authorized B call or terminal return.  Fan-out and dead results
        # encode new policy.
        return False
    return consumed_parent_b


def _node_has_numeric_literal(node: ast.AST) -> bool:
    return any(
        isinstance(item, ast.Constant)
        and isinstance(item.value, (int, float, complex))
        and not isinstance(item.value, bool)
        for item in ast.walk(node)
    )


def _node_source_lines(source: str, node: ast.AST) -> list[str]:
    lines = source.splitlines()
    start = max(0, int(getattr(node, "lineno", 1)) - 1)
    end = int(getattr(node, "end_lineno", start + 1))
    return [
        stripped
        for line in lines[start:end]
        if (stripped := line.strip()) and not stripped.startswith("#")
    ][:12]


def _python_composition_issues(
    path: str,
    parent_a_source: str,
    parent_b_source: str,
    child_source: str,
) -> list[dict[str, Any]]:
    try:
        parent_a_variants = _parse_variants(path, parent_a_source)
        parent_b_variants = _parse_variants(path, parent_b_source)
        child_tree = ast.parse(child_source)
    except SyntaxError as exc:
        return [_artifact_issue(
            path,
            "python_ast_parse_failed",
            detail=f"{type(exc).__name__}: {exc}",
        )]

    child_module_fingerprint = _module_fingerprint(child_tree)
    if any(
        child_module_fingerprint == _module_fingerprint(tree)
        for tree in [*parent_a_variants, *parent_b_variants]
    ):
        return []

    a_raw_tree = parent_a_variants[0]
    child_fingerprints = [_ast_fingerprint(node) for node in child_tree.body]
    parent_a_slots = _component_slots("parent_a", parent_a_variants)
    parent_b_slots = _component_slots("parent_b", parent_b_variants)
    component_slots = [*parent_a_slots, *parent_b_slots]
    exact_allocations = _allocate_exact_component_slots(
        child_fingerprints,
        component_slots,
    )
    known_component_fingerprints = {
        fingerprint
        for slot in component_slots
        for fingerprint in slot.fingerprints
    }
    b_component_fingerprints = {
        fingerprint
        for slot in parent_b_slots
        for fingerprint in slot.fingerprints
    }
    b_child_indices = {
        child_index
        for child_index, slot_index in exact_allocations.items()
        if component_slots[slot_index].source == "parent_b"
    }
    authority = _authority_for_child(
        parent_b_variants,
        child_tree,
        b_component_fingerprints,
        b_child_indices,
    )

    replacement_key_counts: Counter[tuple[str, str]] = Counter()
    consumed_parent_a_indices: set[int] = set()
    has_parent_b_component = bool(b_child_indices)
    issues: list[dict[str, Any]] = []
    a_functions: dict[tuple[type[ast.AST], str], list[ast.AST]] = {}
    for node in a_raw_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a_functions.setdefault((type(node), node.name), []).append(node)

    for child_index, (node, fingerprint) in enumerate(
        zip(child_tree.body, child_fingerprints)
    ):
        slot_index = exact_allocations.get(child_index)
        if slot_index is not None:
            slot = component_slots[slot_index]
            if slot.source == "parent_a":
                consumed_parent_a_indices.add(slot.raw_index)
            else:
                replacement_key_counts.update(_component_keys(node))
            continue
        if fingerprint in known_component_fingerprints:
            # An exact raw or system-normalized parent component whose physical
            # occurrence token was already consumed cannot fall through to the
            # glue policy.  This is strict for side-effect expressions,
            # assignments, deletes, functions, classes, and imports alike.
            issues.append(_artifact_issue(
                path,
                "parent_component_multiplicity_exceeded",
                parents=sorted({
                    slot.source
                    for slot in component_slots
                    if fingerprint in slot.fingerprints
                }),
                nodes=[type(node).__name__],
                lines=_node_source_lines(child_source, node),
            ))
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            candidates = a_functions.get((type(node), node.name), [])
            if any(
                _function_glue_replacement_allowed(candidate, node, authority)
                for candidate in candidates
            ):
                replacement_key_counts.update(_component_keys(node))
                continue
        reason = (
            "independent_numeric_mutation_not_in_parent_b"
            if _node_has_numeric_literal(node)
            else "independent_logic_not_parent_b_component"
        )
        issues.append(_artifact_issue(
            path,
            reason,
            nodes=[type(node).__name__],
            lines=_node_source_lines(child_source, node),
        ))

    for raw_index, node in enumerate(a_raw_tree.body):
        if raw_index in consumed_parent_a_indices:
            continue
        keys = _component_keys(node)
        if keys and all(replacement_key_counts[key] > 0 for key in keys):
            for key in keys:
                replacement_key_counts[key] -= 1
            continue
        issues.append(_artifact_issue(
            path,
            "parent_a_component_deleted_without_parent_b_or_glue_replacement",
            component=sorted(keys) or [type(node).__name__],
        ))

    if not has_parent_b_component:
        issues.append(_artifact_issue(
            path,
            "no_parent_b_component_evidence",
        ))
    return issues


def validate_crossover_recombination_provenance(
    parent_a_baseline: dict[str, Any],
    parent_b_dir: str | Path,
    child_dir: str | Path,
) -> list[dict[str, Any]]:
    """Reject independent mutations hidden inside a pure crossover."""
    parent_a = {
        path: _coerce_baseline_entry(value)
        for path, value in parent_a_baseline.items()
    }
    parent_b = _artifact_snapshot(parent_b_dir)
    child = _artifact_snapshot(child_dir)
    issues: list[dict[str, Any]] = []

    for path in sorted(set(parent_a) | set(child)):
        baseline_entry = parent_a.get(path)
        child_entry = child.get(path)
        if baseline_entry == child_entry:
            continue
        if path in _PROTECTED_CROSSOVER_FILES:
            issues.append(_artifact_issue(
                path,
                "system_owned_runtime_changed_during_crossover",
            ))
            continue

        b_entry = parent_b.get(path)
        if child_entry is not None and child_entry == b_entry:
            continue
        if child_entry is None:
            if b_entry is None:
                continue
            issues.append(_artifact_issue(
                path,
                "deleted_file_not_traceable_to_parent_b",
            ))
            continue
        if child_entry.kind == "directory" or (
            baseline_entry is not None and baseline_entry.kind == "directory"
        ):
            issues.append(_artifact_issue(
                path,
                "directory_mutation_not_traceable_to_parent_b",
            ))
            continue
        if not path.endswith(".py"):
            if _trusted_system_non_python(path, child_entry):
                continue
            issues.append(_artifact_issue(
                path,
                (
                    "new_non_python_artifact_not_exact_parent_b"
                    if baseline_entry is None
                    else "non_python_artifact_not_exact_parent_b"
                ),
            ))
            continue
        if baseline_entry is None:
            if (
                b_entry is not None
                and b_entry.kind == "file"
                and b_entry.content is not None
                and child_entry.content is not None
            ):
                try:
                    parent_b_source = b_entry.content.decode("utf-8")
                    normalized_b = _apply_system_normalizations(path, parent_b_source)
                    if child_entry.content == normalized_b.encode("utf-8"):
                        continue
                except UnicodeError:
                    pass
            issues.append(_artifact_issue(
                path,
                "new_file_not_exact_parent_b_component",
            ))
            continue
        if baseline_entry.kind != "file" or child_entry.kind != "file":
            issues.append(_artifact_issue(path, "python_artifact_type_changed"))
            continue
        try:
            parent_a_source = (baseline_entry.content or b"").decode("utf-8")
            parent_b_source = (
                (b_entry.content or b"").decode("utf-8")
                if b_entry is not None and b_entry.kind == "file"
                else ""
            )
            child_source = (child_entry.content or b"").decode("utf-8")
        except UnicodeDecodeError:
            issues.append(_artifact_issue(path, "python_source_not_utf8"))
            continue
        issues.extend(_python_composition_issues(
            path,
            parent_a_source,
            parent_b_source,
            child_source,
        ))
    return issues
