"""Archived multi-file precommit repair-target tests."""

import sys
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "web" / "core"))

import tool_planning


def _seed_bot_dirs(tmp_path, monkeypatch, changed_files):
    bots_dir = tmp_path / "bots"
    for version in (98, 102):
        bot_dir = bots_dir / f"national_v{version}"
        bot_dir.mkdir(parents=True)
        for filename in ("strategy.py", "postflop.py", "national_bot.py", "main.py"):
            (bot_dir / filename).write_text("# test\n", encoding="utf-8")

    monkeypatch.setattr(
        tool_planning,
        "get_bot_dir",
        lambda version: bots_dir / f"national_v{version}",
    )
    monkeypatch.setattr(
        tool_planning,
        "_py_files_changed_between",
        lambda _source_dir, _next_dir: list(changed_files),
    )


def _checkpoint(directive="National native TCP precommit FAILED: 0W-1L vs parent"):
    return {
        "stage": "precommit_failed",
        "source_v": 98,
        "next_v": 102,
        "gate_results": {
            "precommit_eval": {
                "directive": directive,
            }
        },
    }


def test_precommit_strategy_regression_excludes_protocol_entrypoints(tmp_path, monkeypatch):
    _seed_bot_dirs(
        tmp_path,
        monkeypatch,
        ["national_bot.py", "main.py", "strategy.py"],
    )

    targets = tool_planning._precommit_repair_target_files(_checkpoint(), "")

    assert targets == ["strategy.py"]


def test_precommit_strategy_regression_falls_back_when_only_protocol_files_changed(tmp_path, monkeypatch):
    _seed_bot_dirs(
        tmp_path,
        monkeypatch,
        ["national_bot.py", "main.py"],
    )

    targets = tool_planning._precommit_repair_target_files(_checkpoint(), "")

    assert targets == ["strategy.py"]


def test_precommit_protocol_evidence_allows_entrypoint_repair(tmp_path, monkeypatch):
    _seed_bot_dirs(
        tmp_path,
        monkeypatch,
        ["national_bot.py", "strategy.py"],
    )
    ckpt = _checkpoint(
        "official smoke reported illegal action format in national_bot.py: "
        "invalid action serialization"
    )

    targets = tool_planning._precommit_repair_target_files(ckpt, "")

    assert targets == ["national_bot.py"]


def test_precommit_protocol_task_uses_compliance_contract():
    ckpt = _checkpoint(
        "official smoke reported illegal wire output in national_bot.py: bet 200"
    )

    task = tool_planning._precommit_repair_task("national_bot.py", ckpt, ckpt["gate_results"]["precommit_eval"]["directive"])

    assert task["role"] == "Protocol Compliance Repair Architect"
    assert task["repair_contract"]["subtype"] == "protocol_compliance"
    assert "compliance oracle" in task["worker_prompt"]
    assert "do not use this task for full-flow strength tuning" in task["worker_prompt"]
    assert "not EV policy files" in task["worker_prompt"]


def test_precommit_strategy_task_warns_protocol_entrypoints_are_out_of_scope():
    ckpt = _checkpoint()

    task = tool_planning._precommit_repair_task("strategy.py", ckpt, ckpt["gate_results"]["precommit_eval"]["directive"])

    assert task["role"] == "Strategic Regression Repair Architect"
    assert "Do not edit or reason around `national_bot.py` or `main.py`" in task["worker_prompt"]
    assert "compliance-only" in task["worker_prompt"]
