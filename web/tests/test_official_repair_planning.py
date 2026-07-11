from tool_planning import _synthesize_rework_tasks_from_checkpoint


def test_official_failed_checkpoint_synthesizes_official_repair_task():
    ckpt = {
        "stage": "official_failed",
        "next_v": 134,
        "source_v": 120,
        "gate_results": {
            "official_full": {
                "passed": False,
                "issues": [
                    "official_full_round_incomplete_after_progress: hands_started=33 settlements=32 target=70 max_abs_net_chips=19466",
                ],
                "official_evidence_summary": {
                    "classification": "obvious_decision_error",
                    "blocking": True,
                },
            }
        },
    }

    tasks = _synthesize_rework_tasks_from_checkpoint(ckpt)

    assert len(tasks) == 1
    assert tasks[0]["task_kind"] == "official_repair"
    assert tasks[0]["worker_id"] == "auto_official_full_repair"
    assert "official EXE full-certification repair" in tasks[0]["worker_prompt"]
    assert "not a strength-rating tweak" in tasks[0]["worker_prompt"]
    assert tasks[0]["target_files"]


def test_official_protocol_failure_targets_native_entrypoint():
    ckpt = {
        "stage": "official_failed",
        "next_v": 134,
        "source_v": 120,
        "gate_results": {
            "official_full": {
                "passed": False,
                "issues": ["protocol_raise_format: msg='raise  200'"],
                "official_evidence_summary": {"classification": "protocol", "blocking": True},
            }
        },
    }

    tasks = _synthesize_rework_tasks_from_checkpoint(ckpt)

    assert tasks[0]["task_kind"] == "official_repair"
    assert tasks[0]["target_files"] == ["national_bot.py"]
    assert "Protocol-focused" in tasks[0]["worker_prompt"]


def test_official_llm_words_cannot_redirect_repair_to_native_entrypoint():
    ckpt = {
        "stage": "official_failed",
        "next_v": 135,
        "source_v": 120,
        "gate_results": {
            "official_full": {
                "passed": False,
                "issues": ["obvious_decision_error: repeated river overcall"],
                "official_evidence_summary": {
                    "classification": "obvious_decision_error",
                    "blocking": True,
                },
                "status": {
                    "official_llm_repair_guidance": (
                        "Consider wire protocol serialization even though the "
                        "deterministic verdict did not report it"
                    ),
                    "official_llm_prompt_feedback": "protocol protocol protocol",
                },
            }
        },
        "reviewer_feedback": "wire format may be worth checking",
    }

    tasks = _synthesize_rework_tasks_from_checkpoint(ckpt)

    assert tasks[0]["target_files"] != ["national_bot.py"]
    assert "national_bot.py" not in tasks[0]["target_files"]
    assert "Decision/state-focused" in tasks[0]["worker_prompt"]
