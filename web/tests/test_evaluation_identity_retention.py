import hashlib
import json
from pathlib import Path

import evaluation_data_identity as identity
import evaluation_identity_retention as retention


def _digest_for(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def _make_snapshot(archive: Path, name: str, *, digest: str | None = None) -> Path:
    snapshot = archive / name
    snapshot.mkdir(parents=True)
    payload_digest = digest if digest is not None else _digest_for(name)
    (snapshot / "evaluation_data_manifest.json").write_text(
        json.dumps({"schema_version": 4, "manifest_digest": payload_digest}),
        encoding="utf-8",
    )
    (snapshot / "migration.json").write_text(
        json.dumps({"reason": "fixture"}), encoding="utf-8"
    )
    (snapshot / "glicko_ratings.json").write_text("{}\n", encoding="utf-8")
    return snapshot


def _snapshot_names(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if retention.SNAPSHOT_NAME_RE.fullmatch(entry.name)
    )


def _stamp(index: int) -> str:
    """Distinct, chronologically sortable snapshot names."""

    day = 1 + index // 20
    return f"202609{day:02d}_{(index % 20) * 100 + 1:06d}"


def _load_receipt(path: Path) -> dict:
    body = json.loads(path.read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in body.items() if key != "receipt_digest"}
    assert body["receipt_digest"] == identity.canonical_digest(unsigned)
    return body


def test_more_than_keep_retires_oldest_to_staging_area(tmp_path):
    results = tmp_path / "results"
    archive = retention.archive_dir(results)
    archive.mkdir(parents=True)
    names = [_stamp(index) for index in range(retention.KEEP_RECENT_SNAPSHOTS + 3)]
    for name in names:
        _make_snapshot(archive, name)

    report = retention.run_evaluation_identity_retention(results)

    assert report["ok"] is True
    # Newest 20 stay in the live archive; the 3 oldest moved to staging.
    assert _snapshot_names(archive) == names[3:]
    assert _snapshot_names(retention.retired_dir(results)) == names[:3]
    assert sorted(item["name"] for item in report["moved"]) == names[:3]
    # Nothing was deleted: staged trees keep their payloads.
    for name in names[:3]:
        staged = retention.retired_dir(results) / name
        assert (staged / "evaluation_data_manifest.json").is_file()
        assert (staged / "glicko_ratings.json").is_file()


def test_receipt_records_the_retired_manifest(tmp_path):
    results = tmp_path / "results"
    archive = retention.archive_dir(results)
    archive.mkdir(parents=True)
    names = [_stamp(index) for index in range(retention.KEEP_RECENT_SNAPSHOTS + 2)]
    for name in names:
        _make_snapshot(archive, name)

    report = retention.run_evaluation_identity_retention(results)

    receipt = _load_receipt(Path(report["receipt_path"]))
    assert receipt["schema_version"] == retention.RECEIPT_SCHEMA_VERSION
    assert receipt["kind"] == "evaluation_identity_retention"
    assert receipt["keep"] == retention.KEEP_RECENT_SNAPSHOTS
    assert sorted(item["name"] for item in receipt["moved"]) == names[:2]
    for item in receipt["moved"]:
        assert item["manifest_digest"] == _digest_for(item["name"])
        assert item["bytes"] > 0
    assert receipt["kept_window"] == names[2:]
    assert receipt["failed"] == []
    assert receipt["bytes_staged"] == sum(
        item["bytes"] for item in receipt["moved"]
    )


def test_at_most_keep_snapshots_move_nothing(tmp_path):
    results = tmp_path / "results"
    archive = retention.archive_dir(results)
    archive.mkdir(parents=True)
    names = [_stamp(index) for index in range(retention.KEEP_RECENT_SNAPSHOTS)]
    for name in names:
        _make_snapshot(archive, name)

    report = retention.run_evaluation_identity_retention(results)

    assert report["ok"] is True
    assert report["moved"] == []
    assert _snapshot_names(archive) == names
    assert _snapshot_names(retention.retired_dir(results)) == []
    receipt = _load_receipt(Path(report["receipt_path"]))
    assert receipt["moved"] == []
    assert receipt["kept_window"] == names


def test_live_cycle_manifest_reference_protects_old_snapshot(tmp_path):
    results = tmp_path / "results"
    archive = retention.archive_dir(results)
    archive.mkdir(parents=True)
    names = [_stamp(index) for index in range(retention.KEEP_RECENT_SNAPSHOTS + 2)]
    for name in names:
        _make_snapshot(archive, name)
    # The live published evaluation cycle still binds the OLDEST archived
    # identity — that snapshot must stay in the live archive.
    protected_digest = _digest_for(names[0])
    (results / "evaluation_cycle_manifest.json").write_text(
        json.dumps({"evaluation_identity_digest": protected_digest}),
        encoding="utf-8",
    )

    report = retention.run_evaluation_identity_retention(results)

    assert report["ok"] is True
    assert _snapshot_names(archive) == [names[0], *names[2:]]
    assert _snapshot_names(retention.retired_dir(results)) == [names[1]]
    protections = {item["name"]: item for item in report["protected"]}
    assert protections[names[0]]["reason"] == "live_reference"
    assert protections[names[0]]["manifest_digest"] == protected_digest


def test_live_checkpoint_token_protects_old_snapshot(tmp_path):
    results = tmp_path / "results"
    archive = retention.archive_dir(results)
    archive.mkdir(parents=True)
    names = [_stamp(index) for index in range(retention.KEEP_RECENT_SNAPSHOTS + 1)]
    for name in names:
        _make_snapshot(archive, name)
    protected_digest = _digest_for(names[0])
    (results / "pipeline_state.json").write_text(
        json.dumps({
            "schema_version": 2,
            "workflow_run_id": "workflow-v1",
            "evaluation_identity_digest": protected_digest,
        }),
        encoding="utf-8",
    )

    report = retention.run_evaluation_identity_retention(results)

    assert report["moved"] == []
    assert _snapshot_names(archive) == names
    assert _snapshot_names(retention.retired_dir(results)) == []
    protections = {item["name"]: item for item in report["protected"]}
    assert protections[names[0]]["reason"] == "live_reference"


def test_unreadable_snapshot_manifest_protects_that_snapshot(tmp_path):
    results = tmp_path / "results"
    archive = retention.archive_dir(results)
    archive.mkdir(parents=True)
    names = [_stamp(index) for index in range(retention.KEEP_RECENT_SNAPSHOTS + 2)]
    for name in names:
        _make_snapshot(archive, name)
    (archive / names[0] / "evaluation_data_manifest.json").write_text(
        "{not json", encoding="utf-8"
    )

    report = retention.run_evaluation_identity_retention(results)

    assert report["ok"] is True
    assert _snapshot_names(archive) == [names[0], *names[2:]]
    assert _snapshot_names(retention.retired_dir(results)) == [names[1]]
    protections = {item["name"]: item for item in report["protected"]}
    assert protections[names[0]]["reason"] == "snapshot_manifest_unreadable"


def test_unreadable_live_reference_surface_fails_closed(tmp_path):
    results = tmp_path / "results"
    archive = retention.archive_dir(results)
    archive.mkdir(parents=True)
    for index in range(retention.KEEP_RECENT_SNAPSHOTS + 2):
        _make_snapshot(archive, _stamp(index))
    (results / "evaluation_cycle_manifest.json").write_text(
        "{not json", encoding="utf-8"
    )

    report = retention.run_evaluation_identity_retention(results)

    assert report["ok"] is False
    assert report["moved"] == []
    # Nothing moved: the pass failed before touching any snapshot.
    assert _snapshot_names(retention.retired_dir(results)) == []
    assert (
        len(_snapshot_names(archive)) == retention.KEEP_RECENT_SNAPSHOTS + 2
    )


def test_non_snapshot_entries_are_ignored_not_moved(tmp_path):
    results = tmp_path / "results"
    archive = retention.archive_dir(results)
    archive.mkdir(parents=True)
    for index in range(3):
        _make_snapshot(archive, _stamp(index))
    stray = archive / "notes.txt"
    stray.write_text("operator note\n", encoding="utf-8")

    report = retention.run_evaluation_identity_retention(results, keep=2)

    assert report["ok"] is True
    assert stray.is_file()
    assert len(report["moved"]) == 1


def test_symlinked_retired_directory_fails_closed(tmp_path):
    results = tmp_path / "results"
    archive = retention.archive_dir(results)
    archive.mkdir(parents=True)
    for index in range(retention.KEEP_RECENT_SNAPSHOTS + 1):
        _make_snapshot(archive, _stamp(index))
    outside = tmp_path / "outside"
    outside.mkdir()
    (results / "archive").mkdir(parents=True, exist_ok=True)
    (results / "archive" / retention.RETIRED_DIRNAME).symlink_to(
        outside, target_is_directory=True
    )

    report = retention.run_evaluation_identity_retention(results)

    assert report["ok"] is False
    assert report["moved"] == []
    assert len(_snapshot_names(archive)) == retention.KEEP_RECENT_SNAPSHOTS + 1
    assert list(outside.iterdir()) == []


def test_archive_and_initialize_no_longer_runs_retention(tmp_path):
    # T6 rework 2026-10-15: evaluation_data_identity.py sits inside the rating
    # SEMANTIC_PATHS identity set, so the retention hook moved off it onto the
    # disk-hygiene janitor. The migration itself must not touch retention —
    # a snapshot over budget stays archived until the janitor's next pass.
    results = tmp_path / "results"
    results.mkdir()
    (results / "glicko_ratings.json").write_text(
        '{"national_v1": {}}\n', encoding="utf-8"
    )
    archive = retention.archive_dir(results)
    archive.mkdir(parents=True)
    old_names = [_stamp(index) for index in range(retention.KEEP_RECENT_SNAPSHOTS)]
    for name in old_names:
        _make_snapshot(archive, name)

    migrated = identity.archive_and_initialize(results, reason="test migration")

    new_name = Path(migrated["archive_dir"]).name
    assert "retention" not in migrated
    live_names = _snapshot_names(archive)
    assert live_names[-1] == new_name
    assert old_names[0] in live_names
    assert len(live_names) == retention.KEEP_RECENT_SNAPSHOTS + 1
    assert not retention.retired_dir(results).exists()
    assert (results / identity.MANIFEST_NAME).is_file()


def test_disk_hygiene_janitor_wires_retention(tmp_path, monkeypatch):
    # T6 rework: the hook must live on a non-identity carrier. The janitor
    # runs the pass only when the archive exceeds the keep budget, under the
    # migration writer's exclusive locks.
    import contextlib

    import disk_hygiene

    results = tmp_path / "results"
    results.mkdir()
    calls: list[dict] = []

    def _fake_run(results_dir, *, triggered_by="", keep=None):
        calls.append({"triggered_by": triggered_by})
        return {"ok": True, "moved": ["x"], "receipt_path": "r"}

    import evaluation_bundle
    import evaluation_data_identity

    monkeypatch.setattr(retention, "run_evaluation_identity_retention", _fake_run)
    archive = retention.archive_dir(results)
    for index in range(retention.KEEP_RECENT_SNAPSHOTS + 1):
        (archive / _stamp(index)).mkdir(parents=True, exist_ok=True)

    lock_calls: list[str] = []

    @contextlib.contextmanager
    def _fake_cycle_lock(root, *, exclusive=False):
        lock_calls.append("cycle")
        yield

    @contextlib.contextmanager
    def _fake_manifest_lock(root):
        lock_calls.append("manifest")
        yield

    monkeypatch.setattr(evaluation_bundle, "evaluation_cycle_lock", _fake_cycle_lock)
    monkeypatch.setattr(evaluation_data_identity, "_manifest_lock", _fake_manifest_lock)

    report = disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)

    assert calls == [{"triggered_by": "disk_hygiene_janitor"}]
    assert lock_calls == ["cycle", "manifest"]
    assert report["evaluation_identity_retention"]["moved"] == 1


def test_disk_hygiene_janitor_skips_within_budget(tmp_path):
    import disk_hygiene

    results = tmp_path / "results"
    results.mkdir()
    archive = retention.archive_dir(results)
    for index in range(retention.KEEP_RECENT_SNAPSHOTS):
        (archive / _stamp(index)).mkdir(parents=True, exist_ok=True)

    report = disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)

    assert "evaluation_identity_retention" not in report
    assert not retention.retired_dir(results).exists()
