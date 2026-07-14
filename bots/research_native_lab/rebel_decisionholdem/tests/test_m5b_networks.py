from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import warnings
import zipfile
from pathlib import Path

import numpy as np
import pytest
import torch
from torch.nn import functional as F

from ...common_contracts.national_state import NationalGameState
from ..rebel_like.m5b_contract import canonical_bytes, config_digest, load_config
from ..rebel_like.hunl_pbs import HUNLReachFactorPublicBeliefState
from ..rebel_like.m5b_data import (
    PUBLIC_FEATURE_DIM,
    make_prelabel_plan,
    write_sample_shard,
)
from ..rebel_like.m5b_networks import (
    ACTION_COUNT,
    HUNL_COMBO_COUNT,
    DeterministicTrainer,
    ShardDataset,
    _SyntheticResumeDataset,
    _canonical_json_file_bytes,
    _deterministic_npz_bytes,
    _loss,
    _state_digest,
    _tensor_digest,
    atomic_torch_save,
    build_model,
    compose_resource_receipt,
    export_deploy_npz,
    load_deploy_npz,
    load_training_checkpoint,
    measure_inference_resources,
    measure_training_resources,
    publish_resource_receipt,
    run_model_influence_gate,
    run_non_epoch_resume_probes,
    verify_runtime_binding,
)
from ..rebel_like.m5b_search import (
    PrivateTargets,
    PublicInferenceInput,
    ReachFactors,
    abstract_actions,
)


ROOT = Path(__file__).resolve().parents[1]


def _config():
    return load_config(ROOT / "configs" / "m5b_offline_rebel_loop.json")


def test_network_has_fixed_complete_value_and_actor_policy_shapes() -> None:
    config = _config()
    model = build_model(config["network"], seed=config["seeds"]["network_init"])
    public = torch.zeros(2, PUBLIC_FEATURE_DIM)
    reach = torch.full((2, 2, HUNL_COMBO_COUNT), 1.0 / HUNL_COMBO_COUNT)
    combo_mask = torch.ones(2, HUNL_COMBO_COUNT, dtype=torch.bool)
    action_mask = torch.zeros(2, ACTION_COUNT, dtype=torch.bool)
    action_mask[:, (0, 2, 3, 5, 8)] = True
    values, logits = model(public, reach, combo_mask, action_mask)
    assert values.shape == (2, 2, 1326)
    assert logits.shape == (2, 1326, 9)
    assert model.architecture()["dropout"] is False
    assert model.architecture()["parameter_count"] == 513_419
    assert model.architecture()["policy_internal_unit"] == "legal_action_masked_logits"


def test_actual_cuda_runtime_and_two_non_epoch_resume_probes(tmp_path) -> None:
    if not torch.cuda.is_available():
        raise AssertionError("M5b formal test host lost its required CUDA device")
    config = _config()
    assert verify_runtime_binding(config["runtime_binding"]) == config["runtime_binding"]
    receipt = run_non_epoch_resume_probes(
        network_config=config["network"],
        training_config=config["training"],
        runtime_binding=config["runtime_binding"],
        config_digest=config_digest(config),
        seed=config["seeds"]["training"],
        output_directory=tmp_path,
    )
    assert receipt["status"] == "passed"
    assert receipt["probe_count"] == 2
    assert [probe["split_step"] for probe in receipt["probes"]] == [2, 5]
    assert all(probe["fresh_equals_resumed"] for probe in receipt["probes"])


def test_model_weights_influence_leaf_root_policy_and_actions() -> None:
    config = _config()
    receipt = run_model_influence_gate(
        network_config=config["network"],
        runtime_binding=config["runtime_binding"],
        seed=config["seeds"]["network_init"],
    )
    assert receipt["status"] == "passed"
    assert receipt["formal_trained_model_gate_still_required"] is True
    for comparison in receipt["paired_against_normal"].values():
        assert comparison["leaf_max_abs_difference_chips"] > 0.0
        assert comparison["root_policy_max_abs_difference"] > 0.0
        assert comparison["private_hand_action_difference_count"] > 0


def test_policy_loss_uses_actor_projected_marginal_not_uniform_combo_weight() -> None:
    logits = torch.full((1, HUNL_COMBO_COUNT, ACTION_COUNT), -20.0)
    logits[0, 0, 0:2] = torch.tensor([3.0, 0.0])
    logits[0, 1, 0:2] = torch.tensor([0.0, 3.0])

    class FixedOutputs(torch.nn.Module):
        def forward(self, *_args):
            return torch.zeros(1, 2, HUNL_COMBO_COUNT), logits

    legal_combo = torch.zeros(1, HUNL_COMBO_COUNT, dtype=torch.bool)
    legal_combo[0, :2] = True
    legal_action = torch.zeros(1, ACTION_COUNT, dtype=torch.bool)
    legal_action[0, :2] = True
    target_policy = torch.zeros(1, HUNL_COMBO_COUNT, ACTION_COUNT)
    target_policy[0, :2, 0] = 1.0
    marginals = torch.zeros(1, 2, HUNL_COMBO_COUNT)
    marginals[0, 0, 1] = 1.0
    marginals[0, 1, 0] = 1.0
    batch = {
        "public_features": torch.zeros(1, PUBLIC_FEATURE_DIM),
        "actor": torch.tensor([1], dtype=torch.int8),
        "reach_factors": torch.zeros(1, 2, HUNL_COMBO_COUNT),
        "legal_combo_mask": legal_combo,
        "legal_action_mask": legal_action,
        "oracle_on_policy_private_values": torch.zeros(1, 2, HUNL_COMBO_COUNT),
        "oracle_value_valid_mask": torch.ones(1, 2, HUNL_COMBO_COUNT, dtype=torch.bool),
        "projected_marginals": marginals,
        "oracle_actor_policy": target_policy,
    }
    _, metrics = _loss(FixedOutputs(), batch, 0.0)  # type: ignore[arg-type]
    expected_actor_ce = float(-F.log_softmax(logits[0, 0], dim=-1)[0])
    uniform_ce = float(
        -0.5
        * (
            F.log_softmax(logits[0, 0], dim=-1)[0]
            + F.log_softmax(logits[0, 1], dim=-1)[0]
        )
    )
    assert metrics["policy_ce"] == pytest.approx(expected_actor_ce)
    assert abs(metrics["policy_ce"] - uniform_ce) > 1.0
    bad_batch = dict(batch)
    bad_target = target_policy.clone()
    bad_target[0, 0, 2] = 0.1
    bad_batch["oracle_actor_policy"] = bad_target
    with pytest.raises(ValueError, match="actor-policy target support"):
        _loss(FixedOutputs(), bad_batch, 0.0)  # type: ignore[arg-type]


def _synthetic_trainer(config, *, seed: int) -> DeterministicTrainer:
    dataset = _SyntheticResumeDataset()
    return DeterministicTrainer(
        build_model(config["network"], seed=seed),
        dataset,  # type: ignore[arg-type]
        training_config=config["training"],
        seed=seed + 1,
        config_digest=config_digest(config),
        runtime_binding=config["runtime_binding"],
        device=torch.device("cuda:0"),
    )


def test_trainer_rejects_cpu_before_model_or_optimizer_setup() -> None:
    config = _config()
    with pytest.raises(ValueError, match="fixed cuda:0"):
        DeterministicTrainer(
            build_model(config["network"], seed=701),
            _SyntheticResumeDataset(),  # type: ignore[arg-type]
            training_config=config["training"],
            seed=702,
            config_digest=config_digest(config),
            runtime_binding=config["runtime_binding"],
            device=torch.device("cpu"),
        )


def test_dataset_load_is_external_digest_bound_and_exact(tmp_path: Path) -> None:
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=((), ()))
    pbs = HUNLReachFactorPublicBeliefState.from_state(state)
    actions = abstract_actions(state)
    plan = make_prelabel_plan(
        state,
        pbs,
        trajectory_id="3" * 64,
        rollout_group_id="4" * 64,
        source_copy_group_id="5" * 64,
        source_checkpoint_digest="6" * 64,
        decision_index=0,
        seed_group=0,
    )
    marginals = np.stack(
        (
            np.asarray(pbs.projected_marginal(0), dtype=np.float64),
            np.asarray(pbs.projected_marginal(1), dtype=np.float64),
        )
    )
    policy = np.zeros((HUNL_COMBO_COUNT, ACTION_COUNT), dtype=np.float64)
    policy[:, actions.mask] = 1.0 / int(actions.mask.sum())
    targets = PrivateTargets(
        normalized_values=np.zeros((2, HUNL_COMBO_COUNT), dtype=np.float64),
        unnormalized_cfvs=np.zeros((2, HUNL_COMBO_COUNT), dtype=np.float64),
        value_valid_mask=marginals > 0.0,
        projected_marginals=marginals,
        actor_normalized_q=np.zeros(
            (HUNL_COMBO_COUNT, ACTION_COUNT), dtype=np.float64
        ),
        actor_q_valid_mask=np.zeros(
            (HUNL_COMBO_COUNT, ACTION_COUNT), dtype=np.bool_
        ),
        raw_weighted_zero_sum=0.0,
        diagnostic_cross_profile_q_v_mae=0.0,
    )
    split_sha = "7" * 64
    split_authority_sha = "d" * 64
    split_authority_source_sha = "e" * 64
    generator_config_sha = config_digest(_config())
    generator_kwargs = {
        "generator_config_sha256": generator_config_sha,
        "split_authority_sha256": split_authority_sha,
        "split_authority_source_closure_sha256": split_authority_source_sha,
        "search_result_sha256": "a" * 64,
        "solver_seed": 101,
        "target_seed": 202,
    }
    dataset_authority_kwargs = {
        "expected_split_authority_sha256": split_authority_sha,
        "expected_split_authority_source_closure_sha256": (
            split_authority_source_sha
        ),
        "expected_generator_config_sha256": generator_config_sha,
    }
    metadata = write_sample_shard(
        tmp_path,
        plan=plan,
        split_manifest={
            "sample_splits": {plan.sample_id: "train"},
            "manifest_sha256": split_sha,
        },
        pbs=pbs,
        actions=actions,
        policy_target=policy,
        targets=targets,
        **generator_kwargs,
    )
    metadata_path = tmp_path / f"{plan.sample_id}.json"
    expected_registry = {plan.sample_id: metadata["metadata_sha256"]}
    dataset = ShardDataset(
        [metadata_path],
        split="train",
        expected_split_manifest_sha256=split_sha,
        expected_metadata_sha256_by_sample=expected_registry,
        **dataset_authority_kwargs,
    )
    assert len(dataset) == 1
    assert dataset.rows[0]["actor"].dtype == np.int8
    with pytest.raises(ValueError, match="externally bound shard"):
        ShardDataset(
            [metadata_path],
            split="train",
            expected_split_manifest_sha256=split_sha,
            expected_metadata_sha256_by_sample={plan.sample_id: "8" * 64},
            **dataset_authority_kwargs,
        )
    linked_directory = tmp_path / "linked"
    linked_directory.mkdir()
    linked_metadata = linked_directory / metadata_path.name
    os.symlink(metadata_path, linked_metadata)
    os.symlink(metadata_path.with_suffix(".npz"), linked_metadata.with_suffix(".npz"))
    with pytest.raises(ValueError, match="symlink"):
        write_sample_shard(
            linked_directory,
            plan=plan,
            split_manifest={
                "sample_splits": {plan.sample_id: "train"},
                "manifest_sha256": split_sha,
            },
            pbs=pbs,
            actions=actions,
            policy_target=policy,
            targets=targets,
            **generator_kwargs,
        )
    with pytest.raises(ValueError, match="symlink"):
        ShardDataset(
            [linked_metadata],
            split="train",
            expected_split_manifest_sha256=split_sha,
            expected_metadata_sha256_by_sample=expected_registry,
            **dataset_authority_kwargs,
        )
    actual_directory = tmp_path / "actual-publication"
    actual_directory.mkdir()
    alias_directory = tmp_path / "aliased-publication"
    os.symlink(actual_directory, alias_directory)
    with pytest.raises(ValueError, match="path contains a symlink"):
        write_sample_shard(
            alias_directory,
            plan=plan,
            split_manifest={
                "sample_splits": {plan.sample_id: "train"},
                "manifest_sha256": split_sha,
            },
            pbs=pbs,
            actions=actions,
            policy_target=policy,
            targets=targets,
            **generator_kwargs,
        )
    forged_directory = tmp_path / "forged"
    forged_directory.mkdir()
    forged_metadata = copy.deepcopy(metadata)
    forged_metadata["action_support"]["action_wires"][0] = "raise 201"
    forged_metadata.pop("metadata_sha256")
    forged_metadata["metadata_sha256"] = hashlib.sha256(
        canonical_bytes(forged_metadata)
    ).hexdigest()
    forged_metadata_path = forged_directory / metadata_path.name
    forged_metadata_path.write_bytes(
        json.dumps(forged_metadata, sort_keys=True, allow_nan=False).encode("utf-8")
        + b"\n"
    )
    forged_metadata_path.with_suffix(".npz").write_bytes(
        metadata_path.with_suffix(".npz").read_bytes()
    )
    with pytest.raises(ValueError, match="Common legality"):
        ShardDataset(
            [forged_metadata_path],
            split="train",
            expected_split_manifest_sha256=split_sha,
            expected_metadata_sha256_by_sample={
                plan.sample_id: forged_metadata["metadata_sha256"]
            },
            **dataset_authority_kwargs,
        )
    forged_receipt_directory = tmp_path / "forged-receipt"
    forged_receipt_directory.mkdir()
    forged_receipt = copy.deepcopy(metadata)
    forged_receipt["generator_receipt"] = {}
    forged_receipt.pop("metadata_sha256")
    forged_receipt["metadata_sha256"] = hashlib.sha256(
        canonical_bytes(forged_receipt)
    ).hexdigest()
    forged_receipt_path = forged_receipt_directory / metadata_path.name
    forged_receipt_path.write_bytes(
        json.dumps(forged_receipt, sort_keys=True, allow_nan=False).encode("utf-8")
        + b"\n"
    )
    forged_receipt_path.with_suffix(".npz").write_bytes(
        metadata_path.with_suffix(".npz").read_bytes()
    )
    with pytest.raises(ValueError, match="generator receipt exact schema"):
        ShardDataset(
            [forged_receipt_path],
            split="train",
            expected_split_manifest_sha256=split_sha,
            expected_metadata_sha256_by_sample={
                plan.sample_id: forged_receipt["metadata_sha256"]
            },
            **dataset_authority_kwargs,
        )
    duplicate_directory = tmp_path / "duplicate-member"
    duplicate_directory.mkdir()
    duplicate_buffer = io.BytesIO(metadata_path.with_suffix(".npz").read_bytes())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(duplicate_buffer, mode="a") as archive:
            member = "public_features.npy"
            archive.writestr(member, archive.read(member))
    duplicate_raw = duplicate_buffer.getvalue()
    duplicate_metadata = copy.deepcopy(metadata)
    duplicate_metadata["npz_raw_sha256"] = hashlib.sha256(duplicate_raw).hexdigest()
    duplicate_metadata.pop("metadata_sha256")
    duplicate_metadata["metadata_sha256"] = hashlib.sha256(
        canonical_bytes(duplicate_metadata)
    ).hexdigest()
    duplicate_metadata_path = duplicate_directory / metadata_path.name
    duplicate_metadata_path.write_bytes(
        json.dumps(duplicate_metadata, sort_keys=True, allow_nan=False).encode("utf-8")
        + b"\n"
    )
    duplicate_metadata_path.with_suffix(".npz").write_bytes(duplicate_raw)
    with pytest.raises(ValueError, match="exact array schema"):
        ShardDataset(
            [duplicate_metadata_path],
            split="train",
            expected_split_manifest_sha256=split_sha,
            expected_metadata_sha256_by_sample={
                plan.sample_id: duplicate_metadata["metadata_sha256"]
            },
            **dataset_authority_kwargs,
        )
    with pytest.raises(ValueError, match="existing sample arrays differ"):
        write_sample_shard(
            duplicate_directory,
            plan=plan,
            split_manifest={
                "sample_splits": {plan.sample_id: "train"},
                "manifest_sha256": split_sha,
            },
            pbs=pbs,
            actions=actions,
            policy_target=policy,
            targets=targets,
            **generator_kwargs,
        )
    wrong_authority_kwargs = dict(dataset_authority_kwargs)
    wrong_authority_kwargs["expected_split_authority_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="split_authority_sha256 binding"):
        ShardDataset(
            [metadata_path],
            split="train",
            expected_split_manifest_sha256=split_sha,
            expected_metadata_sha256_by_sample=expected_registry,
            **wrong_authority_kwargs,
        )
    bad_row = {name: np.array(value, copy=True) for name, value in dataset.rows[0].items()}
    bad_row["legal_action_mask"][:] = False
    with pytest.raises(ValueError, match="action support is empty"):
        ShardDataset._validate_row(bad_row)
    bad_dtype = {
        name: np.array(value, copy=True) for name, value in dataset.rows[0].items()
    }
    bad_dtype["actor"] = bad_dtype["actor"].astype(np.int64)
    with pytest.raises(ValueError, match="actor dtype"):
        ShardDataset._validate_row(bad_dtype)
    bad_zero_sum = {
        name: np.array(value, copy=True) for name, value in dataset.rows[0].items()
    }
    bad_zero_sum["oracle_on_policy_private_values"].fill(1000.0)
    with pytest.raises(ValueError, match="weighted zero sum"):
        ShardDataset._validate_row(bad_zero_sum)
    npz_path = metadata_path.with_suffix(".npz")
    npz_path.write_bytes(npz_path.read_bytes() + b"mutated")
    with pytest.raises(ValueError, match="raw digest"):
        ShardDataset(
            [metadata_path],
            split="train",
            expected_split_manifest_sha256=split_sha,
            expected_metadata_sha256_by_sample=expected_registry,
            **dataset_authority_kwargs,
        )


def test_checkpoint_load_restore_is_digest_bound_symlink_free_and_strict(
    tmp_path: Path,
) -> None:
    config = _config()
    source = _synthetic_trainer(config, seed=9101)
    source.run_steps(1)
    payload = source.checkpoint()
    source_at_checkpoint = source.complete_digest()
    payload_digest = _state_digest(payload)
    source.run_steps(1)
    assert _state_digest(payload) == payload_digest
    assert payload["cursor"]["global_step"] == 1
    assert all(
        float(state["step"]) == 1.0
        for state in payload["optimizer"]["state"].values()
    )
    checkpoint = tmp_path / "checkpoint.pt"
    raw_digest = atomic_torch_save(checkpoint, payload)
    colliding_payload = copy.deepcopy(payload)
    colliding_payload["cursor"]["global_step"] += 1
    with pytest.raises(ValueError, match="path collision"):
        atomic_torch_save(checkpoint, colliding_payload)
    loaded = load_training_checkpoint(
        checkpoint, expected_raw_sha256=raw_digest
    )
    restored = _synthetic_trainer(config, seed=9101)
    restored.restore(loaded)
    assert restored.complete_digest() == source_at_checkpoint

    with pytest.raises(ValueError, match="raw digest"):
        load_training_checkpoint(checkpoint, expected_raw_sha256="0" * 64)
    symlink = tmp_path / "checkpoint-link.pt"
    os.symlink(checkpoint, symlink)
    with pytest.raises(ValueError, match="symlink"):
        load_training_checkpoint(symlink, expected_raw_sha256=raw_digest)

    bad_cursor = copy.deepcopy(loaded)
    bad_cursor["cursor"]["batch_cursor"] = 1
    with pytest.raises(ValueError, match="batch cursor"):
        _synthetic_trainer(config, seed=9101).restore(bad_cursor)
    bad_permutation = copy.deepcopy(loaded)
    bad_permutation["cursor"]["permutation"][0] = bad_permutation["cursor"][
        "permutation"
    ][1]
    with pytest.raises(ValueError, match="permutation"):
        _synthetic_trainer(config, seed=9101).restore(bad_permutation)
    bad_optimizer = copy.deepcopy(loaded)
    first_state = next(iter(bad_optimizer["optimizer"]["state"].values()))
    first_state["exp_avg"] = first_state["exp_avg"].reshape(-1)
    with pytest.raises(ValueError, match="exp_avg"):
        _synthetic_trainer(config, seed=9101).restore(bad_optimizer)
    negative_second_moment = copy.deepcopy(loaded)
    first_state = next(
        iter(negative_second_moment["optimizer"]["state"].values())
    )
    first_state["exp_avg_sq"].fill_(-1.0)
    with pytest.raises(ValueError, match="exp_avg_sq"):
        _synthetic_trainer(config, seed=9101).restore(negative_second_moment)


def test_deploy_export_load_is_no_clobber_and_strictly_bound(tmp_path: Path) -> None:
    config = _config()
    model = build_model(config["network"], seed=7201)
    config_sha = config_digest(config)
    dataset_sha = "2" * 64
    deploy = tmp_path / "model.npz"
    metadata = export_deploy_npz(
        model,
        deploy,
        config_digest=config_sha,
        dataset_digest=dataset_sha,
        runtime=config["runtime_binding"],
    )
    loaded = load_deploy_npz(
        deploy,
        config["network"],
        expected_metadata_sha256=metadata["metadata_sha256"],
        expected_raw_npz_sha256=metadata["raw_npz_sha256"],
        expected_config_sha256=config_sha,
        expected_dataset_sha256=dataset_sha,
        expected_runtime=config["runtime_binding"],
    )
    assert _tensor_digest(loaded.state_dict()) == _tensor_digest(model.state_dict())
    assert export_deploy_npz(
        model,
        deploy,
        config_digest=config_sha,
        dataset_digest=dataset_sha,
        runtime=config["runtime_binding"],
    ) == metadata
    with pytest.raises(ValueError, match="different model tensors"):
        export_deploy_npz(
            build_model(config["network"], seed=7202),
            deploy,
            config_digest=config_sha,
            dataset_digest=dataset_sha,
            runtime=config["runtime_binding"],
        )

    with np.load(io.BytesIO(deploy.read_bytes()), allow_pickle=False) as archive:
        arrays = {name: np.array(archive[name], copy=True) for name in archive.files}
    first_name = sorted(arrays)[0]
    arrays[first_name] = arrays[first_name].astype(np.float64)
    mutated_raw = _deterministic_npz_bytes(arrays)
    mutated_metadata = copy.deepcopy(metadata)
    mutated_metadata["raw_npz_sha256"] = hashlib.sha256(mutated_raw).hexdigest()
    mutated_metadata.pop("metadata_sha256")
    mutated_metadata["metadata_sha256"] = hashlib.sha256(
        canonical_bytes(mutated_metadata)
    ).hexdigest()
    mutated_path = tmp_path / "mutated.npz"
    mutated_path.write_bytes(mutated_raw)
    mutated_path.with_suffix(".npz.json").write_bytes(
        _canonical_json_file_bytes(mutated_metadata)
    )
    with pytest.raises(ValueError, match="shape/dtype/value"):
        load_deploy_npz(
            mutated_path,
            config["network"],
            expected_metadata_sha256=mutated_metadata["metadata_sha256"],
            expected_raw_npz_sha256=mutated_metadata["raw_npz_sha256"],
            expected_config_sha256=config_sha,
            expected_dataset_sha256=dataset_sha,
            expected_runtime=config["runtime_binding"],
        )
    linked = tmp_path / "linked.npz"
    os.symlink(deploy, linked)
    os.symlink(deploy.with_suffix(".npz.json"), linked.with_suffix(".npz.json"))
    with pytest.raises(ValueError, match="symlink"):
        load_deploy_npz(
            linked,
            config["network"],
            expected_metadata_sha256=metadata["metadata_sha256"],
            expected_raw_npz_sha256=metadata["raw_npz_sha256"],
            expected_config_sha256=config_sha,
            expected_dataset_sha256=dataset_sha,
            expected_runtime=config["runtime_binding"],
        )


def test_synthetic_cuda_resource_receipts_cover_training_and_inference(
    tmp_path: Path,
) -> None:
    config = _config()
    trainer = _synthetic_trainer(config, seed=8301)
    training = measure_training_resources(trainer, steps=1)
    assert training["steps"] == 1
    assert training["samples"] == config["training"]["batch_size"]
    assert training["peak_cuda_allocated_bytes"] > 0
    assert training["peak_cuda_reserved_bytes"] >= training["peak_cuda_allocated_bytes"]
    assert training["samples_per_second"] > 0.0
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=((), ()))
    pbs = HUNLReachFactorPublicBeliefState.from_state(state)
    actions = abstract_actions(state)
    model_input = PublicInferenceInput.from_state(
        state, ReachFactors.from_pbs(pbs), actions.mask
    )
    fake_runtime = dict(config["runtime_binding"])
    fake_runtime["gpu_name"] = "NOT-THE-ACTUAL-GPU"
    with pytest.raises(RuntimeError, match="runtime differs"):
        measure_inference_resources(
            trainer.model,
            model_inputs=[model_input],
            runtime_binding=fake_runtime,
            warmup=0,
            repeats=1,
        )
    inference = measure_inference_resources(
        trainer.model,
        model_inputs=[model_input],
        runtime_binding=config["runtime_binding"],
        warmup=1,
        repeats=3,
    )
    assert inference["latency_median_ms"] > 0.0
    assert inference["latency_p95_ms"] >= inference["latency_median_ms"]
    assert inference["public_states_per_second_at_median"] > 0.0
    bound = compose_resource_receipt(
        trainer.model,
        config_sha256=config_digest(config),
        dataset_sha256=trainer.dataset.digest,
        runtime=config["runtime_binding"],
        training=training,
        inference=inference,
    )
    wrong_runtime = dict(inference)
    wrong_runtime["runtime_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="inference resource receipt binding"):
        compose_resource_receipt(
            trainer.model,
            config_sha256=config_digest(config),
            dataset_sha256=trainer.dataset.digest,
            runtime=config["runtime_binding"],
            training=training,
            inference=wrong_runtime,
        )
    fake_runtime_digest = hashlib.sha256(canonical_bytes(fake_runtime)).hexdigest()
    fake_training = dict(training)
    fake_training["runtime_sha256"] = fake_runtime_digest
    fake_inference = dict(inference)
    fake_inference["runtime_sha256"] = fake_runtime_digest
    with pytest.raises(RuntimeError, match="runtime differs"):
        compose_resource_receipt(
            trainer.model,
            config_sha256=config_digest(config),
            dataset_sha256=trainer.dataset.digest,
            runtime=fake_runtime,
            training=fake_training,
            inference=fake_inference,
        )
    forged_throughput = dict(inference)
    forged_throughput["public_states_per_second_at_median"] = 999999.0
    with pytest.raises(ValueError, match="inference resource receipt throughput"):
        compose_resource_receipt(
            trainer.model,
            config_sha256=config_digest(config),
            dataset_sha256=trainer.dataset.digest,
            runtime=config["runtime_binding"],
            training=training,
            inference=forged_throughput,
        )
    with pytest.raises(ValueError, match="fixed cuda:0"):
        compose_resource_receipt(
            build_model(config["network"], seed=8302),
            config_sha256=config_digest(config),
            dataset_sha256=trainer.dataset.digest,
            runtime=config["runtime_binding"],
            training=training,
            inference=inference,
        )
    receipt_path = tmp_path / "resource-receipt.json"
    assert publish_resource_receipt(receipt_path, bound) == bound["receipt_sha256"]
    assert publish_resource_receipt(receipt_path, bound) == bound["receipt_sha256"]
