def test_cleanup_old_replays_never_deletes_dot_caches(monkeypatch, tmp_path):
    import elo_daemon

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
    import json
    import elo_daemon

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
