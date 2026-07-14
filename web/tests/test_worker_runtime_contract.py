from agent_workers import _compose_worker_task_prompt
from pathlib import Path

from tool_planning import _load_worker_prompt_template


def test_worker_prompt_includes_runtime_contract_block():
    task = {
        "worker_prompt": "Consume the bounded system precompute from policy.py.",
        "runtime_contract": {
            "decision": {
                "clock": "time.monotonic",
                "hard_deadline_ms": 55_000,
                "baseline_target_ms": 250,
                "refinement_budget_ms": 54_000,
                "baseline_path": "existing deterministic action",
                "fallback_action": "use legal sanitizer fallback",
                "refinement_bound": "no full-history scan",
                "max_samples": 64,
            },
            "precompute_artifacts": [{
                "name": "preflop_bucket_table",
                "owner_file": "precompute.py",
                "build_phase": "module_import",
                "max_build_ms": 500,
                "max_entries": 169,
                "max_bytes": 65536,
                "key_shape": "tuple[int,int,bool]",
                "consumer": "policy.get_baseline_decision",
                "fallback": "legal_baseline",
            }],
            "match_memory": {
                "tracker_class": "OpponentTracker",
                "owner_file": "national_bot.py",
                "reset_boundary": "tcp_connection",
                "update_events": ["hand_start", "opponent_action", "settlement", "showdown"],
                "snapshot_field": "decision_context.opponent_model",
                "max_recent_hands": 8,
                "prior_rule": "beta_prior_weight_8",
                "confidence_rule": (
                    "global_actions_over_actions_plus_24_and_context_samples_over_samples_plus_8"
                ),
                "adaptation_cap": 0.65,
                "consumer": "policy.get_baseline_decision",
            },
            "official_feedback_refs": ["official_evidence:self_play_01"],
            "forbidden_runtime_work": ["file_io_in_decision"],
        },
    }

    prompt = _compose_worker_task_prompt(task, reviewer_feedback="")

    assert "# Runtime Contract" in prompt
    assert "250 ms" in prompt
    assert "preflop_bucket_table" in prompt
    assert "decision_context.opponent_model" in prompt
    assert "adaptation" in prompt.lower()
    assert "official_evidence:self_play_01" in prompt
    assert "do not treat this block as optional" in prompt


def test_worker_template_loader_exposes_only_strict_native_profile():
    prompts = Path(__file__).resolve().parents[1] / "core" / "prompts"

    native = _load_worker_prompt_template(prompts, native_tcp=True)

    assert "{execution_profile_contract}" not in native
    assert "formal submission is the system-owned `national_bot.py`" in native
    assert "code lives behind the versioned `policy.py` ABI" in native
    assert "smoke_tester.py" not in native

    import pytest

    with pytest.raises(RuntimeError, match="requires national native TCP"):
        _load_worker_prompt_template(prompts, native_tcp=False)


def test_timeout_retry_keeps_the_complete_runtime_contract():
    source = (
        Path(__file__).resolve().parents[1] / "core" / "agent_workers.py"
    ).read_text(encoding="utf-8")

    assert "SAME assigned task" in source
    assert "implement every mandatory Runtime Contract boundary" in source
    assert "Implement only the single most impactful change" not in source
