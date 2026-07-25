"""Official repair planning at the strict candidate/system boundary."""

from conftest import STRICT_TARGET_V
from tool_planning import _synthesize_rework_tasks_from_checkpoint


def _checkpoint(issue, *, classification="obvious_decision_error", version=None):
    return {
        "stage": "official_failed",
        "next_v": STRICT_TARGET_V + 1 if version is None else version,
        "source_v": STRICT_TARGET_V,
        "gate_results": {
            "official_full": {
                "passed": False,
                "issues": [issue],
                "official_evidence_summary": {
                    "classification": classification,
                    "blocking": True,
                },
            }
        },
    }


def test_policy_proven_official_failure_synthesizes_policy_only_repair():
    checkpoint = _checkpoint(
        "obvious_decision_error: policy exception during a pending river action"
    )
    tasks = _synthesize_rework_tasks_from_checkpoint(checkpoint)

    assert len(tasks) == 1
    task = tasks[0]
    assert task["task_kind"] == "official_repair"
    assert task["target_files"] == ["policy.py"]
    assert "official EXE full-certification repair" in task["worker_prompt"]
    assert "not a strength-rating tweak" in task["worker_prompt"]
    assert "Candidate scope is policy.py only" in task["worker_prompt"]
    assert "never edit national_bot.py or precompute.py" in task["worker_prompt"]


def test_protocol_failure_is_system_owned_and_has_no_worker_repair_task():
    checkpoint = _checkpoint(
        "protocol_raise_format: msg='raise  200'",
        classification="protocol",
    )
    assert _synthesize_rework_tasks_from_checkpoint(checkpoint) == []


def test_advisory_llm_protocol_words_cannot_redirect_policy_repair():
    checkpoint = _checkpoint(
        "obvious_decision_error: repeated river overcall",
        version=STRICT_TARGET_V + 2,
    )
    checkpoint["gate_results"]["official_full"]["status"] = {
        "official_llm_repair_guidance": "Consider wire protocol serialization",
        "official_llm_prompt_feedback": "protocol protocol protocol",
    }
    checkpoint["reviewer_feedback"] = "wire format may be worth checking"

    tasks = _synthesize_rework_tasks_from_checkpoint(checkpoint)

    assert len(tasks) == 1
    assert tasks[0]["target_files"] == ["policy.py"]
    assert "national_bot.py" not in tasks[0]["target_files"]
    assert "system-owned" in tasks[0]["worker_prompt"]
