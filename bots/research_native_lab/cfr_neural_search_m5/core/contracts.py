"""Strict loaders for the immutable M4 dependency and M5 foundation contracts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from bots.research_native_lab.cfr_neural_search.core.identity import (
    file_sha256,
    payload_sha256,
)
from bots.research_native_lab.cfr_neural_search.core.strict_io import strict_json_read


M5_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
M4_DEPENDENCY_PATH = M5_ROOT / "contracts" / "m4_dependency_c44dd1eb.json"
CFV_SEMANTICS_PATH = M5_ROOT / "contracts" / "cfv_semantics_v1.json"
INDEPENDENCE_PATH = M5_ROOT / "contracts" / "independence_v1.json"
ORACLE_GATE_PATH = M5_ROOT / "contracts" / "oracle_gate_v1.json"


def _is_lowercase_digest(value: Any, *, length: int = 64) -> bool:
    return (
        type(value) is str
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def _strict_keys(payload: Any, keys: set[str], context: str) -> Mapping[str, Any]:
    if type(payload) is not dict or set(payload) != keys:
        raise ValueError(f"{context} differs from strict schema")
    return payload


def load_m4_dependency() -> Mapping[str, Any]:
    payload = strict_json_read(M4_DEPENDENCY_PATH, root=M5_ROOT)
    _strict_keys(
        payload,
        {
            "schema",
            "repository_relative_root",
            "release_commit",
            "release_root_git_tree",
            "tracked_file_count",
            "tracked_paths_payload_sha256",
            "tracked_files_payload_sha256",
            "source_snapshot_sha256",
            "m4_manifest",
            "published_outputs",
            "selector_event_tree_sha256",
            "verifier_source_sha256",
        },
        "M4 dependency contract",
    )
    if payload["schema"] != "route-b-m5-m4-dependency-v1":
        raise ValueError("unsupported M4 dependency contract")
    if payload["repository_relative_root"] != (
        "bots/research_native_lab/cfr_neural_search"
    ):
        raise ValueError("M4 dependency root changed")
    for field in (
        "release_commit",
        "release_root_git_tree",
        "tracked_paths_payload_sha256",
        "tracked_files_payload_sha256",
        "source_snapshot_sha256",
        "selector_event_tree_sha256",
        "verifier_source_sha256",
    ):
        value = payload[field]
        expected_length = 40 if field in {"release_commit", "release_root_git_tree"} else 64
        if not _is_lowercase_digest(value, length=expected_length):
            raise ValueError(f"M4 dependency {field} is not a lowercase digest")
    if type(payload["tracked_file_count"]) is not int or payload["tracked_file_count"] <= 0:
        raise ValueError("M4 dependency tracked file count is invalid")
    manifest = _strict_keys(
        payload["m4_manifest"],
        {"relative_path", "raw_sha256", "payload_sha256"},
        "M4 manifest identity",
    )
    if manifest["relative_path"] != "manifests/m4_gate_20260714.json":
        raise ValueError("M4 manifest dependency path changed")
    for field in ("raw_sha256", "payload_sha256"):
        if not _is_lowercase_digest(manifest[field]):
            raise ValueError(f"M4 manifest {field} is not a lowercase SHA-256")
    outputs = payload["published_outputs"]
    if type(outputs) is not dict or not outputs:
        raise ValueError("M4 published output closure is empty")
    for name, digest in {**outputs, **{manifest["relative_path"]: manifest["raw_sha256"]}}.items():
        if (
            type(name) is not str
            or name.startswith("/")
            or ".." in Path(name).parts
            or not _is_lowercase_digest(digest)
        ):
            raise ValueError("M4 dependency output identity is invalid")
    return payload


def _validate_cfv_semantics_payload(payload: Any) -> Mapping[str, Any]:
    _strict_keys(
        payload,
        {
            "schema",
            "players",
            "private_combo_index",
            "action_slots",
            "input",
            "mask",
            "target",
            "chance_weighting",
            "zero_sum",
            "forbidden_targets",
        },
        "CFV semantics contract",
    )
    if payload["schema"] != "route-b-range-cfv-semantics-v1" or payload["players"] != 2:
        raise ValueError("unsupported CFV semantics contract")
    if payload["action_slots"] != [
        "fold",
        "check",
        "call",
        "min_raise",
        "half_pot",
        "pot",
        "one_and_half_pot",
        "all_in",
    ]:
        raise ValueError("CFV legal action-slot order changed")
    index = _strict_keys(
        payload["private_combo_index"],
        {
            "combo_count",
            "card_ids",
            "ordering",
            "pair_constraint",
            "registry_reference",
            "registry_payload_sha256",
        },
        "CFV private combo index",
    )
    if (
        index["combo_count"] != 1326
        or index["card_ids"] != "0_through_51"
        or index["ordering"] != "lexicographic_first_card_then_second_card"
        or index["pair_constraint"] != "first_card_less_than_second_card"
        or index["registry_reference"]
        != "bots.research_native_lab.common_contracts.cards.all_hole_combinations"
        or index["registry_payload_sha256"]
        != "4534e13c4bd7a32ebb621433f5b08344b2bb81a04f3c78c2840cd9362bddf89a"
    ):
        raise ValueError("CFV physical combo order changed")
    input_contract = _strict_keys(
        payload["input"],
        {
            "legal_action_mask_shape",
            "private_ranges_shape",
            "public_state_fields",
            "range_normalization",
        },
        "CFV input contract",
    )
    if input_contract["private_ranges_shape"] != [2, 1326]:
        raise ValueError("CFV range input shape changed")
    if input_contract["legal_action_mask_shape"] != [8]:
        raise ValueError("CFV legal-action mask shape changed")
    if input_contract["public_state_fields"] != [
        "street",
        "board_card_ids",
        "small_blind_player",
        "actor",
        "pot_bb",
        "stacks_bb",
        "street_commitments_bb",
        "to_call_bb",
        "min_raise_to_bb",
        "public_action_history",
        "legal_action_mask",
    ]:
        raise ValueError("CFV public-state field contract changed")
    if input_contract["range_normalization"] != (
        "each_player_board_legal_nonnegative_sum_one"
    ):
        raise ValueError("CFV reach-range normalization changed")
    mask = _strict_keys(
        payload["mask"],
        {"false_output", "valid_when", "zero_own_reach_remains_evaluable"},
        "CFV mask contract",
    )
    if (
        type(mask["false_output"]) not in (int, float)
        or float(mask["false_output"]) != 0.0
        or mask["valid_when"]
        != "hero_combo_is_board_legal_and_has_positive_compatible_opponent_reach_mass"
        or mask["zero_own_reach_remains_evaluable"] is not True
    ):
        raise ValueError("CFV validity-mask semantics changed")
    target = _strict_keys(
        payload["target"],
        {"equation", "omit_own_reach", "payoff_origin", "payoff_unit", "shape"},
        "CFV target contract",
    )
    if target["shape"] != [2, 1326]:
        raise ValueError("CFV target shape changed")
    if target["omit_own_reach"] is not True:
        raise ValueError("CFV target no longer omits own reach")
    if target["equation"] != (
        "C_i(h_i)=sum_h_minus_i K(h_i,h_minus_i)*beta_minus_i(h_minus_i)*E[u_i_div_big_blind]"
    ):
        raise ValueError("CFV counterfactual target equation changed")
    if target["payoff_origin"] != (
        "net_chips_from_current_hand_including_past_contributions"
    ) or target["payoff_unit"] != "big_blind":
        raise ValueError("CFV payoff unit/origin changed")
    chance = _strict_keys(
        payload["chance_weighting"],
        {"future_cards", "public_prefix_chance"},
        "CFV chance contract",
    )
    if chance != {
        "future_cards": (
            "uniform_without_replacement_conditioned_on_public_board_and_both_private_combos"
        ),
        "public_prefix_chance": "already_conditioned_and_must_not_be_multiplied_again",
    }:
        raise ValueError("CFV chance-weighting semantics changed")
    zero_sum = _strict_keys(
        payload["zero_sum"],
        {"equation", "raw_and_deployed_residuals_required", "tolerance_bb"},
        "CFV zero-sum contract",
    )
    if zero_sum["equation"] != "dot(beta_0,C_0)+dot(beta_1,C_1)=0":
        raise ValueError("CFV zero-sum equation changed")
    if (
        zero_sum["raw_and_deployed_residuals_required"] is not True
        or type(zero_sum["tolerance_bb"]) is not float
        or zero_sum["tolerance_bb"] != 1e-6
    ):
        raise ValueError("CFV zero-sum diagnostics changed")
    if payload["forbidden_targets"] != [
        "sampled_private_deal_scalar",
        "scalar_equity_broadcast",
        "posterior_normalized_conditional_value",
        "bucket_value_expanded_to_physical_combos",
        "per_hand_opposite_player_negation",
    ]:
        raise ValueError("CFV forbidden-target closure changed")
    return payload


def load_cfv_semantics() -> Mapping[str, Any]:
    return _validate_cfv_semantics_payload(
        strict_json_read(CFV_SEMANTICS_PATH, root=M5_ROOT)
    )


def load_independence_contract() -> Mapping[str, Any]:
    payload = strict_json_read(INDEPENDENCE_PATH, root=M5_ROOT)
    _strict_keys(
        payload,
        {
            "schema",
            "m5_absolute_package",
            "allowed_external_import_roots",
            "allowed_m4_import_modules",
            "allowed_native_sever_import_modules",
            "allowed_runtime_roots",
            "allowed_runtime_m4_files",
            "allowed_runtime_native_sever_files",
            "forbidden_dynamic_import_modules",
            "forbidden_import_modules",
            "forbidden_dependency_tokens",
        },
        "M5 independence contract",
    )
    if payload["schema"] != "route-b-m5-independence-v1":
        raise ValueError("unsupported M5 independence contract")
    if payload["m5_absolute_package"] != (
        "bots.research_native_lab.cfr_neural_search_m5"
    ):
        raise ValueError("M5 absolute package identity changed")
    for field in (
        "allowed_external_import_roots",
        "allowed_m4_import_modules",
        "allowed_native_sever_import_modules",
        "allowed_runtime_roots",
        "allowed_runtime_m4_files",
        "allowed_runtime_native_sever_files",
        "forbidden_dynamic_import_modules",
        "forbidden_import_modules",
        "forbidden_dependency_tokens",
    ):
        values = payload[field]
        if (
            type(values) is not list
            or not values
            or any(type(value) is not str or not value for value in values)
            or values != sorted(set(values))
        ):
            raise ValueError(f"independence contract {field} is not sorted and unique")
    if payload["allowed_runtime_roots"] != [
        "bots/research_native_lab/cfr_neural_search_m5",
        "bots/research_native_lab/common_contracts",
    ]:
        raise ValueError("runtime root allowlist is broader than M5/Common")
    if payload["allowed_native_sever_import_modules"] != ["sever.engine.validator"]:
        raise ValueError("native sever import allowlist changed")
    if payload["allowed_runtime_native_sever_files"] != [
        "sever/engine/validator.py"
    ]:
        raise ValueError("native sever runtime-file allowlist changed")
    if payload["forbidden_dynamic_import_modules"] != [
        "importlib",
        "pkgutil",
        "runpy",
        "zipimport",
    ]:
        raise ValueError("dynamic import-module denylist changed")
    if "sever.bot_adapter" not in payload["forbidden_import_modules"]:
        raise ValueError("legacy adapter is not explicitly forbidden")
    if any(
        not module.startswith("bots.research_native_lab.cfr_neural_search.")
        for module in payload["allowed_m4_import_modules"]
    ):
        raise ValueError("M4 import allowlist escaped the frozen Route B package")
    if any(
        not relative.startswith("bots/research_native_lab/cfr_neural_search/")
        for relative in payload["allowed_runtime_m4_files"]
    ):
        raise ValueError("M4 runtime-file allowlist escaped the frozen Route B package")
    return payload


def load_oracle_gate_contract() -> Mapping[str, Any]:
    payload = strict_json_read(ORACLE_GATE_PATH, root=M5_ROOT)
    expected = {
        "authority": {
            "label_generation_authorized": False,
            "online_tcp_authorized": False,
            "training_authorized": False,
        },
        "cfv": {
            "combo_count": 1326,
            "mask_false_output": 0.0,
            "omit_own_reach": True,
            "players": 2,
            "query_common_replay_required": True,
            "shape": [2, 1326],
        },
        "hunl": {
            "common_replay_defines_terminal_node_kind": True,
            "exact_zero_sum_abs_tolerance_bb": 1e-10,
            "payoff_unit": "big_blind_including_past_contributions",
            "required_micro_oracles": [
                "fold",
                "showdown",
                "river_one_decision",
                "turn_allin_exact_runout",
            ],
            "turn_conditioned_river_count": 44,
        },
        "leaf_consumer": {
            "formal_fallback_allowed": False,
            "formal_model_requires_neural_provider_kind": True,
            "formal_primary_requires_external_contract_digest": True,
            "input": "public_state_plus_two_complete_reach_ranges",
            "known_exact_providers": {
                "route-b-m5-hunl-fold-oracle-v1": {
                    "callable": {
                        "module": (
                            "bots.research_native_lab.cfr_neural_search_m5."
                            "cfv.hunl_micro_oracle"
                        ),
                        "qualified_name": "exact_fold_cfv",
                    },
                    "runtime_dependencies_sha256": (
                        "6f50a78ddf1bca18ed85fbb860507700"
                        "221dcea091c8efcf74d8dc94e3861c56"
                    ),
                },
                "route-b-m5-hunl-river-one-decision-oracle-v1": {
                    "callable": {
                        "module": (
                            "bots.research_native_lab.cfr_neural_search_m5."
                            "cfv.hunl_micro_oracle"
                        ),
                        "qualified_name": "exact_river_call_or_fold_cfv",
                    },
                    "runtime_dependencies_sha256": (
                        "4de12fc4702b8dcf77188a4f48dc6f3d"
                        "ef97c2b7f92e84f58e5e557d3fb61c0c"
                    ),
                },
                "route-b-m5-hunl-showdown-oracle-v1": {
                    "callable": {
                        "module": (
                            "bots.research_native_lab.cfr_neural_search_m5."
                            "cfv.hunl_micro_oracle"
                        ),
                        "qualified_name": "exact_showdown_cfv",
                    },
                    "runtime_dependencies_sha256": (
                        "bc871f79a2375746aa78fa67e619dadd"
                        "693ab38de81f805ebdab6706fe271225"
                    ),
                },
                "route-b-m5-hunl-turn-allin-runout-oracle-v1": {
                    "callable": {
                        "module": (
                            "bots.research_native_lab.cfr_neural_search_m5."
                            "cfv.hunl_micro_oracle"
                        ),
                        "qualified_name": "exact_turn_allin_runout_cfv",
                    },
                    "runtime_dependencies_sha256": (
                        "fe13616e676a440668cb3a31352c1d79"
                        "24505e9e18b962d87c9ec96f82ffc99f"
                    ),
                },
            },
            "old_private_state_scalar_leaf_allowed": False,
            "provider_must_be_content_bound_and_sealed": True,
            "provider_source_closure_system_owned": True,
            "runtime_helper_binding_required": True,
            "runtime_tamper_boundary": (
                "provider_and_recursive_dependencies_with_contract_held_pinned_"
                "manifest_builder_not_arbitrary_interpreter_or_consumer_rewrite"
            ),
            "verifier_runtime_manifest_builder": {
                "callable": {
                    "module": (
                        "bots.research_native_lab.cfr_neural_search_m5."
                        "solver.range_cfv_contract"
                    ),
                    "qualified_name": "_runtime_dependency_manifest",
                },
                "code_sha256": (
                    "31fc63253b3a2df29507faf42990058bf"
                    "98cb46a8156f19c448fd3edcda945c8"
                ),
                "runtime_dependencies_sha256": (
                    "ba459ea89695350d88ef3e395a340d110"
                    "f1670e77124f0596b83da53f2470b43"
                ),
            },
        },
        "schema": "route-b-m5-oracle-gate-contract-v1",
        "toy": {
            "finite_difference_epsilon": 1e-7,
            "finite_difference_max_abs_error": 1e-7,
            "games": ["kuhn", "leduc"],
            "one_step_regret_abs_tolerance": 1e-10,
            "root_value_abs_tolerance": 1e-10,
            "zero_sum_abs_tolerance": 1e-10,
        },
    }
    if payload != expected:
        raise ValueError("M5 exact-oracle gate contract changed")
    return payload


def _git(*arguments: str) -> str:
    return subprocess.check_output(
        ("git", *arguments),
        cwd=REPOSITORY_ROOT,
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def _audit_untracked_m4_tree(m4_root: Path, tracked: frozenset[str]) -> int:
    """Return quarantined file count or reject any import/shadow surface."""

    importable_suffixes = frozenset(
        {
            ".dll",
            ".dylib",
            ".egg-link",
            ".pyd",
            ".py",
            ".pyc",
            ".pyo",
            ".pyw",
            ".pth",
            ".so",
        }
    )
    quarantine_files = 0
    for path in sorted(m4_root.rglob("*")):
        relative = path.relative_to(m4_root)
        if path.is_symlink():
            raise ValueError(f"M4 dependency tree contains a symlink: {relative}")
        if not path.is_file():
            continue
        if relative.as_posix() in tracked:
            continue
        if "__pycache__" in relative.parts:
            if path.suffix not in {".pyc", ".pyo"}:
                raise ValueError(f"M4 cache contains a non-cache file: {relative}")
            continue
        if relative.parts and relative.parts[0] == "runtime_outputs":
            if any(path.name.endswith(suffix) for suffix in importable_suffixes):
                raise ValueError(
                    f"M4 runtime quarantine contains importable code: {relative}"
                )
            quarantine_files += 1
            continue
        raise ValueError(f"untracked file shadows the frozen M4 tree: {relative}")
    return quarantine_files


def capture_m4_tracked_closure() -> dict[str, Any]:
    """Hash every currently tracked M4 path and byte, independent of commit text."""

    contract = load_m4_dependency()
    relative_root = str(contract["repository_relative_root"])
    prefix = relative_root + "/"
    names = _git("ls-files", prefix).splitlines()
    if names != sorted(names) or any(not name.startswith(prefix) for name in names):
        raise ValueError("current M4 tracked path set is not canonical")
    relative_names = [name[len(prefix) :] for name in names]
    files: dict[str, str] = {}
    m4_root = REPOSITORY_ROOT / relative_root
    for name in relative_names:
        path = m4_root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"tracked M4 dependency is not a real file: {name}")
        files[name] = file_sha256(path)

    # A byte-exact tracked closure is insufficient if Python can shadow one of
    # those modules with an ignored/untracked source or extension.  Only
    # interpreter caches and the explicitly quarantined M4 runtime-output tree
    # may be present in addition to the release tree.  Runtime outputs can hold
    # old checkpoints, but cannot contain importable code.
    tracked = frozenset(relative_names)
    quarantine_files = _audit_untracked_m4_tree(m4_root, tracked)
    return {
        "file_count": len(files),
        "paths_payload_sha256": payload_sha256({"paths": relative_names}),
        "files_payload_sha256": payload_sha256({"files": files}),
        "untracked_importable_file_count": 0,
        "quarantined_runtime_file_count": quarantine_files,
    }


def verify_m4_dependency() -> dict[str, Any]:
    """Verify current M4 bytes, the release Git object, outputs, and old gate."""

    contract = load_m4_dependency()
    closure = capture_m4_tracked_closure()
    expected_closure = {
        "file_count": contract["tracked_file_count"],
        "paths_payload_sha256": contract["tracked_paths_payload_sha256"],
        "files_payload_sha256": contract["tracked_files_payload_sha256"],
    }
    if {field: closure[field] for field in expected_closure} != expected_closure:
        raise ValueError("M4 tracked-byte closure changed after the M4 release")
    if closure["untracked_importable_file_count"] != 0:
        raise ValueError("M4 import graph contains untracked importable code")
    relative_root = str(contract["repository_relative_root"])
    release_tree = _git("rev-parse", f"{contract['release_commit']}:{relative_root}")
    if release_tree != contract["release_root_git_tree"]:
        raise ValueError("M4 release commit tree differs from dependency contract")
    m4_root = REPOSITORY_ROOT / relative_root
    manifest = contract["m4_manifest"]
    if file_sha256(m4_root / manifest["relative_path"]) != manifest["raw_sha256"]:
        raise ValueError("M4 manifest raw bytes changed")
    for relative, digest in contract["published_outputs"].items():
        if file_sha256(m4_root / relative) != digest:
            raise ValueError(f"M4 published dependency changed: {relative}")
    if file_sha256(m4_root / "tools" / "verify_m4_gate.py") != contract[
        "verifier_source_sha256"
    ]:
        raise ValueError("original M4 verifier source changed")

    from bots.research_native_lab.cfr_neural_search.tools.verify_m4_gate import (
        verify_m4_manifest,
    )

    rendered = verify_m4_manifest()
    if payload_sha256(rendered) != manifest["payload_sha256"]:
        raise ValueError("original M4 verifier returned a different payload")
    if rendered["root_contract"]["source_snapshot_sha256"] != contract[
        "source_snapshot_sha256"
    ]:
        raise ValueError("original M4 source identity changed")
    return {
        "schema": "route-b-m5-m4-dependency-verification-v1",
        "release_commit": contract["release_commit"],
        "source_snapshot_sha256": contract["source_snapshot_sha256"],
        "manifest_payload_sha256": manifest["payload_sha256"],
        **closure,
    }


def foundation_contract_digest() -> str:
    payload = {
        "m4_dependency": load_m4_dependency(),
        "cfv_semantics": load_cfv_semantics(),
        "independence": load_independence_contract(),
        "oracle_gate": load_oracle_gate_contract(),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
