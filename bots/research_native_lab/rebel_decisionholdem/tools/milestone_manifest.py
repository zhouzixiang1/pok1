"""Build and verify a complete route-A milestone content/evidence snapshot."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import json
import sys
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
from ..decisionholdem_like.common_adapter import (
    COMMON_ADAPTER_VERSION,
    COMMON_CONTRACT_VERSION,
)
from ..decisionholdem_like.hunl_blueprint import (
    HUNL_MATERIAL_POLICY_L1_THRESHOLD,
    HUNLBlueprint,
    build_hunl_blueprint_payload,
)
from ..decisionholdem_like.hunl_external_sampling import (
    HUNLExternalSamplingLCFR,
    strict_json_loads,
    training_identity_digest,
)
from ..decisionholdem_like.secure_files import (
    pretty_json_bytes,
    secure_file_map as _shared_secure_file_map,
    stable_read_path,
    stable_read_relative as _shared_stable_read_relative,
)
from ..rebel_like.toy_loop import run_toy_selfplay
from .run_hunl_tcp_smoke import (
    TCP_SMOKE_SCHEMA,
    _backend_snapshot,
    forbidden_backend_imports,
    validate_tcp_semantic_projection,
)
from .train_hunl_blueprint import (
    ITERATION_CANDIDATES,
    ITERATION_SELECTION_CONTRACT,
    SCALE_SCHEMA,
    TRAINING_HEARTBEAT_SCHEMA,
    TRAINING_RUN_CHECKPOINT_SCHEMA,
    TRAINING_RUN_CONTRACT,
    blueprint_nonuniformity_snapshot,
    load_config,
    seed_independence_snapshot,
    select_training_candidate,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_RELATIVE_PATH = Path("manifests/milestone_m0_m4.json")
MANIFEST_PATH = PACKAGE_ROOT / MANIFEST_RELATIVE_PATH
MANIFEST_SCHEMA = "route-a-milestone-manifest-v9"
MANIFEST_STAGE = "M0-M4 route-A low-budget real-HUNL blueprint vertical slice"
HUNL_CONFIG_PATH = PACKAGE_ROOT / "configs/hunl_m4_smoke.json"
HUNL_SCALE_PATH = PACKAGE_ROOT / "evidence/m4_scale_gate.json"
HUNL_TCP_PATH = PACKAGE_ROOT / "evidence/m4_sever_tcp_70h.json"
COMMON_ROOT = PACKAGE_ROOT.parent / "common_contracts"
COMMON_INTERFACE_FILES = (
    "actions.py",
    "cards.py",
    "constants.py",
    "contracts/national_game_v1.json",
    "national_state.py",
    "protocol.py",
)
IGNORED_DIRECTORIES = frozenset(
    {"__pycache__", ".pytest_cache", "checkpoints", "data", "results"}
)

_ROUTE_PACKAGE = __package__.rsplit(".tools", 1)[0]
_COMMON_PACKAGE = _ROUTE_PACKAGE.rsplit(".", 1)[0] + ".common_contracts"
_EXPECTED_MODULE_FILES = {
    __name__: PACKAGE_ROOT / "tools/milestone_manifest.py",
    f"{_ROUTE_PACKAGE}.decisionholdem_like.hunl_blueprint": (
        PACKAGE_ROOT / "decisionholdem_like/hunl_blueprint.py"
    ),
    f"{_ROUTE_PACKAGE}.decisionholdem_like.hunl_external_sampling": (
        PACKAGE_ROOT / "decisionholdem_like/hunl_external_sampling.py"
    ),
    f"{_ROUTE_PACKAGE}.decisionholdem_like.common_native_entry": (
        PACKAGE_ROOT / "decisionholdem_like/common_native_entry.py"
    ),
    f"{_ROUTE_PACKAGE}.decisionholdem_like.hunl_tcp_client": (
        PACKAGE_ROOT / "decisionholdem_like/hunl_tcp_client.py"
    ),
    f"{_ROUTE_PACKAGE}.tools.train_hunl_blueprint": (
        PACKAGE_ROOT / "tools/train_hunl_blueprint.py"
    ),
    f"{_ROUTE_PACKAGE}.tools.run_hunl_tcp_smoke": (
        PACKAGE_ROOT / "tools/run_hunl_tcp_smoke.py"
    ),
    f"{_COMMON_PACKAGE}.actions": COMMON_ROOT / "actions.py",
    f"{_COMMON_PACKAGE}.cards": COMMON_ROOT / "cards.py",
    f"{_COMMON_PACKAGE}.constants": COMMON_ROOT / "constants.py",
    f"{_COMMON_PACKAGE}.national_state": COMMON_ROOT / "national_state.py",
    f"{_COMMON_PACKAGE}.protocol": COMMON_ROOT / "protocol.py",
}
_MANIFEST_FIELDS = {
    "base_sha",
    "common_interface",
    "created_at",
    "large_training_started",
    "m4",
    "m4_blueprint_vertical_slice_complete",
    "online_search_complete",
    "schema",
    "stage",
    "strength_frozen",
    "submission_bot_claimed",
    "tree",
    "validation",
}


def _assert_real_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise ValueError(f"{label} must be an existing directory: {path}")
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise ValueError(f"{label} must be a real non-symlink directory: {path}")


def _assert_authoritative_root(root: Path = PACKAGE_ROOT) -> None:
    """Reject copied/relative/symlink roots and mixed-origin imported modules."""

    if not isinstance(root, Path) or root != PACKAGE_ROOT:
        raise ValueError(
            f"snapshot root must be the loaded authoritative PACKAGE_ROOT: {PACKAGE_ROOT}"
        )
    _assert_real_directory(PACKAGE_ROOT, "PACKAGE_ROOT")
    _assert_real_directory(COMMON_ROOT, "COMMON_ROOT")
    for module_name, expected in _EXPECTED_MODULE_FILES.items():
        module = sys.modules.get(module_name)
        if module is None:
            module = importlib.import_module(module_name)
        actual_value = getattr(module, "__file__", None)
        if not isinstance(actual_value, str):
            raise ValueError(f"critical module has no filesystem origin: {module_name}")
        actual = Path(actual_value)
        if (
            actual != expected
            or actual.is_symlink()
            or actual.resolve(strict=True) != expected
        ):
            raise ValueError(
                "critical module provenance mismatch: "
                f"{module_name} loaded from {actual}, expected {expected}"
            )


def _assert_authoritative_manifest(
    path: Path = MANIFEST_PATH, *, require_exists: bool
) -> None:
    if not isinstance(path, Path) or path != MANIFEST_PATH:
        raise ValueError(
            f"manifest path must be the authoritative MANIFEST_PATH: {MANIFEST_PATH}"
        )
    _assert_authoritative_root(PACKAGE_ROOT)
    if path.is_symlink():
        raise ValueError("authoritative manifest must not be a symlink")
    if require_exists:
        if not path.is_file() or path.resolve(strict=True) != MANIFEST_PATH:
            raise ValueError("authoritative manifest must be an existing regular file")
    elif path.exists() and (
        not path.is_file() or path.resolve(strict=True) != MANIFEST_PATH
    ):
        raise ValueError("authoritative manifest path has an invalid filesystem object")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


# Publication, training identity, checkpoint and artifact loads all use the
# same fd-bound primitives.  The wrappers only supply this manifest's ignored
# directory contract.
def _secure_file_map(
    root: Path,
    *,
    excluded: frozenset[str] = frozenset(),
) -> dict[str, str]:
    return _shared_secure_file_map(
        root,
        ignored_directories=IGNORED_DIRECTORIES,
        excluded=excluded,
    )


def _read_stable_relative(root: Path, relative: Path) -> bytes:
    return _shared_stable_read_relative(root, relative)


def iter_snapshot_files(root: Path = PACKAGE_ROOT) -> tuple[Path, ...]:
    """Return every non-generated regular file except the self-referential manifest."""

    _assert_authoritative_root(root)
    files = _secure_file_map(
        root,
        excluded=frozenset({MANIFEST_RELATIVE_PATH.as_posix()}),
    )
    return tuple(root / relative for relative in files)


def build_tree_snapshot(root: Path = PACKAGE_ROOT) -> dict[str, object]:
    _assert_authoritative_root(root)
    files = _secure_file_map(
        root,
        excluded=frozenset({MANIFEST_RELATIVE_PATH.as_posix()}),
    )
    return {
        "algorithm": "sha256-canonical-file-map-v1",
        "excluded_self": MANIFEST_RELATIVE_PATH.as_posix(),
        "ignored_directories": sorted(IGNORED_DIRECTORIES),
        "file_count": len(files),
        "tree_sha256": _sha256_bytes(_canonical_bytes(files)),
        "files": files,
    }


def build_complete_common_tree_snapshot() -> dict[str, object]:
    """Bind every non-generated regular file in the authoritative Common package."""

    _assert_authoritative_root(PACKAGE_ROOT)
    package_files = _secure_file_map(COMMON_ROOT)
    return {
        "algorithm": "sha256-canonical-file-map-v1",
        "ignored_directories": sorted(IGNORED_DIRECTORIES),
        "file_count": len(package_files),
        "tree_sha256": _sha256_bytes(_canonical_bytes(package_files)),
        "files": package_files,
    }


def build_common_interface_snapshot(
    common_tree: dict[str, object] | None = None,
) -> dict[str, object]:
    _assert_authoritative_root(PACKAGE_ROOT)
    if common_tree is None:
        common_tree = build_complete_common_tree_snapshot()
    expected_tree = build_complete_common_tree_snapshot()
    if common_tree != expected_tree:
        raise ValueError("supplied Common tree is not the current complete package tree")
    package_files = common_tree.get("files")
    if not isinstance(package_files, dict):
        raise ValueError("complete Common tree has no file map")
    files = {
        name: package_files[name] for name in COMMON_INTERFACE_FILES
    }
    return {
        "contract_version": COMMON_CONTRACT_VERSION,
        "adapter_version": COMMON_ADAPTER_VERSION,
        "merged_commit": "a938d7cbc36016cb7b5cb444a7eb2e0f00cae73e",
        "files": files,
        "tree_sha256": _sha256_bytes(_canonical_bytes(files)),
        "package_file_count": common_tree["file_count"],
        "package_tree_sha256": common_tree["tree_sha256"],
        "package_files": package_files,
        "policy_entry": "decisionholdem_like.common_native_entry.CommonA2StrategyRuntime",
        "forbidden_policy_context": ["observation_id", "match_context_id"],
    }


def count_test_functions(root: Path = PACKAGE_ROOT) -> int:
    _assert_authoritative_root(root)
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
    _assert_authoritative_root(root)
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
        "plain_resolve_guess_heads_probability": (
            coin_toss.plain_resolve().guess_heads_probability
        ),
        "safe_resolve_guess_heads_probability": (
            coin_toss.safe_resolve().guess_heads_probability
        ),
        "safe_resolve_claim": (
            "source-shaped-functional-constraint-falsifier-not-full-resolve"
        ),
        "lcfr_reference": "independent-equation-oriented-kuhn-reference-v1",
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


def _load_evidence(path: Path, schema: str) -> dict[str, Any]:
    payload = strict_json_loads(stable_read_path(path))
    if not isinstance(payload, dict) or set(payload) != {
        "body",
        "body_sha256",
        "schema",
    }:
        raise ValueError(f"{path.name} wrapper fields are invalid")
    if payload["schema"] != schema:
        raise ValueError(f"{path.name} schema mismatch")
    body = payload["body"]
    if payload["body_sha256"] != _sha256_bytes(_canonical_bytes(body)):
        raise ValueError(f"{path.name} content hash mismatch")
    return payload


def build_m4_snapshot(root: Path = PACKAGE_ROOT) -> dict[str, object]:
    _assert_authoritative_root(root)
    config = load_config(root / "configs/hunl_m4_smoke.json")
    artifact_path = root / config["artifact_path"]
    artifact = HUNLBlueprint.load(artifact_path)
    training = config["training"]
    trainer, selection_trace = select_training_candidate(
        training,
        source_commit=config["source_commit"],
    )
    bound_checkpoint = trainer.checkpoint_payload()
    checkpoint_reloaded = HUNLExternalSamplingLCFR.from_checkpoint_payload(
        bound_checkpoint
    )
    rebuilt = build_hunl_blueprint_payload(
        checkpoint_reloaded,
        source_commit=config["source_commit"],
    )
    artifact_bytes_reproduce = (
        pretty_json_bytes(rebuilt) == stable_read_path(artifact_path)
    )
    scale = _load_evidence(root / "evidence/m4_scale_gate.json", SCALE_SCHEMA)
    tcp = _load_evidence(root / "evidence/m4_sever_tcp_70h.json", TCP_SMOKE_SCHEMA)
    scale_body = scale["body"]
    tcp_body = tcp["body"]
    seed_snapshot = seed_independence_snapshot(config)
    nonuniformity = blueprint_nonuniformity_snapshot(artifact)
    iteration_selection = {
        "candidate_sequence": list(ITERATION_CANDIDATES),
        "contract": ITERATION_SELECTION_CONTRACT,
        "selected_iterations": trainer.iterations_completed,
        "selection_inputs_exclude_tcp_smoke": True,
        "trace": selection_trace,
    }
    durable_resume = scale_body.get("durable_resume")
    if (
        scale_body.get("artifact_sha256") != artifact.digest
        or scale_body.get("checkpoint_sha256") != trainer.checkpoint_digest()
        or scale_body.get("checkpoint_training_identity_sha256")
        != training_identity_digest()
        or scale_body.get("iterations") != trainer.iterations_completed
        or scale_body.get("nodes_visited") != trainer.nodes_visited
        or scale_body.get("policy_rows") != len(artifact.policies)
        or scale_body.get("trained_backoff_rows")
        != {
            level: len(rows)
            for level, rows in artifact.trained_backoff_policies.items()
        }
        or scale_body.get("correctness_gate_passed") is not True
        or scale_body.get("scale_authorized") is not False
        or scale_body.get("checkpoint_retained") is not True
        or not isinstance(durable_resume, dict)
        or durable_resume.get("run_contract") != TRAINING_RUN_CONTRACT
        or durable_resume.get("checkpoint_schema")
        != TRAINING_RUN_CHECKPOINT_SCHEMA
        or durable_resume.get("heartbeat_schema") != TRAINING_HEARTBEAT_SCHEMA
        or durable_resume.get("every_segment_atomic") is not True
        or durable_resume.get("resume_replays_prior_selection_trace") is not True
        or durable_resume.get("cancel_marker_checked_at_durable_boundaries")
        is not True
        or scale_body.get("parallel_checkpoint_segment_merge_supported") is not False
        or scale_body.get("seed_independence") != seed_snapshot
        or scale_body.get("policy_nonuniformity") != nonuniformity
        or scale_body.get("iteration_selection") != iteration_selection
        or nonuniformity.get("total_materially_nonuniform_rows", 0) < 1
    ):
        raise ValueError("M4 scale evidence disagrees with the rebuilt artifact")
    clients = tcp_body.get("clients")
    influence = tcp_body.get("influence_gate")
    semantic_projection = validate_tcp_semantic_projection(tcp)
    if (
        tcp_body.get("blueprint_sha256") != artifact.digest
        or tcp_body.get("backend", {}).get("snapshot") != _backend_snapshot()
        or tcp_body.get("backend", {}).get("legacy_botzone_backend_used") is not False
        or tcp_body.get("backend", {}).get("authority")
        != "sever.GameEngine over asyncio TCP with explicit sever-local line adapter"
        or tcp_body.get("hands_played") != 70
        or tcp_body.get("illegal_actions") != 0
        or tcp_body.get("timeouts") != 0
        or tcp_body.get("result_authority") != "diagnostic_only_not_strength_evidence"
        or tcp_body.get("transport_framing") != "sever-local-line-adapter"
        or tcp_body.get("official_raw_no_delimiter_framing_proved") is not False
        or tcp_body.get("official_terminal_hand_70_proved") is not False
        or tcp_body.get("seed_independence") != seed_snapshot
        or not isinstance(influence, dict)
        or influence.get("contract")
        != "route-a2-predeclared-trained-policy-influence-v1"
        or influence.get("passed") is not True
        or influence.get("acceptance_uses_chip_result") is not False
        or influence.get("minimum_trained_derived_decisions_per_client") != 1
        or influence.get("minimum_trained_nonuniform_policy_decisions_per_client") != 1
        or influence.get("material_nonuniform_l1_threshold")
        != HUNL_MATERIAL_POLICY_L1_THRESHOLD
        or influence.get("smoke_deck_or_opponent_specific_training") is not False
        or not isinstance(clients, list)
        or len(clients) != 2
        or any(
            client.get("complete_70_hands") is not True
            or client.get("hands_started") != 70
            or client.get("settlements_received") != 70
            or type(client.get("decisions")) is not int
            or client.get("decisions") < 1
            or type(client.get("trained_exact_decisions")) is not int
            or type(client.get("trained_backoff_decisions")) is not int
            or type(client.get("uniform_emergency_decisions")) is not int
            or client.get("trained_derived_policy_decisions")
            != client.get("trained_exact_decisions")
            + client.get("trained_backoff_decisions")
            or client.get("trained_derived_policy_decisions", 0) < 1
            or client.get("trained_nonuniform_policy_decisions", 0) < 1
            or client.get("max_trained_policy_l1_from_uniform", 0.0)
            <= HUNL_MATERIAL_POLICY_L1_THRESHOLD
            or client.get("trained_exact_decisions")
            + client.get("trained_backoff_decisions")
            + client.get("uniform_emergency_decisions")
            != client.get("decisions")
            for client in clients
        )
        or sum(tcp_body.get("total_earnings", [])) != 0
        or semantic_projection.get("acceptance_excludes_chip_result") is not True
        or semantic_projection.get(
            "earnings_are_reproducible_diagnostic_record_only"
        )
        is not True
    ):
        raise ValueError("M4 TCP evidence is not a clean complete sever match")
    legacy_imports = forbidden_backend_imports()
    if legacy_imports:
        raise ValueError("M4 route imports the forbidden top-level engine")
    return {
        "artifact_bytes": artifact_path.stat().st_size,
        "artifact_sha256": artifact.digest,
        "checkpoint_sha256": trainer.checkpoint_digest(),
        "checkpoint_training_identity_sha256": training_identity_digest(),
        "common_tree_sha256": artifact.body["common"]["tree_sha256"],
        "iterations": trainer.iterations_completed,
        "iteration_selection": iteration_selection,
        "nodes_visited": trainer.nodes_visited,
        "policy_rows": len(artifact.policies),
        "policy_nonuniformity": nonuniformity,
        "trained_backoff_rows": {
            level: len(rows)
            for level, rows in artifact.trained_backoff_policies.items()
        },
        "artifact_bytes_reproduce": artifact_bytes_reproduce,
        "scale_evidence_sha256": scale["body_sha256"],
        "scale_authorized": scale_body["scale_authorized"],
        "tcp_backend_tree_sha256": tcp_body["backend"]["snapshot"]["tree_sha256"],
        "tcp_evidence_sha256": tcp["body_sha256"],
        "tcp_hands": tcp_body["hands_played"],
        "tcp_lookup_influence": [
            {
                "decisions": client["decisions"],
                "trained_backoff_decisions": client["trained_backoff_decisions"],
                "trained_derived_policy_decisions": client[
                    "trained_derived_policy_decisions"
                ],
                "trained_exact_decisions": client["trained_exact_decisions"],
                "trained_nonuniform_policy_decisions": client[
                    "trained_nonuniform_policy_decisions"
                ],
                "max_trained_policy_l1_from_uniform": client[
                    "max_trained_policy_l1_from_uniform"
                ],
                "uniform_emergency_decisions": client[
                    "uniform_emergency_decisions"
                ],
            }
            for client in clients
        ],
        "tcp_illegal_actions": tcp_body["illegal_actions"],
        "tcp_timeouts": tcp_body["timeouts"],
        "top_level_engine_imports": legacy_imports,
    }


def build_dynamic_snapshot(root: Path = PACKAGE_ROOT) -> dict[str, object]:
    _assert_authoritative_root(root)
    route_before = build_tree_snapshot(root)
    common_before = build_complete_common_tree_snapshot()
    validation = build_validation_snapshot(root)
    m4 = build_m4_snapshot(root)
    common_interface = build_common_interface_snapshot(common_before)
    route_after = build_tree_snapshot(root)
    common_after = build_complete_common_tree_snapshot()
    if route_before != route_after:
        raise RuntimeError("route package tree changed during milestone verification")
    if common_before != common_after:
        raise RuntimeError("Common package tree changed during milestone verification")
    return {
        "validation": validation,
        "m4": m4,
        "tree": route_before,
        "common_interface": common_interface,
    }


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    _assert_authoritative_manifest(path, require_exists=True)
    payload = strict_json_loads(
        _read_stable_relative(PACKAGE_ROOT, MANIFEST_RELATIVE_PATH)
    )
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_FIELDS:
        raise ValueError("milestone manifest fields are invalid")
    return payload


def render_current_manifest(path: Path = MANIFEST_PATH) -> dict[str, object]:
    _assert_authoritative_manifest(path, require_exists=False)
    existing = load_manifest(path) if path.exists() else {
        "base_sha": "59275e9bf63cfd03d66df9d8a232040586465e65",
        "created_at": "2026-07-14T00:00:00+08:00",
        "large_training_started": False,
        "online_search_complete": False,
        "strength_frozen": False,
        "submission_bot_claimed": False,
    }
    fixed = {
        key: value
        for key, value in existing.items()
        if key not in {"file_sha256", "validation", "m4", "tree", "common_interface"}
    }
    fixed.update(
        {
            "schema": MANIFEST_SCHEMA,
            "stage": MANIFEST_STAGE,
            "m4_blueprint_vertical_slice_complete": True,
        }
    )
    return fixed | build_dynamic_snapshot(path.parents[1])


def verify_manifest(path: Path = MANIFEST_PATH) -> list[str]:
    _assert_authoritative_manifest(path, require_exists=True)
    recorded = load_manifest(path)
    actual = build_dynamic_snapshot(path.parents[1])
    errors: list[str] = []
    if recorded.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"manifest schema must be {MANIFEST_SCHEMA}")
    if recorded.get("stage") != MANIFEST_STAGE:
        errors.append(f"manifest stage must be {MANIFEST_STAGE}")
    for flag in (
        "large_training_started",
        "online_search_complete",
        "strength_frozen",
        "submission_bot_claimed",
    ):
        if recorded.get(flag) is not False:
            errors.append(f"M4 manifest must keep {flag}=false")
    if recorded.get("m4_blueprint_vertical_slice_complete") is not True:
        errors.append("M4 manifest must bind the completed vertical slice")
    if actual["m4"].get("artifact_bytes_reproduce") is not True:
        errors.append("M4 blueprint bytes do not reproduce from the frozen config")
    for field in ("validation", "m4", "tree", "common_interface"):
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
