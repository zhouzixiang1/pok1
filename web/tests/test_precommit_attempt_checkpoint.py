"""Tests for the precommit_attempt field on the pipeline checkpoint.

Global interface contract: pipeline_state.json gains a "precommit_attempt"
field that counts how many times run_precommit_eval has been called against
the CURRENT bot code for this generation. It:

  - defaults to 0 on a fresh checkpoint,
  - is settable via the precommit_attempt kwarg,
  - is resettable via reset_precommit_attempt=True,
  - AUTO-RESETS to 0 when the stage regresses from a LATE stage (verified/
    archived) to an EARLY stage (prepared..critic_checked), because worker
    rework produced new bot code and the precommit counter must restart.

The autouse isolate_state fixture in conftest.py redirects
evolution_infra.PIPELINE_STATE_FILE to a tmp directory, so these tests never
touch the real bots/ dir or production checkpoint file.
"""

import pytest

import evolution_infra
from evolution_infra import (
    write_pipeline_checkpoint,
    read_pipeline_checkpoint,
    MAX_PRECOMMIT_RETRIES,
)


def _write_basic(stage="prepared", **kwargs):
    """Helper: write a minimal checkpoint and return the persisted dict."""
    write_pipeline_checkpoint(next_v=100, source_v=99, stage=stage, **kwargs)
    return read_pipeline_checkpoint()


def test_fresh_checkpoint_defaults_precommit_attempt_to_zero():
    """A brand-new checkpoint has precommit_attempt == 0 (no precommit calls yet)."""
    state = _write_basic(stage="prepared")
    assert state is not None, "checkpoint should have been written"
    assert state.get("precommit_attempt") == 0


def test_fresh_checkpoint_persists_log_correlation_fields():
    """Fresh checkpoints carry the same correlation key the event bus emits."""
    state = _write_basic(stage="prepared")
    assert state.get("run_id") == "100#0"
    assert state.get("generation_attempt") == 0
    assert state.get("audit_attempt") == 0
    assert state.get("precommit_attempt") == 0


def test_precommit_attempt_kwarg_persists_value():
    """write_pipeline_checkpoint(precommit_attempt=2) stores 2."""
    # First establish a checkpoint with a known source/next so the merge path
    # applies (existing.next_v == next_v).
    _write_basic(stage="reviewed")
    # Now bump the precommit attempt counter.
    write_pipeline_checkpoint(
        next_v=100, source_v=99, stage="reviewed", precommit_attempt=2
    )
    state = read_pipeline_checkpoint()
    assert state.get("precommit_attempt") == 2


def test_reset_precommit_attempt_resets_to_zero():
    """reset_precommit_attempt=True forces the counter back to 0."""
    _write_basic(stage="reviewed", precommit_attempt=3)
    assert read_pipeline_checkpoint().get("precommit_attempt") == 3

    write_pipeline_checkpoint(
        next_v=100, source_v=99, stage="reviewed", reset_precommit_attempt=True
    )
    state = read_pipeline_checkpoint()
    assert state.get("precommit_attempt") == 0


def test_precommit_attempt_preserved_on_unrelated_write():
    """When precommit_attempt is not passed, the existing value is preserved."""
    _write_basic(stage="reviewed", precommit_attempt=2)
    # A later write that does NOT touch precommit_attempt should keep it.
    write_pipeline_checkpoint(next_v=100, source_v=99, stage="reviewed")
    state = read_pipeline_checkpoint()
    assert state.get("precommit_attempt") == 2


def test_stage_regression_auto_resets_precommit_attempt():
    """verified -> workers_done (worker rework) resets precommit_attempt to 0.

    Workers reworking the bot produces new code, so the precommit counter
    (which counts attempts against the CURRENT bot code) must restart.
    """
    # Reach a LATE stage with a non-zero precommit attempt count.
    _write_basic(stage="verified", precommit_attempt=3)
    state = read_pipeline_checkpoint()
    assert state.get("stage") == "verified"
    assert state.get("precommit_attempt") == 3

    # Worker rework regresses the stage back to workers_done WITHOUT explicitly
    # touching precommit_attempt — the AUTO-RESET should zero it.
    write_pipeline_checkpoint(next_v=100, source_v=99, stage="workers_done")
    state = read_pipeline_checkpoint()
    assert state.get("stage") == "workers_done"
    assert state.get("precommit_attempt") == 0


def test_critic_checked_to_master_planned_resets_precommit_attempt():
    """critic_checked -> master_planned (the actual precommit-failed rework
    path) must reset precommit_attempt to 0.

    Precommit failures leave the stage at critic_checked (verified is only set
    on a PASS). When the LLM then reworks the bot, the real transition is
    critic_checked -> master_planned, NOT verified -> early. The STAGE_RANK
    reset must fire here so the counter restarts for the new bot code.
    """
    _write_basic(stage="critic_checked", precommit_attempt=2)
    assert read_pipeline_checkpoint().get("precommit_attempt") == 2

    # Master re-plans after precommit failure → new bot code imminent.
    write_pipeline_checkpoint(next_v=100, source_v=99, stage="master_planned")
    state = read_pipeline_checkpoint()
    assert state.get("stage") == "master_planned"
    assert state.get("precommit_attempt") == 0


def test_critic_checked_to_reviewed_does_not_reset_precommit_attempt():
    """A same-code regression (critic_checked -> reviewed) must NOT reset.

    That is the same bot code being re-evaluated, not regenerated, so the
    precommit counter against the current code must survive.
    """
    _write_basic(stage="critic_checked", precommit_attempt=2)
    write_pipeline_checkpoint(next_v=100, source_v=99, stage="reviewed")
    state = read_pipeline_checkpoint()
    assert state.get("stage") == "reviewed"
    assert state.get("precommit_attempt") == 2


def test_timeout_extensions_preserved_when_not_passed():
    """write_pipeline_checkpoint that omits timeout_extensions must preserve it.

    The canonical writer builds its state from explicitly-merged fields only;
    timeout_extensions was being dropped on every normal write. Now it must
    persist across a subsequent write that does not pass the kwarg.
    """
    _write_basic(stage="verified", timeout_extensions=1)
    assert read_pipeline_checkpoint().get("timeout_extensions") == 1

    # A later write (e.g. tool_eval persisting precommit_attempt mid-cycle)
    # that does NOT mention timeout_extensions must keep the existing 1.
    write_pipeline_checkpoint(
        next_v=100, source_v=99, stage="verified", precommit_attempt=2
    )
    state = read_pipeline_checkpoint()
    assert state.get("timeout_extensions") == 1
    assert state.get("precommit_attempt") == 2


def test_timeout_extensions_resets_on_rework_regression():
    """A code-regeneration regression also resets the timeout extension budget."""
    _write_basic(stage="critic_checked", precommit_attempt=1, timeout_extensions=1)
    write_pipeline_checkpoint(next_v=100, source_v=99, stage="workers_done")
    state = read_pipeline_checkpoint()
    assert state.get("timeout_extensions") == 0


def test_forward_progression_does_not_reset_precommit_attempt():
    """Moving forward (reviewed -> critic_checked) must NOT reset the counter.

    Guards against an over-broad auto-reset that would wipe legitimate
    precommit attempt counts on normal forward stage transitions.
    """
    _write_basic(stage="reviewed", precommit_attempt=2)
    write_pipeline_checkpoint(next_v=100, source_v=99, stage="critic_checked")
    state = read_pipeline_checkpoint()
    assert state.get("stage") == "critic_checked"
    assert state.get("precommit_attempt") == 2


def test_max_precommit_retries_constant_is_three():
    """MAX_PRECOMMIT_RETRIES is exposed as the documented cap (3)."""
    assert MAX_PRECOMMIT_RETRIES == 3
