"""Worker and candidate editable-boundary checks.

The LLM worker already receives an allowed write directory, but that only
prevents writes outside the candidate bot directory. These checks enforce the
stronger contract that a worker may only change its declared target files and
explicitly allowed helper files.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot_namespace import bot_relpath


@dataclass
class BoundaryAuditResult:
    passed: bool
    changed_files: list[str] = field(default_factory=list)
    allowed_files: list[str] = field(default_factory=list)
    ignored_changed_files: list[str] = field(default_factory=list)
    violation_files: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def to_gate_metrics(self) -> dict[str, Any]:
        return {
            "changed_files": self.changed_files,
            "allowed_files": self.allowed_files,
            "ignored_changed_files": self.ignored_changed_files,
            "violation_files": self.violation_files,
            "violation_count": len(self.violations),
        }


def _normalize_rel(path: str | Path, next_v: int | None = None) -> str | None:
    text = str(path).strip()
    if not text:
        return None
    text = text.replace("\\", "/")
    marker = bot_relpath(next_v) + "/" if next_v is not None else ""
    if marker and marker in text:
        text = text.split(marker, 1)[1]
    if text.startswith("./"):
        text = text[2:]
    if text.startswith("/"):
        return None
    parts = Path(text).parts
    if not parts or any(part in ("..", "") for part in parts):
        return None
    return Path(*parts).as_posix()


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def snapshot_python_files(root: Path) -> dict[str, bytes]:
    """Capture all Python files under root as relpath -> bytes."""
    root = Path(root)
    if not root.exists():
        return {}
    snap: dict[str, bytes] = {}
    for path in _iter_py_files(root):
        rel = path.relative_to(root).as_posix()
        snap[rel] = path.read_bytes()
    return snap


def diff_snapshot(root: Path, before: dict[str, bytes]) -> list[str]:
    """Return changed, created, or deleted Python relpaths vs a snapshot."""
    root = Path(root)
    after = snapshot_python_files(root)
    changed = set(before) ^ set(after)
    for rel in set(before) & set(after):
        if before[rel] != after[rel]:
            changed.add(rel)
    return sorted(changed)


def hash_changed_files(root: Path, changed_files: list[str]) -> str:
    """Stable diff-ish hash for changed Python files."""
    h = hashlib.sha256()
    root = Path(root)
    for rel in sorted(changed_files):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        path = root / rel
        if path.exists():
            h.update(path.read_bytes())
        else:
            h.update(b"<deleted>")
        h.update(b"\0")
    return h.hexdigest()


def _is_tuner_task(task: dict[str, Any]) -> bool:
    role = str(task.get("role", "")).lower()
    return "tuner" in role or "hyperparameter" in role


def allowed_files_for_task(task: dict[str, Any], next_v: int | None = None) -> list[str]:
    allowed: set[str] = set()
    for key in ("target_files", "files_allowed"):
        for item in task.get(key, []) or []:
            rel = _normalize_rel(item, next_v)
            if rel:
                allowed.add(rel)
    if _is_tuner_task(task):
        allowed = {rel for rel in allowed if rel == "constants.py"}
    return sorted(allowed)


def audit_worker_boundary(
    root: Path,
    task: dict[str, Any],
    before_snapshot: dict[str, bytes],
    *,
    next_v: int | None = None,
    ignored_changed_files: list[str] | set[str] | tuple[str, ...] | None = None,
) -> BoundaryAuditResult:
    changed = diff_snapshot(root, before_snapshot)
    allowed = allowed_files_for_task(task, next_v)
    allowed_set = set(allowed)
    ignored_set = {
        rel for rel in (
            _normalize_rel(item, next_v) for item in (ignored_changed_files or [])
        )
        if rel
    }
    ignored = sorted(rel for rel in changed if rel in ignored_set)
    violation_files = sorted(
        rel for rel in changed
        if rel not in allowed_set and rel not in ignored_set
    )
    violations = [
        f"{rel}: changed outside declared target_files/files_allowed"
        for rel in violation_files
    ]
    return BoundaryAuditResult(
        passed=not violations,
        changed_files=changed,
        allowed_files=allowed,
        ignored_changed_files=ignored,
        violation_files=violation_files,
        violations=violations,
    )


def restore_python_files(root: Path, before_snapshot: dict[str, bytes], changed_files: list[str]) -> None:
    """Restore changed Python files to a prior snapshot."""
    root = Path(root)
    for rel in changed_files:
        path = root / rel
        if rel in before_snapshot:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(before_snapshot[rel])
        elif path.exists():
            path.unlink()


def audit_changed_files_against_plan(
    changed_files: list[str],
    tasks: list[dict[str, Any]],
    *,
    next_v: int | None = None,
) -> BoundaryAuditResult:
    """Audit final candidate diff against the master plan's declared files."""
    allowed: set[str] = set()
    for task in tasks or []:
        allowed.update(allowed_files_for_task(task, next_v))
    normalized_changed = []
    for rel in changed_files:
        normalized = _normalize_rel(rel, next_v)
        if normalized:
            normalized_changed.append(normalized)
    violations = [
        f"{rel}: changed outside master plan target_files/files_allowed"
        for rel in normalized_changed
        if rel not in allowed
    ]
    return BoundaryAuditResult(
        passed=not violations,
        changed_files=sorted(normalized_changed),
        allowed_files=sorted(allowed),
        violations=violations,
    )
