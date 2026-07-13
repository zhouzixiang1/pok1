from __future__ import annotations

import json
import os
import stat
import subprocess
import sys

import pytest

from ..decisionholdem_like.a2_runtime import (
    ActionContext,
    SparseBlueprint,
    choose_blueprint_action,
)
from ..decisionholdem_like.blueprint import (
    BlueprintTrainer,
    export_blueprint_atomic,
    verify_blueprint_export,
)


def test_blueprint_training_checkpoint_resume_is_deterministic(tmp_path) -> None:
    uninterrupted = BlueprintTrainer()
    uninterrupted.train_to(5)

    split = BlueprintTrainer()
    split.train_to(2)
    checkpoint = tmp_path / "a2-checkpoint.json"
    split.save_checkpoint(checkpoint)
    resumed = BlueprintTrainer.load_checkpoint(checkpoint)
    resumed.train_to(5)

    assert resumed.checkpoint_payload() == uninterrupted.checkpoint_payload()
    assert resumed.checkpoint_digest() == uninterrupted.checkpoint_digest()
    assert resumed.blueprint_payload() == uninterrupted.blueprint_payload()


def test_sparse_blueprint_is_versioned_and_preserves_fidelity_boundary() -> None:
    trainer = BlueprintTrainer()
    trainer.train_to(3)
    payload = trainer.blueprint_payload()
    blueprint = SparseBlueprint(payload)

    assert len(blueprint.policies) == 140
    assert payload["fidelity"]["lcfr_kernel"] == (
        "paper-faithful-clean-room-small-game"
    )
    assert payload["fidelity"]["national_projection"] == (
        "functional-adaptation-not-decisionholdem-blueprint"
    )
    assert payload["fidelity"]["off_tree"] == (
        "nearest-action-translation-only-not-safe-resolve"
    )


def test_sparse_blueprint_content_drives_legal_action_selection() -> None:
    trainer = BlueprintTrainer()
    trainer.train_to(3)
    context = ActionContext(
        street="preflop",
        pot=150,
        hero_bet=50,
        opponent_bet=100,
        hero_chips=19_950,
        is_small_blind=True,
        hero_action_count=0,
    )
    fold_payload = trainer.blueprint_payload()
    fold_payload["policies"] = {
        key: {"fold": 1.0} for key in fold_payload["policies"]
    }
    call_payload = trainer.blueprint_payload()
    call_payload["policies"] = {
        key: {"check_call": 1.0} for key in call_payload["policies"]
    }
    arguments = {
        "context": context,
        "private_cards": (12, 25),
        "board": (),
        "random_unit": 0.5,
    }
    assert choose_blueprint_action(
        SparseBlueprint(fold_payload), **arguments
    ).action.action_id == "fold"
    assert choose_blueprint_action(
        SparseBlueprint(call_payload), **arguments
    ).action.action_id == "check_call"
    with pytest.raises(ValueError, match="random_unit"):
        choose_blueprint_action(SparseBlueprint(call_payload), **(arguments | {"random_unit": True}))


def test_sparse_blueprint_detaches_validated_content_from_the_input_payload() -> None:
    trainer = BlueprintTrainer()
    trainer.train_to(2)
    payload = trainer.blueprint_payload()
    blueprint = SparseBlueprint(payload)
    digest = blueprint.digest
    payload["fidelity"]["national_projection"] = "mutated-after-validation"
    next(iter(payload["policies"].values())).clear()
    assert blueprint.digest == digest
    assert blueprint.payload["fidelity"]["national_projection"] == (
        "functional-adaptation-not-decisionholdem-blueprint"
    )
    assert all(blueprint.policies.values())


def test_atomic_export_manifest_detects_drift_and_extra_files(tmp_path) -> None:
    trainer = BlueprintTrainer()
    trainer.train_to(3)
    export = tmp_path / "exported-a2"
    manifest = export_blueprint_atomic(trainer, export)

    assert manifest == verify_blueprint_export(export)
    assert (export / "national_bot.py").stat().st_mode & 0o777 == 0o755
    with pytest.raises(FileExistsError):
        export_blueprint_atomic(trainer, export)

    blueprint = export / "blueprint.json"
    payload = json.loads(blueprint.read_text(encoding="utf-8"))
    payload["iterations_completed"] += 1
    blueprint.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="file binding"):
        verify_blueprint_export(export)


def test_export_rejects_unmanifested_files(tmp_path) -> None:
    trainer = BlueprintTrainer()
    trainer.train_to(2)
    export = tmp_path / "exported-a2"
    export_blueprint_atomic(trainer, export)
    (export / "unexpected.bin").write_bytes(b"not bound")
    with pytest.raises(ValueError, match="file set"):
        verify_blueprint_export(export)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload: payload.update({"training_checkpoint_digest": "0"}), "digest"),
        (
            lambda payload: payload["fidelity"].update(
                {"national_projection": "source-faithful"}
            ),
            "fidelity",
        ),
        (
            lambda payload: next(iter(payload["policies"].values())).update(
                {"check_call": "0.5"}
            ),
            "non-numeric",
        ),
    ),
)
def test_sparse_blueprint_rejects_identity_or_type_tampering(
    mutation,
    message: str,
) -> None:
    trainer = BlueprintTrainer()
    trainer.train_to(2)
    payload = trainer.blueprint_payload()
    mutation(payload)
    with pytest.raises(ValueError, match=message):
        SparseBlueprint(payload)


def test_export_verifier_binds_manifest_metadata_and_rejects_symlinks(tmp_path) -> None:
    trainer = BlueprintTrainer()
    trainer.train_to(2)
    export = tmp_path / "exported-a2"
    export_blueprint_atomic(trainer, export)

    manifest_path = export / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entrypoint"] = "a2_runtime.py"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="entrypoint"):
        verify_blueprint_export(export)

    manifest["entrypoint"] = "national_bot.py"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    real_manifest = tmp_path / "manifest.real.json"
    os.replace(manifest_path, real_manifest)
    manifest_path.symlink_to(real_manifest.name)
    with pytest.raises(ValueError, match="manifest must be a real regular file"):
        verify_blueprint_export(export)


def test_exported_native_entry_is_executable_and_self_contained(tmp_path) -> None:
    trainer = BlueprintTrainer()
    trainer.train_to(2)
    export = tmp_path / "exported-a2"
    manifest = export_blueprint_atomic(trainer, export)
    entry = export / "national_bot.py"
    assert stat.S_IMODE(entry.stat().st_mode) == 0o755
    source = entry.read_text(encoding="utf-8")
    assert "from a2_runtime import" in source
    compile(source, str(entry), "exec")
    completed = subprocess.run(
        [sys.executable, str(entry), "--help"],
        cwd=export,
        check=False,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--blueprint" in completed.stdout
    assert not (export / "__pycache__").exists()
    assert verify_blueprint_export(export)["blueprint_digest"] == manifest[
        "blueprint_digest"
    ]
