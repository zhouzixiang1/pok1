import json
import os
import time

import elo_daemon


def _aged(path, days_old: float) -> None:
    stamp = time.time() - days_old * 24.0 * 3600.0
    os.utime(path, (stamp, stamp))


def test_cleanup_old_replays_never_deletes_dot_caches(monkeypatch, tmp_path):
    replay_dir = tmp_path / "match_replay"
    replay_dir.mkdir()
    for name in (
        ".behavior_acc.json",
        ".behavior_etag.json",
        ".stats_etag.json",
        "20260712_000001_a_vs_b.json",
        "20260712_000002_a_vs_b.json",
        "20260712_000003_a_vs_b.json",
    ):
        (replay_dir / name).write_text("{}", encoding="utf-8")

    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(
        elo_daemon, "MATCH_HISTORY_FILE", tmp_path / "match_history.jsonl"
    )
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "MAX_REPLAY_FILES", 2)
    elo_daemon.cleanup_old_replays()

    assert not (replay_dir / "20260712_000001_a_vs_b.json").exists()
    assert (replay_dir / "20260712_000002_a_vs_b.json").exists()
    assert (replay_dir / "20260712_000003_a_vs_b.json").exists()
    assert (replay_dir / ".behavior_acc.json").exists()
    assert (replay_dir / ".behavior_etag.json").exists()
    assert (replay_dir / ".stats_etag.json").exists()


def test_cleanup_old_replays_retains_bytes_referenced_by_match_history(
    monkeypatch, tmp_path
):
    replay_dir = tmp_path / "match_replay"
    replay_dir.mkdir()
    old = replay_dir / "20260712_000001_a_vs_b.json"
    newer = replay_dir / "20260712_000002_a_vs_b.json"
    old.write_text("{}", encoding="utf-8")
    newer.write_text("{}", encoding="utf-8")
    history = tmp_path / "match_history.jsonl"
    history.write_text(
        json.dumps({"id": old.name}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "MATCH_HISTORY_FILE", history)
    monkeypatch.setattr(elo_daemon, "MAX_REPLAY_FILES", 1)
    elo_daemon.cleanup_old_replays()

    assert old.exists()
    assert not newer.exists()


def _read_reaped(tmp_path):
    ledger = tmp_path / "reaped_replays.jsonl"
    if not ledger.is_file():
        return []
    return [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_cleanup_deletes_aged_true_orphan_even_under_cap(
    monkeypatch, tmp_path
):
    """R1 (post-review): the reference set keeps its full-pin semantics; the
    only widening is that a TRUE orphan (no live-history row, no cycle row,
    not pinned by a verified evidence snapshot) is deleted even when the
    directory is below MAX_REPLAY_FILES, once it is older than the
    write-before-append race window."""
    replay_dir = tmp_path / "match_replay"
    replay_dir.mkdir()
    referenced = replay_dir / "20260712_000001_a_vs_b.json"
    orphan = replay_dir / "20260711_000001_x_vs_y.json"
    referenced.write_text("{}", encoding="utf-8")
    orphan.write_text("{}", encoding="utf-8")
    _aged(orphan, days_old=2.0)
    history = tmp_path / "match_history.jsonl"
    history.write_text(
        json.dumps({"id": referenced.name}) + "\n", encoding="utf-8"
    )

    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "MATCH_HISTORY_FILE", history)
    monkeypatch.setattr(elo_daemon, "MAX_REPLAY_FILES", 10)
    elo_daemon.cleanup_old_replays()

    assert referenced.exists()
    assert not orphan.exists()
    rows = _read_reaped(tmp_path)
    assert [row["id"] for row in rows] == [orphan.name]
    assert rows[0]["reason"] == "unreferenced_orphan_aged_out"
    assert rows[0]["bytes"] == 2


def test_cleanup_keeps_fresh_orphan_inside_write_race_window(
    monkeypatch, tmp_path
):
    """save_match_replay writes the replay file BEFORE appending its history
    row; a fresh unreferenced file may simply be inside that window."""
    replay_dir = tmp_path / "match_replay"
    replay_dir.mkdir()
    fresh_orphan = replay_dir / "20260712_000009_a_vs_b.json"
    fresh_orphan.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(
        elo_daemon, "MATCH_HISTORY_FILE", tmp_path / "match_history.jsonl"
    )
    monkeypatch.setattr(elo_daemon, "MAX_REPLAY_FILES", 10)
    elo_daemon.cleanup_old_replays()

    assert fresh_orphan.exists()
    assert _read_reaped(tmp_path) == []


def test_cleanup_retains_orphan_referenced_by_retained_cycle_copy(
    monkeypatch, tmp_path
):
    from evaluation_bundle import CYCLES_DIRNAME

    replay_dir = tmp_path / "match_replay"
    replay_dir.mkdir()
    pinned = replay_dir / "20260710_000001_m_vs_n.json"
    pinned.write_text("{}", encoding="utf-8")
    _aged(pinned, days_old=30.0)
    cycle_history = (
        tmp_path / CYCLES_DIRNAME / "cycle-0001" / "match_history.jsonl"
    )
    cycle_history.parent.mkdir(parents=True)
    cycle_history.write_text(
        json.dumps({"id": pinned.name}) + "\n", encoding="utf-8"
    )

    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(
        elo_daemon, "MATCH_HISTORY_FILE", tmp_path / "match_history.jsonl"
    )
    monkeypatch.setattr(elo_daemon, "MAX_REPLAY_FILES", 10)
    elo_daemon.cleanup_old_replays()

    assert pinned.exists()
    assert _read_reaped(tmp_path) == []


def test_cleanup_cap_deletion_is_receipt_first(monkeypatch, tmp_path):
    replay_dir = tmp_path / "match_replay"
    replay_dir.mkdir()
    old = replay_dir / "20260712_000001_a_vs_b.json"
    newer = replay_dir / "20260712_000002_a_vs_b.json"
    old.write_text("{}", encoding="utf-8")
    newer.write_text("{}", encoding="utf-8")
    history = tmp_path / "match_history.jsonl"
    history.write_text(json.dumps({"id": old.name}) + "\n", encoding="utf-8")

    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "MATCH_HISTORY_FILE", history)
    monkeypatch.setattr(elo_daemon, "MAX_REPLAY_FILES", 1)

    def _boom(path, entry):
        raise OSError("ledger append failed")

    monkeypatch.setattr(elo_daemon, "append_locked_jsonl", _boom)
    elo_daemon.cleanup_old_replays()
    # Receipt-first: a deletion whose receipt cannot be durably appended
    # keeps the file.
    assert old.exists()
    assert newer.exists()
