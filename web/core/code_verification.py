"""Code verification tools: compile check, file size, smoke test, decision tests."""

import os
import json
import shutil
import subprocess
import sys

from evolution_infra import (
    CORE_DIR, PROJECT_ROOT, REFERENCE_DIR, RESULTS_DIR,
    MAX_LINES_PER_FILE, MAX_LINES_HELPER, MAX_LINES_HARD_CAP,
    LINE_GROWTH_BUDGET, CORE_STRATEGY_FILES, _COPY_IGNORE,
    get_bot_dir,
)


def _count_file_lines(path):
    """Count lines in a file."""
    with open(path) as fh:
        return sum(1 for _ in fh)


def _get_adaptive_limit(filename, base_limit, source_dir=None):
    """Compute adaptive line limit for a file.

    If source_dir is provided and the source file exists, allow growth from
    the source file's size. The limit is:
        max(base_limit, source_lines * (1 + LINE_GROWTH_BUDGET))
    capped at MAX_LINES_HARD_CAP.

    Without source_dir, returns base_limit (backward compatible).
    """
    if source_dir is None:
        return base_limit

    source_path = os.path.join(source_dir, filename)
    if not os.path.exists(source_path):
        return base_limit

    source_lines = _count_file_lines(source_path)
    adaptive = max(base_limit, int(source_lines * (1 + LINE_GROWTH_BUDGET)))
    return min(adaptive, MAX_LINES_HARD_CAP)


def _detect_dead_code_ast(directory, target_files=None):
    """Detect dead code patterns via AST analysis.

    Catches:
    1. Functions with only 'pass' body (empty stubs from incomplete workers)
    2. Code after return/raise/break/continue (unreachable)
    """
    import ast as _ast
    errors = []
    target_paths = []
    if target_files:
        for tf in target_files:
            path = os.path.join(directory, tf) if not os.path.isabs(tf) else tf
            if os.path.exists(path) and path.endswith(".py"):
                target_paths.append(path)
    else:
        for root, _, files in os.walk(directory):
            for f in files:
                if f.endswith(".py"):
                    target_paths.append(os.path.join(root, f))

    for path in target_paths:
        try:
            with open(path) as fh:
                source = fh.read()
            tree = _ast.parse(source, filename=path)
            fname = os.path.basename(path)
            for node in _ast.walk(tree):
                # 1. Functions with only 'pass' (empty stubs) — skip dunder methods
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    body = node.body
                    if (len(body) == 1
                        and isinstance(body[0], _ast.Pass)
                        and not (node.name.startswith("__") and node.name.endswith("__"))):
                        errors.append(
                            f"{fname}: function '{node.name}' at line {node.lineno} "
                            f"contains only 'pass' (empty stub from incomplete worker)"
                        )
                # 2. Unreachable code after return/raise/break/continue
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    stmts = node.body
                    for idx, stmt in enumerate(stmts):
                        if isinstance(stmt, (_ast.Return, _ast.Raise, _ast.Break, _ast.Continue)):
                            # Check for statements after this one (ignore docstrings and ellipsis)
                            for later in stmts[idx + 1:]:
                                if isinstance(later, _ast.Expr) and isinstance(later.value, (_ast.Constant,)):
                                    continue  # docstrings/string constants are ok
                                errors.append(
                                    f"{fname}: unreachable code after {type(stmt).__name__.lower()} "
                                    f"at line {stmt.lineno} in '{node.name}'"
                                )
                                break
                            break  # only flag first dead-code trigger
        except SyntaxError:
            pass  # py_compile already catches syntax errors
    return errors


def _is_reachability_exempt_function(name):
    """Return True for local self-test helpers that are not runtime behavior."""
    return (
        name.startswith("test_")
        or name.startswith("_test_")
        or name.startswith("verify_")
        or name.startswith("_verify_")
    )


def _top_level_function_defs(path):
    """Return {function_name: (lineno, end_lineno)} for top-level functions."""
    import ast as _ast
    try:
        with open(path) as fh:
            source = fh.read()
        tree = _ast.parse(source, filename=path)
    except Exception:
        return {}
    defs = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            name = node.name
            if name.startswith("__") and name.endswith("__"):
                continue
            if _is_reachability_exempt_function(name):
                continue
            defs[name] = (getattr(node, "lineno", 0), getattr(node, "end_lineno", getattr(node, "lineno", 0)))
    return defs


def detect_new_function_reachability_warnings(source_dir, next_dir, changed_files=None):
    """Flag newly-added top-level functions that are never referenced.

    This targets the common evolution failure where a worker appends a plausible
    helper in e.g. postflop.py but never wires it into strategy.py. Import aliases
    alone do not count as reachability; the function must have a non-import Name
    or Attribute reference outside its own body.
    """
    import ast as _ast

    source_dir = os.path.abspath(str(source_dir))
    next_dir = os.path.abspath(str(next_dir))
    rel_files = [p for p in (changed_files or []) if str(p).endswith(".py")]
    if not rel_files:
        rel_files = []
        for root, _, files in os.walk(next_dir):
            for f in files:
                if f.endswith(".py"):
                    rel_files.append(os.path.relpath(os.path.join(root, f), next_dir))

    new_defs = {}
    for rel in rel_files:
        dst = os.path.join(next_dir, rel)
        if not os.path.exists(dst):
            continue
        src = os.path.join(source_dir, rel)
        src_defs = _top_level_function_defs(src) if os.path.exists(src) else {}
        dst_defs = _top_level_function_defs(dst)
        for name, span in dst_defs.items():
            if name not in src_defs:
                new_defs[name] = {"file": rel, "start": span[0], "end": span[1]}

    if not new_defs:
        return []

    refs = {name: 0 for name in new_defs}
    for root, _, files in os.walk(next_dir):
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, next_dir)
            try:
                with open(path) as fh:
                    tree = _ast.parse(fh.read(), filename=path)
            except Exception:
                continue
            for node in _ast.walk(tree):
                name = None
                lineno = getattr(node, "lineno", None)
                if isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Load):
                    name = node.id
                elif isinstance(node, _ast.Attribute):
                    name = node.attr
                if name not in refs or lineno is None:
                    continue
                defn = new_defs[name]
                if rel == defn["file"] and defn["start"] <= lineno <= defn["end"]:
                    continue
                refs[name] += 1

    warnings = []
    for name, meta in sorted(new_defs.items(), key=lambda item: (item[1]["file"], item[1]["start"], item[0])):
        if refs.get(name, 0) == 0:
            warnings.append(
                f"{meta['file']}:L{meta['start']}: reachability — new top-level "
                f"function '{name}' has no non-import references outside its own body; "
                "likely dead code. Wire it into the strategy dispatch path or remove it."
            )
    return warnings


# A3 (evolution-plan-refresh-jun21): detector call-site names that are placement-
# shadow suspects — if their call appears AFTER a `to_call >= my_chips` early-return,
# they are structurally unreachable for stack-covering all-ins (the INERTNESS root
# cause that recurred v138-v143: guards placed at strategy.py:1041 after the
# allin-cover early-return at :1018).
import re as _re
_PLACEMENT_SHADOW_DETECTOR_RE = _re.compile(
    r"(_river_.*_guard|_spr_.*|sb_open_.*|bb_vs_.*|_vulnerable_.*|_river_value_.*)"
)


def _is_to_call_ge_my_chips(test_node):
    """Match the `to_call >= my_chips` (or `to_call > my_chips`) comparison test."""
    import ast as _ast
    if not isinstance(test_node, _ast.Compare):
        return False
    left = test_node.left
    if not (isinstance(left, _ast.Name) and left.id == "to_call"):
        return False
    if not any(isinstance(op, (_ast.GtE, _ast.Gt)) for op in test_node.ops):
        return False
    if not test_node.comparators:
        return False
    right = test_node.comparators[0]
    return isinstance(right, _ast.Name) and right.id == "my_chips"


def detect_placement_shadow_warnings(directory, target_files=None):
    """AST-detect detector call-sites placed AFTER a `to_call >= my_chips`
    early-return — structurally unreachable for stack-covering all-ins.

    This is the placement-shadow INERTNESS root cause: v138 `_river_stackoff_guard`
    was wired at strategy.py:1041 inside the `if to_call > 0:` block, which sits
    AFTER the `if to_call >= my_chips:` early-return at :1018 — so for a true
    stack-covering all-in the guard never runs. Returns advisory warnings
    (non-blocking; the fix is to RELOCATE the call-site before the early-return,
    not to re-tune thresholds).
    """
    warnings = []
    strat_path = os.path.join(directory, "strategy.py")
    if not os.path.exists(strat_path):
        return warnings
    try:
        import ast as _ast
        with open(strat_path) as fh:
            source = fh.read()
        tree = _ast.parse(source, filename=strat_path)
    except Exception:
        return warnings

    # Build a parent map so we can find each call's nearest enclosing to_call If.
    parent = {}
    for node in _ast.walk(tree):
        for child in _ast.iter_child_nodes(node):
            parent[child] = node

    def _nearest_enclosing_to_call_if(call_node):
        """Walk ancestors; return the nearest If whose test references to_call,
        or None. Returns (if_node, kind) where kind in {'gt0', 'eq0', 'ge_chips', 'other'}.
        Skips If nodes whose test CONTAINS the call (those are the If the call
        defines/conditions, not the branch it's nested in)."""
        cur = parent.get(call_node)
        while cur is not None:
            if isinstance(cur, _ast.If):
                # Skip the If whose test is/contains this call (the call is the
                # condition, not nested in the body).
                if any(c is call_node for c in _ast.walk(cur.test)):
                    cur = parent.get(cur)
                    continue
                test = cur.test
                refs_to_call = any(
                    isinstance(n, _ast.Name) and n.id == "to_call"
                    for n in _ast.walk(test)
                )
                if refs_to_call:
                    kind = "other"
                    if _is_to_call_ge_my_chips(test):
                        kind = "ge_chips"
                    elif isinstance(test, _ast.Compare):
                        for cmp in test.comparators:
                            if isinstance(cmp, _ast.Constant) and isinstance(cmp.value, (int, float)):
                                if cmp.value == 0:
                                    if any(isinstance(op, _ast.Gt) for op in test.ops):
                                        kind = "gt0"
                                    elif any(isinstance(op, _ast.Eq) for op in test.ops):
                                        kind = "eq0"
                    return (cur, kind)
            cur = parent.get(cur)
        return (None, "none")

    for fn_node in _ast.walk(tree):
        if not isinstance(fn_node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        # Find `to_call>=my_chips` If nodes in this function whose body returns.
        early_return_lines = []
        for sub in _ast.walk(fn_node):
            if isinstance(sub, _ast.If) and _is_to_call_ge_my_chips(sub.test):
                if any(isinstance(n, _ast.Return) for n in _ast.walk(sub)):
                    early_return_lines.append(sub.lineno)
        if not early_return_lines:
            continue
        earliest = min(early_return_lines)
        # Flag detector call-sites after the earliest early-return. Precision: a call
        # in a `to_call > 0` block is a TRUE shadow (meant to guard bets but cannot
        # cover to_call>=my_chips all-ins). A call in `to_call == 0` offense is NOT
        # shadowed (different to_call range) — downgraded to info.
        seen = set()
        for sub in _ast.walk(fn_node):
            if not (isinstance(sub, _ast.Call) and isinstance(sub.func, _ast.Name)
                    and _PLACEMENT_SHADOW_DETECTOR_RE.match(sub.func.id)
                    and sub.lineno > earliest):
                continue
            key = (sub.func.id, sub.lineno)
            if key in seen:
                continue
            seen.add(key)
            _enc, kind = _nearest_enclosing_to_call_if(sub)
            if kind == "eq0":
                # to_call==0 offense (open-bet/bluff) — correctly NOT covering all-ins.
                continue
            severity = "TRUE SHADOW" if kind == "gt0" else "review"
            warnings.append(
                "strategy.py:L{ln}: placement_shadow ({sev}) — detector '{fn}' call is "
                "AFTER to_call>=my_chips early-return at L{er} (enclosing block: {kind}). "
                "{note}".format(
                    ln=sub.lineno, sev=severity, fn=sub.func.id, er=earliest,
                    kind=kind,
                    note=("Cannot cover to_call>=my_chips stack-covering all-ins — "
                          "RELOCATE call-site BEFORE the early-return, do NOT re-tune."
                          ) if kind == "gt0" else
                         ("Verify this call isn't intended for the all-in path."
                          ))
            )
    return warnings


import ast  # M6 telemetry-fidelity AST gate (b057ead follow-up, evolution-plan-refresh-jun21)

# M6 (b057ead follow-up): telemetry-fidelity AST gate — BLOCKING.
# Multi-arm margin/delta detectors (returned value built from >1 arm: a
# standard/unconditional arm PLUS bucket-gated arms) whose stderr.write telemetry
# is nested inside a bucket/signal If-gate yield SUB-ARM-ONLY telemetry → daemon
# grep delta_adj!=0 returns a false-INERT verdict (v154 99.98%-delta=+0 artifact
# → v155 Master misread the LIVE framework as dead, listed it in do_not_touch).
_MULTI_ARM_DETECTOR_RE = _re.compile(
    r"(postflop_call_margin|sb_open_opp_sizing|bb_vs_.*_sizing|_street_fold_exploit|"
    r"_delayed_calldown_bluff|_river_value_extraction|_vulnerable_made_protection|"
    r"street_fold_boost|bb_vs_raise|bb_vs_limp)"
)

_ACCUMULATOR_HINTS = frozenset(("margin", "adjustment", "adjust", "delta", "boost", "adj"))

_BUCKET_SIGNAL_KW = frozenset((
    "bucket", "tendency", "sizing", "confidence", "samples",
    "fold", "vpip", "pfr", "threebet", "limp_rate", "limp",
    "overbettor", "underbettor", "standard", "postflop_aggr",
    "open_response", "fold_to_", "calldown",
))


def _build_parent_map(tree):
    """Build a child→parent dict for an AST tree."""
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    return parent


def _is_accumulating_assign(node, accum_names):
    """True if `node` is an AugAssign (+=/-=) or self-update Assign (delta = delta + ...)
    targeting an accumulator variable."""
    if isinstance(node, ast.AugAssign):
        tgt = node.target
        if isinstance(tgt, ast.Name) and tgt.id in accum_names:
            return True
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        tgt = node.targets[0]
        if isinstance(tgt, ast.Name) and tgt.id in accum_names:
            read_names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            if tgt.id in read_names:
                return True
    return False


def _stderr_call_label(node):
    """Return a label string if `node` is a telemetry call, else None.
    Recognizes: sys.stderr.write(...), print(..., file=sys.stderr)."""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Attribute):
        val = f.value
        if (isinstance(f.attr, str) and f.attr in ("write", "writelines")
                and isinstance(val, ast.Attribute) and val.attr == "stderr"
                and isinstance(val.value, ast.Name) and val.value.id == "sys"):
            return "sys.stderr.write"
    if isinstance(f, ast.Name) and f.id == "print":
        for kw in node.keywords:
            if kw.arg == "file":
                v = kw.value
                if (isinstance(v, ast.Attribute) and v.attr == "stderr"
                        and isinstance(v.value, ast.Name) and v.value.id == "sys"):
                    return "print(file=stderr)"
    return None


def _is_bucket_signal_test(if_node):
    """True if the If's test references an opp-signal/bucket/confidence variable."""
    test_names = {n.id for n in ast.walk(if_node.test) if isinstance(n, ast.Name)}
    if not test_names:
        return False
    for name in test_names:
        lname = (name or "").lower()
        if any(kw in lname for kw in _BUCKET_SIGNAL_KW):
            return True
    return False


def _nearest_enclosing_if(call_node, parent, fn):
    """Walk ancestors of `call_node`; return the nearest If node (not traversing past `fn`)."""
    cur = parent.get(call_node)
    while cur is not None and cur is not fn:
        if isinstance(cur, ast.If):
            return cur
        cur = parent.get(cur)
    return None


def _has_function_scope_telemetry(stderr_calls, parent, fn):
    """True if at least one stderr call is NOT nested inside any If/Try/With/For/While
    within the function — meaning telemetry is hoisted to function scope."""
    for call, _label in stderr_calls:
        cur = parent.get(call)
        nested_in_compound = False
        while cur is not None and cur is not fn:
            if isinstance(cur, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                nested_in_compound = True
                break
            cur = parent.get(cur)
        if not nested_in_compound:
            return True
    return False


def detect_telemetry_fidelity_warnings(directory, target_files=None):
    """AST-detect multi-arm margin/delta detectors whose telemetry is nested inside a
    bucket/signal If-gate (sub-arm-scoped) instead of hoisted to function scope.

    This is the v154 telemetry-fidelity root cause: ``postflop_call_margin`` is
    behaviorally LIVE via its standard arm A (unconditional hand-property-gated
    ``margin += ...``), but its ``SIZING_MARGIN_ADJ`` stderr.write sits inside the
    ``if sizing is not None and samples>=8 and confidence>=0.30`` block (arm B
    tendency) — daemon grep ``delta_adj!=0`` yields 99.98% +0 → false-INERT verdict
    → next Master misreads the LIVE framework as dead.

    Mirrors ``detect_placement_shadow_warnings``. Returns list[str] of warnings.
    A warning is emitted only when ALL THREE hold (precision triple):
      (1) function is multi-arm  (>=1 bucket-gated accumulator write AND
          >=1 standard-arm accumulator write or a top-level accumulator write);
      (2) a stderr.write/telemetry call exists whose nearest enclosing If is a
          bucket/signal gate (telemetry NOT hoisted to function scope);
      (3) no self-test fixture (function invoked in __main__ AND an assert present)
          exists — a no-op call without an assert does NOT count (refinement #3).
    Single-arm detectors and telemetry already hoisted to FunctionDef do NOT trigger.
    """
    warnings = []
    target_paths = []
    if target_files:
        for tf in target_files:
            path = os.path.join(directory, tf) if not os.path.isabs(tf) else tf
            if os.path.exists(path) and path.endswith(".py"):
                target_paths.append(path)
    else:
        if not os.path.isdir(directory):
            return warnings
        for root, _, files in os.walk(directory):
            for f in files:
                if f.endswith(".py"):
                    target_paths.append(os.path.join(root, f))

    for path in target_paths:
        try:
            with open(path) as fh:
                source = fh.read()
            tree = ast.parse(source, filename=path)
        except Exception:
            continue
        fname = os.path.basename(path)
        parent = _build_parent_map(tree)

        # __main__ self-test fixture: detector invoked AND an assert present.
        # A no-op call without an assert does NOT count (worker cannot trivially
        # defeat the gate by adding postflop_call_margin(...) with no check).
        main_call_names = set()
        main_has_assert = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
                t = node.test
                if (isinstance(t.left, ast.Name) and t.left.id == "__name__"
                        and any(isinstance(c, ast.Constant) and c.value == "__main__"
                                for c in t.comparators)):
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                            main_call_names.add(sub.func.id)
                        if isinstance(sub, ast.Assert):
                            main_has_assert = True

        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Discover accumulator vars in this fn (AugAssign targets +
            # self-update Assigns delta = delta + ...).
            accum_names = set()
            for sub in ast.walk(fn):
                if isinstance(sub, ast.AugAssign) and isinstance(sub.target, ast.Name):
                    n = sub.target.id
                    if (n in _ACCUMULATOR_HINTS
                            or any(h in n.lower() for h in _ACCUMULATOR_HINTS)):
                        accum_names.add(sub.target.id)
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1:
                    tgt = sub.targets[0]
                    if isinstance(tgt, ast.Name) and tgt.id in _ACCUMULATOR_HINTS:
                        read = {nn.id for nn in ast.walk(sub.value) if isinstance(nn, ast.Name)}
                        if tgt.id in read:
                            accum_names.add(tgt.id)
            if not accum_names:
                continue

            # Classify accumulator writes:
            #   top_level  : direct child of FunctionDef body (unconditional arm)
            #   bucket_arm : nested in an If whose test refs opp-signal/bucket var
            #   standard_arm: nested in an If whose test is hand/spot-property only
            top_level = 0
            bucket_arm = 0
            standard_arm = 0
            for sub in ast.walk(fn):
                if not _is_accumulating_assign(sub, accum_names):
                    continue
                par = parent.get(sub)
                if par is fn:
                    top_level += 1
                    continue
                gate = _nearest_enclosing_if(sub, parent, fn)
                if gate is not None and _is_bucket_signal_test(gate):
                    bucket_arm += 1
                else:
                    standard_arm += 1

            has_bucket_arm = bucket_arm >= 1
            has_standard_or_top = (standard_arm >= 1) or (top_level >= 1)
            is_multi_arm = has_bucket_arm and has_standard_or_top
            if not is_multi_arm:
                continue  # single-arm bucketed detector OR pure standard arm

            # Collect telemetry calls.
            stderr_calls = []
            for sub in ast.walk(fn):
                lbl = _stderr_call_label(sub)
                if lbl:
                    stderr_calls.append((sub, lbl))
            if not stderr_calls:
                continue  # multi-arm but no telemetry -> not a fidelity issue

            # If ANY telemetry is hoisted to function scope, assume the author
            # instrumented the TOTAL value correctly -> do not flag.
            if _has_function_scope_telemetry(stderr_calls, parent, fn):
                continue

            fixture_present = fn.name in main_call_names and main_has_assert

            for call, label in stderr_calls:
                gate_if = _nearest_enclosing_if(call, parent, fn)
                if gate_if is None:
                    continue  # not nested in an If at all
                if not _is_bucket_signal_test(gate_if):
                    continue  # nested in a non-bucket If — not the failure mode
                sev = "BLOCKING" if not fixture_present else "advisory(fixture_present)"
                warnings.append(
                    "%s:L%d: telemetry_fidelity (%s) — multi-arm detector '%s' "
                    "(bucket_gated_acc_writes=%d, standard_arm_acc_writes=%d, top_level=%d) "
                    "has %s at L%d nested inside bucket/signal gate (If at L%d) — NOT hoisted "
                    "to function scope. Telemetry covers only the bucket arm; daemon grep "
                    "yields a false-INERT verdict (v154 99.98%%-delta=+0 artifact). FIX: hoist "
                    "sys.stderr.write to function scope (same indent as `return`) printing TOTAL "
                    "margin_milli=round(return*1000) with reason=standard_arm|tendency_fired|"
                    "conf_gate; add a __main__ self-test calling '%s' with live-pool defaults "
                    "(tendency='standard', size_bucket='medium', confidence=0.5) asserting "
                    "margin_milli==round(return*1000)."
                    % (fname, fn.lineno, sev, fn.name, bucket_arm,
                       standard_arm, top_level, label, call.lineno, gate_if.lineno, fn.name)
                )
    return warnings


def verify_code(directory, target_files=None):
    """Verify Python files compile. When target_files is given, only check those
    files instead of walking the entire directory — avoids false compile errors
    from other workers mid-edit in parallel mode."""
    errors = []
    if target_files:
        for tf in target_files:
            path = os.path.join(directory, tf) if not os.path.isabs(tf) else tf
            if os.path.exists(path) and path.endswith(".py"):
                proc = subprocess.run([sys.executable, "-m", "py_compile", path], capture_output=True, text=True)
                if proc.returncode != 0:
                    errors.append(proc.stderr.strip())
    else:
        # Original behavior unchanged - walk entire directory
        for root, _, files in os.walk(directory):
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(root, f)
                    proc = subprocess.run([sys.executable, "-m", "py_compile", path], capture_output=True, text=True)
                    if proc.returncode != 0:
                        errors.append(proc.stderr.strip())

    # AST-based dead code detection (advisory, non-blocking on failure)
    try:
        _ast_errors = _detect_dead_code_ast(directory, target_files)
        errors.extend(_ast_errors)
    except Exception:
        pass  # AST analysis failures must not block the pipeline

    # fix-3: inline placement-shadow TRUE-SHADOW check (eliminates dual-path).
    # Previously only run_quality_gates called detect_placement_shadow_warnings;
    # verify_code did not. This means a bot could pass verify_code but still
    # have TRUE-SHADOW issues that run_quality_gates would catch -- a confusing
    # dual-path. Now verify_code also flags TRUE-SHADOW so the gate is unified.
    try:
        _shadow = detect_placement_shadow_warnings(directory, target_files)
        for w in _shadow:
            if 'TRUE SHADOW' in w:
                errors.append(w)
    except Exception:
        pass  # placement-shadow analysis failures must not block the pipeline

    return errors


def _production_import_modules(directory):
    """Return bot modules whose import-time contracts must hold.

    `py_compile` proves syntax only; it does not execute `from module import name`
    bindings. Importing the production modules in a fresh subprocess catches the
    class of crossover failures where one parent adds a symbol dependency that the
    merged child never defines.
    """
    preferred = ["main", "strategy", "postflop", "opponent", "state"]
    modules = []
    for name in preferred:
        if os.path.exists(os.path.join(directory, f"{name}.py")):
            modules.append(name)
    if "main" not in modules and os.path.exists(os.path.join(directory, "main.py")):
        modules.insert(0, "main")
    return modules


def run_import_contract_test(directory, modules=None, timeout=20):
    """Import production bot modules in a clean subprocess.

    Returns a list of structured error dictionaries. An empty list means the
    runtime import contract is intact.
    """
    directory = os.path.abspath(str(directory))
    if not os.path.isdir(directory):
        return [{
            "module": "<bot_dir>",
            "exception": "FileNotFoundError",
            "message": f"bot directory not found: {directory}",
            "traceback": "",
        }]

    modules = list(modules or _production_import_modules(directory))
    if not modules:
        return [{
            "module": "<modules>",
            "exception": "FileNotFoundError",
            "message": f"no importable production modules found in {directory}",
            "traceback": "",
        }]

    probe = r"""
import importlib
import json
import os
import sys
import traceback

bot_dir = os.path.abspath(sys.argv[1])
modules = sys.argv[2:]
sys.path.insert(0, bot_dir)
for module_name in modules:
    try:
        importlib.import_module(module_name)
    except BaseException as exc:
        print(json.dumps({
            "module": module_name,
            "exception": exc.__class__.__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
print(json.dumps({"ok": True, "modules": modules}, ensure_ascii=False))
"""
    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-c", probe, directory, *modules],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [{
            "module": ",".join(modules),
            "exception": "TimeoutExpired",
            "message": f"runtime import contract timed out after {timeout}s",
            "traceback": "",
        }]

    if proc.returncode == 0:
        return []

    stderr = (proc.stderr or "").strip()
    for line in reversed(stderr.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                return [data]
        except Exception:
            continue
    return [{
        "module": ",".join(modules),
        "exception": f"ProcessExit{proc.returncode}",
        "message": stderr or (proc.stdout or "").strip() or "runtime import contract failed",
        "traceback": stderr,
    }]


def check_code_size(directory, max_lines_per_file=None, source_dir=None):
    """Check single-file LOC limits (excluding backup files). Returns (total, oversized_files).

    Uses tiered limits: CORE_STRATEGY_FILES (strategy.py, postflop.py) get
    MAX_LINES_PER_FILE (2000), all others get MAX_LINES_HELPER (1500).

    When source_dir is provided, applies adaptive limits based on the source
    bot's file sizes plus a growth budget (LINE_GROWTH_BUDGET = 15%).
    All limits are capped at MAX_LINES_HARD_CAP (2500).
    """
    oversized_files = []
    total = 0
    for root, _, files in os.walk(directory):
        for f in files:
            if f.endswith(".py") and "backup" not in f:
                path = os.path.join(root, f)
                lines = _count_file_lines(path)
                total += lines

                # Compute limit: base → adaptive (if source_dir) → override (if max_lines_per_file)
                base_limit = MAX_LINES_PER_FILE if f in CORE_STRATEGY_FILES else MAX_LINES_HELPER
                limit = _get_adaptive_limit(f, base_limit, source_dir)

                # Explicit override wins (backward compatibility)
                if max_lines_per_file is not None:
                    limit = max_lines_per_file

                if lines > limit:
                    oversized_files.append((f, lines, limit))
    return total, oversized_files


def run_smoke_test(directory):
    main_path = os.path.join(directory, "main.py")
    if not os.path.exists(main_path):
        return ["main.py not found!"]
    try:
        proc = subprocess.run(
            [sys.executable, str(CORE_DIR / "smoke_tester.py"), main_path],
            capture_output=True, text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        return ["smoke test timed out after 90s"]
    if proc.returncode != 0:
        return [proc.stderr.strip() or proc.stdout.strip()]
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if "Smoke test passed successfully." in output:
        output = _strip_benign_smoke_cleanup_noise(output)
    failure_tokens = (
        "Traceback (most recent call last)",
        "ImportError",
        "ModuleNotFoundError",
        "NameError",
        "BrokenPipeError",
        "Exception ignored",
        "Bot process exited",
    )
    if any(token in output for token in failure_tokens):
        return [f"smoke test emitted failure output despite exit 0: {output[-2000:]}"]
    return []


def _strip_benign_smoke_cleanup_noise(output):
    """Remove battle subprocess cleanup noise after a successful smoke test.

    mirror_battle can emit CPython finalizer BrokenPipeError tracebacks while
    cleaning up file handles for child subprocesses even when the smoke test has
    already completed and exited 0. Keep all other traceback output intact so a
    real bot/runtime exception still fails the gate.
    """
    lines = output.splitlines()
    cleaned = []
    in_cleanup_block = False
    for line in lines:
        starts_cleanup = (
            "Exception ignored while finalizing file <_io.TextIOWrapper" in line
            or (
                "while finalizing file <_io.TextIOWrapper" in line
                and "Exception ignored" in line
            )
        )
        if starts_cleanup:
            in_cleanup_block = True
            continue
        if in_cleanup_block:
            if "BrokenPipeError: [Errno 32] Broken pipe" in line:
                in_cleanup_block = False
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def run_decision_test_details(directory, extra_scenarios=None):
    """Run standard decision scenarios. Returns detailed gate results."""
    main_path = os.path.join(directory, "main.py")
    if not os.path.exists(main_path):
        return {
            "pass_rate": 0.0,
            "passed": 0,
            "total": 0,
            "critical_passed": 0,
            "critical_total": 0,
            "critical_failures": [{"id": "main.py", "details": "main.py not found"}],
            "failures": [{"id": "main.py", "severity": "critical", "details": "main.py not found"}],
            "scenarios": [],
        }
    from decision_tester import run_decision_tests_detail as _run_detail
    return _run_detail(main_path, verbose=False, extra_scenarios=extra_scenarios)


def run_national_protocol_tests():
    """Run the national TCP platform/adapter alignment tests.

    The evolution loop still evaluates JSON-subprocess bots, but those bots are
    deployed to the national TCP platform through sever/bot_adapter.py. This
    gate keeps protocol parsing, validator behavior, runout handling, and THP
    output aligned with the national documents.
    """
    test_path = PROJECT_ROOT / "sever" / "tests" / "test_national_alignment.py"
    if not test_path.exists():
        return ["sever national alignment tests not found"]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_path), "-q"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return [output or f"pytest exited with {proc.returncode}"]
    return []


def seed_initial_bots(ui):
    """Seed claude_v1 through claude_v6 with bot1 through bot6 if they don't exist."""
    seeded = False
    for i in range(1, 7):
        target_dir = get_bot_dir(i)
        source_dir = REFERENCE_DIR / f"bot{i}"
        if not target_dir.exists() and source_dir.exists():
            ui.log_history(f"Seeding claude_v{i} from reference bot{i}...", "info")
            shutil.copytree(source_dir, target_dir, ignore=_COPY_IGNORE)
            # Apply known fixes to seeded bot
            from fix_injection import apply_known_fixes, log_fix_application
            applied, skipped = apply_known_fixes(target_dir)
            if applied or skipped:
                log_fix_application(applied, skipped, target_dir, i)
            (target_dir / ".completed").touch()
            seeded = True
    return seeded
