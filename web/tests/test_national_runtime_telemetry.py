import json

from national_runtime_telemetry import parse_native_bot_log


def test_deadline_refinement_keeps_system_trusted_progress_without_worker_done():
    report = parse_native_bot_log(
        "\n".join((
            "DECIDE refinement decision_id=7 sequence=2 action=400 elapsed=0.100s "
            "trusted_step=3 trusted_cpu_ms=4.5 reported_samples=3 "
            "reported_confidence=0.25",
            "DECIDE refinement decision_id=7 sequence=11 action=600 elapsed=0.500s "
            "trusted_step=12 trusted_cpu_ms=21.0 reported_samples=99 "
            "reported_confidence=high",
            "DECIDE refinement_deadline decision_id=7 latest_safe=600 sequence=11 "
            "refinement_budget=0.550s hard_deadline=0.680s",
        ))
    )

    refinement = report["refinement"]
    assert refinement["message_count"] == 2
    assert refinement["decision_count"] == 1
    assert refinement["trusted_steps_sum"] == 12
    assert refinement["trusted_steps_max"] == 12
    assert refinement["trusted_cpu"]["max_sec"] == 0.021
    assert refinement["reported_sample_count_max"] == 99
    assert refinement["reported_confidence_max"] == 0.25
    assert refinement["termination_reasons"] == {"refinement_deadline": 1}
    assert refinement["candidate_reported_fields_authoritative"] is False


def test_worker_done_and_progress_are_merged_once_per_decision():
    report = parse_native_bot_log(
        "\n".join((
            "DECIDE refinement decision_id=8 sequence=4 action=-1 elapsed=0.100s "
            "trusted_step=4 trusted_cpu_ms=9.0 reported_samples=None "
            "reported_confidence=None",
            "DECIDE worker_done decision_id=8 sequence=4 latest_safe=-1 elapsed=0.110s "
            "trusted_steps=4 trusted_cpu_ms=9.0 iterator_exhausted=True "
            "termination=iterator_exhausted",
        ))
    )

    refinement = report["refinement"]
    assert refinement["decision_count"] == 1
    assert refinement["trusted_steps_sum"] == 4
    assert refinement["trusted_cpu"]["count"] == 1
    assert refinement["iterator_exhausted_count"] == 1


def test_baseline_only_worker_done_is_not_counted_as_refinement():
    report = parse_native_bot_log(
        "DECIDE worker_done decision_id=9 sequence=1 latest_safe=0 elapsed=0.010s "
        "trusted_steps=0 trusted_cpu_ms=0.0 iterator_exhausted=False "
        "termination=not_available"
    )

    refinement = report["refinement"]
    assert refinement["message_count"] == 0
    assert refinement["decision_count"] == 0
    assert refinement["trusted_steps_sum"] == 0
    assert refinement["trusted_cpu"]["count"] == 0
    assert refinement["termination_reasons"] == {}


def test_typed_policy_decisions_are_parsed_without_integer_action_abi():
    report = parse_native_bot_log(
        "\n".join((
            "DECIDE start name=Native hand=3 stage=turn act_cnt=0",
            "DECIDE refinement decision_id=4 sequence=2 "
            "decision={'kind': 'raise', 'raise_to': 400} elapsed=0.050s "
            "trusted_step=1 trusted_cpu_ms=2.0 reported_samples=32 "
            "reported_confidence=0.75",
            "DECIDE worker_done decision_id=4 sequence=2 "
            "latest_safe={'kind': 'raise', 'raise_to': 400} elapsed=0.060s "
            "trusted_steps=1 trusted_cpu_ms=2.0 iterator_exhausted=True "
            "termination=iterator_exhausted",
            "DECIDE done decision={'kind': 'raise', 'raise_to': 400} "
            "source=incremental_refinement elapsed=0.070s",
        ))
    )

    assert report["decision_latency"]["count"] == 1
    assert report["refinement"]["message_count"] == 1
    assert report["refinement"]["trusted_steps_sum"] == 1


def test_opponent_tracker_uses_last_valid_snapshot_and_ignores_malformed_rows():
    first = {
        "schema_version": 4,
        "hands_completed": 9,
        "showdown_range": {"samples": 0, "class_counts": {}},
    }
    final = {
        "schema_version": 4,
        "hands_completed": 10,
        "showdown_range": {"samples": 1, "class_counts": {"AA": 1}},
    }
    report = parse_native_bot_log(
        "\n".join((
            "2026-07-12 00:00:00 OPPONENT_TRACKER "
            + json.dumps(first, separators=(",", ":")),
            "OPPONENT_TRACKER {not-json}",
            "[native] OPPONENT_TRACKER "
            + json.dumps(final, separators=(",", ":")),
        ))
    )

    tracker = report["opponent_tracker"]
    assert tracker["available"] is True
    assert tracker["snapshot_count"] == 2
    assert tracker["latest"] == final
