"""Phase 5b: one-ahead draft pipeline activation tests.

Verifies the wiring that lets generation N+1's LLM work (direction_audit,
Master, Workers) run concurrently with generation N's consumer gate chain,
filling LLM permit idle time:

  * ``active_slot_override`` ContextVar scoping (asyncio task isolation).
  * ``prepare_generation`` draft shadow identity: provisional next_v label
    (primary_next_v + 1) with ``is_draft=True``, no live floor+2 allocation
    claim, isolated ``draft_candidates/`` worktree, and slot-routed writes.
  * Primary prepare early-return only when ``slot_id is None`` (draft must not
    be short-circuited by an in-flight primary checkpoint).
  * ``_run_draft_cycle`` drives the draft through prepared -> direction_audited
    -> master_planned -> workers_done and stops without triggering the
    Slice 2b seal-at-workers-done seam.
  * Draft -> primary promotion remaps provisional next_v onto the formal live
    successor (``_maybe_promote_draft_to_primary`` and the barrier helper).
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")

# Import orchestrator first so the companion modules (which import
# ``orchestrator as _o``) are fully initialized before we reference them.
import orchestrator  # noqa: F401
import evolution_infra
from evolution_infra import (
    active_slot_override,
    no_slot_override,
    current_slot_override,
    pipeline_state_path,
    read_pipeline_checkpoint,
    write_pipeline_checkpoint,
    clear_pipeline_checkpoint,
)


# ---------------------------------------------------------------------------
# active_slot_override ContextVar scoping
# ---------------------------------------------------------------------------


def test_override_default_is_none():
    assert current_slot_override() is None
    assert pipeline_state_path(None) is evolution_infra.PIPELINE_STATE_FILE


def test_override_redirects_none_slot_to_draft():
    with active_slot_override("draft"):
        assert current_slot_override() == "draft"
        assert (
            pipeline_state_path(None)
            == evolution_infra.RESULTS_DIR / "pipeline_state_draft.json"
        )
    # Restored on exit.
    assert current_slot_override() is None
    assert pipeline_state_path(None) is evolution_infra.PIPELINE_STATE_FILE


def test_override_does_not_affect_explicit_slot_id():
    with active_slot_override("draft"):
        # An explicit non-None slot_id always wins.
        assert (
            pipeline_state_path("canary")
            == evolution_infra.RESULTS_DIR / "pipeline_state_canary.json"
        )


def test_no_slot_override_bypasses_ambient_override():
    with active_slot_override("draft"):
        with no_slot_override():
            assert current_slot_override() is None
            assert pipeline_state_path(None) is evolution_infra.PIPELINE_STATE_FILE
        # Re-arm on exit.
        assert current_slot_override() == "draft"


def test_override_checkpoint_io_targets_draft_slot():
    """write/read/clear inside an override all hit the draft file."""
    # Seed the primary.
    assert write_pipeline_checkpoint(next_v=10, source_v=9, stage="testing")
    with active_slot_override("draft"):
        # Inside the override, a no-slot write goes to the draft file.
        assert write_pipeline_checkpoint(
            next_v=20, source_v=5, stage="evaluation"
        )
        ckpt = read_pipeline_checkpoint()
        assert ckpt is not None
        assert ckpt["next_v"] == 20
        assert ckpt["stage"] == "evaluation"
    # Primary untouched.
    primary = read_pipeline_checkpoint()
    assert primary["next_v"] == 10
    # Draft file exists independently.
    draft = read_pipeline_checkpoint(slot_id="draft")
    assert draft["next_v"] == 20


def test_override_is_asyncio_task_isolated():
    """The override set in a parent task does not leak into a sibling task."""

    async def draft_task():
        with active_slot_override("draft"):
            assert pipeline_state_path(None).name == "pipeline_state_draft.json"
            await asyncio.sleep(0.01)
            # Still draft after await.
            assert pipeline_state_path(None).name == "pipeline_state_draft.json"

    async def primary_task():
        await asyncio.sleep(0.005)
        # Sibling primary task is unaffected by the draft's override.
        assert pipeline_state_path(None) is evolution_infra.PIPELINE_STATE_FILE

    async def main():
        await asyncio.gather(primary_task(), draft_task())
        # Parent is unaffected after children finish.
        assert pipeline_state_path(None) is evolution_infra.PIPELINE_STATE_FILE

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Draft -> primary promotion helpers
# ---------------------------------------------------------------------------


def test_maybe_promote_draft_noop_without_draft():
    from generation_scheduler import _maybe_promote_draft_to_primary

    # No draft checkpoint present -> no promotion, no error.
    assert _maybe_promote_draft_to_primary() is False


def test_maybe_promote_draft_noop_when_draft_not_at_workers_done():
    from generation_scheduler import _maybe_promote_draft_to_primary

    # Draft at an early stage is not promotable.
    assert write_pipeline_checkpoint(
        next_v=11, source_v=10, stage="selected", slot_id="draft"
    )
    assert _maybe_promote_draft_to_primary() is False
    # Draft survives (not cleared) so it can keep pre-computing.
    assert read_pipeline_checkpoint(slot_id="draft") is not None


def test_draft_slot_writes_shadow_identity():
    """Draft-slot checkpoints mark is_draft and do not require floor+1 CAS."""
    # Primary at v10 (synthetic authority treats expected_next_v-1 as published).
    assert write_pipeline_checkpoint(next_v=10, source_v=9, stage="testing")
    # Draft provisional v12 would be floor+2 under a real floor=10; shadow
    # identity must still accept the write.
    assert write_pipeline_checkpoint(
        next_v=12, source_v=10, stage="selected", slot_id="draft"
    )
    draft = read_pipeline_checkpoint(slot_id="draft")
    assert draft is not None
    assert draft["next_v"] == 12
    assert draft.get("is_draft") is True
    # Primary untouched.
    assert read_pipeline_checkpoint()["next_v"] == 10


def test_draft_get_bot_dir_isolates_unpublished_candidate(tmp_path, monkeypatch):
    """Under draft override, unpublished candidates resolve under draft_candidates/."""
    import evolution_infra as ei

    monkeypatch.setattr(ei, "BOTS_DIR", tmp_path / "bots")
    monkeypatch.setattr(ei, "RESULTS_DIR", tmp_path / "results")
    (tmp_path / "bots").mkdir()
    (tmp_path / "results").mkdir()
    # Published parent exists under bots/.
    parent = tmp_path / "bots" / "national_cloud_v10"
    parent.mkdir()
    with active_slot_override("draft"):
        assert ei.get_bot_dir(10) == parent
        # Unpublished candidate goes to isolated draft tree.
        draft_cand = ei.get_bot_dir(12)
        assert draft_cand == tmp_path / "results" / "draft_candidates" / "national_cloud_v12"
        assert "bots" not in str(draft_cand)


def test_is_draft_slot_prefix_matches_legacy_and_numbered():
    """is_draft_slot accepts the legacy 'draft' and numbered draft1/draft2/...
    but rejects the primary slot (None) and unrelated slot ids."""
    import evolution_infra as ei

    assert ei.is_draft_slot("draft") is True
    assert ei.is_draft_slot("draft1") is True
    assert ei.is_draft_slot("draft2") is True
    assert ei.is_draft_slot(None) is False
    assert ei.is_draft_slot("") is False
    assert ei.is_draft_slot("primary") is False


def test_draft_slot_id_numbering():
    """draft_slot_id(n) returns 1-based numbered slot ids."""
    import evolution_infra as ei

    assert ei.draft_slot_id(1) == "draft1"
    assert ei.draft_slot_id(2) == "draft2"
    assert ei.draft_slot_id(10) == "draft10"
    with pytest.raises(ValueError):
        ei.draft_slot_id(0)
    with pytest.raises(ValueError):
        ei.draft_slot_id(-1)


def test_numbered_draft_slot_isolates_per_slot_candidate_tree(tmp_path, monkeypatch):
    """A numbered draft slot (draft2) isolates its candidate under a per-slot
    subdirectory, so two concurrent drafts never overwrite each other."""
    import evolution_infra as ei

    monkeypatch.setattr(ei, "BOTS_DIR", tmp_path / "bots")
    monkeypatch.setattr(ei, "RESULTS_DIR", tmp_path / "results")
    (tmp_path / "bots").mkdir()
    (tmp_path / "results").mkdir()
    # draft1 and draft2 each get their own subtree for the same version.
    with active_slot_override("draft1"):
        d1 = ei.get_bot_dir(20)
    with active_slot_override("draft2"):
        d2 = ei.get_bot_dir(20)
    assert d1 == tmp_path / "results" / "draft_candidates" / "draft1" / "national_cloud_v20"
    assert d2 == tmp_path / "results" / "draft_candidates" / "draft2" / "national_cloud_v20"
    assert d1 != d2  # distinct subtrees -> no collision


def test_maybe_promote_draft_remaps_shadow_next_v(monkeypatch):
    """Promotion remaps a shadow provisional next_v onto the formal successor."""
    from generation_scheduler import _maybe_promote_draft_to_primary
    import epoch_authority

    assert write_pipeline_checkpoint(
        next_v=12,
        source_v=10,
        stage="workers_done",
        slot_id="draft",
    )
    draft = read_pipeline_checkpoint(slot_id="draft")
    assert draft.get("is_draft") is True

    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda **_kwargs: {"next_v": 11},
    )
    # Shadow draft provisional=12 remaps to formal=11.
    assert _maybe_promote_draft_to_primary() is True
    assert read_pipeline_checkpoint(slot_id="draft") is None
    primary = read_pipeline_checkpoint()
    assert primary is not None
    assert primary["next_v"] == 11
    assert primary.get("is_draft") is not True


def test_promote_draft_to_primary_barrier_helper_noop_without_draft():
    from orchestrator_deterministic_route import _promote_draft_to_primary

    # No draft -> False, no exception.
    assert _promote_draft_to_primary(published_next_v=10) is False


def test_promote_draft_to_primary_barrier_helper_remaps_shadow():
    from orchestrator_deterministic_route import _promote_draft_to_primary

    assert write_pipeline_checkpoint(
        next_v=99,
        source_v=10,
        stage="workers_done",
        slot_id="draft",
    )
    # Shadow draft remaps onto published+1 even when provisional differs.
    assert _promote_draft_to_primary(published_next_v=10) is True
    assert read_pipeline_checkpoint(slot_id="draft") is None
    primary = read_pipeline_checkpoint()
    assert primary["next_v"] == 11
    assert primary.get("is_draft") is not True


# ---------------------------------------------------------------------------
# _run_draft_cycle stage driving (mocked deterministic recovery)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_draft_cycle_drives_to_workers_done(monkeypatch):
    """_run_draft_cycle advances the draft slot through the LLM stages and
    stops at workers_done without triggering the seal seam."""
    import orchestrator
    import orchestrator_loop_phases

    # Seed a draft at 'selected' so prepare_generation is short-circuited --
    # we mock it to return a fixed context and avoid all the prepare guards.
    async def fake_prepare(shutdown_mgr, ui=None, min_games=None, *, slot_id=None):
        from generation_scheduler import GenerationContext

        assert slot_id == "draft"
        # Write a selected checkpoint into the draft slot.
        write_pipeline_checkpoint(
            next_v=12, source_v=11, stage="selected", slot_id="draft"
        )
        return GenerationContext(
            current_v=11, next_v=12, strategy="master", source_v=11,
            crossover_parents=(), stagnation_info="", match_analysis="",
            performance_verification="", replay_spotlight="", gen_count=11,
        )

    monkeypatch.setattr(
        "generation_scheduler.prepare_generation", fake_prepare
    )

    # Drive the checkpoint through selected -> prepared -> direction_audited
    # -> master_planned -> workers_done, one stage per recovery call.  The
    # stage is tracked in call_state (not via real CAS writes, which require
    # full strict-epoch parent authority out of scope for this loop-logic
    # test); fake_recovery_context reports the tracked stage so the cycle's
    # workers_done stop condition fires exactly when expected.
    stage_progression = [
        "selected",
        "prepared",
        "direction_audited",
        "master_planned",
        "workers_done",
    ]
    call_state = {"stage_idx": 0, "advances": 0, "recovery_calls": 0}

    def fake_recovery_context(reason, ui=None, *, log_level="warn", label="[Recovery]"):
        call_state["recovery_calls"] += 1
        idx = min(call_state["stage_idx"], len(stage_progression) - 1)
        return {
            "action": "resume",
            "checkpoint": {
                "next_v": 12,
                "source_v": 11,
                "stage": stage_progression[idx],
            },
        }

    async def fake_advance(recovery, ui, *, cost_policy, shutdown_mgr,
                           log_level="info", label="[Pipeline]",
                           gen_ctx=None, gen_count=None):
        call_state["advances"] += 1
        call_state["stage_idx"] += 1
        return {"routed": True, "recovery": recovery, "outcome": {},
                "terminal_action": None}

    monkeypatch.setattr(
        orchestrator, "_checkpoint_recovery_context", fake_recovery_context
    )
    monkeypatch.setattr(
        orchestrator, "_advance_deterministic_recovery", fake_advance
    )
    monkeypatch.setattr(
        orchestrator, "load_operator_generation_cost_policy",
        lambda: None,
    )

    await orchestrator_loop_phases._run_draft_cycle(None, None, 11)

    # The cycle routed exactly four stages (selected->prepared->
    # direction_audited->master_planned) and then observed workers_done on
    # the fifth recovery read, stopping without routing run_quality_gates.
    assert call_state["advances"] == 4
    assert call_state["recovery_calls"] == 5
    assert call_state["stage_idx"] == 4  # advanced to workers_done


@pytest.mark.asyncio
async def test_run_draft_cycle_clears_draft_on_prepare_refusal(monkeypatch):
    """If prepare_generation refuses (returns None), the draft slot is cleared."""
    import orchestrator_loop_phases

    async def refusing_prepare(shutdown_mgr, ui=None, min_games=None, *, slot_id=None):
        return None

    monkeypatch.setattr(
        "generation_scheduler.prepare_generation", refusing_prepare
    )
    # Leave a stale draft to prove it gets cleared.  Write the file directly
    # to avoid CAS authority requirements out of scope for this test.
    import json
    pipeline_state_path("draft").write_text(
        json.dumps({"next_v": 12, "source_v": 11, "stage": "selected"})
    )
    await orchestrator_loop_phases._run_draft_cycle(None, None, 11)
    assert read_pipeline_checkpoint(slot_id="draft") is None


@pytest.mark.asyncio
async def test_run_draft_cycle_clears_draft_on_terminal_action(monkeypatch):
    """A terminal action mid-drive clears the draft slot."""
    import orchestrator
    import orchestrator_loop_phases

    async def fake_prepare(shutdown_mgr, ui=None, min_games=None, *, slot_id=None):
        from generation_scheduler import GenerationContext
        import json
        # Write a real draft file directly so the terminal-action cleanup has
        # something to clear (bypassing the CAS, which needs full epoch
        # authority out of scope for this loop-logic test).
        draft_path = pipeline_state_path("draft")
        draft_path.write_text(json.dumps({"next_v": 12, "source_v": 11, "stage": "x"}))
        return GenerationContext(
            current_v=11, next_v=12, strategy="master", source_v=11,
            crossover_parents=(), stagnation_info="", match_analysis="",
            performance_verification="", replay_spotlight="", gen_count=11,
        )

    def fake_recovery_context(reason, ui=None, *, log_level="warn", label="[Recovery]"):
        # Report a non-terminal stage so the loop routes advance.
        return {
            "action": "resume",
            "checkpoint": {"next_v": 12, "source_v": 11, "stage": "master_planned"},
        }

    async def fake_advance(recovery, ui, **_kwargs):
        return {"routed": True, "recovery": recovery, "outcome": {},
                "terminal_action": "generation_abandoned"}

    monkeypatch.setattr(
        "generation_scheduler.prepare_generation", fake_prepare
    )
    monkeypatch.setattr(
        orchestrator, "_checkpoint_recovery_context", fake_recovery_context
    )
    monkeypatch.setattr(
        orchestrator, "_advance_deterministic_recovery", fake_advance
    )
    monkeypatch.setattr(
        orchestrator, "load_operator_generation_cost_policy", lambda: None
    )

    await orchestrator_loop_phases._run_draft_cycle(None, None, 11)
    # Terminal action -> draft cleared.
    assert read_pipeline_checkpoint(slot_id="draft") is None
