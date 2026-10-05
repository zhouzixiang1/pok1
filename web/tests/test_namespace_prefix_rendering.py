"""Namespace-prefix neutrality of every role-prompt rendering site (P3).

The cloud epoch runs under ``ACTIVE_BOT_PREFIX=national_cloud_v`` (conftest
sets ``POK_CLOUD_RUNTIME=1`` for the whole suite), yet six rendering sites
still hardcoded the main-branch ``national_v`` prefix (agent_workers.py:64,
master_plan_audit.md:97/99, reviewer_prompt.md:33, critic_prompt.md:47,
crossover_prompt.md:56-57, tool_planning_worker_phases_rework.py:1413).
v511's Worker io carried ``bots/national_v511`` 24 times and v510's plan
audit rejection quoted the mis-rendered master_plan_audit.md line verbatim.

Contract: every role prompt is rendered from ``bot_name(version)`` /
``ACTIVE_BOT_PREFIX`` — never a literal ``national_v`` prefix — so the same
templates serve the cloud epoch and the main ``national_v`` line unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bot_namespace import bot_name


ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_BOT_DIR = re.compile(r"bots/national_v\d")
FORBIDDEN_BARE_LABEL = re.compile(r"(?<![a-z_])national_v\d")
# A literal main-branch prefix followed by a template/f-string placeholder
# (e.g. ``bots/national_v{next_v}``) bakes the namespace into the prompt.
FORBIDDEN_PREFIX_PLACEHOLDER = re.compile(r"national_v[a-zA-Z_]*\{")

TEMPLATES = [
    "web/core/prompts/master_plan_audit.md",
    "web/core/prompts/reviewer_prompt.md",
    "web/core/prompts/critic_prompt.md",
    "web/core/prompts/crossover_prompt.md",
]

RENDER_SOURCES = [
    "web/core/agent_workers.py",
    "web/core/tool_planning_worker_phases_rework.py",
]


def test_prompt_templates_carry_no_main_namespace_prefix():
    for rel in TEMPLATES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "national_v" not in text, (
            f"{rel} hardcodes the main-branch namespace prefix; render the "
            "active bot name from an injected variable instead"
        )


def test_render_sources_carry_no_main_namespace_prefix_placeholder():
    for rel in RENDER_SOURCES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        hits = FORBIDDEN_PREFIX_PLACEHOLDER.findall(text)
        assert not hits, f"{rel} interpolates a literal national_v prefix: {hits}"


def _assert_namespace_neutral(rendered: str, *, next_v: int, source_v: int):
    assert FORBIDDEN_BOT_DIR.search(rendered) is None
    assert FORBIDDEN_BARE_LABEL.search(rendered) is None
    assert bot_name(next_v) in rendered or bot_name(source_v) in rendered


def test_master_plan_audit_prompt_renders_active_namespace():
    from audit_agents import _render_master_plan_audit_provider_prompt

    material = _render_master_plan_audit_provider_prompt({
        "source_v": 10,
        "next_v": 11,
        "master_plan": {"tasks": []},
        "recent_commits": "",
        "direction_audit": "",
        "h2h_snapshot_contract": "",
        "recent_directions": "",
    })
    text = material.text
    assert f"bots/{bot_name(11)}/policy.py" in text
    assert f"bots/{bot_name(10)}/" in text
    _assert_namespace_neutral(text, next_v=11, source_v=10)


def test_reviewer_prompt_renders_active_namespace():
    import tool_gates
    from tool_gates_critic_review import _render_reviewer_provider_prompt

    subject = {"review_semantic_mode": "strategy_implementation_v1"}
    semantic_contract = dict(subject)
    semantic_contract["contract_digest"] = tool_gates._canonical_digest(subject)
    material = _render_reviewer_provider_prompt({
        "master_plan": {"tasks": []},
        "source_v": 10,
        "next_v": 11,
        "strict_bootstrap": False,
        "invocation_id": "",
        "authority_slot": "review",
        "focus_areas": [],
        "review_semantic_contract": semantic_contract,
    })
    text = material.text
    assert f"bots/{bot_name(11)}/" in text
    _assert_namespace_neutral(text, next_v=11, source_v=10)


def test_critic_prompt_renders_active_namespace():
    from agent_review import _render_critic_provider_prompt

    material = _render_critic_provider_prompt({
        "source_v": 10,
        "next_v": 11,
        "master_plan": "{}",
        "code_evidence": {
            "lineage_contract": "",
            "evaluation_steps": "",
            "prompt_section": "",
        },
        "h2h_snapshot_contract": "",
        "previous_critic": None,
        "invocation_id": "",
    })
    text = material.text
    assert f"bots/{bot_name(11)}/" in text
    _assert_namespace_neutral(text, next_v=11, source_v=10)


def test_crossover_prompt_renders_active_namespace():
    from agent_review import _render_crossover_provider_prompt

    material = _render_crossover_provider_prompt({
        "parent_a_v": 9,
        "parent_b_v": 10,
        "target_v": 11,
        "parent_artifacts": ["a" * 64, "b" * 64],
        "compatibility_receipt": {},
        "capability_context": {},
        "h2h_snapshot_contract": "",
        "architecture_policy": {},
        "frozen_parent_a_dir": "/tmp/parent-a",
        "frozen_parent_b_dir": "/tmp/parent-b",
        "retry_feedback": "",
    })
    text = material.text
    assert f"`{bot_name(9)}`" in text
    assert f"`{bot_name(10)}`" in text
    _assert_namespace_neutral(text, next_v=11, source_v=9)


def test_worker_prompt_renders_active_namespace(monkeypatch, tmp_path):
    import strategy_reference_pack

    monkeypatch.setattr(
        strategy_reference_pack,
        "current_strict_runtime_prompt_overlay",
        lambda: "",
    )
    from agent_workers import _render_worker_provider_prompt

    material = _render_worker_provider_prompt({
        "task": {"id": "t1", "title": "adjust river sizing"},
        "next_v": 11,
        "source_v": 10,
        "candidate_path": str(tmp_path / "lease"),
        "allowed_files": ["policy.py"],
        "reviewer_feedback": "",
        "attempt_note": "",
        "retry_guidance": "",
        "role": "worker",
    })
    text = material.text
    # The lease-isolation preamble must alias the ACTIVE namespace bot dir.
    assert f"bots/{bot_name(11)}" in text
    _assert_namespace_neutral(text, next_v=11, source_v=10)


def test_main_branch_namespace_still_renders(monkeypatch):
    """The same renderers keep the main ``national_v`` line intact."""

    import bot_namespace

    monkeypatch.setattr(bot_namespace, "ACTIVE_BOT_PREFIX", "national_v")
    assert bot_name(11) == "national_v11"
