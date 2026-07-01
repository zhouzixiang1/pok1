import json

from candidate_store import append_candidate_event, read_candidate_events
from pipeline_schema import GateResult, ScoreCard


def test_scorecard_failed_gates_ignore_nonblocking():
    card = ScoreCard(name="quality")
    card.add(GateResult.from_bool("compile", True))
    card.add(GateResult.from_bool("critic_advisory", False, blocking=False, failures=["low score"]))

    assert card.passed
    assert card.failed_gates == []


def test_candidate_store_appends_locked_jsonl(tmp_path, monkeypatch):
    import candidate_store

    ledger = tmp_path / "candidates.jsonl"
    monkeypatch.setattr(candidate_store, "CANDIDATE_EVENTS_FILE", ledger)

    card = ScoreCard(name="quality")
    card.add(GateResult.from_bool("compile", True))
    entry = append_candidate_event(
        "quality_finished",
        version=245,
        source_v=244,
        gate="quality",
        scorecard=card,
        metrics={"all_passed": True},
    )

    assert entry["candidate_id"] == "claude_v245_from_v244"
    rows = read_candidate_events(path=ledger)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "quality_finished"
    assert rows[0]["scorecard"]["name"] == "quality"
    assert json.loads(ledger.read_text(encoding="utf-8").strip())["version"] == 245
