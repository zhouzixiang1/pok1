"""Build and verify the additive Route-A M5a correctness manifest.

The M4 manifest is a historical whole-tree snapshot.  Advancing A1 must not
rewrite that evidence, so this manifest anchors the frozen M4 bytes and binds
only the new M5a surface plus its exact artifact and import audit.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from ..decisionholdem_like.secure_files import (
    canonical_bytes,
    secure_file_map,
    sha256_bytes,
    stable_read_path,
    stable_read_relative,
    stable_selected_file_map,
    strict_json_loads,
)
from ..rebel_like.label_contract import verify_label_artifact_files


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT.parent
MANIFEST_RELATIVE_PATH = Path("manifests/milestone_m5a.json")
MANIFEST_PATH = PACKAGE_ROOT / MANIFEST_RELATIVE_PATH
CONFIG_PATH = PACKAGE_ROOT / "configs/m5a_pbs_label_contract.json"
ARTIFACT_PATH = PACKAGE_ROOT / "artifacts/m5a_exact_label_fixture.json"

MANIFEST_SCHEMA = "route-a1-m5a-additive-manifest-v1"
MANIFEST_STAGE = "M5a PBS and exact-label contract correctness gate only"
BASE_M4_COMMIT = "0e751feea37177b724151ecb77115455dab8467e"
CREATED_AT = "2026-07-14T00:00:00+08:00"

FROZEN_M4_PATHS = (
    "artifacts/hunl_m4_smoke_blueprint.json",
    "evidence/m4_scale_gate.json",
    "evidence/m4_sever_tcp_70h.json",
    "manifests/milestone_m0_m4.json",
    "reports/m4-hunl-blueprint-validation.md",
)
EXPECTED_FROZEN_M4_HASHES = {
    "artifacts/hunl_m4_smoke_blueprint.json": (
        "aa50b89b5b3d9822712c4f6a93a25448437526071aec3f9760c8abcdb4600539"
    ),
    "evidence/m4_scale_gate.json": (
        "b54419b35d2f3148d08d83dcc303121828fcbc6b6d2180b1676689096d7239ed"
    ),
    "evidence/m4_sever_tcp_70h.json": (
        "1e1cceafa466946354e0a58ebf7c921a41f0f607350422fb3ca731e8e0eb42de"
    ),
    "manifests/milestone_m0_m4.json": (
        "ad453b1d444396678d14b0929369f6a589a78aa6df9887a27ebcb8a748bda99e"
    ),
    "reports/m4-hunl-blueprint-validation.md": (
        "9e1d6f43772607d653e979af7be7f843498a62d50816819ffed0e7b7cf7b268a"
    ),
}
M5A_BOUND_PATHS = (
    "README.md",
    "configs/m5a_pbs_label_contract.json",
    "rebel_like/hunl_pbs.py",
    "rebel_like/label_contract.py",
    "reports/m5a-pbs-label-contract-validation.md",
    "tests/test_m5a_hunl_pbs.py",
    "tests/test_m5a_label_contract.py",
    "tests/test_manifest.py",
    "tools/build_m5a_label_fixture.py",
    "tools/m5a_manifest.py",
)
IGNORED_DIRECTORIES = frozenset(
    {"__pycache__", ".pytest_cache", "checkpoints", "data", "results"}
)
_TOP_LEVEL_FIELDS = {
    "artifact",
    "base_m4_commit",
    "claim_boundary",
    "created_at",
    "frozen_m4",
    "import_audit",
    "m5a_files",
    "schema",
    "stage",
    "validation",
}

CLAIM_BOUNDARY = {
    "exact_pbs_and_label_correctness_gate_complete": True,
    "hunl_labels_generated": False,
    "large_training_authorized": False,
    "network_training_started": False,
    "neural_leaf_value_implemented": False,
    "online_search_implemented": False,
    "official_exe_certified": False,
    "strength_claimed": False,
    "submission_bot_claimed": False,
}

VALIDATION = {
    "additional_compatibility_regression": {
        "authority": "additional_only_not_the_national_native_formal_gate",
        "command": (
            "python -m pytest bots/research_native_lab/common_contracts/tests "
            "sever/tests/test_national_platform_alignment.py "
            "sever/tests/test_national_alignment.py -q"
        ),
        "result": "290 passed, 1 skipped in 178.10s",
    },
    "deterministic_artifact_rebuild": {
        "comparison": "byte-identical cmp against a second output path",
        "raw_sha256": (
            "ae4e8eca65d2c99429f0a7f064abfac9f468347903ab3dd131959865c7ff8797"
        ),
        "result": "passed",
    },
    "m5a_targeted": {
        "command": (
            "python -m pytest "
            "bots/research_native_lab/rebel_decisionholdem/tests/"
            "test_m5a_hunl_pbs.py "
            "bots/research_native_lab/rebel_decisionholdem/tests/"
            "test_m5a_label_contract.py -q"
        ),
        "result": "44 passed in 18.89s",
    },
    "national_native_formal": {
        "authority": "national_native_protocol_regression",
        "command": (
            "python -m pytest "
            "sever/tests/test_national_platform_alignment.py -q"
        ),
        "legacy_adapter_shard_counted": False,
        "result": "10 passed in 0.01s",
    },
    "route_a_full_no_cache": {
        "authority": "complete_route_a_source_tree_regression",
        "command": (
            "PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider "
            "bots/research_native_lab/rebel_decisionholdem/tests -q"
        ),
        "collected": 246,
        "result": "246 passed",
    },
}


def _exact_equal(left: object, right: object) -> bool:
    """Compare JSON values without Python's bool/int or int/float aliases."""

    return canonical_bytes(left) == canonical_bytes(right)


def _stable_json(relative: str | Path) -> Any:
    return strict_json_loads(stable_read_relative(PACKAGE_ROOT, Path(relative)))


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    if path != MANIFEST_PATH:
        raise ValueError(f"M5a manifest path must be authoritative: {MANIFEST_PATH}")
    payload = strict_json_loads(stable_read_relative(PACKAGE_ROOT, MANIFEST_RELATIVE_PATH))
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_FIELDS:
        raise ValueError("M5a manifest fields are invalid")
    return payload


def build_frozen_m4_snapshot() -> dict[str, object]:
    files = stable_selected_file_map(PACKAGE_ROOT, FROZEN_M4_PATHS)
    if not _exact_equal(files, EXPECTED_FROZEN_M4_HASHES):
        changed = sorted(
            name
            for name in set(files) | set(EXPECTED_FROZEN_M4_HASHES)
            if files.get(name) != EXPECTED_FROZEN_M4_HASHES.get(name)
        )
        raise ValueError(
            "frozen M4 bytes differ from audited BASE_M4_COMMIT: "
            + ", ".join(changed)
        )
    historical = _stable_json("manifests/milestone_m0_m4.json")
    if not isinstance(historical, dict):
        raise ValueError("historical M4 manifest must be an object")
    tree = historical.get("tree")
    common = historical.get("common_interface")
    if not isinstance(tree, dict) or not isinstance(common, dict):
        raise ValueError("historical M4 manifest has no frozen tree bindings")
    return {
        "algorithm": "sha256-selected-frozen-files-v1",
        "file_count": len(files),
        "files": files,
        "files_sha256": sha256_bytes(canonical_bytes(files)),
        "historical_common_tree_sha256": common.get("package_tree_sha256"),
        "historical_manifest_schema": historical.get("schema"),
        "historical_route_file_count": tree.get("file_count"),
        "historical_route_tree_sha256": tree.get("tree_sha256"),
        "policy": "anchor_historical_bytes_without_rewriting_m4",
    }


def build_artifact_snapshot() -> dict[str, object]:
    raw = stable_read_path(ARTIFACT_PATH)
    payload = strict_json_loads(raw)
    verified = verify_label_artifact_files(
        payload,
        config_path=CONFIG_PATH,
        source_root=SOURCE_ROOT,
    )
    body = verified["body"]
    if not isinstance(body, dict):
        raise ValueError("M5a artifact body must be an object")
    examples = body.get("examples")
    generators = body.get("generator_bindings")
    config = body.get("config")
    if (
        not isinstance(examples, list)
        or not isinstance(generators, dict)
        or not isinstance(config, dict)
    ):
        raise ValueError("M5a artifact summary fields are malformed")
    coverage = config.get("coverage_contract")
    if not isinstance(coverage, dict):
        raise ValueError("M5a artifact has no coverage contract")
    generator_summary: dict[str, object] = {}
    for game in ("kuhn", "leduc"):
        binding = generators.get(game)
        if not isinstance(binding, dict):
            raise ValueError(f"M5a artifact has no {game} generator binding")
        generator_summary[game] = {
            "average_profile_sha256": binding.get("average_profile_sha256"),
            "current_profile_sha256": binding.get("current_profile_sha256"),
            "solver_checkpoint_sha256": binding.get("solver_checkpoint_sha256"),
            "solver_iterations": binding.get("solver_iterations"),
        }
    game_counts = Counter(example.get("game") for example in examples)
    return {
        "body_sha256": verified.get("body_sha256"),
        "bytes": len(raw),
        "config_file_sha256": body.get("config_file_sha256"),
        "coverage_identity_manifest_sha256": coverage.get(
            "identity_manifest_sha256"
        ),
        "example_count": len(examples),
        "example_sha256": body.get("example_sha256"),
        "game_counts": dict(sorted(game_counts.items())),
        "generator_bindings": generator_summary,
        "generator_bindings_sha256": body.get("generator_bindings_sha256"),
        "hunl_combo_registry_sha256": config.get("hunl_combo_registry_sha256"),
        "large_training_authorized": body.get("large_training_authorized"),
        "network_training_started": body.get("network_training_started"),
        "online_search_implemented": body.get("online_search_implemented"),
        "oracle_bundles_sha256": body.get("oracle_bundles_sha256"),
        "raw_sha256": sha256_bytes(raw),
        "schema": verified.get("schema"),
        "source_snapshot_sha256": body.get("source_snapshot_sha256"),
        "split_counts": body.get("split_counts"),
    }


def build_m5a_file_snapshot() -> dict[str, object]:
    files = stable_selected_file_map(PACKAGE_ROOT, M5A_BOUND_PATHS)
    return {
        "algorithm": "sha256-selected-m5a-files-v1",
        "excluded_self": str(MANIFEST_RELATIVE_PATH),
        "file_count": len(files),
        "files": files,
        "files_sha256": sha256_bytes(canonical_bytes(files)),
    }


def _import_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            prefix = "." * node.level
            names.add(prefix + module)
            names.update(
                prefix + (f"{module}.{alias.name}" if module else alias.name)
                for alias in node.names
            )
            names.update(prefix + alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            target = node.func
            dynamic = (
                isinstance(target, ast.Name)
                and target.id in {"__import__", "import_module"}
            ) or (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "importlib"
                and target.attr == "import_module"
            )
            if dynamic and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    names.add(value)
    return names


def _forbidden_import(name: str) -> bool:
    name = name.lstrip(".")
    return (
        name == "engine"
        or name.startswith("engine.")
        or name == "sever.bot_adapter"
        or name.startswith("sever.bot_adapter.")
    )


def _legacy_adapter_import(name: str) -> bool:
    name = name.lstrip(".")
    return name == "sever.bot_adapter" or name.startswith("sever.bot_adapter.")


def _top_level_engine_import(name: str) -> bool:
    name = name.lstrip(".")
    return name == "engine" or name.startswith("engine.")


def build_import_audit() -> dict[str, object]:
    complete = secure_file_map(
        PACKAGE_ROOT,
        ignored_directories=IGNORED_DIRECTORIES,
    )
    python_names = tuple(sorted(name for name in complete if name.endswith(".py")))
    python_files = stable_selected_file_map(PACKAGE_ROOT, python_names)
    if any(complete[name] != digest for name, digest in python_files.items()):
        raise RuntimeError("Route-A Python files changed during import audit")

    imports: dict[str, list[str]] = {}
    test_function_count = 0
    test_python_file_count = 0
    for name in python_names:
        data = stable_read_relative(PACKAGE_ROOT, Path(name))
        tree = ast.parse(data.decode("utf-8"), filename=name)
        names = sorted(_import_names(tree))
        if names:
            imports[name] = names
        if name.startswith("tests/test_"):
            test_python_file_count += 1
            test_function_count += sum(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
                for node in ast.walk(tree)
            )

    forbidden = {
        path: [name for name in names if _forbidden_import(name)]
        for path, names in imports.items()
    }
    forbidden = {path: names for path, names in forbidden.items() if names}
    return {
        "algorithm": "complete-route-python-ast-import-audit-v1",
        "forbidden_imports": forbidden,
        "forbidden_modules": ["engine", "engine.*", "sever.bot_adapter"],
        "legacy_adapter_imported": any(
            _legacy_adapter_import(name)
            for names in imports.values()
            for name in names
        ),
        "python_file_count": len(python_files),
        "python_files_sha256": sha256_bytes(canonical_bytes(python_files)),
        "test_function_count": test_function_count,
        "test_python_file_count": test_python_file_count,
        "top_level_engine_imported": any(
            _top_level_engine_import(name)
            for names in imports.values()
            for name in names
        ),
    }


def build_dynamic_snapshot() -> dict[str, object]:
    return {
        "artifact": build_artifact_snapshot(),
        "frozen_m4": build_frozen_m4_snapshot(),
        "import_audit": build_import_audit(),
        "m5a_files": build_m5a_file_snapshot(),
    }


def render_current_manifest() -> dict[str, object]:
    return {
        "artifact": build_artifact_snapshot(),
        "base_m4_commit": BASE_M4_COMMIT,
        "claim_boundary": CLAIM_BOUNDARY,
        "created_at": CREATED_AT,
        "frozen_m4": build_frozen_m4_snapshot(),
        "import_audit": build_import_audit(),
        "m5a_files": build_m5a_file_snapshot(),
        "schema": MANIFEST_SCHEMA,
        "stage": MANIFEST_STAGE,
        "validation": VALIDATION,
    }


def verify_manifest(path: Path = MANIFEST_PATH) -> list[str]:
    recorded = load_manifest(path)
    actual = build_dynamic_snapshot()
    expected_fixed = {
        "base_m4_commit": BASE_M4_COMMIT,
        "claim_boundary": CLAIM_BOUNDARY,
        "created_at": CREATED_AT,
        "schema": MANIFEST_SCHEMA,
        "stage": MANIFEST_STAGE,
        "validation": VALIDATION,
    }
    errors: list[str] = []
    for field, expected in expected_fixed.items():
        if not _exact_equal(recorded.get(field), expected):
            errors.append(f"{field} differs from the frozen M5a contract")
    for field, expected in actual.items():
        if not _exact_equal(recorded.get(field), expected):
            errors.append(f"{field} snapshot differs from current files")
    audit = actual["import_audit"]
    if not isinstance(audit, dict):
        errors.append("import audit is malformed")
    elif (
        audit.get("forbidden_imports") != {}
        or audit.get("legacy_adapter_imported") is not False
        or audit.get("top_level_engine_imported") is not False
    ):
        errors.append("Route-A imports a forbidden legacy backend")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render",
        action="store_true",
        help="print the current additive manifest instead of verifying it",
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
