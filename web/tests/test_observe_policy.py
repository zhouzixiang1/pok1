from core import observe_policy


def test_parent_timeout_cancel_is_expected_not_alert_or_fatal():
    event = {
        "type": "pipeline.llm_role_parent_timeout_cancelled",
        "data": {
            "role": "DYNAMIC_TEST_GEN",
            "stage": "workers_done",
            "cancel_scope": "dynamic_test_gen",
            "cancel_reason": "parent_timeout",
        },
    }

    assert observe_policy.is_expected_event(event) is True
    assert observe_policy.should_alert(event) is False
    assert observe_policy.is_fatal_event(event) is False


def test_legacy_dynamic_test_cancel_is_expected():
    event = {
        "type": "pipeline.llm_role_cancelled",
        "data": {"role": "DYNAMIC_TEST_GEN", "stage": "workers_done"},
    }

    assert observe_policy.is_expected_event(event) is True
    assert observe_policy.should_alert(event) is False
    assert observe_policy.is_fatal_event(event) is False


def test_generic_llm_cancel_alerts_but_is_not_fatal():
    event = {
        "type": "pipeline.llm_role_cancelled",
        "data": {"role": "LEAD CODE REVIEWER", "stage": "quality_passed"},
    }

    assert observe_policy.is_expected_event(event) is False
    assert observe_policy.should_alert(event) is True
    assert observe_policy.is_fatal_event(event) is False


def test_subagent_guard_block_remains_fatal():
    event = {
        "type": "pipeline.subagent_guard_block",
        "data": {"role": "CROSSOVER", "stage": "crossover_running"},
    }

    assert observe_policy.should_alert(event) is True
    assert observe_policy.is_fatal_event(event) is True


def test_precommit_eval_only_alerts_when_failed():
    assert observe_policy.should_alert({
        "type": "pipeline.precommit_eval",
        "data": {"passed": True},
    }) is False
    assert observe_policy.should_alert({
        "type": "pipeline.precommit_eval",
        "data": {"passed": False},
    }) is True
