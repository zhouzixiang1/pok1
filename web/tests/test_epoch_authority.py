import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from conftest import STRICT_SOURCE_V, STRICT_TARGET_V
from bot_namespace import bot_name, bot_tag, high_water_tag, parse_bot_version

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")


def _published_parent(name, **_kwargs):
    # parse_bot_version handles whichever active namespace (national_v on main,
    # national_cloud_v on cloud) is configured for this branch, so the parent
    # fixture stays branch-portable.
    version = parse_bot_version(str(name))
    assert version is not None, f"unrecognized bot label: {name!r}"
    return SimpleNamespace(
        eligible=True,
        version=version,
        issues=(),
        runtime_manifest={"epoch": "national_tcp_policy_v1", "version": version},
        epoch_receipt={"epoch": "national_tcp_policy_v1", "version": version},
        publication_identity={
            "published": True,
            "tag": bot_tag(version),
            "version": version,
        },
        certificate_digest="b" * 64,
    )


def _strict_checkpoint(
    next_v,
    source_v,
    *,
    stage="master_planned",
    revision=1,
    published_high_water=None,
    abandoned_receipt_floor=0,
    abandoned_receipt_head_digest=None,
    workflow_attempt=1,
):
    import checkpoint_schema

    binding = checkpoint_schema.build_checkpoint_epoch_binding(
        next_v=next_v,
        source_v=source_v,
        audit_context={},
        published_high_water=(
            next_v - 1 if published_high_water is None else published_high_water
        ),
        abandoned_receipt_floor=abandoned_receipt_floor,
        abandoned_receipt_head_digest=abandoned_receipt_head_digest,
        parent_resolver=_published_parent,
    )
    return {
        "checkpoint_schema_version": checkpoint_schema.CHECKPOINT_SCHEMA_VERSION,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": binding,
        "next_v": next_v,
        "source_v": source_v,
        "parent2_v": None,
        "stage": stage,
        "workflow_run_id": f"generation:{next_v}:workflow-v{workflow_attempt}",
        "checkpoint_revision": revision,
        "audit_context": {},
    }


def _schema2_abandon_claim_for_status():
    import epoch_authority

    target_v = STRICT_TARGET_V + 1
    source_v = STRICT_TARGET_V
    checkpoint = {
        "digest": "d" * 64,
        "next_v": target_v,
        "source_v": source_v,
        "stage": "master_planned",
        "workflow_run_id": f"generation:{target_v}:workflow-v1",
        "checkpoint_revision": 1,
    }
    candidate = {
        "present": True,
        "path": f"bots/{bot_name(target_v)}",
        "manifest_digest": "e" * 64,
        "entry_count": 5,
        "total_bytes": 100,
    }
    git_state = {
        "head": "a" * 40,
        "tracked_worktree_clean": True,
        "candidate_tracked": False,
        "publication_refs": {
            bot_tag(target_v): False,
            high_water_tag(target_v): False,
        },
    }
    ledger = {
        "path_contract": "RESULTS_DIR/abandoned_versions.jsonl",
        "prior_receipt_count": 0,
        "prior_receipt_head_digest": None,
        "receipt_identity": epoch_authority.schema2_abandon_receipt_identity(
            checkpoint,
            "abandon_generation",
        ),
    }
    payload = {
        "schema_version": 2,
        "kind": "national-policy-recorded-abandon-finalize-claim",
        "evaluation_epoch": "national_tcp_policy_v1",
        "git_head": git_state["head"],
        "git_state": git_state,
        "checkout_role": "autonomous_evolution_runtime",
        "transaction_id": "",
        "checkpoint": checkpoint,
        "abandon_reason": "abandon_generation",
        "candidate": candidate,
        "quarantine": epoch_authority.schema2_abandon_quarantine_contract(),
        "ledger": ledger,
    }
    payload["transaction_id"] = epoch_authority._claim_payload_digest(
        epoch_authority.schema2_abandon_transaction_preimage(payload)
    )
    return {
        **payload,
        "claim_digest": epoch_authority._claim_payload_digest(payload),
    }


def test_missing_reset_projects_fresh_v143_and_ignores_old_checkpoint(monkeypatch):
    import epoch_authority
    import evolution_infra

    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "reset_required",
            "initialized": False,
            "strict_published": False,
            "reset_receipt_valid": False,
            "reset_receipt_digest": None,
            "reset_receipt_issues": ["policy_epoch_reset_receipt_missing_or_unsafe"],
            "version_authority_high_water": STRICT_SOURCE_V,
            "first_strict_version": STRICT_TARGET_V,
            "operator_action": "execute_policy_epoch_reset",
            "operator_command": epoch_authority.RESET_COMMAND,
        },
    )
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_SOURCE_V)
    monkeypatch.setattr(evolution_infra, "find_max_committed_v", lambda: STRICT_SOURCE_V)
    monkeypatch.setattr(
        evolution_infra,
        "abandoned_version_authority",
        lambda **_kwargs: {
            "floor": 0,
            "head_digest": None,
            "receipt_count": 0,
        },
    )
    monkeypatch.setattr(evolution_infra, "get_active_bots_read_only", lambda: [])
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: {
            "next_v": STRICT_TARGET_V + 12,
            "source_v": STRICT_SOURCE_V,
            "stage": "direction_audited",
            "workflow_run_id": f"generation:{STRICT_TARGET_V + 12}:workflow-v1",
        },
    )

    projection = epoch_authority.strict_epoch_projection()

    assert projection["current_v"] == STRICT_SOURCE_V
    assert projection["next_v"] == STRICT_TARGET_V
    assert projection["active_generation"] is None
    assert projection["ignored_checkpoint"]["next_v"] == STRICT_TARGET_V + 12
    assert projection["ignored_checkpoint"]["reason"] == (
        "checkpoint_not_bound_to_strict_epoch"
    )


def test_invalid_durable_reset_claim_requires_recovery_not_rerun(tmp_path, monkeypatch):
    import epoch_authority
    import evolution_infra
    import national_runtime_authority

    (tmp_path / "policy_epoch_reset_receipt.json").write_text(
        json.dumps({"kind": "national_tcp_policy_epoch_reset_claim"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_SOURCE_V)
    monkeypatch.setattr(
        evolution_infra,
        "version_namespace_authority",
        lambda: SimpleNamespace(
            high_water=STRICT_SOURCE_V,
            unpaired_completion_versions=(),
            unpaired_high_water_versions=(),
        ),
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda **_kwargs: (),
    )

    state = epoch_authority.policy_epoch_initialization(results_dir=tmp_path)

    assert state["state"] == "reset_evidence_requires_recovery"
    assert state["initialized"] is False
    assert state["operator_action"] == "inspect_policy_epoch_reset_evidence"
    assert state["operator_command"] is None
    assert state["strict_publication_versions_above_high_water"] == []


def test_strict_tag_without_eligible_publication_requires_recovery(tmp_path, monkeypatch):
    import epoch_authority
    import evolution_infra
    import national_runtime_authority

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V + 12)
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda **_kwargs: (),
    )

    state = epoch_authority.policy_epoch_initialization(results_dir=tmp_path)

    assert state["state"] == "version_authority_requires_recovery"
    assert state["initialized"] is False
    assert state["strict_published"] is False
    assert state["operator_action"] == "inspect_strict_version_authority"
    assert state["operator_command"] is None


def test_full_eligible_publication_can_initialize_clean_clone(tmp_path, monkeypatch):
    import epoch_authority
    import evolution_infra
    import national_runtime_authority

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    monkeypatch.setattr(
        evolution_infra,
        "version_namespace_authority",
        lambda: SimpleNamespace(
            high_water=STRICT_TARGET_V,
            paired_versions=(STRICT_TARGET_V,),
            unpaired_completion_versions=(),
            unpaired_high_water_versions=(),
        ),
    )
    observed_ledger_fresh = []

    def strict_bots(**kwargs):
        observed_ledger_fresh.append(kwargs["ledger_fresh"])
        return (bot_name(STRICT_TARGET_V),)

    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        strict_bots,
    )

    state = epoch_authority.policy_epoch_initialization(results_dir=tmp_path)
    projection = epoch_authority.strict_epoch_projection(
        include_checkpoint=False,
    )
    observer_projection = epoch_authority.strict_epoch_projection(
        include_checkpoint=False,
        ledger_fresh=False,
    )

    assert state["state"] == "strict_published"
    assert state["initialized"] is True
    assert state["strict_published"] is True
    assert state["strict_published_bots"] == [bot_name(STRICT_TARGET_V)]
    assert state["strict_published_bot_identities"] == [{
        "generation_ordinal": 1,
        "canonical_version": STRICT_TARGET_V,
        "canonical_bot_name": bot_name(STRICT_TARGET_V),
        "canonical_tag": bot_tag(STRICT_TARGET_V),
    }]
    assert state["namespace_publication_proven"] is True
    assert state["strict_publication_versions_above_high_water"] == []
    assert projection["active_bots"] == [bot_name(STRICT_TARGET_V)]
    assert projection["strict_published_versions"] == [STRICT_TARGET_V]
    assert observer_projection["active_bots"] == [bot_name(STRICT_TARGET_V)]
    assert observed_ledger_fresh == [True, True, False]


def test_reaped_active_pool_subset_does_not_renumber_published_history(
    tmp_path,
    monkeypatch,
):
    import epoch_authority
    import evolution_infra
    import national_runtime_authority

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V + 4)
    monkeypatch.setattr(
        evolution_infra,
        "version_namespace_authority",
        lambda: SimpleNamespace(
            high_water=STRICT_TARGET_V + 4,
            paired_versions=(STRICT_TARGET_V, STRICT_TARGET_V + 4),
            unpaired_completion_versions=(),
            unpaired_high_water_versions=(),
        ),
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda **_kwargs: (bot_name(STRICT_TARGET_V + 4),),
    )
    monkeypatch.setattr(
        evolution_infra,
        "abandoned_version_authority",
        lambda **_kwargs: {
            "floor": 0,
            "head_digest": None,
            "receipt_count": 0,
        },
    )
    checkpoint = _strict_checkpoint(
        STRICT_TARGET_V + 5,
        STRICT_TARGET_V + 4,
        published_high_water=STRICT_TARGET_V + 4,
    )
    monkeypatch.setattr(
        evolution_infra,
        "PIPELINE_STATE_FILE",
        tmp_path / "pipeline_state.json",
    )
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: checkpoint,
    )

    state = epoch_authority.policy_epoch_initialization(results_dir=tmp_path)
    projection = epoch_authority.strict_epoch_projection()

    assert state["initialized"] is True
    assert state["strict_published_bots"] == [bot_name(STRICT_TARGET_V + 4)]
    assert state["strict_published_versions"] == [
        STRICT_TARGET_V,
        STRICT_TARGET_V + 4,
    ]
    assert [
        identity["generation_ordinal"]
        for identity in state["strict_published_bot_identities"]
    ] == [1, 2]
    assert projection["active_bots"] == [bot_name(STRICT_TARGET_V + 4)]
    assert projection["strict_generation_count"] == 2
    assert projection["active_generation"]["canonical_version"] == STRICT_TARGET_V + 5
    assert projection["active_generation"]["generation_ordinal"] == 3


def test_namespace_second_read_failure_does_not_project_active_bot_authority(
    tmp_path,
    monkeypatch,
):
    import epoch_authority
    import evolution_infra
    import national_runtime_authority

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(
        evolution_infra,
        "PIPELINE_STATE_FILE",
        tmp_path / "pipeline_state.json",
    )
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    monkeypatch.setattr(
        evolution_infra,
        "version_namespace_authority",
        lambda: (_ for _ in ()).throw(RuntimeError("transient namespace read")),
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda **_kwargs: (bot_name(STRICT_TARGET_V),),
    )

    initialization = epoch_authority.policy_epoch_initialization(
        results_dir=tmp_path,
    )
    projection = epoch_authority.strict_epoch_projection(
        include_checkpoint=False,
    )

    assert initialization["initialized"] is False
    assert initialization["state"] == "version_authority_requires_recovery"
    assert initialization["strict_published_bots"] == []
    assert projection["active_bots"] == []
    assert projection["strict_published_versions"] == []
    assert projection["strict_generation_count"] == 0


@pytest.mark.parametrize(
    "strict_bots",
    (
        (f"strict_target_plus_one",),
        (f"strict_target_and_plus_one",),
    ),
)
def test_strict_publication_must_match_paired_namespace_high_water(
    tmp_path,
    monkeypatch,
    strict_bots,
):
    import epoch_authority
    import evolution_infra
    import national_runtime_authority

    bot_labels = {
        "strict_target_plus_one": (bot_name(STRICT_TARGET_V + 1),),
        "strict_target_and_plus_one": (
            bot_name(STRICT_TARGET_V),
            bot_name(STRICT_TARGET_V + 1),
        ),
    }[strict_bots[0]]

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    monkeypatch.setattr(
        evolution_infra,
        "version_namespace_authority",
        lambda: SimpleNamespace(
            high_water=STRICT_TARGET_V,
            unpaired_completion_versions=(),
            unpaired_high_water_versions=(),
        ),
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda **_kwargs: bot_labels,
    )

    state = epoch_authority.policy_epoch_initialization(results_dir=tmp_path)
    projection = epoch_authority.strict_epoch_projection(
        include_checkpoint=False,
    )

    assert state["state"] == "version_authority_requires_recovery"
    assert state["initialized"] is False
    assert state["namespace_publication_proven"] is False
    assert state["strict_published_bots"] == []
    assert state["strict_publication_versions_above_high_water"] == [
        STRICT_TARGET_V + 1
    ]
    assert state["operator_action"] == "inspect_strict_version_authority"
    assert projection["active_bots"] == []
    assert projection["strict_published_versions"] == []


@pytest.mark.parametrize("namespace_high_water", (STRICT_SOURCE_V, STRICT_TARGET_V + 1))
def test_namespace_high_water_drift_withholds_active_bot_authority(
    tmp_path,
    monkeypatch,
    namespace_high_water,
):
    import epoch_authority
    import evolution_infra
    import national_runtime_authority

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    monkeypatch.setattr(
        evolution_infra,
        "version_namespace_authority",
        lambda: SimpleNamespace(
            high_water=namespace_high_water,
            unpaired_completion_versions=(),
            unpaired_high_water_versions=(),
        ),
    )
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda **_kwargs: (bot_name(STRICT_TARGET_V),),
    )

    state = epoch_authority.policy_epoch_initialization(results_dir=tmp_path)
    projection = epoch_authority.strict_epoch_projection(
        include_checkpoint=False,
    )

    assert state["state"] == "version_authority_requires_recovery"
    assert state["initialized"] is False
    assert state["namespace_publication_proven"] is False
    assert state["strict_published_bots"] == []
    assert projection["active_bots"] == []
    assert projection["strict_generation_count"] == 0


def test_abandoned_floor_is_epoch_scoped(tmp_path, monkeypatch):
    import epoch_authority
    import evolution_infra

    abandoned = tmp_path / "abandoned_versions.jsonl"
    abandoned.write_text(
        '{"v": %d}\n{"v": %d}\n' % (STRICT_TARGET_V + 12, STRICT_TARGET_V + 24),
        encoding="utf-8",
    )
    monkeypatch.setattr(evolution_infra, "ABANDONED_VERSIONS_FILE", abandoned)
    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {"initialized": False},
    )
    assert evolution_infra.find_abandoned_version_floor() == 0

    # Once the new epoch is initialized, legacy/unbound JSONL is ambiguity and
    # must stop allocation instead of being skipped or trusted.
    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {
            "initialized": True,
            "state": "strict_published",
            "strict_published": True,
        },
    )
    with pytest.raises(evolution_infra.AbandonedVersionLedgerError):
        evolution_infra.find_abandoned_version_floor()

    abandoned.unlink()
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    first = evolution_infra.append_abandoned_version_receipt(
        _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V),
        reason="test-v144",
        timestamp=1.0,
        path=abandoned,
        project_root=tmp_path,
    )
    evolution_infra.append_abandoned_version_receipt(
        _strict_checkpoint(
            STRICT_TARGET_V + 2,
            STRICT_TARGET_V,
            published_high_water=STRICT_TARGET_V,
            abandoned_receipt_floor=STRICT_TARGET_V + 1,
            abandoned_receipt_head_digest=evolution_infra._abandoned_ledger_head_digest(
                [first]
            ),
        ),
        reason="test-v145",
        timestamp=2.0,
        path=abandoned,
        project_root=tmp_path,
    )
    assert evolution_infra.find_abandoned_version_floor() == STRICT_TARGET_V + 2


def test_failed_reserved_v143_attempt_is_audited_but_does_not_burn_label(
    monkeypatch,
):
    import epoch_authority
    import evolution_infra

    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {
            "initialized": True,
            "state": "fresh_bootstrap_ready",
            "strict_published": False,
        },
    )
    receipts = [{
        "version": STRICT_TARGET_V,
        "receipt_digest": "a" * 64,
        "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v18",
    }]
    monkeypatch.setattr(
        evolution_infra,
        "load_abandoned_version_receipts",
        lambda **_kwargs: list(receipts),
    )

    assert evolution_infra.find_abandoned_version_floor() == 0
    assert evolution_infra.abandoned_version_attempt_count(STRICT_TARGET_V) == 18

    receipts.append({
        "version": STRICT_TARGET_V + 1,
        "receipt_digest": "b" * 64,
        "workflow_run_id": f"generation:{STRICT_TARGET_V + 1}:workflow-v1",
    })
    assert evolution_infra.find_abandoned_version_floor() == STRICT_TARGET_V + 1


def test_abandon_receipt_is_checkpoint_bound_and_tamper_evident(
    tmp_path,
    monkeypatch,
):
    import evolution_infra

    ledger = tmp_path / "abandoned_versions.jsonl"
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    first = evolution_infra.append_abandoned_version_receipt(
        _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V, revision=3),
        reason="quality-repair-exhausted",
        timestamp=1.0,
        path=ledger,
        project_root=tmp_path,
    )
    second = evolution_infra.append_abandoned_version_receipt(
        _strict_checkpoint(
            STRICT_TARGET_V + 2,
            STRICT_TARGET_V,
            revision=7,
            published_high_water=STRICT_TARGET_V,
            abandoned_receipt_floor=STRICT_TARGET_V + 1,
            abandoned_receipt_head_digest=evolution_infra._abandoned_ledger_head_digest(
                [first]
            ),
        ),
        reason="native-precommit-exhausted",
        timestamp=2.0,
        path=ledger,
        project_root=tmp_path,
    )

    receipts = evolution_infra.load_abandoned_version_receipts(
        path=ledger,
        project_root=tmp_path,
    )
    assert [row["version"] for row in receipts] == [
        STRICT_TARGET_V + 1,
        STRICT_TARGET_V + 2,
    ]
    # The per-row identity digest is a stable content fingerprint that uniquely
    # identifies each durable receipt without chaining on a per-row field.
    assert (
        evolution_infra._abandoned_version_receipt_identity_digest(first)
        != evolution_infra._abandoned_version_receipt_identity_digest(second)
    )
    assert receipts[0]["checkpoint_envelope"]["epoch_binding"][
        "binding_digest"
    ] == _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V, revision=3)["epoch_binding"][
        "binding_digest"
    ]

    # Tamper-evidence now flows from the structural validation of every row
    # plus the whole-ledger holistic hash consumed by the allocation CAS.
    # Mutating one row's bound version breaks the envelope/version binding the
    # loader re-validates on every read, so the tampered ledger fails closed.
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    rows[0]["version"] = STRICT_TARGET_V + 999
    ledger.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        evolution_infra.AbandonedVersionLedgerError,
        match="checkpoint_version_mismatch|version order regressed",
    ):
        evolution_infra.load_abandoned_version_receipts(
            path=ledger,
            project_root=tmp_path,
        )


def test_abandon_receipt_replay_is_idempotent_after_checkpoint_clear_failure(
    tmp_path,
    monkeypatch,
):
    import evolution_infra

    ledger = tmp_path / "abandoned_versions.jsonl"
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    checkpoint = _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V, revision=9)
    first = evolution_infra.append_abandoned_version_receipt(
        checkpoint,
        reason="checkpoint-clear-failed",
        timestamp=1.0,
        path=ledger,
        project_root=tmp_path,
    )
    replay = evolution_infra.append_abandoned_version_receipt(
        checkpoint,
        reason="checkpoint-clear-failed",
        timestamp=999.0,
        path=ledger,
        project_root=tmp_path,
    )

    assert replay == first
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1
    with pytest.raises(
        evolution_infra.AbandonedVersionLedgerError,
        match="different payload",
    ):
        evolution_infra.append_abandoned_version_receipt(
            checkpoint,
            reason="changed-reason",
            path=ledger,
            project_root=tmp_path,
        )
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1


def test_historical_abandon_receipt_cannot_be_replayed_after_chain_advances(
    tmp_path,
    monkeypatch,
):
    import evolution_infra

    ledger = tmp_path / "abandoned_versions.jsonl"
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    stale_checkpoint = _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V, revision=3)
    first = evolution_infra.append_abandoned_version_receipt(
        stale_checkpoint,
        reason="first-terminal-command",
        timestamp=1.0,
        path=ledger,
        project_root=tmp_path,
    )
    evolution_infra.append_abandoned_version_receipt(
        _strict_checkpoint(
            STRICT_TARGET_V + 2,
            STRICT_TARGET_V,
            revision=4,
            published_high_water=STRICT_TARGET_V,
            abandoned_receipt_floor=STRICT_TARGET_V + 1,
            abandoned_receipt_head_digest=evolution_infra._abandoned_ledger_head_digest(
                [first]
            ),
        ),
        reason="later-terminal-command",
        timestamp=2.0,
        path=ledger,
        project_root=tmp_path,
    )

    with pytest.raises(
        evolution_infra.AbandonedVersionLedgerError,
        match="not the unique (chain head|ledger tail)",
    ):
        evolution_infra.append_abandoned_version_receipt(
            stale_checkpoint,
            reason="first-terminal-command",
            timestamp=3.0,
            path=ledger,
            project_root=tmp_path,
        )
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2


def test_concurrent_same_checkpoint_abandon_is_one_durable_receipt(
    tmp_path,
    monkeypatch,
):
    import evolution_infra

    ledger = tmp_path / "abandoned_versions.jsonl"
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    checkpoint = _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V, revision=3)

    def append(_index):
        return evolution_infra.append_abandoned_version_receipt(
            checkpoint,
            reason="concurrent-terminal-command",
            path=ledger,
            project_root=tmp_path,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(append, range(16)))

    assert (
        len(
            {
                evolution_infra._abandoned_version_receipt_identity_digest(row)
                for row in receipts
            }
        )
        == 1
    )
    assert len(evolution_infra.load_abandoned_version_receipts(
        path=ledger,
        project_root=tmp_path,
    )) == 1


def test_abandon_receipt_size_preflight_never_mutates_ledger(
    tmp_path,
    monkeypatch,
):
    import evolution_infra

    ledger = tmp_path / "abandoned_versions.jsonl"
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    with pytest.raises(
        evolution_infra.AbandonedVersionLedgerError,
        match="reason exceeds byte limit",
    ):
        evolution_infra.append_abandoned_version_receipt(
            _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V),
            reason="x" * (evolution_infra._ABANDONED_VERSION_REASON_MAX_BYTES + 1),
            path=ledger,
            project_root=tmp_path,
        )
    assert not ledger.exists()
    with pytest.raises(
        evolution_infra.AbandonedVersionLedgerError,
        match="infra_failure exceeds byte limit",
    ):
        evolution_infra.append_abandoned_version_receipt(
            _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V),
            reason="oversize-infra",
            infra_failure={
                "detail": "x" * evolution_infra._ABANDONED_VERSION_INFRA_FAILURE_MAX_BYTES
            },
            path=ledger,
            project_root=tmp_path,
        )
    assert not ledger.exists()

    first = evolution_infra.append_abandoned_version_receipt(
        _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V),
        reason="first",
        path=ledger,
        project_root=tmp_path,
    )
    before = ledger.read_bytes()
    monkeypatch.setattr(
        evolution_infra,
        "_ABANDONED_VERSION_LEDGER_MAX_BYTES",
        len(before) + 8,
    )
    with pytest.raises(
        evolution_infra.AbandonedVersionLedgerError,
        match="would exceed byte limit",
    ):
        evolution_infra.append_abandoned_version_receipt(
            _strict_checkpoint(
                STRICT_TARGET_V + 2,
                STRICT_TARGET_V,
                published_high_water=STRICT_TARGET_V,
                abandoned_receipt_floor=STRICT_TARGET_V + 1,
                abandoned_receipt_head_digest=evolution_infra._abandoned_ledger_head_digest(
                    [first]
                ),
            ),
            reason="second",
            path=ledger,
            project_root=tmp_path,
        )
    assert ledger.read_bytes() == before


def test_abandon_sidecar_lock_symlink_is_rejected_without_data_creation(
    tmp_path,
    monkeypatch,
):
    import evolution_infra

    ledger = tmp_path / "abandoned_versions.jsonl"
    lock = ledger.with_suffix(".jsonl.lock")
    outside = tmp_path / "outside.lock"
    outside.write_text("untouched", encoding="utf-8")
    lock.symlink_to(outside)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)

    with pytest.raises(
        evolution_infra.AbandonedVersionLedgerError,
        match="append failed|sidecar lock",
    ):
        evolution_infra.append_abandoned_version_receipt(
            _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V),
            reason="unsafe-lock",
            path=ledger,
            project_root=tmp_path,
        )
    assert not ledger.exists()
    assert outside.read_text(encoding="utf-8") == "untouched"


@pytest.mark.parametrize(
    "contents",
    (
        "",
        "{not-json}\n",
        '{"schema_version":1}',
        '{"schema_version":1}\n\n',
    ),
)
def test_initialized_abandon_ledger_corruption_fails_closed(
    tmp_path,
    contents,
):
    import evolution_infra

    ledger = tmp_path / "abandoned_versions.jsonl"
    ledger.write_text(contents, encoding="utf-8")
    with pytest.raises(evolution_infra.AbandonedVersionLedgerError):
        evolution_infra.load_abandoned_version_receipts(
            path=ledger,
            project_root=tmp_path,
        )


def test_abandon_ledger_byte_limit_is_checked_before_read(
    tmp_path,
    monkeypatch,
):
    import evolution_infra

    ledger = tmp_path / "abandoned_versions.jsonl"
    ledger.write_bytes(b"x" * 9)
    monkeypatch.setattr(
        evolution_infra,
        "_ABANDONED_VERSION_LEDGER_MAX_BYTES",
        8,
    )
    with pytest.raises(
        evolution_infra.AbandonedVersionLedgerError,
        match="exceeds byte limit",
    ):
        evolution_infra.load_abandoned_version_receipts(
            path=ledger,
            project_root=tmp_path,
        )


def test_unreadable_or_symlinked_abandon_ledger_fails_closed(
    tmp_path,
    monkeypatch,
):
    import evolution_infra

    ledger = tmp_path / "abandoned_versions.jsonl"
    ledger.write_text("placeholder\n", encoding="utf-8")

    def denied(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(evolution_infra, "_locked_state_sidecar", denied)
    with pytest.raises(
        evolution_infra.AbandonedVersionLedgerError,
        match="unreadable",
    ):
        evolution_infra.load_abandoned_version_receipts(
            path=ledger,
            project_root=tmp_path,
        )

    monkeypatch.undo()
    target = tmp_path / "target.jsonl"
    target.write_text("placeholder\n", encoding="utf-8")
    ledger.unlink()
    ledger.symlink_to(target)
    with pytest.raises(
        evolution_infra.AbandonedVersionLedgerError,
        match="symlink",
    ):
        evolution_infra.load_abandoned_version_receipts(
            path=ledger,
            project_root=tmp_path,
        )


def test_abandon_receipt_rejects_unbound_checkpoint_without_creating_ledger(
    tmp_path,
):
    import evolution_infra

    ledger = tmp_path / "abandoned_versions.jsonl"
    with pytest.raises(
        evolution_infra.AbandonedVersionLedgerError,
        match="checkpoint",
    ):
        evolution_infra.append_abandoned_version_receipt(
            {
                "next_v": STRICT_TARGET_V + 1,
                "source_v": STRICT_TARGET_V,
                "stage": "master_planned",
                "workflow_run_id": f"generation:{STRICT_TARGET_V + 1}:unbound",
                "checkpoint_revision": 1,
            },
            reason="must-not-bind",
            path=ledger,
            project_root=tmp_path,
        )
    assert not ledger.exists()


def test_bare_commit_diagnostic_cannot_advance_allocation():
    import evolution_infra

    assert evolution_infra.compute_next_generation_v(
        current_v=STRICT_TARGET_V,
        max_committed_v=999,
        abandoned_floor=STRICT_TARGET_V + 1,
    ) == STRICT_TARGET_V + 2


@pytest.mark.parametrize(
    "target",
    (STRICT_TARGET_V + 1, STRICT_TARGET_V + 3, STRICT_TARGET_V + 7),
)
def test_checkpoint_binding_rejects_non_successor_target(target):
    import checkpoint_schema

    with pytest.raises(
        checkpoint_schema.CheckpointSchemaError,
        match="checkpoint_target_not_allocation_floor_successor",
    ):
        checkpoint_schema.build_checkpoint_epoch_binding(
            next_v=target,
            source_v=STRICT_TARGET_V,
            audit_context={},
            published_high_water=STRICT_TARGET_V,
            abandoned_receipt_floor=STRICT_TARGET_V + 1,
            abandoned_receipt_head_digest="a" * 64,
            parent_resolver=_published_parent,
        )


def test_live_checkpoint_rejects_published_floor_and_ledger_head_drift():
    import checkpoint_schema

    checkpoint = _strict_checkpoint(
        STRICT_TARGET_V + 2,
        STRICT_TARGET_V,
        published_high_water=STRICT_TARGET_V,
        abandoned_receipt_floor=STRICT_TARGET_V + 1,
        abandoned_receipt_head_digest="a" * 64,
    )
    errors = checkpoint_schema.live_checkpoint_allocation_authority_errors(
        checkpoint,
        published_high_water=STRICT_TARGET_V + 2,
        abandoned_receipt_floor=STRICT_TARGET_V + 2,
        abandoned_receipt_head_digest="b" * 64,
    )
    assert "checkpoint_target_not_above_live_allocation_floor" in errors
    assert "checkpoint_published_high_water_changed" in errors
    assert "checkpoint_abandoned_receipt_head_changed" in errors


def test_projection_splits_published_high_water_from_allocation_floor(
    tmp_path,
    monkeypatch,
):
    import epoch_authority
    import evolution_infra

    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "strict_published",
            "initialized": True,
            "strict_published": True,
            "strict_published_bots": [bot_name(STRICT_TARGET_V)],
            "reset_receipt_valid": False,
            "reset_receipt_digest": None,
            "reset_receipt_issues": [],
            "version_authority_high_water": STRICT_TARGET_V,
            "first_strict_version": STRICT_TARGET_V,
            "operator_action": None,
            "operator_command": None,
        },
    )
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    monkeypatch.setattr(
        evolution_infra,
        "abandoned_version_authority",
        lambda **_kwargs: {
            "floor": STRICT_TARGET_V + 2,
            "head_digest": "a" * 64,
            "receipt_count": 1,
        },
    )
    monkeypatch.setattr(
        evolution_infra,
        "find_max_committed_v",
        lambda: (_ for _ in ()).throw(AssertionError("bare commit scan forbidden")),
    )
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: None)

    projection = epoch_authority.strict_epoch_projection()

    assert projection["published_high_water"] == STRICT_TARGET_V
    assert projection["allocation_floor"] == STRICT_TARGET_V + 2
    assert projection["abandoned_receipt_floor"] == STRICT_TARGET_V + 2
    assert projection["next_v"] == STRICT_TARGET_V + 3
    assert projection["max_committed_v"] == STRICT_TARGET_V
    assert projection["next_v_authority"] == "published_tags_and_abandon_receipts"


@pytest.mark.parametrize(
    ("checkpoint_case", "expected_issue"),
    (
        ("archived", "terminal_checkpoint_requires_cleanup:archived"),
        ("abandoned", "terminal_checkpoint_requires_cleanup:abandoned"),
        ("missing_stage", "checkpoint_stage_missing"),
        ("missing_next_v", "checkpoint_next_v_missing"),
    ),
)
def test_claimed_terminal_or_incomplete_checkpoint_never_becomes_scheduler_boundary(
    tmp_path,
    monkeypatch,
    checkpoint_case,
    expected_issue,
):
    import epoch_authority
    import evolution_infra
    import server.routes.control as control

    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "strict_published",
            "initialized": True,
            "strict_published": True,
            "strict_published_bots": [bot_name(STRICT_TARGET_V)],
            "reset_receipt_valid": False,
            "reset_receipt_digest": None,
            "reset_receipt_issues": [],
            "version_authority_high_water": STRICT_TARGET_V,
            "first_strict_version": STRICT_TARGET_V,
            "operator_action": None,
            "operator_command": None,
        },
    )
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    monkeypatch.setattr(
        evolution_infra,
        "abandoned_version_authority",
        lambda **_kwargs: {
            "floor": STRICT_TARGET_V,
            "head_digest": None,
            "receipt_count": 0,
        },
    )
    monkeypatch.setattr(
        evolution_infra,
        "PIPELINE_STATE_FILE",
        tmp_path / "pipeline_state.json",
    )
    if checkpoint_case in {"archived", "abandoned"}:
        checkpoint = _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V, stage=checkpoint_case)
    elif checkpoint_case == "missing_stage":
        checkpoint = {"stage": None, "next_v": STRICT_TARGET_V + 1, "source_v": STRICT_TARGET_V}
    else:
        checkpoint = {"stage": "master_planned", "next_v": None, "source_v": STRICT_TARGET_V}
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: checkpoint,
    )

    projection = epoch_authority.strict_epoch_projection()
    health = control._read_pipeline_health({
        **projection,
        "epoch_initialized": True,
        "epoch_state": projection["state"],
        "post_publication_handoff": {"status": "none"},
    })

    assert projection["active_generation"] is None
    assert projection["operator_action"] == "archive_incompatible_checkpoint"
    assert expected_issue in projection["ignored_checkpoint"]["issues"]
    assert health["blocked"] is True
    assert health["scheduler_boundary"] is None


def test_checkpoint_disappearing_during_projection_is_not_clean_scheduler_boundary(
    tmp_path,
    monkeypatch,
):
    import epoch_authority
    import evolution_infra
    import server.routes.control as control

    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "strict_published",
            "initialized": True,
            "strict_published": True,
            "strict_published_bots": [bot_name(STRICT_TARGET_V)],
            "reset_receipt_valid": False,
            "reset_receipt_digest": None,
            "reset_receipt_issues": [],
            "version_authority_high_water": STRICT_TARGET_V,
            "first_strict_version": STRICT_TARGET_V,
            "operator_action": None,
            "operator_command": None,
        },
    )
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    monkeypatch.setattr(
        evolution_infra,
        "abandoned_version_authority",
        lambda **_kwargs: {
            "floor": STRICT_TARGET_V,
            "head_digest": None,
            "receipt_count": 0,
        },
    )
    monkeypatch.setattr(
        evolution_infra,
        "PIPELINE_STATE_FILE",
        tmp_path / "pipeline_state.json",
    )
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: None)
    observations = iter((True, False))
    monkeypatch.setattr(
        epoch_authority.os.path,
        "lexists",
        lambda _path: next(observations),
    )

    projection = epoch_authority.strict_epoch_projection()
    health = control._read_pipeline_health({
        **projection,
        "epoch_initialized": True,
        "epoch_state": projection["state"],
        "post_publication_handoff": {"status": "none"},
    })

    assert projection["active_generation"] is None
    assert projection["operator_action"] == "archive_incompatible_checkpoint"
    assert projection["ignored_checkpoint"]["issues"] == [
        "checkpoint_disappeared_during_read"
    ]
    assert health["blocked"] is True
    assert health["scheduler_boundary"] is None


def test_valid_active_checkpoint_owns_target_but_not_published_high_water(
    tmp_path,
    monkeypatch,
):
    import epoch_authority
    import evolution_infra

    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "strict_published",
            "initialized": True,
            "strict_published": True,
            "strict_published_bots": [bot_name(STRICT_TARGET_V)],
            "reset_receipt_valid": False,
            "reset_receipt_digest": None,
            "reset_receipt_issues": [],
            "version_authority_high_water": STRICT_TARGET_V,
            "first_strict_version": STRICT_TARGET_V,
            "operator_action": None,
            "operator_command": None,
        },
    )
    checkpoint = _strict_checkpoint(
        STRICT_TARGET_V + 2,
        STRICT_TARGET_V,
        revision=8,
        published_high_water=STRICT_TARGET_V,
        abandoned_receipt_floor=STRICT_TARGET_V + 1,
        abandoned_receipt_head_digest="a" * 64,
    )
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    monkeypatch.setattr(
        evolution_infra,
        "abandoned_version_authority",
        lambda **_kwargs: {
            "floor": STRICT_TARGET_V + 1,
            "head_digest": "a" * 64,
            "receipt_count": 1,
        },
    )
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)

    projection = epoch_authority.strict_epoch_projection()

    assert projection["published_high_water"] == STRICT_TARGET_V
    assert projection["allocation_floor"] == STRICT_TARGET_V + 1
    assert projection["next_v"] == STRICT_TARGET_V + 2
    assert projection["next_v_authority"] == "active_checkpoint_epoch_binding"
    assert projection["active_generation"]["checkpoint_revision"] == 8
    assert projection["active_generation"]["next_v"] == STRICT_TARGET_V + 2
    assert projection["active_generation"]["canonical_version"] == STRICT_TARGET_V + 2
    # The intervening version is canonically abandoned and therefore consumes no
    # user-visible Bot ordinal.  The next candidate remains the second potential
    # Bot while retaining its immutable canonical identity.
    assert projection["active_generation"]["generation_ordinal"] == 2
    assert projection["active_generation"]["canonical_bot_name"] == bot_name(
        STRICT_TARGET_V + 2
    )
    assert projection["active_generation"]["canonical_tag"] == bot_tag(
        STRICT_TARGET_V + 2
    )


def test_projection_routes_exact_recorded_abandon_to_cas_finalize(
    tmp_path,
    monkeypatch,
):
    import epoch_authority
    import evolution_infra

    checkpoint = _strict_checkpoint(STRICT_TARGET_V + 1, STRICT_TARGET_V, revision=6)
    ledger = tmp_path / "abandoned_versions.jsonl"
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    terminal = evolution_infra.append_abandoned_version_receipt(
        checkpoint,
        reason="clear-cas-failed",
        path=ledger,
        project_root=tmp_path,
    )
    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "strict_published",
            "initialized": True,
            "strict_published": True,
            "strict_published_bots": [bot_name(STRICT_TARGET_V)],
            "reset_receipt_valid": False,
            "reset_receipt_digest": None,
            "reset_receipt_issues": [],
            "version_authority_high_water": STRICT_TARGET_V,
            "first_strict_version": STRICT_TARGET_V,
            "operator_action": None,
            "operator_command": None,
        },
    )
    monkeypatch.setattr(
        evolution_infra,
        "abandoned_version_authority",
        lambda **_kwargs: {
            "floor": STRICT_TARGET_V + 1,
            "head_digest": evolution_infra._abandoned_version_receipt_identity_digest(
                terminal
            ),
            "receipt_count": 1,
        },
    )
    monkeypatch.setattr(evolution_infra, "ABANDONED_VERSIONS_FILE", ledger)
    monkeypatch.setattr(evolution_infra, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: checkpoint)

    projection = epoch_authority.strict_epoch_projection()

    assert projection["next_v_authority"] == "recorded_abandon_checkpoint_finalize"
    assert projection["active_generation"]["recovery_kind"] == (
        "recorded_abandon_checkpoint_finalize"
    )
    assert projection["operator_action"] == "finalize_recorded_abandon_checkpoint"
    assert "--finalize-recorded-abandon-checkpoint" in projection["operator_command"]


@pytest.mark.asyncio
async def test_scheduler_fails_closed_when_allocation_authority_is_unreadable(
    monkeypatch,
):
    import epoch_authority
    import generation_scheduler

    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("abandon receipt ledger tampered")
        ),
    )
    events = []
    monkeypatch.setattr(
        generation_scheduler,
        "log_system_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    result = await generation_scheduler.prepare_generation(None)

    assert result is None
    assert events[0][0][0] == "pipeline.prepare_blocked_version_authority"


@pytest.mark.asyncio
async def test_scheduler_resumes_only_canonical_active_checkpoint(monkeypatch):
    import epoch_authority
    import generation_scheduler

    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda **_kwargs: {
            "initialized": True,
            "current_v": STRICT_TARGET_V,
            "published_high_water": STRICT_TARGET_V,
            "allocation_floor": STRICT_TARGET_V + 1,
            "abandoned_receipt_floor": STRICT_TARGET_V + 1,
            "next_v": STRICT_TARGET_V + 7,
            "next_v_authority": "active_checkpoint_epoch_binding",
            "ignored_checkpoint": None,
            "active_generation": {
                "next_v": STRICT_TARGET_V + 7,
                "source_v": STRICT_TARGET_V,
                "parent2_v": None,
                "stage": "master_planned",
            },
        },
    )
    monkeypatch.setattr(
        generation_scheduler,
        "log_system_event",
        lambda *_args, **_kwargs: None,
    )

    result = await generation_scheduler.prepare_generation(None)

    assert result is not None
    assert result.current_v == STRICT_TARGET_V
    assert result.next_v == STRICT_TARGET_V + 7
    assert result.source_v == STRICT_TARGET_V


def test_reset_command_contains_required_runtime_acknowledgement():
    from epoch_authority import RESET_COMMAND

    assert "--execute" in RESET_COMMAND
    assert "--acknowledge-runtime-checkout" in RESET_COMMAND


def test_first_strict_operator_transition_is_digest_bound_for_all_four_states():
    import epoch_authority

    checkpoint = {
        "next_v": STRICT_TARGET_V,
        "source_v": STRICT_SOURCE_V,
        "stage": "official_bootstrap_required",
        "workflow_run_id": f"generation:{STRICT_TARGET_V}:transition-digest",
        "checkpoint_revision": 11,
        "audit_context": {
            "official_bootstrap_request": {
                "candidate_hash": "a" * 64,
                "request_digest": "b" * 64,
            }
        },
    }
    expected_contract = {
        "bootstrap_required": (
            "run_first_strict_official_certification",
            epoch_authority.FIRST_STRICT_BOOTSTRAP_COMMAND,
        ),
        "bootstrap_running": (
            "wait_for_first_strict_official_certification",
            None,
        ),
        "bootstrap_failed": (
            "retry_first_strict_official_certification",
            epoch_authority.FIRST_STRICT_BOOTSTRAP_RETRY_COMMAND,
        ),
        "ready_to_finalize": (
            "finalize_first_strict_publication",
            epoch_authority.FIRST_STRICT_FINALIZE_COMMAND,
        ),
    }
    for state, (action, command) in expected_contract.items():
        transition = epoch_authority.first_strict_operator_transition(
            checkpoint,
            state=state,
            job_id="c" * 64 if state != "bootstrap_required" else None,
            certificate_digest=(
                "d" * 64 if state == "ready_to_finalize" else None
            ),
        )
        unsigned = {
            key: value
            for key, value in transition.items()
            if key != "transition_digest"
        }
        expected = hashlib.sha256(json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")).hexdigest()
        assert transition["transition_digest"] == expected
        assert transition["candidate_hash"] == "a" * 64
        assert transition["parked_request_digest"] == "b" * 64
        assert transition["source_v"] == STRICT_SOURCE_V
        assert transition["action"] == action
        assert transition["command"] == command

    with pytest.raises(ValueError, match="invalid first-strict operator transition"):
        epoch_authority.first_strict_operator_transition(
            checkpoint,
            state="automatic_commit",
        )
    with pytest.raises(ValueError, match="requires durable job and certificate"):
        epoch_authority.first_strict_operator_transition(
            checkpoint,
            state="ready_to_finalize",
            job_id="c" * 64,
        )
    with pytest.raises(TypeError):
        epoch_authority.first_strict_operator_transition(
            checkpoint,
            state="bootstrap_failed",
            opponent_authority="archive",
        )


@pytest.mark.parametrize(
    "completion_type,high_water_type,high_water_commit",
    (
        ("commit", "tag", "a" * 40),
        ("tag", "", ""),
        ("tag", "tag", "b" * 40),
    ),
)
def test_publication_ref_proof_rejects_lightweight_missing_or_wrong_tree_tag(
    monkeypatch,
    completion_type,
    high_water_type,
    high_water_commit,
):
    import evolution_infra

    target_v = STRICT_TARGET_V + 1
    completion_tag = bot_tag(target_v)
    high_water = high_water_tag(target_v)
    intent = {
        "completion_tag": completion_tag,
        "high_water_tag": high_water,
        "tag_message": "exact-message",
    }
    commit = "a" * 40

    def fake_git(*args, **_kwargs):
        if args[:2] == ("cat-file", "-t"):
            return (
                completion_type
                if args[2].endswith(completion_tag)
                else high_water_type
            )
        if args[0] == "rev-parse" and args[1].endswith("^{commit}"):
            return (
                commit
                if completion_tag in args[1]
                else high_water_commit
            )
        if args[0] == "rev-parse":
            return "c" * 40
        if args[0] == "for-each-ref":
            return "exact-message"
        return ""

    monkeypatch.setattr(evolution_infra, "_git", fake_git)
    with pytest.raises(RuntimeError, match="invalid local publication refs"):
        evolution_infra._validate_local_publication_refs(intent, commit)


@pytest.mark.parametrize("intent_cert", ("", None))
@pytest.mark.parametrize("spec_cert", (None, ""))
def test_publication_reconciliation_allows_certless_none_vs_empty(
    tmp_path,
    monkeypatch,
    intent_cert,
    spec_cert,
):
    """Certificate-removal regression (v186, 2026-08-16): a cert-less
    publication intent carries ''/None while the resolved bot spec carries
    None — the raw != refused the post-publish baseline bind for EVERY
    cert-less publication, stranding published bots at `publishing` until
    manual recovery. All "no certificate" spellings must compare equal."""
    import evolution_infra
    import national_runtime_authority
    import publication_transaction
    from types import SimpleNamespace

    bot_dir = tmp_path / bot_name(STRICT_TARGET_V + 1)
    bot_dir.mkdir()
    (bot_dir / ".completed").write_text("publication_id=pub\n", encoding="utf-8")
    target_v = STRICT_TARGET_V + 1
    intent = {
        "version": target_v,
        "workflow_run_id": f"generation:{target_v}:workflow-v1",
        "completion_tag": bot_tag(target_v),
        "publication_id": "pub",
        "candidate_artifact_hash": "artifact",
        "official_certificate_digest": intent_cert,
    }
    checkpoint = {
        "stage": "publishing",
        "next_v": target_v,
        "workflow_run_id": intent["workflow_run_id"],
        "publication_intent": intent,
    }
    monkeypatch.setattr(
        publication_transaction,
        "publication_intent_structure_errors",
        lambda _intent: [],
    )
    monkeypatch.setattr(evolution_infra, "_git", lambda *_a, **_k: "a" * 40)
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
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _v: bot_dir)
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
        lambda *_a, **_k: SimpleNamespace(
            eligible=True,
            certificate_digest=spec_cert,
        ),
    )

    assert evolution_infra._publication_checkpoint_reconciliation_allowed(
        checkpoint,
        {"published_high_water": target_v},
    ) is True

    # A REAL digest on one side still mismatches a cert-less other side.
    mismatch = dict(intent)
    mismatch["official_certificate_digest"] = "d" * 64
    mismatched_ckpt = dict(checkpoint, publication_intent=mismatch)
    assert evolution_infra._publication_checkpoint_reconciliation_allowed(
        mismatched_ckpt,
        {"published_high_water": target_v},
    ) is False


@pytest.mark.parametrize("sentinel", ("wrong", "symlink"))
def test_publication_reconciliation_rejects_invalid_completed_sentinel(
    tmp_path,
    monkeypatch,
    sentinel,
):
    import evolution_infra
    import national_runtime_authority
    import publication_transaction

    bot_dir = tmp_path / bot_name(STRICT_TARGET_V + 1)
    bot_dir.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("publication_id=pub\n", encoding="utf-8")
    completed = bot_dir / ".completed"
    if sentinel == "wrong":
        completed.write_text("publication_id=other\n", encoding="utf-8")
    else:
        completed.symlink_to(outside)
    target_v = STRICT_TARGET_V + 1
    intent = {
        "version": target_v,
        "workflow_run_id": f"generation:{target_v}:workflow-v1",
        "completion_tag": bot_tag(target_v),
        "publication_id": "pub",
        "candidate_artifact_hash": "artifact",
        "official_certificate_digest": "d" * 64,
    }
    checkpoint = {
        "stage": "publishing",
        "next_v": target_v,
        "workflow_run_id": intent["workflow_run_id"],
        "publication_intent": intent,
    }
    monkeypatch.setattr(
        publication_transaction,
        "publication_intent_structure_errors",
        lambda _intent: [],
    )
    monkeypatch.setattr(evolution_infra, "_git", lambda *_a, **_k: "a" * 40)
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
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _v: bot_dir)
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
        lambda *_a, **_k: SimpleNamespace(
            eligible=True,
            certificate_digest="d" * 64,
        ),
    )

    assert evolution_infra._publication_checkpoint_reconciliation_allowed(
        checkpoint,
        {"published_high_water": target_v},
    ) is False


def test_resigned_schema2_extra_field_is_invalid_launch_barrier(tmp_path):
    import epoch_authority

    claim = _schema2_abandon_claim_for_status()
    claim["forged_phase"] = "checkpoint_cleared"
    unsigned = {key: value for key, value in claim.items() if key != "claim_digest"}
    claim["claim_digest"] = epoch_authority._claim_payload_digest(unsigned)
    path = tmp_path / "policy_epoch_reconciliation_claim.json"
    path.write_text(json.dumps(claim) + "\n", encoding="utf-8")

    status = epoch_authority._runtime_reconciliation_claim_status(path)

    assert status["claimed"] is True
    assert status["valid"] is False
    assert status["kind"] is None
    assert any("fields_invalid" in issue for issue in status["issues"])


def test_reconciliation_barrier_projects_no_active_bots(monkeypatch):
    import epoch_authority
    import evolution_infra

    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: STRICT_TARGET_V)
    monkeypatch.setattr(
        evolution_infra,
        "abandoned_version_authority",
        lambda **_kwargs: {"floor": 0, "head_digest": None},
    )
    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "runtime_reconciliation_in_progress",
            "initialized": False,
            "epoch_initialized": True,
            "strict_published": True,
            "strict_published_bots": [bot_name(STRICT_TARGET_V)],
            "strict_published_versions": [STRICT_TARGET_V],
            "namespace_publication_proven": True,
            "publication_recovery_ready": False,
            "unpaired_completion_versions": [],
            "unpaired_high_water_versions": [],
            "reset_receipt_valid": True,
            "reset_receipt_digest": "a" * 64,
            "reset_receipt_issues": [],
            "version_authority_high_water": STRICT_TARGET_V,
            "runtime_reconciliation_claimed": True,
            "runtime_reconciliation_claim_valid": False,
            "runtime_reconciliation_claim_issues": [
                "RuntimeError:recorded_abandon_claim_fields_invalid"
            ],
            "operator_action": "inspect_runtime_reconciliation_claim",
            "operator_command": None,
        },
    )

    projection = epoch_authority.strict_epoch_projection(include_checkpoint=False)

    assert projection["initialized"] is False
    assert projection["active_bots"] == []
    assert projection["active_bots_count"] == 0
    assert projection["active_generation"] is None
    assert projection["operator_action"] == "inspect_runtime_reconciliation_claim"
    assert projection["operator_command"] is None


def test_epoch_claim_reader_rejects_same_inode_same_size_rewrite(
    tmp_path,
    monkeypatch,
):
    import epoch_authority

    path = tmp_path / "claim.json"
    original = '{"value":1}\n'
    replacement = '{"value":2}\n'
    path.write_text(original, encoding="utf-8")
    real_read = epoch_authority.os.read
    injected = []

    def rewrite_after_read(descriptor, amount):
        raw = real_read(descriptor, amount)
        if not injected:
            injected.append(True)
            path.write_text(replacement, encoding="utf-8")
        return raw

    monkeypatch.setattr(epoch_authority.os, "read", rewrite_after_read)
    with pytest.raises(RuntimeError, match="claim_json_unsafe"):
        epoch_authority._read_bounded_regular_json(path)
    assert injected == [True]


def test_strict_epoch_projection_honors_draft_slot_override(tmp_path, monkeypatch):
    """A one-ahead draft prepare must not be refused via the primary checkpoint.

    Regression: ``strict_epoch_projection`` used the hard-coded
    ``PIPELINE_STATE_FILE`` constant for its checkpoint existence/claimed
    detection while reading via the override-aware ``read_pipeline_checkpoint()``.
    Inside the draft task's ``active_slot_override("draft")`` this split-brain
    saw the PRIMARY's parked checkpoint as "claimed but unreadable" and set
    ``ignored_checkpoint``, which ``prepare_generation`` treated as a hard
    refusal -- so the one-ahead draft never got to create its own draft-slot
    checkpoint.  The existence check must resolve through the same slot-aware
    path as the read.
    """
    import epoch_authority
    import evolution_infra as infra

    # Primary checkpoint exists on disk (parked at workers_done for the consumer).
    primary_state = tmp_path / "pipeline_state.json"
    primary_state.write_text(
        json.dumps(
            {
                "next_v": STRICT_TARGET_V + 1,
                "source_v": STRICT_TARGET_V,
                "stage": "workers_done",
            }
        ),
        encoding="utf-8",
    )
    # Draft slot checkpoint does NOT exist yet (this is the whole point: the
    # draft is about to create it).
    draft_state = tmp_path / "pipeline_state_draft.json"
    assert not draft_state.exists()

    monkeypatch.setattr(infra, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(infra, "PIPELINE_STATE_FILE", primary_state)

    # Canonical version-authority stubs (not under test here).
    _stub_namespace(monkeypatch, epoch_authority, infra)

    # The read returns None because the draft slot has no checkpoint.  Without
    # the fix, the primary's existence (via the hard-coded constant) would flip
    # checkpoint_claimed=True and set ignored_checkpoint.
    monkeypatch.setattr(infra, "read_pipeline_checkpoint", lambda: None)

    # Enter the draft slot override the way _draft_prepare_task does.
    with infra.active_slot_override("draft"):
        projection = epoch_authority.strict_epoch_projection()

    # The draft prepare must NOT be refused on the basis of the primary's
    # parked checkpoint.  No ignored_checkpoint, no operator archive action.
    assert projection.get("ignored_checkpoint") is None
    assert projection.get("operator_action") != "archive_incompatible_checkpoint"


def _stub_namespace(monkeypatch, epoch_authority, infra):
    """Minimal version-authority stubs shared by draft-override tests."""
    monkeypatch.setattr(
        infra,
        "find_current_v",
        lambda: STRICT_TARGET_V,
    )
    monkeypatch.setattr(
        infra,
        "find_max_committed_v",
        lambda: STRICT_TARGET_V,
    )
    monkeypatch.setattr(
        infra,
        "abandoned_version_authority",
        lambda **_kw: {"floor": STRICT_TARGET_V, "head_digest": "a" * 64, "receipt_count": 0},
    )

