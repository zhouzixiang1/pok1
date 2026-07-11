"""Code-level stage contracts for the evolution pipeline.

This registry mirrors the current orchestrator order without changing runtime
control flow yet. It gives tests, UI, and future scheduler work a single source
of truth for legal stage order, hard gates, and retryability.
"""

from __future__ import annotations

from pipeline_schema import StageContract


STAGE_CONTRACTS: tuple[StageContract, ...] = (
    StageContract(
        name="prepare",
        retryable=True,
        expected_outputs=["candidate_dir", "source_v", "next_v"],
    ),
    StageContract(
        name="direction_audit",
        retryable=True,
        expected_outputs=["constraints", "suggested_direction"],
    ),
    StageContract(
        name="master",
        retryable=True,
        required_inputs=["source_v", "next_v"],
        expected_outputs=["master_plan.tasks"],
        hard_gates=["master_plan_schema"],
    ),
    StageContract(
        name="workers",
        retryable=True,
        required_inputs=["master_plan.tasks"],
        expected_outputs=["changed_files"],
        hard_gates=["worker_boundary", "py_compile"],
    ),
    StageContract(
        name="quality",
        retryable=True,
        required_inputs=["changed_files"],
        hard_gates=[
            "code_changed",
            "post_master_delta",
            "declared_scope",
            "compile",
            "runtime_import",
            "protected_contract",
            "smoke",
            "national_protocol",
            "national_acceptance",
            "decision",
            "size",
            "fix_verification",
            "telemetry_fidelity",
            "reachability",
        ],
        soft_gates=["placement_shadow_review"],
    ),
    StageContract(
        name="review",
        retryable=True,
        required_inputs=["quality.scorecard"],
        hard_gates=["review_approved"],
    ),
    StageContract(
        name="critic",
        retryable=True,
        required_inputs=["review"],
        soft_gates=["critic_score"],
    ),
    StageContract(
        name="precommit_eval",
        retryable=True,
        required_inputs=["quality", "review", "critic"],
        hard_gates=["precommit_regression"],
    ),
    StageContract(
        name="commit",
        retryable=False,
        required_inputs=["precommit_eval.passed"],
        expected_outputs=["git_commit", "bot_tag", "completed_sentinel"],
    ),
    StageContract(
        name="archivist",
        retryable=False,
        required_inputs=["commit"],
        expected_outputs=["experience_updates", "archive_record"],
    ),
)


def get_stage_contract(name: str) -> StageContract | None:
    for contract in STAGE_CONTRACTS:
        if contract.name == name:
            return contract
    return None


def stage_order() -> list[str]:
    return [contract.name for contract in STAGE_CONTRACTS]


def next_stage_name(stage: str) -> str | None:
    names = stage_order()
    try:
        idx = names.index(stage)
    except ValueError:
        return None
    if idx + 1 >= len(names):
        return None
    return names[idx + 1]
