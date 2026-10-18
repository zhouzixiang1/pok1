"""Publishing-checkpoint drift reconciliation (2026-10-18).

v566 published cleanly, then two bug-fix deploys advanced HEAD past the
publication commit. The publishing stage's resume policy demands an
unchanged evaluation contract, so recovery refused with
``repo_baseline_head_mismatch`` — even though the publication's tags,
commit and intent are independently proven and the only remaining effects
are idempotent recovery steps (sentinel, checkpoint clear, handoff
convergence). The fully proven intent-bound publication window
(``_publication_checkpoint_reconciliation_allowed``) now tolerates contract
drift in ``checkpoint_recovery_diagnostics``.
"""

from bot_namespace import bot_name, bot_tag
from conftest import STRICT_TARGET_V


def _publishing_checkpoint(target_v: int) -> dict:
    return {
        "stage": "publishing",
        "next_v": target_v,
        "source_v": STRICT_TARGET_V,
        "workflow_run_id": f"generation:{target_v}:workflow-v1",
        "publication_intent": {
            "version": target_v,
            "workflow_run_id": f"generation:{target_v}:workflow-v1",
            "completion_tag": bot_tag(target_v),
            "publication_id": "pub",
            "candidate_artifact_hash": "artifact",
            "official_certificate_digest": "",
        },
        "repo_baseline": {
            "head": "b" * 40,
            "branch": "tencent-cloud-runtime",
            "evaluation_contract": {"version": 1},
        },
    }


def _prove_window(monkeypatch, tmp_path, target_v: int, *, high_water=None):
    """Monkeypatch the publication-window legs to a fully proven state."""
    from types import SimpleNamespace

    import checkpoint_schema
    import evolution_infra
    import national_runtime_authority
    import publication_transaction

    # The epoch gate is orthogonal to drift reconciliation; neutralize it
    # the way sibling recovery tests build full epoch bindings.
    monkeypatch.setattr(
        checkpoint_schema, "checkpoint_epoch_errors", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_parent_authority_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        checkpoint_schema, "checkpoint_epoch_reset_route", lambda *_a, **_k: None
    )
    bot_dir = tmp_path / "root" / "bots" / bot_name(target_v)
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / ".completed").write_text(
        "publication_id=pub\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        publication_transaction,
        "publication_intent_structure_errors",
        lambda _intent: [],
    )
    monkeypatch.setattr(
        evolution_infra, "_git", lambda *a, **_k: "a" * 40
    )
    monkeypatch.setattr(
        evolution_infra,
        "_validate_local_publication_refs",
        lambda *_a, **_k: {},
    )
    monkeypatch.setattr(
        evolution_infra,
        "_validate_existing_publication_commit",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        evolution_infra, "get_bot_dir", lambda _v: bot_dir
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "build_pending_local_publication_proof",
        lambda _path: {
            "version": target_v,
            "artifact_hash": "artifact",
            "commit_oid": "a" * 40,
            "tag": bot_tag(target_v),
        },
    )
    monkeypatch.setattr(
        evolution_infra,
        "resolve_national_bot_spec",
        lambda *_a, **_k: SimpleNamespace(issues=[], certificate_digest=None, eligible=True),
    )
    if high_water is not None:
        monkeypatch.setattr(
            evolution_infra,
            "checkpoint_allocation_authority",
            lambda **_k: {"published_high_water": high_water},
        )


def _drifted_head_environment(monkeypatch, tmp_path):
    """A project root whose git baseline differs from the checkpoint's."""
    import subprocess

    import pipeline_recovery

    root = tmp_path / "root"
    (root / "web").mkdir(parents=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "tencent-cloud-runtime"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    (root / "bots").mkdir()
    (root / "web" / "f.txt").write_text("x")
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(root),
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "commit", "-qm", "base"],
        cwd=str(root),
        check=True,
        capture_output=True,
        env=env,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.setattr(pipeline_recovery, "PROJECT_ROOT", root)
    monkeypatch.setattr(
        pipeline_recovery,
        "evaluate_head_drift",
        lambda *_a, **_k: (False, {"head_contract_paths": ["web/core/x.py"]}),
    )
    return root, head


def test_proven_publication_window_tolerates_contract_drift(
    tmp_path, monkeypatch
):
    import pipeline_recovery

    target_v = STRICT_TARGET_V + 6
    checkpoint = _publishing_checkpoint(target_v)
    _root, head = _drifted_head_environment(monkeypatch, tmp_path)
    _prove_window(
        monkeypatch, tmp_path, target_v, high_water=target_v
    )

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot={"ok": True, "branch": "tencent-cloud-runtime", "head": head, "entries": []},
    )

    assert diag["active"] is True
    assert "repo_baseline_head_mismatch" not in diag["issues"]
    repo = diag.get("repo", {})
    assert repo.get("head_drift_publication_reconciliation") is True
    assert "commit_bot" in repo.get("head_drift_allowed_tools", [])


def test_unproven_window_still_refuses_drift(tmp_path, monkeypatch):
    import pipeline_recovery

    target_v = STRICT_TARGET_V + 7
    checkpoint = _publishing_checkpoint(target_v)
    _root, head = _drifted_head_environment(monkeypatch, tmp_path)
    # High water BELOW the target: the publication is not the proven
    # published window, so the reconciliation door stays shut.
    _prove_window(
        monkeypatch, tmp_path, target_v, high_water=target_v - 1
    )

    diag = pipeline_recovery.checkpoint_recovery_diagnostics(
        checkpoint,
        snapshot={"ok": True, "branch": "tencent-cloud-runtime", "head": head, "entries": []},
    )

    assert "repo_baseline_head_mismatch" in diag["issues"]
    assert diag.get("repo", {}).get("head_drift_publication_reconciliation") is None
