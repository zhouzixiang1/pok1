"""Superseded frozen repair preparation regression (2026-10-07).

v520 and v525 both died with ``worker_terminal_abandon`` /
``DURABLE_REPAIR_PREPARATION_UNAVAILABLE: RuntimeError: frozen Worker
preparation input contract drift`` (events.jsonl 398694 / 403368) after this
exact sequence:

1. an in-place precommit repair round COMPLETED (execute_workers froze a
   ``work_item`` with ``frozen_worker_input`` bound to that round's tasks and
   feedback; the failed ``gate_results.precommit_eval`` receipt stayed in the
   checkpoint because precommit does not re-run until after review/critic);
2. a later two-verdict review adjudication rejected the candidate, so
   ``run_review`` wrote stage ``repair_planned`` plus a NEW combined
   ``reviewer_feedback`` while the checkpoint ``master_plan`` still carried
   the completed round's work_item (the two run_review calls are the designed
   reviewer retry at ``quality_passed``, NOT a routing error);
3. resuming ``execute_workers`` at ``repair_planned`` mistook the stale
   work_item for a crashed same-round preparation and canonically abandoned
   the generation.

The fix classifies such a work_item as SUPERSEDED and re-plans the repair
from the checkpoint's current repair authority.  These tests drive the REAL
phase B/C with the REAL family classifiers and REAL task synthesis (only
filesystem / checkpoint-store / LLM-environment seams are stubbed) and pin
the routing: a superseded precommit round followed by a review double
rejection must re-plan as ``review_repair`` and count into
``quality_rework_round_count`` -- never as a precommit repair, which would
wrap the reviewer's code-quality blocker into an EV/matchup regression
prompt and burn the ``precommit_rework_count`` budget (the v520/v525
misroute).  A genuine same-round resume keeps the strict frozen-resume
semantics, and a real contract drift still fails closed naming the drifted
fields.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import tool_planning  # noqa: F401  (parent-first import order)
import tool_planning_worker  # noqa: F401
import tool_planning_worker_durable as tool_planning_worker_durable_module
import tool_planning_worker_phases_rework as phases_rework

sys.path.insert(0, str(Path(__file__).resolve().parent))

NEXT_V = 25
SOURCE_V = 24
WORKFLOW_RUN_ID = "generation:25:workflow-v1"
WORKER_TEMPLATE = "# national worker template (test)\n"
WORKER_TEMPLATE_HASH = hashlib.sha256(
    WORKER_TEMPLATE.encode("utf-8")
).hexdigest()
OLD_PRECOMMIT_FEEDBACK = (
    "Precommit failed:\n- national native TCP regression vs 2 opponents "
    "(11W-13L-0D); fix the preflop aggro threshold regression in policy.py."
)
NEW_REVIEW_FEEDBACK = (
    "Reviewer attempt 1: score=2 — policy.py duplicates the flop cbet "
    "constant instead of importing it.\n\nReviewer attempt 2: score=3 — "
    "same blocker, plus dead branch in turn check-raise sizing."
)
PREIMAGE_ARTIFACT_HASH = "a" * 64
PREIMAGE_SNAPSHOT_HASH = "b" * 64
PREPARED_SNAPSHOT_HASH = "c" * 64


def _digest(payload) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _precommit_round_tasks():
    """Tasks shaped like the completed round's frozen precommit repair plan."""

    return [
        {
            "worker_id": "auto_precommit_repair_policy_py",
            "role": "Strategic Regression Repair Architect",
            "target_files": ["policy.py"],
            "files_allowed": ["policy.py"],
            "must_change_files": ["policy.py"],
            "task_kind": "precommit_repair",
            "worker_prompt": (
                "Fix the measured national position invariant regression. "
                "decision_context.hand.position and decision_context.line."
                "position are not an EV/matchup lever."
            ),
            "repair_contract": {
                "blocker": "precommit_regression",
                "files": ["policy.py"],
                "evidence": OLD_PRECOMMIT_FEEDBACK[:2000],
                "source_stage": "precommit_failed",
            },
        }
    ]


def _stale_work_item(*, template_hash=None):
    frozen_input = {
        "schema_version": 4,
        "tasks": _precommit_round_tasks(),
        "reviewer_feedback": OLD_PRECOMMIT_FEEDBACK,
        "worker_template_hash": template_hash or WORKER_TEMPLATE_HASH,
        "backend_contract": (
            tool_planning_worker_durable_module._worker_backend_contract()
        ),
        "projection_preimage_artifact_hash": PREIMAGE_ARTIFACT_HASH,
        "projection_preimage_snapshot_hash": PREIMAGE_SNAPSHOT_HASH,
    }
    return {
        "kind": "precommit_repair",
        "source_stage": "precommit_failed",
        "reset_performed": False,
        "prepared_snapshot_hash": PREPARED_SNAPSHOT_HASH,
        "repair_baseline_artifact_hash": PREPARED_SNAPSHOT_HASH,
        "projection_preimage_artifact_hash": PREIMAGE_ARTIFACT_HASH,
        "projection_preimage_snapshot_hash": PREIMAGE_SNAPSHOT_HASH,
        "frozen_worker_input": frozen_input,
        "frozen_worker_input_digest": _digest(frozen_input),
    }


def _checkpoint(*, reviewer_feedback, template_hash=None):
    """The v520/v525 repair_planned shape.

    Includes BOTH stale family signals the real classifiers read: the
    completed precommit round's work_item (kind/source_stage) and the
    residual ``gate_results.precommit_eval.passed=False`` regression
    receipt, alongside the fresh review double rejection that actually owns
    the round.
    """
    work_item = _stale_work_item(template_hash=template_hash)
    return {
        "next_v": NEXT_V,
        "source_v": SOURCE_V,
        "stage": "repair_planned",
        "checkpoint_revision": 9,
        "workflow_run_id": WORKFLOW_RUN_ID,
        "master_plan": {
            "strategy": "master",
            "tasks": _precommit_round_tasks(),
            "work_item": work_item,
        },
        "reviewer_feedback": reviewer_feedback,
        "gate_results": {
            "quality": {"passed": True, "declared_scope_ok": True},
            "review": {
                "passed": False,
                "approved": False,
                "feedback": NEW_REVIEW_FEEDBACK,
                "quality_score": 3,
            },
            "precommit_eval": {
                "passed": False,
                "blockers": [
                    {
                        "reason": (
                            "national native TCP regression vs 2 opponents "
                            "(11W-13L-0D)"
                        )
                    }
                ],
            },
        },
        "worker_failure_count": 0,
        "quality_rework_round_count": 0,
        "precommit_rework_count": 1,
    }


class _StubArtifacts:
    def __init__(self, root: Path, fingerprint_overrides: dict):
        self.root = root
        self.snapshots = root / "snaps"
        self.snapshots.mkdir(parents=True, exist_ok=True)
        self.fingerprint_overrides = fingerprint_overrides

    def path_for(self, digest_hash: str) -> Path:
        return self.snapshots / digest_hash

    def capture(self, directory) -> str:
        key = str(Path(directory))
        if key in self.fingerprint_overrides:
            return self.fingerprint_overrides[key]
        return _dir_hash(directory)

    def preparation_workspace(self, **_kwargs) -> Path:
        workspace = self.root / "prep-workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def discard_workspace(self, _directory) -> str:
        return ""


def _dir_hash(directory) -> str:
    return hashlib.sha256(str(directory).encode("utf-8")).hexdigest()


class _Recorder:
    def __init__(self):
        self.events = []
        self.checkpoint_writes = []
        self.forced_abandons = []

    def log_system_event(self, event_type, level, message, data=None):
        self.events.append(
            {
                "type": event_type,
                "level": level,
                "message": message,
                "data": data or {},
            }
        )


def _install_mocks(monkeypatch, tmp_path, recorder):
    """Stub ONLY the environment: artifact hashing/store, checkpoint CAS,
    candidate hygiene, the workflow profile, and the publication-lock
    abandon.  Every rework-family classifier and every task-synthesis
    function runs for real."""
    tw = tool_planning_worker
    fingerprint_overrides = {}

    def fake_fingerprint(directory):
        path = Path(directory)
        if str(path) in fingerprint_overrides:
            return fingerprint_overrides[str(path)]
        return _dir_hash(path)

    next_dir = tmp_path / "bots" / f"national_cloud_v{NEXT_V}"
    next_dir.mkdir(parents=True, exist_ok=True)
    artifacts = _StubArtifacts(tmp_path / "artifacts", fingerprint_overrides)

    # The stale work_item's snapshot/preimage receipts must resolve: in the
    # v520/v525 run the prior round's snapshots still existed in the workflow
    # artifact store, which is exactly why execution survived the baseline
    # recheck and died later at the frozen-input drift gate.
    fingerprint_overrides[str(artifacts.path_for(PREPARED_SNAPSHOT_HASH))] = (
        PREPARED_SNAPSHOT_HASH
    )
    fingerprint_overrides[str(artifacts.path_for(PREIMAGE_SNAPSHOT_HASH))] = (
        PREIMAGE_ARTIFACT_HASH
    )
    fingerprint_overrides[str(next_dir)] = PREIMAGE_ARTIFACT_HASH

    async def fake_force_abandon(*_args, **_kwargs):
        recorder.forced_abandons.append((_args, _kwargs))
        return {}

    import candidate_hygiene
    import workflow_profiles

    monkeypatch.setattr(tw, "_checkpoint_architecture_policy_identity_errors", lambda ckpt: [])
    monkeypatch.setattr(tw, "_complete_artifact_fingerprint", fake_fingerprint)
    monkeypatch.setattr(tw, "_force_abandon_frozen_worker_generation", fake_force_abandon)
    monkeypatch.setattr(tw, "log_system_event", recorder.log_system_event)
    monkeypatch.setattr(
        tw,
        "write_pipeline_checkpoint",
        lambda *args, **kwargs: (
            recorder.checkpoint_writes.append((args, kwargs)) or True
        ),
    )
    monkeypatch.setattr(
        candidate_hygiene, "sanitize_candidate_dir", lambda *a, **k: None
    )
    monkeypatch.setattr(
        workflow_profiles,
        "get_workflow_profile",
        lambda: SimpleNamespace(national_execution_mode="native_tcp"),
    )
    return {
        "artifacts": artifacts,
        "next_dir": next_dir,
        "fingerprint_overrides": fingerprint_overrides,
    }


def _run_phase_b(monkeypatch, tmp_path, *, reviewer_feedback, template_hash=None):
    recorder = _Recorder()
    mocks = _install_mocks(monkeypatch, tmp_path, recorder)
    ckpt = _checkpoint(
        reviewer_feedback=reviewer_feedback, template_hash=template_hash
    )
    result = asyncio.run(
        phases_rework._execute_workers_phase_b_rework_synthesis(
            actor_lock_owned=False,
            checkpoint_tasks=_precommit_round_tasks(),
            ckpt=ckpt,
            durable_worker_envelope={},
            durable_worker_resume=False,
            durable_worker_status="idle",
            next_dir=mocks["next_dir"],
            next_v=NEXT_V,
            reviewer_feedback=reviewer_feedback,
            source_v=SOURCE_V,
            tasks=[],
            tasks_provided=False,
            worker_workflow=SimpleNamespace(
                artifacts=mocks["artifacts"], run_id=WORKFLOW_RUN_ID
            ),
        )
    )
    return result, recorder, ckpt, mocks


def _result_payload(result):
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return json.loads(result["content"][0]["text"])
    if isinstance(result, str):
        return json.loads(result)
    raise AssertionError(f"expected json tool result, got {result!r}")


def _run_phase_c(monkeypatch, tmp_path, recorder, ckpt, mocks, *, frozen_rework_resume,
                 reviewer_feedback, tasks, durable_cycle=2):
    return asyncio.run(
        phases_rework._execute_workers_phase_c_rework_preparation(
            actor_lock_owned=False,
            ckpt=ckpt,
            durable_worker_state={
                "status": "idle",
                "cycle": durable_cycle,
                "envelope": None,
            },
            durable_worker_status="idle",
            frozen_rework_resume=frozen_rework_resume,
            next_dir=mocks["next_dir"],
            next_v=NEXT_V,
            replace_checkpoint_tasks=True,
            review_rework_checkpoint=False,
            reviewer_feedback=reviewer_feedback,
            source_v=SOURCE_V,
            tasks=tasks,
            worker_template=WORKER_TEMPLATE,
            worker_workflow=SimpleNamespace(
                artifacts=mocks["artifacts"], run_id=WORKFLOW_RUN_ID
            ),
        )
    )


# --- the v520/v525 sequence: review re-adjudication owns the new round -------


def test_superseded_precommit_round_replans_as_review_repair(monkeypatch, tmp_path):
    """Stale precommit work_item + residual precommit receipt + fresh review
    double rejection: the re-planned round must be a REVIEW repair counted
    into quality_rework_round_count, not a precommit repair."""
    result, recorder, ckpt, mocks = _run_phase_b(
        monkeypatch,
        tmp_path,
        reviewer_feedback=NEW_REVIEW_FEEDBACK,
    )
    assert isinstance(result, tuple), (
        "phase B must continue, got exit "
        f"{_result_payload(result) if not isinstance(result, tuple) else result!r}"
    )
    ctx = result[0]
    assert ctx["frozen_rework_resume"] is False, (
        "a superseded preparation from a completed prior repair round must "
        "not be treated as a frozen rework resume"
    )
    # The synthesized tasks come from the REAL synthesis and must be review
    # repairs against the reviewer's blocker -- not precommit EV/matchup
    # repairs against the superseded regression receipt.
    assert ctx["tasks"], "phase B must synthesize repair tasks for the new round"
    assert all(
        str(task.get("task_kind") or "") == "review_repair"
        for task in ctx["tasks"]
    ), f"expected review_repair tasks, got {[t.get('task_kind') for t in ctx['tasks']]}"
    assert all(
        str(task.get("repair_blocker") or "") == "review_rejection"
        for task in ctx["tasks"]
    )
    assert not any(
        "matchup" in str(task.get("worker_prompt") or "").lower()
        or "ev/" in str(task.get("worker_prompt") or "").lower()
        for task in ctx["tasks"]
    )
    superseded_events = [
        event
        for event in recorder.events
        if event["type"] == "pipeline.worker_repair_preparation_superseded"
    ]
    assert superseded_events, (
        "the superseded classification must be observable "
        f"(events: {[e['type'] for e in recorder.events]})"
    )
    event_data = superseded_events[0]["data"]
    assert event_data.get("drift_fields") == ["reviewer_feedback"]
    assert event_data.get("dropped_prepared_snapshot_hash") == PREPARED_SNAPSHOT_HASH
    assert (
        event_data.get("dropped_repair_baseline_artifact_hash")
        == PREPARED_SNAPSHOT_HASH
    )
    assert event_data.get("dropped_precommit_receipt") is True
    assert event_data.get("superseded_kind") == "precommit_repair"

    phase_c_result = _run_phase_c(
        monkeypatch,
        tmp_path,
        recorder,
        ckpt,
        mocks,
        frozen_rework_resume=ctx["frozen_rework_resume"],
        reviewer_feedback=ctx["reviewer_feedback"],
        tasks=ctx["tasks"],
    )
    assert isinstance(phase_c_result, tuple), (
        "phase C must re-plan the superseded repair, got exit "
        f"{_result_payload(phase_c_result) if not isinstance(phase_c_result, tuple) else phase_c_result!r}"
    )
    plan_metadata = phase_c_result[0]["rework_plan_metadata"]
    assert plan_metadata["kind"] == "review_repair"
    frozen_input = plan_metadata["frozen_worker_input"]
    # The fresh freeze binds the NEW round's feedback (the in-place review
    # repair NOTE is appended by design), never the superseded precommit one.
    assert frozen_input["reviewer_feedback"].startswith(NEW_REVIEW_FEEDBACK)
    assert frozen_input["tasks"] == ctx["tasks"]
    assert recorder.checkpoint_writes, (
        "the re-planned repair must publish a fresh repair_planned checkpoint"
    )
    repair_writes = [
        write[1]
        for write in recorder.checkpoint_writes
        if len(write[0]) > 2 and write[0][2] == "repair_planned"
    ]
    assert repair_writes, (
        f"no repair_planned checkpoint write, got {recorder.checkpoint_writes}"
    )
    # The round is counted as a quality/review rework round, NOT against the
    # precommit rework budget (1 legit precommit round already used).
    assert repair_writes[0].get("quality_rework_round_count") == 1, (
        f"round must count into quality_rework_round_count, got {repair_writes[0]}"
    )
    assert repair_writes[0].get("precommit_rework_count") is None, (
        f"round must NOT touch precommit_rework_count, got {repair_writes[0]}"
    )
    assert not recorder.forced_abandons


def test_superseded_preparation_does_not_abandon_with_drift(monkeypatch, tmp_path):
    """Direct regression on the fatal exit: with the stale work_item present,
    resuming must not produce DURABLE_REPAIR_PREPARATION_UNAVAILABLE."""
    result, recorder, ckpt, mocks = _run_phase_b(
        monkeypatch,
        tmp_path,
        reviewer_feedback=NEW_REVIEW_FEEDBACK,
    )
    assert isinstance(result, tuple)
    ctx = result[0]
    phase_c_result = _run_phase_c(
        monkeypatch,
        tmp_path,
        recorder,
        ckpt,
        mocks,
        frozen_rework_resume=ctx["frozen_rework_resume"],
        reviewer_feedback=NEW_REVIEW_FEEDBACK,
        tasks=ctx["tasks"],
    )
    if not isinstance(phase_c_result, tuple):
        payload = _result_payload(phase_c_result)
        assert payload.get("error") != "DURABLE_REPAIR_PREPARATION_UNAVAILABLE", (
            f"superseded preparation must not abandon, got {payload}"
        )
    # A continuation tuple is the desired outcome: the superseded preparation
    # was re-planned instead of drifted/abandoned.


# --- a genuine precommit authority keeps the precommit family ----------------


def test_same_round_precommit_resume_keeps_precommit_family(monkeypatch, tmp_path):
    """Crash between the precommit-repair publication and WorkerPrepared: the
    frozen input still binds the checkpoint's current precommit feedback, so
    the round is NOT superseded, the strict frozen-resume validation stays
    engaged, and the family stays precommit (real classifiers)."""
    matching_feedback = OLD_PRECOMMIT_FEEDBACK
    result, recorder, ckpt, mocks = _run_phase_b(
        monkeypatch, tmp_path, reviewer_feedback=matching_feedback
    )
    assert isinstance(result, tuple), (
        "same-round phase B must continue, got exit "
        f"{_result_payload(result) if not isinstance(result, tuple) else result!r}"
    )
    ctx = result[0]
    assert ctx["frozen_rework_resume"] is True
    assert not [
        event
        for event in recorder.events
        if event["type"] == "pipeline.worker_repair_preparation_superseded"
    ]
    # Real classifier verdicts for the same-round precommit checkpoint.
    import tool_planning_quality_repair_targets as repair_targets

    assert repair_targets._is_precommit_rework_checkpoint(ckpt) is True
    # Tasks stay the frozen precommit round's tasks (master plan authority).
    assert ctx["tasks"] == _precommit_round_tasks()
    phase_c_result = _run_phase_c(
        monkeypatch,
        tmp_path,
        recorder,
        ckpt,
        mocks,
        frozen_rework_resume=True,
        reviewer_feedback=matching_feedback,
        tasks=_precommit_round_tasks(),
        durable_cycle=1,
    )
    assert isinstance(phase_c_result, tuple), (
        "a matching same-round preparation must resume, got exit "
        f"{_result_payload(phase_c_result) if not isinstance(phase_c_result, tuple) else phase_c_result!r}"
    )
    assert (
        phase_c_result[0]["rework_plan_metadata"]["frozen_worker_input"][
            "reviewer_feedback"
        ]
        == matching_feedback
    )
    # A real same-round precommit resume is counted against the precommit
    # budget (round already counted at planning), not quality rounds.
    resumed_counts = phase_c_result[0]["precommit_rework_count_for_write"]
    assert resumed_counts == 1


# --- genuine contract drift still fails closed, now with field names --------


def test_genuine_drift_still_fails_closed_naming_the_drifted_fields(
    monkeypatch, tmp_path
):
    """A frozen input whose worker_template_hash no longer matches the live
    template is real drift: the exit must stay DURABLE_REPAIR_PREPARATION_
    UNAVAILABLE and name the drifted field instead of a bare token."""
    result, recorder, ckpt, mocks = _run_phase_b(
        monkeypatch,
        tmp_path,
        reviewer_feedback=OLD_PRECOMMIT_FEEDBACK,
        template_hash="0" * 64,
    )
    assert isinstance(result, tuple)
    ctx = result[0]
    assert ctx["frozen_rework_resume"] is True
    phase_c_result = _run_phase_c(
        monkeypatch,
        tmp_path,
        recorder,
        ckpt,
        mocks,
        frozen_rework_resume=True,
        reviewer_feedback=OLD_PRECOMMIT_FEEDBACK,
        tasks=_precommit_round_tasks(),
        durable_cycle=1,
    )
    payload = _result_payload(phase_c_result)
    assert payload.get("error") == "DURABLE_REPAIR_PREPARATION_UNAVAILABLE"
    assert "worker_template_hash" in str(payload.get("message")), (
        f"drift must name the drifted field, got {payload.get('message')!r}"
    )
    assert "worker_template_hash" in payload.get("drift_fields", []), (
        f"drift_fields must list the drifted field, got {payload.get('drift_fields')!r}"
    )
