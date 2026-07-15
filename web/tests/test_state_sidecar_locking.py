"""Concurrency and filesystem-authenticity tests for durable state helpers."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path

import pytest

import elo_daemon
import evolution_infra


def _instrument_data_open(monkeypatch, path: Path, thread_name: str):
    opened = threading.Event()
    real_open = evolution_infra.os.open

    def observed_open(candidate, flags, *args, **kwargs):
        if (
            threading.current_thread().name == thread_name
            and Path(candidate) == path
        ):
            opened.set()
        return real_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(evolution_infra.os, "open", observed_open)
    return opened


def test_waiting_reader_opens_live_inode_only_after_atomic_writer(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "state.json"
    evolution_infra.write_locked_json(path, {"value": "old"})
    writer_inside = threading.Event()
    release_writer = threading.Event()
    real_publish = evolution_infra._atomic_publish_state_text

    def paused_publish(candidate, raw):
        writer_inside.set()
        assert release_writer.wait(5)
        return real_publish(candidate, raw)

    monkeypatch.setattr(evolution_infra, "_atomic_publish_state_text", paused_publish)
    reader_opened = _instrument_data_open(monkeypatch, path, "state-reader")
    result = {}
    writer = threading.Thread(
        target=evolution_infra.write_locked_json,
        args=(path, {"value": "new"}),
        name="state-writer",
    )
    reader = threading.Thread(
        target=lambda: result.setdefault(
            "value", evolution_infra.read_locked_json(path)
        ),
        name="state-reader",
    )

    writer.start()
    assert writer_inside.wait(5)
    reader.start()
    assert not reader_opened.wait(0.2)
    release_writer.set()
    writer.join(5)
    reader.join(5)

    assert not writer.is_alive()
    assert not reader.is_alive()
    assert reader_opened.is_set()
    assert result["value"] == {"value": "new"}


def test_waiting_appender_targets_replaced_live_inode(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    path.write_text('{"value":"old"}\n', encoding="utf-8")
    writer_inside = threading.Event()
    release_writer = threading.Event()
    appender_opened = _instrument_data_open(monkeypatch, path, "state-appender")
    errors: list[BaseException] = []

    def writer_body():
        try:
            with evolution_infra._locked_state_sidecar(
                path,
                lock_type=fcntl.LOCK_EX,
            ):
                writer_inside.set()
                assert release_writer.wait(5)
                evolution_infra._atomic_publish_state_text(
                    path,
                    '{"value":"new"}\n',
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def appender_body():
        try:
            evolution_infra.append_locked_jsonl(path, {"row": 1})
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    writer = threading.Thread(target=writer_body, name="state-writer")
    appender = threading.Thread(target=appender_body, name="state-appender")
    writer.start()
    assert writer_inside.wait(5)
    appender.start()
    assert not appender_opened.wait(0.2)
    release_writer.set()
    writer.join(5)
    appender.join(5)

    assert not errors
    assert not writer.is_alive()
    assert not appender.is_alive()
    assert appender_opened.is_set()
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"value": "new"},
        {"row": 1},
    ]


def test_priority_expiry_unlinks_old_inode_before_waiting_writer_publishes(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "priority_eval.json"
    evolution_infra.write_locked_json(
        path,
        {"bot": "national_v143", "min_games": 10},
    )
    monkeypatch.setattr(elo_daemon, "PRIORITY_EVAL_FILE", path)

    criterion_entered = threading.Event()
    release_criterion = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    result = {}
    errors: list[BaseException] = []

    def load_stats():
        criterion_entered.set()
        assert release_criterion.wait(5)
        return {"national_v143": {"games": 10}}

    def consume():
        try:
            result["priority"] = elo_daemon._load_priority_eval()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def publish_replacement():
        try:
            writer_started.set()
            evolution_infra.write_locked_json(
                path,
                {"bot": "national_v144", "min_games": 500},
            )
            writer_done.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    monkeypatch.setattr(elo_daemon, "load_bot_stats", load_stats)
    consumer = threading.Thread(target=consume, name="priority-consumer")
    writer = threading.Thread(
        target=publish_replacement,
        name="priority-writer",
    )
    consumer.start()
    assert criterion_entered.wait(5)
    writer.start()
    assert writer_started.wait(5)
    assert not writer_done.wait(0.2)

    release_criterion.set()
    consumer.join(5)
    writer.join(5)

    assert not errors
    assert not consumer.is_alive()
    assert not writer.is_alive()
    assert result["priority"] is None
    assert evolution_infra.read_locked_json(path) == {
        "bot": "national_v144",
        "min_games": 500,
    }


def test_unsatisfied_priority_remains_live_and_is_returned(tmp_path, monkeypatch):
    path = tmp_path / "priority_eval.json"
    payload = {"bot": "national_v143", "min_games": 10}
    evolution_infra.write_locked_json(path, payload)
    monkeypatch.setattr(elo_daemon, "PRIORITY_EVAL_FILE", path)
    monkeypatch.setattr(
        elo_daemon,
        "load_bot_stats",
        lambda: {"national_v143": {"games": 9}},
    )

    assert elo_daemon._load_priority_eval() == "national_v143"
    assert evolution_infra.read_locked_json(path) == payload


def test_reap_consumer_durably_unlinks_before_waiting_writer_publishes(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / ".reap_signal"
    path.write_text("1000.000000\n", encoding="utf-8")
    monkeypatch.setattr(elo_daemon.time, "time", lambda: 1001.0)

    unlink_fsync_entered = threading.Event()
    release_unlink_fsync = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    result = {}
    errors: list[BaseException] = []
    real_fsync_directory = evolution_infra._fsync_directory

    def paused_consumer_fsync(directory):
        if threading.current_thread().name == "reap-consumer":
            unlink_fsync_entered.set()
            assert release_unlink_fsync.wait(5)
        return real_fsync_directory(directory)

    def consume():
        try:
            result["fresh"] = elo_daemon._consume_reap_signal(path)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def publish_replacement():
        try:
            writer_started.set()
            with evolution_infra.locked_file(
                path,
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("1002.000000\n")
            writer_done.set()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    monkeypatch.setattr(
        evolution_infra,
        "_fsync_directory",
        paused_consumer_fsync,
    )
    consumer = threading.Thread(target=consume, name="reap-consumer")
    writer = threading.Thread(target=publish_replacement, name="reap-writer")
    consumer.start()
    assert unlink_fsync_entered.wait(5)
    assert not path.exists()
    writer.start()
    assert writer_started.wait(5)
    assert not writer_done.wait(0.2)

    release_unlink_fsync.set()
    consumer.join(5)
    writer.join(5)

    assert not errors
    assert not consumer.is_alive()
    assert not writer.is_alive()
    assert result["fresh"] is True
    assert path.read_text(encoding="utf-8") == "1002.000000\n"


def test_old_reap_signal_still_requests_crash_recovery_refresh(tmp_path):
    path = tmp_path / ".reap_signal"
    path.write_text("1000.000000\n", encoding="utf-8")

    assert elo_daemon._consume_reap_signal(path) is True
    assert not path.exists()


def test_locked_consumer_rejects_path_swap_without_unlinking_new_inode(tmp_path):
    path = tmp_path / "signal"
    retired = tmp_path / "retired-signal"
    path.write_text("old", encoding="utf-8")

    def swap_path(_raw):
        path.rename(retired)
        path.write_text("new", encoding="utf-8")
        return True

    with pytest.raises(OSError, match="opened safe regular file"):
        evolution_infra.read_and_maybe_unlink_locked_text(path, swap_path)

    assert path.read_text(encoding="utf-8") == "new"
    assert retired.read_text(encoding="utf-8") == "old"


def test_locked_consumer_unlink_failure_preserves_selected_inode(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "signal"
    path.write_text("old", encoding="utf-8")
    real_unlink = evolution_infra.os.unlink

    def denied(candidate, *args, **kwargs):
        if Path(candidate) == path:
            raise OSError("unlink denied")
        return real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(evolution_infra.os, "unlink", denied)
    with pytest.raises(OSError, match="unlink denied"):
        evolution_infra.read_and_maybe_unlink_locked_text(
            path,
            lambda _raw: True,
        )

    assert path.read_text(encoding="utf-8") == "old"


def test_locked_consumer_surfaces_parent_fsync_failure_and_releases_lock(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "signal"
    path.write_text("old", encoding="utf-8")
    real_fsync_directory = evolution_infra._fsync_directory

    def denied(_directory):
        raise OSError("unlink directory fsync denied")

    monkeypatch.setattr(evolution_infra, "_fsync_directory", denied)
    with pytest.raises(OSError, match="unlink directory fsync denied"):
        evolution_infra.read_and_maybe_unlink_locked_text(
            path,
            lambda _raw: True,
        )
    assert not path.exists()

    # The failed durability proof does not strand the stable sidecar lease; a
    # retrying producer can publish a new inode, which must remain live.
    monkeypatch.setattr(
        evolution_infra,
        "_fsync_directory",
        real_fsync_directory,
    )
    evolution_infra.write_locked_json(path, {"generation": "next"})
    assert evolution_infra.read_locked_json(path) == {"generation": "next"}


def test_state_helpers_reject_data_or_sidecar_hardlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("untouched", encoding="utf-8")
    data = tmp_path / "state.json"
    os.link(outside, data)
    with pytest.raises(OSError, match="single-link"):
        evolution_infra.write_locked_json(data, {"unsafe": True})
    assert outside.read_text(encoding="utf-8") == "untouched"

    data.unlink()
    lock_path = data.with_suffix(".json.lock")
    lock_path.unlink()
    os.link(outside, lock_path)
    with pytest.raises(OSError, match="regular file"):
        evolution_infra.write_locked_json(data, {"unsafe": True})
    assert not data.exists()
    assert outside.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize("mode", ["w", "w+"])
def test_locked_file_truncating_modes_do_not_damage_swapped_hardlink_victim(
    tmp_path,
    monkeypatch,
    mode,
):
    path = tmp_path / "state.json"
    path.write_text("old-state", encoding="utf-8")
    victim = tmp_path / "victim.txt"
    victim.write_text("must-survive", encoding="utf-8")
    lock_path = path.with_suffix(".json.lock")
    real_open = evolution_infra.os.open
    swapped = False

    def swap_before_writer_open(candidate, flags, *args, **kwargs):
        nonlocal swapped
        candidate_path = Path(candidate)
        access_mode = flags & os.O_ACCMODE
        if (
            not swapped
            and candidate_path.parent == tmp_path
            and candidate_path != lock_path
            and access_mode in {os.O_WRONLY, os.O_RDWR}
        ):
            # Reproduce the exact pre-open race: the preflight saw a safe
            # single-link target, then an attacker installs a victim hardlink
            # immediately before the writer opens its data descriptor.  The
            # former O_TRUNC open damaged victim before its post-open proof.
            path.unlink()
            os.link(victim, path)
            swapped = True
        return real_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(evolution_infra.os, "open", swap_before_writer_open)
    with pytest.raises(OSError, match="changed during atomic write"):
        with evolution_infra.locked_file(path, mode, encoding="utf-8") as handle:
            handle.write("replacement")

    assert swapped is True
    assert victim.read_text(encoding="utf-8") == "must-survive"
    assert path.read_text(encoding="utf-8") == "must-survive"
    assert os.stat(path).st_ino == os.stat(victim).st_ino
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_state_publication_surfaces_parent_fsync_failure(tmp_path, monkeypatch):
    path = tmp_path / "state.json"

    def denied(_path):
        raise OSError("directory fsync denied")

    monkeypatch.setattr(evolution_infra, "_fsync_directory", denied)
    with pytest.raises(OSError, match="directory fsync denied"):
        evolution_infra.write_locked_json(path, {"value": "published"})


def test_rating_rotation_never_truncates_append_only_authority(tmp_path):
    path = tmp_path / "match_history.jsonl"
    original = b"".join(
        json.dumps({"row": row}).encode("utf-8") + b"\n"
        for row in range(5)
    )
    path.write_bytes(original)

    receipt = elo_daemon._rotate_jsonl(path, max_lines=2)

    assert receipt == {
        "rotated": False,
        "reason": "append_only_authority_preserved",
        "path": str(path),
        "requested_max_lines": 2,
    }
    assert path.read_bytes() == original


def test_sidecar_exclusive_lock_is_reentrant_for_nested_reader(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "state.json"
    evolution_infra.write_locked_json(path, {"value": 1})
    lock_path = path.with_suffix(".json.lock")
    real_open = evolution_infra.os.open
    lock_opens = []

    def observed_open(candidate, flags, *args, **kwargs):
        if Path(candidate) == lock_path:
            lock_opens.append((candidate, flags))
        return real_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(evolution_infra.os, "open", observed_open)
    with evolution_infra._locked_state_sidecar(
        path,
        lock_type=fcntl.LOCK_EX,
    ):
        assert evolution_infra.read_locked_json(path) == {"value": 1}

    assert len(lock_opens) == 1


def test_sidecar_shared_to_exclusive_upgrade_fails_without_deadlock(tmp_path):
    path = tmp_path / "state.json"
    evolution_infra.write_locked_json(path, {"value": 1})

    with evolution_infra._locked_state_sidecar(
        path,
        lock_type=fcntl.LOCK_SH,
    ):
        with pytest.raises(OSError, match="cannot be upgraded"):
            with evolution_infra._locked_state_sidecar(
                path,
                lock_type=fcntl.LOCK_EX,
            ):
                raise AssertionError("unreachable")


def test_publication_linearization_uses_checkpoint_data_sidecar(monkeypatch):
    observed = []

    class Guard:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def guard(path, *, lock_type):
        observed.append((Path(path), lock_type))
        return Guard()

    monkeypatch.setattr(evolution_infra, "_locked_state_sidecar", guard)
    with evolution_infra._publication_checkpoint_linearization_lock():
        pass

    assert observed == [(Path(evolution_infra.PIPELINE_STATE_FILE), fcntl.LOCK_EX)]


def test_web_cost_total_does_not_double_count_copied_archive_ranges(
    tmp_path,
    monkeypatch,
):
    import web_ui

    costs = tmp_path / "llm_costs.jsonl"
    costs.write_text(
        '{"cost_usd":1.25}\n{"cost_usd":2.75}\n',
        encoding="utf-8",
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "cost_summary.json").write_text(
        '{"grand_total":4.0}',
        encoding="utf-8",
    )
    monkeypatch.setattr(web_ui, "_COSTS_FILE", costs)
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", archive)

    assert web_ui.WebUI._load_grand_cost() == 4.0


def test_sidecar_swap_is_detected_even_when_protected_body_raises(tmp_path):
    path = tmp_path / "state.json"
    evolution_infra.write_locked_json(path, {"value": 1})
    lock_path = path.with_suffix(".json.lock")
    retired = tmp_path / "retired.lock"

    with pytest.raises(OSError, match="opened safe regular file"):
        with evolution_infra._locked_state_sidecar(
            path,
            lock_type=fcntl.LOCK_EX,
        ):
            lock_path.rename(retired)
            lock_path.write_text("replacement", encoding="utf-8")
            raise ValueError("body failed")


def test_sidecar_hardlink_is_detected_on_exception_exit(tmp_path):
    path = tmp_path / "state.json"
    evolution_infra.write_locked_json(path, {"value": 1})
    lock_path = path.with_suffix(".json.lock")
    extra = tmp_path / "extra.lock"

    with pytest.raises(OSError, match="opened safe regular file"):
        with evolution_infra._locked_state_sidecar(
            path,
            lock_type=fcntl.LOCK_EX,
        ):
            os.link(lock_path, extra)
            raise ValueError("body failed")


def test_bot_publication_lock_swap_is_detected_on_body_error(tmp_path):
    lock_path = tmp_path / ".bot_publication.lock"
    retired = tmp_path / ".bot_publication.retired"

    with pytest.raises(OSError, match="inode changed"):
        with evolution_infra.bot_publication_lock(results_dir=tmp_path):
            lock_path.rename(retired)
            lock_path.write_text("replacement", encoding="utf-8")
            raise ValueError("body failed")


def test_append_detects_live_path_swap_during_write(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    path.write_text('{"old":true}\n', encoding="utf-8")
    retired = tmp_path / "retired.jsonl"
    real_write = evolution_infra.os.write
    swapped = False

    def swap_then_write(descriptor, payload):
        nonlocal swapped
        if not swapped:
            swapped = True
            path.rename(retired)
            path.write_text('{"replacement":true}\n', encoding="utf-8")
        return real_write(descriptor, payload)

    monkeypatch.setattr(evolution_infra.os, "write", swap_then_write)
    with pytest.raises(OSError, match="changed during write"):
        evolution_infra.append_locked_jsonl(path, {"new": True})

    assert path.read_text(encoding="utf-8") == '{"replacement":true}\n'
    assert '"new": true' in retired.read_text(encoding="utf-8")


def test_atomic_publication_binds_the_exact_temporary_inode(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "state.json"
    path.write_text('{"old":true}', encoding="utf-8")
    stolen = tmp_path / "stolen.json"
    real_replace = evolution_infra.os.replace

    def replace_then_swap(source, target):
        real_replace(source, target)
        Path(target).rename(stolen)
        Path(target).write_text('{"attacker":true}', encoding="utf-8")

    monkeypatch.setattr(evolution_infra.os, "replace", replace_then_swap)
    with pytest.raises(OSError, match="temporary inode"):
        evolution_infra.write_locked_json(path, {"new": True})

    assert path.read_text(encoding="utf-8") == '{"attacker":true}'
