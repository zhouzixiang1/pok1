import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


CORE = Path(__file__).resolve().parents[1] / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def _strict_artifact(root: Path, version: int, *, action: str = "pass") -> Path:
    from bot_namespace import refresh_policy_identity_documents

    root.mkdir(parents=True, exist_ok=True)
    (root / "national_bot.py").write_text(
        "from policy import decide\n",
        encoding="utf-8",
    )
    (root / "policy.py").write_text(
        "def decide(_context):\n"
        f"    return {{'kind': {action!r}}}\n",
        encoding="utf-8",
    )
    (root / "precompute.py").write_text("TABLE = ()\n", encoding="utf-8")
    (root / "national_runtime_manifest.json").write_text("{}\n", encoding="utf-8")
    (root / "policy_epoch_receipt.json").write_text("{}\n", encoding="utf-8")
    refresh_policy_identity_documents(
        root,
        version,
        parent_versions=() if version == 143 else (version - 1,),
    )
    return root


def _published_parent(version: int) -> SimpleNamespace:
    return SimpleNamespace(
        eligible=True,
        version=version,
        issues=(),
        runtime_manifest={"epoch": "national_tcp_policy_v1", "version": version},
        epoch_receipt={"epoch": "national_tcp_policy_v1", "version": version},
        publication_identity={
            "published": True,
            "tag": f"national-bot-v{version}",
            "version": version,
        },
        certificate_digest="b" * 64,
    )


def _resolve_published_parent(name: str, **_kwargs) -> SimpleNamespace:
    return _published_parent(int(str(name).rsplit("national_v", 1)[1]))


def _strict_checkpoint(
    next_v: int,
    source_v: int,
    stage: str,
    *,
    parent2_v: int | None = None,
    **extra,
) -> dict:
    import checkpoint_schema

    audit_context = dict(extra.pop("audit_context", {}) or {})
    if next_v == 143:
        from system_strict_bootstrap import build_fresh_bootstrap_receipt

        audit_context.setdefault(
            "protocol_bootstrap",
            build_fresh_bootstrap_receipt(
                active_bots=(),
                epoch_reset_receipt_digest="a" * 64,
            ),
        )
    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=next_v,
        source_v=source_v,
        parent2_v=parent2_v,
        audit_context=audit_context,
        parent_resolver=_resolve_published_parent,
    )
    return {
        "checkpoint_schema_version": checkpoint_schema.CHECKPOINT_SCHEMA_VERSION,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": binding,
        "next_v": next_v,
        "source_v": source_v,
        "parent2_v": parent2_v,
        "stage": stage,
        "workflow_run_id": f"generation:{next_v}:runtime-governance-test",
        "checkpoint_revision": 1,
        "audit_context": audit_context,
        **extra,
    }


@pytest.fixture(autouse=True)
def _hermetic_strict_parent_resolution(monkeypatch):
    import checkpoint_schema

    monkeypatch.setattr(
        checkpoint_schema,
        "resolve_national_bot_spec",
        _resolve_published_parent,
    )


def test_literature_probe_cache_roundtrip(tmp_path, monkeypatch):
    import evolution_infra
    import tool_planning

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    payload = {
        "next_v": 300,
        "source_v": 299,
        "proposal": {"claim": "c", "target_fn": "f", "numeric_claim": "+1", "source_url": "u"},
        "candidate_id": "research-1",
        "gated_out": False,
        "reason": "completed",
        "weakness": "vs station",
        "stagnation_info": "flat WR",
    }

    tool_planning._write_literature_probe_cache(300, payload)
    cached = tool_planning._read_literature_probe_cache(
        300,
        source_v=299,
        h2h_weakness="vs station",
        stagnation_info="flat WR",
    )

    assert cached["cached"] is True
    assert cached["candidate_id"] == "research-1"
    assert cached["context_fingerprint"]
    assert "Research Proposal" in cached["inject_text"]


def test_literature_probe_cache_rejects_context_mismatch(tmp_path, monkeypatch):
    import evolution_infra
    import tool_planning

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    payload = {
        "next_v": 300,
        "source_v": 299,
        "proposal": {"claim": "c", "target_fn": "f", "numeric_claim": "+1", "source_url": "u"},
        "candidate_id": "research-1",
        "gated_out": False,
        "reason": "completed",
        "weakness": "vs station",
        "stagnation_info": "flat WR",
    }

    tool_planning._write_literature_probe_cache(300, payload)

    assert tool_planning._read_literature_probe_cache(
        300,
        source_v=298,
        h2h_weakness="vs station",
        stagnation_info="flat WR",
    ) is None
    assert tool_planning._read_literature_probe_cache(
        300,
        source_v=299,
        h2h_weakness="different weakness",
        stagnation_info="flat WR",
    ) is None


def test_literature_probe_rejects_old_checkpoint_receipt_after_context_refresh(tmp_path, monkeypatch):
    import asyncio
    import evolution_infra
    from master_context_contract import build_master_context
    from pipeline_state import literature_probe_receipt_binding
    import tool_planning

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path / "results")
    original_context = build_master_context(
        next_v=300,
        source_v=299,
        stagnation_info="STAGNATION_DETECTED (is_stagnant=true)",
        match_analysis="original weakness",
    )
    checkpoint = {
        "next_v": 300,
        "source_v": 299,
        "stage": "direction_audited",
        "direction_audit": {
            "repetition_detected": True,
            "suggested_direction": "original weakness",
        },
        "audit_context": {"master_context": original_context},
    }
    binding, errors = literature_probe_receipt_binding(checkpoint)
    assert not errors
    payload = {
        "next_v": 300,
        "source_v": 299,
        "proposal": {"claim": "c", "target_fn": "f", "numeric_claim": "+1", "source_url": "u"},
        "candidate_id": "research-1",
        "reason": "completed",
        "weakness": "original weakness",
        "stagnation_info": "original stagnation",
        "context_fingerprint": tool_planning._literature_probe_context_fingerprint(
            299, "original weakness", "original stagnation"
        ),
        **binding,
    }
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "prepared",
        audit_context={"master_context": original_context},
    )
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "direction_audited",
        direction_audit=checkpoint["direction_audit"],
        literature_probe=payload,
    )
    refreshed_context = build_master_context(
        next_v=300,
        source_v=299,
        stagnation_info="STAGNATION_DETECTED (is_stagnant=true)",
        match_analysis="refreshed weakness",
    )
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "direction_audited",
        audit_context={"master_context": refreshed_context},
    )

    import research_governance
    monkeypatch.setattr(research_governance, "should_trigger_web_retrieval", lambda _v: False)

    result = asyncio.run(tool_planning.run_literature_probe.handler({
        "next_v": 300,
        "source_v": 299,
        "h2h_weakness": "slightly different resumed weakness",
        "stagnation_info": "slightly different resumed stagnation",
    }))
    data = json.loads(result["content"][0]["text"])

    assert data.get("cached") is not True
    assert data["reason"] == "web retrieval in cooldown or disabled by governance"
    assert data["master_context_digest"] == refreshed_context["context_digest"]
    assert data["master_context_digest"] != payload["master_context_digest"]
    persisted = evolution_infra.read_pipeline_checkpoint()["literature_probe"]
    assert persisted["requirement_context_digest"] == data["requirement_context_digest"]


def test_plan_compiler_externalizes_oversized_worker_prompt(tmp_path):
    import plan_compiler

    target = _strict_artifact(tmp_path / "national_v300", 300)
    long_prompt = "Implement this carefully.\n" + ("detail " * 2500)
    plan = {
        "tasks": [
            {
                "worker_id": 1,
                "role": "Algorithmic Logic Architect",
                "target_files": ["policy.py"],
                "worker_prompt": long_prompt,
            }
        ]
    }

    compiled, meta = plan_compiler.compile_master_plan(
        plan,
        next_v=300,
        target_dir=target,
        project_root=tmp_path,
    )

    task = compiled["tasks"][0]
    assert meta["compiled"] is True
    assert task["worker_prompt_compiled"] is True
    assert len(task["worker_prompt"]) < plan_compiler.HARD_WORKER_PROMPT_CHARS
    assert "task_brief_file" in task
    assert (tmp_path / task["task_brief_file"]).exists()
    assert compiled["plan_compiler"]["compiled_tasks"][0]["original_chars"] == len(long_prompt)

    # Defense-in-depth callers may compile an already compiled checkpoint plan.
    # That pass must not delete the only externalized brief and leave a dangling
    # task_brief_file reference.
    recompiled, second_meta = plan_compiler.compile_master_plan(
        compiled,
        next_v=300,
        target_dir=target,
        project_root=tmp_path,
    )
    assert second_meta["preserved_compiled_context"] is True
    assert recompiled["tasks"][0]["task_brief_file"] == task["task_brief_file"]
    assert (tmp_path / task["task_brief_file"]).exists()


def test_plan_compiler_clears_stale_task_context_for_short_plan(tmp_path):
    import plan_compiler

    target = _strict_artifact(tmp_path / "national_v301", 301)
    stale_dir = target / ".task_context"
    stale_dir.mkdir()
    (stale_dir / "w1.md").write_text("stale next_v: 290", encoding="utf-8")

    plan = {
        "tasks": [
            {
                "worker_id": 1,
                "role": "Hyperparameter Tuner",
                "target_files": ["policy.py"],
                "worker_prompt": "Tune one typed policy decision.",
            }
        ]
    }

    compiled, meta = plan_compiler.compile_master_plan(
        plan,
        next_v=301,
        target_dir=target,
        project_root=tmp_path,
    )

    assert meta["compiled"] is False
    assert not stale_dir.exists()
    assert "task_brief_file" not in compiled["tasks"][0]


def test_plan_compiler_clears_stale_task_context_for_invalid_task_shape(tmp_path):
    import plan_compiler

    target = _strict_artifact(tmp_path / "national_v302", 302)
    stale_dir = target / ".task_context"
    stale_dir.mkdir()
    (stale_dir / "w1.md").write_text("stale next_v: 290", encoding="utf-8")

    compiled, meta = plan_compiler.compile_master_plan(
        {"tasks": {"not": "a list"}},
        next_v=302,
        target_dir=target,
        project_root=tmp_path,
    )

    assert meta["compiled"] is False
    assert compiled["tasks"] == {"not": "a list"}
    assert not stale_dir.exists()


def test_candidate_copy_excludes_task_context_and_parent_artifacts(tmp_path):
    import evolution_infra

    source = _strict_artifact(tmp_path / "source", 299)
    (source / ".completed").write_text("", encoding="utf-8")
    (source / "old.pyc").write_bytes(b"pyc")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "policy.cpython.pyc").write_bytes(b"pyc")
    (source / ".task_context").mkdir()
    (source / ".task_context" / "w1.md").write_text("stale next_v: 290", encoding="utf-8")

    target = tmp_path / "target"
    evolution_infra.copy_bot_tree_for_candidate(source, target)

    assert (target / "policy.py").exists()
    assert not (target / ".completed").exists()
    assert not (target / "old.pyc").exists()
    assert not (target / "__pycache__").exists()
    assert not (target / ".task_context").exists()


def test_successful_worker_cleanup_removes_compiler_context_before_quality(tmp_path):
    import tool_gates
    import tool_planning

    target = _strict_artifact(tmp_path / "national_v303", 303)
    context = target / ".task_context"
    context.mkdir()
    (context / "w1.md").write_text("system-owned brief", encoding="utf-8")
    nested = target / "tables" / ".task_context"
    nested.mkdir(parents=True)
    (nested / "w2.md").write_text("nested brief", encoding="utf-8")
    pytest_cache = target / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "action").write_bytes(b"fold")
    bytecode = target / "__pycache__"
    bytecode.mkdir()
    (bytecode / "policy.pyc").write_bytes(b"bytecode")

    assert tool_gates._transient_task_context_errors(target)
    tool_planning._clear_compiled_task_context(target)

    assert not context.exists()
    assert not nested.exists()
    assert not pytest_cache.exists()
    assert not bytecode.exists()
    assert tool_gates._transient_task_context_errors(target) == []


def test_quality_hygiene_finds_nested_task_context(tmp_path):
    import tool_gates

    target = tmp_path / "national_v304"
    nested = target / "tables" / ".task_context"
    nested.mkdir(parents=True)
    (nested / "hidden.md").write_text("not publishable", encoding="utf-8")

    assert tool_gates._transient_task_context_errors(target) == [
        "transient_control_artifact_present:tables/.task_context"
    ]


def test_excluded_cache_cannot_be_hidden_policy_dependency(tmp_path):
    from bot_artifact import hash_path
    from candidate_hygiene import (
        cleanup_transient_candidate_artifacts,
        forbidden_runtime_dependency_errors,
    )

    target = tmp_path / "national_v305"
    target.mkdir()
    (target / "policy.py").write_text(
        "from pathlib import Path\nACTION = Path('.pytest_cache/action').read_text()\n",
        encoding="utf-8",
    )
    cache = target / ".pytest_cache"
    cache.mkdir()
    action = cache / "action"
    action.write_text("0", encoding="utf-8")
    before = hash_path(target)
    action.write_text("-1", encoding="utf-8")

    assert hash_path(target) == before
    assert forbidden_runtime_dependency_errors(target) == [
        "forbidden_transient_runtime_dependency:policy.py:2:.pytest_cache"
    ]
    cleanup_transient_candidate_artifacts(target, include_task_context=False)
    assert not cache.exists()


def test_transient_cleanup_rejects_symlink_in_cache(tmp_path):
    from candidate_hygiene import cleanup_transient_candidate_artifacts

    target = tmp_path / "national_v306"
    cache = target / ".pytest_cache"
    cache.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    (cache / "action").symlink_to(outside)

    with pytest.raises(RuntimeError, match="symlink"):
        cleanup_transient_candidate_artifacts(target)
    assert outside.read_text(encoding="utf-8") == "keep"


def test_master_prompt_disallows_manual_task_context_files():
    text = (CORE / "prompts" / "master_prompt.md").read_text(encoding="utf-8")

    assert "Do not manually create, copy, or reference `.task_context`" in text
    assert "write it to `.task_context" not in text


def test_near_cap_core_file_cannot_grow(tmp_path):
    import code_verification

    source = tmp_path / "source"
    child = tmp_path / "child"
    source.mkdir()
    child.mkdir()
    (source / "policy.py").write_text("x = 1\n" * 2486, encoding="utf-8")
    (child / "policy.py").write_text("x = 1\n" * 2493, encoding="utf-8")

    _total, oversized = code_verification.check_code_size(child, source_dir=source)

    assert oversized == [("policy.py", 2493, 2486)]


def test_oversized_source_does_not_inflate_child_limit(tmp_path):
    """Regression: source > base_limit must NOT relax the child limit beyond source_lines.

    Previously _get_adaptive_limit computed max(2000, source*1.15), so a 2147-line
    source gave the child a 2469-line limit, letting the snowball grow every
    generation. Now the limit for an oversized source is exactly source_lines.
    """
    import code_verification

    source = tmp_path / "source"
    child = tmp_path / "child"
    source.mkdir()
    child.mkdir()
    (source / "policy.py").write_text("x = 1\n" * 2147, encoding="utf-8")
    (child / "policy.py").write_text("x = 1\n" * 2178, encoding="utf-8")

    _total, oversized = code_verification.check_code_size(child, source_dir=source)

    # limit must be 2147 (source_lines), NOT 2469 (source*1.15)
    assert oversized == [("policy.py", 2178, 2147)]


def test_system_generated_national_entry_uses_repository_hard_cap(tmp_path):
    import code_verification

    source = tmp_path / "source"
    child = tmp_path / "child"
    source.mkdir()
    child.mkdir()
    (source / "national_bot.py").write_text("x = 1\n" * 1400, encoding="utf-8")
    (child / "national_bot.py").write_text("x = 1\n" * 2326, encoding="utf-8")

    _total, oversized = code_verification.check_code_size(child, source_dir=source)

    assert oversized == []


def test_oversized_source_allows_child_to_match_or_shrink(tmp_path):
    """A child matching or shrinking an oversized source must pass the size gate.

    This lets a strict-policy descendant match or shrink an inherited oversized
    ``policy.py`` while still preventing further growth.
    """
    import code_verification

    source = tmp_path / "source"
    child = tmp_path / "child"
    source.mkdir()
    child.mkdir()
    (source / "policy.py").write_text("x = 1\n" * 2147, encoding="utf-8")

    # Exactly match source
    (child / "policy.py").write_text("x = 1\n" * 2147, encoding="utf-8")
    _total, oversized = code_verification.check_code_size(child, source_dir=source)
    assert oversized == []

    # Shrink below source
    (child / "policy.py").write_text("x = 1\n" * 2100, encoding="utf-8")
    _total, oversized = code_verification.check_code_size(child, source_dir=source)
    assert oversized == []


def test_compliant_source_keeps_growth_budget(tmp_path):
    """source <= base_limit still gets the 15% LINE_GROWTH_BUDGET."""
    import code_verification

    source = tmp_path / "source"
    child = tmp_path / "child"
    source.mkdir()
    child.mkdir()
    (source / "policy.py").write_text("x = 1\n" * 1900, encoding="utf-8")
    # 2050 < max(2000, 1900*1.15=2185) -> within budget
    (child / "policy.py").write_text("x = 1\n" * 2050, encoding="utf-8")

    _total, oversized = code_verification.check_code_size(child, source_dir=source)
    assert oversized == []

    # 2200 > 2185 -> over budget
    (child / "policy.py").write_text("x = 1\n" * 2200, encoding="utf-8")
    _total, oversized = code_verification.check_code_size(child, source_dir=source)
    assert oversized == [("policy.py", 2200, 2185)]




def test_repo_state_snapshot_classifies_dirty_untracked_and_protected(monkeypatch, tmp_path):
    import subprocess
    import repo_state

    status = "\n".join([
        "## codex/test...origin/main",
        " M web/core/tool_gates.py",
        " M sever/server/tcp_server.py",
        " M sever/国赛平台/通信协议.docx",
        "?? bots/national_v245/",
        "?? web/logs/restart.log",
        "?? bots/neural_national_lab/data/run.jsonl",
    ])

    def _fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(_args[0], 0, stdout=status, stderr="")

    monkeypatch.setattr(repo_state.subprocess, "run", _fake_run)

    snapshot = repo_state.git_worktree_snapshot(tmp_path)

    assert snapshot["ok"] is True
    assert snapshot["branch"] == "codex/test...origin/main"
    assert snapshot["dirty_count"] == 3
    assert snapshot["untracked_count"] == 3
    assert snapshot["generated_bot_dirs"] == ["?? bots/national_v245/"]
    assert snapshot["protected_entries"] == [
        " M web/core/tool_gates.py",
        " M sever/server/tcp_server.py",
    ]
    assert " M sever/国赛平台/通信协议.docx" in snapshot["ignored_entries"]
    assert "?? web/logs/restart.log" in snapshot["ignored_entries"]
    assert "?? bots/neural_national_lab/data/run.jsonl" in snapshot["ignored_entries"]


def test_repo_state_log_emits_structured_event(monkeypatch):
    import repo_state

    events = []
    monkeypatch.setattr(repo_state, "_LAST_SNAPSHOT", None)
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: {"ok": True, "entries": []})

    import system_log

    def _fake_log(event_type, severity, message, data):
        events.append((event_type, severity, message, data))

    monkeypatch.setattr(system_log, "log_system_event", _fake_log)

    payload = repo_state.log_git_worktree_snapshot(
        "repo.worktree_snapshot",
        "snapshot",
        next_v=300,
    )

    assert payload["next_v"] == 300
    assert events == [("repo.worktree_snapshot", "info", "snapshot", payload)]


def test_repo_state_delta_emits_branch_and_worktree_events(monkeypatch):
    import repo_state

    events = []
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "aaa111",
            "dirty_count": 0,
            "untracked_count": 0,
            "entry_count": 0,
            "entries": [],
        },
        {
            "ok": True,
            "branch": "codex/refactor",
            "head": "bbb222",
            "dirty_count": 4,
            "untracked_count": 1,
            "entry_count": 5,
            "entries": [
                " M web/core/tool_gates.py",
                " M docs/notes.md",
                " M sever/国赛平台/通信协议.docx",
                " M sever/server/tcp_server.py",
                "?? bots/national_v251/",
            ],
        },
    ])
    monkeypatch.setattr(repo_state, "_LAST_SNAPSHOT", None)
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    import system_log

    def _fake_log(event_type, severity, message, data):
        events.append((event_type, severity, message, data))

    monkeypatch.setattr(system_log, "log_system_event", _fake_log)

    repo_state.log_git_worktree_snapshot("repo.worktree_snapshot", "before", emit_delta=True)
    repo_state.log_git_worktree_snapshot("repo.worktree_snapshot", "after", emit_delta=True)

    event_types = [event[0] for event in events]
    assert "repo.worktree_baseline" in event_types
    assert "repo.branch_changed" in event_types
    assert "repo.head_changed" in event_types
    assert "repo.worktree_changed" in event_types
    branch_event = next(event for event in events if event[0] == "repo.branch_changed")
    assert branch_event[3]["previous_branch"] == "main...origin/main"
    assert branch_event[3]["current_branch"] == "codex/refactor"
    worktree_event = next(event for event in events if event[0] == "repo.worktree_changed")
    assert worktree_event[1] == "warn"
    assert " M web/core/tool_gates.py" in worktree_event[3]["new_dirty_entries"]
    assert " M sever/server/tcp_server.py" in worktree_event[3]["new_protected_entries"]
    assert " M docs/notes.md" in worktree_event[3]["new_ignored_entries"]
    assert " M sever/国赛平台/通信协议.docx" in worktree_event[3]["new_ignored_entries"]
    assert "?? bots/national_v251/" in worktree_event[3]["new_generated_bot_dirs"]
    assert "?? bots/national_v251/" not in worktree_event[3]["new_protected_entries"]


def test_repo_state_external_worktree_delta_is_info(monkeypatch):
    import repo_state

    events = []
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "aaa111",
            "dirty_count": 0,
            "untracked_count": 0,
            "entry_count": 0,
            "entries": [],
        },
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "aaa111",
            "dirty_count": 2,
            "untracked_count": 2,
            "entry_count": 4,
            "entries": [
                " M docs/notes.md",
                " M sever/国赛平台/通信协议.docx",
                "?? bots/neural_national_lab/data/run.jsonl",
                "?? bots/national_v251/",
            ],
        },
    ])
    monkeypatch.setattr(repo_state, "_LAST_SNAPSHOT", None)
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    import system_log

    def _fake_log(event_type, severity, message, data):
        events.append((event_type, severity, message, data))

    monkeypatch.setattr(system_log, "log_system_event", _fake_log)

    repo_state.log_git_worktree_snapshot("repo.worktree_snapshot", "before", emit_delta=True)
    repo_state.log_git_worktree_snapshot("repo.worktree_snapshot", "after", emit_delta=True)

    worktree_event = next(event for event in events if event[0] == "repo.worktree_changed")
    assert worktree_event[1] == "info"
    assert worktree_event[3]["new_protected_entries"] == []
    assert " M docs/notes.md" in worktree_event[3]["new_ignored_entries"]
    assert " M sever/国赛平台/通信协议.docx" in worktree_event[3]["new_ignored_entries"]
    assert "?? bots/neural_national_lab/data/run.jsonl" in worktree_event[3]["new_ignored_entries"]


def test_runtime_code_has_no_local_absolute_project_paths():
    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        text=False,
        timeout=30,
    )
    assert proc.returncode == 0

    code_suffixes = (".py", ".sh", ".js", ".ts", ".tsx", ".json")
    excluded_prefixes = (
        "archive/",
        "docs/",
        "web/tests/",
        "bots/neural_national_lab/data/",
        "ladder_results/",
        "results/",
        "scripts/research_eval/",
    )
    forbidden_tokens = (
        "/home/zzx/project/pok",
        "pok_evolution_run",
        "pok_national_native_run_clone",
    )

    hits = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="replace")
        if not rel.endswith(code_suffixes):
            continue
        if rel.startswith(excluded_prefixes):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for token in forbidden_tokens:
            if token in text:
                hits.append(f"{rel}: {token}")
                break

    assert hits == []


def test_runtime_guard_allows_current_candidate_dir(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
        },
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
        },
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "direction_audited",
        repo_baseline={
            "head": "abc123",
            "branch": "main...origin/main",
            "captured_stage": "direction_audited",
        },
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_master",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["guard"] == "ok"


def test_runtime_guard_blocks_master_before_direction_audit(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
        },
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
        },
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "prepared",
        repo_baseline={
            "head": "abc123",
            "branch": "main...origin/main",
            "captured_stage": "prepared",
        },
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_master",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["error"] == "pipeline_route_guard_blocked"
    assert payload["reason"] == "wrong_pipeline_stage"
    assert payload["checkpoint_stage"] == "prepared"
    assert payload["next_tool"] == "run_direction_audit"
    assert payload["allowed_tools"] == ["run_direction_audit"]


def test_pipeline_route_guard_blocks_mutating_tools_without_checkpoint(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "_log_guard_event", lambda *_args: None)

    for tool_name in sorted(tool_runtime_guard._PIPELINE_ROUTE_TOOLS):
        ok, payload = tool_runtime_guard._pipeline_route_guard(
            tool_name=tool_name,
            args={},
            candidate_v=300,
            source_v=299,
        )

        assert ok is False
        assert payload["error"] == "pipeline_route_guard_blocked"
        assert payload["reason"] == "no_active_checkpoint"
        assert payload["next_tool"] == "prepare_generation"
        assert payload["allowed_tools"] == ["prepare_generation"]


def test_pipeline_route_guard_no_checkpoint_keeps_scheduler_and_read_only_exceptions(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)

    for tool_name in ("prepare_generation", "get_status"):
        ok, payload = tool_runtime_guard._pipeline_route_guard(
            tool_name=tool_name,
            args={},
            candidate_v=None,
            source_v=None,
        )

        assert ok is True
        assert payload == {}


def test_pipeline_route_guard_allows_only_tagged_post_commit_archivist(
    tmp_path,
    monkeypatch,
):
    import evolution_infra
    import tool_runtime_guard

    monkeypatch.setattr(tool_runtime_guard, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)
    pending = {"value": True}
    monkeypatch.setattr(
        evolution_infra,
        "validate_post_commit_archivist_receipt",
        lambda version, source_v: (
            pending["value"] and int(version) == 300 and int(source_v) == 299,
            "",
            {"receipt_digest": "r" * 64},
        ),
    )

    ok, payload = tool_runtime_guard._pipeline_route_guard(
        tool_name="run_archivist",
        args={"version": 300, "source_v": 299},
        candidate_v=300,
        source_v=299,
    )

    assert ok is True
    assert payload == {
        "post_commit_archivist": True,
        "candidate_v": 300,
        "source_v": 299,
        "receipt_digest": "r" * 64,
    }

    pending["value"] = False
    ok, payload = tool_runtime_guard._pipeline_route_guard(
        tool_name="run_archivist",
        args={"version": 300, "source_v": 299},
        candidate_v=300,
        source_v=299,
    )
    assert ok is False
    assert payload["reason"] == "no_active_checkpoint"


def test_post_commit_archivist_receipt_is_content_bound_and_single_use(
    tmp_path,
    monkeypatch,
):
    import evolution_infra

    bot_dir = _strict_artifact(tmp_path / "bots" / "national_v300", 300)
    archive_dir = tmp_path / "archive"
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _v: bot_dir)
    monkeypatch.setattr(evolution_infra, "get_active_bots", lambda: ["national_v300"])
    monkeypatch.setattr(evolution_infra, "load_ratings", lambda: {})
    monkeypatch.setattr(evolution_infra, "git_has_tag", lambda _v: True)
    monkeypatch.setattr(
        evolution_infra,
        "_git",
        lambda *args, **_kwargs: "commit-300" if args[0] == "rev-parse" else "",
    )

    snapshot = evolution_infra.archive_generation(300, 299, {})
    receipt = snapshot["post_commit_archivist_receipt"]
    assert receipt["status"] == "pending"

    ok, reason, _ = evolution_infra.validate_post_commit_archivist_receipt(300, 299)
    assert ok is True
    assert reason == ""
    wrong_source_ok, wrong_source_reason, _ = (
        evolution_infra.validate_post_commit_archivist_receipt(300, 298)
    )
    assert wrong_source_ok is False
    assert wrong_source_reason == "post_commit_archivist_source_mismatch"

    consumed, consume_reason, _ = evolution_infra.consume_post_commit_archivist_receipt(
        300,
        299,
    )
    assert consumed is True
    assert consume_reason == ""
    replay_ok, replay_reason, _ = evolution_infra.validate_post_commit_archivist_receipt(
        300,
        299,
    )
    assert replay_ok is False
    assert replay_reason == "post_commit_archivist_receipt_consumed"

    (bot_dir / "policy.py").write_text("DRIFTED = True\n", encoding="utf-8")
    second, second_reason, _ = evolution_infra.consume_post_commit_archivist_receipt(
        300,
        299,
    )
    assert second is False
    assert second_reason == "post_commit_archivist_receipt_consumed"


def test_runtime_guard_blocks_random_pipeline_tool_without_checkpoint(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": ["?? bots/national_v300/"],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_review",
        {"version": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "no_active_checkpoint"
    assert payload["next_tool"] == "prepare_generation"
    assert payload["allowed_tools"] == ["prepare_generation"]


def test_operator_bootstrap_stage_blocks_commit_without_valid_certificate(monkeypatch):
    import tool_runtime_guard

    checkpoint = _strict_checkpoint(
        143,
        142,
        "official_bootstrap_required",
        gate_results={"precommit_eval": {"passed": True}},
    )
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        tool_runtime_guard,
        "_operator_bootstrap_certificate_valid",
        lambda _candidate_v: False,
    )

    ok, payload = tool_runtime_guard._pipeline_route_guard(
        tool_name="commit_bot",
        args={"version": 143, "source_v": 142},
        candidate_v=143,
        source_v=142,
    )

    assert ok is False
    assert payload["reason"] == "official_bootstrap_certificate_required"
    assert payload["checkpoint_stage"] == "official_bootstrap_required"


def test_operator_bootstrap_stage_allows_commit_only_after_full_validation(monkeypatch):
    import tool_runtime_guard

    checkpoint = _strict_checkpoint(
        143,
        142,
        "official_bootstrap_required",
        gate_results={"precommit_eval": {"passed": True}},
    )
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: checkpoint)
    calls = []
    monkeypatch.setattr(
        tool_runtime_guard,
        "_operator_bootstrap_certificate_valid",
        lambda candidate_v: calls.append(candidate_v) or True,
    )

    ok, payload = tool_runtime_guard._pipeline_route_guard(
        tool_name="commit_bot",
        args={"version": 143, "source_v": 142},
        candidate_v=143,
        source_v=142,
    )

    assert ok is True
    assert payload == {}
    assert calls == [143]


def test_operator_bootstrap_guard_uses_complete_official_validator(tmp_path, monkeypatch):
    import official_certification
    import tool_runtime_guard

    candidate = _strict_artifact(tmp_path / "bots" / "national_v143", 143)
    status = {"status": "official-certified", "certificate_digest": "signed"}
    calls = []
    monkeypatch.setattr(tool_runtime_guard, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(official_certification, "read_status", lambda path: status)

    def validate(requested_status, path):
        calls.append((requested_status, path))
        return requested_status is status and path == candidate

    monkeypatch.setattr(official_certification, "official_full_certified", validate)

    assert tool_runtime_guard._operator_bootstrap_certificate_valid(143) is True
    assert calls == [(status, candidate)]


def test_runtime_guard_allows_pre_master_literature_probe(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
        },
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v300/"],
        },
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "direction_audited",
        repo_baseline={
            "head": "abc123",
            "branch": "main...origin/main",
            "captured_stage": "direction_audited",
        },
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_literature_probe",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["guard"] == "ok"


def test_runtime_guard_cleanup_tools_infer_authoritative_next_v(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v301/"],
        },
        {
            "ok": True,
            "branch": "main...origin/main",
            "head": "abc123",
            "entries": ["?? bots/national_v301/"],
        },
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)

    import evolution_infra
    monkeypatch.setattr(evolution_infra, "compute_next_generation_v", lambda: 301)

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard("cleanup_incomplete", {})

    assert ok is True
    assert payload["candidate_v"] == 301


def test_runtime_guard_blocks_unexpected_system_dirty(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": [" M web/core/tool_gates.py", "?? bots/national_v300/"],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "unexpected_worktree_entries"
    assert " M web/core/tool_gates.py" in payload["unexpected_entries"]


def test_runtime_guard_allows_unrelated_inplace_dirty_entries(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": [
            " M docs/notes.md",
            " M sever/国赛平台/通信协议.docx",
            "?? bots/neural_national_lab/data/run.jsonl",
            "?? bots/national_v300/",
        ],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "master_planned",
        repo_baseline={
            "head": "abc123",
            "branch": "main...origin/main",
            "captured_stage": "master_planned",
        },
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["ignored_count"] == 3
    assert " M docs/notes.md" in payload["ignored_entries"]
    assert " M sever/国赛平台/通信协议.docx" in payload["ignored_entries"]


def test_runtime_guard_allows_noncritical_web_core_dirty_entries(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": [
            " M web/core/replay_spotlight.py",
            "?? bots/national_v300/",
        ],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "master_planned",
        repo_baseline={
            "head": "abc123",
            "branch": "main...origin/main",
            "captured_stage": "master_planned",
        },
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert " M web/core/replay_spotlight.py" in payload["ignored_entries"]


def test_runtime_guard_blocks_foreign_national_bot_dir(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": ["?? bots/national_v299/", "?? bots/national_v300/"],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "unexpected_worktree_entries"
    assert "?? bots/national_v299/" in payload["unexpected_entries"]


def test_runtime_guard_blocks_truncated_snapshot(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "abc123",
        "entries": ["?? bots/national_v300/"] * 40,
        "entry_count": 41,
        "truncated": True,
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "worktree_snapshot_truncated"
    assert payload["entry_count"] == 41


def test_runtime_guard_blocks_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "new456",
        "entries": ["?? bots/national_v300/"],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "old123"})

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_quality_gates",
        {"version": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "head_changed_during_generation"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_unrelated_head_drift(monkeypatch):
    import evaluation_contract
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "old123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "workers_done",
        repo_baseline={
            "head": "old123",
            "branch": "main...origin/main",
            "captured_stage": "workers_done",
        },
    ))
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: ["docs/experiment-notes.md", "bots/neural_national_lab/data/run.jsonl"],
    )

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_quality_gates",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_unrelated_allowed"] is True
    assert "docs/experiment-notes.md" in "\n".join(payload["head_changed_paths"])


def test_unrelated_head_drift_cannot_bypass_mandatory_literature_route(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "new456",
        "entries": ["?? bots/national_v300/"],
    }
    checkpoint = _strict_checkpoint(
        300,
        299,
        "direction_audited",
        audit_context={
            "master_context": {
                "stagnation_info": "STAGNATION_DETECTED (is_stagnant=true)",
            },
        },
        repo_baseline={
            "head": "old123",
            "branch": "main...origin/main",
            "captured_stage": "direction_audited",
        },
    )
    events = []
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        tool_runtime_guard,
        "_unrelated_head_drift_allowed",
        lambda **_kwargs: (True, {"head_changed_paths": ["docs/notes.md"]}),
    )
    monkeypatch.setattr(tool_runtime_guard, "_log_guard_event", lambda *args: events.append(args))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_master",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["error"] == "pipeline_route_guard_blocked"
    assert payload["reason"] == "wrong_pipeline_stage"
    assert payload["next_tool"] == "run_literature_probe"
    assert payload["allowed_tools"] == ["run_literature_probe"]
    assert any(event[0] == "repo.runtime_guard_head_drift_unrelated_allowed" for event in events)
    assert any(event[0] == "pipeline.route_guard_blocked" for event in events)


def test_checkpoint_head_resume_cannot_bypass_mandatory_literature_route(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "new456",
        "entries": ["?? bots/national_v300/"],
    }
    checkpoint = _strict_checkpoint(
        300,
        299,
        "direction_audited",
        audit_context={
            "master_context": {
                "stagnation_info": "STAGNATION_DETECTED (is_stagnant=true)",
            },
        },
        repo_baseline={
            "head": "old123",
            "branch": "main...origin/main",
            "captured_stage": "direction_audited",
        },
    )
    events = []
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: checkpoint)
    monkeypatch.setattr(
        tool_runtime_guard,
        "_unrelated_head_drift_allowed",
        lambda **_kwargs: (False, {}),
    )
    monkeypatch.setattr(tool_runtime_guard, "_log_guard_event", lambda *args: events.append(args))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_master",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["error"] == "pipeline_route_guard_blocked"
    assert payload["reason"] == "wrong_pipeline_stage"
    assert payload["next_tool"] == "run_literature_probe"
    assert payload["allowed_tools"] == ["run_literature_probe"]
    assert any(event[0] == "repo.runtime_guard_head_drift_repair_allowed" for event in events)
    assert any(event[0] == "pipeline.route_guard_blocked" for event in events)


def test_runtime_guard_blocks_source_bot_contract_head_drift(monkeypatch):
    import evaluation_contract
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "old123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: ["bots/national_v299/policy.py"],
    )

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_quality_gates",
        {"version": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "head_changed_during_generation"
    assert payload["evaluation_contract_unchanged"] is False
    assert "bots/national_v299/policy.py" in payload["head_contract_paths"]


def test_evaluation_contract_classifies_dynamic_bot_versions(monkeypatch):
    import evaluation_contract

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "default")
    contract = evaluation_contract.build_evaluation_contract(
        Path.cwd(),
        candidate_v=300,
        source_v=299,
        checkpoint={
            "gate_results": {
                "precommit_eval": {
                    "opponents": [
                        {"name": "national_v297"},
                        {"name": "bots/neural_national_lab/versions/v058"},
                    ]
                }
            },
            "official_job": {"opponent": "national_v298"},
        },
    )
    scope = evaluation_contract.classify_contract_paths(
        [
            "engine/battle.py",
            "web/core/replay_spotlight.py",
            "bots/national_v300/policy.py",
            "bots/national_v299/policy.py",
            "bots/national_v297/policy.py",
            "official_certificates/national_v297.json",
            "official_certificates/national_v298.json",
            "bots/neural_national_lab/data/run.json",
            "sever/server/tcp_server.py",
            "sever/国赛平台/通信协议.docx",
            "docs/official-raise-boundary-oracle-2026-07-11.md",
            "docs/official-terminal-settlement-oracle-2026-07-11.md",
            "docs/notes.md",
        ],
        contract,
    )

    assert "engine/battle.py" in scope["external_paths"]
    assert "bots/national_v300/policy.py" in scope["contract_paths"]
    assert "bots/national_v299/policy.py" in scope["contract_paths"]
    assert "bots/national_v297/policy.py" in scope["contract_paths"]
    assert "official_certificates/national_v297.json" in scope["contract_paths"]
    assert "official_certificates/national_v298.json" in scope["contract_paths"]
    assert "sever/server/tcp_server.py" in scope["contract_paths"]
    assert "docs/official-raise-boundary-oracle-2026-07-11.md" in scope[
        "contract_paths"
    ]
    assert "docs/official-terminal-settlement-oracle-2026-07-11.md" in scope[
        "contract_paths"
    ]
    assert "web/core/replay_spotlight.py" in scope["external_paths"]
    assert "sever/国赛平台/通信协议.docx" in scope["external_paths"]
    assert "bots/neural_national_lab/data/run.json" in scope["external_paths"]
    assert "docs/notes.md" in scope["external_paths"]


def test_official_oracle_docs_override_docs_non_contract_prefix(monkeypatch):
    import evaluation_contract

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    contract = evaluation_contract.build_evaluation_contract(
        Path.cwd(),
        candidate_v=300,
        source_v=299,
        checkpoint={
            "stage": "workers_done",
            "next_v": 300,
            "source_v": 299,
        },
    )
    scope = evaluation_contract.classify_contract_paths(
        [
            "docs/official-raise-boundary-oracle-2026-07-11.md",
            "docs/official-terminal-settlement-oracle-2026-07-11.md",
            "docs/ordinary-note.md",
        ],
        contract,
    )

    assert scope["contract_paths"] == [
        "docs/official-raise-boundary-oracle-2026-07-11.md",
        "docs/official-terminal-settlement-oracle-2026-07-11.md",
    ]
    assert scope["external_paths"] == ["docs/ordinary-note.md"]


def test_evaluation_contract_excludes_local_engine_under_native_profile(monkeypatch):
    import evaluation_contract

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    contract = evaluation_contract.build_evaluation_contract(
        Path.cwd(),
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "workers_done", "next_v": 300, "source_v": 299},
    )
    scope = evaluation_contract.classify_contract_paths(
        [
            "engine/battle.py",
            "web/core/engine/battle.py",
            "web/core/smoke_tester.py",
            "sever/bot_adapter.py",
            "sever/tests/test_national_alignment.py",
            "scripts/national_acceptance_matrix.py",
            "sever/server/tcp_server.py",
            "sever/tests/test_national_platform_alignment.py",
            "web/core/national_acceptance.py",
            "web/core/national_eval.py",
            "web/core/national_native.py",
            "bots/national_v300/policy.py",
            "bots/national_v299/policy.py",
        ],
        contract,
    )

    assert contract["national_execution_mode"] == "native_tcp"
    assert "engine/battle.py" in scope["external_paths"]
    assert "web/core/engine/battle.py" in scope["external_paths"]
    assert "web/core/smoke_tester.py" in scope["external_paths"]
    assert "sever/bot_adapter.py" in scope["external_paths"]
    assert "sever/tests/test_national_alignment.py" in scope["external_paths"]
    assert "scripts/national_acceptance_matrix.py" in scope["external_paths"]
    assert "sever/server/tcp_server.py" in scope["contract_paths"]
    assert "sever/tests/test_national_platform_alignment.py" in scope["contract_paths"]
    assert "web/core/national_acceptance.py" in scope["external_paths"]
    assert "web/core/national_eval.py" in scope["external_paths"]
    assert "web/core/national_native.py" in scope["contract_paths"]
    assert "bots/national_v300/policy.py" in scope["contract_paths"]
    assert "bots/national_v299/policy.py" in scope["contract_paths"]


def test_evaluation_contract_rejects_retired_adapter_profile(monkeypatch):
    import evaluation_contract

    with pytest.raises(ValueError, match="only native_tcp is active"):
        evaluation_contract.build_evaluation_contract(
            Path.cwd(),
            candidate_v=300,
            source_v=299,
            checkpoint={"stage": "workers_done", "next_v": 300, "source_v": 299},
            national_execution_mode="adapter",
        )


def test_worktree_scope_uses_native_evaluation_contract_for_dirty_paths(monkeypatch):
    import evaluation_contract
    import evolution_scope

    monkeypatch.setenv("POK_WORKFLOW_PROFILE", "national_native")
    checkpoint = {"stage": "workers_done", "next_v": 300, "source_v": 299}
    contract = evaluation_contract.build_evaluation_contract(
        Path.cwd(),
        candidate_v=300,
        source_v=299,
        checkpoint=checkpoint,
    )

    scope = evolution_scope.classify_status_entries(
        [
            " M engine/battle.py",
            " M web/core/smoke_tester.py",
            " M sever/bot_adapter.py",
            " M sever/server/tcp_server.py",
            " M web/core/national_acceptance.py",
            " M bots/national_v299/policy.py",
            "?? bots/national_v300/",
        ],
        300,
        contract_bot_versions=evaluation_contract.contract_bot_versions(
            candidate_v=300,
            checkpoint=checkpoint,
        ),
        evaluation_contract=contract,
    )

    assert " M engine/battle.py" in scope["external_entries"]
    assert " M web/core/smoke_tester.py" in scope["external_entries"]
    assert " M sever/bot_adapter.py" in scope["external_entries"]
    assert " M sever/server/tcp_server.py" in scope["critical_entries"]
    assert " M web/core/national_acceptance.py" in scope["external_entries"]
    assert " M bots/national_v299/policy.py" in scope["foreign_bot_entries"]
    assert "?? bots/national_v300/" in scope["candidate_entries"]
    assert scope["blocking_entries"] == [
        " M sever/server/tcp_server.py",
        " M bots/national_v299/policy.py",
    ]


def test_evaluation_contract_allows_noncritical_web_core_head_drift(monkeypatch):
    import evaluation_contract

    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: ["web/core/replay_spotlight.py"],
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
    )

    assert allowed is True
    assert payload["evaluation_contract_unchanged"] is True
    assert payload["head_contract_paths"] == []
    assert payload["head_external_paths"] == ["web/core/replay_spotlight.py"]


def test_evaluation_contract_allows_observability_and_launcher_head_drift(monkeypatch):
    import evaluation_contract

    non_contract_paths = [
        "sever/main.py",
        "web/main.py",
        "web/core/api_concurrency.py",
        "web/core/event_bus.py",
        "web/core/observe_policy.py",
        "web/core/rate_limiter.py",
        "web/core/system_log.py",
        "web/core/web_ui.py",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(non_contract_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
    )

    assert allowed is True
    assert payload["evaluation_contract_unchanged"] is True
    assert payload["head_contract_paths"] == []
    assert payload["head_external_paths"] == sorted(non_contract_paths)


def test_evaluation_contract_blocks_gate_and_prompt_head_drift(monkeypatch):
    import evaluation_contract

    contract_paths = [
        "web/core/eval_stats.py",
        "web/core/national_native.py",
        "web/core/prompts/worker_prompt.md",
        "web/core/tool_eval.py",
        "web/core/worker_boundary.py",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(contract_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
    )

    assert allowed is False
    assert payload["evaluation_contract_unchanged"] is False
    assert payload["head_contract_paths"] == sorted(contract_paths)
    assert payload["head_external_paths"] == []


def test_evaluation_contract_uses_stage_scoped_post_worker_contract(monkeypatch):
    import evaluation_contract

    changed_paths = [
        "web/core/agent_master.py",
        "web/core/eval_stats.py",
        "web/core/prompts/master_prompt.md",
        "web/core/prompts/worker_prompt.md",
        "sever/server/tcp_server.py",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(changed_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "workers_done", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["evaluation_contract"]["stage"] == "workers_done"
    assert payload["head_contract_paths"] == [
        "sever/server/tcp_server.py",
        "web/core/eval_stats.py",
    ]
    assert payload["head_external_paths"] == [
        "web/core/agent_master.py",
        "web/core/prompts/master_prompt.md",
        "web/core/prompts/worker_prompt.md",
    ]


def test_evaluation_contract_defers_gate_files_until_gate_stage(monkeypatch):
    import evaluation_contract

    changed_paths = [
        "sever/server/tcp_server.py",
        "web/core/prompts/master_prompt.md",
        "web/core/tool_gates.py",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(changed_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "direction_audited", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["evaluation_contract"]["stage"] == "direction_audited"
    assert payload["head_contract_paths"] == ["web/core/prompts/master_prompt.md"]
    assert payload["head_external_paths"] == [
        "sever/server/tcp_server.py",
        "web/core/tool_gates.py",
    ]

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "workers_done", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["evaluation_contract"]["stage"] == "workers_done"
    assert payload["head_contract_paths"] == [
        "sever/server/tcp_server.py",
        "web/core/tool_gates.py",
    ]
    assert payload["head_external_paths"] == ["web/core/prompts/master_prompt.md"]


def test_evaluation_contract_tracks_worker_files_for_repair_stage(monkeypatch):
    import evaluation_contract

    changed_paths = [
        "web/core/agent_master.py",
        "web/core/prompts/worker_prompt.md",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(changed_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "precommit_failed", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["evaluation_contract"]["stage"] == "precommit_failed"
    assert payload["head_contract_paths"] == ["web/core/prompts/worker_prompt.md"]
    assert payload["head_external_paths"] == ["web/core/agent_master.py"]


def test_evaluation_contract_uses_stage_specific_worker_contract(monkeypatch):
    import evaluation_contract

    changed_paths = [
        "web/core/agent_master.py",
        "web/core/agent_workers.py",
        "web/core/eval_stats.py",
        "web/core/prompts/master_prompt.md",
        "web/core/prompts/worker_prompt.md",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(changed_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "master_planned", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["evaluation_contract"]["stage"] == "master_planned"
    assert payload["head_contract_paths"] == [
        "web/core/agent_workers.py",
        "web/core/prompts/worker_prompt.md",
    ]
    assert payload["head_external_paths"] == [
        "web/core/agent_master.py",
        "web/core/eval_stats.py",
        "web/core/prompts/master_prompt.md",
    ]


def test_evaluation_contract_always_tracks_guard_files_after_workers(monkeypatch):
    import evaluation_contract

    changed_paths = [
        "web/core/evaluation_contract.py",
        "web/core/prompts/worker_prompt.md",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: list(changed_paths),
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "critic_checked", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["head_contract_paths"] == ["web/core/evaluation_contract.py"]
    assert payload["head_external_paths"] == ["web/core/prompts/worker_prompt.md"]


def test_evaluation_contract_tracks_shared_native_and_official_authority_files(monkeypatch):
    import evaluation_contract

    contract_paths = [
        "sever/server/transport.py",
        "web/core/national_game_runtime.py",
        "web/core/national_bot_launcher.py",
        "web/core/national_runtime_telemetry.py",
        "web/core/runtime_capacity.py",
        "web/core/blocking_runtime.py",
        "web/core/official_platform_resource.py",
        "web/core/official_execution_profile.json",
        "web/core/official_verdict_ledger.py",
    ]
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: [
            *contract_paths,
            "web/server/routes/national_arena.py",
        ],
    )

    allowed, payload = evaluation_contract.evaluate_head_drift(
        Path.cwd(),
        "old123",
        "new456",
        candidate_v=300,
        source_v=299,
        checkpoint={"stage": "master_planned", "next_v": 300, "source_v": 299},
    )

    assert allowed is False
    assert payload["head_contract_paths"] == sorted(contract_paths)
    assert payload["head_external_paths"] == ["web/server/routes/national_arena.py"]


def test_worktree_scope_keeps_observability_nonblocking_but_gate_logic_blocking():
    import evolution_scope

    scope = evolution_scope.classify_status_entries(
        [
            " M web/core/event_bus.py",
            " M web/core/system_log.py",
            " M web/core/web_ui.py",
            " M web/core/eval_stats.py",
            " M web/core/worker_boundary.py",
        ],
        candidate_v=300,
    )

    assert " M web/core/eval_stats.py" in scope["critical_entries"]
    assert " M web/core/worker_boundary.py" in scope["critical_entries"]
    assert " M web/core/event_bus.py" in scope["external_entries"]
    assert " M web/core/system_log.py" in scope["external_entries"]
    assert " M web/core/web_ui.py" in scope["external_entries"]
    assert scope["blocking_entries"] == [
        " M web/core/eval_stats.py",
        " M web/core/worker_boundary.py",
    ]


def test_evolution_scope_is_exact_file_scoped():
    import evolution_scope

    assert evolution_scope.CRITICAL_PREFIXES == ()
    assert evolution_scope.classify_path("web/core/replay_spotlight.py", candidate_v=300) == "external"
    assert evolution_scope.classify_path("web/core/eval_stats.py", candidate_v=300) == "critical"
    assert evolution_scope.classify_path("sever/main.py", candidate_v=300) == "external"
    assert evolution_scope.classify_path("sever/server/tcp_server.py", candidate_v=300) == "critical"
    assert evolution_scope.classify_path("sever/国赛平台/通信协议.docx", candidate_v=300) == "external"
    assert evolution_scope.classify_path("bots/national_v300/policy.py", candidate_v=300) == "candidate"
    assert evolution_scope.classify_path("bots/national_v299/policy.py", candidate_v=300) == "foreign_active_bot"


def test_evolution_scope_can_limit_foreign_bot_blocking_to_contract_versions():
    import evolution_scope

    assert (
        evolution_scope.classify_path(
            "bots/national_v299/policy.py",
            candidate_v=300,
            contract_bot_versions=[300, 299],
        )
        == "foreign_active_bot"
    )
    assert (
        evolution_scope.classify_path(
            "bots/national_v298/policy.py",
            candidate_v=300,
            contract_bot_versions=[300, 299],
        )
        == "external"
    )

    scope = evolution_scope.classify_status_entries(
        [
            " M bots/national_v299/policy.py",
            " M bots/national_v298/policy.py",
            "?? bots/national_v300/",
        ],
        candidate_v=300,
        contract_bot_versions=[300, 299],
    )

    assert scope["blocking_entries"] == [" M bots/national_v299/policy.py"]
    assert scope["external_entries"] == [" M bots/national_v298/policy.py"]
    assert scope["candidate_entries"] == ["?? bots/national_v300/"]


def test_evaluation_contract_hash_ignores_non_contract_national_docs(tmp_path, monkeypatch):
    import evaluation_contract

    server_file = tmp_path / "sever" / "server" / "tcp_server.py"
    docs_file = tmp_path / "sever" / "国赛平台" / "通信协议.docx"
    server_file.parent.mkdir(parents=True)
    docs_file.parent.mkdir(parents=True)
    server_file.write_text("server-v1\n", encoding="utf-8")
    docs_file.write_text("doc-v1\n", encoding="utf-8")

    monkeypatch.setattr(
        evaluation_contract,
        "_git_ls_files",
        lambda _root, _pathspecs: [
            "sever/server/tcp_server.py",
            "sever/国赛平台/通信协议.docx",
        ],
    )
    contract = evaluation_contract.build_evaluation_contract(tmp_path)

    before = evaluation_contract.evaluation_contract_hash(tmp_path, contract)
    docs_file.write_text("doc-v2\n", encoding="utf-8")
    after_doc_change = evaluation_contract.evaluation_contract_hash(tmp_path, contract)
    server_file.write_text("server-v2\n", encoding="utf-8")
    after_server_change = evaluation_contract.evaluation_contract_hash(tmp_path, contract)

    assert after_doc_change == before
    assert after_server_change != before


def test_runtime_guard_allows_explicit_abandon_after_contract_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "new456",
        "entries": ["?? bots/national_v300/"],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "direction_audited",
        repo_baseline={
            "head": "old123",
            "branch": "main...origin/main",
            "captured_stage": "direction_audited",
        },
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "abandon_generation",
        {},
    )

    assert ok is True
    assert payload["guard"] == "ok"
    assert payload["candidate_v"] == 300


def test_runtime_guard_abandon_still_blocks_unrelated_dirty_entries(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "new456",
        "entries": [
            "?? bots/national_v300/",
            " M web/core/official_certification.py",
        ],
    }
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "direction_audited",
        repo_baseline={
            "head": "old123",
            "branch": "main...origin/main",
            "captured_stage": "direction_audited",
        },
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "abandon_generation",
        {},
    )

    assert ok is False
    assert payload["reason"] == "unexpected_worktree_entries"
    assert " M web/core/official_certification.py" in payload["unexpected_entries"]


def test_runtime_guard_allows_execute_workers_after_repair_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "quality_failed",
        repo_baseline={"head": "old123", "branch": "main...origin/main", "captured_stage": "prepared"},
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_repair_allowed"] is True
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_execute_workers_after_master_planned_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "master_planned",
        master_plan={"tasks": [{"worker_id": "w1", "target_files": ["policy.py"]}]},
        repo_baseline={"head": "old123", "branch": "main...origin/main", "captured_stage": "selected"},
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "initial_workers"
    assert payload["stage"] == "master_planned"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_direction_audit_after_prepared_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "prepared",
        repo_baseline={"head": "old123", "branch": "main...origin/main", "captured_stage": "selected"},
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_direction_audit",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "pre_master"
    assert payload["stage"] == "prepared"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_master_after_direction_audited_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "direction_audited",
        repo_baseline={"head": "old123", "branch": "main...origin/main", "captured_stage": "prepared"},
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_master",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "pre_master"
    assert payload["stage"] == "direction_audited"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_crossover_after_selected_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": []},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": []},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        250,
        "selected",
        parent2_v=240,
        repo_baseline={"head": "old123", "branch": "main...origin/main", "captured_stage": "selected"},
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_crossover",
        {"next_v": 300, "source_v": 250, "parent2_v": 240},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "selected"
    assert payload["stage"] == "selected"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_crossover_running_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        250,
        "crossover_running",
        parent2_v=240,
        repo_baseline={"head": "old123", "branch": "main...origin/main", "captured_stage": "selected"},
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_crossover",
        {"target_v": 300, "parent_a": 250, "parent_b": 240},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "crossover"
    assert payload["stage"] == "crossover_running"


def test_runtime_guard_allows_quality_after_workers_done_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "workers_done",
        repo_baseline={"head": "old123", "branch": "main...origin/main", "captured_stage": "workers_done"},
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_quality_gates",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "gate"
    assert payload["stage"] == "workers_done"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_review_after_post_quality_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "quality_passed",
        repo_baseline={"head": "old123", "branch": "main...origin/main", "captured_stage": "quality_passed"},
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_review",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "post_quality"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_review_repair_workers_after_post_quality_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "quality_passed",
        repo_baseline={"head": "old123", "branch": "main...origin/main", "captured_stage": "quality_passed"},
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "execute_workers",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "post_quality"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_allows_commit_after_verified_head_drift(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    snapshots = iter([
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "main...origin/main", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "verified",
        repo_baseline={"head": "old123", "branch": "main...origin/main", "captured_stage": "quality_passed"},
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "commit_bot",
        {"version": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["head_drift_repair_allowed"] is False
    assert payload["resume_kind"] == "post_quality"
    assert payload["stage"] == "verified"
    assert payload["baseline_head"] == "old123"
    assert payload["current_head"] == "new456"


def test_runtime_guard_blocks_clean_branch_drift_without_auto_checkout(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    monkeypatch.delenv("POK_RUNTIME_EXPECTED_HEAD", raising=False)
    snapshot = {"ok": True, "branch": "codex/refactor", "head": "abc123", "entries": ["?? bots/national_v300/"]}
    commands = []
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "_run_git", lambda *args: commands.append(args))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_literature_probe",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "branch_drift"
    assert payload["expected_branch"] == "main"
    assert commands == []


def test_runtime_guard_allows_non_commit_tool_on_same_head_branch_alias(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "abc123")
    snapshots = iter([
        {"ok": True, "branch": "codex/refactor", "head": "abc123", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "codex/refactor", "head": "abc123", "entries": ["?? bots/national_v300/"]},
    ])
    events = []
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "direction_audited",
        repo_baseline={
            "head": "abc123",
            "branch": "main...origin/main",
            "captured_stage": "direction_audited",
        },
    ))
    monkeypatch.setattr(tool_runtime_guard, "_log_guard_event", lambda *args: events.append(args))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_literature_probe",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["branch_alias_allowed"] is True
    assert events[0][0] == "repo.runtime_guard_branch_alias_allowed"


def test_runtime_guard_allows_pre_master_head_resume_on_same_head_branch_alias(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "new456")
    snapshots = iter([
        {"ok": True, "branch": "codex/refactor", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "codex/refactor", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: None)
    monkeypatch.setattr(tool_runtime_guard, "_unrelated_head_drift_allowed", lambda **_kwargs: (False, {}))
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "direction_audited",
        repo_baseline={"head": "old123", "branch": "main...origin/main", "captured_stage": "prepared"},
    ))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_master",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_resume_allowed"] is True
    assert payload["resume_kind"] == "pre_master"
    assert payload["branch_alias_allowed"] is True


def test_runtime_guard_blocks_commit_on_same_head_branch_alias(monkeypatch):
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "abc123")
    snapshot = {"ok": True, "branch": "codex/refactor", "head": "abc123", "entries": ["?? bots/national_v300/"]}
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "abc123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "commit_bot",
        {"version": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "branch_drift"
    assert payload["expected_branch"] == "main"


def test_runtime_guard_allows_non_commit_tool_on_branch_with_unrelated_head_drift(monkeypatch):
    import evaluation_contract
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "old123")
    snapshots = iter([
        {"ok": True, "branch": "codex/docs", "head": "new456", "entries": ["?? bots/national_v300/"]},
        {"ok": True, "branch": "codex/docs", "head": "new456", "entries": ["?? bots/national_v300/"]},
    ])
    events = []
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "old123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: _strict_checkpoint(
        300,
        299,
        "quality_passed",
        repo_baseline={
            "head": "old123",
            "branch": "main...origin/main",
            "captured_stage": "quality_passed",
        },
    ))
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args: ["docs/notes.md", "bots/neural_national_lab/data/run.json"],
    )
    monkeypatch.setattr(tool_runtime_guard, "_log_guard_event", lambda *args: events.append(args))

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "run_review",
        {"next_v": 300, "source_v": 299},
    )

    assert ok is True
    assert payload["head_drift_unrelated_allowed"] is True
    assert payload["head_changed_paths"] == [
        "docs/notes.md",
        "bots/neural_national_lab/data/run.json",
    ]
    assert any(event[0] == "repo.runtime_guard_branch_head_drift_unrelated_allowed" for event in events)


def test_runtime_guard_blocks_commit_on_branch_with_unrelated_head_drift(monkeypatch):
    import evaluation_contract
    import tool_runtime_guard

    monkeypatch.setenv("POK_FORCE_TOOL_RUNTIME_GUARD", "1")
    monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "old123")
    snapshot = {"ok": True, "branch": "codex/docs", "head": "new456", "entries": ["?? bots/national_v300/"]}
    monkeypatch.setattr(tool_runtime_guard, "git_worktree_snapshot", lambda: snapshot)
    monkeypatch.setattr(tool_runtime_guard, "get_last_snapshot", lambda: {"head": "old123"})
    monkeypatch.setattr(tool_runtime_guard, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(evaluation_contract, "changed_paths_between_heads", lambda *_args: ["docs/notes.md"])

    ok, payload = tool_runtime_guard.ensure_runtime_git_guard(
        "commit_bot",
        {"version": 300, "source_v": 299},
    )

    assert ok is False
    assert payload["reason"] == "branch_drift"
    assert payload["expected_branch"] == "main"


def test_write_pipeline_checkpoint_persists_repo_baseline(tmp_path, monkeypatch):
    import evolution_infra
    import repo_state

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: {
        "branch": "main...origin/main",
        "head": "abc123",
        "entry_count": 1,
        "dirty_count": 0,
        "untracked_count": 1,
        "entries": ["?? bots/national_v300/"],
        "truncated": False,
    })

    assert evolution_infra.write_pipeline_checkpoint(300, 299, "prepared") is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "abc123"
    assert state["repo_baseline"]["branch"] == "main...origin/main"
    assert state["repo_baseline"]["captured_stage"] == "prepared"


def test_write_pipeline_checkpoint_refreshes_baseline_after_planning_handoff(tmp_path, monkeypatch):
    import evolution_infra
    import repo_state

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    snapshots = iter([
        {
            "branch": "main...origin/main",
            "head": "old123",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "mid456",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "new789",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
    ])
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    assert evolution_infra.write_pipeline_checkpoint(300, 299, "prepared") is True
    assert evolution_infra.write_pipeline_checkpoint(300, 299, "direction_audited") is True
    state = evolution_infra.read_pipeline_checkpoint()
    assert state["repo_baseline"]["head"] == "mid456"
    assert state["repo_baseline"]["captured_stage"] == "direction_audited"

    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "master_planned",
        master_plan={"tasks": [{"worker_id": "w1", "target_files": ["policy.py"]}]},
    ) is True
    state = evolution_infra.read_pipeline_checkpoint()
    assert state["repo_baseline"]["head"] == "new789"
    assert state["repo_baseline"]["captured_stage"] == "master_planned"


def test_write_pipeline_checkpoint_refreshes_baseline_on_rework(tmp_path, monkeypatch):
    import evolution_infra
    import repo_state

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    snapshots = iter([
        {
            "branch": "main...origin/main",
            "head": "old123",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "new456",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
    ])
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    assert evolution_infra.write_pipeline_checkpoint(300, 299, "quality_failed") is True
    assert evolution_infra.write_pipeline_checkpoint(300, 299, "repair_planned") is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "new456"
    assert state["repo_baseline"]["captured_stage"] == "repair_planned"


def test_write_pipeline_checkpoint_refreshes_baseline_after_quality_gate(tmp_path, monkeypatch):
    import evolution_infra
    import repo_state

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    snapshots = iter([
        {
            "branch": "main...origin/main",
            "head": "old123",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "new456",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
    ])
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    assert evolution_infra.write_pipeline_checkpoint(300, 299, "workers_done") is True
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "quality_passed",
        gate_results={"quality": {"all_passed": True}},
    ) is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "new456"
    assert state["repo_baseline"]["captured_stage"] == "quality_passed"


def test_write_pipeline_checkpoint_refreshes_same_validation_stage_with_gate_result(tmp_path, monkeypatch):
    import evolution_infra
    import repo_state

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    snapshots = iter([
        {
            "branch": "main...origin/main",
            "head": "old123",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "new456",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
    ])
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "quality_passed",
        gate_results={"quality": {"all_passed": True}},
    ) is True
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "quality_passed",
        gate_results={"quality": {"all_passed": True, "rerun": True}},
    ) is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "new456"
    assert state["repo_baseline"]["captured_stage"] == "quality_passed"


def test_write_pipeline_checkpoint_refreshes_baseline_after_precommit_gate(tmp_path, monkeypatch):
    import evolution_infra
    import repo_state

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    snapshots = iter([
        {
            "branch": "main...origin/main",
            "head": "old123",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
        {
            "branch": "main...origin/main",
            "head": "new456",
            "entry_count": 1,
            "dirty_count": 0,
            "untracked_count": 1,
            "entries": ["?? bots/national_v300/"],
            "truncated": False,
        },
    ])
    monkeypatch.setattr(repo_state, "git_worktree_snapshot", lambda: next(snapshots))

    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "critic_checked",
        precommit_attempt=2,
        gate_results={
            "quality": {"all_passed": True},
            "review": {"approved": True},
            "critic": {"approved": True},
        },
    ) is True
    assert evolution_infra.write_pipeline_checkpoint(
        300,
        299,
        "verified",
        gate_results={"precommit_eval": {"passed": True}},
    ) is True
    state = evolution_infra.read_pipeline_checkpoint()

    assert state["repo_baseline"]["head"] == "new456"
    assert state["repo_baseline"]["captured_stage"] == "verified"
    assert state["precommit_attempt"] == 2


def test_publish_ready_blocks_required_push_disabled(monkeypatch):
    import evolution_infra

    monkeypatch.setenv("POK_REQUIRE_EVOLUTION_PUSH", "1")
    monkeypatch.delenv("EVOLUTION_GIT_PUSH", raising=False)
    monkeypatch.setattr(evolution_infra, "git_publish_status", lambda: {
        "ok": True,
        "branch": "main",
        "head": "abc123",
        "upstream": "origin/main",
        "upstream_head": "abc123",
        "ahead": 0,
        "behind": 0,
    })

    ok, payload = evolution_infra.ensure_publish_ready_for_new_generation()

    assert ok is False
    assert payload["reason"] == "evolution_git_push_disabled"


def test_publish_ready_blocks_unpublished_local_commits(monkeypatch):
    import evolution_infra

    monkeypatch.setenv("POK_REQUIRE_EVOLUTION_PUSH", "1")
    monkeypatch.setenv("EVOLUTION_GIT_PUSH", "1")
    monkeypatch.setattr(evolution_infra, "git_publish_status", lambda: {
        "ok": True,
        "branch": "main",
        "head": "def456",
        "upstream": "origin/main",
        "upstream_head": "abc123",
        "ahead": 2,
        "behind": 0,
    })

    ok, payload = evolution_infra.ensure_publish_ready_for_new_generation()

    assert ok is False
    assert payload["reason"] == "unpublished_local_commits"


def test_publish_ready_allows_synchronized_runtime(monkeypatch):
    import evolution_infra

    monkeypatch.setenv("POK_EVOLUTION_RUNTIME", "1")
    monkeypatch.setenv("EVOLUTION_GIT_PUSH", "1")
    monkeypatch.delenv("POK_REQUIRE_EVOLUTION_PUSH", raising=False)
    monkeypatch.setattr(evolution_infra, "git_publish_status", lambda: {
        "ok": True,
        "branch": "main",
        "head": "abc123",
        "upstream": "origin/main",
        "upstream_head": "abc123",
        "ahead": 0,
        "behind": 0,
    })

    ok, payload = evolution_infra.ensure_publish_ready_for_new_generation()

    assert ok is True
    assert payload["push_required"] is True
    assert payload["push_enabled"] is True


def test_checkpoint_recovery_diagnostics_allows_workers_done_head_mismatch(tmp_path):
    import pipeline_recovery

    _strict_artifact(tmp_path / "bots" / "national_v257", 257)
    checkpoint = _strict_checkpoint(
        257, 197, "workers_done", repo_baseline={"branch": "main", "head": "old123"}
    )
    snapshot = {"ok": True, "branch": "main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_gate_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True
    assert diag["target"]["exists"] is True


def test_checkpoint_recovery_diagnostics_allows_same_head_branch_alias(tmp_path, monkeypatch):
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    _strict_artifact(tmp_path / "bots" / "national_v257", 257)
    checkpoint = _strict_checkpoint(
        257, 197, "workers_done", repo_baseline={"branch": "main", "head": "same123"}
    )
    snapshot = {"ok": True, "branch": "codex/refactor", "head": "same123"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is True
    assert "repo_not_on_evolution_branch" not in diag["issues"]
    assert "repo_baseline_branch_mismatch" not in diag["issues"]
    assert "repo_current_branch_alias_resume" in diag["warnings"]
    assert "repo_baseline_branch_alias_resume" in diag["warnings"]
    assert diag["repo"]["current_branch_alias_allowed"] is True
    assert diag["repo"]["baseline_branch_alias_allowed"] is True


def test_checkpoint_recovery_diagnostics_blocks_verified_branch_alias(tmp_path, monkeypatch):
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    _strict_artifact(tmp_path / "bots" / "national_v257", 257)
    checkpoint = _strict_checkpoint(
        257, 197, "verified", repo_baseline={"branch": "main", "head": "same123"}
    )
    snapshot = {"ok": True, "branch": "codex/refactor", "head": "same123"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is False
    assert "repo_not_on_evolution_branch" in diag["issues"]


def test_checkpoint_recovery_diagnostics_allows_main_resume_from_alias_after_ancestor_head_advance(
    tmp_path,
    monkeypatch,
):
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    monkeypatch.setattr(pipeline_recovery, "_head_is_ancestor", lambda *_args: True)
    _strict_artifact(tmp_path / "bots" / "national_v281", 281)
    checkpoint = _strict_checkpoint(
        281,
        279,
        "workers_done",
        repo_baseline={"branch": "codex/neural-work", "head": "old123"},
    )
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is True
    assert "repo_baseline_branch_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_branch_alias_resume" in diag["warnings"]
    assert "repo_baseline_head_mismatch_gate_resume" in diag["warnings"]
    assert diag["repo"]["baseline_branch_alias_allowed"] is True
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True
    assert diag["repo"]["baseline_branch_alias_reason"] == "main_resume_ancestor_head_drift"


def test_checkpoint_recovery_diagnostics_blocks_main_resume_from_alias_after_non_ancestor_head_advance(
    tmp_path,
    monkeypatch,
):
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    monkeypatch.setattr(pipeline_recovery, "_head_is_ancestor", lambda *_args: False)
    _strict_artifact(tmp_path / "bots" / "national_v281", 281)
    checkpoint = _strict_checkpoint(
        281,
        279,
        "workers_done",
        repo_baseline={"branch": "codex/neural-work", "head": "old123"},
    )
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is False
    assert "repo_baseline_branch_mismatch" in diag["issues"]
    assert "repo_baseline_head_mismatch" in diag["issues"]
    assert diag["repo"]["baseline_branch_alias_reason"] == "non_ancestor_head_drift"


def test_checkpoint_recovery_diagnostics_allows_branch_with_unrelated_head_drift(
    tmp_path,
    monkeypatch,
):
    import evaluation_contract
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args: [
            "bots/neural_national_lab/data/run.json",
            "bots/neural_national_lab/versions/v026/main.py",
        ],
    )
    _strict_artifact(tmp_path / "bots" / "national_v281", 281)
    checkpoint = _strict_checkpoint(
        281,
        279,
        "quality_passed",
        repo_baseline={"branch": "main...origin/main", "head": "old123"},
    )
    snapshot = {"ok": True, "branch": "codex/neural-work", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is True
    assert "repo_not_on_evolution_branch" not in diag["issues"]
    assert "repo_baseline_branch_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_current_branch_unrelated_head_resume" in diag["warnings"]
    assert "repo_baseline_head_mismatch_post_quality_resume" in diag["warnings"]
    assert diag["repo"]["current_branch_unrelated_head_allowed"] is True


def test_checkpoint_recovery_diagnostics_blocks_branch_with_critical_head_drift(
    tmp_path,
    monkeypatch,
):
    import evaluation_contract
    import pipeline_recovery

    monkeypatch.setenv("POK_FORCE_PIPELINE_RECOVERY_GUARD", "1")
    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args: ["web/core/orchestrator.py"],
    )
    _strict_artifact(tmp_path / "bots" / "national_v281", 281)
    checkpoint = _strict_checkpoint(
        281,
        279,
        "quality_passed",
        repo_baseline={"branch": "main...origin/main", "head": "old123"},
    )
    snapshot = {"ok": True, "branch": "codex/neural-work", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is False
    assert "repo_not_on_evolution_branch" in diag["issues"]
    assert "repo_baseline_branch_mismatch" in diag["issues"]
    assert "repo_baseline_head_mismatch" in diag["issues"]
    assert diag["repo"]["current_branch_head_blocking_entries"] == ["?? web/core/orchestrator.py"]


def test_checkpoint_recovery_diagnostics_allows_master_planned_head_mismatch(tmp_path):
    import pipeline_recovery

    _strict_artifact(tmp_path / "bots" / "national_v257", 257)
    checkpoint = _strict_checkpoint(
        257,
        197,
        "master_planned",
        repo_baseline={"branch": "main", "head": "old123"},
        master_plan={"tasks": [{"worker_id": "w1", "target_files": ["policy.py"]}]},
    )
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_initial_workers_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True
    assert diag["target"]["exists"] is True


def test_checkpoint_recovery_allows_master_planned_contract_head_mismatch(
    tmp_path,
    monkeypatch,
):
    import evaluation_contract
    import pipeline_recovery

    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: ["web/core/prompts/worker_prompt.md"],
    )
    _strict_artifact(tmp_path / "bots" / "national_v257", 257)
    checkpoint = _strict_checkpoint(
        257,
        197,
        "master_planned",
        repo_baseline={
            "branch": "main",
            "head": "old123",
            "captured_stage": "selected",
            "evaluation_contract": {"version": 2, "hash": "old"},
        },
        master_plan={"tasks": [{"worker_id": "w1", "target_files": ["policy.py"]}]},
    )
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_initial_workers_resume" in diag["warnings"]
    assert diag["repo"]["baseline_evaluation_contract_unchanged"] is False
    assert "web/core/prompts/worker_prompt.md" in diag["repo"]["baseline_head_contract_paths"]
    assert diag["repo"]["head_drift_requires_contract_unchanged"] is False
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True


def test_checkpoint_recovery_diagnostics_allows_selected_head_mismatch_without_target(tmp_path):
    import pipeline_recovery

    checkpoint = _strict_checkpoint(
        257,
        197,
        "selected",
        parent2_v=188,
        repo_baseline={"branch": "main", "head": "old123"},
    )
    snapshot = {"ok": True, "branch": "main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_selected_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True
    assert "target" not in diag


def test_checkpoint_recovery_diagnostics_allows_crossover_running_head_mismatch(tmp_path):
    import pipeline_recovery

    _strict_artifact(tmp_path / "bots" / "national_v257", 257)
    checkpoint = _strict_checkpoint(
        257,
        197,
        "crossover_running",
        parent2_v=188,
        repo_baseline={"branch": "main", "head": "old123"},
    )
    snapshot = {"ok": True, "branch": "main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_crossover_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True
    assert diag["repo"]["head_drift_resume_kind"] == "crossover"
    assert diag["target"]["exists"] is True


def test_checkpoint_recovery_diagnostics_allows_reentrant_crossover_after_contract_head_drift(
    tmp_path,
    monkeypatch,
):
    import evaluation_contract
    import pipeline_recovery

    monkeypatch.setattr(
        evaluation_contract,
        "changed_paths_between_heads",
        lambda *_args, **_kwargs: ["web/core/pipeline_state.py"],
    )
    _strict_artifact(tmp_path / "bots" / "national_v257", 257)
    checkpoint = _strict_checkpoint(
        257,
        197,
        "crossover_running",
        parent2_v=188,
        repo_baseline={
            "branch": "main",
            "head": "old123",
            "evaluation_contract": {"version": 2, "hash": "old"},
        },
    )
    snapshot = {"ok": True, "branch": "main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "web/core/pipeline_state.py" in diag["repo"]["baseline_head_contract_paths"]
    assert diag["repo"]["baseline_evaluation_contract_unchanged"] is False
    assert diag["repo"]["head_drift_requires_contract_unchanged"] is False
    assert diag["repo"]["head_drift_resume_kind"] == "crossover"


def test_checkpoint_recovery_diagnostics_allows_pre_master_head_mismatch(tmp_path):
    import pipeline_recovery

    for stage in ("prepared", "direction_audited"):
        _strict_artifact(tmp_path / "bots" / "national_v257", 257)
        checkpoint = _strict_checkpoint(
            257, 197, stage, repo_baseline={"branch": "main", "head": "old123"}
        )
        snapshot = {"ok": True, "branch": "main", "head": "new456"}

        diag = pipeline_recovery.checkpoint_recovery_diagnostics(
            checkpoint,
            snapshot=snapshot,
            project_root=tmp_path,
        )

        assert diag["active"] is True
        assert diag["recoverable"] is True
        assert "repo_baseline_head_mismatch" not in diag["issues"]
        assert "repo_baseline_head_mismatch_pre_master_resume" in diag["warnings"]
        assert diag["repo"]["baseline_head_mismatch_allowed"] is True
        assert diag["target"]["exists"] is True


def test_checkpoint_recovery_diagnostics_blocks_preparing_repo_head_mismatch(tmp_path):
    import pipeline_recovery

    _strict_artifact(tmp_path / "bots" / "national_v257", 257)
    checkpoint = _strict_checkpoint(
        257, 197, "preparing", repo_baseline={"branch": "main", "head": "old123"}
    )
    snapshot = {"ok": True, "branch": "main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is False
    assert "repo_baseline_head_mismatch" in diag["issues"]
    assert diag["target"]["exists"] is True


def test_checkpoint_recovery_diagnostics_allows_repair_head_mismatch(tmp_path):
    import pipeline_recovery

    _strict_artifact(tmp_path / "bots" / "national_v269", 269)
    checkpoint = _strict_checkpoint(
        269, 237, "quality_failed", repo_baseline={"branch": "main", "head": "old123"}
    )
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_repair_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True


def test_checkpoint_recovery_diagnostics_allows_post_quality_head_mismatch(tmp_path):
    import pipeline_recovery

    _strict_artifact(tmp_path / "bots" / "national_v269", 269)
    checkpoint = _strict_checkpoint(
        269, 237, "quality_passed", repo_baseline={"branch": "main", "head": "old123"}
    )
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_post_quality_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True


def test_checkpoint_recovery_diagnostics_allows_verified_head_mismatch(tmp_path):
    import pipeline_recovery

    _strict_artifact(tmp_path / "bots" / "national_v269", 269)
    checkpoint = _strict_checkpoint(
        269, 237, "verified", repo_baseline={"branch": "main", "head": "old123"}
    )
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "new456"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    assert "repo_baseline_head_mismatch_post_quality_resume" in diag["warnings"]
    assert diag["repo"]["baseline_head_mismatch_allowed"] is True


def test_checkpoint_recovery_diagnostics_allows_matching_active_checkpoint(tmp_path):
    import pipeline_recovery

    _strict_artifact(tmp_path / "bots" / "national_v258", 258)
    checkpoint = _strict_checkpoint(
        258, 254, "workers_done", repo_baseline={"branch": "main", "head": "same123"}
    )
    snapshot = {"ok": True, "branch": "main...origin/main", "head": "same123"}

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["active"] is True
    assert diag["recoverable"] is True
    assert diag["issues"] == []


def test_checkpoint_recovery_diagnostics_ignores_unrelated_dirty_entries(tmp_path):
    import pipeline_recovery

    _strict_artifact(tmp_path / "bots" / "national_v258", 258)
    checkpoint = _strict_checkpoint(
        258, 254, "workers_done", repo_baseline={"branch": "main", "head": "same123"}
    )
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "same123",
        "entries": [
            " M docs/notes.md",
            "?? bots/neural_national_lab/data/run.jsonl",
            "?? bots/national_v258/",
        ],
    }

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is True
    assert "repo_unrelated_worktree_entries_ignored" in diag["warnings"]
    assert diag["worktree_scope"]["ignored_count"] == 2


def test_checkpoint_recovery_diagnostics_only_blocks_contract_bot_versions(tmp_path):
    import pipeline_recovery

    _strict_artifact(tmp_path / "bots" / "national_v258", 258)
    checkpoint = _strict_checkpoint(
        258,
        254,
        "workers_done",
        parent2_v=253,
        repo_baseline={"branch": "main", "head": "same123"},
    )
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "same123",
        "entries": [
            " M bots/national_v254/policy.py",
            " M bots/national_v999/policy.py",
        ],
    }

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is False
    assert "repo_blocking_worktree_entries" in diag["issues"]
    assert diag["worktree_scope"]["blocking_entries"] == [" M bots/national_v254/policy.py"]
    assert diag["worktree_scope"]["ignored_entries"] == [" M bots/national_v999/policy.py"]


def test_checkpoint_recovery_diagnostics_blocks_critical_dirty_entries(tmp_path):
    import pipeline_recovery

    _strict_artifact(tmp_path / "bots" / "national_v258", 258)
    checkpoint = _strict_checkpoint(
        258, 254, "workers_done", repo_baseline={"branch": "main", "head": "same123"}
    )
    snapshot = {
        "ok": True,
        "branch": "main...origin/main",
        "head": "same123",
        "entries": [" M sever/server/tcp_server.py", "?? bots/national_v258/"],
    }

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot=snapshot,
        project_root=tmp_path,
    )

    assert diag["recoverable"] is False
    assert "repo_blocking_worktree_entries" in diag["issues"]
    assert " M sever/server/tcp_server.py" in diag["worktree_scope"]["blocking_entries"]


def test_runtime_branch_guard_requests_shutdown_on_branch_drift(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    snapshots = iter([
        {"branch": "main", "head": "abc123", "branch_status": "main"},
        {"branch": "codex/other", "head": "abc123", "branch_status": "codex/other"},
    ])
    events = []
    cleared = []
    monkeypatch.setattr(orchestrator, "_runtime_git_identity", lambda: next(snapshots))
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *args: events.append(args))
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **kwargs: cleared.append(kwargs))

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    class DummyOwnerTask:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    shutdown = DummyShutdown()
    owner = DummyOwnerTask()
    hard_stop = asyncio.Event()

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="",
        owner_task=owner,
        hard_stop_event=hard_stop,
        check_interval=0.001,
    ))

    assert shutdown.requested is True
    assert owner.cancelled is True
    assert hard_stop.is_set() is True
    assert cleared == [{"reason": "runtime_branch_drift"}]
    assert events[0][0] == "repo.runtime_branch_drift_shutdown"
    assert events[0][3]["reason"] == "branch_drift"


def test_runtime_git_identity_tolerates_empty_branch_status(monkeypatch):
    import orchestrator

    calls = []

    class Result:
        def __init__(self, returncode, stdout=""):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["git", "status", "--short"]:
            return Result(1, "")
        if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return Result(0, "main\n")
        if args == ["git", "rev-parse", "--short=12", "HEAD"]:
            return Result(0, "abc123def456\n")
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)

    identity = orchestrator._runtime_git_identity()

    assert orchestrator._branch_name("") == ""
    assert identity == {
        "branch": "main",
        "branch_status": "main",
        "head": "abc123def456",
    }
    assert "--untracked-files=no" in calls[0]


def test_runtime_branch_guard_tolerates_same_head_branch_alias(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    snapshots = iter([
        {"branch": "codex/other", "head": "abc123", "branch_status": "codex/other"},
    ])
    events = []
    cleared = []
    monkeypatch.setattr(orchestrator, "_runtime_git_identity", lambda: next(snapshots))
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **kwargs: cleared.append(kwargs))

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    class DummyOwnerTask:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True

    shutdown = DummyShutdown()

    def _fake_log(*args):
        events.append(args)
        shutdown.is_shutting_down = True

    monkeypatch.setattr(orchestrator, "log_system_event", _fake_log)

    owner = DummyOwnerTask()
    hard_stop = asyncio.Event()

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="abc123",
        owner_task=owner,
        hard_stop_event=hard_stop,
        check_interval=0.001,
    ))

    assert shutdown.requested is False
    assert owner.cancelled is False
    assert hard_stop.is_set() is False
    assert cleared == []
    assert events[0][0] == "repo.runtime_branch_alias_allowed"


def test_runtime_branch_guard_tolerates_unrelated_head_drift(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    snapshots = iter([
        {"branch": "codex/docs", "head": "def456", "branch_status": "codex/docs"},
    ])
    events = []
    cleared = []
    monkeypatch.setattr(orchestrator, "_runtime_git_identity", lambda: next(snapshots))
    monkeypatch.setattr(
        orchestrator,
        "_runtime_head_drift_unrelated_allowed",
        lambda *_args: (True, {"head_changed_paths": ["docs/notes.md"], "candidate_v": 300}),
    )
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **kwargs: cleared.append(kwargs))

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    shutdown = DummyShutdown()

    def _fake_log(*args):
        events.append(args)
        shutdown.is_shutting_down = True

    monkeypatch.setattr(orchestrator, "log_system_event", _fake_log)

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="abc123",
        check_interval=0.001,
    ))

    assert shutdown.requested is False
    assert cleared == []
    assert events[0][0] == "repo.runtime_head_drift_unrelated_allowed"
    assert events[0][3]["head_changed_paths"] == ["docs/notes.md"]
    assert events[0][3]["advanced_expected_head"] == "def456"
    assert os.environ["POK_RUNTIME_EXPECTED_HEAD"] == "def456"


def test_runtime_branch_guard_advances_baseline_for_repeated_unrelated_head_drift(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    monkeypatch.delenv("POK_RUNTIME_EXPECTED_HEAD", raising=False)

    snapshots = iter([
        {"branch": "main", "head": "def456", "branch_status": "main"},
        {"branch": "main", "head": "ghi789", "branch_status": "main"},
    ])
    drift_checks = []
    events = []
    cleared = []
    monkeypatch.setattr(orchestrator, "_runtime_git_identity", lambda: next(snapshots))

    def _allow_unrelated(expected_head, current_head):
        drift_checks.append((expected_head, current_head))
        return True, {"head_changed_paths": [f"docs/{current_head}.md"]}

    monkeypatch.setattr(orchestrator, "_runtime_head_drift_unrelated_allowed", _allow_unrelated)
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **kwargs: cleared.append(kwargs))

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    shutdown = DummyShutdown()

    def _fake_log(*args):
        events.append(args)
        if len(events) == 2:
            shutdown.is_shutting_down = True

    monkeypatch.setattr(orchestrator, "log_system_event", _fake_log)

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="abc123",
        check_interval=0.001,
    ))

    assert shutdown.requested is False
    assert cleared == []
    assert [event[0] for event in events] == [
        "repo.runtime_head_drift_unrelated_allowed",
        "repo.runtime_head_drift_unrelated_allowed",
    ]
    assert drift_checks == [("abc123", "def456"), ("def456", "ghi789")]
    assert events[0][3]["advanced_expected_head"] == "def456"
    assert events[1][3]["expected_head"] == "def456"
    assert events[1][3]["advanced_expected_head"] == "ghi789"
    assert os.environ["POK_RUNTIME_EXPECTED_HEAD"] == "ghi789"


def test_runtime_branch_guard_adopts_pipeline_published_expected_head(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    monkeypatch.delenv("POK_RUNTIME_EXPECTED_HEAD", raising=False)

    snapshots = iter([
        {"branch": "main", "head": "def456", "branch_status": "main"},
    ])
    events = []
    cleared = []

    def _identity_after_pipeline_publish():
        monkeypatch.setenv("POK_RUNTIME_EXPECTED_HEAD", "def456")
        return next(snapshots)

    monkeypatch.setattr(orchestrator, "_runtime_git_identity", _identity_after_pipeline_publish)

    def _unexpected_drift_check(*_args):
        raise AssertionError("published current HEAD should be adopted before drift checks")

    monkeypatch.setattr(orchestrator, "_runtime_head_drift_unrelated_allowed", _unexpected_drift_check)
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **kwargs: cleared.append(kwargs))

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    shutdown = DummyShutdown()

    def _fake_log(*args):
        events.append(args)
        shutdown.is_shutting_down = True

    monkeypatch.setattr(orchestrator, "log_system_event", _fake_log)

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="abc123",
        check_interval=0.001,
    ))

    assert shutdown.requested is False
    assert cleared == []
    assert events[0][0] == "repo.runtime_expected_head_adopted"
    assert events[0][3]["previous_expected_head"] == "abc123"
    assert events[0][3]["expected_head"] == "def456"
    assert os.environ["POK_RUNTIME_EXPECTED_HEAD"] == "def456"


def test_publish_runtime_expected_head_updates_tool_guard_baseline(monkeypatch):
    import evolution_infra

    monkeypatch.delenv("POK_RUNTIME_EXPECTED_HEAD", raising=False)
    monkeypatch.setattr(evolution_infra, "_git", lambda *args, **_kwargs: "abc123def456\n")

    head = evolution_infra.publish_runtime_expected_head("test_publish", version=289)

    assert head == "abc123def456"
    assert os.environ["POK_RUNTIME_EXPECTED_HEAD"] == "abc123def456"


def test_runtime_branch_guard_requests_shutdown_on_head_drift(monkeypatch):
    import asyncio
    import orchestrator

    monkeypatch.setenv("POK_FORCE_RUNTIME_BRANCH_GUARD", "1")
    snapshots = iter([
        {"branch": "main", "head": "abc123", "branch_status": "main"},
        {"branch": "main", "head": "def456", "branch_status": "main"},
    ])
    events = []
    monkeypatch.setattr(orchestrator, "_runtime_git_identity", lambda: next(snapshots))
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *args: events.append(args))
    monkeypatch.setattr(orchestrator, "_clear_orchestrator_session", lambda **_kwargs: None)

    class DummyShutdown:
        def __init__(self):
            self.is_shutting_down = False
            self.requested = False

        def request_shutdown(self):
            self.requested = True
            self.is_shutting_down = True

    shutdown = DummyShutdown()

    asyncio.run(orchestrator._runtime_branch_guard_coroutine(
        None,
        shutdown,
        expected_branch="main",
        expected_head="abc123",
        check_interval=0.001,
    ))

    assert shutdown.requested is True
    assert events[0][0] == "repo.runtime_branch_drift_shutdown"
    assert events[0][3]["reason"] == "head_drift"


def test_checkpoint_recovery_diagnostics_tracks_rework_target_dirs(tmp_path):
    import pipeline_recovery

    for stage in ("quality_failed", "repair_planned", "rework_running"):
        _strict_artifact(tmp_path / "bots" / "national_v259", 259)
        checkpoint = _strict_checkpoint(
            259, 254, stage, repo_baseline={"branch": "main", "head": "same123"}
        )
        snapshot = {"ok": True, "branch": "main...origin/main", "head": "same123"}

        diag = pipeline_recovery.checkpoint_recovery_diagnostics(
            checkpoint,
            snapshot=snapshot,
            project_root=tmp_path,
        )

        assert diag["active"] is True
        assert diag["recoverable"] is True
        assert diag["target"]["exists"] is True


def test_startup_recovery_blocks_unrecoverable_checkpoint(monkeypatch):
    import sys
    from types import SimpleNamespace

    import orchestrator_session
    import pipeline_recovery

    checkpoint = _strict_checkpoint(
        257, 197, "workers_done", repo_baseline={"branch": "old", "head": "old123"}
    )
    cleared = []
    events = []
    fake_evolution_core = SimpleNamespace(
        read_pipeline_checkpoint=lambda: checkpoint,
        clear_pipeline_checkpoint=lambda: None,
    )
    fake_system_log = SimpleNamespace(
        log_system_event=lambda *args, **kwargs: events.append((args, kwargs))
    )

    monkeypatch.setitem(sys.modules, "evolution_core", fake_evolution_core)
    monkeypatch.setitem(sys.modules, "system_log", fake_system_log)
    monkeypatch.setattr(orchestrator_session, "_load_orchestrator_session", lambda: "session-abc")
    monkeypatch.setattr(
        orchestrator_session,
        "_clear_orchestrator_session",
        lambda reason="completed_or_reset": cleared.append(reason),
    )
    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda _checkpoint: {
            "active": True,
            "recoverable": False,
            "issues": ["repo_baseline_head_mismatch"],
        },
    )

    result = orchestrator_session._startup_recovery()

    assert result["action"] == "blocked"
    assert result["reason"] == "unrecoverable_checkpoint"
    assert cleared == ["unrecoverable_checkpoint"]
    assert any(args[0] == "orchestrator.recovery_blocked" for args, _ in events)


def test_startup_recovery_resumes_prepared_without_master_plan(monkeypatch):
    import sys
    from types import SimpleNamespace

    import evolution_infra
    import orchestrator_session
    import pipeline_recovery

    checkpoint = _strict_checkpoint(
        260,
        254,
        "prepared",
        master_plan=None,
        repo_baseline={"branch": "main", "head": "same123"},
    )
    cleared = []
    events = []
    fake_evolution_core = SimpleNamespace(
        read_pipeline_checkpoint=lambda: checkpoint,
        clear_pipeline_checkpoint=lambda: cleared.append("checkpoint"),
    )
    fake_system_log = SimpleNamespace(
        log_system_event=lambda *args, **kwargs: events.append((args, kwargs))
    )

    monkeypatch.setitem(sys.modules, "evolution_core", fake_evolution_core)
    monkeypatch.setitem(sys.modules, "system_log", fake_system_log)
    monkeypatch.setattr(orchestrator_session, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator_session,
        "_clear_orchestrator_session",
        lambda reason="completed_or_reset": cleared.append(reason),
    )
    monkeypatch.setattr(evolution_infra, "git_has_tag", lambda _v: False)
    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda _checkpoint: {"active": True, "recoverable": True, "issues": []},
    )

    result = orchestrator_session._startup_recovery()

    assert result["action"] == "resume"
    assert result["stage"] == "prepared"
    assert result["next_v"] == 260
    assert cleared == []
    assert any(args[0] == "orchestrator.recovery_decision" for args, _ in events)


def test_startup_recovery_resumes_old_quality_failed_checkpoint(monkeypatch):
    import sys
    from types import SimpleNamespace

    import evolution_infra
    import orchestrator_session
    import pipeline_recovery

    checkpoint = _strict_checkpoint(
        261,
        254,
        "quality_failed",
        timestamp="2000-01-01T00:00:00",
        repo_baseline={"branch": "main", "head": "same123"},
    )
    cleared = []
    events = []
    fake_evolution_core = SimpleNamespace(
        read_pipeline_checkpoint=lambda: checkpoint,
        clear_pipeline_checkpoint=lambda: cleared.append("checkpoint"),
    )
    fake_system_log = SimpleNamespace(
        log_system_event=lambda *args, **kwargs: events.append((args, kwargs))
    )

    monkeypatch.setitem(sys.modules, "evolution_core", fake_evolution_core)
    monkeypatch.setitem(sys.modules, "system_log", fake_system_log)
    monkeypatch.setattr(orchestrator_session, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator_session,
        "_clear_orchestrator_session",
        lambda reason="completed_or_reset": cleared.append(reason),
    )
    monkeypatch.setattr(evolution_infra, "git_has_tag", lambda _v: False)
    monkeypatch.setattr(
        pipeline_recovery,
        "checkpoint_recovery_diagnostics",
        lambda _checkpoint: {"active": True, "recoverable": True, "issues": []},
    )

    result = orchestrator_session._startup_recovery()

    assert result["action"] == "resume"
    assert result["stage"] == "quality_failed"
    assert result["next_v"] == 261
    assert cleared == []
    assert any(args[0] == "orchestrator.recovery_decision" for args, _ in events)
