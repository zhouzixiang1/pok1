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


CAPABILITY_SCHEMA_VERSION = 2
NATIONAL_CAPABILITY_DETECTOR_VERSION = "national-policy-static-v1"
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
)

ADVISORY_CHECKS = (
    "incremental_refinement_protocol",
    "budget_scaled_refinement",
    "precompute_lookup_path",
    "precompute_runtime_influence",
    "incremental_opponent_model",
    "terminal_response_adaptation",
    "showdown_range_adaptation",
    "donk_line_reachability",
    "delayed_probe_line_reachability",
)

_FORBIDDEN_IMPORT_ROOTS = frozenset({
    "asyncio",
    "http",
    "multiprocessing",
    "pathlib",
    "requests",
    "socket",
    "subprocess",
    "urllib",
})
_FORBIDDEN_CALL_LEAVES = frozenset({
    "Popen",
    "__import__",
    "compile",
    "eval",
    "exec",
    "open",
    "run",
    "socket",
    "system",
})
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


def _policy_static_evidence(tree: ast.Module | None) -> dict[str, Any]:
    if tree is None:
        return {
            "imports": set(),
            "forbidden_imports": [],
            "forbidden_calls": [],
            "loaded_names": set(),
            "string_literals": set(),
            "integer_return_locations": [],
            "raise_dict_locations": [],
            "kind_literals": set(),
            "large_literal_locations": [],
            "context_fields": set(),
        }
    imports: set[str] = set()
    forbidden_imports: list[str] = []
    forbidden_calls: list[str] = []
    loaded_names: set[str] = set()
    string_literals: set[str] = set()
    integer_return_locations: list[str] = []
    raise_dict_locations: list[str] = []
    kind_literals: set[str] = set()
    large_literal_locations: list[str] = []
    context_fields: set[str] = set()

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
        elif isinstance(node, ast.Call):
            leaf = _call_leaf(node)
            if leaf in _FORBIDDEN_CALL_LEAVES:
                forbidden_calls.append(f"policy.py:{node.lineno}:{leaf}")
            # context.get("field")
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "context"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                context_fields.add(node.args[0].value)
        elif isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id == "context":
                key = node.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    context_fields.add(key.value)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loaded_names.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            string_literals.add(node.value)
        elif isinstance(node, ast.Return):
            if isinstance(node.value, ast.Constant) and (
                isinstance(node.value.value, int) and not isinstance(node.value.value, bool)
            ):
                integer_return_locations.append(f"policy.py:{node.lineno}")
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            size = len(node.elts) if hasattr(node, "elts") else len(node.keys)
            if size > 4096:
                large_literal_locations.append(f"policy.py:{node.lineno}:{size}")

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

    return {
        "imports": imports,
        "forbidden_imports": forbidden_imports,
        "forbidden_calls": forbidden_calls,
        "loaded_names": loaded_names,
        "string_literals": string_literals,
        "integer_return_locations": integer_return_locations,
        "raise_dict_locations": raise_dict_locations,
        "kind_literals": kind_literals,
        "large_literal_locations": large_literal_locations,
        "context_fields": context_fields,
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
        | static["string_literals"].intersection({"call", "check"})
    )
    typed_ok = bool(
        baseline_ok
        and refinement_ok
        and not static["integer_return_locations"]
        and not forbidden_kind_literals
    )
    if "raise" in static["kind_literals"] and not static["raise_dict_locations"]:
        typed_ok = False

    candidate_io_ok = not static["forbidden_imports"] and not static["forbidden_calls"]
    history_ok = not static["loaded_names"].intersection(_HISTORY_RECONSTRUCTION_NAMES)
    table_ok = not static["large_literal_locations"]
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
            policy_tree is not None and not retired_present and not extra_python,
            guidance=(
                "Provide policy.py only for candidate decisions; remove retired active ABI files."
            ),
            summary=(
                "policy.py is the sole active candidate decision module"
                if policy_tree is not None and not retired_present and not extra_python
                else (
                    "policy_missing_or_forbidden_python_present:"
                    f"retired={retired_present}:extra={extra_python}"
                )
            ),
            locations=["policy.py", *retired_present, *extra_python],
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
            baseline_ok and candidate_io_ok and table_ok,
            guidance="Keep get_baseline_decision synchronous, I/O-free, and free of oversized construction.",
            summary=(
                "bounded synchronous policy baseline"
                if baseline_ok and candidate_io_ok and table_ok
                else "baseline is missing, blocking, or performs forbidden work"
            ),
            locations=["policy.py:get_baseline_decision"],
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
                *static["raise_dict_locations"],
            ],
            details={
                "observed_kind_literals": sorted(static["kind_literals"]),
                "forbidden_kind_literals": forbidden_kind_literals,
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
            guidance="Use bounded decision_context.history/opponent snapshots; never rebuild protocol history.",
            summary="no retired history reconstruction identifiers" if history_ok else "retired history identifiers loaded",
            locations=[f"policy.py:{name}" for name in sorted(static["loaded_names"].intersection(_HISTORY_RECONSTRUCTION_NAMES))],
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
    ]

    advisory_states = {
        "incremental_refinement_protocol": refinement_ok,
        "budget_scaled_refinement": refinement_ok and "deadline" in static["loaded_names"],
        "precompute_lookup_path": "precompute" in static["imports"],
        "precompute_runtime_influence": "precompute" in static["imports"],
        "incremental_opponent_model": "opponent" in context_fields,
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
