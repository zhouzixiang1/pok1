from worker_mcp.idempotency import request_fingerprint
from worker_mcp.schemas import ExecutionProfile, TaskEnvelope


def make(goal="inspect   source", criteria=None):
    return TaskEnvelope(
        goal=goal,
        context="",
        repo="/tmp/repo",
        base_commit="abcdef1",
        allowed_paths=["src"],
        forbidden_paths=["archive"],
        constraints=["no writes"],
        acceptance_criteria=criteria or ["cite files"],
        execution=ExecutionProfile(),
        idempotency_key="fingerprint-test-0001",
    )


def test_fingerprint_normalizes_whitespace_and_binds_acceptance():
    assert request_fingerprint(make()) == request_fingerprint(make("inspect source"))
    assert request_fingerprint(make()) != request_fingerprint(
        make(criteria=["different result"])
    )
