import json
import sqlite3

from bot_namespace import bot_name
from candidate_store import (
    append_candidate_event,
    candidate_observability_identity,
    count_candidate_children,
    get_candidate_summary,
    read_candidate_artifacts,
    read_candidate_entities,
    read_candidate_events,
)
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V
from pipeline_contracts import next_stage_name, stage_order
from pipeline_schema import ArtifactRef, GateResult, ScoreCard, StageRunRecord


def _candidate_id(version: int, source_v: int) -> str:
    """Branch-portable canonical candidate primary key."""
    return f"{bot_name(version)}_from_{bot_name(source_v)}"


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

    assert entry["candidate_id"] == _candidate_id(245, 244)
    rows = read_candidate_events(path=ledger)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "quality_finished"
    assert rows[0]["scorecard"]["name"] == "quality"
    assert json.loads(ledger.read_text(encoding="utf-8").strip())["version"] == 245

    entities = read_candidate_entities(path=ledger, event_source=None)
    assert len(entities) == 1
    assert entities[0]["candidate_id"] == _candidate_id(245, 244)
    assert entities[0]["latest_event_type"] == "quality_finished"
    assert entities[0]["latest_metrics"]["all_passed"] is True


def test_first_strict_candidate_ledger_has_numeric_high_water_not_parent(
    tmp_path,
):
    identity = candidate_observability_identity(STRICT_TARGET_V, STRICT_SOURCE_V)
    assert identity == {
        "candidate_id": f"{bot_name(STRICT_TARGET_V)}_numeric_high_water_v{STRICT_SOURCE_V}",
        "parent_ids": [],
        "lineage_kind": "numeric_high_water_only",
        "numeric_high_water_version": STRICT_SOURCE_V,
        "source_artifact_inherited": False,
    }

    ledger = tmp_path / "candidates.jsonl"
    entry = append_candidate_event(
        "quality_started",
        version=STRICT_TARGET_V,
        source_v=STRICT_SOURCE_V,
        candidate_id=_candidate_id(STRICT_TARGET_V, STRICT_SOURCE_V),
        parent_ids=[bot_name(STRICT_SOURCE_V)],
        metrics={
            "probe": "kept",
            "lineage_kind": "forged_parent",
            "source_artifact_inherited": True,
        },
        path=ledger,
    )

    assert entry["candidate_id"] == f"{bot_name(STRICT_TARGET_V)}_numeric_high_water_v{STRICT_SOURCE_V}"
    assert entry["parent_ids"] == []
    assert entry["metrics"] == {
        "lineage_kind": "numeric_high_water_only",
        "numeric_high_water_version": STRICT_SOURCE_V,
        "probe": "kept",
        "source_artifact_inherited": False,
    }


def test_candidate_store_records_artifacts_and_children(tmp_path, monkeypatch):
    import candidate_store

    ledger = tmp_path / "candidates.jsonl"
    monkeypatch.setattr(candidate_store, "CANDIDATE_EVENTS_FILE", ledger)

    append_candidate_event(
        "quality_finished",
        version=250,
        source_v=249,
        parent_ids=[bot_name(249)],
        skill_layers=["spr"],
        changed_files=["policy.py"],
        artifact_refs=[ArtifactRef(kind="report", path="reports/v250.json", label="quality")],
    )

    summary = get_candidate_summary(_candidate_id(250, 249), path=ledger)
    assert summary is not None
    assert summary["skill_layers"] == ["spr"]
    assert summary["changed_files"] == ["policy.py"]
    assert count_candidate_children(bot_name(249), path=ledger) == 1
    artifacts = read_candidate_artifacts(_candidate_id(250, 249), path=ledger)
    assert artifacts[0]["kind"] == "report"


def test_candidate_entities_are_isolated_by_event_source(tmp_path, monkeypatch):
    import candidate_store

    ledger = tmp_path / "candidates.jsonl"
    monkeypatch.setattr(candidate_store, "CANDIDATE_EVENTS_FILE", ledger)

    append_candidate_event(
        "quality_finished",
        event_source="runtime",
        version=252,
        source_v=251,
        scorecard={"passed": True},
        metrics={"runtime": True},
    )
    append_candidate_event(
        "quality_finished",
        event_source="test",
        version=252,
        source_v=251,
        scorecard={"passed": False},
        metrics={"test": True},
        failures=["synthetic failure"],
    )

    runtime_rows = read_candidate_entities(path=ledger)
    test_rows = read_candidate_entities(path=ledger, event_source="test")
    all_rows = read_candidate_entities(path=ledger, event_source=None)

    assert len(runtime_rows) == 1
    assert runtime_rows[0]["event_source"] == "runtime"
    assert runtime_rows[0]["latest_status"] == "passed"
    assert runtime_rows[0]["latest_metrics"] == {"runtime": True}
    assert len(test_rows) == 1
    assert test_rows[0]["event_source"] == "test"
    assert test_rows[0]["latest_status"] == "failed"
    assert len(all_rows) == 2

    summary = get_candidate_summary(_candidate_id(252, 251), path=ledger)
    assert summary["event_source"] == "runtime"
    assert summary["latest_status"] == "passed"


def test_candidate_store_migrates_v1_candidate_primary_key(tmp_path):
    import candidate_store

    ledger = tmp_path / "candidates.jsonl"
    db = ledger.with_suffix(".sqlite3")
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE candidates (
                candidate_id TEXT PRIMARY KEY,
                version INTEGER,
                source_v INTEGER,
                event_source TEXT NOT NULL DEFAULT 'runtime',
                profile_id TEXT NOT NULL DEFAULT 'default',
                workflow_profile_id TEXT NOT NULL DEFAULT '',
                prompt_profile_id TEXT NOT NULL DEFAULT '',
                model_id TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                parent_ids_json TEXT NOT NULL DEFAULT '[]',
                skill_layers_json TEXT NOT NULL DEFAULT '[]',
                changed_files_json TEXT NOT NULL DEFAULT '[]',
                diff_hash TEXT NOT NULL DEFAULT '',
                latest_stage TEXT NOT NULL DEFAULT '',
                latest_event_type TEXT NOT NULL DEFAULT '',
                latest_gate TEXT NOT NULL DEFAULT '',
                latest_status TEXT NOT NULL DEFAULT '',
                latest_scorecard_json TEXT NOT NULL DEFAULT '{}',
                latest_metrics_json TEXT NOT NULL DEFAULT '{}',
                latest_failures_json TEXT NOT NULL DEFAULT '[]',
                failure_class TEXT NOT NULL DEFAULT '',
                artifacts_json TEXT NOT NULL DEFAULT '{}',
                artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO candidates(candidate_id, version, source_v, event_source, created_at, updated_at) "
            "VALUES (?, 253, 252, 'runtime', 1.0, 1.0)",
            (_candidate_id(253, 252),),
        )
        conn.commit()

    append_candidate_event(
        "quality_finished",
        event_source="test",
        version=253,
        source_v=252,
        path=ledger,
    )

    rows = read_candidate_entities(candidate_id=_candidate_id(253, 252), event_source=None, path=ledger)
    assert {row["event_source"] for row in rows} == {"runtime", "test"}


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
    assert rows[0]["candidate_id"] == _candidate_id(251, 250)


def test_stage_contract_order_and_stage_record():
    assert stage_order()[0] == "prepare"
    assert next_stage_name("quality") == "review"
    record = StageRunRecord(candidate_id=_candidate_id(250, 249), stage="quality", status="passed")
    assert record.candidate_id == _candidate_id(250, 249)
