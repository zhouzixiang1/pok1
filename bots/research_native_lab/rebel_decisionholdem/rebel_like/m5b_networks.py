"""Deterministic CUDA value/policy networks and crash-safe M5b training."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import random
import resource
import stat
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

import numpy as np
import torch
from torch import Tensor, nn
from torch.nn import functional as F

from ...common_contracts.actions import Action
from ...common_contracts.national_state import NationalGameState
from .hunl_pbs import HUNL_COMBO_COUNT
from .hunl_pbs import (
    HUNLReachFactorPublicBeliefState,
    legal_combo_mask,
    validate_hunl_network_input,
)
from .m5b_contract import ACTION_SLOTS, M5B_MODEL_SCHEMA, canonical_bytes
from .m5b_data import (
    PUBLIC_FEATURE_DIM,
    PreLabelPlan,
    encode_public_features,
    validate_generator_receipt,
)
from .m5b_search import (
    ACTION_COUNT,
    AbstractActionSet,
    DepthLimitedCFRAvg,
    PublicInferenceInput,
    ReachFactors,
    abstract_actions,
)


VALUE_SCALE_CHIPS = 20000.0
DEPLOY_SCHEMA = "route-a1-m5b-safe-deploy-npz-v1"
DEPLOY_METADATA_FIELDS = {
    "schema",
    "architecture",
    "config_sha256",
    "dataset_sha256",
    "runtime",
    "runtime_sha256",
    "value_output_unit",
    "policy_output",
    "raw_npz_sha256",
    "tensor_state_sha256",
    "tensor_schema",
    "pickle_required",
    "metadata_sha256",
}
TRAINING_CHECKPOINT_SCHEMA = "route-a1-m5b-training-checkpoint-v1"
TRAINING_CHECKPOINT_FIELDS = {
    "schema",
    "model",
    "optimizer",
    "scheduler",
    "scaler",
    "cursor",
    "cpu_rng",
    "cuda_rng_all",
    "numpy_rng",
    "python_rng",
    "loader_generator_rng",
    "config_digest",
    "dataset_digest",
    "runtime_digest",
    "loss_history",
}
TRAINING_RESOURCE_FIELDS = {
    "schema",
    "device",
    "config_sha256",
    "dataset_sha256",
    "runtime_sha256",
    "start_global_step",
    "end_global_step",
    "start_model_tensor_sha256",
    "end_model_tensor_sha256",
    "steps",
    "samples",
    "elapsed_wall_seconds",
    "gpu_seconds",
    "gpu_seconds_semantics",
    "steps_per_second",
    "samples_per_second",
    "peak_cuda_allocated_bytes",
    "peak_cuda_reserved_bytes",
    "process_peak_rss_before_bytes",
    "process_peak_rss_after_bytes",
}
INFERENCE_RESOURCE_FIELDS = {
    "schema",
    "device",
    "model_tensor_sha256",
    "runtime_sha256",
    "batch_size",
    "warmup_runs",
    "measured_runs",
    "latency_semantics",
    "latency_median_ms",
    "latency_p95_ms",
    "latency_min_ms",
    "latency_max_ms",
    "public_states_per_second_at_median",
    "elapsed_wall_seconds",
    "gpu_seconds",
    "gpu_seconds_semantics",
    "peak_cuda_allocated_bytes",
    "peak_cuda_reserved_bytes",
    "process_peak_rss_before_bytes",
    "process_peak_rss_after_bytes",
}


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _reject_symlink_components(path: Path) -> None:
    absolute = Path(path) if Path(path).is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise ValueError(f"artifact path contains a symlink: {current}")


def _stable_regular_bytes(path: Path) -> bytes:
    """Read one immutable regular file without following symlinks."""

    path = Path(path)
    _reject_symlink_components(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ValueError(f"artifact is not a non-symlink regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("artifact changed during secure open")
        chunks: list[bytes] = []
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise ValueError("artifact changed during stable read")
        _reject_symlink_components(path)
        final = path.lstat()
        if (final.st_dev, final.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("artifact path changed during stable read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _no_clobber_bytes(path: Path, payload: bytes) -> None:
    _reject_symlink_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(path.parent)
    descriptor, temporary_text = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_text)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _stable_regular_bytes(path) != payload:
                raise ValueError(f"content-addressed path collision: {path}")
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_json_file_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _strict_json_object(raw: bytes, *, label: str) -> dict[str, object]:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate {label} key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite {label} value: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not strict JSON") from exc
    if type(payload) is not dict:
        raise ValueError(f"{label} is not an object")
    return payload


def _deterministic_npz_bytes(arrays: Mapping[str, np.ndarray]) -> bytes:
    """Build a byte-stable, pickle-free NPZ with fixed ZIP metadata."""

    output = io.BytesIO()
    with zipfile.ZipFile(
        output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for name, source in sorted(arrays.items()):
            if not name or "/" in name or "\\" in name:
                raise ValueError("unsafe NPZ tensor name")
            array = np.ascontiguousarray(source)
            member = io.BytesIO()
            np.lib.format.write_array(member, array, allow_pickle=False)
            info = zipfile.ZipInfo(f"{name}.npy", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            archive.writestr(
                info,
                member.getvalue(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    return output.getvalue()


def configure_deterministic_runtime() -> None:
    torch.use_deterministic_algorithms(True)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


def runtime_receipt() -> dict[str, object]:
    if not torch.cuda.is_available():
        raise RuntimeError("M5b formal training requires CUDA")
    fields = subprocess.check_output(
        (
            "nvidia-smi",
            "--id=0",
            "--query-gpu=name,uuid,driver_version,memory.total",
            "--format=csv,noheader,nounits",
        ),
        text=True,
    ).strip().split(", ")
    if len(fields) != 4:
        raise RuntimeError("unexpected nvidia-smi runtime receipt")
    properties = torch.cuda.get_device_properties(0)
    return {
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "cudnn": torch.backends.cudnn.version(),
        "driver": fields[2],
        "gpu_name": fields[0],
        "gpu_uuid": fields[1],
        "gpu_total_memory_bytes": int(properties.total_memory),
        "deterministic_algorithms": torch.are_deterministic_algorithms_enabled(),
        "cublas_workspace_config": os.environ.get("CUBLAS_WORKSPACE_CONFIG"),
        "tf32_matmul": torch.backends.cuda.matmul.allow_tf32,
        "tf32_cudnn": torch.backends.cudnn.allow_tf32,
    }


def verify_runtime_binding(expected: Mapping[str, object]) -> dict[str, object]:
    configure_deterministic_runtime()
    observed = runtime_receipt()
    if dict(expected) != observed:
        raise RuntimeError(
            "formal CUDA runtime differs from preregistered binding: "
            + json.dumps({"expected": dict(expected), "observed": observed}, sort_keys=True)
        )
    return observed


class M5BValuePolicyNet(nn.Module):
    """Public PBS network with fixed full-combo value and actor-policy heads."""

    def __init__(
        self,
        *,
        combo_embedding_dim: int,
        global_hidden_dim: int,
        trunk_hidden_dim: int,
        layers: int,
    ) -> None:
        super().__init__()
        self.combo_embedding_dim = int(combo_embedding_dim)
        self.global_hidden_dim = int(global_hidden_dim)
        self.trunk_hidden_dim = int(trunk_hidden_dim)
        self.layers = int(layers)
        global_input_dim = (
            PUBLIC_FEATURE_DIM + 2 * HUNL_COMBO_COUNT + HUNL_COMBO_COUNT + ACTION_COUNT
        )
        self.global_encoder = nn.Sequential(
            nn.Linear(global_input_dim, global_hidden_dim),
            nn.GELU(),
            nn.LayerNorm(global_hidden_dim),
            nn.Linear(global_hidden_dim, global_hidden_dim),
            nn.GELU(),
            nn.LayerNorm(global_hidden_dim),
        )
        self.combo_embedding = nn.Embedding(HUNL_COMBO_COUNT, combo_embedding_dim)
        local_dim = global_hidden_dim + combo_embedding_dim + 3
        trunk: list[nn.Module] = []
        for layer in range(layers):
            trunk.extend(
                (
                    nn.Linear(local_dim if layer == 0 else trunk_hidden_dim, trunk_hidden_dim),
                    nn.GELU(),
                    nn.LayerNorm(trunk_hidden_dim),
                )
            )
        self.trunk = nn.Sequential(*trunk)
        self.value_head = nn.Linear(trunk_hidden_dim, 2)
        self.policy_head = nn.Linear(trunk_hidden_dim, ACTION_COUNT)
        self.register_buffer(
            "combo_indices", torch.arange(HUNL_COMBO_COUNT), persistent=False
        )

    def architecture(self) -> dict[str, object]:
        return {
            "schema": M5B_MODEL_SCHEMA,
            "public_feature_dim": PUBLIC_FEATURE_DIM,
            "combo_count": HUNL_COMBO_COUNT,
            "action_slots": list(ACTION_SLOTS),
            "combo_embedding_dim": self.combo_embedding_dim,
            "global_hidden_dim": self.global_hidden_dim,
            "trunk_hidden_dim": self.trunk_hidden_dim,
            "layers": self.layers,
            "activation": "gelu",
            "normalization": "layer_norm",
            "dropout": False,
            "parameter_count": sum(
                parameter.numel() for parameter in self.parameters()
            ),
            "parameter_dtype": "float32",
            "value_shape": [2, 1326],
            "policy_shape": [1326, 9],
            "value_internal_unit": "chips_div_20000",
            "policy_internal_unit": "legal_action_masked_logits",
            "policy_consumer_transform": "softmax_over_nine_slots",
        }

    def forward(
        self,
        public_features: Tensor,
        reach_factors: Tensor,
        legal_combo_mask: Tensor,
        legal_action_mask: Tensor,
    ) -> tuple[Tensor, Tensor]:
        batch = public_features.shape[0]
        if public_features.shape != (batch, PUBLIC_FEATURE_DIM):
            raise ValueError("public feature batch shape differs")
        if reach_factors.shape != (batch, 2, HUNL_COMBO_COUNT):
            raise ValueError("reach batch shape differs")
        if legal_combo_mask.shape != (batch, HUNL_COMBO_COUNT):
            raise ValueError("combo mask batch shape differs")
        if legal_action_mask.shape != (batch, ACTION_COUNT):
            raise ValueError("action mask batch shape differs")
        if legal_combo_mask.dtype != torch.bool or legal_action_mask.dtype != torch.bool:
            raise ValueError("network masks must be boolean")
        if not bool(torch.all(torch.any(legal_combo_mask, dim=1))):
            raise ValueError("network input has no legal private combo")
        if not bool(torch.all(torch.any(legal_action_mask, dim=1))):
            raise ValueError("network input has no legal public action")
        if not bool(torch.all(torch.isfinite(public_features))) or not bool(
            torch.all(torch.isfinite(reach_factors))
        ):
            raise ValueError("network input is non-finite")
        if bool(torch.any(reach_factors < 0.0)):
            raise ValueError("network reach factors are negative")
        global_input = torch.cat(
            (
                public_features,
                reach_factors.flatten(1),
                legal_combo_mask.float(),
                legal_action_mask.float(),
            ),
            dim=1,
        )
        global_context = self.global_encoder(global_input)
        embedding = self.combo_embedding(self.combo_indices).unsqueeze(0).expand(
            batch, -1, -1
        )
        local = torch.cat(
            (
                embedding,
                reach_factors.transpose(1, 2),
                legal_combo_mask.float().unsqueeze(-1),
                global_context.unsqueeze(1).expand(-1, HUNL_COMBO_COUNT, -1),
            ),
            dim=2,
        )
        hidden = self.trunk(local)
        values = self.value_head(hidden).transpose(1, 2)
        logits = self.policy_head(hidden)
        floor = torch.finfo(logits.dtype).min
        logits = logits.masked_fill(~legal_action_mask[:, None, :], floor)
        return values, logits


def build_model(network_config: Mapping[str, object], *, seed: int) -> M5BValuePolicyNet:
    torch.manual_seed(int(seed))
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(int(seed))
    return M5BValuePolicyNet(
        combo_embedding_dim=int(network_config["combo_embedding_dim"]),
        global_hidden_dim=int(network_config["global_hidden_dim"]),
        trunk_hidden_dim=int(network_config["trunk_hidden_dim"]),
        layers=int(network_config["layers"]),
    )


class ShardDataset:
    """In-memory readback of content-bound NPZ shards."""

    ARRAY_FIELDS = (
        "public_features",
        "actor",
        "reach_factors",
        "legal_combo_mask",
        "legal_action_mask",
        "oracle_on_policy_private_values",
        "oracle_value_valid_mask",
        "projected_marginals",
        "oracle_actor_policy",
    )
    METADATA_FIELDS = {
        "schema",
        "sample_id",
        "split",
        "prelabel_plan",
        "pbs_network_input_sha256",
        "pbs_network_input",
        "pbs_public_state",
        "public_family_id",
        "split_manifest_sha256",
        "action_support",
        "primary_value_target",
        "primary_policy_target",
        "q_cfv_training_authority",
        "payoff_unit",
        "payoff_origin",
        "array_content_sha256",
        "npz_raw_sha256",
        "generator_receipt",
        "metadata_sha256",
    }

    def __init__(
        self,
        metadata_paths: Sequence[Path],
        *,
        split: str,
        expected_split_manifest_sha256: str,
        expected_split_authority_sha256: str,
        expected_split_authority_source_closure_sha256: str,
        expected_generator_config_sha256: str,
        expected_metadata_sha256_by_sample: Mapping[str, str],
    ) -> None:
        if split not in {"train", "validation", "test"}:
            raise ValueError("dataset split is invalid")
        if not _is_sha256(expected_split_manifest_sha256):
            raise ValueError("dataset expected split-manifest digest is invalid")
        if any(
            not _is_sha256(digest)
            for digest in (
                expected_split_authority_sha256,
                expected_split_authority_source_closure_sha256,
                expected_generator_config_sha256,
            )
        ):
            raise ValueError("dataset expected generator authority digest is invalid")
        if type(expected_metadata_sha256_by_sample) is not dict or any(
            not _is_sha256(sample_id) or not _is_sha256(digest)
            for sample_id, digest in expected_metadata_sha256_by_sample.items()
        ):
            raise ValueError("dataset expected shard digest registry is invalid")
        self.rows: list[dict[str, np.ndarray]] = []
        self.metadata: list[dict[str, object]] = []
        identities: list[dict[str, object]] = []
        seen_sample_ids: set[str] = set()
        for metadata_path in sorted(Path(path) for path in metadata_paths):
            metadata_raw = _stable_regular_bytes(metadata_path)

            def reject_duplicates(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError(f"duplicate dataset metadata key: {key}")
                    result[key] = value
                return result

            metadata = json.loads(
                metadata_raw,
                object_pairs_hook=reject_duplicates,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    ValueError(f"non-finite dataset metadata value: {token}")
                ),
            )
            if type(metadata) is not dict or set(metadata) != self.METADATA_FIELDS:
                raise ValueError("dataset metadata exact schema differs")
            if metadata_raw != (
                json.dumps(metadata, sort_keys=True, allow_nan=False).encode("utf-8")
                + b"\n"
            ):
                raise ValueError("dataset metadata is not canonical JSON bytes")
            claimed_metadata_digest = metadata["metadata_sha256"]
            unsigned_metadata = dict(metadata)
            unsigned_metadata.pop("metadata_sha256")
            if (
                not _is_sha256(claimed_metadata_digest)
                or hashlib.sha256(canonical_bytes(unsigned_metadata)).hexdigest()
                != claimed_metadata_digest
            ):
                raise ValueError("dataset metadata digest differs")
            if (
                metadata["schema"] != "route-a1-m5b-hunl-search-sample-v1"
                or metadata["primary_value_target"]
                != "oracle_on_policy_private_values"
                or metadata["primary_policy_target"] != "oracle_actor_policy"
                or metadata["q_cfv_training_authority"] is not False
                or metadata["payoff_unit"] != "chips"
                or metadata["payoff_origin"]
                != "net_from_initial_20000_chip_stack_v1"
            ):
                raise ValueError("dataset label authority/semantics differ")
            if metadata_path.stem != metadata["sample_id"] or not _is_sha256(
                metadata["sample_id"]
            ):
                raise ValueError("dataset sample filename identity differs")
            sample_id = str(metadata["sample_id"])
            if sample_id in seen_sample_ids:
                raise ValueError("dataset repeats one sample identity")
            seen_sample_ids.add(sample_id)
            if (
                expected_metadata_sha256_by_sample.get(sample_id)
                != claimed_metadata_digest
            ):
                raise ValueError("dataset externally bound shard digest differs")
            if metadata["split"] != split:
                raise ValueError("dataset path belongs to a different split")
            if metadata["split_manifest_sha256"] != expected_split_manifest_sha256:
                raise ValueError("dataset split-manifest binding differs")
            for field in (
                "pbs_network_input_sha256",
                "public_family_id",
                "split_manifest_sha256",
                "array_content_sha256",
                "npz_raw_sha256",
            ):
                if not _is_sha256(metadata[field]):
                    raise ValueError(f"dataset {field} is invalid")
            plan = metadata["prelabel_plan"]
            expected_plan_fields = {
                "sample_id",
                "pbs_state_id",
                "public_family_id",
                "trajectory_id",
                "rollout_group_id",
                "augmentation_parent_sample_id",
                "source_copy_group_id",
                "source_checkpoint_digest",
                "decision_index",
                "seed_group",
                "labels_generated",
                "outcome_present",
            }
            if type(plan) is not dict or set(plan) != expected_plan_fields:
                raise ValueError("dataset pre-label plan exact schema differs")
            plan_object = PreLabelPlan(
                sample_id=plan["sample_id"],
                pbs_state_id=plan["pbs_state_id"],
                public_family_id=plan["public_family_id"],
                trajectory_id=plan["trajectory_id"],
                rollout_group_id=plan["rollout_group_id"],
                augmentation_parent_sample_id=plan["augmentation_parent_sample_id"],
                source_copy_group_id=plan["source_copy_group_id"],
                source_checkpoint_digest=plan["source_checkpoint_digest"],
                decision_index=plan["decision_index"],
                seed_group=plan["seed_group"],
            )
            if (
                plan_object.sample_id != sample_id
                or plan_object.pbs_state_id != metadata["pbs_network_input_sha256"]
                or plan_object.public_family_id != metadata["public_family_id"]
            ):
                raise ValueError("dataset pre-label identity binding differs")
            npz_path = metadata_path.with_suffix(".npz")
            npz_raw = _stable_regular_bytes(npz_path)
            if hashlib.sha256(npz_raw).hexdigest() != metadata["npz_raw_sha256"]:
                raise ValueError("dataset shard raw digest differs")
            with np.load(io.BytesIO(npz_raw), allow_pickle=False) as payload:
                expected_array_fields = self.ARRAY_FIELDS + (
                    "diagnostic_unnormalized_cfvs",
                    "diagnostic_conditional_q",
                    "diagnostic_q_valid_mask",
                )
                if payload.files != list(expected_array_fields):
                    raise ValueError("dataset shard exact array schema differs")
                all_arrays = {
                    field: np.array(payload[field], copy=True)
                    for field in expected_array_fields
                }
                row = {field: all_arrays[field] for field in self.ARRAY_FIELDS}
            content_digest = hashlib.sha256()
            for name in sorted(all_arrays):
                array = np.ascontiguousarray(all_arrays[name])
                content_digest.update(name.encode("utf-8") + b"\0")
                content_digest.update(array.dtype.str.encode("ascii") + b"\0")
                content_digest.update(canonical_bytes(list(array.shape)))
                content_digest.update(array.tobytes())
            if content_digest.hexdigest() != metadata["array_content_sha256"]:
                raise ValueError("dataset canonical array content digest differs")
            diagnostic_shapes = {
                "diagnostic_unnormalized_cfvs": (2, HUNL_COMBO_COUNT),
                "diagnostic_conditional_q": (HUNL_COMBO_COUNT, ACTION_COUNT),
                "diagnostic_q_valid_mask": (HUNL_COMBO_COUNT, ACTION_COUNT),
            }
            for field, shape in diagnostic_shapes.items():
                if all_arrays[field].shape != shape:
                    raise ValueError(f"dataset {field} shape differs")
            if (
                all_arrays["diagnostic_unnormalized_cfvs"].dtype != np.float32
                or all_arrays["diagnostic_conditional_q"].dtype != np.float32
                or all_arrays["diagnostic_q_valid_mask"].dtype != np.bool_
            ):
                raise ValueError("dataset diagnostic array dtype differs")
            self._validate_row(row)
            public_state = metadata["pbs_public_state"]
            if not isinstance(public_state, dict) or not np.array_equal(
                encode_public_features(public_state), row["public_features"]
            ):
                raise ValueError("dataset public feature encoding differs")
            replay_payload = dict(public_state)
            replay_payload.update(
                {
                    "hand_number": 1,
                    "hole_cards": [[], []],
                    "match_net_before": [0, 0],
                    "terminal_reason": None,
                    "winner": None,
                }
            )
            try:
                replayed = NationalGameState.from_dict(replay_payload)
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("dataset public state is not Common-replay-valid") from exc
            roundtrip_public = replayed.hand_public_dict()
            roundtrip_public.pop("terminal_reason")
            roundtrip_public.pop("winner")
            if roundtrip_public != public_state:
                raise ValueError("dataset public state differs after Common replay")
            exact_pbs = validate_hunl_network_input(metadata["pbs_network_input"])
            if (
                exact_pbs.network_input_sha256
                != metadata["pbs_network_input_sha256"]
                or exact_pbs.public_state != public_state
                or not np.array_equal(
                    np.asarray(exact_pbs.reach_factors, dtype=np.float32),
                    row["reach_factors"],
                )
                or not np.array_equal(
                    np.asarray(exact_pbs.legal_mask(), dtype=np.bool_),
                    row["legal_combo_mask"],
                )
                or not np.allclose(
                    np.asarray(
                        (
                            exact_pbs.projected_marginal(0),
                            exact_pbs.projected_marginal(1),
                        ),
                        dtype=np.float64,
                    ),
                    row["projected_marginals"],
                    rtol=0.0,
                    atol=0.0,
                )
            ):
                raise ValueError("dataset exact PBS/network tensor binding differs")
            expected_combo_mask = np.asarray(
                legal_combo_mask(public_state["board"]), dtype=np.bool_
            )
            if not np.array_equal(expected_combo_mask, row["legal_combo_mask"]):
                raise ValueError("dataset legal combo mask differs from public board")
            if int(row["actor"]) != public_state["actor"]:
                raise ValueError("dataset actor differs from Common public actor")
            action_support = metadata["action_support"]
            if (
                type(action_support) is not dict
                or set(action_support)
                != {
                    "public_state_id",
                    "slot_names",
                    "action_wires",
                    "legal_mask",
                    "exact_offtree_slot",
                    "nearest_action_translation_used",
                }
                or action_support["slot_names"] != list(ACTION_SLOTS)
                or action_support["legal_mask"]
                != row["legal_action_mask"].tolist()
                or not isinstance(action_support["action_wires"], list)
                or len(action_support["action_wires"]) != ACTION_COUNT
                or action_support["nearest_action_translation_used"] is not False
            ):
                raise ValueError("dataset action support mask differs")
            wires = action_support["action_wires"]
            if any(
                (wire is None) != (not bool(row["legal_action_mask"][slot]))
                or (wire is not None and type(wire) is not str)
                for slot, wire in enumerate(wires)
            ) or len([wire for wire in wires if wire is not None]) != len(
                {wire for wire in wires if wire is not None}
            ):
                raise ValueError("dataset action support wires differ")
            public_state_id = hashlib.sha256(canonical_bytes(public_state)).hexdigest()
            if action_support["public_state_id"] != public_state_id:
                raise ValueError("dataset action support public state binding differs")
            exact_offtree_slot = action_support["exact_offtree_slot"]
            if exact_offtree_slot not in (None, 7):
                raise ValueError("dataset exact off-tree slot differs")
            exact_offtree_action = (
                None
                if exact_offtree_slot is None
                else Action.from_wire(action_support["action_wires"][7])
            )
            if abstract_actions(
                replayed, exact_offtree_action=exact_offtree_action
            ).snapshot() != action_support:
                raise ValueError("dataset action support differs from Common legality")
            if (
                plan["labels_generated"] is not False
                or plan["outcome_present"] is not False
            ):
                raise ValueError("dataset pre-label provenance differs")
            weighted_zero_sum_residual_chips = math.fsum(
                float(row["projected_marginals"][player, hand])
                * float(row["oracle_on_policy_private_values"][player, hand])
                for player in (0, 1)
                for hand in range(HUNL_COMBO_COUNT)
            )
            validate_generator_receipt(
                metadata["generator_receipt"],
                expected_sample_id=sample_id,
                expected_pbs_network_input_sha256=metadata[
                    "pbs_network_input_sha256"
                ],
                expected_split_manifest_sha256=expected_split_manifest_sha256,
                expected_split_authority_sha256=expected_split_authority_sha256,
                expected_split_authority_source_closure_sha256=(
                    expected_split_authority_source_closure_sha256
                ),
                expected_generator_config_sha256=expected_generator_config_sha256,
                expected_source_checkpoint_sha256=plan_object.source_checkpoint_digest,
                expected_array_content_sha256=metadata["array_content_sha256"],
                weighted_zero_sum_residual_chips=weighted_zero_sum_residual_chips,
            )
            self.rows.append(row)
            self.metadata.append(metadata)
            identities.append(
                {
                    "sample_id": metadata["sample_id"],
                    "metadata_sha256": metadata["metadata_sha256"],
                    "npz_raw_sha256": metadata["npz_raw_sha256"],
                }
            )
        if not self.rows:
            raise ValueError(f"dataset split {split} is empty")
        loaded_ids = {str(metadata["sample_id"]) for metadata in self.metadata}
        if loaded_ids != set(expected_metadata_sha256_by_sample):
            raise ValueError("dataset externally bound shard set differs")
        self.split = split
        self.digest = hashlib.sha256(
            canonical_bytes(
                {
                    "schema": "route-a1-m5b-loaded-shard-set-v1",
                    "split": split,
                    "identities": identities,
                }
            )
        ).hexdigest()

    @staticmethod
    def _validate_row(row: Mapping[str, np.ndarray]) -> None:
        shapes = {
            "public_features": (PUBLIC_FEATURE_DIM,),
            "actor": (),
            "reach_factors": (2, HUNL_COMBO_COUNT),
            "legal_combo_mask": (HUNL_COMBO_COUNT,),
            "legal_action_mask": (ACTION_COUNT,),
            "oracle_on_policy_private_values": (2, HUNL_COMBO_COUNT),
            "oracle_value_valid_mask": (2, HUNL_COMBO_COUNT),
            "projected_marginals": (2, HUNL_COMBO_COUNT),
            "oracle_actor_policy": (HUNL_COMBO_COUNT, ACTION_COUNT),
        }
        expected_dtypes = {
            "public_features": np.dtype("float32"),
            "actor": np.dtype("int8"),
            "reach_factors": np.dtype("float32"),
            "legal_combo_mask": np.dtype("bool"),
            "legal_action_mask": np.dtype("bool"),
            "oracle_on_policy_private_values": np.dtype("float64"),
            "oracle_value_valid_mask": np.dtype("bool"),
            "projected_marginals": np.dtype("float64"),
            "oracle_actor_policy": np.dtype("float32"),
        }
        for field, shape in shapes.items():
            if row[field].shape != shape:
                raise ValueError(f"dataset {field} shape differs")
            if row[field].dtype != expected_dtypes[field]:
                raise ValueError(f"dataset {field} dtype differs")
            if row[field].dtype.kind == "f" and not np.all(np.isfinite(row[field])):
                raise ValueError(f"dataset {field} is non-finite")
        actor = int(row["actor"])
        if actor not in (0, 1):
            raise ValueError("dataset actor differs")
        if not np.any(row["legal_action_mask"]):
            raise ValueError("dataset action support is empty")
        target = row["oracle_actor_policy"]
        if np.any(target < 0.0) or np.any(target[:, ~row["legal_action_mask"]] != 0.0):
            raise ValueError("dataset policy target leaks illegal action mass")
        valid_rows = row["legal_combo_mask"]
        if not np.any(valid_rows):
            raise ValueError("dataset has no legal private-hand support")
        if not np.allclose(target[valid_rows].sum(axis=1), 1.0, atol=1e-6, rtol=0.0):
            raise ValueError("dataset actor policy rows are not normalized")
        marginals = row["projected_marginals"]
        if np.any(marginals < 0.0) or not np.allclose(
            marginals.sum(axis=1), 1.0, atol=1e-6, rtol=0.0
        ) or np.any(marginals[:, ~valid_rows] != 0.0):
            raise ValueError("dataset projected marginals are invalid")
        reach = row["reach_factors"]
        if (
            np.any(reach < 0.0)
            or not np.allclose(reach.sum(axis=1), 1.0, atol=1e-6, rtol=0.0)
            or np.any(reach[:, ~valid_rows] != 0.0)
        ):
            raise ValueError("dataset reach factors are invalid")
        if not np.array_equal(
            row["oracle_value_valid_mask"], marginals > 0.0
        ):
            raise ValueError("dataset value-valid mask differs from target support")
        weighted_zero_sum = math.fsum(
            float(marginals[player, hand])
            * float(row["oracle_on_policy_private_values"][player, hand])
            for player in (0, 1)
            for hand in range(HUNL_COMBO_COUNT)
        )
        if abs(weighted_zero_sum) > 1e-5:
            raise ValueError("dataset primary value targets violate weighted zero sum")

    def __len__(self) -> int:
        return len(self.rows)

    def batch(self, indices: Sequence[int], device: torch.device) -> dict[str, Tensor]:
        result: dict[str, Tensor] = {}
        for field in self.ARRAY_FIELDS:
            array = np.stack([self.rows[int(index)][field] for index in indices])
            result[field] = torch.from_numpy(array).to(device=device)
        return result


def _loss(model: M5BValuePolicyNet, batch: Mapping[str, Tensor], brier_weight: float) -> tuple[Tensor, dict[str, float]]:
    value_scaled, logits = model(
        batch["public_features"].float(),
        batch["reach_factors"].float(),
        batch["legal_combo_mask"].bool(),
        batch["legal_action_mask"].bool(),
    )
    target_value = batch["oracle_on_policy_private_values"].float() / VALUE_SCALE_CHIPS
    valid = batch["oracle_value_valid_mask"].bool()
    marginals = batch["projected_marginals"].float()
    if (
        not bool(torch.all(torch.isfinite(target_value)))
        or not bool(torch.all(torch.isfinite(marginals)))
        or bool(torch.any(marginals < 0.0))
        or not bool(
            torch.allclose(
                marginals.sum(dim=-1),
                torch.ones_like(marginals.sum(dim=-1)),
                rtol=0.0,
                atol=1e-6,
            )
        )
    ):
        raise ValueError("batch value targets/projected marginals are invalid")
    weights = marginals * valid.float()
    if not bool(torch.all(weights.sum(dim=-1) > 0.0)):
        raise ValueError("batch value target support is empty")
    pointwise = F.smooth_l1_loss(value_scaled, target_value, reduction="none")
    value_loss = (pointwise * weights).sum() / weights.sum().clamp_min(1e-12)

    target_policy = batch["oracle_actor_policy"].float()
    combo_valid = batch["legal_combo_mask"].bool()
    legal_actions = batch["legal_action_mask"].bool()
    if (
        not bool(torch.all(torch.isfinite(target_policy)))
        or bool(torch.any(target_policy < 0.0))
        or bool(
            torch.any(
                target_policy.masked_select(~legal_actions[:, None, :]) != 0.0
            )
        )
        or not bool(
            torch.allclose(
                target_policy.sum(dim=-1).masked_select(combo_valid),
                torch.ones_like(target_policy.sum(dim=-1).masked_select(combo_valid)),
                rtol=0.0,
                atol=1e-6,
            )
        )
    ):
        raise ValueError("batch actor-policy target support is invalid")
    log_probs = F.log_softmax(logits, dim=-1)
    probabilities = log_probs.exp()
    ce_rows = -(target_policy * log_probs).sum(dim=-1)
    raw_actors = batch["actor"]
    actors = raw_actors.long()
    if (
        raw_actors.dtype
        not in (torch.int8, torch.int16, torch.int32, torch.int64, torch.uint8)
        or actors.ndim != 1
        or torch.any((actors < 0) | (actors > 1))
    ):
        raise ValueError("batch actor indices are invalid")
    actor_marginals = marginals[
        torch.arange(actors.shape[0], device=actors.device), actors
    ]
    policy_weights = actor_marginals * combo_valid.float()
    policy_ce = (ce_rows * policy_weights).sum() / policy_weights.sum().clamp_min(1e-12)
    brier_rows = ((probabilities - target_policy) ** 2).sum(dim=-1)
    brier = (brier_rows * policy_weights).sum() / policy_weights.sum().clamp_min(1e-12)
    total = value_loss + policy_ce + float(brier_weight) * brier
    return total, {
        "total": float(total.detach().cpu()),
        "value_huber": float(value_loss.detach().cpu()),
        "policy_ce": float(policy_ce.detach().cpu()),
        "policy_brier": float(brier.detach().cpu()),
    }


def _tensor_digest(state: Mapping[str, Tensor]) -> str:
    digest = hashlib.sha256()
    for name, tensor in sorted(state.items()):
        value = tensor.detach().cpu().contiguous()
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(str(value.dtype).encode("ascii") + b"\0")
        digest.update(canonical_bytes(list(value.shape)))
        digest.update(value.numpy().tobytes())
    return digest.hexdigest()


def _state_digest(value: object) -> str:
    digest = hashlib.sha256()

    def update(item: object) -> None:
        if torch.is_tensor(item):
            tensor = item.detach().cpu().contiguous()
            digest.update(b"tensor\0" + str(tensor.dtype).encode("ascii") + b"\0")
            digest.update(canonical_bytes(list(tensor.shape)))
            digest.update(tensor.numpy().tobytes())
        elif isinstance(item, dict):
            digest.update(b"dict\0")
            for key in sorted(item, key=str):
                update(str(key))
                update(item[key])
        elif isinstance(item, (list, tuple)):
            digest.update(b"sequence\0")
            for child in item:
                update(child)
        elif isinstance(item, np.ndarray):
            array = np.ascontiguousarray(item)
            digest.update(b"numpy\0" + array.dtype.str.encode("ascii") + b"\0")
            digest.update(canonical_bytes(list(array.shape)))
            digest.update(array.tobytes())
        else:
            digest.update(canonical_bytes(item))

    update(value)
    return digest.hexdigest()


def _checkpoint_snapshot(value: object) -> object:
    """Detach a checkpoint from all live trainer/model/optimizer references."""

    if torch.is_tensor(value):
        return value.detach().cpu().contiguous().clone()
    if isinstance(value, dict):
        return {key: _checkpoint_snapshot(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_checkpoint_snapshot(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_checkpoint_snapshot(child) for child in value)
    return copy.deepcopy(value)


@dataclass(slots=True)
class TrainingCursor:
    epoch: int
    batch_cursor: int
    global_step: int
    permutation: list[int]


class DeterministicTrainer:
    def __init__(
        self,
        model: M5BValuePolicyNet,
        dataset: ShardDataset,
        *,
        training_config: Mapping[str, object],
        seed: int,
        config_digest: str,
        runtime_binding: Mapping[str, object],
        device: torch.device,
    ) -> None:
        if (
            type(device) is not torch.device
            or device.type != "cuda"
            or device.index != 0
            or training_config.get("device_required") != "cuda"
        ):
            raise ValueError("M5b training requires the fixed cuda:0 device")
        configure_deterministic_runtime()
        observed_runtime = verify_runtime_binding(runtime_binding)
        self.model = model.to(device)
        self.dataset = dataset
        self.config = dict(training_config)
        self.device = device
        self.optimizer = torch.optim.Adam(
            self.model.parameters(),
            lr=float(training_config["learning_rate"]),
            weight_decay=float(training_config["weight_decay"]),
            foreach=False,
            fused=False,
        )
        self.scheduler = None
        self.scaler = None
        self.loader_generator = torch.Generator(device="cpu")
        self.loader_generator.manual_seed(int(seed))
        self.cursor = TrainingCursor(0, 0, 0, self._new_permutation())
        self.config_digest = config_digest
        self.runtime_binding = observed_runtime
        self.runtime_digest = hashlib.sha256(
            canonical_bytes(observed_runtime)
        ).hexdigest()
        self.loss_history: list[dict[str, float]] = []

    def _new_permutation(self) -> list[int]:
        return torch.randperm(
            len(self.dataset), generator=self.loader_generator
        ).tolist()

    @property
    def batches_per_epoch(self) -> int:
        batch_size = int(self.config["batch_size"])
        return (len(self.dataset) + batch_size - 1) // batch_size

    def step(self) -> dict[str, float]:
        if self.cursor.batch_cursor >= len(self.cursor.permutation):
            self.cursor.epoch += 1
            self.cursor.batch_cursor = 0
            self.cursor.permutation = self._new_permutation()
        batch_size = int(self.config["batch_size"])
        start = self.cursor.batch_cursor
        indices = self.cursor.permutation[start : start + batch_size]
        batch = self.dataset.batch(indices, self.device)
        self.model.train()
        self.optimizer.zero_grad(set_to_none=True)
        loss, metrics = _loss(
            self.model, batch, float(self.config["policy_brier_aux_weight"])
        )
        loss.backward()
        torch.nn.utils.clip_grad_norm_(
            self.model.parameters(), float(self.config["gradient_clip_norm"])
        )
        self.optimizer.step()
        self.cursor.batch_cursor += len(indices)
        self.cursor.global_step += 1
        self.loss_history.append(metrics)
        return metrics

    def run_steps(self, steps: int) -> None:
        for _ in range(int(steps)):
            self.step()

    def run_epochs(self, epochs: int) -> None:
        target = self.cursor.global_step + int(epochs) * self.batches_per_epoch
        self.run_steps(target - self.cursor.global_step)

    def checkpoint(self) -> dict[str, object]:
        numpy_state = np.random.get_state()
        return {
            "schema": TRAINING_CHECKPOINT_SCHEMA,
            "model": _checkpoint_snapshot(self.model.state_dict()),
            "optimizer": _checkpoint_snapshot(self.optimizer.state_dict()),
            "scheduler": None,
            "scaler": None,
            "cursor": {
                "epoch": self.cursor.epoch,
                "batch_cursor": self.cursor.batch_cursor,
                "global_step": self.cursor.global_step,
                "permutation": list(self.cursor.permutation),
            },
            "cpu_rng": torch.get_rng_state(),
            "cuda_rng_all": torch.cuda.get_rng_state_all(),
            "numpy_rng": {
                "algorithm": numpy_state[0],
                "keys": numpy_state[1].astype(np.uint32).tolist(),
                "position": int(numpy_state[2]),
                "has_gauss": int(numpy_state[3]),
                "cached_gaussian": float(numpy_state[4]),
            },
            "python_rng": random.getstate(),
            "loader_generator_rng": self.loader_generator.get_state(),
            "config_digest": self.config_digest,
            "dataset_digest": self.dataset.digest,
            "runtime_digest": self.runtime_digest,
            "loss_history": copy.deepcopy(self.loss_history),
        }

    def restore(self, payload: Mapping[str, object]) -> None:
        if type(payload) is not dict or set(payload) != TRAINING_CHECKPOINT_FIELDS:
            raise ValueError("training checkpoint exact schema differs")
        if payload.get("schema") != TRAINING_CHECKPOINT_SCHEMA:
            raise ValueError("training checkpoint schema differs")
        for field, expected in (
            ("config_digest", self.config_digest),
            ("dataset_digest", self.dataset.digest),
            ("runtime_digest", self.runtime_digest),
        ):
            if payload.get(field) != expected:
                raise ValueError(f"training checkpoint {field} differs")
        model_state = payload["model"]
        expected_model = self.model.state_dict()
        if not isinstance(model_state, dict) or set(model_state) != set(expected_model):
            raise ValueError("training checkpoint model tensor names differ")
        for name, expected in expected_model.items():
            observed = model_state[name]
            if (
                not torch.is_tensor(observed)
                or observed.shape != expected.shape
                or observed.dtype != expected.dtype
                or observed.layout != torch.strided
                or not bool(torch.all(torch.isfinite(observed)))
            ):
                raise ValueError(f"training checkpoint model tensor differs: {name}")
        optimizer_state = payload["optimizer"]
        if (
            not isinstance(optimizer_state, dict)
            or set(optimizer_state) != {"state", "param_groups"}
            or not isinstance(optimizer_state["state"], dict)
            or not isinstance(optimizer_state["param_groups"], list)
            or len(optimizer_state["param_groups"]) != 1
        ):
            raise ValueError("training checkpoint optimizer schema differs")
        if payload["scheduler"] is not None or payload["scaler"] is not None:
            raise ValueError("training checkpoint scheduler/scaler differs")
        cursor = payload["cursor"]
        if not isinstance(cursor, dict) or set(cursor) != {
            "epoch",
            "batch_cursor",
            "global_step",
            "permutation",
        }:
            raise ValueError("training checkpoint cursor schema differs")
        for field in ("epoch", "batch_cursor", "global_step"):
            if type(cursor[field]) is not int or cursor[field] < 0:
                raise ValueError(f"training checkpoint cursor {field} differs")
        permutation = cursor["permutation"]
        if (
            not isinstance(permutation, list)
            or any(type(index) is not int for index in permutation)
            or sorted(permutation) != list(range(len(self.dataset)))
        ):
            raise ValueError("training checkpoint sampler permutation differs")
        batch_size = int(self.config["batch_size"])
        valid_cursors = {
            min(batch * batch_size, len(self.dataset))
            for batch in range(self.batches_per_epoch + 1)
        }
        if cursor["batch_cursor"] not in valid_cursors:
            raise ValueError("training checkpoint batch cursor is not a boundary")
        processed_batches = (
            0
            if cursor["batch_cursor"] == 0
            else (cursor["batch_cursor"] + batch_size - 1) // batch_size
        )
        if cursor["global_step"] != (
            cursor["epoch"] * self.batches_per_epoch + processed_batches
        ):
            raise ValueError("training checkpoint global step/cursor disagree")
        expected_optimizer = self.optimizer.state_dict()
        observed_group = optimizer_state["param_groups"][0]
        expected_group = expected_optimizer["param_groups"][0]
        if type(observed_group) is not dict or observed_group != expected_group:
            raise ValueError("training checkpoint Adam parameter group differs")
        parameter_ids = expected_group["params"]
        observed_states = optimizer_state["state"]
        expected_state_ids = set(parameter_ids) if cursor["global_step"] else set()
        if set(observed_states) != expected_state_ids:
            raise ValueError("training checkpoint Adam state IDs differ")
        parameters = list(self.model.parameters())
        if len(parameters) != len(parameter_ids):
            raise AssertionError("model/optimizer parameter registry differs")
        for parameter_id, parameter in zip(parameter_ids, parameters, strict=True):
            if cursor["global_step"] == 0:
                continue
            state = observed_states[parameter_id]
            if type(state) is not dict or set(state) != {
                "step",
                "exp_avg",
                "exp_avg_sq",
            }:
                raise ValueError("training checkpoint Adam tensor schema differs")
            step = state["step"]
            if (
                not torch.is_tensor(step)
                or step.shape != ()
                or step.dtype != torch.float32
                or float(step) != float(cursor["global_step"])
            ):
                raise ValueError("training checkpoint Adam step differs")
            for name in ("exp_avg", "exp_avg_sq"):
                tensor = state[name]
                if (
                    not torch.is_tensor(tensor)
                    or tensor.shape != parameter.shape
                    or tensor.dtype != parameter.dtype
                    or tensor.layout != torch.strided
                    or not bool(torch.all(torch.isfinite(tensor)))
                    or (name == "exp_avg_sq" and bool(torch.any(tensor < 0.0)))
                ):
                    raise ValueError(f"training checkpoint Adam {name} differs")
        cpu_rng = payload["cpu_rng"]
        loader_rng = payload["loader_generator_rng"]
        cuda_rng = payload["cuda_rng_all"]
        expected_cpu_rng_size = torch.get_rng_state().numel()
        expected_loader_rng_size = self.loader_generator.get_state().numel()
        expected_cuda_rng_sizes = [state.numel() for state in torch.cuda.get_rng_state_all()]
        if (
            not torch.is_tensor(cpu_rng)
            or cpu_rng.dtype != torch.uint8
            or cpu_rng.ndim != 1
            or cpu_rng.numel() != expected_cpu_rng_size
            or not torch.is_tensor(loader_rng)
            or loader_rng.dtype != torch.uint8
            or loader_rng.ndim != 1
            or loader_rng.numel() != expected_loader_rng_size
            or not isinstance(cuda_rng, list)
            or len(cuda_rng) != torch.cuda.device_count()
            or any(
                not torch.is_tensor(item)
                or item.dtype != torch.uint8
                or item.ndim != 1
                or item.numel() != expected_cuda_rng_sizes[index]
                for index, item in enumerate(cuda_rng)
            )
        ):
            raise ValueError("training checkpoint RNG tensor schema differs")
        numpy_rng = payload["numpy_rng"]
        if not isinstance(numpy_rng, dict) or set(numpy_rng) != {
            "algorithm",
            "keys",
            "position",
            "has_gauss",
            "cached_gaussian",
        }:
            raise ValueError("training checkpoint NumPy RNG schema differs")
        if (
            numpy_rng["algorithm"] != "MT19937"
            or not isinstance(numpy_rng["keys"], list)
            or len(numpy_rng["keys"]) != 624
            or any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in numpy_rng["keys"])
            or type(numpy_rng["position"]) is not int
            or not 0 <= numpy_rng["position"] <= 624
            or type(numpy_rng["has_gauss"]) is not int
            or numpy_rng["has_gauss"] not in (0, 1)
            or type(numpy_rng["cached_gaussian"]) is not float
            or not np.isfinite(numpy_rng["cached_gaussian"])
        ):
            raise ValueError("training checkpoint NumPy RNG values differ")
        python_probe = random.Random()
        try:
            python_probe.setstate(payload["python_rng"])
        except (TypeError, ValueError) as exc:
            raise ValueError("training checkpoint Python RNG differs") from exc
        history = payload["loss_history"]
        if not isinstance(history, list) or len(history) != cursor["global_step"]:
            raise ValueError("training checkpoint loss history length differs")
        for row in history:
            if (
                not isinstance(row, dict)
                or set(row) != {"total", "value_huber", "policy_ce", "policy_brier"}
                or any(type(value) is not float or not np.isfinite(value) for value in row.values())
            ):
                raise ValueError("training checkpoint loss history differs")

        self.model.load_state_dict(model_state, strict=True)
        self.optimizer.load_state_dict(optimizer_state)
        group = self.optimizer.param_groups[0]
        if (
            float(group["lr"]) != float(self.config["learning_rate"])
            or float(group["weight_decay"]) != float(self.config["weight_decay"])
            or group.get("foreach") is not False
            or group.get("fused") is not False
        ):
            raise ValueError("restored Adam configuration differs")
        self.cursor = TrainingCursor(
            int(cursor["epoch"]),
            int(cursor["batch_cursor"]),
            int(cursor["global_step"]),
            list(permutation),
        )
        torch.set_rng_state(cpu_rng)
        torch.cuda.set_rng_state_all(cuda_rng)
        np.random.set_state(
            (
                numpy_rng["algorithm"],
                np.asarray(numpy_rng["keys"], dtype=np.uint32),
                numpy_rng["position"],
                numpy_rng["has_gauss"],
                numpy_rng["cached_gaussian"],
            )
        )
        random.setstate(payload["python_rng"])
        self.loader_generator.set_state(loader_rng)
        self.loss_history = copy.deepcopy(history)

    def complete_digest(self) -> str:
        checkpoint = self.checkpoint()
        return _state_digest(checkpoint)


def atomic_torch_save(path: Path, payload: Mapping[str, object]) -> str:
    _reject_symlink_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(descriptor)
    try:
        torch.save(dict(payload), temporary)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            temporary_raw = _stable_regular_bytes(Path(temporary))
            if _stable_regular_bytes(path) != temporary_raw:
                raise ValueError(f"training checkpoint path collision: {path}")
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return hashlib.sha256(_stable_regular_bytes(path)).hexdigest()


def load_training_checkpoint(
    path: Path, *, expected_raw_sha256: str
) -> Mapping[str, object]:
    if not _is_sha256(expected_raw_sha256):
        raise ValueError("checkpoint expected raw digest is invalid")
    raw = _stable_regular_bytes(path)
    if hashlib.sha256(raw).hexdigest() != expected_raw_sha256:
        raise ValueError("checkpoint raw digest differs")
    payload = torch.load(io.BytesIO(raw), map_location="cpu", weights_only=True)
    if type(payload) is not dict or set(payload) != TRAINING_CHECKPOINT_FIELDS:
        raise ValueError("checkpoint payload exact schema differs")
    if payload.get("schema") != TRAINING_CHECKPOINT_SCHEMA:
        raise ValueError("checkpoint payload schema differs")
    return payload


def _process_peak_rss_bytes() -> int:
    # Linux reports ru_maxrss in KiB. This repository's supported training host
    # is Linux; the field is named as a process-lifetime high-water mark below.
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024


def measure_training_resources(
    trainer: DeterministicTrainer, *, steps: int
) -> dict[str, object]:
    """Run explicitly requested steps and return an honest CUDA resource receipt."""

    if type(steps) is not int or steps <= 0:
        raise ValueError("resource measurement steps must be a positive integer")
    if trainer.device.type != "cuda":
        raise ValueError("formal M5b training resource measurement requires CUDA")
    observed_runtime = verify_runtime_binding(trainer.runtime_binding)
    if hashlib.sha256(canonical_bytes(observed_runtime)).hexdigest() != trainer.runtime_digest:
        raise RuntimeError("trainer runtime binding changed before measurement")
    device = trainer.device
    torch.cuda.synchronize(device)
    torch.cuda.reset_peak_memory_stats(device)
    rss_before = _process_peak_rss_bytes()
    start_global_step = trainer.cursor.global_step
    start_tensor_digest = _tensor_digest(trainer.model.state_dict())
    samples = 0
    started = time.perf_counter_ns()
    for _ in range(steps):
        cursor = trainer.cursor.batch_cursor
        if cursor >= len(trainer.dataset):
            cursor = 0
        samples += min(int(trainer.config["batch_size"]), len(trainer.dataset) - cursor)
        trainer.step()
    torch.cuda.synchronize(device)
    elapsed_seconds = (time.perf_counter_ns() - started) / 1_000_000_000.0
    if elapsed_seconds <= 0.0:
        raise RuntimeError("training resource clock did not advance")
    return {
        "schema": "route-a1-m5b-training-resource-receipt-v1",
        "device": str(device),
        "config_sha256": trainer.config_digest,
        "dataset_sha256": trainer.dataset.digest,
        "runtime_sha256": trainer.runtime_digest,
        "start_global_step": start_global_step,
        "end_global_step": trainer.cursor.global_step,
        "start_model_tensor_sha256": start_tensor_digest,
        "end_model_tensor_sha256": _tensor_digest(trainer.model.state_dict()),
        "steps": steps,
        "samples": samples,
        "elapsed_wall_seconds": elapsed_seconds,
        "gpu_seconds": elapsed_seconds,
        "gpu_seconds_semantics": (
            "single_cuda_device_synchronized_wall_envelope_not_utilization_time_v1"
        ),
        "steps_per_second": steps / elapsed_seconds,
        "samples_per_second": samples / elapsed_seconds,
        "peak_cuda_allocated_bytes": int(torch.cuda.max_memory_allocated(device)),
        "peak_cuda_reserved_bytes": int(torch.cuda.max_memory_reserved(device)),
        "process_peak_rss_before_bytes": rss_before,
        "process_peak_rss_after_bytes": _process_peak_rss_bytes(),
    }


def measure_inference_resources(
    model: M5BValuePolicyNet,
    *,
    model_inputs: Sequence[PublicInferenceInput],
    runtime_binding: Mapping[str, object],
    warmup: int = 10,
    repeats: int = 100,
) -> dict[str, object]:
    """Measure public encode through D2H value+policy latency on one CUDA model."""

    if type(warmup) is not int or warmup < 0:
        raise ValueError("inference warmup must be a nonnegative integer")
    if type(repeats) is not int or repeats <= 0:
        raise ValueError("inference repeats must be a positive integer")
    observed_runtime = verify_runtime_binding(runtime_binding)
    runtime_sha256 = hashlib.sha256(canonical_bytes(observed_runtime)).hexdigest()
    if (
        not isinstance(model_inputs, Sequence)
        or not model_inputs
        or any(type(item) is not PublicInferenceInput for item in model_inputs)
    ):
        raise ValueError("inference measurement requires public-only model inputs")
    try:
        device = next(model.parameters()).device
    except StopIteration as exc:
        raise ValueError("inference model has no parameters") from exc
    if device.type != "cuda":
        raise ValueError("M5b inference resource measurement requires CUDA")
    batch_size = len(model_inputs)

    def end_to_end_forward() -> tuple[np.ndarray, np.ndarray]:
        public = torch.from_numpy(
            np.stack(
                [encode_public_features(item.public_state) for item in model_inputs]
            )
        ).to(device=device)
        reach = torch.from_numpy(
            np.stack(
                [
                    np.asarray(item.reach_factors, dtype=np.float32)
                    for item in model_inputs
                ]
            )
        ).to(device=device)
        combo = torch.from_numpy(
            np.stack(
                [
                    np.asarray(item.legal_combo_mask, dtype=np.bool_)
                    for item in model_inputs
                ]
            )
        ).to(device=device)
        action = torch.from_numpy(
            np.stack(
                [
                    np.asarray(item.legal_action_mask, dtype=np.bool_)
                    for item in model_inputs
                ]
            )
        ).to(device=device)
        values, logits = model(public, reach, combo, action)
        policies = torch.softmax(logits, dim=-1)
        return (
            values.float().cpu().numpy() * VALUE_SCALE_CHIPS,
            policies.float().cpu().numpy(),
        )

    model.eval()
    model_tensor_digest = _tensor_digest(model.state_dict())
    with torch.inference_mode():
        values, policies = end_to_end_forward()
        for _ in range(warmup):
            values, policies = end_to_end_forward()
        torch.cuda.synchronize(device)
        if values.shape != (batch_size, 2, HUNL_COMBO_COUNT) or policies.shape != (
            batch_size,
            HUNL_COMBO_COUNT,
            ACTION_COUNT,
        ) or not np.all(np.isfinite(values)) or not np.all(np.isfinite(policies)):
            raise RuntimeError("inference resource probe output shape differs")
        for index, model_input in enumerate(model_inputs):
            if (
                np.any(policies[index, :, ~model_input.legal_action_mask] != 0.0)
                or not np.allclose(
                    policies[index].sum(axis=1), 1.0, rtol=0.0, atol=1e-6
                )
            ):
                raise RuntimeError("inference resource probe policy support differs")
        torch.cuda.reset_peak_memory_stats(device)
        rss_before = _process_peak_rss_bytes()
        latencies_ms: list[float] = []
        started_total = time.perf_counter_ns()
        for _ in range(repeats):
            torch.cuda.synchronize(device)
            started = time.perf_counter_ns()
            end_to_end_forward()
            torch.cuda.synchronize(device)
            latencies_ms.append((time.perf_counter_ns() - started) / 1_000_000.0)
        elapsed_total = (time.perf_counter_ns() - started_total) / 1_000_000_000.0
    latency = np.asarray(latencies_ms, dtype=np.float64)
    median_ms = float(np.median(latency))
    return {
        "schema": "route-a1-m5b-inference-resource-receipt-v1",
        "device": str(device),
        "model_tensor_sha256": model_tensor_digest,
        "runtime_sha256": runtime_sha256,
        "batch_size": batch_size,
        "warmup_runs": warmup,
        "measured_runs": repeats,
        "latency_semantics": (
            "public_json_feature_encode_h2d_value_policy_softmax_d2h_cuda_sync_v1"
        ),
        "latency_median_ms": median_ms,
        "latency_p95_ms": float(np.percentile(latency, 95.0)),
        "latency_min_ms": float(np.min(latency)),
        "latency_max_ms": float(np.max(latency)),
        "public_states_per_second_at_median": batch_size * 1000.0 / median_ms,
        "elapsed_wall_seconds": elapsed_total,
        "gpu_seconds": elapsed_total,
        "gpu_seconds_semantics": (
            "single_cuda_device_synchronized_wall_envelope_not_utilization_time_v1"
        ),
        "peak_cuda_allocated_bytes": int(torch.cuda.max_memory_allocated(device)),
        "peak_cuda_reserved_bytes": int(torch.cuda.max_memory_reserved(device)),
        "process_peak_rss_before_bytes": rss_before,
        "process_peak_rss_after_bytes": _process_peak_rss_bytes(),
    }


def _validate_resource_measurements(
    training: Mapping[str, object], inference: Mapping[str, object]
) -> None:
    semantics = "single_cuda_device_synchronized_wall_envelope_not_utilization_time_v1"
    if type(training) is not dict or set(training) != TRAINING_RESOURCE_FIELDS:
        raise ValueError("training resource receipt exact schema differs")
    if type(inference) is not dict or set(inference) != INFERENCE_RESOURCE_FIELDS:
        raise ValueError("inference resource receipt exact schema differs")
    if (
        training["schema"] != "route-a1-m5b-training-resource-receipt-v1"
        or inference["schema"] != "route-a1-m5b-inference-resource-receipt-v1"
        or training["device"] != "cuda:0"
        or inference["device"] != "cuda:0"
        or training["gpu_seconds_semantics"] != semantics
        or inference["gpu_seconds_semantics"] != semantics
        or inference["latency_semantics"]
        != "public_json_feature_encode_h2d_value_policy_softmax_d2h_cuda_sync_v1"
    ):
        raise ValueError("resource receipt measurement semantics differ")
    for field in (
        "config_sha256",
        "dataset_sha256",
        "runtime_sha256",
        "start_model_tensor_sha256",
        "end_model_tensor_sha256",
    ):
        if not _is_sha256(training[field]):
            raise ValueError(f"training resource receipt {field} is invalid")
    for field in ("model_tensor_sha256", "runtime_sha256"):
        if not _is_sha256(inference[field]):
            raise ValueError(f"inference resource receipt {field} is invalid")
    for field in ("start_global_step", "end_global_step", "steps", "samples"):
        if type(training[field]) is not int or training[field] < 0:
            raise ValueError(f"training resource receipt {field} is invalid")
    if (
        training["steps"] <= 0
        or training["samples"] <= 0
        or training["end_global_step"] - training["start_global_step"]
        != training["steps"]
    ):
        raise ValueError("training resource receipt counter relation differs")
    for field in ("batch_size", "warmup_runs", "measured_runs"):
        if type(inference[field]) is not int or inference[field] < 0:
            raise ValueError(f"inference resource receipt {field} is invalid")
    if inference["batch_size"] <= 0 or inference["measured_runs"] <= 0:
        raise ValueError("inference resource receipt counters are empty")

    training_floats = (
        "elapsed_wall_seconds",
        "gpu_seconds",
        "steps_per_second",
        "samples_per_second",
    )
    inference_floats = (
        "latency_median_ms",
        "latency_p95_ms",
        "latency_min_ms",
        "latency_max_ms",
        "public_states_per_second_at_median",
        "elapsed_wall_seconds",
        "gpu_seconds",
    )
    for receipt, fields in ((training, training_floats), (inference, inference_floats)):
        for field in fields:
            if type(receipt[field]) is not float or not math.isfinite(receipt[field]):
                raise ValueError(f"resource receipt {field} is not a finite float")
            if receipt[field] <= 0.0:
                raise ValueError(f"resource receipt {field} must be positive")
    integer_fields = (
        "peak_cuda_allocated_bytes",
        "peak_cuda_reserved_bytes",
        "process_peak_rss_before_bytes",
        "process_peak_rss_after_bytes",
    )
    for receipt in (training, inference):
        for field in integer_fields:
            if type(receipt[field]) is not int or receipt[field] < 0:
                raise ValueError(f"resource receipt {field} is invalid")
        if receipt["peak_cuda_reserved_bytes"] < receipt["peak_cuda_allocated_bytes"]:
            raise ValueError("resource receipt CUDA peak relation differs")
        if receipt["process_peak_rss_after_bytes"] < receipt[
            "process_peak_rss_before_bytes"
        ]:
            raise ValueError("resource receipt RSS high-water mark regressed")
        if receipt["gpu_seconds"] != receipt["elapsed_wall_seconds"]:
            raise ValueError("resource receipt GPU wall envelope differs")
    if not (
        inference["latency_min_ms"]
        <= inference["latency_median_ms"]
        <= inference["latency_p95_ms"]
        <= inference["latency_max_ms"]
    ):
        raise ValueError("inference resource receipt latency order differs")
    if not math.isclose(
        training["steps_per_second"],
        training["steps"] / training["elapsed_wall_seconds"],
        rel_tol=1e-12,
        abs_tol=0.0,
    ) or not math.isclose(
        training["samples_per_second"],
        training["samples"] / training["elapsed_wall_seconds"],
        rel_tol=1e-12,
        abs_tol=0.0,
    ):
        raise ValueError("training resource receipt throughput relation differs")
    expected_inference_throughput = (
        inference["batch_size"] * 1000.0 / inference["latency_median_ms"]
    )
    if not math.isclose(
        inference["public_states_per_second_at_median"],
        expected_inference_throughput,
        rel_tol=1e-12,
        abs_tol=0.0,
    ):
        raise ValueError("inference resource receipt throughput relation differs")


def compose_resource_receipt(
    model: M5BValuePolicyNet,
    *,
    config_sha256: str,
    dataset_sha256: str,
    runtime: Mapping[str, object],
    training: Mapping[str, object],
    inference: Mapping[str, object],
) -> dict[str, object]:
    """Bind measured resources to one exact config, dataset, runtime and model."""

    if not _is_sha256(config_sha256) or not _is_sha256(dataset_sha256):
        raise ValueError("resource receipt config/dataset digest is invalid")
    if type(runtime) is not dict:
        raise ValueError("resource receipt runtime must be an exact object")
    if type(model) is not M5BValuePolicyNet:
        raise TypeError("resource receipt requires the exact M5b model type")
    try:
        model_device = next(model.parameters()).device
    except StopIteration as exc:
        raise ValueError("resource receipt model has no parameters") from exc
    if model_device != torch.device("cuda:0"):
        raise ValueError("resource receipt model must reside on fixed cuda:0")
    observed_runtime = verify_runtime_binding(runtime)
    runtime_digest = hashlib.sha256(canonical_bytes(observed_runtime)).hexdigest()
    model_digest = _tensor_digest(model.state_dict())
    _validate_resource_measurements(training, inference)
    if (
        type(training) is not dict
        or training.get("schema") != "route-a1-m5b-training-resource-receipt-v1"
        or training.get("config_sha256") != config_sha256
        or training.get("dataset_sha256") != dataset_sha256
        or training.get("runtime_sha256") != runtime_digest
        or training.get("end_model_tensor_sha256") != model_digest
    ):
        raise ValueError("training resource receipt binding differs")
    if (
        type(inference) is not dict
        or inference.get("schema") != "route-a1-m5b-inference-resource-receipt-v1"
        or inference.get("model_tensor_sha256") != model_digest
        or inference.get("runtime_sha256") != runtime_digest
    ):
        raise ValueError("inference resource receipt binding differs")
    body: dict[str, object] = {
        "schema": "route-a1-m5b-bound-resource-receipt-v1",
        "config_sha256": config_sha256,
        "dataset_sha256": dataset_sha256,
        "model_tensor_sha256": model_digest,
        "runtime": observed_runtime,
        "runtime_sha256": runtime_digest,
        "training": dict(training),
        "inference": dict(inference),
    }
    body["receipt_sha256"] = hashlib.sha256(canonical_bytes(body)).hexdigest()
    return body


def publish_resource_receipt(path: Path, receipt: Mapping[str, object]) -> str:
    if type(receipt) is not dict or set(receipt) != {
        "schema",
        "config_sha256",
        "dataset_sha256",
        "model_tensor_sha256",
        "runtime",
        "runtime_sha256",
        "training",
        "inference",
        "receipt_sha256",
    }:
        raise ValueError("bound resource receipt exact schema differs")
    unsigned = dict(receipt)
    claimed = unsigned.pop("receipt_sha256")
    runtime = receipt["runtime"]
    training = receipt["training"]
    inference = receipt["inference"]
    if type(runtime) is not dict or type(training) is not dict or type(inference) is not dict:
        raise ValueError("bound resource receipt nested schema differs")
    observed_runtime = verify_runtime_binding(runtime)
    _validate_resource_measurements(training, inference)
    if (
        receipt["schema"] != "route-a1-m5b-bound-resource-receipt-v1"
        or not _is_sha256(claimed)
        or any(
            not _is_sha256(receipt[field])
            for field in (
                "config_sha256",
                "dataset_sha256",
                "model_tensor_sha256",
                "runtime_sha256",
            )
        )
        or receipt["runtime_sha256"]
        != hashlib.sha256(canonical_bytes(observed_runtime)).hexdigest()
        or runtime != observed_runtime
        or training["config_sha256"] != receipt["config_sha256"]
        or training["dataset_sha256"] != receipt["dataset_sha256"]
        or training["runtime_sha256"] != receipt["runtime_sha256"]
        or training["end_model_tensor_sha256"] != receipt["model_tensor_sha256"]
        or inference["model_tensor_sha256"] != receipt["model_tensor_sha256"]
        or inference["runtime_sha256"] != receipt["runtime_sha256"]
        or hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != claimed
    ):
        raise ValueError("bound resource receipt digest differs")
    raw = _canonical_json_file_bytes(receipt)
    _no_clobber_bytes(Path(path), raw)
    if _stable_regular_bytes(Path(path)) != raw:
        raise RuntimeError("bound resource receipt readback differs")
    return str(claimed)


class _SyntheticResumeDataset:
    """Non-authoritative deterministic tensors used only by the resume gate."""

    def __init__(self) -> None:
        rng = np.random.default_rng(2026071491)
        self.rows: list[dict[str, np.ndarray]] = []
        combo_axis = np.linspace(-1.0, 1.0, HUNL_COMBO_COUNT, dtype=np.float32)
        for sample in range(5):
            action_mask = np.asarray(
                [True, sample % 2 == 0, True, True, False, True, False, False, True],
                dtype=np.bool_,
            )
            active = np.flatnonzero(action_mask)
            policy = np.zeros((HUNL_COMBO_COUNT, ACTION_COUNT), dtype=np.float32)
            logits = np.stack(
                [combo_axis * (slot + 1) * 0.1 + sample * 0.01 for slot in active],
                axis=1,
            )
            logits -= logits.max(axis=1, keepdims=True)
            probabilities = np.exp(logits)
            probabilities /= probabilities.sum(axis=1, keepdims=True)
            policy[:, active] = probabilities
            values = np.stack(
                (
                    500.0 * np.sin(combo_axis * (sample + 1)),
                    -500.0 * np.cos(combo_axis * (sample + 1)),
                )
            ).astype(np.float32)
            self.rows.append(
                {
                    "public_features": rng.normal(
                        0.0, 0.1, PUBLIC_FEATURE_DIM
                    ).astype(np.float32),
                    "actor": np.asarray(sample % 2, dtype=np.int8),
                    "reach_factors": np.full(
                        (2, HUNL_COMBO_COUNT), 1.0 / HUNL_COMBO_COUNT, dtype=np.float32
                    ),
                    "legal_combo_mask": np.ones(HUNL_COMBO_COUNT, dtype=np.bool_),
                    "legal_action_mask": action_mask,
                    "oracle_on_policy_private_values": values,
                    "oracle_value_valid_mask": np.ones(
                        (2, HUNL_COMBO_COUNT), dtype=np.bool_
                    ),
                    "projected_marginals": np.full(
                        (2, HUNL_COMBO_COUNT), 1.0 / HUNL_COMBO_COUNT, dtype=np.float32
                    ),
                    "oracle_actor_policy": policy,
                }
            )
        self.digest = hashlib.sha256(
            canonical_bytes(
                {
                    "schema": "route-a1-m5b-synthetic-resume-fixture-v1",
                    "seed": 2026071491,
                    "rows": len(self.rows),
                }
            )
        ).hexdigest()

    def __len__(self) -> int:
        return len(self.rows)

    def batch(self, indices: Sequence[int], device: torch.device) -> dict[str, Tensor]:
        return {
            field: torch.from_numpy(
                np.stack([self.rows[int(index)][field] for index in indices])
            ).to(device)
            for field in ShardDataset.ARRAY_FIELDS
        }


def run_non_epoch_resume_probes(
    *,
    network_config: Mapping[str, object],
    training_config: Mapping[str, object],
    runtime_binding: Mapping[str, object],
    config_digest: str,
    seed: int,
    output_directory: Path,
) -> dict[str, object]:
    """Prove two same-stack CUDA fresh-vs-resume executions bit-exact."""

    observed_runtime = verify_runtime_binding(runtime_binding)
    runtime_digest = hashlib.sha256(canonical_bytes(observed_runtime)).hexdigest()
    dataset = _SyntheticResumeDataset()
    device = torch.device("cuda:0")
    probe_config = dict(training_config)
    probe_config["batch_size"] = 2

    def make_trainer() -> DeterministicTrainer:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        model = build_model(network_config, seed=seed)
        return DeterministicTrainer(
            model,
            dataset,  # type: ignore[arg-type]
            training_config=probe_config,
            seed=seed + 1,
            config_digest=config_digest,
            runtime_binding=observed_runtime,
            device=device,
        )

    total_steps = 8
    receipts: list[dict[str, object]] = []
    for split_step in (2, 5):
        if split_step % 3 == 0:
            raise AssertionError("resume probe accidentally uses an epoch boundary")
        fresh = make_trainer()
        fresh.run_steps(total_steps)
        fresh_complete = fresh.complete_digest()
        fresh_tensor = _tensor_digest(fresh.model.state_dict())

        interrupted = make_trainer()
        interrupted.run_steps(split_step)
        checkpoint_payload = interrupted.checkpoint()
        checkpoint_state_digest = _state_digest(checkpoint_payload)
        checkpoint_path = output_directory / (
            f"resume-probe-step-{split_step}-{checkpoint_state_digest}.pt"
        )
        raw_checkpoint_digest = atomic_torch_save(
            checkpoint_path, checkpoint_payload
        )
        resumed = make_trainer()
        resumed.restore(
            load_training_checkpoint(
                checkpoint_path, expected_raw_sha256=raw_checkpoint_digest
            )
        )
        resumed.run_steps(total_steps - split_step)
        resumed_complete = resumed.complete_digest()
        resumed_tensor = _tensor_digest(resumed.model.state_dict())
        if fresh_complete != resumed_complete or fresh_tensor != resumed_tensor:
            raise RuntimeError(
                f"non-epoch CUDA resume probe {split_step} is not bit-exact"
            )
        receipts.append(
            {
                "split_step": split_step,
                "total_steps": total_steps,
                "batches_per_epoch": 3,
                "non_epoch_boundary": True,
                "checkpoint_state_sha256": checkpoint_state_digest,
                "checkpoint_raw_sha256": raw_checkpoint_digest,
                "final_complete_state_sha256": fresh_complete,
                "final_tensor_sha256": fresh_tensor,
                "fresh_equals_resumed": True,
            }
        )
    return {
        "schema": "route-a1-m5b-two-non-epoch-resume-gate-v1",
        "status": "passed",
        "runtime": observed_runtime,
        "runtime_sha256": runtime_digest,
        "synthetic_fixture_sha256": dataset.digest,
        "synthetic_fixture_training_authority": False,
        "probe_count": len(receipts),
        "probes": receipts,
    }


class TorchPublicProviders:
    """Eval-only public policy and value providers for offline backfeed."""

    def __init__(self, model: M5BValuePolicyNet, device: torch.device, version: str):
        self.model = model.to(device).eval()
        self.device = device
        self.version = version

    def _forward(self, model_input: PublicInferenceInput) -> tuple[np.ndarray, np.ndarray]:
        public = torch.from_numpy(
            encode_public_features(model_input.public_state)
        ).unsqueeze(0).to(self.device)
        reach = torch.from_numpy(
            np.array(model_input.reach_factors, dtype=np.float32, copy=True)
        ).unsqueeze(0).to(self.device)
        combo = torch.from_numpy(
            np.array(model_input.legal_combo_mask, dtype=np.bool_, copy=True)
        ).unsqueeze(0).to(self.device)
        action = torch.from_numpy(
            np.array(model_input.legal_action_mask, dtype=np.bool_, copy=True)
        ).unsqueeze(0).to(self.device)
        self.model.eval()
        with torch.inference_mode():
            values, logits = self.model(public, reach, combo, action)
            policy = torch.softmax(logits, dim=-1)
        return (
            values[0].float().cpu().numpy() * VALUE_SCALE_CHIPS,
            policy[0].float().cpu().numpy(),
        )

    def value(self, model_input: PublicInferenceInput) -> np.ndarray:
        return self._forward(model_input)[0]

    def policy(
        self, model_input: PublicInferenceInput, actions: AbstractActionSet
    ) -> np.ndarray:
        if not np.array_equal(model_input.legal_action_mask, actions.mask):
            raise ValueError("public policy action mask differs from exact support")
        return self._forward(model_input)[1]


class PublicValueAdapter:
    def __init__(self, providers: TorchPublicProviders):
        self.providers = providers
        self.version = providers.version

    def __call__(self, model_input: PublicInferenceInput) -> np.ndarray:
        return self.providers.value(model_input)


class PublicPolicyAdapter:
    def __init__(self, providers: TorchPublicProviders):
        self.providers = providers
        self.version = providers.version

    def __call__(
        self, model_input: PublicInferenceInput, actions: AbstractActionSet
    ) -> np.ndarray:
        return self.providers.policy(model_input, actions)


def export_deploy_npz(
    model: M5BValuePolicyNet,
    path: Path,
    *,
    config_digest: str,
    dataset_digest: str,
    runtime: Mapping[str, object],
) -> dict[str, object]:
    path = Path(path)
    if path.suffix != ".npz":
        raise ValueError("deployment artifact must use the .npz suffix")
    if not _is_sha256(config_digest) or not _is_sha256(dataset_digest):
        raise ValueError("deployment config/dataset digest is invalid")
    if type(runtime) is not dict:
        raise ValueError("deployment runtime binding must be an exact object")
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(path.parent)
    metadata_path = path.with_suffix(path.suffix + ".json")
    artifact_exists = os.path.lexists(path)
    metadata_exists = os.path.lexists(metadata_path)
    if artifact_exists != metadata_exists:
        raise ValueError("partial deployment publication exists")
    current_tensor_digest = _tensor_digest(model.state_dict())
    if artifact_exists:
        metadata = _read_deploy_metadata(metadata_path)
        loaded = load_deploy_npz(
            path,
            {
                "combo_embedding_dim": model.combo_embedding_dim,
                "global_hidden_dim": model.global_hidden_dim,
                "trunk_hidden_dim": model.trunk_hidden_dim,
                "layers": model.layers,
            },
            expected_metadata_sha256=str(metadata["metadata_sha256"]),
            expected_raw_npz_sha256=str(metadata["raw_npz_sha256"]),
            expected_config_sha256=config_digest,
            expected_dataset_sha256=dataset_digest,
            expected_runtime=runtime,
        )
        if _tensor_digest(loaded.state_dict()) != current_tensor_digest:
            raise ValueError("deployment path collides with different model tensors")
        return metadata

    arrays = {
        name: np.ascontiguousarray(tensor.detach().cpu().numpy())
        for name, tensor in sorted(model.state_dict().items())
    }
    if any(not np.all(np.isfinite(array)) for array in arrays.values()):
        raise ValueError("deployment model has non-finite tensors")
    raw_npz = _deterministic_npz_bytes(arrays)
    tensor_schema = {
        name: {"shape": list(array.shape), "dtype": array.dtype.str}
        for name, array in sorted(arrays.items())
    }
    runtime_body = dict(runtime)
    metadata = {
        "schema": DEPLOY_SCHEMA,
        "architecture": model.architecture(),
        "config_sha256": config_digest,
        "dataset_sha256": dataset_digest,
        "runtime": runtime_body,
        "runtime_sha256": hashlib.sha256(canonical_bytes(runtime_body)).hexdigest(),
        "value_output_unit": "chips_after_multiply_by_20000",
        "policy_output": (
            "legal_action_masked_actor_logits_softmax_by_consumer_over_nine_slots"
        ),
        "raw_npz_sha256": hashlib.sha256(raw_npz).hexdigest(),
        "tensor_state_sha256": current_tensor_digest,
        "tensor_schema": tensor_schema,
        "pickle_required": False,
    }
    metadata["metadata_sha256"] = hashlib.sha256(canonical_bytes(metadata)).hexdigest()
    metadata_raw = _canonical_json_file_bytes(metadata)
    _no_clobber_bytes(path, raw_npz)
    _no_clobber_bytes(metadata_path, metadata_raw)
    loaded = load_deploy_npz(
        path,
        {
            "combo_embedding_dim": model.combo_embedding_dim,
            "global_hidden_dim": model.global_hidden_dim,
            "trunk_hidden_dim": model.trunk_hidden_dim,
            "layers": model.layers,
        },
        expected_metadata_sha256=str(metadata["metadata_sha256"]),
        expected_raw_npz_sha256=str(metadata["raw_npz_sha256"]),
        expected_config_sha256=config_digest,
        expected_dataset_sha256=dataset_digest,
        expected_runtime=runtime_body,
    )
    if _tensor_digest(loaded.state_dict()) != current_tensor_digest:
        raise RuntimeError("deployment readback tensor digest differs")
    return metadata


def _read_deploy_metadata(metadata_path: Path) -> dict[str, object]:
    raw = _stable_regular_bytes(metadata_path)
    metadata = _strict_json_object(raw, label="deployment metadata")
    if set(metadata) != DEPLOY_METADATA_FIELDS:
        raise ValueError("deployment metadata exact schema differs")
    if raw != _canonical_json_file_bytes(metadata):
        raise ValueError("deployment metadata is not canonical JSON bytes")
    claimed_digest = metadata["metadata_sha256"]
    unsigned = dict(metadata)
    unsigned.pop("metadata_sha256")
    if (
        not _is_sha256(claimed_digest)
        or hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != claimed_digest
    ):
        raise ValueError("deployment metadata digest differs")
    return metadata


def load_deploy_npz(
    path: Path,
    network_config: Mapping[str, object],
    *,
    expected_metadata_sha256: str,
    expected_raw_npz_sha256: str,
    expected_config_sha256: str,
    expected_dataset_sha256: str,
    expected_runtime: Mapping[str, object],
    seed: int = 0,
) -> M5BValuePolicyNet:
    for label, digest in (
        ("metadata", expected_metadata_sha256),
        ("raw NPZ", expected_raw_npz_sha256),
        ("config", expected_config_sha256),
        ("dataset", expected_dataset_sha256),
    ):
        if not _is_sha256(digest):
            raise ValueError(f"deployment expected {label} digest is invalid")
    if type(expected_runtime) is not dict:
        raise ValueError("deployment expected runtime must be an exact object")
    path = Path(path)
    metadata = _read_deploy_metadata(path.with_suffix(path.suffix + ".json"))
    if metadata["schema"] != DEPLOY_SCHEMA:
        raise ValueError("deployment schema differs")
    if metadata["metadata_sha256"] != expected_metadata_sha256:
        raise ValueError("deployment externally bound metadata digest differs")
    if (
        metadata["raw_npz_sha256"] != expected_raw_npz_sha256
        or metadata["config_sha256"] != expected_config_sha256
        or metadata["dataset_sha256"] != expected_dataset_sha256
    ):
        raise ValueError("deployment external artifact binding differs")
    if metadata["runtime"] != dict(expected_runtime) or metadata[
        "runtime_sha256"
    ] != hashlib.sha256(canonical_bytes(dict(expected_runtime))).hexdigest():
        raise ValueError("deployment runtime binding differs")
    if (
        metadata["value_output_unit"] != "chips_after_multiply_by_20000"
        or metadata["policy_output"]
        != "legal_action_masked_actor_logits_softmax_by_consumer_over_nine_slots"
        or metadata["pickle_required"] is not False
    ):
        raise ValueError("deployment output semantics differ")
    model = build_model(network_config, seed=seed)
    expected = model.state_dict()
    if metadata["architecture"] != model.architecture():
        raise ValueError("deployment architecture differs")
    expected_schema = {
        name: {
            "shape": list(tensor.shape),
            "dtype": tensor.detach().cpu().numpy().dtype.str,
        }
        for name, tensor in sorted(expected.items())
    }
    if metadata["tensor_schema"] != expected_schema or not _is_sha256(
        metadata["tensor_state_sha256"]
    ):
        raise ValueError("deployment tensor schema differs")
    raw_npz = _stable_regular_bytes(path)
    if hashlib.sha256(raw_npz).hexdigest() != expected_raw_npz_sha256:
        raise ValueError("deployment NPZ raw digest differs")
    with np.load(io.BytesIO(raw_npz), allow_pickle=False) as payload:
        if payload.files != sorted(expected):
            raise ValueError("deploy NPZ tensor names differ")
        state: dict[str, Tensor] = {}
        for name, expected_tensor in expected.items():
            array = np.array(payload[name], copy=True)
            expected_array = expected_tensor.detach().cpu().numpy()
            if (
                list(array.shape) != list(expected_array.shape)
                or array.dtype.str != expected_array.dtype.str
                or not np.all(np.isfinite(array))
            ):
                raise ValueError(f"deployment tensor shape/dtype/value differs: {name}")
            state[name] = torch.from_numpy(np.ascontiguousarray(array))
    if _tensor_digest(state) != metadata["tensor_state_sha256"]:
        raise ValueError("deployment tensor-state digest differs")
    model.load_state_dict(state, strict=True)
    return model.eval()


def _array_sha256(array: np.ndarray) -> str:
    return hashlib.sha256(
        np.ascontiguousarray(array, dtype="<f4").tobytes()
    ).hexdigest()


def _fixed_quantile_actions(policy: np.ndarray, mask: np.ndarray) -> np.ndarray:
    probabilities = np.where(mask[None, :], policy, 0.0)
    probabilities /= probabilities.sum(axis=1, keepdims=True)
    cumulative = np.cumsum(probabilities, axis=1)
    return np.argmax(cumulative >= 0.37123456789, axis=1)


def run_model_influence_gate(
    *,
    network_config: Mapping[str, object],
    runtime_binding: Mapping[str, object],
    seed: int,
) -> dict[str, object]:
    """Pair normal/zero/shuffle/namespace-swap through leaf and root search."""

    runtime = verify_runtime_binding(runtime_binding)
    device = torch.device("cuda:0")
    base = build_model(network_config, seed=seed)
    variants: dict[str, M5BValuePolicyNet] = {"normal": base}

    zeroed = copy.deepcopy(base)
    with torch.no_grad():
        for parameter in zeroed.parameters():
            parameter.zero_()
    variants["zeroed"] = zeroed

    shuffled = copy.deepcopy(base)
    with torch.no_grad():
        permutation = torch.arange(HUNL_COMBO_COUNT - 1, -1, -1)
        shuffled.combo_embedding.weight.copy_(
            shuffled.combo_embedding.weight[permutation]
        )
    variants["shuffled"] = shuffled

    swapped = copy.deepcopy(base)
    with torch.no_grad():
        value_weight = swapped.value_head.weight.detach().clone()
        value_bias = swapped.value_head.bias.detach().clone()
        policy_weight = swapped.policy_head.weight.detach().clone()
        policy_bias = swapped.policy_head.bias.detach().clone()
        swapped.value_head.weight.copy_(policy_weight[:2])
        swapped.value_head.bias.copy_(policy_bias[:2])
        for slot in range(ACTION_COUNT):
            swapped.policy_head.weight[slot].copy_(value_weight[slot % 2])
            swapped.policy_head.bias[slot].copy_(value_bias[slot % 2])
    variants["value_policy_namespace_swapped"] = swapped

    state = NationalGameState.new_hand(1, small_blind=0)
    pbs = HUNLReachFactorPublicBeliefState.from_state(state)
    reach = ReachFactors.from_pbs(pbs)
    actions = abstract_actions(state)
    model_input = PublicInferenceInput.from_state(state, reach, actions.mask)
    outputs: dict[str, dict[str, object]] = {}
    raw_arrays: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for name, model in variants.items():
        providers = TorchPublicProviders(
            model, device, f"m5b-influence-{name}-v1"
        )
        value_adapter = PublicValueAdapter(providers)
        policy_adapter = PublicPolicyAdapter(providers)
        leaf_values = value_adapter(model_input)
        solver = DepthLimitedCFRAvg(
            iterations=2,
            deals_per_iteration=1,
            public_action_depth=1,
            warm_policy=policy_adapter,
            public_value_leaf=value_adapter,
            seed=seed + 100,
        )
        result = solver.solve(state, pbs)
        root_policy = result.root_average_policy
        selected_actions = _fixed_quantile_actions(root_policy, actions.mask)
        raw_arrays[name] = (leaf_values, root_policy, selected_actions)
        outputs[name] = {
            "leaf_values_sha256": _array_sha256(leaf_values),
            "root_policy_sha256": _array_sha256(root_policy),
            "selected_actions_sha256": hashlib.sha256(
                selected_actions.astype("<i2").tobytes()
            ).hexdigest(),
            "sampled_search_iteration": result.sampled_search_iteration,
        }
    normal = raw_arrays["normal"]
    comparisons: dict[str, dict[str, object]] = {}
    for name in (
        "zeroed",
        "shuffled",
        "value_policy_namespace_swapped",
    ):
        variant = raw_arrays[name]
        comparison = {
            "leaf_max_abs_difference_chips": float(
                np.max(np.abs(normal[0] - variant[0]))
            ),
            "root_policy_max_abs_difference": float(
                np.max(np.abs(normal[1] - variant[1]))
            ),
            "private_hand_action_difference_count": int(
                np.count_nonzero(normal[2] != variant[2])
            ),
        }
        if (
            comparison["leaf_max_abs_difference_chips"] <= 0.0
            or comparison["root_policy_max_abs_difference"] <= 0.0
            or comparison["private_hand_action_difference_count"] <= 0
        ):
            raise RuntimeError(f"model variant {name} has no end-to-end influence")
        comparisons[name] = comparison
    return {
        "schema": "route-a1-m5b-model-influence-gate-v1",
        "status": "passed",
        "weights_are_untrained_gate_fixture": True,
        "formal_trained_model_gate_still_required": True,
        "public_input_sha256": model_input.digest,
        "runtime": runtime,
        "variants": outputs,
        "paired_against_normal": comparisons,
    }
