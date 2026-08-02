"""Phase 1A fail-closed safety net: eval-source rating-eligibility precheck.

Root cause being guarded: a staging-tier published master without a full signed
certificate is ``ROLE_RATING_POOL``-ineligible, so the rating daemon structurally
never schedules matches for it (0 games).  ``wait_for_daemon_eval`` would then
loop forever on an unreachable games floor, emitting only generic 评估超时 warnings.

The precheck in ``prepare_generation`` (PRIMARY lane only; drafts intentionally
use stale ratings and run no matches) raises a distinct, operator-actionable
``EvalSourceRatingIneligible`` instead of silently degrading the floor.  These
tests pin:

  1. an ineligible primary eval source raises ``EvalSourceRatingIneligible`` with
     the right fields and never blocks on the daemon;
  2. the draft lane (``slot_id`` set) skips the precheck entirely;
  3. ``_prepare_or_fail`` propagates the signal instead of swallowing it to None.
"""

import asyncio
from types import SimpleNamespace

import pytest

from bot_namespace import ROLE_RATING_POOL, bot_name
from conftest import STRICT_TARGET_V
from orchestrator_cost_policy import EvalSourceRatingIneligible


def _ineligible_spec(label, *, tier="staging", issues=("signed_full_official_certificate_required",)):
    """A NationalBotSpec-shaped object that fails rating-pool eligibility."""
    return SimpleNamespace(
        eligible=False,
        issues=tuple(issues),
        publication_tier=tier,
        label=label,
        version=None,
    )


def _eligible_spec(label):
    return SimpleNamespace(
        eligible=True,
        issues=(),
        publication_tier="certified",
        label=label,
        version=None,
    )


def _patch_prepare_upstream(monkeypatch, *, active_v, active_bots):
    """Stub the heavy prepare_generation prelude so it reaches the eval-wait branch.

    The goal is to reach the primary-lane ``else:`` at the eval wait with a
    controllable active bot pool, without touching the daemon, the cost ledger,
    the filesystem publish/runtime guards, or epoch authority.
    """
    import epoch_authority
    import evolution_infra
    import generation_scheduler
    import post_publication_handoff
    import tool_runtime_guard
    import workflow_profiles

    # No pending post-publication handoff; not shutting down.
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none"},
    )

    next_v = int(active_v) + 1
    projection = {
        "initialized": True,
        "ignored_checkpoint": None,
        "active_generation": None,
        "published_high_water": int(active_v),
        "abandoned_receipt_floor": 0,
        "allocation_floor": int(active_v),
        "next_v": next_v,
        "next_v_authority": "high_water_plus_one",
        "state": "steady",
        "operator_action": None,
        "reset_receipt_issues": [],
        "active_bots": list(active_bots),
        "current_v": int(active_v),
        "abandoned_receipt_head_digest": "",
    }
    monkeypatch.setattr(
        epoch_authority, "strict_epoch_projection", lambda **_kw: dict(projection)
    )

    monkeypatch.setattr(
        workflow_profiles,
        "get_workflow_profile",
        lambda: SimpleNamespace(
            profile_id="national_native",
            national_execution_mode="native_tcp",
            eval_wait_rd_threshold=110,
            eval_wait_rd_min_games=12,
            eval_wait_min_games=24,
        ),
    )

    # Runtime/publish guards must pass.
    monkeypatch.setattr(
        tool_runtime_guard,
        "ensure_runtime_git_guard",
        lambda *a, **k: (True, {"reason": "ok"}),
    )
    monkeypatch.setattr(
        evolution_infra,
        "ensure_publish_ready_for_new_generation",
        lambda: (True, {"reason": "ok"}),
    )

    # Active pool with more than one bot avoids the singleton/bootstrap branch
    # (len(active_bots) <= 1) and the reap branch (len > MAX_ACTIVE_BOTS).
    monkeypatch.setattr(evolution_infra, "find_latest_active_v", lambda: int(active_v))
    monkeypatch.setattr(evolution_infra, "get_active_bots", lambda: list(active_bots))

    # Noop the cost-scope binding and log-context binding (filesystem/ledger).
    monkeypatch.setattr(
        generation_scheduler,
        "_bind_prepare_generation_cost_scope",
        lambda next_v, ui=None, **_kw: f"generation:{next_v}:workflow-v1",
    )
    monkeypatch.setattr(
        generation_scheduler, "_bind_prepare_log_context", lambda *a, **k: 0
    )
    # Noop the priority-eval signal writer (filesystem).
    monkeypatch.setattr(
        generation_scheduler, "_ensure_priority_eval_signal", lambda *a, **k: None
    )
    # No abandoned-version history.
    monkeypatch.setattr(
        evolution_infra, "abandoned_version_attempt_count", lambda *a, **k: 0
    )
    # Suppress system-event logging noise.
    monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *a, **k: None)


def test_prepare_generation_raises_eval_source_ineligible_for_staging_source(
    monkeypatch,
):
    """Primary lane: a staging (no-full-certificate) eval source fails closed."""
    import bot_namespace
    import evolution_infra
    import generation_scheduler

    active_v = STRICT_TARGET_V + 5
    pool = [bot_name(STRICT_TARGET_V + 4), bot_name(active_v)]

    _patch_prepare_upstream(monkeypatch, active_v=active_v, active_bots=pool)

    resolved = {}

    def fake_resolve(path_or_label, role=ROLE_RATING_POOL, **_kwargs):
        # Record the call and return an ineligible staging spec for the active
        # source label only.
        resolved["label"] = path_or_label
        resolved["role"] = role
        return _ineligible_spec(path_or_label, tier="staging")

    monkeypatch.setattr(bot_namespace, "resolve_national_bot_spec", fake_resolve)

    daemon_calls = []

    async def fake_wait(*args, **kwargs):
        daemon_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(evolution_infra, "wait_for_daemon_eval", fake_wait)

    with pytest.raises(EvalSourceRatingIneligible) as exc_info:
        asyncio.run(
            generation_scheduler.prepare_generation(
                None, ui=None, min_games=24, slot_id=None
            )
        )

    err = exc_info.value
    assert err.bot_name == bot_name(active_v)
    assert err.version == active_v
    assert err.publication_tier == "staging"
    assert err.issues == ("signed_full_official_certificate_required",)

    # The precheck fired on the rating-pool role for the active source label.
    assert resolved.get("role") == ROLE_RATING_POOL
    assert resolved.get("label") == bot_name(active_v)

    # Fail closed BEFORE touching the daemon: no blocking eval wait happened.
    assert daemon_calls == []


def test_draft_prepare_skips_eligibility_precheck(monkeypatch):
    """Draft lane (slot_id set) intentionally uses stale ratings and must not
    hit the rating-pool precheck or block on the daemon."""
    import bot_namespace
    import evolution_infra
    import generation_scheduler

    active_v = STRICT_TARGET_V + 5
    pool = [bot_name(STRICT_TARGET_V + 4), bot_name(active_v)]

    _patch_prepare_upstream(monkeypatch, active_v=active_v, active_bots=pool)

    rating_pool_calls = []

    real_resolve = bot_namespace.resolve_national_bot_spec

    def tracking_resolve(path_or_label, role=ROLE_RATING_POOL, **kwargs):
        if role == ROLE_RATING_POOL:
            rating_pool_calls.append(path_or_label)
            # If the draft lane ever reached the precheck, this ineligible spec
            # would raise EvalSourceRatingIneligible -- proving it is skipped.
            return _ineligible_spec(path_or_label, tier="staging")
        return real_resolve(path_or_label, role=role, **kwargs)

    monkeypatch.setattr(bot_namespace, "resolve_national_bot_spec", tracking_resolve)

    daemon_calls = []

    async def fake_wait(*args, **kwargs):
        daemon_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(evolution_infra, "wait_for_daemon_eval", fake_wait)

    # A draft must NOT raise EvalSourceRatingIneligible even though the rating
    # pool spec would be ineligible.  It may return None / raise something else
    # downstream (speculative path), but never this typed control signal.
    try:
        result = asyncio.run(
            generation_scheduler.prepare_generation(
                None, ui=None, min_games=24, slot_id="draft"
            )
        )
    except EvalSourceRatingIneligible:
        pytest.fail("draft lane must skip the rating-pool eligibility precheck")
    except Exception:
        # Downstream speculative-path failures are out of scope here; the
        # contract under test is only that the precheck is not reached.
        result = None

    # The draft never blocks on the daemon and never consults the rating pool.
    assert daemon_calls == []
    assert rating_pool_calls == []
    # Sanity: the function returned or fell through without the typed signal.
    assert result is None or hasattr(result, "next_v")


def test_prepare_or_fail_propagates_eval_source_ineligible(monkeypatch):
    """_prepare_or_fail must NOT swallow EvalSourceRatingIneligible into None."""
    import orchestrator

    async def raising_prepare(shutdown_mgr, ui=None, min_games=None, *, slot_id=None):
        raise EvalSourceRatingIneligible(
            bot_name="national_cloud_v9",
            version=9,
            issues=("signed_full_official_certificate_required",),
            publication_tier="staging",
        )

    monkeypatch.setattr(
        "generation_scheduler.prepare_generation", raising_prepare
    )

    with pytest.raises(EvalSourceRatingIneligible):
        asyncio.run(orchestrator._prepare_or_fail(None, ui=None, min_games=24))
