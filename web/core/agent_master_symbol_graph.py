"""Master source-symbol graph subsystem.

Extracted from agent_master_validation.py as a single business responsibility:
walk a frozen source artifact, build a deterministic AST call-graph + digest,
normalize/fuzzy-resolve "policy.py:symbol" references, compute policy-ABI
reachability depths from entrypoint symbols, and render the bounded prompt
indexes (source-symbol catalog and snapshot-reference catalog) the Master
ensemble actually sees.

All public symbols are re-exported by agent_master_validation.py and then by
agent_master.py for backward compatibility.
"""

import ast
import hashlib
import json
from pathlib import Path


_POLICY_ABI_ENTRYPOINT_SYMBOLS = (
    "policy.py:get_baseline_decision",
    "policy.py:iter_decisions",
)
_DECISION_RELEVANT_SYMBOL_TERMS = (
    "action",
    "decision",
    "equity",
    "intent",
    "line",
    "memory",
    "opponent",
    "posterior",
    "raise",
    "range",
    "refine",
    "simulation",
    "strategy",
    "strength",
)
_UTILITY_SYMBOL_TERMS = (
    "bounded",
    "card_id",
    "clamp",
    "hole_ids",
    "integer",
    "number",
)


def _safe_relative_python_path(value: object) -> str | None:
    """Return one normalized source-relative Python path, never an escape."""
    raw = str(value or "").strip().replace("\\", "/")
    path = Path(raw)
    if (
        not raw
        or path.is_absolute()
        or ".." in path.parts
        or path.suffix != ".py"
    ):
        return None
    return path.as_posix()


def _source_symbol_graph(source_dir: Path) -> tuple[dict[str, set[str]], str]:
    """Index real top-level functions/methods and their direct call leaves.

    The graph deliberately proves only a small, deterministic claim: every
    symbol exists in the frozen baseline and every adjacent item in a submitted
    reachability chain is a direct syntactic call.  It does not ask an LLM to
    judge whether prose merely *sounds* reachable.
    """
    graph: dict[str, set[str]] = {}
    digest = hashlib.sha256()
    source_dir = Path(source_dir).resolve()
    for path in sorted(source_dir.rglob("*.py")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            relative = path.resolve().relative_to(source_dir).as_posix()
            payload = path.read_bytes()
        except (OSError, ValueError):
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        try:
            tree = ast.parse(payload, filename=relative)
        except SyntaxError:
            # Syntax-invalid files cannot supply evidence, but they still bind
            # the source artifact digest and therefore cannot drift invisibly.
            continue

        def calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
            """Return calls executed by this body, excluding nested scopes."""

            result: set[str] = set()

            class DirectBodyCalls(ast.NodeVisitor):
                def visit_Call(self, child: ast.Call) -> None:
                    target = child.func
                    if isinstance(target, ast.Name):
                        result.add(target.id)
                    elif isinstance(target, ast.Attribute):
                        result.add(target.attr)
                    self.generic_visit(child)

                # A nested scope's body is not executed merely because the
                # enclosing policy function runs. Treat it as a separate,
                # unindexed proof obligation instead of inventing a direct edge.
                def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
                    return None

                def visit_AsyncFunctionDef(
                    self,
                    child: ast.AsyncFunctionDef,
                ) -> None:
                    return None

                def visit_ClassDef(self, child: ast.ClassDef) -> None:
                    return None

                def visit_Lambda(self, child: ast.Lambda) -> None:
                    return None

            visitor = DirectBodyCalls()
            for statement in node.body:
                visitor.visit(statement)
            return result

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                graph[f"{relative}:{node.name}"] = calls(node)
            elif isinstance(node, ast.ClassDef):
                # Calling a class does not execute every method body. Keep the
                # class symbol available as a callee but give it no fabricated
                # aggregate edges; each method owns its own direct-call facts.
                graph[f"{relative}:{node.name}"] = set()
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        graph[f"{relative}:{node.name}.{child.name}"] = calls(child)
    return graph, digest.hexdigest()


def _source_symbol_ast_digest(source_dir: Path, symbol: str) -> str | None:
    """Digest one exact graph symbol's executable AST without line metadata."""

    normalized = _normalize_source_symbol(symbol)
    if normalized is None:
        return None
    relative, qualified = normalized.rsplit(":", 1)
    source_dir = Path(source_dir).resolve()
    candidate = (source_dir / relative).resolve()
    try:
        candidate.relative_to(source_dir)
        tree = ast.parse(candidate.read_bytes(), filename=relative)
    except (OSError, ValueError, SyntaxError):
        return None
    parts = qualified.split(".")
    node: ast.AST | None = None
    for top_level in tree.body:
        if getattr(top_level, "name", None) == parts[0] and isinstance(
            top_level,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            node = top_level
            break
    for part in parts[1:]:
        if not isinstance(node, ast.ClassDef):
            return None
        node = next(
            (
                child
                for child in node.body
                if getattr(child, "name", None) == part
                and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            ),
            None,
        )
    if node is None:
        return None
    canonical = ast.dump(node, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _proposal_source_symbol_digests(
    proposals: list[dict],
    source_dir: Path,
) -> dict[str, dict[str, str]]:
    """Freeze the prepared baseline functions named by each Scout contract."""

    result: dict[str, dict[str, str]] = {}
    for proposal in proposals:
        proposal_id = str(proposal.get("proposal_id") or "")
        row = {}
        for symbol in proposal.get("source_symbols") or []:
            digest = _source_symbol_ast_digest(source_dir, str(symbol))
            if digest is None:
                raise ValueError(
                    f"proposal_source_symbol_digest_missing:{proposal_id}:{symbol}"
                )
            row[str(symbol)] = digest
        result[proposal_id] = row
    return result


def _verified_source_edges(
    graph: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Resolve direct call leaves to unique frozen source symbols."""

    symbols_by_leaf: dict[str, list[str]] = {}
    for symbol in sorted(graph):
        leaf = symbol.rsplit(":", 1)[1].rsplit(".", 1)[-1]
        symbols_by_leaf.setdefault(leaf, []).append(symbol)
    return {
        caller: sorted({
            candidates[0]
            for leaf in graph[caller]
            if len(candidates := symbols_by_leaf.get(leaf, [])) == 1
            and candidates[0] != caller
        })
        for caller in sorted(graph)
    }


def _policy_abi_reachable_depths(
    graph: dict[str, set[str]],
) -> dict[str, int]:
    """Return symbols reachable from the two candidate policy ABI entries."""

    verified_edges = _verified_source_edges(graph)
    reachable = {
        symbol: 0
        for symbol in _POLICY_ABI_ENTRYPOINT_SYMBOLS
        if symbol in verified_edges
    }
    pending = list(reachable)
    while pending:
        caller = pending.pop(0)
        for callee in verified_edges.get(caller, ()):
            if callee in verified_edges and callee not in reachable:
                reachable[callee] = reachable[caller] + 1
                pending.append(callee)
    return reachable


def _source_symbol_prompt_index(
    graph: dict[str, set[str]],
    *,
    maximum_chars: int = 18_000,
) -> str:
    """Render deterministic, validator-matching call evidence for weak scouts.

    Asking a weaker model to rediscover exact ``file.py:symbol`` spellings and
    direct call leaves wastes both calls and context.  The system has already
    parsed the frozen source, so expose the accepted edge vocabulary directly.
    Lines are kept whole under a hard bound; omitted tails remain available via
    the read-only source tool but cannot be invented in a proposal.
    """
    verified_edges = _verified_source_edges(graph)

    header = (
        "SYSTEM-VERIFIED SOURCE CALL INDEX (exact proposal spellings; each arrow "
        "is a validator-accepted direct syntactic call leaf):"
    )
    lines = [header]
    used_chars = len(header)

    def append_line(line: str) -> bool:
        nonlocal used_chars
        required = len(line) + (1 if lines else 0)
        if used_chars + required > maximum_chars:
            return False
        lines.append(line)
        used_chars += required
        return True

    # Prefer only policy edges reachable from the two actual policy ABI
    # entrypoints.  A syntactically valid but dead helper must not become the
    # model's easiest copied chain merely because its name sorts first.
    reachable_depth = _policy_abi_reachable_depths(graph)
    entrypoints = set(_POLICY_ABI_ENTRYPOINT_SYMBOLS)
    preferred_candidates = [
        (
            reachable_depth[caller],
            caller,
            callee,
        )
        for caller in reachable_depth
        if caller.startswith("policy.py:")
        for callee in verified_edges.get(caller, ())
    ]

    def preferred_rank(item: tuple[int, str, str]) -> tuple:
        depth, caller, callee = item
        leaf = callee.rsplit(":", 1)[1].rsplit(".", 1)[-1].lower()
        downstream = verified_edges.get(callee, ())
        decision_score = sum(
            term in leaf for term in _DECISION_RELEVANT_SYMBOL_TERMS
        ) + sum(
            any(term in target.lower() for term in _DECISION_RELEVANT_SYMBOL_TERMS)
            for target in downstream
        )
        utility_score = sum(term in leaf for term in _UTILITY_SYMBOL_TERMS)
        return (
            decision_score <= 0,
            utility_score > 0,
            -decision_score,
            caller not in entrypoints,
            depth,
            -len(downstream),
            caller,
            callee,
        )

    preferred = sorted(preferred_candidates, key=preferred_rank)[:8]
    preferred_header = (
        "SYSTEM-VERIFIED PREFERRED CURRENT STARTING EDGES (extend through "
        "current direct edges until reachable_chain terminates at change_symbol; "
        "a two-symbol edge is complete only when its callee is change_symbol):"
    )
    preferred_lines = [
        "- " + json.dumps([caller, callee], separators=(",", ":"))
        for _depth, caller, callee in preferred
    ]
    if preferred_lines and (
        used_chars + 1 + len(preferred_header) + 1 + len(preferred_lines[0])
        <= maximum_chars
    ):
        append_line(preferred_header)
        for line in preferred_lines:
            if not append_line(line):
                break
        append_line("FULL VALIDATED EDGE INDEX:")
    # The validator requires the chain's first symbol to be reachable from the
    # candidate policy ABI.  Publishing unrelated national_bot/precompute/dead
    # helper edges made them look admissible and consumed ~8k prompt chars.
    # Every callee below is still a verified syntactic edge; only impossible
    # starting subgraphs are omitted.
    for caller in sorted(
        reachable_depth,
        key=lambda symbol: (reachable_depth[symbol], symbol),
    ):
        callees = verified_edges[caller]
        if not callees:
            continue
        line = f"- {caller} -> {', '.join(callees)}"
        if not append_line(line):
            append_line("- [remaining verified edges omitted by deterministic size bound]")
            break
    if len(lines) == 1:
        append_line("- [no validator-accepted internal call edges]")
    return "\n".join(lines)


def _snapshot_reference_prompt_index(snapshot_dir: Path) -> str:
    """Render bounded, validator-ready JSON-pointer anchors for Scout evidence."""
    # Lazy import: the strength-snapshot evidence business (the four helpers
    # ``_STRENGTH_SNAPSHOT_FILENAMES``, ``_SNAPSHOT_METADATA_ONLY_TERMINALS``,
    # ``_SNAPSHOT_STRENGTH_SIGNAL_KEYS`` and ``_snapshot_node_has_strength_signal``)
    # stays in agent_master_validation.  This function only needs the filename
    # allowlist, the metadata-only terminal set, and the evidence-binding helper
    # (which itself delegates to ``_snapshot_node_has_strength_signal``).  No
    # circular-import risk: agent_master_symbol_graph imports only stdlib at
    # module top level plus this one function-local import.
    from agent_master_validation import (
        _STRENGTH_SNAPSHOT_FILENAMES,
        _SNAPSHOT_METADATA_ONLY_TERMINALS,
        _snapshot_reference_evidence_binding,
    )

    root = Path(snapshot_dir)
    rows: list[str] = []
    try:
        candidates = sorted(
            path for path in root.iterdir()
            if path.is_file() and not path.is_symlink()
            and path.suffix.lower() == ".json"
            and path.name in _STRENGTH_SNAPSHOT_FILENAMES
        )[:16]
    except OSError:
        candidates = []
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload:
            pointers = []
            for key in (
                key
                for key in payload
                if str(key).lower() not in _SNAPSHOT_METADATA_ONLY_TERMINALS
            ):
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                reference = f"snapshot:{path.name}#/{escaped}"
                if _snapshot_reference_evidence_binding(reference, root) is None:
                    continue
                pointers.append(reference)
                if len(pointers) >= 12:
                    break
            if not pointers:
                continue
            rows.append(
                f"- {path.name}: " + ", ".join(pointers)
            )
        elif isinstance(payload, list) and payload:
            reference = f"snapshot:{path.name}#/0"
            if _snapshot_reference_evidence_binding(reference, root) is not None:
                rows.append(f"- {path.name}: {reference}")
    if not rows:
        return ""
    return "\n".join((
        "SYSTEM-VERIFIED SNAPSHOT POINTER INDEX (Read the chosen JSON and copy "
        "at least one exact relative pointer whose node supports the weakness):",
        *rows,
    ))


def _normalize_source_symbol(value: object) -> str | None:
    text = str(value or "").strip()
    if ":" not in text:
        return None
    filename, symbol = text.rsplit(":", 1)
    filename = _safe_relative_python_path(filename)
    if filename is None:
        return None
    symbol_parts = symbol.split(".")
    if not symbol_parts or any(not part.isidentifier() for part in symbol_parts):
        return None
    return f"{filename}:{symbol}"


def _fuzzy_resolve_symbol(
    symbol: str,
    source_graph: dict,
    *,
    emit_event: bool = True,
) -> str | None:
    """Resolve a source symbol that may have a minor naming error.

    The system injects the exact call index into every scout prompt, but weak
    models still misspell function names (e.g. ``_hole_ids`` instead of
    ``_card_ids``).  This resolver corrects unambiguous within-file mismatches
    without weakening the existence guarantee: the resolved symbol must still
    be a real entry in the frozen source graph.
    """
    if symbol in source_graph:
        return symbol
    file_part, _, leaf = symbol.rpartition(":")
    if not file_part or not leaf:
        return None
    candidates = []
    for key in source_graph:
        key_file, _, key_leaf = key.rpartition(":")
        if key_file == file_part:
            candidates.append((key, key_leaf.rsplit(".", 1)[-1]))
    if not candidates:
        return None
    bare_leaf = leaf.rsplit(".", 1)[-1]
    exact = [k for k, cl in candidates if cl == bare_leaf]
    if len(exact) == 1:
        return exact[0]
    import difflib
    close = difflib.get_close_matches(
        bare_leaf, [cl for _, cl in candidates], n=1, cutoff=0.5)
    if not close:
        return None
    matches = [k for k, cl in candidates if cl == close[0]]
    if len(matches) == 1:
        if emit_event:
            from system_log import log_system_event
            log_system_event("proposal.fuzzy_symbol_resolution", "info",
                f"fuzzy resolved {symbol} to {matches[0]}",
                {"claimed": symbol, "resolved": matches[0]})
        return matches[0]
    return None
