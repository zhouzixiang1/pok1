"""Candidate directory hygiene for in-progress bot generations.

Generation tools create bot directories by copying completed parents. Parent
metadata such as ``.completed`` is authoritative only after ``commit_bot`` has
created the git commit and tag, so it must never leak into an in-progress
candidate. System-owned runtime bytes and manifests are immutable preparation
inputs: hygiene validates them but never repairs or overwrites them.
"""

from __future__ import annotations

import ast
import shutil
import stat
from pathlib import Path
from typing import Any


_TRANSIENT_DIRECTORY_NAMES = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".task_context",
})
_TRANSIENT_FILE_NAMES = frozenset({".completed"})
_TRANSIENT_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})
_FORBIDDEN_RUNTIME_DEPENDENCY_TOKENS = tuple(sorted({
    *_TRANSIENT_DIRECTORY_NAMES,
    *_TRANSIENT_FILE_NAMES,
    *_TRANSIENT_FILE_SUFFIXES,
}))


def cleanup_transient_candidate_artifacts(
    bot_dir: str | Path,
    *,
    include_task_context: bool = True,
) -> list[str]:
    """Safely remove unpublished runtime/control artifacts from a candidate."""
    root = Path(bot_dir)
    if not root.exists():
        return []
    matched: list[Path] = []
    for path in root.rglob("*"):
        relative_parts = path.relative_to(root).parts
        inside_transient_dir = any(
            part in _TRANSIENT_DIRECTORY_NAMES
            and (include_task_context or part != ".task_context")
            for part in relative_parts
        )
        direct_match = (
            path.name in _TRANSIENT_FILE_NAMES
            or path.suffix.lower() in _TRANSIENT_FILE_SUFFIXES
            or (
                path.name in _TRANSIENT_DIRECTORY_NAMES
                and (include_task_context or path.name != ".task_context")
            )
        )
        if not inside_transient_dir and not direct_match:
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(
                "transient candidate artifact is a symlink: "
                + path.relative_to(root).as_posix()
            )
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise RuntimeError(
                "transient candidate artifact is not regular: "
                + path.relative_to(root).as_posix()
            )
        matched.append(path)

    removed = [path.relative_to(root).as_posix() for path in matched]
    for path in sorted(matched, key=lambda item: len(item.parts), reverse=True):
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    return sorted(set(removed))


def forbidden_runtime_dependency_errors(bot_dir: str | Path) -> list[str]:
    """Reject source code that treats excluded cache/control paths as policy."""
    root = Path(bot_dir)
    errors: list[str] = []
    if not root.exists():
        return errors
    for path in sorted(root.rglob("*.py")):
        if any(part in _TRANSIENT_DIRECTORY_NAMES for part in path.relative_to(root).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, (str, bytes)):
                continue
            value = (
                node.value.decode("utf-8", "ignore")
                if isinstance(node.value, bytes)
                else node.value
            )
            for token in _FORBIDDEN_RUNTIME_DEPENDENCY_TOKENS:
                if token in value:
                    errors.append(
                        "forbidden_transient_runtime_dependency:"
                        f"{path.relative_to(root).as_posix()}:{getattr(node, 'lineno', 0)}:{token}"
                    )
    return sorted(set(errors))


def transient_control_artifact_errors(bot_dir: str | Path) -> list[str]:
    """Find compiler-owned context that must never reach certification."""
    root = Path(bot_dir)
    errors: list[str] = []
    if not root.exists():
        return errors
    try:
        for path in root.rglob(".task_context"):
            errors.append(
                "transient_control_artifact_present:"
                + path.relative_to(root).as_posix()
            )
    except OSError as exc:
        errors.append(
            f"transient_control_artifact_scan_error:{type(exc).__name__}:{str(exc)[:160]}"
        )
    return errors


def sanitize_candidate_dir(
    bot_dir: str | Path,
    *,
    require_native_tcp: bool = False,
) -> dict[str, Any]:
    """Remove transient metadata and validate the immutable native artifact.

    Returns a small audit payload that callers can include in logs/tests.
    A missing or stale system runtime is an integrity failure.  Only the
    system preparation/materialization owner may create those bytes; later
    stages must never make a broken candidate appear valid by rewriting it.
    """

    root = Path(bot_dir)
    result: dict[str, Any] = {
        "bot_dir": str(root),
        "completed_removed": False,
        "native_entry": None,
        "native_entry_refreshed": False,
        "native_entry_contract_errors": [],
    }

    sentinel = root / ".completed"
    if sentinel.exists():
        sentinel.unlink()
        result["completed_removed"] = True

    if require_native_tcp:
        from national_native import check_native_contract

        entry_path = root / "national_bot.py"
        contract_errors = check_native_contract(
            root,
            require_current_stream_decoder=True,
            require_current_decision_runtime=True,
        )
        result["native_entry_contract_errors"] = contract_errors[:20]
        if contract_errors:
            raise RuntimeError(
                "candidate native artifact failed immutable contract: "
                + "; ".join(contract_errors[:8])
            )
        result["native_entry"] = entry_path.name

    return result
