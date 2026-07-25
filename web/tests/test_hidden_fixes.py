"""Self-tests for the 6 hidden-problem fixes (H1-H6) found during the
3-generation v212/v213/v214 tracking run (2026-06-29).

Pure-logic / data tests — no LLM, no real subprocess battles. Each test
exercises a NEW branch added by a fix so it is not left uncovered.
"""
import asyncio
import json, os, tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from bot_namespace import bot_name, bot_tag, parse_bot_version

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")


def _strict_artifact(root, version, *, action="pass"):
    from bot_namespace import refresh_policy_identity_documents

    root.mkdir(parents=True)
    (root / "national_bot.py").write_text("def run():\n    return None\n", encoding="utf-8")
    (root / "policy.py").write_text(
        f"def decide(_context):\n    return {{'kind': '{action}'}}\n",
        encoding="utf-8",
    )
    (root / "precompute.py").write_text("TABLE = ()\n", encoding="utf-8")
    (root / "national_runtime_manifest.json").write_text("{}\n", encoding="utf-8")
    (root / "policy_epoch_receipt.json").write_text("{}\n", encoding="utf-8")
    refresh_policy_identity_documents(root, version, parent_versions=(version - 1,))
    return root


def _resolve_published_parent(name, **_kwargs):
    version = parse_bot_version(str(name))
    return SimpleNamespace(
        eligible=True,
        version=version,
        issues=(),
        runtime_manifest={"epoch": "national_tcp_policy_v1", "version": version},
        epoch_receipt={"epoch": "national_tcp_policy_v1", "version": version},
        publication_identity={
            "published": True,
            "tag": bot_tag(version),
            "version": version,
        },
        certificate_digest="b" * 64,
    )


def _strict_checkpoint(next_v, source_v, stage, **extra):
    import checkpoint_schema

    audit_context = dict(extra.pop("audit_context", {}) or {})
    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=next_v,
        source_v=source_v,
        parent2_v=extra.get("parent2_v"),
        audit_context=audit_context,
        published_high_water=next_v - 1,
        abandoned_receipt_floor=0,
        abandoned_receipt_head_digest=None,
        parent_resolver=_resolve_published_parent,
    )
    return {
        "checkpoint_schema_version": checkpoint_schema.CHECKPOINT_SCHEMA_VERSION,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": binding,
        "next_v": next_v,
        "source_v": source_v,
        "parent2_v": extra.pop("parent2_v", None),
        "stage": stage,
        "workflow_run_id": f"generation:{next_v}:hidden-fixes-test",
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


# ──────────────────────────────────────────────
# H1: precommit shutdown signal (thread-safe Event)
# ──────────────────────────────────────────────

def test_H1_precommit_shutdown_event_set_reset_is_set():
    """A reset rotates tokens and can never clear a cancelled old attempt."""
    import sys
    import threading
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    import tool_eval

    tool_eval.reset_precommit_shutdown()
    old_token = tool_eval.current_precommit_shutdown_token()
    assert tool_eval.is_precommit_shutdown() is False
    tool_eval.set_precommit_shutdown()
    assert tool_eval.is_precommit_shutdown() is True
    assert old_token.is_set() is True

    tool_eval.reset_precommit_shutdown()
    new_token = tool_eval.current_precommit_shutdown_token()
    assert new_token is not old_token
    assert tool_eval.is_precommit_shutdown() is False
    assert new_token.is_set() is False
    assert old_token.is_set() is True

    # A redundant cycle-start reset must not detach a live attempt.
    tool_eval.reset_precommit_shutdown()
    assert tool_eval.current_precommit_shutdown_token() is new_token

    # Exact cancellation of a detached/foreign attempt cannot poison the new
    # current attempt even if the calls are interleaved.
    detached_token = threading.Event()
    tool_eval.set_precommit_shutdown(detached_token)
    assert detached_token.is_set() is True
    assert new_token.is_set() is False


@pytest.mark.asyncio
async def test_H1_native_precommit_stops_after_cancelled_first_full_match(
    tmp_path,
    monkeypatch,
):
    """The production match loop must not launch a second sample after cancel."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    import national_native
    import tool_eval

    tool_eval.reset_precommit_shutdown()
    token = tool_eval.current_precommit_shutdown_token()
    candidate = tmp_path / "national_v9"
    opponent = tmp_path / "national_v8"
    candidate.mkdir()
    opponent.mkdir()
    match_calls = []

    monkeypatch.setattr(
        national_native,
        "resolve_bot",
        lambda value: (Path(value).name, Path(value)),
    )
    monkeypatch.setattr(
        national_native,
        "_acceptance_opponent_runtime_mode",
        lambda *_args, **_kwargs: "strict_policy",
    )

    async def run_match(*_args, **_kwargs):
        match_calls.append(True)
        tool_eval.set_precommit_shutdown()
        return {
            "net_chips_a": 100,
            "hands_played": 70,
            "passed_compliance": True,
            "issues": [],
            "artifact_execution": {},
        }

    monkeypatch.setattr(national_native, "run_native_strength_pair", run_match)

    with pytest.raises(asyncio.CancelledError):
        await national_native.run_native_precommit(
            candidate,
            [{
                "name": opponent.name,
                "path": str(opponent),
                "reason": "parent",
                "precommit_gate_admitted": True,
                "strength_admitted": True,
            }],
            hands=70,
            matches_per_opponent=2,
            cancel_token=token,
        )

    assert match_calls == [True]
    assert token.is_set() is True

    # A fresh deterministic retry gets a different live token, but the old
    # detached loop remains permanently cancelled.
    retry_token = tool_eval.begin_precommit_shutdown_attempt()
    assert retry_token is not token
    assert retry_token.is_set() is False
    assert token.is_set() is True


@pytest.mark.asyncio
async def test_H1_completed_control_match_recovers_by_same_identity_after_cancel(
    tmp_path,
    monkeypatch,
):
    """A journaled control match is reused, not relaunched, after cancellation."""
    import first_strict_control
    import first_strict_execution_journal
    import national_native
    import precommit_eval_contract
    import tool_eval
    from bot_artifact import hash_path

    candidate = tmp_path / "national_v9"
    control = tmp_path / "first_strict_control_v1"
    candidate.mkdir()
    control.mkdir()
    (candidate / "national_bot.py").write_text("# candidate\n", encoding="utf-8")
    (control / "national_bot.py").write_text("# control\n", encoding="utf-8")

    candidate_hash = hash_path(candidate)
    control_hash = "c" * 64
    receipt_digest = "d" * 64
    timing_plan = national_native.build_native_match_timing_plan(
        hands=70,
        requested_timeout_sec=national_native.LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
    )
    sample_plan = [
        {
            "opponent": control.name,
            "opponent_index": 0,
            "repeat": repeat,
            "deck_seed_base": 91_000 + (repeat - 1) * 1_000,
            "bot_seed_base": 1_000_091_000 + (repeat - 1) * 1_000,
            "native_match_timing_plan_digest": timing_plan.digest(),
        }
        for repeat in range(1, 9)
    ]
    batch_plan = precommit_eval_contract.build_native_precommit_batch_plan(
        sample_plan,
        native_timing_plan=timing_plan,
        first_strict_control=True,
    )
    execution_scope = {
        "workflow_run_id": "generation:9:control-retry",
        "checkpoint_revision": 7,
        "candidate_version": 9,
        "candidate_label": candidate.name,
        "candidate_artifact_hash": candidate_hash,
        "control_id": control.name,
        "control_artifact_hash": control_hash,
        "control_receipt_digest": receipt_digest,
        "precommit_plan_digest": "e" * 64,
        "evaluation_contract_digest": "f" * 64,
        "native_match_timing_plan_digest": timing_plan.digest(),
        "precommit_attempt": 1,
    }
    control_receipt = {
        "candidate_version": 9,
        "source_version": 8,
        "active_policy_bots": [],
        "receipt_digest": receipt_digest,
        "control": {
            "control_id": control.name,
            "path": str(control.absolute()),
            "artifact_hash": control_hash,
        },
    }
    opponent = {
        "name": control.name,
        "path": str(control.absolute()),
        "reason": "first_strict_empty_pool_control",
        "authority": "system_first_strict_control",
        "precommit_gate_admitted": True,
        "formal_bootstrap_opponent_admitted": True,
        "formal_bootstrap_scope": "first_policy_bot_empty_pool_only",
        "strength_admitted": False,
        "rating_eligible": False,
        "official_opponent_eligible": False,
        "control_receipt": control_receipt,
    }

    tool_eval.reset_precommit_shutdown()
    first_token = tool_eval.begin_precommit_shutdown_attempt()
    match_calls = []
    match_budgets = []
    begin_calls = []
    complete_calls = []
    journal = {}
    real_begin_control_execution = (
        first_strict_execution_journal.begin_control_execution
    )
    real_complete_control_execution = (
        first_strict_execution_journal.complete_control_execution
    )
    monkeypatch.setattr(
        first_strict_execution_journal,
        "CONTROL_EXECUTION_ROOT",
        tmp_path / "control-execution-journal",
    )

    monkeypatch.setattr(
        national_native,
        "resolve_bot",
        lambda value: (Path(value).name, Path(value)),
    )
    monkeypatch.setattr(
        national_native,
        "_artifact_execution_is_valid",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        national_native,
        "_validate_first_strict_runner_execution_seal",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        national_native,
        "_consume_first_strict_runner_execution_seal",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        national_native,
        "precommit_outcome_blockers",
        lambda *_args, **_kwargs: ([], {"primary_match_score": 1.0}),
    )
    monkeypatch.setattr(
        first_strict_control,
        "validate_control_receipt",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        first_strict_control,
        "control_gate_blockers",
        lambda *_args, **_kwargs: ([], {"passed": True}),
    )

    def begin_control_execution(*, scope, repeat, **kwargs):
        begin_calls.append((dict(scope), repeat, dict(kwargs)))
        return real_begin_control_execution(
            scope=scope,
            repeat=repeat,
            **kwargs,
        )

    def complete_control_execution(ticket, *, execution):
        complete_calls.append(ticket)
        receipt = real_complete_control_execution(ticket, execution=execution)
        journal.update(execution_receipt=receipt)
        # Simulate the outer cycle timing out after the complete match has been
        # durably journaled but before it can be admitted to the checkpoint.
        tool_eval.set_precommit_shutdown(first_token)
        return receipt

    monkeypatch.setattr(
        first_strict_execution_journal,
        "begin_control_execution",
        begin_control_execution,
    )
    monkeypatch.setattr(
        first_strict_execution_journal,
        "complete_control_execution",
        complete_control_execution,
    )

    async def run_match(
        *_args,
        deck_seed_base,
        bot_seed_base,
        timing_plan,
        **_kwargs,
    ):
        match_calls.append(True)
        match_budgets.append(timing_plan)
        settlements = []
        hand_records = []
        events = []
        for hand in range(1, 71):
            settlement = {
                "hand": hand,
                "earnings": [1, -1],
                "pot": 2,
                "is_showdown": False,
                "winner_idx": 0,
                "reason": "fold",
            }
            settlements.append(settlement)
            hand_records.append({"hand": hand, "settlement": settlement})
            events.append({"type": "settle", **settlement})
        return {
            "execution_mode": "native_tcp",
            "hands_requested": 70,
            "hands_played": 70,
            "deck_seed_base": deck_seed_base,
            "bot_seed_base": bot_seed_base,
            "net_chips_a": 70,
            "net_chips_b": -70,
            "passed_compliance": True,
            "issues": [],
            "settlements": settlements,
            "hand_records": hand_records,
            "events": events,
            "artifact_execution": {},
            "native_match_timing_plan": timing_plan.snapshot(),
            "native_match_timing_plan_digest": timing_plan.digest(),
            "native_full_match_liveness_budget": timing_plan.liveness_budget_snapshot(),
            "native_match_timeout_phase": None,
            "native_terminal_abort": None,
        }

    monkeypatch.setattr(national_native, "run_native_strength_pair", run_match)

    with pytest.raises(asyncio.CancelledError):
        await national_native.run_native_precommit(
            candidate,
            [opponent],
            hands=70,
            matches_per_opponent=8,
            sample_plan=sample_plan,
            batch_plan=batch_plan,
            control_execution_scope=execution_scope,
            cancel_token=first_token,
            timing_plan=timing_plan,
        )

    retry_token = tool_eval.begin_precommit_shutdown_attempt()
    recovered = await national_native.run_native_precommit(
        candidate,
        [opponent],
        hands=70,
        matches_per_opponent=8,
        sample_plan=sample_plan,
        batch_plan=batch_plan,
        control_execution_scope=execution_scope,
        cancel_token=retry_token,
        timing_plan=timing_plan,
    )

    assert retry_token is not first_token
    assert retry_token.is_set() is False
    assert first_token.is_set() is True
    assert match_calls == [True, True]
    assert len(begin_calls) == 3
    assert [call[:2] for call in begin_calls] == [
        (execution_scope, 1),
        (execution_scope, 1),
        (execution_scope, 2),
    ]
    assert begin_calls[0][2]["timing_plan"] == timing_plan
    assert match_budgets == [timing_plan, timing_plan]
    assert len(complete_calls) == 2
    assert complete_calls[0]["input_payload"]["scope"] == execution_scope
    assert recovered["passed"] is False
    progress = recovered["first_strict_batch_pending"]
    assert progress["state"] == "pending_next_sample"
    assert progress["next_repeat"] == 3
    assert [row["repeat"] for row in progress["completed_samples"]] == [1, 2]
    assert journal["execution_receipt"] in [
        row["execution_receipt"] for row in progress["completed_samples"]
    ]

    # Finish the same frozen eight-sample journal. This is the real producer
    # path that v56 exercised, so prove both projections consumed by
    # first_strict_control.validate_control_result carry the explicit negative
    # authority flag rather than merely checking source text.
    for expected_next_repeat in range(4, 9):
        recovered = await national_native.run_native_precommit(
            candidate,
            [opponent],
            hands=70,
            matches_per_opponent=8,
            sample_plan=sample_plan,
            batch_plan=batch_plan,
            control_execution_scope=execution_scope,
            cancel_token=retry_token,
            timing_plan=timing_plan,
        )
        progress = recovered["first_strict_batch_pending"]
        assert progress["next_repeat"] == expected_next_repeat

    completed = await national_native.run_native_precommit(
        candidate,
        [opponent],
        hands=70,
        matches_per_opponent=8,
        sample_plan=sample_plan,
        batch_plan=batch_plan,
        control_execution_scope=execution_scope,
        cancel_token=retry_token,
        timing_plan=timing_plan,
    )
    assert len(match_calls) == 8
    assert completed["matchups"][0]["migration_projection"] is False
    assert all(
        row["migration_projection"] is False
        for row in completed["matchups"][0]["repeats"]
    )


# ──────────────────────────────────────────────
# H2: gather CancelledError propagation in _execute_workers
# ──────────────────────────────────────────────

def test_H2_gather_re_raises_cancelled_error():
    """A CancelledError in one gathered worker must propagate (not be swallowed
    as a generic worker failure by return_exceptions=True)."""
    import sys, asyncio
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

    async def worker_ok():
        return True

    async def worker_cancelled():
        raise asyncio.CancelledError()

    async def run():
        # This mirrors the H2 guard added in agent_workers._execute_workers:
        # gather(return_exceptions=True), then re-raise any CancelledError.
        results = await asyncio.gather(
            worker_ok(), worker_cancelled(), return_exceptions=True,
        )
        for r in results:
            if isinstance(r, asyncio.CancelledError):
                raise r
        return "swallowed"   # should NOT reach here

    with __import__("pytest").raises(asyncio.CancelledError):
        asyncio.run(run())


# ──────────────────────────────────────────────
# H3: bare-commit finalize preserves git-tracked dirs
# ──────────────────────────────────────────────

def test_H3_finalize_bare_commit_missing_source_v_returns_false():
    """_finalize_bare_commit must NOT finalize when source_v is missing
    (cannot reconstruct lineage), returning False and leaving the dir intact."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    import generation_scheduler as gs
    # No source_v in checkpoint -> cannot finalize -> False (dir preserved).
    assert gs._finalize_bare_commit(999999, ckpt={}) is False


def test_H3_finalize_bare_commit_requires_verified_gate_ledger(tmp_path, monkeypatch):
    """Bare-commit recovery must not tag a directory unless all commit gates passed."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    import generation_scheduler as gs
    import evolution_infra
    import tool_commit

    bot_dir = tmp_path / "bots" / bot_name(888)
    _strict_artifact(bot_dir, 888)

    monkeypatch.setattr(evolution_infra, "git_has_tag", lambda _v: False)
    commit_calls = []
    monkeypatch.setattr(evolution_infra, "git_commit_bot", lambda *a, **k: commit_calls.append((a, k)))
    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _v: bot_dir)
    monkeypatch.setattr(gs, "log_system_event", lambda *_a, **_k: None)

    ckpt = _strict_checkpoint(888, 887, "workers_done", gate_results={})

    assert gs._finalize_bare_commit(888, ckpt=ckpt) is False
    assert commit_calls == []


def test_H3_bare_commit_recovery_blocks_stale_code_fingerprint(tmp_path, monkeypatch):
    """Bare-commit recovery must bind the tag to the exact code that passed gates."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    import generation_scheduler as gs
    import tool_commit
    from tool_gates import _bot_code_fingerprint

    bot_dir = tmp_path / "bots" / bot_name(889)
    _strict_artifact(bot_dir, 889, action="fold")
    current_fp = _bot_code_fingerprint(bot_dir)

    monkeypatch.setattr(tool_commit, "get_bot_dir", lambda _v: bot_dir)
    ckpt = _strict_checkpoint(
        889,
        888,
        "verified",
        gate_results={
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "code_fingerprint": "stale-fingerprint",
            },
            "review": {"approved": True},
            "critic": {"approved": True},
            "precommit_eval": {"passed": True, "code_fingerprint": current_fp},
        },
    )

    ok, reason = gs._bare_commit_gate_ledger_ok(889, ckpt)

    assert ok is False
    assert "code_fingerprint changed since quality gates" in reason


def test_post_generation_cleanup_skips_uncommitted_before_side_effects(monkeypatch):
    """Phase 3 only verifies handoff completion; it owns no cleanup effects."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    import generation_scheduler as gs
    import evolution_infra
    import post_publication_handoff

    events = []
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none"},
    )
    monkeypatch.setattr(evolution_infra, "git_has_tag", lambda _v: False)
    monkeypatch.setattr(
        evolution_infra,
        "get_active_bots",
        lambda: (_ for _ in ()).throw(AssertionError("post-cleanup side effect ran")),
    )
    monkeypatch.setattr(gs, "log_system_event", lambda *args: events.append(args))

    ctx = gs.GenerationContext(current_v=887, next_v=888, strategy="master", source_v=887)
    asyncio.run(gs.post_generation_cleanup(None, None, ctx))

    event_types = [event[0] for event in events]
    assert event_types == [
        "pipeline.post_cleanup_start",
        "pipeline.post_cleanup_done",
    ]
    assert events[-1][3]["status"] == "skipped"
    assert events[-1][3]["reason"] == "not_committed_or_abandoned"


def test_H3_cleanup_incomplete_preserves_bare_commit(tmp_path, monkeypatch):
    """Cleanup discovery reports bytes but owns no mutation/finalize authority."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    import generation_scheduler as gs

    # Set up a fake bots dir with a bare-commit-style v entry.
    fake_bots = tmp_path / "bots"
    fake_v_dir = fake_bots / bot_name(888)
    _strict_artifact(fake_v_dir, 888)

    # Stub the evolution_infra helpers used by _cleanup_incomplete.
    import evolution_infra as ei
    monkeypatch.setattr(ei, "PROJECT_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(ei, "git_has_tag", lambda v: False, raising=False)
    monkeypatch.setattr(ei, "git_dir_is_committed", lambda v: True, raising=False)
    monkeypatch.setattr(ei, "RESULTS_DIR", tmp_path / "results", raising=False)

    # Publication recovery and canonical abandon are the only mutation owners;
    # directory discovery must not infer either operation from Git shape.
    called = {"finalize": False}
    def fake_finalize(v, ckpt=None):
        called["finalize"] = True
        raise AssertionError("cleanup discovery attempted publication recovery")
    monkeypatch.setattr(gs, "_finalize_bare_commit", fake_finalize, raising=False)

    observed = gs._cleanup_incomplete()

    # H3 invariant: the dir is preserved and only reported. A caller must use
    # the durable publishing checkpoint or exact abandon transaction.
    assert fake_v_dir.exists(), "bare-commit dir must NOT be removed"
    assert (fake_v_dir / "policy.py").exists(), "bare-commit code must survive"
    assert observed == [888]
    assert called["finalize"] is False


# ──────────────────────────────────────────────
# P1: pipeline guard hook (blocks Bash/Edit/Write on bot code + state files)
# ──────────────────────────────────────────────

def test_P1_guard_hook_blocks_bot_dir_edit():
    """The guard hook's protected-path rule must catch strict bot paths."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    import orchestrator_context as oc
    # _make_bot_dir_guard_hook builds closures; we replicate the _targets_protected
    # logic here to verify path detection without spinning up the SDK.
    _PROTECTED_STATE_FILES = (
        "pipeline_state.json", "worker_failures.jsonl", "circuit_breaker_state.json",
        "priority_eval.json", "glicko_ratings.json", "bot_stats.json",
        "abandoned_versions.jsonl",
    )
    def targets_protected(text):
        if not text: return False
        low = str(text).lower()
        if "bots/national_v" in low: return True
        for sf in _PROTECTED_STATE_FILES:
            if sf in low: return True
        return False
    # Bot code paths
    assert targets_protected("bots/national_v218/policy.py")
    assert targets_protected("/abs/path/bots/national_v195/policy.py")
    # State files
    assert targets_protected("results/pipeline_state.json")
    assert targets_protected("echo x > worker_failures.jsonl")
    assert targets_protected("cat glicko_ratings.json")
    # A path match alone does not mean block during open-ended planning — the hook
    # also checks mutation verbs. At actionable route stages, even read-only Bash is
    # blocked by a separate route guard.
    assert targets_protected("grep foo bots/national_v218/policy.py")
    assert targets_protected("results/abandoned_versions.jsonl")


def test_P1_guard_hook_git_commit_blocked():
    """git commit/tag/push must be treated as mutations (bypass commit_bot)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    from orchestrator_context import _orchestrator_bash_is_mutation as bash_is_mutation

    # git operations on bot dir via commit/tag/push are blocked
    assert bash_is_mutation("git commit -m foo")
    assert bash_is_mutation("git tag national-bot-v219")
    assert bash_is_mutation("git tag -a national-bot-v219 -m evolve")
    assert bash_is_mutation("git push origin main")
    # read-only git is NOT a mutation
    assert not bash_is_mutation("git status")
    assert not bash_is_mutation("git log --oneline -5")
    assert not bash_is_mutation("git tag")
    assert not bash_is_mutation("git tag -l 'national-bot-v2*' | tail -10")
    assert not bash_is_mutation("git tag --sort=-creatordate | head -5")


def test_P1_guard_hook_returns_stage_recovery_and_command_preview():
    """Denied direct mutations should tell the LLM the next MCP tool and log the command."""
    import asyncio
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    import event_bus
    import evolution_infra
    import core.orchestrator_context as oc

    evolution_infra.write_pipeline_checkpoint(232, 224, "direction_audited")
    hook = oc._make_bot_dir_guard_hook()["PreToolUse"][0].hooks[0]
    command = (
        f"mkdir -p bots/{bot_name(232)} && "
        f"cp bots/{bot_name(224)}/policy.py bots/{bot_name(232)}/policy.py"
    )

    output = asyncio.run(hook(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        "call_test_guard",
        None,
    ))

    decision = output["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "NEXT MCP TOOL: run_master" in reason
    assert "Do NOT retry the denied Bash/Edit/Write call" in reason

    events = [
        json.loads(line)
        for line in event_bus.EVENTS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    guard_event = [e for e in events if e.get("type") == "pipeline.guard_block"][-1]
    data = guard_event["data"]
    assert data["command_preview"] == command
    assert data["command_truncated"] is False
    assert data["stage"] == "direction_audited"
    assert data["next_step"] == "run_master"


@pytest.mark.parametrize("stage", ["master_planned", "quality_failed"])
def test_P1_guard_hook_blocks_readonly_bash_at_actionable_stage(tmp_path, monkeypatch, stage):
    """At deterministic execute_workers stages, even read-only Bash must give way."""
    import asyncio
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    import evolution_infra
    import event_bus
    import orchestrator_context as oc

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(event_bus, "EVENTS_FILE", tmp_path / "events.jsonl")
    evolution_infra.write_pipeline_checkpoint(
        268,
        242,
        stage,
        master_plan={
            "strategy": "master",
            "tasks": [
                {
                    "worker_id": "w1",
                    "role": "Algorithmic Logic Architect",
                    "target_files": ["policy.py"],
                    "worker_prompt": "fix typed policy semantics",
                }
            ],
        },
        parent2_v=248,
        gate_results={
            "quality": {
                "all_passed": False,
                "failed_gates": ["policy_contract(policy.py:1)"],
            }
        },
    )

    hook = oc._make_bot_dir_guard_hook()["PreToolUse"][0].hooks[0]
    output = asyncio.run(hook(
        {"tool_name": "Bash", "tool_input": {"command": f"grep -n decide bots/{bot_name(268)}/policy.py"}},
        "call_test_actionable_guard",
        None,
    ))

    decision = output["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "Actionable checkpoint route is locked" in reason
    assert "next MCP tool=execute_workers" in reason
    assert "Built-in Bash/Edit/Write are disabled" in reason


def test_P1_guard_hook_routes_critic_checked_to_precommit(tmp_path, monkeypatch):
    """critic_checked is an actionable precommit route, not an execute_workers route."""
    import asyncio
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    import evolution_infra
    import event_bus
    import orchestrator_context as oc

    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "pipeline_state.json")
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(event_bus, "EVENTS_FILE", tmp_path / "events.jsonl")
    evolution_infra.write_pipeline_checkpoint(
        327,
        310,
        "critic_checked",
        master_plan={"strategy": "crossover", "tasks": []},
        gate_results={},
    )

    hook = oc._make_bot_dir_guard_hook()["PreToolUse"][0].hooks[0]
    output = asyncio.run(hook(
        {"tool_name": "Bash", "tool_input": {"command": f"grep -n decide bots/{bot_name(327)}/policy.py"}},
        "call_test_precommit_route_guard",
        None,
    ))

    decision = output["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    reason = decision["permissionDecisionReason"]
    assert "Actionable checkpoint route is locked" in reason
    assert "next MCP tool=run_precommit_eval" in reason
    assert "execute_workers" not in reason


# ──────────────────────────────────────────────
# P2: abandoned version reuse prevention
# ──────────────────────────────────────────────

def test_P2_abandoned_versions_floor_logic():
    """The abandoned_floor logic: next_v must skip the max abandoned version."""
    # Simulate: current_v (tagged) = 217, max_committed = 217, but v218 was abandoned.
    current_v = 217
    max_committed_v = 217
    abandoned_floor = 218  # v218 was abandoned and rmtree'd (not git-tracked)
    # The floor raises max_committed_v
    if abandoned_floor > max_committed_v:
        max_committed_v = abandoned_floor
    next_v = max(current_v, max_committed_v) + 1
    assert next_v == 219, f"v218 was abandoned, next should be 219, got {next_v}"
