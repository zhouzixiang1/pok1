"""Disk janitor: reap non-authority runtime artifacts, never touch ledgers."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import disk_hygiene


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _touch_tree(path: Path, name: str = "blob.bin", size: int = 64) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_bytes(b"x" * size)


def test_hygiene_does_not_touch_authority_ledgers(tmp_path: Path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "pipeline_state.json").write_text(
        json.dumps({"next_v": 276, "source_v": 1, "stage": "selected"}),
        encoding="utf-8",
    )
    (results / "events.jsonl").write_text("{" + '"x":1}\n' * 50, encoding="utf-8")
    (results / "abandoned_versions.jsonl").write_text(
        json.dumps({"version": 200, "reason": "x"}) + "\n",
        encoding="utf-8",
    )
    (results / "match_history.jsonl").write_text("{}\n", encoding="utf-8")
    before = {
        name: (results / name).read_bytes()
        for name in (
            "pipeline_state.json",
            "events.jsonl",
            "abandoned_versions.jsonl",
            "match_history.jsonl",
        )
    }

    report = disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)

    assert report["ok"] is True
    for name, raw in before.items():
        assert (results / name).read_bytes() == raw


def test_hygiene_prunes_saturator_sessions_and_findings(tmp_path: Path):
    results = tmp_path / "results"
    sat = results / "saturator"
    sat.mkdir(parents=True)
    now = time.time()
    for i in range(5):
        path = sat / f"session_{i:05d}.txt"
        path.write_text(f"s{i}", encoding="utf-8")
        os.utime(path, (now - (5 - i) * 10, now - (5 - i) * 10))
    (sat / "findings.jsonl").write_text(
        "\n".join(f'{{"n":{i}}}' for i in range(20)) + "\n",
        encoding="utf-8",
    )

    report = disk_hygiene.run_disk_hygiene(
        results,
        keep_saturator_sessions=2,
        keep_findings_lines=3,
        min_free_bytes=1,
    )

    leftover = sorted(p.name for p in sat.glob("session_*.txt"))
    assert leftover == ["session_00003.txt", "session_00004.txt"]
    findings = (sat / "findings.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert findings == ['{"n":17}', '{"n":18}', '{"n":19}']
    assert report["removed"] >= 3
    assert report["bytes_freed"] > 0


def test_hygiene_reaps_orphan_drafts_behind_high_water(tmp_path: Path):
    results = tmp_path / "results"
    bots = tmp_path / "bots"
    results.mkdir()
    published = bots / "national_cloud_v188"
    published.mkdir(parents=True)
    (published / ".completed").write_text("ok", encoding="utf-8")

    _write_json(
        results / "pipeline_state_draft1.json",
        {"next_v": 184, "source_v": 27, "stage": "prepared"},
    )
    _touch_tree(results / "draft_candidates" / "draft1" / "national_cloud_v184")
    orphan = results / "draft_candidates" / "draft2" / "orphan"
    _touch_tree(orphan)
    old = time.time() - 7200
    os.utime(results / "draft_candidates" / "draft2", (old, old))
    os.utime(orphan, (old, old))

    # A live-ahead draft must survive.
    _write_json(
        results / "pipeline_state_draft3.json",
        {"next_v": 200, "source_v": 188, "stage": "prepared"},
    )
    _touch_tree(results / "draft_candidates" / "draft3" / "national_cloud_v200")
    fresh_orphan = results / "draft_candidates" / "fresh_slot" / "wip"
    _touch_tree(fresh_orphan)

    disk_hygiene.run_disk_hygiene(results, bots_dir=bots, min_free_bytes=1)

    assert not (results / "pipeline_state_draft1.json").exists()
    assert not (results / "draft_candidates" / "draft1").exists()
    assert not (results / "draft_candidates" / "draft2").exists()
    assert (results / "pipeline_state_draft3.json").exists()
    assert (results / "draft_candidates" / "draft3" / "national_cloud_v200").exists()
    # A brand-new unnamed slot is not reaped until it ages out.
    assert (results / "draft_candidates" / "fresh_slot").exists()


def test_hygiene_keeps_live_and_published_result_trees(tmp_path: Path):
    results = tmp_path / "results"
    bots = tmp_path / "bots"
    results.mkdir()
    (bots / "national_cloud_v1").mkdir(parents=True)
    (bots / "national_cloud_v1" / ".completed").write_text("ok", encoding="utf-8")
    _write_json(
        results / "pipeline_state.json",
        {"next_v": 276, "source_v": 1, "stage": "selected"},
    )
    ledger = results / "abandoned_versions.jsonl"
    rows = [{"version": n, "reason": "x"} for n in range(200, 210)]
    ledger.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    for n in list(range(200, 210)) + [1, 276]:
        _touch_tree(results / f"v{n}")

    disk_hygiene.run_disk_hygiene(
        results,
        bots_dir=bots,
        keep_abandoned_result_dirs=3,
        min_free_bytes=1,
    )

    assert (results / "v1").exists()
    assert (results / "v276").exists()
    # keep the three highest abandoned versions
    assert (results / "v209").exists()
    assert (results / "v208").exists()
    assert (results / "v207").exists()
    assert not (results / "v200").exists()
    assert not (results / "v204").exists()


def test_hygiene_drops_stale_consumer_checkpoints(tmp_path: Path):
    results = tmp_path / "results"
    bots = tmp_path / "bots"
    results.mkdir()
    (bots / "national_cloud_v188").mkdir(parents=True)
    (bots / "national_cloud_v188" / ".completed").write_text("ok", encoding="utf-8")
    _write_json(
        results / "pipeline_state.json",
        {"next_v": 276, "source_v": 1, "parent2_v": 173},
    )
    stale = results / "pipeline_state_consumer-candidate-v176.json"
    _write_json(stale, {"next_v": 176, "stage": "quality_failed"})
    live = results / "pipeline_state_consumer-candidate-v276.json"
    _write_json(live, {"next_v": 276, "stage": "quality_running"})

    disk_hygiene.run_disk_hygiene(results, bots_dir=bots, min_free_bytes=1)

    assert not stale.exists()
    assert live.exists()


def test_hygiene_skips_symlinks(tmp_path: Path):
    results = tmp_path / "results"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret"
    target.write_text("keep", encoding="utf-8")
    results.mkdir()
    link = results / "v200"
    os.symlink(outside, link)
    ledger = results / "abandoned_versions.jsonl"
    ledger.write_text(json.dumps({"version": 200}) + "\n", encoding="utf-8")

    disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)

    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"


def test_hygiene_loop_respects_disable_flag(monkeypatch):
    seen = []

    async def scenario():
        monkeypatch.setenv("POK_DISK_HYGIENE_ENABLED", "0")
        await disk_hygiene.run_disk_hygiene_loop(shutdown_mgr=None)
        seen.append("returned")

    import asyncio

    asyncio.run(scenario())
    assert seen == ["returned"]
