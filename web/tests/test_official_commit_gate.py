import asyncio
from pathlib import Path

from official_certification import STATUS_CERTIFIED, STATUS_INCONCLUSIVE


def _native_bot(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "national_bot.py").write_text("import socket\n# native tcp entry\n", encoding="utf-8")
    return path


def test_official_full_commit_gate_requires_full_spec(tmp_path, monkeypatch):
    import official_certification
    import tool_commit

    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _native_bot(tmp_path / "bots" / "national_v134")
    opponent = _native_bot(tmp_path / "bots" / "national_v70")
    (opponent / ".completed").touch()
    monkeypatch.setenv("POK_OFFICIAL_OPPONENT", str(opponent))
    monkeypatch.setattr(tool_commit, "get_active_bots", lambda: [str(opponent)])
    calls = []

    def fake_run_certification(spec, **kwargs):
        calls.append((spec, kwargs))
        return {
            "status": STATUS_CERTIFIED,
            "mode": "full",
            "issues": [],
            "cache_hit": False,
            "official_evidence_path": str(tmp_path / "evidence.json"),
            "official_evidence_summary": {"classification": "pass", "blocking": False},
        }

    monkeypatch.setattr(official_certification, "run_certification", fake_run_certification)

    result = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            134,
            123,
            candidate,
            {"national_execution_mode": "native_tcp"},
            {},
        )
    )

    assert result["passed"] is True
    assert calls
    spec, kwargs = calls[0]
    assert spec.mode == "full"
    assert spec.self_play_rounds == 5
    assert spec.opponent_rounds == 3
    assert spec.target_hands == 70
    assert kwargs["queue_on_busy"] is False
    assert result["opponent_selection"]["opponent"]["reason"] == "bootstrap_grandfathered"
    assert result["opponent_selection"]["opponent"]["grandfather_record"]["status"] == "official-grandfathered"


def test_official_full_commit_gate_blocks_inconclusive_result(tmp_path, monkeypatch):
    import official_certification
    import tool_commit

    monkeypatch.setenv("POK_OFFICIAL_CERT_DIR", str(tmp_path / "cert"))
    candidate = _native_bot(tmp_path / "bots" / "national_v134")
    opponent = _native_bot(tmp_path / "bots" / "national_v70")
    (opponent / ".completed").touch()
    monkeypatch.setenv("POK_OFFICIAL_OPPONENT", str(opponent))
    monkeypatch.setattr(tool_commit, "get_active_bots", lambda: [str(opponent)])

    def fake_run_certification(_spec, **_kwargs):
        return {
            "status": STATUS_INCONCLUSIVE,
            "mode": "full",
            "issues": ["thp_incomplete_for_full_certification: hands=69 target=70"],
            "official_evidence_path": str(tmp_path / "evidence.json"),
            "official_evidence_summary": {"classification": "inconclusive", "blocking": False},
        }

    monkeypatch.setattr(official_certification, "run_certification", fake_run_certification)

    result = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            134,
            123,
            candidate,
            {"national_execution_mode": "native_tcp"},
            {},
        )
    )

    assert result["passed"] is False
    assert result["status"]["status"] == STATUS_INCONCLUSIVE
    assert result["issues"] == ["thp_incomplete_for_full_certification: hands=69 target=70"]


def test_official_full_commit_gate_skips_non_native_workflow(monkeypatch, tmp_path):
    import official_certification
    import tool_commit

    monkeypatch.setattr(
        official_certification,
        "run_certification",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    result = asyncio.run(
        tool_commit._run_official_full_commit_gate(
            10,
            9,
            tmp_path / "bot",
            {"national_execution_mode": "adapter"},
            {},
        )
    )

    assert result["passed"] is True
    assert result["skipped"] is True
    assert result["reason"] == "non_native_tcp_workflow"
