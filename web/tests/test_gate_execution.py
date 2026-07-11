from gate_execution import GateExecution


def test_infrastructure_execution_has_stable_identity_and_explicit_side():
    execution = GateExecution.infrastructure(
        "native_smoke_harness",
        "workflow_smoke",
        ["server launch failed"],
        identity={"runner": "native-v3", "candidate": "abc"},
    )
    same = GateExecution.infrastructure(
        "native_smoke_harness",
        "workflow_smoke",
        ["different diagnostic text"],
        identity={"candidate": "abc", "runner": "native-v3"},
    )

    assert execution.outcome == "infrastructure_failure"
    assert execution.side == "harness"
    assert execution.retryable is True
    assert execution.identity_digest == same.identity_digest


def test_candidate_failure_cannot_be_constructed_by_infrastructure_factory():
    execution = GateExecution.infrastructure("validator", "contract", ["crash"])
    assert execution.is_infrastructure is True
    assert execution.outcome != "candidate_failure"
