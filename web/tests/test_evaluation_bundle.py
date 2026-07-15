import fcntl
import json
import os
import threading
import time

import pytest


def _write_json(path, payload):
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _patch_results(monkeypatch, tmp_path):
    import evaluation_data_identity
    import evolution_infra

    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "H2H_FILE", results / "head_to_head.json")
    monkeypatch.setattr(evolution_infra, "BOT_STATS_FILE", results / "bot_stats.json")
    monkeypatch.setattr(evolution_infra, "RATINGS_FILE", results / "glicko_ratings.json")
    monkeypatch.setattr(evolution_infra, "STATS_FILE", results / "elo_daemon_stats.json")
    monkeypatch.setattr(evolution_infra, "MATCH_HISTORY_FILE", results / "match_history.jsonl")
    monkeypatch.setattr(evolution_infra, "RATING_HISTORY_FILE", results / "rating_history.jsonl")
    identity_manifest = evaluation_data_identity.ensure_evaluation_data_identity(results)
    assert identity_manifest["schema_version"] == 4
    return results, identity_manifest


def _write_cycle_files(
    results,
    active,
    *,
    save_num=1,
    h2h=None,
    daemon_stats=None,
    match_history_rows=(),
    rating_history_rows=(),
):
    active = sorted(active)
    identity_payload = json.loads(
        (results / "evaluation_data_manifest.json").read_text(encoding="utf-8")
    )
    identity_digest = identity_payload["manifest_digest"]

    def identity_bound(row):
        payload = dict(row)
        payload.setdefault("evaluation_epoch", "national_tcp_policy_v1")
        payload.setdefault("execution_mode", "native_tcp")
        payload.setdefault("evaluation_identity_digest", identity_digest)
        return payload

    _write_json(results / "head_to_head.json", h2h or {})
    _write_json(
        results / "bot_stats.json",
        {name: {"games": 20, "wins": 10, "losses": 10, "win_rate": 0.5} for name in active},
    )
    _write_json(
        results / "glicko_ratings.json",
        {
            name: {"r": 1500.0, "rd": 90.0, "sigma": 0.06}
            for name in active
        },
    )
    _write_json(
        results / "selection_snapshot.json",
        {
            "schema_version": 1,
            "save_num": save_num,
            "daemon_run_id": "test-run",
            "active_bots": active,
            "rows": [
                {
                    "name": name,
                    "selection_score": 0.5,
                    "leaderboard_score": 0.5,
                    "h2h_coverage": 1.0,
                    "h2h_opponents": max(0, len(active) - 1),
                    "h2h_opponents_total": max(0, len(active) - 1),
                }
                for name in active
            ],
            "rating_history_tail": [],
        },
    )
    _write_json(
        results / "elo_daemon_stats.json",
        daemon_stats
        or {
            "save_num": save_num,
            "total_games": 20 if len(active) > 1 else 0,
            "pairs": {},
        },
    )
    _write_jsonl(
        results / "match_history.jsonl",
        [identity_bound(row) for row in match_history_rows],
    )
    _write_jsonl(
        results / "rating_history.jsonl",
        [identity_bound(row) for row in rating_history_rows],
    )


def _publish(results, active, *, save_num=1, **kwargs):
    from evaluation_bundle import publish_evaluation_cycle_manifest

    return publish_evaluation_cycle_manifest(
        save_num=save_num,
        daemon_run_id="test-run",
        active_bots=active,
        results_dir=results,
        _test_only_allow_unleased=True,
        **kwargs,
    )


def test_cycle_manifest_binds_all_authoritative_payloads_and_append_logs(
    monkeypatch, tmp_path
):
    from evaluation_bundle import (
        APPEND_LOGS,
        BUNDLE_FILES,
        load_published_evaluation_bundle,
    )

    results, identity_manifest = _patch_results(monkeypatch, tmp_path)
    active = ["national_v1", "national_v2"]
    shared_identity = {
        "evaluation_epoch": "national_tcp_policy_v1",
        "execution_mode": "native_tcp",
        "evaluation_identity_digest": identity_manifest["manifest_digest"],
    }
    match_row = {
        "id": "match-1",
        "bot0": active[0],
        "bot1": active[1],
        **shared_identity,
    }
    rating_row = {"period": 9, "daemon_run_id": "test-run", **shared_identity}
    _write_cycle_files(
        results,
        active,
        save_num=9,
        daemon_stats={"total_games": 20, "pairs": {"national_v1 vs national_v2": 20}},
        match_history_rows=[match_row],
        rating_history_rows=[rating_row],
    )
    manifest = _publish(results, active, save_num=9)

    bundle = load_published_evaluation_bundle(results)

    assert bundle["available"] is True
    assert bundle["manifest_digest"] == manifest["manifest_digest"]
    assert bundle["manifest"]["save_num"] == 9
    assert bundle["manifest"]["evaluation_identity_digest"] == identity_manifest["manifest_digest"]
    assert sorted(bundle["ratings"]) == active
    assert [row["name"] for row in bundle["selection"]["rows"]] == active
    assert bundle["daemon_stats"]["total_games"] == 20
    assert set(bundle["raw_files"]) == set(BUNDLE_FILES)
    assert set(bundle["raw_append_logs"]) == set(APPEND_LOGS)
    assert json.loads(bundle["raw_append_logs"]["match_history"]) == match_row
    assert json.loads(bundle["raw_append_logs"]["rating_history"]) == rating_row

    cycle_dir = results / manifest["cycle_dir"]
    assert cycle_dir.is_dir()
    payload_names = {
        *BUNDLE_FILES.values(),
        *APPEND_LOGS.values(),
    }
    observed_names = {path.name for path in cycle_dir.iterdir()}
    # Stable ``<payload>.lock`` sidecars are implementation-owned lock
    # authority. They may be materialized by a reader, but no unrelated file
    # is allowed to enter the immutable cycle directory.
    assert payload_names <= observed_names
    assert observed_names <= payload_names | {
        f"{name}.lock" for name in payload_names
    }


def test_uncommitted_alias_and_log_writes_do_not_advance_cycle_and_recovery_repairs_them(
    monkeypatch, tmp_path
):
    from evaluation_bundle import (
        APPEND_LOGS,
        BUNDLE_FILES,
        load_published_evaluation_bundle,
        recover_published_evaluation_bundle,
    )

    results, _identity = _patch_results(monkeypatch, tmp_path)
    active = ["national_v1", "national_v2"]
    _write_cycle_files(
        results,
        active,
        h2h={
            "national_v1 vs national_v2": {
                "games": 1,
                "a_wins": 1,
                "b_wins": 0,
                "draws": 0,
            }
        },
        match_history_rows=[{"id": "committed-match"}],
        rating_history_rows=[{"period": 1}],
    )
    _publish(results, active)
    committed = load_published_evaluation_bundle(results)

    # Model a crash after aliases and append-only logs started the next save but
    # before the immutable pointer advanced.
    for filename in BUNDLE_FILES.values():
        (results / filename).write_text('{"uncommitted": true}', encoding="utf-8")
    for filename in APPEND_LOGS.values():
        with (results / filename).open("ab") as stream:
            stream.write(b'{"uncommitted": true}\n')

    still_committed = load_published_evaluation_bundle(results)
    assert still_committed["available"] is True
    assert still_committed["manifest_digest"] == committed["manifest_digest"]
    assert still_committed["h2h"]["national_v1 vs national_v2"]["games"] == 1

    recovered = recover_published_evaluation_bundle(results)
    assert recovered["available"] is True
    for role, filename in BUNDLE_FILES.items():
        assert (results / filename).read_bytes() == committed["raw_files"][role]
    for role, filename in APPEND_LOGS.items():
        assert (results / filename).read_bytes() == committed["raw_append_logs"][role]


def test_immutable_cycle_payload_tampering_fails_closed(monkeypatch, tmp_path):
    from evaluation_bundle import load_published_evaluation_bundle

    results, _identity = _patch_results(monkeypatch, tmp_path)
    active = ["national_v1"]
    _write_cycle_files(results, active)
    manifest = _publish(results, active)
    cycle_stats = results / manifest["cycle_dir"] / "elo_daemon_stats.json"
    cycle_stats.write_text('{"total_games": 999}', encoding="utf-8")

    bundle = load_published_evaluation_bundle(results)

    assert bundle["available"] is False
    assert bundle["reason"] == "cycle_bundle_integrity_failure"
    assert "cycle_payload_daemon_stats_digest_mismatch" in bundle["issues"]


def test_compare_and_swap_rejects_stale_predecessor_without_advancing_pointer(
    monkeypatch, tmp_path
):
    from evaluation_bundle import load_published_evaluation_bundle

    results, _identity = _patch_results(monkeypatch, tmp_path)
    active = ["national_v1", "national_v2"]
    _write_cycle_files(results, active, save_num=1)
    first = _publish(results, active, save_num=1)

    _write_cycle_files(results, active, save_num=2, daemon_stats={"total_games": 2})
    second = _publish(
        results,
        active,
        save_num=2,
        expected_previous_manifest_digest=first["manifest_digest"],
        expected_previous_save_num=1,
        require_predecessor_match=True,
    )

    _write_cycle_files(results, active, save_num=3, daemon_stats={"total_games": 999})
    with pytest.raises(ValueError, match="predecessor digest changed"):
        _publish(
            results,
            active,
            save_num=3,
            expected_previous_manifest_digest=first["manifest_digest"],
            expected_previous_save_num=1,
            require_predecessor_match=True,
        )

    bundle = load_published_evaluation_bundle(results)
    assert bundle["available"] is True
    assert bundle["manifest_digest"] == second["manifest_digest"]
    assert bundle["manifest"]["save_num"] == 2
    assert bundle["daemon_stats"]["total_games"] == 2


@pytest.mark.parametrize("identity_failure", ["missing", "corrupt", "base_mismatch"])
def test_cycle_reader_rejects_missing_corrupt_or_base_mismatched_identity(
    monkeypatch, tmp_path, identity_failure
):
    import evaluation_data_identity
    from evaluation_bundle import load_published_evaluation_bundle

    results, _identity = _patch_results(monkeypatch, tmp_path)
    active = ["national_v1"]
    _write_cycle_files(results, active)
    _publish(results, active)
    identity_path = results / evaluation_data_identity.MANIFEST_NAME

    if identity_failure == "missing":
        identity_path.unlink()
    else:
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
        if identity_failure == "corrupt":
            payload["manifest_digest"] = "0" * 64
        else:
            payload["base_identity"]["profile_id"] = "wrong-rating-authority"
            payload["manifest_digest"] = evaluation_data_identity.canonical_digest(
                {key: value for key, value in payload.items() if key != "manifest_digest"}
            )
        _write_json(identity_path, payload)

    bundle = load_published_evaluation_bundle(results)
    assert bundle["available"] is False
    assert "cycle_manifest_evaluation_identity_invalid" in bundle["issues"]


def test_publication_requires_daemon_writer_lease_unless_test_bypass_is_explicit(
    monkeypatch, tmp_path
):
    from evaluation_bundle import publish_evaluation_cycle_manifest

    results, _identity = _patch_results(monkeypatch, tmp_path)
    active = ["national_v1"]
    _write_cycle_files(results, active)

    with pytest.raises(ValueError, match="requires daemon writer lease"):
        publish_evaluation_cycle_manifest(
            save_num=1,
            daemon_run_id="test-run",
            active_bots=active,
            results_dir=results,
        )

    unrelated = results / "not-the-daemon-lease.lock"
    descriptor = os.open(unrelated, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="writer lease is not held"):
            publish_evaluation_cycle_manifest(
                save_num=1,
                daemon_run_id="test-run",
                active_bots=active,
                results_dir=results,
                writer_lease_fd=descriptor,
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    lease = results / ".evaluation_daemon_writer.lock"
    descriptor = os.open(lease, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        leased_manifest = publish_evaluation_cycle_manifest(
            save_num=1,
            daemon_run_id="test-run",
            active_bots=active,
            results_dir=results,
            writer_lease_fd=descriptor,
        )
        assert leased_manifest["save_num"] == 1
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    manifest = _publish(results, active)
    assert manifest["save_num"] == 1


def test_cycle_reader_waits_for_exclusive_writer(monkeypatch, tmp_path):
    from evaluation_bundle import evaluation_cycle_lock, load_published_evaluation_bundle

    results, _identity = _patch_results(monkeypatch, tmp_path)
    active = ["national_v1"]
    _write_cycle_files(results, active)
    _publish(results, active)
    writer_ready = threading.Event()
    release_writer = threading.Event()
    reader_done = threading.Event()
    loaded = []

    def writer():
        with evaluation_cycle_lock(results, exclusive=True):
            writer_ready.set()
            release_writer.wait(timeout=2)

    def reader():
        loaded.append(load_published_evaluation_bundle(results))
        reader_done.set()

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    writer_thread.start()
    assert writer_ready.wait(timeout=1)
    reader_thread.start()
    time.sleep(0.05)
    assert reader_done.is_set() is False
    release_writer.set()
    writer_thread.join(timeout=1)
    reader_thread.join(timeout=1)

    assert reader_done.is_set() is True
    assert loaded[0]["available"] is True


def test_publication_rejects_selection_pool_mismatch(monkeypatch, tmp_path):
    results, _identity = _patch_results(monkeypatch, tmp_path)
    _write_cycle_files(results, ["national_v1"])

    with pytest.raises(ValueError, match="active pool"):
        _publish(results, ["national_v1", "national_v2"])


def test_publication_rejects_cross_file_semantic_mismatch(monkeypatch, tmp_path):
    results, _identity = _patch_results(monkeypatch, tmp_path)
    active = ["national_v1", "national_v2"]
    _write_cycle_files(results, active)

    ratings = json.loads((results / "glicko_ratings.json").read_text(encoding="utf-8"))
    del ratings["national_v2"]
    _write_json(results / "glicko_ratings.json", ratings)
    with pytest.raises(ValueError, match="ratings_active_pool_mismatch"):
        _publish(results, active)

    _write_cycle_files(
        results,
        active,
        h2h={
            "national_v1 vs national_v2": {
                "games": 2,
                "a_wins": 1,
                "b_wins": 0,
                "draws": 0,
            }
        },
    )
    with pytest.raises(ValueError, match="h2h_counts_invalid"):
        _publish(results, active)

    _write_cycle_files(results, active)
    selection_path = results / "selection_snapshot.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    selection["rows"][0]["rating"] = 1499.9
    _write_json(selection_path, selection)
    with pytest.raises(ValueError, match="selection_rating_mismatch"):
        _publish(results, active)


@pytest.mark.parametrize(
    ("field", "value", "issue"),
    [
        ("evaluation_identity_digest", None, "match_history_row_identity_mismatch"),
        ("evaluation_identity_digest", "f" * 64, "match_history_row_identity_mismatch"),
        ("evaluation_epoch", "national_native_v1", "match_history_row_epoch_mismatch"),
        ("execution_mode", "official_exe", "match_history_row_execution_mode_mismatch"),
        ("execution_mode", "national_arena", "match_history_row_execution_mode_mismatch"),
    ],
)
def test_publication_rejects_foreign_match_history_before_cycle_binding(
    monkeypatch,
    tmp_path,
    field,
    value,
    issue,
):
    results, _identity = _patch_results(monkeypatch, tmp_path)
    active = ["national_v1", "national_v2"]
    row = {
        "id": "foreign-match",
        "bot0": active[0],
        "bot1": active[1],
        field: value,
    }
    _write_cycle_files(results, active, match_history_rows=[row])

    with pytest.raises(ValueError, match=issue):
        _publish(results, active)


def test_publication_rejects_foreign_rating_history_before_prompt_projection(
    monkeypatch,
    tmp_path,
):
    results, _identity = _patch_results(monkeypatch, tmp_path)
    active = ["national_v1", "national_v2"]
    _write_cycle_files(
        results,
        active,
        rating_history_rows=[{
            "period": 1,
            "evaluation_identity_digest": "f" * 64,
        }],
    )

    with pytest.raises(ValueError, match="rating_history_row_identity_mismatch"):
        _publish(results, active)


def test_daemon_authoritative_save_publishes_one_complete_cycle(monkeypatch, tmp_path):
    import elo_daemon
    import evaluation_data_identity
    import evolution_infra
    from evaluation_bundle import load_published_evaluation_bundle
    from glicko2 import Glicko2Player

    results = tmp_path / "results"
    results.mkdir()
    identity_manifest = evaluation_data_identity.ensure_evaluation_data_identity(results)
    paths = {
        "RESULTS_DIR": results,
        "H2H_FILE": results / "head_to_head.json",
        "BOT_STATS_FILE": results / "bot_stats.json",
        "RATINGS_FILE": results / "glicko_ratings.json",
        "STATS_FILE": results / "elo_daemon_stats.json",
        "SELECTION_SNAPSHOT_FILE": results / "selection_snapshot.json",
        "MATCH_HISTORY_FILE": results / "match_history.jsonl",
    }
    for name, value in paths.items():
        monkeypatch.setattr(elo_daemon, name, value)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "MATCH_HISTORY_FILE", paths["MATCH_HISTORY_FILE"])
    monkeypatch.setattr(elo_daemon, "daemon_run_id", "daemon-test")
    monkeypatch.setattr(
        elo_daemon,
        "daemon_evaluation_identity_digest",
        identity_manifest["manifest_digest"],
    )
    monkeypatch.setattr(elo_daemon, "daemon_last_cycle_manifest_digest", None)
    monkeypatch.setattr(elo_daemon, "daemon_last_cycle_save_num", None)
    monkeypatch.setattr(elo_daemon, "_daemon_writer_lease_fd", None)
    paths["MATCH_HISTORY_FILE"].write_text("", encoding="utf-8")
    active = ["national_v1", "national_v2"]
    ratings = {
        name: Glicko2Player(r=1500, rd=90, sigma=0.06) for name in active
    }
    h2h = {
        "national_v1 vs national_v2": {
            "games": 20,
            "a_wins": 9,
            "b_wins": 11,
            "draws": 0,
        }
    }
    bot_stats = {
        name: {"games": 20, "wins": 10, "losses": 10, "win_rate": 0.5}
        for name in active
    }

    manifest = elo_daemon._save_authoritative_evaluation_cycle(
        ratings,
        h2h,
        bot_stats,
        {"scheduler": "healthy"},
        7,
        active,
        _test_only_allow_unleased=True,
    )
    bundle = load_published_evaluation_bundle(results)

    assert manifest["save_num"] == 7
    assert bundle["available"] is True
    assert bundle["manifest"]["daemon_run_id"] == "daemon-test"
    assert bundle["daemon_stats"]["scheduler"] == "healthy"
    assert bundle["daemon_stats"]["total_games"] == 20
    assert bundle["daemon_stats"]["pairs"] == {"national_v1 vs national_v2": 20}
    assert len(bundle["selection"]["rows"]) == 2
    assert bundle["selection"]["rating_history_tail"][-1]["period"] == 7
    assert json.loads(bundle["raw_append_logs"]["rating_history"])["period"] == 7
