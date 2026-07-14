import json


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
            "version_authority_high_water": 142,
            "first_strict_version": 143,
            "operator_action": "execute_policy_epoch_reset",
            "operator_command": epoch_authority.RESET_COMMAND,
        },
    )
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 142)
    monkeypatch.setattr(evolution_infra, "find_max_committed_v", lambda: 142)
    monkeypatch.setattr(evolution_infra, "find_abandoned_version_floor", lambda: 0)
    monkeypatch.setattr(evolution_infra, "get_active_bots_read_only", lambda: [])
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: {
            "next_v": 155,
            "source_v": 142,
            "stage": "direction_audited",
            "workflow_run_id": "generation:155:workflow-v1",
        },
    )

    projection = epoch_authority.strict_epoch_projection()

    assert projection["current_v"] == 142
    assert projection["next_v"] == 143
    assert projection["active_generation"] is None
    assert projection["ignored_checkpoint"]["next_v"] == 155
    assert projection["ignored_checkpoint"]["reason"] == (
        "checkpoint_not_bound_to_strict_epoch"
    )


def test_invalid_durable_reset_claim_requires_recovery_not_rerun(tmp_path, monkeypatch):
    import epoch_authority
    import evolution_infra

    (tmp_path / "policy_epoch_reset_receipt.json").write_text(
        json.dumps({"kind": "national_tcp_policy_epoch_reset_claim"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 142)

    state = epoch_authority.policy_epoch_initialization(results_dir=tmp_path)

    assert state["state"] == "reset_evidence_requires_recovery"
    assert state["initialized"] is False
    assert state["operator_action"] == "inspect_policy_epoch_reset_evidence"
    assert state["operator_command"] is None


def test_strict_tag_without_eligible_publication_requires_recovery(tmp_path, monkeypatch):
    import epoch_authority
    import evolution_infra
    import national_runtime_authority

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 155)
    monkeypatch.setattr(
        national_runtime_authority, "strict_published_bot_names", lambda: ()
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
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 143)
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda: ("national_v143",),
    )

    state = epoch_authority.policy_epoch_initialization(results_dir=tmp_path)

    assert state["state"] == "strict_published"
    assert state["initialized"] is True
    assert state["strict_published"] is True
    assert state["strict_published_bots"] == ["national_v143"]


def test_abandoned_floor_is_epoch_scoped(tmp_path, monkeypatch):
    import epoch_authority
    import evolution_infra

    abandoned = tmp_path / "abandoned_versions.jsonl"
    abandoned.write_text('{"v": 155}\n{"v": 167}\n', encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "ABANDONED_VERSIONS_FILE", abandoned)
    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {"initialized": False},
    )
    assert evolution_infra.find_abandoned_version_floor() == 0

    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {"initialized": True},
    )
    assert evolution_infra.find_abandoned_version_floor() == 167


def test_reset_command_contains_required_runtime_acknowledgement():
    from epoch_authority import RESET_COMMAND

    assert "--execute" in RESET_COMMAND
    assert "--acknowledge-runtime-checkout" in RESET_COMMAND


def test_first_strict_bootstrap_checkpoint_projects_exact_operator_command(monkeypatch):
    import checkpoint_schema
    import epoch_authority
    import evolution_infra

    monkeypatch.setattr(
        epoch_authority,
        "policy_epoch_initialization",
        lambda **_kwargs: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "fresh_bootstrap_ready",
            "initialized": True,
            "strict_published": False,
            "strict_published_bots": [],
            "reset_receipt_valid": True,
            "reset_receipt_digest": "a" * 64,
            "reset_receipt_issues": [],
            "version_authority_high_water": 142,
            "first_strict_version": 143,
            "operator_action": None,
            "operator_command": None,
        },
    )
    monkeypatch.setattr(evolution_infra, "find_current_v", lambda: 142)
    monkeypatch.setattr(evolution_infra, "find_max_committed_v", lambda: 142)
    monkeypatch.setattr(evolution_infra, "find_abandoned_version_floor", lambda: 0)
    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: {
            "next_v": 143,
            "source_v": 142,
            "stage": "official_bootstrap_required",
            "workflow_run_id": "generation:143:strict-test",
            "generation_attempt": 1,
        },
    )
    monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda *_a, **_k: [])
    monkeypatch.setattr(
        checkpoint_schema,
        "live_policy_epoch_reset_receipt_errors",
        lambda *_a, **_k: [],
    )

    projection = epoch_authority.strict_epoch_projection()

    assert projection["active_generation"]["stage"] == "official_bootstrap_required"
    assert projection["operator_action"] == "run_first_strict_official_certification"
    assert projection["operator_command"] == epoch_authority.FIRST_STRICT_BOOTSTRAP_COMMAND
    assert "bots/national_v143" in projection["operator_command"]
    assert "--acknowledge-one-time-first-strict-control" in projection["operator_command"]
