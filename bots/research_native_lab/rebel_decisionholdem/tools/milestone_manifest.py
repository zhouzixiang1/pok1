"""Build and verify a complete route-A milestone content/evidence snapshot."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from ..common_runtime.evaluation import exploitability, nash_conv
from ..common_runtime.leduc import all_infosets as leduc_infosets
from ..common_runtime.leduc import ordered_deals as leduc_deals
from ..common_runtime.leduc import uniform_strategy as leduc_uniform_strategy
from ..common_runtime.leduc_evaluation import exploitability as leduc_exploitability
from ..decisionholdem_like.a2_runtime import SparseBlueprint
from ..decisionholdem_like.blueprint import build_sparse_blueprint_payload
from ..decisionholdem_like.leduc_linear_cfr import LeducLinearCFR
from ..decisionholdem_like.linear_cfr import LinearCFR
from ..decisionholdem_like.native_entry import NATIONAL_STREAM_DECODER_VERSION
from ..decisionholdem_like.resolving import CoinTossResolveGame
from ..rebel_like.toy_loop import run_toy_selfplay

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_RELATIVE_PATH = Path("manifests/milestone_m0_m3.json")
MANIFEST_PATH = PACKAGE_ROOT / MANIFEST_RELATIVE_PATH
MANIFEST_SCHEMA = "route-a-milestone-manifest-v3"
MANIFEST_STAGE = "M0-M3 route-A small-game gate; M4 projection prototype incomplete"
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
    leduc_config = json.loads(
        (root / "configs/leduc_gate.json").read_text(encoding="utf-8")
    )
    leduc_solver = LeducLinearCFR()
    leduc_solver.train(int(leduc_config["iterations"]))
    leduc_profile = leduc_solver.average_strategy()
    blueprint = SparseBlueprint(build_sparse_blueprint_payload(leduc_solver))
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
        "leduc_physical_deals": len(leduc_deals()),
        "leduc_infosets": len(leduc_infosets()),
        "leduc_lcfr_iterations": leduc_solver.iterations_completed,
        "leduc_uniform_exploitability": leduc_exploitability(
            leduc_uniform_strategy()
        ),
        "leduc_trained_exploitability": leduc_exploitability(leduc_profile),
        "leduc_checkpoint_sha256": leduc_solver.checkpoint_digest(),
        "a2_projection_policy_rows": len(blueprint.policies),
        "a2_projection_sha256": blueprint.digest,
        "a2_projection_claim": "m4-prototype-only-not-hunl-blueprint",
        "native_stream_decoder_version": NATIONAL_STREAM_DECODER_VERSION,
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
    fixed.update(
        {
            "schema": MANIFEST_SCHEMA,
            "stage": MANIFEST_STAGE,
            "m4_blueprint_complete": False,
        }
    )
    return fixed | build_dynamic_snapshot(path.parents[1])


def verify_manifest(path: Path = MANIFEST_PATH) -> list[str]:
    recorded = load_manifest(path)
    actual = build_dynamic_snapshot(path.parents[1])
    errors: list[str] = []
    if recorded.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"manifest schema must be {MANIFEST_SCHEMA}")
    if recorded.get("stage") != MANIFEST_STAGE:
        errors.append(f"manifest stage must be {MANIFEST_STAGE}")
    if recorded.get("large_training_started") is not False:
        errors.append("M3 manifest must deny large training")
    if recorded.get("hunl_bot_claimed") is not False:
        errors.append("M3 manifest must deny a HUNL bot claim")
    if recorded.get("m4_blueprint_complete") is not False:
        errors.append("M3 manifest must deny M4 blueprint completion")
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
