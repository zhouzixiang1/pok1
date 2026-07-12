from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import run_opponent_multitask_v4_scaling as scaling  # noqa: E402
from opponent_multitask_model_v4 import MODEL_FORMAT  # noqa: E402
from train_opponent_multitask_v4 import REPORT_SCHEMA  # noqa: E402


def _fake_contract(
    args, *, root, scales, encoders, seeds, created_at,
    ledger_snapshot=None,
):
    del ledger_snapshot
    requested = {
        "scales": list(scales),
        "encoders": list(encoders),
        "seeds": list(seeds),
        "configurations": len(scales) * len(encoders),
        "device": str(args.device),
        "cross_transformer_heads": int(args.cross_transformer_heads),
    }
    jobs = []
    for scale in scales:
        for encoder in encoders:
            for seed in seeds:
                slug = scaling._slug(scale, encoder, seed)
                jobs.append({
                    "scale": scale,
                    "encoder": encoder,
                    "seed": seed,
                    "slug": slug,
                    "run_id": f"{args.run_id_prefix}-{slug}",
                    "output_dir": str((root / slug).resolve()),
                    "pythonhashseed": str(seed),
                    "training_environment": {"device": str(args.device)},
                    "command": [
                        sys.executable,
                        str(scaling.TRAINER.resolve()),
                    ],
                })
    role_contracts = {
        role: {
            "opponents": [f"national_{role}"],
            "artifact_sha256": "c" * 64,
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
    intent_run_id = (
        f"{args.run_id_prefix}-{scaling.SCALING_CONTRACT_INTENT_SUFFIX}"
    )
    intent_events = [
        {
            "sequence": index,
            "timestamp_utc": "2026-07-12T00:00:00+00:00",
            "event": "open",
            "role": role,
            "run_id": intent_run_id,
            "opponents": role_contracts[role]["opponents"],
            "candidate_sha256": None,
            "artifact_sha256": role_contracts[role]["artifact_sha256"],
        }
        for index, role in enumerate(("train", "early_stop"), start=1)
    ]
    intent_exposure_sha256 = scaling._canonical_sha256({
        "schema": scaling.TRAINING_EXPOSURE_RECEIPT_SCHEMA,
        "run_id": intent_run_id,
        "events": intent_events,
    })
    training_options = {
        attribute: getattr(args, attribute)
        for _, attribute in scaling.TRAINING_OPTION_SPECS
    }
    unsigned = {
        "schema": scaling.RUN_CONTRACT_SCHEMA,
        "created_at": created_at,
        "output_dir": str(root),
        "role_manifest": str(args.role_manifest.resolve()),
        "role_manifest_sha256": "a" * 64,
        "ledger": str(args.ledger.resolve()),
        "run_id_prefix": args.run_id_prefix,
        "requested": requested,
        "jobs": jobs,
        "allow_incomplete_smoke": bool(args.allow_incomplete_smoke),
        "training_options": training_options,
        "python_executable": sys.executable,
        "trainer": str(scaling.TRAINER.resolve()),
        "trainer_sha256": "b" * 64,
        "training_code_artifacts": {
            "trainer": {"bytes": 1, "sha256": "b" * 64},
        },
        "training_roles": {
            "collection_boundary": {
                scaling.SCALING_CONTRACT_INTENT_FIELD: {
                    "schema": scaling.SCALING_CONTRACT_INTENT_SCHEMA,
                    "run_id": intent_run_id,
                    "events": intent_events,
                    "exposure_sha256": intent_exposure_sha256,
                },
            },
            "candidate_snapshot": {"name": "candidate", "sha256": "f" * 64},
            "roles": role_contracts,
        },
        "environment": {"device": str(args.device)},
        "git_commit": "1" * 40,
        "summary_schema": scaling.SUMMARY_SCHEMA,
        "model_format": MODEL_FORMAT,
        "selection_method": scaling.SELECTION_METHOD,
        "selection_key_order": list(scaling.SELECTION_KEY_ORDER),
        "scaling_tool_sha256": scaling._sha256(Path(scaling.__file__)),
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    return {
        **unsigned,
        "payload_sha256": scaling._canonical_sha256(unsigned),
    }


def _completed_smoke_row(root: Path) -> dict:
    slug = "small_gru_seed101"
    return {
        "scale": "small",
        "encoder": "gru",
        "seed": 101,
        "slug": slug,
        "run_id": f"resume-test-{slug}",
        "output_dir": str(root / slug),
        "log": str(root / f"{slug}.log"),
        "returncode": 0,
        "completed": True,
        "selection_key": [0.1, 0.2, 0.3, 0.4],
        "selection_key_order": list(scaling.SELECTION_KEY_ORDER),
        "best_epoch": 1,
        "parameters": 100,
        "checkpoint_sha256": "a" * 64,
        "role_manifest_sha256": "b" * 64,
        "source_collection_complete": False,
        "source_completed_passes": 1,
        "source_requested_passes": 160,
        "incomplete_smoke": True,
        "training_device": "cpu",
        "training_exposure_sha256": "e" * 64,
        "cross_transformer_heads": None,
    }


def _exposure_event(
    sequence: int,
    *,
    run_id: str,
    role: str,
    opponents: list[str],
    event: str = "open",
    candidate_sha256: str | None = None,
    artifact_sha256: str | None = None,
) -> dict:
    return {
        "sequence": sequence,
        "timestamp_utc": "2026-07-12T00:00:00+00:00",
        "event": event,
        "role": role,
        "run_id": run_id,
        "opponents": opponents,
        "candidate_sha256": candidate_sha256,
        "artifact_sha256": artifact_sha256,
    }


def _write_ledger(path: Path, events: list[dict]) -> None:
    path.write_text(json.dumps({
        "schema": scaling.EXPOSURE_LEDGER_SCHEMA,
        "events": events,
    }), encoding="utf-8")


def _training_role_contracts() -> dict:
    return {
        "train": {
            "opponents": ["national_v1", "national_v2"],
            "artifact_sha256": "1" * 64,
        },
        "early_stop": {
            "opponents": ["national_v3"],
            "artifact_sha256": "2" * 64,
        },
    }


def _role_contract_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SimpleNamespace, SimpleNamespace, set[Path]]:
    root = tmp_path / "roles"
    root.mkdir()
    role_names = {
        "train": ["national_v1", "national_v2"],
        "early_stop": ["national_v3"],
    }
    artifact_sha256 = {
        "train": "1" * 64,
        "early_stop": "2" * 64,
    }
    outputs = {}
    role_paths = set()
    for role in scaling.MODEL_TRAINING_ROLES:
        for prefix in ("cf", "opponent_actions"):
            filename = f"{prefix}_{role}.jsonl"
            path = root / filename
            path.write_text(f'{{"role":"{role}"}}\n', encoding="utf-8")
            role_paths.add(path)
            outputs[filename] = {
                "rows": 1,
                "bytes": path.stat().st_size,
                "sha256": scaling._sha256(path),
            }
    dataset = SimpleNamespace(
        root=root,
        manifest={
            "source_completed_passes": 8,
            "source_requested_passes": 160,
            "source_collection_complete": False,
        },
        roles=role_names,
        outputs=outputs,
        candidate_snapshot={"name": "candidate", "sha256": "f" * 64},
    )
    dataset._role_artifact_sha256 = lambda role: artifact_sha256[role]
    dataset.require_collection_boundary = lambda expected_passes=160: {
        "source_completed_passes": expected_passes,
        "source_requested_passes": expected_passes,
        "source_collection_complete": True,
    }
    args = SimpleNamespace(
        role_manifest=root / "role_manifest.json",
        ledger=tmp_path / "ledger.json",
        run_id_prefix="exposure-first",
        allow_incomplete_smoke=True,
    )

    def dataset_factory(
        manifest_path, *, ledger_path, run_id, require_complete
    ):
        assert manifest_path == args.role_manifest
        assert ledger_path == args.ledger
        assert run_id == scaling._scaling_contract_intent_run_id(args)
        assert require_complete is False
        return dataset

    monkeypatch.setattr(scaling, "RoleDatasetAccess", dataset_factory)
    return args, dataset, role_paths


def _row(
    scale: str,
    encoder: str,
    seed: int,
    key: list[float],
    *,
    complete_source: bool = True,
    device: str = "cuda",
) -> dict:
    return {
        "scale": scale,
        "encoder": encoder,
        "seed": seed,
        "completed": True,
        "selection_key": key,
        "parameters": 100 if encoder == "gru" else 200,
        "source_collection_complete": complete_source,
        "source_completed_passes": 160 if complete_source else 159,
        "source_requested_passes": 160,
        "incomplete_smoke": not complete_source,
        "training_device": device,
    }


def test_scaling_aggregates_every_v4_selection_key_component() -> None:
    seeds = [101, 211, 307]
    rows = [
        *[
            _row("small", "gru", seed, [0.20, 0.01, 0.01, 0.01])
            for seed in seeds
        ],
        *[
            _row("small", "deep_set", seed, [0.10, 0.99, 0.99, 0.99])
            for seed in seeds
        ],
    ]

    configurations, selected = scaling.summarize_runs(
        rows, required_seeds=seeds
    )

    assert selected is not None
    assert selected["encoder"] == "deep_set"
    assert selected["median_selection_key"] == [0.10, 0.99, 0.99, 0.99]
    assert all(
        len(row["median_selection_key"]) == len(scaling.SELECTION_KEY_ORDER)
        for row in configurations
    )


def test_scaling_rejects_parameter_drift_across_seeds() -> None:
    rows = [
        _row("small", "gru", seed, [0.1] * 4)
        for seed in (101, 211, 307)
    ]
    rows[-1]["parameters"] += 1

    configurations, selected = scaling.summarize_runs(
        rows, required_seeds=[101, 211, 307]
    )

    assert configurations[0]["parameters_consistent"] is False
    assert configurations[0]["parameters"] is None
    assert selected is None


def test_formal_scaling_requires_all_encoders_three_seeds_and_complete_source() -> None:
    seeds = [101, 211, 307]
    rows = [
        *[_row(scale, encoder, seed, [0.1] * 4)
          for scale in scaling.FORMAL_SCALES
          for encoder in scaling.FORMAL_ENCODERS
          for seed in seeds],
    ]
    configurations, selected = scaling.summarize_runs(
        rows, required_seeds=seeds
    )

    assert scaling.formal_selection_allowed(
        rows,
        configurations,
        selected,
        allow_incomplete_smoke=False,
    )
    assert not scaling.formal_selection_allowed(
        rows,
        [row for row in configurations if row["scale"] == "small"],
        selected,
        allow_incomplete_smoke=False,
    )
    without_transformer = [
        row for row in configurations if row["encoder"] != "transformer"
    ]
    assert not scaling.formal_selection_allowed(
        rows,
        without_transformer,
        selected,
        allow_incomplete_smoke=False,
    )
    missing_pair_rows = [
        row
        for row in rows
        if (row["scale"], row["encoder"]) != ("small", "deep_set")
    ]
    missing_pair_configurations, missing_pair_selected = scaling.summarize_runs(
        missing_pair_rows, required_seeds=seeds
    )
    assert not scaling.formal_selection_allowed(
        missing_pair_rows,
        missing_pair_configurations,
        missing_pair_selected,
        allow_incomplete_smoke=False,
    )
    rows[0]["source_collection_complete"] = False
    assert not scaling.formal_selection_allowed(
        rows,
        configurations,
        selected,
        allow_incomplete_smoke=False,
    )
    assert not scaling.formal_selection_allowed(
        rows,
        configurations,
        selected,
        allow_incomplete_smoke=True,
    )


def test_formal_scaling_rejects_cpu_training_rows() -> None:
    seeds = [101, 211, 307]
    rows = [
        _row(scale, encoder, seed, [0.1] * 4)
        for scale in scaling.FORMAL_SCALES
        for encoder in scaling.FORMAL_ENCODERS
        for seed in seeds
    ]
    configurations, selected = scaling.summarize_runs(
        rows, required_seeds=seeds
    )
    rows[0]["training_device"] = "cpu"

    assert not scaling.formal_selection_allowed(
        rows,
        configurations,
        selected,
        allow_incomplete_smoke=False,
    )
    rows[0]["training_device"] = "cuda:0"
    assert scaling.formal_selection_allowed(
        rows,
        configurations,
        selected,
        allow_incomplete_smoke=False,
    )


def test_training_report_device_must_match_requested_device() -> None:
    report = {
        "schema": REPORT_SCHEMA,
        "run_id": "run-1",
        "opened_roles": ["train", "early_stop"],
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_tcp_evaluated": False,
        "model": {
            "format": MODEL_FORMAT,
            "scale": "small",
            "cross_encoder": "gru",
        },
        "config": {"seed": 101},
        "environment": {"device": "cuda"},
        "early_stop": {
            "selection_key": [0.1, 0.2, 0.3, 0.4],
            "selection_key_order": list(scaling.SELECTION_KEY_ORDER),
            "selection_key_is_lexicographic": True,
            "selection_score_is_strength_evidence": False,
        },
    }

    assert scaling.validate_training_report(
        report,
        scale="small",
        encoder="gru",
        seed=101,
        run_id="run-1",
        device="cuda",
    ) == [0.1, 0.2, 0.3, 0.4]
    with pytest.raises(ValueError, match="role contract"):
        scaling.validate_training_report(
            report,
            scale="small",
            encoder="gru",
            seed=101,
            run_id="run-1",
            device="cpu",
        )


def test_transformer_training_report_binds_head_count() -> None:
    report = {
        "schema": REPORT_SCHEMA,
        "run_id": "run-transformer",
        "opened_roles": ["train", "early_stop"],
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_tcp_evaluated": False,
        "model": {
            "format": MODEL_FORMAT,
            "scale": "small",
            "cross_encoder": "transformer",
            "cross_transformer_heads": 4,
        },
        "config": {"seed": 101, "cross_transformer_heads": 4},
        "environment": {"device": "cuda"},
        "early_stop": {
            "selection_key": [0.1, 0.2, 0.3, 0.4],
            "selection_key_order": list(scaling.SELECTION_KEY_ORDER),
            "selection_key_is_lexicographic": True,
            "selection_score_is_strength_evidence": False,
        },
    }

    assert scaling.validate_training_report(
        report,
        scale="small",
        encoder="transformer",
        seed=101,
        run_id="run-transformer",
        device="cuda",
        transformer_heads=4,
    ) == [0.1, 0.2, 0.3, 0.4]
    report["model"]["cross_transformer_heads"] = 8
    with pytest.raises(ValueError, match="role contract"):
        scaling.validate_training_report(
            report,
            scale="small",
            encoder="transformer",
            seed=101,
            run_id="run-transformer",
            device="cuda",
            transformer_heads=4,
        )


def test_scaling_run_contract_rejects_self_hashed_tampering(
    tmp_path: Path,
) -> None:
    options = {
        attribute: 0 for _, attribute in scaling.TRAINING_OPTION_SPECS
    }
    options.update({"device": "cpu", "cross_transformer_heads": 4})
    args = SimpleNamespace(
        **options,
        role_manifest=tmp_path / "role_manifest.json",
        ledger=tmp_path / "ledger.json",
        run_id_prefix="contract-test",
        allow_incomplete_smoke=True,
    )
    contract = _fake_contract(
        args,
        root=tmp_path / "sweep",
        scales=["small"],
        encoders=["gru"],
        seeds=[101],
        created_at="2026-07-12T00:00:00+00:00",
    )

    assert scaling.validate_run_contract(contract) == contract
    tampered = copy.deepcopy(contract)
    tampered["requested"]["seeds"] = [307]
    with pytest.raises(ValueError, match="binding changed"):
        scaling.validate_run_contract(tampered)


def test_formal_contract_rejects_removed_and_resigned_exposure_intent(
    tmp_path: Path,
) -> None:
    options = {
        attribute: 0 for _, attribute in scaling.TRAINING_OPTION_SPECS
    }
    options.update({"device": "cuda", "cross_transformer_heads": 4})
    args = SimpleNamespace(
        **options,
        role_manifest=tmp_path / "role_manifest.json",
        ledger=tmp_path / "ledger.json",
        run_id_prefix="formal-contract-test",
        allow_incomplete_smoke=False,
    )
    contract = _fake_contract(
        args,
        root=tmp_path / "sweep",
        scales=["small"],
        encoders=["gru"],
        seeds=[101],
        created_at="2026-07-12T00:00:00+00:00",
    )
    assert scaling.validate_run_contract(contract) == contract

    tampered = copy.deepcopy(contract)
    del tampered["training_roles"]["collection_boundary"][
        scaling.SCALING_CONTRACT_INTENT_FIELD
    ]
    unsigned = dict(tampered)
    unsigned.pop("payload_sha256")
    tampered["payload_sha256"] = scaling._canonical_sha256(unsigned)
    with pytest.raises(ValueError, match="contract intent is missing"):
        scaling.validate_run_contract(tampered)


def test_training_role_contract_opens_both_roles_before_any_jsonl_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _dataset, role_paths = _role_contract_fixture(tmp_path, monkeypatch)
    opened = []
    real_open = scaling.open_exposure
    real_stat = Path.stat

    def tracked_open(*call_args, **call_kwargs):
        result = real_open(*call_args, **call_kwargs)
        opened.append(call_kwargs["role"])
        return result

    def guarded_stat(path, *call_args, **call_kwargs):
        if path in role_paths:
            assert opened == list(scaling.MODEL_TRAINING_ROLES)
        return real_stat(path, *call_args, **call_kwargs)

    monkeypatch.setattr(scaling, "open_exposure", tracked_open)
    monkeypatch.setattr(Path, "stat", guarded_stat)

    contract = scaling._training_role_contract(args)

    assert opened == ["train", "early_stop"]
    intent = contract["collection_boundary"][
        scaling.SCALING_CONTRACT_INTENT_FIELD
    ]
    assert intent["run_id"] == scaling._scaling_contract_intent_run_id(args)
    assert [event["role"] for event in intent["events"]] == [
        "train", "early_stop"
    ]
    assert scaling._is_sha256(intent["exposure_sha256"])


def test_training_role_contract_failure_keeps_both_intents_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, dataset, _role_paths = _role_contract_fixture(tmp_path, monkeypatch)
    (dataset.root / "cf_train.jsonl").write_text("changed\n", encoding="utf-8")

    for _ in range(2):
        with pytest.raises(ValueError, match="training role artifact changed"):
            scaling._training_role_contract(args)

    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    assert [event["role"] for event in ledger["events"]] == [
        "train", "early_stop"
    ]
    assert all(
        event["run_id"] == scaling._scaling_contract_intent_run_id(args)
        for event in ledger["events"]
    )


def test_second_intent_failure_keeps_first_exposure_without_reading_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _dataset, role_paths = _role_contract_fixture(tmp_path, monkeypatch)
    real_open = scaling.open_exposure
    real_stat = Path.stat
    role_stats = 0

    def fail_second(*call_args, **call_kwargs):
        if call_kwargs["role"] == "early_stop":
            raise ValueError("second intent failed")
        return real_open(*call_args, **call_kwargs)

    def guarded_stat(path, *call_args, **call_kwargs):
        nonlocal role_stats
        if path in role_paths:
            role_stats += 1
        return real_stat(path, *call_args, **call_kwargs)

    monkeypatch.setattr(scaling, "open_exposure", fail_second)
    monkeypatch.setattr(Path, "stat", guarded_stat)
    with pytest.raises(ValueError, match="second intent failed"):
        scaling._training_role_contract(args)

    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    assert [event["role"] for event in ledger["events"]] == ["train"]
    assert role_stats == 0


def test_training_role_contract_rejects_intent_drift_before_jsonl_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, _dataset, role_paths = _role_contract_fixture(tmp_path, monkeypatch)
    scaling._training_role_contract(args)
    scaling.open_exposure(
        args.ledger,
        role="model_calibration",
        opponents=["national_v4"],
        run_id=scaling._scaling_contract_intent_run_id(args),
        artifact_sha256="4" * 64,
    )
    role_stats = 0
    real_stat = Path.stat

    def guarded_stat(path, *call_args, **call_kwargs):
        nonlocal role_stats
        if path in role_paths:
            role_stats += 1
        return real_stat(path, *call_args, **call_kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    with pytest.raises(
        scaling.ProtectedExposureError, match="exposure binding changed"
    ):
        scaling._training_role_contract(args)
    assert role_stats == 0


def test_resume_reuses_verified_jobs_and_rebuilds_identical_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sweep"
    role_manifest = tmp_path / "role_manifest.json"
    ledger = tmp_path / "ledger.json"
    calls = []

    monkeypatch.setattr(scaling, "build_run_contract", _fake_contract)

    def run_one(args, *, root, scale, encoder, seed, contract):
        calls.append((scale, encoder, seed))
        (root / scaling._slug(scale, encoder, seed)).mkdir()
        return _completed_smoke_row(root)

    def reuse(
        args, *, root, scale, encoder, seed, contract, ledger_snapshot=None
    ):
        assert (root / scaling._slug(scale, encoder, seed)).is_dir()
        return _completed_smoke_row(root)

    monkeypatch.setattr(scaling, "_run_one", run_one)
    monkeypatch.setattr(scaling, "validated_completed_row", reuse)
    monkeypatch.setattr(
        scaling,
        "locked_exposure_ledger_snapshot",
        lambda path: scaling.nullcontext({}),
    )
    base = [
        "--role-manifest", str(role_manifest),
        "--ledger", str(ledger),
        "--out-dir", str(root),
        "--run-id-prefix", "resume-test",
        "--scales", "small",
        "--encoders", "gru",
        "--seeds", "101",
        "--allow-incomplete-smoke",
        "--device", "cpu",
    ]

    assert scaling.main(base) == 0
    assert calls == [("small", "gru", 101)]
    first_summary = (root / scaling.SUMMARY_NAME).read_bytes()

    assert scaling.main([*base, "--resume", "--training-workers", "2"]) == 0
    assert calls == [("small", "gru", 101)]
    assert (root / scaling.SUMMARY_NAME).read_bytes() == first_summary

    with pytest.raises(SystemExit, match="arguments or provenance changed"):
        scaling.main([*base, "--resume", "--dropout", "0.2"])

    (root / "foreign.partial").write_text("not authoritative", encoding="utf-8")
    with pytest.raises(SystemExit, match="unexpected.*resume entry"):
        scaling.main([*base, "--resume"])


def test_resume_requires_an_existing_valid_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "legacy-sweep"
    root.mkdir()
    monkeypatch.setattr(scaling, "build_run_contract", _fake_contract)

    with pytest.raises(SystemExit, match="invalid v4 scaling run contract"):
        scaling.main([
            "--role-manifest", str(tmp_path / "role_manifest.json"),
            "--ledger", str(tmp_path / "ledger.json"),
            "--out-dir", str(root),
            "--run-id-prefix", "resume-test",
            "--scales", "small",
            "--encoders", "gru",
            "--seeds", "101",
            "--allow-incomplete-smoke",
            "--device", "cpu",
            "--resume",
        ])


def test_scaling_root_lock_rejects_concurrent_runner(tmp_path: Path) -> None:
    root = tmp_path / "locked-sweep"
    first = scaling.acquire_run_lock(root)
    try:
        with pytest.raises(ValueError, match="is locked"):
            scaling.acquire_run_lock(root)
    finally:
        first.close()

    second = scaling.acquire_run_lock(root)
    second.close()


def test_scaling_main_releases_root_lock_after_setup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "failed-sweep"
    monkeypatch.setattr(
        scaling,
        "prepare_run_root",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("setup failed")),
    )

    with pytest.raises(SystemExit, match="setup failed"):
        scaling.main([
            "--role-manifest", str(tmp_path / "role_manifest.json"),
            "--ledger", str(tmp_path / "ledger.json"),
            "--out-dir", str(root),
            "--run-id-prefix", "lock-release",
            "--scales", "small",
            "--encoders", "gru",
            "--seeds", "101",
            "--allow-incomplete-smoke",
            "--device", "cpu",
        ])

    lock = scaling.acquire_run_lock(root)
    lock.close()


def test_training_exposure_receipt_uses_exact_raw_group_events(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.json"
    run_id = "formal-small-gru-101"
    roles = _training_role_contracts()
    events = [
        _exposure_event(
            1,
            run_id=run_id,
            role="train",
            opponents=roles["train"]["opponents"],
            artifact_sha256=roles["train"]["artifact_sha256"],
        ),
        _exposure_event(
            2,
            run_id="unrelated-job",
            role="train",
            opponents=["national_v9"],
            artifact_sha256="9" * 64,
        ),
        _exposure_event(
            3,
            run_id=run_id,
            role="early_stop",
            opponents=roles["early_stop"]["opponents"],
            artifact_sha256=roles["early_stop"]["artifact_sha256"],
        ),
    ]
    _write_ledger(ledger, events)

    receipt = scaling.validate_training_job_exposures(
        ledger, run_id=run_id, role_contracts=roles
    )
    events.append(_exposure_event(
        4,
        run_id="another-unrelated-job",
        role="early_stop",
        opponents=["national_v8"],
        artifact_sha256="8" * 64,
    ))
    _write_ledger(ledger, events)
    assert scaling.validate_training_job_exposures(
        ledger, run_id=run_id, role_contracts=roles
    ) == receipt

    events.append(_exposure_event(
        5,
        run_id=run_id,
        role="model_calibration",
        opponents=["national_v4"],
        artifact_sha256="4" * 64,
    ))
    _write_ledger(ledger, events)
    with pytest.raises(
        scaling.ProtectedExposureError, match="exposure binding changed"
    ):
        scaling.validate_training_job_exposures(
            ledger, run_id=run_id, role_contracts=roles
        )


def test_training_exposure_receipt_rejects_group_and_ledger_tampering(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.json"
    run_id = "formal-small-gru-101"
    roles = _training_role_contracts()
    valid = [
        _exposure_event(
            1,
            run_id=run_id,
            role="train",
            opponents=roles["train"]["opponents"],
            artifact_sha256=roles["train"]["artifact_sha256"],
        ),
        _exposure_event(
            2,
            run_id=run_id,
            role="early_stop",
            opponents=roles["early_stop"]["opponents"],
            artifact_sha256=roles["early_stop"]["artifact_sha256"],
        ),
    ]
    attacks = []

    reversed_roles = copy.deepcopy(valid)
    reversed_roles[0]["role"], reversed_roles[1]["role"] = (
        reversed_roles[1]["role"], reversed_roles[0]["role"]
    )
    attacks.append(reversed_roles)

    split_group = copy.deepcopy(valid)
    split_group[0]["opponents"] = ["national_v1"]
    attacks.append(split_group)

    candidate_bound = copy.deepcopy(valid)
    candidate_bound[0]["candidate_sha256"] = "c" * 64
    attacks.append(candidate_bound)

    wrong_artifact = copy.deepcopy(valid)
    wrong_artifact[1]["artifact_sha256"] = "f" * 64
    attacks.append(wrong_artifact)

    bad_sequence = copy.deepcopy(valid)
    bad_sequence[1]["sequence"] = True
    attacks.append(bad_sequence)

    extra_field = copy.deepcopy(valid)
    extra_field[0]["forged"] = True
    attacks.append(extra_field)

    reserve = copy.deepcopy(valid)
    reserve.insert(0, _exposure_event(
        1,
        run_id=run_id,
        role=scaling.FINAL_BLIND_ROLE,
        opponents=["national_v7"],
        event="reserve",
        candidate_sha256="7" * 64,
    ))
    for index, event in enumerate(reserve, start=1):
        event["sequence"] = index
    attacks.append(reserve)

    for events in attacks:
        _write_ledger(ledger, events)
        with pytest.raises(scaling.ProtectedExposureError):
            scaling.validate_training_job_exposures(
                ledger, run_id=run_id, role_contracts=roles
            )


def test_resume_quarantines_only_dead_canonical_training_temporaries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sweep"
    root.mkdir()
    dead = root / ".small_gru_seed101.tmp-99999999"
    dead.mkdir()
    expected_name = f"{dead.name}.abandoned-{dead.stat().st_mtime_ns}"

    scaling.quarantine_stale_training_temporaries(
        root, slugs={"small_gru_seed101"}
    )

    assert not dead.exists()
    quarantine = tmp_path / ".sweep.abandoned-partials"
    assert [path.name for path in quarantine.iterdir()] == [expected_name]

    active = root / f".small_gru_seed101.tmp-{os.getpid()}"
    active.mkdir()
    with pytest.raises(ValueError, match="live PID"):
        scaling.quarantine_stale_training_temporaries(
            root, slugs={"small_gru_seed101"}
        )


def test_resume_quarantines_interrupted_atomic_metadata_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sweep"
    root.mkdir()
    stale = root / f".{scaling.SUMMARY_NAME}.tmp-interrupted"
    stale.write_text("partial", encoding="utf-8")
    expected_name = f"{stale.name}.abandoned-{stale.stat().st_mtime_ns}"

    scaling.quarantine_stale_metadata_temporaries(root)

    assert not stale.exists()
    quarantine = tmp_path / ".sweep.abandoned-partials"
    assert [path.name for path in quarantine.iterdir()] == [expected_name]
