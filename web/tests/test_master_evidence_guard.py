from pathlib import Path

import pytest


def test_master_read_guard_blocks_other_checkout_live_alias(tmp_path):
    import llm_query

    allowed = tmp_path / "runtime" / "web/core/results/v149/evidence_snapshot"
    allowed.mkdir(parents=True)
    other_checkout = tmp_path / "operator" / "web/core/results/glicko_ratings.json"

    violation = llm_query._master_live_evidence_read_violation(
        "Read",
        {"file_path": str(other_checkout)},
        str(allowed),
    )

    assert violation == str(other_checkout.resolve(strict=False))


def test_master_read_guard_allows_only_exact_snapshot_tree(tmp_path):
    import llm_query

    allowed = tmp_path / "runtime" / "web/core/results/v149/evidence_snapshot"
    allowed.mkdir(parents=True)
    frozen = allowed / "selection_snapshot.json"
    copied = tmp_path / "copy/results/v149/evidence_snapshot/selection_snapshot.json"

    assert llm_query._master_live_evidence_read_violation(
        "Read", {"file_path": str(frozen)}, str(allowed)
    ) is None
    assert llm_query._master_live_evidence_read_violation(
        "Read", {"file_path": str(copied)}, str(allowed)
    ) == str(copied.resolve(strict=False))


@pytest.mark.parametrize(
    "command",
    [
        "find web/core/results -type f",
        "cat web/core/results/*",
        "find web/core/res* -type f",
        "cat web/core/result?/*",
        "rg national_v web/core/[r]esults",
        "cat /tmp/head_to_*.json",
        "rg national_v web/core/results",
        "python -c 'from pathlib import Path; print(list(Path(\"web/core/results\").iterdir()))'",
        "cat ../other-checkout/web/core/results/head_to_head.json",
    ],
)
def test_evidence_guard_blocks_bash_directory_glob_and_other_checkout(command):
    import llm_query

    violation = llm_query._master_live_evidence_read_violation(
        "Bash",
        {"command": command},
        None,
    )

    assert violation is not None


def test_evidence_guard_allows_bash_path_inside_exact_snapshot(tmp_path):
    import llm_query

    allowed = tmp_path / "runtime/web/core/results/v149/evidence_snapshot"
    allowed.mkdir(parents=True)

    assert llm_query._master_live_evidence_read_violation(
        "Bash",
        {"command": f"cat {allowed}/*.json"},
        str(allowed),
    ) is None


def test_evidence_guard_allows_only_bound_candidate_under_results(tmp_path):
    import llm_query

    workspace = tmp_path / "web/core/results/workflow/artifacts/workspaces/lease-a"
    workspace.mkdir(parents=True)
    candidate_file = workspace / "policy.py"
    sibling_live = tmp_path / "web/core/results/head_to_head.json"

    assert llm_query._master_live_evidence_read_violation(
        "Read",
        {"file_path": str(candidate_file)},
        None,
        [workspace],
    ) is None
    assert llm_query._master_live_evidence_read_violation(
        "Bash",
        {"command": f"python -m py_compile {candidate_file}"},
        None,
        [workspace],
    ) is None
    assert llm_query._master_live_evidence_read_violation(
        "Read",
        {"file_path": str(sibling_live)},
        None,
        [workspace],
    ) == str(sibling_live.resolve(strict=False))


@pytest.mark.parametrize(
    "role",
    [
        "MASTER (Try 1)",
        "STRATEGY CRITIC",
        "CROSSOVER v149×v143→v150",
        "LEAD CODE REVIEWER",
        "WORKER 1 (Algorithmic Logic Architect)",
        "DEBUG AGENT (v150)",
    ],
)
def test_every_decision_changing_role_requires_frozen_evidence_guard(role):
    import llm_query

    assert llm_query._role_requires_frozen_evidence_guard(role) is True


def test_unrelated_toolless_role_does_not_require_evidence_guard():
    import llm_query

    assert llm_query._role_requires_frozen_evidence_guard("COMBINED ANALYST") is False
