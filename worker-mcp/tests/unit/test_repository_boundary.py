from __future__ import annotations

import ast
from pathlib import Path
import subprocess


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _tracked_active_files() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "ls-files",
            "-z",
            "web",
            "sever",
            "bots",
            "scripts",
        ],
        check=True,
        capture_output=True,
    )
    return [
        REPOSITORY_ROOT / value.decode("utf-8")
        for value in result.stdout.split(b"\0")
        if value
    ]


def test_poker_runtime_never_imports_or_starts_codex_worker_mcp() -> None:
    violations: list[str] = []
    for path in _tracked_active_files():
        if path.suffix == ".py":
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name.startswith("worker_mcp") for alias in node.names):
                        violations.append(str(path.relative_to(REPOSITORY_ROOT)))
                elif isinstance(node, ast.ImportFrom):
                    if (node.module or "").startswith("worker_mcp"):
                        violations.append(str(path.relative_to(REPOSITORY_ROOT)))
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if "worker_mcp.server" in node.value or "pok-worker-mcp" in node.value:
                        violations.append(str(path.relative_to(REPOSITORY_ROOT)))
        elif path.suffix in {".js", ".jsx", ".ts", ".tsx", ".sh"}:
            source = path.read_text(encoding="utf-8")
            if "worker_mcp" in source or "pok-worker-mcp" in source:
                violations.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert not violations, (
        "poker runtime/control-plane code must not import, start, supervise, or "
        f"call the Codex-only Worker MCP: {sorted(set(violations))}"
    )
