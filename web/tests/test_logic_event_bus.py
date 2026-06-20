"""Tests for event_bus.py — the Phase-0 log-system-redesign backbone.

Covers the structural guarantees the redesign relies on:
  - dual-write (new events.jsonl + legacy system_events.jsonl)
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

import pytest

import event_bus


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


@pytest.fixture(autouse=True)
def _reset_event_bus():
    event_bus.reset_for_test()
    yield
    event_bus.reset_for_test()


@pytest.fixture
def isolated_files(tmp_path, monkeypatch):
    """Point both the new and legacy sinks at tmp_path so emit() is hermetic."""
    import system_log
    monkeypatch.setattr(event_bus, "EVENTS_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(system_log, "SYSTEM_EVENTS_FILE", tmp_path / "system_events.jsonl")
    return tmp_path


def _ckpt(**kw):
    return lambda: kw


# ── dual-write + schema ──────────────────────────────────────────────────────

def test_emit_dual_writes_new_and_legacy(isolated_files):
    import system_log
    event_bus.emit("pipeline.test", "info", "hello", next_v=127)
    new = _read_jsonl(isolated_files / "events.jsonl")
    legacy = _read_jsonl(system_log.SYSTEM_EVENTS_FILE)
    assert len(new) == 1 and new[0]["type"] == "pipeline.test"
    assert len(legacy) == 1 and legacy[0]["type"] == "pipeline.test"


def test_emit_schema_has_correlation_fields(isolated_files):
    """Every event carries run_id/stage/attempt/pid/proc/category (RC2/RC5/RC6)."""
    event_bus.emit("pipeline.x", "info", "m", next_v=5)
    data = _read_jsonl(isolated_files / "events.jsonl")[0]["data"]
    for k in ("category", "stage", "attempt", "run_id", "pid", "proc"):
        assert k in data, f"missing correlation field {k}"
    assert data["category"] == "pipeline.x"
    assert data["pid"] == os.getpid()


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
    """log_system_event keeps its 4-arg signature and forwards to emit (dual-write)."""
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
        next_v=7, source_v=6, stage="master_planned")
    with evolution_infra.locked_file(
            evolution_infra.PIPELINE_STATE_FILE, "r", lock_type=fcntl.LOCK_EX):
        event_bus.emit("pipeline.under_lock", "info", "inside EX scope")
    ev = _read_jsonl(isolated_files / "events.jsonl")[-1]
    assert ev["data"]["run_id"] == "7#0"
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
    import evolution_core
    import tool_gates
    wf = isolated_files / "worker_failures.jsonl"
    monkeypatch.setattr(evolution_core, "WORKER_FAILURES_FILE", wf)
    tool_gates._record_quality_failure(127, "critic", "Strategy Critic", "rejected")
    line = json.loads(wf.read_text().strip())
    assert line["category"] == "gate"


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
        next_v=42, source_v=41, stage="master_planned",
        generation_attempt=1, audit_attempt=2, precommit_attempt=3)
    event_bus.emit("pipeline.after_checkpoint_write", "info", "m")
    data = _read_jsonl(isolated_files / "events.jsonl")[-1]["data"]
    assert data["run_id"] == "42#1"                  # composite key from checkpoint
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
        next_v=50, source_v=49, stage="archived", generation_attempt=0)
    evolution_infra.clear_pipeline_checkpoint()      # post-commit
    event_bus.emit("pipeline.post_commit_archivist", "info", "m")
    data = _read_jsonl(isolated_files / "events.jsonl")[-1]["data"]
    assert data["run_id"] == "50#0"                  # last-known, not lost
