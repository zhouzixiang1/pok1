import asyncio
import json

import pytest

from official_llm_analysis import (
    advisory_analysis_contract_issues,
    build_official_analysis_prompt,
    compact_evidence_for_llm,
    normalize_official_analysis,
    run_official_llm_analysis,
    run_official_llm_analysis_sync,
    safe_default_analysis,
)


def _clean_evidence():
    return {
        "schema_version": 1,
        "candidate": "/tmp/national_v1",
        "purpose": "official_platform_compliance",
        "strength_evaluation": "not_applicable",
        "summary": {"passed": True, "target_hands": 70, "rounds_run": 1},
        "deterministic": {
            "passed": True,
            "classification": "pass",
            "blocking": False,
            "issues": [],
            "rounds_run": 1,
            "target_hands": 70,
        },
        "rounds": [],
    }


def _blocking_evidence():
    evidence = _clean_evidence()
    evidence["summary"]["passed"] = False
    evidence["deterministic"] = {
        "passed": False,
        "classification": "protocol",
        "blocking": True,
        "issues": ["self_play_01: wire_replay: illegal_check"],
        "rounds_run": 1,
        "target_hands": 70,
    }
    return evidence


def test_official_analysis_prompt_forbids_strength_evaluation():
    prompt = build_official_analysis_prompt(_clean_evidence())

    assert "Do not evaluate poker strength" in prompt
    assert "compliance oracle only" in prompt
    assert "It is not\na poker-strength oracle" in prompt
    assert "official_platform_compliance" in prompt
    assert '"strength_evaluation": "not_applicable"' in prompt


def test_official_analysis_prompt_excludes_raw_and_path_evidence():
    evidence = _blocking_evidence()
    evidence["deterministic"]["issues"].append(
        "summary_read_error: /private/DETERMINISTIC_PATH_MARKER/summary.json"
    )
    evidence.update({
        "candidate": "/private/CANDIDATE_PATH_MARKER/national_v1",
        "opponent": "/private/OPPONENT_PATH_MARKER/national_v2",
        "artifact_root": "/private/ARTIFACT_ROOT_MARKER/suite",
        "summary": {
            "request": "FULL_REQUEST_MARKER",
            "history": ["FULL_HISTORY_MARKER"],
            "net_chips": 19000,
        },
        "rounds": [
            {
                "round_id": "self_play_01",
                "round_kind": "self_play",
                "round_index": 1,
                "target_hands": 70,
                "passed": False,
                "classification": "protocol",
                "issues": ["wire_replay: illegal_check"],
                "log_summary": {
                    "request_history": "LOG_SUMMARY_HISTORY_MARKER",
                    "net_chips": -19000,
                },
                "log_tails": {"bot_a_log": "RAW_LOG_TAIL_MARKER"},
                "artifacts": {
                    "receipt": {"path": "/private/ABSOLUTE_ARTIFACT_PATH_MARKER/receipt.json"},
                },
                "thp_summaries": [
                    {
                        "path": "/private/THP_PATH_MARKER/match.txt",
                        "hand_records": 17,
                        "bytes": 2048,
                        "history": "THP_HISTORY_MARKER",
                    }
                ],
                "wire_replay_summary": {
                    "events_seen": 9,
                    "hands_started_min": 1,
                    "settlements_min": 0,
                    "raw_hex": "RAW_HEX_MARKER_001122334455",
                    "request": "WIRE_REQUEST_MARKER",
                    "history": ["WIRE_HISTORY_MARKER"],
                    "issues": [
                        {
                            "kind": "illegal_check",
                            "conn": "A",
                            "hand": 1,
                            "stage": "flop",
                            "message": "check",
                            "raw_hex": "ISSUE_RAW_HEX_MARKER",
                            "request": "ISSUE_REQUEST_MARKER",
                            "history": ["ISSUE_HISTORY_MARKER"],
                            "previous_event": {
                                "conn": "A",
                                "direction": "server_to_bot",
                                "messages": ["RAW_STATE_REQUEST_MARKER"],
                            },
                        }
                    ],
                },
            }
        ],
    })

    prompt = build_official_analysis_prompt(evidence)

    forbidden_markers = {
        "CANDIDATE_PATH_MARKER",
        "OPPONENT_PATH_MARKER",
        "ARTIFACT_ROOT_MARKER",
        "FULL_REQUEST_MARKER",
        "FULL_HISTORY_MARKER",
        "LOG_SUMMARY_HISTORY_MARKER",
        "RAW_LOG_TAIL_MARKER",
        "ABSOLUTE_ARTIFACT_PATH_MARKER",
        "DETERMINISTIC_PATH_MARKER",
        "THP_PATH_MARKER",
        "THP_HISTORY_MARKER",
        "RAW_HEX_MARKER_001122334455",
        "WIRE_REQUEST_MARKER",
        "WIRE_HISTORY_MARKER",
        "ISSUE_RAW_HEX_MARKER",
        "ISSUE_REQUEST_MARKER",
        "ISSUE_HISTORY_MARKER",
        "RAW_STATE_REQUEST_MARKER",
    }
    assert all(marker not in prompt for marker in forbidden_markers)
    assert '"raw_hex"' not in prompt
    assert '"log_tails"' not in prompt
    assert '"artifact_paths"' not in prompt
    assert '"net_chips"' not in prompt
    assert "summary_read_error: <absolute-path>" in prompt


def test_compact_evidence_keeps_stable_bounded_attribution():
    evidence = _blocking_evidence()
    evidence["rounds"] = [
        {
            "round_id": "self_play_01",
            "round_kind": "self_play",
            "round_index": 1,
            "target_hands": 70,
            "passed": False,
            "classification": "protocol",
            "issues": ["wire_replay: illegal_check"],
            "thp_summaries": [
                {
                    "path": "/private/never-send-this/match.txt",
                    "exists": True,
                    "hand_records": 17,
                    "bytes": 2048,
                }
            ],
            "wire_replay_summary": {
                "events_seen": 42,
                "hands_started_min": 17,
                "settlements_min": 16,
                "max_platform_silent_gap_sec": 1.25,
                "issues": [
                    {
                        "kind": "illegal_check",
                        "conn": "A",
                        "hand": 17,
                        "stage": "turn",
                        "message": "check",
                        "dt": 12.5,
                        "expected_reason": "respond_to_check",
                        "previous_event": {
                            "dt": 12.0,
                            "conn": "A",
                            "direction": "server_to_bot",
                            "messages": ["check"],
                        },
                        "next_event": {
                            "dt": 12.6,
                            "conn": "A",
                            "direction": "server_to_bot",
                            "messages": ["call"],
                        },
                    }
                ],
                "pending_expected_actions": [
                    {
                        "conn": "B",
                        "hand": 17,
                        "stage": "turn",
                        "waited_sec": 2.75,
                        "expected_reason": "turn_first_action",
                    }
                ],
            },
        }
    ]

    first = compact_evidence_for_llm(evidence)
    second = compact_evidence_for_llm(evidence)

    assert first == second
    round_item = first["rounds"][0]
    assert round_item["round_id"] == "self_play_01"
    assert round_item["evidence_id"].startswith("round-")
    assert round_item["thp_summaries"] == [
        {
            "summary_index": 1,
            "exists": True,
            "hand_records": 17,
            "size_bytes": 2048,
            "evidence_id": round_item["thp_summaries"][0]["evidence_id"],
        }
    ]
    replay = round_item["wire_replay_summary"]
    assert replay["events_seen"] == 42
    assert replay["hands_started_min"] == 17
    assert replay["settlements_min"] == 16
    finding = replay["issues"][0]
    assert finding["evidence_id"].startswith("wire-issue-")
    assert finding["round_evidence_id"] == round_item["evidence_id"]
    assert finding["round_id"] == "self_play_01"
    assert finding["hand"] == 17
    assert finding["street"] == "turn"
    assert finding["connection"] == "A"
    assert finding["observed_action"] == "check"
    assert finding["expected_rule"] == "check_legal_for_current_state"
    assert finding["expected_reason"] == "respond_to_check"
    assert 1 <= len(finding["state_sequence"]) <= 5
    assert all(item["evidence_id"].startswith("state-") for item in finding["state_sequence"])
    pending = replay["pending_expected_actions"][0]
    assert pending["connection"] == "B"
    assert pending["hand"] == 17
    assert pending["street"] == "turn"
    assert pending["expected_reason"] == "turn_first_action"
    prompt = build_official_analysis_prompt(evidence)
    assert finding["evidence_id"] in prompt
    assert round_item["thp_summaries"][0]["evidence_id"] in prompt


def test_normalize_does_not_allow_llm_to_block_clean_deterministic_pass():
    raw = {
        "compliance_verdict": "fail",
        "failure_class": "obvious_decision_error",
        "blocking": True,
        "confidence": 0.9,
        "evidence": [],
        "root_cause": "Bot lost the round badly.",
        "repair_guidance": "Improve strategy.",
        "prompt_feedback": "Avoid weak calls.",
        "rating": 1200,
    }

    analysis = normalize_official_analysis(raw, _clean_evidence())

    assert analysis["analysis_status"] == "no_findings"
    assert analysis["authority"] == "advisory_only"
    assert "compliance_verdict" not in analysis
    assert "blocking" not in analysis
    assert analysis["strength_evaluation"] == "not_applicable"
    assert analysis["ignored_strength_fields"] == ["rating"]
    assert analysis["ignored_authority_fields"] == ["blocking", "compliance_verdict"]
    assert "llm_authority_fields_ignored" in analysis["notes"]
    assert analysis["root_cause_hypothesis"] == ""
    assert analysis["repair_guidance"] == ""
    assert analysis["prompt_feedback"] == ""
    assert analysis["ignored_strength_text_fields"] == [
        "prompt_feedback",
        "repair_guidance",
        "root_cause_hypothesis",
    ]


def test_clean_pass_cannot_inject_grounded_repair_feedback():
    evidence = _clean_evidence()
    evidence["rounds"] = [{
        "round_id": "self_play_01",
        "round_kind": "self_play",
        "round_index": 1,
        "target_hands": 70,
        "passed": True,
        "classification": "pass",
        "issues": [],
    }]
    evidence_id = compact_evidence_for_llm(evidence)["rounds"][0]["evidence_id"]

    analysis = normalize_official_analysis({
        "analysis_status": "explained",
        "hypothesis_class": "state_machine",
        "confidence": 0.99,
        "evidence": [{"evidence_id": evidence_id}],
        "root_cause_hypothesis": "The wire layer is wrong.",
        "repair_guidance": "Rewrite the wire layer.",
        "prompt_feedback": "Make the next worker change protocol code.",
    }, evidence)

    assert analysis["analysis_status"] == "no_findings"
    assert analysis["hypothesis_class"] == "none"
    assert analysis["root_cause_hypothesis"] == ""
    assert analysis["repair_guidance"] == ""
    assert analysis["prompt_feedback"] == ""
    assert "clean_pass_llm_feedback_removed" in analysis["notes"]


@pytest.mark.parametrize(
    "text",
    [
        "Increase EV and equity realization by bluffing more aggressively.",
        "提高胜率和强度，并增加诈唬与激进程度。",
    ],
)
def test_strength_tuning_text_is_removed_from_grounded_failure_analysis(text):
    evidence = _blocking_evidence()
    finding_id = "self_play_01:001:strength-filter"
    evidence["rounds"] = [{
        "round_id": "self_play_01",
        "round_kind": "self_play",
        "round_index": 1,
        "target_hands": 70,
        "passed": False,
        "classification": "protocol",
        "issues": ["illegal_check"],
        "attribution": {
            "findings": [{
                "finding_id": finding_id,
                "code": "illegal_check",
                "category": "protocol",
                "subject_domain": "candidate",
                "subject_instance_id": "candidate_a",
                "candidate_impact": "block",
                "connection": "A",
                "evidence": {"hand": 17, "stage": "turn", "message": "check"},
            }],
        },
        "wire_replay_summary": {},
        "thp_summaries": [],
    }]

    analysis = normalize_official_analysis({
        "analysis_status": "explained",
        "hypothesis_class": "protocol",
        "evidence": [{"evidence_id": finding_id}],
        "repair_guidance": text,
        "prompt_feedback": text,
    }, evidence)

    assert analysis["repair_guidance"] == ""
    assert analysis["prompt_feedback"] == ""


def test_normalize_removes_strength_tuning_text_from_nonblocking_analysis():
    raw = {
        "compliance_verdict": "pass",
        "failure_class": "none",
        "blocking": False,
        "confidence": 0.9,
        "evidence": [],
        "root_cause": "Protocol is clean.",
        "repair_guidance": "Increase win rate by exploiting weak calls.",
        "prompt_feedback": "Raise rating with stronger river play.",
    }

    analysis = normalize_official_analysis(raw, _clean_evidence())

    assert analysis["analysis_status"] == "no_findings"
    assert analysis["root_cause_hypothesis"] == ""
    assert analysis["repair_guidance"] == ""
    assert analysis["prompt_feedback"] == ""
    assert analysis["ignored_strength_text_fields"] == [
        "prompt_feedback",
        "repair_guidance",
    ]


def test_normalize_forces_fail_when_deterministic_evidence_blocks():
    raw = {
        "compliance_verdict": "pass",
        "failure_class": "none",
        "blocking": False,
        "confidence": 0.7,
        "evidence": [],
        "root_cause": "No issue.",
        "repair_guidance": "",
        "prompt_feedback": "",
    }

    analysis = normalize_official_analysis(raw, _blocking_evidence())

    assert analysis["analysis_status"] == "insufficient_evidence"
    assert analysis["hypothesis_class"] == "none"
    assert analysis["deterministic_context"]["blocking"] is True
    assert "blocking" not in analysis
    assert "llm_authority_fields_ignored" in analysis["notes"]


def test_normalize_rejects_forged_evidence_and_preserves_deterministic_actor():
    evidence = _blocking_evidence()
    evidence["rounds"] = [{
        "round_id": "opponent_01",
        "round_kind": "opponent",
        "round_index": 1,
        "passed": False,
        "classification": "protocol",
        "issues": [],
        "attribution": {
            "policy_id": "official-attribution-v1",
            "candidate_verdict": "fail",
            "candidate_blocking": True,
            "countable": False,
            "retry_required": False,
            "findings": [{
                "finding_id": "opponent_01:001:abc123",
                "code": "illegal_check",
                "category": "protocol",
                "subject_domain": "candidate",
                "subject_instance_id": "candidate_a",
                "candidate_impact": "block",
                "connection": "A",
                "evidence": {"hand": 17, "stage": "turn", "message": "check"},
            }],
        },
        "wire_replay_summary": {},
        "thp_summaries": [],
    }]
    raw = {
        "compliance_verdict": "fail",
        "failure_class": "protocol",
        "blocking": True,
        "confidence": 0.9,
        "evidence": [
            {
                "evidence_id": "opponent_01:001:abc123",
                "subject_domain": "opponent",
                "subject_instance_id": "forged_actor",
            },
            {"evidence_id": "invented-id", "subject_domain": "candidate"},
        ],
    }

    analysis = normalize_official_analysis(raw, evidence)

    assert analysis["evidence"] == [{
        "evidence_id": "opponent_01:001:abc123",
        "round_id": "opponent_01",
        "hand": 17,
        "street": "turn",
        "connection": "A",
        "observed_action": "check",
        "subject_domain": "candidate",
        "subject_instance_id": "candidate_a",
        "candidate_impact": "block",
        "code": "illegal_check",
        "category": "protocol",
    }]
    assert any(note.startswith("llm_evidence_ids_rejected:invented-id") for note in analysis["notes"])


def test_run_official_llm_analysis_accepts_fake_runner_and_writes_json(tmp_path):
    def fake_runner(prompt):
        assert "Official National Platform Compliance Analyst" in prompt
        return json.dumps(
            {
                "compliance_verdict": "fail",
                "failure_class": "protocol",
                "blocking": True,
                "confidence": 0.88,
                "evidence": [
                    {
                        "round": "self_play_01",
                        "hand": 1,
                        "street": "preflop",
                        "bot": "candidate",
                        "observed": "check",
                        "expected": "raise/call/fold/allin",
                        "source": "wire_events.jsonl",
                    }
                ],
                "root_cause": "Small blind sent check as the first preflop action.",
                "repair_guidance": "Guard preflop blind opening actions before send.",
                "prompt_feedback": "Require pending-action and blind-position validation before every send.",
            }
        )

    output_path = tmp_path / "llm_official_analysis.json"
    analysis = asyncio.run(
        run_official_llm_analysis(_blocking_evidence(), runner=fake_runner, output_path=output_path)
    )

    assert analysis["analysis_status"] == "insufficient_evidence"
    assert analysis["hypothesis_class"] == "protocol"
    assert analysis["authority"] == "advisory_only"
    assert analysis["confidence"] == 0.88
    assert output_path.exists()


def test_run_official_llm_analysis_sync_uses_fake_runner(tmp_path):
    analysis = run_official_llm_analysis_sync(
        _clean_evidence(),
        runner=lambda _prompt: json.dumps(
            {
                "compliance_verdict": "pass",
                "failure_class": "none",
                "blocking": False,
                "confidence": 0.66,
                "evidence": [],
                "root_cause": "No compliance problem.",
                "repair_guidance": "",
                "prompt_feedback": "",
            }
        ),
        output_path=tmp_path / "analysis.json",
    )

    assert analysis["analysis_status"] == "no_findings"
    assert analysis["authority"] == "advisory_only"
    assert analysis["strength_evaluation"] == "not_applicable"


def test_default_official_analysis_runner_supports_headless_ui(monkeypatch, tmp_path):
    import llm_query
    from evolution_infra import NullUI

    seen = {}

    async def fake_stream(full_prompt, options, log_file_path, ui, role_name, **_kwargs):
        seen.update({
            "full_prompt": full_prompt,
            "tools": options.tools,
            "log_file_path": log_file_path,
            "ui": ui,
            "role_name": role_name,
        })
        return [json.dumps({
            "compliance_verdict": "pass",
            "failure_class": "none",
            "blocking": False,
            "confidence": 0.91,
            "evidence": [],
            "root_cause": "No compliance problem.",
            "repair_guidance": "",
            "prompt_feedback": "",
        })], 0.01, {"input_tokens": 10, "output_tokens": 20}

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda *_args, **_kwargs: None)

    output_path = tmp_path / "llm_official_analysis.json"
    log_path = tmp_path / "llm_official_analysis.log"
    analysis = asyncio.run(run_official_llm_analysis(
        _clean_evidence(),
        output_path=output_path,
        log_file=log_path,
    ))

    assert analysis["analysis_source"] == "llm"
    assert analysis["analysis_status"] == "no_findings"
    assert analysis["authority"] == "advisory_only"
    assert analysis["confidence"] == 0.91
    assert isinstance(seen["ui"], NullUI)
    assert seen["role_name"] == "OFFICIAL PLATFORM COMPLIANCE ANALYST"
    assert seen["tools"] == []
    assert seen["log_file_path"] == log_path
    assert "Official National Platform Compliance Analyst" in seen["full_prompt"]
    assert output_path.exists()


def test_safe_default_analysis_never_evaluates_strength():
    analysis = safe_default_analysis(_blocking_evidence(), reason="llm_not_run")

    assert analysis["analysis_status"] == "insufficient_evidence"
    assert analysis["authority"] == "advisory_only"
    assert analysis["deterministic_context"]["blocking"] is True
    assert "blocking" not in analysis
    assert analysis["strength_evaluation"] == "not_applicable"


def test_advisory_contract_rejects_authority_fields_and_ungrounded_feedback():
    valid = safe_default_analysis(_clean_evidence())
    assert advisory_analysis_contract_issues(valid) == []

    invalid = {
        **valid,
        "blocking": False,
        "compliance_verdict": "pass",
        "repair_guidance": "Change the bot without evidence.",
    }
    issues = advisory_analysis_contract_issues(invalid)
    assert any("forbidden_authority_fields" in issue for issue in issues)
    assert "official_llm_analysis_feedback_without_evidence" in issues
