from __future__ import annotations

import ast
import sys

import pytest

from ..tools import m5a_manifest


def test_committed_m5a_manifest_is_complete_and_self_consistent() -> None:
    assert m5a_manifest.verify_manifest() == []


def test_dynamic_drift_invalidates_m5a_manifest(
    monkeypatch,
) -> None:
    recorded = m5a_manifest.load_manifest()
    drifted = {
        field: recorded[field]
        for field in ("artifact", "frozen_m4", "import_audit", "m5a_files")
    }
    drifted["frozen_m4"] = dict(drifted["frozen_m4"])
    drifted["frozen_m4"]["files_sha256"] = "0" * 64
    monkeypatch.setattr(m5a_manifest, "build_dynamic_snapshot", lambda: drifted)
    assert (
        "frozen_m4 snapshot differs from current files"
        in m5a_manifest.verify_manifest()
    )


def test_route_collection_does_not_import_legacy_backend() -> None:
    forbidden = sorted(
        name
        for name in sys.modules
        if name == "engine"
        or name.startswith("engine.")
        or name == "sever.bot_adapter"
        or name.startswith("sever.bot_adapter.")
    )
    assert forbidden == []


def test_complete_route_ast_has_no_legacy_backend_import() -> None:
    audit = m5a_manifest.build_import_audit()
    assert audit["forbidden_imports"] == {}
    assert audit["legacy_adapter_imported"] is False
    assert audit["top_level_engine_imported"] is False


def test_frozen_m4_bytes_are_pinned_to_the_audited_base(
    monkeypatch,
) -> None:
    drifted = dict(m5a_manifest.EXPECTED_FROZEN_M4_HASHES)
    drifted["evidence/m4_scale_gate.json"] = "0" * 64
    monkeypatch.setattr(
        m5a_manifest,
        "stable_selected_file_map",
        lambda _root, _paths: drifted,
    )
    with pytest.raises(ValueError, match="audited BASE_M4_COMMIT"):
        m5a_manifest.build_frozen_m4_snapshot()


@pytest.mark.parametrize(
    "source",
    (
        "from .engine import battle",
        "from .. import engine",
        "from .sever import bot_adapter",
        "from ..sever.bot_adapter import main",
    ),
)
def test_relative_imports_cannot_escape_legacy_backend_audit(source: str) -> None:
    imported = m5a_manifest._import_names(ast.parse(source))
    assert any(m5a_manifest._forbidden_import(name) for name in imported)
