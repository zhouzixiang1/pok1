"""Static capability contract for national-native bot architecture.

This gate is intentionally separate from protocol legality.  The validator and
official EXE decide whether a bot may act on the wire; this module tells the
evolution pipeline whether a candidate is using the national-native runtime
model well: bounded decision work, reusable precomputation, persistent
match-memory, and clean diagnostics.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any


def _read_python_sources(bot_dir: str | Path) -> dict[str, str]:
    root = Path(bot_dir)
    sources: dict[str, str] = {}
    for path in sorted(root.glob("*.py")):
        try:
            sources[path.name] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return sources


def _contains(text: str, *patterns: str) -> bool:
    lower = text.lower()
    return any(pattern.lower() in lower for pattern in patterns)


def _regex(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) is not None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _name_text(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name_text(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _decision_function_nodes(sources: dict[str, str]) -> list[tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Return likely per-action decision functions from bot sources."""
    nodes: list[tuple[str, str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    decision_name_markers = (
        "get_action",
        "decide",
        "decision",
        "choose_action",
        "select_action",
        "act",
    )
    for filename, text in sources.items():
        try:
            tree = ast.parse(text, filename=filename)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name.lower()
            if name == "action" or any(marker in name for marker in decision_name_markers):
                nodes.append((filename, node.name, node))
    return nodes


class _DecisionPathVisitor(ast.NodeVisitor):
    """Collect runtime-architecture risks inside likely decision functions."""

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

    def __init__(self) -> None:
        self.external_io: list[str] = []
        self.history_scans: list[str] = []
        self.large_runtime_tables: list[str] = []

    def visit_Call(self, node: ast.Call) -> Any:
        name = _call_name(node.func)
        attr = name.rsplit(".", 1)[-1] if name else ""
        if name in self._EXTERNAL_IO_CALLS or attr in self._FILE_METHODS:
            self.external_io.append(f"L{getattr(node, 'lineno', '?')}:{name or attr}")
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> Any:
        iter_name = _name_text(node.iter).lower()
        if any(marker in iter_name for marker in ("requests", "responses", "history", "_requests", "_history")):
            self.history_scans.append(f"L{getattr(node, 'lineno', '?')}:{iter_name}")
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> Any:
        iter_name = _name_text(node.iter).lower()
        if any(marker in iter_name for marker in ("requests", "responses", "history", "_requests", "_history")):
            self.history_scans.append(f"L{getattr(node, 'lineno', '?')}:{iter_name}")
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> Any:
        if len(node.keys) >= 30:
            self.large_runtime_tables.append(f"L{getattr(node, 'lineno', '?')}:dict[{len(node.keys)}]")
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> Any:
        if len(node.elts) >= 60:
            self.large_runtime_tables.append(f"L{getattr(node, 'lineno', '?')}:list[{len(node.elts)}]")
        self.generic_visit(node)

    def visit_Set(self, node: ast.Set) -> Any:
        if len(node.elts) >= 60:
            self.large_runtime_tables.append(f"L{getattr(node, 'lineno', '?')}:set[{len(node.elts)}]")
        self.generic_visit(node)

    def _comprehension_range_size(self, generators: list[ast.comprehension]) -> int | None:
        if not generators:
            return None
        call = generators[0].iter
        if not isinstance(call, ast.Call) or _call_name(call.func) != "range":
            return None
        if not call.args:
            return None
        try:
            values = [
                arg.value for arg in call.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, int)
            ]
        except Exception:
            return None
        if len(values) != len(call.args):
            return None
        if len(values) == 1:
            return max(0, values[0])
        if len(values) >= 2:
            start, stop = values[0], values[1]
            step = values[2] if len(values) >= 3 and values[2] else 1
            return max(0, (stop - start + (step - 1)) // step)
        return None

    def visit_DictComp(self, node: ast.DictComp) -> Any:
        size = self._comprehension_range_size(node.generators)
        if size is not None and size >= 60:
            self.large_runtime_tables.append(f"L{getattr(node, 'lineno', '?')}:dictcomp_range[{size}]")
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> Any:
        size = self._comprehension_range_size(node.generators)
        if size is not None and size >= 60:
            self.large_runtime_tables.append(f"L{getattr(node, 'lineno', '?')}:listcomp_range[{size}]")
        self.generic_visit(node)

    def visit_SetComp(self, node: ast.SetComp) -> Any:
        size = self._comprehension_range_size(node.generators)
        if size is not None and size >= 60:
            self.large_runtime_tables.append(f"L{getattr(node, 'lineno', '?')}:setcomp_range[{size}]")
        self.generic_visit(node)


def _decision_path_risks(sources: dict[str, str]) -> dict[str, Any]:
    external_io: list[str] = []
    history_scans: list[str] = []
    large_runtime_tables: list[str] = []
    decision_functions: list[str] = []
    for filename, function_name, node in _decision_function_nodes(sources):
        decision_functions.append(f"{filename}:{function_name}")
        visitor = _DecisionPathVisitor()
        visitor.visit(node)
        external_io.extend(f"{filename}:{function_name}:{item}" for item in visitor.external_io)
        history_scans.extend(f"{filename}:{function_name}:{item}" for item in visitor.history_scans)
        large_runtime_tables.extend(
            f"{filename}:{function_name}:{item}" for item in visitor.large_runtime_tables
        )
    return {
        "decision_functions": decision_functions,
        "external_io": external_io,
        "history_scans": history_scans,
        "large_runtime_tables": large_runtime_tables,
    }


def _check(name: str, passed: bool, severity: str, evidence: str, guidance: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "severity": severity,
        "evidence": evidence,
        "guidance": guidance,
    }


def evaluate_national_capabilities(bot_dir: str | Path) -> dict[str, Any]:
    """Evaluate architecture-level national-native capabilities.

    Required checks are wire-safety basics that must stay true.  Advisory checks
    are the evolution direction requested for future generations: exploit the
    60-second budget with precomputed/cached facts and persistent match memory
    rather than slow per-action recomputation.
    """
    sources = _read_python_sources(bot_dir)
    joined = "\n".join(sources.values())
    national_bot = sources.get("national_bot.py", "")
    strategy_text = "\n".join(
        text for name, text in sources.items()
        if name in {"strategy.py", "postflop.py", "simulation.py", "opponent.py", "state.py", "constants.py"}
    )
    decision_risks = _decision_path_risks(sources)

    checks = [
        _check(
            "official_safe_wire_send",
            "_send_wire_action" in national_bot and "POK_OFFICIAL_ACTION_DELAY" in national_bot,
            "required",
            "national_bot.py preserves the official action send helper and throttle",
            "Send formal EXE actions only through _send_wire_action and keep POK_OFFICIAL_ACTION_DELAY near 0.30 by default.",
        ),
        _check(
            "clean_diagnostics_channel",
            ("--log" in national_bot or "POK_TRACE_DECISIONS" in national_bot)
            and not _regex(national_bot, r"(?m)^\s*print\s*\("),
            "required",
            "diagnostics are available without stdout pollution",
            "Write communication/decision diagnostics to --log or stderr; never print diagnostics to stdout in the native TCP entry.",
        ),
        _check(
            "decision_time_budget_visible",
            _contains(joined, "time.monotonic", "perf_counter", "elapsed", "duration_ms", "decision_ms")
            and _contains(strategy_text or joined, "max_", "limit", "cap", "samples", "budget", "deadline"),
            "advisory",
            "decision paths expose timing or bounded-work markers",
            "Make per-action work bounded and observable; add deadline-aware fallback before increasing simulation/search.",
        ),
        _check(
            "decision_path_no_external_io",
            not decision_risks["external_io"],
            "advisory",
            "likely per-action decision functions avoid file/network/subprocess I/O",
            "Keep file, network, subprocess, and log-file writes out of get_action/decide paths; collect diagnostics in the TCP layer.",
        ),
        _check(
            "decision_path_no_full_history_scan",
            not decision_risks["history_scans"],
            "advisory",
            "decision paths should consume incremental match summaries instead of rescanning full request/history lists",
            "Update an OpponentTracker or match-state object incrementally on inbound messages; do not rescan full history each action.",
        ),
        _check(
            "decision_path_no_large_runtime_tables",
            not decision_risks["large_runtime_tables"],
            "advisory",
            "large pure lookup tables should be built at module import/startup rather than inside decisions",
            "Move large card/range/texture tables to module-level immutable constants or bounded startup caches.",
        ),
        _check(
            "precompute_lookup_path",
            _contains(strategy_text, "precompute", "lookup", "bucket", "cache", "memo", "table")
            and not _regex(strategy_text, r"def\s+get_action[\s\S]{0,2000}(precompute|build_.*table|lookup\s*=\s*\{)"),
            "advisory",
            "pure poker facts can be reused instead of rebuilt inside get_action",
            "Move pure card/range/texture computations into bounded module/startup lookup tables or immutable caches.",
        ),
        _check(
            "persistent_match_memory",
            _contains(national_bot, "_requests", "_history", "_showdowns")
            and _contains(national_bot, "earnchips", "oppo_hands"),
            "advisory",
            "native client keeps match-level request/history/showdown state",
            "Keep hand state separate from match state; preserve match-level opponent summaries across the 70 hands.",
        ),
        _check(
            "incremental_opponent_model",
            _contains(joined, "opponenttracker", "incremental", "update_opponent", "record_opponent", "match_profile")
            and _contains(strategy_text, "opponent", "opp_"),
            "advisory",
            "opponent model appears incrementally updated and consumed by strategy",
            "Prefer an OpponentTracker-style object over rebuilding the whole model from full request history every action.",
        ),
    ]

    required_failures = [item for item in checks if item["severity"] == "required" and not item["passed"]]
    advisory_warnings = [item for item in checks if item["severity"] == "advisory" and not item["passed"]]
    return {
        "schema_version": 1,
        "bot_dir": str(Path(bot_dir)),
        "ok": not required_failures,
        "required_failures": required_failures,
        "advisory_warnings": advisory_warnings,
        "checks": checks,
        "decision_path_risks": decision_risks,
    }


def national_runtime_feedback_summary(
    bot_dir: str | Path,
    *,
    source_label: str = "source bot",
    max_chars: int = 4000,
) -> str:
    """Return bounded architecture feedback for Master planning prompts.

    This summary is not a protocol verdict.  It is a planning signal that tells
    Master where the current native bot is failing to use the official
    60-second action window well: bounded work, precomputed lookup paths,
    persistent match memory, and clean decision diagnostics.
    """
    result = evaluate_national_capabilities(bot_dir)
    required = result.get("required_failures") or []
    advisory = result.get("advisory_warnings") or []
    checks = result.get("checks") or []
    lines = [
        f"National runtime architecture feedback for {source_label}:",
        "This is a planning signal only; official EXE compliance and native TCP gates remain authoritative for legality.",
    ]
    if required:
        lines.append("Required runtime contract failures:")
        for item in required[:4]:
            lines.append(
                f"- {item.get('name')}: {item.get('guidance')} "
                f"(evidence: {item.get('evidence')})"
            )
    if advisory:
        lines.append("Architecture improvement opportunities:")
        for item in advisory[:6]:
            lines.append(
                f"- {item.get('name')}: {item.get('guidance')} "
                f"(evidence: {item.get('evidence')})"
            )
    else:
        lines.append("No advisory runtime-architecture gaps detected by the static contract.")
    passed = [item for item in checks if item.get("passed")]
    if passed:
        lines.append(
            "Already present: "
            + ", ".join(str(item.get("name")) for item in passed[:8])
        )
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text
