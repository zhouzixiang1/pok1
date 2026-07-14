"""Verification for the strict national TCP typed-policy artifact.

The candidate-owned execution surface is ``policy.py``.  Socket parsing,
legality, hand reconstruction, precomputed domain facts, and wire output are
system-owned.  This module therefore validates exact Python bytes and the
policy import/call graph; it contains no Botzone action decoder or retired
``main.py``/``strategy.py`` compatibility path.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterable

from candidate_sandbox import (
    CandidateSandboxError,
    CandidateSandboxTimeout,
    run_candidate_probe,
)
from gate_execution import GateExecution


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_LINES_PER_FILE = 2000
MAX_LINES_HELPER = 1500
MAX_LINES_HARD_CAP = 2500
LINE_GROWTH_BUDGET = 0.15


POLICY_MODULE = "policy.py"
SYSTEM_PYTHON_MODULES = frozenset({"national_bot.py", "precompute.py"})
EMBEDDED_SELFTEST_MODULES = (POLICY_MODULE,)
POLICY_ABI_FUNCTIONS = frozenset({
    "get_baseline_decision",
    "iter_decisions",
})


_CANDIDATE_SCRIPT_PROBE = r'''
import json
import runpy
import sys

sys.path.insert(0, "/work")
runpy.run_path("/work/" + sys.argv[1], run_name="__main__")
print(json.dumps({
    "schema": "strict-policy-selftest-v1",
    "ok": True,
    "module": sys.argv[1],
}, ensure_ascii=False))
'''


_CANDIDATE_IMPORT_PROBE = r'''
import importlib
import json
import sys
import traceback

sys.path.insert(0, "/work")
modules = sys.argv[1:]
for module_name in modules:
    try:
        importlib.import_module(module_name)
    except BaseException as exc:
        print(json.dumps({
            "schema": "strict-policy-import-v1",
            "ok": False,
            "module": module_name,
            "exception": exc.__class__.__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
print(json.dumps({
    "schema": "strict-policy-import-v1",
    "ok": True,
    "modules": modules,
}, ensure_ascii=False))
'''


def _count_file_lines(path: str | Path) -> int:
    with open(path, encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def _compact_process_output(stdout: str, stderr: str, max_chars: int = 1600) -> str:
    text = "\n".join(
        part.strip()
        for part in (str(stdout or ""), str(stderr or ""))
        if str(part or "").strip()
    )
    return text if len(text) <= max_chars else text[-max_chars:]


def _has_embedded_selftest(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    lowered = text.lower()
    return "__main__" in text and ("self-test" in lowered or "selftest" in lowered)


def run_bot_embedded_self_tests_execution(
    bot_dir: str | Path,
    timeout: float = 20.0,
) -> GateExecution:
    """Run an explicitly declared ``policy.py`` self-test in isolation.

    Absence of an embedded test is valid because the authoritative decision
    fixtures are system-owned.  No system socket entrypoint is executed here.
    """

    root = Path(bot_dir)
    candidate_errors: list[str] = []
    infrastructure_errors: list[str] = []
    executed: list[str] = []
    for module_name in EMBEDDED_SELFTEST_MODULES:
        path = root / module_name
        if not path.is_file() or not _has_embedded_selftest(path):
            continue
        executed.append(module_name)
        try:
            proc = run_candidate_probe(
                root,
                _CANDIDATE_SCRIPT_PROBE,
                args=(module_name,),
                timeout=timeout,
            )
        except CandidateSandboxTimeout as exc:
            candidate_errors.append(
                f"{module_name}: timeout after {timeout:.0f}s: {exc}"
            )
            continue
        except CandidateSandboxError as exc:
            infrastructure_errors.append(
                f"{module_name}: mandatory sandbox unavailable: {str(exc)[:300]}"
            )
            continue
        except Exception as exc:  # trusted runner failure
            infrastructure_errors.append(
                f"{module_name}: runner {type(exc).__name__}: {str(exc)[:300]}"
            )
            continue
        lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        try:
            receipt = json.loads(lines[-1]) if lines else None
        except (TypeError, json.JSONDecodeError):
            receipt = None
        if not (
            proc.returncode == 0
            and proc.trusted_completion
            and isinstance(receipt, dict)
            and receipt.get("schema") == "strict-policy-selftest-v1"
            and receipt.get("ok") is True
            and receipt.get("module") == module_name
        ):
            candidate_errors.append(
                f"{module_name}: no trusted self-test completion: "
                + _compact_process_output(proc.stdout, proc.stderr)
            )

    identity = {
        "contract": "strict-policy-selftest-v1",
        "timeout": timeout,
        "modules": list(EMBEDDED_SELFTEST_MODULES),
        "executed": executed,
    }
    if infrastructure_errors:
        return GateExecution.infrastructure(
            "embedded_selftest_runner",
            "embedded_selftest",
            infrastructure_errors,
            identity=identity,
        )
    if candidate_errors:
        return GateExecution.candidate_failure(
            "embedded_selftests",
            "embedded_selftest",
            candidate_errors,
            identity=identity,
        )
    return GateExecution.passed(
        "embedded_selftests",
        "embedded_selftest",
        identity=identity,
    )


def run_bot_embedded_self_tests(bot_dir: str | Path, timeout: float = 20.0) -> list[str]:
    return run_bot_embedded_self_tests_execution(bot_dir, timeout).issues


def _python_paths(directory: str | Path, target_files: Iterable[str] | None = None) -> list[Path]:
    root = Path(directory)
    if target_files is not None:
        paths = []
        for relative in target_files:
            path = Path(relative)
            path = path if path.is_absolute() else root / path
            if path.is_file() and path.suffix == ".py":
                paths.append(path)
        return sorted(set(paths))
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _unreachable_statement_lines(body: list[ast.stmt]) -> list[int]:
    lines: list[int] = []
    terminated = False
    for statement in body:
        if terminated:
            lines.append(int(getattr(statement, "lineno", 0) or 0))
            continue
        if isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            terminated = True
        for child_body in (
            getattr(statement, "body", None),
            getattr(statement, "orelse", None),
            getattr(statement, "finalbody", None),
        ):
            if isinstance(child_body, list):
                lines.extend(_unreachable_statement_lines(child_body))
    return lines


def _detect_dead_code_ast(
    directory: str | Path,
    target_files: Iterable[str] | None = None,
) -> list[str]:
    """Reject incomplete stubs and syntactically unreachable statements."""

    errors: list[str] = []
    root = Path(directory)
    for path in _python_paths(root, target_files):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                meaningful = [item for item in node.body if not isinstance(item, ast.Expr) or not isinstance(item.value, ast.Constant) or not isinstance(item.value.value, str)]
                if len(meaningful) == 1 and isinstance(meaningful[0], ast.Pass):
                    if not node.name.startswith("__"):
                        errors.append(
                            f"{relative}:L{node.lineno}: empty function stub {node.name}"
                        )
        for line in _unreachable_statement_lines(tree.body):
            if line:
                errors.append(f"{relative}:L{line}: unreachable statement")
    return sorted(set(errors))


def _top_level_defs(path: Path) -> dict[str, tuple[int, int]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return {}
    return {
        node.name: (
            int(node.lineno),
            int(getattr(node, "end_lineno", node.lineno)),
        )
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _reachability_exempt(name: str) -> bool:
    return (
        name in POLICY_ABI_FUNCTIONS
        or name.startswith("__")
        or name.startswith("_self_test")
    )


def detect_new_function_reachability_warnings(
    source_dir: str | Path,
    next_dir: str | Path,
    changed_files: Iterable[str] | None = None,
) -> list[str]:
    """Ensure each new policy helper is referenced outside its own body."""

    source_root = Path(source_dir)
    next_root = Path(next_dir)
    files = {
        Path(item).name
        for item in (changed_files or (POLICY_MODULE,))
        if str(item).endswith(".py")
    }
    if POLICY_MODULE not in files:
        return []
    source_defs = _top_level_defs(source_root / POLICY_MODULE)
    next_path = next_root / POLICY_MODULE
    next_defs = _top_level_defs(next_path)
    new_defs = {
        name: span
        for name, span in next_defs.items()
        if name not in source_defs and not _reachability_exempt(name)
    }
    if not new_defs:
        return []
    try:
        tree = ast.parse(next_path.read_text(encoding="utf-8"), filename=str(next_path))
    except (OSError, UnicodeError, SyntaxError):
        return []
    references = {name: 0 for name in new_defs}
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        if name not in references:
            continue
        start, end = new_defs[name]
        line = int(getattr(node, "lineno", 0) or 0)
        if start <= line <= end:
            continue
        references[name] += 1
    return [
        f"policy.py:L{new_defs[name][0]}: new helper {name!r} has no "
        "reference from the typed policy dispatch"
        for name in sorted(new_defs)
        if references[name] == 0
    ]


def verify_code(
    directory: str | Path,
    target_files: Iterable[str] | None = None,
) -> list[str]:
    """Compile candidate Python bytes and run generic dead-code checks."""

    errors: list[str] = []
    for path in _python_paths(directory, target_files):
        try:
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        except (OSError, SyntaxError, ValueError, TypeError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
    errors.extend(_detect_dead_code_ast(directory, target_files))
    return errors


def _production_import_modules(directory: str | Path) -> list[str]:
    root = Path(directory)
    return [
        name
        for name in ("precompute", "policy")
        if (root / f"{name}.py").is_file()
    ]


def run_import_contract_test(
    directory: str | Path,
    modules: Iterable[str] | None = None,
    timeout: float = 20.0,
) -> list[dict]:
    """Import the policy and its system precompute dependency in isolation."""

    root = Path(directory).absolute()
    if not root.is_dir():
        return [{
            "module": "<bot_dir>",
            "exception": "FileNotFoundError",
            "message": f"bot directory not found: {root}",
            "traceback": "",
        }]
    selected = list(modules or _production_import_modules(root))
    if selected != ["precompute", "policy"]:
        return [{
            "module": ",".join(selected) or "<modules>",
            "exception": "StrictPolicyImportSetMismatch",
            "message": "strict artifact must import exactly precompute,policy",
            "traceback": "",
        }]
    try:
        proc = run_candidate_probe(
            root,
            _CANDIDATE_IMPORT_PROBE,
            args=selected,
            timeout=timeout,
        )
    except CandidateSandboxTimeout:
        return [{
            "module": ",".join(selected),
            "exception": "TimeoutExpired",
            "message": f"strict policy import timed out after {timeout}s",
            "traceback": "",
        }]
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    try:
        receipt = json.loads(lines[-1]) if lines else None
    except (TypeError, json.JSONDecodeError):
        receipt = None
    if (
        proc.returncode == 0
        and proc.trusted_completion
        and isinstance(receipt, dict)
        and receipt.get("schema") == "strict-policy-import-v1"
        and receipt.get("ok") is True
        and receipt.get("modules") == selected
    ):
        return []
    for line in reversed((proc.stderr or "").splitlines()):
        try:
            value = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return [value]
    return [{
        "module": ",".join(selected),
        "exception": f"ProcessExit{proc.returncode}",
        "message": _compact_process_output(proc.stdout, proc.stderr),
        "traceback": (proc.stderr or "")[-1200:],
    }]


def _get_adaptive_limit(
    filename: str,
    base_limit: int,
    source_dir: str | Path | None = None,
) -> int:
    if source_dir is None:
        return base_limit
    source_path = Path(source_dir) / filename
    if not source_path.is_file():
        return base_limit
    source_lines = _count_file_lines(source_path)
    if source_lines <= base_limit:
        return min(
            MAX_LINES_HARD_CAP,
            max(base_limit, int(source_lines * (1 + LINE_GROWTH_BUDGET))),
        )
    return min(source_lines, MAX_LINES_HARD_CAP)


def check_code_size(
    directory: str | Path,
    max_lines_per_file: int | None = None,
    source_dir: str | Path | None = None,
) -> tuple[int, list[tuple[str, int, int]]]:
    """Apply policy-only LOC growth; system modules receive the hard cap."""

    root = Path(directory)
    total = 0
    oversized: list[tuple[str, int, int]] = []
    for path in _python_paths(root):
        relative = path.relative_to(root).as_posix()
        if "backup" in path.name:
            continue
        lines = _count_file_lines(path)
        total += lines
        if max_lines_per_file is not None:
            limit = int(max_lines_per_file)
        elif relative in SYSTEM_PYTHON_MODULES:
            limit = MAX_LINES_HARD_CAP
        elif relative == POLICY_MODULE:
            limit = _get_adaptive_limit(relative, MAX_LINES_PER_FILE, source_dir)
        else:
            limit = _get_adaptive_limit(relative, MAX_LINES_HELPER, source_dir)
        if lines > limit:
            oversized.append((relative, lines, limit))
    return total, oversized


def run_smoke_test(directory: str | Path) -> list[str]:
    """Retired API: active smoke is the direct raw-TCP artifact runner."""

    del directory
    return ["retired_non_tcp_smoke_api"]


def run_decision_test_details(directory: str | Path, extra_scenarios=None) -> dict:
    """Retired API: active fixtures consume the typed decision context."""

    del directory, extra_scenarios
    return {
        "pass_rate": 0.0,
        "passed": 0,
        "total": 0,
        "critical_passed": 0,
        "critical_total": 0,
        "critical_failures": [{"id": "retired_api"}],
        "failures": [{"id": "retired_api", "severity": "critical"}],
        "scenarios": [],
    }


def run_national_protocol_tests(*, native_tcp_mode: bool = False) -> list[str]:
    """Run the sole official-EXE-aligned raw TCP platform shard."""

    if not native_tcp_mode:
        return ["non_native_protocol_test_mode_retired"]
    test_path = PROJECT_ROOT / "sever" / "tests" / "test_national_platform_alignment.py"
    if not test_path.is_file():
        return [f"national alignment tests missing: {test_path.name}"]
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_path), "-q"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"national protocol test runner failed: {type(exc).__name__}: {exc}"]
    if proc.returncode == 0:
        return []
    return [
        _compact_process_output(proc.stdout, proc.stderr, max_chars=3000)
        or f"national protocol tests exited {proc.returncode}"
    ]
