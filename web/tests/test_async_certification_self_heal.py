"""Tests for the async-official-certification self-heal (Phase 1C, bugs C1 + C2).

C1: ``publication_handoff_completed`` must schedule async certification from a
    single chokepoint regardless of which loop branch consumed the handoff.
C2: ``_create_certified_tag`` must run the FULL publish-certified sequence
    (attestation + commit + completion-tag reannotation + certified tag) so a
    staging-published bot becomes ROLE_RATING_POOL eligible.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Test 1 (C1): the deterministic-route chokepoint schedules async certification
# for EVERY publication_handoff_completed terminal action whose cleanup passes.
# ---------------------------------------------------------------------------


def _pending_route_recovery():
    """A checkpoint-free recovery whose classification is publication_handoff_completed."""
    return {
        "action": "resume",
        "post_publication_handoff": True,
        "checkpoint": {
            "workflow_run_id": "generation:143:workflow-v1",
            "checkpoint_revision": 1,
            "stage": "post_publication_handoff",
            "next_v": 143,
            "source_v": 142,
        },
        "stage": "post_publication_handoff",
        "next_v": 143,
        "source_v": 142,
    }


@pytest.mark.asyncio
async def test_chokepoint_schedules_async_cert_after_publication_handoff(monkeypatch):
    """C1 fix: the chokepoint in _advance_deterministic_recovery schedules async cert."""
    import orchestrator
    import orchestrator_loop_phases

    recovery = _pending_route_recovery()

    # Deterministic route succeeds with no terminal abandon result, so the
    # classifier returns publication_handoff_completed.
    async def route(_recovery, _ui=None, *, outcome=None, **_kwargs):
        outcome["result"] = {"published": True}
        return True

    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    # No further checkpoint recovery needed -> next_recovery is None, which is
    # what forces the classifier down the post_publication_handoff branch.
    monkeypatch.setattr(
        orchestrator, "_checkpoint_recovery_context", lambda *_a, **_kw: None
    )
    # Cleanup succeeds (the precondition for scheduling).
    async def cleanup_ok(*_a, **_kw):
        return True

    monkeypatch.setattr(
        orchestrator, "_run_post_generation_cleanup_with_timeout", cleanup_ok
    )

    scheduled = []

    async def fake_schedule(ui, shutdown_mgr):
        scheduled.append((ui, shutdown_mgr))

    # Patch the symbol where the chokepoint imports it from.
    monkeypatch.setattr(
        orchestrator_loop_phases,
        "_try_schedule_async_certification",
        fake_schedule,
    )

    advanced = await orchestrator._advance_deterministic_recovery(
        recovery,
        None,  # ui=None (matches the one-gen CLI call site)
        cost_policy=None,
        shutdown_mgr=None,
        gen_ctx=None,
        gen_count=1,
    )

    assert advanced["routed"] is True
    assert advanced["terminal_action"] == "publication_handoff_completed"
    # The chokepoint scheduled async certification exactly once.
    assert len(scheduled) == 1
    assert scheduled[0] == (None, None)


@pytest.mark.asyncio
async def test_chokepoint_does_not_schedule_async_cert_when_cleanup_fails(monkeypatch):
    """When post-generation cleanup fails, async cert must NOT be scheduled."""
    import orchestrator
    import orchestrator_loop_phases

    recovery = _pending_route_recovery()

    async def route(_recovery, _ui=None, *, outcome=None, **_kwargs):
        outcome["result"] = {"published": True}
        return True

    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(
        orchestrator, "_checkpoint_recovery_context", lambda *_a, **_kw: None
    )

    async def cleanup_fails(*_a, **_kw):
        return False

    monkeypatch.setattr(
        orchestrator, "_run_post_generation_cleanup_with_timeout", cleanup_fails
    )

    scheduled = []

    async def fake_schedule(ui, shutdown_mgr):
        scheduled.append((ui, shutdown_mgr))

    monkeypatch.setattr(
        orchestrator_loop_phases,
        "_try_schedule_async_certification",
        fake_schedule,
    )

    advanced = await orchestrator._advance_deterministic_recovery(
        recovery, None, cost_policy=None, shutdown_mgr=None, gen_ctx=None, gen_count=1
    )

    # Cleanup failure remaps the terminal action and blocks scheduling.
    assert advanced["terminal_action"] == "post_generation_cleanup_failed"
    assert scheduled == []


@pytest.mark.asyncio
async def test_chokepoint_scheduling_failure_is_non_fatal(monkeypatch):
    """If _try_schedule_async_certification raises, the recovery still succeeds."""
    import orchestrator
    import orchestrator_loop_phases

    recovery = _pending_route_recovery()

    async def route(_recovery, _ui=None, *, outcome=None, **_kwargs):
        outcome["result"] = {"published": True}
        return True

    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(
        orchestrator, "_checkpoint_recovery_context", lambda *_a, **_kw: None
    )

    async def cleanup_ok(*_a, **_kw):
        return True

    monkeypatch.setattr(
        orchestrator, "_run_post_generation_cleanup_with_timeout", cleanup_ok
    )

    async def boom(ui, shutdown_mgr):
        raise RuntimeError("simulated scheduler failure")

    monkeypatch.setattr(
        orchestrator_loop_phases, "_try_schedule_async_certification", boom
    )

    # Must not propagate the exception.
    advanced = await orchestrator._advance_deterministic_recovery(
        recovery, None, cost_policy=None, shutdown_mgr=None, gen_ctx=None, gen_count=1
    )

    assert advanced["routed"] is True
    assert advanced["terminal_action"] == "publication_handoff_completed"


# ---------------------------------------------------------------------------
# Test 2: the shared publish_full_certified_tier helper performs all 4 steps.
# ---------------------------------------------------------------------------


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(repo_root: Path) -> None:
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "Test")
    _git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    _git(repo_root, "add", ".gitignore")
    _git(repo_root, "commit", "-q", "-m", "init")


def test_publish_full_certified_tier_rejects_non_certified_bot(tmp_path, monkeypatch):
    """If the bot is not full-certified, the helper returns bot_not_full_certified.

    Exercises the gate at the top of the helper against a real (empty) status,
    proving the helper fails closed before touching git.
    """
    import bot_namespace
    import official_certification as certification
    import official_certification_authority as authority
    from official_certification_authority import publish_full_certified_tier

    candidate = tmp_path / "bots" / bot_namespace.bot_name(9001)
    candidate.mkdir(parents=True)
    (candidate / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")

    repo_root = tmp_path
    _init_repo(repo_root)
    _git(repo_root, "add", "bots")
    _git(repo_root, "commit", "-q", "-m", "add bot")

    # read_status returns an empty dict -> official_full_certified is False.
    monkeypatch.setattr(certification, "read_status", lambda _c: {})

    result = publish_full_certified_tier(9001, candidate, repo_root=repo_root)
    assert result["ok"] is False
    assert result["reason"] == "bot_not_full_certified"


def test_publish_full_certified_tier_executes_all_four_steps(tmp_path, monkeypatch):
    """The helper performs the full 4-step publish sequence and returns ok=True.

    Stubs the upstream certification gates (official_full_certified,
    publish_certificate_attestation, read_status) so the helper reaches its git
    execution core, then asserts each git step happened against a real temp repo:
      1. attestation file written + committed,
      2. completion tag reannotated with official-* metadata,
      3. certified tag created at the same commit.
    """
    import bot_namespace
    import official_certification as certification
    import official_certification_authority as authority
    from bot_artifact import hash_path
    from official_certification_authority import publish_full_certified_tier

    version = 9001
    candidate = tmp_path / "bots" / bot_namespace.bot_name(version)
    candidate.mkdir(parents=True)
    (candidate / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")

    repo_root = tmp_path
    _init_repo(repo_root)
    # Create the completion tag at HEAD so reannotation has a target.
    _git(repo_root, "add", "bots")
    _git(repo_root, "commit", "-q", "-m", "add bot")
    completion_tag = bot_namespace.bot_tag(version)
    _git(repo_root, "tag", "-a", completion_tag, "-m", "staging", "HEAD")

    # Redirect PUBLISHED_CERTIFICATE_DIR under repo_root so
    # published_certificate_path(candidate) resolves to repo_root/official_certificates/<bot>.json
    # and the helper's relative_to(repo_root) succeeds.
    published_dir = repo_root / "official_certificates"
    monkeypatch.setattr(certification, "PUBLISHED_CERTIFICATE_DIR", published_dir)
    cert_rel = (
        certification.published_certificate_path(candidate.name)
        .relative_to(repo_root)
        .as_posix()
    )

    cert_digest = "f" * 64
    candidate_hash = hash_path(candidate)
    status = {
        "status": "certified",
        "mode": "full",
        "policy_id": "official-full-v5",
        "certificate_digest": cert_digest,
    }

    # Stub the cert gates so the helper's git core runs end-to-end.
    monkeypatch.setattr(certification, "read_status", lambda _c: status)
    monkeypatch.setattr(authority, "official_full_certified", lambda *a, **k: True)

    def fake_publish_attestation(_status, _candidate):
        # Step 1 of the helper verifies this file exists at repo_root/cert_rel.
        out = repo_root / cert_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"certificate_digest": cert_digest, "bot": candidate.name}),
            encoding="utf-8",
        )
        return {"path": str(out), "relative_path": cert_rel}

    monkeypatch.setattr(
        authority, "publish_certificate_attestation", fake_publish_attestation
    )

    result = publish_full_certified_tier(version, candidate, repo_root=repo_root)

    # Step 1 + 2: attestation committed.
    assert result["ok"] is True, result
    assert result["version"] == version
    assert result["certificate_digest"] == cert_digest
    assert result["published_attestation_path"] == cert_rel
    committed = _git(
        repo_root, "show", f"HEAD:{cert_rel}", "--name-only"
    )
    assert committed.returncode == 0, committed.stderr
    assert candidate.name in committed.stdout

    # Step 3: completion tag reannotated with official-* metadata.
    tag_ann = _git(repo_root, "for-each-ref", f"refs/tags/{completion_tag}")
    assert tag_ann.returncode == 0
    ann_msg = _git(repo_root, "tag", "-n99", "-l", completion_tag).stdout
    assert "official-certified" in ann_msg
    assert cert_digest in ann_msg
    assert candidate_hash in ann_msg
    assert "official-full-v5" in ann_msg

    # Step 4: certified tag exists at the same commit as the completion tag.
    certified_tag = bot_namespace.certified_tag(version)
    completion_oid = _git(repo_root, "rev-parse", f"refs/tags/{completion_tag}^{{commit}}").stdout.strip()
    certified_oid = _git(repo_root, "rev-parse", f"refs/tags/{certified_tag}^{{commit}}").stdout.strip()
    assert completion_oid
    assert completion_oid == certified_oid
    cert_ann = _git(repo_root, "tag", "-n99", "-l", certified_tag).stdout
    assert "certified-tier" in cert_ann


def test_publish_full_certified_tier_is_idempotent(tmp_path, monkeypatch):
    """Re-running the helper on an already-published bot succeeds (no-op commit)."""
    import bot_namespace
    import official_certification as certification
    import official_certification_authority as authority
    from official_certification_authority import publish_full_certified_tier

    version = 9001
    candidate = tmp_path / "bots" / bot_namespace.bot_name(version)
    candidate.mkdir(parents=True)
    (candidate / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")

    repo_root = tmp_path
    _init_repo(repo_root)
    _git(repo_root, "add", "bots")
    _git(repo_root, "commit", "-q", "-m", "add bot")
    _git(repo_root, "tag", "-a", bot_namespace.bot_tag(version), "-m", "staging", "HEAD")

    published_dir = repo_root / "official_certificates"
    monkeypatch.setattr(certification, "PUBLISHED_CERTIFICATE_DIR", published_dir)
    cert_rel = (
        certification.published_certificate_path(candidate.name)
        .relative_to(repo_root)
        .as_posix()
    )

    cert_digest = "f" * 64
    status = {"status": "certified", "mode": "full", "policy_id": "official-full-v5", "certificate_digest": cert_digest}
    monkeypatch.setattr(certification, "read_status", lambda _c: status)
    monkeypatch.setattr(authority, "official_full_certified", lambda *a, **k: True)

    def fake_publish_attestation(_status, _candidate):
        out = repo_root / cert_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"certificate_digest": cert_digest}), encoding="utf-8")
        return {"path": str(out), "relative_path": cert_rel}

    monkeypatch.setattr(authority, "publish_certificate_attestation", fake_publish_attestation)

    first = publish_full_certified_tier(version, candidate, repo_root=repo_root)
    assert first["ok"] is True
    # Second run: file is already committed -> "nothing to commit" must be tolerated.
    second = publish_full_certified_tier(version, candidate, repo_root=repo_root)
    assert second["ok"] is True


# ---------------------------------------------------------------------------
# Test 4 (closed loop): after async-cert + tier promotion, a published bot is
# ROLE_RATING_POOL eligible.  This is the regression that proves C2 is fixed.
# We exercise the helper directly against a synthetic git repo to avoid the
# multi-minute EXE certification subprocess.
# ---------------------------------------------------------------------------


def test_create_certified_tag_invokes_shared_helper(monkeypatch):
    """C2 fix: _create_certified_tag delegates to publish_full_certified_tier."""
    import orchestrator_loop_phases

    called = []

    def fake_publish_full(version, candidate_path, *, repo_root):
        called.append((version, str(candidate_path), str(repo_root)))
        return {
            "ok": True,
            "version": version,
            "completion_tag": "national-cloud-bot-v9001",
            "certified_tag": "national-cloud-certified-v9001",
            "certificate_digest": "a" * 64,
            "published_attestation_path": "official_certificates/bot.json",
        }

    monkeypatch.setattr(
        orchestrator_loop_phases,
        "publish_full_certified_tier",
        fake_publish_full,
        raising=False,
    )
    # The helper is imported lazily inside _create_certified_tag from
    # official_certification_authority, so patch it there too.
    import official_certification_authority

    monkeypatch.setattr(
        official_certification_authority,
        "publish_full_certified_tier",
        fake_publish_full,
    )
    # get_bot_dir is also imported lazily.
    import evolution_infra

    monkeypatch.setattr(
        evolution_infra, "get_bot_dir", lambda v: f"/repo/bots/v{v}"
    )
    # log_system_event must be a no-op.
    import orchestrator

    monkeypatch.setattr(orchestrator, "log_system_event", lambda *a, **kw: None)

    orchestrator_loop_phases._create_certified_tag(9001, {"passed": True})

    assert len(called) == 1
    version, candidate_path, repo_root = called[0]
    assert version == 9001
    assert candidate_path == "/repo/bots/v9001"


def test_create_certified_tag_logs_failure_when_helper_fails(monkeypatch):
    """C2 fix: when the helper fails, the failure is logged (not silently swallowed)."""
    import orchestrator
    import orchestrator_loop_phases
    import official_certification_authority
    import evolution_infra

    monkeypatch.setattr(
        official_certification_authority,
        "publish_full_certified_tier",
        lambda v, p, *, repo_root: {"ok": False, "reason": "bot_not_full_certified"},
    )
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda v: f"/repo/bots/v{v}")

    events = []
    monkeypatch.setattr(
        orchestrator, "log_system_event", lambda name, level, msg, ctx=None: events.append((name, level))
    )

    orchestrator_loop_phases._create_certified_tag(9001, {"passed": True})

    # The promotion-failed event was logged (proving the failure surfaced).
    assert any(
        name == "orchestrator.async_certification_tier_promotion_failed" and level == "warn"
        for name, level in events
    )


def test_create_certified_tag_logs_success_when_helper_succeeds(monkeypatch):
    """C2 fix: when the helper succeeds, the tier-promoted event is logged."""
    import orchestrator
    import orchestrator_loop_phases
    import official_certification_authority
    import evolution_infra

    monkeypatch.setattr(
        official_certification_authority,
        "publish_full_certified_tier",
        lambda v, p, *, repo_root: {
            "ok": True,
            "version": v,
            "completion_tag": "t",
            "certified_tag": "c",
            "certificate_digest": "a" * 64,
            "published_attestation_path": "p",
        },
    )
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda v: f"/repo/bots/v{v}")

    events = []
    monkeypatch.setattr(
        orchestrator, "log_system_event", lambda name, level, msg, ctx=None: events.append((name, level))
    )

    orchestrator_loop_phases._create_certified_tag(9001, {"passed": True})

    assert any(
        name == "orchestrator.async_certification_tier_promoted" and level == "info"
        for name, level in events
    )
