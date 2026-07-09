import asyncio
import json

from official_llm_analysis import (
    build_official_analysis_prompt,
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
    assert "official_platform_compliance" in prompt
    assert '"strength_evaluation": "not_applicable"' in prompt


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

    assert analysis["compliance_verdict"] == "inconclusive"
    assert analysis["blocking"] is False
    assert analysis["strength_evaluation"] == "not_applicable"
    assert analysis["ignored_strength_fields"] == ["rating"]
    assert "llm_failure_without_deterministic_confirmation_is_advisory" in analysis["notes"]


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

    assert analysis["compliance_verdict"] == "fail"
    assert analysis["failure_class"] == "protocol"
    assert analysis["blocking"] is True
    assert "llm_pass_overridden_by_deterministic_blocking_evidence" in analysis["notes"]


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

    assert analysis["compliance_verdict"] == "fail"
    assert analysis["failure_class"] == "protocol"
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

    assert analysis["compliance_verdict"] == "pass"
    assert analysis["blocking"] is False
    assert analysis["strength_evaluation"] == "not_applicable"


def test_safe_default_analysis_never_evaluates_strength():
    analysis = safe_default_analysis(_blocking_evidence(), reason="llm_not_run")

    assert analysis["compliance_verdict"] == "fail"
    assert analysis["blocking"] is True
    assert analysis["strength_evaluation"] == "not_applicable"
