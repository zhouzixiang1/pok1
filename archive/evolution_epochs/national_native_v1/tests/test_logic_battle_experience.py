"""Archived tests for the retired replay-analysis background bridge."""

from __future__ import annotations

from copy import deepcopy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

import battle_experience as be
import battle_memory
from test_logic_replay_analysis import IDENTITY, make_strict_replay


def _history_entry(replay: dict) -> dict:
    return {
        key: deepcopy(value)
        for key, value in replay.items()
        if key != "games" and key != "replay_schema_version"
    }


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    results = tmp_path / "results"
    replay_dir = results / "match_replay"
    replay_dir.mkdir(parents=True)
    monkeypatch.setattr(be, "RESULTS_DIR", results)
    monkeypatch.setattr(be, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(be, "MATCH_HISTORY_FILE", results / "match_history.jsonl")
    monkeypatch.setattr(be, "BATTLE_EVIDENCE_FILE", results / "battle_evidence.jsonl")
    monkeypatch.setattr(be, "BATTLE_PENDING_SUMMARIES_FILE", results / "battle_pending_summaries.jsonl")
    monkeypatch.setattr(be, "BATTLE_LESSONS_FILE", results / "battle_lessons.jsonl")
    monkeypatch.setattr(be, "ANALYSIS_MARKER_FILE", results / ".battle_analysis_progress.json")
    monkeypatch.setattr(be, "LLM_COSTS_FILE", results / "llm_costs.jsonl")
    monkeypatch.setattr(be, "_current_identity_digest", lambda: IDENTITY)


def _write_match(replay: dict) -> dict:
    entry = _history_entry(replay)
    be.REPLAY_DIR.joinpath(replay["id"]).write_text(json.dumps(replay), encoding="utf-8")
    with be.MATCH_HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_unanalyzed_selection_accepts_only_current_strict_history():
    valid = _write_match(make_strict_replay("valid.json"))
    old_epoch = _history_entry(make_strict_replay("old.json"))
    old_epoch["evaluation_epoch"] = "national_native_v1"
    wrong_identity = _history_entry(make_strict_replay("wrong.json"))
    wrong_identity["evaluation_identity_digest"] = "f" * 64
    with be.MATCH_HISTORY_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(old_epoch) + "\n")
        handle.write(json.dumps(wrong_identity) + "\n")
    be.REPLAY_DIR.joinpath("old.json").write_text("{}", encoding="utf-8")
    be.REPLAY_DIR.joinpath("wrong.json").write_text("{}", encoding="utf-8")

    assert be.get_unanalyzed_matches() == [valid]


def test_process_persists_two_perspectives_with_identity():
    entry = _write_match(make_strict_replay("strict.json"))
    be._process_one_match(entry)

    evidence = _jsonl(be.BATTLE_EVIDENCE_FILE)
    pending = _jsonl(be.BATTLE_PENDING_SUMMARIES_FILE)
    assert {row["bot"] for row in evidence} == {"national_v143", "national_v144"}
    assert all(row["epoch"] == "national_tcp_policy_v1" for row in evidence)
    assert all(row["evaluation_identity_digest"] == IDENTITY for row in evidence)
    assert len(pending) == 1
    assert len(pending[0]["evidence_ids"]) == 2
    assert be.is_analyzed("strict.json") is True


def test_retired_replay_is_rejected_and_never_enters_memory():
    replay = make_strict_replay("retired.json")
    entry = _history_entry(replay)
    be.REPLAY_DIR.joinpath("retired.json").write_text(json.dumps({
        "bot0": "national_v143",
        "bot1": "national_v144",
        "games": [{"logs": [], "requests": [], "responses": []}],
    }), encoding="utf-8")

    assert be._process_one_match_safe(entry) is None
    assert be.is_analyzed("retired.json") is True
    assert not be.BATTLE_EVIDENCE_FILE.exists()
    assert be.get_battle_experience("national_v143") == ""


def test_marker_from_retired_schema_does_not_close_current_replay():
    be.ANALYSIS_MARKER_FILE.write_text(json.dumps(["strict.json"]), encoding="utf-8")
    assert be.is_analyzed("strict.json") is False
    be.mark_analyzed("strict.json")
    marker = json.loads(be.ANALYSIS_MARKER_FILE.read_text(encoding="utf-8"))
    assert marker["schema_version"] == 2
    assert marker["epoch"] == "national_tcp_policy_v1"
    assert be.is_analyzed("strict.json") is True


def test_wrong_identity_memory_rows_are_not_injected():
    entry = _write_match(make_strict_replay("strict.json"))
    be._process_one_match(entry)
    current = be.get_battle_experience("national_v143")
    assert "Native replay contract" in current
    assert "fold_to_raise" in current

    be._current_identity_digest = lambda: "f" * 64
    assert be.get_battle_experience("national_v143") == ""


def test_memory_rejects_unbound_or_retired_rows(tmp_path):
    paths = battle_memory.BattleMemoryPaths(
        evidence_file=tmp_path / "evidence.jsonl",
        pending_file=tmp_path / "pending.jsonl",
        lessons_file=tmp_path / "lessons.jsonl",
    )
    retired = {
        "schema_version": 1,
        "bot": "national_v143",
        "opponent": "national_v144",
        "sample_n": 1,
        "match_id": "old.json",
    }
    assert battle_memory.append_evidence([retired], paths=paths) == []
    assert battle_memory.markdown_lessons_to_records(
        "- unsupported lesson",
        evidence_ids=[],
        evaluation_identity_digest=IDENTITY,
    ) == []
    assert battle_memory.format_battle_memory_for_master(
        expected_evaluation_identity_digest=IDENTITY,
        paths=paths,
    ) == ""


def test_readonly_lessons_require_current_identity_evidence():
    entry = _write_match(make_strict_replay("strict.json"))
    be._process_one_match(entry)
    paths = be._memory_paths()
    evidence_ids = [row["evidence_id"] for row in _jsonl(be.BATTLE_EVIDENCE_FILE)]
    records = battle_memory.markdown_lessons_to_records(
        "- River call evidence should remain advisory and identity bound.",
        evidence_ids=evidence_ids,
        evaluation_identity_digest=IDENTITY,
    )
    assert battle_memory.append_lessons(records, paths=paths)
    lessons = battle_memory.read_identity_bound_lessons(
        expected_evaluation_identity_digest=IDENTITY,
        paths=paths,
    )
    assert len(lessons) == 1
    assert lessons[0]["evidence_ids"] == evidence_ids
    assert battle_memory.read_identity_bound_lessons(
        expected_evaluation_identity_digest="f" * 64,
        paths=paths,
    ) == []


def test_llm_is_opt_in_and_deterministic_evidence_still_closes(monkeypatch):
    entry = _write_match(make_strict_replay("strict.json"))
    monkeypatch.setattr(be, "BATTLE_EXPERIENCE_LLM_ENABLED", False)
    monkeypatch.setattr(
        be,
        "_run_sync_llm_call",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("LLM must not run")),
    )
    payload = be._process_one_match_safe(entry)
    be._apply_batch_results([(entry, True, payload)])
    assert be.is_analyzed("strict.json") is True
    assert len(_jsonl(be.BATTLE_EVIDENCE_FILE)) == 2


def test_prompt_compaction_is_bounded(monkeypatch):
    monkeypatch.setattr(be, "BATTLE_PROMPT_CURRENT_BUDGET", 300)
    monkeypatch.setattr(be, "BATTLE_PROMPT_NEW_DATA_BUDGET", 400)
    monkeypatch.setattr(be, "BATTLE_PROMPT_MATCH_SECTION_BUDGET", 180)
    current, new = be._prepare_prompt_inputs("x" * 3000, "y" * 3000, mode="test")
    assert len(current) <= 300
    assert len(new) <= 400
    assert "omitted" in current
    assert "omitted" in new


def test_path_traversal_history_id_is_rejected():
    replay = make_strict_replay("strict.json")
    entry = _history_entry(replay)
    entry["id"] = "../strict.json"
    assert be._history_entry_valid(entry, IDENTITY) is False
