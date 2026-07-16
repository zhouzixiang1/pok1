"""Fail-closed provenance for the clean-room LLL-informed v143 blueprint."""

from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest

import system_strict_bootstrap as bootstrap


EXPECTED_SHA256 = (
    "a7aef0b3b8b1a0096164631e87f9f1dd0c57b1a95c2738762c9f6301bc434dfb"
)


def test_fresh_receipt_builder_is_importable_from_core_only_cli_path(tmp_path):
    core = Path(bootstrap.__file__).resolve().parent
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(core)!r}); "
        "import system_strict_bootstrap as bootstrap; "
        "receipt = bootstrap.build_fresh_bootstrap_receipt("
        "epoch_reset_receipt_digest='0' * 64); "
        "assert bootstrap.validate_fresh_bootstrap_receipt(receipt) == []; "
        "print(receipt['receipt_digest'])"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert len(completed.stdout.strip()) == 64


def test_blueprint_binds_semantics_without_inheriting_external_authority():
    manifest = bootstrap.load_blueprint_manifest()
    provenance = manifest["provenance"]
    assert manifest["schema_version"] == 3
    assert provenance == bootstrap._BLUEPRINT_PROVENANCE
    assert provenance["source_label"] == "lll/lll/bot/国赛平台代码.py"
    assert provenance["source_sha256"] == EXPECTED_SHA256
    assert provenance["semantic_reference_only"] is True
    for field in (
        "source_bytes_inherited",
        "strength_evidence_inherited",
        "runtime_imported",
        "history_injected",
    ):
        assert provenance[field] is False
    assert bootstrap.validate_blueprint_package() == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_sha256", "0" * 64),
        ("semantic_reference_only", False),
        ("source_bytes_inherited", True),
        ("strength_evidence_inherited", True),
        ("runtime_imported", True),
        ("history_injected", True),
    ],
)
def test_provenance_drift_fails_closed(field, value):
    manifest = deepcopy(bootstrap.load_blueprint_manifest())
    manifest["provenance"][field] = value
    assert "system_bootstrap_provenance_contract_mismatch" in (
        bootstrap.validate_blueprint_package(manifest)
    )


def test_missing_or_extra_provenance_fields_fail_closed():
    missing = deepcopy(bootstrap.load_blueprint_manifest())
    missing["provenance"].pop("history_injected")
    extra = deepcopy(bootstrap.load_blueprint_manifest())
    extra["provenance"]["external_rating"] = 1
    for manifest in (missing, extra):
        assert "system_bootstrap_provenance_contract_mismatch" in (
            bootstrap.validate_blueprint_package(manifest)
        )


def test_production_validation_never_reads_or_resolves_external_reference(
    monkeypatch,
):
    real_open = Path.open
    real_resolve = Path.resolve

    def guarded_open(self, *args, **kwargs):
        assert "国赛平台代码.py" not in str(self)
        return real_open(self, *args, **kwargs)

    def guarded_resolve(self, *args, **kwargs):
        assert "国赛平台代码.py" not in str(self)
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    assert bootstrap.validate_blueprint_package() == []
