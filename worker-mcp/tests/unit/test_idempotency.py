from worker_mcp.idempotency import request_fingerprint
from worker_mcp.schemas import ExecutionProfile, TaskEnvelope


def make(goal="inspect source", criteria=None, *, context="", trace_id=None):
    return TaskEnvelope(
        goal=goal,
        context=context,
        repo="/tmp/repo",
        base_commit="abcdef1",
        allowed_paths=["src"],
        forbidden_paths=["archive"],
        constraints=["no writes"],
        acceptance_criteria=criteria or ["cite files"],
        execution=ExecutionProfile(),
        idempotency_key="fingerprint-test-0001",
        trace_id=trace_id,
    )


def test_fingerprint_binds_exact_prompt_and_acceptance_but_not_trace_metadata():
    assert request_fingerprint(make()) != request_fingerprint(make("inspect   source"))
    assert request_fingerprint(make()) != request_fingerprint(
        make(criteria=["different result"])
    )
    assert request_fingerprint(make()) != request_fingerprint(
        make(context="materially different evidence")
    )
    assert request_fingerprint(make(trace_id="attempt-one")) == request_fingerprint(
        make(trace_id="attempt-two")
    )
