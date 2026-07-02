import json

from candidate_store import (
    append_candidate_event,
    count_candidate_children,
    get_candidate_summary,
    read_candidate_artifacts,
    read_candidate_entities,
    read_candidate_events,
)
from pipeline_contracts import next_stage_name, stage_order
from pipeline_schema import ArtifactRef, GateResult, ScoreCard, StageRunRecord


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

    entities = read_candidate_entities(path=ledger, event_source=None)
    assert len(entities) == 1
    assert entities[0]["candidate_id"] == "claude_v245_from_v244"
    assert entities[0]["latest_event_type"] == "quality_finished"
    assert entities[0]["latest_metrics"]["all_passed"] is True


def test_candidate_store_records_artifacts_and_children(tmp_path, monkeypatch):
    import candidate_store

    ledger = tmp_path / "candidates.jsonl"
    monkeypatch.setattr(candidate_store, "CANDIDATE_EVENTS_FILE", ledger)

    append_candidate_event(
        "quality_finished",
        version=250,
        source_v=249,
        parent_ids=["claude_v249"],
        skill_layers=["spr"],
        changed_files=["postflop.py"],
        artifact_refs=[ArtifactRef(kind="report", path="reports/v250.json", label="quality")],
    )

    summary = get_candidate_summary("claude_v250_from_v249", path=ledger)
    assert summary is not None
    assert summary["skill_layers"] == ["spr"]
    assert summary["changed_files"] == ["postflop.py"]
    assert count_candidate_children("claude_v249", path=ledger) == 1
    artifacts = read_candidate_artifacts("claude_v250_from_v249", path=ledger)
    assert artifacts[0]["kind"] == "report"


def test_candidate_store_follows_dynamic_results_dir(tmp_path, monkeypatch):
    import candidate_store
    import evolution_infra

    results_dir = tmp_path / "isolated_results"
    results_dir.mkdir()
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(
        candidate_store,
        "CANDIDATE_EVENTS_FILE",
        candidate_store._IMPORT_CANDIDATE_EVENTS_FILE,
    )

    append_candidate_event("quality_started", version=251, source_v=250)

    ledger = results_dir / "candidates.jsonl"
    assert ledger.exists()
    assert (results_dir / "candidates.sqlite3").exists()
    rows = read_candidate_events(path=ledger)
    assert rows[0]["candidate_id"] == "claude_v251_from_v250"


def test_stage_contract_order_and_stage_record():
    assert stage_order()[0] == "prepare"
    assert next_stage_name("quality") == "review"
    record = StageRunRecord(candidate_id="claude_v250_from_v249", stage="quality", status="passed")
    assert record.candidate_id == "claude_v250_from_v249"
