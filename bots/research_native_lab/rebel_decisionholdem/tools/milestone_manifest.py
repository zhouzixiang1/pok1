"""Build and verify a complete route-A milestone content/evidence snapshot."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from ..common_runtime.evaluation import exploitability, nash_conv
from ..decisionholdem_like.linear_cfr import LinearCFR
from ..decisionholdem_like.resolving import CoinTossResolveGame
from ..rebel_like.toy_loop import run_toy_selfplay

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_RELATIVE_PATH = Path("manifests/milestone_m0_m3.json")
MANIFEST_PATH = PACKAGE_ROOT / MANIFEST_RELATIVE_PATH
MANIFEST_SCHEMA = "route-a-milestone-manifest-v2"
IGNORED_DIRECTORIES = frozenset(
    {"__pycache__", ".pytest_cache", "checkpoints", "data", "results"}
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def iter_snapshot_files(root: Path = PACKAGE_ROOT) -> tuple[Path, ...]:
    """Return every non-generated regular file except the self-referential manifest."""

    files: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRECTORIES for part in relative.parts):
            continue
        if relative == MANIFEST_RELATIVE_PATH or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(f"snapshot tree must not contain symlinks: {relative}")
        if path.is_file():
            files.append(path)
    return tuple(sorted(files, key=lambda path: path.relative_to(root).as_posix()))


def build_tree_snapshot(root: Path = PACKAGE_ROOT) -> dict[str, object]:
    files = {
        path.relative_to(root).as_posix(): _sha256_file(path)
        for path in iter_snapshot_files(root)
    }
    return {
        "algorithm": "sha256-canonical-file-map-v1",
        "excluded_self": MANIFEST_RELATIVE_PATH.as_posix(),
        "ignored_directories": sorted(IGNORED_DIRECTORIES),
        "file_count": len(files),
        "tree_sha256": _sha256_bytes(_canonical_bytes(files)),
        "files": files,
    }


def count_test_functions(root: Path = PACKAGE_ROOT) -> int:
    count = 0
    for path in sorted((root / "tests").glob("test_*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        count += sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
            for node in ast.walk(module)
        )
    return count


def build_validation_snapshot(root: Path = PACKAGE_ROOT) -> dict[str, object]:
    config = json.loads(
        (root / "configs/small_game_gate.json").read_text(encoding="utf-8")
    )
    iterations = int(config["a2"]["iterations"])
    solver = LinearCFR()
    solver.train(iterations)
    profile = solver.average_strategy()
    coin_toss = CoinTossResolveGame(
        alternative_payoffs=tuple(config["a2"]["coin_toss_alternative_payoffs"])
    )
    a1_trace = run_toy_selfplay(
        deal=tuple(config["a1"]["deal"]), seed=int(config["a1"]["seed"])
    )
    return {
        "test_function_count": count_test_functions(root),
        "pytest_command": (
            "python -m pytest "
            "bots/research_native_lab/rebel_decisionholdem/tests -q"
        ),
        "a1_trace_sha256": _sha256_bytes(_canonical_bytes(a1_trace)),
        "lcfr_iterations": solver.iterations_completed,
        "kuhn_nash_conv": nash_conv(profile),
        "kuhn_exploitability": exploitability(profile),
        "checkpoint_sha256": solver.checkpoint_digest(),
        "plain_resolve_exploitability_delta": (
            coin_toss.plain_resolve().exploitability_delta
        ),
        "safe_resolve_exploitability_delta": (
            coin_toss.safe_resolve().exploitability_delta
        ),
    }


def build_dynamic_snapshot(root: Path = PACKAGE_ROOT) -> dict[str, object]:
    return {
        "validation": build_validation_snapshot(root),
        "tree": build_tree_snapshot(root),
    }


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def render_current_manifest(path: Path = MANIFEST_PATH) -> dict[str, object]:
    existing = load_manifest(path)
    fixed = {
        key: value
        for key, value in existing.items()
        if key not in {"file_sha256", "validation", "tree"}
    }
    return fixed | build_dynamic_snapshot(path.parents[1])


def verify_manifest(path: Path = MANIFEST_PATH) -> list[str]:
    recorded = load_manifest(path)
    actual = build_dynamic_snapshot(path.parents[1])
    errors: list[str] = []
    if recorded.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"manifest schema must be {MANIFEST_SCHEMA}")
    if recorded.get("large_training_started") is not False:
        errors.append("first-milestone manifest must deny large training")
    if recorded.get("hunl_bot_claimed") is not False:
        errors.append("first-milestone manifest must deny a HUNL bot claim")
    for field in ("validation", "tree"):
        if recorded.get(field) != actual[field]:
            errors.append(f"{field} snapshot differs from current package")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render",
        action="store_true",
        help="print a regenerated manifest instead of verifying the committed one",
    )
    args = parser.parse_args()
    if args.render:
        print(json.dumps(render_current_manifest(), indent=2, sort_keys=True))
        return 0
    errors = verify_manifest()
    print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
