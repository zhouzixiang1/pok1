"""Crossover prepare must bind the policy payload's source_bot to a semantic name.

Root cause of the v447+ ``prepared_architecture_policy_digest_mismatch`` loop:
crossover prepare builds the frozen policy from the content-addressed snapshot
directory (64-hex basename), so the payload's ``source_bot`` was that hex name,
while the Master-entry gate recomputes the payload from the live
``bots/national_cloud_vNN`` directory. The digests could never match.
``build_architecture_policy(source_bot_label=...)`` (mirroring
``build_prepared_capability_snapshot``'s ``parent_bot_label``) lets the
prepare-side caller bind the canonical semantic identity.
"""

from __future__ import annotations

import copy

import runtime_architecture_policy
from runtime_architecture_policy import (
    RUNTIME_FLOOR_CHECKS,
    build_architecture_policy,
)


def _minimal_capabilities():
    check_ids = sorted(set(RUNTIME_FLOOR_CHECKS) | {"precompute_runtime_influence"})
    checks = [
        {
            "check_id": check_id,
            "name": check_id,
            "passed": True,
            "required": check_id in RUNTIME_FLOOR_CHECKS,
            "guidance": check_id,
            "evidence": {},
        }
        for check_id in check_ids
    ]
    return {
        "schema_version": 2,
        "epoch": runtime_architecture_policy.ACTIVE_EPOCH,
        "conclusive": True,
        "ok": True,
        "outcome": "passed",
        "checks": checks,
        "checks_by_id": {item["check_id"]: item for item in checks},
        "required_checks": sorted(RUNTIME_FLOOR_CHECKS),
        "required_failures": [],
        "advisory_warnings": [],
        "infrastructure_failures": [],
    }


def test_source_bot_label_binds_frozen_dir_to_semantic_identity(tmp_path):
    hex_name = ("684c4001" + "0" * 56)[:64]
    hex_dir = tmp_path / hex_name
    sem_dir = tmp_path / "national_cloud_v88"
    hex_dir.mkdir()
    sem_dir.mkdir()
    caps = _minimal_capabilities()

    # Backcompat anchor: without a label the payload keeps deriving the
    # identity from the directory (the asymmetric behavior that produced the
    # mismatch loop is preserved for callers that do not bind a label).
    unlabelled_hex = build_architecture_policy(
        hex_dir, source_capabilities=copy.deepcopy(caps)
    )
    unlabelled_sem = build_architecture_policy(
        sem_dir, source_capabilities=copy.deepcopy(caps)
    )
    assert unlabelled_hex["source_bot"] == hex_dir.name
    assert unlabelled_sem["source_bot"] == sem_dir.name
    assert unlabelled_hex["policy_digest"] != unlabelled_sem["policy_digest"]

    # The fix: a labelled build of the frozen hex directory reproduces the
    # live-directory digest byte for byte.
    labelled = build_architecture_policy(
        hex_dir,
        source_capabilities=copy.deepcopy(caps),
        source_bot_label="national_cloud_v88",
    )
    assert labelled["source_bot"] == "national_cloud_v88"
    assert labelled["policy_digest"] == unlabelled_sem["policy_digest"]
    # The omitted-label digest is unchanged by the parameter's existence.
    assert unlabelled_hex["policy_digest"] == build_architecture_policy(
        hex_dir, source_capabilities=copy.deepcopy(caps)
    )["policy_digest"]


def test_prepare_call_site_binds_semantic_label():
    """Static wiring guard: the crossover prepare call must pass the label."""

    source = (
        __import__("pathlib")
        .Path(__import__("runtime_architecture_policy").__file__)
        .resolve()
        .parent
        / "tool_commit.py"
    )
    text = source.read_text(encoding="utf-8")
    assert "source_bot_label=bot_name(parent_a)" in text
