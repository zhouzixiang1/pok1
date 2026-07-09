from agent_workers import _compose_worker_task_prompt


def test_worker_prompt_includes_runtime_contract_block():
    task = {
        "worker_prompt": "Implement the bounded precompute change.",
        "runtime_contract": {
            "decision_budget_ms": 250,
            "fallback_action": "use legal sanitizer fallback",
            "decision_path_bound": "no full-history scan",
            "precompute_artifacts": ["preflop_bucket_table"],
            "state_lifecycle": "reset on new TCP connection",
            "official_feedback_refs": ["official_evidence:self_play_01"],
            "forbidden_runtime_work": ["file_io_in_decision"],
        },
    }

    prompt = _compose_worker_task_prompt(task, reviewer_feedback="")

    assert "# Runtime Contract" in prompt
    assert "250 ms" in prompt
    assert "preflop_bucket_table" in prompt
    assert "official_evidence:self_play_01" in prompt
    assert "do not treat this block as optional" in prompt
