from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import calibrate_opponent_multitask_v4_ensemble as calibration  # noqa: E402
import freeze_opponent_role_dataset as freeze  # noqa: E402
from match_outcome_calibration import (  # noqa: E402
    CALIBRATION_METHOD,
    CALIBRATION_SCHEMA,
    calibration_payload_sha256,
)
from match_outcome_schema import match_outcome_metadata  # noqa: E402
import opponent_exposure_ledger as exposure  # noqa: E402
from opponent_response_schema import (  # noqa: E402
    annotate_response_row,
    response_schema_metadata,
)
from role_dataset_access import RoleDatasetAccess  # noqa: E402
import run_opponent_multitask_v4_scaling as scaling  # noqa: E402
import train_opponent_multitask_v4 as v4_trainer  # noqa: E402


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _training_exposure_sha(run_id: str) -> str:
    return _sha(f"formal-training-exposure:{run_id}".encode())


def _scaling_contract(requested: dict) -> dict:
    root = Path("/verified/scaling")
    run_id_prefix = "formal-grid"
    jobs = []
    for scale in requested["scales"]:
        for encoder in requested["encoders"]:
            for seed in requested["seeds"]:
                slug = scaling._slug(scale, encoder, seed)
                jobs.append({
                    "scale": scale,
                    "encoder": encoder,
                    "seed": seed,
                    "slug": slug,
                    "run_id": f"{run_id_prefix}-{slug}",
                    "output_dir": str((root / slug).resolve()),
                    "pythonhashseed": str(seed),
                    "training_environment": {"device": requested["device"]},
                    "command": [sys.executable, str(scaling.TRAINER.resolve())],
                })
    training_options = {
        attribute: 0 for _, attribute in scaling.TRAINING_OPTION_SPECS
    }
    training_options.update({
        "device": requested["device"],
        "cross_transformer_heads": requested["cross_transformer_heads"],
    })
    roles = {
        role: {
            "opponents": [f"national_{role}"],
            "artifact_sha256": (
                "1" * 64 if role == "train" else "2" * 64
            ),
            "files": {
                f"cf_{role}.jsonl": {
                    "rows": 1, "bytes": 1, "sha256": "d" * 64,
                },
                f"opponent_actions_{role}.jsonl": {
                    "rows": 1, "bytes": 1, "sha256": "e" * 64,
                },
            },
        }
        for role in ("train", "early_stop")
    }
    unsigned = {
        "schema": scaling.RUN_CONTRACT_SCHEMA,
        "created_at": "2026-07-12T00:00:00+00:00",
        "output_dir": str(root),
        "role_manifest": "/verified/role_manifest.json",
        "role_manifest_sha256": "b" * 64,
        "ledger": "/verified/ledger.json",
        "run_id_prefix": run_id_prefix,
        "requested": copy.deepcopy(requested),
        "jobs": jobs,
        "allow_incomplete_smoke": False,
        "training_options": training_options,
        "python_executable": sys.executable,
        "trainer": str(scaling.TRAINER.resolve()),
        "trainer_sha256": v4_trainer._code_artifacts()["trainer"]["sha256"],
        "training_code_artifacts": v4_trainer._code_artifacts(),
        "training_roles": {
            "collection_boundary": {},
            "candidate_snapshot": {"name": "candidate", "sha256": "f" * 64},
            "roles": roles,
        },
        "environment": {"device": requested["device"]},
        "git_commit": "1" * 40,
        "summary_schema": scaling.SUMMARY_SCHEMA,
        "model_format": calibration.MODEL_FORMAT,
        "selection_method": calibration.SELECTION_METHOD,
        "selection_key_order": list(calibration.SELECTION_KEY_ORDER),
        "scaling_tool_sha256": calibration._sha256(
            calibration._scaling_tool_path()
        ),
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    return {
        **unsigned,
        "payload_sha256": scaling._canonical_sha256(unsigned),
    }


def _bind_scaling_contract(summary: dict) -> None:
    contract = _scaling_contract(summary["requested"])
    summary.update({
        "created_at": contract["created_at"],
        "role_manifest": contract["role_manifest"],
        "ledger": contract["ledger"],
        "run_contract": contract,
        "run_contract_sha256": contract["payload_sha256"],
    })


def _value_row(role: str, opponent: str, index: int) -> dict:
    mask = [0, 0, 1, 1, 0, 0]
    deltas = [None, None, 0.0, -200.0, None, None]
    profile = [0.25, 0.125, 0.10, 0.30, 0.40, 0.15, 0.05, 0.20,
               0.10, 0.15, 0.15, 0.05]
    cross_hand = [0.25, 0.1, 0.3, 0.4, 0.15, 0.05, 0.2, 0.2,
                  0.2, 0.1, 0.2, 1.0, 0.0, 0.0, 1.0, -0.1]
    return {
        "opponent": opponent,
        "_opponent_label": opponent,
        "_split": role,
        "_evidence_role": role,
        "_collection_hands": 70,
        "deck_seed_base": index * 1000,
        "bot_seed_base": index * 1000 + 1,
        "decision_sampling": "uniform",
        "eligible_decisions": 1,
        "selected_decisions": 1,
        "decision_inclusion_probability": 1.0,
        "decision_inverse_probability_weight": 1.0,
        "legal_mask": mask,
        "rule_label_id": 2,
        "state_features": [0.5] * 48,
        "opponent_profile_features": profile,
        "cross_hand_sequence_schema": "public_opponent_hand_v1",
        "cross_hand_sequence": [cross_hand],
        "baseline_match_net_chips": 100.0,
        "delta_vs_rule": deltas,
        "tail_delta_vs_rule": deltas,
        "match_delta_vs_rule": deltas,
        "match_action_values": [None, None, 100.0, -100.0, None, None],
        "target_masks": {
            field: mask
            for field in (
                "delta_vs_rule", "tail_delta_vs_rule", "match_delta_vs_rule"
            )
        },
        "request": {
            "my_id": 0,
            "dealer_id": 0,
            "my_chips": 19_950,
            "opponent_chips": 19_900,
            "my_stage_bet": 50,
            "opponent_stage_bet": 100,
            "pot": 150,
            "to_call": 50,
            "history": [],
            "my_cards": [0, 4],
            "public_cards": [],
            "remaining_hands": 70,
            "total_win_chips": [0, 0],
        },
        "state": {"round": 0, "pot": 150, "to_call": 50},
        "probes": [],
    }


def _behavior_row(role: str, opponent: str, index: int) -> dict:
    profile = [0.25, 0.125, 0.10, 0.30, 0.40, 0.15, 0.05, 0.20,
               0.10, 0.15, 0.15, 0.05]
    cross_hand = [0.25, 0.1, 0.3, 0.4, 0.15, 0.05, 0.2, 0.2,
                  0.2, 0.1, 0.2, 1.0, 0.0, 0.0, 1.0, -0.1]
    row = {
        "opponent": opponent,
        "_opponent_label": opponent,
        "_split": role,
        "_evidence_role": role,
        "deck_seed_base": index * 1000,
        "bot_seed_base": index * 1000 + 1,
        "stage": "preflop",
        "hero_action": 200,
        "hero_action_label_id": 2,
        "opponent_action": "call",
        "opponent_action_label_id": 2,
        "opponent_action_amount": 100,
        "state_features": [0.5] * 48,
        "opponent_profile_features": profile,
        "cross_hand_sequence_schema": "public_opponent_hand_v1",
        "cross_hand_sequence": [cross_hand],
        "request": {
            "my_id": 0,
            "dealer_id": 0,
            "my_chips": 19_950,
            "opponent_chips": 19_900,
            "my_stage_bet": 50,
            "opponent_stage_bet": 100,
            "pot": 150,
            "to_call": 50,
            "history": [],
            "my_cards": [0, 4],
            "public_cards": [],
            "remaining_hands": 70,
            "total_win_chips": [0, 0],
        },
        "state": {"round": 0, "pot": 150, "to_call": 50},
    }
    return annotate_response_row(row)


def _role_dataset(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "dataset"
    root.mkdir()
    candidate = root / "candidate"
    candidate.mkdir()
    roles = {
        role: [f"national_v{index}"]
        for index, role in enumerate(freeze.EVIDENCE_ROLES, 1)
    }
    outputs = {}
    for index, (role, opponents) in enumerate(roles.items(), 1):
        opponent = opponents[0]
        for prefix, row in (
            ("cf", _value_row(role, opponent, index)),
            ("opponent_actions", _behavior_row(role, opponent, index)),
        ):
            filename = f"{prefix}_{role}.jsonl"
            raw = (json.dumps(row, sort_keys=True) + "\n").encode()
            (root / filename).write_bytes(raw)
            outputs[filename] = {
                "rows": 1,
                "bytes": len(raw),
                "sha256": _sha(raw),
                "opponents": opponents,
                **(
                    {"row_schema": "national_opponent_response_v2"}
                    if prefix == "opponent_actions"
                    else {}
                ),
            }
    manifest = {
        "schema": freeze.SCHEMA,
        "source_collection_complete": False,
        "candidate_snapshot": {
            "path": str(candidate),
            "name": candidate.name,
            "sha256": "f" * 64,
        },
        "strategy_context_runtime_mode": (
            freeze.STRATEGY_CONTEXT_RUNTIME_MODE
        ),
        "roles": roles,
        "outputs": outputs,
        "behavior_supervision": response_schema_metadata(),
        "match_outcome_supervision": match_outcome_metadata(),
        "invariants": {
            "opponent_disjoint": True,
            "match_cluster_disjoint": True,
            "deck_blocks_non_overlapping": True,
            "uniform_decision_ipw_validated": True,
            "national_response_v2_validated": True,
            "national_70_hand_outcome_validated": True,
            "artifact_snapshots_verified": True,
            "final_blind_in_dataset": False,
        },
    }
    manifest_path = root / "role_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, tmp_path / "ledger.json"


def test_real_incomplete_scaling_resume_reuses_strict_training_artifact(
    tmp_path: Path,
) -> None:
    manifest, ledger = _role_dataset(tmp_path)
    output = tmp_path / "scaling"
    command = [
        "--role-manifest", str(manifest),
        "--ledger", str(ledger),
        "--out-dir", str(output),
        "--run-id-prefix", "real-resume",
        "--scales", "small",
        "--encoders", "gru",
        "--seeds", "101",
        "--training-workers", "1",
        "--allow-incomplete-smoke",
        "--epochs", "1",
        "--patience", "1",
        "--batch-size", "1",
        "--device", "cpu",
    ]

    assert scaling.main(command) == 0
    summary_path = output / scaling.SUMMARY_NAME
    first_summary = summary_path.read_bytes()
    checkpoint = output / "small_gru_seed101" / "checkpoint.pt"
    first_checkpoint_sha256 = scaling._sha256(checkpoint)

    assert scaling.main([*command, "--resume", "--training-workers", "2"]) == 0
    assert summary_path.read_bytes() == first_summary
    assert scaling._sha256(checkpoint) == first_checkpoint_sha256
    assert exposure.status(ledger)["events"] == 2

    forbidden = output / "small_gru_seed101" / "policy_selection_result.json"
    forbidden.write_text("{}", encoding="utf-8")
    assert scaling.main([*command, "--resume"]) == 0
    quarantine = output.parent / ".scaling.invalid-jobs"
    quarantined = list(quarantine.glob("small_gru_seed101.invalid-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / forbidden.name).is_file()
    assert not forbidden.exists()
    assert exposure.status(ledger)["events"] == 2

    exposure.open_exposure(
        ledger,
        role="model_calibration",
        opponents=["national_v1"],
        run_id="real-resume-small_gru_seed101",
        artifact_sha256="c" * 64,
    )
    with pytest.raises(SystemExit, match="ledger exposure binding changed"):
        scaling.main([*command, "--resume"])


def _outcome_payload(
    checkpoint: str,
    *,
    seed: int,
    role_manifest: str,
    role_artifact: str,
    opponents: list[str],
) -> dict:
    payload = {
        "schema": CALIBRATION_SCHEMA,
        "method": CALIBRATION_METHOD,
        "scale": 1.0,
        "bias": 0.0,
        "run_id": "chain-1",
        "member_seed": seed,
        "model_format": "opponent_multitask_distributional_outcome_v4",
        "checkpoint_sha256": checkpoint,
        "role_manifest_sha256": role_manifest,
        "calibration_role": "model_calibration",
        "model_calibration_artifact_sha256": role_artifact,
        "model_calibration_opponents": opponents,
        "source_collection_complete": False,
        "metrics": {},
        "action_observations": {},
        "policy_evidence_used": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    payload["payload_sha256"] = calibration_payload_sha256(payload)
    return payload


def test_formal_scaling_selection_requires_all_encoders_and_three_seeds() -> None:
    seeds = [101, 211, 307]
    runs = []
    counter = 1
    for scale in scaling.FORMAL_SCALES:
        for encoder in scaling.FORMAL_ENCODERS:
            for seed in seeds:
                runs.append({
                    "completed": True,
                    "scale": scale,
                    "encoder": encoder,
                    "seed": seed,
                    "selection_key": [0.1 + counter / 1000, 0.2, 0.3, 0.4],
                    "parameters": 100 + len(runs) // len(seeds),
                    "checkpoint_sha256": f"{counter:064x}",
                    "source_collection_complete": True,
                    "source_completed_passes": 160,
                    "source_requested_passes": 160,
                    "training_device": "cuda:0",
                })
                counter += 1
    configurations, selected = scaling.summarize_runs(
        runs, required_seeds=seeds
    )
    assert selected is not None
    summary = {
        "schema": calibration.SUMMARY_SCHEMA,
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_tcp_evaluated": False,
        "selection_key_order": list(calibration.SELECTION_KEY_ORDER),
        "selection_method": calibration.SELECTION_METHOD,
        "scaling_tool_sha256": calibration._sha256(
            calibration._scaling_tool_path()
        ),
        "selection_eligible": True,
        "selected_configuration": selected,
        "provisional_best_configuration": selected,
        "source_collection_complete": True,
        "requested": {
            "scales": list(scaling.FORMAL_SCALES),
            "encoders": list(scaling.FORMAL_ENCODERS),
            "seeds": seeds,
            "configurations": 12,
            "device": "cuda:0",
            "cross_transformer_heads": 4,
        },
        "configurations": configurations,
        "runs": runs,
    }
    _bind_scaling_contract(summary)

    _, runs = calibration.selected_scaling_runs(
        summary, allow_incomplete_smoke=False
    )
    assert [row["seed"] for row in runs] == [101, 211, 307]

    summary["requested"]["scales"] = ["small"]
    summary["requested"]["configurations"] = len(
        summary["requested"]["encoders"]
    )
    _bind_scaling_contract(summary)
    with pytest.raises(ValueError, match="every scale"):
        calibration.selected_scaling_runs(
            summary, allow_incomplete_smoke=False
        )


def _formal_grid_fixture() -> tuple[dict, dict[str, dict]]:
    seeds = [101, 211, 307]
    runs = []
    actual_by_output = {}
    code_artifacts = v4_trainer._code_artifacts()
    counter = 0
    for scale in scaling.FORMAL_SCALES:
        for encoder in scaling.FORMAL_ENCODERS:
            counter += 1
            parameters = counter * 1000
            for seed_index, seed in enumerate(seeds):
                slug = scaling._slug(scale, encoder, seed)
                output_dir = f"/verified/scaling/{slug}"
                selection_key = [
                    0.1 * counter + seed_index / 1000,
                    0.2,
                    0.3,
                    0.4,
                ]
                checkpoint = f"{counter * 10 + seed_index:064x}"
                row = {
                    "scale": scale,
                    "encoder": encoder,
                    "cross_transformer_heads": (
                        4 if encoder == "transformer" else None
                    ),
                    "seed": seed,
                    "run_id": f"formal-grid-{slug}",
                    "output_dir": output_dir,
                    "completed": True,
                    "selection_key": selection_key,
                    "selection_key_order": list(calibration.SELECTION_KEY_ORDER),
                    "parameters": parameters,
                    "checkpoint_sha256": checkpoint,
                    "role_manifest_sha256": "b" * 64,
                    "source_collection_complete": True,
                    "source_completed_passes": 160,
                    "source_requested_passes": 160,
                    "incomplete_smoke": False,
                    "training_device": "cuda:0",
                    "training_exposure_sha256": _training_exposure_sha(
                        f"formal-grid-{slug}"
                    ),
                }
                runs.append(row)
                actual_by_output[output_dir] = {
                    **row,
                    "early_stop_selection_key": selection_key,
                    "model_metadata": {
                        "format": calibration.MODEL_FORMAT,
                        "scale": scale,
                        "cross_encoder": encoder,
                        "parameters": parameters,
                        **({
                            "cross_transformer_heads": 4,
                        } if encoder == "transformer" else {}),
                    },
                    "training_config": {
                        "seed": seed,
                        "scale": scale,
                        "cross_encoder": encoder,
                        "cross_transformer_heads": 4,
                        "epochs": 10,
                    },
                    "training_artifact_sha256": {
                        "train": "1" * 64,
                        "early_stop": "2" * 64,
                    },
                    "code_artifacts": code_artifacts,
                }
    configurations, selected = scaling.summarize_runs(
        runs, required_seeds=seeds
    )
    assert selected is not None
    summary = {
        "schema": calibration.SUMMARY_SCHEMA,
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_tcp_evaluated": False,
        "selection_key_order": list(calibration.SELECTION_KEY_ORDER),
        "selection_method": calibration.SELECTION_METHOD,
        "scaling_tool_sha256": calibration._sha256(
            calibration._scaling_tool_path()
        ),
        "selection_eligible": True,
        "selected_configuration": selected,
        "provisional_best_configuration": selected,
        "source_collection_complete": True,
        "requested": {
            "scales": list(scaling.FORMAL_SCALES),
            "encoders": list(scaling.FORMAL_ENCODERS),
            "seeds": seeds,
            "configurations": 12,
            "device": "cuda:0",
            "cross_transformer_heads": 4,
        },
        "configurations": configurations,
        "runs": runs,
    }
    _bind_scaling_contract(summary)
    planned_jobs = {
        (row["scale"], row["encoder"], row["seed"]): row
        for row in summary["run_contract"]["jobs"]
    }
    contract_roles = summary["run_contract"]["training_roles"]["roles"]
    expected_role_counts = {
        role: {
            "opponents": details["opponents"],
            "value": details["files"][f"cf_{role}.jsonl"]["rows"],
            "behavior": details["files"][
                f"opponent_actions_{role}.jsonl"
            ]["rows"],
            "provenance": {
                "artifact_sha256": details["artifact_sha256"],
                "manifest_sha256": "b" * 64,
                "candidate_sha256": None,
            },
        }
        for role, details in contract_roles.items()
    }
    for actual in actual_by_output.values():
        planned = planned_jobs[
            (actual["scale"], actual["encoder"], actual["seed"])
        ]
        config_arguments = SimpleNamespace(
            **summary["run_contract"]["training_options"]
        )
        config_arguments.scale = actual["scale"]
        config_arguments.cross_encoder = actual["encoder"]
        config_arguments.seed = actual["seed"]
        actual.update({
            "training_command": planned["command"],
            "training_environment": planned["training_environment"],
            "training_config": v4_trainer._config(config_arguments),
            "role_counts": expected_role_counts,
        })
    return summary, actual_by_output


def _install_fake_grid_verifier(
    monkeypatch: pytest.MonkeyPatch, actual_by_output: dict[str, dict]
) -> list[str]:
    visited = []

    def verify(row: dict, **kwargs: object) -> dict:
        assert kwargs["device"] == "cpu"
        assert kwargs["retain_model"] is False
        actual = actual_by_output[row["output_dir"]]
        for field in (
            "scale", "encoder", "seed", "run_id", "output_dir", "completed",
            "selection_key", "selection_key_order", "parameters",
            "checkpoint_sha256", "role_manifest_sha256",
            "source_collection_complete", "source_completed_passes",
            "source_requested_passes", "incomplete_smoke", "training_device",
        ):
            if row.get(field) != actual[field]:
                raise ValueError(f"verified artifact disagrees on {field}")
        visited.append(row["output_dir"])
        return copy.deepcopy(actual)

    monkeypatch.setattr(calibration, "_verified_member", verify)
    monkeypatch.setattr(
        calibration,
        "validate_training_job_exposures",
        lambda ledger_path, *, run_id, role_contracts, ledger_snapshot=None: (
            _training_exposure_sha(run_id)
        ),
    )
    return visited


def test_formal_grid_verifies_every_real_run_and_recomputes_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary, actual = _formal_grid_fixture()
    visited = _install_fake_grid_verifier(monkeypatch, actual)

    proof = calibration.verify_formal_scaling_artifacts(
        summary,
        ledger_path=Path("/verified/ledger.json"),
        role_manifest_sha256="b" * 64,
        training_artifact_sha256={
            "train": "1" * 64, "early_stop": "2" * 64,
        },
    )

    assert set(visited) == set(actual)
    assert len(visited) == 36
    assert proof["configurations"] == summary["configurations"]
    assert proof["selected_configuration"] == summary["selected_configuration"]
    assert all("model" not in row for row in proof["verified_runs"])
    calibration.validate_formal_grid_verification(
        proof,
        ledger_path=Path("/verified/ledger.json"),
        role_manifest_sha256="b" * 64,
        training_artifact_sha256={
            "train": "1" * 64, "early_stop": "2" * 64,
        },
        selected_configuration=summary["selected_configuration"],
    )

    tampered = copy.deepcopy(proof)
    transformer_row = next(
        row
        for row in tampered["verified_runs"]
        if row["encoder"] == "transformer"
    )
    transformer_row["cross_transformer_heads"] = 8
    unsigned = dict(tampered)
    unsigned.pop("payload_sha256")
    tampered["payload_sha256"] = calibration._canonical_sha256(unsigned)
    with pytest.raises(ValueError, match="binding changed"):
        calibration.validate_formal_grid_verification(
            tampered,
            ledger_path=Path("/verified/ledger.json"),
            role_manifest_sha256="b" * 64,
            training_artifact_sha256={
                "train": "1" * 64, "early_stop": "2" * 64,
            },
            selected_configuration=summary["selected_configuration"],
        )


def test_formal_grid_rejects_resigned_run_contract_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary, actual = _formal_grid_fixture()
    _install_fake_grid_verifier(monkeypatch, actual)
    proof = calibration.verify_formal_scaling_artifacts(
        summary,
        ledger_path=Path("/verified/ledger.json"),
        role_manifest_sha256="b" * 64,
        training_artifact_sha256={
            "train": "1" * 64, "early_stop": "2" * 64,
        },
    )

    def replace_run_ids(contract: dict) -> None:
        contract["run_id_prefix"] = "forged-grid"
        for job in contract["jobs"]:
            job["run_id"] = f"forged-grid-{job['slug']}"

    def replace_output_root(contract: dict) -> None:
        contract["output_dir"] = "/forged/scaling"
        for job in contract["jobs"]:
            job["output_dir"] = f"/forged/scaling/{job['slug']}"

    def replace_code(contract: dict) -> None:
        contract["training_code_artifacts"]["trainer"] = {
            "bytes": 1,
            "sha256": "9" * 64,
        }
        contract["trainer_sha256"] = "9" * 64

    mutations = [
        lambda contract: contract.__setitem__("allow_incomplete_smoke", True),
        lambda contract: contract.__setitem__("role_manifest_sha256", "8" * 64),
        lambda contract: contract["training_roles"]["roles"]["train"].__setitem__(
            "artifact_sha256", "7" * 64
        ),
        lambda contract: contract.__setitem__("trainer_sha256", "6" * 64),
        lambda contract: contract.__setitem__("scaling_tool_sha256", "5" * 64),
        lambda contract: contract.__setitem__("ledger", "/forged/ledger.json"),
        replace_run_ids,
        replace_output_root,
        replace_code,
    ]
    for mutate in mutations:
        tampered = copy.deepcopy(proof)
        contract = tampered["scaling_run_contract"]
        mutate(contract)
        unsigned_contract = dict(contract)
        unsigned_contract.pop("payload_sha256")
        contract["payload_sha256"] = calibration._canonical_sha256(
            unsigned_contract
        )
        tampered["scaling_run_contract_sha256"] = contract["payload_sha256"]
        unsigned_proof = dict(tampered)
        unsigned_proof.pop("payload_sha256")
        tampered["payload_sha256"] = calibration._canonical_sha256(
            unsigned_proof
        )
        with pytest.raises(ValueError, match="binding changed"):
            calibration.validate_formal_grid_verification(
                tampered,
                ledger_path=Path("/verified/ledger.json"),
                role_manifest_sha256="b" * 64,
                training_artifact_sha256={
                    "train": "1" * 64, "early_stop": "2" * 64,
                },
                selected_configuration=summary["selected_configuration"],
            )


def test_formal_grid_rejects_resigned_verified_row_contract_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary, actual = _formal_grid_fixture()
    _install_fake_grid_verifier(monkeypatch, actual)
    proof = calibration.verify_formal_scaling_artifacts(
        summary,
        ledger_path=Path("/verified/ledger.json"),
        role_manifest_sha256="b" * 64,
        training_artifact_sha256={
            "train": "1" * 64, "early_stop": "2" * 64,
        },
    )

    mutations = [
        lambda row: row.__setitem__("training_command", ["forged"]),
        lambda row: row.__setitem__("training_environment", {"device": "cuda:9"}),
        lambda row: row["training_config"].__setitem__("dropout", 0.99),
        lambda row: row.__setitem__("role_counts", {}),
        lambda row: row.__setitem__("training_exposure_sha256", "0" * 64),
        lambda row: row.__setitem__("training_code_artifacts_sha256", "0" * 64),
    ]
    for mutate in mutations:
        tampered = copy.deepcopy(proof)
        mutate(tampered["verified_runs"][0])
        unsigned = dict(tampered)
        unsigned.pop("payload_sha256")
        tampered["payload_sha256"] = calibration._canonical_sha256(unsigned)
        with pytest.raises(ValueError, match="binding changed"):
            calibration.validate_formal_grid_verification(
                tampered,
                ledger_path=Path("/verified/ledger.json"),
                role_manifest_sha256="b" * 64,
                training_artifact_sha256={
                    "train": "1" * 64, "early_stop": "2" * 64,
                },
                selected_configuration=summary["selected_configuration"],
            )


def test_formal_grid_rejects_forged_nonselected_run_and_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary, actual = _formal_grid_fixture()
    _install_fake_grid_verifier(monkeypatch, actual)
    selected = summary["selected_configuration"]
    nonselected = next(
        row for row in summary["runs"]
        if (row["scale"], row["encoder"])
        != (selected["scale"], selected["encoder"])
    )
    nonselected["parameters"] += 1
    with pytest.raises(ValueError, match="disagrees on parameters"):
            calibration.verify_formal_scaling_artifacts(
                summary,
                ledger_path=Path("/verified/ledger.json"),
                role_manifest_sha256="b" * 64,
                training_artifact_sha256={
                    "train": "1" * 64, "early_stop": "2" * 64,
                },
            )

    summary, actual = _formal_grid_fixture()
    _install_fake_grid_verifier(monkeypatch, actual)
    original = summary["selected_configuration"]
    summary["selected_configuration"] = next(
        config for config in summary["configurations"] if config != original
    )
    with pytest.raises(ValueError, match="does not match verified artifacts"):
        calibration.verify_formal_scaling_artifacts(
            summary,
            ledger_path=Path("/verified/ledger.json"),
            role_manifest_sha256="b" * 64,
            training_artifact_sha256={
                "train": "1" * 64, "early_stop": "2" * 64,
            },
        )


def test_formal_grid_binds_requested_transformer_heads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary, actual = _formal_grid_fixture()
    _install_fake_grid_verifier(monkeypatch, actual)
    summary["requested"]["cross_transformer_heads"] = 8
    _bind_scaling_contract(summary)

    with pytest.raises(ValueError, match="complete CUDA run"):
        calibration.verify_formal_scaling_artifacts(
            summary,
            ledger_path=Path("/verified/ledger.json"),
            role_manifest_sha256="b" * 64,
            training_artifact_sha256={
                "train": "1" * 64, "early_stop": "2" * 64,
            },
        )


def test_calibration_chain_does_not_read_policy_roles(tmp_path: Path) -> None:
    manifest, ledger_path = _role_dataset(tmp_path)
    dataset = RoleDatasetAccess(
        manifest,
        ledger_path=ledger_path,
        run_id="chain-1",
        require_complete=False,
    )
    training_artifacts = {
        role: dataset._role_artifact_sha256(role)
        for role in ("train", "early_stop")
    }
    members = [{"training_artifact_sha256": training_artifacts}]
    for role in ("policy_selection", "policy_gate"):
        (dataset.root / f"cf_{role}.jsonl").unlink()
        (dataset.root / f"opponent_actions_{role}.jsonl").unlink()

    _, authorization, phase = calibration.prepare_ensemble_calibration_phase(
        dataset,
        members,
        ensemble_manifest_sha256="f" * 64,
    )

    assert authorization["checkpoint_sha256"] == "f" * 64
    assert phase["opened_roles"] == ["model_calibration"]
    ledger = exposure.status(ledger_path)
    opened = {
        row["role"]
        for item in ledger["opponents"].values()
        for row in item["exposures"]
    }
    assert opened == {"train", "early_stop", "model_calibration"}


def test_member_outcome_calibration_binds_exact_provenance() -> None:
    observations = {
        "logits": torch.tensor([-1.0, 1.0]),
        "targets": torch.tensor([0.0, 1.0]),
        "weights": torch.ones(2),
        "action_ids": torch.tensor([2, 3]),
        "source_rows": 1,
    }
    member = {"seed": 101, "checkpoint_sha256": "a" * 64}
    role = {
        "opponents": ["national_v142"],
        "provenance": {"artifact_sha256": "c" * 64},
    }

    payload = calibration._member_outcome_calibration(
        member,
        observations,
        role,
        run_id="chain-1",
        role_manifest_sha256="b" * 64,
        source_collection_complete=False,
        steps=2,
        learning_rate=0.01,
        l2=0.0,
    )

    assert payload["checkpoint_sha256"] == "a" * 64
    assert payload["role_manifest_sha256"] == "b" * 64
    assert payload["calibration_role"] == "model_calibration"
    assert payload["policy_evidence_used"] is False
    assert payload["deployment_policy_value"] is False
    assert payload["strength_evidence"] is False
    assert payload["payload_sha256"] == calibration_payload_sha256(payload)


class _BoundaryRecorder:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def require_collection_boundary(self, expected_passes: int = 160) -> dict:
        self.calls.append(expected_passes)
        return {}


def test_formal_train_and_calibration_require_exact_160_boundary() -> None:
    dataset = _BoundaryRecorder()

    v4_trainer.require_formal_collection_boundary(
        dataset, allow_incomplete_smoke=False
    )
    calibration.require_formal_collection_boundary(dataset, formal=True)
    assert dataset.calls == [160, 160]

    v4_trainer.require_formal_collection_boundary(
        dataset, allow_incomplete_smoke=True
    )
    calibration.require_formal_collection_boundary(dataset, formal=False)
    assert dataset.calls == [160, 160]


def test_formal_calibration_cannot_disable_uncertainty_lcb() -> None:
    calibration.require_formal_uncertainty_contract(1.0, 1.0, formal=True)
    calibration.require_formal_uncertainty_contract(0.0, 0.0, formal=False)

    with pytest.raises(ValueError, match="uncertainty std weights"):
        calibration.require_formal_uncertainty_contract(
            0.0, 1.0, formal=True
        )
    with pytest.raises(ValueError, match="uncertainty std weights"):
        calibration.require_formal_uncertainty_contract(
            1.0, 0.0, formal=True
        )


def test_formal_member_verification_requires_cuda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    members = [
        {
            "seed": seed,
            "checkpoint_sha256": f"{seed:064x}",
            "training_config": {"seed": seed, "scale": "small"},
            "training_artifact_sha256": {"train": "1", "early_stop": "2"},
            "code_artifacts": {"trainer": "same"},
            "model_metadata": {"format": "v4"},
            "source_collection_complete": True,
            "training_device": "cuda:0" if seed != 307 else "cpu",
        }
        for seed in (101, 211, 307)
    ]
    by_seed = {member["seed"]: member for member in members}
    monkeypatch.setattr(
        calibration,
        "_verified_member",
        lambda row, **kwargs: dict(by_seed[int(row["seed"])]),
    )

    with pytest.raises(ValueError, match="trained on CUDA"):
        calibration.verify_members(
            [{"seed": seed} for seed in (101, 211, 307)],
            role_manifest_sha256="b" * 64,
            training_artifact_sha256={"train": "1", "early_stop": "2"},
            device="cpu",
            formal=True,
        )


def test_verified_member_rejects_current_training_code_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkpoint_sha = "a" * 64
    role_sha = "b" * 64
    training_artifacts = {"train": "1" * 64, "early_stop": "2" * 64}
    current_code = {"trainer": {"bytes": 10, "sha256": "3" * 64}}
    root = tmp_path / "member-101"
    root.mkdir()
    for name in (*calibration.EXPECTED_TRAINING_FILES, "artifact_manifest.json"):
        (root / name).write_bytes(b"fixture")
    metadata = {
        "format": calibration.MODEL_FORMAT,
        "scale": "small",
        "cross_encoder": "gru",
        "parameters": 1000,
    }
    config = {
        "seed": 101,
        "scale": "small",
        "cross_encoder": "gru",
        "cross_transformer_heads": 4,
    }
    environment = {"device": "cuda:0"}
    report = {field: None for field in calibration.TRAINING_REPORT_FIELDS}
    report.update({
        "schema": calibration.TRAINING_REPORT_SCHEMA,
        "created_at": "2026-07-12T00:00:00+00:00",
        "run_id": "member-101",
        "command": ["python", "trainer.py"],
        "role_manifest": "/verified/role_manifest.json",
        "ledger": "/verified/ledger.json",
        "opened_roles": ["train", "early_stop"],
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_tcp_evaluated": False,
        "role_manifest_sha256": role_sha,
        "checkpoint_sha256": checkpoint_sha,
        "model": metadata,
        "config": config,
        "environment": environment,
        "role_counts": {},
        "history": [],
        "best_epoch": 1,
        "early_stop": {
            "selection_key": [0.1, 0.2, 0.3, 0.4],
            "selection_key_order": list(calibration.SELECTION_KEY_ORDER),
            "selection_key_is_lexicographic": True,
            "selection_score_is_strength_evidence": False,
        },
        "source_completed_passes": 160,
        "source_requested_passes": 160,
        "source_collection_complete": True,
        "incomplete_smoke": False,
        "code_artifacts": current_code,
        "checkpoint_authorization": {},
    })
    artifact = {field: None for field in calibration.TRAINING_ARTIFACT_FIELDS}
    artifact.update({
        "schema": calibration.TRAINING_ARTIFACT_SCHEMA,
        "run_id": "member-101",
        "files": {
            name: {"bytes": 7, "sha256": "f" * 64}
            for name in calibration.EXPECTED_TRAINING_FILES
        },
        "source_collection_complete": True,
        "deployment_policy_value": False,
        "strength_evidence": False,
    })
    authorization = {
        "schema": calibration.FROZEN_CHECKPOINT_SCHEMA,
        "frozen": True,
        "early_stop_complete": True,
        "run_id": "member-101",
        "role_manifest_sha256": role_sha,
        "training_roles": ["train", "early_stop"],
        "training_artifact_sha256": training_artifacts,
        "checkpoint_sha256": checkpoint_sha,
    }
    checkpoint = {field: None for field in calibration.TRAINING_CHECKPOINT_FIELDS}
    checkpoint.update({
        "schema": calibration.TRAINING_CHECKPOINT_SCHEMA,
        "role_manifest_sha256": role_sha,
        "training_artifact_sha256": training_artifacts,
        "model_metadata": metadata,
        "training_config": config,
        "training_data": calibration.training_data_metadata(),
        "best_epoch": 1,
        "training_environment": environment,
        "code_artifacts": current_code,
        "source_completed_passes": 160,
        "source_requested_passes": 160,
        "source_collection_complete": True,
        "state_dict": {},
    })
    payloads = {
        "artifact_manifest.json": artifact,
        "training_report.json": report,
        "checkpoint_authorization.json": authorization,
    }
    monkeypatch.setattr(
        calibration, "_load_json",
        lambda path, **kwargs: payloads[path.name],
    )
    monkeypatch.setattr(calibration, "_verify_file_contracts", lambda *a, **k: None)
    monkeypatch.setattr(
        calibration, "_sha256",
        lambda path: checkpoint_sha if path.name == "checkpoint.pt" else "f" * 64,
    )
    monkeypatch.setattr(
        calibration, "load_checkpoint", lambda *a, **k: (object(), checkpoint)
    )
    monkeypatch.setattr(
        calibration, "_current_training_code_artifacts", lambda: current_code
    )
    row = {
        "scale": "small",
        "encoder": "gru",
        "seed": 101,
        "run_id": "member-101",
        "output_dir": str(root),
        "completed": True,
        "selection_key": [0.1, 0.2, 0.3, 0.4],
        "selection_key_order": list(calibration.SELECTION_KEY_ORDER),
        "parameters": 1000,
        "checkpoint_sha256": checkpoint_sha,
        "role_manifest_sha256": role_sha,
        "source_completed_passes": 160,
        "source_requested_passes": 160,
        "source_collection_complete": True,
        "incomplete_smoke": False,
        "training_device": "cuda:0",
    }
    verified = calibration._verified_member(
        row,
        role_manifest_sha256=role_sha,
        training_artifact_sha256=training_artifacts,
        device="cpu",
        retain_model=False,
    )
    assert "model" not in verified

    stale_code = {"trainer": {"bytes": 9, "sha256": "4" * 64}}
    report["code_artifacts"] = stale_code
    checkpoint["code_artifacts"] = stale_code
    with pytest.raises(ValueError, match="training code artifacts changed"):
        calibration._verified_member(
            row,
            role_manifest_sha256=role_sha,
            training_artifact_sha256=training_artifacts,
            device="cpu",
            retain_model=False,
        )


class _LoaderDataset:
    run_id = "chain-1"
    manifest_sha256 = "b" * 64
    manifest = {"source_collection_complete": False}
    roles = {"model_calibration": ["national_v3"]}

    def _role_artifact_sha256(self, role: str) -> str:
        return {
            "train": "1" * 64,
            "early_stop": "2" * 64,
            "model_calibration": "3" * 64,
        }[role]


def test_strict_loader_checks_boundary_before_artifact_reads(tmp_path: Path) -> None:
    class _RejectBoundary(_LoaderDataset):
        def require_collection_boundary(self, expected_passes: int = 160) -> dict:
            assert expected_passes == 160
            raise ValueError("boundary checked first")

    with pytest.raises(ValueError, match="boundary checked first"):
        calibration.load_calibrated_ensemble(
            tmp_path / "missing-calibration",
            dataset=_RejectBoundary(),
            run_id="chain-1",
            device="cpu",
            formal=True,
        )


def test_strict_loader_returns_outcome_calibration_on_each_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "calibration"
    root.mkdir()
    checkpoint = "a" * 64
    member = {
        "seed": 101,
        "run_id": "member-101",
        "output_dir": "/tmp/member-101",
        "checkpoint_sha256": checkpoint,
        "checkpoint_authorization_sha256": "4" * 64,
        "training_report_sha256": "5" * 64,
        "training_artifact_manifest_sha256": "6" * 64,
        "scale": "small",
        "encoder": "gru",
        "parameters": 1000,
        "role_manifest_sha256": "b" * 64,
        "selection_key_order": list(calibration.SELECTION_KEY_ORDER),
        "early_stop_selection_key": [0.1, 0.2, 0.3, 0.4],
        "source_completed_passes": None,
        "source_requested_passes": None,
        "source_collection_complete": False,
        "incomplete_smoke": True,
        "training_device": "cpu",
    }
    ensemble = {
        "schema": calibration.ENSEMBLE_MANIFEST_SCHEMA,
        "run_id": "chain-1",
        "role_manifest_sha256": "b" * 64,
        "selected_configuration": {
            "scale": "small", "encoder": "gru", "requested_seeds": [101],
            "median_selection_key": [0.1, 0.2, 0.3, 0.4],
            "mean_selection_key": [0.1, 0.2, 0.3, 0.4],
            "worst_selection_key": [0.1, 0.2, 0.3, 0.4],
        },
        "members": [member],
        "model_format": "opponent_multitask_distributional_outcome_v4",
        "selection_key_order": list(calibration.SELECTION_KEY_ORDER),
        "selection_method": calibration.SELECTION_METHOD,
        "scaling_tool_sha256": calibration._sha256(
            calibration._scaling_tool_path()
        ),
        "source_collection_complete": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_tcp_evaluated": False,
    }
    calibration._write_json(root / "ensemble_checkpoint_manifest.json", ensemble)
    ensemble_sha = calibration._sha256(
        root / "ensemble_checkpoint_manifest.json"
    )
    outcome = _outcome_payload(
        checkpoint,
        seed=101,
        role_manifest="b" * 64,
        role_artifact="3" * 64,
        opponents=["national_v3"],
    )
    value_lower = {
        "target_preprocessing": "symmetric_clip_before_residual",
        "target_clips": {
            field: 2000.0 for field in calibration.VALUE_FIELDS
        },
        "fields": {
            field: {"offsets": [0.0] * 6}
            for field in calibration.VALUE_FIELDS
        },
    }
    payload = {
        "schema": calibration.ENSEMBLE_CALIBRATION_SCHEMA,
        "run_id": "chain-1",
        "role_manifest_sha256": "b" * 64,
        "checkpoint_sha256": ensemble_sha,
        "calibration_role": "model_calibration",
        "calibration_artifact_sha256": "3" * 64,
        "opponents": ["national_v3"],
        "value_lower": value_lower,
        "response_temperature": {"temperature": 1.0},
        "policy_evidence_used": False,
        "ensemble": {
            "manifest_sha256": ensemble_sha,
            "members": [{"seed": 101, "checkpoint_sha256": checkpoint}],
            "value_lower_aggregation": (
                "mean_member_quantile_minus_mean_value_std"
            ),
            "lower_quantile": 0.2,
            "uncertainty_std_weight": 1.0,
            "response_aggregation": "mean_member_logits_then_temperature",
            "outcome_aggregation": calibration.OUTCOME_AGGREGATION_METHOD,
            "outcome_uncertainty_std_weight": 1.25,
            "outcome_calibration_payload_sha256": [outcome["payload_sha256"]],
        },
        "outcome_calibrations": [outcome],
        "source_collection_complete": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    payload["payload_sha256"] = calibration._canonical_sha256(payload)
    calibration._write_json(root / "calibration.json", payload)
    authorization = {
        "schema": calibration.FROZEN_CHECKPOINT_SCHEMA,
        "frozen": True,
        "early_stop_complete": True,
        "run_id": "chain-1",
        "role_manifest_sha256": "b" * 64,
        "training_roles": ["train", "early_stop"],
        "training_artifact_sha256": {
            "train": "1" * 64, "early_stop": "2" * 64,
        },
        "checkpoint_sha256": ensemble_sha,
    }
    calibration._write_json(root / "checkpoint_authorization.json", authorization)
    report = {
        "schema": calibration.CALIBRATION_REPORT_SCHEMA,
        "run_id": "chain-1",
        "role_manifest_sha256": "b" * 64,
        "ensemble_manifest_sha256": ensemble_sha,
        "calibration_payload_sha256": payload["payload_sha256"],
        "member_checkpoint_sha256": [checkpoint],
        "opened_roles": ["train", "early_stop", "model_calibration"],
        "policy_roles_opened": False,
        "formal_selection": False,
        "source_collection_complete": False,
        "incomplete_smoke": True,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_tcp_evaluated": False,
        "calibration_tool_sha256": calibration._sha256(
            Path(calibration.__file__).resolve()
        ),
    }
    calibration._write_json(root / "calibration_report.json", report)
    files = calibration.EXPECTED_CALIBRATION_FILES
    calibration._write_json(root / "artifact_manifest.json", {
        "schema": calibration.ARTIFACT_MANIFEST_SCHEMA,
        "run_id": "chain-1",
        "files": {
            name: {
                "bytes": (root / name).stat().st_size,
                "sha256": calibration._sha256(root / name),
            }
            for name in files
        },
        "policy_roles_opened": False,
        "calibration_tool_sha256": calibration._sha256(
            Path(calibration.__file__).resolve()
        ),
        "deployment_policy_value": False,
        "strength_evidence": False,
    })
    fake_member = {
        "seed": 101,
        "checkpoint_sha256": checkpoint,
        "checkpoint_path": "/tmp/checkpoint.pt",
        "early_stop_selection_key": [0.1, 0.2, 0.3, 0.4],
        "training_device": "cpu",
        "model": object(),
    }
    monkeypatch.setattr(
        calibration, "verify_members", lambda *args, **kwargs: [fake_member]
    )

    loaded = calibration.load_calibrated_ensemble(
        root,
        dataset=_LoaderDataset(),
        run_id="chain-1",
        device="cpu",
        formal=False,
    )

    assert loaded["outcome_uncertainty_std_weight"] == 1.25
    assert loaded["outcome_calibrations"] == [outcome]
    assert loaded["members"][0]["outcome_calibration"] == outcome

    report["calibration_tool_sha256"] = "f" * 64
    calibration._write_json(root / "calibration_report.json", report)
    artifact = calibration._load_json(
        root / "artifact_manifest.json", field="artifact manifest"
    )
    artifact["files"]["calibration_report.json"] = {
        "bytes": (root / "calibration_report.json").stat().st_size,
        "sha256": calibration._sha256(root / "calibration_report.json"),
    }
    calibration._write_json(root / "artifact_manifest.json", artifact)
    with pytest.raises(ValueError, match="bindings are invalid"):
        calibration.load_calibrated_ensemble(
            root,
            dataset=_LoaderDataset(),
            run_id="chain-1",
            device="cpu",
            formal=False,
        )
