import fcntl
from pathlib import Path

from official_platform_harness import OfficialPlatformConfig
from official_certification import (
    STATUS_COMPLIANCE_PASS,
    STATUS_CERTIFIED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SMOKE_PASS,
    build_spec,
    cache_key,
    enqueue_certification,
    official_failure_blocks_parent,
    process_certification_queue,
    queue_snapshot,
    record_local_pass,
    report_valid_for_spec,
    run_certification,
    write_status,
)


class FakeResult:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self):
        return self.payload


def _bot(path: Path, body: str = "def act():\n    return 0\n") -> Path:
    path.mkdir(parents=True)
    (path / "main.py").write_text(body, encoding="utf-8")
    (path / "national_bot.py").write_text("import socket\n# raise fold call check allin sock.recv _split_messages\n", encoding="utf-8")
    return path


def _config(tmp_path: Path) -> OfficialPlatformConfig:
    exe = tmp_path / "platform.exe"
    exe.write_bytes(b"fake-exe")
    wine = tmp_path / "wine"
    wine.mkdir()
    return OfficialPlatformConfig(
        exe_path=exe,
        wineprefix=wine,
        results_dir=tmp_path / "official",
        lock_path=tmp_path / "official.lock",
    )


def _report(*, target_hands: int, rounds: int, passed=True, issues=None, thp_hands=None):
    receipts = []
    for idx in range(rounds):
        receipts.append({
            "passed": passed,
            "issues": issues or [],
            "target_hands": target_hands,
            "artifacts": {
                "thp_summaries": [{"hand_records": target_hands if thp_hands is None else thp_hands}],
            },
        })
    return {
        "passed": passed,
        "issues": issues or [],
        "report": {
            "summary": {"suite_dir": "/tmp/suite", "rounds_run": rounds},
            "rounds": receipts,
        },
    }


def test_cache_key_changes_when_inputs_change(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)

    spec = build_spec("smoke", candidate, opponent=opponent)
    first = cache_key(spec, cfg)
    (candidate / "main.py").write_text("def act():\n    return 1\n", encoding="utf-8")
    changed_candidate = cache_key(spec, cfg)
    changed_mode = cache_key(build_spec("full", candidate, opponent=opponent), cfg)

    assert first != changed_candidate
    assert changed_candidate != changed_mode


def test_smoke_receipt_cannot_satisfy_full_certification(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    smoke = build_spec("smoke", candidate, opponent=opponent)
    full = build_spec("full", candidate, opponent=opponent)
    payload = _report(target_hands=10, rounds=2)

    assert report_valid_for_spec(payload, smoke) is True
    assert report_valid_for_spec(payload, full) is False


def test_compliance_mode_requires_two_full_70_hand_rounds(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    compliance = build_spec("compliance", candidate, opponent=opponent)

    assert compliance.self_play_rounds == 1
    assert compliance.opponent_rounds == 1
    assert compliance.target_hands == 70
    assert report_valid_for_spec(_report(target_hands=70, rounds=2), compliance) is True
    assert report_valid_for_spec(_report(target_hands=10, rounds=2), compliance) is False


def test_bad_receipts_are_not_valid_for_cache(tmp_path):
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    spec = build_spec("full", candidate, opponent=opponent)

    assert report_valid_for_spec(_report(target_hands=70, rounds=8, issues=["illegal"]), spec) is False
    assert report_valid_for_spec(_report(target_hands=70, rounds=8, thp_hands=69), spec) is False


def test_run_certification_uses_valid_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    def runner(*_args, **_kwargs):
        return FakeResult(_report(target_hands=10, rounds=2))

    first = run_certification(spec, config=cfg, runner=runner, queue_on_busy=False)
    second = run_certification(
        spec,
        config=cfg,
        runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("cache miss")),
        queue_on_busy=False,
    )

    assert first["status"] == STATUS_SMOKE_PASS
    assert second["status"] == STATUS_SMOKE_PASS
    assert second["cache_hit"] is True


def test_full_certification_status_requires_full_report(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("full", candidate, opponent=opponent)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=70, rounds=8)),
        queue_on_busy=False,
    )

    assert result["status"] == STATUS_CERTIFIED


def test_compliance_certification_has_distinct_status(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("compliance", candidate, opponent=opponent)

    result = run_certification(
        spec,
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=70, rounds=2)),
        queue_on_busy=False,
    )

    assert result["status"] == STATUS_COMPLIANCE_PASS
    assert result["mode"] == "compliance"


def test_record_local_pass_does_not_clear_failed_status(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")

    write_status(candidate, STATUS_FAILED, mode="smoke", issues=["protocol_raise_format"])
    result = record_local_pass(candidate)

    assert result["status"] == STATUS_FAILED
    assert result["issues"] == ["protocol_raise_format"]


def test_only_protocol_official_failures_block_parent_selection():
    assert official_failure_blocks_parent({
        "status": STATUS_FAILED,
        "issues": ["self_play_1: protocol_raise_format: msg='raise  200'"],
    })
    assert not official_failure_blocks_parent({
        "status": STATUS_FAILED,
        "issues": ["official_acceptance_suite_exception: FileNotFoundError: wine"],
    })


def test_smoke_enqueue_does_not_downgrade_certified_status(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")

    write_status(candidate, STATUS_CERTIFIED, mode="full", cache_key="full-key", issues=[])
    result = enqueue_certification(build_spec("smoke", candidate, opponent=opponent), reason="manual_smoke")

    assert result["status"] == STATUS_CERTIFIED
    assert queue_snapshot()["pending"] == 0


def test_process_certification_queue_consumes_pending_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)

    enqueue_certification(spec, reason="test")
    assert queue_snapshot()["pending"] == 1

    result = process_certification_queue(
        config=cfg,
        runner=lambda *_args, **_kwargs: FakeResult(_report(target_hands=10, rounds=2)),
    )

    assert result["processed"] == 1
    assert result["remaining"] == 0
    assert result["results"][0]["status"] == STATUS_SMOKE_PASS
    assert queue_snapshot()["pending"] == 0


def test_process_certification_queue_respects_official_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)
    enqueue_certification(spec, reason="test")
    cfg.lock_path.touch()

    with cfg.lock_path.open("r+", encoding="utf-8") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = process_certification_queue(
                config=cfg,
                runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not run")),
            )
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)

    assert result["processed"] == 0
    assert result["lock_busy"] is True
    assert queue_snapshot()["pending"] == 1


def test_official_lock_busy_queues_without_running(tmp_path, monkeypatch):
    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _bot(tmp_path / "national_v1")
    opponent = _bot(tmp_path / "national_v2")
    cfg = _config(tmp_path)
    spec = build_spec("smoke", candidate, opponent=opponent)
    cfg.lock_path.touch()

    with cfg.lock_path.open("r+", encoding="utf-8") as lock_fp:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = run_certification(
                spec,
                config=cfg,
                runner=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not run")),
                queue_on_busy=True,
            )
        finally:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)

    assert result["status"] == STATUS_PENDING
    assert result["queued"] is True
    assert (tmp_path / "cert" / "queue.jsonl").exists()
