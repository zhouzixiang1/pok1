from core.pipeline_state import generic_abandon_block, next_tool_for_checkpoint, validate_stage_transition


def test_precommit_failed_is_forward_and_reworkable():
    ok, reason = validate_stage_transition("critic_checked", "precommit_failed")
    assert ok, reason

    ok, reason = validate_stage_transition("precommit_failed", "master_planned")
    assert ok, reason
    assert "retry" in reason


def test_old_critic_checked_failed_precommit_routes_to_workers():
    checkpoint = {
        "stage": "critic_checked",
        "next_v": 263,
        "source_v": 244,
        "precommit_attempt": 1,
        "gate_results": {
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "semantic_regression"}],
            }
        },
    }

    assert next_tool_for_checkpoint(checkpoint) == "execute_workers"
    blocked = generic_abandon_block(checkpoint, max_precommit_retries=3)
    assert blocked["blocked"] is True
    assert blocked["next_tool"] == "execute_workers"
    assert blocked["failure_class"] == "regression"


def test_precommit_failed_blocks_abandon_until_hard_limit():
    checkpoint = {
        "stage": "precommit_failed",
        "next_v": 263,
        "source_v": 244,
        "precommit_attempt": 1,
        "gate_results": {
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "aggregate_precommit_regression"}],
            }
        },
    }

    blocked = generic_abandon_block(checkpoint, max_precommit_retries=3)
    assert blocked["next_tool"] == "execute_workers"

    checkpoint["precommit_attempt"] = 3
    assert generic_abandon_block(checkpoint, max_precommit_retries=3) is None


def test_precommit_infra_stays_on_precommit_retry():
    checkpoint = {
        "stage": "critic_checked",
        "next_v": 263,
        "source_v": 244,
        "precommit_attempt": 1,
        "gate_results": {
            "precommit_eval": {
                "passed": False,
                "blockers": [{"reason": "match_timeout"}],
            }
        },
    }

    assert next_tool_for_checkpoint(checkpoint) == "run_precommit_eval"
    blocked = generic_abandon_block(checkpoint, max_precommit_retries=3)
    assert blocked["next_tool"] == "run_precommit_eval"


def test_selected_next_tool_distinguishes_master_and_crossover():
    assert next_tool_for_checkpoint({"stage": "selected", "next_v": 265, "source_v": 254}) == "prepare_next_gen"
    assert (
        next_tool_for_checkpoint(
            {"stage": "selected", "next_v": 266, "source_v": 254, "parent2_v": 240}
        )
        == "run_crossover"
    )
