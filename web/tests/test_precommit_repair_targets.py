"""Strict policy-epoch precommit repair routing tests."""

from pathlib import Path

import pytest

from bot_namespace import STRICT_ARTIFACT_FILES, strict_artifact_layout_errors
import tool_planning


def _write_strict_bot(root: Path) -> Path:
    root.mkdir(parents=True)
    payloads = {
        "national_bot.py": "# system runtime\n",
        "precompute.py": "FACT = 1\n",
        "policy.py": "def get_baseline_decision(context): return {'kind': 'pass'}\n",
        "national_runtime_manifest.json": "{}\n",
        "policy_epoch_receipt.json": "{}\n",
    }
    assert frozenset(payloads) == STRICT_ARTIFACT_FILES
    for relative, payload in payloads.items():
        (root / relative).write_text(payload, encoding="utf-8")
    assert strict_artifact_layout_errors(root) == []
    return root


def _seed_bot_dirs(tmp_path, monkeypatch, changed_files):
    bots = tmp_path / "bots"
    _write_strict_bot(bots / "national_v143")
    _write_strict_bot(bots / "national_v144")
    monkeypatch.setattr(
        tool_planning,
        "get_bot_dir",
        lambda version: bots / f"national_v{version}",
    )
    monkeypatch.setattr(
        tool_planning,
        "_py_files_changed_between",
        lambda _source, _candidate: list(changed_files),
    )


def _checkpoint(directive="National TCP precommit FAILED: 0W-1L vs parent"):
    return {
        "stage": "precommit_failed",
        "source_v": 143,
        "next_v": 144,
        "gate_results": {"precommit_eval": {"directive": directive}},
    }


def test_strategy_regression_routes_only_to_policy(tmp_path, monkeypatch):
    _seed_bot_dirs(
        tmp_path,
        monkeypatch,
        ["national_bot.py", "precompute.py", "policy.py"],
    )
    assert tool_planning._precommit_repair_target_files(_checkpoint(), "") == [
        "policy.py"
    ]


def test_system_only_diff_falls_back_to_policy(tmp_path, monkeypatch):
    _seed_bot_dirs(
        tmp_path,
        monkeypatch,
        ["national_bot.py", "precompute.py"],
    )
    assert tool_planning._precommit_repair_target_files(_checkpoint(), "") == [
        "policy.py"
    ]


def test_protocol_failure_never_grants_system_runtime_write_authority(
    tmp_path, monkeypatch
):
    _seed_bot_dirs(tmp_path, monkeypatch, ["policy.py"])
    checkpoint = _checkpoint(
        "official smoke reported illegal wire action serialization in national_bot.py"
    )
    assert tool_planning._precommit_repair_target_files(checkpoint, "") == []
    with pytest.raises(ValueError, match="system/extra artifact"):
        tool_planning._precommit_repair_task(
            "national_bot.py",
            checkpoint,
            checkpoint["gate_results"]["precommit_eval"]["directive"],
        )


def test_policy_repair_task_preserves_system_owned_runtime():
    checkpoint = _checkpoint()
    feedback = checkpoint["gate_results"]["precommit_eval"]["directive"]
    task = tool_planning._precommit_repair_task("policy.py", checkpoint, feedback)

    assert task["role"] == "Strategic Regression Repair Architect"
    assert task["target_files"] == ["policy.py"]
    assert task["repair_contract"]["file"] == "policy.py"
    assert "`policy.py` is the sole writable file" in task["worker_prompt"]
    assert "national_bot.py and precompute.py remain byte-identical" in task[
        "worker_prompt"
    ]
    assert "decision_context.hand.position" in task["worker_prompt"]


def test_compatibility_flag_cannot_expand_policy_write_scope():
    assert tool_planning._precommit_filter_repair_targets(
        ["national_bot.py", "precompute.py", "policy.py", "helper.py"],
        allow_protocol_files=True,
    ) == ["policy.py"]
