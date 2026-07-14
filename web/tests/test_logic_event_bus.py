"""Tests for event_bus.py — the Phase-0 log-system-redesign backbone.

Covers the structural guarantees the redesign relies on:
  - one canonical events.jsonl write, with the same event broadcast over SSE
  - mandatory correlation schema (run_id/stage/attempt/pid/proc/category)
  - severity normalisation to the frontend's canonical 4 values
  - _resolve_context() fallback chain (contextvar → last-known → checkpoint)
  - bind() scope + last-known surviving checkpoint clear (RC2 race fix)
  - cross-thread context handoff (RC6 blind spot: contextvars don't cross threads)
  - POK_LOG_STRICT hard-asserts (enforcement, not convention)
  - log_system_event shim forwards to emit + keeps mock target valid
"""

import json
import os
import threading
from types import SimpleNamespace

import checkpoint_schema
import pytest

import event_bus


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


@pytest.fixture(autouse=True)
def _reset_event_bus(monkeypatch):
    def resolve(label, **_kwargs):
        version = int(str(label).rsplit("_v", 1)[1])
        return SimpleNamespace(
            eligible=True,
            version=version,
            issues=(),
            runtime_manifest={"epoch": "national_tcp_policy_v1"},
            epoch_receipt={"epoch": "national_tcp_policy_v1", "version": version},
            publication_identity={"published": True, "version": version},
            certificate_digest="a" * 64,
        )

    monkeypatch.setattr(checkpoint_schema, "resolve_national_bot_spec", resolve)
    event_bus.reset_for_test()
    yield
    event_bus.reset_for_test()


@pytest.fixture
def isolated_files(tmp_path, monkeypatch):
    """Point the canonical sink at tmp_path so emit() is hermetic."""
    monkeypatch.setattr(event_bus, "EVENTS_FILE", tmp_path / "events.jsonl")
    return tmp_path


def _ckpt(**kw):
    return lambda: kw


# ── dual-write + schema ──────────────────────────────────────────────────────

def test_emit_writes_exactly_one_canonical_row(isolated_files):
    event_bus.emit("pipeline.test", "info", "hello", next_v=127)
    rows = _read_jsonl(isolated_files / "events.jsonl")
    assert len(rows) == 1 and rows[0]["type"] == "pipeline.test"
    assert not (isolated_files / "system_events.jsonl").exists()


def test_emit_schema_has_correlation_fields(isolated_files):
    """Every event carries run_id/stage/attempt/pid/proc/category (RC2/RC5/RC6)."""
    event_bus.emit("pipeline.x", "info", "m", next_v=5)
    data = _read_jsonl(isolated_files / "events.jsonl")[0]["data"]
    for k in ("category", "stage", "attempt", "run_id", "pid", "proc"):
        assert k in data, f"missing correlation field {k}"
    assert data["category"] == "pipeline.x"
    assert data["pid"] == os.getpid()
    assert data["emitter_pid"] == os.getpid()


def test_emit_stamps_current_policy_epoch_identity(isolated_files, monkeypatch):
    monkeypatch.setattr(
        event_bus,
        "_current_epoch_identity",
        lambda: ("national_tcp_policy_v1", "a" * 64),
    )

    event_bus.emit("pipeline.identity", "info", "m")

    data = _read_jsonl(isolated_files / "events.jsonl")[0]["data"]
    assert data["evaluation_epoch"] == "national_tcp_policy_v1"
    assert data["epoch_reset_receipt_digest"] == "a" * 64


def test_emit_preserves_business_pid_and_records_emitter(isolated_files):
    """Daemon lifecycle logs may carry a target pid; event_bus must not overwrite it."""
    event_bus.emit("daemon.stop_requested", "info", "stop", pid=12345, proc="daemon")
    data = _read_jsonl(isolated_files / "events.jsonl")[0]["data"]
    assert data["pid"] == 12345
    assert data["proc"] == "daemon"
    assert data["emitter_pid"] == os.getpid()
    assert data["emitter_proc"] == event_bus.current_proc()


# ── severity normalisation ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("info", "info"), ("warn", "warn"), ("error", "error"), ("success", "success"),
    ("warning", "warn"), ("critical", "error"), ("fatal", "error"),
    ("OK", "success"), ("done", "success"), ("bogus", "error"),
])
def test_severity_normalisation(isolated_files, raw, expected):
    event_bus.emit("pipeline.x", raw, "m")
    ev = _read_jsonl(isolated_files / "events.jsonl")[-1]
    assert ev["severity"] == expected
    if raw != expected:
        assert ev["data"]["severity_raw"] == raw


# ── context resolution (RC2) ──────────────────────────────────────────────────

def test_resolve_context_from_checkpoint(isolated_files, monkeypatch):
    """No contextvar/last-known → run_id/stage/attempt come from checkpoint."""
    monkeypatch.setattr(event_bus, "_read_ckpt_cached", _ckpt(
        next_v=127, stage="master_planned",
        generation_attempt=2, audit_attempt=1, precommit_attempt=0))
    event_bus.emit("pipeline.x", "info", "m")
    data = _read_jsonl(isolated_files / "events.jsonl")[0]["data"]
    assert data["run_id"] == "127#2"           # composite key (deadloop-visible)
    assert data["stage"] == "master_planned"
    assert data["attempt"]["generation"] == 2
    assert data["attempt"]["audit"] == 1


def test_checkpoint_context_updates_last_known_before_checkpoint_clear(isolated_files, monkeypatch):
    """Checkpoint-derived context becomes last-known for post-clear tail logs."""
    states = [
        {
            "next_v": 127,
            "stage": "master_planned",
            "generation_attempt": 2,
            "audit_attempt": 1,
            "precommit_attempt": 0,
        },
    ]
    monkeypatch.setattr(event_bus, "_read_ckpt_cached", lambda: states.pop(0) if states else {})

    event_bus.emit("pipeline.from_ckpt", "info", "m1")
    event_bus.emit("pipeline.after_clear", "info", "m2")

    rows = _read_jsonl(isolated_files / "events.jsonl")
    assert rows[0]["data"]["run_id"] == "127#2"
    assert rows[1]["data"]["run_id"] == "127#2"
    assert rows[1]["data"]["stage"] == "master_planned"


def test_live_checkpoint_replaces_stale_last_known_for_unbound_emitters(isolated_files, monkeypatch):
    """Daemon/background emitters must follow the current checkpoint, not stale last-known."""
    event_bus.update_last_known(
        run_id="127#0",
        stage="verified",
        attempt={"generation": 0, "audit": 0, "precommit": 0},
    )
    monkeypatch.setattr(event_bus, "_read_ckpt_cached", _ckpt(
        next_v=128,
        stage="workers_done",
        generation_attempt=1,
        audit_attempt=2,
        precommit_attempt=3,
    ))

    event_bus.emit("daemon.progress", "info", "m")
    data = _read_jsonl(isolated_files / "events.jsonl")[-1]["data"]

    assert data["run_id"] == "128#1"
    assert data["stage"] == "workers_done"
    assert data["attempt"]["audit"] == 2


def test_logging_correlation_filter_uses_event_bus_capture_context(monkeypatch):
    import logging
    import logging_config

    monkeypatch.setattr(event_bus, "capture_context", lambda: {"run_id": "127#2"})
    record = logging.LogRecord("pok.test", logging.INFO, __file__, 1, "message", (), None)

    assert logging_config.CorrelationFilter().filter(record) is True
    assert record.run_id == "127#2"
    assert record.pid == os.getpid()


def test_explicit_overrides_win(isolated_files, monkeypatch):
    monkeypatch.setattr(event_bus, "_read_ckpt_cached", _ckpt(
        next_v=127, stage="master_planned", generation_attempt=2))
    event_bus.emit("pipeline.x", "info", "m", run_id="CUSTOM", stage="custom_stage")
    data = _read_jsonl(isolated_files / "events.jsonl")[0]["data"]
    assert data["run_id"] == "CUSTOM"
    assert data["stage"] == "custom_stage"


def test_bind_sets_context_and_last_known_survives(isolated_files, monkeypatch):
    """bind() sets contextvar + last-known; contextvar resets on exit, last-known stays."""
    monkeypatch.setattr(event_bus, "_read_ckpt_cached", _ckpt())
    with event_bus.bind(run_id="99#0", stage="workers_done"):
        event_bus.emit("pipeline.x", "info", "inside")
        assert _read_jsonl(isolated_files / "events.jsonl")[-1]["data"]["run_id"] == "99#0"
    # after exit: contextvar reset, but last-known kept → next emit still 99#0
    event_bus.emit("pipeline.y", "info", "outside")
    assert _read_jsonl(isolated_files / "events.jsonl")[-1]["data"]["run_id"] == "99#0"


def test_last_known_survives_checkpoint_clear(isolated_files, monkeypatch):
    """Post-commit window: checkpoint None, but last-known resolves the just-finished gen."""
    with event_bus.bind(run_id="55#1", stage="reviewed"):
        pass
    monkeypatch.setattr(event_bus, "_read_ckpt_cached", _ckpt())  # simulate clear
    event_bus.emit("pipeline.post_commit", "info", "m")
    assert _read_jsonl(isolated_files / "events.jsonl")[-1]["data"]["run_id"] == "55#1"


def test_payload_version_does_not_override_existing_run_context(isolated_files, monkeypatch):
    """A role may analyze v243 while the active pipeline run is v244#0."""
    monkeypatch.setattr(event_bus, "_read_ckpt_cached", _ckpt())
    with event_bus.bind(run_id="244#0", stage="preparing",
                        attempt={"generation": 0, "audit": 0, "precommit": 0}):
        event_bus.emit("pipeline.llm_role_start", "info", "MATCH ANALYST", version=243)

    data = _read_jsonl(isolated_files / "events.jsonl")[-1]["data"]
    assert data["run_id"] == "244#0"
    assert data["stage"] == "preparing"
    assert data["version"] == 243


# ── cross-thread context handoff (RC6 blind spot) ────────────────────────────

def test_capture_apply_context_across_thread(isolated_files):
    """contextvars don't cross threads — capture/apply must hand off explicitly."""
    with event_bus.bind(run_id="77#3", stage="critic_checked"):
        ctx = event_bus.capture_context()
        assert ctx["run_id"] == "77#3"
        seen = {}
        def worker():
            event_bus.apply_context(ctx)
            event_bus.emit("pipeline.thread", "info", "from thread")
            seen["rid"] = _read_jsonl(isolated_files / "events.jsonl")[-1]["data"]["run_id"]
        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert seen["rid"] == "77#3"


def test_capture_context_falls_back_to_checkpoint(isolated_files, monkeypatch):
    """Role-IO correlation uses capture_context(), so it must match emit() fallback."""
    monkeypatch.setattr(event_bus, "_read_ckpt_cached", _ckpt(
        next_v=243, stage="master_planned",
        generation_attempt=0, audit_attempt=0, precommit_attempt=0))
    ctx = event_bus.capture_context()
    assert ctx["run_id"] == "243#0"
    assert ctx["stage"] == "master_planned"
    assert ctx["attempt"]["generation"] == 0


def test_thread_without_apply_falls_back_to_checkpoint(isolated_files, monkeypatch):
    """A long-lived worker thread that did NOT apply_context still resolves via checkpoint."""
    monkeypatch.setattr(event_bus, "_read_ckpt_cached", _ckpt(
        next_v=42, stage="verified", generation_attempt=0))
    seen = {}
    def worker():
        event_bus.emit("pipeline.worker", "info", "m")
        seen["rid"] = _read_jsonl(isolated_files / "events.jsonl")[-1]["data"]["run_id"]
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["rid"] == "42#0"


# ── enforcement (POK_LOG_STRICT) ─────────────────────────────────────────────

def test_strict_mode_rejects_bad_category(monkeypatch):
    monkeypatch.setattr(event_bus, "_STRICT", True)
    with pytest.raises(AssertionError):
        event_bus.emit("no_dot_prefix", "info", "m")


def test_strict_mode_rejects_bad_severity(monkeypatch):
    monkeypatch.setattr(event_bus, "_STRICT", True)
    with pytest.raises(AssertionError):
        event_bus.emit("pipeline.x", "bogus", "m")


# ── semantic family + failure_mode (RC1/RC4) ─────────────────────────────────

def test_semantic_family_and_failure_mode(isolated_files):
    event_bus.failure("pipeline.parse", "bad", failure_mode="NO_FENCE", next_v=3)
    event_bus.success("pipeline.done", "ok")
    event_bus.warn("pipeline.caution", "careful")
    event_bus.progress("pipeline.step", "going")
    evs = _read_jsonl(isolated_files / "events.jsonl")
    assert [e["severity"] for e in evs] == ["error", "success", "warn", "info"]
    assert evs[0]["data"]["failure_mode"] == "NO_FENCE"


# ── legacy shim compatibility ────────────────────────────────────────────────

def test_log_system_event_shim_forwards(isolated_files):
    """log_system_event keeps its 4-arg signature and forwards to emit."""
    import system_log
    system_log.log_system_event("pipeline.legacy", "info", "msg", {"next_v": 10})
    ev = _read_jsonl(isolated_files / "events.jsonl")[0]
    assert ev["type"] == "pipeline.legacy"
    assert ev["data"]["next_v"] == 10
    assert "run_id" in ev["data"]            # correlation auto-injected via emit


def test_log_system_event_monkeypatch_target_still_valid(monkeypatch, tmp_path):
    """6+ existing tests monkeypatch log_system_event — the shim keeps mock target valid."""
    import system_log
    monkeypatch.setattr(event_bus, "EVENTS_FILE", tmp_path / "events.jsonl")
    captured = []
    monkeypatch.setattr(system_log, "log_system_event",
                        lambda *a, **k: captured.append(a))
    system_log.log_system_event("pipeline.x", "info", "m", {"a": 1})
    assert len(captured) == 1 and captured[0][0] == "pipeline.x"
    # mock short-circuited the shim → emit never ran → no events.jsonl written
    assert not (tmp_path / "events.jsonl").exists()


# ── checkpoint TTL cache ─────────────────────────────────────────────────────

def test_ckpt_cache_avoids_repeated_reads(isolated_files, monkeypatch):
    """500ms TTL: many emits in-window read pipeline_state at most once (daemon hot path)."""
    calls = {"n": 0}
    def fake_read():
        calls["n"] += 1
        return {"next_v": 1, "stage": "prepared", "generation_attempt": 0,
                "audit_attempt": 0, "precommit_attempt": 0}
    monkeypatch.setattr(event_bus, "_read_ckpt_nolock", fake_read)
    event_bus.invalidate_ckpt_cache()
    for _ in range(5):
        event_bus.emit("pipeline.x", "info", "m")
    assert calls["n"] <= 1                    # cached across the 5 emits


def test_emit_does_not_deadlock_when_checkpoint_locked(isolated_files):
    """Regression: emit() reached inside write_pipeline_checkpoint's LOCK_EX
    scope must not self-deadlock. fcntl.flock is per-process — EX blocks SH on
    the same file even within one process — so _resolve_context reads the
    checkpoint with a plain open() (no LOCK_SH). This test would hang ~30s
    (pytest-timeout) if anyone re-introduces a locked read in the fallback.
    """
    import fcntl
    import evolution_infra
    evolution_infra.write_pipeline_checkpoint(
        next_v=207, source_v=206, stage="master_planned")
    with evolution_infra.locked_file(
            evolution_infra.PIPELINE_STATE_FILE, "r", lock_type=fcntl.LOCK_EX):
        event_bus.emit("pipeline.under_lock", "info", "inside EX scope")
    ev = _read_jsonl(isolated_files / "events.jsonl")[-1]
    assert ev["data"]["run_id"] == "207#0"
    assert ev["data"]["stage"] == "master_planned"


# ── Phase 1: category separation (RC5) ───────────────────────────────────────

def test_worker_failure_recorded_with_worker_category(isolated_files, monkeypatch):
    """RC5: _record_worker_failure tags category='worker' so the 49 critic + 9
    reviewer noise in worker_failures.jsonl can be filtered out."""
    import agent_workers
    import evolution_infra
    wf = isolated_files / "worker_failures.jsonl"
    monkeypatch.setattr(evolution_infra, "WORKER_FAILURES_FILE", wf)
    monkeypatch.setattr(agent_workers, "WORKER_FAILURES_FILE", wf)
    agent_workers._record_worker_failure(127, 1, "LogicArchitect", "compile error")
    line = json.loads(wf.read_text().strip())
    assert line["category"] == "worker"
    assert line["role"] == "LogicArchitect"


def test_quality_failure_recorded_with_gate_category(isolated_files, monkeypatch):
    """RC5: _record_quality_failure (reviewer/critic) tags category='gate',
    separable from real worker crashes."""
    import checkpoint_schema
    import evolution_infra
    import tool_gates
    wf = isolated_files / "worker_failures.jsonl"
    monkeypatch.setattr(evolution_infra, "WORKER_FAILURES_FILE", wf)
    monkeypatch.setattr(tool_gates, "read_pipeline_checkpoint", lambda: {"strict": True})
    monkeypatch.setattr(
        checkpoint_schema,
        "strict_checkpoint_event_identity",
        lambda *_args, **_kwargs: {
            "gen": 127,
            "evaluation_epoch": "national_tcp_policy_v1",
            "workflow_run_id": "generation:127:test",
        },
    )
    tool_gates._record_quality_failure(127, "critic", "Strategy Critic", "rejected")
    line = json.loads(wf.read_text().strip())
    assert line["category"] == "gate"
    assert line["evaluation_epoch"] == "national_tcp_policy_v1"
    assert line["workflow_run_id"] == "generation:127:test"


# ── Phase 1: checkpoint → last-known auto-correlation (RC2) ──────────────────

def test_write_checkpoint_updates_last_known(isolated_files, monkeypatch):
    """RC2: write_pipeline_checkpoint refreshes event_bus last-known, so an emit
    after a stage advance (with no manual bind) auto-carries run_id/stage/attempt.
    This is what makes correlation automatic for ALL pipeline code."""
    import evolution_infra
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE",
                        isolated_files / "pipeline_state.json")
    event_bus.reset_for_test()
    evolution_infra.write_pipeline_checkpoint(
        next_v=242, source_v=241, stage="master_planned",
        generation_attempt=1, audit_attempt=2, precommit_attempt=3)
    event_bus.emit("pipeline.after_checkpoint_write", "info", "m")
    data = _read_jsonl(isolated_files / "events.jsonl")[-1]["data"]
    assert data["run_id"] == "242#1"                 # composite key from checkpoint
    assert data["stage"] == "master_planned"
    assert data["attempt"]["generation"] == 1
    assert data["attempt"]["audit"] == 2
    assert data["attempt"]["precommit"] == 3


def test_last_known_survives_clear_via_write_checkpoint(isolated_files, monkeypatch):
    """RC2: after write_pipeline_checkpoint then clear_pipeline_checkpoint (the
    post-commit window), emits still resolve the just-finished generation."""
    import evolution_infra
    ckpt = isolated_files / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", ckpt)
    event_bus.reset_for_test()
    evolution_infra.write_pipeline_checkpoint(
        next_v=250, source_v=249, stage="archived", generation_attempt=0)
    evolution_infra.clear_pipeline_checkpoint()      # post-commit
    event_bus.emit("pipeline.post_commit_archivist", "info", "m")
    data = _read_jsonl(isolated_files / "events.jsonl")[-1]["data"]
    assert data["run_id"] == "250#0"                 # last-known, not lost
