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
