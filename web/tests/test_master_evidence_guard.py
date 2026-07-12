from pathlib import Path


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
